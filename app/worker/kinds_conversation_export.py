"""``conversation-export`` worker job kind — the push sink for the
conversation-corpus export (design 2026-09-08 §3.12, Task 11).

Task 10 built the PULL side (``GET /api/admin/conversations/corpus`` —
``app/api/conversations_export.py``): an admin (or a data platform with a
PAT) asks for a window and gets it back. This is the PUSH side: a
scheduled ``conversation-export`` job reads a Postgres-persisted watermark
(``export_watermarks``, keyed by :func:`watermark_name` — never a single
fixed row, see below), walks every conversation completed since it via
the SAME record builder (``src.conversation_export.iter_conversations``),
and POSTs newline-delimited JSON batches to
``observability.conversation_export.endpoint`` — advancing the watermark
only after a batch's destination answers 2xx, so a failed delivery is
retried on the next tick rather than silently skipped, and a delivered
one is never resent from scratch.

**The watermark is a keyset position, not a record timestamp.** It is
``(last_message_at, id)`` of the last DELIVERED ``chat_sessions`` row —
straight from ``iter_conversations``'s own ``keys`` return, never from a
delivered record's own ``conversation_end`` (the last MESSAGE's
timestamp, which can diverge from the session's ``last_message_at`` — a
forked session bumps the latter without moving the former). Resuming with
``list_completed_between(..., after=(watermark, cursor_id))`` — the same
strict ``>`` keyset clause an in-run page uses to turn its own page — is
what makes a run's first page and a run's Nth page (and the FIRST page of
the NEXT run) all resume identically, and is what stops the trailing
conversation of a run from being re-sent on every subsequent tick.

**A second, coarser watermark sweeps feedback that lands late.** The main
keyset walk above only ever advances past a ``chat_sessions`` row once, so
a thumbs-up/down (or a comment edit) filed AFTER a conversation was already
delivered would sit in ``chat_message_feedback`` forever with no cursor
that ever revisits it -- the delivered record's ``feedback_json`` would
stay empty for good. After the main walk, this job additionally sweeps
:func:`src.repositories.chat_message_feedback_pg.ChatMessageFeedbackPgRepository.
session_ids_updated_between` for the ids whose feedback changed since the
LAST aux sweep, re-builds and re-POSTs just those records (through the
exact same :func:`src.conversation_export.records_for_session_ids` builder,
batching and retry path -- the destination already upserts by
``thread_id``, so a re-delivered record simply replaces the stale one),
and only then advances a SECOND watermark row
(:func:`feedback_watermark_name` -- the same ``export_watermarks`` table,
one row per delivery configuration, suffixed ``:feedback`` so it never
collides with the main one). A session the main walk already delivered in
the SAME run is skipped here -- its feedback (whatever existed at query
time) was already read into that record by the same bulk
``feedback.list_for_sessions`` call every record build uses. Memory-status
changes (``agent_memories``) are NOT covered by this sweep: that table has
no single ``updated_at`` column -- only
``created_at``/``activated_at``/``archived_at`` -- so there is no one
timestamp to sweep on without a migration, out of scope for this fix.

**The aux sweep's watermark is a keyset position too, capped but
resumable.** :func:`session_ids_updated_between` is capped at
:data:`FEEDBACK_SWEEP_LIMIT` ids per call, but the sweep's own watermark is
a ``(timestamp, session_id)`` pair advanced only to the last id it actually
SCANNED -- exactly the main watermark's shape, not a bare timestamp. A page
that comes back full advances the watermark to that last scanned id rather
than jumping to this run's ``until``, so the NEXT run resumes the same
burst with ``after=(that timestamp, that id)`` instead of silently
dropping every id past the cap; a page that comes back short (the window
is exhausted) advances all the way to ``until``. A feedback burst wider
than the cap in one window therefore spreads over as many runs as it takes
to drain, never loses a straggler.

**``surfaces`` is pushed into the query, not filtered after the fact --
for BOTH watermarks.** ``observability.conversation_export.surfaces``
reaches ``list_completed_between``'s own ``surface = ANY(...)`` clause via
``iter_conversations`` for the main walk, and
``session_ids_updated_between``'s own join+filter for the aux sweep, so a
row this instance will never deliver is never fetched at all by either --
an excluded surface never causes a filtered tail to be re-fetched and
re-discarded on every tick, and never consumes a slot in the aux sweep's
capped page ahead of an included surface's feedback update.

**The watermark identity tracks the delivery configuration, not a fixed
name.** :func:`watermark_name` derives the ``export_watermarks.name`` row
from a hash of ``endpoint`` and the sorted ``surfaces`` allowlist
(prefixed :data:`WATERMARK_PREFIX`), so two runs with the SAME endpoint
and SAME surfaces resume the SAME cursor exactly as before, while a run
whose endpoint changed or whose surfaces widened/narrowed resolves to a
DIFFERENT row and starts fresh at the epoch for that configuration. This
is deliberate: resuming the OLD cursor after such a change would mean the
new destination (or the newly-included surface) never receives the
conversations completed before the change. The old watermark row is left
in place, harmless. The consequence is that changing ``endpoint`` or
``surfaces`` RE-DELIVERS the whole corpus under the new configuration —
the destination collector must upsert by ``thread_id``, exactly as it
already must for a forked/continued session (see below and the settle
window paragraph).

**A conversation is walked only once it has settled for
``SETTLE_WINDOW`` (5 minutes).** This job sets ``until = now -
SETTLE_WINDOW`` rather than ``now`` when calling ``list_completed_between``,
so a session whose ``last_message_at`` was just bumped by a USER message
that has no assistant answer yet is left off this run's page instead of
being exported (and its watermark advanced past) mid-turn — a later tick
re-checks a fresh ``now`` and picks it up once it is quiet. An
interrupted turn (a user message that never gets answered) still exports
once the window passes, unchanged from today. A thread that continues
after being exported is re-exported later with the fuller transcript once
it settles again, so here too the destination must upsert by
``thread_id``.

**Registered unconditionally, no-ops when unconfigured** — the same
posture as ``distribution-mirror``/``ducklake-maintenance`` in
``app/worker/kinds.py``: a stray/manual enqueue on an instance that never
set ``observability.conversation_export.endpoint`` is a clean, logged
no-op, not an error. ``services/scheduler/__main__.py``'s own tick ALSO
only enqueues the job when the endpoint is configured — this is the
belt-and-braces re-check, not the primary gate.

**PG-only, fails clean on DuckDB.** ``export_watermarks`` and the tables
the export reads (``llm_calls``, ``chat_message_feedback``) are all
Postgres-only (A3 ratchet). ``_repo_bundle`` below resolves the PG-only
repos FIRST, exactly like ``app/api/conversations_export.py
::_export_repo_bundle_deps`` — so a DuckDB-backed instance raises
``RequiresPostgresBackend`` before any other work happens, and this
module's own top-level handler swallows that into a clean, logged skip
(never an unhandled exception reaching the worker loop).

**Under the content-export policy** (spec 3.6/3.12), exactly like the pull
endpoint: a ``mode`` of ``off`` (no basis/approver, or workload ``chat``
excluded from ``workloads``) means no request is made at all and the
watermark never moves. Warned once per process, not once per tick — an
operator who never turns the policy on should not get a scheduler-cadence
flood of the same warning.

**Errors never escape this job** (spec 3.9: a sink swallows its own
exceptions). A ``RequiresPostgresBackend`` becomes a clean skip; any other
exception raised mid-walk is caught, logged, audited with
``result="error:<ClassName>"`` with the exception's CLASS NAME (never its message,
which could carry content or a header value) in ``params``, and the job
returns ``{"failed": "<ClassName>"}`` rather than raising — the worker
runtime's own generic failure handling is for a truly unexpected fault
elsewhere in the dispatch path, not for this job's own delivery loop.

Single-run discipline: the scheduler's own enqueue is idempotency-keyed
(``idempotency_key="conversation-export"``, see
``services/scheduler/__main__.py``) so a second tick while one run is
still queued/running is a no-op at the QUEUE level; this module's
``run_conversation_export`` additionally takes a non-blocking Postgres
advisory lock (``src.db_pg.conversation_export_lease``) as belt-and-braces
against a stray manual enqueue or two worker replicas racing to claim two
different job rows — the exact same two-layer shape ``knowledge-packaging``
already uses (see ``app/worker/kinds.py::_run_knowledge_packaging``). The
lease is acquired only AFTER the cheap config/policy gates below (no DB
connection at all on an unconfigured or policy-off instance).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.audit_helpers import log_safe
from src.conversation_export import (
    ConversationExportRepoBundle,
    encode_cursor,
    iter_conversations,
    records_for_session_ids,
    serialize_jsonl,
)
from src.observability.content_policy import content_export_mode, export_text, load_content_export_policy
from src.observability.otel import parse_otlp_headers

logger = logging.getLogger(__name__)

#: The ``jobs.kind`` string this module owns and the prefix for the
#: ``export_watermarks.name`` row :func:`watermark_name` derives — kept as
#: named constants rather than repeated literals so the scheduler/registry/
#: repo layers can't drift on the spelling.
KIND = "conversation-export"
WATERMARK_PREFIX = "conversation_export"
IDEMPOTENCY_KEY = "conversation-export"

#: A conversation is walked only once its ``last_message_at`` is at least
#: this old — see the module docstring's "settle window" paragraph. A
#: user message with no assistant answer yet bumps ``last_message_at``
#: before the record is complete; leaving it off this run's page (rather
#: than exporting a half-turn and advancing the watermark past it) means a
#: later tick re-checks a fresh "now" and picks it up once it is quiet.
SETTLE_WINDOW = timedelta(minutes=5)

#: Spec 3.12: "batches of at most 200 records or 8 MiB".
MAX_BATCH_RECORDS = 200
MAX_BATCH_BYTES = 8 * 1024 * 1024
#: Spec 3.12 / docs/observability.md: "retried three times" — three RETRIES
#: after the first attempt, four attempts total, exponential backoff
#: (1s, 2s, 4s between attempts). A non-5xx response (2xx success, or a
#: 4xx — incl. 429 — the destination will never accept on retry within
#: this run) never consumes a retry; only a 5xx or a connection-level
#: error does.
MAX_ATTEMPTS = 4
RETRY_BACKOFF_S = 1.0
HTTP_TIMEOUT_S = 30.0
#: Page size for the underlying ``iter_conversations`` walk — independent
#: of the OUTBOUND batch size above (a page becomes one or more outbound
#: batches once the byte cap is applied).
PAGE_LIMIT = 200

#: Cap on how many session ids the "late feedback" aux sweep re-exports in
#: one run — see the module docstring's "second, coarser watermark"
#: paragraph. Independent of PAGE_LIMIT: this walks `chat_message_feedback`
#: by change timestamp, never `chat_sessions` by keyset.
FEEDBACK_SWEEP_LIMIT = 500

#: A watermark-less first run exports every conversation ever held —
#: deliberate: this is an opt-in feature (an operator must set an
#: ``endpoint``), so a first run backfilling the whole transcript is the
#: expected behaviour, not a runaway scan.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def watermark_name(endpoint: str, surfaces: tuple[str, ...]) -> str:
    """The ``export_watermarks.name`` row for one delivery configuration —
    see the module docstring's "watermark identity" paragraph. A short
    hash of ``endpoint`` and the SORTED ``surfaces`` allowlist (order must
    not matter — ``("web", "slack")`` and ``("slack", "web")`` are the
    same configuration), prefixed :data:`WATERMARK_PREFIX`. Two calls with
    the same arguments always return the same name; changing either
    argument returns a different one, which is what makes a repointed
    endpoint or a widened/narrowed surfaces list start a fresh cursor
    rather than silently resuming the old configuration's position.
    """
    digest = hashlib.sha256(f"{endpoint}\n{','.join(sorted(surfaces))}".encode()).hexdigest()[:16]
    return f"{WATERMARK_PREFIX}:{digest}"


def feedback_watermark_name(endpoint: str, surfaces: tuple[str, ...]) -> str:
    """The "late feedback" aux sweep's own ``export_watermarks.name`` row --
    a thin wrapper around :func:`watermark_name` (same endpoint+surfaces
    identity, same reasoning for why it changes when either does), suffixed
    ``:feedback`` purely so the two cursors for one delivery configuration
    never collide in the shared table. Kept as a wrapper -- never a second,
    independent hash -- so the two names can never drift on how the base
    identity is derived.
    """
    return f"{watermark_name(endpoint, surfaces)}:feedback"


#: One warning per process for "policy excludes workload chat" — the
#: scheduler ticks hourly by default; nobody needs that warning hourly.
_warned_policy_off = False


def _warn_policy_off_once() -> None:
    global _warned_policy_off
    if not _warned_policy_off:
        logger.info(
            "conversation-export: content-export policy excludes workload 'chat' "
            "(mode is off, has no recorded basis, or its workloads allowlist excludes "
            "'chat') -- the push sink will not run until an operator completes "
            "observability.content_export in instance.yaml"
        )
        _warned_policy_off = True


def _quick_skip_reason() -> str | None:
    """The two gates that need no database connection at all: is the sink
    configured, and does the content-export policy allow workload
    ``chat``. Checked BEFORE the advisory lease (``run_conversation_export``
    below) so a stray/manual enqueue on an unconfigured or policy-off
    instance never opens a Postgres connection for nothing. Returns the
    skip reason, or ``None`` when the run should proceed — in which case
    :func:`run_conversation_export_once` re-checks both anyway (cheap, and
    it is also called directly without going through this gate in tests
    and in a hand-run pass).
    """
    from app.instance_config import get_conversation_export_config

    if get_conversation_export_config() is None:
        return "not_configured"
    if content_export_mode(workload="chat") == "off":
        _warn_policy_off_once()
        return "content_export_disabled"
    return None


def _repo_bundle() -> dict[str, Any]:
    """Resolve every repo the export needs, PG-only repos FIRST — the same
    ordering rationale as ``app/api/conversations_export.py
    ::_export_repo_bundle_deps``: resolving ``llm_calls_repo()`` (PG-only)
    before the DuckDB-capable ``chat_session_repo()``/``chat_message_repo()``
    is what makes a DuckDB-backed instance raise ``RequiresPostgresBackend``
    up front instead of reaching for a bulk-export method the DuckDB side
    never grew.
    """
    from src.repositories import (
        agent_memories_repo,
        chat_message_feedback_repo,
        chat_message_repo,
        chat_session_repo,
        llm_calls_repo,
        users_repo,
    )

    calls = llm_calls_repo()  # PG-only -> raises RequiresPostgresBackend on DuckDB
    feedback = chat_message_feedback_repo()  # PG-only -> same
    return {
        "sessions": chat_session_repo(),
        "messages": chat_message_repo(),
        "calls": calls,
        "feedback": feedback,
        "memories": agent_memories_repo(),
        "users": users_repo(),
    }


def _resolve_headers(headers_secret_env: str | None) -> dict[str, str]:
    """Auth headers for the outbound POST, parsed exactly like
    ``OTEL_EXPORTER_OTLP_HEADERS`` (``k1=v1,k2=v2``) from the environment
    variable NAMED by ``headers_secret_env`` — never the header value
    itself in ``instance.yaml``, mirroring the ``token_env`` indirection
    used throughout this codebase."""
    if not headers_secret_env:
        return {}
    raw = os.environ.get(headers_secret_env, "")
    return parse_otlp_headers(raw)


#: One yielded item: a corpus record paired with the exact keyset position
#: (from the underlying ``chat_sessions`` row) it was built from.
_RecordAndKey = tuple[dict[str, Any], tuple[datetime, str]]

#: One `_batches` item generically — `Any` (not a `TypeVar`, deliberately:
#: PEP 695 generic-function syntax needs Python 3.12, this repo still
#: supports 3.11) so the aux "late feedback" sweep, which has no keyset
#: position at all, can reuse the exact same record-count/byte-size split
#: logic with a bare `None` key instead of duplicating it.
_BatchItem = tuple[dict[str, Any], Any]


def _records(
    bundle: ConversationExportRepoBundle,
    *,
    since: datetime,
    until: datetime,
    surfaces: tuple[str, ...],
    start_cursor: str | None,
) -> Iterator[_RecordAndKey]:
    """Every conversation-corpus record completed in ``[since, until)``,
    paired with its ``(last_message_at, id)`` keyset key, in one
    strictly-ordered walk. ``surfaces`` is pushed straight into
    ``iter_conversations``'s own query (never filtered on the returned
    records) so the walk only ever touches rows this run will actually
    deliver. ``start_cursor`` resumes the walk from the persisted
    watermark's exact position — the SAME cursor mechanism a second page
    within this run uses, so "resume this run" and "resume the last run"
    are one code path, not two.
    """
    cursor = start_cursor
    while True:
        page, next_cursor, keys = iter_conversations(
            bundle, since=since, until=until, surfaces=surfaces or None, limit=PAGE_LIMIT, cursor=cursor
        )
        yield from zip(page, keys, strict=True)
        if next_cursor is None:
            return
        cursor = next_cursor


def _batches(items: Iterable[_BatchItem]) -> Iterator[list[_BatchItem]]:
    """Split an ordered (record, key) stream into outbound batches of at
    most :data:`MAX_BATCH_RECORDS` records or :data:`MAX_BATCH_BYTES`
    (whichever is hit first) — batch boundaries, not page boundaries,
    since a page and a batch are sized independently. A single record
    whose own serialized line already exceeds the byte cap is yielded
    alone — spec 3.12 says this export is "complete, never truncated", so
    there is no smaller unit to split it into, and the run loop, not this
    splitter, decides what to do with an over-contract line. The key half of
    each item is untyped (:data:`_BatchItem`) purely so the aux "late
    feedback" sweep — which has no keyset position, only a bare record — can
    reuse this with ``None`` keys rather than duplicating the split logic.
    """
    batch: list[_BatchItem] = []
    batch_bytes = 0
    for item in items:
        record, _key = item
        line_bytes = len(json.dumps(record, default=str).encode("utf-8")) + 1
        if batch and (len(batch) >= MAX_BATCH_RECORDS or batch_bytes + line_bytes > MAX_BATCH_BYTES):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += line_bytes
    if batch:
        yield batch


def _post_with_retry(
    client: httpx.Client,
    endpoint: str,
    headers: dict[str, str],
    body: bytes,
    *,
    sleep: Callable[[float], None],
) -> httpx.Response | None:
    """POST one ndjson batch, retrying up to :data:`MAX_ATTEMPTS` total
    attempts on a 5xx response or a connection-level error (spec 3.12),
    with exponential backoff (1s, 2s, 4s between attempts). A non-5xx
    response (2xx success, or a 4xx — incl. 429 — the destination will
    never accept on retry within this run) returns immediately without
    consuming a retry, and the run's watermark stays wherever the last
    successful batch left it. Returns ``None`` only when every attempt
    raised a connection error; otherwise returns the last response
    received, whatever its status.
    """
    response: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("conversation-export: POST failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            response = None
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_S * (2 ** (attempt - 1)))
                continue
            return None
        if response.status_code >= 500:
            logger.warning(
                "conversation-export: endpoint returned %d (attempt %d/%d)",
                response.status_code,
                attempt,
                MAX_ATTEMPTS,
            )
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_S * (2 ** (attempt - 1)))
                continue
        return response
    return response


def _sweep_feedback_updates(
    *,
    bundle: ConversationExportRepoBundle,
    feedback_repo: Any,
    watermark_repo: Any,
    endpoint: str,
    surfaces: tuple[str, ...],
    exclude: set[str],
    settle_bound: datetime,
    http_client: httpx.Client,
    headers: dict[str, str],
    sleep: Callable[[float], None],
) -> tuple[int, bool]:
    """The "late feedback" aux sweep — see the module docstring's "second,
    coarser watermark" paragraph. Runs AFTER the main keyset walk, in the
    same job invocation, never as a redesign of it.

    Unlike the main walk's ``until`` (deliberately settle-window-lagged so
    a session mid-turn is never exported half-done), a feedback row has no
    such ambiguity — :meth:`ChatMessageFeedbackPgRepository.upsert` commits
    it in one transaction, never half-written — so this sweeps all the way
    up to a fresh ``now``, not the main walk's lagged ``until``.

    ``surfaces`` is pushed into :meth:`session_ids_updated_between`'s own
    query exactly like the main walk pushes it into
    ``list_completed_between`` — an excluded surface's feedback is never
    fetched at all, so it can neither be delivered nor consume a slot in
    the capped page ahead of an included surface's update.

    ``exclude`` is the set of session ids the main walk already delivered
    in THIS run: their feedback (whatever existed at query time) already
    rode along in that record via the same bulk ``feedback.list_for_sessions``
    read every record build uses, so re-sending them here would just be a
    wasted duplicate POST for the same ``thread_id``. Excluded AFTER the
    capped page is fetched, never as part of the query, so an excluded id
    still counts as scanned for the purpose of advancing the watermark past
    it.

    The watermark this sweep persists is a ``(timestamp, session_id)``
    keyset position, exactly like the main watermark — not a bare
    timestamp — so a feedback burst wider than :data:`FEEDBACK_SWEEP_LIMIT`
    resumes on the NEXT run from the last id actually scanned rather than
    jumping straight to this run's ``until`` and silently dropping
    everything past the cap. A page that comes back full (more rows exist
    past the cap) advances the watermark only to the last scanned row's own
    key; a page that comes back short (the window is exhausted) advances it
    all the way to ``until``, exactly like the main walk's own resume logic.

    Returns ``(refreshed, failed)`` — the count of records actually
    delivered (for the audit params' ``refreshed`` figure), and whether any
    batch failed (so the caller can fold it into the run's overall
    ``result``, the same "partial" outcome a failed MAIN batch produces).
    """
    name = feedback_watermark_name(endpoint, surfaces)
    watermark = watermark_repo.get(name)
    if watermark is None:
        since = _EPOCH
        after: tuple[datetime, str] | None = None
    else:
        watermark_ts, watermark_cursor_id = watermark
        since = watermark_ts
        after = None if watermark_cursor_id == "-" else (watermark_ts, watermark_cursor_id)
    until = datetime.now(UTC)
    if not since < until:
        return 0, False

    rows = feedback_repo.session_ids_updated_between(
        since, until, limit=FEEDBACK_SWEEP_LIMIT + 1, surfaces=surfaces or None, after=after
    )
    has_more = len(rows) > FEEDBACK_SWEEP_LIMIT
    rows = rows[:FEEDBACK_SWEEP_LIMIT]
    if not rows:
        watermark_repo.set(name, until, "-")
        return 0, False

    # A session the FEEDBACK picked may be mid-turn: a thumbs verdict on an
    # old answer says nothing about whether the conversation has settled,
    # and rebuilding it now would ship a transcript whose newest turn is
    # still being written -- the very thing SETTLE_WINDOW exists to prevent.
    # Rows are walked in order and cut at the FIRST unsettled one, so its
    # feedback is not scanned past: the watermark stops in front of it and
    # the next run picks it up once the conversation settles.
    settled_at = bundle.sessions.last_message_at_for([sid for sid, _ in rows])
    held_back = False
    for index, (sid, _updated_at) in enumerate(rows):
        last_message_at = settled_at.get(sid)
        if last_message_at is None or last_message_at >= settle_bound:
            rows = rows[:index]
            has_more = True  # there IS more to do; the watermark must not jump to `until`
            held_back = True
            break
    if not rows:
        return 0, False
    if held_back:
        logger.debug("conversation-export: feedback sweep stopped at an unsettled conversation")

    session_ids = [sid for sid, _updated_at in rows if sid not in exclude]
    records = records_for_session_ids(bundle, session_ids) if session_ids else []
    refreshed = 0
    for batch in _batches((record, None) for record in records):
        body = b"".join(serialize_jsonl(record for record, _key in batch))
        response = _post_with_retry(http_client, endpoint, headers, body, sleep=sleep)
        if response is None or not (200 <= response.status_code < 300):
            logger.warning(
                "conversation-export: feedback-refresh batch of %d record(s) failed to deliver -- "
                "aux watermark not advanced, will retry from here next run",
                len(batch),
            )
            return refreshed, True
        refreshed += len(batch)

    last_id, last_updated_at = rows[-1][0], rows[-1][1]
    if has_more:
        watermark_repo.set(name, last_updated_at, last_id)
    else:
        watermark_repo.set(name, until, "-")
    return refreshed, False


def run_conversation_export_once(
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One push-sink pass: resolve config + policy, walk records since the
    watermark, POST them in batches, advance the watermark after each
    batch a 2xx acknowledges, audit the run.

    Never raises. A DuckDB-backed instance gets a clean
    ``{"skipped": "requires_postgres_backend"}``. Any OTHER exception
    raised while walking/posting is caught, logged, audited with
    ``result="error:<ClassName>"``, and turned into ``{"failed": "<ClassName>"}`` —
    spec 3.9: a sink swallows its own exceptions, it never fails the
    caller.

    ``client``/``sleep`` are injection seams for tests (an
    ``httpx.MockTransport``-backed client, a no-op sleep) — production
    callers leave both at their defaults.
    """
    from app.instance_config import get_conversation_export_config

    config = get_conversation_export_config()
    if config is None:
        return {"skipped": "not_configured"}

    mode = content_export_mode(workload="chat")
    if mode == "off":
        _warn_policy_off_once()
        return {"skipped": "content_export_disabled"}

    from src.repositories import RequiresPostgresBackend, export_watermarks_repo

    try:
        repos = _repo_bundle()
        watermark_repo = export_watermarks_repo()
    except RequiresPostgresBackend:
        logger.debug("conversation-export: DuckDB-backed instance -- no push sink to run")
        return {"skipped": "requires_postgres_backend"}

    endpoint = config["endpoint"]
    endpoint_host = urlsplit(endpoint).hostname or ""
    # The same egress control the credentialed remote ATTACH uses (security
    # playbook: gate credential egress with a host allowlist): with
    # AGNES_REMOTE_ATTACH_HOST_ALLOWLIST set, the destination host must be on
    # it, else the run ends here -- before the secret headers are even
    # resolved. The URL's SHAPE (https to a named host, no credentials) was
    # already enforced by get_conversation_export_config.
    from src.orchestrator_security import is_attach_host_allowed

    if not is_attach_host_allowed(endpoint):
        logger.warning(
            "conversation-export: endpoint host %r is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; the push sink stays off",
            endpoint_host,
        )
        return {"skipped": "endpoint_not_allowlisted"}
    headers = {"Content-Type": "application/x-ndjson", **_resolve_headers(config["headers_secret_env"])}
    surfaces = tuple(config["surfaces"]) if config["surfaces"] else ()
    name = watermark_name(endpoint, surfaces)

    watermark = watermark_repo.get(name)
    if watermark is None:
        since = _EPOCH
        start_cursor = None
    else:
        watermark_ts, watermark_cursor_id = watermark
        since = watermark_ts
        start_cursor = encode_cursor(watermark_ts, watermark_cursor_id)
    # SETTLE_WINDOW: a session whose last_message_at falls after `until`
    # may still be mid-turn (a user message with no assistant answer yet)
    # -- left for a later tick rather than exported incomplete.
    until = datetime.now(UTC) - SETTLE_WINDOW

    anonymizer = export_text if mode == "pseudonymized" else None
    bundle = ConversationExportRepoBundle(
        sessions=repos["sessions"],
        messages=repos["messages"],
        calls=repos["calls"],
        feedback=repos["feedback"],
        memories=repos["memories"],
        users=repos["users"],
        content_mode=mode,
        anonymizer=anonymizer,
    )

    own_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
    total_sent = 0
    batches_sent = 0
    batches_failed = 0
    oversized_skipped = 0
    refreshed = 0
    aux_failed = False
    delivered_this_run: set[str] = set()
    exc_class: str | None = None
    try:
        for batch in _batches(_records(bundle, since=since, until=until, surfaces=surfaces, start_cursor=start_cursor)):
            records = [record for record, _key in batch]
            body = b"".join(serialize_jsonl(records))
            # A single conversation whose own line exceeds the batch size
            # this sink advertises. It cannot be split (the export is
            # "complete, never truncated"), so it is still ATTEMPTED --
            # skipping it unasked would leave the destination corpus
            # quietly incomplete. What changes is the handling of a refusal:
            # a collector that enforces the cap rejects this one record on
            # every run, and treating that as an ordinary failed batch stops
            # the walk and blocks every conversation behind it forever. So a
            # refused over-cap record is stepped over instead: counted,
            # logged with its id, and the watermark advanced past it, while
            # the pull endpoint (no batch cap) still serves it whole.
            oversized = len(batch) == 1 and len(body) > MAX_BATCH_BYTES
            response = _post_with_retry(http_client, endpoint, headers, body, sleep=sleep)
            if oversized and response is not None and 400 <= response.status_code < 500:
                last_ts, last_id = batch[0][1]
                logger.warning(
                    "conversation-export: conversation %s serializes to %d bytes, over the %d-byte batch cap, "
                    "and the destination refused it with %d -- stepped over so it cannot block the ones behind "
                    "it; fetch it with the pull endpoint",
                    last_id,
                    len(body),
                    MAX_BATCH_BYTES,
                    response.status_code,
                )
                oversized_skipped += 1
                watermark_repo.set(name, last_ts, last_id)
                # Excluded from the aux sweep below for this run too: the
                # destination just refused this exact record, and offering
                # it again in the same tick because a thumb arrived on it
                # would only collect the same refusal.
                delivered_this_run.add(last_id)
                continue
            if response is None or not (200 <= response.status_code < 300):
                logger.warning(
                    "conversation-export: batch of %d record(s) failed to deliver -- "
                    "watermark not advanced, will retry from here next run",
                    len(batch),
                )
                batches_failed += 1
                break
            last_ts, last_id = batch[-1][1]
            watermark_repo.set(name, last_ts, last_id)
            delivered_this_run.update(key[1] for _record, key in batch)
            total_sent += len(batch)
            batches_sent += 1

        # Late-feedback aux sweep -- see the module docstring's "second,
        # coarser watermark" paragraph. Runs regardless of whether the main
        # walk above sent anything this tick; only skipped when the main
        # walk itself raised (the `except` below then marks the whole run
        # failed, same as before this sweep existed).
        refreshed, aux_failed = _sweep_feedback_updates(
            bundle=bundle,
            feedback_repo=repos["feedback"],
            watermark_repo=watermark_repo,
            endpoint=endpoint,
            surfaces=surfaces,
            exclude=delivered_this_run,
            settle_bound=until,
            http_client=http_client,
            headers=headers,
            sleep=sleep,
        )
    except Exception as exc:
        exc_class = type(exc).__name__
        logger.exception("conversation-export: unexpected failure mid-walk, run marked failed")
    finally:
        if own_client:
            http_client.close()

    base_params = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "count": total_sent,
        "oversized_skipped": oversized_skipped,
        "refreshed": refreshed,
        "content_mode": mode,
        "placement": load_content_export_policy().placement,
        "delivery": "push",
        "endpoint_host": endpoint_host,
    }

    if exc_class is not None:
        log_safe(
            user_id=None,
            action="conversations.export",
            resource="conversations:export",
            params={**base_params, "error": exc_class},
            result=f"error:{exc_class}",
            client_kind="scheduler",
        )
        return {"failed": exc_class}

    log_safe(
        user_id=None,
        action="conversations.export",
        resource="conversations:export",
        params=base_params,
        result="success" if batches_failed == 0 and not aux_failed else "partial",
        client_kind="scheduler",
    )

    return {
        "sent": total_sent,
        "batches": batches_sent,
        "batches_failed": batches_failed,
        "oversized_skipped": oversized_skipped,
        "refreshed": refreshed,
    }


def run_conversation_export(payload: dict) -> dict:
    """The registered ``conversation-export`` job handler.

    Checks the cheap config/policy gates FIRST (:func:`_quick_skip_reason`)
    so an unconfigured or policy-off instance never opens a database
    connection for the advisory lease at all, then wraps the actual work
    in the same idempotency-key-plus-advisory-lease belt-and-braces
    ``knowledge-packaging`` uses (see ``app/worker/kinds.py
    ::_run_knowledge_packaging``). Skipping (not failing) when the lock is
    already held is the correct outcome: the other run is doing the exact
    same work.
    """
    skip_reason = _quick_skip_reason()
    if skip_reason is not None:
        return {"skipped": skip_reason}

    from src.db_pg import conversation_export_lease

    with conversation_export_lease() as acquired:
        if not acquired:
            logger.info(
                "conversation-export: advisory lock already held by another run -- skipping "
                "(belt-and-braces on top of the idempotency-key dedupe)"
            )
            return {"skipped": "lock_held"}
        return run_conversation_export_once()


__all__ = [
    "IDEMPOTENCY_KEY",
    "KIND",
    "MAX_ATTEMPTS",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_RECORDS",
    "SETTLE_WINDOW",
    "WATERMARK_PREFIX",
    "run_conversation_export",
    "run_conversation_export_once",
    "watermark_name",
]
