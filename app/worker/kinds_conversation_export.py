"""``conversation-export`` worker job kind — the push sink for the
conversation-corpus export (design 2026-09-08 §3.12, Task 11).

Task 10 built the PULL side (``GET /api/admin/conversations/corpus`` —
``app/api/conversations_export.py``): an admin (or a data platform with a
PAT) asks for a window and gets it back. This is the PUSH side: a
scheduled ``conversation-export`` job reads a Postgres-persisted watermark
(``export_watermarks``, name ``"conversation_export"``), walks every
conversation completed since it via the SAME record builder
(``src.conversation_export.iter_conversations``), and POSTs
newline-delimited JSON batches to ``observability.conversation_export
.endpoint`` — advancing the watermark only after a batch's destination
answers 2xx, so a failed delivery is retried on the next tick rather than
silently skipped, and a delivered one is never resent from scratch.

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

Single-run discipline: the scheduler's own enqueue is idempotency-keyed
(``idempotency_key="conversation-export"``, see
``services/scheduler/__main__.py``) so a second tick while one run is
still queued/running is a no-op at the QUEUE level; this module's
``run_once`` additionally takes a non-blocking Postgres advisory lock
(``src.db_pg.conversation_export_lease``) as belt-and-braces against a
stray manual enqueue or two worker replicas racing to claim two different
job rows — the exact same two-layer shape ``knowledge-packaging`` already
uses (see ``app/worker/kinds.py::_run_knowledge_packaging``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.audit_helpers import log_safe
from src.conversation_export import ConversationExportRepoBundle, iter_conversations, serialize_jsonl
from src.observability.content_policy import content_export_mode, export_text, load_content_export_policy
from src.observability.otel import parse_otlp_headers

logger = logging.getLogger(__name__)

#: The ``jobs.kind`` string and the ``export_watermarks.name`` row this
#: module owns — kept as named constants rather than repeated literals so
#: the scheduler/registry/repo layers can't drift on the spelling.
KIND = "conversation-export"
WATERMARK_NAME = "conversation_export"
IDEMPOTENCY_KEY = "conversation-export"

#: Spec 3.12: "batches of at most 200 records or 8 MiB".
MAX_BATCH_RECORDS = 200
MAX_BATCH_BYTES = 8 * 1024 * 1024
#: Spec 3.12: "retried with backoff" — three attempts total (one send plus
#: two retries), matching ``LLMDetector``'s own attempt/backoff shape
#: (``src/anonymization_ner.py``).
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0
HTTP_TIMEOUT_S = 30.0
#: Page size for the underlying ``iter_conversations`` walk — independent
#: of the OUTBOUND batch size above (a page becomes one or more outbound
#: batches once the byte cap is applied).
PAGE_LIMIT = 200

#: A watermark-less first run exports every conversation ever held —
#: deliberate: this is an opt-in feature (an operator must set an
#: ``endpoint``), so a first run backfilling the whole transcript is the
#: expected behaviour, not a runaway scan.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

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


def _records(
    bundle: ConversationExportRepoBundle,
    *,
    since: datetime,
    until: datetime,
    surfaces: tuple[str, ...],
) -> Iterator[dict[str, Any]]:
    """Every conversation-corpus record completed in ``[since, until)``, in
    one strictly-ordered walk across ALL surfaces (the underlying keyset
    cursor is ``(last_message_at, id)`` regardless of surface) — filtering
    by ``surfaces`` client-side rather than per-surface queries keeps the
    walk a single ordered stream, which is what makes "advance the
    watermark to this batch's latest record" safe: batches never
    interleave out of order the way per-surface sub-loops could.
    """
    cursor: str | None = None
    while True:
        page, next_cursor = iter_conversations(bundle, since=since, until=until, limit=PAGE_LIMIT, cursor=cursor)
        for record in page:
            if surfaces and record.get("surface") not in surfaces:
                continue
            yield record
        if next_cursor is None:
            return
        cursor = next_cursor


def _batches(records: Iterable[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Split an ordered record stream into outbound batches of at most
    :data:`MAX_BATCH_RECORDS` records or :data:`MAX_BATCH_BYTES` (whichever
    is hit first). A single record whose own serialized line already
    exceeds the byte cap is still sent alone — spec 3.12 says this export
    is "complete, never truncated", so there is no smaller unit to split
    it into.
    """
    batch: list[dict[str, Any]] = []
    batch_bytes = 0
    for record in records:
        line_bytes = len(json.dumps(record, default=str).encode("utf-8")) + 1
        if batch and (len(batch) >= MAX_BATCH_RECORDS or batch_bytes + line_bytes > MAX_BATCH_BYTES):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(record)
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
    attempts on a 5xx response or a connection-level error (spec 3.12).
    A non-5xx response (2xx success, or a 4xx the destination will never
    accept on retry) returns immediately without consuming a retry.
    Returns ``None`` only when every attempt raised a connection error;
    otherwise returns the last response received, whatever its status.
    """
    response: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("conversation-export: POST failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            response = None
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_S * attempt)
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
                sleep(RETRY_BACKOFF_S * attempt)
                continue
        return response
    return response


def run_conversation_export_once(
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One push-sink pass: resolve config + policy, walk records since the
    watermark, POST them in batches, advance the watermark after each
    batch a 2xx acknowledges, audit the run. Never raises
    ``RequiresPostgresBackend`` — a DuckDB-backed instance gets a clean
    ``{"skipped": "requires_postgres_backend"}`` instead. Any OTHER
    exception (a DB error mid-walk, a malformed config) is allowed to
    propagate — the caller (the registered job handler) lets that fail the
    job normally, exactly like every other kind in
    ``app/worker/kinds.py``.

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

    since = watermark_repo.get(WATERMARK_NAME) or _EPOCH
    until = datetime.now(UTC)

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

    endpoint = config["endpoint"]
    endpoint_host = urlsplit(endpoint).hostname or ""
    headers = {"Content-Type": "application/x-ndjson", **_resolve_headers(config["headers_secret_env"])}

    own_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
    total_sent = 0
    batches_sent = 0
    batches_failed = 0
    try:
        for batch in _batches(_records(bundle, since=since, until=until, surfaces=config["surfaces"])):
            body = b"".join(serialize_jsonl(batch))
            response = _post_with_retry(http_client, endpoint, headers, body, sleep=sleep)
            if response is None or not (200 <= response.status_code < 300):
                logger.warning(
                    "conversation-export: batch of %d record(s) failed to deliver -- "
                    "watermark not advanced, will retry from here next run",
                    len(batch),
                )
                batches_failed += 1
                break
            ends = [r["conversation_end"] for r in batch if r.get("conversation_end")]
            if ends:
                watermark_repo.advance(WATERMARK_NAME, datetime.fromisoformat(max(ends)))
            total_sent += len(batch)
            batches_sent += 1
    finally:
        if own_client:
            http_client.close()

    log_safe(
        user_id=None,
        action="conversations.export",
        resource="conversations:export",
        params={
            "since": since.isoformat(),
            "until": until.isoformat(),
            "count": total_sent,
            "content_mode": mode,
            "placement": load_content_export_policy().placement,
            "delivery": "push",
            "endpoint_host": endpoint_host,
        },
        result="success" if batches_failed == 0 else "partial",
        client_kind="scheduler",
    )

    return {"sent": total_sent, "batches": batches_sent, "batches_failed": batches_failed}


def run_conversation_export(payload: dict) -> dict:
    """The registered ``conversation-export`` job handler — a thin adapter
    over :func:`run_conversation_export_once`, wrapped in the same
    idempotency-key-plus-advisory-lease belt-and-braces
    ``knowledge-packaging`` uses (see ``app/worker/kinds.py
    ::_run_knowledge_packaging``). Skipping (not failing) when the lock is
    already held is the correct outcome: the other run is doing the exact
    same work.
    """
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
    "MAX_BATCH_BYTES",
    "MAX_BATCH_RECORDS",
    "WATERMARK_NAME",
    "run_conversation_export",
    "run_conversation_export_once",
]
