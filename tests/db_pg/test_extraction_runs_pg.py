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


def test_fail_for_job_closes_the_running_row_it_owns(pg_engine, monkeypatch):
    """2026-09 incident: the worker runtime calls this the moment a job's
    OWN `jobs` row reaches 'failed' (attempts exhausted, or an unhandled
    exception past the last retry) — the matching `extraction_runs` row
    must flip to 'failed' too, with the job's error and a `finished_at`,
    in the SAME write."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a", job_id="job-1")

    closed = repo.fail_for_job("job-1", error="lease expired after max attempts")

    assert closed == run_id
    row = repo.get(run_id)
    assert row["status"] == "failed"
    assert row["error"] == "lease expired after max attempts"
    assert row["finished_at"] is not None


def test_fail_for_job_is_a_no_op_when_no_running_row_matches(pg_engine, monkeypatch):
    """No job_id ever opened a row (most job kinds), or the run already
    finalized itself — either way, nothing to close, and no OTHER
    connection's row is ever touched."""
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.fail_for_job("job-never-seen", error="boom") is None

    run_id = repo.start(connection_id="conn_a", job_id="job-2")
    repo.finish(run_id, status="done", report={"new": 1})
    assert repo.fail_for_job("job-2", error="too late") is None
    assert repo.get(run_id)["status"] == "done"


def test_fail_for_job_never_touches_a_different_jobs_row(pg_engine, monkeypatch):
    """A NEW run for the same connection under a fresh job_id must never be
    closed by an OLDER job's own exhaustion sweep firing late."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.start(connection_id="conn_a", job_id="job-old")
    new_run = repo.start(connection_id="conn_a", job_id="job-new")

    closed = repo.fail_for_job("job-old", error="stale reclaim sweep")

    assert closed != new_run
    assert repo.get(new_run)["status"] == "running"


def test_last_failed_finds_the_newest_failed_run_only(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    older = repo.start(connection_id="conn_a")
    repo.finish(older, status="failed", error="first boom")
    newer = repo.start(connection_id="conn_a")
    repo.finish(newer, status="failed", error="second boom")
    repo.start(connection_id="conn_a")  # still running — must never win

    row = repo.last_failed("conn_a")
    assert row["id"] == newer
    assert row["error"] == "second boom"


def test_last_failed_is_none_with_nothing_failed(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    done = repo.start(connection_id="conn_a")
    repo.finish(done, status="done", report={"new": 5})

    assert repo.last_failed("conn_a") is None


def test_list_for_connection_is_newest_first_and_scoped(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    first = repo.start(connection_id="conn_a")
    second = repo.start(connection_id="conn_a")
    repo.start(connection_id="conn_b")

    rows = repo.list_for_connection("conn_a", limit=10)
    ids = [r["id"] for r in rows]
    assert set(ids) == {first, second}
    assert ids.index(second) < ids.index(first)


def test_list_for_connection_strips_failed_and_skipped_items_but_get_keeps_them(pg_engine, monkeypatch):
    """The LIST projection (history drawer) never needs the itemized
    ``failed_items``/``skipped_items`` bodies — only the single-run detail
    endpoint (:meth:`get`) does. A run report can carry thousands of these,
    each with a path and error text, so shipping them in a list response
    made the drawer's own byte count scale with how badly one run failed."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    report = {
        "new": 3,
        "failed_items": [{"path": f"/f{i}", "reason": "convert_error"} for i in range(50)],
        "skipped_items": [{"path": f"/s{i}", "reason": "unsupported"} for i in range(50)],
    }
    repo.finish(run_id, status="done", report=report, files_seen=4, files_done=4)

    listed = repo.list_for_connection("conn_a", limit=10)[0]
    assert "failed_items" not in listed["report"]
    assert "skipped_items" not in listed["report"]
    assert listed["report"]["new"] == 3  # every OTHER report key survives

    detail = repo.get(run_id)
    assert len(detail["report"]["failed_items"]) == 50
    assert len(detail["report"]["skipped_items"]) == 50


def test_list_latest_for_connections_strips_failed_and_skipped_items(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="conn_a")
    repo.finish(
        run_id,
        status="done",
        report={"failed_items": [{"path": "/f0"}], "skipped_items": [{"path": "/s0"}]},
        files_seen=1,
        files_done=1,
    )
    latest = repo.list_latest_for_connections(["conn_a"], running_only=False)
    assert "failed_items" not in latest["conn_a"]["report"]
    assert "skipped_items" not in latest["conn_a"]["report"]


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


# ---------------------------------------------------------------------------
# repoint_connection — split-merge (POST …/splits/merge) folding a sibling's
# run history onto the surviving target connection.
# ---------------------------------------------------------------------------


def test_repoint_connection_moves_every_run_and_marks_merged_from(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="sibling-1")
    repo.finish(run_id, status="done", report={"files": 3})

    moved = repo.repoint_connection(from_connection_id="sibling-1", to_connection_id="target-1")
    assert moved == 1

    row = repo.get(run_id)
    assert row["connection_id"] == "target-1"
    assert row["progress"]["merged_from"] == "sibling-1"
    # Everything else about the row is untouched.
    assert row["report"] == {"files": 3}
    assert row["status"] == "done"


def test_repoint_connection_never_overwrites_an_existing_merged_from(pg_engine, monkeypatch):
    """A run already carried over by an earlier merge keeps its ORIGINAL
    connection on `merged_from`, even if it is folded a second time (a
    merge target that is itself later merged into a bigger target)."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = repo.start(connection_id="sibling-1")
    repo.repoint_connection(from_connection_id="sibling-1", to_connection_id="mid")
    repo.repoint_connection(from_connection_id="mid", to_connection_id="final")

    row = repo.get(run_id)
    assert row["connection_id"] == "final"
    assert row["progress"]["merged_from"] == "sibling-1"


def test_repoint_connection_with_no_runs_returns_zero(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.repoint_connection(from_connection_id="never-ran", to_connection_id="target-1") == 0


def test_repoint_connection_only_touches_the_named_source(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    other = repo.start(connection_id="conn_b")
    repo.start(connection_id="sibling-1")

    repo.repoint_connection(from_connection_id="sibling-1", to_connection_id="target-1")

    assert repo.get(other)["connection_id"] == "conn_b"


# Shard-crawl columns/methods (2026-09-03 auto-parallel-crawl design §4.2/
# §4.3, migration 0103_crawl_shards).
# ---------------------------------------------------------------------------


def test_start_accepts_shard_columns(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=3)
    child = repo.start(
        connection_id="conn_a",
        parent_run_id=parent,
        shard_key="drv1:folder1",
        shard_label="Reports/Q1",
    )

    parent_row = repo.get(parent)
    assert parent_row["shards_total"] == 3
    assert parent_row["shards_done"] == 0
    assert parent_row["parent_run_id"] is None

    child_row = repo.get(child)
    assert child_row["parent_run_id"] == parent
    assert child_row["shard_key"] == "drv1:folder1"
    assert child_row["shard_label"] == "Reports/Q1"
    assert child_row["shards_total"] is None


def test_get_running_never_returns_a_shard_childs_own_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=2)
    repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv1")

    running = repo.get_running("conn_a")
    assert running["id"] == parent


def test_list_latest_for_connections_never_returns_a_shard_childs_own_row(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=1)
    repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv1")

    latest = repo.list_latest_for_connections(["conn_a"])
    assert latest["conn_a"]["id"] == parent


def test_list_latest_for_connections_carries_the_shard_columns(pg_engine, monkeypatch):
    """A LIST projection's own column set (``_RUN_LIST_COLUMNS``) must
    carry ``shards_total``/``shards_done`` — a caller deriving "is this a
    sharded run" (``app.api.admin_extraction._run_out``'s ``mode``) off a
    row read through this method, not :meth:`get`/:meth:`get_running`, must
    see the same columns either way. Regression: these two were missing
    from the LIST projection through 2026-09-03, so every fleet/history row
    for a sharded site silently reported ``mode: "inline"``."""
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=3)

    latest = repo.list_latest_for_connections(["conn_a"])
    assert latest["conn_a"]["id"] == parent
    assert latest["conn_a"]["shards_total"] == 3
    assert latest["conn_a"]["shards_done"] == 0


def test_list_for_connection_and_count_never_include_shard_children(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=2)
    repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv1")
    repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv2")

    rows = repo.list_for_connection("conn_a", limit=10)
    assert [r["id"] for r in rows] == [parent]
    assert rows[0]["shards_total"] == 2  # same LIST-projection regression as list_latest_for_connections above
    assert repo.count_for_connection("conn_a") == 1


def test_last_completed_and_last_failed_never_include_shard_children(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=1)
    child = repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv1")
    repo.finish(child, status="failed", error="boom")
    repo.finish(parent, status="failed", error="a shard failed: drv1")

    assert repo.last_completed("conn_a") is None
    failed = repo.last_failed("conn_a")
    assert failed["id"] == parent


def test_bump_parent_checkpoint_advances_the_parents_checkpoint_at(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=1)
    before = repo.get(parent)["checkpoint_at"]

    repo.bump_parent_checkpoint(parent)

    assert repo.get(parent)["checkpoint_at"] >= before


def test_bump_parent_checkpoint_is_a_no_op_once_the_parent_finished(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=1)
    repo.finish(parent, status="done")
    finished_at = repo.get(parent)["checkpoint_at"]

    repo.bump_parent_checkpoint(parent)

    assert repo.get(parent)["checkpoint_at"] == finished_at


def test_finish_shard_increments_shards_done_and_returns_the_totals(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=2)

    first = repo.finish_shard(parent)
    assert first == {"shards_done": 1, "shards_total": 2}
    second = repo.finish_shard(parent)
    assert second == {"shards_done": 2, "shards_total": 2}

    assert repo.get(parent)["shards_done"] == 2


def test_finish_shard_on_an_unknown_parent_returns_none(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.finish_shard("er_never_existed") is None


def test_claim_finalize_wins_exactly_once_under_a_simulated_race(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=2)

    winners = [repo.claim_finalize(parent) for _ in range(5)]

    assert winners.count(True) == 1
    assert repo.get(parent)["phase"] == "finalizing"


def test_children_for_returns_every_child_keyed_by_parent(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent_a = repo.start(connection_id="conn_a", shards_total=2)
    parent_b = repo.start(connection_id="conn_b", shards_total=1)
    child_a1 = repo.start(connection_id="conn_a", parent_run_id=parent_a, shard_key="drv1")
    child_a2 = repo.start(connection_id="conn_a", parent_run_id=parent_a, shard_key="drv2")
    child_b1 = repo.start(connection_id="conn_b", parent_run_id=parent_b, shard_key="drv1")

    children = repo.children_for([parent_a, parent_b])

    assert {c["id"] for c in children[parent_a]} == {child_a1, child_a2}
    assert {c["id"] for c in children[parent_b]} == {child_b1}


def test_children_for_a_parent_with_no_children_yet_is_an_empty_list(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=3)

    children = repo.children_for([parent])
    assert children == {parent: []}


def test_children_for_empty_ids_returns_empty(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.children_for([]) == {}


def test_children_for_strips_failed_and_skipped_items_but_keeps_shard_key(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    parent = repo.start(connection_id="conn_a", shards_total=1)
    child = repo.start(connection_id="conn_a", parent_run_id=parent, shard_key="drv1", shard_label="Docs")
    repo.finish(
        child,
        status="done",
        report={"new": 5, "failed_items": [{"path": "/f"}], "skipped_items": [{"path": "/s"}]},
    )

    rows = repo.children_for([parent])[parent]
    assert len(rows) == 1
    assert rows[0]["shard_key"] == "drv1"
    assert rows[0]["shard_label"] == "Docs"
    assert "failed_items" not in rows[0]["report"]
    assert rows[0]["report"]["new"] == 5
