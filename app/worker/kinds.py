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
  design.md) — document extraction for one SharePoint connection. Off by
  default (``sharepoint.enabled: false``, ``config/instance.yaml
  .example``). A thin delegate like every other handler here: the whole
  crawl -> convert -> (anonymize) -> ingest pipeline runs IN-PROCESS from
  ``connectors.sharepoint.crawler.run_builtin_crawl`` (owner decision
  2026-08-31 — the built-in pipeline is the ONLY pipeline; the external
  producer subprocess, its ``extraction.producer.*`` config, its curated
  child env and its Admin-grade callback credential were all removed with
  it). Credentials are resolved by the crawler through
  ``connectors.sharepoint.settings.resolve_sharepoint_settings`` — the
  SAME resolution path the SharePoint admin UI uses — and no secret is
  ever put on argv, because there is no argv.
  Registered UNCONDITIONALLY (its own no-op guard on
  ``sharepoint.enabled`` makes an accidental claim on a process that
  never opted into the ``extraction`` lane harmless, mirroring
  ``webhook-deliver``'s posture above) but only ever CLAIMED by a lane
  slot that opted into ``AGNES_WORKER_LANES=extraction`` — see
  ``app/worker/runtime.py``'s ``selected_lanes()``.
- ``sharepoint-acl-sync`` (LIGHT) — 2026-08-30 plan, Task 4. Mirrors
  SharePoint scope-root permissions into Agnes groups/memberships/collection
  grants (spec 2026-08-28-sharepoint-acl-mirroring-design.md §5) — network-
  bound Graph paging + a few hundred repo writes per connection, not a
  DuckDB rebuild, so LIGHT rather than HEAVY. The sync body (read →
  classify → resolve → diff → write, per-connection failure isolation, the
  must_not/should_not staleness fork) lives entirely in
  ``connectors.sharepoint.acl_sync.run_acl_sync`` — this kind's handler is a
  thin delegate, same posture as every OTHER kind here. Registered
  UNCONDITIONALLY: ``run_acl_sync``'s own ``sharepoint.enabled`` gate
  makes an accidental/scheduled claim on an instance that hasn't turned the
  feature on harmless, identical to ``ducklake-maintenance``'s and
  ``corpus-extraction``'s no-op postures above.
- ``sharepoint-subtree-sweep`` (LIGHT) — 2026-08-30 plan, Task 7. Walks each
  mirrored scope's folder tree, probing ``hasUniqueRoleAssignments`` per
  folder, to find and exclude broken-inheritance subtrees (spec §3(b),
  §6.2) — a full pass over a large library is MULTI-HOUR (§6.2's cost
  model), but that bounds the SWEEP, not its lease (see the "Lease/retry
  tuning" note below): it gets the same small, heartbeat-protected lease
  (``AGNES_SP_SWEEP_LEASE_S``) as every other long-running kind, plus
  NO automatic retry, same "an operator looks at a failed multi-hour run"
  rationale as ``corpus-extraction`` above. The walk/probe/persist body
  lives entirely in ``connectors.sharepoint.acl_sync.run_subtree_sweep``
  (own ``sharepoint.enabled`` gate, own per-connection cadence
  self-guard) — this kind's handler is a thin delegate. Registered
  UNCONDITIONALLY, same no-op posture as ``sharepoint-acl-sync`` above. Its
  output (each mirrored scope's ``excluded_subtrees``) is read straight off
  the connection's scope rows by the built-in crawler, which HONORS the
  exclusions itself.
- ``sharepoint-facts-extraction`` (EXTRACTION — shares ``corpus-extraction``'s
  lane) — run one fact-extraction pass over a connection's ALREADY-INDEXED
  corpus, without a crawl. Before this kind existed, ``maybe_run_after_crawl``
  chained onto a crawl's tail was the ONLY way this pass ever ran — so an
  operator wanting to (re)build the graph over documents already sitting in
  ``corpus_files`` had no answer but "re-run the whole crawl", and a crawl
  that ran long could leave the chained pass with none of its own time
  (observed live: a 900s crawl left it an already-expired deadline, stopping
  it after 3 documents). This kind gives the pass its own trigger (``POST
  …/connections/{id}/facts-extract``, ``agnes admin sharepoint
  facts-extract``) and its own wall-clock budget
  (``extraction.facts.run_timeout_s``, independent of the crawl's
  ``extraction.timeout_s``). A thin delegate to
  ``connectors.sharepoint.facts_extraction.run_standalone_facts_extraction``
  — same posture as every other kind here: this handler owns only the
  ``sharepoint.enabled`` gate, the two facts-specific switches
  (``extraction.facts.enabled`` / ``facts.enabled``) are that function's own
  job. Registered UNCONDITIONALLY, same no-op posture as
  ``sharepoint-acl-sync`` above.

Every handler below is a THIN ADAPTER — it imports and calls the existing
function/method and does not reimplement any of its logic. Each import is
deferred (inside the handler, not at module import time) for the same
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

Lease/retry tuning: a job kind's lease is a LIVENESS ceiling, not a
DURATION ceiling. ``app/worker/runtime.py``'s heartbeat renews it every
``lease_seconds/3`` for as long as the handler thread is alive, so the
lease only has to survive the GAP BETWEEN TWO HEARTBEAT TICKS, never the
whole run. ``data-refresh``, ``corpus-extraction`` and
``sharepoint-subtree-sweep`` used to be sized to their own expected
DURATION instead (900s / timeout_s+margin / 14400s respectively) — that
buys a live run nothing the heartbeat wasn't already doing, and costs a
dead one everything: a worker killed mid-job (observed live — a native
crash in a converter backend, twice in one afternoon) leaves its job
``status='running'`` with a dead ``leased_by`` until ``lease_expires_at``
passes, so a duration-sized lease is a duration-sized wait before
``claim_next()``'s crash-recovery reclaim can even see it. All three now
share ``_DEFAULT_HEARTBEAT_PROTECTED_LEASE_S`` (300s, the same value the
LIGHT kinds below already run at) regardless of how long their own work
may legitimately take:

- ``data-refresh`` (``AGNES_DATA_REFRESH_LEASE_S``, default 300s — also
  the lease ``analytics-migrate``/``analytics-rebuild`` reuse via
  ``_data_refresh_lease_seconds()``) — a full Keboola extractor subprocess
  run + materialized pass + orchestrator rebuild can legitimately take
  much longer than 300s on a large registry; that's fine, the heartbeat
  is what keeps a genuinely running sync's lease alive, not the lease's
  own size.
- ``jira-refresh`` is also HEAVY (shares the lane with ``data-refresh``,
  and both run through ``_sweep_stale_scratch()`` before every HEAVY
  claim — see ``app/worker/runtime.py``) but is a plain orchestrator
  rebuild (re-ATTACH + view creation over already-written parquet), so a
  much shorter lease (300s) is plenty.
- The LIGHT kinds (``marketplaces-sync``, ``session-collector``,
  ``corporate-memory``) default to 300s — bulk git clones / LLM catalog
  refresh / filesystem walks, but bounded by their own internal
  timeouts, not multi-minute by design.
- ``corpus-extraction`` (``_DEFAULT_EXTRACTION_LEASE_S``, 300s, no env
  override) — the run's own wall-clock bound is ``extraction.timeout_s``
  (default 3600s), enforced INSIDE the crawl between files and between
  delta pages; the lease no longer tracks it at all (it used to, plus a
  margin — the exact "long job = long lease" mistake this note warns
  against: a crashed crawl's job stayed unreclaimable for up to an hour).
  No retry by default: a failed run (bad credentials, a crawl
  error, an exhausted throttle budget) usually needs an operator to look
  at it, not an automatic re-run against the same corpus a few minutes
  later — and a resumed run picks up from the persisted crawl state
  anyway.
- ``sharepoint-subtree-sweep`` (``AGNES_SP_SWEEP_LEASE_S``, default
  300s) — a full probe pass over a large library is multi-hour (spec
  §6.2), which used to size the lease itself (4h, i.e. the same mistake
  as ``corpus-extraction`` above); same fix, same no-retry rationale (an
  operator looks at a failed multi-hour sweep, not an unattended re-run).

Two kinds still size their lease off their own expected duration
(``jira-org-refresh``'s ``_DEFAULT_JIRA_ORG_REFRESH_LEASE_S`` and
``ducklake-maintenance``'s ``_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S``) —
same shape of issue, deliberately left alone here rather than folded into
this fix (out of this change's declared scope; a good follow-up).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC

from app.worker.registry import EXTRACTION_LANE, HEAVY_LANE, JOB_KINDS, LIGHT_LANE, JobKind, register_kind
from src.anonymization_key import ANONYMIZATION_HMAC_KEY_ENV_DEFAULT
from src.anonymization_key import AnonymizationKeyError as _AnonymizationKeyError
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

# Shared default for every kind whose OWN work can legitimately run long
# but is protected by the worker's heartbeat rather than by this lease's
# size — see the module docstring's "Lease/retry tuning" note for the full
# reasoning. 300s is not a new number: it's the value marketplaces-sync/
# session-collector/corporate-memory (_DEFAULT_LIGHT_LEASE_S below) already
# run at, so reusing it here is the conservative choice, not an invented
# one. A 300s lease gives a 100s heartbeat cadence (lease_seconds/3): a
# dead worker's job is reclaimable within minutes, and a live one has three
# full ticks of slack before the lease could ever lapse out from under it.
_DEFAULT_HEARTBEAT_PROTECTED_LEASE_S = 300
_DEFAULT_DATA_REFRESH_LEASE_S = _DEFAULT_HEARTBEAT_PROTECTED_LEASE_S
_DEFAULT_JIRA_REFRESH_LEASE_S = 300
# One API request per organization, gently paced — a few-hundred-organization site
# takes minutes, so the lease has to outlast the whole sweep or the job would be
# reclaimed mid-run and start over. At ~0.2s pacing plus request latency this covers
# roughly 3,500 organizations; an estate materially larger than that wants a
# size-derived lease rather than a bigger constant, or it will reclaim in a loop.
#
# NOTE: this sizing shares the same shape of issue _DEFAULT_HEARTBEAT_PROTECTED_LEASE_S
# above exists to fix (the heartbeat, not this constant, is what actually keeps a live
# sweep's lease alive) — left as-is here, out of this change's declared scope; a good
# follow-up.
_DEFAULT_JIRA_ORG_REFRESH_LEASE_S = 1800
_DEFAULT_LIGHT_LEASE_S = 300
# merge_adjacent_files/expire_snapshots/cleanup_old_files/VACUUM can each
# take a while over a large lake — same shape of issue
# _DEFAULT_HEARTBEAT_PROTECTED_LEASE_S above exists to fix; left as-is here,
# out of this change's declared scope, same follow-up note as
# _DEFAULT_JIRA_ORG_REFRESH_LEASE_S above.
_DEFAULT_DUCKLAKE_MAINTENANCE_LEASE_S = 900
# Expected ceiling on one extraction run (extraction.timeout_s in
# instance.yaml overrides this) — a full crawl+convert+anonymize+ingest
# pass over a real SharePoint site can legitimately run for a while. This
# is the run's own wall-clock bound, enforced INSIDE the crawl (between
# files and between delta pages) — unrelated to the job's lease below.
_DEFAULT_EXTRACTION_TIMEOUT_S = 3600
# corpus-extraction's lease used to be _extraction_timeout_seconds() plus a
# margin — tying crash-recovery speed to the run's own expected duration,
# the defect the module docstring's "Lease/retry tuning" note describes.
# It is now the same heartbeat-protected default as every other
# long-running kind, entirely independent of extraction.timeout_s.
_DEFAULT_EXTRACTION_LEASE_S = _DEFAULT_HEARTBEAT_PROTECTED_LEASE_S
# TCRD-296 C.11 — the extraction kinds' JobKind.transient_retry_in_seconds:
# a raised handler exception that `src.db_transient.is_transient_db_error`
# classifies as a connection-pool/deadlock/serialization hiccup requeues
# after this delay instead of finalizing on its first attempt (see
# `app/worker/runtime.py::_run_one`). Short relative to `retry_in_seconds`
# elsewhere in this module (300s) on purpose: this is a DB-layer blip, not a
# tenant-throttle or an outage worth minutes of backoff — long enough for
# connection-pool pressure to plausibly subside before the whole run
# restarts from its persisted per-document/per-item state.
_TRANSIENT_INGEST_RETRY_S = 60
# 2026-08-30 plan, Task 7: a full sharepoint-subtree-sweep pass (probing
# hasUniqueRoleAssignments over every folder in a mirrored scope) is
# multi-hour on a large library (spec §6.2's ~98k-folder reference) — that
# used to size the lease itself (4h), same "long job = long lease" mistake
# as corpus-extraction's old formula above. The heartbeat, not this lease,
# is what keeps a genuinely multi-hour sweep's lease alive, so this now
# gets the same small heartbeat-protected default as every other
# long-running kind too.
_DEFAULT_SP_SWEEP_LEASE_S = _DEFAULT_HEARTBEAT_PROTECTED_LEASE_S


def _data_refresh_lease_seconds() -> int:
    raw = os.environ.get("AGNES_DATA_REFRESH_LEASE_S")
    if raw is None:
        return _DEFAULT_DATA_REFRESH_LEASE_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_DATA_REFRESH_LEASE_S


def _sp_sweep_lease_seconds() -> int:
    raw = os.environ.get("AGNES_SP_SWEEP_LEASE_S")
    if raw is None:
        return _DEFAULT_SP_SWEEP_LEASE_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_SP_SWEEP_LEASE_S


def _run_data_refresh(payload: dict) -> dict | None:
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

    Result exposure (#1620): passes a sink dict into ``_run_sync``'s
    ``result_sink`` kwarg and returns it (the ``JobKind.handler`` contract
    already supports an ``Optional[dict]`` return — see ``app/worker/
    registry.py``'s ``JobKind`` docstring; ``agent_response`` was the
    first kind to use it) so ``GET /api/jobs/{id}``'s stored
    ``payload_json["result"]`` shows exactly which tables were
    materialized/skipped (and why — e.g. ``due_check``, ``not_in_target``)
    vs. errored on THIS run. Only reaches ``JobsRepository.complete(...,
    result=...)`` on the success path below — a raised ``RuntimeError``
    (the ``ok is False`` branch) still fails the job via ``.fail(...)``,
    which has no equivalent result slot; the per-table detail for a
    failed run remains visible in server logs and ``sync_state`` as
    before this change.
    """
    from app.api.sync import _run_sync

    result: dict = {}
    ok = _run_sync(payload.get("tables"), payload.get("source"), result_sink=result)
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
    return result or None


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
    the same ``dry_run=False`` default as that endpoint.

    issue #1971 Part 3: ``collect_all``'s return value used to be discarded
    entirely here — the scheduled collector ran every night with no durable
    trace of what it did. Now its stats feed one ``memory_detection_runs``
    row via the best-effort writer (never raises; degrades to a warning log
    line on a DuckDB-backed instance, where the table doesn't exist). The
    collector consults no editable policy (issue #1971 Part 2 left its
    structurally different prompt on the built-in default), so
    ``policy_text`` is omitted — the row's ``policy_fingerprint`` is
    ``NULL``, distinct from an empty-but-real policy.
    """
    from datetime import datetime

    from services.corporate_memory.collector import collect_all
    from src.memory_detection_logging import record_detection_run

    started_at = datetime.now(UTC)
    stats = collect_all(dry_run=False) or {}
    errors = stats.get("errors") or []
    record_detection_run(
        source="claude_local_md",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        sessions_scanned=stats.get("users_scanned", 0),
        items_proposed=stats.get("items_extracted", 0),
        items_filtered=stats.get("items_filtered", 0),
        items_inserted=stats.get("items_db_inserted", 0),
        # The collector's LLM path proposes no scope label — Part 2 left it
        # on its built-in prompt (structurally incompatible output schema),
        # so nothing here is ever routed to the engagement-scoped domain.
        items_routed_side_domain=0,
        dry_run=False,
        error="; ".join(str(e) for e in errors) if errors else None,
    )


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
        from datetime import datetime

        from app.observability.metrics import record_ducklake_snapshot_age

        row = conn.execute("SELECT max(snapshot_time) FROM ducklake_snapshots('lake')").fetchone()
        if row is None or row[0] is None:
            return
        snapshot_time = row[0]
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=UTC)
        else:
            snapshot_time = snapshot_time.astimezone(UTC)
        age_seconds = max((datetime.now(UTC) - snapshot_time).total_seconds(), 0.0)
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


#: Wall-clock budget for one ``knowledge-packaging`` run (TCRD-296 synthesis
#: C.15). A plain constant, not an env knob — the live incident this fixes
#: was an UNBOUNDED in-request run (the scheduler's 600s CLIENT timeout was
#: the only limit, and it didn't stop the server-side work), not a value
#: that needed tuning; ``run_packaging_pass``'s checkpoint-per-collection
#: means a run that hits this budget resumes cleanly next tick rather than
#: needing a bigger number. 20 minutes comfortably covers a full sweep of a
#: real instance's Collections while still leaving the LIGHT lane's other
#: kinds (``webhook-deliver``, ``agent_response``) a bounded wait behind it.
_DEFAULT_KNOWLEDGE_PACKAGING_TIMEOUT_S = 20 * 60


def _run_knowledge_packaging(payload: dict) -> dict:
    """``knowledge-packaging`` — rebuild per-collection ``knowledge.duckdb``
    artifacts whose chunk content changed (K3, #798; TCRD-296 synthesis
    C.15).

    Used to run INLINE inside ``POST /api/admin/run-knowledge-packaging``,
    HTTP-called by the scheduler on a 600s client timeout shorter than a
    real pass could take — a slow pass outlived that timeout, the next
    scheduler tick fired a SECOND overlapping call before the first
    finished, and the two collided (a shared per-corpus tmp DuckDB path —
    see ``src.knowledge_packaging``'s module docstring) hard enough to OOM
    the app process. This handler is now the only thing that runs the
    pass; the endpoint (``app/api/admin.py::run_knowledge_packaging``) is a
    thin enqueue.

    Single-run is enforced two ways: the idempotency-keyed enqueue (the
    endpoint's job) means a second scheduler tick while one run is still
    ``'queued'``/``'running'`` is a no-op, and — belt-and-braces, for a path
    that bypasses that dedupe (a manual ``POST /api/jobs``, or two workers
    racing to claim two different rows) — a non-blocking Postgres advisory
    lock (:func:`src.db_pg.knowledge_packaging_lease`). Skipping (not
    failing) when the lock is already held is the correct outcome: the
    other run is doing the exact same work.

    ``run_packaging_pass``'s own ``deadline`` argument bounds this run's
    wall-clock cost at :data:`_DEFAULT_KNOWLEDGE_PACKAGING_TIMEOUT_S` —
    see that function's docstring for the checkpoint-per-collection
    contract that makes a mid-sweep interruption resumable rather than a
    lost pass. Returns the pass's summary dict (built/skipped/pruned/
    errors/interrupted_reason/duration_s/collections_total/
    collections_processed) as the job's result
    (``GET /api/jobs/{id}``'s ``payload_json["result"]``) — this is what
    makes ``GET /api/admin/knowledge-packaging/status`` a measurement of
    the last real run rather than a guess.
    """
    from src.db_pg import knowledge_packaging_lease
    from src.knowledge_packaging import run_packaging_pass

    with knowledge_packaging_lease() as acquired:
        if not acquired:
            logger.info(
                "knowledge-packaging: advisory lock already held by another run — skipping "
                "(belt-and-braces on top of the idempotency-key dedupe; the other run covers this work)"
            )
            return {"skipped": "lock_held"}
        deadline = time.monotonic() + _DEFAULT_KNOWLEDGE_PACKAGING_TIMEOUT_S
        return run_packaging_pass(deadline=deadline)


def _extraction_timeout_seconds() -> int:
    from app.instance_config import get_value

    raw = get_value("extraction", "timeout_s", default=_DEFAULT_EXTRACTION_TIMEOUT_S)
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return _DEFAULT_EXTRACTION_TIMEOUT_S


# The per-instance anonymization HMAC key (spec §9.2) is resolved — and, as
# of owner decision 2026-09-01, PROVISIONED — by ``src/anonymization_key.py``.
# The error type and the default env NAME are re-exported here because this
# module is where both have always been imported from
# (``connectors/sharepoint/crawler.py``, the worker tests); moving the
# implementation must not move the import path out from under those callers.
AnonymizationKeyError = _AnonymizationKeyError
_ANONYMIZATION_HMAC_KEY_ENV_DEFAULT = ANONYMIZATION_HMAC_KEY_ENV_DEFAULT


def _resolve_anonymization_key() -> str:
    """Resolve this instance's per-instance anonymization HMAC key (spec §9.2).

    A thin delegate to :func:`src.anonymization_key.resolve_or_provision_key`
    — the single owner of that resolution, including the allowlist gate on
    the admin-writable env var NAME (a security control that must have
    exactly one implementation) and, when no operator key is configured, the
    write-once generate-and-store path that makes the key zero-ops.

    Precedence, unchanged at the top and extended below it: an
    operator-minted env key (``extraction.anonymization.hmac_key_env``,
    allowlist-checked) always wins; otherwise the vault-stored instance key;
    otherwise one is generated and stored; otherwise
    :class:`AnonymizationKeyError`, never a fallback or empty key.

    Kept as a module-level function with this exact name/signature because
    ``connectors/sharepoint/crawler.py`` imports it by name.
    """
    from src.anonymization_key import resolve_or_provision_key

    return resolve_or_provision_key().decode("utf-8")


def _run_corpus_extraction(payload: dict) -> dict:
    """``corpus-extraction`` — run the built-in document pipeline for one
    SharePoint connection (spec §7.5 / §16 step 7).

    A thin delegate, exactly like ``_run_sharepoint_acl_sync`` /
    ``_run_sharepoint_subtree_sweep`` below: the crawl -> convert ->
    (anonymize) -> ingest body, its Graph transport hardening, its resumable
    crawl state, its per-scope -> per-collection routing, its fail-closed
    anonymization and its oversize/permission/convert-failure accounting all
    live in ``connectors.sharepoint.crawler``. This handler owns exactly one
    thing the crawler does not: the ``sharepoint.enabled`` gate.

    Owner decision 2026-08-31: the built-in pipeline is the ONLY pipeline.
    The external-producer subprocess this handler used to shell out to — and
    with it ``extraction.producer.*``, the curated child-env allowlist, the
    ``AGNES_EXTRACTION_*``/``AGNES_SP_EXCLUDED_SUBTREE_IDS`` handoff vars and
    the Admin-grade ``AGNES_API_TOKEN`` callback over-grant — is gone. There
    is no argv, so no secret can reach one; credentials are resolved inside
    the crawler through ``connectors.sharepoint.settings
    .resolve_sharepoint_settings``, the SAME path the SharePoint admin UI
    uses (security playbook F7).

    ``payload``:
      - ``connection_id`` (required) — a ``source_connections`` row,
        ``source_type='sharepoint'``. Validated by the crawler, which raises
        a named ``CrawlError`` rather than a bare traceback.
      - ``scopes`` (optional) — ``source_scope_id``s to narrow the run to;
        every confirmed scope otherwise.
      - ``timeout_s`` (optional) — overrides ``extraction.timeout_s`` for
        this one run (0 = unbounded). The crawl enforces it itself, between
        files and between delta pages; this handler's own lease is a fixed,
        heartbeat-protected constant (``_DEFAULT_EXTRACTION_LEASE_S`` — see
        the module docstring's lease/retry tuning note) that does not track
        ``extraction.timeout_s`` at all, so a payload override — in either
        direction — has no bearing on how quickly a crashed run's job is
        reclaimed.
      - ``concurrency`` (optional) — overrides
        ``extraction.crawler.concurrency`` for this one run, clamped to
        ``[1, 16]``: how many files of ONE delta page the crawl pipelines at
        a time (``1`` = the sequential pre-parallel behaviour). NOT the same
        knob as ``extraction.concurrency``, which sizes how many extraction
        JOBS this worker runs at once — the two multiply against one tenant.
      - ``resync`` (optional, truthy) — drops this connection's persisted
        deltaLinks and item-failure queue before crawling, so every drive
        re-enumerates from scratch (cTags are kept, so unchanged files are
        not re-downloaded). The supported recovery path for a connection
        whose delta cursor ran past documents it never actually ingested —
        see ``connectors.sharepoint.crawler._apply_resync``.
      - ``force_reprocess`` (optional, truthy) — ignores BOTH the persisted
        deltaLinks and cTags for this one run, so every item is
        re-downloaded, re-converted and re-ingested even when it looks
        unchanged. The operator control for "re-process everything" — e.g.
        a converter or anonymizer setting changed and the content on disk
        did not. Unlike ``resync``, never written to the state file up
        front: a run interrupted mid-way leaves the connection exactly as
        resumable as it was before — see
        ``connectors.sharepoint.crawler._crawl_drive``.

    Returns the crawl report (the same dict persisted as ``last_run`` in the
    connection's crawl state, so the job result and the state file can never
    disagree about what a run did).

    No-op guard: raises (so the job fails cleanly) when ``sharepoint.enabled``
    is false — the same "off unless explicitly turned on" posture as
    ``ducklake-maintenance``'s backend check, just failing instead of
    silently returning, since a ``corpus-extraction`` job only ever exists
    because something explicitly enqueued it.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        raise RuntimeError("corpus-extraction: sharepoint.enabled is false — refusing to run")

    from connectors.sharepoint.crawler import run_builtin_crawl

    return run_builtin_crawl(payload)


def _run_corpus_extraction_shard(payload: dict) -> dict:
    """``corpus-extraction-shard`` (2026-09-03 auto-parallel-crawl design
    §4.3) — one shard child's own crawl, enqueued by a ``corpus-extraction``
    run that decided to plan rather than crawl inline
    (``connectors.sharepoint.crawler._plan_or_run_inline``).

    A thin delegate, exactly like ``_run_corpus_extraction`` above: the
    shard's own targets, its per-delta-unit state rows, its own
    ``extraction_runs`` row, and the finalize-on-last-child coordination all
    live in ``connectors.sharepoint.crawler.run_shard_crawl``. This handler
    owns exactly the same one thing ``_run_corpus_extraction`` does: the
    ``sharepoint.enabled`` gate.

    ``payload``: ``connection_id``, ``parent_run_id``, ``shard_index``,
    ``shard`` — see ``run_shard_crawl``'s own docstring for the full shape.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        raise RuntimeError("corpus-extraction-shard: sharepoint.enabled is false — refusing to run")

    from connectors.sharepoint.crawler import run_shard_crawl

    return run_shard_crawl(payload)


def _run_sharepoint_acl_sync(payload: dict) -> dict:
    """Thin delegate to ``connectors.sharepoint.acl_sync.run_acl_sync`` — the
    sync body (read → classify → resolve → diff → write, per-connection
    failure isolation, the must_not/should_not staleness fork) lives entirely
    in that module (2026-08-30 plan, Task 4); this handler imports and calls
    it and does not reimplement any of its logic, same as every other kind
    in this file."""
    from connectors.sharepoint.acl_sync import run_acl_sync

    return run_acl_sync(payload)


def _run_sharepoint_subtree_sweep(payload: dict) -> dict:
    """Thin delegate to ``connectors.sharepoint.acl_sync.run_subtree_sweep``
    — the walk/probe/persist body (broken-inheritance subtree detection,
    2026-08-30 plan, Task 7) lives entirely in that module; this handler
    imports and calls it and does not reimplement any of its logic, same as
    every other kind in this file."""
    from connectors.sharepoint.acl_sync import run_subtree_sweep

    return run_subtree_sweep(payload)


def _run_sharepoint_facts_extraction(payload: dict) -> dict:
    """``sharepoint-facts-extraction`` — run ONE fact-extraction pass over a
    SharePoint connection's ALREADY-INDEXED corpus, without running a crawl
    first. The operator's own trigger — "how do we get the fact graph
    populated with what we already have?" required re-running an entire
    crawl before this kind existed, which is absurd for a corpus already
    sitting in ``corpus_files``.

    A thin delegate, exactly like ``_run_corpus_extraction`` above: the
    walk, the model calls, the verbatim gate, the ingest batching and the
    per-document state all live in
    ``connectors.sharepoint.facts_extraction.run_standalone_facts_extraction``
    — which ALSO enforces the stage's own two cost/surface gates
    (``extraction.facts.enabled`` / ``facts.enabled``), loudly, via
    ``FactsExtractionDisabled``, rather than this handler duplicating that
    check. This handler owns exactly one thing that function does not: the
    ``sharepoint.enabled`` gate, same posture as ``corpus-extraction``.

    ``payload``:
      - ``connection_id`` (required)
      - ``doc_ids`` (optional list[str]) — narrow the pass to specific
        documents (``corpus_file_sources.source_doc_id``) — the "test one
        document" path already supported by ``run_facts_extraction`` and
        threaded straight through here.
      - ``timeout_s`` (optional) — overrides ``extraction.facts.run_timeout_s``
        for this one run (0 = unbounded). Its OWN budget, never the crawl's
        ``extraction.timeout_s`` — see
        ``run_standalone_facts_extraction``'s docstring for why a
        crawl-chained pass sharing the crawl's own deadline is exactly the
        problem a standalone trigger with its own budget avoids (observed
        live: a 900s crawl left the chained pass an already-expired
        deadline, stopping it after 3 documents).

    Returns the pass report (see
    ``connectors.sharepoint.facts_extraction._Report.render``). When this
    report says ``interrupted: timeout`` and the connection's ledger still
    has documents pending, the worker automatically chains the next pass
    onto this one (TCRD-296 gap #61) — that decision runs from ``app/
    worker/runtime.py``'s post-``complete()`` hook
    (``_maybe_continue_facts_extraction`` ->
    ``connectors.sharepoint.facts_extraction.maybe_continue_pass``), NEVER
    from inside this handler: the continuation reuses this job's own
    idempotency key, and enqueuing it before THIS job leaves ``'running'``
    would self-collide against its own still-live row.

    No-op guard: raises (so the job fails cleanly) when ``sharepoint.enabled``
    is false — same "this job only ever exists because something explicitly
    enqueued it" rationale as ``corpus-extraction`` above.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        raise RuntimeError("sharepoint-facts-extraction: sharepoint.enabled is false — refusing to run")

    from connectors.sharepoint.facts_extraction import run_standalone_facts_extraction

    connection_id = str(payload["connection_id"])
    doc_ids = payload.get("doc_ids")
    timeout_s = payload.get("timeout_s")
    return run_standalone_facts_extraction(connection_id, doc_ids=doc_ids, timeout_s=timeout_s)


#: Kinds whose payload gets this claimed job's own ``id`` merged in before
#: the handler runs — see :func:`_payload_for_handler`. A plain set, not a
#: per-kind flag on ``JobKind``: ``corpus-extraction`` and
#: ``corpus-extraction-shard`` (2026-09-03 auto-parallel-crawl design §4.3)
#: are the only two with anywhere to put it (``extraction_runs.job_id``,
#: on the run each one opens for itself), and a third consumer can add
#: itself here without a registry shape change.
_INJECT_JOB_ID_KINDS = frozenset({"corpus-extraction", "corpus-extraction-shard"})


def _payload_for_handler(job: dict) -> dict:
    """The payload a handler runs with — ``job["payload_json"]`` unchanged,
    except for :data:`_INJECT_JOB_ID_KINDS`, which get this claimed job's own
    ``id`` merged in as ``job_id`` when the payload does not already carry
    one.

    ``kind.handler`` (the ``JobKind`` contract, ``app/worker/registry.py``)
    only ever receives the payload dict — never the job row — so this is the
    one seam that can hand a handler its own job's id without widening that
    contract for every kind. ``connectors.sharepoint.crawler.run_builtin_
    crawl`` records it on the ``extraction_runs`` row it opens
    (``job_id``), which is what makes a run traceable back to the job that
    spawned it; before this it was always null, because nothing upstream of
    here ever supplied it (see that function's own docstring).

    Never mutates ``job["payload_json"]`` in place: a copy, so a payload
    that started with no ``job_id`` does not gain one behind the caller's
    back if it is inspected again after dispatch.
    """
    payload = job.get("payload_json") or {}
    if job.get("kind") in _INJECT_JOB_ID_KINDS and isinstance(payload, dict) and not payload.get("job_id"):
        return {**payload, "job_id": job.get("id")}
    return payload


def dispatch_job(job: dict) -> dict | None:
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
    live (one dispatch-level wrapper, not one per kind), and (see
    :func:`_payload_for_handler`) the one place a handler's payload can be
    enriched with the job's own id without widening ``JobKind.handler``'s
    contract for every kind.

    Runs outside any HTTP request — there is no ASGI scope for
    ``src.audit_context``'s autofill to read, so ``duration_ms`` is
    measured explicitly here and ``client_kind="scheduler"`` is always
    passed. ``user_id=None``: a scheduled/worker job has no human caller to
    attribute the row to.
    """
    kind = JOB_KINDS[job["kind"]]
    t0 = time.monotonic()
    try:
        result = kind.handler(_payload_for_handler(job))
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
            name="knowledge-packaging",
            handler=_run_knowledge_packaging,
            lane=LIGHT_LANE,
            # Heartbeat-protected, same default as the other LIGHT kinds —
            # NOT sized to the pass's own duration (bounded separately, by
            # _DEFAULT_KNOWLEDGE_PACKAGING_TIMEOUT_S, enforced inside
            # run_packaging_pass via `deadline`). See the module docstring's
            # lease/retry tuning note.
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
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
            # Heartbeat-protected, NOT tied to extraction.timeout_s (the
            # crawl's own wall-clock bound, enforced separately, inside the
            # crawl) — see the module docstring's lease/retry tuning note.
            lease_seconds=_DEFAULT_EXTRACTION_LEASE_S,
            # No automatic retry: a failed run (bad credentials, a crawl
            # error, an exhausted throttle budget) needs an operator to look
            # at it, not an unattended re-run a few minutes later.
            retry_in_seconds=None,
            # ...UNLESS the raised exception is a TRANSIENT infrastructure
            # fault (TCRD-296 C.11) — see `_TRANSIENT_INGEST_RETRY_S`.
            transient_retry_in_seconds=_TRANSIENT_INGEST_RETRY_S,
        )
    )
    register_kind(
        JobKind(
            name="corpus-extraction-shard",
            handler=_run_corpus_extraction_shard,
            # Same lane as corpus-extraction — a shard child IS a crawl,
            # just over a narrower set of targets.
            lane=EXTRACTION_LANE,
            # Same lease shape as corpus-extraction above.
            lease_seconds=_DEFAULT_EXTRACTION_LEASE_S,
            # No automatic retry — same rationale as corpus-extraction: a
            # failed shard needs an operator to look at it, not an
            # unattended re-run. (Re-running just this shard is still
            # possible via `POST …/extract` with `shards: [index]` —
            # app/api/admin_sharepoint.py, Task 5.)
            retry_in_seconds=None,
            # Same TCRD-296 C.11 opt-in as corpus-extraction above.
            transient_retry_in_seconds=_TRANSIENT_INGEST_RETRY_S,
        )
    )
    register_kind(
        JobKind(
            name="sharepoint-acl-sync",
            handler=_run_sharepoint_acl_sync,
            lane=LIGHT_LANE,
            lease_seconds=_DEFAULT_LIGHT_LEASE_S,
            retry_in_seconds=300,
        )
    )
    register_kind(
        JobKind(
            name="sharepoint-subtree-sweep",
            handler=_run_sharepoint_subtree_sweep,
            lane=LIGHT_LANE,
            # Heartbeat-protected, same default LIGHT-kind lease as every
            # other kind here — NOT sized to the probe pass's own multi-hour
            # duration (spec §6.2). See the module docstring's lease/retry
            # tuning note.
            lease_seconds=_sp_sweep_lease_seconds(),
            # No automatic retry — same rationale as corpus-extraction: a
            # failed multi-hour sweep (throttling, a Graph outage mid-walk)
            # needs an operator to look at it, not an unattended re-run.
            retry_in_seconds=None,
        )
    )
    register_kind(
        JobKind(
            name="sharepoint-facts-extraction",
            handler=_run_sharepoint_facts_extraction,
            # Same lane as corpus-extraction: an LLM-calling document-
            # processing stage, the same cost/resource class — shares its
            # concurrency ceiling rather than getting its own.
            lane=EXTRACTION_LANE,
            # Heartbeat-protected, NOT tied to the pass's own
            # extraction.facts.run_timeout_s (its wall-clock bound, enforced
            # separately inside run_standalone_facts_extraction, between
            # documents) — same shape as corpus-extraction's own lease
            # above. See the module docstring's lease/retry tuning note.
            lease_seconds=_DEFAULT_EXTRACTION_LEASE_S,
            # No automatic retry: a failed pass (no model credential, an
            # exhausted retry budget) needs an operator to look at it, not
            # an unattended re-run — and a resumed run already picks up
            # from the persisted per-document state anyway, same rationale
            # as corpus-extraction above.
            retry_in_seconds=None,
            # Same TCRD-296 C.11 opt-in as corpus-extraction above.
            transient_retry_in_seconds=_TRANSIENT_INGEST_RETRY_S,
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
