"""POST /api/admin/telemetry/reprocess and /prune."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _seed_usage_state(conn, n_events=3):
    """Insert one session_processor_state row + N usage_events + 1 summary row."""
    conn.execute(
        "INSERT INTO session_processor_state (processor_name, session_file, username, processed_at, items_extracted, file_hash) "
        "VALUES ('usage', 'alice/x.jsonl', 'alice', current_timestamp, ?, 'h1')",
        [n_events],
    )
    conn.execute(
        "INSERT INTO session_processor_state (processor_name, session_file, username, processed_at, items_extracted, file_hash) "
        "VALUES ('verification', 'alice/x.jsonl', 'alice', current_timestamp, 0, 'h1')",
    )
    for i in range(n_events):
        conn.execute(
            """INSERT INTO usage_events
            (id, session_id, session_file, username, event_type, tool_name,
             is_error, source, occurred_at, processor_version)
            VALUES (?, 'sess', 'alice/x.jsonl', 'alice', 'tool_use', 'Bash',
                    false, 'builtin', ?, 1)""",
            [f"e-{i}", datetime(2026, 5, 12, tzinfo=timezone.utc)],
        )
    conn.execute(
        """INSERT INTO usage_session_summary
        (session_file, session_id, username, started_at, ended_at, tool_calls, processor_version)
        VALUES ('alice/x.jsonl', 'sess', 'alice', current_timestamp, current_timestamp, ?, 1)""",
        [n_events],
    )
    conn.execute(
        """INSERT INTO usage_tool_daily (day, tool_name, source, invocations, error_count, distinct_users, distinct_sessions)
        VALUES (current_date, 'Bash', 'builtin', ?, 0, 1, 1)""",
        [n_events],
    )


def test_reprocess_clears_usage_state_only(seeded_app, admin_user):
    from src.db import get_system_db

    conn = get_system_db()
    _seed_usage_state(conn, n_events=3)
    conn.close()

    resp = seeded_app["client"].post("/api/admin/telemetry/reprocess", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    deleted = data["deleted"]
    assert deleted["state_rows"] == 1
    assert deleted["events"] == 3
    assert deleted["summaries"] == 1
    assert deleted["tool_daily"] == 1

    # Verification state untouched
    conn = get_system_db()
    v = conn.execute("SELECT COUNT(*) FROM session_processor_state WHERE processor_name='verification'").fetchone()[0]
    u = conn.execute("SELECT COUNT(*) FROM session_processor_state WHERE processor_name='usage'").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    conn.close()
    assert v == 1
    assert u == 0
    assert n_events == 0


def test_reprocess_writes_audit_log(seeded_app, admin_user):
    from src.db import get_system_db

    conn = get_system_db()
    _seed_usage_state(conn, n_events=2)
    conn.close()
    seeded_app["client"].post("/api/admin/telemetry/reprocess", headers=admin_user)
    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='usage.reprocess'").fetchone()[0]
    conn.close()
    assert n >= 1


def test_reprocess_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].post("/api/admin/telemetry/reprocess", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_prune_skipped_when_retention_unset(seeded_app, admin_user, monkeypatch):
    monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)
    resp = seeded_app["client"].post("/api/admin/telemetry/prune", headers=admin_user)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_prune_skipped_when_retention_zero(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "0")
    resp = seeded_app["client"].post("/api/admin/telemetry/prune", headers=admin_user)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_prune_respects_retention(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "7")
    from src.db import get_system_db

    conn = get_system_db()
    # Old event (10 days ago)
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_type, tool_name,
         is_error, source, occurred_at, processor_version)
        VALUES (?, 's', 'a/x.jsonl', 'a', 'tool_use', 'Bash', false, 'builtin', ?, 1)""",
        ["old", datetime.now(timezone.utc) - timedelta(days=10)],
    )
    # Recent event (1 day ago)
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_type, tool_name,
         is_error, source, occurred_at, processor_version)
        VALUES (?, 's', 'a/x.jsonl', 'a', 'tool_use', 'Bash', false, 'builtin', ?, 1)""",
        ["recent", datetime.now(timezone.utc) - timedelta(days=1)],
    )
    conn.close()
    resp = seeded_app["client"].post("/api/admin/telemetry/prune", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["deleted"] == 1
    assert data["remaining"] == 1
    conn = get_system_db()
    remaining_ids = [r[0] for r in conn.execute("SELECT id FROM usage_events").fetchall()]
    conn.close()
    assert "recent" in remaining_ids
    assert "old" not in remaining_ids


def test_prune_writes_audit_log(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "7")
    from src.db import get_system_db

    conn = get_system_db()
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_type, tool_name,
         is_error, source, occurred_at, processor_version)
        VALUES ('prune-test', 's', 'a/x.jsonl', 'a', 'tool_use', 'Bash', false, 'builtin', ?, 1)""",
        [datetime.now(timezone.utc) - timedelta(days=30)],
    )
    conn.close()
    seeded_app["client"].post("/api/admin/telemetry/prune", headers=admin_user)
    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='usage.prune'").fetchone()[0]
    conn.close()
    assert n >= 1


def test_prune_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].post("/api/admin/telemetry/prune", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_prune_respects_retention_config_key_when_env_var_unset(seeded_app, admin_user, monkeypatch):
    """Track E3 Slice 1 reconciliation: retention.usage_events_days in
    instance.yaml is honored when USAGE_EVENTS_RETENTION_DAYS is unset —
    the config-file path, not just the legacy env var."""
    import app.instance_config as ic

    monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)

    def _get_value(*keys, default=None):
        return 7 if keys == ("retention", "usage_events_days") else default

    monkeypatch.setattr(ic, "get_value", _get_value)

    from src.db import get_system_db

    conn = get_system_db()
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_type, tool_name,
         is_error, source, occurred_at, processor_version)
        VALUES ('cfg-old', 's', 'a/x.jsonl', 'a', 'tool_use', 'Bash', false, 'builtin', ?, 1)""",
        [datetime.now(timezone.utc) - timedelta(days=10)],
    )
    conn.close()

    resp = seeded_app["client"].post("/api/admin/telemetry/prune", headers=admin_user)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["retention_days"] == 7
    assert body["deleted"] == 1
