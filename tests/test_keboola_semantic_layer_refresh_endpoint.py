"""What is left of ``app/api/keboola_semantic_layer_refresh.py`` after #1707
Block 3 step 4 retired ``POST /api/admin/run-keboola-semantic-layer-refresh``:
the login-triggered background sync and the coverage report.

The scheduled trigger moved to the generic sweep over ``semantic_sources``
(``tests/test_semantic_sources_refresh_endpoint.py``), which imports the same
Keboola projects under the same provenance — the equivalence is pinned by
``tests/test_semantic_legacy_refresh_migration.py``.
"""

import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict — reset it around every test
    in this file so run order/leakage across tests can't affect assertions."""
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    reset = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    endpoint_module._refresh_state.update(reset)
    yield
    endpoint_module._refresh_state.update(reset)


def test_the_retired_refresh_route_is_gone(seeded_app):
    """Removed, not merely unscheduled: leaving it callable would let an
    admin (or a stale scheduler config) run a second writer over the same
    upstream the sweep now owns."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/run-keboola-semantic-layer-refresh",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    # 405 rather than 404: another router still owns a GET at this path
    # shape. Either way there is no POST handler left to run a second
    # writer over the upstream the sweep now owns.
    assert r.status_code in (404, 405), r.text


class TestBackgroundRefreshSummary:
    """`get_last_refresh_summary()` — the in-memory (since-last-restart)
    status of the LOGIN-triggered sync that stayed behind. The admin page's
    strip reads the generic sweep's summary instead (#1707 step 4)."""

    def test_initial_state_is_never_synced(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary

        summary = get_last_refresh_summary()
        assert summary["last_completed_at"] is None
        assert summary["last_status"] is None
        assert summary["last_result"] is None

    def test_successful_background_sync_records_summary(self, e2e_env):
        import app.api.keboola_semantic_layer_refresh as kslr

        fake_result = {"status": "ok", "created_or_updated": 5, "pruned": 1}
        with patch.object(kslr, "sync_semantic_layer", return_value=fake_result):
            asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="keboola-login"))

        summary = kslr.get_last_refresh_summary()
        assert summary["last_status"] == "ok"
        assert summary["last_completed_at"]
        assert summary["last_result"]["created_or_updated"] == 5
        # In-flight tracking still clears back to None once the run finishes.
        assert kslr._refresh_state["run_id"] is None
        assert kslr._refresh_state["started_at"] is None

    def test_returned_error_status_records_failure_summary(self, e2e_env):
        """A returned {"status": "error"} dict (not an exception) must also
        flip last_status to "error" — the case that once recorded "ok"
        unconditionally and showed a false-green summary."""
        import app.api.keboola_semantic_layer_refresh as kslr

        fake_result = {
            "status": "error",
            "error": "Keboola credentials not configured",
            "code": "credentials_not_configured",
        }
        with patch.object(kslr, "sync_semantic_layer", return_value=fake_result):
            asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="keboola-login"))

        summary = kslr.get_last_refresh_summary()
        assert summary["last_status"] == "error"
        assert summary["last_completed_at"]
        assert summary["last_result"] == "Keboola credentials not configured"

    def test_unexpected_exception_also_records_failure_summary(self, e2e_env):
        """A background sync never raises at its caller — but it must leave a
        visible trace instead of failing silently (#953)."""
        import app.api.keboola_semantic_layer_refresh as kslr

        with patch.object(kslr, "sync_semantic_layer", side_effect=RuntimeError("boom")):
            asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="keboola-login"))

        summary = kslr.get_last_refresh_summary()
        assert summary["last_status"] == "error"
        assert "boom" in summary["last_result"]


def test_coverage_endpoint_returns_computed_sources(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake = {
        "sources": [
            {
                "connection_id": "conn-1",
                "name": "Demo project",
                "metrics": {"upstream": 50, "importable": 0},
                "glossary": {"upstream": 9},
                "unregistered_tables": ["in.c-demo.subscriptions"],
                "blocked": [],
                "warnings": [{"code": "no_metrics_bound", "message": "…"}],
            }
        ]
    }
    with patch(
        "connectors.keboola.semantic_layer.compute_semantic_coverage",
        return_value=fake,
    ):
        r = c.get(
            "/api/admin/semantic-layer/coverage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    source = r.json()["sources"][0]
    assert source["metrics"] == {"upstream": 50, "importable": 0}
    assert source["unregistered_tables"] == ["in.c-demo.subscriptions"]
    assert [w["code"] for w in source["warnings"]] == ["no_metrics_bound"]


def test_coverage_endpoint_requires_admin(seeded_app):
    """Coverage names upstream project ids and dataset paths — admin-only, like
    every other surface over a connection's configuration."""
    c = seeded_app["client"]
    r = c.get("/api/admin/semantic-layer/coverage")
    assert r.status_code in (401, 403), r.text


class TestBackgroundRefreshSingleFlight:
    """The login-provisioning tail shares the endpoint's single-flight guard;
    a concurrent second caller must SKIP, never queue a duplicate run — and
    the claim must clear even when the sync raises."""

    def test_second_concurrent_caller_skips(self, monkeypatch):
        import asyncio
        import threading

        import app.api.keboola_semantic_layer_refresh as kslr

        calls = []
        release = threading.Event()

        def fake_sync():
            calls.append(1)
            # Hold the run in flight until the test releases it. An
            # instantly-returning fake would NOT guarantee overlap: on
            # Python 3.13 the executor future can resolve before the
            # awaiting task ever yields, so two gather()ed callers run
            # strictly one after the other — and a second refresh AFTER a
            # completed one is correct behavior, not the duplicate this
            # test exists to catch.
            release.wait(timeout=10)
            return {"status": "ok"}

        monkeypatch.setattr(kslr, "sync_semantic_layer", fake_sync)

        async def run_overlapped():
            first = asyncio.ensure_future(kslr.run_semantic_layer_refresh_background(trigger="login-a"))
            try:
                for _ in range(1000):
                    if calls:
                        break
                    await asyncio.sleep(0.005)
                assert calls, "the first caller never entered the sync"
                # The overlapping caller must return immediately (skip) —
                # one that queued behind the in-flight run would still be
                # waiting when this times out.
                await asyncio.wait_for(kslr.run_semantic_layer_refresh_background(trigger="login-b"), timeout=5)
                assert not first.done()
            finally:
                release.set()
            await first

        asyncio.run(run_overlapped())
        assert len(calls) == 1

    def test_claim_clears_after_a_failed_run(self, monkeypatch):
        import asyncio

        import app.api.keboola_semantic_layer_refresh as kslr

        calls = []

        def flaky_sync():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream exploded")
            return {"status": "ok"}

        monkeypatch.setattr(kslr, "sync_semantic_layer", flaky_sync)
        asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="first"))
        asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="second"))
        assert len(calls) == 2
        assert kslr.KEBOOLA_SEMANTIC_REFRESH.busy is False

    def test_the_login_sync_skips_while_the_scheduled_sweep_holds_the_guard(self, monkeypatch):
        """The other half of the single flight: the generic sweep claims the
        SAME guard while importing a keboola-provenance source, so a login
        landing mid-sweep skips instead of racing a second upsert+prune pass
        over one connection's rows."""
        import app.api.keboola_semantic_layer_refresh as kslr

        calls = []
        monkeypatch.setattr(kslr, "sync_semantic_layer", lambda: calls.append(1) or {"status": "ok"})

        with kslr.KEBOOLA_SEMANTIC_REFRESH.try_claim("sweep:keboola_conn-a") as claimed:
            assert claimed
            asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="keboola-login"))

        assert calls == []
        assert kslr.KEBOOLA_SEMANTIC_REFRESH.busy is False
