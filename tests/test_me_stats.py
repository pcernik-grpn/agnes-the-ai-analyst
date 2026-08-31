"""/api/me/stats/* — per-user dashboard endpoints.

Coverage:
- All four endpoints scope rows to ``user["id"]`` so user A cannot read
  user B's data (gates are server-side; the page renders a shell with no
  caller-scope params). Summary rows are seeded the way the pipeline
  writes them: ``username`` = display email, ``user_id`` = account id.
- Empty user returns zero-counts / empty arrays, not 500.
- Tokens aggregates daily series, by-model, top-N, and lifetime totals
  from a seeded sample.
- Sync activity surfaces both ``manifest.fetch`` and ``sync.trigger``,
  filtered to caller.
- GET /api/sync/manifest writes the ``manifest.fetch`` audit row.
"""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from src.db import _SYSTEM_SCHEMA
from src.repositories.audit import AuditRepository


@pytest.fixture
def stats_conn(tmp_path, monkeypatch):
    """Point the singleton ``get_system_db`` at an isolated per-test DB.

    Endpoints in ``app.api.me_stats`` now resolve their repos through the
    factory in ``src.repositories``, which calls ``get_system_db()``. The
    test fixture must therefore mutate the global singleton (not just
    open a new file by itself) so endpoint-side reads see the same rows
    the test writes.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    from src.db import close_system_db, get_system_db
    close_system_db()
    conn = get_system_db()
    return conn


def _seed_user(conn, *, uid, email):
    conn.execute(
        "INSERT INTO users (id, email, active, onboarded) VALUES (?, ?, TRUE, TRUE)",
        [uid, email],
    )


def _seed_session(conn, *, sf, user_id, username=None, started_sql,
                  model="claude-opus-4-7",
                  user_messages=0, tool_calls=0,
                  input_tokens=0, output_tokens=0,
                  cache_read=0, cache_creation=0):
    """Insert a summary row the way ``session_pipeline.runner`` writes it:
    ``user_id`` is the account id every self-scoped read filters on, and
    ``username`` is the display email (v60 canonicalization)."""
    conn.execute(
        f"""
        INSERT INTO usage_session_summary
          (session_file, session_id, username, user_id, started_at, ended_at,
           active_seconds, wall_seconds, user_messages, assistant_messages,
           tool_calls, tool_errors, skill_invocations, subagent_dispatches,
           mcp_calls, slash_commands, distinct_tools, distinct_skills,
           primary_model, input_tokens, output_tokens, cache_read_tokens,
           cache_creation_tokens, processor_version)
        VALUES (?, ?, ?, ?, {started_sql}, current_timestamp,
                10, 30, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, 2)
        """,
        [sf, sf, username or f"{user_id}@example.com", user_id,
         user_messages, user_messages,
         tool_calls, model, input_tokens, output_tokens,
         cache_read, cache_creation],
    )


# ---------------------------------------------------------------------------
# Sessions tab
# ---------------------------------------------------------------------------


def test_sessions_endpoint_scopes_to_caller(stats_conn, tmp_path, monkeypatch):
    """User A's sessions endpoint must not return user B's rows."""
    # Point session-fs scan at an empty dir so unprocessed-jsonl path is no-op.
    monkeypatch.setenv("AGNES_SESSION_DATA_DIR", str(tmp_path / "noop"))

    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    _seed_user(stats_conn, uid="ub", email="bob@example.com")
    _seed_session(stats_conn, sf="a1.jsonl", user_id="ua",
                  started_sql="current_timestamp - INTERVAL 1 HOUR",
                  user_messages=4, input_tokens=100, output_tokens=50)
    _seed_session(stats_conn, sf="b1.jsonl", user_id="ub",
                  started_sql="current_timestamp - INTERVAL 1 HOUR",
                  user_messages=9, input_tokens=999, output_tokens=999)

    from app.api.me_stats import list_self_sessions
    res_a = list_self_sessions(
        limit=50, offset=0,
        user={"id": "ua", "email": "alice@example.com"},
        conn=stats_conn,
    )
    assert res_a["total"] == 1
    assert res_a["rows"][0]["session_file"] == "a1.jsonl"
    assert res_a["rows"][0]["user_messages"] == 4
    assert res_a["rows"][0]["tokens_total"] == 150


def test_sessions_endpoint_pagination(stats_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_SESSION_DATA_DIR", str(tmp_path / "noop"))
    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    for i in range(5):
        _seed_session(stats_conn, sf=f"s{i}.jsonl", user_id="ua",
                      started_sql=f"current_timestamp - INTERVAL {i} HOUR",
                      user_messages=i)

    from app.api.me_stats import list_self_sessions
    page1 = list_self_sessions(
        limit=2, offset=0,
        user={"id": "ua", "email": "alice@example.com"}, conn=stats_conn,
    )
    page2 = list_self_sessions(
        limit=2, offset=2,
        user={"id": "ua", "email": "alice@example.com"}, conn=stats_conn,
    )
    assert page1["total"] == 5
    assert len(page1["rows"]) == 2
    assert len(page2["rows"]) == 2
    assert page1["rows"][0]["session_file"] != page2["rows"][0]["session_file"]


# ---------------------------------------------------------------------------
# Tokens tab
# ---------------------------------------------------------------------------


def test_tokens_endpoint_empty_user(stats_conn):
    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    from app.api.me_stats import get_tokens
    res = get_tokens(
        days=30,
        user={"id": "ua", "email": "alice@example.com"},
        conn=stats_conn,
    )
    assert res["totals"]["total"] == 0
    assert res["daily"] == []
    assert res["by_model"] == []
    assert res["top_sessions"] == []


def test_tokens_endpoint_aggregates(stats_conn):
    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    _seed_session(stats_conn, sf="x.jsonl", user_id="ua",
                  started_sql="current_timestamp - INTERVAL 1 HOUR",
                  model="claude-opus-4-7",
                  input_tokens=100, output_tokens=50,
                  cache_read=800, cache_creation=25)
    _seed_session(stats_conn, sf="y.jsonl", user_id="ua",
                  started_sql="current_timestamp - INTERVAL 2 DAY",
                  model="claude-sonnet-4-6",
                  input_tokens=200, output_tokens=100,
                  cache_read=400, cache_creation=10)
    # Far-past row excluded by `days=7` window for the daily series, but
    # still counted in lifetime totals + by_model + top.
    _seed_session(stats_conn, sf="z.jsonl", user_id="ua",
                  started_sql="current_timestamp - INTERVAL 60 DAY",
                  model="claude-opus-4-7",
                  input_tokens=1, output_tokens=1)

    from app.api.me_stats import get_tokens
    res = get_tokens(
        days=7,
        user={"id": "ua", "email": "alice@example.com"},
        conn=stats_conn,
    )
    # Lifetime totals include all three sessions
    assert res["totals"]["sessions"] == 3
    assert res["totals"]["input"] == 301
    assert res["totals"]["output"] == 151
    assert res["totals"]["cache_read"] == 1200
    assert res["totals"]["cache_creation"] == 35
    assert res["totals"]["total"] == 1687

    # Daily series window excludes the 60-day-old row
    daily_sessions = sum(d["sessions"] for d in res["daily"])
    assert daily_sessions == 2

    # By-model: opus has 2 sessions (x + z), sonnet has 1 (y)
    models = {m["model"]: m for m in res["by_model"]}
    assert models["claude-opus-4-7"]["sessions"] == 2
    assert models["claude-sonnet-4-6"]["sessions"] == 1

    # Top sessions: largest first
    assert res["top_sessions"][0]["session_file"] in ("x.jsonl", "y.jsonl")
    assert res["top_sessions"][0]["total"] >= res["top_sessions"][1]["total"]


# ---------------------------------------------------------------------------
# Data access tab
# ---------------------------------------------------------------------------


def test_queries_endpoint_filters_to_query_actions(stats_conn):
    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    repo = AuditRepository(stats_conn)
    repo.log(user_id="ua", action="query.local", resource="orders",
             result="ok", duration_ms=42)
    repo.log(user_id="ua", action="query.remote", resource="web_sessions",
             result="ok", duration_ms=1500)
    # Non-query action — must not appear
    repo.log(user_id="ua", action="manifest.fetch", result="ok")
    # Same query action but different user — must not appear
    repo.log(user_id="ub", action="query.local", result="ok")

    from app.api.me_stats import list_self_queries
    res = list_self_queries(
        limit=50, cursor_ts=None, cursor_id=None,
        user={"id": "ua", "email": "alice@example.com"}, conn=stats_conn,
    )
    assert len(res["rows"]) == 2
    actions = {r["action"] for r in res["rows"]}
    assert actions == {"query.local", "query.remote"}


# ---------------------------------------------------------------------------
# Sync activity tab
# ---------------------------------------------------------------------------


def test_sync_endpoint_returns_manifest_fetch_rows(stats_conn):
    _seed_user(stats_conn, uid="ua", email="alice@example.com")
    repo = AuditRepository(stats_conn)
    repo.log(user_id="ua", action="manifest.fetch", resource="manifest",
             result="ok", client_kind="api")
    repo.log(user_id="ua", action="sync.trigger", result="ok")
    # Other user — must not leak
    repo.log(user_id="ub", action="manifest.fetch", result="ok")
    # Unrelated action — must not surface
    repo.log(user_id="ua", action="query.local", result="ok")
    # Stamp last_pull_at so the header card has a value
    stats_conn.execute(
        "UPDATE users SET last_pull_at = current_timestamp WHERE id = ?",
        ["ua"],
    )

    from app.api.me_stats import list_self_sync_activity
    res = list_self_sync_activity(
        limit=50, cursor_ts=None, cursor_id=None,
        user={"id": "ua", "email": "alice@example.com"}, conn=stats_conn,
    )
    actions = {r["action"] for r in res["rows"]}
    assert actions == {"manifest.fetch", "sync.trigger"}
    assert res["last_pull_at"] is not None


# ---------------------------------------------------------------------------
# Manifest endpoint writes the audit_log row
# ---------------------------------------------------------------------------


def test_sync_manifest_writes_audit_row(stats_conn, monkeypatch, tmp_path):
    """GET /api/sync/manifest must emit a manifest.fetch audit_log row
    so the Sync activity tab can list per-pull history."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _seed_user(stats_conn, uid="ua", email="alice@example.com")

    from app.api.sync import sync_manifest
    sync_manifest(
        user={"id": "ua", "email": "alice@example.com"},
        conn=stats_conn,
    )
    rows = stats_conn.execute(
        "SELECT action, resource, result, client_kind FROM audit_log "
        "WHERE user_id = ? ORDER BY timestamp DESC",
        ["ua"],
    ).fetchall()
    assert len(rows) == 1
    action, resource, result, client_kind = rows[0]
    assert action == "manifest.fetch"
    assert resource == "manifest"
    assert result == "success"
    assert client_kind == "api"
