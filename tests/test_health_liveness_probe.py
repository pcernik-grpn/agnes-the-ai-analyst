"""`GET /api/health` is the auth-free liveness probe hit by the LB, the
docker-compose healthcheck, and the on-VM watchdog (every few seconds /
minutes). It must:

1. Keep its body contract — `status` + `db_schema` + `current` — because the
   watchdog parses the schema number straight out of this body to emit the
   "DB: schema bump" info event (no extra DB access on the VM, #647), and the
   docker smoke test asserts `db_schema == "ok"`.

2. Never block the event loop on the DuckDB read. The handler is `async def`;
   doing the schema `SELECT` synchronously serializes every probe behind any
   in-flight orchestrator rebuild (which writes `sync_state` on the same
   system connection). Under that contention the probe times out and the
   watchdog fires a false `HEALTH: /api/health not returning 200`. The read
   is offloaded to a worker thread and memoized so repeated probes don't
   re-hit the DB (the schema only changes at startup migration).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import httpx

import app.api.health as health_mod


class _ConcurrencyPeak:
    """Highest number of callers inside :meth:`track` at the same moment.

    The direct form of what the two guards below actually assert — that the
    synchronous schema read runs OFF the event loop, so two probes are inside
    it at once. Wall-clock thresholds cannot say this reliably: the gap
    between overlap (one sleep + overhead) and serialization (two sleeps +
    overhead) is one sleep wide, while a loaded shard runner's ASGI/
    thread-pool overhead alone was measured at ~1.05s and varies run to run.
    That is what failed this guard twice at 1.54s/0.9s and again at
    2.55s/2.5s with `app/api/health.py` byte-identical to a passing `main`.
    Peak concurrency is 2 when off-loop and 1 when blocked, whatever the
    machine is doing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight = 0
        self.value = 0

    @contextlib.contextmanager
    def track(self):
        with self._lock:
            self._inflight += 1
            self.value = max(self.value, self._inflight)
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1


def _reset_cache() -> None:
    health_mod._schema_cache = None
    health_mod._schema_cache_at = 0.0


def test_health_body_contract(seeded_app):
    """Liveness body keeps the fields the watchdog + docker smoke depend on."""
    _reset_cache()
    r = seeded_app["client"].get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert body["db_schema"] == "ok", body
    assert "current" in body, body
    assert "vault_key_configured" in body, body


def test_schema_check_memoized(seeded_app, monkeypatch):
    """Repeated probes must not re-query the DB — the schema only changes at
    startup migration. Two sequential probes => one underlying read."""
    _reset_cache()
    calls = {"n": 0}
    orig = health_mod._check_db_schema

    def counting():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(health_mod, "_check_db_schema", counting)
    c = seeded_app["client"]
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").status_code == 200
    assert calls["n"] == 1, f"schema read not memoized: {calls['n']} DB hits"


def test_unreachable_result_not_cached(seeded_app, monkeypatch):
    """A transient `unreachable` (DB momentarily busy) must not get pinned in
    the cache — the next probe retries and recovers to `ok`."""
    _reset_cache()
    state = {"first": True}

    def flaky():
        if state["first"]:
            state["first"] = False
            return {"db_schema": "unreachable", "detail": "busy"}
        return {"db_schema": "ok", "current": 1, "expected": 1}

    monkeypatch.setattr(health_mod, "_check_db_schema", flaky)
    c = seeded_app["client"]
    r1 = c.get("/api/health")
    assert r1.json()["db_schema"] == "unreachable", r1.text
    r2 = c.get("/api/health")
    assert r2.json()["db_schema"] == "ok", r2.text


def test_health_does_not_block_event_loop(seeded_app, monkeypatch):
    """Two concurrent probes against a slow schema read must be inside it at
    the same moment, proving the synchronous DuckDB call runs off the event
    loop.

    Two things about how this is asserted, both of them prior defects:

    The slow stand-in replaces ``_cached_db_schema`` -- the memoized wrapper
    the endpoint actually awaits -- and NOT ``_check_db_schema`` behind it.
    Patching the inner function made this guard vacuous: with the loop
    blocked, the first probe slept, filled the cache, and the second
    returned from it for free, so healthy and regressed measured the SAME
    single sleep (1.53s either way; the inner read ran twice off-loop and
    once when blocked). It could not fail for the reason it exists.

    And the assertion is peak concurrency, not elapsed time. A wall-clock
    threshold has to fit between one sleep + overhead and two sleeps +
    overhead, and a loaded shard runner's overhead alone measured ~1.05s
    and varied: this guard failed at 1.54s against 0.9s, then at 2.55s
    against a widened 2.5s, both times with ``app/api/health.py``
    byte-identical to a passing ``main``. Peak concurrency is 2 off-loop
    and 1 when blocked, whatever the machine is doing."""
    _reset_cache()
    app = seeded_app["client"].app
    orig = health_mod._cached_db_schema
    peak = _ConcurrencyPeak()

    def slow():
        with peak.track():
            time.sleep(0.5)  # simulate rebuild-lock contention on the system conn
            return orig()

    monkeypatch.setattr(health_mod, "_cached_db_schema", slow)

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            t0 = time.monotonic()
            r1, r2 = await asyncio.gather(ac.get("/api/health"), ac.get("/api/health"))
            return time.monotonic() - t0, r1, r2

    elapsed, r1, r2 = asyncio.run(fire())
    assert r1.status_code == 200 and r2.status_code == 200
    assert peak.value == 2, (
        f"probes serialized (peak concurrency {peak.value}) — event loop blocked"
    )
    # Secondary, deliberately loose: catches a hang, never the runner's load.
    assert elapsed < 5.0, f"probes took {elapsed:.2f}s"


def test_detailed_schema_check_does_not_block_event_loop(seeded_app, monkeypatch):
    """`/api/health/detailed?include=schema` must not do its synchronous PG
    round-trip on the event loop. Two concurrent authenticated probes against a
    slow schema read must be inside it at the same moment, proving the read
    runs off the loop (via `asyncio.to_thread`). Asserted as peak
    concurrency rather than elapsed time, and patched at the memoized
    layer -- see :func:`test_health_does_not_block_event_loop` for why both
    of those matter."""
    _reset_cache()
    app = seeded_app["client"].app
    token = seeded_app["admin_token"]
    orig = health_mod._cached_db_schema
    peak = _ConcurrencyPeak()

    def slow():
        with peak.track():
            time.sleep(0.5)
            return orig()

    monkeypatch.setattr(health_mod, "_cached_db_schema", slow)

    async def fire():
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            t0 = time.monotonic()
            r1, r2 = await asyncio.gather(
                ac.get("/api/health/detailed?include=schema", headers=headers),
                ac.get("/api/health/detailed?include=schema", headers=headers),
            )
            return time.monotonic() - t0, r1, r2

    elapsed, r1, r2 = asyncio.run(fire())
    assert r1.status_code == 200 and r2.status_code == 200
    assert peak.value == 2, (
        f"detailed probes serialized (peak concurrency {peak.value}) — event loop blocked"
    )
    assert elapsed < 5.0, f"detailed probes took {elapsed:.2f}s"


def test_detailed_schema_check_shares_liveness_cache(seeded_app, monkeypatch):
    """`/api/health/detailed?include=schema` must reuse the same 30s cache as
    `/api/health`, not re-read the DB on every dashboard poll. Guards against a
    regression to `await asyncio.to_thread(_check_db_schema)` (off-loop but
    uncached), which would keep the event-loop test green while restoring a PG
    round-trip per poll."""
    _reset_cache()
    calls = {"n": 0}
    orig = health_mod._check_db_schema

    def counting():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(health_mod, "_check_db_schema", counting)
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Warm the shared cache via the liveness probe...
    assert c.get("/api/health").status_code == 200
    # ...then the detailed endpoint must hit that cache, not re-read the DB.
    r = c.get(
        "/api/health/detailed?include=schema",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["services"]["db_schema"]["db_schema"] == "ok", r.text
    assert calls["n"] == 1, f"detailed endpoint re-read the DB instead of reusing the 30s cache: {calls['n']} reads"


def test_detailed_check_does_not_pollute_liveness_cache(seeded_app):
    """`/api/health/detailed` stamps an `audience` label onto each check in
    place. Since the schema check now reuses the shared 30s cache object, the
    detailed handler must copy it — otherwise `audience` bleeds into the cached
    dict the unauthenticated `/api/health` probe spreads into its flat body,
    silently changing the liveness contract for up to 30s."""
    _reset_cache()
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # A clean liveness body never carries `audience`.
    assert "audience" not in c.get("/api/health").json()

    # Run the detailed check that tags every check with an audience...
    r = c.get(
        "/api/health/detailed?include=schema",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["services"]["db_schema"]["audience"] == "operator", r.text

    # ...the liveness body must STILL be clean (cached dict not mutated).
    body = c.get("/api/health").json()
    assert "audience" not in body, f"detailed check leaked `audience` into liveness body: {body}"
