"""Cross-engine contract tests for the audit repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

This is the test that proves the dual-write window in the parent plan
(Phase 2 step 3) can work without invisible behaviour deltas.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.audit import AuditRepository

    # Route through _open_duckdb (not a bare duckdb.connect()) so this
    # connection gets the same `SET GLOBAL TimeZone='UTC'` pin every
    # production connection gets (src/duckdb_conn.py). prune_older_than
    # is the first audit method whose correctness hinges on DB-server
    # `current_timestamp` arithmetic rather than a Python-supplied
    # cutoff, so the boundary tests below need production's timezone
    # guarantee to be meaningful.
    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AuditRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.audit_pg import AuditPgRepository

    return AuditPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def audit_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# contract assertions — same SQL questions, same answers
# ---------------------------------------------------------------------------


def test_log_returns_id(audit_repo):
    repo, _, _ = audit_repo
    entry_id = repo.log(user_id="u1", action="auth.login")
    assert isinstance(entry_id, str)
    assert len(entry_id) > 0


def test_log_all_kwargs_round_trip(audit_repo):
    repo, _, _ = audit_repo
    entry_id = repo.log(
        user_id="u1",
        action="registry.update",
        resource="table:web_sessions",
        params={"after": {"cron": "*/15 * * * *"}},
        params_before={"cron": "0 */1 * * *"},
        client_ip="10.0.0.42",
        client_kind="web",
        correlation_id="corr-123",
        result="success",
        duration_ms=42,
    )
    rows, _cursor = repo.query(correlation_id="corr-123", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == entry_id
    assert row["user_id"] == "u1"
    assert row["action"] == "registry.update"
    assert row["resource"] == "table:web_sessions"
    assert row["client_ip"] == "10.0.0.42"
    assert row["client_kind"] == "web"
    assert row["correlation_id"] == "corr-123"
    assert row["result"] == "success"
    assert row["duration_ms"] == 42
    # JSON columns normalised to dict on read
    assert _as_dict(row["params"]) == {"after": {"cron": "*/15 * * * *"}}
    assert _as_dict(row["params_before"]) == {"cron": "0 */1 * * *"}


def test_query_time_range(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a.1")
    repo.log(action="a.2")
    # Need actual time-window narrowing; both impls let us set timestamp via
    # implementation-specific paths — just use a wide window to cover all rows.
    rows, _ = repo.query(since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    actions = {r["action"] for r in rows}
    assert {"a.1", "a.2"}.issubset(actions)


def test_query_action_prefix(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="sync.trigger")
    repo.log(action="sync.complete")
    repo.log(action="auth.login")
    rows, _ = repo.query(action_prefix="sync.")
    actions = {r["action"] for r in rows}
    assert actions == {"sync.trigger", "sync.complete"}


def test_query_action_in(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a")
    repo.log(action="b")
    repo.log(action="c")
    rows, _ = repo.query(action_in=["a", "c"])
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_filter_by_user(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="x")
    repo.log(user_id="u2", action="x")
    rows, _ = repo.query(user_id="u1")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"


def test_query_filter_by_resource(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", resource="table:a")
    repo.log(action="x", resource="table:b")
    rows, _ = repo.query(resource="table:a")
    assert len(rows) == 1
    assert rows[0]["resource"] == "table:a"


def test_query_result_pattern(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", result="success")
    repo.log(action="x", result="error.timeout")
    repo.log(action="x", result="error.permission")
    rows, _ = repo.query(result_pattern="error.%")
    assert {r["result"] for r in rows} == {"error.timeout", "error.permission"}


def test_query_full_text_q(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", params={"sql": "SELECT * FROM finance"})
    repo.log(action="x", params={"sql": "SELECT * FROM marketing"})
    rows, _ = repo.query(q="finance")
    assert len(rows) == 1


def test_query_ordering_newest_first(audit_repo):
    """Both impls must order by (timestamp DESC, id DESC)."""
    repo, _, _ = audit_repo
    import time

    repo.log(action="first")
    time.sleep(0.01)
    repo.log(action="second")
    time.sleep(0.01)
    repo.log(action="third")
    rows, _ = repo.query()
    actions_seen = [r["action"] for r in rows]
    # Most recent first
    assert actions_seen[0] == "third"
    assert actions_seen[-1] == "first"


def test_query_actions_helper(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a")
    repo.log(action="b")
    repo.log(action="c")
    rows = repo.query_actions(["a", "c"], limit=10)
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_for_resources_helper(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", resource="store_submission:abc")
    repo.log(action="y", resource="store_submission:abc")
    repo.log(action="z", resource="store_submission:def")
    rows = repo.query_for_resources(["store_submission:abc"], limit=10)
    assert all(r["resource"] == "store_submission:abc" for r in rows)
    assert len(rows) == 2


def test_query_cursor_pagination(audit_repo):
    repo, _, _ = audit_repo
    import time

    for i in range(5):
        repo.log(action=f"a.{i}")
        time.sleep(0.005)
    page1, c1 = repo.query(limit=2)
    assert len(page1) == 2
    assert c1 is not None
    page2, c2 = repo.query(limit=2, cursor=c1)
    assert len(page2) == 2
    page3, c3 = repo.query(limit=2, cursor=c2)
    assert len(page3) == 1
    assert c3 is None
    all_ids = {r["id"] for r in page1 + page2 + page3}
    assert len(all_ids) == 5


# ---------------------------------------------------------------------------
# aggregates — count_for_user / query_governance / facets / kpis
# ---------------------------------------------------------------------------


def test_count_for_user(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="a")
    repo.log(user_id="u1", action="b")
    repo.log(user_id="u2", action="c")
    assert repo.count_for_user("u1") == 2
    assert repo.count_for_user("u2") == 1
    assert repo.count_for_user("nobody") == 0


def test_query_governance_dual_prefix(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="corporate_memory.write")
    repo.log(action="km_write")  # legacy prefix
    repo.log(action="auth.login")  # neither prefix
    rows = repo.query_governance(limit=50)
    actions = {r["action"] for r in rows}
    assert actions == {"corporate_memory.write", "km_write"}


def test_query_governance_action_filter(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="corporate_memory.write")
    repo.log(action="km_write")
    repo.log(action="corporate_memory.delete")
    repo.log(action="km_delete")
    rows = repo.query_governance(action="write", limit=50)
    actions = {r["action"] for r in rows}
    assert actions == {"corporate_memory.write", "km_write"}


def test_query_governance_offset_paging(audit_repo):
    repo, _, _ = audit_repo
    import time

    for i in range(5):
        repo.log(action=f"corporate_memory.evt_{i}")
        time.sleep(0.005)
    page1 = repo.query_governance(limit=2, offset=0)
    page2 = repo.query_governance(limit=2, offset=2)
    page3 = repo.query_governance(limit=2, offset=4)
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    seen = {r["id"] for r in page1 + page2 + page3}
    assert len(seen) == 5


def test_facets_group_buckets(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", resource="r1", result="success", client_kind="web")
    repo.log(user_id="u1", action="a", resource="r1", result="success", client_kind="web")
    repo.log(user_id="u2", action="b", resource="r2", result="error.x", client_kind="cli")
    # scheduler classification is rule-based (action LIKE 'run_%'), not a
    # hardcoded action list — these two must land in 'scheduler' without any
    # caller-supplied fallback names.
    repo.log(user_id="u3", action="run_session_processor:usage")
    repo.log(user_id="u3", action="marketplace.sync_all")
    # NULL user + non-scheduler action → 'system'
    repo.log(user_id=None, action="job.enqueue")
    out = repo.facets(since=since, limit=50)
    assert set(out.keys()) == {"users", "actions", "results", "result_classes", "resources", "sources"}
    user_counts = {u["id"]: u["count"] for u in out["users"]}
    assert user_counts["u1"] == 2
    assert user_counts["u2"] == 1
    action_counts = {a["value"]: a["count"] for a in out["actions"]}
    assert action_counts["a"] == 2
    sources = {s["value"]: s["count"] for s in out["sources"]}
    assert sources.get("web") == 2
    assert sources.get("cli") == 1
    assert sources.get("scheduler") == 2
    assert sources.get("system") == 1


def test_query_rows_carry_computed_source(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="table.read", client_kind="cli")
    repo.log(user_id="u1", action="run_session_processor:usage")
    repo.log(user_id=None, action="job.enqueue")
    repo.log(user_id="u1", action="table.read")
    rows, _ = repo.query(limit=10)
    by_action_kind = {(r["action"], r["client_kind"]): r["source"] for r in rows}
    assert by_action_kind[("table.read", "cli")] == "cli"
    assert by_action_kind[("run_session_processor:usage", None)] == "scheduler"
    assert by_action_kind[("job.enqueue", None)] == "system"
    assert by_action_kind[("table.read", None)] == "other"


def test_kpis(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", result="success", duration_ms=100)
    repo.log(user_id="u1", action="b", result="success", duration_ms=200)
    repo.log(user_id="u2", action="c", result="error.timeout", duration_ms=300)
    repo.log(user_id=None, action="sys", result="success", duration_ms=400)
    out = repo.kpis(since=since)
    assert out["events_total"] == 4
    assert out["active_users"] == 2  # u1, u2 (NULL user excluded)
    assert out["errors"] == 1
    # p95 differs between approx_quantile (DuckDB) and percentile_cont (PG);
    # both should land in the upper range of {100,200,300,400}.
    assert out["p95"] is not None
    assert 250 <= out["p95"] <= 400


# ---------------------------------------------------------------------------
# last_scheduler_tick / active_users_since — Activity Center health pulse
# ---------------------------------------------------------------------------


def test_last_scheduler_tick_none_when_no_matching_rows(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="other_action", result="success")
    assert repo.last_scheduler_tick() is None


def test_last_scheduler_tick_matches_run_prefix_or_marketplace_sync(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="run_session_processor", result="success")
    repo.log(user_id="u1", action="marketplace.sync_all", result="success")
    repo.log(user_id="u1", action="unrelated", result="success")
    assert repo.last_scheduler_tick() is not None


def test_active_users_since_counts_distinct_non_null_user_ids(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", result="success")
    repo.log(user_id="u1", action="b", result="success")
    repo.log(user_id="u2", action="c", result="success")
    repo.log(user_id=None, action="sys", result="success")
    assert repo.active_users_since(since) == 2


def test_active_users_since_excludes_rows_before_window(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="a", result="success")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert repo.active_users_since(future) == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_dict(v):
    """Normalize ``params``/``params_before`` to a dict for cross-backend
    comparison. DuckDB JSON returns the parsed value; SQLAlchemy with
    psycopg returns dict too, but we accept str for safety."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        return json.loads(v)
    return v


# ---------------------------------------------------------------------------
# PR-B: facets/kpis honor the timeline filter set; result_class / source /
# include_self_reads filters (same semantics both backends)
# ---------------------------------------------------------------------------


def _seed_parity_rows(repo):
    repo.log(user_id="u1", action="table.read", result="success", client_kind="cli")
    repo.log(user_id="u1", action="table.read", result="ok", client_kind="cli")
    repo.log(user_id="u1", action="query.run", result="error.400", client_kind="web")
    repo.log(user_id="u2", action="query.run", result="denied", client_kind="web")
    repo.log(user_id="sched", action="run_session_processor:usage", result="success")
    repo.log(user_id=None, action="job.enqueue")
    repo.log(user_id="u1", action="activity.read", result="success", client_kind="web")


def test_kpis_honor_filters(audit_repo):
    repo, _, _ = audit_repo
    _seed_parity_rows(repo)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    k = repo.kpis(since=since, user_id="u1")
    # u1 rows: table.read x2, query.run, activity.read (self-read included by default)
    assert k["events_total"] == 4
    k2 = repo.kpis(since=since, user_id="u1", include_self_reads=False)
    assert k2["events_total"] == 3
    k3 = repo.kpis(since=since, source="cli")
    assert k3["events_total"] == 2


def test_kpis_active_users_excludes_system_actors(audit_repo):
    repo, _, _ = audit_repo
    _seed_parity_rows(repo)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    k = repo.kpis(since=since)
    # u1 + u2 are people; 'sched' rows classify as scheduler, NULL as system
    assert k["active_users"] == 2
    # errors counts result_class='error' only (denied is its own class)
    assert k["errors"] == 1
    assert 0.0 <= k["duration_coverage"] <= 1.0


def test_query_result_class_and_source_filters(audit_repo):
    repo, _, _ = audit_repo
    _seed_parity_rows(repo)
    rows, _ = repo.query(result_class="success", limit=50)
    assert {r["result"] for r in rows} == {"success", "ok"}
    rows, _ = repo.query(result_class="denied", limit=50)
    assert {r["result"] for r in rows} == {"denied"}
    rows, _ = repo.query(source="scheduler", limit=50)
    assert {r["action"] for r in rows} == {"run_session_processor:usage"}
    rows, _ = repo.query(include_self_reads=False, limit=50)
    assert "activity.read" not in {r["action"] for r in rows}


def test_facets_honor_filters(audit_repo):
    repo, _, _ = audit_repo
    _seed_parity_rows(repo)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    out = repo.facets(since=since, user_id="u1", include_self_reads=False)
    assert {a["value"] for a in out["actions"]} == {"table.read", "query.run"}
    assert [u["id"] for u in out["users"]] == ["u1"]
    # result_classes bucket present for the UI dropdown
    classes = {c["value"]: c["count"] for c in out["result_classes"]}
    assert classes["success"] == 2  # success + ok
    assert classes["error"] == 1


def test_log_autofills_duration_from_request_context(audit_repo):
    """duration_ms=None auto-fills from the request-timing contextvar in
    BOTH backends; outside a request scope it stays NULL."""
    import contextvars

    from src.audit_context import mark_request_start

    repo, _, _ = audit_repo

    def _in_fresh_context(fn):
        return contextvars.copy_context().run(fn)

    _in_fresh_context(lambda: repo.log(user_id="u1", action="no.scope"))

    def _scoped():
        mark_request_start()
        repo.log(user_id="u1", action="in.scope")

    _in_fresh_context(_scoped)
    rows, _ = repo.query(limit=10)
    by_action = {r["action"]: r["duration_ms"] for r in rows}
    assert by_action["no.scope"] is None
    assert by_action["in.scope"] is not None and by_action["in.scope"] >= 0


# ---------------------------------------------------------------------------
# B8: prune_older_than — retention-based audit_log pruning
# ---------------------------------------------------------------------------


def _backdate(audit_repo_tuple, entry_id: str, ts: datetime) -> None:
    """Rewrite one row's ``timestamp`` directly — ``log()`` always stamps
    ``now()``, so the prune tests need an implementation-specific path to
    plant an old row, exactly like ``test_query_time_range``'s docstring
    notes for the same reason."""
    repo, conn, backend = audit_repo_tuple
    if backend == "duckdb":
        conn.execute("UPDATE audit_log SET timestamp = ? WHERE id = ?", [ts, entry_id])
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text("UPDATE audit_log SET timestamp = :ts WHERE id = :id"),
                {"ts": ts, "id": entry_id},
            )


def test_prune_older_than_deletes_only_old_rows(audit_repo):
    repo, _, _ = audit_repo
    old_id = repo.log(action="old.one")
    new_id = repo.log(action="new.one")
    _backdate(audit_repo, old_id, datetime.now(timezone.utc) - timedelta(days=400))

    pruned = repo.prune_older_than(365)

    assert pruned == 1
    rows, _ = repo.query(limit=10)
    ids = {r["id"] for r in rows}
    assert old_id not in ids
    assert new_id in ids


def test_prune_older_than_returns_zero_when_nothing_qualifies(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="recent.one")
    repo.log(action="recent.two")

    pruned = repo.prune_older_than(365)

    assert pruned == 0
    rows, _ = repo.query(limit=10)
    assert len(rows) == 2


def test_prune_older_than_boundary_is_strict_less_than_on_both_backends(audit_repo):
    """Pins the DELETE's cutoff semantics at the boundary — both engines run
    ``timestamp < (current_timestamp - INTERVAL 'N days')`` (see
    ``AuditRepository.prune_older_than`` / ``AuditPgRepository.prune_older_than``),
    where ``current_timestamp`` is evaluated by the DATABASE SERVER at
    DELETE-execution time, not supplied by Python. That distinction is the
    whole point of this test: a Python-computed "exact cutoff" isn't the same
    instant the SQL sees, so the boundary case needs its own assertion rather
    than trusting the 400-day-old / 0-day-old cases above to generalize.

    Three rows around one nominal cutoff (``now - retention_days``, using a
    single Python ``datetime.now(timezone.utc)`` reference captured before
    any row is written):
      - ``+1 minute`` (inside the retention window)  -> must survive
      - ``-1 minute`` (outside the retention window)  -> must be pruned
      - exactly the nominal cutoff                    -> must be pruned

    The exact-cutoff row's fate is NOT a coin flip pinned arbitrarily: real
    wall-clock time elapses between capturing the Python ``now`` reference
    and the DELETE actually running (three ``log()`` INSERTs, three backdating
    UPDATEs, then the DELETE itself), so the database's own
    ``current_timestamp`` at DELETE time is always strictly LATER than the
    Python reference — which pushes the DB-computed cutoff (``db_now - N
    days``) strictly later than the row's timestamp (``python_now - N
    days``), landing the row on the strict-``<`` (pruned) side every time.
    Both backends must agree on this, since they share the identical
    ``timestamp < cutoff`` shape.
    """
    retention_days = 30
    repo, _, _ = audit_repo
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    survivor_id = repo.log(action="survivor")
    exact_boundary_id = repo.log(action="exact-boundary")
    pruned_id = repo.log(action="just-past-cutoff")

    _backdate(audit_repo, survivor_id, cutoff + timedelta(minutes=1))
    _backdate(audit_repo, exact_boundary_id, cutoff)
    _backdate(audit_repo, pruned_id, cutoff - timedelta(minutes=1))

    pruned_count = repo.prune_older_than(retention_days)

    rows, _ = repo.query(limit=10)
    remaining_ids = {r["id"] for r in rows}

    assert survivor_id in remaining_ids
    assert pruned_id not in remaining_ids
    # Boundary-semantics pin: strict `<` against the DB server's own clock
    # (not the Python reference) puts the exact-cutoff row on the PRUNED
    # side — identically on DuckDB and Postgres.
    assert exact_boundary_id not in remaining_ids
    assert pruned_count == 2


def test_upload_filenames_since_parses_params_on_both_engines(audit_repo):
    """PR-C: the reconciliation source — distinct session.upload filenames.
    Exercises the JSONB-vs-JSON-string params divergence between engines."""
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="session.upload", params={"bytes": 1, "filename": "aaa.jsonl"})
    repo.log(user_id="u1", action="session.upload", params={"bytes": 2, "filename": "aaa.jsonl"})  # dup → distinct
    repo.log(user_id="u2", action="session.upload", params={"bytes": 3, "filename": "bbb.jsonl"})
    repo.log(user_id="u2", action="session.upload", params={"bytes": 4})  # no filename
    repo.log(user_id="u2", action="other.action", params={"filename": "zzz.jsonl"})
    assert repo.upload_filenames_since(since) == ["aaa.jsonl", "bbb.jsonl"]


# ---------------------------------------------------------------------------
# E3 slice 2 — query_unified: the Activity Center timeline widened across
# audit_log + sync_history + llm_usage + agent_scope_snapshots (never
# chat_messages). Raw INSERTs into the three non-audit trail tables, since
# there is no repo-level writer for this test's purposes — same pattern as
# ``_backdate`` above (implementation-specific path per backend).
# ---------------------------------------------------------------------------


def _insert_sync_history(
    audit_repo_tuple, *, id_, table_id, synced_at, rows=0, duration_ms=None, status="ok", error=None
):
    repo, conn, backend = audit_repo_tuple
    if backend == "duckdb":
        conn.execute(
            "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [id_, table_id, synced_at, rows, duration_ms, status, error],
        )
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
                    "VALUES (:id, :table_id, :synced_at, :rows, :duration_ms, :status, :error)"
                ),
                {
                    "id": id_,
                    "table_id": table_id,
                    "synced_at": synced_at,
                    "rows": rows,
                    "duration_ms": duration_ms,
                    "status": status,
                    "error": error,
                },
            )


def _insert_llm_usage(
    audit_repo_tuple,
    *,
    id_,
    agent_id,
    user_id,
    session_id,
    model,
    input_tokens=10,
    output_tokens=20,
    cache_read_tokens=0,
    cache_creation_tokens=0,
    created_at=None,
):
    repo, conn, backend = audit_repo_tuple
    created_at = created_at or datetime.now(timezone.utc)
    if backend == "duckdb":
        conn.execute(
            "INSERT INTO llm_usage (id, agent_id, user_id, session_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_creation_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                id_,
                agent_id,
                user_id,
                session_id,
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                created_at,
            ],
        )
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO llm_usage (id, agent_id, user_id, session_id, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_creation_tokens, created_at) VALUES "
                    "(:id, :agent_id, :user_id, :session_id, :model, :input_tokens, :output_tokens, "
                    ":cache_read_tokens, :cache_creation_tokens, :created_at)"
                ),
                {
                    "id": id_,
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "created_at": created_at,
                },
            )


def _insert_agent_scope_snapshot(audit_repo_tuple, *, id_, session_id, agent_id, effective_scope, created_at=None):
    repo, conn, backend = audit_repo_tuple
    created_at = created_at or datetime.now(timezone.utc)
    if backend == "duckdb":
        conn.execute(
            "INSERT INTO agent_scope_snapshots (id, session_id, agent_id, effective_scope, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [id_, session_id, agent_id, effective_scope, created_at],
        )
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO agent_scope_snapshots (id, session_id, agent_id, effective_scope, created_at) "
                    "VALUES (:id, :session_id, :agent_id, :effective_scope, :created_at)"
                ),
                {
                    "id": id_,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "effective_scope": effective_scope,
                    "created_at": created_at,
                },
            )


def _insert_chat_message(audit_repo_tuple, *, session_id, message_id, content):
    """Plants one chat_sessions + chat_messages row (FK-required) so the
    privacy regression test has something a leaky projection COULD surface."""
    repo, conn, backend = audit_repo_tuple
    now = datetime.now(timezone.utc)
    if backend == "duckdb":
        conn.execute(
            "INSERT INTO chat_sessions (id, user_email, surface, started_at) VALUES (?, ?, ?, ?)",
            [session_id, "leak-probe@example.com", "web", now],
        )
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [message_id, session_id, "user", content, now],
        )
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO chat_sessions (id, user_email, surface, started_at) "
                    "VALUES (:id, :email, :surface, :started_at)"
                ),
                {"id": session_id, "email": "leak-probe@example.com", "surface": "web", "started_at": now},
            )
            c.execute(
                sa.text(
                    "INSERT INTO chat_messages (id, session_id, role, content, created_at) "
                    "VALUES (:id, :session_id, :role, :content, :created_at)"
                ),
                {"id": message_id, "session_id": session_id, "role": "user", "content": content, "created_at": now},
            )


def test_query_unified_folds_in_all_trails(audit_repo):
    repo, _, _ = audit_repo
    now = datetime.now(timezone.utc)
    repo.log(user_id="u1", action="table.read", resource="table:orders", result="success", client_kind="web")
    _insert_sync_history(audit_repo, id_="sh1", table_id="t_web_sessions", synced_at=now, rows=100, status="ok")
    _insert_llm_usage(audit_repo, id_="lu1", agent_id="agent-1", user_id="u2", session_id="sess-1", model="claude-x")
    _insert_agent_scope_snapshot(
        audit_repo, id_="ss1", session_id="sess-1", agent_id="agent-1", effective_scope='{"tables":["orders"]}'
    )

    rows, _ = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=50)
    by_trail = {r["trail"]: r for r in rows}
    assert set(by_trail) == {"audit", "sync", "llm", "agent_scope"}

    assert by_trail["audit"]["action"] == "table.read"

    sync_row = by_trail["sync"]
    assert sync_row["action"] == "sync.table"
    assert sync_row["resource"] == "table:t_web_sessions"
    assert sync_row["result"] == "ok"
    assert sync_row["source"] == "scheduler"
    assert sync_row["user_id"] is None

    llm_row = by_trail["llm"]
    assert llm_row["action"] == "llm.call"
    assert llm_row["resource"] == "agent:agent-1"
    assert llm_row["user_id"] == "u2"
    assert llm_row["source"] == "agent"

    scope_row = by_trail["agent_scope"]
    assert scope_row["action"] == "agent.spawn.scope"
    assert scope_row["resource"] == "agent:agent-1"
    assert scope_row["source"] == "agent"


def test_query_unified_orders_across_trails_newest_first(audit_repo):
    repo, _, _ = audit_repo
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _insert_sync_history(audit_repo, id_="sh-old", table_id="t1", synced_at=base, status="ok")
    repo_id = repo.log(action="mid.event")
    _insert_agent_scope_snapshot(
        audit_repo,
        id_="ss-new",
        session_id="s1",
        agent_id="a1",
        effective_scope="{}",
        created_at=base + timedelta(hours=2),
    )
    # Backdate the audit row deterministically between the other two.
    if audit_repo[2] == "duckdb":
        audit_repo[1].execute("UPDATE audit_log SET timestamp = ? WHERE id = ?", [base + timedelta(hours=1), repo_id])
    else:
        with repo._engine.begin() as c:
            c.execute(
                sa.text("UPDATE audit_log SET timestamp = :ts WHERE id = :id"),
                {"ts": base + timedelta(hours=1), "id": repo_id},
            )

    rows, _ = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=50)
    order = [r["trail"] for r in rows]
    assert order == ["agent_scope", "audit", "sync"]


def test_query_unified_trail_filter_narrows_to_one_trail(audit_repo):
    repo, _, _ = audit_repo
    now = datetime.now(timezone.utc)
    repo.log(action="audit.only")
    _insert_sync_history(audit_repo, id_="sh1", table_id="t1", synced_at=now, status="ok")
    _insert_llm_usage(audit_repo, id_="lu1", agent_id="a1", user_id="u1", session_id="s1", model="m")

    rows, _ = repo.query_unified(trail="audit", since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=50)
    assert {r["trail"] for r in rows} == {"audit"}
    assert {r["action"] for r in rows} == {"audit.only"}

    rows2, _ = repo.query_unified(trail="sync", since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=50)
    assert {r["trail"] for r in rows2} == {"sync"}


def test_query_unified_invalid_trail_raises(audit_repo):
    repo, _, _ = audit_repo
    with pytest.raises(ValueError):
        repo.query_unified(trail="not-a-real-trail")


def test_query_unified_cursor_pagination_across_trails(audit_repo):
    repo, _, _ = audit_repo
    import time

    repo.log(action="a.1")
    time.sleep(0.005)
    repo.log(action="a.2")
    time.sleep(0.005)
    _insert_sync_history(audit_repo, id_="sh1", table_id="t1", synced_at=datetime.now(timezone.utc), status="ok")
    time.sleep(0.005)
    _insert_llm_usage(audit_repo, id_="lu1", agent_id="a1", user_id="u1", session_id="s1", model="m")
    time.sleep(0.005)
    _insert_agent_scope_snapshot(audit_repo, id_="ss1", session_id="s1", agent_id="a1", effective_scope="{}")

    page1, c1 = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=2)
    assert len(page1) == 2
    assert c1 is not None
    page2, c2 = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=2, cursor=c1)
    assert len(page2) == 2
    assert c2 is not None
    page3, c3 = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=2, cursor=c2)
    assert len(page3) == 1
    assert c3 is None
    seen_trails = {r["trail"] for r in page1 + page2 + page3}
    assert seen_trails == {"audit", "sync", "llm", "agent_scope"}
    seen_ids = {r["id"] for r in page1 + page2 + page3}
    assert len(seen_ids) == 5


def test_query_unified_result_class_and_resource_prefix_filters_apply_across_trails(audit_repo):
    """The unified projection reuses ``_filters_where`` unchanged — prove a
    filter that only ever matched audit_log rows before now also reaches
    into the folded-in trails."""
    repo, _, _ = audit_repo
    now = datetime.now(timezone.utc)
    _insert_sync_history(audit_repo, id_="sh-ok", table_id="t_ok", synced_at=now, status="ok")
    _insert_sync_history(audit_repo, id_="sh-err", table_id="t_err", synced_at=now, status="error")

    rows, _ = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), result_class="success", limit=50)
    assert {r["resource"] for r in rows} == {"table:t_ok"}

    rows2, _ = repo.query_unified(
        since=datetime(2000, 1, 1, tzinfo=timezone.utc), resource_prefix="table:t_err", limit=50
    )
    assert {r["resource"] for r in rows2} == {"table:t_err"}


def test_query_unified_never_surfaces_chat_messages(audit_repo):
    """Privacy regression (E3 slice 2): chat transcript content must never
    leak into the Activity Center timeline, unfiltered."""
    repo, _, _ = audit_repo
    secret = "the customer's Q3 churn number is 4.2% — do not repeat this"
    _insert_chat_message(audit_repo, session_id="leak-sess", message_id="leak-msg", content=secret)
    repo.log(action="unrelated.audit.row")

    rows, _ = repo.query_unified(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=200)
    assert "chat" not in {r["trail"] for r in rows}
    blob = json.dumps(rows, default=str)
    assert secret not in blob
    assert "leak-sess" not in blob
    assert "leak-msg" not in blob
