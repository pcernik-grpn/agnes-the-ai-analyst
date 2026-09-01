"""Backend-parity tests for the admin telemetry + sessions endpoints.

Every endpoint here used to read usage_events / usage_session_summary /
audit_log off a raw DuckDB ``_get_db`` connection — which silently hit the
wrong backend on a Postgres instance. The fix routes each aggregate read
through ``usage_repo()`` (factory), so the same assertions must hold on both
backends.

Covered endpoints:
  * GET /api/admin/telemetry/summary
  * GET /api/admin/telemetry/facets
  * GET /api/admin/telemetry/kpis
  * GET /api/admin/telemetry/query   (grouped + ungrouped)
  * GET /api/admin/sessions/list
  * GET /api/admin/sessions/kpis
  * GET /api/admin/sessions/facets

Seeds go through the factory (``usage_repo()`` / ``audit_repo()``) so the
write lands on whichever backend the parametrized fixture selected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def _event(
    *,
    username: str,
    tool_name: str | None,
    is_error: bool = False,
    source: str = "claude_code",
    event_type: str = "tool_use",
    occurred_at: datetime | None = None,
) -> dict:
    eid = uuid.uuid4().hex
    return {
        "id": eid,
        "session_id": f"sess-{eid[:8]}",
        "session_file": f"{username}/{eid[:8]}.jsonl",
        "username": username,
        "event_uuid": None,
        "parent_uuid": None,
        "event_type": event_type,
        "tool_name": tool_name,
        "skill_name": None,
        "subagent_type": None,
        "command_name": None,
        "is_error": is_error,
        "source": source,
        "ref_id": None,
        "model": "claude-x",
        "cwd": None,
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "user_id": None,
    }


def _seed_events(repo) -> None:
    """5 Bash (1 error), 3 Read, all for two users, within the window.

    Use a timestamp ~5 days in the past: recent enough to fall inside the
    7d/30d windows AND inside the 30-entry DAU sparkline series (which spans
    [today-30, today-1] — today itself is excluded by the series builder).
    """
    when = datetime.now(timezone.utc) - timedelta(days=5)
    rows = (
        [_event(username="alice@test.com", tool_name="Bash", occurred_at=when) for _ in range(4)]
        + [_event(username="alice@test.com", tool_name="Bash", is_error=True, occurred_at=when)]
        + [_event(username="bob@test.com", tool_name="Read", occurred_at=when) for _ in range(3)]
    )
    repo.upsert_events(rows, processor_version=1)


def _seed_summary(repo, session_file: str, username: str, **over) -> None:
    summary = {
        "session_file": session_file,
        "session_id": session_file.split("/", 1)[-1],
        "username": username,
        "started_at": datetime.now(timezone.utc),
        "ended_at": datetime.now(timezone.utc),
        "active_seconds": 100,
        "wall_seconds": 200,
        "user_messages": 5,
        "assistant_messages": 5,
        "tool_calls": over.get("tool_calls", 10),
        "tool_errors": over.get("tool_errors", 0),
        "mcp_calls": over.get("mcp_calls", 0),
        "subagent_dispatches": over.get("subagent_dispatches", 0),
        "primary_model": over.get("primary_model", "claude-x"),
    }
    repo.upsert_summary(summary, processor_version=1)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# /api/admin/telemetry/summary
# ---------------------------------------------------------------------------


def test_telemetry_summary(seeded_app_both):
    from src.repositories import usage_repo, audit_repo

    _seed_events(usage_repo())
    # Seed audit durations so slow_actions has >= 5 samples for one action.
    for _ in range(6):
        audit_repo().log(
            user_id="admin1",
            action="data.export",
            result="success",
            duration_ms=120,
            client_kind="web",
        )

    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/summary?window=30d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    tools = {t["tool_name"]: t for t in body["top_tools"]}
    assert tools["Bash"]["invocations"] == 5
    assert tools["Read"]["invocations"] == 3

    users = {u["username"]: u["tool_calls"] for u in body["top_users"]}
    assert users["alice@test.com"] == 5
    assert users["bob@test.com"] == 3

    err = {e["tool_name"]: e for e in body["error_rate"]}
    assert err["Bash"]["errors"] == 1
    assert err["Bash"]["invocations"] == 5

    assert len(body["dau_series"]) == 30
    assert any(d["active_users"] >= 2 for d in body["dau_series"])

    slow = {s["action"]: s for s in body["slow_actions"]}
    assert "data.export" in slow
    assert slow["data.export"]["n"] == 6


def test_telemetry_summary_forbids_non_admin(seeded_app_both):
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/summary",
        headers=_h(seeded_app_both["analyst_token"]),
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# /api/admin/telemetry/facets
# ---------------------------------------------------------------------------


def test_telemetry_facets(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/facets",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    tool_values = {t["value"] for t in body["tools"]}
    assert {"Bash", "Read"} <= tool_values
    user_values = {u["value"] for u in body["users"]}
    assert {"alice@test.com", "bob@test.com"} <= user_values


# ---------------------------------------------------------------------------
# /api/admin/telemetry/kpis
# ---------------------------------------------------------------------------


def test_telemetry_kpis(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/kpis",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events_total"] == 8
    assert body["distinct_users"] == 2
    assert body["distinct_tools"] == 2
    assert body["errors"] == 1
    assert round(body["error_rate"], 4) == round(1 / 8, 4)


def test_telemetry_kpis_filtered_by_tool(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/kpis?tool_name=Bash&only_errors=true",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events_total"] == 1
    assert body["errors"] == 1


# ---------------------------------------------------------------------------
# /api/admin/telemetry/query  (grouped)
# ---------------------------------------------------------------------------


def test_telemetry_query_grouped_by_tool(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/query?group_by=tool_name&sort=invocations:desc",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] == "tool_name"
    assert body["group_alias"] == "tool_name"
    buckets = {row["bucket"]: row for row in body["rows"]}
    assert "Bash" in buckets
    assert "Read" in buckets
    assert buckets["Bash"]["invocations"] == 5
    assert buckets["Read"]["invocations"] == 3
    assert buckets["Bash"]["errors"] == 1
    # total = number of distinct tool_name values
    assert body["total"] == 2


def test_telemetry_query_ungrouped(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/query?limit=20",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] is None
    assert body["total"] == 8
    assert len(body["rows"]) == 8
    # each row carries occurred_at as ISO string
    for row in body["rows"]:
        assert isinstance(row["occurred_at"], str)


def test_telemetry_query_ungrouped_filter(seeded_app_both):
    from src.repositories import usage_repo

    _seed_events(usage_repo())
    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/telemetry/query?tool_name=Bash&only_errors=true",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["is_error"] is True


# ---------------------------------------------------------------------------
# /api/admin/sessions/list  +  /kpis  +  /facets
# ---------------------------------------------------------------------------


def test_sessions_list(seeded_app_both):
    from src.repositories import usage_repo

    repo = usage_repo()
    _seed_summary(
        repo,
        "alice@test.com/s1.jsonl",
        "alice@test.com",
        tool_calls=10,
        tool_errors=2,
        mcp_calls=7,
        subagent_dispatches=1,
    )
    _seed_summary(repo, "bob@test.com/s2.jsonl", "bob@test.com", tool_calls=4, tool_errors=0)

    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/sessions/list",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    by_user = {row["username"]: row for row in body["rows"]}
    assert by_user["alice@test.com"]["tool_calls"] == 10
    assert by_user["alice@test.com"]["tool_errors"] == 2
    # The list rows carry the call breakdown so the UI can total them.
    assert by_user["alice@test.com"]["mcp_calls"] == 7
    assert by_user["alice@test.com"]["subagent_dispatches"] == 1
    # session_dir derived from the "<dir>/<file>" session_file shape.
    assert by_user["alice@test.com"]["session_dir"] == "alice@test.com"


def test_sessions_list_filter_only_errors(seeded_app_both):
    from src.repositories import usage_repo

    repo = usage_repo()
    _seed_summary(repo, "alice@test.com/s1.jsonl", "alice@test.com", tool_errors=2)
    _seed_summary(repo, "bob@test.com/s2.jsonl", "bob@test.com", tool_errors=0)

    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/sessions/list?only_errors=true",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["username"] == "alice@test.com"


def test_sessions_kpis(seeded_app_both):
    from src.repositories import usage_repo

    repo = usage_repo()
    _seed_summary(
        repo,
        "alice@test.com/s1.jsonl",
        "alice@test.com",
        tool_calls=10,
        tool_errors=2,
        mcp_calls=3,
        subagent_dispatches=1,
    )
    _seed_summary(repo, "bob@test.com/s2.jsonl", "bob@test.com", tool_calls=4, tool_errors=0)

    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/sessions/kpis",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessions_total"] == 2
    assert body["distinct_users"] == 2
    assert body["error_sessions"] == 1
    # Total counts ALL calls: 10 native + 3 MCP + 1 subagent + 4 native.
    assert body["tool_calls_total"] == 18
    assert body["tool_errors_total"] == 2
    assert round(body["tool_error_rate"], 4) == round(2 / 18, 4)


def test_sessions_facets(seeded_app_both):
    from src.repositories import usage_repo

    repo = usage_repo()
    _seed_summary(repo, "alice@test.com/s1.jsonl", "alice@test.com", primary_model="claude-sonnet")
    _seed_summary(repo, "bob@test.com/s2.jsonl", "bob@test.com", primary_model="claude-haiku")

    client = seeded_app_both["client"]
    r = client.get(
        "/api/admin/sessions/facets",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    user_values = {u["value"] for u in body["users"]}
    assert {"alice@test.com", "bob@test.com"} <= user_values
    model_values = {m["value"] for m in body["models"]}
    assert {"claude-sonnet", "claude-haiku"} <= model_values
