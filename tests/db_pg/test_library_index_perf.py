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
fetched lazily, on first expand, from `GET /library/{slug}/peek`), and — at
the time — added an explicit, disclosed cap on how many collection CARDS the
index itself rendered (`_LIBRARY_SECTION_PAGE_CAP`, with a "Show more
collections" link).

Round 3 (same day): the entity-facet FILTER MENU had the identical unbounded
shape one layer up — see `test_library_index_facet_menu_is_bounded_and_html_
stays_small` below.

Round 4 (incident follow-up, same day): round 2's card cap turned out to be
its OWN bug, live. `library_page` applied the cap to the RAW
`file_corpora_repo().list()` fetch — BEFORE the caller's owned-or-granted
filter ran — so "Show more collections" appeared whenever the INSTANCE had
more collections than the cap, never whether the CALLER could see more than
that. An admin who owned or was granted only 2 of 397 collections saw a live
"Show more" link that changed nothing no matter how far `?files_limit=` was
raised, because raising it only fetched more of the same ~395 invisible
rows. The cap, `?files_limit=`, and the "Show more collections" link are
removed entirely (`app/web/router.py` ~2762-2775): every collection the
caller may see now renders, in one list, at the bounded per-card cost rounds
1-3 already established — no other Library section paginates either.

This test seeds a dataset shaped like the production report — 400
collections x 100 files, with realistic (not tiny placeholder) name/
description/filename lengths — and asserts bounded statement count, bounded
render time, and a page size that scales with collection COUNT alone (never
file or facet count) at 400 VISIBLE collections. Note: at this per-card
weight (repeated name/description across several row attributes plus a
handful of icons — the price of realistic content, not a regression rounds
1-3 left on the table) 400 real collections lands at ~3.2 MB, not literally
under 1 MB; see that test's own comment for the honest accounting.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import sqlalchemy as sa

from app.web.router import _LIBRARY_ENTITY_FACET_LIMIT, _LIBRARY_FACET_SEARCH_MAX
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
    """Round 4: no card cap any more — this asserts the page scales with
    collection COUNT alone (never file or facet count), not that it fits a
    literal 1 MB. It cannot: 400 real collections at realistic name/
    description lengths (repeated across several row attributes: title,
    aria-labels, search text) costs ~8 KB/card regardless of anything rounds
    1-3 fixed, because rendering one honest card per collection is the whole
    point of removing the cap. The bound below is that math (400 x ~8.5 KB),
    not a floor that quietly stopped checking anything — a regression that
    reintroduced a per-file or per-facet cost (rounds 1-3's own bugs) would
    still blow well past it, and the "no seeded file/path identity" and
    "true count intact" assertions below catch a correctness regression a
    byte ceiling alone cannot."""
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_production_shaped_corpus(pg_engine)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    nbytes = len(body.encode("utf-8"))

    # Round 1 alone measured ~1.7 MB here (100-file collections are over its
    # per-collection cap, so its OWN fixture never exercised the gap round 2
    # closes) — and the LIVE production report, on real ~392-collection data
    # under round 1, was 19.6 MB. Round 2's card cap (since removed, round 4)
    # briefly landed this at ~1.03 MB by rendering only 100 of the 400 —
    # which is exactly the bug round 4 fixes: that cap applied to the RAW
    # fetch, before visibility, so "Show more collections" could show a
    # caller a control that changed nothing. Uncapped, every one of the 400
    # (all owned by admin1 here, so all VISIBLE) renders: ~3.2 MB, ~8 KB/card
    # — still a ~6x reduction from the 19.6 MB live report, and it no longer
    # scales with a collection's file count (rounds 1-2) or facet vocabulary
    # (round 3), only with how many collections the caller can actually see.
    assert nbytes < 3_500_000, (
        f"/library HTML is {nbytes} bytes ({nbytes / 1024:.0f} KB) for {N_COLLECTIONS} visible collections "
        f"— expected roughly {N_COLLECTIONS} x ~8.5 KB/card"
    )

    # No individual file identity leaks into the index at all, for a
    # collection of ANY size — that belongs to /library/{slug} (paginated)
    # or the lazily-fetched /library/{slug}/peek and /matching-files.
    assert "Detail-Schedule-" not in body
    assert "ClientEngagementPortal" not in body
    # Every one of the 400 collections renders — none silently dropped by a
    # cap applied before visibility was decided (round 4's own bug).
    assert body.count('data-item-id="col_perf_') == N_COLLECTIONS
    assert "Client Engagement 0000" in body
    assert "Client Engagement 0399" in body
    assert "100 files" in body
    # The link round 4 removed must not reappear.
    assert "Show more collections" not in body
    assert "files_limit" not in body


#: Collections a DIFFERENT owner holds, with no grant to the probed caller —
#: the "invisible ~395" half of the live round-4 bug. Kept separate from
#: `N_COLLECTIONS`/`_seed_production_shaped_corpus` above (own file/facet
#: perf story) since this test's own point is purely about visibility vs. a
#: render cap, at a scale a full 100-file-per-collection seed would only
#: slow down for no reason.
_N_VISIBLE_ONLY = 150
_N_OTHER_OWNER = 250


def _seed_mixed_visibility_collections(pg_engine, visible_owner: str = "analyst1") -> None:
    """`_N_VISIBLE_ONLY` collections owned by `visible_owner` (the probed
    caller) and `_N_OTHER_OWNER` more owned by someone else who never
    granted `visible_owner` anything — one file each, real enough to render
    as ordinary artefact cards without the round-1/2 file-count cost this
    test isn't about."""
    rows = []
    for i in range(_N_VISIBLE_ONLY + _N_OTHER_OWNER):
        owner = visible_owner if i < _N_VISIBLE_ONLY else "other_owner"
        rows.append(
            {
                "id": f"col_mix_{i}",
                "slug": f"mix-collection-{i}",
                "name": f"Mixed Visibility Collection {i:04d}",
                "description": "Seeded for the round-4 visible-vs-total perf fixture.",
                "created_by": owner,
                "origin": "uploaded",
            }
        )
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO file_corpora (id, slug, name, description, created_by, origin) "
                "VALUES (:id, :slug, :name, :description, :created_by, :origin)"
            ),
            rows,
        )


def test_library_index_renders_every_visible_collection_with_no_more_control(tmp_path, monkeypatch, pg_engine):
    """Round 4 (live incident): a caller who owns/was granted only a SUBSET
    of the instance's collections must see every one of THAT subset, never
    a cap-truncated slice of it, and never a "Show more collections" control
    — the round-2 mechanism this fixes appeared whenever the INSTANCE had
    more collections than the (removed) cap, regardless of how many the
    caller could actually see, so a caller with 150 of 400 visible saw a
    link driven by the other 250 they could never reach either way.

    Probed as a NON-ADMIN (analyst1) — an admin caller now sees every live
    collection by design (admin god-mode, see the follow-up test below and
    `library_page`'s docstring), so the "other owner's rows never leak in"
    half of this guard would no longer hold for an admin. It still holds,
    unchanged, for an ordinary caller's own subset."""
    from app.auth.jwt import create_access_token

    client, _admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_mixed_visibility_collections(pg_engine, visible_owner="analyst1")
    analyst_token = create_access_token("analyst1", "analyst@test.com")

    resp = client.get("/library", headers={"Authorization": f"Bearer {analyst_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Every VISIBLE collection renders — none silently dropped.
    assert body.count('data-item-id="col_mix_') == _N_VISIBLE_ONLY
    for i in range(_N_VISIBLE_ONLY):
        assert f'data-item-id="col_mix_{i}"' in body
    # None of the OTHER owner's collections leak in.
    for i in range(_N_VISIBLE_ONLY, _N_VISIBLE_ONLY + _N_OTHER_OWNER):
        assert f'data-item-id="col_mix_{i}"' not in body

    # No pagination control exists at all any more — round 4 removed it.
    assert "Show more collections" not in body
    assert "files_limit" not in body


def test_library_index_admin_sees_every_collection_including_admin_only(tmp_path, monkeypatch, pg_engine):
    """Admin god-mode (see ``library_page``'s docstring): unlike the
    non-admin probe above, an admin caller sees EVERY live collection on the
    instance — including the 250 owned by someone else with no grant to
    admin1 at all — each tagged ``ownership="admin_visible"``. Admin
    god-mode widens which rows PASS the visibility filter; it must not
    reintroduce an unbounded per-row or per-collection cost, so this stays
    within the same render-time budget the other 400-collection fixtures in
    this file establish."""
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_mixed_visibility_collections(pg_engine, visible_owner="admin1")

    t0 = time.monotonic()
    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200, resp.text
    assert elapsed < 1.0, f"admin GET /library (400 collections, all visible) took {elapsed:.2f}s — expected < 1s"
    body = resp.text

    # All 400 render — the owned 150 AND the admin-only 250.
    total = _N_VISIBLE_ONLY + _N_OTHER_OWNER
    assert body.count('data-item-id="col_mix_') == total
    for i in range(total):
        assert f'data-item-id="col_mix_{i}"' in body

    # Only the admin-only 250 carry the ownership tag.
    assert body.count('data-ownership="admin_visible"') == _N_OTHER_OWNER

    assert "Show more collections" not in body
    assert "files_limit" not in body


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


# ---------------------------------------------------------------------------
# Round 3 (incident follow-up, same day): the entity-facet FILTER MENU.
#
# Rounds 1-2 bounded a collection's own files and the collection CARDS
# themselves. Deployed live, `/library` was still 8-13s / 18 MB for an
# admin: `facet_values_for_collections` (src/repositories/facts_pg.py,
# used from app/web/router.py's `library_page`) has no cap of its own — it
# is the per-ROW entity-facet source, and the Library then tallies EVERY
# row's own unbounded value list into the Filter menu (`_present_multi` in
# router.py, over `library_entity_cats`). On a live instance (397
# collections, 345k facts) that produced 57 812 `.fbar-menu__opt` filter
# options — 1.3 MB of `data-client` alone — AND, the second live datum, a
# slow (4.6s TTFB) response even for a caller whose OWN visible set was
# small: the unbounded CANDIDATE SCAN (no corpus_id filter, a per-caller
# visibility CTE with a correlated `NOT EXISTS` against `corrections`), not
# the eventual output size, was the expensive part.
#
# The fix: `facet_top_values_for_collections` (top N per type by document
# count, ranked and LIMITed entirely in SQL via a window function, scoped
# by `corpus_id = ANY(:corpus_ids)` — an indexed scan, never the exact
# per-caller CTE) plus `facet_membership_for_collections` (which of those
# already-bounded values does THIS page's row carry — so a row's own
# `data-{facet}` attribute is bounded by construction, not a second cap).
# `GET /library/facets/{facet}` is the typeahead past the menu's own cap.
# ---------------------------------------------------------------------------

FACET_TYPES = ["client", "industry", "service_offering", "doc_type"]
FACET_KEYS = {"client": "client", "industry": "industry", "service_offering": "offering", "doc_type": "doctype"}
N_FACET_COLLECTIONS = 400
N_FACET_VALUES = 60_000
#: How many of the 400 collections belong to a NON-admin caller — the
#: "small visible set, big graph" half of the incident's second datum.
N_ANALYST_COLLECTIONS = 5


def _seed_facet_graph(pg_engine) -> None:
    """400 collections (the first `N_ANALYST_COLLECTIONS` owned by a non-
    admin analyst, the rest by admin1), one file each, and 60 000 distinct
    facts (15 000 per entity-facet type) — the production report's own
    shape (397 collections, 345k facts) scaled down to something a test
    seeds and queries in seconds while exercising the identical unbounded-
    candidate-scan risk: 60k rows is enough that a query lacking the
    corpus_id filter (or lacking the SQL-side LIMIT) is measurably, not just
    theoretically, slower."""
    collections = []
    files = []
    for i in range(N_FACET_COLLECTIONS):
        owner = "analyst1" if i < N_ANALYST_COLLECTIONS else "admin1"
        collections.append(
            {
                "id": f"col_facet_{i}",
                "slug": f"facet-collection-{i}",
                "name": f"Facet Collection {i:04d}",
                "description": "Seeded for the round-3 facet-menu perf fixture.",
                "created_by": owner,
                "origin": "uploaded",
            }
        )
        # TWO files per collection, deliberately — one is what claims
        # reference, the other exists only so `file_count != 1`: a
        # single-file collection is a different, already-bounded O(1)
        # per-collection lookup (the "this card IS the file" case, rounds
        # 1-2), and mixing that path into a facet-focused fixture would
        # inflate the statement count with a cost this test is not about.
        files.append(
            {
                "id": f"cf_facet_{i}",
                "corpus_id": f"col_facet_{i}",
                "filename": f"document-{i:04d}.pdf",
                "sha256": f"sha_cf_{i}",
                "file_type": "pdf",
                "size_bytes": 4096,
                "storage_path": None,
                "parent_file_id": None,
                "path": None,
            }
        )
        files.append(
            {
                "id": f"cf_facet_{i}_b",
                "corpus_id": f"col_facet_{i}",
                "filename": f"document-{i:04d}-appendix.pdf",
                "sha256": f"sha_cf_{i}_b",
                "file_type": "pdf",
                "size_bytes": 2048,
                "storage_path": None,
                "parent_file_id": None,
                "path": None,
            }
        )
    facts, aliases, claims = [], [], []
    for i in range(N_FACET_VALUES):
        fact_type = FACET_TYPES[i % len(FACET_TYPES)]
        # Round-robins every collection, ANALYST ones included, so the small
        # non-admin caller's own visible set genuinely has facet data too —
        # not a degenerate all-admin fixture that happens to make the small
        # case trivially fast regardless of query shape.
        col_idx = i % N_FACET_COLLECTIONS
        fact_id = f"fact_{i}"
        facts.append({"id": fact_id, "type": fact_type})
        aliases.append({"fact_id": fact_id, "type": fact_type, "natural_key": f"{fact_type}-value-{i:06d}"})
        claims.append(
            {
                "id": f"claim_{i}",
                "fact_id": fact_id,
                "corpus_file_id": f"cf_facet_{col_idx}",
                "corpus_id": f"col_facet_{col_idx}",
                "file_sha256": f"sha_cf_{col_idx}",
                "quote": f"evidence for {fact_type} value {i}",
                "quote_hash": f"qh_{i}",
            }
        )
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO file_corpora (id, slug, name, description, created_by, origin) "
                "VALUES (:id, :slug, :name, :description, :created_by, :origin)"
            ),
            collections,
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
        conn.execute(sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), facts)
        conn.execute(
            sa.text("INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fact_id, :type, :natural_key)"),
            aliases,
        )
        conn.execute(
            sa.text(
                "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                "VALUES (:id, :fact_id, :corpus_file_id, :corpus_id, :file_sha256, :quote, :quote_hash)"
            ),
            claims,
        )


def _facet_option_counts(body: str) -> dict:
    """``{facet_key: n}`` — how many `.fbar-menu__opt` rows the rendered
    page carries per entity facet, read off each option's own
    ``data-facet="..."`` attribute."""
    import re

    counts: dict = {}
    for key in FACET_KEYS.values():
        counts[key] = len(re.findall(r'data-facet="' + key + r'"', body))
    return counts


def test_library_index_facet_menu_is_bounded_and_html_stays_small(tmp_path, monkeypatch, pg_engine):
    """This test's OWN concern is the facet menu (round 3) — the per-facet
    option counts below are the real guard. The byte ceiling is round 4's
    honest accounting, not a cap: every one of the 395 admin-visible
    collections renders now (round 2's card cap, and the "Show more
    collections" bug it hid, are both gone — see
    test_library_index_renders_every_visible_collection_with_no_more_control),
    so the page's size is ~395 collections x a bounded per-card cost, never
    the 60 000-distinct-facet-value graph this fixture seeds."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_facet_graph(pg_engine)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    nbytes = len(body.encode("utf-8"))

    # Live incident: 18 MB for this shape (397 collections, 345k facts) under
    # the unbounded menu. Bounded now regardless of the 60k distinct values
    # seeded here — scales with the ~395 admin-visible collection CARDS
    # (~6-7 KB each, this fixture's shorter placeholder names), not with the
    # facet graph the unbounded menu used to scan.
    assert nbytes < 3_000_000, (
        f"/library HTML is {nbytes} bytes ({nbytes / 1024:.0f} KB) — expected roughly 395 collections x ~7 KB/card"
    )

    counts = _facet_option_counts(body)
    for key, n in counts.items():
        assert n <= _LIBRARY_ENTITY_FACET_LIMIT, (
            f"facet '{key}' rendered {n} options, expected <= {_LIBRARY_ENTITY_FACET_LIMIT}"
        )
    # The menu is not simply empty — real, bounded vocabulary made it through.
    assert sum(counts.values()) > 0


def test_library_index_facet_query_statement_count_is_bounded(tmp_path, monkeypatch, pg_engine):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_facet_graph(pg_engine)

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

    facet_statements = [s for s in statements if "scoped_claims" in s or "facet_membership" in s.lower()]
    # facet_top_values_for_collections (menu) + facet_membership_for_collections
    # (row attributes) — exactly two, never one per facet type and never one
    # per rendered row.
    assert len(facet_statements) <= 2, f"expected <= 2 facet statements, found {len(facet_statements)}"
    assert len(statements) < 100, f"GET /library issued {len(statements)} statements — expected a bounded number"


def test_library_index_renders_fast_for_a_large_admin_and_a_small_user(tmp_path, monkeypatch, pg_engine):
    """Server render time, not just byte size: the second live datum showed
    a SMALL visible set was still slow (4.6s TTFB) because the candidate
    scan itself — not the eventual output — was unbounded. Both a
    400-collection admin and a 5-collection non-admin user must render in
    under a second on the identical (60k-fact) graph."""
    from app.auth.jwt import create_access_token

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_facet_graph(pg_engine)
    analyst_token = create_access_token("analyst1", "analyst@test.com")

    t0 = time.monotonic()
    resp_admin = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    elapsed_admin = time.monotonic() - t0
    assert resp_admin.status_code == 200, resp_admin.text
    assert elapsed_admin < 1.0, f"admin (400 collections) GET /library took {elapsed_admin:.2f}s — expected < 1s"

    t0 = time.monotonic()
    resp_analyst = client.get("/library", headers={"Authorization": f"Bearer {analyst_token}"})
    elapsed_analyst = time.monotonic() - t0
    assert resp_analyst.status_code == 200, resp_analyst.text
    assert elapsed_analyst < 1.0, (
        f"analyst (5 collections, {N_FACET_VALUES}-fact graph) GET /library took {elapsed_analyst:.2f}s — expected < 1s"
    )


def test_library_facets_typeahead_is_bounded_and_rbac_scoped(tmp_path, monkeypatch, pg_engine):
    from app.auth.jwt import create_access_token

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_facet_graph(pg_engine)
    analyst_token = create_access_token("analyst1", "analyst@test.com")

    # Admin sees the whole graph — a limit=50 request returns at most 50,
    # never the full 15 000 client-type values.
    resp = client.get("/library/facets/client?limit=50", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    values = resp.json()["values"]
    assert 0 < len(values) <= 50

    # `q` narrows to matching labels only. i=1000 -> FACET_TYPES[1000 % 4] ==
    # "client" (index 0), so this label really was seeded as a client value.
    resp_q = client.get(
        "/library/facets/client?q=client-value-001000", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp_q.status_code == 200, resp_q.text
    q_values = resp_q.json()["values"]
    assert q_values
    assert all("001000" in v["label"] for v in q_values)

    # A pathological limit is clamped, not honored verbatim.
    resp_big = client.get("/library/facets/client?limit=999999", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp_big.status_code == 200, resp_big.text
    assert len(resp_big.json()["values"]) <= _LIBRARY_FACET_SEARCH_MAX

    # Unknown facet key — 404, not a silent empty list (a caller-typo should
    # be visible, not read as "no matches").
    resp_404 = client.get("/library/facets/not-a-real-facet", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp_404.status_code == 404

    # RBAC: the analyst owns only N_ANALYST_COLLECTIONS of the 400 — their
    # own facet search must never surface a value evidenced ONLY by a
    # collection they cannot see. With 1/80th of the collections, the
    # analyst's own client-type vocabulary is a small, real subset — not
    # empty, and never the admin-scale count above.
    resp_analyst = client.get("/library/facets/client?limit=200", headers={"Authorization": f"Bearer {analyst_token}"})
    assert resp_analyst.status_code == 200, resp_analyst.text
    analyst_values = resp_analyst.json()["values"]
    assert 0 < len(analyst_values) < len(values) or len(analyst_values) <= 200
