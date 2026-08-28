"""Tests for the wave-2B worker runtime (``app/worker/registry.py`` +
``app/worker/runtime.py``).

Uses fake registered kinds against a fresh temp-DATA_DIR DuckDB backend —
no real job kinds are registered yet (that's a later task in the same
wave). Runs the real asyncio ``worker_loop`` for a short, bounded window
via ``asyncio.create_task`` + ``cancel()``, mirroring how ``app/main.py``
starts/stops the ``canary_loop`` background task.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time

import pytest

from app.api import health_probes


@pytest.fixture(autouse=True)
def _reset_drain_deadline():
    """Keep the process-global shutdown-drain budget out of these tests.

    Tests here cancel `worker_loop` while it polls, and a cancellation that
    lands mid-DB-call is exactly what the drain reacts to. Without this
    reset one test could leave an armed (or spent) deadline behind and make
    a later drain assertion depend on how long the rest of the file took.
    """
    health_probes._drain_deadline = None
    yield
    health_probes._drain_deadline = None


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, closed after the test.

    ``jobs_repo()`` (the repo factory the runtime module calls) resolves
    to the DuckDB backend here since neither DATABASE_URL nor
    AGNES_DB_URL is set — mirrors the ``system_db`` fixture pattern in
    ``tests/test_state_checkpoint.py``.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. the jobs table)
    yield
    close_system_db()


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


async def _run_and_cancel(coro, duration_s: float) -> None:
    """Start ``coro`` as a task, let it run for ``duration_s``, then
    cancel it and wait for a clean shutdown (mirrors the
    ``_canary_task.cancel()`` / ``await _canary_task`` pattern in
    ``app/main.py``'s lifespan)."""
    task = asyncio.create_task(coro)
    await asyncio.sleep(duration_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


async def _lane_task_names(coro, duration_s: float) -> set[str]:
    """Like ``_run_and_cancel``, but snapshots every OTHER live task's name
    right before cancelling — used to assert exactly which lane slots
    ``worker_loop`` spawned (``worker-heavy-N`` / ``worker-light-N`` /
    ``worker-extraction-N``, see ``worker_loop``'s task-naming)."""
    outer = asyncio.create_task(coro)
    await asyncio.sleep(duration_s)
    names = {t.get_name() for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    outer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await outer
    return names


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_register_kind_stores_by_name():
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind

    register_kind(JobKind(name="sanity", handler=lambda payload: None, lane=LIGHT_LANE))
    assert "sanity" in JOB_KINDS
    assert JOB_KINDS["sanity"].lane == LIGHT_LANE
    assert JOB_KINDS["sanity"].lease_seconds == 120  # dataclass default
    assert JOB_KINDS["sanity"].retry_in_seconds == 300  # dataclass default


def test_register_kind_rejects_unknown_lane():
    from app.worker.registry import JobKind, register_kind

    with pytest.raises(ValueError):
        register_kind(JobKind(name="bad", handler=lambda payload: None, lane="medium"))


def test_register_kind_accepts_extraction_lane():
    """The extraction lane (spec §7.5 / §16 step 7) is a valid ``JobKind``
    lane, same as heavy/light."""
    from app.worker.registry import EXTRACTION_LANE, JOB_KINDS, JobKind, register_kind

    register_kind(JobKind(name="extraction_sanity", handler=lambda payload: None, lane=EXTRACTION_LANE))
    assert JOB_KINDS["extraction_sanity"].lane == EXTRACTION_LANE


def test_kinds_for_lane_returns_extraction_kinds():
    from app.worker.registry import EXTRACTION_LANE, HEAVY_LANE, JobKind, register_kind
    from app.worker.runtime import _kinds_for_lane

    register_kind(JobKind(name="extraction_sanity", handler=lambda payload: None, lane=EXTRACTION_LANE))
    register_kind(JobKind(name="heavy_sanity", handler=lambda payload: None, lane=HEAVY_LANE))

    assert _kinds_for_lane(EXTRACTION_LANE) == ["extraction_sanity"]
    assert "extraction_sanity" not in _kinds_for_lane(HEAVY_LANE)


# ---------------------------------------------------------------------------
# selected_lanes() / AGNES_WORKER_LANES
# ---------------------------------------------------------------------------


class TestSelectedLanes:
    def test_unset_returns_heavy_and_light_only_not_extraction(self, monkeypatch):
        """Backward compatibility: unset is the exact set worker_loop always
        spawned before the extraction lane existed — extraction is opt-in."""
        monkeypatch.delenv("AGNES_WORKER_LANES", raising=False)
        from app.worker.registry import HEAVY_LANE, LIGHT_LANE
        from app.worker.runtime import selected_lanes

        assert selected_lanes() == (HEAVY_LANE, LIGHT_LANE)

    def test_empty_string_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", "  ")
        from app.worker.registry import HEAVY_LANE, LIGHT_LANE
        from app.worker.runtime import selected_lanes

        assert selected_lanes() == (HEAVY_LANE, LIGHT_LANE)

    def test_extraction_only(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")
        from app.worker.registry import EXTRACTION_LANE
        from app.worker.runtime import selected_lanes

        assert selected_lanes() == (EXTRACTION_LANE,)

    def test_heavy_and_light(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", "heavy,light")
        from app.worker.registry import HEAVY_LANE, LIGHT_LANE
        from app.worker.runtime import selected_lanes

        assert selected_lanes() == (HEAVY_LANE, LIGHT_LANE)

    def test_whitespace_and_duplicates_are_tolerated_order_preserved(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", " light , heavy ,light")
        from app.worker.registry import HEAVY_LANE, LIGHT_LANE
        from app.worker.runtime import selected_lanes

        assert selected_lanes() == (LIGHT_LANE, HEAVY_LANE)

    def test_unknown_token_raises_value_error_naming_valid_lanes(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", "medium")
        from app.worker.runtime import selected_lanes

        with pytest.raises(ValueError, match="medium"):
            selected_lanes()

    def test_unknown_token_error_lists_every_valid_lane(self, monkeypatch):
        monkeypatch.setenv("AGNES_WORKER_LANES", "medium")
        from app.worker.runtime import selected_lanes

        with pytest.raises(ValueError, match=r"heavy.*light.*extraction"):
            selected_lanes()


# ---------------------------------------------------------------------------
# worker_loop
# ---------------------------------------------------------------------------


def test_default_worker_id_is_hostname_colon_pid():
    from app.worker.runtime import default_worker_id

    assert default_worker_id() == f"{socket.gethostname()}:{os.getpid()}"


def test_worker_loop_default_spawns_heavy_and_light_but_not_extraction(worker_db, monkeypatch):
    """Backward compatibility: unset ``AGNES_WORKER_LANES`` must reproduce
    the pre-extraction-lane spawn set exactly — one heavy slot, two light
    slots, no extraction slot."""
    monkeypatch.delenv("AGNES_WORKER_LANES", raising=False)
    from app.worker.runtime import worker_loop

    names = asyncio.run(_lane_task_names(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.2))

    assert sum(n.startswith("worker-heavy-") for n in names) == 1
    assert sum(n.startswith("worker-light-") for n in names) == 2
    assert sum(n.startswith("worker-extraction-") for n in names) == 0


def test_worker_loop_spawns_extraction_only_lane_selected(worker_db, monkeypatch):
    monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")
    from app.worker.runtime import worker_loop

    names = asyncio.run(_lane_task_names(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.2))

    assert sum(n.startswith("worker-extraction-") for n in names) == 1
    assert sum(n.startswith("worker-heavy-") for n in names) == 0
    assert sum(n.startswith("worker-light-") for n in names) == 0


def test_worker_loop_spawns_all_three_lanes_when_selected(worker_db, monkeypatch):
    monkeypatch.setenv("AGNES_WORKER_LANES", "heavy,light,extraction")
    from app.worker.runtime import worker_loop

    names = asyncio.run(_lane_task_names(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.2))

    assert sum(n.startswith("worker-heavy-") for n in names) == 1
    assert sum(n.startswith("worker-light-") for n in names) == 2
    assert sum(n.startswith("worker-extraction-") for n in names) == 1


def test_worker_loop_invalid_lane_raises_before_spawning_anything(worker_db, monkeypatch):
    monkeypatch.setenv("AGNES_WORKER_LANES", "bogus")
    from app.worker.runtime import worker_loop

    async def _run():
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        with pytest.raises(ValueError):
            await task

    asyncio.run(_run())


def test_heavy_lane_serializes_while_light_lane_proceeds_concurrently(worker_db):
    """Two queued heavy jobs must never run at the same time (concurrency
    1), but a queued light job must be able to run WHILE a heavy job is
    still in flight (separate lane, separate slot) — not merely after
    both heavy jobs finish."""
    from app.worker.registry import HEAVY_LANE, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    lock = threading.Lock()
    heavy_state = {"n": 0, "peak": 0}
    heavy_intervals: list[tuple[float, float]] = []
    light_started_at: list[float] = []

    def heavy_handler(payload: dict) -> None:
        start = time.monotonic()
        with lock:
            heavy_state["n"] += 1
            heavy_state["peak"] = max(heavy_state["peak"], heavy_state["n"])
        time.sleep(0.4)
        with lock:
            heavy_state["n"] -= 1
        heavy_intervals.append((start, time.monotonic()))

    def light_handler(payload: dict) -> None:
        light_started_at.append(time.monotonic())

    register_kind(JobKind(name="heavy_test", handler=heavy_handler, lane=HEAVY_LANE, lease_seconds=30))
    register_kind(JobKind(name="light_test", handler=light_handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    repo.enqueue("heavy_test", {})
    repo.enqueue("heavy_test", {})
    repo.enqueue("light_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 1.3))

    assert heavy_state["peak"] == 1, "heavy lane ran more than one job concurrently"
    assert len(heavy_intervals) == 2, f"expected both heavy jobs to complete, got {heavy_intervals}"
    assert light_started_at, "light lane never ran"
    light_t = light_started_at[0]
    heavy_all_done_at = max(end for _, end in heavy_intervals)
    assert light_t < heavy_all_done_at, (
        f"light job (t={light_t}) ran only after ALL heavy work finished (t={heavy_all_done_at}) "
        f"— light lane appears to be serialized behind the heavy lane instead of running concurrently "
        f"(heavy intervals: {heavy_intervals})"
    )

    done_heavy = repo.list(kind="heavy_test", status="done")
    assert len(done_heavy) == 2


def test_handler_exception_fails_job_with_retry(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise RuntimeError("boom")

    register_kind(
        JobKind(name="boom_test", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30, retry_in_seconds=60)
    )

    repo = jobs_repo()
    job = repo.enqueue("boom_test", {}, max_attempts=5)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    row = repo.get(job["id"])
    assert row["status"] == "queued", "a failed job with attempts remaining and retry_in_seconds must be requeued"
    assert row["error"] == "boom"
    assert row["run_after"] is not None
    assert row["attempts"] == 1
    assert row["leased_by"] is None


def test_handler_exception_at_max_attempts_finalizes_failed(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise ValueError("nope")

    register_kind(JobKind(name="boom_once", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    job = repo.enqueue("boom_once", {}, max_attempts=1)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert row["error"] == "nope"
    assert row["finished_at"] is not None


def test_graceful_cancel_mid_poll_returns_promptly(worker_db):
    """No kinds registered => every lane sits idle in its poll sleep.
    Cancelling must not wait out the (long) poll interval."""
    from app.worker.runtime import worker_loop

    async def _timed_drive() -> float:
        start = time.monotonic()
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=5.0))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        return time.monotonic() - start

    elapsed = asyncio.run(_timed_drive())
    assert elapsed < 1.0, f"cancel took {elapsed:.2f}s to take effect against a 5s poll interval"


def test_lane_slot_survives_transient_claim_next_failure(worker_db, monkeypatch):
    """A single blip in claim_next() (e.g. a DB hiccup) must not
    permanently kill the lane slot — it should log and retry after the
    next poll tick, same hardening convention as canary_loop /
    _state_checkpoint_loop in app/main.py."""
    import app.worker.runtime as runtime_mod
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    real_repo = jobs_repo()
    calls = {"n": 0}

    class FlakyRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient db hiccup")
            return real_repo.claim_next(**kwargs)

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: FlakyRepo())

    ran = {"done": False}

    def handler(payload: dict) -> None:
        ran["done"] = True

    register_kind(JobKind(name="flaky_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    job = real_repo.enqueue("flaky_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.5))

    assert calls["n"] >= 2, "expected at least one failed claim_next() attempt followed by a retry"
    assert ran["done"] is True, "lane slot never recovered from the transient failure"
    assert real_repo.get(job["id"])["status"] == "done"


def test_unregistered_kind_is_terminally_failed_without_retry(worker_db, monkeypatch):
    """Registry-drift guard (carry-over finding): a claimed job whose kind
    isn't (or is no longer) registered on this process must be failed
    terminally with no retry, not re-claimed forever.

    Under the current registry design a job can only be claimed for a
    kind that's registered (kinds passed to ``claim_next`` come straight
    from ``JOB_KINDS``), so the natural trigger is a race: the kind is
    deregistered on this process *between* the claim and the
    ``JOB_KINDS.get(job["kind"])`` lookup (e.g. a concurrent registry
    update). Simulated here via a repo proxy that pops the kind from
    ``JOB_KINDS`` right after ``claim_next`` returns it.
    """
    import app.worker.runtime as runtime_mod
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def handler(payload: dict) -> None:
        raise AssertionError("handler must never run — the guard fires before dispatch")

    register_kind(JobKind(name="ephemeral_kind", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    real_repo = jobs_repo()
    job = real_repo.enqueue("ephemeral_kind", {}, max_attempts=5)

    class DriftingRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            claimed = real_repo.claim_next(**kwargs)
            if claimed is not None:
                # Simulate registry drift between claim and dispatch.
                JOB_KINDS.pop("ephemeral_kind", None)
            return claimed

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: DriftingRepo())

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    row = real_repo.get(job["id"])
    assert row["status"] == "failed"
    assert "no registered handler" in row["error"]
    assert row["run_after"] is None, "must be finalized, not requeued, despite attempts remaining"


def test_shutdown_drain_waits_for_in_flight_handler_and_finalizes(worker_db, monkeypatch):
    """Critical: cancelling ``worker_loop`` must not orphan an in-flight
    handler's OS thread against a DB the caller is about to close.
    ``asyncio.to_thread``'s cancellation delivers ``CancelledError``
    immediately while the underlying thread keeps running — the loop must
    perform a bounded drain (wait for the handler, then finalize normally)
    instead of abandoning it the instant cancellation is requested."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "5")

    started = threading.Event()

    def slow_handler(payload: dict) -> None:
        started.set()
        time.sleep(0.4)

    register_kind(JobKind(name="slow_test", handler=slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("slow_test", {})

    async def _drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(_drive())

    row = repo.get(job["id"])
    assert row["status"] == "done", "in-flight handler must be finalized (not abandoned) by the shutdown drain"
    assert row["finished_at"] is not None


def test_shutdown_drain_notifies_webhooks_for_agent_response(worker_db, monkeypatch):
    """A job handed off to the shutdown drain (see
    ``test_shutdown_drain_waits_for_in_flight_handler_and_finalizes``) still
    reaches a terminal state via ``_drain_in_flight``, not ``_run_one`` —
    the webhook notify must fire from THAT path too (`_InFlightJob` carries
    no ``payload_json``, so ``_drain_in_flight`` re-fetches the row before
    delegating; see ``_notify_in_flight_agent_response``)."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "5")
    calls = _patch_webhook_notify(monkeypatch)

    started = threading.Event()

    def slow_handler(payload: dict) -> dict:
        started.set()
        time.sleep(0.4)
        return {"answer": "hi"}

    register_kind(JobKind(name="agent_response", handler=slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("agent_response", {"agent_id": "agent-1"})

    async def _drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert repo.get(job["id"])["status"] == "done"
    assert calls == [("agent-1", job["id"], "completed")]


def test_shutdown_drain_times_out_and_abandons_slow_handler(worker_db, monkeypatch):
    """If the in-flight handler doesn't finish within
    ``AGNES_WORKER_DRAIN_TIMEOUT_S``, the drain gives up (logs, doesn't
    wait forever) and leaves the job 'running' for lease-expiry recovery
    (reclaim or ``reap_exhausted``), rather than blocking shutdown
    indefinitely."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "0.2")

    started = threading.Event()
    release = threading.Event()

    def very_slow_handler(payload: dict) -> None:
        started.set()
        # Held open past the drain timeout; released once the test has
        # observed the timeout behavior so the (non-daemon) thread doesn't
        # block interpreter shutdown.
        release.wait(timeout=5)

    register_kind(JobKind(name="very_slow_test", handler=very_slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("very_slow_test", {})

    async def _drive() -> float:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        start = time.monotonic()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        elapsed = time.monotonic() - start
        # Release the abandoned handler thread while the loop is still
        # running so its (harmless) completion callback has somewhere to
        # land, then give the loop one more tick to process it.
        release.set()
        await asyncio.sleep(0.05)
        return elapsed

    elapsed = asyncio.run(_drive())

    assert elapsed < 1.5, f"drain should give up around the 0.2s configured timeout, took {elapsed:.2f}s"
    row = repo.get(job["id"])
    assert row["status"] == "running", "abandoned in-flight job must NOT be finalized by a timed-out drain"


def test_shutdown_drain_timeout_cancels_abandoned_heartbeat(worker_db, monkeypatch):
    """An abandoned (drain-timed-out) job's heartbeat task must be
    cancelled (not left running) before worker_loop returns — otherwise
    it keeps extending the lease of a job the loop no longer owns, after
    the worker process has already told the caller shutdown is done."""
    from app.worker import runtime
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "0.2")

    started = threading.Event()
    release = threading.Event()

    def very_slow_handler(payload: dict) -> None:
        started.set()
        # Held open past the drain timeout; released once the test has
        # observed the timeout behavior so the (non-daemon) thread doesn't
        # block interpreter shutdown.
        release.wait(timeout=5)

    register_kind(JobKind(name="very_slow_hb_test", handler=very_slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("very_slow_hb_test", {})

    captured: dict[str, object] = {}
    real_drain = runtime._drain_in_flight

    async def _capturing_drain(in_flight, worker_id, **kwargs):
        # Snapshot the in-flight entries (there's exactly one) before the
        # real drain runs, then let it do its normal timeout/cancel work.
        # **kwargs passes through worker_loop's shared shutdown budget.
        captured["entries"] = dict(in_flight)
        await real_drain(in_flight, worker_id, **kwargs)

    monkeypatch.setattr(runtime, "_drain_in_flight", _capturing_drain)

    async def _drive() -> None:
        task = asyncio.create_task(runtime.worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(_drive())

    entries = captured.get("entries")
    assert entries, "drain should have seen the in-flight job"
    entry = entries[job["id"]]
    assert entry.hb_task.cancelled() or entry.hb_task.done(), (
        "abandoned entry's heartbeat task must be cancelled/done once worker_loop returns, "
        "or it keeps extending a lease the loop no longer owns"
    )

    row = repo.get(job["id"])
    assert row["status"] == "running", "abandoned in-flight job must NOT be finalized by a timed-out drain"


def test_shutdown_drain_finalization_error_does_not_escape_worker_loop(worker_db, monkeypatch):
    """A DB exception raised by complete()/fail() while finalizing an
    in-flight job during the shutdown drain must be logged and swallowed,
    not propagated — otherwise it would abort the rest of the caller's
    lifespan shutdown (including closing the system DB) with the drain
    only partway through finalizing the other in-flight jobs."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo
    from src.repositories.jobs import JobsRepository

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "5")

    started = threading.Event()
    finish = threading.Event()

    def quick_handler(payload: dict) -> None:
        started.set()
        finish.wait(timeout=5)

    register_kind(JobKind(name="finalize_boom_test", handler=quick_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("finalize_boom_test", {})

    def boom_complete(self, job_id, worker_id, lease_token) -> None:
        raise RuntimeError("simulated DB failure during drain finalization")

    monkeypatch.setattr(JobsRepository, "complete", boom_complete)

    async def _drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        # Let the handler finish while the drain is waiting on it, so it
        # lands in `_done` and the drain takes the complete() path.
        finish.set()
        # If the fix regresses, the complete() RuntimeError raised inside
        # the drain's `finally` block replaces the CancelledError as what
        # propagates out of worker_loop, so this suppress would NOT catch
        # it and asyncio.run(_drive()) below would fail with the
        # RuntimeError instead of completing cleanly.
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        assert task.cancelled(), "worker_loop must finish via its own cancellation, not an escaped finalization error"

    asyncio.run(_drive())

    row = repo.get(job["id"])
    assert row["status"] == "running", "job whose complete() raised must not end up falsely marked done"


def test_worker_loop_reaps_stuck_jobs(worker_db):
    """The reap sweep is wired into worker_loop itself, independent of
    whether any handler is registered for the stuck job's kind."""
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    repo = jobs_repo()
    job = repo.enqueue("nobody_handles_this", {}, max_attempts=1)
    claimed = repo.claim_next(kinds=["nobody_handles_this"], worker_id="dead-worker", lease_seconds=-5)
    assert claimed is not None
    assert repo.get(job["id"])["status"] == "running"

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert row["error"] == "lease expired after max attempts"


# ---------------------------------------------------------------------------
# observability (three-plane wave 2D, task 2): job-queue + worker metrics
# ---------------------------------------------------------------------------


def _metric_value(name, **labels):
    from app.observability.metrics import REGISTRY

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


def test_worker_loop_records_claims_total(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    register_kind(JobKind(name="metrics_claim_e2e", handler=lambda payload: None, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    repo.enqueue("metrics_claim_e2e", {})

    before = _metric_value("agnes_job_claims_total", kind="metrics_claim_e2e") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    after = _metric_value("agnes_job_claims_total", kind="metrics_claim_e2e")
    assert after == before + 1.0


def test_worker_loop_records_duration_and_done_outcome(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    register_kind(JobKind(name="metrics_duration_ok", handler=lambda payload: None, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("metrics_duration_ok", {})

    before_count = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_ok", outcome="done") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert repo.get(job["id"])["status"] == "done"
    after_count = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_ok", outcome="done")
    assert after_count == before_count + 1.0


def test_worker_loop_records_duration_and_failed_outcome(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise RuntimeError("metrics boom")

    register_kind(JobKind(name="metrics_duration_fail", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    repo.enqueue("metrics_duration_fail", {}, max_attempts=1)

    before_duration = (
        _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_fail", outcome="failed") or 0.0
    )
    before_failures = (
        _metric_value("agnes_job_failures_total", kind="metrics_duration_fail", reason="RuntimeError") or 0.0
    )

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    after_duration = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_fail", outcome="failed")
    after_failures = _metric_value("agnes_job_failures_total", kind="metrics_duration_fail", reason="RuntimeError")
    assert after_duration == before_duration + 1.0
    assert after_failures == before_failures + 1.0


# ---------------------------------------------------------------------------
# agent_response webhook notification (V1b Task 6)
# ---------------------------------------------------------------------------


def _patch_webhook_notify(monkeypatch):
    """Records `(agent_id, job_id, status)` calls instead of touching the
    real `app.chat.webhook_delivery` module (that module's own behavior —
    SSRF guard, HMAC signing, fan-out — is covered by
    `tests/test_webhook_delivery.py` / `tests/test_agent_webhooks_api.py`;
    these tests cover only whether `app/worker/runtime.py` calls it at the
    right time, for the right kind, with the right status)."""
    calls: list[tuple[str, str, str]] = []

    def fake_enqueue(*, agent_id, job_id, status):
        calls.append((agent_id, job_id, status))

    import app.chat.webhook_delivery as webhook_delivery

    monkeypatch.setattr(webhook_delivery, "enqueue_job_event_webhooks", fake_enqueue)
    return calls


def test_agent_response_completion_notifies_webhooks(worker_db, monkeypatch):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="agent_response", handler=lambda payload: {"answer": "hi"}, lane=LIGHT_LANE))
    job = jobs_repo().enqueue("agent_response", {"agent_id": "agent-1"})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert calls == [("agent-1", job["id"], "completed")]


def test_agent_response_terminal_failure_notifies_webhooks(worker_db, monkeypatch):
    """`agent_response` never retries (`retry_in_seconds=None` in the real
    registration, mirrored here) — `.fail()` finalizes straight to
    `'failed'`, so the notify must fire."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom(payload: dict) -> None:
        raise RuntimeError("turn failed")

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="agent_response", handler=boom, lane=LIGHT_LANE, retry_in_seconds=None))
    job = jobs_repo().enqueue("agent_response", {"agent_id": "agent-1"}, max_attempts=5)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert jobs_repo().get(job["id"])["status"] == "failed"
    assert calls == [("agent-1", job["id"], "failed")]


def test_agent_response_requeued_failure_does_not_notify_yet(worker_db, monkeypatch):
    """A failure with `retry_in_seconds` set requeues instead of
    finalizing — must NOT fire a premature `job.failed` webhook for a job
    that may still succeed on a later attempt."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom(payload: dict) -> None:
        raise RuntimeError("transient")

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="agent_response", handler=boom, lane=LIGHT_LANE, retry_in_seconds=300))
    jobs_repo().enqueue("agent_response", {"agent_id": "agent-1"}, max_attempts=5)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert calls == []


def test_other_job_kinds_do_not_notify_webhooks(worker_db, monkeypatch):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="unrelated_kind", handler=lambda payload: None, lane=LIGHT_LANE))
    jobs_repo().enqueue("unrelated_kind", {"agent_id": "agent-1"})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert calls == []


def test_agent_response_missing_agent_id_does_not_notify(worker_db, monkeypatch):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="agent_response", handler=lambda payload: {"answer": "hi"}, lane=LIGHT_LANE))
    jobs_repo().enqueue("agent_response", {})  # no agent_id in payload

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert calls == []


def test_stale_lease_complete_does_not_notify_webhooks(worker_db, monkeypatch):
    """MEDIUM 1: `complete()`/`fail()` are documented raise-free NO-OPS
    once the lease token no longer matches (the job was reclaimed by
    another worker/slot while this handler was still running) — the
    webhook notify must be gated on the finalize call actually having
    mutated the row, not fired unconditionally just because the handler
    itself ran to completion under a now-stale claim."""
    import app.worker.runtime as runtime_mod
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    real_repo = jobs_repo()
    calls = _patch_webhook_notify(monkeypatch)

    class StaleOnCompleteRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def complete(self, job_id, worker_id, lease_token, result=None):
            # Simulate a reclaim landing between the handler finishing and
            # this finalize call: another (fake) claimant now owns the
            # row under a different lease_token, so this call's token is
            # stale — exactly the no-op path `complete()`'s docstring
            # describes.
            real_repo.conn.execute(
                "UPDATE jobs SET lease_token = 'someone-elses-token' WHERE id = ?",
                [job_id],
            )
            return real_repo.complete(job_id, worker_id, lease_token, result)

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: StaleOnCompleteRepo())

    register_kind(JobKind(name="agent_response", handler=lambda payload: {"answer": "hi"}, lane=LIGHT_LANE))
    job = real_repo.enqueue("agent_response", {"agent_id": "agent-1"})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    row = real_repo.get(job["id"])
    assert row["status"] == "running", "stale complete() call must not have flipped the row to done"
    assert calls == [], "no webhook should fire for a complete() call that was a stale no-op"


def test_reap_exhausted_notifies_failed_webhook(worker_db, monkeypatch):
    """MEDIUM 2a: a job whose worker crashed on its LAST attempt is
    finalized to 'failed' by the reap sweep (`reap_exhausted()`), not by a
    live `fail()` call — the terminal `job.failed` webhook must still
    fire from that path, or a receiver waits forever after a worker
    crash."""
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    calls = _patch_webhook_notify(monkeypatch)
    repo = jobs_repo()
    job = repo.enqueue("agent_response", {"agent_id": "agent-1"}, max_attempts=1)
    claimed = repo.claim_next(kinds=["agent_response"], worker_id="dead-worker", lease_seconds=-5)
    assert claimed is not None

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert calls == [("agent-1", job["id"], "failed")]


def test_agent_response_fail_notifies_despite_retry_config_when_attempts_exhausted(worker_db, monkeypatch):
    """MEDIUM 2b: `fail()` finalizes to 'failed' whenever attempts are
    exhausted, REGARDLESS of the kind's static `retry_in_seconds` config —
    gating the notify on `kind.retry_in_seconds is None` (the old code)
    would wrongly skip it here, since this kind IS configured to retry."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom(payload: dict) -> None:
        raise RuntimeError("boom")

    calls = _patch_webhook_notify(monkeypatch)
    register_kind(JobKind(name="agent_response", handler=boom, lane=LIGHT_LANE, retry_in_seconds=300))
    job = jobs_repo().enqueue("agent_response", {"agent_id": "agent-1"}, max_attempts=1)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert jobs_repo().get(job["id"])["status"] == "failed"
    assert calls == [("agent-1", job["id"], "failed")]


def test_worker_loop_lane_active_reflects_busy_heavy_lane(worker_db):
    """agnes_worker_lane_active{lane="heavy"} must go up while a heavy job's
    handler is actually running, and come back down once it finishes."""
    from app.worker.registry import HEAVY_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    started = threading.Event()
    release = threading.Event()

    def slow_heavy_handler(payload: dict) -> None:
        started.set()
        release.wait(timeout=5)

    register_kind(JobKind(name="metrics_lane_active", handler=slow_heavy_handler, lane=HEAVY_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("metrics_lane_active", {})

    baseline = _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) or 0.0
    baseline_running = _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) or 0.0

    async def _drive():
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"

        # While the handler is blocked mid-flight, the lane must show busy.
        assert _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) == baseline + 1.0
        assert (
            _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) == baseline_running + 1.0
        )

        release.set()
        # Give the handler + finalization a moment to complete, then cancel.
        deadline = time.monotonic() + 2.0
        while repo.get(job["id"])["status"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert repo.get(job["id"])["status"] == "done"
    assert _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) == baseline
    assert _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) == baseline_running


def test_worker_loop_no_handler_records_failure_metric(worker_db, monkeypatch):
    """The registry-drift ("no registered handler") branch finalizes the
    job without ever running a handler — it must still bump
    agnes_job_failures_total instead of being invisible to observability.

    Mirrors ``test_unregistered_kind_is_terminally_failed_without_retry``'s
    registry-drift simulation: a job can only be claimed for a kind
    currently in ``JOB_KINDS`` (``_kinds_for_lane`` filters on it), so an
    entirely-unregistered kind is never claimed at all. The drift has to
    happen *between* claim and dispatch, via a repo proxy that pops the
    kind right after ``claim_next`` returns it.
    """
    import app.worker.runtime as runtime_mod
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def handler(payload: dict) -> None:
        raise AssertionError("handler must never run — the guard fires before dispatch")

    register_kind(JobKind(name="metrics_drift_kind", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    real_repo = jobs_repo()
    real_repo.enqueue("metrics_drift_kind", {}, max_attempts=5)

    class DriftingRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            claimed = real_repo.claim_next(**kwargs)
            if claimed is not None:
                JOB_KINDS.pop("metrics_drift_kind", None)
            return claimed

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: DriftingRepo())

    before = _metric_value("agnes_job_failures_total", kind="other", reason="no-registered-handler") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    after = _metric_value("agnes_job_failures_total", kind="other", reason="no-registered-handler")
    assert after == before + 1.0


def test_cancelled_worker_poll_waits_for_inflight_claim(worker_db, monkeypatch):
    """Regression: cancelling ``worker_loop`` must drain an in-flight
    poll-path DB call (``claim_next``) instead of abandoning its thread.

    Same abandoned-thread-vs-``close_system_db()`` race as ``canary_loop``'s
    (see tests/test_health_probes.py): the bounded shutdown drain covers
    handler futures, but the lane slots' own ``claim_next``/``reap_exhausted``
    threads used to be orphaned mid-statement on cancellation, letting the
    lifespan close the DuckDB singleton under an active write.
    """
    import app.worker.runtime as runtime_mod
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop

    register_kind(JobKind(name="poll_drain_kind", handler=lambda payload: None, lane=LIGHT_LANE))

    claim_started = threading.Event()
    release_claim = threading.Event()
    claim_finished = threading.Event()

    class BlockingRepo:
        def claim_next(self, **kwargs):
            claim_started.set()
            release_claim.wait(timeout=10)
            claim_finished.set()
            return None

        def reap_exhausted(self, *a, **k):
            return 0

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: BlockingRepo())

    async def drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        assert await asyncio.to_thread(claim_started.wait, 5), "claim_next never started"
        task.cancel()
        asyncio.get_running_loop().call_later(0.2, release_claim.set)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        finished_before_return = claim_finished.is_set()
        release_claim.set()  # never leave the thread blocked, whatever the outcome
        assert finished_before_return, (
            "cancelled worker_loop returned while a claim_next thread was still running "
            "(lifespan would proceed to close the DB under the in-flight statement)"
        )

    asyncio.run(drive())
