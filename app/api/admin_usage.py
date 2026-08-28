"""Admin telemetry endpoints: /api/admin/usage/*.

Phase C.1: GET /api/admin/usage/export — stream telemetry export as csv|json|parquet
Phase C.3: POST /api/admin/usage/ask  — LLM Text-to-SQL over usage_* tables
Phase C.4: POST /api/admin/usage/reprocess, POST /api/admin/usage/prune

All endpoints admin-only. Export writes one audit_log row per call.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth.access import require_admin
from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
)
from connectors.llm.factory import create_vertex_extractor, vertex_config_or_none
from src.repositories import (
    audit_repo,
    usage_repo,
    use_pg,
)
from src.usage_ask import (
    RESPONSE_SCHEMA,
    build_prompt,
    system_prompt,
    validate_select_only,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/telemetry", tags=["admin-telemetry"])

_FORMATS = ("csv", "json", "parquet")


@router.get("/export")
def export_usage(
    format: Literal["csv", "json", "parquet"] = Query("csv"),
    since: Optional[str] = Query(None, description="ISO date or datetime; events with occurred_at >= since"),
    until: Optional[str] = Query(None, description="ISO date or datetime; events with occurred_at < until"),
    user_id: Optional[str] = Query(None, description="Filter to a single user_id"),
    source: Optional[Literal["curated", "flea", "builtin"]] = Query(None),
    user: dict = Depends(require_admin),
):
    """Stream usage_events filtered by since/until/user_id/source.

    Reads through the backend-aware repository factory so the export
    reflects the active state backend (DuckDB or Postgres), not the
    always-DuckDB request connection (#513/#518 bug class).

    CSV: standard library `csv.writer`, one row per event with all columns.
    JSON: streaming NDJSON (one JSON object per line) — easier to pipe + tail.
    Parquet: pyarrow table written to an in-memory buffer (engine-agnostic).
    """
    filters: dict = {"username": user_id, "source": source}
    if since:
        try:
            filters["since"] = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid since: {since}")
    if until:
        try:
            filters["until"] = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid until: {until}")

    repo = usage_repo()
    # Row count for audit (one extra query — acceptable for audit fidelity)
    row_count = repo.count_events_export(filters)

    audit_params = {
        "format": format,
        "since": since,
        "until": until,
        "user_id": user_id,
        "source": source,
        "row_count": row_count,
    }
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.export",
            params=audit_params,
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.export; continuing")

    cols, rows = repo.export_events(filters)
    if format == "csv":
        return _stream_csv(cols, rows)
    elif format == "json":
        return _stream_ndjson(cols, rows)
    else:
        return _stream_parquet(cols, rows)


def _stream_csv(cols, rows):
    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows:
            w.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_events.csv"},
    )


def _stream_ndjson(cols, rows):
    def gen():
        for row in rows:
            d = {}
            for k, v in zip(cols, row):
                if isinstance(v, datetime):
                    d[k] = v.isoformat()
                else:
                    d[k] = v
            yield json.dumps(d) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=usage_events.ndjson"},
    )


def _stream_parquet(cols, rows):
    # Engine-agnostic: build the parquet in memory with pyarrow (a core
    # dependency) instead of a DuckDB copy-to-file, so the same path serves
    # both state backends and no temp file can be leaked.
    import pyarrow as pa
    import pyarrow.parquet as pq

    if rows:
        table = pa.Table.from_pylist([dict(zip(cols, r)) for r in rows])
    else:
        # Zero-row export still needs a valid schema; string-type every column.
        table = pa.table({c: pa.array([], type=pa.string()) for c in cols})

    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)

    def gen():
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=usage_events.parquet"},
    )


# ---------------------------------------------------------------------------
# POST /api/admin/usage/ask — LLM Text-to-SQL (Phase C.3)
# ---------------------------------------------------------------------------

_ASK_MODEL = os.environ.get("USAGE_ASK_MODEL", "claude-haiku-4-5-20251001")


@router.post("/ask")
def ask_usage(
    payload: dict = Body(...),
    user: dict = Depends(require_admin),
):
    """Translate a natural-language question to SELECT-only SQL via Anthropic + execute.

    The prompt asks for the active backend's dialect and the validated
    SELECT runs through the repository factory, so a Postgres-backed
    instance answers from Postgres — not the orphaned DuckDB file.

    Returns the generated SQL even when validation rejects it, so the
    admin sees what the LLM tried.
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="question too long (>1000 chars)")

    vertex = vertex_config_or_none()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key and not vertex:
        raise HTTPException(
            status_code=503,
            detail=(
                "ANTHROPIC_API_KEY is not configured on the server. Set it in "
                "instance env / .env_overlay, or configure ai.provider: vertex."
            ),
        )

    dialect = "postgresql" if use_pg() else "duckdb"
    try:
        # Constructed inside the error boundary: the SDK import is deferred
        # to first use, so client construction is a failure point too — an
        # escaped exception here would surface as an unhandled 500 instead
        # of this endpoint's documented LLM-failure response. t0 is taken
        # after construction so the reported llm_ms stays the API call.
        extractor: AnthropicExtractor
        if vertex:
            extractor = create_vertex_extractor(_ASK_MODEL)
        else:
            extractor = AnthropicExtractor(api_key=api_key, model=_ASK_MODEL)
        t0 = time.monotonic()
        llm_out = extractor.extract_json(
            prompt=build_prompt(question),
            max_tokens=1024,
            json_schema=RESPONSE_SCHEMA,
            schema_name="usage_ask_response",
            system=system_prompt(dialect),
        )
    except LLMAuthError:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is invalid")
    except LLMRateLimitError:
        raise HTTPException(status_code=503, detail="LLM rate limit — try again in a moment")
    except LLMTimeoutError:
        raise HTTPException(status_code=503, detail="LLM timeout — try again")
    except LLMRefusalError:
        raise HTTPException(status_code=400, detail="LLM refused the request (probably an unsafe question)")
    except LLMFormatError:
        raise HTTPException(status_code=502, detail="LLM returned non-JSON output — try rephrasing")
    except Exception as e:
        logger.exception("usage.ask LLM call failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    llm_ms = int((time.monotonic() - t0) * 1000)

    sql = llm_out.get("sql") or ""
    rationale = llm_out.get("rationale") or ""

    try:
        validated_sql = validate_select_only(sql)
    except ValueError as e:
        # Return 200 with rejection details so admin sees what the LLM tried.
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="usage.ask",
                params={"question": question, "sql": sql, "rejected": str(e), "llm_ms": llm_ms},
                result="error.invalid_sql",
            )
        except Exception:
            logger.exception("audit_log write failed for usage.ask rejection")
        return {
            "question": question,
            "sql": sql,
            "rationale": rationale,
            "rejected": str(e),
            "rows": None,
            "row_count": 0,
            "llm_ms": llm_ms,
        }

    # Execute the validated SQL with a row cap (defense in depth even though prompt asks for LIMIT)
    exec_t0 = time.monotonic()
    try:
        cols, rows = usage_repo().execute_readonly_select(validated_sql)
        if len(rows) > 1000:
            rows = rows[:1000]
            truncated = True
        else:
            truncated = False
        row_dicts = [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(cols, r)} for r in rows]
    except Exception as e:
        logger.exception("usage.ask SQL execution failed")
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="usage.ask",
                params={"question": question, "sql": validated_sql, "error": str(e), "llm_ms": llm_ms},
                result="error.exec_failed",
            )
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"SQL execution failed: {e}")
    exec_ms = int((time.monotonic() - exec_t0) * 1000)

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.ask",
            params={
                "question": question,
                "sql": validated_sql,
                "row_count": len(row_dicts),
                "llm_ms": llm_ms,
                "exec_ms": exec_ms,
            },
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.ask success")

    return {
        "question": question,
        "sql": validated_sql,
        "rationale": rationale,
        "columns": cols,
        "rows": row_dicts,
        "row_count": len(row_dicts),
        "truncated": truncated,
        "llm_ms": llm_ms,
        "exec_ms": exec_ms,
    }


# ---------------------------------------------------------------------------
# POST /api/admin/usage/reprocess — force re-extraction (Phase C.4)
# ---------------------------------------------------------------------------


@router.post("/reprocess")
def reprocess_usage(
    user: dict = Depends(require_admin),
):
    """Force re-extraction of all sessions for the usage processor.

    DELETEs:
      - session_processor_state WHERE processor_name='usage' (so the next
        scheduler tick re-scans every JSONL) + processor_name='marketplace_rollup_30d'
        (forces 30d window rebuild on the next tick)
      - usage_events
      - usage_session_summary
      - usage_tool_daily (legacy)
      - usage_marketplace_item_daily
      - usage_marketplace_item_window

    Verification processor's state untouched (composite PK isolates each processor).
    Audit-logged with deleted-row counts.
    """
    counts = {}
    try:
        # Single atomic reset: the usage rollups AND the matching processor
        # checkpoints are cleared in one transaction so a failure can't leave
        # the scheduler half-reset (processor state gone, usage data retained).
        reset = usage_repo().reset_all(clear_processors=["usage", "marketplace_rollup_30d"])
        counts["state_rows"] = reset["state_rows"]
        counts["events"] = reset["events"]
        counts["summaries"] = reset["session_summary"]
        counts["tool_daily"] = reset["tool_daily"]
        counts["marketplace_item_daily"] = reset["marketplace_item_daily"]
        counts["marketplace_item_window"] = reset["marketplace_item_window"]
    except Exception as e:
        logger.exception("reprocess failed")
        raise HTTPException(status_code=500, detail=f"reprocess failed: {e}")

    # Full rebuild (since_day=None) immediately re-establishes a consistent
    # rollup state — #728. Right after reset_all, usage_events is empty (the
    # next scheduler tick re-scans every session jsonl and repopulates it);
    # the real payoff is that any FUTURE rollup rebuild that finds re-ingested
    # history covers it in full, since the free-function's old "since_day=None
    # -> today-7" default (the bug this PR fixes) used to leave days 8+ back
    # empty forever after a reprocess. Best-effort: a failure here shouldn't
    # fail the reprocess request itself (the reset already succeeded).
    try:
        usage_repo().rebuild_rollups(force_30d=True)
    except Exception:
        logger.exception("usage rollup rebuild after reprocess failed; continuing")

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.reprocess",
            params=counts,
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.reprocess; continuing")

    return {"status": "ok", "deleted": counts}


# ---------------------------------------------------------------------------
# POST /api/admin/usage/prune — retention-based event pruning (Phase C.4)
# ---------------------------------------------------------------------------


@router.post("/prune")
def prune_usage(
    user: dict = Depends(require_admin),
):
    """Delete usage_events older than the configured retention window.

    Window source: ``USAGE_EVENTS_RETENTION_DAYS`` env var (back-compat), or
    ``retention.usage_events_days`` in instance.yaml (Track E3 Slice 1) —
    see ``app.instance_config.get_usage_events_retention_days``. Default
    retention: unset or ``0`` → no pruning (forever). Daily rollup tables
    untouched — they're tiny and lossy-by-design.
    """
    from app.instance_config import get_usage_events_retention_days

    retention = get_usage_events_retention_days()
    if retention <= 0:
        return {"status": "skipped", "reason": "usage_events retention window unset or 0"}
    try:
        deleted = usage_repo().delete_older_than(retention)
        after = usage_repo().count_events()
    except Exception as e:
        logger.exception("prune failed")
        raise HTTPException(status_code=500, detail=f"prune failed: {e}")

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="usage.prune",
            params={"retention_days": retention, "deleted": deleted, "remaining": after},
            result="success",
        )
    except Exception:
        logger.exception("audit_log write failed for usage.prune; continuing")

    return {"status": "ok", "retention_days": retention, "deleted": deleted, "remaining": after}
