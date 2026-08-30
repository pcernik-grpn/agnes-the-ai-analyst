"""End-to-end tests for POST /api/admin/run-semantic-sources-refresh.

This is the ONE scheduled refresh over every registered ``semantic_sources``
row (git / upload / connection kinds) — issue #1707 Block 3. It walks
``semantic_sources``, skips ``enabled=False`` rows (counted, never synced),
and calls ``src.semantic.transports.import_source`` on the rest, one failing
source never aborting the sweep over the others.

Since step 4 it is also the only one: the per-connector Keboola and
Databricks refresh endpoints are gone, and every sweep starts by
auto-registering the sources they implied
(``src.semantic.legacy_migration``). The equivalence of old and new output
is pinned in ``tests/test_semantic_legacy_refresh_migration.py``; what this
file adds is that the sweep actually runs the migration and the post-import
reconciliation, and survives either of them failing.
"""

from __future__ import annotations

import asyncio

import pytest

from src.semantic.importer import ImportReport

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


_RESET_STATE = {
    "run_id": None,
    "started_at": None,
    "last_completed_at": None,
    "last_status": None,
    "last_result": None,
}

#: What a patched ``import_source`` returns when the test does not care about
#: the import itself, only about the sweep's bookkeeping around it.
_FAKE_REPORT = ImportReport()


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    from app.api import semantic_sources_refresh as endpoint_module

    endpoint_module._refresh_state.update(_RESET_STATE)
    yield
    endpoint_module._refresh_state.update(_RESET_STATE)


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


class TestLegacyAutoMigration:
    """Every sweep first registers the sources the retired per-connector
    refreshes implied, then syncs them in the same pass."""

    def test_sweep_registers_and_syncs_a_migrated_source_in_one_pass(self, seeded_app, monkeypatch):
        from src.repositories import semantic_source_repo

        migrated = {
            "id": "keboola_conn-a",
            "kind": "connection",
            "name": "Keboola semantic layer — Project A",
            "adapter": "keboola_metastore",
            "config": {"connection_id": "conn-a"},
        }

        def fake_ensure():
            semantic_source_repo().create(**migrated, enabled=True)
            return [semantic_source_repo().get(migrated["id"])]

        import app.api.semantic_sources_refresh as endpoint_module

        monkeypatch.setattr(endpoint_module, "ensure_legacy_semantic_sources", fake_ensure)
        monkeypatch.setattr(endpoint_module, "import_source", lambda source_id: _FAKE_REPORT)

        c = seeded_app["client"]
        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        # Registered AND swept in the same run — a row that had to wait for
        # the next tick would leave a 6 h hole on the upgrade run.
        assert body["migrated"] == [migrated["id"]]
        assert [s["id"] for s in body["sources"]] == [migrated["id"]]
        assert body["synced"] == 1

    def test_a_failing_connector_migration_never_aborts_the_sweep(self, seeded_app, monkeypatch):
        """An unreadable vault or an unreachable connection registry must not
        cost the instance its sync of everything already registered — nor
        stop the OTHER connector from being migrated."""
        import src.semantic.legacy_migration as migration

        def boom():
            raise RuntimeError("connection registry unavailable")

        monkeypatch.setattr(migration, "_ensure_keboola_sources", boom)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        source_id = _create_source(c, token, kind="upload", name="Bundle A", config={"documents": [DOC]})

        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["migrated"] == []
        assert [s["id"] for s in body["sources"]] == [source_id]
        assert body["synced"] == 1

    def test_reconciliation_result_rides_along_but_a_failure_does_not_fail_the_sync(
        self, seeded_app, monkeypatch
    ):
        import app.api.semantic_sources_refresh as endpoint_module

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        source_id = _create_source(c, token, kind="upload", name="Bundle A", config={"documents": [DOC]})

        monkeypatch.setattr(
            endpoint_module, "reconcile_after_import", lambda source, report: {"metrics": 3}
        )
        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert r.status_code == 200, r.text
        entry = {s["id"]: s for s in r.json()["sources"]}[source_id]
        assert entry["status"] == "ok"
        assert entry["reconciled_legacy"] == {"metrics": 3}

    def test_a_raising_reconciler_leaves_the_sync_successful_and_the_sweep_running(self, seeded_app, monkeypatch):
        """Reconciliation is best-effort by contract: it runs AFTER the import
        already wrote and recorded. A reconciler that raises must therefore
        neither turn that success into a failure nor abort the sources behind
        it — which it did while the call sat outside the per-source guard."""
        import app.api.semantic_sources_refresh as endpoint_module

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = _create_source(c, token, kind="upload", name="A bundle", config={"documents": [DOC]})
        second = _create_source(c, token, kind="upload", name="B bundle", config={"documents": [DOC]})

        def boom(source, report):
            raise RuntimeError("reconciliation exploded")

        monkeypatch.setattr(endpoint_module, "reconcile_after_import", boom)

        r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["synced"] == 2
        assert body["failed"] == 0
        statuses = {s["id"]: s["status"] for s in body["sources"]}
        assert statuses[first] == "ok"
        assert statuses[second] == "ok"


class TestSharedSingleFlightWithTheLoginTriggeredSync:
    """The Keboola login-triggered sync (`run_semantic_layer_refresh_background`)
    and this sweep write the SAME
    ``(source='keboola_metastore', source_ref=<connection id>)`` rows. Two
    locks in two modules made them overlappable; one shared guard makes that
    impossible."""

    @pytest.fixture(autouse=True)
    def _no_legacy_env(self, monkeypatch):
        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

    def test_a_keboola_source_is_skipped_while_the_login_sync_holds_the_guard(self, seeded_app):
        from src.semantic.refresh_guard import KEBOOLA_SEMANTIC_REFRESH

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        keboola_id = _create_source(
            c,
            token,
            kind="connection",
            name="A Keboola project",
            adapter="keboola_metastore",
            config={"connection_id": "conn-a"},
        )
        other_id = _create_source(c, token, kind="upload", name="Z bundle", config={"documents": [DOC]})

        with KEBOOLA_SEMANTIC_REFRESH.try_claim("login:test") as claimed:
            assert claimed
            r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))

        assert r.status_code == 200, r.text
        body = r.json()
        statuses = {s["id"]: s["status"] for s in body["sources"]}
        assert statuses[keboola_id] == "skipped_running"
        assert statuses[other_id] == "ok"
        assert body["skipped_running"] == 1
        # Skipped, not failed — the next sweep picks it up, and the row keeps
        # whatever its last real sync said.
        assert body["failed"] == 0
        row = c.get(f"/api/admin/semantic-sources/{keboola_id}", headers=_auth(token))
        assert row.json()["last_sync_status"] is None

    def test_the_guard_is_released_again_once_the_sweep_returns(self, seeded_app):
        from src.semantic.refresh_guard import KEBOOLA_SEMANTIC_REFRESH

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        _create_source(
            c,
            token,
            kind="connection",
            name="A Keboola project",
            adapter="keboola_metastore",
            config={"connection_id": "conn-a"},
        )

        c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        assert KEBOOLA_SEMANTIC_REFRESH.busy is False


class TestSweepSummary:
    """The /admin/semantic-layer status strip reads this — the whole-sweep
    view, since the per-connector endpoint that used to feed it is gone."""

    def test_a_completed_sweep_is_recorded(self, seeded_app):
        from app.api.semantic_sources_refresh import get_last_refresh_summary

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        _create_source(c, token, kind="upload", name="Bundle A", config={"documents": [DOC]})

        assert get_last_refresh_summary()["last_status"] is None
        c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))

        summary = get_last_refresh_summary()
        assert summary["last_status"] == "ok"
        assert summary["last_completed_at"]
        assert summary["last_result"]["synced"] == 1

    def test_the_in_memory_accessor_never_answers_for_the_sources(self, seeded_app):
        """A10: the two accessors have to stay distinct. This one is
        in-memory by design ("no sweep in THIS process"); the composed one
        below is what may speak about history. A source synced before the
        (simulated) restart must not make this one claim a sweep ran."""
        from app.api.semantic_sources_refresh import get_last_refresh_summary
        from src.repositories import semantic_source_repo

        c = seeded_app["client"]
        source_id = _create_source(
            c, seeded_app["admin_token"], kind="upload", name="Bundle A", config={"documents": [DOC]}
        )
        semantic_source_repo().record_sync(source_id, status="ok", error=None)

        assert get_last_refresh_summary()["last_status"] is None

    def test_the_composed_summary_falls_back_to_the_sources_last_sync(self, seeded_app):
        """And the composed one does exactly what the strip needs: with no
        sweep in this process it reports the sources' own last sync, counted,
        so the caller can label it as the different claim it is."""
        from app.api.semantic_sources_refresh import get_sync_status_summary
        from src.repositories import semantic_source_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = _create_source(c, token, kind="upload", name="Bundle A", config={"documents": [DOC]})
        _create_source(c, token, kind="upload", name="Bundle B", config={"documents": [DOC]})
        semantic_source_repo().record_sync(first, status="error", error="boom")

        summary = get_sync_status_summary()
        assert summary["last_status"] is None
        # A failed sync is still a sync — the fallback is "last sync of any
        # kind", which is precisely why the strip must not label it "OK".
        assert summary["fallback_last_sync_at"]
        # One of two: the count the time speaks for, and the registered total.
        assert summary["fallback_synced_count"] == 1
        assert summary["fallback_source_count"] == 2
        assert summary["fallback_unavailable"] is False

        # Once a sweep runs in this process, the richer view takes over and
        # the fallback stops being computed at all.
        c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
        after = get_sync_status_summary()
        assert after["last_status"] == "ok"
        assert after["fallback_last_sync_at"] is None
        assert after["fallback_synced_count"] == 0

    def test_a_skipped_source_is_not_counted_as_synced(self, seeded_app):
        """`record_sync(status='skipped')` stamps `last_sync_at` on a source
        the sweep never imported from (the duplicate-upstream skip). Counting
        it would let a never-read source define the "last source sync"."""
        from app.api.semantic_sources_refresh import get_sync_status_summary
        from src.repositories import semantic_source_repo

        c = seeded_app["client"]
        source_id = _create_source(
            c, seeded_app["admin_token"], kind="upload", name="Bundle A", config={"documents": [DOC]}
        )
        semantic_source_repo().record_sync(source_id, status="skipped", error="two sources, one upstream")

        summary = get_sync_status_summary()
        assert summary["fallback_last_sync_at"] is None
        assert summary["fallback_synced_count"] == 0
        # The row still exists — it is just not something to speak for.
        assert summary["fallback_source_count"] == 1

    def test_a_failed_read_is_reported_as_unavailable_not_as_never(self, seeded_app, monkeypatch):
        """The distinction the strip needs: "I could not look" is not "nothing
        ever happened"."""
        import app.api.semantic_sources_refresh as sweep_module

        def _boom():
            raise RuntimeError("app-state unreachable")

        monkeypatch.setattr(sweep_module, "semantic_source_repo", _boom)

        summary = sweep_module.get_sync_status_summary()
        assert summary["fallback_unavailable"] is True
        assert summary["fallback_last_sync_at"] is None


def test_the_databricks_row_is_swept_exactly_once(seeded_app, monkeypatch):
    """The inverse of the guard this sweep needed before the migration.

    While the dedicated Databricks refresh existed, it called the very same
    ``import_source('databricks_default')`` on the very same row under the
    identical provenance, so the sweep had to SKIP that row or import it twice
    per tick. That job is gone (#1707 steps 3-4) and the sweep is now its only
    writer — the row must be imported, exactly once, like any other.
    """
    from connectors.databricks.semantic_layer import DATABRICKS_SEMANTIC_SOURCE_ID
    from src.repositories import semantic_source_repo

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    # The production row is created by ensure_semantic_source() through the
    # repository with this FIXED id — the admin API generates its own ids,
    # so go the same route the connector does.
    semantic_source_repo().create(
        id=DATABRICKS_SEMANTIC_SOURCE_ID,
        kind="connection",
        name="Databricks metric views",
        adapter="databricks_metric_views",
        config={"safe_prune": True},
        enabled=True,
    )

    calls = []

    def _record(source_id):
        calls.append(source_id)
        return _FAKE_REPORT

    monkeypatch.setattr("app.api.semantic_sources_refresh.import_source", _record)
    r = c.post("/api/admin/run-semantic-sources-refresh", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()

    assert calls == [DATABRICKS_SEMANTIC_SOURCE_ID]
    assert body["synced"] == 1
    assert {"id": DATABRICKS_SEMANTIC_SOURCE_ID, "name": "Databricks metric views", "status": "ok"} in body["sources"]
    # The counter that named the retired job is gone with it.
    assert "skipped_legacy_owned" not in body
