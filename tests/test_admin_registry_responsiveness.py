"""A slow registry poll must not stall unrelated HTTP traffic (#2352)."""

import asyncio
import contextlib
import threading

import anyio
import httpx


def test_version_responds_while_registry_read_is_blocked(seeded_app, monkeypatch):
    from app.api import admin

    entered = threading.Event()
    release = threading.Event()
    watchdog_fired = threading.Event()
    repo = admin.table_registry_repo()
    original_list_all = repo.list_all

    def slow_list_all(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test controller did not release registry read"
        return original_list_all(*args, **kwargs)

    monkeypatch.setattr(repo, "list_all", slow_list_all)
    monkeypatch.setattr(admin, "table_registry_repo", lambda: repo)

    def watchdog():
        # A timeout on the ASGI event loop cannot rescue a blocked loop.
        # Release from an independent thread so the broken implementation
        # fails an assertion instead of hanging the test runner.
        if entered.wait(5) and not release.wait(3):
            watchdog_fired.set()
            release.set()

    controller = threading.Thread(target=watchdog)
    controller.start()

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 4
        transport = httpx.ASGITransport(app=seeded_app["client"].app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            registry = asyncio.create_task(
                client.get(
                    "/api/admin/registry",
                    headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5), "registry never reached the read"
                response = await client.get("/api/version")
                assert response.status_code == 200
                # More overlapping polls than pool slots must fail fast, leaving
                # room for real authentication dependencies and sync handlers.
                polls = await asyncio.gather(
                    *(
                        client.get(
                            "/api/admin/registry", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}
                        )
                        for _ in range(12)
                    )
                )
                assert all(p.status_code == 503 and p.headers["Retry-After"] == "3" for p in polls)
                authenticated = await client.get(
                    "/api/sync/status", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}
                )
                assert authenticated.status_code == 200
                assert not watchdog_fired.is_set(), "registry read blocked the HTTP event loop"
                assert not registry.done(), "version must respond before the registry read is released"
            finally:
                release.set()
                result = await registry
                limiter.total_tokens = original_tokens
            assert result.status_code == 200
            assert result.json()["packaged_read_ok"] is True
            # The admission lock must be released after the read finishes.
            again = await client.get(
                "/api/admin/registry", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}
            )
            assert again.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        controller.join(timeout=6)
        repo.conn.close()


def test_http_responds_during_queued_sync(seeded_app, monkeypatch):
    """Real trigger, DuckDB queue and worker; only extraction is held."""
    from app.api import health_probes, sync
    from app.worker import kinds, registry, runtime

    entered = threading.Event()
    release = threading.Event()

    def slow_sync(tables, source, *, result_sink):
        entered.set()
        assert release.wait(10), "HTTP requests did not finish during sync"
        result_sink["synced_tables"] = []
        return True

    monkeypatch.setattr(sync, "_run_sync", slow_sync)
    monkeypatch.setattr(sync, "_recent_trigger_at", 0.0)
    monkeypatch.setattr(kinds, "_maybe_enqueue_distribution_mirror", lambda: None)
    monkeypatch.setattr(health_probes, "_drain_deadline", None)
    monkeypatch.setenv("AGNES_WORKER_LANES", "heavy")
    job_kinds = {"data-refresh": registry.JobKind("data-refresh", kinds._run_data_refresh, registry.HEAVY_LANE)}
    for module in (registry, runtime, kinds):
        monkeypatch.setattr(module, "JOB_KINDS", job_kinds)

    async def exercise():
        transport = httpx.ASGITransport(app=seeded_app["client"].app)
        headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            triggered = await client.post("/api/sync/trigger", headers=headers)
            assert triggered.status_code == 200
            job_id = triggered.json()["job_id"]
            worker = asyncio.create_task(runtime.worker_loop(worker_id="responsiveness-test", poll_interval_s=0.02))
            try:
                assert await asyncio.to_thread(entered.wait, 5), "worker never started extraction"
                version = await client.get("/api/version")
                poll = await client.get("/api/admin/registry", headers=headers)
                duplicate = await client.post("/api/sync/trigger", headers=headers)
                state = await client.get(f"/api/jobs/{job_id}", headers=headers)
                assert version.status_code == poll.status_code == state.status_code == 200
                assert state.json()["job"]["status"] == "running"
                assert duplicate.status_code == 409
                assert duplicate.json()["detail"]["job_id"] == job_id
                release.set()
                async with asyncio.timeout(5):
                    while True:
                        state = await client.get(f"/api/jobs/{job_id}", headers=headers)
                        assert state.status_code == 200
                        if state.json()["job"]["status"] == "done":
                            break
                        await asyncio.sleep(0.02)
            finally:
                release.set()
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_registry_admission_recovers_after_read_failure(monkeypatch):
    from app.api import admin

    def fail():
        raise RuntimeError("read failed")

    monkeypatch.setattr(admin, "_read_registry", fail)
    import pytest

    with pytest.raises(RuntimeError, match="read failed"):
        admin.list_registry(user={}, conn=None)
    monkeypatch.setattr(admin, "_read_registry", lambda: {"tables": []})
    assert admin.list_registry(user={}, conn=None) == {"tables": []}
