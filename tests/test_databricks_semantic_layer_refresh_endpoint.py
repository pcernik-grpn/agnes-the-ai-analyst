"""End-to-end tests for POST /api/admin/run-databricks-semantic-layer-refresh.

Since Track D6, this endpoint no longer runs a direct `metric_definitions`
writer — it ensures the Databricks connection is registered as a
`connection`-kind semantic source and syncs it through
`src.semantic.transports.import_source`, the same pipeline every other
semantic source rides. These tests mock at that boundary; the adapter's own
fetch/compose logic is covered by `tests/test_databricks_semantic_ossie.py`
and `tests/test_databricks_wire_e2e.py`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from connectors.databricks.client import DatabricksApiError
from src.semantic.importer import ImportReport
from src.semantic.projection import ProjectionReport


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict — reset it around every test."""
    from app.api import databricks_semantic_layer_refresh as endpoint_module

    blank = {"run_id": None, "started_at": None}
    endpoint_module._refresh_state.update(blank)
    yield
    endpoint_module._refresh_state.update(blank)


def _post(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    return c.post(
        "/api/admin/run-databricks-semantic-layer-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )


def _patched(**overrides):
    defaults = dict(
        ensure_semantic_source=lambda: "databricks_default",
        import_source=lambda source_id: ImportReport(
            models_written=1,
            models_unchanged=0,
            models_pruned=[],
            invalid=[],
            projection=ProjectionReport(metrics_written=2, metrics_pruned=0),
        ),
        purge_legacy_metric_rows=lambda: 0,
    )
    defaults.update(overrides)
    return [patch(f"app.api.databricks_semantic_layer_refresh.{name}", fn) for name, fn in defaults.items()]


def test_run_refresh_returns_sync_result(seeded_app):
    patches = _patched(purge_legacy_metric_rows=lambda: 3)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["models_written"] == 1
    assert body["projection"]["metrics_written"] == 2
    assert body["purged_legacy"] == 3
    assert body["run_id"]
    assert body["started_at"]


def test_ensure_semantic_source_id_is_passed_to_import_source(seeded_app):
    seen = {}

    def _fake_import(source_id):
        seen["source_id"] = source_id
        return ImportReport(projection=ProjectionReport())

    patches = _patched(import_source=_fake_import)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    assert seen["source_id"] == "databricks_default"


def test_not_configured_answers_400(seeded_app):
    def _raise(source_id):
        raise RuntimeError("Databricks is not configured — refusing to sync semantic views")

    patches = _patched(import_source=_raise)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_upstream_client_error_answers_400(seeded_app):
    def _raise(source_id):
        raise DatabricksApiError("permission denied", status=403)

    patches = _patched(import_source=_raise)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 400


def test_upstream_server_error_answers_502(seeded_app):
    def _raise(source_id):
        raise DatabricksApiError("warehouse unreachable", status=503)

    patches = _patched(import_source=_raise)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 502


def test_unmapped_exception_answers_502(seeded_app):
    def _raise(source_id):
        raise ValueError("mystery")

    patches = _patched(import_source=_raise)
    for p in patches:
        p.start()
    try:
        r = _post(seeded_app)
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 502


def test_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/run-databricks-semantic-layer-refresh")
    assert r.status_code in (401, 403)


def test_run_refresh_returns_409_when_already_running(seeded_app):
    from app.api import databricks_semantic_layer_refresh as endpoint_module

    async def _acquire():
        await endpoint_module._refresh_lock.acquire()

    asyncio.run(_acquire())
    try:
        r = _post(seeded_app)
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_running"
    finally:
        endpoint_module._refresh_lock.release()
