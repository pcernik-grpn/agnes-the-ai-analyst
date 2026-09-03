"""The file-source source card (`app.web.router._sharepoint_pipeline_cell`,
spec §13.2 "Source card") — PG-only, since it reads through
``facts_ingest_runs_repo()`` and ``facts_repo()`` (both A3 ratchet PG-only).

DuckDB-side coverage (graceful degrade with no Postgres, certificate row,
"no connection yet") lives in ``tests/test_admin_data_sources_page.py`` —
same split every other facts surface in this repo uses.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_a"
CORPUS_B = "col_b"
#: `scope_ids` (`app.web.router._sharepoint_pipeline_cell`) reads
#: `config.scopes[].collection_id` — the connection's OWN confirmed scopes,
#: not `facts_ingest_runs_repo().distinct_corpus_ids()` (retired). Every
#: test whose assertions depend on `scope_ids` resolving to both fixture
#: collections passes this.
_TWO_SCOPES = [
    {"source_scope_id": "s-a", "display_path": "A", "anonymize": False, "collection_id": CORPUS_A},
    {"source_scope_id": "s-b", "display_path": "B", "anonymize": False, "collection_id": CORPUS_B},
]


def _admin_user() -> dict:
    return {"id": "admin1", "email": "admin1@test.com"}


def pg_env_setup(tmp_path, monkeypatch, pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)

    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    users_repo().create(id="admin1", email="admin1@test.com", name="Admin")
    admin_gid = user_groups_repo().get_by_name("Admin")["id"]
    user_group_members_repo().add_member("admin1", admin_gid, source="test-fixture")
    return pg_engine


def _seed_collection(collection_id: str, created_by: str = "uploader1") -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )


def _seed_corpus_file(corpus_id: str, file_id: str, status: str = "indexed", sha256: str = "sha1") -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :status)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": sha256, "status": status},
        )


def _fixture(pg_env):
    """Two collections (A, B), files in each with varied processing_status,
    one fact + one edge claimed in A, an ingest run touching both, and a
    grant on A only (B is left ungranted — the "collections with no group"
    case)."""
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo, users_repo

    users_repo().create(id="uploader1", email="uploader1@test.com", name="Uploader")
    _seed_collection(CORPUS_A)
    _seed_collection(CORPUS_B)
    _seed_corpus_file(CORPUS_A, "cf_a1", status="indexed")
    _seed_corpus_file(CORPUS_A, "cf_a2", status="processing")
    _seed_corpus_file(CORPUS_B, "cf_b1", status="needs_review")

    from src.repositories.facts_pg import FactsPgRepository
    import src.db_pg as db_pg

    facts_repo_ = FactsPgRepository(db_pg.get_engine())
    subj = facts_repo_.create_fact(type="engagement")
    facts_repo_.add_claim(fact_id=subj, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Q1.")
    dst = facts_repo_.create_fact(type="person")
    edge_id = facts_repo_.create_edge(src=subj, type="owned_by", dst=dst)
    facts_repo_.add_claim(edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Q2.")

    from src.repositories import facts_ingest_runs_repo

    facts_ingest_runs_repo().create(
        corpus_ids=[CORPUS_A, CORPUS_B],
        caller="scheduler@system.local",
        documents_seen=3,
        claims_written=2,
        claims_rejected=[
            {"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1"},
            {"row": 1, "reason": "unresolved_doc_id", "doc_id": "d2"},
        ],
        deferred=[{"row": 2, "doc_id": "d3"}],
        subjects_created=2,
        subjects_deleted=0,
        review_items=[],
    )

    grp = user_groups_repo().create(name="sp-group", description="test", created_by="test-fixture")
    user_group_members_repo().add_member("admin1", grp["id"], source="test-fixture")
    resource_grants_repo().create(grp["id"], "collection", CORPUS_A, "test-fixture", "required")


def _create_sharepoint_connection_named(conn_id: str, **config_overrides) -> str:
    from src.repositories import source_connections_repo

    config = {"tenant_id": "tenant-1", "client_id": "client-1"}
    config.update(config_overrides)
    source_connections_repo().create(
        id=conn_id, name=f"Corp SharePoint {conn_id}", source_type="sharepoint", config=config
    )
    return conn_id


def _create_sharepoint_connection(**config_overrides) -> str:
    return _create_sharepoint_connection_named("sp-conn-1", **config_overrides)


def test_pipeline_strip_counts_documents_extract_facts_edges(tmp_path, monkeypatch, pg_engine):
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=_TWO_SCOPES,
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    cells = _source_pipelines(user=_admin_user())[conn_id]
    fs = cells["file_source"]

    assert fs["crawl"]["documents"] == 3
    assert fs["extract"] == {"indexed": 1, "processing": 1, "needs_review": 1}
    # NOT computed here (perf follow-up, 2026-09-03) — see
    # `test_facts_graph_counts_endpoint_matches_the_old_page_numbers` below
    # for the same fixture's facts/edges count, now served by the lazy
    # `GET .../facts-graph-counts` endpoint instead.
    assert fs["graph"] is None


def test_facts_graph_counts_endpoint_matches_the_old_page_numbers(tmp_path, monkeypatch, pg_engine):
    """`GET .../facts-graph-counts` (`app.api.admin_sharepoint.facts_graph_
    counts`) now serves the SAME {facts, edges} numbers the page's own
    pipeline strip used to compute inline — same fixture as the test above,
    minus the page-render call."""
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=_TWO_SCOPES,
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.api.admin_sharepoint import facts_graph_counts

    counts = facts_graph_counts(conn_id, user=_admin_user())
    assert counts == {"facts": 1, "edges": 1}


def test_facts_graph_counts_endpoint_is_zero_with_no_scopes(tmp_path, monkeypatch, pg_engine):
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_sharepoint_connection()

    from app.api.admin_sharepoint import facts_graph_counts

    assert facts_graph_counts(conn_id, user=_admin_user()) == {"facts": 0, "edges": 0}


def test_facts_graph_counts_endpoint_404s_for_a_non_sharepoint_connection(tmp_path, monkeypatch, pg_engine):
    from fastapi import HTTPException

    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    from src.repositories import source_connections_repo

    source_connections_repo().create(id="not-sp", name="Snowflake", source_type="snowflake", config={})

    from app.api.admin_sharepoint import facts_graph_counts

    import pytest

    with pytest.raises(HTTPException) as exc:
        facts_graph_counts("not-sp", user=_admin_user())
    assert exc.value.status_code == 404


def test_pipeline_strip_counts_documents_with_no_facts_ingest_run_at_all(tmp_path, monkeypatch, pg_engine):
    """The live symptom this fixes: a connection whose crawl indexed real
    files but never reached the (opt-in, off-by-default) facts stage used to
    report `crawl.documents == 0` / `extract == {}` forever — `scope_ids`
    came from `facts_ingest_runs_repo().distinct_corpus_ids()`, empty when no
    run had ever touched a collection. It now comes from the connection's
    OWN `config.scopes[].collection_id`, so the crawl/extract counts are
    correct whether or not facts extraction has ever run."""
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _seed_collection(CORPUS_A)
    _seed_corpus_file(CORPUS_A, "cf_a1", status="indexed")
    _seed_corpus_file(CORPUS_A, "cf_a2", status="indexed")
    _seed_corpus_file(CORPUS_A, "cf_a3", status="processing")
    # No `facts_ingest_runs_repo().create(...)` call anywhere in this test —
    # the old proxy would resolve `scope_ids == []` here.
    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=[{"source_scope_id": "s-a", "display_path": "A", "anonymize": False, "collection_id": CORPUS_A}],
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]

    assert fs["crawl"]["documents"] == 3
    assert fs["extract"] == {"indexed": 2, "processing": 1}
    assert fs["last_run"] is None  # honest: no ingest run has ever been recorded


def test_error_badges_from_the_last_run_report_are_categorized(tmp_path, monkeypatch, pg_engine):
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    last_run = fs["last_run"]
    assert last_run is not None
    assert len(last_run["rejected_quotes"]) == 1
    assert last_run["rejected_quotes"][0]["reason"] == "verbatim_gate_failed"
    assert len(last_run["protocol_errors"]) == 1
    assert last_run["protocol_errors"][0]["reason"] == "unresolved_doc_id"
    assert len(last_run["deferred"]) == 1
    # O7 follow-up: no dropped source_url in this fixture's run — the badge
    # category is always present (never a missing key), just empty.
    assert last_run["source_urls_rejected"] == []

    # Cost placeholder is explicitly labeled as such and derived from the
    # same 3 queue items (1 rejected quote + 1 protocol error + 1 deferred)
    # — a dropped source_url is deliberately NOT in that count (the claim
    # itself still wrote, nothing is queued for retry over it).
    assert fs["queue"]["items"] > 0


def test_meaningfulness_floor_rejection_joins_the_rejected_quotes_badge(tmp_path, monkeypatch, pg_engine):
    """`quote_not_meaningful` (spec §8.4) is a DIFFERENT `claims_rejected`
    reason from `verbatim_gate_failed`, but the SAME gate (§8) doing its
    job — both must land in the `rejected_quotes` badge, never
    `protocol_errors` (unresolved doc id, malformed edge, …)."""
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from src.repositories import facts_ingest_runs_repo

    facts_ingest_runs_repo().create(
        corpus_ids=[CORPUS_A],
        caller="scheduler@system.local",
        documents_seen=1,
        claims_written=0,
        claims_rejected=[
            {"row": 0, "reason": "quote_not_meaningful", "doc_id": "d5"},
            {"row": 1, "reason": "unresolved_doc_id", "doc_id": "d6"},
        ],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
    )

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    last_run = fs["last_run"]
    rejected_reasons = {r["reason"] for r in last_run["rejected_quotes"]}
    assert rejected_reasons == {"quote_not_meaningful"}
    protocol_reasons = {r["reason"] for r in last_run["protocol_errors"]}
    assert "quote_not_meaningful" not in protocol_reasons
    assert "unresolved_doc_id" in protocol_reasons


def test_source_urls_rejected_badge_is_populated_from_the_last_run(tmp_path, monkeypatch, pg_engine):
    """O7 follow-up: a dropped `source_url` surfaces as its own badge
    category on the source card, itemized doc_id + reason — never folded
    into `protocol_errors` (a dropped source_url never rejects the claim,
    so it is a different signal)."""
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from src.repositories import facts_ingest_runs_repo

    facts_ingest_runs_repo().create(
        corpus_ids=[CORPUS_A],
        caller="scheduler@system.local",
        documents_seen=1,
        claims_written=1,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        source_urls_rejected=[{"doc_id": "d4", "reason": "not_https"}],
    )

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    rejected = fs["last_run"]["source_urls_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "not_https"
    assert rejected[0]["doc_id"] == "d4"


def test_rejection_rows_resolve_doc_id_to_file_name_and_collection(tmp_path, monkeypatch, pg_engine):
    """The live-use complaint this fixes: a bare sha16 like
    `6a8e0bc93c07c56a` "tells nobody anything". `_enrich_sharepoint_
    rejection_rows` resolves it through `corpus_file_sources` to the corpus
    file's own name + its collection's name, added as a `doc` key alongside
    (never replacing) the raw `doc_id`/`reason` fields."""
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from src.repositories import corpus_file_sources_repo

    # `d1` is the rejected-quote row's doc_id in `_fixture`'s last run —
    # anchor it to the already-seeded `cf_a1` (filename `cf_a1.md`) in
    # CORPUS_A ("col_a", named "col_a" — `_seed_collection` uses the id as
    # its display name too).
    corpus_file_sources_repo().upsert(
        corpus_file_id="cf_a1", corpus_id=CORPUS_A, source_stable_id="stable-1", source_doc_id="d1"
    )

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    rejected = fs["last_run"]["rejected_quotes"]
    assert len(rejected) == 1
    assert rejected[0]["doc_id"] == "d1"  # raw field untouched
    assert rejected[0]["reason"] == "verbatim_gate_failed"
    assert rejected[0]["doc"] == {"name": "cf_a1.md", "collection": CORPUS_A}


def test_rejection_rows_degrade_to_unresolved_when_doc_id_is_unknown(tmp_path, monkeypatch, pg_engine):
    """`_fixture`'s protocol-error doc_id (`d2`) was never anchored through
    `corpus_file_sources` — the card must fall back to `doc: None` (the
    template renders the raw sha16 + "not in any collection"), never a
    500 for the whole cell."""
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    protocol_errors = fs["last_run"]["protocol_errors"]
    assert len(protocol_errors) == 1
    assert protocol_errors[0]["doc_id"] == "d2"
    assert protocol_errors[0]["doc"] is None


def test_identity_row_counts_matched_groups_and_ungranted_collections(tmp_path, monkeypatch, pg_engine):
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=_TWO_SCOPES,
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    # CORPUS_A is granted (1 group), CORPUS_B is not — fail-closed, counted.
    assert fs["identity"] == {"groups_matched": 1, "collections_no_group": 1, "collections_total": 2}


def test_anonymization_distinguishes_requested_declared_and_pending(tmp_path, monkeypatch, pg_engine):
    """Spec §9.2/§13.2: the checkbox alone (`requested`) must never be
    read as `declared` — only the LATEST run's own declaration earns that.
    A_scope is requested AND the latest run declares it (-> declared);
    B_scope is requested but the run declares nothing for it (-> pending)."""
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _seed_collection(CORPUS_A)
    _seed_collection(CORPUS_B)

    from src.repositories import facts_ingest_runs_repo

    facts_ingest_runs_repo().create(
        corpus_ids=[CORPUS_A],
        caller="scheduler@system.local",
        documents_seen=1,
        claims_written=1,
        claims_rejected=[],
        deferred=[],
        subjects_created=1,
        subjects_deleted=0,
        review_items=[],
        anonymization={"declared": True, "scopes": {CORPUS_A: {"docs_anonymized": 2, "docs_skipped": 0}}},
    )

    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=[
            {"source_scope_id": "s-a", "display_path": "A", "anonymize": True, "collection_id": CORPUS_A},
            {"source_scope_id": "s-b", "display_path": "B", "anonymize": True, "collection_id": CORPUS_B},
        ],
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    assert fs["anonymization"] == {
        "requested": [CORPUS_A, CORPUS_B],
        "declared": [CORPUS_A],
        "pending": [CORPUS_B],
    }


def test_scopes_cell_lists_each_confirmed_scope_with_its_resolved_collection_and_group_state(
    tmp_path, monkeypatch, pg_engine
):
    """`scopes` is the connection's own `config.scopes`, reused through
    `admin_sharepoint._scope_out` — the exact shape the connect wizard's own
    step-3 "Share" preview reads, so clicking a scope row on the card can
    open the wizard straight onto that same row.

    NOT server-rendered into the page any more (perf follow-up,
    2026-09-03, second finding) — `_sharepoint_pipeline_cell` keeps only
    the cheap `scopes_total` count; the enriched rows this test pins are
    now served exclusively by `GET .../scopes`
    (`admin_sharepoint.list_scopes`), fetched by the card on expand.
    """
    import asyncio

    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)

    conn_id = _create_sharepoint_connection(
        cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY",
        scopes=[
            {"source_scope_id": "s-a", "display_path": "A / Contracts", "anonymize": False, "collection_id": CORPUS_A},
            {"source_scope_id": "s-b", "display_path": "B / Reports", "anonymize": False, "collection_id": CORPUS_B},
        ],
    )
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    assert fs["scopes_total"] == 2

    from app.api.admin_sharepoint import list_scopes

    body = asyncio.run(list_scopes(conn_id, user=_admin_user()))
    by_id = {s["source_scope_id"]: s for s in body["items"]}
    assert set(by_id) == {"s-a", "s-b"}
    # CORPUS_A is granted a group in `_fixture` -> no warning, group present.
    assert by_id["s-a"]["collection"]["name"] == CORPUS_A
    assert by_id["s-a"]["no_group_warning"] is False
    assert by_id["s-a"]["group_ids"]
    # CORPUS_B is left ungranted in `_fixture` -> the warning fires.
    assert by_id["s-b"]["collection"]["name"] == CORPUS_B
    assert by_id["s-b"]["no_group_warning"] is True
    assert by_id["s-b"]["group_ids"] == []


def test_anonymization_empty_when_no_scope_requests_it(tmp_path, monkeypatch, pg_engine):
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)  # last run exists but no `anonymization` block was sent
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    assert fs["anonymization"] == {"requested": [], "declared": [], "pending": []}


def test_certificate_row_shows_origin_and_never_the_value(tmp_path, monkeypatch, pg_engine):
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv(
        "SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nsecret-value\n-----END PRIVATE KEY-----"
    )

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    cert = fs["certificate"]
    assert cert["origin"] == "env"
    assert cert["env_name"] == "SHAREPOINT_CERT_PRIVATE_KEY"
    assert cert["error"] is None
    assert "secret-value" not in str(cert)


def test_no_sharepoint_connection_renders_no_file_source_cell(tmp_path, monkeypatch, pg_engine):
    pg_env_setup(tmp_path, monkeypatch, pg_engine)

    from app.web.router import _source_pipelines

    cells = _source_pipelines(user=_admin_user())
    for row in cells.values():
        assert "file_source" not in row


# ---------------------------------------------------------------------------
# Perf regression: /admin/data-sources page (bounded queries, bounded payload)
#
# A real SharePoint connection can carry 50-180 confirmed scopes; the page
# renders every SharePoint connection's pipeline strip in one server-side
# fold (`_source_inventory`). Before this fix, both the query count AND the
# inlined JSON payload scaled with total scope count across every
# connection on the page — see CHANGELOG / PR description for the
# before/after numbers.
# ---------------------------------------------------------------------------


def _many_scopes(n: int, *, prefix: str = "col") -> list[dict]:
    """Synthetic confirmed-scope config rows — no backing `file_corpora`/
    `corpus_files` rows needed: every code path under test degrades a
    missing collection to `None`/absent rather than raising, so this stays
    cheap to seed even at n=180."""
    return [
        {"source_scope_id": f"s-{prefix}-{i}", "display_path": f"/{prefix}/{i}", "collection_id": f"{prefix}_{i}"}
        for i in range(n)
    ]


def test_scopes_total_is_exact_and_not_capped(tmp_path, monkeypatch, pg_engine):
    """`cell["scopes"]` (the enriched, per-scope row list) is gone entirely
    from the page fold (perf follow-up, 2026-09-03, second finding) — the
    card fetches it lazily, unbounded, from `GET .../scopes` instead. Only
    the cheap `scopes_total` count survives here, and it is exact at any
    scope count — nothing to cap when nothing is inlined."""
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_sharepoint_connection(scopes=_many_scopes(60))

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    assert "scopes" not in fs
    assert "scopes_truncated" not in fs
    assert fs["scopes_total"] == 60


def test_scopes_total_is_exact_for_a_small_connection(tmp_path, monkeypatch, pg_engine):
    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_sharepoint_connection(scopes=_many_scopes(3))

    from app.web.router import _source_pipelines

    fs = _source_pipelines(user=_admin_user())[conn_id]["file_source"]
    assert fs["scopes_total"] == 3


def test_source_pipelines_payload_size_does_not_scale_with_scope_count(tmp_path, monkeypatch, pg_engine):
    """The bug this guards: `cell["scopes"]` used to carry EVERY confirmed
    scope (path, collection, badges) for EVERY SharePoint connection on the
    page — the exact structure `{{ source_pipelines | tojson }}` inlines
    into the HTML response verbatim. A connection with 180 scopes made that
    inline payload roughly proportional to 180. `cell["scopes"]` is gone
    entirely now (perf follow-up, 2026-09-03, second finding — the enriched
    rows are fetched lazily instead, `GET .../scopes`), so the payload
    should not grow AT ALL between 60 and 180 scopes beyond the (tiny)
    `scopes_total` integer."""
    import json

    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_sharepoint_connection(scopes=_many_scopes(60))

    from app.web.router import _source_pipelines

    small = _source_pipelines(user=_admin_user())
    small_bytes = len(json.dumps(small))

    from src.repositories import source_connections_repo

    source_connections_repo().update(conn_id, config={"tenant_id": "tenant-1", "scopes": _many_scopes(180)})
    large = _source_pipelines(user=_admin_user())
    large_bytes = len(json.dumps(large))

    # 60 -> 180 scopes is a 3x growth in the underlying config; the payload
    # should differ only by the (tiny) `scopes_total` integer, never by
    # anything proportional to scope count.
    assert large_bytes < small_bytes * 1.05, (
        f"source_pipelines payload grew {small_bytes} -> {large_bytes} bytes for a 3x scope-count "
        f"increase — scope rows must never be inlined again"
    )


def test_source_inventory_query_count_is_bounded_at_high_scope_count(tmp_path, monkeypatch, pg_engine):
    """Before the first perf fix: `_sharepoint_pipeline_cell` issued one
    `corpus_files.list_for_corpus` call PER SCOPE, one full-table
    `resource_grants` scan PER SCOPE (via `_scope_out` -> `_group_ids_for_
    collection`), and a `file_corpora.get` PER SCOPE — a 180-scope
    connection cost roughly 540 round trips on those three alone.

    Before the follow-up fix (perf follow-up, 2026-09-03, two more live
    findings on the same instance): `facts_repo().count_visible_facts_for_
    collections`/`count_visible_edges_for_collections` — the caller-scoped
    fact/edge counts feeding `cell["graph"]` — ran one query per corpus_id
    (~2 per scope, ~360 for 180 scopes), which is what a live instance's
    `pg_stat_activity` showed dominating the page's own render time (22 of
    ~28 samples over one page load were exactly these two statements); and
    `_scope_out` (the enriched per-scope rows for `cell["scopes"]`, capped
    at 50 in the FIRST fix) still cost a `file_corpora.get` per scope shown.
    Neither is computed at page-render time at all any more — `cell["graph"]`
    moved to the lazy `GET .../facts-graph-counts` endpoint, and
    `cell["scopes"]` is gone entirely in favor of `GET .../scopes`, both
    fetched by the card only once painted — so this connection's
    contribution to the page's query count is now flat regardless of scope
    count, independent of the 50-scope cap that used to bound it.
    """
    import sqlalchemy as sa

    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _create_sharepoint_connection(scopes=_many_scopes(180))

    import src.db_pg as db_pg
    from app.web.router import _source_pipelines

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_pg.get_engine()
    sa.event.listen(engine, "before_cursor_execute", _capture)
    try:
        _source_pipelines(user=_admin_user())
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)

    assert len(statements) < 30, (
        f"_source_pipelines issued {len(statements)} statements for a single 180-scope connection "
        f"— expected a small, scope-count-independent number now that facts/edges counts and scope "
        f"rows are both lazy"
    )
    assert not any("claims" in s for s in statements), (
        "_source_pipelines touched the `claims` table — the page render must never scan it; "
        "fact/edge counts are computed lazily by GET .../facts-graph-counts instead"
    )


def test_admin_data_sources_page_route_issues_no_claims_statements(tmp_path, monkeypatch, pg_engine):
    """The literal live regression: `GET /admin/data-sources`, driven through
    a real `TestClient` (not just the inner `_source_pipelines()` function),
    must never touch `claims` — the table the fact/edge visibility CTEs scan
    — regardless of how many SharePoint connections or scopes exist. Eight
    connections x 50 scopes each is the shape of the live instance that
    surfaced this."""
    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    for i in range(8):
        _create_sharepoint_connection_named(f"sp-conn-many-{i}", scopes=_many_scopes(50, prefix=f"c{i}"))

    import src.db_pg as db_pg
    from app.main import create_app

    app = create_app()
    client = TestClient(app)
    from app.auth.jwt import create_access_token

    token = create_access_token("admin1", "admin1@test.com")
    client.cookies.set("access_token", token)

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_pg.get_engine()
    sa.event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/admin/data-sources", headers={"Accept": "text/html"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200, resp.text
    claims_statements = [s for s in statements if "claims" in s]
    assert not claims_statements, (
        f"GET /admin/data-sources issued {len(claims_statements)} statement(s) touching `claims` "
        f"across 8 connections x 50 scopes — the page route must issue ZERO: {claims_statements[:3]!r}"
    )


def test_admin_data_sources_page_response_size_is_bounded(tmp_path, monkeypatch, pg_engine):
    """The other half of the same live regression: the raw HTML response for
    `GET /admin/data-sources` must stay small regardless of scope count.

    Before this fix (measured on the deployed merge commit of the FIRST
    perf PR, already carrying the 50-scope cap): 8 connections x 50 scopes
    rendered ~343 KB — ~171 KB of it the inlined `SOURCE_PIPELINES` JSON's
    per-scope enriched rows (path, collection, group grants), which nothing
    on first paint reads (`app.web.router._sharepoint_pipeline_cell` only
    keeps the cheap `scopes_total` count now; the enriched list is fetched
    lazily by the card on expand, `GET .../scopes`). Target (coordinator,
    2026-09-03): well under 200 KB.
    """
    from fastapi.testclient import TestClient

    pg_env_setup(tmp_path, monkeypatch, pg_engine)
    for i in range(8):
        _create_sharepoint_connection_named(f"sp-conn-many-{i}", scopes=_many_scopes(50, prefix=f"c{i}"))

    from app.main import create_app
    from app.auth.jwt import create_access_token

    app = create_app()
    client = TestClient(app)
    token = create_access_token("admin1", "admin1@test.com")
    client.cookies.set("access_token", token)

    resp = client.get("/admin/data-sources", headers={"Accept": "text/html"})
    assert resp.status_code == 200, resp.text
    size = len(resp.content)
    assert size < 200_000, (
        f"GET /admin/data-sources returned {size} bytes for 8 connections x 50 scopes — "
        f"expected well under 200 000 (200 KB)"
    )
    # The enriched per-scope rows (path, collection badges) must not be
    # baked into the response at all — only fetched on expand. A specific
    # scope's own `display_path` (`_many_scopes`: `/c0/0`, `/c0/1`, …) is a
    # precise negative — `.ds-sp-scope-row__path` alone would also match
    # the (legitimate, static) CSS rule that styles it once fetched.
    assert "no collection yet" not in resp.text
    assert "/c0/0" not in resp.text
    assert "/c3/25" not in resp.text
