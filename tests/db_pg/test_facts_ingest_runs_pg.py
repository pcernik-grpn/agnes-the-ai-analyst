"""Postgres-only tests for the ``facts_ingest_runs`` repository.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_corpus_file_sources_pg.py``'s PG construction helper.

The HTTP-level "a real ingest actually writes a row" round-trip lives in
``tests/db_pg/test_facts_ingest_pg.py`` (extends the existing happy-path
E2E) — this file only exercises the repository directly.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.facts_ingest_runs_pg import FactsIngestRunsPgRepository

    return FactsIngestRunsPgRepository(db_pg.get_engine())


def _create(repo, **overrides):
    defaults = dict(
        corpus_ids=["col_a"],
        caller="scheduler@system.local",
        documents_seen=3,
        claims_written=2,
        claims_rejected=[{"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1"}],
        deferred=[{"row": 1, "doc_id": "d2"}],
        subjects_created=1,
        subjects_deleted=0,
        review_items=[],
    )
    defaults.update(overrides)
    return repo.create(**defaults)


def test_anonymization_defaults_to_empty_dict_not_null(pg_engine, monkeypatch):
    """A producer that never anonymizes omits the field entirely — the
    stored column must still round-trip as `{}`, never `None`, so every
    reader can treat it as always-present (spec §9.2)."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row["anonymization"] == {}


def test_anonymization_declaration_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    anonymization = {
        "declared": True,
        "scopes": {"col_a": {"docs_anonymized": 5, "docs_skipped": 1}},
    }
    run_id = _create(repo, anonymization=anonymization)
    row = repo.get(run_id)
    assert row["anonymization"] == anonymization

    listed = repo.list_recent(limit=10)
    assert listed[0]["anonymization"] == anonymization


def test_create_returns_an_ir_prefixed_id(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    assert run_id.startswith("ir_")


def test_get_round_trips_every_field(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row is not None
    assert row["corpus_ids"] == ["col_a"]
    assert row["caller"] == "scheduler@system.local"
    assert row["documents_seen"] == 3
    assert row["claims_written"] == 2
    assert row["claims_rejected_count"] == 1
    assert row["claims_rejected"] == [{"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1"}]
    assert row["deferred"] == [{"row": 1, "doc_id": "d2"}]
    assert row["subjects_created"] == 1
    assert row["subjects_deleted"] == 0
    assert row["review_items"] == []
    assert row["created_at"] is not None


def test_get_returns_none_when_missing(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.get("ir_nonexistent") is None


def test_claims_rejected_count_is_derived_from_the_detail_list(pg_engine, monkeypatch):
    """The stored count column is never trusted from the caller — it is
    always ``len(claims_rejected)``, so it can never drift from the detail
    it summarizes."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(
        repo,
        claims_rejected=[
            {"row": 0, "reason": "verbatim_gate_failed"},
            {"row": 1, "reason": "unresolved_doc_id"},
            {"row": 2, "reason": "missing_node_id"},
        ],
    )
    row = repo.get(run_id)
    assert row["claims_rejected_count"] == 3


def test_list_recent_orders_newest_first(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    first = _create(repo, documents_seen=1)
    second = _create(repo, documents_seen=2)
    rows = repo.list_recent(limit=10)
    ids = [r["id"] for r in rows]
    assert ids.index(second) < ids.index(first)


def test_list_recent_respects_limit(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    for _ in range(5):
        _create(repo)
    assert len(repo.list_recent(limit=2)) == 2


def test_distinct_corpus_ids_unions_across_runs(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(repo, corpus_ids=["col_a", "col_b"])
    _create(repo, corpus_ids=["col_b", "col_c"])
    assert repo.distinct_corpus_ids() == ["col_a", "col_b", "col_c"]


def test_distinct_corpus_ids_empty_when_no_runs(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.distinct_corpus_ids() == []


def test_corpus_ids_are_deduplicated_and_sorted_on_write(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo, corpus_ids=["col_b", "col_a", "col_b"])
    row = repo.get(run_id)
    assert row["corpus_ids"] == ["col_a", "col_b"]
