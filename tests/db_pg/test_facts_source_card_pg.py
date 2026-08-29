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


def _create_sharepoint_connection(**config_overrides) -> str:
    from src.repositories import source_connections_repo

    config = {"tenant_id": "tenant-1", "client_id": "client-1"}
    config.update(config_overrides)
    source_connections_repo().create(id="sp-conn-1", name="Corp SharePoint", source_type="sharepoint", config=config)
    return "sp-conn-1"


def test_pipeline_strip_counts_documents_extract_facts_edges(tmp_path, monkeypatch, pg_engine):
    pg_env = pg_env_setup(tmp_path, monkeypatch, pg_engine)
    _fixture(pg_env)
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")

    from app.web.router import _source_pipelines

    cells = _source_pipelines(user=_admin_user())[conn_id]
    fs = cells["file_source"]

    assert fs["crawl"]["documents"] == 3
    assert fs["extract"] == {"indexed": 1, "processing": 1, "needs_review": 1}
    assert fs["graph"] == {"facts": 1, "edges": 1}


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
    assert fs["cost_estimate"]["placeholder"] is True
    assert fs["cost_estimate"]["amount_usd"] > 0


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
    conn_id = _create_sharepoint_connection(cert_private_key_env="SHAREPOINT_CERT_PRIVATE_KEY")
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
    open the wizard straight onto that same row."""
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
    by_id = {s["source_scope_id"]: s for s in fs["scopes"]}
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
