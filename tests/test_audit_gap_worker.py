"""Audit coverage for the worker's single dispatch-level entry point
(``job.run``) and the previously-silent scheduler endpoints
(``run_bq_metadata_refresh``, ``run_semantic_sources_refresh``,
``run_store_lint_audit``) — F2b, audit-full-coverage plan, Task 4.

The two per-connector semantic-layer refreshes this file also covered
(``run_keboola_semantic_layer_refresh`` / ``run_databricks_semantic_layer_
refresh``) were retired by #1707 Block 3 step 4; the one generic sweep that
replaced them carries their audit obligation and is covered here in their
place. Their catalog entries survive in ``src/audit_events.py`` for the rows
existing instances already wrote — see the note there.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _rows(action: str):
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action=action, limit=50)
    return rows


def _params(row: dict) -> dict:
    """`audit_repo().query()` returns `params` as the raw stored JSON
    string, not a parsed dict — decode it here so tests can assert on the
    structured fields (same helper as `tests/test_agent_memory_write_api.py`)."""
    v = row.get("params")
    return json.loads(v) if isinstance(v, str) else (v or {})


# ---------------------------------------------------------------------------
# app/worker/kinds.py::dispatch_job — the single dispatch-level wrapper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test
    (mirrors ``tests/test_worker_kinds.py``/``tests/test_worker_runtime.py``)."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, closed after the test
    (mirrors ``tests/test_worker_runtime.py``'s ``worker_db`` fixture)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. audit_log)
    yield
    close_system_db()


class TestDispatchJobAudit:
    def test_success_writes_one_job_run_row(self, worker_env):
        from app.worker.kinds import dispatch_job
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        register_kind(JobKind(name="trivial-kind", handler=lambda payload: {"ok": True}, lane=LIGHT_LANE))
        job = {"id": "job-1", "kind": "trivial-kind", "payload_json": {}}

        result = dispatch_job(job)

        assert result == {"ok": True}
        rows = _rows("job.run")
        assert len(rows) == 1
        row = rows[0]
        assert row["resource"] == "job:trivial-kind"
        assert _params(row)["kind"] == "trivial-kind"
        assert _params(row)["outcome"] == "success"
        assert _params(row)["job_id"] == "job-1"
        assert row["result"] == "success"
        assert row["client_kind"] == "scheduler"
        assert row["user_id"] is None
        assert row["duration_ms"] is not None

    def test_error_writes_one_job_run_row_with_error_result(self, worker_env):
        from app.worker.kinds import dispatch_job
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        def _boom(payload):
            raise RuntimeError("boom")

        register_kind(JobKind(name="boom-kind", handler=_boom, lane=LIGHT_LANE))
        job = {"id": "job-2", "kind": "boom-kind", "payload_json": {}}

        with pytest.raises(RuntimeError):
            dispatch_job(job)

        rows = _rows("job.run")
        assert len(rows) == 1
        row = rows[0]
        assert _params(row)["kind"] == "boom-kind"
        assert _params(row)["outcome"] == "error"
        assert row["result"] == "error:RuntimeError"

    def test_dispatch_job_is_the_only_job_run_writer(self):
        """Ratchet against re-introducing per-kind audit calls: `job.run`
        must be emitted from exactly one place in `app/worker/kinds.py` —
        the two branches (success/error) of `dispatch_job` itself — never
        from an individual `_run_*` handler."""
        import inspect

        import app.worker.kinds as kinds

        source = inspect.getsource(kinds)
        assert source.count('action="job.run"') == 2


# ---------------------------------------------------------------------------
# The silent scheduler endpoints
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestBqMetadataRefreshAudit:
    def test_run_writes_audit_row(self, seeded_app):
        from app.api._metadata_models import TableMetadata
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                name="a_remote",
                id="a_remote",
                source_type="bigquery",
                bucket="dwh_base",
                source_table="a_remote",
                query_mode="remote",
            )
        finally:
            conn.close()

        fake = TableMetadata(rows=5, size_bytes=512, partition_by="d", clustered_by=["c"])
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("connectors.bigquery.metadata.fetch", return_value=fake):
            r = c.post("/api/admin/run-bq-metadata-refresh", headers=_auth(token))
        assert r.status_code == 200, r.text

        rows = _rows("run_bq_metadata_refresh")
        assert rows
        assert _params(rows[0])["succeeded"] >= 1
        assert _params(rows[0])["duration_ms"] is not None


class TestSemanticSourcesRefreshAudit:
    """The generic sweep replaced the two per-connector refresh endpoints
    (#1707 Block 3 step 4), so it inherits their audit obligation: one row
    per run, counters matching the response body exactly."""

    @pytest.fixture(autouse=True)
    def _reset_refresh_state(self):
        from app.api import semantic_sources_refresh as endpoint_module

        blank = {
            "run_id": None,
            "started_at": None,
            "last_completed_at": None,
            "last_status": None,
            "last_result": None,
        }
        endpoint_module._refresh_state.update(blank)
        yield
        endpoint_module._refresh_state.update(blank)

    def test_run_writes_audit_row(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        doc = (
            "version: '0.2.0.dev0'\n"
            "semantic_model:\n"
            "  - name: retail\n"
            "    datasets:\n"
            "      - name: orders\n"
            "        source: db.public.orders\n"
            "        fields: []\n"
        )
        created = c.post(
            "/api/admin/semantic-sources",
            json={"kind": "upload", "name": "Audited bundle", "adapter": "native", "config": {"documents": [doc]}},
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text

        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()

        rows = _rows("run_semantic_sources_refresh")
        assert rows
        params = _params(rows[0])
        assert params["run_id"] == body["run_id"]
        assert params["synced"] == body["synced"] >= 1
        assert params["failed"] == body["failed"] == 0
        # The counters the sweep actually reports — `skipped_legacy_owned` is
        # NOT one of them any more (the legacy jobs it used to yield to are
        # retired), so the audit row must not resurrect it.
        assert params["skipped_disabled"] == body["skipped_disabled"]
        assert params["skipped_running"] == body["skipped_running"]
        assert params["skipped_duplicate_project"] == body["skipped_duplicate_project"]
        assert params["migrated"] == len(body["migrated"])
        assert "skipped_legacy_owned" not in params


class TestStoreLintAuditAudit:
    def test_run_writes_audit_row(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post("/api/admin/store/lint-audit", json={"force": True}, headers=_auth(token))
        assert r.status_code == 200, r.text

        rows = _rows("run_store_lint_audit")
        assert rows
        assert _params(rows[0])["trigger"] == "admin"

    def test_skip_path_also_writes_audit_row(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        headers = _auth(token)

        first = c.post("/api/admin/store/lint-audit", json={"force": True}, headers=headers)
        assert first.status_code == 200, first.text

        second = c.post("/api/admin/store/lint-audit", json={}, headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["skipped"] is True

        rows = _rows("run_store_lint_audit")
        assert any(_params(row).get("skipped") is True for row in rows)
