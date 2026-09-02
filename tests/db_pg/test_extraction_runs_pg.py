"""Postgres-only tests for the ``extraction_runs`` repository (2026-08-31
extraction-observability-ui design §7.1).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_facts_ingest_runs_pg.py``.

The table is created from ``Base.metadata`` for the single model under test
rather than by running the whole Alembic ladder: this file is about the
repository's own contract, and ``tests/db_pg/test_alembic_roundtrip.py``
already owns "the migration and the model agree".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.extraction import ExtractionRun

    db_pg.dispose()
    engine = db_pg.get_engine()
    ExtractionRun.__table__.create(engine, checkfirst=True)

    from src.repositories.extraction_runs_pg import ExtractionRunsPgRepository

    return ExtractionRunsPgRepository(engine)


def test_start_returns_an_er_prefixed_running_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    assert run_id.startswith("er_")

    row = repo.get(run_id)
    assert row["status"] == "running"
    assert row["connection_id"] == "conn_a"
    assert row["phase"] == "crawl"
    # A row that has never checkpointed still carries a freshness stamp, so
    # the card's "as of" caption is never blank.
    assert row["started_at"] is not None
    assert row["checkpoint_at"] is not None
    assert row["finished_at"] is None


def test_json_columns_default_to_dicts_not_null(pg_engine, monkeypatch):
    """Every reader treats `report`/`progress`/`usage`/`skips` as
    always-present mappings — a fresh run must not hand back `None`."""
    repo = _make_repo(pg_engine, monkeypatch)
    row = repo.get(repo.start(connection_id="conn_a"))
    assert row["report"] == {}
    assert row["progress"] == {}
    assert row["usage"] == {}
    assert row["skips"] == {}


def test_checkpoint_advances_counters_and_freshness(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    before = repo.get(run_id)["checkpoint_at"]

    repo.checkpoint(run_id, files_seen=200, files_done=200, progress={"new": 12, "unchanged": 188})
    row = repo.get(run_id)
    assert row["files_seen"] == 200
    assert row["files_done"] == 200
    assert row["progress"] == {"new": 12, "unchanged": 188}
    assert row["checkpoint_at"] >= before
    # Enumeration is never claimed complete while the run is live: the delta
    # feed can always hand back another page.
    assert row["enumeration_done"] is False


def test_checkpoint_after_finalize_is_a_no_op(pg_engine, monkeypatch):
    """A late checkpoint from a crawl already recorded as interrupted must
    not resurrect it as running — the write is scoped to `status='running'`."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(run_id, status="interrupted", report={"interrupted": True}, files_done=42)

    repo.checkpoint(run_id, files_seen=999, files_done=999)
    row = repo.get(run_id)
    assert row["status"] == "interrupted"
    assert row["files_done"] == 42


def test_finish_records_the_report_and_closes_the_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    report = {"new": 3, "changed": 1, "duration_s": 12.5, "interrupted": False}
    repo.finish(run_id, status="done", report=report, files_seen=4, files_done=4)

    row = repo.get(run_id)
    assert row["status"] == "done"
    assert row["report"] == report
    assert row["finished_at"] is not None
    assert row["enumeration_done"] is True


def test_finish_refuses_to_close_into_running(pg_engine, monkeypatch):
    """A row that says "running" and carries a `finished_at` is exactly the
    kind of value that looks checked and is not."""
    import pytest

    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    with pytest.raises(ValueError):
        repo.finish(run_id, status="running")


def test_failed_run_keeps_its_error_text(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(run_id, status="failed", error="SharePointGraphError: 403 on drive b!x")
    row = repo.get(run_id)
    assert row["status"] == "failed"
    assert "403" in row["error"]


def test_usage_empty_dict_means_no_tokens_spent(pg_engine, monkeypatch):
    """`{}` is "no LLM tokens were spent", which the UI must keep tellable
    apart from a computed $0.00 (design §7.2)."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(run_id, status="done", report={}, usage={})
    assert repo.get(run_id)["usage"] == {}

    other = repo.start(connection_id="conn_a")
    repo.finish(other, status="done", usage={"model": "m", "input_tokens": 10})
    assert repo.get(other)["usage"]["input_tokens"] == 10


def test_get_running_returns_only_this_connections_live_run(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    mine = repo.start(connection_id="conn_a")
    repo.start(connection_id="conn_b")
    assert repo.get_running("conn_a")["id"] == mine

    repo.finish(mine, status="done")
    assert repo.get_running("conn_a") is None


def test_abandon_stale_running_closes_every_running_row_for_the_connection(pg_engine, monkeypatch):
    """A worker that dies mid-crawl leaves its row `running` forever unless
    something closes it. The production symptom this guards: two zombie
    rows for the same connection, both `running`, neither ever finalized."""
    repo = _make_repo(pg_engine, monkeypatch)
    zombie1 = repo.start(connection_id="conn_a")
    repo.checkpoint(zombie1, files_seen=61, files_done=61, progress={"new": 61})
    zombie2 = repo.start(connection_id="conn_a")
    other_conn_run = repo.start(connection_id="conn_b")

    abandoned = repo.abandon_stale_running("conn_a")

    assert set(abandoned) == {zombie1, zombie2}

    row1 = repo.get(zombie1)
    assert row1["status"] == "interrupted"
    assert row1["report"]["interrupted"] is True
    assert row1["report"]["interrupted_reason"] == "abandoned"
    assert row1["finished_at"] is not None
    assert row1["error"]
    # The dead run's own progress is preserved, not lost or overwritten.
    assert row1["files_done"] == 61

    row2 = repo.get(zombie2)
    assert row2["status"] == "interrupted"
    assert row2["report"]["interrupted_reason"] == "abandoned"

    # A different connection's live run is never touched.
    assert repo.get(other_conn_run)["status"] == "running"


def test_abandon_stale_running_is_a_no_op_with_nothing_to_close(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.abandon_stale_running("conn_never_run") == []


def test_abandon_stale_running_never_touches_an_already_finished_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    done = repo.start(connection_id="conn_a")
    repo.finish(done, status="done", report={"new": 3})

    assert repo.abandon_stale_running("conn_a") == []
    row = repo.get(done)
    assert row["status"] == "done"
    assert row["report"] == {"new": 3}


def test_last_completed_ignores_live_and_failed_runs(pg_engine, monkeypatch):
    """The card's "last run" figures come from a run that actually ended and
    has counters — never from a failed row that has none."""
    repo = _make_repo(pg_engine, monkeypatch)
    done = repo.start(connection_id="conn_a")
    repo.finish(done, status="done", report={"new": 5})
    failed = repo.start(connection_id="conn_a")
    repo.finish(failed, status="failed", error="boom")
    repo.start(connection_id="conn_a")  # still running

    assert repo.last_completed("conn_a")["id"] == done


def test_last_completed_counts_an_interrupted_run(pg_engine, monkeypatch):
    """An interrupted run ingested what it ingested — its numbers are real
    and it is the last thing that happened."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(run_id, status="interrupted", report={"new": 2, "interrupted": True})
    assert repo.last_completed("conn_a")["id"] == run_id


def test_list_for_connection_is_newest_first_and_scoped(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    first = repo.start(connection_id="conn_a")
    second = repo.start(connection_id="conn_a")
    repo.start(connection_id="conn_b")

    rows = repo.list_for_connection("conn_a", limit=10)
    ids = [r["id"] for r in rows]
    assert set(ids) == {first, second}
    assert ids.index(second) < ids.index(first)


def test_count_for_connection_is_the_total_not_the_page(pg_engine, monkeypatch):
    """The drawer button's count is the TOTAL, so "5 more runs" is never a
    silent truncation."""
    repo = _make_repo(pg_engine, monkeypatch)
    for _ in range(7):
        repo.start(connection_id="conn_a")
    assert len(repo.list_for_connection("conn_a", limit=3)) == 3
    assert repo.count_for_connection("conn_a") == 7
    assert repo.count_for_connection("conn_nothing") == 0


def test_cap_skips_reports_listed_and_total_separately():
    """Only oversize skips keep a path; a run that refused 27 documents and
    can name 20 of them must say exactly that."""
    from src.repositories.extraction_runs_pg import cap_skips

    capped = cap_skips([{"path": f"/f{i}", "reason": "oversize"} for i in range(20)], total=27)
    assert capped["listed"] == 20
    assert capped["total"] == 27
    assert capped["truncated"] is True

    exact = cap_skips([{"path": "/f", "reason": "oversize"}])
    assert exact["listed"] == 1
    assert exact["total"] == 1
    assert exact["truncated"] is False


def test_cap_skips_never_lists_more_than_the_cap(pg_engine, monkeypatch):
    from src.repositories.extraction_runs_pg import _SKIPS_CAP, cap_skips

    capped = cap_skips([{"path": f"/f{i}"} for i in range(_SKIPS_CAP + 50)])
    assert capped["listed"] == _SKIPS_CAP
    assert capped["total"] == _SKIPS_CAP + 50
    assert capped["truncated"] is True

    # …and the capped shape round-trips through the column unchanged.
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(run_id, status="done", skips=capped)
    stored = repo.get(run_id)["skips"]
    assert stored["listed"] == _SKIPS_CAP
    assert stored["total"] == _SKIPS_CAP + 50


def test_timestamps_round_trip_as_iso_strings(pg_engine, monkeypatch):
    """The API hands these straight to the browser; a datetime would not
    serialize the same way on both read paths."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    row = repo.get(run_id)
    parsed = datetime.fromisoformat(row["started_at"])
    assert parsed.tzinfo is not None
    assert abs(parsed - datetime.now(timezone.utc)) < timedelta(minutes=5)


# -- list_latest_for_connections (fleet dashboard, 2026-09-02) --------------


def test_list_latest_for_connections_picks_the_newest_row_per_connection(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    older = repo.start(connection_id="conn_a")
    repo.finish(older, status="done")
    newer = repo.start(connection_id="conn_a")
    only = repo.start(connection_id="conn_b")

    latest = repo.list_latest_for_connections(["conn_a", "conn_b"])
    assert latest["conn_a"]["id"] == newer
    assert latest["conn_b"]["id"] == only


def test_list_latest_for_connections_omits_a_connection_with_no_run(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.start(connection_id="conn_a")

    latest = repo.list_latest_for_connections(["conn_a", "conn_never_run"])
    assert set(latest.keys()) == {"conn_a"}


def test_list_latest_for_connections_empty_ids_returns_empty(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.list_latest_for_connections([]) == {}


def test_list_latest_for_connections_running_only_excludes_finished_connections(pg_engine, monkeypatch):
    """The fleet view's default (active) scope: a connection whose newest
    run already finished has nothing to say about "is it on pace right now"
    and must be absent, not represented with a stale finished row."""
    repo = _make_repo(pg_engine, monkeypatch)
    finished = repo.start(connection_id="conn_a")
    repo.finish(finished, status="done")
    running = repo.start(connection_id="conn_b")

    latest = repo.list_latest_for_connections(["conn_a", "conn_b"], running_only=True)
    assert set(latest.keys()) == {"conn_b"}
    assert latest["conn_b"]["id"] == running


def test_list_latest_for_connections_running_only_still_prefers_newest_running_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    zombie = repo.start(connection_id="conn_a")
    repo.checkpoint(zombie, files_seen=10, files_done=10)
    current = repo.start(connection_id="conn_a")

    latest = repo.list_latest_for_connections(["conn_a"], running_only=True)
    assert latest["conn_a"]["id"] == current
