"""Activity Center read API."""

from datetime import UTC, datetime

import pytest


@pytest.fixture(autouse=True)
def _reset_activity_dedup():
    from app.api.activity import _HEALTH_CACHE, _RECENT_AUDITS

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

    now = datetime.now(UTC)
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
        [str(uuid.uuid4()), "t1", datetime.now(UTC), 1, 1, "ok", None],
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
        [session_id, "leak-probe@example.com", "web", datetime.now(UTC)],
    )
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), session_id, "user", secret, datetime.now(UTC)],
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

    now = datetime.now(UTC)
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


class TestKpiTableParity:
    """The regression this whole change exists to prevent: KPI cards, facets
    and the timeline must tell one story for any filter state — INCLUDING
    the sync_history/llm_usage/agent_scope_snapshots trails folded into the
    timeline by E3 slice 2. Seeding only audit_log here would miss exactly
    the bug this class exists to catch (kpis()/facets() silently staying
    audit_log-only while the timeline widened)."""

    def _seed(self):
        import uuid
        from datetime import datetime

        from src.db import get_system_db
        from src.repositories.audit import AuditRepository

        conn = get_system_db()
        repo = AuditRepository(conn)
        repo.log(user_id="alice", action="table.read", result="success", client_kind="web")
        repo.log(user_id="alice", action="table.read", result="success", client_kind="cli")
        repo.log(user_id="alice", action="query.run", result="error.400", client_kind="web")
        repo.log(user_id="bob", action="query.run", result="denied", client_kind="cli")
        repo.log(user_id="sched-user", action="run_session_processor:usage", result="success")
        now = datetime.now(UTC)
        # One row per non-audit trail — sync_history/agent_scope_snapshots
        # carry no user_id (system/agent-runtime actors); the llm_usage row
        # reuses "alice" as the owning user so the pre-existing
        # active-users/user_id=alice assertions below stay meaningful rather
        # than needing an unrelated third identity.
        conn.execute(
            "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), "t_kpi", now, 10, 100, "ok", None],
        )
        conn.execute(
            "INSERT INTO llm_usage (id, agent_id, user_id, session_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_creation_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), "agent-kpi", "alice", "sess-kpi", "claude-x", 10, 20, 0, 0],
        )
        conn.execute(
            "INSERT INTO agent_scope_snapshots (id, session_id, agent_id, effective_scope) VALUES (?, ?, ?, ?)",
            [str(uuid.uuid4()), "sess-kpi", "agent-kpi", "{}"],
        )
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
            # E3 slice 2: the folded-in trails must agree under the SAME
            # trail= filter the timeline accepts — not just the unfiltered
            # (all-trails) case above.
            "trail=audit",
            "trail=sync",
            "trail=llm",
            "trail=agent_scope",
            "source=agent",
        ):
            k, t = self._counts(c, admin_user, qs)
            assert k == t, f"KPI {k} != timeline {t} for {qs}"

    def test_kpis_and_facets_include_folded_in_trails(self, seeded_app, admin_user):
        """Unfiltered kpis()/facets() must count the non-audit trails too —
        the exact bug this fix closes (cards silently undercounting the rows
        the table right below them shows)."""
        self._seed()
        c = seeded_app["client"]

        kpi_all = c.get("/api/admin/observability/kpis?since_minutes=60", headers=admin_user).json()
        kpi_audit_only = c.get("/api/admin/observability/kpis?since_minutes=60&trail=audit", headers=admin_user).json()
        assert kpi_all["events_total"] > kpi_audit_only["events_total"]
        assert kpi_all["events_total"] == kpi_audit_only["events_total"] + 3  # sync + llm + agent_scope

        f = c.get("/api/admin/observability/facets?since_minutes=60", headers=admin_user).json()
        actions = {a["value"] for a in f["actions"]}
        assert {"sync.table", "llm.call", "agent.spawn.scope"} <= actions
        sources = {s["value"] for s in f["sources"]}
        assert "scheduler" in sources
        assert "agent" in sources

        f_audit_only = c.get("/api/admin/observability/facets?since_minutes=60&trail=audit", headers=admin_user).json()
        actions_audit_only = {a["value"] for a in f_audit_only["actions"]}
        assert "sync.table" not in actions_audit_only
        assert "llm.call" not in actions_audit_only

    def test_kpis_and_facets_reject_unknown_trail(self, seeded_app, admin_user):
        c = seeded_app["client"]
        assert c.get("/api/admin/observability/kpis?trail=bogus", headers=admin_user).status_code == 400
        assert c.get("/api/admin/observability/facets?trail=bogus", headers=admin_user).status_code == 400

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
        # "llm.call" is present because _seed()'s llm_usage row is owned by
        # alice (E3 slice 2 — the unified facets() now see it too).
        assert {a["value"] for a in f["actions"]} == {"table.read", "query.run", "llm.call"}
        classes = {x["value"]: x["count"] for x in f["result_classes"]}
        assert classes["success"] == 2 and classes["error"] == 1


def test_sessions_kpis_match_adoption_kpis(seeded_app, admin_user):
    """Glossary pin (consistency spec Phase D): the sessions browser and the
    adoption dashboard read the same table with the same anchor — their
    headline numbers must be equal for an equivalent window."""
    from datetime import datetime

    from src.db import get_system_db
    from src.repositories.usage import UsageRepository

    conn = get_system_db()
    repo = UsageRepository(conn)
    now = datetime.now(UTC)
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
    from datetime import datetime

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
                "started_at": datetime.now(UTC),
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


# ---------------------------------------------------------------------------
# Cursor pagination — window pinning (PR #2400 review, Finding B)
#
# since_minutes computes `since = now() - since_minutes` fresh on every
# call. A paged read spans several calls; if wall-clock time advances
# between them, that floor drifts forward and a row that was inside the
# caller's original window can fall out between two pages — appearing on
# neither. next_cursor now carries the floor the FIRST call computed
# (since_ts) so a continuation can pin the window instead of recomputing it.
# ---------------------------------------------------------------------------


class _FrozenDatetime(datetime):
    """A `datetime` subclass whose `.now()` returns a settable fixed value,
    monkeypatched over `app.api.activity.datetime` to control "now" without
    a real clock dependency (same pattern as tests/test_kai_host.py)."""

    _fixed: "datetime"

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def test_pinned_floor_survives_a_wall_clock_advance_between_pages(seeded_app, admin_user, monkeypatch):
    import app.api.activity as activity_mod
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    t0 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    _FrozenDatetime._fixed = t0
    monkeypatch.setattr(activity_mod, "datetime", _FrozenDatetime)

    conn = get_system_db()
    repo = AuditRepository(conn)
    recent_id = repo.log(action="test.winpin.recent", result="success")
    edge_id = repo.log(action="test.winpin.edge", result="success")
    # since_minutes=60 -> floor at t0 is t0 - 60min. The "recent" row sits
    # well inside the window; the "edge" row sits only 30s inside it — close
    # enough to fall below a floor recomputed after more than 30s pass.
    from datetime import timedelta as _td

    conn.execute("UPDATE audit_log SET timestamp = ? WHERE id = ?", [t0 - _td(minutes=1), recent_id])
    conn.execute("UPDATE audit_log SET timestamp = ? WHERE id = ?", [t0 - _td(minutes=59, seconds=30), edge_id])
    conn.close()

    c = seeded_app["client"]
    page1 = c.get(
        "/api/admin/activity",
        params={"since_minutes": 60, "limit": 1, "action_prefix": "test.winpin."},
        headers=admin_user,
    ).json()
    assert [r["action"] for r in page1["rows"]] == ["test.winpin.recent"]
    cursor = page1["next_cursor"]
    assert cursor is not None
    assert "since_ts" in cursor

    # Wall clock advances 5 minutes between the two page requests — long
    # enough that a floor recomputed at page-2 time (t0 + 5min - 60min)
    # would sit past the edge row's timestamp and exclude it.
    _FrozenDatetime._fixed = t0 + _td(minutes=5)

    page2 = c.get(
        "/api/admin/activity",
        params={
            "since_minutes": 60,
            "limit": 1,
            "action_prefix": "test.winpin.",
            "cursor_ts": cursor["ts"],
            "cursor_id": cursor["id"],
            "since_ts": cursor["since_ts"],
        },
        headers=admin_user,
    ).json()
    assert [r["action"] for r in page2["rows"]] == ["test.winpin.edge"], (
        "the edge row fell out of the paged read when the clock advanced between "
        "calls — pagination must run over the floor pinned at the first call, not "
        "a freshly recomputed one"
    )


def test_omitting_since_ts_falls_back_to_recomputing_the_floor(seeded_app, admin_user):
    """Backward compatibility: a caller that reconstructs cursor_ts/cursor_id
    by hand without since_ts (e.g. the Activity Center web page before its
    own forwarding fix, or any other pre-existing integration) must keep
    working exactly as before this field existed."""
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/activity",
        params={
            "since_minutes": 60,
            "limit": 1,
            "cursor_ts": "2026-01-01T00:00:00+00:00",
            "cursor_id": "does-not-exist",
        },
        headers=admin_user,
    )
    assert r.status_code == 200


def test_cursor_without_since_ts_still_pages_correctly(seeded_app, admin_user):
    """The pre-change cursor shape (cursor_ts + cursor_id, no since_ts) must
    keep paging through REAL rows correctly, not merely return 200 — a
    caller that never forwards since_ts (an old integration, or a hand-built
    cursor) must still reach page two, not silently restart at page one."""
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="test.nosincets.first", result="success")
    repo.log(action="test.nosincets.second", result="success")
    conn.close()

    c = seeded_app["client"]
    page1 = c.get(
        "/api/admin/activity",
        params={"action_prefix": "test.nosincets.", "limit": 1},
        headers=admin_user,
    ).json()
    assert page1["rows"][0]["action"] == "test.nosincets.second"
    cursor = page1["next_cursor"]
    assert cursor is not None

    page2 = c.get(
        "/api/admin/activity",
        params={
            "action_prefix": "test.nosincets.",
            "limit": 1,
            "cursor_ts": cursor["ts"],
            "cursor_id": cursor["id"],
        },
        headers=admin_user,
    ).json()
    assert page2["rows"][0]["action"] == "test.nosincets.first"


# ---------------------------------------------------------------------------
# Cursor / since_ts validation hardening (PR #2400 second review round)
#
# since_ts riding back in next_cursor let `since = since_ts if since_ts is
# not None else ...` accept ANY absolute floor unconditionally — a caller
# could pass a floor years back and scan the whole archive regardless of the
# since_minutes ceiling (`ge=1, le=43200`). since_ts is now continuation-only
# (requires a complete cursor) and bounded to at most since_minutes before
# cursor_ts, so a hand-built cursor cannot widen a single read's scan beyond
# what since_minutes already allows. A half cursor (exactly one of
# cursor_ts/cursor_id) is rejected the same way the MCP tool and CLI already
# reject it, instead of silently restarting the timeline at page one.
# ---------------------------------------------------------------------------


def test_standalone_since_ts_without_cursor_is_rejected(seeded_app, admin_user):
    """A fresh (uncursored) read that sets since_ts directly must be
    refused, not silently ignored — a silently ignored parameter is its own
    trap (the caller believes the floor is pinned; it silently isn't)."""
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/activity",
        params={"since_ts": "2020-01-01T00:00:00+00:00"},
        headers=admin_user,
    )
    assert r.status_code == 400
    assert "since_ts" in r.json()["detail"]


def test_since_ts_predating_the_window_is_rejected(seeded_app, admin_user):
    """A hand-built cursor cannot use since_ts to widen a single read's scan
    beyond what since_minutes bounds — since_ts more than since_minutes
    before cursor_ts must be refused, not silently honoured."""
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/activity",
        params={
            "since_minutes": 60,
            "cursor_ts": "2026-09-09T12:00:00+00:00",
            "cursor_id": "abc-123",
            "since_ts": "2020-01-01T00:00:00+00:00",
        },
        headers=admin_user,
    )
    assert r.status_code == 400
    assert "since_ts" in r.json()["detail"]


def test_half_cursor_ts_only_is_rejected_at_endpoint(seeded_app, admin_user):
    """The MCP tool and CLI already reject a half cursor — the endpoint
    itself must too, rather than silently returning page one for a REST
    caller supplying only one half."""
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/activity",
        params={"cursor_ts": "2026-09-09T12:00:00+00:00"},
        headers=admin_user,
    )
    assert r.status_code == 400
    assert "cursor_id" in r.json()["detail"]


def test_half_cursor_id_only_is_rejected_at_endpoint(seeded_app, admin_user):
    c = seeded_app["client"]
    r = c.get(
        "/api/admin/activity",
        params={"cursor_id": "abc-123"},
        headers=admin_user,
    )
    assert r.status_code == 400
    assert "cursor_ts" in r.json()["detail"]
