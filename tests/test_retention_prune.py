"""Track E3 Slice 1: per-trail retention.

Generalizes the B8 ``audit_log`` retention pattern (``tests/test_audit_retention.py``)
to the other unbounded audit/activity trails: ``sync_history``, ``llm_usage``,
and ``agent_scope_snapshots``. Covers:

  1. ``src/audit_retention.py::prune_trail`` / ``run_retention_sweep`` — the
     generalized dispatcher.
  2. The four new config knobs (``app/instance_config.py``): ``retention.
     sync_history_days``, ``retention.llm_usage_days``, ``retention.
     agent_scope_snapshots_days`` (all default 0 = off), plus ``retention.
     usage_events_days`` reconciled with the pre-existing
     ``USAGE_EVENTS_RETENTION_DAYS`` env var.
  3. ``POST /api/admin/run-retention-prune`` — the scheduler-driven trigger.
  4. Scheduler job registration (``services/scheduler/__main__.py``).
  5. The safe default: with nothing configured, the sweep prunes nothing.

Dual-backend parity for the new repo methods
(``SyncStateRepository.prune_history_older_than``,
``LlmUsageRepository.prune_older_than``,
``AgentsRepository.prune_scope_snapshots_older_than``, and their ``_pg``
siblings) lives in the respective ``tests/db_pg/test_*_contract.py`` files.
This file is DuckDB-only / dispatcher-level, matching
``tests/test_audit_retention.py``'s split.
"""

from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# src/audit_retention.py::prune_trail / run_retention_sweep
# ---------------------------------------------------------------------------


class TestPruneTrail:
    def test_unknown_trail_raises(self):
        from src.audit_retention import prune_trail

        with pytest.raises(ValueError):
            prune_trail("chat_messages", retention_days=30)

    def test_retention_days_zero_is_noop_and_never_touches_repo(self):
        from src.audit_retention import prune_trail

        class _ExplodingRepo:
            def prune_history_older_than(self, days):
                raise AssertionError("repo must not be called when retention_days<=0")

        result = prune_trail("sync_history", retention_days=0, repo=_ExplodingRepo())
        assert result == {"pruned": 0, "skipped": True}

    def test_retention_days_negative_is_noop(self):
        from src.audit_retention import prune_trail

        class _ExplodingRepo:
            def prune_older_than(self, days):
                raise AssertionError("repo must not be called when retention_days<=0")

        result = prune_trail("llm_usage", retention_days=-5, repo=_ExplodingRepo())
        assert result == {"pruned": 0, "skipped": True}

    def test_dispatches_sync_history_to_prune_history_older_than(self):
        from src.audit_retention import prune_trail

        calls = {}

        class _FakeRepo:
            def prune_history_older_than(self, days):
                calls["days"] = days
                return 3

        result = prune_trail("sync_history", retention_days=10, repo=_FakeRepo())
        assert result == {"pruned": 3, "skipped": False}
        assert calls["days"] == 10

    def test_dispatches_llm_usage_to_prune_older_than(self):
        from src.audit_retention import prune_trail

        class _FakeRepo:
            def prune_older_than(self, days):
                return 7

        result = prune_trail("llm_usage", retention_days=10, repo=_FakeRepo())
        assert result == {"pruned": 7, "skipped": False}

    def test_dispatches_agent_scope_snapshots_to_prune_scope_snapshots_older_than(self):
        from src.audit_retention import prune_trail

        class _FakeRepo:
            def prune_scope_snapshots_older_than(self, days):
                return 2

        result = prune_trail("agent_scope_snapshots", retention_days=10, repo=_FakeRepo())
        assert result == {"pruned": 2, "skipped": False}

    def test_uses_repo_factory_by_default(self, monkeypatch):
        from src.audit_retention import prune_trail

        class _FakeRepo:
            def prune_history_older_than(self, days):
                return 5

        monkeypatch.setattr("src.repositories.sync_state_repo", lambda: _FakeRepo())
        result = prune_trail("sync_history", retention_days=10)
        assert result == {"pruned": 5, "skipped": False}

    def test_logs_the_pruned_count(self, caplog):
        from src.audit_retention import prune_trail

        class _FakeRepo:
            def prune_older_than(self, days):
                return 4

        with caplog.at_level(logging.INFO, logger="src.audit_retention"):
            prune_trail("llm_usage", retention_days=30, repo=_FakeRepo())

        assert any("4" in rec.getMessage() and "llm_usage" in rec.getMessage() for rec in caplog.records)


class TestRunRetentionSweep:
    def test_default_config_prunes_nothing(self):
        """The safe-default proof: an empty windows dict (every trail
        implicitly 0 = keep forever) must not touch any repo at all."""
        from src.audit_retention import run_retention_sweep

        result = run_retention_sweep({})
        assert result == {
            "sync_history": {"pruned": 0, "skipped": True},
            "llm_usage": {"pruned": 0, "skipped": True},
            "agent_scope_snapshots": {"pruned": 0, "skipped": True},
        }

    def test_zero_windows_prunes_nothing(self):
        from src.audit_retention import run_retention_sweep

        result = run_retention_sweep({"sync_history": 0, "llm_usage": 0, "agent_scope_snapshots": 0})
        assert all(v["skipped"] for v in result.values())
        assert all(v["pruned"] == 0 for v in result.values())

    def test_only_configured_trail_is_pruned(self, monkeypatch):
        from src.audit_retention import run_retention_sweep

        class _FakeSyncRepo:
            def prune_history_older_than(self, days):
                return 9

        class _ExplodingLlmRepo:
            def prune_older_than(self, days):
                raise AssertionError("llm_usage must stay skipped (window=0)")

        class _ExplodingSnapshotRepo:
            def prune_scope_snapshots_older_than(self, days):
                raise AssertionError("agent_scope_snapshots must stay skipped (window=0)")

        monkeypatch.setattr("src.repositories.sync_state_repo", lambda: _FakeSyncRepo())
        monkeypatch.setattr("src.repositories.llm_usage_repo", lambda: _ExplodingLlmRepo())
        monkeypatch.setattr("src.repositories.agents_repo", lambda: _ExplodingSnapshotRepo())

        result = run_retention_sweep({"sync_history": 90})

        assert result["sync_history"] == {"pruned": 9, "skipped": False}
        assert result["llm_usage"] == {"pruned": 0, "skipped": True}
        assert result["agent_scope_snapshots"] == {"pruned": 0, "skipped": True}

    def test_unregistered_trail_key_in_input_is_ignored(self):
        from src.audit_retention import run_retention_sweep

        result = run_retention_sweep({"chat_messages": 30, "sync_history": 0})
        assert "chat_messages" not in result
        assert set(result) == {"sync_history", "llm_usage", "agent_scope_snapshots"}


# ---------------------------------------------------------------------------
# app/instance_config.py — the four new retention knobs
# ---------------------------------------------------------------------------


class TestRetentionDaysConfig:
    @pytest.mark.parametrize(
        "getter_name,config_key",
        [
            ("get_sync_history_retention_days", "sync_history_days"),
            ("get_llm_usage_retention_days", "llm_usage_days"),
            ("get_agent_scope_snapshots_retention_days", "agent_scope_snapshots_days"),
        ],
    )
    def test_default_is_zero(self, monkeypatch, getter_name, config_key):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert getattr(ic, getter_name)() == 0

    @pytest.mark.parametrize(
        "getter_name,config_key",
        [
            ("get_sync_history_retention_days", "sync_history_days"),
            ("get_llm_usage_retention_days", "llm_usage_days"),
            ("get_agent_scope_snapshots_retention_days", "agent_scope_snapshots_days"),
        ],
    )
    def test_reads_configured_value(self, monkeypatch, getter_name, config_key):
        import app.instance_config as ic

        def _get_value(*keys, default=None):
            return 45 if keys == ("retention", config_key) else default

        monkeypatch.setattr(ic, "get_value", _get_value)
        assert getattr(ic, getter_name)() == 45

    @pytest.mark.parametrize(
        "getter_name",
        [
            "get_sync_history_retention_days",
            "get_llm_usage_retention_days",
            "get_agent_scope_snapshots_retention_days",
        ],
    )
    def test_negative_value_clamped_to_zero(self, monkeypatch, getter_name):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: -10)
        assert getattr(ic, getter_name)() == 0

    @pytest.mark.parametrize(
        "getter_name",
        [
            "get_sync_history_retention_days",
            "get_llm_usage_retention_days",
            "get_agent_scope_snapshots_retention_days",
        ],
    )
    def test_invalid_value_falls_back_to_zero(self, monkeypatch, getter_name):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: "garbage")
        assert getattr(ic, getter_name)() == 0


class TestUsageEventsRetentionDaysReconciliation:
    def test_default_is_zero(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.get_usage_events_retention_days() == 0

    def test_env_var_wins_over_config(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "7")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: 90)
        assert ic.get_usage_events_retention_days() == 7

    def test_config_used_when_env_var_unset(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)

        def _get_value(*keys, default=None):
            return 21 if keys == ("retention", "usage_events_days") else default

        monkeypatch.setattr(ic, "get_value", _get_value)
        assert ic.get_usage_events_retention_days() == 21

    def test_empty_env_var_falls_through_to_config(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "")

        def _get_value(*keys, default=None):
            return 14 if keys == ("retention", "usage_events_days") else default

        monkeypatch.setattr(ic, "get_value", _get_value)
        assert ic.get_usage_events_retention_days() == 14


# ---------------------------------------------------------------------------
# POST /api/admin/run-retention-prune
# ---------------------------------------------------------------------------


class TestRunRetentionPruneEndpoint:
    def test_default_config_reports_all_skipped_and_deletes_nothing(self, seeded_app, admin_user):
        """The end-to-end safe-default proof: on a freshly-seeded instance
        (no retention.* configured), the endpoint must report every trail
        skipped — nothing pruned."""
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-retention-prune", headers=admin_user)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        for trail_result in body["details"].values():
            assert trail_result == {"pruned": 0, "skipped": True}

    def test_admin_can_trigger_prune_when_configured(self, seeded_app, admin_user, monkeypatch):
        import app.instance_config as ic
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository

        def _get_value(*keys, default=None):
            return 30 if keys == ("retention", "sync_history_days") else default

        monkeypatch.setattr(ic, "get_value", _get_value)

        sys_conn = get_system_db()
        repo = SyncStateRepository(sys_conn)
        repo.update_sync(table_id="t.old", rows=1, file_size_bytes=10, hash="h1")
        old_hist_id = repo.get_sync_history("t.old")[0]["id"]
        from datetime import datetime, timedelta, timezone

        sys_conn.execute(
            "UPDATE sync_history SET synced_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) - timedelta(days=400), old_hist_id],
        )
        sys_conn.close()

        c = seeded_app["client"]
        resp = c.post("/api/admin/run-retention-prune", headers=admin_user)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["details"]["sync_history"]["skipped"] is False
        assert body["details"]["sync_history"]["pruned"] >= 1
        assert body["details"]["llm_usage"] == {"pruned": 0, "skipped": True}
        assert body["details"]["agent_scope_snapshots"] == {"pruned": 0, "skipped": True}

        check_conn = get_system_db()
        remaining = check_conn.execute("SELECT id FROM sync_history WHERE id = ?", [old_hist_id]).fetchall()
        check_conn.close()
        assert remaining == []

    def test_writes_its_own_audit_row(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-retention-prune", headers=admin_user)
        assert resp.status_code == 200

        from src.db import get_system_db

        check_conn = get_system_db()
        row = check_conn.execute(
            "SELECT action, resource FROM audit_log WHERE action = 'run_retention_prune' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        check_conn.close()
        assert row is not None
        assert row[1] == "job:retention-prune"

    def test_non_admin_blocked(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-retention-prune", headers=analyst_user)
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-retention-prune")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scheduler registration — services/scheduler/__main__.py::build_jobs
# ---------------------------------------------------------------------------


class TestRetentionPruneSchedulerJob:
    def test_retention_prune_job_present(self):
        from services.scheduler.__main__ import build_jobs

        jobs = {j[0]: j for j in build_jobs()}
        assert "retention-prune" in jobs
        name, schedule, endpoint, method, timeout_sec = jobs["retention-prune"][:5]
        assert schedule == "daily 05:45"
        assert endpoint == "/api/admin/run-retention-prune"
        assert method == "POST"

    def test_retention_prune_job_not_a_queued_enqueue_body(self):
        """Cheap DELETEs — runs synchronously via HTTP like audit-prune, not
        routed through the /api/jobs queue (no 6th json_body element)."""
        from services.scheduler.__main__ import build_jobs

        job = next(j for j in build_jobs() if j[0] == "retention-prune")
        assert len(job) == 5

    def test_retention_prune_offset_from_audit_prune(self):
        """Both are cheap DELETE-only jobs; distinct ticks so they never
        compete for the same 30s scheduler window."""
        from services.scheduler.__main__ import build_jobs

        jobs = {j[0]: j for j in build_jobs()}
        assert jobs["audit-prune"][1] != jobs["retention-prune"][1]
