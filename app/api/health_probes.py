"""LB probes: /healthz (liveness) and /readyz (readiness).

Readiness = background write-canary result with M-of-N hysteresis
(3 consecutive failures -> not ready, 2 consecutive successes -> ready)
plus any registered role-specific checks. The canary runs on a timer,
NOT per probe request — N replicas probing a slow DB must not amplify
load or flap together. /api/health is unchanged and stays the
compatibility alias. Spec §3.7.
"""

import asyncio
import contextlib
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["probes"])

_FAILS_TO_TRIP = 3
_OKS_TO_RECOVER = 2

# Sentinel (user_id, dataset) pair used to park the write-canary row in the
# existing `user_sync_settings` table via `sync_settings_repo()` — the
# smallest KV-shaped repo already routed through the backend factory
# (src/repositories/__init__.py). Chosen over other candidates because it
# needs no extra config (unlike `system_secrets_repo`, which raises
# VaultKeyNotConfiguredError when AGNES_VAULT_KEY is unset — a false
# readiness failure unrelated to DB health) and doesn't collide with or
# overwrite real operator-facing content (unlike the `instance_templates`
# rows backing claude_md/welcome/news_template). No real user can ever
# authenticate as this sentinel id, so the row never appears in a real
# user's settings.
_CANARY_USER_ID = "__system__"
_CANARY_DATASET = "__readiness_canary__"


class ReadinessState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = True
        self._consec_fail = 0
        self._consec_ok = 0
        self._last_canary_at: str | None = None

    def record_canary(self, ok: bool) -> None:
        with self._lock:
            self._last_canary_at = datetime.now(timezone.utc).isoformat()
            if ok:
                self._consec_ok += 1
                self._consec_fail = 0
                if not self._ready and self._consec_ok >= _OKS_TO_RECOVER:
                    self._ready = True
                    logger.info("readiness: recovered")
            else:
                self._consec_fail += 1
                self._consec_ok = 0
                if self._ready and self._consec_fail >= _FAILS_TO_TRIP:
                    self._ready = False
                    logger.error("readiness: tripped after %d canary failures", self._consec_fail)

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "canary_ready": self._ready,
                "consecutive_failures": self._consec_fail,
                "last_canary_at": self._last_canary_at,
            }


readiness = ReadinessState()
_extra_checks: dict[str, Callable[[], bool]] = {}


def register_readiness_check(name: str, fn: Callable[[], bool]) -> None:
    _extra_checks[name] = fn


def _write_canary() -> bool:
    try:
        # Reuse the existing user_sync_settings KV surface through the repo
        # factory so the write exercises whichever backend (DuckDB or
        # Postgres) is currently active — see module docstring above for
        # why this repo was picked over the other KV-shaped candidates.
        from src.repositories import sync_settings_repo

        sync_settings_repo().set_dataset_enabled(_CANARY_USER_ID, _CANARY_DATASET, True)
        return True
    except Exception:
        logger.exception("readiness write-canary failed")
        return False


_T = TypeVar("_T")

#: Total drain budget for the WHOLE shutdown, not per call.
#:
#: Deliberately much smaller than the worker runtime's
#: ``AGNES_WORKER_DRAIN_TIMEOUT_S`` (45s): that one waits on arbitrary job
#: handlers, this one waits on single jobs-table/canary statements. 10s
#: already means something is badly wrong with the DB. Keeping it small is
#: what makes the two independent budgets add up to less than the 60s
#: stop_grace_period a container typically gets between SIGTERM and SIGKILL.
_DEFAULT_DRAIN_TIMEOUT_S = 10.0

#: Monotonic instant the shared shutdown budget expires at. Armed by
#: :func:`begin_shutdown`, NOT by the first drain — see there.
_drain_deadline: float | None = None


def begin_shutdown() -> None:
    """Start the shared drain budget. Called once from the lifespan's shutdown.

    Arming this on the first drain instead would be wrong: the worker cancels
    a job's heartbeat task on every completed job
    (``app/worker/runtime.py``'s ``hb_task.cancel()``), which is a routine,
    non-shutdown cancellation that can land mid-DB-call. That would start the
    clock during normal operation and, one budget later, leave every real
    shutdown drain with zero time — silently disabling the protection while
    the process runs fine.

    Re-arms unconditionally: each shutdown gets its own budget. Arming only
    once would leave every application started after the first in the same
    process (every ``TestClient`` in a test run, any process that restarts
    the app) with a spent deadline and therefore no drain at all.

    Also fires a non-blocking interrupt at any long statement in flight on a
    cursor of a DuckDB state singleton — the rolling-snapshot ``EXPORT
    DATABASE`` (#1294) and, since it moved off the singleton lock onto a child
    cursor, the periodic ``CHECKPOINT`` (#2352). The checkpoint/rolling-snapshot
    loop is the first task the lifespan cancels, and its drain would otherwise
    block on a multi-second export or WAL flush — consuming the shared budget
    this function just armed and leaving the canary/worker drains with none.
    Neither interrupt costs anything: the snapshot is best-effort, and
    ``close_system_db()`` / ``close_operational_db()`` each re-run the
    CHECKPOINT on the parent connection. The deadline is armed FIRST so the
    refresh's own pre-EXPORT ``is_shutdown_started()`` gate closes the
    interrupt's one blind spot (landing between the export's ``CHECKPOINT`` and
    ``EXPORT DATABASE`` statements). Local import, like ``src.db``'s own
    function-local import of this module — the two modules reference each other
    only at call time.
    """
    global _drain_deadline
    _drain_deadline = time.monotonic() + _drain_timeout_s()

    try:
        from src.db import interrupt_inflight_singleton_statements

        interrupt_inflight_singleton_statements(caller="begin_shutdown")
    except Exception as exc:  # pragma: no cover - defensive; never blocks shutdown
        logger.debug("begin_shutdown: in-flight singleton-statement interrupt failed (%s)", exc)


def end_shutdown() -> None:
    """Clear the budget once shutdown is over.

    Without this a process that keeps running after an app shutdown (again:
    a test run) would carry a spent deadline into normal operation, where a
    routine mid-call cancellation would then get zero drain instead of the
    full timeout.
    """
    global _drain_deadline
    _drain_deadline = None


def is_shutdown_started() -> bool:
    """True once :func:`begin_shutdown` has armed the shared drain budget.

    Lets a periodic background job check, before kicking off new work,
    whether the lifespan has begun tearing down — see
    ``src.db.refresh_rolling_snapshot`` (#1294), which must not start a
    fresh multi-second ``EXPORT DATABASE`` once shutdown is under way: a
    drain abandoned by :func:`to_thread_drain_on_cancel` would otherwise
    still be executing when the lifespan reaches ``close_system_db()``.
    """
    return _drain_deadline is not None


def _drain_timeout_s() -> float:
    raw = os.environ.get("AGNES_DRAIN_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_DRAIN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = None
    # inf/nan parse fine but make the bound meaningless: inf restores the
    # unbounded wait this exists to prevent, and nan poisons every
    # comparison it feeds. Treat them as a misconfiguration, not a setting
    # (review finding on #1140).
    if value is None or not math.isfinite(value):
        logger.warning("readiness: invalid AGNES_DRAIN_TIMEOUT_S=%r, using default", raw)
        return _DEFAULT_DRAIN_TIMEOUT_S
    return max(value, 0.0)


def _drain_budget_s() -> float:
    """Seconds this drain may wait.

    During shutdown the budget is SHARED, not per call: the lifespan cancels
    the checkpoint loop, then the canary loop, then the worker loop
    *sequentially*, and the worker's ``_drain_in_flight`` waits on a
    heartbeat task per in-flight entry. Per-call bounds would stack and blow
    past the very stop_grace_period they exist to stay under, turning a
    graceful drain into the SIGKILL it was added to prevent. So every drain
    after :func:`begin_shutdown` draws from what remains of one budget.

    Outside shutdown there is nothing to share with and no
    ``close_system_db()`` waiting behind us — a routine heartbeat
    cancellation is an isolated event — so such a drain gets the full
    timeout and leaves the shutdown budget untouched.

    Known trade-off: sharing is first-come, so a slow drain early in the
    sequence (the CHECKPOINT is the one call here that can legitimately take
    seconds) can leave the later ones with 0.0 and send them back to
    abandoning their thread. Both alternatives are worse — per-call bounds
    stack past ``stop_grace_period``, and a per-call sub-bound just moves
    which call gets starved. Operators who see the timeout log during a
    slow-DB shutdown should raise ``AGNES_DRAIN_TIMEOUT_S`` (staying under
    the grace period their orchestrator gives the container).
    """
    if _drain_deadline is None:
        return _drain_timeout_s()
    return max(0.0, _drain_deadline - time.monotonic())


async def to_thread_drain_on_cancel(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run ``fn`` in a worker thread; on cancellation, wait for an in-flight
    call to finish before propagating.

    ``asyncio.to_thread`` cancellation delivers ``CancelledError`` to the
    awaiting coroutine immediately while the OS thread keeps running (see the
    graceful-shutdown notes in ``app/worker/runtime.py``). For the background
    loops using this helper, the awaiter's next step after cancellation is
    ``app/main.py``'s lifespan closing the DuckDB singletons — returning with
    a DB call still mid-flight lets ``close_system_db()`` race that call,
    which can wedge DuckDB inside ``conn.execute`` and then deadlock
    event-loop teardown on the default-executor join (observed as a 60s
    pytest-timeout inside ``TestClient.__exit__``). Draining is cheap here:
    these calls are single-row writes / CHECKPOINTs bounded by a normal
    statement, not arbitrary user work (the worker runtime keeps its own
    bounded-drain registry for that).

    The drain is nonetheless **bounded**, by a budget shared across the whole
    shutdown (``AGNES_DRAIN_TIMEOUT_S``, default
    ``_DEFAULT_DRAIN_TIMEOUT_S`` — see :func:`_drain_budget_s`). "Single
    statement" is an assumption, not a guarantee — lock contention or a
    partitioned Postgres can hang one indefinitely, and an unbounded wait
    would trade the abandoned-thread bug for a shutdown that never
    completes. Once the budget is spent we log and abandon, i.e. fall back
    to the pre-drain behavior rather than hanging.
    """
    future = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # fn's own failure, if any, no longer matters once we're cancelled.
        budget = _drain_budget_s()
        with contextlib.suppress(Exception):
            # shield again: wait_for cancels its argument on timeout, which
            # would re-orphan the very thread we are draining.
            await asyncio.wait_for(asyncio.shield(future), timeout=budget)
        if not future.done():
            logger.warning(
                "readiness: drain budget exhausted (%.1fs of %.0fs%s) waiting for %s; abandoning the thread",
                budget,
                _drain_timeout_s(),
                ", shared across shutdown" if _drain_deadline is not None else "",
                getattr(fn, "__name__", repr(fn)),
            )
        raise


async def canary_loop(interval_s: float = 30.0) -> None:
    while True:
        ok = await to_thread_drain_on_cancel(_write_canary)
        readiness.record_canary(ok)
        await asyncio.sleep(interval_s)


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "alive"}


@router.get("/readyz")
def readyz():
    failed = [name for name, fn in _extra_checks.items() if not _safe(fn)]
    ok = readiness.is_ready() and not failed
    body = {"status": "ready" if ok else "not_ready", "failed_checks": failed, **readiness.snapshot()}
    return JSONResponse(status_code=200 if ok else 503, content=body)


def _safe(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        logger.exception("readiness extra check crashed")
        return False
