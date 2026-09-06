"""Perf regression: ``GET /library/{slug}`` (the collection detail page)
renders a bounded Facts section for a collection with many claims —
TCRD-296 gap #78, the detail-page counterpart to
``test_library_index_perf.py``'s round 1-4 guards on the INDEX.

Production measurement, 2026-09-04: a collection with 282k files / 14.3M
chunks / 2.4M claims / 552k facts cost ~5.9s server-side per detail render
(``?page=2`` of the Files section cost the same again), almost entirely
``collection_facts_summary`` re-deriving the SAME per-caller candidate set
FOUR times per call (type breakdown, page, two review-item candidate
queries) — see ``src/repositories/facts_pg.py::collection_facts_summary``'s
own docstring for the fix: one combined statement sharing a single
``visible``/``candidates`` CTE, ``approximate_counts_for_collections`` for
an admin's total, and a short-TTL cache (``app/web/router.py``,
``_cached_collection_facts_summary``) so the Files section's OWN pager
(``?files_page=``) never re-triggers the facts query at all.
"""

from __future__ import annotations

import sqlalchemy as sa

from tests.db_pg._parity_sweep_util import build_seeded_client
from tests.db_pg.test_facts_read_pg import (
    _make_group_with_grant,
    _seed_collection,
    _seed_corpus_file,
    _seed_uploader,
)

N_FACTS = 5000
#: > `_FILES_SECTION_PAGE_SIZE` (25, `app/web/router.py`) so the Files
#: section genuinely has a second page for the cache test below.
N_EXTRA_FILES = 30


def _seed_many_claims_collection(pg_engine, *, collection_id: str, owner: str) -> None:
    """One collection, one claim-carrying file, `N_FACTS` facts each with
    one claim on that file, plus `N_EXTRA_FILES` claim-free files so the
    Files section pages past `?files_page=1`. Bulk SQL (not the repo's
    one-row-at-a-time write path) since this is test setup, matching
    `test_library_index_perf.py::_seed_facet_graph`'s own pattern."""
    _seed_uploader(owner)
    _seed_collection(collection_id=collection_id, created_by=owner)
    _seed_corpus_file(corpus_id=collection_id, file_id=f"{collection_id}_cf0")

    extra_files = [
        {
            "id": f"{collection_id}_cf_extra_{i}",
            "corpus_id": collection_id,
            "filename": f"extra-{i}.md",
            "sha256": f"sha_extra_{collection_id}_{i}",
        }
        for i in range(N_EXTRA_FILES)
    ]
    facts = [{"id": f"{collection_id}_fact_{i}", "type": "engagement"} for i in range(N_FACTS)]
    claims = [
        {
            "id": f"{collection_id}_claim_{i}",
            "fact_id": f"{collection_id}_fact_{i}",
            "corpus_file_id": f"{collection_id}_cf0",
            "corpus_id": collection_id,
            "file_sha256": "sha1",
            "quote": f"evidence {i}",
            "quote_hash": f"qh_{collection_id}_{i}",
        }
        for i in range(N_FACTS)
    ]
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256) "
                "VALUES (:id, :corpus_id, :filename, :sha256)"
            ),
            extra_files,
        )
        conn.execute(sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), facts)
        conn.execute(
            sa.text(
                "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                "VALUES (:id, :fact_id, :corpus_file_id, :corpus_id, :file_sha256, :quote, :quote_hash)"
            ),
            claims,
        )


def _capture_statements(engine):
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    return statements, _capture


def test_library_detail_admin_skips_exact_visibility_cte(tmp_path, monkeypatch, pg_engine):
    """Admin render: `collection_facts_summary` must never run the exact,
    audience-gated per-caller CTE branch — `endpoint_claims` is the SQL
    fragment `_visible_facts_for_corpus_cte` only ever emits OUTSIDE its
    `is_admin and not all_evidence` fast path (see that method's own
    docstring), so its absence is a precise proxy for "the membership-
    indexed admin fast path ran, not the exact one". The combined
    statement (type counts / page / both review-item candidate sets,
    sharing ONE `visible` CTE) must also run exactly once — never the
    pre-fix four separate round trips."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    from app.web.router import _reset_facts_summary_cache

    _reset_facts_summary_cache()
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_many_claims_collection(pg_engine, collection_id="col_detail_admin", owner="uploader1")

    import src.db_pg as db_pg

    engine = db_pg.get_engine()
    statements, capture = _capture_statements(engine)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get("/library/col_detail_admin", headers={"Authorization": f"Bearer {admin_token}"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text

    exact_cte_statements = [s for s in statements if "endpoint_claims" in s]
    assert not exact_cte_statements, (
        f"admin render ran the exact per-caller visibility CTE {len(exact_cte_statements)} time(s), expected 0"
    )
    combined_statements = [s for s in statements if "sv_candidates_cte" in s]
    assert len(combined_statements) == 1, (
        f"expected collection_facts_summary's combined statement exactly once, found {len(combined_statements)}"
    )


def test_library_detail_admin_type_counts_skip_facts_join_when_maintained(tmp_path, monkeypatch, pg_engine):
    """TCRD-296 gap #81 — the type-counts fast path's own regression. Once
    `fact_collection_type_counts` is populated, the admin render must not
    run the facts-joining `GROUP BY f.type` aggregate at all: production
    measurement, 2026-09-05, 4.5-4.8s on a 291k-file/801k-fact collection —
    a `Parallel Seq Scan` of the whole `facts` table hash-joined to
    `fact_collection_membership` to produce a 27-row breakdown. Must fail
    BEFORE the fast path exists (the pre-fix code always ran this
    aggregate, regardless of whether a maintained table existed)."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    from app.web.router import _reset_facts_summary_cache

    _reset_facts_summary_cache()
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_many_claims_collection(pg_engine, collection_id="col_detail_typecounts", owner="uploader1")

    from src.repositories import facts_repo

    facts_repo().rebuild_collection_stats(corpus_ids=["col_detail_typecounts"])

    import src.db_pg as db_pg

    engine = db_pg.get_engine()
    statements, capture = _capture_statements(engine)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get("/library/col_detail_typecounts", headers={"Authorization": f"Bearer {admin_token}"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text

    type_join_statements = [s for s in statements if "GROUP BY f.type" in s]
    assert not type_join_statements, (
        f"admin render ran the facts-joining type_counts aggregate {len(type_join_statements)} time(s) "
        f"even though fact_collection_type_counts is populated"
    )
    # And the numbers must still be right.
    assert f"{N_FACTS} engagements" in resp.text


def test_library_detail_non_admin_runs_exact_cte_at_most_once(tmp_path, monkeypatch, pg_engine):
    """Non-admin render: the exact per-caller CTE branch (`endpoint_claims`)
    legitimately runs — RBAC narrowing genuinely needs it — but at most
    ONCE per render, not once per sub-query the way it used to."""
    from app.auth.jwt import create_access_token
    from app.web.router import _reset_facts_summary_cache

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    _reset_facts_summary_cache()
    client, _admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_many_claims_collection(pg_engine, collection_id="col_detail_nonadmin", owner="uploader1")
    _make_group_with_grant(
        pg_engine,
        group_name="group-detail-nonadmin",
        collection_id="col_detail_nonadmin",
        member_user_id="analyst1",
    )
    analyst_token = create_access_token("analyst1", "analyst@test.com")

    import src.db_pg as db_pg

    engine = db_pg.get_engine()
    statements, capture = _capture_statements(engine)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get("/library/col_detail_nonadmin", headers={"Authorization": f"Bearer {analyst_token}"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text

    exact_cte_statements = [s for s in statements if "endpoint_claims" in s]
    assert len(exact_cte_statements) <= 1, (
        f"non-admin render ran the exact per-caller visibility CTE {len(exact_cte_statements)} times, expected <= 1"
    )


def test_library_detail_files_pagination_is_cache_hit_no_graph_statements(tmp_path, monkeypatch, pg_engine):
    """`?files_page=2` must not touch the fact graph at all once the Facts
    section's own cache is warm (`_cached_collection_facts_summary`) — the
    Files pager re-renders the WHOLE route without ever changing
    `facts_page`, so a cold re-fetch on every click was the other half of
    the production regression."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    from app.web.router import _reset_facts_summary_cache

    _reset_facts_summary_cache()
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_many_claims_collection(pg_engine, collection_id="col_detail_cache", owner="uploader1")

    warm = client.get("/library/col_detail_cache", headers={"Authorization": f"Bearer {admin_token}"})
    assert warm.status_code == 200, warm.text

    import src.db_pg as db_pg

    engine = db_pg.get_engine()
    statements, capture = _capture_statements(engine)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get("/library/col_detail_cache?files_page=2", headers={"Authorization": f"Bearer {admin_token}"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text

    graph_statements = [
        s for s in statements if "sv_candidates_cte" in s or "endpoint_claims" in s or "fact_collection_stats" in s
    ]
    assert not graph_statements, (
        f"?files_page=2 with a warm facts cache ran {len(graph_statements)} graph statement(s), expected 0: "
        f"{graph_statements[:2]}"
    )


def test_library_detail_non_admin_parity_on_small_fixture(tmp_path, monkeypatch, pg_engine):
    """A small fixture, still exercised through the exact (non-admin) CTE
    path — output parity with the pre-refactor behavior already proven at
    the repo layer (``tests/db_pg/test_facts_read_pg.py``), now asserted at
    the HTTP layer since the route goes through the TTL cache and the
    combined statement."""
    from app.auth.jwt import create_access_token
    from app.web.router import _reset_facts_summary_cache

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "true")
    _reset_facts_summary_cache()
    client, _admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_uploader("uploader1")
    _seed_collection(collection_id="col_detail_small", created_by="uploader1")
    _seed_corpus_file(corpus_id="col_detail_small", file_id="cf_small_1")
    _make_group_with_grant(
        pg_engine,
        group_name="group-detail-small",
        collection_id="col_detail_small",
        member_user_id="analyst1",
    )

    import src.db_pg as db_pg
    from src.repositories.facts_pg import FactsPgRepository

    repo = FactsPgRepository(db_pg.get_engine())
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(
        fact_id=fact_id, type="engagement", natural_key="engagement:acme-renewal", corpus_id="col_detail_small"
    )
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_small_1",
        corpus_id="col_detail_small",
        file_sha256="sha1",
        quote="Renewal signed.",
    )

    analyst_token = create_access_token("analyst1", "analyst@test.com")
    resp = client.get("/library/col_detail_small", headers={"Authorization": f"Bearer {analyst_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "1 engagement" in body
    assert "engagement:acme-renewal" in body
    assert "1 claim" in body
