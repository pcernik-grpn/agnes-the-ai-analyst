"""Worker runtime loop: claims jobs off the ``jobs`` queue and runs their
registered handlers (spec §3.3 / plan wave-2B Task 3).

Up to three independent lanes share one asyncio loop:

- **heavy** — concurrency 1 (one slot/task)
- **light** — concurrency 2 (two slots/tasks)
- **extraction** — concurrency **configurable**, default 1 (see
  :func:`_extraction_concurrency`; spec §7.5 / §16 step 7 of
  docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md —
  document extraction gets its OWN lane rather than sharing HEAVY, because a
  corpus re-extraction sitting in HEAVY's concurrency-1 slot would block
  every table sync for its whole duration)

Stage 1 of Agnes-owned extraction parallelism (the design note in the PR
that added this): before this, the ONLY parallelism in a `corpus-extraction`
run lived inside the external producer's own `--workers` flag — this lane
was hardcoded to one slot, so multiple SharePoint connections extracted
strictly one-at-a-time even though each is an independent producer
subprocess with no shared state. `extraction.concurrency` /
`AGNES_EXTRACTION_CONCURRENCY` (see :func:`_extraction_concurrency`) lets
several `corpus-extraction` jobs for DIFFERENT connections run at once —
the per-connection idempotency key (`app.api.admin_sharepoint
._extraction_idempotency_key`) still prevents two jobs for the SAME
connection from ever coexisting, unaffected by this change (see the
same-connection-dedup regression test in `tests/test_worker_runtime.py`).
Sharding ONE connection into per-scope jobs is explicitly NOT this change —
see the design note in the PR body that introduced `extraction.concurrency`.

**Which lanes THIS process spawns slots for** is controlled by the
``AGNES_WORKER_LANES`` env var (comma-separated lane names — see
:func:`selected_lanes`). Unset (the default) spawns HEAVY + LIGHT only —
exactly what every process spawned before the extraction lane existed —
so no existing single-process/all-in-one deployment is affected by its
addition: it neither polls for ``corpus-extraction`` nor pays for a third
idle lane slot unless asked to. A deployment that wants extraction running
(after enabling ``extraction.enabled`` in ``instance.yaml``) either adds
``extraction`` to this process's own ``AGNES_WORKER_LANES`` list, or runs
the dedicated ``extraction-worker`` compose service (which sets
``AGNES_WORKER_LANES=extraction``, isolating it onto its own
process/container) — see that service in ``docker-compose.yml`` and the
``worker`` Dockerfile build target.

Each lane slot repeats: ``claim_next(kinds=<lane's registered kinds>)`` ->
if nothing eligible, sleep ``poll_interval_s`` and retry -> otherwise run
the kind's handler via ``asyncio.to_thread`` while a heartbeat task
extends the lease every ``lease_seconds/3`` -> ``complete()``/``fail()``.

A third, independent task sweeps ``reap_exhausted()`` once per
``poll_interval_s`` tick — this is the stuck-job reaper: a 'running' job
whose lease expired on its LAST attempt is not eligible for
``claim_next()``'s crash-recovery reclaim (which requires
``attempts < max_attempts``), so without an active sweep it would stay
'running' forever. Kept as one task independent of lane activity so it
converges every lane's stuck jobs from a single cadence rather than
racing N lane slots into duplicate sweeps.

Same-worker double-execution guard (lease_token): all lane slots inside
one worker *process* share the same ``worker_id`` (hostname:pid — see
``default_worker_id``). After a stale slot's lease expires, ANOTHER slot
of the SAME process can reclaim the job under the *identical*
``worker_id``. ``JobsRepository``/``JobsPgRepository`` therefore guard
``heartbeat()``/``complete()``/``fail()`` on a fresh-per-claim
``lease_token`` (uuid4, minted by ``claim_next()``) rather than
``worker_id`` — a ``worker_id``-only guard cannot tell the two slots
apart, so the stale slot's late ``heartbeat()``/``complete()``/``fail()``
call would flip (or requeue) the live claim out from under the new slot.
This module threads the claimed row's ``lease_token`` through every
subsequent call for that job (see ``_run_one`` / ``_heartbeat_loop``).

Heartbeat-lost handling: if ``heartbeat()`` ever returns ``False`` (the
job's lease was reclaimed — by another worker, or by another slot of
this SAME worker — see ``JobsRepository.heartbeat``'s docstring), the
heartbeat task logs and stops extending. The in-flight handler thread
cannot be cancelled cooperatively (it's a real OS thread, not a
coroutine) and is left to run to completion; its eventual
``complete()``/``fail()`` call is a raise-free no-op against the
now-reclaimed row (guarded by ``lease_token = <this claim's token> AND
status = 'running'`` — see those methods' docstrings), so no state gets
clobbered.

Graceful shutdown (bounded drain): cancelling the task returned by
``worker_loop(...)`` (mirrors the ``canary_loop`` task-create/cancel
pattern in ``app/main.py``) delivers ``CancelledError`` at the next
`await`. For an idle lane slot that's the poll sleep — immediate exit.
For a slot with a handler mid-flight, ``asyncio.to_thread``'s
cancellation semantics are NOT "wait for the thread, then raise" —
cancelling the awaiting coroutine delivers ``CancelledError``
IMMEDIATELY, while the underlying OS thread keeps running in the
background regardless (a plain ``asyncio.Future`` — which is what
``run_in_executor``/``to_thread`` hands back — always honors ``.cancel()``
on the *awaiter* side even though the wrapped ``concurrent.futures``
future refuses cancellation once its thread has started). Left
unhandled, that orphans the handler thread exactly at the moment
``app/main.py``'s lifespan proceeds to close the DuckDB singletons the
thread may still be reading/writing — a WAL-corruption-class race.

To avoid that, ``_run_one`` runs the handler as a separate
``asyncio.shield``-ed future: our own await gets cancelled promptly (so
shutdown isn't blocked indefinitely), but the shielded future itself
keeps running untouched, and is registered into ``worker_loop``'s
``in_flight`` registry instead of being abandoned. ``worker_loop``, once
every lane/reaper task has been cancelled, performs ONE bounded drain
pass: wait on all registered in-flight futures together for up to
``AGNES_WORKER_DRAIN_TIMEOUT_S`` seconds (default 45s — comfortably under
the 60s ``stop_grace_period`` a compose/Kubernetes SIGTERM-then-SIGKILL
shutdown typically allows). Every future that finishes within the
window is finalized normally (``complete()``/``fail()``, using that
job's own ``lease_token``) before ``worker_loop`` returns — so a
handler that finishes during the drain window still gets its outcome
recorded instead of relying on lease-expiry recovery. Anything still
running when the timeout elapses is logged (job id + kind) and left
running; a hard kill at that point leaves the job 'running' with a
lease that later expires and is recovered via ``claim_next()``'s reclaim
path, or — if attempts are already exhausted by then — this module's own
``reap_exhausted()`` sweep.

The bounded drain above covers *handler* futures only. Every jobs-table DB
call this module makes — the poll-path ``claim_next``/``reap_exhausted``/
``heartbeat`` and the shutdown-path ``complete``/``fail``/``get`` inside
``_drain_in_flight`` — goes through ``to_thread_drain_on_cancel``
(``app/api/health_probes.py``) instead: cancellation waits for the
in-flight single-statement call rather than orphaning its thread, so
``app/main.py``'s lifespan can't proceed to ``close_system_db()`` while a
jobs-table statement is still executing — the same
abandoned-thread-vs-DB-close race the canary/checkpoint loops drain
against, just with a millisecond window instead of a CHECKPOINT-sized one.

Those drains all draw from one budget shared across the whole shutdown
(see ``_drain_budget_s``), so cancelling the checkpoint loop, then the
canary loop, then this one cannot stack a full timeout each and overrun
the container's stop_grace_period.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import os
import socket
import time

from app.job_correlation import bind_request_id, unbind_request_id
from app.api.health_probes import to_thread_drain_on_cancel
from app.observability import metrics as obs_metrics
from app.worker import wakeup
from app.worker.kinds import dispatch_job
from app.worker.registry import EXTRACTION_LANE, HEAVY_LANE, JOB_KINDS, LIGHT_LANE, JobKind

logger = logging.getLogger(__name__)

_HEAVY_CONCURRENCY = 1
_LIGHT_CONCURRENCY = 2
#: Extraction lane concurrency (spec §7.5 / §16 step 7) fallback — this
#: constant is used only as the STATIC placeholder in `_LANE_CONCURRENCY`
#: below (kept for the `selected_lanes()` valid-token check); the actual
#: slot count `worker_loop` spawns comes from `_extraction_concurrency()`,
#: resolved once at worker start. See that function's docstring.
_EXTRACTION_CONCURRENCY = 1

#: Default extraction lane slot count when `extraction.concurrency` /
#: `AGNES_EXTRACTION_CONCURRENCY` is unset or invalid — same value as the
#: pre-configurable behavior, so an instance that never touches either knob
#: is byte-for-byte unaffected.
_DEFAULT_EXTRACTION_CONCURRENCY = 1

#: Clamp bounds for `extraction.concurrency` / `AGNES_EXTRACTION_CONCURRENCY`
#: (Stage 1 of Agnes-owned extraction parallelism — see module docstring).
#: Each slot spawns a FULL producer subprocess (its own crawl workers plus
#: an LLM pass), sized for the `extraction-worker` compose service's default
#: 4g/2cpu envelope which assumes exactly ONE concurrent producer run —
#: raising this without also raising `AGNES_EXTRACTION_WORKER_MEM_LIMIT`/
#: `AGNES_EXTRACTION_WORKER_CPUS` on that service risks OOM/CPU starvation
#: under the resulting concurrent producer load. 8 is a sanity ceiling, not
#: a tuned number — an operator sizing for more should raise the compose
#: limits well before approaching it.
_MIN_EXTRACTION_CONCURRENCY = 1
_MAX_EXTRACTION_CONCURRENCY = 8

#: Every lane this build knows about, in spawn order — the valid-token set
#: ``selected_lanes()`` checks an ``AGNES_WORKER_LANES`` token against.
_ALL_LANES: tuple[str, ...] = (HEAVY_LANE, LIGHT_LANE, EXTRACTION_LANE)

#: What ``selected_lanes()`` returns when ``AGNES_WORKER_LANES`` is unset —
#: deliberately HEAVY+LIGHT only, NOT ``_ALL_LANES``. This is the exact set
#: ``worker_loop`` always spawned before the extraction lane existed, so an
#: instance that never sets the env var (every deployment today, and the
#: default single-container/all-in-one topology) is byte-for-byte
#: unaffected: it neither polls for ``corpus-extraction`` nor pays for a
#: third idle lane slot. Extraction is opt-in the same way the feature
#: itself is (``extraction.enabled: false`` by default,
#: config/instance.yaml.example) — a deployment that wants it running
#: enables the config block AND either runs the dedicated
#: ``extraction-worker`` compose service (which sets
#: ``AGNES_WORKER_LANES=extraction``) or adds ``extraction`` to this
#: process's own ``AGNES_WORKER_LANES`` list.
_DEFAULT_LANES: tuple[str, ...] = (HEAVY_LANE, LIGHT_LANE)

_LANE_CONCURRENCY: dict[str, int] = {
    HEAVY_LANE: _HEAVY_CONCURRENCY,
    LIGHT_LANE: _LIGHT_CONCURRENCY,
    EXTRACTION_LANE: _EXTRACTION_CONCURRENCY,
}

#: Floor on the heartbeat cadence so a misconfigured/very short
#: ``lease_seconds`` (e.g. in a test) can't spin the heartbeat loop.
_MIN_HEARTBEAT_INTERVAL_S = 0.5

#: Default bound for the shutdown drain (see module docstring). Kept
#: comfortably under the 60s stop_grace_period compose/k8s typically give
#: a container between SIGTERM and SIGKILL.
_DEFAULT_DRAIN_TIMEOUT_S = 45.0


def default_worker_id() -> str:
    """``<hostname>:<pid>`` — stable per-process identity for ``leased_by``.

    NOTE: this is shared by every lane slot in this process — it is NOT
    a unique per-claim identity. See the module docstring's same-worker
    double-execution note for why the atomicity guard uses ``lease_token``
    (minted fresh per claim) instead.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def selected_lanes() -> tuple[str, ...]:
    """Parse ``AGNES_WORKER_LANES`` into the lanes THIS process's
    ``worker_loop`` should spawn slots for (spec §16 step 7's "per-process
    lane-selection env var").

    Mirrors :func:`app.roles.active_roles`'s env-var convention for the
    fail-loud-on-typo part: an unknown token raises ``ValueError`` naming
    every valid lane, rather than silently spawning no slot for it (whose
    registered kinds would then sit ``'queued'`` forever with no signal
    beyond an eventually-noticed backlog).

    Unlike ``active_roles()``, unset does NOT mean "every known lane" —
    it means :data:`_DEFAULT_LANES` (heavy + light), the exact set
    ``worker_loop`` always spawned before the extraction lane existed. The
    extraction lane is opt-in the same way the feature it serves is
    (``extraction.enabled: false`` by default): a deployment that never
    sets this env var is byte-for-byte unaffected by its addition, and one
    that wants extraction running adds it explicitly — either to this
    process's own list, or by running the dedicated ``extraction-worker``
    compose service (which sets ``AGNES_WORKER_LANES=extraction``).

    Order is preserved and de-duplicated (``"light,heavy,light"`` spawns
    each lane's slots exactly once); an empty or all-whitespace value is
    treated the same as unset.
    """
    raw = os.environ.get("AGNES_WORKER_LANES")
    if raw is None:
        return _DEFAULT_LANES
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return _DEFAULT_LANES
    unknown = [t for t in tokens if t not in _LANE_CONCURRENCY]
    if unknown:
        raise ValueError(f"Invalid AGNES_WORKER_LANES token(s) {unknown!r} — valid tokens: {', '.join(_ALL_LANES)}")
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return tuple(result)


def _extraction_concurrency() -> int:
    """Effective EXTRACTION lane slot count — ``AGNES_EXTRACTION_CONCURRENCY``
    (env) overrides ``extraction.concurrency`` (instance.yaml), same
    env-over-yaml posture as ``app.coordination.factory.resolve_backend_name``
    (env var checked first via ``os.environ.get`` and short-circuiting the
    yaml lookup entirely when set).

    Only called from :func:`worker_loop`, once, before any lane slot task is
    spawned — the resolved value is baked into that single ``worker_loop``
    call's slot count for its whole lifetime. Changing either knob therefore
    requires restarting the worker process (same as ``AGNES_WORKER_LANES``);
    there is no live-reload path for lane sizing.

    Never raises: an unset/empty value falls back to
    :data:`_DEFAULT_EXTRACTION_CONCURRENCY` (1) silently, a non-integer
    value falls back to the same default WITH a logged warning (never
    crashes the worker on a typo'd setting), and an in-range-but-out-of-
    bounds integer is clamped to
    :data:`_MIN_EXTRACTION_CONCURRENCY`/:data:`_MAX_EXTRACTION_CONCURRENCY`
    (1..8) with a logged warning rather than rejected outright.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_EXTRACTION_CONCURRENCY")
    if raw is None:
        raw = get_value("extraction", "concurrency", default=_DEFAULT_EXTRACTION_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "worker: invalid extraction.concurrency/AGNES_EXTRACTION_CONCURRENCY=%r, using default %d",
            raw,
            _DEFAULT_EXTRACTION_CONCURRENCY,
        )
        return _DEFAULT_EXTRACTION_CONCURRENCY
    if value < _MIN_EXTRACTION_CONCURRENCY or value > _MAX_EXTRACTION_CONCURRENCY:
        clamped = max(_MIN_EXTRACTION_CONCURRENCY, min(value, _MAX_EXTRACTION_CONCURRENCY))
        logger.warning(
            "worker: extraction.concurrency/AGNES_EXTRACTION_CONCURRENCY=%d out of range [%d, %d], clamping to %d",
            value,
            _MIN_EXTRACTION_CONCURRENCY,
            _MAX_EXTRACTION_CONCURRENCY,
            clamped,
        )
        return clamped
    return value


def _drain_timeout_s() -> float:
    raw = os.environ.get("AGNES_WORKER_DRAIN_TIMEOUT_S")
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
        logger.warning("worker: invalid AGNES_WORKER_DRAIN_TIMEOUT_S=%r, using default", raw)
        return _DEFAULT_DRAIN_TIMEOUT_S
    return max(value, 0.0)


def _jobs_repo():
    # Imported lazily (module-function, not module-level import) so tests
    # can monkeypatch ``src.repositories.jobs_repo`` freely and so this
    # module carries no import-time dependency on which backend is active.
    from src.repositories import jobs_repo

    return jobs_repo()


def _kinds_for_lane(lane: str) -> list[str]:
    return [name for name, kind in JOB_KINDS.items() if kind.lane == lane]


def _notify_agent_response_webhooks(job: dict, status: str) -> None:
    """Best-effort outbound-webhook fan-out when an ``agent_response`` job
    reaches a terminal state (V1b Task 6, ``app.chat.webhook_delivery``).

    This is the single place an ``agent_response`` job's outcome becomes
    externally observable via webhook: every other job kind driven by this
    runtime is untouched (``job["kind"] != "agent_response"`` short-circuits
    immediately) — webhooks are scoped to Agent-as-API responses, not this
    runtime's internal maintenance kinds (``data-refresh``,
    ``distribution-mirror``, ...), and not ``webhook-deliver`` jobs
    themselves (which would recurse).

    Privacy (C11 — see ``app.chat.webhook_delivery`` module docstring): the
    notification payload built downstream never carries the agent's answer,
    only ``{event, job_id, agent_slug, status, ts}``.

    Best-effort by design, mirroring
    ``app/worker/kinds.py::_maybe_enqueue_distribution_mirror``'s shape — a
    failure to enqueue a webhook notification must never be able to
    un-finalize (retry/fail) a job whose ``complete()``/``fail()`` call
    above already committed.
    """
    if job.get("kind") != "agent_response":
        return
    agent_id = (job.get("payload_json") or {}).get("agent_id")
    if not agent_id:
        return
    try:
        from app.chat.webhook_delivery import enqueue_job_event_webhooks

        enqueue_job_event_webhooks(agent_id=agent_id, job_id=job["id"], status=status)
    except Exception:
        logger.warning("worker: agent_response webhook notify failed for job %s (non-fatal)", job["id"], exc_info=True)


def _sweep_stale_scratch() -> None:
    """Best-effort orphaned-scratch sweep, run before each HEAVY job.

    Heavy jobs (``data-refresh``, ``jira-refresh`` — registered in a later
    task) are exactly the Keboola-export workload that leaves
    ``kbc-export-*`` / ``kbc-slice-*`` staging dirs behind when a process
    is hard-killed mid-export (SIGKILL/OOM/container recreate) — see
    ``connectors/keboola/storage_api.py:sweep_orphaned_scratch``'s
    docstring for the full failure mode this prevents (unswept scratch
    fills the data disk until every sync fails with ENOSPC). Reused as-is,
    not reimplemented: same age-gate (``AGNES_SCRATCH_MAX_AGE_SEC``,
    default 1h) and prefix set.
    """
    try:
        from connectors.keboola.storage_api import sweep_orphaned_scratch

        sweep_orphaned_scratch()
    except Exception:
        logger.exception("worker: stale-scratch sweep failed (non-fatal)")


@dataclasses.dataclass
class _InFlightJob:
    """One handler still running when shutdown drain begins — see the
    module docstring's "Graceful shutdown (bounded drain)" section."""

    job_id: str
    kind_name: str
    worker_id: str
    lease_token: str
    retry_in_seconds: int | None
    handler_future: asyncio.Future[None]
    hb_task: asyncio.Task[None]
    #: Lane the job was running in — carried through to `_drain_in_flight`
    #: so it can decrement `agnes_jobs_running`/`agnes_worker_lane_active`
    #: (incremented in `_run_one`, before the handoff) once this entry is
    #: finally resolved (finished or abandoned).
    lane: str
    #: `time.monotonic()` when the handler started — lets `_drain_in_flight`
    #: observe `agnes_job_duration_seconds` for a job that finishes during
    #: the drain window instead of losing its timing entirely.
    started_at: float


async def _heartbeat_loop(job_id: str, worker_id: str, lease_token: str, lease_seconds: int) -> None:
    """Extend the lease every ``lease_seconds/3`` while a handler runs.

    Stops silently (no exception) the first time ``heartbeat()`` returns
    ``False`` — see the module docstring for why the in-flight handler
    thread is left running regardless. A transient failure calling
    ``heartbeat()`` itself (e.g. a DB hiccup) is logged and retried at the
    next tick rather than killing this task outright — same hardening
    convention as ``_lane_slot``.
    """
    interval = max(lease_seconds / 3, _MIN_HEARTBEAT_INTERVAL_S)
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await to_thread_drain_on_cancel(_jobs_repo().heartbeat, job_id, worker_id, lease_token, lease_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "worker %s: heartbeat for job %s failed transiently (non-fatal); retrying next tick",
                worker_id,
                job_id,
            )
            continue
        if not ok:
            logger.warning(
                "worker %s: heartbeat lost for job %s (lease reclaimed — possibly by another slot of "
                "this same worker); abandoning heartbeat",
                worker_id,
                job_id,
            )
            return


async def _run_one(job: dict, kind: JobKind, worker_id: str, in_flight: dict[str, _InFlightJob]) -> None:
    """Run one claimed job's handler with a concurrent heartbeat, then
    complete()/fail() it.

    The handler runs as a separate, ``asyncio.shield``-ed future so that
    cancelling THIS coroutine (shutdown) cannot cancel the handler's
    underlying OS thread — see the module docstring. If our own await is
    cancelled before the handler finishes, the future is handed off to
    ``in_flight`` for ``worker_loop``'s bounded shutdown drain to finish
    waiting on (and finalize) instead of being abandoned here.

    Wraps ``agnes_jobs_running``/``agnes_worker_lane_active`` (lane-slot
    occupancy) and ``agnes_job_duration_seconds``/``agnes_job_failures_total``
    (outcome) around the handler invocation. The observability helpers
    (``app.observability.metrics``) never raise, so a metrics bug can't fail
    a job — but the occupancy gauges are only decremented here on the
    normal (success/exception) exit paths; a handed-off (cancelled) job
    stays "running" until ``_drain_in_flight`` resolves it, since the
    handler keeps executing in the background regardless of this
    coroutine's cancellation (see module docstring).
    """
    lease_token = job["lease_token"]
    # Bind the originating request-id (`_enqueued_by_request`, stamped at
    # enqueue time by `app.job_correlation.stamp_request_id`) into the same
    # `request_id_var` contextvar the request middleware uses, for the
    # duration of running this job. `asyncio.create_task`/`asyncio.to_thread`
    # both copy the *current* context at creation time, so this must happen
    # before `hb_task`/`handler_future` are created below in order for the
    # heartbeat task and the handler thread to see it. No-op (returns
    # `None`) when the payload has no (or a malformed) `_enqueued_by_request`
    # — never raises, so a missing/malformed key can't break job execution.
    rid_token = bind_request_id(job.get("payload_json"))
    try:
        hb_task = asyncio.create_task(
            _heartbeat_loop(job["id"], worker_id, lease_token, kind.lease_seconds),
            name=f"worker-heartbeat-{job['id']}",
        )
        # `dispatch_job` (F2b — audit-full-coverage plan, Task 4) is the ONE
        # dispatch-level entry point that runs a claimed job's handler and
        # writes its `job.run` audit row — every kind funnels through it
        # instead of this module calling `kind.handler(...)` directly, so
        # audit coverage lives in exactly one place regardless of kind.
        handler_future = asyncio.ensure_future(asyncio.to_thread(dispatch_job, job))
        handed_off = False
        obs_metrics.begin_job_running(job["kind"], kind.lane)
        started_at = time.monotonic()
        try:
            handler_result = await asyncio.shield(handler_future)
        except asyncio.CancelledError:
            handed_off = True
            in_flight[job["id"]] = _InFlightJob(
                job_id=job["id"],
                kind_name=job["kind"],
                worker_id=worker_id,
                lease_token=lease_token,
                retry_in_seconds=kind.retry_in_seconds,
                handler_future=handler_future,
                hb_task=hb_task,
                lane=kind.lane,
                started_at=started_at,
            )
            raise
        except Exception as exc:
            logger.exception("worker %s: job %s (kind=%s) failed", worker_id, job["id"], job["kind"])
            # Persist the outcome before recording it in metrics — if `.fail()`
            # itself raises, this propagates without ever having reported an
            # outcome that was never actually persisted.
            finalized = await to_thread_drain_on_cancel(
                _jobs_repo().fail,
                job["id"],
                worker_id,
                lease_token,
                str(exc),
                retry_in_seconds=kind.retry_in_seconds,
            )
            obs_metrics.record_job_duration(job["kind"], "failed", time.monotonic() - started_at)
            obs_metrics.record_job_failure(job["kind"], type(exc).__name__)
            if finalized:
                # `fail()` returns `True` only when THIS call actually
                # finalized the row to `'failed'` — `False` covers both a
                # successful requeue (job isn't terminal yet; a kind
                # configured to retry must not fire a premature
                # `job.failed` webhook for a job that may still succeed)
                # AND a stale-lease no-op (this claim was already reclaimed
                # by another worker/slot, whose own finalize — not this
                # one — owns the real outcome). Gating on the kind's static
                # `retry_in_seconds` config here would miss the case where
                # a *retrying* kind's attempts are actually exhausted — see
                # `JobsRepository.fail`'s docstring.
                _notify_agent_response_webhooks(job, "failed")
        else:
            # Same ordering rationale as the failure branch above.
            # `handler_result` is the handler's return value — `None` for
            # every kind except `agent_response` (Task 9), which returns a
            # result dict `complete()` merges into `payload_json["result"]`.
            mutated = await to_thread_drain_on_cancel(
                _jobs_repo().complete, job["id"], worker_id, lease_token, handler_result
            )
            obs_metrics.record_job_duration(job["kind"], "done", time.monotonic() - started_at)
            if mutated:
                # `complete()` returns `False` for the same stale-lease
                # no-op as `fail()` above — a reclaimed claim's late
                # `complete()` must not fire a notification for an outcome
                # another slot already owns.
                _notify_agent_response_webhooks(job, "completed")
        finally:
            if not handed_off:
                obs_metrics.end_job_running(job["kind"], kind.lane)
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
    finally:
        # Safe to reset unconditionally, including when handed off: the
        # handler future already copied its own context at creation time
        # (above), so resetting here doesn't affect a still-running
        # handed-off handler — it only prevents this contextvar from
        # leaking into whatever `_lane_slot` claims next in this same task.
        unbind_request_id(rid_token)


async def _lane_slot(
    lane: str,
    worker_id: str,
    poll_interval_s: float,
    in_flight: dict[str, _InFlightJob],
) -> None:
    """One concurrency slot for ``lane``: claim -> run -> repeat, sleeping
    ``poll_interval_s`` whenever there's nothing to do (no registered
    kinds for the lane, or nothing eligible to claim).

    The whole iteration body runs under a broad ``except Exception`` (NOT
    ``except BaseException`` — ``asyncio.CancelledError`` must propagate
    for shutdown to work) so a transient failure anywhere in the claim/run
    path (e.g. a DB hiccup on ``claim_next()``) logs and retries after
    ``poll_interval_s`` instead of permanently killing this slot — and,
    via ``asyncio.gather``'s cancel-all-on-first-exception semantics in
    ``worker_loop``, every OTHER slot and the reaper too. Mirrors the
    hardening already used by ``canary_loop``/``_state_checkpoint_loop``
    in ``app/main.py``.
    """
    while True:
        try:
            kinds = _kinds_for_lane(lane)
            if not kinds:
                await asyncio.sleep(poll_interval_s)
                continue

            # claim_next() needs a lease duration before it knows which job
            # (and therefore which kind) it will return; using the longest
            # lease configured across the lane's kinds guarantees the initial
            # lease never expires before the first heartbeat tick corrects it
            # to the claimed job's actual kind.lease_seconds (heartbeat reads
            # the kind fresh after claiming, below).
            max_lease = max((JOB_KINDS[name].lease_seconds for name in kinds), default=120)
            job = await to_thread_drain_on_cancel(
                _jobs_repo().claim_next,
                kinds=kinds,
                worker_id=worker_id,
                lease_seconds=max_lease,
            )
            if job is None:
                # Idle: sleep up to poll_interval, but wake early on a
                # NOTIFY-driven signal (app.worker.wakeup). Degrades to a
                # plain poll_interval sleep when nothing signals (DuckDB
                # backend / listener down), so never worse than poll-only.
                await wakeup.idle_wait(poll_interval_s)
                continue

            # Counted here, not inside _run_one — a claim_next() success is
            # a claim regardless of what happens next (including the
            # no-registered-handler branch immediately below), so this is
            # the one place that sees every claim exactly once.
            obs_metrics.record_job_claim(job["kind"])

            kind = JOB_KINDS.get(job["kind"])
            if kind is None:
                # Registry drift: this job's kind isn't (or is no longer)
                # registered on this process. Fail it outright rather than
                # spin forever re-claiming a job nobody here can execute.
                logger.error(
                    "worker %s: no registered handler for job kind %r (job %s); failing",
                    worker_id,
                    job["kind"],
                    job["id"],
                )
                obs_metrics.record_job_failure(job["kind"], "no-registered-handler")
                await to_thread_drain_on_cancel(
                    _jobs_repo().fail,
                    job["id"],
                    worker_id,
                    job["lease_token"],
                    f"no registered handler for kind {job['kind']!r}",
                    retry_in_seconds=None,
                )
                continue

            if lane == HEAVY_LANE:
                await asyncio.to_thread(_sweep_stale_scratch)

            await _run_one(job, kind, worker_id, in_flight)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker %s: lane %s poll iteration failed (non-fatal); retrying", worker_id, lane)
            await asyncio.sleep(poll_interval_s)


async def _reap_loop(poll_interval_s: float) -> None:
    """Sweep ``reap_exhausted()`` once per ``poll_interval_s`` tick,
    independent of lane activity (see module docstring).

    A reaped job reaches ``'failed'`` without ever going through
    ``fail()`` (its owning worker crashed before it could call anything),
    so this is the ONLY place its terminal-state webhook notify can fire
    from (MEDIUM 2a) — without it a receiver waits forever for a
    `job.failed` that never comes after a worker crash on a job's last
    attempt. Mirrors ``_notify_agent_response_webhooks``'s no-op-for-other-
    kinds behavior; a non-``agent_response`` reaped job is a silent no-op.
    """
    while True:
        try:
            reaped = await to_thread_drain_on_cancel(_jobs_repo().reap_exhausted)
            if reaped:
                logger.info("worker: reaped %d stuck job(s) (lease expired at max attempts)", len(reaped))
                for job in reaped:
                    _notify_agent_response_webhooks(job, "failed")
        except Exception:
            logger.exception("worker: reap_exhausted sweep failed (non-fatal)")
        await asyncio.sleep(poll_interval_s)


async def _notify_in_flight_agent_response(job_id: str, status: str) -> None:
    """`_drain_in_flight`'s counterpart to `_notify_agent_response_webhooks`.

    `_InFlightJob` (unlike the `job` dict `_run_one` already has in hand)
    carries no `payload_json` — a handler that finished during the bounded
    shutdown drain was handed off before its full job row was needed again
    — so this re-fetches the row fresh via `jobs_repo().get()` before
    delegating. Best-effort in its own right (separate from the
    finalization `try`/`except` around the `complete()`/`fail()` call this
    runs after), so a fetch/notify failure here is never misattributed as a
    "finalization (complete/fail) failed" error for a job whose outcome was
    already durably persisted."""
    try:
        job_row = await to_thread_drain_on_cancel(_jobs_repo().get, job_id)
        if job_row is not None:
            _notify_agent_response_webhooks(job_row, status)
    except Exception:
        logger.warning(
            "worker: agent_response webhook notify (shutdown drain) failed for job %s", job_id, exc_info=True
        )


async def _drain_in_flight(
    in_flight: dict[str, _InFlightJob], worker_id: str, *, budget_s: float | None = None
) -> None:
    """Bounded shutdown drain: wait on every handler future handed off by
    ``_run_one`` (see module docstring) for up to
    ``AGNES_WORKER_DRAIN_TIMEOUT_S`` seconds, finalizing whichever finish
    in time and logging (without finalizing) whichever don't.

    ``budget_s`` lets the caller pass what is LEFT of one shutdown-wide
    budget rather than granting a fresh full one. worker_loop does that:
    its straggler wait and this drain run back to back, so two independent
    45s bounds would sum past the 60s stop_grace_period and get the
    container SIGKILLed mid-drain — the outcome these bounds exist to avoid
    (review finding on #1140).
    """
    if not in_flight:
        return
    timeout = _drain_timeout_s() if budget_s is None else max(budget_s, 0.0)
    logger.info(
        "worker %s: shutdown draining %d in-flight job(s) (timeout=%.0fs): %s",
        worker_id,
        len(in_flight),
        timeout,
        sorted(in_flight),
    )
    futures = [entry.handler_future for entry in in_flight.values()]
    _done, pending = await asyncio.wait(futures, timeout=timeout)
    for job_id, entry in in_flight.items():
        # `agnes_jobs_running`/`agnes_worker_lane_active` were incremented
        # in `_run_one` before the handoff and never decremented there
        # (the handler keeps running regardless of that coroutine's own
        # cancellation) — this drain loop is the only place every in-flight
        # entry is eventually resolved (finished in time, cancelled, or
        # abandoned on timeout), so it's the one place that must undo it,
        # exactly once per entry, regardless of which branch below fires.
        try:
            fut = entry.handler_future
            if fut in pending:
                logger.warning(
                    "worker %s: shutdown drain timed out after %.0fs — job %s (kind=%s) abandoned mid-flight; "
                    "will recover via lease expiry (reclaim or reap_exhausted)",
                    worker_id,
                    timeout,
                    job_id,
                    entry.kind_name,
                )
                entry.hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.hb_task
                continue
            if fut.cancelled():
                logger.warning(
                    "worker %s: in-flight job %s (kind=%s) handler future was cancelled during drain",
                    worker_id,
                    job_id,
                    entry.kind_name,
                )
                continue
            exc = fut.exception()
            duration = time.monotonic() - entry.started_at
            # Persist first, record metrics only after the persist call
            # actually succeeds — same ordering rationale as `_run_one`
            # (a failed `.fail()`/`.complete()` write must not still report
            # an outcome that never made it to the DB).
            try:
                if exc is not None:
                    logger.exception(
                        "worker %s: job %s (kind=%s) failed (finished during shutdown drain)",
                        worker_id,
                        job_id,
                        entry.kind_name,
                        exc_info=exc,
                    )
                    finalized = await to_thread_drain_on_cancel(
                        _jobs_repo().fail,
                        job_id,
                        entry.worker_id,
                        entry.lease_token,
                        str(exc),
                        retry_in_seconds=entry.retry_in_seconds,
                    )
                    obs_metrics.record_job_duration(entry.kind_name, "failed", duration)
                    obs_metrics.record_job_failure(entry.kind_name, type(exc).__name__)
                    # `finalized` (not `entry.retry_in_seconds is None`) is
                    # the terminal-state signal — see `_run_one`'s matching
                    # comment / `JobsRepository.fail`'s docstring.
                    if entry.kind_name == "agent_response" and finalized:
                        await _notify_in_flight_agent_response(job_id, "failed")
                else:
                    handler_result = fut.result()
                    mutated = await to_thread_drain_on_cancel(
                        _jobs_repo().complete, job_id, entry.worker_id, entry.lease_token, handler_result
                    )
                    obs_metrics.record_job_duration(entry.kind_name, "done", duration)
                    if entry.kind_name == "agent_response" and mutated:
                        await _notify_in_flight_agent_response(job_id, "completed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "worker %s: job %s (kind=%s) finalization (complete/fail) failed during shutdown drain "
                    "(non-fatal); job will recover via lease expiry",
                    worker_id,
                    job_id,
                    entry.kind_name,
                )
            entry.hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry.hb_task
        finally:
            obs_metrics.end_job_running(entry.kind_name, entry.lane)


async def worker_loop(*, worker_id: str, poll_interval_s: float = 5.0) -> None:
    """Run the worker runtime until cancelled.

    Starts the reaper task, then — for every lane :func:`selected_lanes`
    returns (default: heavy + light only, extraction is opt-in; see the
    module docstring and ``AGNES_WORKER_LANES``) — that lane's own
    concurrency worth of slots, and waits on all of them. HEAVY and LIGHT's
    slot counts are the static :data:`_LANE_CONCURRENCY` values; EXTRACTION's
    is resolved fresh HERE, once, via :func:`_extraction_concurrency` —
    baked into this call's slot count for its whole lifetime (see that
    function's docstring for why changing the underlying config needs a
    worker restart). Cancelling the
    enclosing task (the ``canary_loop`` task-create/cancel pattern in
    ``app/main.py``'s lifespan) cancels every child task too —
    ``asyncio.gather`` propagates cancellation of its own awaiter to every
    task it's gathering. Before
    returning, performs one bounded drain of any handler still mid-flight
    (see module docstring).

    Raises ``ValueError`` (from :func:`selected_lanes`) before spawning
    anything if ``AGNES_WORKER_LANES`` names an unknown lane.
    """
    lanes = selected_lanes()
    # EXTRACTION_LANE resolved fresh, not the static _EXTRACTION_CONCURRENCY
    # placeholder — see _extraction_concurrency()'s docstring. HEAVY/LIGHT
    # are untouched (Stage 1 scope — see module docstring).
    lane_concurrency = {**_LANE_CONCURRENCY, EXTRACTION_LANE: _extraction_concurrency()}
    in_flight: dict[str, _InFlightJob] = {}
    tasks = [asyncio.create_task(_reap_loop(poll_interval_s), name="worker-reaper")]
    # Best-effort PG LISTEN loop that wakes idle lane slots on a fresh
    # enqueue (see app.worker.wakeup). A clean no-op on DuckDB / if it can't
    # connect — the lane slots keep polling regardless.
    tasks.append(asyncio.create_task(wakeup.notify_listener(), name="worker-notify-listener"))
    for lane in lanes:
        tasks += [
            asyncio.create_task(_lane_slot(lane, worker_id, poll_interval_s, in_flight), name=f"worker-{lane}-{i}")
            for i in range(lane_concurrency[lane])
        ]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Defensive: make sure every child is actually cancelled/awaited
        # even if gather() returned early for a reason other than our own
        # cancellation (e.g. one task raised and gather fails fast while
        # siblings are still running). Skip tasks already processing a
        # cancellation (`cancelling() > 0`): they are mid-drain in
        # `to_thread_drain_on_cancel`, and a second cancel would interrupt
        # that drain and re-orphan the in-flight DB thread — the exact race
        # the drain exists to prevent.
        for t in tasks:
            if not t.done() and t.cancelling() == 0:
                t.cancel()
        # Bounded: skipping the second cancel for a task that is mid-drain is
        # right, but it means a task which somehow lost its cancellation is
        # never asked again — and an unbounded wait on it would hang shutdown
        # forever, the very symptom this change removes. The drains inside
        # those tasks are themselves bounded by the shared DB-drain budget,
        # so this only has to outlast that — the worker's own (larger) drain
        # knob does. Past it we log and let shutdown proceed.
        #
        # NOTE this whole block is inert on the ordinary cancellation path:
        # cancelling the awaiter of a gather() cancels each child but does not
        # resolve the gather future until every child is actually done, so we
        # arrive here with all tasks complete. It earns its keep only when
        # gather() failed fast because a child raised, leaving siblings live
        # (review findings on #1140).
        # ONE budget for the whole worker shutdown: the straggler wait below
        # and the in-flight drain after it run back to back, so giving each
        # its own full bound would stack past the container's grace period.
        shutdown_deadline = time.monotonic() + _drain_timeout_s()
        finished, still_running = await asyncio.wait(tasks, timeout=max(shutdown_deadline - time.monotonic(), 0.0))
        for t in finished:
            # asyncio.wait, unlike gather(return_exceptions=True), does not
            # retrieve results — an unconsumed exception would be dropped
            # here and only resurface as "Task exception was never retrieved"
            # at GC, losing a real shutdown-path failure.
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                logger.warning(
                    "worker %s: lane task %s failed during shutdown: %s",
                    worker_id,
                    t.get_name(),
                    type(exc).__name__,
                    exc_info=exc,
                )
        for t in still_running:
            logger.warning(
                "worker %s: lane task %s did not stop within the shutdown budget; abandoning it "
                "(it may still touch the DB after this returns)",
                worker_id,
                t.get_name(),
            )
        # Every lane slot has now stopped claiming new work. Any handler
        # that was mid-flight when its slot got cancelled was handed off
        # into `in_flight` (see _run_one) instead of being abandoned —
        # drain it here, bounded, before this function returns and
        # app/main.py proceeds to close the DB singletons.
        await _drain_in_flight(in_flight, worker_id, budget_s=shutdown_deadline - time.monotonic())
