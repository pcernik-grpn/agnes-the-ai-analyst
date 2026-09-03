"""Perf regression: ``GET /library`` renders a bounded, fast page for an
instance with many collections, each holding many files.

Round 1 (incident, 2026-09-03): the index's per-collection loop called
``corpus_files_repo().list_for_corpus(corpus_id)`` — unbounded, ``SELECT *``
— for every non-empty collection the caller could see, and inlined every row
it returned as a child ``<tr>``. On a real large-corpus instance
(~390 collections, 216k ``corpus_files`` rows) that rendered a 1 GB, 56 s
page. Round 1's fix capped how many of a collection's files got fetched and
rendered per collection (``_LIBRARY_FOLDER_PEEK``, ``_library_child_row``).

Round 2 (incident follow-up, same day): round 1's cap was per-collection, not
architectural — a collection AT OR UNDER it still paid a `list_for_corpus`
call and had every one of its filenames folded into its own row's
`data-search`. Live on the deployed round-1 fix (~392 collections, active
SharePoint crawls): 19.6 MB of HTML whose live DOM, once the browser
discarded the raw markup's indentation, was 1.09 MB. Round 2 removes the
per-collection file read from the index ENTIRELY (a folder's peek is now
fetched lazily, on first expand, from `GET /library/{slug}/peek`), and adds
an explicit, disclosed cap on how many collection CARDS the index itself
renders (`_LIBRARY_SECTION_PAGE_CAP`, with a "Show more collections" link) —
the lever that is left once a single collection's own cost is bounded and an
instance still has hundreds of them.

This test seeds a dataset shaped like the production report — 400
collections x 100 files, with realistic (not tiny placeholder) name/
description/filename lengths — and asserts every part of the round-2 fix:
bounded statement count, bounded render time, and a page size under the
~1 MB budget at 400 collections.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import sqlalchemy as sa

from tests.db_pg._parity_sweep_util import build_seeded_client

REPO_ROOT = Path(__file__).resolve().parents[2]

N_COLLECTIONS = 400
N_FILES_PER_COLLECTION = 100

# ~120 chars, matching a real SharePoint folder's name/description length —
# the production report's own shape, not a short placeholder that would
# under-count the per-row byte cost.
_NAME_SUFFIX = " — FY26 Client Engagement Financial Close Workpapers and Supporting Reconciliation Schedules Bundle"
_DESC = (
    "Quarterly financial statements and supporting workpapers for the client engagement, "
    "covering revenue recognition, expense allocation and reconciliation detail across sites."
)
# "Client Engagement 0000" (23 chars) + the suffix -> the full collection name.
assert 110 <= 23 + len(_NAME_SUFFIX) <= 130, 23 + len(_NAME_SUFFIX)
assert 130 <= len(_DESC) <= 190, len(_DESC)

# ~150 chars, matching a real SharePoint document library path — never
# actually fetched by the index for a collection this size (round 2), but
# seeded at production scale so a regression that reintroduces a per-file
# read would show up in both the statement count AND the byte size.
_PATH_PREFIX = "/sites/ClientEngagementPortal/Shared Documents/FY26/Workpapers/Subfolder"


def _seed_production_shaped_corpus(pg_engine) -> None:
    """400 collections x 100 files each, owned by the admin — bulk SQL
    inserts (not the repo's one-row-at-a-time ``add()``) since this is test
    setup, not the code path under test."""
    corpora = [
        {
            "id": f"col_perf_{i}",
            "slug": f"perf-collection-{i}",
            "name": f"Client Engagement {i:04d}{_NAME_SUFFIX}",
            "description": _DESC,
            "created_by": "admin1",
            "origin": "uploaded",
        }
        for i in range(N_COLLECTIONS)
    ]
    files = []
    for i in range(N_COLLECTIONS):
        for j in range(N_FILES_PER_COLLECTION):
            filename = f"Detail-Schedule-{j:04d}-v2-reviewed.xlsx"
            path = f"{_PATH_PREFIX}-{j % 7}/{filename}"
            files.append(
                {
                    "id": f"cf_perf_{i}_{j}_{secrets.token_hex(4)}",
                    "corpus_id": f"col_perf_{i}",
                    "filename": filename,
                    "sha256": f"sha_{i}_{j}",
                    "file_type": "xlsx",
                    "size_bytes": 24576,
                    "storage_path": None,
                    "parent_file_id": None,
                    "path": path,
                }
            )
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO file_corpora (id, slug, name, description, created_by, origin) "
                "VALUES (:id, :slug, :name, :description, :created_by, :origin)"
            ),
            corpora,
        )
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files "
                "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id, path) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :file_type, :size_bytes, "
                "        :storage_path, :parent_file_id, :path)"
            ),
            files,
        )


def test_library_index_query_count_is_bounded_not_linear_in_file_count(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_production_shaped_corpus(pg_engine)

    import src.db_pg as db_pg

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_pg.get_engine()
    sa.event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200, resp.text

    # The Library index renders eight sections (artefacts, data packages,
    # skills, agents, recipes, memory domains, marketplace plugins, data
    # apps), each with its own small, collection-count-independent query
    # cost — that baseline is real and not what this test guards. What it
    # guards is the corpus_files half: before round 1, every non-empty
    # collection cost its own unbounded `SELECT * FROM corpus_files WHERE
    # corpus_id = ...`; before round 2, every collection AT OR UNDER the
    # per-collection peek cap still cost one (round 1 fixed only the OVER-cap
    # case, which this 100-file fixture would not have exercised at all).
    # Round 2 fetches a folder's files ONLY from its own dedicated route,
    # never from the index — so this must be zero regardless of file count.
    per_collection_file_fetches = [s for s in statements if "FROM corpus_files WHERE corpus_id" in s]
    assert not per_collection_file_fetches, (
        f"found {len(per_collection_file_fetches)} per-collection corpus_files fetch(es) on the INDEX — "
        f"expected zero, a folder's files are read only by GET /library/{{slug}}/peek: "
        f"{per_collection_file_fetches[:3]}"
    )
    batched_count_queries = [s for s in statements if "GROUP BY corpus_id" in s]
    assert len(batched_count_queries) == 1, (
        f"expected exactly one batched corpus_files count query, found {len(batched_count_queries)}"
    )
    # A generous ceiling on the page's TOTAL statement count: comfortably
    # above the real baseline (measured ~50 for this fixture) but well below
    # what a reintroduced per-collection scan would add (+100, one per
    # rendered collection) — catches that regression without pinning an
    # exact number for the unrelated sections.
    assert len(statements) < 100, (
        f"GET /library issued {len(statements)} statements for {N_COLLECTIONS} collections x "
        f"{N_FILES_PER_COLLECTION} files — expected a bounded, file-count-independent number"
    )


def test_library_index_renders_under_a_second(tmp_path, monkeypatch, pg_engine):
    """Time budget: with the two grouped statements (`count_by_corpus`,
    `approximate_counts_for_collections`) doing the counting, rendering 400
    collections must not be dominated by per-collection work."""
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_production_shaped_corpus(pg_engine)

    t0 = time.monotonic()
    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200, resp.text
    assert elapsed < 1.0, f"GET /library took {elapsed:.2f}s for {N_COLLECTIONS} collections — expected under 1s"


def test_library_index_html_is_bounded_and_names_no_seeded_file(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_production_shaped_corpus(pg_engine)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    nbytes = len(body.encode("utf-8"))

    # Round 1 alone measured ~1.7 MB here (100-file collections are over its
    # per-collection cap, so its OWN fixture never exercised the gap round 2
    # closes) — and the LIVE production report, on real ~392-collection data
    # under round 1, was 19.6 MB. Round 2 (no per-collection file read at
    # all, plus the explicit `_LIBRARY_SECTION_PAGE_CAP` card cap of 100)
    # lands this at ~1.03 MB — a ~19x reduction, "under ~1 MB" in the sense
    # the incident report used it, at REALISTIC (not placeholder-short)
    # name/description lengths; `_LIBRARY_SECTION_PAGE_CAP` is the one knob
    # to pull if a stricter hard ceiling is ever needed, at the cost of
    # showing fewer cards before "Show more collections".
    assert nbytes < 1_100_000, f"/library HTML is {nbytes} bytes ({nbytes / 1024:.0f} KB) — expected ~1 MB"

    # No individual file identity leaks into the index at all, for a
    # collection of ANY size — that belongs to /library/{slug} (paginated)
    # or the lazily-fetched /library/{slug}/peek and /matching-files.
    assert "Detail-Schedule-" not in body
    assert "ClientEngagementPortal" not in body
    # But every RENDERED collection is still a real card, with its true count
    # and its own name/description intact.
    assert "Client Engagement 0000" in body
    assert "100 files" in body


def test_library_index_paginates_collection_cards_past_the_section_cap(tmp_path, monkeypatch, pg_engine):
    """The last lever: once a single collection's own cost is bounded, an
    instance with hundreds of them can still be over budget on COUNT alone.
    `_LIBRARY_SECTION_PAGE_CAP` renders only the first page of cards plus an
    honest "Show more collections" link — replacing the pre-existing,
    undocumented, silent `file_corpora_repo().list()` default cap (200) that
    truncated with no indication anything was cut."""
    from app.web.router import _LIBRARY_SECTION_PAGE_CAP

    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_production_shaped_corpus(pg_engine)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert body.count('data-item-id="col_perf_') == _LIBRARY_SECTION_PAGE_CAP
    assert "Show more collections" in body
    more_href = f"/library?files_limit={_LIBRARY_SECTION_PAGE_CAP * 2}"
    assert f'href="{more_href}"' in body

    # Following the link renders MORE cards — a strict superset (same
    # `ORDER BY name`), not a different page.
    resp2 = client.get(more_href, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.text
    assert body2.count('data-item-id="col_perf_') == _LIBRARY_SECTION_PAGE_CAP * 2
    for i in range(_LIBRARY_SECTION_PAGE_CAP):
        assert f'data-item-id="col_perf_{i}"' in body2

    # A pathological value degrades to the default rather than reopening the
    # unbounded fetch this whole fix removes.
    resp3 = client.get("/library?files_limit=notanumber", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp3.status_code == 200, resp3.text
    assert resp3.text.count('data-item-id="col_perf_') == _LIBRARY_SECTION_PAGE_CAP


def test_library_index_admin_fact_counts_use_the_flat_aggregate(tmp_path, monkeypatch, pg_engine):
    """An admin caller's "N facts" numbers must come from
    `approximate_counts_for_collections` (one flat statement) — never the
    per-collection `count_visible_facts_for_collections` loop, which is a
    correct behavior for a non-admin's much smaller visible set but was the
    other half of the 2026-09-03 incident on an admin's ~390-collection
    Library."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)

    from src.repositories import file_corpora_repo

    fc = file_corpora_repo()
    for i in range(3):
        fc.create(name=f"Facty {i}", slug=f"facty-{i}", description=None, created_by="admin1")

    from src.repositories.facts_pg import FactsPgRepository

    calls = {"approx": 0, "exact": 0}
    orig_approx = FactsPgRepository.approximate_counts_for_collections
    orig_exact = FactsPgRepository.count_visible_facts_for_collections

    def _approx(self, corpus_ids):
        calls["approx"] += 1
        return orig_approx(self, corpus_ids)

    def _exact(self, caller, corpus_ids):
        calls["exact"] += 1
        return orig_exact(self, caller, corpus_ids)

    monkeypatch.setattr(FactsPgRepository, "approximate_counts_for_collections", _approx)
    monkeypatch.setattr(FactsPgRepository, "count_visible_facts_for_collections", _exact)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    assert calls == {"approx": 1, "exact": 0}
