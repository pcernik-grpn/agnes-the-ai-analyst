"""Per-processor runner — drives one SessionProcessor across all unprocessed
sessions in /data/user_sessions/. Each processor is invoked independently
(one call to run_processor per scheduler tick per processor); there is no
cross-processor coupling.

Failure handling mirrors the pre-refactor verification_detector behavior:
per-session try/except, on raise the state row is NOT written → the same
session will be retried on the next tick. There is no max_retries / dead
letter. A permanently malformed session will retry forever; that is a
known limitation we may revisit (out of scope for this refactor).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from services.session_pipeline.contract import ProcessorResult, SessionProcessor
from services.session_pipeline.lib import compute_file_hash
from src.repositories import (
    session_processor_state_repo,
    users_repo,
)

logger = logging.getLogger(__name__)


def resolve_user_identity(username: str) -> tuple[str | None, str | None]:
    """Map a session-directory name to ``(users.id, users.email)``.

    Two conventions exist for the directory name under
    ``/data/user_sessions/``:

    * **Session collector** writes under the OS username, which in
      current deployments equals the email local-part (e.g. ``alice``).
    * **Upload API** writes under ``user["id"]`` — a UUID.

    Resolution order:
    1. Exact match on ``users.id`` (covers the UUID path).
    2. Email local-part match: ``users.email LIKE '<username>@%'``.
       If multiple users share the same local-part (different domains),
       we pick the one most recently updated.
    3. Fallback: return ``(None, None)`` (orphaned / deleted user).

    Email is returned so the runner can normalise the ``username``
    column in ``usage_events`` / ``usage_session_summary`` to a stable
    human-readable identity regardless of which ingestion path the
    session arrived through — otherwise the admin telemetry dropdown
    lists the same user under both their UUID (upload API) and their
    email (REST event emitters).

    Routes through :func:`src.repositories.users_repo` (not a raw
    connection) so the lookup hits the active backend — a raw DuckDB
    query here silently returned no rows on Postgres instances.
    """
    repo = users_repo()
    row = repo.get_by_id(username)
    if row:
        return row["id"], row["email"]
    row = repo.get_by_email_prefix(username)
    if row:
        return row["id"], row["email"]
    return None, None


def resolve_user_id(username: str) -> str | None:
    """Backward-compatible wrapper returning just the resolved ``users.id``.

    Existing call sites (and tests) that only need the UUID stay
    unchanged; new code in ``run_processor`` uses
    :func:`resolve_user_identity` to get the email too.
    """
    uid, _ = resolve_user_identity(username)
    return uid


DEFAULT_SESSION_DATA_DIR = Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))

# Wall-clock budget (seconds) for the *whole tick* — the cross-session loop
# in run_processor(), not any single session. Incident 2026-07-20: the
# "usage" processor is explicitly exempted from max_sessions_per_run (see
# admin.py — cheap, no network I/O, so an attempt-count cap "just throttles
# telemetry"). A bulk onboarding wave left it with a large backlog; the
# uncapped run drained the whole backlog synchronously — hundreds of
# sessions' worth of jsonl parsing + repository writes plus the post-tick
# rollup rebuild — and held the request-serving process for ~6 minutes,
# producing app-wide 503s on completely unrelated endpoints (classic
# event-loop/threadpool starvation).
#
# max_sessions_per_run alone doesn't close this: it's an attempt-count cap,
# so (a) a processor can be exempted from it entirely, as usage was, and
# (b) even under a cap, a handful of unusually large sessions can still blow
# the wall-clock. This budget bounds the thing that actually caused the
# outage — elapsed time holding the process — for every processor,
# regardless of any attempt-count cap. 150s: comfortably under the
# multi-minute range that caused the incident, while still generous enough
# for a normal tick to finish uninterrupted (same order of magnitude as the
# 180s per-session budget PR #893 gave VerificationProcessor, but applied at
# the tick level across all sessions rather than per session).
_DEFAULT_TIME_BUDGET_SECONDS = 150.0


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime to UTC-aware for comparison.

    DuckDB returns naive datetimes for TIMESTAMP columns even though the
    values are always written as UTC (see the comment on this in
    app/api/admin_sessions.py); Postgres columns and ``Path.stat()``'s
    ``st_mtime`` are unambiguous. Treating a naive value as already-UTC
    (rather than local time) matches every other write in this codebase.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _sweep_chat_session_exports(effective_dir: Path, *, limit: int = 200) -> int:
    """Bounded pre-scan: export any recently-active chat session whose
    messages are newer than its already-exported jsonl's mtime (or that has
    no exported file yet) — F4, audit-full-coverage plan Task 8. Runs once
    at the top of every ``run_processor()`` call (i.e. once per processor
    per scheduler tick); cheap even called that often since the candidate
    query is capped at *limit* most-recently-active sessions and each one's
    own mtime check skips anything already current. No-ops (no repo call at
    all) when ``sessions.include_chat`` is off.

    Returns the number of sessions actually (re-)exported, for the caller's
    log line.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sessions", "include_chat", env_var="AGNES_SESSIONS_INCLUDE_CHAT", default=True):
        return 0

    from app.chat.session_export import export_chat_session_jsonl
    from src.repositories import chat_session_repo, users_repo

    try:
        sessions = chat_session_repo().list_recently_active(limit=limit)
    except Exception:
        logger.warning("chat session export sweep: could not list recently-active sessions", exc_info=True)
        return 0

    exported = 0
    for s in sessions:
        last_active = _as_utc(s.last_message_at)
        if last_active is None:
            continue
        # Same owner lookup export_chat_session_jsonl itself uses (exact
        # email match, NOT the local-part/prefix resolution
        # resolve_user_identity does for CLI-collector directory names) —
        # so the path checked here for a stale mtime is exactly the path
        # the export call below will write.
        owner = users_repo().get_by_email(s.user_email)
        if not owner:
            continue
        target = effective_dir / owner["id"] / f"chat-{s.id}.jsonl"
        if target.exists():
            try:
                file_mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
            except OSError:
                file_mtime = None
            if file_mtime is not None and last_active <= file_mtime:
                continue
        try:
            if export_chat_session_jsonl(s.id) is not None:
                exported += 1
        except Exception:
            logger.warning("chat session export sweep: export failed for %s", s.id, exc_info=True)
    return exported


def run_processor(
    conn: duckdb.DuckDBPyConnection,
    processor: SessionProcessor,
    session_data_dir: Path | None = None,
    max_sessions_per_run: int | None = None,
    time_budget_seconds: float | None = _DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Run *processor* against every unprocessed session in
    *session_data_dir* (defaults to $SESSION_DATA_DIR or /data/user_sessions).

    Returns a stats dict with: scanned, processed, skipped, capped, errors,
    items_extracted, errors_detail. Caller (admin endpoint) puts this in the
    audit row and HTTP response body.

    ``max_sessions_per_run``, when set, caps how many candidates get an
    actual processing attempt (a ``processor.process_session()`` call) in
    this call — the rest are left for the next scheduler tick. Bounds the
    worst-case wall-clock/CPU cost of a single invocation (each attempt can
    trigger multiple synchronous, blocking LLM calls); a burst of session
    closures landing in the same tick no longer processes unboundedly in
    one request. The cap is enforced on attempts, not on the raw candidate
    count: ``scan_unprocessed_for`` uses a cheap mtime-based prefilter that
    can surface candidates the hash-aware ``is_processed`` check below then
    skips for free (e.g. a file whose mtime bumped but content didn't
    change) — counting those against the budget would let skip-only
    candidates consume the cap and starve genuinely unprocessed sessions
    behind them. ``scanned`` always reflects the true total found;
    ``capped`` reports how many were left un-visited when the budget ran
    out, so operators can see a forming backlog before it becomes one.

    A *failed* attempt still counts against the budget (unlike a free
    ``is_processed`` skip) — deliberately: a session that raises can still
    have burned real wall-clock/LLM cost before failing, so exempting
    errors would reopen the unbounded-tick-duration problem this cap
    exists to close. The accepted tradeoff (Devin Review, PR #894): a large
    cluster of persistently-failing sessions sorted ahead of healthy ones
    could consume the whole per-tick budget indefinitely, deferring the
    healthy sessions behind them. This is an extension of the same
    "no max_retries / dead letter" limitation already documented in the
    module docstring above (a poison session already retries forever,
    capped or not) rather than a new failure mode introduced by capping —
    revisit together if it bites in practice (e.g. a separate error quota
    or least-recently-attempted ordering).

    ``time_budget_seconds``, when set, bounds the wall-clock time spent in
    the candidate loop below (independent of, and in addition to,
    ``max_sessions_per_run``). Checked before each candidate is visited —
    once elapsed time exceeds the budget, the loop stops (no new candidate
    is hashed, skip-checked, or processed) and the remaining candidates are
    left for the next scheduler tick, same as an attempt-count cap. This
    does NOT raise: a partial tick that already processed some sessions
    successfully is a normal, successful outcome, not an error — contrast
    with ``VerificationProcessor``'s per-session ``TimeBudgetExceeded``
    (services/session_processors/verification.py), which raises so a
    partially-worked *session* is retried whole. Reuses the ``capped``
    counter — from the caller's perspective a time-budget stop and an
    attempt-count stop have the same effect (some candidates deferred to
    next tick).
    """
    effective_dir = session_data_dir if session_data_dir is not None else DEFAULT_SESSION_DATA_DIR

    try:
        n_exported = _sweep_chat_session_exports(effective_dir)
        if n_exported:
            logger.info("chat session export sweep: exported %d session(s)", n_exported)
    except Exception:
        # Best-effort, like every other step of this sweep — a chat-export
        # failure must never block the processor tick it happens to share.
        logger.warning("chat session export sweep failed (non-fatal)", exc_info=True)

    stats: dict[str, Any] = {
        "processor": processor.name,
        "scanned": 0,
        "processed": 0,
        "skipped": 0,
        "capped": 0,
        "errors": 0,
        "items_extracted": 0,
        "errors_detail": [],
    }

    repo = session_processor_state_repo()
    candidates = repo.scan_unprocessed_for(processor.name, effective_dir)
    stats["scanned"] = len(candidates)

    if not candidates:
        logger.info("No sessions to process for processor=%s", processor.name)
        return stats

    # Pre-resolve (user_id, email) per directory name so each processor
    # can store the stable identity. Cache avoids repeated DB lookups
    # when one user has many sessions. Email is used as the canonical
    # ``username`` written to usage_* tables so the admin telemetry
    # dropdown surfaces one row per user regardless of whether the
    # session arrived via /api/upload/sessions (UUID dir) or the legacy
    # collector (OS-username dir).
    _identity_cache: dict[str, tuple[str | None, str | None]] = {}
    attempts = 0
    loop_start = time.monotonic()

    for idx, (dir_name, jsonl_path) in enumerate(candidates):
        if max_sessions_per_run is not None and attempts >= max_sessions_per_run:
            stats["capped"] = len(candidates) - idx
            logger.info(
                "Processor %s: hit %d-attempt budget after %d candidates; %d left for next tick",
                processor.name,
                max_sessions_per_run,
                idx,
                stats["capped"],
            )
            break

        if time_budget_seconds is not None and time.monotonic() - loop_start > time_budget_seconds:
            stats["capped"] = len(candidates) - idx
            logger.info(
                "Processor %s: hit %.0fs tick time budget after %d candidates; %d left for next tick",
                processor.name,
                time_budget_seconds,
                idx,
                stats["capped"],
            )
            break

        session_key = f"{dir_name}/{jsonl_path.name}"

        # Record the content-observation time before we read the file. Any
        # append concurrent with or after the read will have an mtime >= this
        # value, so the next tick sees the file as changed and re-checks the
        # hash. Sampling after the hash leaves the hashing interval unprotected.
        read_at = datetime.now(UTC)

        try:
            file_hash = compute_file_hash(jsonl_path)
        except Exception as e:  # noqa: BLE001 -- defensive per-session hashing
            logger.warning(
                "Cannot hash %s for processor=%s: %s",
                session_key,
                processor.name,
                e,
            )
            stats["errors"] += 1
            stats["errors_detail"].append({"session": session_key, "error": str(e)})
            continue

        # Hash-aware skip: scan_unprocessed_for returns every candidate; we
        # do the authoritative is_processed check here so the runner is the
        # single place that decides "this exact (processor, session, hash)
        # tuple is already done". Cost: one extra SELECT per candidate, but
        # only for files that survived directory scan. Free with respect to
        # the attempt budget above — it never calls the (expensive, LLM-
        # driving) processor, so it can't be starved out by the cap.
        if repo.is_processed(processor.name, session_key, file_hash):
            stats["skipped"] += 1
            continue

        attempts += 1

        if dir_name not in _identity_cache:
            _identity_cache[dir_name] = resolve_user_identity(dir_name)
        resolved_uid, resolved_email = _identity_cache[dir_name]
        # Canonical username = email when the user resolves; fall back
        # to the directory name otherwise (orphaned uploads, sessions
        # for deleted users). The directory name remains the filesystem
        # lookup key via ``session_key`` (``<dir>/<file>``); ``username``
        # is purely the display/grouping identity for telemetry.
        canonical_username = resolved_email or dir_name

        try:
            result = processor.process_session(
                jsonl_path,
                canonical_username,
                session_key,
                conn,
                user_id=resolved_uid,
            )
        except Exception as e:
            logger.exception(
                "Processor %s failed on %s — leaving state unwritten for retry",
                processor.name,
                session_key,
            )
            stats["errors"] += 1
            stats["errors_detail"].append({"session": session_key, "error": str(e)})
            continue

        if not isinstance(result, ProcessorResult):
            # Defensive: Protocol can't enforce the return type at runtime,
            # so a misbehaving processor that returns None or an arbitrary
            # dict shouldn't poison the state-write path. Treat it as zero
            # items but still mark processed — the alternative (raise) would
            # cause the same session to be retried forever.
            logger.warning(
                "Processor %s returned non-ProcessorResult on %s; coercing to empty result",
                processor.name,
                session_key,
            )
            result = ProcessorResult(items_count=0)

        repo.mark_processed(
            processor_name=processor.name,
            session_file=session_key,
            username=canonical_username,
            items_count=result.items_count,
            file_hash=file_hash,
            read_at=read_at,
        )
        stats["processed"] += 1
        stats["items_extracted"] += result.items_count

    logger.info(
        "Processor %s: scanned=%d processed=%d skipped=%d capped=%d errors=%d items=%d",
        processor.name,
        stats["scanned"],
        stats["processed"],
        stats["skipped"],
        stats["capped"],
        stats["errors"],
        stats["items_extracted"],
    )
    return stats
