"""Activity Center read API."""

import pytest
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def _reset_activity_dedup():
    from app.api.activity import _RECENT_AUDITS, _HEALTH_CACHE

    _RECENT_AUDITS.clear()
    _HEALTH_CACHE["data"] = None
    _HEALTH_CACHE["expires_at"] = None
    yield
    _RECENT_AUDITS.clear()
    _HEALTH_CACHE["data"] = None
    _HEALTH_CACHE["expires_at"] = None


def test_activity_timeline_requires_admin(seeded_app, analyst_user):
    """Non-admin user gets 403."""
    resp = seeded_app["client"].get("/api/admin/activity", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_activity_timeline_returns_recent_rows(seeded_app, admin_user):
    """Seeded audit_log rows appear in the response."""
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    AuditRepository(conn).log(user_id="u1", action="test.activity", result="success")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert "next_cursor" in data
    assert any(r["action"] == "test.activity" for r in data["rows"])


def test_activity_timeline_supports_filters(seeded_app, admin_user):
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="sync.trigger")
    repo.log(action="auth.login")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    assert resp.status_code == 200
    actions = {r["action"] for r in resp.json()["rows"]}
    assert "sync.trigger" in actions
    assert "auth.login" not in actions


def test_activity_timeline_supports_resource_prefix(seeded_app, admin_user):
    # Activity Center "Resource" dropdown sends `resource_prefix=table:`
    # (etc). The endpoint must echo the filter so the UI's narrative line
    # can show it, and the repo must apply it as a LIKE so rows with any
    # id under that namespace are returned.
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="table.read", resource="table:web_sessions")
    repo.log(action="table.read", resource="table:orders")
    repo.log(action="user.update", resource="user:u1")
    conn.close()

    resp = seeded_app["client"].get(
        "/api/admin/activity?resource_prefix=table:",
        headers=admin_user,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filter"]["resource_prefix"] == "table:"
    resources = {r["resource"] for r in body["rows"]}
    assert resources == {"table:web_sessions", "table:orders"}


def test_activity_timeline_folds_in_sync_llm_and_agent_scope_trails(seeded_app, admin_user):
    """E3 slice 2: the timeline is a unified projection over audit_log +
    sync_history + llm_usage + agent_scope_snapshots — not audit_log alone."""
    import uuid
    from src.db import get_system_db

    now = datetime.now(timezone.utc)
    conn = get_system_db()
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "t_web_sessions", now, 100, 500, "ok", None],
    )
    conn.execute(
        "INSERT INTO llm_usage (id, agent_id, user_id, session_id, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "agent-1", "u1", "sess-1", "claude-x", 10, 20, 0, 0],
    )
    conn.execute(
        "INSERT INTO agent_scope_snapshots (id, session_id, agent_id, effective_scope) VALUES (?, ?, ?, ?)",
        [str(uuid.uuid4()), "sess-1", "agent-1", '{"tables": ["orders"]}'],
    )
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    rows = resp.json()["rows"]

    sync_row = next(r for r in rows if r["action"] == "sync.table")
    assert sync_row["resource"] == "table:t_web_sessions"
    assert sync_row["result"] == "ok"
    assert sync_row["source"] == "scheduler"

    llm_row = next(r for r in rows if r["action"] == "llm.call")
    assert llm_row["resource"] == "agent:agent-1"
    assert llm_row["user_id"] == "u1"
    assert llm_row["source"] == "agent"

    scope_row = next(r for r in rows if r["action"] == "agent.spawn.scope")
    assert scope_row["resource"] == "agent:agent-1"
    assert scope_row["source"] == "agent"


def test_activity_timeline_trail_filter_narrows_to_audit_only(seeded_app, admin_user):
    import uuid
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    AuditRepository(conn).log(action="a.plain.audit.row")
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "t1", datetime.now(timezone.utc), 1, 1, "ok", None],
    )
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity?trail=audit", headers=admin_user)
    assert resp.status_code == 200
    body = resp.json()
    assert body["filter"]["trail"] == "audit"
    actions = {r["action"] for r in body["rows"]}
    assert "a.plain.audit.row" in actions
    assert "sync.table" not in actions


def test_activity_timeline_unknown_trail_is_400(seeded_app, admin_user):
    resp = seeded_app["client"].get("/api/admin/activity?trail=not-a-trail", headers=admin_user)
    assert resp.status_code == 400


def test_activity_timeline_never_surfaces_chat_messages(seeded_app, admin_user):
    """Privacy regression: chat transcript content must never leak into the
    unified Activity Center timeline (docs/observability.md)."""
    import uuid
    from src.db import get_system_db

    secret = "customer churn is 4.2 percent, keep this confidential"
    conn = get_system_db()
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_sessions (id, user_email, surface, started_at) VALUES (?, ?, ?, ?)",
        [session_id, "leak-probe@example.com", "web", datetime.now(timezone.utc)],
    )
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), session_id, "user", secret, datetime.now(timezone.utc)],
    )
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity?limit=200", headers=admin_user)
    assert resp.status_code == 200
    assert secret not in resp.text
    assert session_id not in resp.text


def test_activity_health_returns_pulse(seeded_app, admin_user):
    resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("green", "yellow", "red")
    assert "fields" in data
    assert "sentence" in data
    field_keys = {f["key"] for f in data["fields"]}
    assert "scheduler" in field_keys
    assert "sync_24h" in field_keys
    assert "active_users_today" in field_keys


def test_activity_sync_returns_recent(seeded_app, admin_user):
    import uuid
    from src.db import get_system_db

    now = datetime.now(timezone.utc)
    conn = get_system_db()
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "t_test", now, 42, 1500, "ok", None],
    )
    conn.close()
    resp = seeded_app["client"].get("/api/admin/activity/sync", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert any(r["table_id"] == "t_test" for r in data["rows"])


def test_admin_activity_page_renders(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    # Page is the unified observability shell. All data loads client-side
    # so we assert only the structural anchors the JS attaches to.
    assert "obs-page" in resp.text
    assert "obs-table" in resp.text
    assert "Audit log" in resp.text


def test_activity_center_redirects_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/activity-center", headers=admin_user, follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/admin/activity"


def test_dashboard_links_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/dashboard", headers=admin_user)
    assert resp.status_code == 200
    assert "/admin/activity" in resp.text
    assert "/activity-center" not in resp.text  # old URL removed


def test_admin_header_includes_activity_link(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    assert 'href="/admin/activity"' in resp.text


def test_activity_health_does_not_audit_polling(seeded_app, admin_user):
    """Polling /health every 30s shouldn't blow up audit_log."""
    from src.db import get_system_db

    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='activity.read'").fetchone()[0]
    conn.close()
    for _ in range(5):
        c.get("/api/admin/activity/health", headers=admin_user)
    conn = get_system_db()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='activity.read'").fetchone()[0]
    conn.close()
    assert after - before <= 1  # at most one row from the burst


def test_activity_timeline_audits_first_call_only(seeded_app, admin_user):
    """Two identical filter calls within 60s produce one audit row."""
    from src.db import get_system_db

    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='activity.read'").fetchone()[0]
    conn.close()
    assert n == 1


def test_activity_timeline_audits_different_filters(seeded_app, admin_user):
    """Different filter combinations each get their own audit row."""
    from src.db import get_system_db

    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=auth.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='activity.read'").fetchone()[0]
    conn.close()
    assert n == 2


def test_activity_health_emits_posthog_event_when_enabled(seeded_app, admin_user):
    from unittest.mock import patch

    with patch("app.api.activity.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = True
        seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        mock_client.capture.assert_called()
        kw = mock_client.capture.call_args.kwargs
        assert kw.get("event") == "activity_health_viewed"


def test_activity_endpoints_silent_when_posthog_disabled(seeded_app, admin_user):
    from unittest.mock import patch

    with patch("app.api.activity.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = False
        resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        # capture may be called but the inner SDK is no-op; that's the contract.
        # Assert: no exception, healthy response.
        assert resp.status_code == 200


class TestKpiTableParity:
    """The regression this whole change exists to prevent: KPI cards, facets
    and the timeline must tell one story for any filter state."""

    def _seed(self):
        from src.db import get_system_db
        from src.repositories.audit import AuditRepository

        conn = get_system_db()
        repo = AuditRepository(conn)
        repo.log(user_id="alice", action="table.read", result="success", client_kind="web")
        repo.log(user_id="alice", action="table.read", result="success", client_kind="cli")
        repo.log(user_id="alice", action="query.run", result="error.400", client_kind="web")
        repo.log(user_id="bob", action="query.run", result="denied", client_kind="cli")
        repo.log(user_id="sched-user", action="run_session_processor:usage", result="success")
        conn.close()

    def _counts(self, client, admin_user, qs):
        kpi = client.get(f"/api/admin/observability/kpis?since_minutes=60&{qs}", headers=admin_user).json()
        tl = client.get(f"/api/admin/activity?since_minutes=60&limit=200&{qs}", headers=admin_user).json()
        return kpi["events_total"], len(tl["rows"])

    def test_kpis_match_timeline_under_filters(self, seeded_app, admin_user):
        self._seed()
        c = seeded_app["client"]
        for qs in (
            "user_id=alice",
            "result_class=success",
            "result_class=denied",
            "source=cli",
            "user_id=alice&source=web",
        ):
            k, t = self._counts(c, admin_user, qs)
            assert k == t, f"KPI {k} != timeline {t} for {qs}"

    def test_self_reads_hidden_by_default(self, seeded_app, admin_user):
        self._seed()
        c = seeded_app["client"]
        # generate one self-read row
        c.get("/api/admin/activity?since_minutes=60", headers=admin_user)
        from app.api.activity import _RECENT_AUDITS

        _RECENT_AUDITS.clear()
        tl = c.get("/api/admin/activity?since_minutes=60&limit=200", headers=admin_user).json()
        assert "activity.read" not in {r["action"] for r in tl["rows"]}
        tl2 = c.get("/api/admin/activity?since_minutes=60&limit=200&include_self_reads=1", headers=admin_user).json()
        assert "activity.read" in {r["action"] for r in tl2["rows"]}

    def test_active_users_counts_people_only(self, seeded_app, admin_user):
        self._seed()
        c = seeded_app["client"]
        kpi = c.get("/api/admin/observability/kpis?since_minutes=60", headers=admin_user).json()
        assert kpi["active_users"] == 2  # alice + bob; scheduler row excluded
        assert "duration_coverage" in kpi

    def test_facets_carry_result_classes_and_honor_filters(self, seeded_app, admin_user):
        self._seed()
        c = seeded_app["client"]
        f = c.get("/api/admin/observability/facets?since_minutes=60&user_id=alice", headers=admin_user).json()
        assert {a["value"] for a in f["actions"]} == {"table.read", "query.run"}
        classes = {x["value"]: x["count"] for x in f["result_classes"]}
        assert classes["success"] == 2 and classes["error"] == 1


def test_sessions_kpis_match_adoption_kpis(seeded_app, admin_user):
    """Glossary pin (consistency spec Phase D): the sessions browser and the
    adoption dashboard read the same table with the same anchor — their
    headline numbers must be equal for an equivalent window."""
    from datetime import datetime, timezone

    from src.db import get_system_db
    from src.repositories.usage import UsageRepository

    conn = get_system_db()
    repo = UsageRepository(conn)
    now = datetime.now(timezone.utc)
    for i, (user, sid) in enumerate([("ann", "s1"), ("ann", "s2"), ("ben", "s3")]):
        repo.upsert_summary(
            {
                "session_file": f"{user}/{sid}.jsonl",
                "session_id": sid,
                "username": f"{user}@example.com",
                "user_id": f"uid-{user}",
                "started_at": now,
                "ended_at": now,
                "active_seconds": 60 + i,
                "wall_seconds": 60 + i,
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_calls": 2,
                "tool_errors": 0,
                "skill_invocations": 0,
                "subagent_dispatches": 0,
                "mcp_calls": 0,
                "slash_commands": 0,
                "distinct_tools": 1,
                "distinct_skills": 0,
                "primary_model": "test-model",
                "total_tokens": 10,
            },
            processor_version=1,
        )
    conn.close()

    c = seeded_app["client"]
    sk = c.get("/api/admin/sessions/kpis?since_minutes=10080", headers=admin_user).json()
    ak = c.get("/api/admin/adoption/kpis?window=7d", headers=admin_user).json()
    assert sk["sessions_total"] == ak["sessions"] == 3
    assert sk["distinct_users"] == ak["active_users"] == 2


def test_health_reconciles_uploads_vs_ingested(seeded_app, admin_user):
    """Health pulse: uploaded session files without a summary row show as a
    yellow ingest gap; complete ingest is green. Join on file basename."""
    from datetime import datetime, timezone

    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    from src.repositories.usage import UsageRepository

    conn = get_system_db()
    audit = AuditRepository(conn)
    usage = UsageRepository(conn)
    for fn in ("aaa.jsonl", "bbb.jsonl", "ccc.jsonl"):
        audit.log(
            user_id="u-1",
            action="session.upload",
            params={"bytes": 1, "filename": fn},
            result="success",
        )
    for sid in ("aaa", "bbb"):
        usage.upsert_summary(
            {
                "session_file": f"u-1/{sid}.jsonl",
                "session_id": f"content-id-{sid}",  # differs from file stem on purpose
                "username": "ann@example.com",
                "user_id": "u-1",
                "started_at": datetime.now(timezone.utc),
            },
            processor_version=1,
        )
    conn.close()

    c = seeded_app["client"]
    h = c.get("/api/admin/activity/health", headers=admin_user).json()
    field = next(f for f in h["fields"] if f["key"] == "session_ingest")
    assert field["value"] == "3 up / 2 ingested"
    assert field["color"] == "yellow"
    assert field["raw"] == 1
