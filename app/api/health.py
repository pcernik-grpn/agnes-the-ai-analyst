"""Health check endpoint — structured diagnostics for AI agents.

## Severity vocabulary

Per-check `status` values, in order of escalation:

- `ok`     — nothing to surface.
- `info`   — non-trivial observation worth showing the operator, but the
             situation isn't broken. **Does not** promote the overall
             status to `degraded` (issue #178).
- `unknown`— check couldn't run (missing dependency, FS error). Surfaced
             but doesn't promote overall.
- `warning`— real issue, operator should look. Promotes overall to
             `degraded`.
- `error`  — critical. Promotes overall to `unhealthy`.

Add an `info`-tier check by returning `{"status": "info", ...}` from the
check function. The aggregator at the bottom of `health_check_detailed`
treats `info` as non-promoting.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import _get_db, get_current_user
from app.secrets_vault import vault_key_configured
from src.db import SCHEMA_VERSION, get_system_db
from src.repositories import (
    session_processor_state_repo,
    sync_state_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Captured at module import (i.e., app process start) — proxy for "deployed at".
# When the cron auto-upgrade pulls a new digest and recreates the container,
# this resets. Accurate enough for a UI "last updated" badge.
_DEPLOYED_AT = datetime.now(timezone.utc).isoformat()


def _check_bq_billing_project() -> dict | None:
    """Surface the USER_PROJECT_DENIED footgun when a BQ instance has
    `billing_project` falling back to (or explicitly equal to) `project`.

    Background: connectors/bigquery/access.py:339-342 lets `billing` default
    to `data` when `billing_project` is unset. A service account with
    `roles/bigquery.dataViewer` on the data project but no
    `serviceusage.services.use` on it then 403s on every BQ call with
    USER_PROJECT_DENIED. The config is technically valid, so we warn rather
    than error — the operator's billable project must be set distinctly.

    Returns:
      None when the check doesn't apply (non-BQ instance, or BQ deps missing).
      A service-entry dict otherwise: {"status": "ok"} or
      {"status": "warning", "detail": ..., "hint": ..., "billing_project": ...,
       "data_project": ...}.
    """
    try:
        from app.instance_config import get_data_source_type
    except Exception:
        return None
    if (get_data_source_type() or "").lower() != "bigquery":
        return None

    try:
        from connectors.bigquery.access import get_bq_access

        bq = get_bq_access()
        billing = bq.projects.billing
        data = bq.projects.data
    except Exception as e:
        # Resolution failure (missing google-cloud-bigquery, auth error,
        # malformed config) is itself a problem worth surfacing. Returning
        # status='ok' would mask the failure from automated alerting that
        # keys on `status != 'ok'`. Use 'unknown' so the entry shows as
        # non-green in operator dashboards but doesn't promote the overall
        # check to 'degraded' (which 'warning' does). Devin finding
        # 2026-05-01: ANALYSIS_pr-review-job-642ff90f_0007.
        return {
            "status": "unknown",
            "detail": f"could not resolve BQ projects: {e}",
        }

    if not data:
        # not_configured sentinel — surfaced elsewhere; nothing to warn about here.
        return {"status": "ok", "detail": "BigQuery project not configured"}

    if billing == data:
        # Issue #178: this is informational, not a fault. Many valid
        # single-project dev instances run with billing == data and the SA
        # has `serviceusage.services.use`. Keep the message visible but
        # don't promote the overall status to `degraded` for it.
        return {
            "status": "info",
            "detail": "BigQuery billing project equals data project",
            "hint": (
                "If the SA hits USER_PROJECT_DENIED 403, set "
                "data_source.bigquery.billing_project in instance.yaml to a "
                "project the SA can bill against (typically your dev/billable "
                "project, distinct from a shared read-only data project). "
                "Configurable via /admin/server-config UI."
            ),
            "billing_project": billing,
            "data_project": data,
        }

    return {
        "status": "ok",
        "billing_project": billing,
        "data_project": data,
    }


def _stuck_file_grace_seconds() -> int:
    """How long (seconds) an unprocessed jsonl must sit before triggering
    the FIFO check warning. Defaults to 4× the verification-detector grace
    (= 2h with default 30min grace = 8 × 15min cadence). Configurable via
    SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS env var.

    Started conservatively at 4× to avoid false positives on routine LLM
    API hiccups. Operators can tighten with the env var once they have
    prod data on extraction throughput.
    """
    explicit = os.environ.get("SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS")
    if explicit:
        try:
            v = int(explicit)
            if v > 0:
                return v
        except ValueError:
            pass
    return 4 * _verification_detector_grace_seconds()


def _check_session_pipeline() -> dict:
    """Detect a stuck session pipeline: jsonls land but never get processed.

    Heuristic (#176):
      max(mtime of /data/user_sessions/**/*.jsonl) <=
      max(processed_at in session_processor_state where processor='verification') + grace_seconds

    grace_seconds = 2 × the verification-detector cadence (default 15m → 30m).
    Operators with a custom SCHEDULER_VERIFICATION_DETECTOR_INTERVAL can
    extend the grace by setting that env var.

    The check is scoped to the verification processor specifically — that's
    the LLM-gated pipeline an operator most needs to know is stuck. Other
    processors in the framework (e.g. usage) might lag for benign reasons
    (no LLM, lighter scan cadence) and shouldn't trip a warning.

    Returns ``warning`` (never ``error``) — the LLM may be down for
    maintenance, not a hard failure. Returns ``ok`` when no session
    files exist (cold-start case).
    """
    # Resolve user_sessions dir from the same DATA_DIR conftest sets up.
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    user_sessions = data_dir / "user_sessions"

    try:
        session_files = list(user_sessions.glob("**/*.jsonl"))
    except OSError:
        # Permission / FS error — surface as 'unknown' rather than ok/warning.
        return {"status": "unknown", "detail": "could not scan user_sessions"}

    if not session_files:
        return {"status": "ok", "detail": "no session files yet"}

    try:
        latest_session_mtime = max(f.stat().st_mtime for f in session_files)
    except OSError:
        return {"status": "unknown", "detail": "could not stat session files"}

    # Look up the most recent processed_at for the verification processor.
    try:
        last_processed = session_processor_state_repo().max_processed_at("verification")
    except Exception as e:
        return {"status": "unknown", "detail": f"could not query session_processor_state: {e}"}

    grace_seconds = _verification_detector_grace_seconds()

    if last_processed is None:
        # Files exist but verification has no state rows — pipeline never ran here.
        if (datetime.now(timezone.utc).timestamp() - latest_session_mtime) > grace_seconds:
            return {
                "status": "warning",
                "detail": (
                    "session_processor_state has no verification rows but jsonl files exist. "
                    "Check the verification-detector scheduler job."
                ),
                "session_files": len(session_files),
            }
        return {"status": "ok", "session_files": len(session_files)}

    # Both available — compare. `session_processor_state.processed_at` is
    # stored as DuckDB TIMESTAMP (naive). The DuckDB connection helper
    # (`src.db._open_duckdb`) pins the session timezone to UTC, so the
    # naive read is UTC-clock. Compare against UTC-naive `now` to keep
    # both sides on the same axis.
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(last_processed, "tzinfo") and last_processed.tzinfo is not None:
        last_processed = last_processed.replace(tzinfo=None)
    proc_age_seconds = (now_utc_naive - last_processed).total_seconds()
    file_age_seconds = time_now() - latest_session_mtime

    # File is newer than the last processed_at by more than grace_seconds.
    if proc_age_seconds - file_age_seconds > grace_seconds:
        lag_seconds = int(proc_age_seconds - file_age_seconds)
        return {
            "status": "warning",
            "detail": (
                f"session jsonls newer than verification's session_processor_state rows by ~{lag_seconds}s "
                f"(grace={grace_seconds}s). Check the verification-detector scheduler "
                f"job — uploads are not being processed."
            ),
            "lag_seconds": lag_seconds,
            "session_files": len(session_files),
        }

    # FIFO check (#0.47.4): the MAX-only comparison above can pass silently
    # when the verification-detector skips a particular file but keeps
    # processing newer ones. Detect that case by finding the oldest FS
    # jsonl whose path is NOT in session_processor_state.session_file
    # (for processor_name='verification') and surfacing it once it's older
    # than _stuck_file_grace_seconds.
    try:
        processed = session_processor_state_repo().processed_session_files("verification")
    except Exception as e:
        # Don't fail the health check on this enrichment.
        logger.debug("FIFO check: could not read session_processor_state: %s", e)
        return {"status": "ok", "session_files": len(session_files)}

    # session_processor_state.session_file is stored as the path the
    # processor saw. Older rows store an absolute path (e.g.
    # "/data/user_sessions/x/y.jsonl"); newer code stores a relative path
    # ("x/y.jsonl"). Match on either form so the FIFO check is robust to
    # both — a row stored under either spelling counts as processed.
    user_sessions_root = data_dir / "user_sessions"
    oldest_unprocessed: tuple[float, str] | None = None
    for f in session_files:
        try:
            rel = str(f.relative_to(user_sessions_root))
        except ValueError:
            continue  # not under user_sessions_root, skip
        absolute = str(f)
        if rel in processed or absolute in processed:
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if oldest_unprocessed is None or mtime < oldest_unprocessed[0]:
            oldest_unprocessed = (mtime, rel)

    if oldest_unprocessed is not None:
        stuck_grace = _stuck_file_grace_seconds()
        age_s = time_now() - oldest_unprocessed[0]
        if age_s > stuck_grace:
            return {
                "status": "info",
                "detail": (
                    f"verification-detector skipped a file: oldest unprocessed "
                    f"jsonl is ~{int(age_s)}s old "
                    f"(stuck_grace={stuck_grace}s, file={oldest_unprocessed[1]}). "
                    f"Newer files ARE being processed (this is FIFO-stuck, not "
                    f"a backlog). Check the verification-detector logs for "
                    f"this file's processing attempts."
                ),
                "stuck_file_age_seconds": int(age_s),
                "stuck_file": oldest_unprocessed[1],
                "session_files": len(session_files),
            }

    return {"status": "ok", "session_files": len(session_files)}


def time_now() -> float:
    """Wall-clock seconds since epoch — separated out for test seam parity."""
    import time as _t

    return _t.time()


def _verification_detector_grace_seconds() -> int:
    """Compute the staleness grace window for the session pipeline check."""
    cadence_seconds_default = 15 * 60
    raw = os.environ.get("SCHEDULER_VERIFICATION_DETECTOR_INTERVAL")
    if raw:
        try:
            cadence_seconds = int(raw)
            if cadence_seconds > 0:
                return 2 * cadence_seconds
        except ValueError:
            pass
    return 2 * cadence_seconds_default


def _check_db_schema() -> dict:
    """Check DB schema version against the expected head.

    Returns a dict with 'db_schema' key and optional 'detail' key.

    Backend-aware: on Postgres the system state lives in PG (the system DuckDB
    must never be opened — ``get_system_db()`` raises there), so we compare the
    ``alembic_version`` revision against the shipped migration head. On DuckDB
    we compare the ``schema_version`` table against ``SCHEMA_VERSION``.
    """
    from src.repositories import use_pg

    if use_pg():
        return _check_pg_db_schema()
    try:
        conn = get_system_db()
        row = conn.execute("SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1").fetchone()
        if row is None:
            return {"db_schema": "mismatch", "detail": "no schema_version row found"}
        current_version = row[0]
        if current_version == SCHEMA_VERSION:
            return {"db_schema": "ok", "current": current_version, "expected": SCHEMA_VERSION}
        else:
            return {"db_schema": "mismatch", "current": current_version, "expected": SCHEMA_VERSION}
    except Exception as e:
        return {"db_schema": "unreachable", "detail": str(e)}


def _check_pg_db_schema() -> dict:
    """Postgres variant of :func:`_check_db_schema` — compares the DB's stamped
    Alembic revision against the migration head shipped in this image. Doubles
    as a PG connectivity probe. Never opens the system DuckDB.
    """
    try:
        from src.db_pg import _pg_revisions

        current, head, _db_ahead = _pg_revisions()
        if current is None:
            return {"db_schema": "mismatch", "detail": "postgres alembic_version not stamped"}
        if current == head:
            return {"db_schema": "ok", "current": current, "expected": head}
        return {"db_schema": "mismatch", "current": current, "expected": head}
    except Exception as e:
        return {"db_schema": "unreachable", "detail": str(e)}


# The liveness probe (`/api/health`) is hit every few seconds by the LB, the
# compose healthcheck, and the on-VM watchdog. Re-running the schema `SELECT`
# on every probe serializes it behind any in-flight orchestrator rebuild
# (which writes `sync_state` on the same system connection) — the probe then
# times out and the watchdog fires a false `HEALTH: not 200`. The schema only
# changes at startup migration, so memoize the result for a short TTL.
_SCHEMA_CACHE_TTL = 30.0  # seconds
_schema_cache: dict | None = None
_schema_cache_at: float = 0.0


def _cached_db_schema() -> dict:
    """`_check_db_schema()` memoized for `_SCHEMA_CACHE_TTL` seconds.

    Transient `unreachable` results are never cached so a momentarily-busy DB
    recovers to `ok` on the next probe rather than pinning a stale failure.
    """
    global _schema_cache, _schema_cache_at
    now = time.monotonic()
    cached = _schema_cache
    if cached is not None and (now - _schema_cache_at) < _SCHEMA_CACHE_TTL:
        return cached
    result = _check_db_schema()
    if result.get("db_schema") != "unreachable":
        _schema_cache = result
        _schema_cache_at = now
    return result


@router.get("/api/health")
async def health_check():
    """Minimal health check for load balancers / compose healthcheck. No auth required.

    The schema read is offloaded to a worker thread (`get_system_db()` hands
    out a cursor-per-call, so this is thread-safe) and memoized — see
    `_cached_db_schema` — so a probe never blocks the event loop on DB
    contention during an orchestrator rebuild.
    """
    schema_check = await asyncio.to_thread(_cached_db_schema)
    status = "ok"
    if schema_check["db_schema"] != "ok":
        status = "unhealthy"
    return {"status": status, "vault_key_configured": vault_key_configured(), **schema_check}


@router.get("/api/health/detailed")
async def health_check_detailed(
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    _user: dict = Depends(get_current_user),
    include: str = Query(
        "",
        description=(
            "Comma-separated list of optional checks to include. "
            "Recognised values: `schema` (DB schema version against the "
            "expected migration). The default response omits these because "
            "they're rarely actionable on a healthy instance and add noise "
            "to `agnes diagnose` output (issue #204). Pass `?include=schema` "
            "to get the legacy behavior."
        ),
    ),
):
    """Structured health check with deployment metadata. Requires authentication."""
    checks = {}
    include_set = {p.strip() for p in include.split(",") if p.strip()}

    # State-backend connectivity probe. Backend-aware: on Postgres the request
    # conn (from _get_db) is None — the system DuckDB must never be opened — so
    # probe the active PG engine instead. A healthy PG instance must report ok
    # here, not flip the whole check to "unhealthy" on a None.execute().
    from src.repositories import use_pg

    try:
        if use_pg():
            import sqlalchemy as sa

            from src.db_pg import get_engine

            with get_engine().connect() as _pg_conn:
                _pg_conn.execute(sa.text("SELECT 1")).fetchone()
        else:
            conn.execute("SELECT 1").fetchone()
        checks["duckdb_state"] = {"status": "ok"}
    except Exception as e:
        checks["duckdb_state"] = {"status": "error", "detail": str(e)}

    # DB schema version check — opt-in (issue #204). Operators who run a
    # fresh release pinned to the same image as the running schema rarely
    # care about this number; analysts hitting the endpoint via
    # `agnes diagnose` see it as noise. Surface it on demand via
    # `?include=schema` (the dashboard / admin UI passes this; default
    # CLI does not).
    if "schema" in include_set:
        # Offload to a worker thread + reuse the 30s liveness cache: the schema
        # read is a synchronous PG round-trip and this handler is `async def`,
        # so running it inline blocks the event loop when the dashboard polls
        # `?include=schema`. Copy the cached dict — `_cached_db_schema` returns
        # the shared `_schema_cache` object, and the audience-tagging loop below
        # mutates each check in place; without the copy that `audience` key would
        # bleed into the cached result the public `/api/health` probe returns.
        checks["db_schema"] = dict(await asyncio.to_thread(_cached_db_schema))

    # Sync state summary
    try:
        repo = sync_state_repo()
        all_states = repo.get_all_states()
        total_tables = len(all_states)
        total_rows = sum(s.get("rows", 0) or 0 for s in all_states)
        stale = []
        now = datetime.now(timezone.utc)
        for s in all_states:
            last = s.get("last_sync")
            if last:
                try:
                    # Handle both tz-aware and tz-naive datetimes from DuckDB
                    if hasattr(last, "tzinfo") and last.tzinfo is None:
                        from datetime import timezone as tz

                        last = last.replace(tzinfo=tz.utc)
                    if (now - last).total_seconds() > 86400:
                        stale.append(s["table_id"])
                except (TypeError, AttributeError):
                    pass  # skip if timestamp comparison fails
        checks["data"] = {
            "status": "ok" if not stale else "warning",
            "tables": total_tables,
            "total_rows": total_rows,
            "stale_tables": stale,
        }
    except Exception as e:
        checks["data"] = {"status": "error", "detail": str(e)}

    # User count
    try:
        user_count = users_repo().count_all()
        checks["users"] = {"status": "ok", "count": user_count}
    except Exception as e:
        checks["users"] = {"status": "error", "detail": str(e)}

    # BigQuery billing-project sanity check (USER_PROJECT_DENIED footgun).
    bq_cfg = _check_bq_billing_project()
    if bq_cfg is not None:
        checks["bq_config"] = bq_cfg

    # Session pipeline (#176): warn when uploaded jsonls aren't getting
    # processed by the verification-detector cadence.
    try:
        checks["session_pipeline"] = _check_session_pipeline()
    except Exception as e:
        checks["session_pipeline"] = {"status": "unknown", "detail": str(e)}

    # Per-check audience tagging (issue #345 B): which checks does an
    # analyst care about vs. which are operator-only? An analyst seeing
    # `Overall: degraded` because the scheduler is behind on session
    # extraction has no way to act on it — the warning is real, but it's
    # not their warning. ``audience`` lets clients compute a role-aware
    # headline without dropping checks from the response payload.
    _AUDIENCE = {
        "duckdb_state": "analyst",  # query path; visible everywhere
        "db_schema": "operator",  # schema migration sanity
        "data": "operator",  # stale tables — admin scheduler
        "users": "operator",  # admin-side ops only
        "bq_config": "operator",  # cluster-level config
        "session_pipeline": "operator",  # admin-driven cadence
    }
    for name, check in checks.items():
        check.setdefault("audience", _AUDIENCE.get(name, "operator"))

    def _aggregate(items) -> str:
        out = "healthy"
        for check in items:
            if check.get("status") == "error":
                return "unhealthy"
            if check.get("status") == "warning":
                out = "degraded"
        return out

    # Schema mismatch escalates only when the caller asked for the check
    # — otherwise the absent key is treated as "not asserted".
    def _apply_schema_escalation(status: str) -> str:
        if "db_schema" in checks and checks["db_schema"].get("db_schema") != "ok":
            return "unhealthy"
        return status

    overall = _apply_schema_escalation(_aggregate(checks.values()))
    overall_analyst = _aggregate(c for c in checks.values() if c.get("audience") == "analyst")
    # db_schema is audience=operator, so it never escalates the analyst
    # headline — that's the intent.

    # Caller role surface: clients pick the headline they should display
    # based on their own privilege. Admins/operators see ``overall``;
    # analysts see ``overall_analyst`` and an optional flag to opt in.
    caller_role = "admin" if _user.get("is_admin") else "analyst"
    is_admin_caller = caller_role == "admin"

    # SECURITY (#16): the per-check ``audience`` tags were advisory only — every
    # authenticated caller received the full body, including operator-only infra
    # detail (real GCP billing/data project ids in bq_config, total user count,
    # stale table ids, schema versions) plus the build fingerprints
    # (image_tag/commit_sha). Enforce the tags: a non-admin analyst gets
    # operator-audience checks reduced to a bare {status, audience} and the
    # image_tag/commit_sha omitted. Admins/operators still get everything.
    if is_admin_caller:
        services = checks
        build_meta = {
            "version": os.environ.get("AGNES_VERSION", "dev"),
            "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
            "image_tag": os.environ.get("AGNES_TAG", "unknown"),
            "commit_sha": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        }
    else:
        services = {
            name: (
                {"status": check.get("status"), "audience": "operator"}
                if check.get("audience") == "operator"
                else check
            )
            for name, check in checks.items()
        }
        build_meta = {
            "version": os.environ.get("AGNES_VERSION", "dev"),
            "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
        }

    return {
        "status": overall,
        "overall_analyst": overall_analyst,
        "caller_role": caller_role,
        **build_meta,
        "schema_version": SCHEMA_VERSION,
        "deployed_at": _DEPLOYED_AT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }


@router.get("/api/debug/throw")
async def debug_throw(
    user: dict = Depends(get_current_user),
    kind: str = "RuntimeError",
    msg: str = "intentional debug throw",
):
    """Deliberate-crash route for verifying observability wiring.

    Gated by ``DEBUG=1`` — returns 404 in production. Always raises after
    the auth dependency resolves, so ``request.state.user`` is populated
    by the time the unhandled-exception handler runs. Use it to confirm
    that a real unhandled exception reaches the log pipeline with the
    request context attached (``request_id``, path, method) and renders
    the error page you expect.

    Optional query params let you pick the exception type and message:
        /api/debug/throw?kind=ValueError&msg=hello
    """
    if os.environ.get("DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
        from fastapi import HTTPException

        raise HTTPException(status_code=404)

    types = {
        "RuntimeError": RuntimeError,
        "ValueError": ValueError,
        "ZeroDivisionError": ZeroDivisionError,
        "KeyError": KeyError,
        "TypeError": TypeError,
    }
    cls = types.get(kind, RuntimeError)
    raise cls(msg)


@router.get("/api/version")
async def version_info():
    """Lightweight version info — cacheable, no DB touch. Used by UI footer badge."""
    return {
        "version": os.environ.get("AGNES_VERSION", "dev"),
        "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
        "image_tag": os.environ.get("AGNES_TAG", "unknown"),
        "deployed_at": _DEPLOYED_AT,
    }
