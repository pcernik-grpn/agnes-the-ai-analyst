"""Real job kinds for the worker runtime (wave-2B, spec §3.3 — Task 4;
``ducklake-maintenance`` added in wave-2G Task 5; ``analytics-migrate``
added in wave-2G Task 6; ``distribution-mirror`` added in wave-2H Task
WF-3 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

``register_all_kinds()`` registers the ten kinds the scheduler's
current HTTP-driven jobs (plus the analytics migrate command, the
distribution mirror, and the api-role write conversions) map onto:

- ``data-refresh``       (HEAVY) — wraps ``app.api.sync._run_sync``, the
  body behind ``POST /api/sync/trigger``.
- ``marketplaces-sync``  (LIGHT) — wraps ``src.marketplace.sync_marketplaces``,
  the body behind ``POST /api/marketplaces/sync-all``.
- ``session-collector``  (LIGHT) — wraps ``services.session_collector.collector.run``,
  the body behind ``POST /api/admin/run-session-collector``.
- ``corporate-memory``   (LIGHT) — wraps ``services.corporate_memory.collector.collect_all``,
  the body behind ``POST /api/admin/run-corporate-memory``.
- ``jira-refresh``       (HEAVY) — wraps ``SyncOrchestrator().rebuild_source("jira")``,
  previously called inline from the Jira webhook's incremental-transform
  path (``connectors/jira/service.py:trigger_incremental_transform``).
- ``ducklake-maintenance`` (LIGHT) — runs the POC-verified DuckLake
  maintenance sequence (merge → expire snapshots → cleanup old files →
  catalog VACUUM) on the writer session. No-ops when
  ``analytics.backend`` is not ``ducklake`` — see
  ``_run_ducklake_maintenance`` below.
- ``analytics-migrate``  (HEAVY) — wraps
  ``SyncOrchestrator().migrate_to_backend(to)``, the body behind
  ``POST /api/admin/analytics/migrate``. Admin-triggered only (no
  scheduler row) — see ``_run_analytics_migrate`` below.
- ``distribution-mirror`` (LIGHT) — mirrors every downloadable local/
  materialized parquet whose ``sync_state.hash`` differs from the
  configured object store's stamped metadata, then writes a marker index
  of what's currently mirrored. No-ops (clean, no ``boto3`` import) when
  ``src.object_store.object_store()`` returns ``None`` — signed-URL
  distribution off or no store configured — see
  ``_run_distribution_mirror`` below. Enqueued automatically after a
  successful ``data-refresh`` (see ``_maybe_enqueue_distribution_mirror``);
  no scheduler row — event-chained, not cron.
- ``analytics-rebuild``  (HEAVY) — wraps ``app.api.admin._materialize_bigquery_extract``
  (BQ extract rebuild + master views); enqueued by admin register-table /
  registry-rebuild / BQ-row-update endpoints when the process lacks the
  worker role (three-plane §3.1: api plane is analytics-write-free).
- ``collections-purge``  (HEAVY) — wraps the derived-table purge helpers in
  ``app.api.collections`` (extract.duckdb surgery + ``rebuild_source``);
  enqueued by collection/file delete endpoints when the process lacks the
  worker role. Single-box ``all`` deployments never enqueue either kind —
  the original synchronous/BackgroundTask paths are unchanged there.
- ``agent_response``     (LIGHT) — Task 9's background/sync-timeout-degrade
  path for ``POST /api/v1/agents/{slug}/responses``. Enqueued by
  ``app/api/agent_runtime.py``, and since v120 also by the agent-schedules
  sweep (``app/api/agent_schedules.py`` — scheduler-driven, always
  ``mode="fresh"`` under the agent owner's identity, with a backlog guard
  so a topology where this kind never registers can't stack queued jobs).
  Two ``payload["mode"]`` shapes:

  - ``"fresh"`` — ``background: true`` was requested up front. Runs
    ``app.chat.headless.run_one_shot`` (fresh session, sends the prompt).
  - ``"continue"`` — a SYNC call's wait hit ``timeout_s`` (the run itself
    was never killed — only the wait was bounded). Runs
    ``app.chat.headless.await_completion`` against the ALREADY-RUNNING
    ``payload["chat_id"]`` — no prompt is resent.

  Unlike every other kind here, the handler RETURNS a result dict
  (``{"answer", "session_id", "usage", "timed_out"}``) instead of
  ``None`` — ``app/worker/runtime.py``'s ``_run_one``/``_drain_in_flight``
  pass a handler's return value straight through to
  ``jobs_repo().complete(..., result=...)``, which merges it into
  ``payload_json["result"]`` (see that method's docstring for why: the
  ``jobs`` table has no dedicated result column, and adding one was judged
  out of scope for this task versus reusing the existing JSON payload
  field). ``GET /api/v1/jobs/{id}`` (``app/api/agent_runtime.py``) reads it
  from there.

  Runs on the SAME event loop the live ``ChatManager`` singleton
  (``app.chat.manager.get_current_chat_manager``) is bound to — via
  ``asyncio.run_coroutine_threadsafe`` against
  ``app.chat.manager.get_current_chat_loop()`` — because the worker
  runtime executes every handler synchronously in a thread
  (``asyncio.to_thread``, see ``app/worker/runtime.py``), and
  ``ChatManager``'s internal locks/tasks/sinks are asyncio primitives tied
  to whichever loop created them; calling its async methods from a
  DIFFERENT loop (e.g. one built fresh in the worker thread via
  ``asyncio.run``) would break those primitives across loops. No-ops
  (raises, so the job fails and retries) when chat is disabled on this
  process — see ``_run_agent_response`` below.
- ``webhook-deliver``     (LIGHT) — V1b Task 6's SSRF-hardened, HMAC-signed
  outbound webhook delivery. Enqueued by
  ``app.chat.webhook_delivery.enqueue_job_event_webhooks`` (called from
  ``app/worker/runtime.py`` when an ``agent_response`` job reaches
  ``completed``/``failed``), one job per active webhook subscribed to that
  event. The handler resolves the webhook row by id and calls
  ``app.chat.webhook_delivery.deliver`` — see that module's docstring for
  the resolve-and-pin SSRF guard and the notification-not-answer payload
  contract. Registered UNCONDITIONALLY (unlike ``agent_response`` above) —
  it's a plain outbound HTTP POST with no dependency on the live
  ``ChatManager``/chat event loop, so it runs fine on a worker-only,
  gateway-less process too.
- ``corpus-extraction``    (EXTRACTION — its own lane, spec §7.5 / §16
  step 7 of docs/superpowers/specs/2026-08-27-fact-graph-over-collections-
  design.md) — the producer-invocation SEAM for document extraction. Off
  by default (``extraction.enabled: false``, ``config/instance.yaml
  .example``). Unlike every other handler in this module, this one is
  NOT a thin adapter over an in-process function: it resolves this
  connection's SharePoint/tenant credentials from config/vault
  (``connectors.sharepoint.settings.resolve_sharepoint_settings`` — the
  SAME resolution path the SharePoint admin UI uses) and shells out to
  the operator-configured producer (crawl -> convert -> anonymize ->
  extract -> ingest against ``POST /api/facts/ingest``, spec §7.2) as a
  subprocess, under a bounded timeout, with the resolved credentials passed
  via the CHILD PROCESS ENVIRONMENT — never argv, never logged (security
  playbook F7). That child env is NOT the full parent environment: only a
  curated non-secret allowlist (+ any operator-opted-in
  ``extraction.producer.env_passthrough``) plus the named SharePoint
  credentials and the corpus id are forwarded — see
  ``_EXTRACTION_PRODUCER_ENV_ALLOWLIST``. No other instance secret ever
  reaches this subprocess. The producer itself
  is a separate project the operator supplies, adopted rather than
  ported into this repo (spec §1 "Out of scope") — see
  ``_run_corpus_extraction`` below for exactly where that boundary is.
  Registered UNCONDITIONALLY (its own no-op guard on
  ``extraction.enabled`` makes an accidental claim on a process that
  never opted into the ``extraction`` lane harmless, mirroring
  ``webhook-deliver``'s posture above) but only ever CLAIMED by a lane
  slot that opted into ``AGNES_WORKER_LANES=extraction`` — see
  ``app/worker/runtime.py``'s ``selected_lanes()``.

Every handler below is a THIN ADAPTER — it imports and calls the existing
function/method and does not reimplement any of its logic — EXCEPT
``corpus-extraction``, whose "existing function" is an external subprocess
rather than an in-process call; see its own docstring for where the seam
sits. Each import is deferred (inside the handler, not at module import
time) for the same
reason ``app/worker/runtime.py``'s ``_jobs_repo()`` and
``_sweep_stale_scratch()`` defer theirs: this module must not carry an
import-time dependency on heavyweight subsystems (LLM clients, the
DuckDB/BigQuery extractor stack, marketplace git plumbing) that may not
be configured on every process that imports ``app.worker.kinds`` (e.g. a
test importing just the registry), and so tests can monkeypatch the
target module attribute freely without this module having already bound
a stale reference to it at import time.

Called once from ``app/main.py``'s lifespan, before the worker loop task
is created (see the comment there) — registration is idempotent
(``register_kind`` replaces any existing entry by name), so calling it
more than once (e.g. across re-imports in a test process) is harmless.

Lease/retry tuning:

- ``data-refresh`` gets the longest lease (``AGNES_DATA_REFRESH_LEASE_S``,
  default 900s / 15min) — a full Keboola extractor subprocess run +
  materialized pass + orchestrator rebuild can legitimately take that
  long on a large registry; the worker's heartbeat keeps the lease alive
  every ``lease_seconds/3`` while the handler thread runs, so this is a
  ceiling on "how long before a crashed/stuck run is reclaimed", not a
  hard timeout on the sync itself.
- ``jira-refresh`` is also HEAVY (shares the lane with ``data-refresh``,
  and both run through ``_sweep_stale_scratch()`` before every HEAVY
  claim — see ``app/worker/runtime.py``) but is a plain orchestrator
  rebuild (re-ATTACH + view creation over already-written parquet), so a
  much shorter lease (300s) is plenty.
- The LIGHT kinds (``marketplaces-sync``, ``session-collector``,
  ``corporate-memory``) default to 300s — bulk git clones / LLM catalog
  refresh / filesystem walks, but bounded by their own internal
  timeouts, not multi-minute by design.
- ``corpus-extraction``'s lease tracks its own ``extraction.timeout_s``
  config (default 3600s) plus a margin — the producer subprocess is
  killed at that timeout regardless (``subprocess.run(..., timeout=...)``),
  so the lease only has to outlast it long enough for the timeout itself
  to fire and finalize the job, same "generous ceiling, not the actual
  bound" reasoning as ``data-refresh`` above. No retry by default: a
  failed producer run (bad credentials, crawl error, timeout) usually
  needs an operator to look at it, not an automatic re-run against the
  same corpus a few minutes later.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from typing import Optional

from app.worker.registry import JOB_KINDS, EXTRACTION_LANE, HEAVY_LANE, LIGHT_LANE, JobKind, register_kind
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

_DEFAULT_DATA_REFRESH_LEASE_S = 900
_DEFAULT_JIRA_REFRESH_LEASE_S = 300
# One API request per organization, gently paced — a few-hundred-organization site
# takes minutes, so the lease has to outlast the whole sweep or the job would be
# reclaimed mid-run and start over. At ~0.2s pacing plus request latency this covers
# roughly 3,500 organizations; an estate materially larger than that wants a
# size-derived lease rather than a bigger constant, or it will reclaim in a loop.
_DEFAULT_JIRA_ORG_REFRESH_LEASE_S = 1800
_DEFAULT_LIGHT_LEASE_S = 300
# merge_adjacent_files/expire_snapshots/cleanup_old_files/VACUUM can each
# take a while over a large lake — same "generous ceiling, not a hard
# timeout" reasoning as _DEFAULT_DATA_REFRESH_LEASE_S (the worker's
# heartbeat keeps the lease alive every lease_seconds/3 while the handler
# thread runs).
_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S = 900
# Hard ceiling on one producer subprocess run (extraction.timeout_s in
# instance.yaml overrides this) — a full crawl+convert+anonymize+extract
# pass over a real SharePoint site can legitimately run for a while.
_DEFAULT_EXTRACTION_TIMEOUT_S = 3600
# The job's own lease outlives the subprocess timeout by a margin so a
# heartbeat tick never expires the lease out from under a still-running
# (not-yet-timed-out) producer call — same pattern as _agent_response_job
# _timeout_seconds()'s lease_seconds below.
_EXTRACTION_LEASE_MARGIN_S = 120

# Non-secret operational env vars forwarded to the producer subprocess from
# THIS process's own environment, when present. Deliberately a NARROW
# allowlist, never `{**os.environ}`: `extraction.producer.command`/`.module`
# names an EXTERNAL, admin-configurable binary — unlike the in-repo Keboola
# extractor subprocess `app/api/sync.py` spawns (which legitimately inherits
# the full parent env because it IS this codebase, reviewed and trusted the
# same way the rest of the process is), a producer an admin can point
# anywhere must not receive this instance's secrets (JWT_SECRET_KEY,
# AGNES_VAULT_KEY, ANTHROPIC_API_KEY, POSTGRES_PASSWORD/DATABASE_URL,
# SLACK_BOT_TOKEN, KEBOOLA_STORAGE_TOKEN, ...) just because they happen to
# sit in os.environ. Only what a well-behaved subprocess needs to run at
# all (PATH), plus locale/timezone/tempdir/TLS/proxy settings — nothing an
# attacker (or a merely careless producer) could exfiltrate for profit.
_EXTRACTION_PRODUCER_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _data_refresh_lease_seconds() -> int:
    raw = os.environ.get("AGNES_DATA_REFRESH_LEASE_S")
    if raw is None:
        return _DEFAULT_DATA_REFRESH_LEASE_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_DATA_REFRESH_LEASE_S


def _run_data_refresh(payload: dict) -> None:
    """Wrap ``app.api.sync._run_sync`` — same defaults as the HTTP trigger
    path (``tables=None`` syncs every registered table). ``payload`` may
    carry ``tables`` (list[str]) and/or ``source`` (source_type filter),
    mirroring ``POST /api/sync/trigger``'s body/`` ?source=`` params, but
    an empty payload (the scheduler's normal enqueue) behaves identically
    to the old unfiltered trigger.

    ``_run_sync`` itself acquires the module-level ``_sync_lock`` and
    fast-returns if another sync is already in flight (see its
    docstring). That fast-fail is harmless here too: the worker's HEAVY
    lane already runs at concurrency 1, so within a single worker process
    two ``data-refresh`` jobs can never be mid-handler simultaneously.
    ``_sync_lock`` is a plain ``threading.Lock`` — it is invisible across
    processes, so it does NOT guard against the legacy HTTP trigger path
    running in a separate ``api`` process (or a second worker process)
    racing this one. Cross-process serialization of the actual rebuild
    critical section is handled independently, inside
    ``SyncOrchestrator.rebuild()``/``rebuild_source()``, via
    ``src.db_pg.rebuild_lease()`` (a Postgres advisory lock; no-op on the
    DuckDB backend, where a single-process startup guard already applies).

    Job-outcome honesty (wave-2B review carry-over, W2B-4/7): ``_run_sync``
    used to swallow every failure internally (log + best-effort webhook
    notify) and return nothing, so a ``data-refresh`` job always finalized
    ``'done'`` even when the underlying sync failed outright or partially
    — ``GET /api/jobs/{id}`` had no way to show it, and the job's
    retry-on-failure semantics (``retry_in_seconds=300`` below) never
    engaged. ``_run_sync`` now returns ``True`` (clean run), ``False``
    (fatal exception or any per-table failure), or ``None`` (this call
    was a no-op — another same-process invocation already held
    ``_sync_lock``, not a failure of this job). Only ``False`` raises —
    the worker's lane-slot handler (``app/worker/runtime.py``) turns an
    uncaught exception into ``jobs_repo().fail(..., retry_in_seconds=...)``,
    so this is the sole mechanism needed for the job to record `failed`
    and retry.
    """
    from app.api.sync import _run_sync

    ok = _run_sync(payload.get("tables"), payload.get("source"))
    if ok is False:
        raise RuntimeError("data-refresh sync failed — see server logs and sync_state for per-table errors")
    if ok:
        # `ok is True` here (the `False` branch above already raised) — a
        # real sync just completed in THIS call, so the extracts tree is
        # settled and safe to mirror. `ok is None` (another same-process
        # `_run_sync` held the lock) is deliberately excluded: that means a
        # sync may still be in flight elsewhere, and mirroring now could
        # read a half-written parquet.
        _maybe_enqueue_distribution_mirror()


def _run_analytics_rebuild(payload: dict) -> None:
    """BQ extract + master-view rebuild, enqueued by api-role admin endpoints.

    Three-plane §3.1: the api plane must not write analytics in-process. On a
    role-split deployment `POST /api/admin/register-table` / `/registry/rebuild`
    (and BQ-row updates) enqueue this kind instead of running the rebuild in a
    FastAPI BackgroundTask; single-box ``all`` deployments keep the original
    synchronous/BackgroundTask path and never enqueue it. Payload is empty —
    the rebuild is registry-wide by design (same body as the BackgroundTask
    wrapper it replaces).

    Lazy import from ``app.api.admin`` mirrors ``_run_data_refresh``'s import
    of ``app.api.sync._run_sync`` — the handler bodies live next to their HTTP
    siblings so the two invocation paths can't drift.
    """
    from app.api.admin import _materialize_bigquery_extract

    result = _materialize_bigquery_extract() or {}
    errors = result.get("errors") or []
    if errors:
        raise RuntimeError(f"analytics-rebuild surfaced {len(errors)} error(s); first: {errors[:3]}")


def _run_collections_purge(payload: dict) -> None:
    """Derived-table purge for a deleted/reingested collection/file
    (extract.duckdb surgery + ``rebuild_source``), enqueued by api-role
    collection deletes and reingests.

    ``payload``: ``corpus_id`` (required), ``file_id`` (optional — present for
    a single-file delete or reingest, absent for a whole-collection delete),
    ``reingest_after_purge`` (optional, only set alongside ``file_id`` by
    ``reingest_file``) — when true, ``ingest_file`` runs immediately after the
    purge, in this same job, so the purge always completes before the
    re-ingest starts. Decoupling them (enqueue purge, schedule ingest
    separately) would let the purge land *after* the re-ingest and delete the
    freshly rebuilt table — same §3.1 rationale and single-box behavior as
    ``_run_analytics_rebuild``.
    """
    from app.api.collections import (
        _purge_derived_tabular_row_for_file,
        _purge_derived_tabular_rows,
    )

    corpus_id = payload["corpus_id"]
    file_id = payload.get("file_id")
    if file_id:
        _purge_derived_tabular_row_for_file(corpus_id, file_id)
    else:
        _purge_derived_tabular_rows(corpus_id)

    if payload.get("reingest_after_purge") and file_id:
        from src.ingest.runner import ingest_file

        ingest_file(file_id)


def _maybe_enqueue_distribution_mirror() -> None:
    """Enqueue a ``distribution-mirror`` job after a successful
    ``data-refresh`` (wave-2H WF-3) — but only when signed-URL distribution
    is actually configured. Legacy/no-store instances must never accumulate
    ``distribution-mirror`` rows in the ``jobs`` table for nothing; checking
    ``object_store()`` here (not just relying on the handler's own no-op
    guard) keeps the queue clean on every sync for the common case.

    Mirrors the Jira webhook's enqueue-and-log-on-failure shape
    (``connectors/jira/service.py::trigger_incremental_transform``):
    best-effort, a failure to enqueue must never fail the ``data-refresh``
    job that already succeeded.
    """
    from src.object_store import object_store

    if object_store() is None:
        return
    try:
        from src.repositories import jobs_repo

        jobs_repo().enqueue("distribution-mirror", {}, idempotency_key="distribution-mirror")
    except Exception:
        logger.warning("distribution-mirror: failed to enqueue follow-up job", exc_info=True)


def _run_marketplaces_sync(payload: dict) -> None:
    """Wrap ``src.marketplace.sync_marketplaces`` — the body behind
    ``POST /api/marketplaces/sync-all``. No payload fields are consumed;
    it always syncs every registered (non-builtin) marketplace, same as
    the HTTP endpoint."""
    from src.marketplace import sync_marketplaces

    sync_marketplaces()


def _run_session_collector(payload: dict) -> None:
    """Wrap ``services.session_collector.collector.run`` — the body
    behind ``POST /api/admin/run-session-collector``. Called with the
    same ``dry_run=False, verbose=False`` defaults as that endpoint."""
    from services.session_collector import collector

    collector.run(dry_run=False, verbose=False)


def _run_corporate_memory(payload: dict) -> None:
    """Wrap ``services.corporate_memory.collector.collect_all`` — the
    body behind ``POST /api/admin/run-corporate-memory``. Called with
    the same ``dry_run=False`` default as that endpoint."""
    from services.corporate_memory.collector import collect_all

    collect_all(dry_run=False)


def _run_jira_refresh(payload: dict) -> None:
    """Refresh Jira's ``_meta``, then rebuild the source's master views.

    Wraps ``SyncOrchestrator().rebuild_source("jira")`` — previously called
    inline from ``connectors/jira/service.py``'s
    ``trigger_incremental_transform`` after every webhook-driven incremental
    parquet transform. Now enqueued instead (see that module), deduped via the
    ``"jira-refresh"`` idempotency key so a burst of webhook events collapses
    into a single rebuild.

    The ``update_meta`` pass moved here from the per-event transform. Two
    reasons, one of them a live incident:

    * **Correctness.** ``update_meta`` opens ``extract.duckdb`` for writing while
      the rebuild ATTACHes the same file, and DuckDB is single-writer. Losing
      that ATTACH is only logged, and the rebuild then swaps in a freshly built
      analytics DB with no Jira views — so the tables disappear until some later
      rebuild wins. Running both here puts them in one sequential process
      instead of racing across a burst.
    * **Cost.** Per event it was a write-open plus a full count over every
      partition of all six tables. Per coalesced rebuild it is once.

    Doing it *before* the rebuild matters on a fresh install: ``update_meta``
    creates ``extract.duckdb`` when it is missing, and ``rebuild_source``
    returns early when there is no such file to attach.

    Data freshness does not depend on any of this — the views inside
    ``extract.duckdb`` glob the parquet per query, so a written partition is
    served immediately. ``_meta`` holds the catalog's row/size numbers only.
    """
    from connectors.jira.extract_init import JIRA_TABLES, get_default_output_dir, update_meta
    from src.orchestrator import SyncOrchestrator, rebuild_mutex

    try:
        extract_dir = get_default_output_dir()
        # Under the same mutex `rebuild()`/`rebuild_source()` take — the pattern
        # `_run_ducklake_maintenance` below already follows. Running in the HEAVY
        # lane (concurrency 1) serialises this against the rebuild on the next
        # line, but NOT against the other processes that rebuild: API startup,
        # the admin and sync endpoints, collections, tabular ingest. Those ATTACH
        # this same file, and DuckDB is single-writer, so without the mutex a
        # rebuild elsewhere can still collide with this loop.
        #
        # Held around the loop ONLY. `rebuild_source` acquires the same mutex
        # itself, so keeping it across that call would deadlock.
        with rebuild_mutex():
            for table_name in JIRA_TABLES:
                update_meta(extract_dir, table_name)
    except Exception as meta_err:
        # Non-fatal, exactly as it was on the per-event path: stale catalog
        # numbers must not cost us the rebuild that publishes the data.
        logger.warning(f"Could not update Jira extract.duckdb _meta: {meta_err}")

    SyncOrchestrator().rebuild_source("jira")


def _run_jira_org_refresh(payload: dict) -> None:
    """Rebuild the Jira ``organizations`` dimension from the organization API.

    Resolves the organization ids that ``issues.organization_ids`` carries to a
    current name plus whichever organization detail fields the operator configured
    (``JIRA_ORG_DETAIL_FIELDS``) — the join path from a ticket to whatever those
    details point at, without matching on organization names, which drift on rename.

    Enqueued by the scheduler on a daily cadence, not per webhook: organization
    membership and details change on a scale of weeks, and the refresh costs one API
    request per organization because the CSM API exposes no bulk read that returns
    detail *ids* (see ``JiraService.fetch_organization``). A day-stale name is
    immaterial; a per-event refresh would spend hundreds of requests to learn nothing.

    This handler triggers no rebuild itself, but ``refresh_organizations`` enqueues a
    coalesced ``jira-refresh`` after a successful write, and that enqueue is
    load-bearing rather than housekeeping: ``_attach_and_create_views`` skips any
    ``_meta`` row whose inner object did not exist when it ran, so on the first
    refresh the table would otherwise stay invisible in the master database. It also
    refreshes this table's ``_meta`` row (under ``rebuild_mutex()``) for the catalog's
    row/size numbers. Failures propagate so the job retries rather than silently
    leaving the dimension stale.
    """
    from connectors.jira.organizations import FAILURE_REASONS, refresh_organizations

    stats = refresh_organizations()
    reason = stats.get("skipped_reason")

    # A run that published nothing it should have must not finalize `done`. Raising puts
    # the refusal in job history and gets the job retried; returning quietly meant a
    # total outage — or the mass-removal guard, which deliberately never self-clears —
    # looked like a healthy nightly run indefinitely, with one ERROR log line as the only
    # signal (Devin Review on #1274). `FAILURE_REASONS` is shared with the CLI so the two
    # surfaces cannot drift on what counts as failure.
    if reason in FAILURE_REASONS:
        raise RuntimeError(
            f"Jira organization refresh did not publish: {reason} "
            f"({stats.get('written')} written, {stats.get('preserved')} preserved, "
            f"{stats.get('removed')} removed, {stats.get('failed')} failed)"
        )

    if reason:
        logger.info(f"Jira organization refresh skipped: {reason}")
        return

    logger.info(
        "Jira organization refresh: %s written, %s preserved, %s removed, %s failed",
        stats.get("written"),
        stats.get("preserved"),
        stats.get("removed"),
        stats.get("failed"),
    )


def _ducklake_expire_older_than_sql(retention_days: int) -> str:
    """Build the ``older_than => ...`` argument for ``ducklake_expire_snapshots``,
    enforcing :func:`src.analytics_backend.ducklake_min_retention_floor_seconds`
    as an absolute safety floor.

    ``ducklake_snapshot_retention_days()`` deliberately allows ``0`` ("no
    retention grace" — see its docstring), but ``0`` with no further
    guardrail would let this job expire a snapshot a live analyst query is
    still reading from: there is no hard statement timeout on local
    DuckLake queries (nothing in this codebase caps how long
    ``agnes query`` / ``/api/query`` can run), so a long-running query
    holding a reference to "the current snapshot at the time it started"
    must not have that snapshot pulled out from under it mid-query.

    ``retention_days * 86400`` is compared against the floor in seconds;
    whenever the configured retention is below the floor (in practice only
    ``retention_days == 0``, since any ``retention_days >= 1`` is already
    ``86400s >= `` the 3600s default floor), the clamped floor value is
    used instead — expressed in seconds (not days) so the clamp doesn't
    round down to zero days again. A warning is logged so an operator who
    intentionally configured aggressive reclamation knows why the actual
    cutoff differs from what they set.
    """
    from src.analytics_backend import ducklake_min_retention_floor_seconds

    floor_seconds = ducklake_min_retention_floor_seconds()
    retention_seconds = retention_days * 86400
    if retention_seconds < floor_seconds:
        logger.warning(
            "ducklake-maintenance: configured snapshot_retention_days=%d (%ds) is below the "
            "%ds safety floor (max plausible in-flight analytic query duration + margin) — "
            "clamping older_than to now() - %ds so an active reader's held snapshot is never "
            "expired out from under it",
            retention_days,
            retention_seconds,
            floor_seconds,
            floor_seconds,
        )
        return f"now() - INTERVAL '{floor_seconds} seconds'"
    # retention_days is always a non-negative int (validated by
    # ducklake_snapshot_retention_days()) — safe to interpolate directly
    # into the INTERVAL literal.
    return f"now() - INTERVAL '{retention_days} days'"


def _record_ducklake_snapshot_age(conn) -> None:
    """Populate ``agnes_ducklake_snapshot_age_seconds`` (spec §3.7) from
    the newest ``snapshot_time`` in ``ducklake_snapshots('lake')``, using
    the writer connection the maintenance CALLs just ran on.

    Best-effort: any failure (extension quirk, empty lake, a fake/spy
    connection in a test that doesn't implement ``fetchone``) is logged
    and swallowed — a metrics read must never fail the maintenance job.
    Naive timestamps (never observed against the real ``ducklake``
    extension, but defensive) are treated as UTC, mirroring
    ``_QueuedJobsCollector``'s same normalization in
    ``app/observability/metrics.py``.
    """
    try:
        from datetime import datetime, timezone

        from app.observability.metrics import record_ducklake_snapshot_age

        row = conn.execute("SELECT max(snapshot_time) FROM ducklake_snapshots('lake')").fetchone()
        if row is None or row[0] is None:
            return
        snapshot_time = row[0]
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
        else:
            snapshot_time = snapshot_time.astimezone(timezone.utc)
        age_seconds = max((datetime.now(timezone.utc) - snapshot_time).total_seconds(), 0.0)
        record_ducklake_snapshot_age(age_seconds)
    except Exception:
        logger.exception("ducklake-maintenance: failed to record snapshot-age metric (non-fatal)")


def _run_ducklake_maintenance(payload: dict) -> None:
    """Run the POC-verified DuckLake maintenance sequence on the writer
    session, in order:

    1. ``CALL lake.merge_adjacent_files()`` — compacts small adjacent
       Parquet files written by successive copy-ingest rebuilds.
    2. ``CALL ducklake_expire_snapshots('lake', older_than => now() -
       INTERVAL '<N> days')`` — drops catalog snapshots older than the
       configured retention window (``src.analytics_backend
       .ducklake_snapshot_retention_days()``, default 7 days; floored by
       :func:`_ducklake_expire_older_than_sql`), freeing the files that
       only they referenced for step 3 to reclaim.
    3. ``CALL ducklake_cleanup_old_files('lake', cleanup_all => true)`` —
       physically deletes data files no longer referenced by any
       remaining snapshot.
    4. Catalog ``VACUUM`` (``src.ducklake_session.vacuum_ducklake_catalog``)
       — Postgres-catalog only; a no-op (logged, not an error) on a
       DuckDB-file catalog, which has no equivalent storage-compaction
       VACUUM.

    After the CALL sequence (still on the same writer connection, before
    it's closed) :func:`_record_ducklake_snapshot_age` populates
    ``agnes_ducklake_snapshot_age_seconds`` — this job's own (daily)
    cadence is the natural refresh point for that gauge (spec §3.7).

    Every CALL signature here was verified directly against the real
    ``ducklake`` extension (DuckDB 1.5.2) before being written — see the
    task 5 report for the scratch session that exercised each one
    (snapshot count dropping from N to 1 after
    ``ducklake_expire_snapshots`` + ``ducklake_cleanup_old_files``, and a
    direct ``psycopg`` ``VACUUM`` against a live pgserver-backed catalog).

    **No-op on the legacy backend.** A ``ducklake-maintenance`` job can
    only ever be enqueued by this instance's own scheduler row (daily,
    see ``services/scheduler/__main__.py::build_jobs``), but the backend
    could have been flipped back to ``legacy`` between the job being
    queued and a worker claiming it (or a stray manual enqueue via
    ``POST /api/jobs`` on a legacy instance) — checking here, not just
    trusting the scheduler's own gate, makes a stray/stale enqueue
    harmless instead of raising ``ducklake`` extension errors against a
    backend that was never attached.

    **Mutual exclusion with rebuild (wave-2G Task 5 review carry-over,
    finding 1-concurrency).** ``ducklake-maintenance`` (LIGHT lane) and
    ``SyncOrchestrator.rebuild()``/``rebuild_source()`` (HEAVY lane, via
    ``data-refresh``/``jira-refresh``) both write the lake through the
    same ``get_ducklake_write()`` singleton, and both lanes run in the
    same worker process on independent OS threads (see
    ``app/worker/runtime.py``) — so a long rebuild running past this job's
    schedule could otherwise race a catalog-wide expire/cleanup pass
    against an in-progress per-table ``CREATE OR REPLACE TABLE``. Wrapping
    the whole write section in ``src.orchestrator.rebuild_mutex()`` — the
    identical in-process lock + cross-process Postgres advisory lease pair
    ``rebuild()``/``rebuild_source()`` already take, in the same order —
    makes maintenance and rebuild mutually exclusive without introducing a
    second lock-acquisition order (which would risk deadlock).
    """
    from src.analytics_backend import analytics_backend, ducklake_snapshot_retention_days

    if analytics_backend() != "ducklake":
        logger.info("ducklake-maintenance: analytics.backend is not 'ducklake' — no-op")
        return

    from src.ducklake_session import get_ducklake_write, vacuum_ducklake_catalog
    from src.orchestrator import rebuild_mutex

    retention_days = ducklake_snapshot_retention_days()
    older_than_sql = _ducklake_expire_older_than_sql(retention_days)

    with rebuild_mutex():
        conn = get_ducklake_write()
        try:
            conn.execute("CALL lake.merge_adjacent_files()")
            conn.execute(f"CALL ducklake_expire_snapshots('lake', older_than => {older_than_sql})")
            conn.execute("CALL ducklake_cleanup_old_files('lake', cleanup_all => true)")
            _record_ducklake_snapshot_age(conn)
        finally:
            conn.close()

        vacuumed = vacuum_ducklake_catalog()

    logger.info(
        "ducklake-maintenance: merge/expire(retention=%dd, older_than=%s)/cleanup done; catalog VACUUM %s",
        retention_days,
        older_than_sql,
        "ran" if vacuumed else "skipped (file catalog)",
    )


def _run_analytics_migrate(payload: dict) -> None:
    """Wrap ``SyncOrchestrator().migrate_to_backend(to)`` — the body
    behind ``POST /api/admin/analytics/migrate`` (wave-2G Task 6).

    ``payload["to"]`` is ``"ducklake"`` or ``"legacy"``, already validated
    by the endpoint before enqueueing (an unknown value re-raises via
    ``migrate_to_backend``'s own ``ValueError``, which the worker turns
    into a failed job the same way any other handler exception does).
    Unlike ``data-refresh``/``jira-refresh``, this rebuilds into the
    EXPLICITLY named target backend regardless of the currently
    configured ``analytics.backend`` — see ``migrate_to_backend``'s
    docstring for why that distinction matters (config is boot-time
    cached, not hot-reloaded)."""
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().migrate_to_backend(payload.get("to"))


def _run_distribution_mirror(payload: dict) -> None:
    """Mirror every downloadable local/materialized parquet to the
    configured object store, then write the marker index of what's
    currently mirrored (wave 2-H, WF-3 — see
    ``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

    **Clean no-op, no ``boto3`` import** when
    ``src.object_store.object_store()`` returns ``None`` (signed-URL
    distribution off, or no store configured) — the common case for
    every S/M-tier instance and any instance that never installed the
    ``distribution`` extra. This check happens before any other import in
    this function, so a legacy instance never even imports ``boto3``.

    Enumerates the same download set ``agnes pull`` computes
    (``cli/lib/pull.py``): ``sync_state`` rows whose registry
    ``query_mode`` is ``local`` or ``materialized``, excluding
    ``server_only`` rows (kept fresh server-side, never distributed as a
    parquet). Joined against ``table_registry`` by id first, name second
    (B1 — ``sync_state.table_id`` is the registry id when a matching row
    existed at write time, ``src.sync_state_key``); either way, the STEM
    used for the on-disk lookup and the object-store key is always
    ``table_registry.name`` — the extractor / materialize pass's filename
    contract, a separate convention this key migration does not touch —
    the same resolution ``app/api/sync.py::_build_manifest_for_user`` does
    for the manifest's flat ``tables{}`` dict.

    **Single-file tables only.** A partitioned table (``sync_state.parts`` set)
    is a directory of per-period parquets and has no single object to mirror or
    presign; ``agnes pull`` fetches its parts over the app-served
    ``/api/data/<id>/download?part=`` route, which never consults the mirror.
    See the skip below.

    The md5 compared/stamped is ``sync_state.hash`` — the SAME hash the
    manifest exposes to ``agnes pull`` (computed once, in
    ``src.orchestrator._update_sync_state`` / the materialized-pass
    equivalent) — so the marker index and the manifest never disagree about
    "is this the current content".

    That claim used to be merely ASSERTED — the value uploaded and stamped
    was ``sync_state.hash`` itself, read from the DB, never checked against
    the bytes ``put_file`` actually sent. Issue #1360: a concurrent sync
    landing between the ``head_md5`` network round trip above and the
    upload (no lock spans that window) could rewrite the on-disk parquet in
    between, producing an object whose stamped md5 permanently disagreed
    with its own content — undetectable afterwards, since every later run
    only ever compares that stamp (a label) against ``sync_state.hash``
    (another label). Now it is VERIFIED: immediately before a table's
    object would be uploaded, :func:`src.object_store.hash_file_md5`
    streams the on-disk parquet and the result is compared against
    ``sync_state.hash`` — the same "hash exactly what you are about to
    send, from one read, right before sending it" rule
    ``app/api/data.py::_serve_part_self_describing`` established for the
    app-served part-download response (``X-Agnes-Content-MD5`` /
    ``src.distribution.CONTENT_MD5_HEADER``). A disagreement means the
    parquet moved under the mirror since the ``sync_state`` read above; the
    table is SKIPPED this run rather than published under a stamp that no
    longer describes its bytes, and the next run reconciles once the race
    has settled. Only the about-to-upload path re-hashes — a table whose
    object is already stamped with ``sync_state.hash`` is still skipped on
    the label comparison alone (below), so an unchanged table costs exactly
    the one ``head_md5`` round trip it always has, never an extra local
    read of a potentially multi-GB parquet.

    Idempotent: a table whose object already carries the current md5
    (``head_md5(key) == current_md5``) is skipped, not re-uploaded. Per-file
    failures (network blip, permissions, a parquet that vanished or changed
    mid-race) are logged and do not abort the run — a partial mirror is
    safe, since the marker index below only lists tables that ARE currently
    mirrored AND whose content was just verified to match; WF-2's manifest
    presign reads that index and simply omits ``signed_url`` for anything
    not in it, so the client falls back to the app-served download path.
    """
    from src.object_store import object_store

    store = object_store()
    if store is None:
        logger.info("distribution mirror: no object store configured, skipping")
        return

    from app.utils import resolve_local_parquet
    from src.distribution import write_mirror_index
    from src.object_store import hash_file_md5
    from src.repositories import sync_state_repo, table_registry_repo

    all_tables = table_registry_repo().list_all()
    registry_by_id = {t["id"]: t for t in all_tables}
    registry_by_name = {t["name"]: t for t in all_tables}

    uploaded = 0
    skipped = 0
    raced = 0
    failed = 0
    mirrored: dict[str, str] = {}

    for state in sync_state_repo().get_all_states():
        raw_table_id = state["table_id"]
        # B1: sync_state.table_id is the registry id when a matching row
        # existed at write time (src.sync_state_key); a legacy/unmatched
        # row is still name-keyed. Either way, the parquet actually on disk
        # is named after `table_registry.name` (the extractor / materialize
        # pass's own filename contract, unrelated to this key), so every
        # filesystem / object-store touch below resolves that STEM off the
        # registry row, never off the raw sync_state key.
        reg = registry_by_id.get(raw_table_id) or registry_by_name.get(raw_table_id) or {}
        stem = reg.get("name") or raw_table_id
        query_mode = reg.get("query_mode") or "local"
        if query_mode not in ("local", "materialized"):
            continue
        if reg.get("server_only"):
            continue
        current_md5 = state.get("hash") or ""
        if not current_md5:
            # Never successfully synced yet — nothing on disk to mirror.
            continue
        if state.get("parts") is not None:
            # Partitioned table — its data is a DIRECTORY of per-period parquets,
            # while this mirror addresses exactly ONE `<table_id>.parquet` object
            # per table (and `_maybe_attach_signed_url` hands the client exactly
            # one presigned URL for it). Analysts still get the table: `agnes
            # pull` syncs it part-by-part over the app-served
            # `/api/data/<id>/download?part=` route, which never consults the
            # mirror. So this is a presign-acceleration gap, not a distribution
            # gap — mirroring per-part objects would have to change the manifest
            # and the CLI together, so it stays out of the read-surface fix.
            #
            # Skipped EXPLICITLY rather than by falling into the single-file
            # lookup below, whose "no on-disk parquet found" warning told
            # operators a healthy table's sync was broken.
            logger.debug(
                "distribution mirror: %s is partitioned, distributed via the app-served part route",
                stem,
            )
            continue
        parquet_path = resolve_local_parquet(stem, reg.get("source_type"))
        if parquet_path is None:
            logger.warning("distribution mirror: no on-disk parquet found for %s, skipping", stem)
            continue

        key = f"{stem}.parquet"
        try:
            existing_md5 = store.head_md5(key)
        except Exception:
            logger.exception("distribution mirror: head_md5 failed for %s", stem)
            failed += 1
            continue

        if existing_md5 == current_md5:
            skipped += 1
            mirrored[stem] = current_md5
            continue

        # About to publish — hash the bytes we are actually about to send
        # (streamed, one read) rather than trusting `current_md5` blindly.
        # `head_md5` disagreeing only means "this object needs a refresh";
        # it is not license to upload whatever happens to be on disk.
        try:
            actual_md5 = hash_file_md5(parquet_path)
        except Exception:
            logger.exception("distribution mirror: could not hash on-disk parquet for %s", stem)
            failed += 1
            continue

        if actual_md5 != current_md5:
            # The parquet moved under the mirror since `sync_state` was read
            # above — a concurrent sync landed in the window `head_md5`'s
            # network round trip sits in (issue #1360; no lock spans it).
            # Publishing now would stamp bytes that no longer match the
            # label, so leave whatever object is already there untouched
            # and let the next run reconcile once the race has settled.
            logger.info(
                "distribution mirror: %s changed on disk since sync_state was read, skipping this run "
                "(sync_state %s, on-disk %s)",
                stem,
                current_md5[:12],
                actual_md5[:12],
            )
            raced += 1
            continue

        try:
            store.put_file(parquet_path, key, md5=actual_md5)
        except Exception:
            logger.exception("distribution mirror: upload failed for %s", stem)
            failed += 1
            continue

        uploaded += 1
        mirrored[stem] = actual_md5

    write_mirror_index(store, mirrored)

    logger.info(
        "distribution mirror: uploaded=%d skipped=%d raced=%d failed=%d mirrored_total=%d",
        uploaded,
        skipped,
        raced,
        failed,
        len(mirrored),
    )


_DEFAULT_AGENT_RESPONSE_JOB_TIMEOUT_S = 1800

#: Prefix marking a `_run_agent_response` failure as a concurrency-cap hit —
#: see the handler's `except ConcurrencyCapHit` branch below. Duplicated
#: (not imported) into `app/api/agent_runtime.py::_serialize_job`, which
#: parses it back out of the persisted `jobs.error` string to surface a
#: structured `{"code": "concurrency_cap", ...}` — same
#: HEAVY_LANE/LIGHT_LANE-style duplication rationale as `app/worker/registry
#: .py`'s module docstring: the CONTRACT is the string value, not which
#: module owns the source of truth for it.
CONCURRENCY_CAP_ERROR_PREFIX = "concurrency_cap: "

#: Prefix marking a `_run_agent_response` failure as a `response_format`
#: schema-validation failure (V1b Task 7, C13). Duplicated (not imported)
#: into `app/api/agent_runtime.py::_serialize_error` — same rationale as
#: `CONCURRENCY_CAP_ERROR_PREFIX` above. Unlike that prefix, the remainder
#: of the string here is a JSON object (`code`/`message`/`session_id`/
#: `usage`/`raw_answer`) rather than a plain message, since the caller needs
#: more than a one-line hint to recover the paid-for answer.
SCHEMA_VALIDATION_ERROR_PREFIX = "schema_validation_failed: "


def _agent_response_job_timeout_seconds() -> int:
    raw = os.environ.get("AGNES_AGENT_RESPONSE_JOB_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_AGENT_RESPONSE_JOB_TIMEOUT_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_AGENT_RESPONSE_JOB_TIMEOUT_S


def _run_agent_response(payload: dict) -> dict:
    """Task 9's ``agent_response`` handler — see the module docstring's
    ``agent_response`` entry for the ``"fresh"``/``"continue"`` mode split
    and the cross-loop dispatch rationale.

    Returns a result dict (NOT ``None``, unlike every other kind here) —
    ``app/worker/runtime.py`` passes it through to
    ``jobs_repo().complete(..., result=...)``.
    """
    import asyncio

    from app.chat.manager import ConcurrencyCapHit, get_current_chat_loop, get_current_chat_manager

    manager = get_current_chat_manager()
    loop = get_current_chat_loop()
    if manager is None or loop is None:
        # Defensive only — should be unreachable post-fix: `register_all_kinds()`
        # (this module's own caller-facing entry point) only registers
        # `agent_response` when `get_current_chat_manager()` is non-None at
        # registration time (see its docstring), so a process without a live
        # chat manager never has this kind in `JOB_KINDS` and therefore never
        # claims (or runs) one of these jobs in the first place. Kept as a
        # fail-closed guard in case that invariant is ever violated (e.g. a
        # future direct-dispatch caller that bypasses the registry).
        raise RuntimeError("agent_response: chat is disabled on this worker process")

    from app.chat.headless import await_completion, run_one_shot

    timeout_s = _agent_response_job_timeout_seconds()
    mode = payload.get("mode", "fresh")
    if mode == "continue":
        coro = await_completion(
            manager,
            chat_id=payload["chat_id"],
            timeout_s=timeout_s,
            agent_id=payload.get("agent_id"),
            owner_user_id=payload.get("owner_user_id"),
        )
    else:
        coro = run_one_shot(
            manager,
            user_email=payload["owner_email"],
            agent_id=payload.get("agent_id"),
            prompt=payload["prompt"],
            timeout_s=timeout_s,
            owner_user_id=payload.get("owner_user_id"),
        )
    # `run_coroutine_threadsafe` schedules onto the loop the live
    # ChatManager actually runs on (this handler itself executes in a
    # worker THREAD, via `asyncio.to_thread` — see `app/worker/runtime.py`);
    # `.result(timeout=...)` blocks this thread until that coroutine
    # resolves, with a grace margin over the coroutine's own internal
    # wait so a same-duration race doesn't spuriously time out the OUTER
    # call before the inner `asyncio.wait_for` has a chance to return.
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        run_result = future.result(timeout=timeout_s + 60)
    except ConcurrencyCapHit as exc:
        # `run_one_shot`'s `create_session()` call hit the per-user
        # concurrency cap (`"fresh"` mode only — `await_completion`, the
        # `"continue"` mode, never creates a session). Re-raised with a
        # recognizable prefix (not the bare `ConcurrencyCapHit` message) so
        # `_run_one`'s generic `except Exception` -> `jobs_repo().fail(...,
        # str(exc), ...)` persists an `error` string
        # `app/api/agent_runtime.py::_serialize_job` can parse back into a
        # structured `{"code": "concurrency_cap", ...}` for `GET
        # /api/v1/jobs/{id}` — a clear signal instead of a raw traceback.
        # `retry_in_seconds=None` on this kind (see `register_all_kinds()`
        # below) means this finalizes straight to `'failed'`, no retry loop.
        raise RuntimeError(f"{CONCURRENCY_CAP_ERROR_PREFIX}{exc}") from exc

    _flush_usage_accumulator()

    from app.chat.agent_usage import usage_for_session

    usage = usage_for_session(payload.get("agent_id"), run_result["chat_id"])

    # V1b Task 7 / C13: a background or sync-timeout-degraded run that
    # requested structured output is validated here too — the sync path
    # (`app/api/agent_runtime.py`) only validates when it itself produced
    # the final answer; a run that degraded to this job (or was
    # `background: true` from the start) never passes back through that
    # code, so this is the only place its answer is ever checked. On
    # failure, raise with `SCHEMA_VALIDATION_ERROR_PREFIX` + a JSON body
    # carrying the same fields the sync path's 422 does (`session_id`,
    # `usage`, `raw_answer`) — the run already spent tokens, so this must
    # not be a bare error string with no way to recover the answer.
    # `retry_in_seconds=None` on this kind means a validation failure
    # finalizes straight to `'failed'`, matching C13 ("the job goes failed
    # with the same structured error").
    response_format = payload.get("response_format")
    if response_format is not None and not run_result["timed_out"]:
        # Validation applies to FINAL answers only. A timed-out leg's
        # answer is empty/partial by definition — validating it would fail
        # the job as schema_validation_failed and misreport a healthy
        # long-running turn (see the bounded-wait-leg contract below);
        # the caller continues the wait and the FINAL leg gets validated.
        from app.chat.structured_output import validate

        ok, parsed, validation_error = validate(run_result["answer"], response_format)
        if not ok:
            error_payload = {
                "code": "schema_validation_failed",
                "message": validation_error,
                "session_id": run_result["chat_id"],
                "usage": usage,
                "raw_answer": run_result["answer"],
            }
            raise RuntimeError(f"{SCHEMA_VALIDATION_ERROR_PREFIX}{json.dumps(error_payload)}")
        return {
            "answer": run_result["answer"],
            "session_id": run_result["chat_id"],
            "usage": usage,
            "timed_out": run_result["timed_out"],
            "parsed": parsed,
        }

    # A `timed_out: true` result (possibly with an EMPTY `answer`) still
    # COMPLETES the job — deliberately. The job bounds one WAIT leg, not
    # the turn: the turn keeps running server-side on `session_id`, and
    # the caller distinguishes "done" from "still generating" by the
    # `timed_out` flag, then either re-polls the session or enqueues
    # another continue leg. Failing the job here would misreport a
    # healthy long-running turn as an error (and with retries disabled
    # on this kind, would dead-end it).
    return {
        "answer": run_result["answer"],
        "session_id": run_result["chat_id"],
        "usage": usage,
        "timed_out": run_result["timed_out"],
    }


def _flush_usage_accumulator() -> None:
    """Flush the broker's batched `llm_usage` ledger before summing usage
    (review carry-over, Task 9) — a just-finished turn's rows may still be
    sitting in the in-memory accumulator otherwise, undercounting the
    `usage` this handler returns. The sync path
    (`app/api/agent_runtime.py::usage_accumulator_flush`) already does this
    before its own `usage_for_session()` call; this worker path reads the
    exact same ledger and needs the identical flush — duplicated (not
    imported from that router module) because this module must stay
    independent of FastAPI routing (see `app/chat/agent_usage.py`'s module
    docstring for the same rationale, one level up the dependency chain).
    Deferred import + best-effort (never raises) for the same reasons as
    the router's copy."""
    try:
        from app.api.broker_agent_policy import usage_accumulator

        usage_accumulator.flush()
    except Exception:
        logger.exception("usage_accumulator.flush() failed — usage totals may undercount this response")


def _run_webhook_deliver(payload: dict) -> None:
    """V1b Task 6 — deliver one outbound webhook notification.

    ``payload`` is ``{"webhook_id": ..., "notification": {...}}``, built by
    ``app.chat.webhook_delivery.enqueue_job_event_webhooks``. Re-fetches the
    webhook row fresh (rather than trusting anything enqueue-time snapshot
    of it) so a webhook deleted or disabled between enqueue and this claim
    is a clean no-op instead of a wasted/misdirected send.

    Raises (so the worker's standard ``fail(..., retry_in_seconds=...)``
    path requeues it, bounded by the ``webhook-deliver`` kind's own retry
    config below) when ``app.chat.webhook_delivery.deliver`` returns
    ``False`` — the send itself already recorded the failure against the
    webhook row (consecutive-failure counter / auto-disable); this raise is
    purely what drives the JOB's own retry, a separate concern from the
    webhook's own failure bookkeeping.
    """
    webhook_id = payload.get("webhook_id")
    if not webhook_id:
        raise RuntimeError("webhook-deliver: payload missing webhook_id")

    from src.repositories import agent_webhooks_repo

    webhook = agent_webhooks_repo().get(webhook_id)
    if webhook is None or not webhook.get("active", True):
        logger.info("webhook-deliver: webhook %s no longer exists/active — skipping", webhook_id)
        return

    from app.chat.webhook_delivery import deliver

    notification = payload.get("notification") or {}
    if not deliver(webhook, notification):
        raise RuntimeError(f"webhook-deliver: POST to webhook {webhook_id} failed")


#: How much of a failed producer's stderr to keep for the DEBUG line. Enough
#: for a Python traceback plus context, small enough that it can never be the
#: reason a worker dies.
_PRODUCER_STDERR_TAIL_BYTES = 64 * 1024


def _tail_text(fh, limit: int) -> str:
    """Last ``limit`` bytes of an open binary file, decoded leniently.

    Seeks rather than reads forward, so a multi-gigabyte producer log costs
    one seek. ``errors="replace"`` because the cut can land mid-codepoint and
    a diagnostic must never raise on its way to the log.
    """
    try:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - limit))
        return fh.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - a diagnostic must not mask the failure it describes
        return "<unreadable>"


def _extraction_timeout_seconds() -> int:
    from app.instance_config import get_value

    raw = get_value("extraction", "timeout_s", default=_DEFAULT_EXTRACTION_TIMEOUT_S)
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return _DEFAULT_EXTRACTION_TIMEOUT_S


def _extraction_producer_argv() -> list[str] | None:
    """Build the producer's argv from ``extraction.producer`` config
    (``config/instance.yaml.example``).

    ``command`` (a full command line — either a YAML list, taken verbatim,
    or a string split with ``shlex.split``) wins when both are set;
    ``module`` is the ``python -m <module>`` shorthand for a producer the
    ``worker`` image installed as a package (see the Dockerfile's
    ``EXTRACTION_PRODUCER_INSTALL`` build-arg). Returns ``None`` when
    neither is configured — the caller turns that into a clear "not
    configured" failure rather than a confusing subprocess error.
    """
    from app.instance_config import get_value

    command = get_value("extraction", "producer", "command", default=None)
    if command:
        if isinstance(command, list):
            return [str(c) for c in command]
        return shlex.split(str(command))

    module = get_value("extraction", "producer", "module", default=None)
    if module:
        import sys

        return [sys.executable, "-m", str(module)]

    return None


def _extraction_producer_env_passthrough() -> list[str]:
    """Extra env var NAMES an operator explicitly opted into forwarding to
    the producer, beyond :data:`_EXTRACTION_PRODUCER_ENV_ALLOWLIST`
    (``extraction.producer.env_passthrough``, default empty). A per-name
    opt-in, not a way back to `{**os.environ}` — only the names listed here
    are copied, and only when they actually exist in this process's
    ``os.environ``."""
    from app.instance_config import get_value

    raw = get_value("extraction", "producer", "env_passthrough", default=[])
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if raw:
        return [str(raw)]
    return []


def _extraction_producer_env() -> dict[str, str]:
    """The non-secret base env for the producer subprocess: the curated
    allowlist plus whatever :func:`_extraction_producer_env_passthrough`
    names — each copied from ``os.environ`` only when present. Callers add
    the resolved SharePoint credentials + corpus id on top of this."""
    names = list(_EXTRACTION_PRODUCER_ENV_ALLOWLIST) + _extraction_producer_env_passthrough()
    return {name: os.environ[name] for name in names if name in os.environ}


class AnonymizationKeyError(RuntimeError):
    """At least one selected scope is ``anonymize=true`` but no per-instance
    HMAC key resolves (design spec §9.2's pseudonym scheme —
    ``PERSON_<hmac(key, ...)>`` etc., never a fixed marker). Raised rather
    than silently omitting the key: a producer falling back to a shared or
    absent key defeats the "tokens never correlate across tenants"
    guarantee the key exists for, so the job fails clean instead of running
    with a weaker guarantee than the wizard promised."""


_ANONYMIZATION_HMAC_KEY_ENV_DEFAULT = "AGNES_ANONYMIZATION_HMAC_KEY"


def _anonymize_marked_scope_map(connection: dict) -> dict[str, str]:
    """``{source_scope_id: collection_id}`` for exactly the scopes THIS
    connection's wizard marked ``anonymize=true`` (spec §9's
    anonymize-in-front pipeline: source -> crawl -> convert -> anonymize ->
    Agnes) — the producer handoff for which scopes it must run through the
    anonymizer before uploading.

    Reads the connection row's own ``config.scopes`` directly (the shape
    ``app/api/admin_sharepoint.py`` writes and reads:
    ``{source_scope_id, display_path, anonymize, collection_id}``) rather
    than importing that admin router — this worker handler must not gain a
    dependency on the admin API surface. ``GET .../corpus-map`` stays the
    flat ``{source_scope_id: collection_id}`` producers already consume;
    this is the SAME mapping, narrowed to anonymize-marked rows, used only
    internally to build the child env below.
    """
    scopes = (connection.get("config") or {}).get("scopes")
    if not isinstance(scopes, list):
        return {}
    out: dict[str, str] = {}
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if scope.get("anonymize") and scope.get("source_scope_id") and scope.get("collection_id"):
            out[str(scope["source_scope_id"])] = str(scope["collection_id"])
    return out


def _resolve_anonymization_key() -> str:
    """Resolve this instance's per-instance anonymization HMAC key (spec
    §9.2): an admin-configurable env var NAME
    (``extraction.anonymization.hmac_key_env``, default
    ``AGNES_ANONYMIZATION_HMAC_KEY``), checked against
    :func:`src.orchestrator_security.is_producer_key_env_allowed` BEFORE the
    value is read. The env var NAME is admin-writable config, so without a
    gate an admin could point ``hmac_key_env`` at an unrelated instance
    secret (``ANTHROPIC_API_KEY``, ``JWT_SECRET_KEY``, ...) and have it
    forwarded to the external producer as if it were the anonymization key.

    Deliberately uses ``is_producer_key_env_allowed`` — a SEPARATE, narrower
    allowlist from ``is_token_env_allowed`` (the connector-ATTACH `token_env`
    gate) — NOT the same function the SharePoint certificate resolver uses.
    Sharing the certificate's allowlist would additionally make this key a
    legal `token_env` for a connector-written `_remote_attach` row (a
    SECOND, unrelated consumer of that allowlist in ``src/orchestrator.py``
    / ``src/db.py``), letting a malicious connector exfiltrate the resolved
    key value via ``ATTACH ... TOKEN`` to a connector-chosen URL (RBAC
    review, 2026-08-28). See ``_PRODUCER_KEY_ENVS``'s docstring in
    ``src/orchestrator_security.py`` for the full trust-boundary argument.

    Raises :class:`AnonymizationKeyError` (never returns a fallback/empty
    key) when the name is disallowed or unset — see that class's docstring
    for why.
    """
    from app.instance_config import get_value
    from src.orchestrator_security import is_producer_key_env_allowed

    env_name = str(get_value("extraction", "anonymization", "hmac_key_env", default="") or "").strip()
    env_name = env_name or _ANONYMIZATION_HMAC_KEY_ENV_DEFAULT

    if not is_producer_key_env_allowed(env_name):
        raise AnonymizationKeyError(
            f"extraction.anonymization.hmac_key_env={env_name!r} is not an allowed anonymization "
            f"key variable. Use the default name, {_ANONYMIZATION_HMAC_KEY_ENV_DEFAULT}, or leave "
            "hmac_key_env empty."
        )

    value = os.environ.get(env_name)
    if not value:
        raise AnonymizationKeyError(
            f"{env_name} is not set on the server, so the per-instance anonymization key cannot "
            "be resolved. At least one selected scope is marked anonymize=true — set "
            f"{env_name} (see docs/anonymization.md) or unmark the scope in the connect wizard."
        )
    return value


def _run_corpus_extraction(payload: dict) -> dict:
    """Producer-invocation SEAM for the ``corpus-extraction`` kind (spec
    §7.5 / §16 step 7). See the module docstring's entry for the wider
    picture; this is the mechanics.

    THIS HANDLER DOES NOT CRAWL, CONVERT, ANONYMIZE, OR EXTRACT ANYTHING
    ITSELF — it resolves credentials, builds a command line, runs one
    subprocess, and reports what happened. The crawl -> convert ->
    anonymize -> extract -> ingest pipeline behind that subprocess is the
    external producer (a separate project of the operator's, adopted per spec
    §7.1); porting its internals into this repo is explicitly out of scope
    (spec §1 "Out of scope") — this handler is the seam a future producer
    integration plugs into, not a place to grow pipeline logic.

    ``payload``:
      - ``connection_id`` (required) — a ``source_connections`` row,
        ``source_type='sharepoint'``. Credentials are resolved from ITS
        vault slot or the server's ``SHAREPOINT_CERT_PRIVATE_KEY`` env var
        via :func:`connectors.sharepoint.settings.resolve_sharepoint_settings`
        — the SAME resolution the SharePoint admin UI uses
        (``app/api/admin_sharepoint.py``). Never hardcoded, never read from
        this payload directly.
      - ``corpus_id`` (optional, ``scope`` accepted as an alias) — which
        collection the producer should write into. Passed through to the
        producer verbatim; this handler does not interpret it.

    Anonymize-in-front handoff (spec §9/§9.2): this connection's own
    ``config.scopes`` rows carry a per-scope ``anonymize`` flag (the connect
    wizard's step-2 column, ``app/api/admin_sharepoint.py``) — the ONLY
    place that flag is real is here. When at least one confirmed scope is
    ``anonymize=true``, the child env additionally carries
    ``AGNES_EXTRACTION_ANONYMIZE_SCOPES`` (a JSON object,
    ``{source_scope_id: collection_id}``, covering ONLY the anonymize-marked
    scopes — see :func:`_anonymize_marked_scope_map`) and
    ``AGNES_ANONYMIZATION_HMAC_KEY`` (the per-instance pseudonym key, see
    :func:`_resolve_anonymization_key`). Neither var is set when no scope is
    anonymize-marked — an instance that never anonymizes never resolves or
    forwards a key it does not need.

    Security (playbook F7): every secret this handler resolves —
    tenant id, client id, certificate private key, and (when needed) the
    anonymization HMAC key — reaches the producer ONLY via the child
    process's environment, never on argv (readable via `ps`/
    `/proc/<pid>/cmdline`) and never logged. That child env is NOT
    `{**os.environ}` — `extraction.producer` names an EXTERNAL,
    admin-configurable binary, so it starts from a curated non-secret
    allowlist (`_EXTRACTION_PRODUCER_ENV_ALLOWLIST`) plus any operator-
    opted-in `extraction.producer.env_passthrough`, then adds only the
    three named credentials + the corpus id + (conditionally) the two
    anonymization vars above. No other instance secret (vault key, LLM API
    key, DB DSN, ...) is ever forwarded, no matter what happens to be
    sitting in this process's own environment. The producer's own
    stdout/stderr are logged at DEBUG only, and only on failure, in case a
    misbehaving producer echoes something it shouldn't at INFO-visible
    levels.

    No-op guard: raises (so the job fails cleanly, not with a confusing
    subprocess error) when ``extraction.enabled`` is false or no producer
    command/module is configured — the same "off unless explicitly turned
    on" posture as ``ducklake-maintenance``'s backend check, just failing
    instead of silently returning, since a `corpus-extraction` job only
    ever exists because something explicitly enqueued it.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("extraction", "enabled", env_var="AGNES_EXTRACTION_ENABLED", default=False):
        raise RuntimeError("corpus-extraction: extraction.enabled is false — refusing to run")

    argv = _extraction_producer_argv()
    if not argv:
        raise RuntimeError(
            "corpus-extraction: no producer configured — set extraction.producer.command "
            "or extraction.producer.module in instance.yaml"
        )

    connection_id = payload.get("connection_id")
    if not connection_id:
        raise RuntimeError("corpus-extraction: payload missing connection_id")

    from src.repositories import source_connections_repo

    connection = source_connections_repo().get(connection_id)
    if connection is None or connection.get("source_type") != "sharepoint":
        raise RuntimeError(f"corpus-extraction: connection {connection_id!r} not found or not a sharepoint connection")

    from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings

    try:
        settings = resolve_sharepoint_settings(connection)
    except SharePointSettingsError as exc:
        # Named cause, not a bare 500-class traceback — mirrors
        # app/api/admin_sharepoint.py::_resolved_token's typed handling of
        # the identical error.
        raise RuntimeError(f"corpus-extraction: {exc}") from exc

    corpus_id = payload.get("corpus_id") or payload.get("scope")

    # Secrets go in the CHILD process env, never on argv (security playbook
    # F7) — but NOT the full parent environment. `extraction.producer`
    # names an EXTERNAL, admin-configurable binary, so this starts from the
    # curated non-secret allowlist (+ any operator-opted-in
    # `env_passthrough`) — see `_EXTRACTION_PRODUCER_ENV_ALLOWLIST`'s
    # comment for why `{**os.environ}` would leak every instance secret
    # (vault key, LLM API key, DB DSN, ...) to whatever the admin pointed
    # this at — and adds only the resolved credentials and the corpus id.
    child_env = {
        **_extraction_producer_env(),
        "AGNES_SHAREPOINT_TENANT_ID": settings.tenant_id,
        "AGNES_SHAREPOINT_CLIENT_ID": settings.client_id,
        "AGNES_SHAREPOINT_PRIVATE_KEY": settings.private_key,
    }
    if corpus_id:
        child_env["AGNES_EXTRACTION_CORPUS_ID"] = str(corpus_id)

    # Anonymize-in-front handoff (spec §9/§9.2): which of THIS connection's
    # scopes the producer must run through the anonymizer before uploading,
    # plus the per-instance pseudonym key — env only (never argv), and only
    # added when at least one scope actually needs it, so an instance that
    # never anonymizes never resolves/forwards the key at all.
    anonymize_scopes = _anonymize_marked_scope_map(connection)
    if anonymize_scopes:
        child_env["AGNES_EXTRACTION_ANONYMIZE_SCOPES"] = json.dumps(anonymize_scopes, sort_keys=True)
        child_env["AGNES_ANONYMIZATION_HMAC_KEY"] = _resolve_anonymization_key()

    timeout_s = _extraction_timeout_seconds()

    logger.info(
        "corpus-extraction: invoking producer for connection %s (corpus=%s, timeout=%ds)",
        connection_id,
        corpus_id,
        timeout_s,
    )
    # NOT `capture_output=True`: that holds every byte the producer writes in
    # THIS process's memory for the whole run, and the run may legitimately
    # last `extraction.timeout_s` (default an hour) crawling a real site. The
    # captured text is used for exactly one thing — a DEBUG line on failure —
    # so an hour of a chatty producer's progress output would buy a diagnostic
    # tail at the price of OOM-killing a worker whose container memory limit is
    # 4g by default (Devin Review on this PR). stdout goes to /dev/null (this
    # handler never reads it — the producer reports through the ingest API, not
    # through its own stdout), and stderr streams to a temp file from which
    # only the last `_PRODUCER_STDERR_TAIL_BYTES` are read back on failure.
    # Trading unbounded RSS for bounded RSS plus scratch disk is the right way
    # round: the memory limit is what kills the worker, and the tail is the
    # part of a stack trace anyone reads anyway.
    try:
        with tempfile.TemporaryFile(mode="w+b") as stderr_buf:
            result = subprocess.run(
                argv,
                env=child_env,
                timeout=timeout_s,
                stdout=subprocess.DEVNULL,
                stderr=stderr_buf,
                check=False,
            )
            stderr_tail = _tail_text(stderr_buf, _PRODUCER_STDERR_TAIL_BYTES) if result.returncode != 0 else ""
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"corpus-extraction: producer timed out after {timeout_s}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"corpus-extraction: producer command not found: {argv[0]!r}") from exc

    if result.returncode != 0:
        logger.debug("corpus-extraction: producer stderr tail (connection %s): %s", connection_id, stderr_tail)
        raise RuntimeError(f"corpus-extraction: producer exited {result.returncode} for connection {connection_id}")

    logger.info("corpus-extraction: producer completed for connection %s", connection_id)
    return {
        "connection_id": connection_id,
        "corpus_id": corpus_id,
        "returncode": result.returncode,
    }


def dispatch_job(job: dict) -> Optional[dict]:
    """THE single dispatch-level entry point for running one claimed job's
    handler (F2b — audit-full-coverage plan, Task 4). Looks ``job["kind"]``
    up in the process-wide ``JOB_KINDS`` registry, runs its handler, and
    writes exactly one ``job.run`` audit row regardless of outcome — no
    individual ``_run_*`` handler above calls ``log_safe`` itself, so a
    future kind gets audit coverage for free just by registering through
    ``register_kind``.

    ``app/worker/runtime.py``'s ``_run_one`` calls this (via
    ``asyncio.to_thread``) INSTEAD OF ``kind.handler(job["payload_json"])``
    directly — the one place in the whole worker that actually executes a
    claimed job, so this is also the one place audit coverage needs to
    live (one dispatch-level wrapper, not one per kind).

    Runs outside any HTTP request — there is no ASGI scope for
    ``src.audit_context``'s autofill to read, so ``duration_ms`` is
    measured explicitly here and ``client_kind="scheduler"`` is always
    passed. ``user_id=None``: a scheduled/worker job has no human caller to
    attribute the row to.
    """
    kind = JOB_KINDS[job["kind"]]
    t0 = time.monotonic()
    try:
        result = kind.handler(job["payload_json"])
    except Exception as exc:
        log_safe(
            user_id=None,
            action="job.run",
            resource=f"job:{job['kind']}",
            params={"kind": job["kind"], "outcome": "error", "job_id": job.get("id")},
            result=f"error:{type(exc).__name__}",
            duration_ms=int((time.monotonic() - t0) * 1000),
            client_kind="scheduler",
        )
        raise
    log_safe(
        user_id=None,
        action="job.run",
        resource=f"job:{job['kind']}",
        params={"kind": job["kind"], "outcome": "success", "job_id": job.get("id")},
        result="success",
        duration_ms=int((time.monotonic() - t0) * 1000),
        client_kind="scheduler",
    )
    return result


def register_all_kinds() -> None:
    """Register the real job kinds. Idempotent — safe to call more than
    once (e.g. across test re-imports); ``register_kind`` replaces any
    existing entry of the same name rather than erroring.

    **``agent_response`` is registered CONDITIONALLY** (role-split review
    carry-over, Task 9) — every other kind here always registers
    unconditionally, since ``app/worker/runtime.py``'s lane slots only ever
    claim kinds present in the process-wide ``JOB_KINDS`` registry (see
    ``_kinds_for_lane``). ``agent_response``'s handler
    (``_run_agent_response`` above) needs the live ``ChatManager`` singleton
    (``app.chat.manager.get_current_chat_manager``/``get_current_chat_loop``)
    to actually run a turn — and in the role-split deployment topology
    (``Role.GATEWAY`` owns chat; ``Role.WORKER`` may run in a SEPARATE
    process/replica with no chat manager at all — see ``app/main.py``'s
    CHAT-INIT block), a worker-only process has neither. Before this fix,
    every process registered the kind unconditionally regardless of role,
    so a worker-only process would claim an ``agent_response`` job, find
    ``get_current_chat_manager()`` is ``None``, raise, and permanently fail
    the job (``retry_in_seconds=None`` — see below): background/degraded
    agent runs were silently unrunnable on that topology.

    Gating registration on ``get_current_chat_manager() is not None``
    instead makes a worker-only process simply never add ``agent_response``
    to its ``JOB_KINDS`` — ``_kinds_for_lane(LIGHT_LANE)`` then never
    includes it, so that process's LIGHT lane slots never claim one of
    these jobs; the job stays ``'queued'``, visible via ``GET
    /api/v1/jobs/{id}``, until a process that DOES host a chat manager
    (gateway-colocated worker — i.e. ``Role.GATEWAY`` and ``Role.WORKER``
    both enabled, which is what the default ``all``/single-container
    topology already gives you) claims it instead.

    Ordering requirement this relies on: ``app/main.py``'s lifespan calls
    ``set_current_chat_manager(app.state.chat_manager)`` (CHAT-INIT settling
    to a real manager or ``None``) BEFORE calling ``register_all_kinds()`` —
    see the comment at that call site. Calling this function before CHAT-INIT
    has run would see a stale/no manager even on a real gateway process.
    """
    register_kind(
        JobKind(
            name="data-refresh",
            handler=_run_data_refresh,
            lane=HEAVY_LANE,
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="marketplaces-sync",
            handler=_run_marketplaces_sync,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="session-collector",
            handler=_run_session_collector,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="corporate-memory",
            handler=_run_corporate_memory,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="jira-refresh",
            handler=_run_jira_refresh,
            lane=HEAVY_LANE,
            lease_seconds=_DEFAULT_JIRA_REFRESH_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            # LIGHT lane: this is network-bound (one request per organization), not a
            # DuckDB rebuild. It touches extract.duckdb only for the brief `_meta`
            # update, which takes `rebuild_mutex()` itself, so it does not need the
            # HEAVY lane's concurrency-1 serialisation.
            name="jira-org-refresh",
            handler=_run_jira_org_refresh,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_JIRA_ORG_REFRESH_LEASE_S,
            retry_in_seconds=3600,
        )
    )
    register_kind(
        JobKind(
            name="ducklake-maintenance",
            handler=_run_ducklake_maintenance,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="analytics-migrate",
            handler=_run_analytics_migrate,
            lane=HEAVY_LANE,
            # Same cost class as data-refresh (a full extracts-tree rebuild,
            # just into a different target backend) — reuse the same
            # generous lease default/override knob rather than inventing a
            # second one.
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="distribution-mirror",
            handler=_run_distribution_mirror,
            lane=LIGHT_LANE,
            # Same LIGHT-lane default as marketplaces-sync/session-collector/
            # corporate-memory: bounded by the number of tables + their
            # individual upload times, not multi-minute by design. No
            # dedicated env override — a mirror run is bounded work, unlike
            # the ducklake catalog operations that justified their own knob.
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="webhook-deliver",
            handler=_run_webhook_deliver,
            lane=LIGHT_LANE,
            # Plain outbound HTTP POST bounded by
            # AGNES_WEBHOOK_DELIVERY_TIMEOUT_S (app.chat.webhook_delivery,
            # default 10s) — the LIGHT-lane default lease is comfortably
            # generous over that.
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            # Short, bounded backoff — a transient outage on the receiving
            # end should recover in a minute or two; agent_webhooks' own
            # consecutive_failures/webhook_max_failures counter (not this
            # job's attempts) is what ultimately disables a permanently-dead
            # endpoint (see app.chat.webhook_delivery.deliver).
            retry_in_seconds=60,
        )
    )
    register_kind(
        JobKind(
            name="analytics-rebuild",
            handler=_run_analytics_rebuild,
            lane=HEAVY_LANE,
            # Same cost class as data-refresh (BQ extract rebuild + master
            # views) — reuse its lease knob.
            lease_seconds=_data_refresh_lease_seconds(),
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="collections-purge",
            handler=_run_collections_purge,
            lane=HEAVY_LANE,
            # extract.duckdb surgery + rebuild_source — same serialization
            # class as the other analytics writers, so HEAVY (concurrency 1).
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="corpus-extraction",
            handler=_run_corpus_extraction,
            lane=EXTRACTION_LANE,
            # Tracks the producer subprocess's own timeout (extraction.timeout_s,
            # default 3600s) plus a margin — see the module docstring's
            # lease/retry tuning note.
            lease_seconds=_extraction_timeout_seconds() + _EXTRACTION_LEASE_MARGIN_S,
            # No automatic retry: a failed producer run (bad credentials,
            # crawl error, timeout) needs an operator to look at it, not an
            # unattended re-run against the same corpus a few minutes later.
            retry_in_seconds=None,
        )
    )
    from app.chat.manager import get_current_chat_manager

    if get_current_chat_manager() is not None:
        register_kind(
            JobKind(
                name="agent_response",
                handler=_run_agent_response,
                lane=LIGHT_LANE,
                # Lease covers the job's own internal wait (default 1800s,
                # AGNES_AGENT_RESPONSE_JOB_TIMEOUT_S) plus this margin — the
                # heartbeat renews well before either expires, so this is a
                # ceiling on "how long before a crashed/stuck run is
                # reclaimed", not a hard timeout on the underlying chat turn.
                lease_seconds=_agent_response_job_timeout_seconds() + 120,
                # No retry: a fresh retry would re-run `run_one_shot` and
                # re-send the user's prompt a second time (or, for `"continue"`,
                # re-attach to a `chat_id` that may have already produced and
                # lost its only answer) — neither is a safe automatic retry
                # target. A failed job stays `'failed'`; the caller sees that
                # via `GET /api/v1/jobs/{id}` and can re-POST if they want a
                # fresh attempt.
                retry_in_seconds=None,
            )
        )
    else:
        # This process has no live ChatManager (role-split topology, chat
        # disabled/misconfigured, or `set_current_chat_manager()` hasn't run
        # yet — see the docstring above). Leaving the kind unregistered here
        # means a worker-only process's lane slots never claim an
        # `agent_response` job — see `_kinds_for_lane` in
        # `app/worker/runtime.py` — so it stays `'queued'` for a
        # gateway-colocated worker to pick up instead of being claimed and
        # failed outright.
        logger.info(
            "agent_response job kind NOT registered on this process (no live "
            "ChatManager) — background agent-response jobs will only be "
            "claimed by a gateway-colocated worker"
        )
