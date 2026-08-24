"""Scheduled runs for agent profiles (agent schedules, v119).

Design doc: docs/superpowers/specs/2026-08-17-agent-schedules-design.md.

Two surfaces, one router (``prefix="/api/v1/agents"``):

- Owner-scoped CRUD — ``GET/POST /{slug}/schedules``,
  ``PATCH/DELETE /{slug}/schedules/{schedule_id}``. Same auth posture as the
  rest of the agent-profiles management surface (``app/api/agents_admin.py``,
  ``app/api/agent_webhooks.py``): ``require_session_token`` rejects every
  PAT flavor (plain and agent PATs alike) — a schedule is a standing config
  an owner sets up once, not something an agent tool call should be able to
  grant itself unattended runs through.
- ``POST /run-due`` — the admin/scheduler-driven sweep. Modeled on
  ``POST /api/scripts/run-due`` (``app/api/scripts.py``): walk every
  enabled row (any owner), evaluate due-ness with
  ``src.scheduler.is_table_due``, atomically claim via
  ``AgentSchedulesRepository.claim_for_run`` (optimistic concurrency — a
  concurrent sweep tick that already won the claim is silently skipped),
  and enqueue the existing ``agent_response`` job kind directly with the
  agent OWNER's identity. This is what makes scheduler-initiated runs work
  at all: the public ``/responses`` endpoint resolves an agent by
  (caller, slug) and the scheduler owns no agents of its own, so the sweep
  enqueues straight into the jobs table instead of impersonating a session.
  Per-row failures are logged and skipped — they never abort the sweep.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc

from app.auth.access import require_admin, require_agent_profiles_enabled
from app.auth.dependencies import _get_db, require_session_or_user_pat, require_session_token
from src.repositories import agent_schedules_repo, agents_repo, audit_repo, jobs_repo, users_repo
from src.scheduler import is_table_due, is_valid_schedule

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agent-schedules"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

#: Design doc "Storage" — the "unattended fan-out needs a ceiling" cap.
MAX_SCHEDULES_PER_AGENT = 20

#: A single safe path-ish segment (mirrors the run-type-label examples in
#: the design doc, e.g. "morning-briefing") — letters/digits/dash/underscore
#: only, no "/" or "..", so a schedule name is never mistakable for a path.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: The existing background job kind a schedule fires — see
#: `app/worker/kinds.py::_run_agent_response` for the payload shape this
#: matches (`mode`, `owner_user_id`, `owner_email`, `agent_id`, `prompt`).
_JOB_KIND = "agent_response"


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(actor: Optional[str], action: str, target: str, params: Optional[dict] = None) -> None:
    try:
        audit_repo().log(user_id=actor, action=action, resource=f"agent_schedule:{target}", params=params)
    except Exception:
        logger.exception("audit_log write failed for %s; continuing", action)


def _load_agent_by_slug(slug: str, user: dict) -> Dict[str, Any]:
    """404 (never 403) for an unknown or not-owned slug — existence of
    another owner's agent is never leaked, matching
    `app/api/agent_webhooks.py::_load_agent_by_slug`."""
    agent = agents_repo().get_by_slug(user["id"], slug)
    if agent is None or agent.get("deleted_at") is not None:
        raise _err(404, "agent_not_found", "Agent not found")
    return agent


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("last_run_at", "created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise _err(
            400,
            "invalid_name",
            "name must be a single path-ish segment: letters, digits, '-' or '_' only, max 64 chars",
        )
    return name


def _validate_schedule(schedule: str) -> str:
    if not is_valid_schedule(schedule):
        raise _err(
            400,
            "invalid_schedule",
            "schedule must be 'every Nm' / 'every Nh', 'daily HH:MM[,HH:MM,...]' (UTC), "
            "or 'cron <5-field expr>' (UTC) — note the literal 'cron ' prefix",
        )
    return schedule


def _validate_prompt(prompt: str) -> str:
    if not prompt or not prompt.strip():
        raise _err(400, "invalid_prompt", "prompt is required")
    return prompt


class CreateScheduleRequest(BaseModel):
    name: str
    schedule: str
    prompt: str
    enabled: bool = True


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    schedule: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Owner-scoped CRUD
# ---------------------------------------------------------------------------


@router.get("/{slug}/schedules")
async def list_schedules(
    slug: str,
    user: dict = Depends(require_session_or_user_pat),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    agent = _load_agent_by_slug(slug, user)
    rows = agent_schedules_repo().list_for_agent(agent["id"])
    return {"data": [_serialize(r) for r in rows], "has_more": False, "next_cursor": None}


@router.post("/{slug}/schedules", status_code=201)
async def create_schedule(
    slug: str,
    payload: CreateScheduleRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    agent = _load_agent_by_slug(slug, user)
    repo = agent_schedules_repo()

    if repo.count_for_agent(agent["id"]) >= MAX_SCHEDULES_PER_AGENT:
        raise _err(
            400,
            "schedule_limit",
            f"agent already has the maximum of {MAX_SCHEDULES_PER_AGENT} schedules",
        )

    name = _validate_name(payload.name)
    schedule = _validate_schedule(payload.schedule)
    prompt = _validate_prompt(payload.prompt)

    if repo.get_by_name(agent["id"], name) is not None:
        raise _err(409, "schedule_name_taken", f"schedule name '{name}' is already in use for this agent")

    schedule_id = str(uuid.uuid4())
    try:
        repo.create(
            id=schedule_id,
            agent_id=agent["id"],
            name=name,
            schedule=schedule,
            prompt=prompt,
            enabled=payload.enabled,
        )
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # Race backstop — see `tests/db_pg/test_agent_schedules_contract.py
        # ::test_unique_name_per_agent_enforced_at_the_db_layer`.
        raise _err(409, "schedule_name_taken", f"schedule name '{name}' is already in use for this agent")

    row = repo.get(schedule_id)
    assert row is not None  # just created above, same transaction/connection
    _audit(user["id"], "agent_schedule.create", schedule_id, {"agent_id": agent["id"], "name": name})
    return _serialize(row)


@router.patch("/{slug}/schedules/{schedule_id}")
async def update_schedule(
    slug: str,
    schedule_id: str,
    payload: UpdateScheduleRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    agent = _load_agent_by_slug(slug, user)
    repo = agent_schedules_repo()
    row = repo.get(schedule_id)
    if row is None or row["agent_id"] != agent["id"]:
        raise _err(404, "schedule_not_found", "Schedule not found")

    updates = payload.model_dump(exclude_unset=True)
    # An explicitly-sent `"enabled": null` survives exclude_unset and would
    # trip the column's NOT NULL constraint — which the except below maps to
    # a misleading 409 schedule_name_taken (Devin Review on #1404). Reject it
    # as the validation error it is.
    if "enabled" in updates and updates["enabled"] is None:
        raise _err(400, "invalid_enabled", "enabled must be true or false, not null")
    if "name" in updates:
        updates["name"] = _validate_name(updates["name"])
        existing = repo.get_by_name(agent["id"], updates["name"])
        if existing is not None and existing["id"] != schedule_id:
            raise _err(
                409,
                "schedule_name_taken",
                f"schedule name '{updates['name']}' is already in use for this agent",
            )
    if "schedule" in updates:
        updates["schedule"] = _validate_schedule(updates["schedule"])
    if "prompt" in updates:
        updates["prompt"] = _validate_prompt(updates["prompt"])

    if updates:
        try:
            repo.update(schedule_id, **updates)
        except (duckdb.ConstraintException, sa_exc.IntegrityError):
            raise _err(409, "schedule_name_taken", "schedule name is already in use for this agent")
        _audit(user["id"], "agent_schedule.update", schedule_id, {"fields": sorted(updates)})

    updated = repo.get(schedule_id)
    assert updated is not None  # just updated (or no-op) above, same connection
    return _serialize(updated)


@router.delete("/{slug}/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    slug: str,
    schedule_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    agent = _load_agent_by_slug(slug, user)
    repo = agent_schedules_repo()
    row = repo.get(schedule_id)
    if row is None or row["agent_id"] != agent["id"]:
        raise _err(404, "schedule_not_found", "Schedule not found")
    repo.delete(schedule_id)
    _audit(user["id"], "agent_schedule.delete", schedule_id, {"agent_id": agent["id"]})


# ---------------------------------------------------------------------------
# Admin/scheduler-driven sweep
# ---------------------------------------------------------------------------


def _dispatch_if_due(row: Dict[str, Any], now: datetime) -> bool:
    """Evaluate one ``agent_schedules`` row and, if due, claim + dispatch it.

    Returns True iff this call won the claim (i.e. the row was due and no
    concurrent sweep tick got there first) — the caller counts that as one
    dispatched row, whether or not the subsequent enqueue itself succeeded
    (a failed enqueue still records `last_status='failed_enqueue'` so the
    row isn't silently stuck). Returns False for every other outcome
    (disabled — filtered by the caller already, agent missing/soft-deleted,
    not due yet, or lost the claim race). Never raises: the caller wraps
    this per-row and keeps sweeping regardless.
    """
    agent = agents_repo().get_by_id(row["agent_id"])
    if agent is None or agent.get("deleted_at") is not None:
        return False

    last_run_at = row.get("last_run_at")
    last_run_iso = last_run_at.isoformat() if last_run_at else None
    if not is_table_due(row["schedule"], last_run_iso, now=now):
        return False

    repo = agent_schedules_repo()
    # Backlog guard: if this schedule's previous run is still sitting queued
    # (no worker has claimed it — e.g. a process topology where nothing
    # registers the agent_response kind, or a saturated LIGHT lane), don't
    # stack another job on top of it every cadence hit. The pile-up is
    # bounded to ONE queued job per schedule; the row's tick is still
    # consumed via the claim below so the next check is a cadence away
    # (Devin Review on #1404).
    prev_job_id = row.get("last_job_id")
    if prev_job_id:
        prev = jobs_repo().get(prev_job_id)
        if prev is not None and prev.get("status") == "queued":
            if not repo.claim_for_run(row["id"], last_run_at, now):
                return False
            logger.warning(
                "agent_schedule %s: previous run %s is still queued — recording backlogged instead of enqueuing another",
                row["id"],
                prev_job_id,
            )
            repo.record_dispatch_result(row["id"], "backlogged", job_id=prev_job_id)
            return True

    if not repo.claim_for_run(row["id"], last_run_at, now):
        # Lost the race to a concurrent sweep tick — not an error.
        return False

    owner = users_repo().get_by_id(agent["owner_user_id"])
    if owner is None:
        logger.warning(
            "agent_schedule %s: owner %s no longer exists; recording failed_enqueue",
            row["id"],
            agent["owner_user_id"],
        )
        repo.record_dispatch_result(row["id"], "failed_enqueue")
        return True

    # "agent-schedule:<schedule_id>:<floor(unix_now/60)>" — a retried sweep
    # within the same minute dedupes at the jobs table (see
    # `src.repositories.jobs.JobsRepository.enqueue`).
    idempotency_key = f"agent-schedule:{row['id']}:{int(now.timestamp() // 60)}"
    try:
        job = jobs_repo().enqueue(
            _JOB_KIND,
            {
                "mode": "fresh",
                "owner_user_id": agent["owner_user_id"],
                "owner_email": owner["email"],
                "agent_id": agent["id"],
                "prompt": row["prompt"],
                "metadata": {"trigger": "agent_schedule", "schedule_id": row["id"], "schedule_name": row["name"]},
                "response_format": None,
            },
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.exception("agent_schedule %s: enqueue failed", row["id"])
        repo.record_dispatch_result(row["id"], "failed_enqueue")
        return True

    repo.record_dispatch_result(row["id"], "enqueued", job_id=job["id"])
    return True


@router.post("/run-due")
async def run_due_agent_schedules(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Fire every enabled schedule whose cadence says it is due.

    Gated like every other scheduler-driven sweep (`require_admin` — the
    scheduler's shared-secret token resolves to a synthetic Admin-group
    user, see `app.auth.scheduler_token`). Scheduler row: `agents:run-due`
    in `services/scheduler/__main__.py`, `every 1m`, gated on
    `SCHEDULER_AGENT_SCHEDULES` (default on).
    """
    now = datetime.now(timezone.utc)
    repo = agent_schedules_repo()
    dispatched: List[str] = []
    for row in repo.list_enabled():
        try:
            if _dispatch_if_due(row, now):
                dispatched.append(row["id"])
        except Exception:
            # Never let one bad row abort the sweep.
            logger.exception("agents:run-due — schedule %s failed; continuing sweep", row.get("id"))

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="agent_schedules.run_due.tick",
            params={"dispatched": len(dispatched)},
            result="success",
            client_kind="scheduler",
        )
    except Exception:
        logger.exception("audit_log write failed for agent_schedules.run_due.tick; continuing")

    return {"dispatched": dispatched, "count": len(dispatched)}
