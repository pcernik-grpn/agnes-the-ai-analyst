"""B8: audit-trail viewer seam.

Covers three pieces:

  1. ``src/audit_retention.py::prune_audit_log`` + its config knob
     (``app/instance_config.get_audit_retention_days``, ``audit.retention_days``).
  2. ``POST /api/admin/run-audit-prune`` — the scheduler-driven trigger.
  3. The Activity Center's fan-out links to the two viewers it doesn't cover
     (``/admin/sessions``, ``/admin/telemetry``) + the daily scheduler job
     registration.

Dual-backend parity for the new ``AuditRepository``/``AuditPgRepository``
``prune_older_than`` method lives in
``tests/db_pg/test_audit_contract.py`` (contract-test style, both engines).
This file is DuckDB-only, matching the rest of the single-backend test suite.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from src.db import _ensure_schema as init_database
from src.repositories.audit import AuditRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def _backdate(conn, entry_id: str, days_ago: int) -> None:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    conn.execute("UPDATE audit_log SET timestamp = ? WHERE id = ?", [ts, entry_id])


# ---------------------------------------------------------------------------
# app/instance_config.get_audit_retention_days
# ---------------------------------------------------------------------------


class TestAuditRetentionDaysConfig:
    def test_default_is_365(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.get_audit_retention_days() == 365

    def test_reads_configured_value(self, monkeypatch):
        import app.instance_config as ic

        def _get_value(*keys, default=None):
            return 30 if keys == ("audit", "retention_days") else default

        monkeypatch.setattr(ic, "get_value", _get_value)
        assert ic.get_audit_retention_days() == 30

    def test_zero_is_valid_keep_forever(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: 0)
        assert ic.get_audit_retention_days() == 0

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: "garbage")
        assert ic.get_audit_retention_days() == 365

    def test_negative_value_clamped_to_zero(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: -10)
        assert ic.get_audit_retention_days() == 0


# ---------------------------------------------------------------------------
# src/audit_retention.py::prune_audit_log
# ---------------------------------------------------------------------------


class TestPruneAuditLog:
    def test_prunes_rows_older_than_retention(self, conn):
        from src.audit_retention import prune_audit_log

        repo = AuditRepository(conn)
        old_id = repo.log(action="old.one")
        new_id = repo.log(action="new.one")
        _backdate(conn, old_id, 400)

        result = prune_audit_log(retention_days=365, repo=repo)

        assert result == {"pruned": 1, "skipped": False}
        rows, _ = repo.query(limit=10)
        ids = {r["id"] for r in rows}
        assert old_id not in ids
        assert new_id in ids

    def test_retention_days_zero_is_noop_and_never_touches_repo(self):
        from src.audit_retention import prune_audit_log

        class _ExplodingRepo:
            def prune_older_than(self, days):
                raise AssertionError("repo must not be called when retention_days<=0")

        result = prune_audit_log(retention_days=0, repo=_ExplodingRepo())
        assert result == {"pruned": 0, "skipped": True}

    def test_retention_days_negative_is_noop(self):
        from src.audit_retention import prune_audit_log

        class _ExplodingRepo:
            def prune_older_than(self, days):
                raise AssertionError("repo must not be called when retention_days<=0")

        result = prune_audit_log(retention_days=-5, repo=_ExplodingRepo())
        assert result == {"pruned": 0, "skipped": True}

    def test_logs_the_pruned_count(self, conn, caplog):
        from src.audit_retention import prune_audit_log

        repo = AuditRepository(conn)
        old_id = repo.log(action="old.one")
        _backdate(conn, old_id, 400)

        with caplog.at_level(logging.INFO, logger="src.audit_retention"):
            prune_audit_log(retention_days=365, repo=repo)

        assert any("1" in rec.getMessage() for rec in caplog.records)

    def test_uses_audit_repo_factory_by_default(self, monkeypatch):
        from src.audit_retention import prune_audit_log

        calls = {}

        class _FakeRepo:
            def prune_older_than(self, days):
                calls["days"] = days
                return 3

        monkeypatch.setattr("src.repositories.audit_repo", lambda: _FakeRepo())
        result = prune_audit_log(retention_days=10)

        assert result == {"pruned": 3, "skipped": False}
        assert calls["days"] == 10


# ---------------------------------------------------------------------------
# POST /api/admin/run-audit-prune
# ---------------------------------------------------------------------------


class TestRunAuditPruneEndpoint:
    def test_admin_can_trigger_prune(self, seeded_app, admin_user):
        from src.db import get_system_db

        sys_conn = get_system_db()
        repo = AuditRepository(sys_conn)
        old_id = repo.log(action="old.one")
        new_id = repo.log(action="new.one")
        _backdate(sys_conn, old_id, 400)
        sys_conn.close()

        c = seeded_app["client"]
        resp = c.post("/api/admin/run-audit-prune", headers=admin_user)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["skipped"] is False
        assert body["details"]["pruned"] >= 1

        check_conn = get_system_db()
        remaining = check_conn.execute("SELECT id FROM audit_log WHERE id IN (?, ?)", [old_id, new_id]).fetchall()
        remaining_ids = {r[0] for r in remaining}
        check_conn.close()
        assert old_id not in remaining_ids
        assert new_id in remaining_ids

    def test_writes_its_own_audit_row(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-audit-prune", headers=admin_user)
        assert resp.status_code == 200

        from src.db import get_system_db

        check_conn = get_system_db()
        row = check_conn.execute(
            "SELECT action, resource FROM audit_log WHERE action = 'run_audit_prune' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        check_conn.close()
        assert row is not None
        assert row[1] == "job:audit-prune"

    def test_zero_retention_reports_skipped(self, seeded_app, admin_user, monkeypatch):
        import app.instance_config as ic

        def _get_value(*keys, default=None):
            return 0 if keys == ("audit", "retention_days") else default

        monkeypatch.setattr(ic, "get_value", _get_value)

        c = seeded_app["client"]
        resp = c.post("/api/admin/run-audit-prune", headers=admin_user)
        assert resp.status_code == 200
        assert resp.json()["details"] == {"pruned": 0, "skipped": True}

    def test_non_admin_blocked(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-audit-prune", headers=analyst_user)
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-audit-prune")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Activity Center fan-out links — route/template test, admin gate unchanged
# ---------------------------------------------------------------------------


class TestActivityCenterFanOutLinks:
    def test_page_links_to_sessions_and_telemetry(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/admin/activity", headers=admin_user)
        assert resp.status_code == 200
        assert 'href="/admin/sessions"' in resp.text
        assert 'href="/admin/telemetry"' in resp.text

    def test_admin_gate_non_admin_blocked(self, seeded_app, analyst_user):
        """Unchanged: a non-admin still gets 403 on the page."""
        c = seeded_app["client"]
        resp = c.get("/admin/activity", headers=analyst_user)
        assert resp.status_code == 403

    def test_admin_gate_unauth_redirects_to_login(self, seeded_app):
        """Unchanged: an unauthenticated GET redirects to /login (existing
        HTML-route contract), not a bare 401 and not a 200 render."""
        c = seeded_app["client"]
        c.cookies.clear()
        resp = c.get("/admin/activity", headers={}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# Scheduler registration — services/scheduler/__main__.py::build_jobs
# ---------------------------------------------------------------------------


class TestAuditPruneSchedulerJob:
    def test_audit_prune_job_present(self):
        from services.scheduler.__main__ import build_jobs

        jobs = {j[0]: j for j in build_jobs()}
        assert "audit-prune" in jobs
        name, schedule, endpoint, method, timeout_sec = jobs["audit-prune"][:5]
        assert schedule == "daily 05:30"
        assert endpoint == "/api/admin/run-audit-prune"
        assert method == "POST"

    def test_audit_prune_job_not_a_queued_enqueue_body(self):
        """Cheap DELETE — runs synchronously via HTTP like store-blocked-purge,
        not routed through the /api/jobs queue (no 6th json_body element)."""
        from services.scheduler.__main__ import build_jobs

        job = next(j for j in build_jobs() if j[0] == "audit-prune")
        assert len(job) == 5
