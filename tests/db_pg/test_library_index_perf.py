"""Perf regression: ``GET /library`` renders a bounded page for an instance
with many collections, each holding many files.

Before this fix, the index's per-collection loop called
``corpus_files_repo().list_for_corpus(corpus_id)`` — unbounded, ``SELECT *``
— for every non-empty collection the caller could see, and inlined every
row it returned as a child ``<tr>``. On a real large-corpus instance
(~390 collections, 216k ``corpus_files`` rows) that rendered a 1 GB, 56 s
page (incident, 2026-09-03). The fix (``app/web/router.py``,
``_LIBRARY_INLINE_FILES_CAP``) reads the per-collection file COUNT off the
batched ``count_by_corpus()`` projection and only fetches/renders a
collection's actual rows when its count is small; a collection over the cap
renders as a plain count-only card and its files are browsed at
``/library/{slug}`` (paginated) instead.

This test seeds a scaled-down but shape-equivalent dataset (50 collections
x 1000 files = 50k rows, comfortably over the cap) and asserts both halves
of the fix: the number of SQL statements the index issues does not grow
with the file count, and the HTML never mentions a seeded file's name.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from tests.db_pg._parity_sweep_util import build_seeded_client

REPO_ROOT = Path(__file__).resolve().parents[2]

N_COLLECTIONS = 50
N_FILES_PER_COLLECTION = 1000


def _seed_large_corpus(pg_engine) -> None:
    """50 collections x 1000 files each, owned by the admin — bulk SQL
    inserts (not the repo's one-row-at-a-time ``add()``) since this is test
    setup, not the code path under test."""
    import secrets

    corpora = [
        {
            "id": f"col_perf_{i}",
            "slug": f"perf-collection-{i}",
            "name": f"Perf Collection {i}",
            "description": None,
            "created_by": "admin1",
            "origin": "uploaded",
        }
        for i in range(N_COLLECTIONS)
    ]
    files = []
    for i in range(N_COLLECTIONS):
        for j in range(N_FILES_PER_COLLECTION):
            files.append(
                {
                    "id": f"cf_perf_{i}_{j}_{secrets.token_hex(4)}",
                    "corpus_id": f"col_perf_{i}",
                    "filename": f"secret-seeded-file-{i}-{j}.csv",
                    "sha256": f"sha_{i}_{j}",
                    "file_type": "csv",
                    "size_bytes": 100,
                    "storage_path": None,
                    "parent_file_id": None,
                    "path": None,
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
    _seed_large_corpus(pg_engine)

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
    # guards is the corpus_files half: before the fix, every one of the 50
    # non-empty collections cost its own unbounded
    # `SELECT * FROM corpus_files WHERE corpus_id = ...` (each returning
    # 1000 rows) — 50 extra, expensive statements that scaled with
    # collection count and whose RESULT SIZE scaled with file count. Every
    # collection seeded here is over `_LIBRARY_INLINE_FILES_CAP`, so the
    # fixed code issues none of them: file COUNTS come from one batched
    # `GROUP BY corpus_id`, never a per-collection fetch.
    per_collection_file_fetches = [s for s in statements if "FROM corpus_files WHERE corpus_id" in s]
    assert not per_collection_file_fetches, (
        f"found {len(per_collection_file_fetches)} per-collection corpus_files fetch(es) — "
        f"expected zero for collections over _LIBRARY_INLINE_FILES_CAP: {per_collection_file_fetches[:3]}"
    )
    batched_count_queries = [s for s in statements if "GROUP BY corpus_id" in s]
    assert len(batched_count_queries) == 1, (
        f"expected exactly one batched corpus_files count query, found {len(batched_count_queries)}"
    )
    # A generous ceiling on the page's TOTAL statement count: comfortably
    # above the real baseline (measured ~50 for this fixture) but well below
    # what a reintroduced per-collection scan would add (+50, one per
    # collection) — catches that regression without pinning an exact number
    # for the unrelated sections.
    assert len(statements) < 100, (
        f"GET /library issued {len(statements)} statements for {N_COLLECTIONS} collections x "
        f"{N_FILES_PER_COLLECTION} files — expected a bounded, file-count-independent number"
    )


def test_library_index_html_is_bounded_and_names_no_seeded_file(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_large_corpus(pg_engine)

    resp = client.get("/library", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Before the fix this page was ~1 GB (every one of the 50k seeded files
    # inlined as a child row). Bounded by collection count now, not file
    # count — well under 1 MB even at hundreds of collections.
    assert len(body.encode("utf-8")) < 1_000_000, f"/library HTML is {len(body)} bytes — expected well under 1 MB"

    # No individual file identity leaks into the index — that belongs to
    # /library/{slug}, which is paginated.
    assert "secret-seeded-file-" not in body
    # But every collection is still represented as a card, with its true
    # count.
    assert "Perf Collection 0" in body
    assert "1000 files" in body or "1,000 files" in body


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
