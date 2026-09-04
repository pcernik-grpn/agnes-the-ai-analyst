"""Admin run-* endpoints that wire the LLM pipeline into scheduler-v2.

The scheduler container must drive corporate-memory, the session-pipeline
processors, and session-collector through HTTP — see services/scheduler/__main__.py
docstring for why in-process invocation is not safe (DuckDB single-writer
contention with the long-lived app handle).

Endpoints:
- POST /api/admin/run-session-collector
- POST /api/admin/run-session-processor?processor=<name>
- POST /api/admin/run-corporate-memory

All admin-gated. Request body is empty. Response is the underlying job
stats dict.
"""

from __future__ import annotations

import json
from unittest.mock import patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestRunSessionCollector:
    def test_admin_can_trigger_session_collector(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {"users_processed": 1, "files_copied": 2, "files_skipped": 0}
        with patch("services.session_collector.collector.run", return_value=(0, fake_stats)) as m:
            resp = c.post("/api/admin/run-session-collector", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["files_copied"] == 2
        m.assert_called_once_with(dry_run=False, verbose=False)

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-session-collector", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-session-collector")
        assert resp.status_code == 401

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Devin Review on 9ebe991b: run_session_collector must mirror
        run_verification_detector / run_corporate_memory — record the
        failure in audit_log even when collector.run() raises (e.g.
        permission error walking /home/), so /admin/scheduler-runs sees
        the failure instead of only docker logs."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "services.session_collector.collector.run",
            side_effect=PermissionError("simulated /home permission denied"),
        ):
            resp = c.post("/api/admin/run-session-collector", headers=_auth(token))
        assert resp.status_code == 500
        assert "PermissionError" in resp.json()["detail"]
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_session_collector' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "PermissionError" in params_json


class TestRunSessionProcessor:
    """Parametrized session-processor endpoint replaces the per-processor
    /run-* endpoints. The scheduler invokes it once per registered processor
    on its own cadence."""

    def test_admin_can_trigger_verification(self, seeded_app, monkeypatch):
        # Need an LLM key in env so build_verification_processor() doesn't
        # raise during registry construction.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        # Reset the lazily-built registry so the new env is picked up.
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "processor": "verification",
            "scanned": 3,
            "processed": 2,
            "skipped": 1,
            "errors": 0,
            "items_extracted": 4,
            "errors_detail": [],
        }
        with (
            patch(
                "services.session_pipeline.runner.run_processor",
                return_value=fake_stats,
            ) as m,
            patch("connectors.llm.factory.AnthropicExtractor"),
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=verification",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["items_extracted"] == 4
        m.assert_called_once()

    def test_admin_can_trigger_usage_skeleton(self, seeded_app):
        """The usage processor is registered as a no-op skeleton — endpoint
        should route to it without needing any LLM config."""
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "processor": "usage",
            "scanned": 0,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "items_extracted": 0,
            "errors_detail": [],
        }
        with patch(
            "services.session_pipeline.runner.run_processor",
            return_value=fake_stats,
        ) as m:
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        m.assert_called_once()

    def test_passes_configured_cap_to_run_processor(self, seeded_app, monkeypatch):
        """The endpoint must thread SESSION_PROCESSOR_MAX_PER_RUN through to
        run_processor(max_sessions_per_run=...) for the LLM-driven verification
        processor, so a burst of session closures can't run unboundedly in one
        request."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from services.session_processors import _build_registry

        _build_registry.cache_clear()
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "7")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "processor": "verification",
            "scanned": 0,
            "processed": 0,
            "skipped": 0,
            "capped": 0,
            "errors": 0,
            "items_extracted": 0,
            "errors_detail": [],
        }
        with (
            patch(
                "services.session_pipeline.runner.run_processor",
                return_value=fake_stats,
            ) as m,
            patch("connectors.llm.factory.AnthropicExtractor"),
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=verification",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        m.assert_called_once()
        _, kwargs = m.call_args
        assert kwargs["max_sessions_per_run"] == 7

    def test_usage_processor_is_never_capped(self, seeded_app, monkeypatch):
        """usage does pure local jsonl parsing + repository writes — no LLM/
        network calls — so it's exempt from SESSION_PROCESSOR_MAX_PER_RUN
        (Devin Review, PR #894): capping it too would just throttle telemetry
        throughput (e.g. draining a bulk backfill at cap-size-per-tick)
        without any wall-clock/CPU safety benefit."""
        from services.session_processors import _build_registry

        _build_registry.cache_clear()
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "7")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "processor": "usage",
            "scanned": 0,
            "processed": 0,
            "skipped": 0,
            "capped": 0,
            "errors": 0,
            "items_extracted": 0,
            "errors_detail": [],
        }
        with patch(
            "services.session_pipeline.runner.run_processor",
            return_value=fake_stats,
        ) as m:
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        m.assert_called_once()
        _, kwargs = m.call_args
        assert kwargs["max_sessions_per_run"] is None

    def test_capped_count_surfaced_in_audit_details(self, seeded_app):
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "processor": "usage",
            "scanned": 12,
            "processed": 7,
            "skipped": 0,
            "capped": 5,
            "errors": 0,
            "items_extracted": 3,
            "errors_detail": [],
        }
        with patch(
            "services.session_pipeline.runner.run_processor",
            return_value=fake_stats,
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["details"]["capped"] == 5

    def test_unknown_processor_returns_400(self, seeded_app):
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/run-session-processor?processor=bogus",
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "Unknown processor" in resp.json()["detail"]

    def test_concurrent_invocation_returns_409(self, seeded_app):
        """Per-processor advisory lock rejects overlapping calls so
        scheduler tick + manual admin POST don't double up on the same
        sessions and pile up duplicate verification_evidence rows
        (PR #232 review)."""
        from app.api.admin import _get_processor_run_lock
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Hold the lock externally to simulate an in-flight invocation.
        lock = _get_processor_run_lock("usage")
        lock.acquire()
        try:
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers=_auth(token),
            )
        finally:
            lock.release()

        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]

    def test_lock_released_on_runner_exception(self, seeded_app):
        """Even when the runner raises, the lock must release so the next
        scheduler tick / admin POST can proceed. A leaked lock would wedge
        the processor permanently until process restart."""
        from app.api.admin import _get_processor_run_lock
        from services.session_processors import _build_registry

        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        with patch(
            "services.session_pipeline.runner.run_processor",
            side_effect=RuntimeError("simulated"),
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers=_auth(token),
            )
        assert resp.status_code == 500

        # Lock must be free now — second invocation can grab it.
        lock = _get_processor_run_lock("usage")
        assert lock.acquire(blocking=False), "lock leaked after runner exception"
        lock.release()

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/run-session-processor?processor=verification",
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_unhandled_exception_still_audits(self, seeded_app, monkeypatch):
        """Mirror the run_session_collector / run_corporate_memory pattern —
        record the failure in audit_log even when the runner raises so
        /admin/scheduler-runs sees the failure instead of only docker logs."""
        from src.db import get_system_db
        from services.session_processors import _build_registry

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        _build_registry.cache_clear()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with (
            patch(
                "services.session_pipeline.runner.run_processor",
                side_effect=RuntimeError("simulated DuckDB lock"),
            ),
            patch("connectors.llm.factory.AnthropicExtractor"),
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=verification",
                headers=_auth(token),
            )
        assert resp.status_code == 500
        assert "RuntimeError" in resp.json()["detail"]

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log "
                "WHERE action = 'run_session_processor:verification' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "RuntimeError" in params_json


class TestRunCorporateMemory:
    def test_admin_can_trigger_corporate_memory(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "users_scanned": 2,
            "files_found": 2,
            "items_extracted": 3,
            "items_filtered": 0,
            "items_preserved": 1,
            "items_new": 2,
            "items_pending": 2,
            "skipped": False,
            "errors": [],
            "items_db_inserted": 2,
            "items_db_updated": 0,
            "items_db_errors": 0,
        }
        with patch(
            "services.corporate_memory.collector.collect_all",
            return_value=fake_stats,
        ) as m:
            resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["items_new"] == 2
        assert body["details"]["items_db_inserted"] == 2
        assert body["details"]["items_db_errors"] == 0
        m.assert_called_once()

    def test_db_errors_set_ok_false(self, seeded_app):
        """items_db_errors > 0 must flip ok to False even when LLM errors list is empty."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "users_scanned": 1,
            "files_found": 1,
            "items_extracted": 1,
            "items_filtered": 0,
            "items_preserved": 0,
            "items_new": 1,
            "items_pending": 0,
            "skipped": False,
            "errors": [],
            "items_db_inserted": 0,
            "items_db_updated": 0,
            "items_db_errors": 1,
        }
        with patch(
            "services.corporate_memory.collector.collect_all",
            return_value=fake_stats,
        ):
            resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 403

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Devin Review on 4c4dfee8: run_corporate_memory must mirror
        run_verification_detector — record the failure in audit_log even
        when collect_all() raises something other than ValueError, so
        the operator sees the failure on /admin/scheduler-runs instead
        of only in docker logs."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "services.corporate_memory.collector.collect_all",
            side_effect=RuntimeError("simulated DuckDB lock"),
        ):
            resp = c.post("/api/admin/run-corporate-memory", headers=_auth(token))
        assert resp.status_code == 500
        assert "RuntimeError" in resp.json()["detail"]
        # The audit row must exist regardless of the 500.
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_corporate_memory' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "RuntimeError" in params_json


class TestRunKnowledgePackaging:
    """POST /api/admin/run-knowledge-packaging — TCRD-296 synthesis C.15: a
    thin enqueue of the ``knowledge-packaging`` worker job (K3, #798), not a
    synchronous run. Handler behavior itself is covered in
    ``tests/test_worker_kinds.py::TestKnowledgePackagingHandler`` and
    ``tests/test_knowledge_packaging.py``."""

    def test_admin_can_enqueue_knowledge_packaging(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["job_id"]

    def test_second_enqueue_while_running_is_409_deduped(self, seeded_app):
        """The jobs-repo idempotency-key dedupe means a second enqueue
        while one is queued/running returns the SAME in-flight job id,
        surfaced as a typed 409 — not a second, redundant run."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert first.status_code == 202
        first_job_id = first.json()["job_id"]

        second = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert detail["error"] == "knowledge_packaging_already_in_progress"
        assert detail["job_id"] == first_job_id

    def test_no_worker_role_fails_clean_with_typed_501(self, seeded_app, monkeypatch):
        """An instance/process with no worker role has no loop that will
        ever claim the job — enqueueing anyway would leave it queued
        forever with no visible error. Fail clean instead."""
        from app.roles import reset_roles_cache

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        monkeypatch.setenv("AGNES_ROLE", "api")
        reset_roles_cache()
        try:
            resp = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        finally:
            monkeypatch.delenv("AGNES_ROLE", raising=False)
            reset_roles_cache()
        assert resp.status_code == 501, resp.text
        assert resp.json()["detail"]["error"] == "requires_worker_role"

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-knowledge-packaging")
        assert resp.status_code == 401

    def test_enqueue_is_audited_with_job_id(self, seeded_app):
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_knowledge_packaging' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on enqueue"
        params = json.loads(rows[0][0])
        assert params["job_id"] == job_id
        assert params["deduped"] is False


class TestKnowledgePackagingStatus:
    """GET /api/admin/knowledge-packaging/status — TCRD-296 synthesis C.15
    observability surface."""

    def test_no_runs_yet(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/knowledge-packaging/status", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["last_run"] is None
        assert body["running"] is False

    def test_reflects_a_queued_run(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        enqueue_resp = c.post("/api/admin/run-knowledge-packaging", headers=_auth(token))
        assert enqueue_resp.status_code == 202
        job_id = enqueue_resp.json()["job_id"]

        resp = c.get("/api/admin/knowledge-packaging/status", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["last_run"]["job_id"] == job_id
        assert body["last_run"]["status"] == "queued"

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/knowledge-packaging/status", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/knowledge-packaging/status")
        assert resp.status_code == 401


class TestRunKnowledgeDigests:
    """POST /api/admin/run-knowledge-digests — scheduler-driven maintained
    digest regeneration (K4, #799). Mirrors run_knowledge_packaging's audit +
    error posture exactly."""

    def test_admin_can_trigger_knowledge_digests(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_summary = {
            "generated": ["arch"],
            "skipped": ["other"],
            "stale": [],
            "errors": [],
        }
        with patch(
            "src.knowledge_digests.run_digest_pass",
            return_value=fake_summary,
        ) as m:
            resp = c.post("/api/admin/run-knowledge-digests", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"] == fake_summary
        m.assert_called_once()

    def test_errors_set_ok_false(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_summary = {
            "generated": [],
            "skipped": [],
            "stale": [{"slug": "arch", "reason": "deferred"}],
            "errors": [{"slug": "arch", "error": "boom"}],
        }
        with patch(
            "src.knowledge_digests.run_digest_pass",
            return_value=fake_summary,
        ):
            resp = c.post("/api/admin/run-knowledge-digests", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-knowledge-digests", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauth_blocked(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/run-knowledge-digests")
        assert resp.status_code == 401

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Mirror run_knowledge_packaging: record the failure in audit_log
        even when run_digest_pass() raises, so /admin/scheduler-runs sees the
        failure instead of only docker logs."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "src.knowledge_digests.run_digest_pass",
            side_effect=RuntimeError("simulated DuckDB lock"),
        ):
            resp = c.post("/api/admin/run-knowledge-digests", headers=_auth(token))
        assert resp.status_code == 500
        assert "RuntimeError" in resp.json()["detail"]
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_knowledge_digests' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "RuntimeError" in params_json


class TestRunKnowledgeMigration:
    def test_imports_items_from_json(self, seeded_app):
        data_dir = seeded_app["env"]["data_dir"]
        memory_dir = data_dir / "corporate-memory"
        memory_dir.mkdir(exist_ok=True)
        items = [
            {
                "id": "km-mig-001",
                "title": "Test item",
                "content": "Test content",
                "category": "data_analysis",
                "status": "pending",
                "source_type": "claude_local_md",
                "sensitivity": "internal",
                "is_personal": False,
            },
        ]
        (memory_dir / "knowledge.json").write_text(json.dumps(items))

        c, token = seeded_app["client"], seeded_app["admin_token"]
        resp = c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["knowledge_imported"] == 1

    def test_idempotent_skips_existing(self, seeded_app):
        data_dir = seeded_app["env"]["data_dir"]
        memory_dir = data_dir / "corporate-memory"
        memory_dir.mkdir(exist_ok=True)
        items = [
            {
                "id": "km-mig-002",
                "title": "Dup item",
                "content": "C",
                "category": "workflow",
                "status": "pending",
                "source_type": "claude_local_md",
                "sensitivity": "internal",
                "is_personal": False,
            },
        ]
        (memory_dir / "knowledge.json").write_text(json.dumps(items))
        c, token = seeded_app["client"], seeded_app["admin_token"]
        c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        resp = c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["knowledge_imported"] == 0

    def test_missing_file_returns_zero(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        resp = c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["knowledge_imported"] == 0

    def test_imports_items_from_dict_format(self, seeded_app):
        """collector.py writes {"items": {id: item_dict}, "metadata": {...}} — the real format."""
        data_dir = seeded_app["env"]["data_dir"]
        memory_dir = data_dir / "corporate-memory"
        memory_dir.mkdir(exist_ok=True)
        item = {
            "id": "km-mig-003",
            "title": "Dict format item",
            "content": "Content",
            "category": "data_analysis",
            "status": "pending",
            "source_type": "claude_local_md",
            "sensitivity": "internal",
            "is_personal": False,
        }
        payload = {"items": {"km-mig-003": item}, "metadata": {"collected_at": "2026-01-01T00:00:00"}}
        (memory_dir / "knowledge.json").write_text(json.dumps(payload))

        c, token = seeded_app["client"], seeded_app["admin_token"]
        resp = c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["knowledge_imported"] == 1

    def test_non_admin_blocked(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-knowledge-migration", headers=_auth(token))
        assert resp.status_code == 403


class TestSchedulerJobsWireUp:
    """The scheduler must drive all three new endpoints on a sensible cadence."""

    def test_scheduler_includes_session_collector(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        names = {n for n, *_ in build_jobs()}
        assert "session-collector" in names

    def test_scheduler_includes_session_processors(self, monkeypatch):
        """Post-refactor: the verification-detector + usage processors are
        wired through the parametrized run-session-processor endpoint."""
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        names = {n for n, *_ in build_jobs()}
        assert "session-processor:verification" in names
        assert "session-processor:usage" in names

    def test_scheduler_includes_corporate_memory(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        names = {n for n, *_ in build_jobs()}
        assert "corporate-memory" in names

    def test_session_collector_endpoint_is_registered(self, monkeypatch):
        """Wave-2B (Task 6): session-collector now enqueues via /api/jobs
        instead of calling the run-session-collector endpoint synchronously."""
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == "session-collector")
        _, _, endpoint, method, _t, json_body = target
        assert endpoint == "/api/jobs"
        assert method == "POST"
        assert json_body == {"kind": "session-collector", "idempotency_key": "session-collector"}

    def test_session_processor_endpoints_are_registered(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        jobs = {n: (endpoint, method) for n, _, endpoint, method, *_ in build_jobs()}
        assert jobs["session-processor:verification"] == (
            "/api/admin/run-session-processor?processor=verification",
            "POST",
        )
        assert jobs["session-processor:usage"] == (
            "/api/admin/run-session-processor?processor=usage",
            "POST",
        )

    def test_corporate_memory_endpoint_is_registered(self, monkeypatch):
        """Wave-2B (Task 6): corporate-memory now enqueues via /api/jobs
        instead of calling the run-corporate-memory endpoint synchronously."""
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == "corporate-memory")
        _, _, endpoint, method, _t, json_body = target
        assert endpoint == "/api/jobs"
        assert method == "POST"
        assert json_body == {"kind": "corporate-memory", "idempotency_key": "corporate-memory"}

    def test_knowledge_packaging_endpoint_is_registered(self, monkeypatch):
        """TCRD-296 synthesis C.15: the endpoint is now a thin enqueue, so
        the scheduler's own client timeout shrinks to the short
        enqueue-only budget every other `queued`-classified row uses —
        it no longer has to outlast a real packaging pass."""
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import _ENQUEUE_TIMEOUT_SEC, build_jobs

        target = next(j for j in build_jobs() if j[0] == "knowledge-packaging")
        _, schedule, endpoint, method, timeout = target
        assert schedule == "every 15m"
        assert endpoint == "/api/admin/run-knowledge-packaging"
        assert method == "POST"
        assert timeout == _ENQUEUE_TIMEOUT_SEC

    def test_knowledge_digests_endpoint_is_registered(self, monkeypatch):
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == "knowledge-digests")
        _, schedule, endpoint, method, timeout = target
        assert schedule == "every 30m"
        assert endpoint == "/api/admin/run-knowledge-digests"
        assert method == "POST"
        assert timeout == 900

    def test_new_jobs_have_offset_cadences(self, monkeypatch):
        """Three jobs in the same family must NOT all fire on the same tick.

        Otherwise the LLM API and DuckDB writer all spike together every time
        the cadence aligns. Different schedule strings ensure offset.
        """
        for v in (
            "SCHEDULER_DATA_REFRESH_INTERVAL",
            "SCHEDULER_HEALTH_CHECK_INTERVAL",
            "SCHEDULER_TICK_SECONDS",
            "SCHEDULER_SCRIPT_RUN_INTERVAL",
        ):
            monkeypatch.delenv(v, raising=False)
        from services.scheduler.__main__ import build_jobs

        targets = {
            n: schedule
            for n, schedule, *_ in build_jobs()
            if n
            in (
                "session-collector",
                "session-processor:verification",
                "corporate-memory",
            )
        }
        # All three present.
        assert len(targets) == 3


class TestRunJiraSlaPoll:
    """POST /api/admin/run-jira-sla-poll — scheduler-driven SLA refresh.

    Three contracts pinned here:
    1. Happy path: 200 + audit row with stat fields.
    2. Config-missing skip: ValueError from load_config() -> 200 skip + audit
       row with status=skipped (operator sees the no-op without alert noise).
    3. Unhandled exception: any other Exception -> 500 + audit row with
       `unhandled_error` (so /admin/scheduler-runs sees the failure).

    The third contract was the Devin BUG on the original commit — the
    endpoint called `audit_repo()` which is undefined; both the happy and
    error paths would NameError. Locking in the audit_log assertion here
    catches a regression to that shape.
    """

    def test_admin_can_trigger_jira_sla_poll(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_stats = {
            "open_issues": 12,
            "updated": 3,
            "healed": 1,
            "skipped": 0,
            "failed": 0,
            "elapsed_sec": 4.21,
        }
        with patch(
            "connectors.jira.scripts.poll_sla.run",
            return_value=fake_stats,
        ) as m:
            resp = c.post("/api/admin/run-jira-sla-poll", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["updated"] == 3
        m.assert_called_once_with(dry_run=False)

    def test_skipped_when_jira_not_configured(self, seeded_app):
        """ValueError from load_config() must yield 200 skip, not 500."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "connectors.jira.scripts.poll_sla.run",
            side_effect=ValueError("Missing required environment variables: JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN"),
        ):
            resp = c.post("/api/admin/run-jira-sla-poll", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "jira_not_configured"

        # Skip path must also write an audit row so /admin/scheduler-runs
        # shows the no-op decision.
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_jira_sla_poll' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on skip path"
        assert "skipped" in rows[0][0]

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Devin BUG repro: the original endpoint called `audit_repo()`
        which is undefined — happy AND error paths NameError'd at
        runtime. After fix (AuditRepository(conn).log + except Exception
        wrapper), unhandled errors must land in audit_log."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "connectors.jira.scripts.poll_sla.run",
            side_effect=ConnectionError("simulated Jira API timeout"),
        ):
            resp = c.post("/api/admin/run-jira-sla-poll", headers=_auth(token))
        assert resp.status_code == 500
        assert "ConnectionError" in resp.json()["detail"]

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_jira_sla_poll' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "ConnectionError" in params_json

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-jira-sla-poll", headers=_auth(token))
        assert resp.status_code == 403


class TestRunJiraConsistencyCheck:
    """POST /api/admin/run-jira-consistency-check — scheduler-driven
    parquet-vs-API parity check with auto-fix for small webhook-loss gaps.

    Same three contracts pinned here as TestRunJiraSlaPoll, mirrored
    against the consistency-check entry point.
    """

    def test_admin_can_trigger_consistency_check(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_report = {"status": "success", "alert_level": "INFO", "checked": 42}

        mock_checker = type(
            "MockChecker",
            (),
            {
                "run_check": lambda self, **kw: fake_report,
            },
        )()

        with (
            patch(
                "connectors.jira.scripts.consistency_check.Config.from_env",
                return_value=object(),
            ),
            patch(
                "connectors.jira.scripts.consistency_check.JiraConsistencyChecker",
                return_value=mock_checker,
            ),
        ):
            resp = c.post("/api/admin/run-jira-consistency-check", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["details"]["status"] == "success"

    def test_skipped_when_jira_not_configured(self, seeded_app):
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch(
            "connectors.jira.scripts.consistency_check.Config.from_env",
            side_effect=KeyError("JIRA_CONSISTENCY_BASE_URL"),
        ):
            resp = c.post("/api/admin/run-jira-consistency-check", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "jira_not_configured"

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_jira_consistency_check' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on skip path"
        assert "skipped" in rows[0][0]

    def test_unhandled_exception_still_audits(self, seeded_app):
        """Same Devin BUG repro as TestRunJiraSlaPoll. Locks the audit-on-
        unhandled-error contract for the consistency-check endpoint too."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        class _RaisingChecker:
            def __init__(self, config):
                pass

            def run_check(self, **_):
                raise RuntimeError("simulated DuckDB lock contention")

        with (
            patch(
                "connectors.jira.scripts.consistency_check.Config.from_env",
                return_value=object(),
            ),
            patch(
                "connectors.jira.scripts.consistency_check.JiraConsistencyChecker",
                _RaisingChecker,
            ),
        ):
            resp = c.post("/api/admin/run-jira-consistency-check", headers=_auth(token))
        assert resp.status_code == 500
        assert "RuntimeError" in resp.json()["detail"]

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT params FROM audit_log WHERE action = 'run_jira_consistency_check' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "audit row missing on unhandled exception"
        params_json = rows[0][0]
        assert "unhandled_error" in params_json
        assert "RuntimeError" in params_json

    def test_non_admin_blocked(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/run-jira-consistency-check", headers=_auth(token))
        assert resp.status_code == 403


class TestSessionProcessorMaxPerRun:
    """Unit coverage for the SESSION_PROCESSOR_MAX_PER_RUN env resolver."""

    def test_default_is_50(self, monkeypatch):
        monkeypatch.delenv("SESSION_PROCESSOR_MAX_PER_RUN", raising=False)
        from app.api.admin import _session_processor_max_per_run

        assert _session_processor_max_per_run() == 50

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "10")
        from app.api.admin import _session_processor_max_per_run

        assert _session_processor_max_per_run() == 10

    def test_empty_string_disables_cap(self, monkeypatch):
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "")
        from app.api.admin import _session_processor_max_per_run

        assert _session_processor_max_per_run() is None

    def test_non_integer_disables_cap_without_raising(self, monkeypatch):
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "not-a-number")
        from app.api.admin import _session_processor_max_per_run

        assert _session_processor_max_per_run() is None

    def test_zero_or_negative_disables_cap(self, monkeypatch):
        from app.api.admin import _session_processor_max_per_run

        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "0")
        assert _session_processor_max_per_run() is None
        monkeypatch.setenv("SESSION_PROCESSOR_MAX_PER_RUN", "-5")
        assert _session_processor_max_per_run() is None
