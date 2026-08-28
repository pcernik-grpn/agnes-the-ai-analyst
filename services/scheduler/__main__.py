"""Scheduler service — replaces systemd timers.

Lightweight sidecar that fires scheduled jobs over HTTP against the main
app. Authenticates with ``SCHEDULER_API_TOKEN`` (shared-secret synthetic
admin — see ``app.auth.scheduler_token``); falls back to no-auth in
LOCAL_DEV_MODE.

Schedules are strings parsed by ``src.scheduler.is_table_due`` — accepts
"every 15m", "every 1h", "daily 03:00", "daily 07:00,13:00".

Why every job is HTTP and nothing runs in-process: the scheduler container
shares ``/data/state/system.duckdb`` with the app container, but DuckDB
permits only one writer per file across processes. An in-process call
from the scheduler raced the app's long-lived handle and 500-ed on
``Could not set lock on file``. Going through HTTP makes the app the sole
writer; the scheduler is reduced to a pure cron clock.

Wave-2B job-queue migration (Task 6): four rows (``data-refresh``,
``marketplaces``, ``session-collector``, ``corporate-memory``) no longer
run their work inside that HTTP call at all — they POST a fire-and-forget
enqueue request to ``/api/jobs`` and a worker process executes the job
out-of-band. Wave-2G Task 5 added a fifth such row, ``ducklake-maintenance``.
See ``_ENQUEUE_BODIES`` below and ``docs/jobs-classification.md`` for the
full per-row classification.

Usage: python -m services.scheduler
"""

import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.logging_config import setup_logging
from src.scheduler import is_table_due

setup_logging(__name__)
logger = logging.getLogger(__name__)

# Durable last-run state (three-plane spec §3.3: "per-job catch-up replaces
# in-memory last_run"). The scheduler is a DB-less HTTP cron clock, so its
# catch-up state can't live in the jobs table — it persists here, on the
# same /data volume the app + scheduler share, surviving container restart
# and recreate. Without it, every restart resets last_run to None → every
# job re-fires on the first post-grace tick (is_table_due(None) is always
# True); harmless for the idempotency-keyed enqueue jobs, but a burst of
# duplicate fires + audit noise on the HTTP jobs. Best-effort throughout: a
# load or persist failure logs and falls back to the old in-memory behavior,
# never crashing the scheduler.
_LAST_RUN_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "scheduler_last_run.json"
_last_run_lock = threading.Lock()


def _load_last_run(job_names: set[str]) -> dict[str, "str | None"]:
    """Seed last_run from the durable file, keyed to the current job set.

    Only keys still present in ``job_names`` are carried over (a renamed or
    removed job's stale entry is dropped); jobs with no persisted entry start
    at ``None`` (== "never ran, due now"), preserving first-boot behavior.
    """
    seeded: dict[str, str | None] = {name: None for name in job_names}
    try:
        if _LAST_RUN_PATH.exists():
            stored = json.loads(_LAST_RUN_PATH.read_text())
            for name, ts in stored.items():
                if name in seeded and isinstance(ts, str):
                    seeded[name] = ts
            logger.info(
                "scheduler: restored last_run for %d/%d jobs from %s",
                sum(1 for v in seeded.values() if v is not None),
                len(seeded),
                _LAST_RUN_PATH,
            )
    except Exception:
        logger.warning("scheduler: could not read %s; starting from empty last_run", _LAST_RUN_PATH, exc_info=True)
    return seeded


def _persist_last_run(last_run: dict[str, "str | None"]) -> None:
    """Atomically write last_run to the durable file. Best-effort — a write
    failure (read-only mount, disk full) is logged once and swallowed so the
    scheduler keeps running on its in-memory copy.

    The tmp filename is suffixed with our own PID: two scheduler containers
    sharing ``DATA_DIR`` (the anti-synchronize bq-metadata-refresh offset
    logic above assumes exactly this topology) would otherwise race on the
    same shared tmp path — one process's write clobbering the other's before
    either calls ``os.replace``. ``_last_run_lock`` only serializes threads
    within this process; it does nothing across processes."""
    try:
        with _last_run_lock:
            _LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _LAST_RUN_PATH.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(last_run))
            os.replace(tmp, _LAST_RUN_PATH)
    except Exception:
        logger.warning("scheduler: could not persist last_run to %s (non-fatal)", _LAST_RUN_PATH, exc_info=True)


API_URL = os.environ.get("API_URL", "http://localhost:8000")
SCHEDULER_API_TOKEN = os.environ.get("SCHEDULER_API_TOKEN", "")

_token_warning_emitted = False


def _get_auth_token() -> str:
    """Return the bearer token for API calls.

    Production: ``SCHEDULER_API_TOKEN`` is a shared secret generated by the
    Terraform startup script and written to ``/opt/agnes/.env``. Both the
    ``app`` and ``scheduler`` containers source the same .env via Docker
    Compose ``env_file:``, so the secret is symmetric. The app validates
    incoming Bearer tokens against this env var (constant-time compare in
    ``app.auth.scheduler_token``) and resolves matches to a synthetic
    ``scheduler@system.local`` user that is a member of the Admin group.

    Dev / LOCAL_DEV_MODE: leave it unset. The scheduler returns the empty
    string and calls the API without an ``Authorization`` header — the
    API's dev-bypass auto-authenticates the request as the dev user.
    """
    global _token_warning_emitted
    if SCHEDULER_API_TOKEN:
        return SCHEDULER_API_TOKEN
    if not _token_warning_emitted:
        logger.warning(
            "SCHEDULER_API_TOKEN is not set — calling the API without "
            "Authorization. Required in production; in LOCAL_DEV_MODE "
            "the dev-bypass auto-authenticates and this is fine."
        )
        _token_warning_emitted = True
    return ""


# --- Env parsing ------------------------------------------------------------

_DEFAULTS = {
    "SCHEDULER_DATA_REFRESH_INTERVAL": 15 * 60,  # seconds
    "SCHEDULER_HEALTH_CHECK_INTERVAL": 5 * 60,
    "SCHEDULER_SCRIPT_RUN_INTERVAL": 1 * 60,
    "SCHEDULER_TICK_SECONDS": 30,
    # LLM pipeline cadences (#176, #179 review). Defaults preserve the
    # 10m / 15m / 17m coprime offset so the three jobs don't fire on the
    # same tick and stack their API + DB load. The verification-detector
    # default (900s) is also the source of truth for the health-check
    # staleness grace window in app/api/health.py — single env var drives
    # both, so an operator changing the cadence moves both.
    "SCHEDULER_SESSION_COLLECTOR_INTERVAL": 10 * 60,
    # Drives the verification session-processor cadence AND the
    # health-check staleness grace window in app/api/health.py
    # (single env var → both, so an operator changing the cadence moves
    # both). Name retained post session-pipeline refactor for operator
    # compatibility — existing docker-compose env files keep working.
    # SCHEDULER_VERIFICATION_SCHEDULE (see _verification_schedule()) can
    # override the interval-derived cadence with a fixed schedule string
    # (e.g. "daily 04:15") — when set, bump this interval to match the real
    # cadence too, so the health-check grace window stays calibrated.
    "SCHEDULER_VERIFICATION_DETECTOR_INTERVAL": 15 * 60,
    "SCHEDULER_USAGE_PROCESSOR_INTERVAL": 10 * 60,
    "SCHEDULER_CORPORATE_MEMORY_INTERVAL": 17 * 60,
    # BigQuery metadata refresh: walks remote registry rows and updates the
    # persistent ``bq_metadata_cache``. Default 4 h — long enough that the
    # cumulative BQ jobs API cost stays negligible on a typical 10–50-table
    # registry, short enough that operator-edited tables show real numbers
    # within an analyst's working day.
    "SCHEDULER_BQ_METADATA_REFRESH_INTERVAL": 4 * 60 * 60,
    # Keboola semantic layer (Metastore) refresh: walks a Keboola project's
    # datasets/metrics/constraints and upserts+prunes metric_definitions
    # rows tagged source='keboola_semantic_layer'. Default 6 h — metrics
    # change less often than BQ metadata cache entries (4 h default), and
    # each run does far fewer HTTP calls (a handful of Metastore list
    # requests vs one BQ metadata fetch per registered table).
    "SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL": 6 * 60 * 60,
    # Databricks semantic layer (Unity Catalog metric views) refresh: same
    # cadence + rationale as the Keboola sibling — a handful of warehouse
    # statements per run, syncing the workspace's `connection`-kind semantic
    # source (source='ossie_connection', source_ref='databricks_default').
    "SCHEDULER_DATABRICKS_SEMANTIC_LAYER_REFRESH_INTERVAL": 6 * 60 * 60,
    # Generic semantic-sources refresh (Block 3 step 2, issue #1707): the
    # ONE scheduled sweep over every registered `semantic_sources` row
    # (git/upload/connection kinds), honoring each row's `enabled` flag.
    # Same 6 h default and rationale as its Keboola/Databricks siblings
    # above — this row runs ALONGSIDE them, not instead of them, until a
    # later, separately-sequenced migration (#1707 steps 3-4) moves their
    # callers onto this generic path and retires the two legacy endpoints.
    "SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL": 6 * 60 * 60,
    # Pause between scheduler startup and the first tick. Keeps the
    # scheduler from synchronising its "Table never synced, marking as
    # due" burst with the app's own startup cache_warmup. Set to 0 to
    # disable — useful in tests that need deterministic-fast first-tick.
    "SCHEDULER_STARTUP_GRACE_SECONDS": 60,
    # Daily prune of usage_events older than USAGE_EVENTS_RETENTION_DAYS
    # (default 0 = forever). Rollups are not pruned.
    "SCHEDULER_USAGE_PRUNE_INTERVAL": 86400,
    # Jira self-healing pair (parity with the legacy Data Broker
    # jira-sla-poll.timer / jira-consistency.timer). Both endpoints
    # short-circuit when JIRA_* env vars are unset, so customers without
    # Jira ingest pay no cost for these jobs running on the default schedule.
    #
    # Consistency still matches its systemd unit (30min); the SLA poll
    # deliberately does not match its 15min one. The poll walks every open
    # ticket serially (connectors/jira/scripts/poll_sla.py), so on a busy
    # service desk one pass runs well past 15min. systemd tolerated that —
    # it won't start a unit that is still active — but this scheduler
    # abandons the HTTP call at the job's timeout, clears its in_flight
    # guard, and re-fires on the next tick, stacking a second pass onto the
    # unfinished first. The two then contend on the same per-issue and
    # per-month advisory locks, doubling Jira API load for no fresher data.
    # So the cadence must exceed a realistic pass, not the legacy timer.
    "SCHEDULER_JIRA_SLA_POLL_INTERVAL": 45 * 60,
    "SCHEDULER_JIRA_CONSISTENCY_INTERVAL": 30 * 60,
    # K3 (#798) local knowledge packaging: rebuilds per-collection
    # knowledge.duckdb artifacts whose chunk content changed since the last
    # pass (fingerprint-only when idle — cheap). The ingest pipeline writes
    # chunks asynchronously, so a 15 min ceiling on artifact staleness
    # matches the corporate-memory cadence class.
    "SCHEDULER_KNOWLEDGE_PACKAGING_INTERVAL": 15 * 60,
    # K4 (#799) maintained digests: fingerprints each digest's instructions +
    # source corpora and regenerates via LLM only when the fingerprint
    # changed (fingerprint-only when idle — cheap; LLM only on change).
    # Same cadence class as corporate-memory, but a longer default since
    # digest regeneration is a heavier LLM call than the packaging rebuild.
    "SCHEDULER_KNOWLEDGE_DIGESTS_INTERVAL": 30 * 60,
    # Data apps idle-reaper: terminates idle data-app VMs (Task 7+9).
    # Default 5 min — lightweight (just marks stale VM rows for cleanup),
    # so the cadence doesn't require tuning. The actual cleanup runs
    # out-of-band via the jobs queue (not on the scheduler's tick).
    "SCHEDULER_DATA_APP_REAP_INTERVAL": 5 * 60,
}


def _read_positive_int(name: str) -> int:
    """Read an env var as a positive integer or fall back to the default.

    Treats unset env (``None``) as "use default". Treats explicitly empty
    string (``""``) as an operator typo and raises — silently defaulting
    on a literal ``FOO=`` in the env_file would mask configuration bugs.
    """
    raw = os.environ.get(name)
    if raw is None:
        if name not in _DEFAULTS:
            raise ValueError(f"Unknown scheduler env var: {name}")
        return _DEFAULTS[name]
    if raw == "":
        raise ValueError(f"{name}='' must be a positive integer (seconds)")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={raw!r} must be a positive integer (seconds)")
    if value <= 0:
        raise ValueError(f"{name}={value} must be > 0 (seconds)")
    return value


def _read_non_negative_int(name: str) -> int:
    """Like ``_read_positive_int`` but also accepts ``0`` as a valid value.

    Used for knobs where ``0`` means "disable" rather than "operator typo"
    — e.g. ``SCHEDULER_STARTUP_GRACE_SECONDS=0`` legitimately skips the
    startup pause in unit tests / fast-iteration dev setups.
    """
    raw = os.environ.get(name)
    if raw is None:
        if name not in _DEFAULTS:
            raise ValueError(f"Unknown scheduler env var: {name}")
        return _DEFAULTS[name]
    if raw == "":
        raise ValueError(f"{name}='' must be a non-negative integer (seconds)")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={raw!r} must be a non-negative integer (seconds)")
    if value < 0:
        raise ValueError(f"{name}={value} must be >= 0 (seconds)")
    return value


def _seconds_to_schedule(seconds: int) -> str:
    """Convert a seconds value to the closest 'every Nm' / 'every Nh' string.

    Uses ceiling division so a non-multiple-of-60 input never produces a
    schedule that fires MORE often than the operator configured (90s →
    'every 2m', not 'every 1m'). Sub-minute inputs clamp to 'every 1m'
    because the schedule grammar has minute-level resolution.
    """
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"every {seconds // 3600}h"
    # Ceiling division: -(-x // y) is the standard trick.
    minutes = max(1, -(-seconds // 60))
    return f"every {minutes}m"


_IW_SYNC_SCHEDULE_DEFAULT = "daily 03:30"

# Sentinel distinguishing "key absent / YAML null" (use the default) from an
# explicit empty string (admin cleared → disable). get_value collapses both a
# missing key and a null value to its `default`, so we pass this unique object
# and only a real "" value read back from YAML reaches the disable branch.
_IW_SCHEDULE_UNSET = object()


def _iw_sync_schedule() -> Optional[str]:
    """Resolve the Initial Workspace Template nightly auto-sync cadence
    (#622 Slice 3 PR-B).

    Read order:
      1. ``SCHEDULER_INITIAL_WORKSPACE_SCHEDULE`` env override
      2. instance.yaml ``initial_workspace.sync_schedule``
      3. default ``daily 03:30`` (offset from the marketplaces job at 03:00
         so the two nightly git-clone bursts don't stack)

    Returns ``None`` when the admin has explicitly cleared the schedule
    (``sync_schedule: ""`` in the overlay) — the documented "leave empty to
    disable auto-sync" contract — and ``build_jobs`` then omits the nightly
    job entirely. A key that is absent or null is treated as "never
    configured" and falls back to the default, so existing instances keep
    auto-sync on.

    Any non-empty value that fails ``is_valid_schedule`` falls back to the
    default — defensive, matching ``_seconds_to_schedule``'s posture — so a
    typo in the env / YAML never produces a schedule string ``is_table_due``
    would silently ignore. ``build_jobs`` runs once at container start, so a
    UI edit to ``sync_schedule`` takes effect only on the next scheduler
    restart.
    """
    from src.scheduler import is_valid_schedule

    raw = os.environ.get("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", "").strip()
    if not raw:
        try:
            from app.instance_config import get_value

            yaml_val = get_value("initial_workspace", "sync_schedule", default=_IW_SCHEDULE_UNSET)
            if yaml_val is _IW_SCHEDULE_UNSET:
                # Key absent or null → never explicitly configured.
                return _IW_SYNC_SCHEDULE_DEFAULT
            raw = (yaml_val or "").strip() if isinstance(yaml_val, str) else ""
            if not raw:
                # Explicitly set to empty → admin disabled auto-sync.
                return None
        except Exception:
            logger.exception("scheduler: failed to read initial_workspace.sync_schedule")
            return _IW_SYNC_SCHEDULE_DEFAULT

    if raw and is_valid_schedule(raw):
        return raw
    if raw:
        logger.warning(
            "scheduler: invalid initial_workspace sync_schedule %r — falling back to %r",
            raw,
            _IW_SYNC_SCHEDULE_DEFAULT,
        )
    return _IW_SYNC_SCHEDULE_DEFAULT


def _verification_schedule(verify_seconds: int) -> str:
    """Resolve the verification-detector processor's cadence.

    Read order:
      1. ``SCHEDULER_VERIFICATION_SCHEDULE`` env override (validated via
         ``is_valid_schedule``) — e.g. ``"daily 04:15"`` to pin the LLM-heavy
         verification pass to a fixed off-peak time instead of firing every
         N minutes/hours. Useful on instances where daytime CPU contention
         with request-serving matters more than near-real-time verification
         freshness.
      2. Interval-derived ``"every Nm"``/``"every Nh"`` from
         ``SCHEDULER_VERIFICATION_DETECTOR_INTERVAL`` (existing default
         behavior, unchanged for instances that don't set the override).

    An invalid override falls back to the interval-derived schedule rather
    than crashing the scheduler on a typo.

    IMPORTANT: the health check's staleness grace window
    (``app/api/health.py::_verification_detector_grace_seconds``) reads
    ``SCHEDULER_VERIFICATION_DETECTOR_INTERVAL`` directly and independently
    of this override — an operator who pins a daily schedule here should
    also set ``SCHEDULER_VERIFICATION_DETECTOR_INTERVAL=86400`` so the grace
    window (2x the cadence) stays calibrated to the real ~24h gap between
    runs instead of false-positiving a "stuck pipeline" warning every day.
    """
    from src.scheduler import is_valid_schedule

    raw = os.environ.get("SCHEDULER_VERIFICATION_SCHEDULE", "").strip()
    if raw:
        if is_valid_schedule(raw):
            return raw
        logger.warning(
            "scheduler: invalid SCHEDULER_VERIFICATION_SCHEDULE %r — falling back to interval-derived schedule",
            raw,
        )
    return _seconds_to_schedule(verify_seconds)


def resolved_tick_seconds() -> int:
    """Read + validate SCHEDULER_TICK_SECONDS in isolation (test helper)."""
    return _read_positive_int("SCHEDULER_TICK_SECONDS")


def resolved_startup_grace_seconds() -> int:
    """Read SCHEDULER_STARTUP_GRACE_SECONDS (default 60).

    Sleep duration between scheduler startup and the first tick. Mitigates
    the "post-deploy contention burst" where the scheduler's "everything
    is due" first tick (5+ paralle HTTP POSTs against the just-restarted
    app) overlaps the app's own startup ``cache_warmup`` job, doubling
    disk I/O on the host's boot disk and dropping concurrent parquet
    downloads from ~3 MB/s to ~1 MB/s for the duration of the burst.
    """
    return _read_non_negative_int("SCHEDULER_STARTUP_GRACE_SECONDS")


def resolved_bq_metadata_initial_offset_seconds(rng=None) -> int:
    """Random startup-jitter for ``bq-metadata-refresh``.

    Returns a value in ``[0, BQ_METADATA_INITIAL_OFFSET_MAX_SECONDS]``
    that the run loop uses to fake a recent ``last_run`` for the
    ``bq-metadata-refresh`` job at startup. With ``last_run = now - jitter``
    and the default 4 h interval, the first refresh fires
    ``interval - jitter`` seconds after the startup grace finishes
    (≈ 3 h 45 m to 4 h). This intentionally suppresses an immediate
    refresh on every container start — the app's own ``cache_warmup``
    already populates the persistent cache at startup, so a duplicate
    refresh from the scheduler would just compete for disk I/O while
    adding nothing.

    ``rng`` injectable for deterministic tests.
    """
    import random as _random

    cap = int(
        os.environ.get(
            "SCHEDULER_BQ_METADATA_INITIAL_OFFSET_MAX_SECONDS",
            "900",
        )
    )
    if cap <= 0:
        return 0
    r = rng or _random.Random()
    return r.randint(0, cap)


def _agent_schedules_enabled() -> bool:
    """Whether the ``agents:run-due`` sweep row is included in
    ``build_jobs()`` (agent-schedules design,
    docs/superpowers/specs/2026-08-17-agent-schedules-design.md).

    Default ON — a boolean toggle, not one of the interval-seconds knobs
    above, so it follows the codebase's existing default-on boolean-env
    convention (``AGNES_DB_SELF_HEAL``, ``EGRESS_BLOCK_PRIVATE``) rather
    than ``_read_positive_int``. Set ``SCHEDULER_AGENT_SCHEDULES=0`` (or
    ``false``/``no``) to disable the sweep on an instance that doesn't want
    scheduled agent runs firing.
    """
    return os.environ.get("SCHEDULER_AGENT_SCHEDULES", "1").strip().lower() not in ("0", "false", "no")


# Wave-2B job-queue migration (Task 6), plus ``ducklake-maintenance`` (wave-2G
# Task 5). These scheduler rows no longer run their work synchronously inside
# the scheduler's HTTP call — they POST
# an enqueue request to ``/api/jobs`` (see ``app/api/jobs.py``) and return
# immediately; a worker process picks the job up and does the actual work
# out-of-band. ``idempotency_key`` is a fixed per-kind string (not per-tick)
# so a second enqueue before the previous run finished is deduped by
# ``JobsRepository.enqueue`` instead of piling up duplicate queued rows.
# Full queued-vs-stays-HTTP classification of every scheduler row:
# docs/jobs-classification.md.
_ENQUEUE_BODIES: dict[str, dict[str, str]] = {
    "data-refresh": {"kind": "data-refresh", "idempotency_key": "sync"},
    "marketplaces": {"kind": "marketplaces-sync", "idempotency_key": "marketplaces-sync"},
    "session-collector": {"kind": "session-collector", "idempotency_key": "session-collector"},
    "corporate-memory": {"kind": "corporate-memory", "idempotency_key": "corporate-memory"},
    # wave-2G Task 5: DuckLake merge/expire/cleanup/VACUUM maintenance pass.
    # The handler itself (app/worker/kinds.py::_run_ducklake_maintenance)
    # no-ops when analytics.backend != "ducklake", so this row is enqueued
    # unconditionally — harmless on a legacy-backend instance.
    "ducklake-maintenance": {"kind": "ducklake-maintenance", "idempotency_key": "ducklake-maintenance"},
    # Jira organizations dimension. Daily rather than interval-driven: organization
    # names and detail fields change on a scale of weeks, and the refresh costs one
    # API request per organization. The handler no-ops when Jira is unconfigured, so
    # this row is harmless on an instance without Jira ingest.
    "jira-org-refresh": {"kind": "jira-org-refresh", "idempotency_key": "jira-org-refresh"},
}

# HTTP timeout for a ``/api/jobs`` enqueue call. Short on purpose: enqueueing
# just inserts a row and returns 202 — the work no longer runs inside this
# HTTP call, so there's no reason to hold the long timeouts (120s-900s) the
# old synchronous endpoints needed.
_ENQUEUE_TIMEOUT_SEC = 30

JobRow = tuple[str, str, str, str, int]
EnqueueJobRow = tuple[str, str, str, str, int, dict[str, str]]


def build_jobs() -> list[JobRow | EnqueueJobRow]:
    """Build the JOBS list from env, applying defaults and validation.

    Tuple shape: (name, schedule_string, endpoint, method, http_timeout_sec)
    for HTTP-executed jobs, with an optional 6th element
    (json_body: dict) for jobs that enqueue via ``POST /api/jobs`` instead
    of running synchronously — see ``_ENQUEUE_BODIES`` above.
    Marketplaces stays hardcoded — promoting it to env is out of #77 scope.
    """
    refresh = _read_positive_int("SCHEDULER_DATA_REFRESH_INTERVAL")
    health = _read_positive_int("SCHEDULER_HEALTH_CHECK_INTERVAL")
    scripts = _read_positive_int("SCHEDULER_SCRIPT_RUN_INTERVAL")
    sess = _read_positive_int("SCHEDULER_SESSION_COLLECTOR_INTERVAL")
    verify = _read_positive_int("SCHEDULER_VERIFICATION_DETECTOR_INTERVAL")
    usage = _read_positive_int("SCHEDULER_USAGE_PROCESSOR_INTERVAL")
    corpmem = _read_positive_int("SCHEDULER_CORPORATE_MEMORY_INTERVAL")
    bqmeta = _read_positive_int("SCHEDULER_BQ_METADATA_REFRESH_INTERVAL")
    kbsl = _read_positive_int("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL")
    dbxsl = _read_positive_int("SCHEDULER_DATABRICKS_SEMANTIC_LAYER_REFRESH_INTERVAL")
    semsrc = _read_positive_int("SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL")
    usageprune = _read_positive_int("SCHEDULER_USAGE_PRUNE_INTERVAL")
    jirasla = _read_positive_int("SCHEDULER_JIRA_SLA_POLL_INTERVAL")
    jiraconsis = _read_positive_int("SCHEDULER_JIRA_CONSISTENCY_INTERVAL")
    kpkg = _read_positive_int("SCHEDULER_KNOWLEDGE_PACKAGING_INTERVAL")
    kdig = _read_positive_int("SCHEDULER_KNOWLEDGE_DIGESTS_INTERVAL")
    reap = _read_positive_int("SCHEDULER_DATA_APP_REAP_INTERVAL")
    tick = _read_positive_int("SCHEDULER_TICK_SECONDS")
    smallest = min(
        refresh,
        health,
        scripts,
        sess,
        verify,
        usage,
        corpmem,
        bqmeta,
        kbsl,
        dbxsl,
        semsrc,
        usageprune,
        jirasla,
        jiraconsis,
        kpkg,
        kdig,
        reap,
    )
    if tick > smallest:
        raise ValueError(
            f"SCHEDULER_TICK_SECONDS={tick} must be <= the smallest job "
            f"interval ({smallest}s) so jobs don't consistently miss their "
            f"cadence by up to one tick"
        )
    jobs: list[JobRow | EnqueueJobRow] = [
        (
            "data-refresh",
            _seconds_to_schedule(refresh),
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["data-refresh"],
        ),
        ("health-check", _seconds_to_schedule(health), "/api/health", "GET", 30),
        ("script-runner", _seconds_to_schedule(scripts), "/api/scripts/run-due", "POST", 600),
        (
            "marketplaces",
            "daily 03:00",
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["marketplaces"],
        ),
        # Offset from every other daily row so none of them fire on the same tick:
        # `marketplaces` 03:00, `initial-workspace` 03:30, `store-blocked-purge` 04:00,
        # `ducklake-maintenance` 04:30. Sharing 04:30 with ducklake-maintenance was the
        # actual hazard, not a cosmetic one: both are LIGHT-lane and the worker runs
        # exactly `_LIGHT_CONCURRENCY = 2` light slots, so a minutes-long sweep next to
        # the maintenance pass occupies both and stalls `webhook-deliver` /
        # `agent_response` for the window (Devin Review on #1274). A fixed daily
        # schedule rather than an interval env var on purpose — an interval would have
        # to join the `smallest` min() above and constrain SCHEDULER_TICK_SECONDS for a
        # job that needs no precision at all.
        (
            "jira-org-refresh",
            "daily 05:00",
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["jira-org-refresh"],
        ),
        # LLM pipeline (#176, #179 review). Cadences are deliberately offset
        # (10m / 15m / 17m by default — all coprime modulo the 30s tick) so
        # the three LLM-driven jobs don't fire on the same tick and stack
        # their API + DB load. Driven by env so an operator can throttle
        # without a code change; the verification-detector cadence is the
        # single source of truth for the health-check staleness grace
        # window in app/api/health.py (which uses 2x the cadence).
        (
            "session-collector",
            _seconds_to_schedule(sess),
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["session-collector"],
        ),
        # session-pipeline processors — independent loops, each invoked on
        # its own cadence via the parametrized run-session-processor endpoint.
        # Adding a third processor in the future is one line here + one entry
        # in services/session_processors/__init__.py registry.
        (
            "session-processor:verification",
            _verification_schedule(verify),
            "/api/admin/run-session-processor?processor=verification",
            "POST",
            900,
        ),
        (
            "session-processor:usage",
            _seconds_to_schedule(usage),
            "/api/admin/run-session-processor?processor=usage",
            "POST",
            300,
        ),
        (
            "corporate-memory",
            _seconds_to_schedule(corpmem),
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["corporate-memory"],
        ),
        # v30: TTL purge of blocked-bundle bytes. Cheap (just rmtree
        # + UPDATE), runs once daily at 04:00 UTC so the spike is
        # visible in audit_log without competing with the marketplaces
        # job at 03:00. Endpoint reads guardrails.blocked_bundle_ttl_days
        # from instance.yaml and short-circuits when set to 0.
        ("store-blocked-purge", "daily 04:00", "/api/admin/run-blocked-purge", "POST", 600),
        # B8: retention-based audit_log pruning. Cheap (one indexed DELETE),
        # runs once daily at 05:30 UTC — offset from the other nightly rows
        # (marketplaces 03:00, initial-workspace 03:30, store-blocked-purge
        # 04:00, ducklake-maintenance 04:30, jira-org-refresh/store-lint-audit
        # 05:00) so none of them fire on the same tick. Endpoint reads
        # audit.retention_days from instance.yaml and short-circuits when
        # set to 0 (default 365). Short 60s timeout — a single DELETE.
        ("audit-prune", "daily 05:30", "/api/admin/run-audit-prune", "POST", 60),
        # Track E3 Slice 1: generalized per-trail retention sweep for
        # sync_history / llm_usage / agent_scope_snapshots — the other
        # unbounded trails alongside audit_log. Offset 15 min after
        # audit-prune so the two DELETE-only jobs never share a tick. Every
        # trail defaults to retention_days=0 (keep forever), so on a
        # freshly-installed instance this job runs and prunes nothing.
        # Endpoint reads retention.{sync_history,llm_usage,
        # agent_scope_snapshots}_days from instance.yaml. Short 60s
        # timeout — a handful of indexed DELETEs.
        ("retention-prune", "daily 05:45", "/api/admin/run-retention-prune", "POST", 60),
        # wave-2G Task 5: DuckLake merge_adjacent_files/expire_snapshots/
        # cleanup_old_files/VACUUM pass (app/worker/kinds.py::
        # _run_ducklake_maintenance). Offset 30 min after the 04:00 store
        # purge so the two nightly maintenance passes don't stack. LIGHT
        # lane, enqueued unconditionally — the handler itself no-ops when
        # analytics.backend != "ducklake" (see docs/jobs-classification.md).
        (
            "ducklake-maintenance",
            "daily 04:30",
            "/api/jobs",
            "POST",
            _ENQUEUE_TIMEOUT_SEC,
            _ENQUEUE_BODIES["ducklake-maintenance"],
        ),
        # Stuck-review reaper (#7). A submission stays at
        # status='pending_llm' until the BackgroundTasks worker writes
        # a verdict. If the worker crashes, the row sits forever. Run
        # every 15 minutes; reap_stuck_llm_reviews flips rows older
        # than guardrails.stuck_review_grace_seconds (default 1800)
        # to review_error so admin can retry. Cheap (one indexed
        # SELECT + N small UPDATEs); short timeout sufficient.
        ("store-reap-stuck-reviews", "every 15m", "/api/admin/run-reap-stuck-reviews", "POST", 60),
        # Semantic-layer auto-draft sweep (semantic-phase5 wave 2): drafts a
        # semantic model for a handful of uncovered tables per tick via a
        # headless chat session, landing each result in the
        # authoring_suggestions moderation queue. `every 55m` is the V0
        # cadence; no env override — one more scheduler knob is unwarranted
        # before this has run in production. 60s timeout is
        # this HTTP call's own client-side budget only: the endpoint stamps
        # each table's dedup flag before invoking its session, so a call
        # that outruns this timeout server-side is never re-triggered for
        # the same table on the next tick.
        ("semantic-auto-draft-sweep", "every 55m", "/api/admin/semantic-auto-draft-sweep", "POST", 60),
        # Weekly skill-lint retro-audit (#687). Re-lints published skills,
        # skipping entities whose content is unchanged since their last lint
        # (zero LLM cost on a static store). The endpoint self-guards against
        # restart-refire via a min-interval check on the last scheduler run,
        # so the effective cadence holds even though scheduler last_run state
        # is in-memory. Monday 05:00 UTC — off-peak, distinct from the daily
        # 03:00/04:00 store jobs.
        ("store-lint-audit", "cron 0 5 * * 1", "/api/admin/store/lint-audit", "POST", 900),
        # BigQuery metadata refresh — keeps ``bq_metadata_cache`` warm so
        # ``GET /api/v2/catalog`` never has to call BQ at request time.
        ("bq-metadata-refresh", _seconds_to_schedule(bqmeta), "/api/admin/run-bq-metadata-refresh", "POST", 1800),
        # Keboola semantic layer refresh — keeps metric_definitions rows
        # tagged source='keboola_semantic_layer' in sync with the project's
        # Metastore. Short-circuits (returns an error result, doesn't crash)
        # when Keboola credentials aren't configured or the token isn't a
        # master token — see connectors/keboola/semantic_layer.py.
        (
            "keboola-semantic-layer-refresh",
            _seconds_to_schedule(kbsl),
            "/api/admin/run-keboola-semantic-layer-refresh",
            "POST",
            900,
        ),
        # Databricks semantic layer refresh — syncs the workspace's Unity
        # Catalog metric views into the semantic layer through the same
        # semantic-source pipeline every other source uses (rows land
        # tagged source='ossie_connection', source_ref='databricks_default').
        # Answers 400 (doesn't crash the scheduler) when data_source.databricks
        # is not configured — see connectors/databricks/semantic_layer.py and
        # connectors/databricks/semantic_ossie.py.
        (
            "databricks-semantic-layer-refresh",
            _seconds_to_schedule(dbxsl),
            "/api/admin/run-databricks-semantic-layer-refresh",
            "POST",
            900,
        ),
        # Generic semantic-sources refresh — Block 3 step 2 of issue #1707.
        # The ONE scheduled sweep over every registered `semantic_sources`
        # row (git/upload/connection kinds), skipping `enabled=False` rows.
        # Runs ALONGSIDE the Keboola/Databricks rows above, not instead of
        # them: those legacy refreshes keep their own schedules and
        # provenance labels until a later, separately-sequenced migration
        # (#1707 steps 3-4) moves their callers onto this path and retires
        # them — see app/api/semantic_sources_refresh.py for the full
        # scope note and why scheduling both is safe today.
        (
            "semantic-sources-refresh",
            _seconds_to_schedule(semsrc),
            "/api/admin/run-semantic-sources-refresh",
            "POST",
            900,
        ),
        # Usage event retention pruning. Reads USAGE_EVENTS_RETENTION_DAYS
        # on the server side; short-circuits when unset or 0. Runs daily
        # (default 86400s). Rollup tables untouched.
        ("usage-prune", _seconds_to_schedule(usageprune), "/api/admin/usage/prune", "POST", 60),
        # Jira self-healing pair (parity with the legacy Data Broker
        # ``jira-sla-poll.timer`` and ``jira-consistency.timer`` systemd
        # units). Both endpoints short-circuit when the JIRA_* env vars
        # are not set, so a customer without Jira ingest pays nothing
        # for these scheduler entries.
        #
        # poll-sla: refreshes elapsed_millis + status on open tickets
        # whose snapshot would otherwise stagnate between webhooks.
        # consistency-check: compares Jira API ↔ raw JSON ↔ parquet and
        # backfills small gaps left behind by missed webhooks. The
        # ``--max-age-days 30`` default in the endpoint matches the
        # cadence: 30 min cadence × 30-day window keeps the cost
        # bounded while catching realistic drift.
        # 3600s rather than the 900s its cadence class would suggest: the
        # scheduler must not abandon a pass that is still running server-side.
        # _run_job's finally releases in_flight on a timeout as readily as on
        # success, and a sync handler is not cancelled when its client
        # disconnects — so a short timeout is precisely what stacked a second
        # pass onto the unfinished first. The cadence above keeps passes from
        # running back-to-back; this keeps them from running concurrently.
        ("jira-sla-poll", _seconds_to_schedule(jirasla), "/api/admin/run-jira-sla-poll", "POST", 3600),
        (
            "jira-consistency-check",
            _seconds_to_schedule(jiraconsis),
            "/api/admin/run-jira-consistency-check",
            "POST",
            1800,
        ),
        # K3 (#798): per-collection knowledge.duckdb artifact rebuild.
        # Fingerprint-only when nothing changed since the last pass.
        ("knowledge-packaging", _seconds_to_schedule(kpkg), "/api/admin/run-knowledge-packaging", "POST", 600),
        # K4 (#799): maintained-digest regeneration. Fingerprint-only when
        # idle; LLM call only when a digest's sources or instructions
        # changed. Longer 900s timeout — same job class as corporate-memory
        # (an LLM generation, not just a DuckDB rebuild).
        ("knowledge-digests", _seconds_to_schedule(kdig), "/api/admin/run-knowledge-digests", "POST", 900),
        # Data-app idle-reaper (Task 7+9): marks stale VMs for cleanup.
        # Lightweight operation that runs every 5m by default; the actual
        # VM cleanup runs out-of-band via jobs.
        ("data-app-idle-reaper", _seconds_to_schedule(reap), "/api/data-apps/reap-idle", "POST", 120),
    ]

    # Initial Workspace Template nightly auto-sync (#622 Slice 3 PR-B).
    # Endpoint self-gates: returns 200 {skipped:true} when no IWT repo is
    # registered, so this job is a no-op on instances without one. Default
    # 03:30 offsets from the marketplaces job (03:00) so the two nightly
    # git-clone bursts don't stack. Configurable via
    # SCHEDULER_INITIAL_WORKSPACE_SCHEDULE or instance.yaml
    # initial_workspace.sync_schedule; an explicitly cleared schedule
    # (_iw_sync_schedule() -> None) omits the job entirely. Inserted right
    # after marketplaces (index 4) to keep the nightly git-clone jobs grouped.
    iw_sched = _iw_sync_schedule()
    if iw_sched is not None:
        jobs.insert(
            4,
            ("initial-workspace", iw_sched, "/api/admin/initial-workspace/sync-if-configured", "POST", 900),
        )

    # Agent schedules run-due sweep (design doc
    # docs/superpowers/specs/2026-08-17-agent-schedules-design.md), modeled
    # on `script-runner` above. Fixed "every 1m" — not driven by an interval
    # env var, so (like `jira-org-refresh`'s fixed daily schedule) it
    # deliberately stays out of the `smallest`/tick-guard computation; a
    # coarser SCHEDULER_TICK_SECONDS only makes it fire less precisely on
    # the minute, never more often than configured.
    if _agent_schedules_enabled():
        jobs.append(("agents:run-due", "every 1m", "/api/v1/agents/run-due", "POST", 600))

    return jobs


_running = True


def _signal_handler(sig, frame):
    global _running
    logger.info(f"Received signal {sig}, shutting down...")
    _running = False


def _call_api(
    endpoint: str,
    method: str,
    timeout_sec: int,
    json_body: Optional[dict] = None,
) -> bool:
    """Call the main app API. Returns True on success.

    ``json_body`` is set for the jobs that enqueue via ``POST /api/jobs``
    instead of running synchronously (see ``_ENQUEUE_BODIES``); ``None``
    for every other job, which sends no request body exactly as before.
    """
    url = f"{API_URL}{endpoint}"
    headers = {}
    token = _get_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if method == "POST":
            resp = httpx.post(url, headers=headers, timeout=timeout_sec, json=json_body)
        else:
            resp = httpx.get(url, headers=headers, timeout=timeout_sec)
        if resp.status_code < 400:
            logger.info(f"Job {endpoint}: {resp.status_code}")
            return True
        else:
            logger.warning(f"Job {endpoint}: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Job {endpoint} failed: {e}")
        try:
            from src.observability import get_posthog

            get_posthog().capture_exception(
                e,
                distinct_id="system",
                properties={"job": endpoint, "method": method, "component": "scheduler"},
            )
        except Exception:
            logger.exception("PostHog capture_exception failed in scheduler")
        return False


def run():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    jobs = build_jobs()
    tick = resolved_tick_seconds()
    grace = resolved_startup_grace_seconds()
    bqmeta_offset = resolved_bq_metadata_initial_offset_seconds()
    logger.info(
        "Scheduler started. API_URL=%s, %d jobs, tick=%ds, "
        "startup_grace=%ds, bq_metadata_initial_offset=%ds. Schedules: %s",
        API_URL,
        len(jobs),
        tick,
        grace,
        bqmeta_offset,
        {name: schedule for name, schedule, *_ in jobs},
    )

    # Startup grace — see ``resolved_startup_grace_seconds`` for why.
    # Honors SIGTERM by polling _running in short slices, so an operator
    # `docker compose stop` during grace doesn't hang for ~60s.
    grace_remaining = grace
    while grace_remaining > 0 and _running:
        time.sleep(min(grace_remaining, 5))
        grace_remaining -= 5
    if not _running:
        logger.info("Scheduler shutdown during startup grace; exiting.")
        return

    last_run: dict[str, str | None] = _load_last_run({name for name, *_ in jobs})

    # Suppress the first ``bq-metadata-refresh`` fire by pretending the
    # job ran ``bqmeta_offset`` seconds ago at startup. ``is_table_due``
    # will then wait the remainder of the configured interval before
    # firing for the first time. Two scheduler containers that came up
    # within seconds of each other will pick different offsets and stop
    # synchronising their refresh ticks against one another.
    # Only synthesize the offset when there's no DURABLE last_run for this
    # job — a restored real timestamp already provides the anti-synchronize
    # suppression, and overwriting it would discard the persisted catch-up
    # state this fix exists to preserve.
    if bqmeta_offset > 0 and last_run.get("bq-metadata-refresh") is None:
        from datetime import timedelta as _td

        offset_ago = (datetime.now(timezone.utc) - _td(seconds=bqmeta_offset)).isoformat()
        last_run["bq-metadata-refresh"] = offset_ago

    # Per-tick concurrency: one thread per job slot, so a 900s verification
    # run can't block the 60s health-check or the 30s data-refresh from
    # firing on their own cadences (PR #232 review fix). Pure I/O workload
    # (httpx) — GIL is irrelevant. `in_flight` prevents the same job being
    # re-launched on a subsequent tick while the previous invocation is
    # still running; otherwise a 10-min run during which 20 ticks fire
    # would queue 20 duplicate POSTs against the same processor (the
    # admin endpoint's per-processor lock would 409 most of them, but
    # they'd still be wasted requests + audit-log noise).
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=max(4, len(jobs)))

    while _running:
        now_iso = datetime.now(timezone.utc).isoformat()
        for job in jobs:
            # Most rows are a 5-tuple; the four enqueue-migrated jobs
            # (see _ENQUEUE_BODIES) carry a 6th json_body element.
            name, schedule, endpoint, method, timeout_sec = job[:5]
            json_body = job[5] if len(job) > 5 else None
            if not is_table_due(schedule, last_run[name]):
                continue
            with in_flight_lock:
                if name in in_flight:
                    # Previous tick's invocation hasn't returned yet; skip.
                    continue
                in_flight.add(name)
            logger.info("Running job: %s (%s)", name, schedule)
            executor.submit(
                _run_job,
                name,
                endpoint,
                method,
                timeout_sec,
                now_iso,
                last_run,
                in_flight,
                in_flight_lock,
                json_body,
            )
        time.sleep(tick)

    logger.info("Scheduler stopping; waiting for in-flight jobs.")
    executor.shutdown(wait=True)
    logger.info("Scheduler stopped.")


def _run_job(
    name: str,
    endpoint: str,
    method: str,
    timeout: int,
    now_iso: str,
    last_run: dict[str, "str | None"],
    in_flight: set[str],
    in_flight_lock: threading.Lock,
    json_body: Optional[dict] = None,
) -> None:
    """Execute one scheduled job + bookkeeping. Lifted out of run() so it's
    unit-testable.

    Advances last_run on terminal state (success OR failure) so a permanently
    failing job retries on its cadence (e.g. 15 min), not on every scheduler
    tick (default 30s). Pre-fix behavior caused a hot-loop on persistent 5xx —
    30× more requests + LLM tokens than the operator configured. Errors still
    surface via _call_api's logging + audit_log on the receiving side.

    ``json_body`` (default None) is forwarded to ``_call_api`` unchanged —
    set only for the four enqueue-migrated jobs (see ``_ENQUEUE_BODIES``).
    """
    try:
        _call_api(endpoint, method, timeout, json_body=json_body)
    finally:
        last_run[name] = now_iso
        # Persist so this run survives a scheduler restart/recreate (see
        # _LAST_RUN_PATH). Best-effort — never blocks the tick loop's
        # bookkeeping on a disk error.
        _persist_last_run(last_run)
        with in_flight_lock:
            in_flight.discard(name)


if __name__ == "__main__":
    run()
