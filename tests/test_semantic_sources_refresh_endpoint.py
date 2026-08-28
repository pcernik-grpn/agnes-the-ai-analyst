"""End-to-end tests for POST /api/admin/run-semantic-sources-refresh.

This is the ONE generic scheduled refresh over every registered
``semantic_sources`` row (git / upload / connection kinds) — Block 3 step 2
of issue #1707. It walks ``semantic_sources``, skips ``enabled=False`` rows
(counted, never synced), and calls ``src.semantic.transports.import_source``
on the rest, one failing source never aborting the sweep over the others.

Out of scope here (steps 3-4 of #1707, a separate follow-up): the legacy
Keboola (``run-keboola-semantic-layer-refresh``) and Databricks
(``run-databricks-semantic-layer-refresh``) refresh endpoints, which keep
their own schedules and provenance labels untouched.
"""

from __future__ import annotations

import asyncio

import pytest

DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_source(c, token, *, kind, name, adapter="native", config=None, enabled=True):
    r = c.post(
        "/api/admin/semantic-sources",
        json={
            "kind": kind,
            "name": name,
            "adapter": adapter,
            "config": config or {},
            "enabled": enabled,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    from app.api import semantic_sources_refresh as endpoint_module

    endpoint_module._refresh_state.update({"run_id": None, "started_at": None})
    yield
    endpoint_module._refresh_state.update({"run_id": None, "started_at": None})


def test_run_refresh_syncs_enabled_sources(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _create_source(c, token, kind="upload", name="Bundle A", config={"documents": [DOC]})

    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["synced"] == 1
    assert body["failed"] == 0
    assert body["skipped_disabled"] == 0
    assert len(body["sources"]) == 1
    assert body["sources"][0]["status"] == "ok"


def test_run_refresh_skips_disabled_sources_and_counts_them(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _create_source(c, token, kind="upload", name="Enabled bundle", config={"documents": [DOC]})
    disabled_id = _create_source(
        c, token, kind="upload", name="Disabled bundle", config={"documents": [DOC]}, enabled=False
    )

    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == 1
    assert body["skipped_disabled"] == 1
    assert body["failed"] == 0
    statuses = {s["id"]: s["status"] for s in body["sources"]}
    assert statuses[disabled_id] == "skipped_disabled"

    # A disabled source's last_sync_* must be untouched by the sweep.
    row = c.get(f"/api/admin/semantic-sources/{disabled_id}", headers=_auth(token))
    assert row.json()["last_sync_status"] is None


def test_one_failing_source_does_not_abort_the_sweep(seeded_app, monkeypatch):
    """A ``git`` source whose clone fails must not stop the sweep from
    reaching the sources after it. Mocks ``transports._clone`` rather than
    hitting a real (unreachable) host — same pattern as
    ``tests/test_semantic_transports.py::test_failed_clone_records_the_error_and_imports_nothing``.
    Only the git source calls ``_clone``; the upload source is unaffected.
    """
    import src.semantic.transports as transports

    def _boom(**kwargs):
        raise RuntimeError("clone failed: host unreachable")

    monkeypatch.setattr(transports, "_clone", _boom)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    bad_id = _create_source(
        c,
        token,
        kind="git",
        name="Broken repo",
        config={"repo_url": "https://example.com/nope.git"},
    )
    good_id = _create_source(c, token, kind="upload", name="Good bundle", config={"documents": [DOC]})

    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["synced"] == 1
    assert body["failed"] == 1
    statuses = {s["id"]: s for s in body["sources"]}
    assert statuses[bad_id]["status"] == "error"
    assert statuses[bad_id].get("error")
    assert statuses[good_id]["status"] == "ok"

    # import_source already records per-source last_sync_* — verify it did,
    # so the endpoint isn't double-recording on top of it.
    bad_row = c.get(f"/api/admin/semantic-sources/{bad_id}", headers=_auth(token))
    assert bad_row.json()["last_sync_status"] == "error"
    good_row = c.get(f"/api/admin/semantic-sources/{good_id}", headers=_auth(token))
    assert good_row.json()["last_sync_status"] == "ok"


def test_run_refresh_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/run-semantic-sources-refresh")
    assert r.status_code == 401


def test_non_admin_is_forbidden(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 403


def test_run_refresh_returns_409_when_already_running(seeded_app):
    from app.api import semantic_sources_refresh as endpoint_module

    async def _acquire():
        await endpoint_module._refresh_lock.acquire()

    asyncio.run(_acquire())
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_running"
    finally:
        endpoint_module._refresh_lock.release()


def test_no_sources_registered_is_a_clean_noop(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == 0
    assert body["failed"] == 0
    assert body["skipped_disabled"] == 0
    assert body["sources"] == []


class TestManualSyncOnDisabledSource:
    """POST /api/admin/semantic-sources/{id}/sync must refuse a disabled
    source rather than syncing it anyway — the manual escape hatch honors
    the same `enabled` flag as the scheduled sweep."""

    def test_manual_sync_of_disabled_source_answers_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        source_id = _create_source(
            c, token, kind="upload", name="Disabled bundle", config={"documents": [DOC]}, enabled=False
        )

        r = c.post(f"/api/admin/semantic-sources/{source_id}/sync", headers=_auth(token))
        assert r.status_code == 409, r.text
        body = r.json()["detail"]
        assert body["error"] == "source_disabled"
        assert "enabled" in body["hint"].lower()

    def test_manual_sync_of_enabled_source_still_works(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        source_id = _create_source(c, token, kind="upload", name="Enabled bundle", config={"documents": [DOC]})

        r = c.post(f"/api/admin/semantic-sources/{source_id}/sync", headers=_auth(token))
        assert r.status_code == 200, r.text
