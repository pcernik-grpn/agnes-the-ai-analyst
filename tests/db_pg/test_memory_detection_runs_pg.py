"""Postgres-only tests for the ``memory_detection_runs`` repository (issue
#1971 Part 3 — detection run logs, the missing corporate-memory
observability).

PG-first ratchet (A3): brand-new app-state table, no DuckDB half to
parametrize against — see ``docs/migrations.md`` -> "Adding a PG-only
feature". Pattern follows ``tests/db_pg/test_extraction_runs_pg.py`` and
``tests/db_pg/test_semantic_health_mutes_pg.py``.

The table is created from ``Base.metadata`` for the single model under test
rather than by running the whole Alembic ladder — this file is about the
repository's own contract; ``tests/db_pg/test_alembic_roundtrip.py`` already
owns "the migration and the model agree".
"""

from __future__ import annotations

from datetime import datetime, timezone


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.memory_detection_runs import MemoryDetectionRun

    db_pg.dispose()
    engine = db_pg.get_engine()
    MemoryDetectionRun.__table__.create(engine, checkfirst=True)

    from src.repositories.memory_detection_runs_pg import MemoryDetectionRunsPgRepository

    return MemoryDetectionRunsPgRepository(engine)


def _now():
    return datetime.now(timezone.utc)


def test_create_returns_an_mdr_prefixed_id(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    started = _now()
    run_id = repo.create(
        source="session_transcripts",
        started_at=started,
        finished_at=_now(),
        sessions_scanned=3,
        items_proposed=5,
        items_filtered=1,
        items_inserted=3,
        items_routed_side_domain=1,
        dry_run=False,
        policy_fingerprint="abc123",
    )
    assert run_id.startswith("mdr_")


def test_get_round_trips_every_field(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    started = _now()
    finished = _now()
    run_id = repo.create(
        source="session_transcripts",
        started_at=started,
        finished_at=finished,
        sessions_scanned=3,
        items_proposed=5,
        items_filtered=1,
        items_inserted=3,
        items_routed_side_domain=1,
        dry_run=False,
        policy_fingerprint="abc123",
        token_usage={"input_tokens": 100, "output_tokens": 20},
    )
    row = repo.get(run_id)
    assert row["id"] == run_id
    assert row["source"] == "session_transcripts"
    assert row["sessions_scanned"] == 3
    assert row["items_proposed"] == 5
    assert row["items_filtered"] == 1
    assert row["items_inserted"] == 3
    assert row["items_routed_side_domain"] == 1
    assert row["dry_run"] is False
    assert row["policy_fingerprint"] == "abc123"
    assert row["token_usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert row["error"] is None


def test_token_usage_defaults_to_empty_dict_not_null(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.create(source="claude_local_md", started_at=_now(), finished_at=_now())
    row = repo.get(run_id)
    assert row["token_usage"] == {}


def test_error_text_is_recorded_on_failure(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.create(
        source="session_transcripts",
        started_at=_now(),
        finished_at=_now(),
        error="LLMTimeoutError: upstream timed out",
    )
    row = repo.get(run_id)
    assert row["error"] == "LLMTimeoutError: upstream timed out"


def test_dry_run_flag_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.create(source="session_transcripts", started_at=_now(), finished_at=_now(), dry_run=True)
    row = repo.get(run_id)
    assert row["dry_run"] is True


def test_list_recent_is_newest_first_and_respects_limit(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    ids = [
        repo.create(source="session_transcripts", started_at=_now(), finished_at=_now(), sessions_scanned=i)
        for i in range(5)
    ]
    rows = repo.list_recent(limit=2)
    assert [r["id"] for r in rows] == [ids[4], ids[3]]


def test_count_covers_every_recorded_run_not_just_the_page(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    for _ in range(3):
        repo.create(source="session_transcripts", started_at=_now(), finished_at=_now())
    assert repo.count() == 3
    assert len(repo.list_recent(limit=1)) == 1


def test_list_recent_supports_offset_for_pagination(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    ids = [repo.create(source="session_transcripts", started_at=_now(), finished_at=_now()) for _ in range(3)]
    page2 = repo.list_recent(limit=1, offset=1)
    assert [r["id"] for r in page2] == [ids[1]]
