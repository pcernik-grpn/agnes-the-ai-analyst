"""Agent-as-API runtime — `POST /api/v1/agents/{slug}/responses` +
`GET /api/v1/jobs/{id}` (Task 9, `docs/superpowers/specs/
2026-07-21-agent-profiles-and-agent-api-design.md` §3).

One-shot request/response over an agent the caller owns OR the agent was
shared to (C2.3, shared-agent runtime): creates a fresh headless chat
session (`app/chat/headless.py`), sends the caller's `input`, and either
returns the answer synchronously (bounded by `timeout_s`) or degrades to a
background job (`background: true`, or a sync call whose wait outran
`timeout_s` — the RUN itself is never killed, only the wait).

Auth chain (`require_agent_runtime_principal`): `get_current_user` (never
`get_optional_user` — this surface must 401, not silently downgrade to
anonymous, on a missing/invalid credential) -> resolve the agent
owned-or-runnable for that caller (`agents_repo().get_runnable_by_slug`,
404 if neither) -> if the credential is an agent PAT, 403 unless its
`agent_id` claim matches this agent -> the same `ResourceType.CHAT` grant
check the chat WS route uses (`app/api/chat.py::require_chat_access`).
Row-level access policies (`src/access_policy.py`) bind to THIS caller, not
the agent's owner — a shared agent's rows are filtered per grantee.

Idempotency (`Idempotency-Key` header): scoped to `(key, owner, agent)` —
see `src/repositories/idempotency.py`. A replay with an identical raw-body
hash returns the stored response verbatim (same status, same body, `answer`
NOT re-generated); a replay with a different body under the same key is
`409 idempotency_key_reuse`. A key is RESERVED (via `idempotency_repo()
.reserve()`) immediately after the initial `get()` miss, before either the
sync run or the background enqueue — this closes the double-execution
window where two concurrent requests under the same key both miss `get()`
and both run the underlying work; the loser of that race gets `409
idempotency_key_in_flight` (same hash) or `idempotency_key_reuse`
(different hash) instead. See `reserve()`'s docstring for the staleness
rules governing a reservation whose owning request crashed.

`ConcurrencyCapHit` (the per-user active-session cap,
`app.chat.manager.ChatManager.create_session`) is mapped to `429
{"code": "concurrency_cap"}` on the sync path — same condition
`app/api/chat.py::create_session` 429s on, just this router's own
`detail.code` envelope shape instead of `chat.py`'s `detail.kind`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from app.auth.access import can_access, is_user_admin, require_agent_profiles_enabled
from app.auth.dependencies import _get_db, get_current_user
from app.auth.pat_resolver import agent_id_from_request
from app.auth.session_principal import PRINCIPAL_TYPES
from app.chat.agent_usage import agent_config_hash, usage_for_session
from app.chat.headless import run_one_shot
from app.chat.manager import ConcurrencyCapHit, get_current_chat_manager
from app.chat.structured_output import schema_directive, validate
from app.logging_config import request_id_var
from app.resource_types import ResourceType
from src.audit_helpers import log_safe
from src.repositories import agents_repo, idempotency_repo, jobs_repo, llm_usage_repo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["agent-runtime"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

#: `jobs.kind` for both the background-from-the-start path and the
#: sync-timeout-degrades-to-background path — see `app/worker/kinds.py`'s
#: `_run_agent_response` for how it branches on `payload["mode"]`.
JOB_KIND = "agent_response"

#: Duplicated (not imported) from `app/worker/kinds.py::CONCURRENCY_CAP_ERROR_PREFIX`
#: — same HEAVY_LANE/LIGHT_LANE-style rationale as that module's own comment:
#: the CONTRACT is the string value persisted into `jobs.error`, not which
#: module owns the source of truth for it. `_serialize_job` below strips this
#: prefix back off to surface a structured `{"code": "concurrency_cap", ...}`
#: instead of a raw string for `GET /api/v1/jobs/{id}`.
_CONCURRENCY_CAP_ERROR_PREFIX = "concurrency_cap: "

#: Duplicated (not imported) from
#: `app/worker/kinds.py::SCHEMA_VALIDATION_ERROR_PREFIX` — same rationale as
#: `_CONCURRENCY_CAP_ERROR_PREFIX` above. The remainder of the string (after
#: this prefix) is a JSON object — see `_serialize_error` below.
_SCHEMA_VALIDATION_ERROR_PREFIX = "schema_validation_failed: "

_DEFAULT_TIMEOUT_S = 120
_MIN_TIMEOUT_S = 1
_MAX_TIMEOUT_S = 600

# Public status mapping for GET /api/v1/jobs/{id} — the internal `jobs.status`
# vocabulary (queued/running/done/failed) is DB-lifecycle language; the API
# surfaces request/response-shaped names instead.
_PUBLIC_STATUS_MAP = {
    "queued": "queued",
    "running": "in_progress",
    "done": "completed",
    "failed": "failed",
}


def _idempotency_ttl_s() -> int:
    raw = os.environ.get("AGNES_IDEMPOTENCY_TTL_S")
    if raw is None:
        return 86400  # 24h default, per the task spec
    try:
        return max(int(raw), 1)
    except ValueError:
        return 86400


class AgentResponseRequest(BaseModel):
    input: str
    background: bool = False
    timeout_s: int = _DEFAULT_TIMEOUT_S
    metadata: Optional[Dict[str, Any]] = None
    #: Structured-output request — `{"type": "json_schema", "schema": {...}}`.
    #: When present, `schema_directive()`'s text is appended to `input`
    #: before the run (prompt-steering, see `app.chat.structured_output`'s
    #: module docstring), and the collected answer is validated against the
    #: schema afterward (`validate()`) before the response is built. See
    #: `create_agent_response` below for the 200-with-`parsed` /
    #: 422-`schema_validation_failed` split.
    response_format: Optional[Dict[str, Any]] = None

    @field_validator("input")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("input must be non-empty")
        return v


def _clamp_timeout(raw: int) -> int:
    return max(_MIN_TIMEOUT_S, min(_MAX_TIMEOUT_S, raw))


class AgentRuntimePrincipal:
    """Resolved (user, agent) pair for one `/api/v1/agents/{slug}/...` call."""

    def __init__(self, user: dict, agent: dict) -> None:
        self.user = user
        self.agent = agent


def require_agent_runtime_principal(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> AgentRuntimePrincipal:
    """Auth dependency for every `/api/v1/agents/{slug}/...` runtime route.

    Any restricted principal is hard-denied — this surface needs a real
    (interactive-session or user-PAT) credential naming a single caller. A
    `SessionPrincipal` carries no single identity to resolve an agent
    against; an `AgentPrincipal` is the sandbox's own narrowed credential,
    which must never be able to drive the caller-facing agent API (nor
    read `user["id"]` off a frozen dataclass).

    The agent is resolved OWNED-OR-RUNNABLE (C2.3, shared-agent runtime):
    `agents_repo().get_runnable_by_slug` returns it either when `slug` is
    this caller's own slug, or — for an agent shared to them via a
    `ResourceType.AGENT` grant — when `slug` is the agent's id (slugs are
    only unique per-owner, so a grantee cannot name someone else's agent by
    a borrowed slug string; see that method's docstring). 404 either way a
    non-owner/non-grantee gets today — existence of another user's agent is
    never leaked.
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=403, detail={"code": "agent_runtime_requires_owner_credential"})

    agent = agents_repo().get_runnable_by_slug(user["id"], slug)
    if agent is None or agent.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found"})

    pat_agent_id = agent_id_from_request(request)
    if pat_agent_id is not None and pat_agent_id != agent["id"]:
        raise HTTPException(status_code=403, detail={"code": "agent_pat_wrong_agent"})

    # Same check `app/api/chat.py::require_chat_access` gates the chat WS
    # route with — copying the call (not the mechanism), per the task
    # brief, since this dependency already needs its own composite
    # 404/403 ordering that `require_resource_access` doesn't expose.
    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(status_code=403, detail={"code": "chat_access_denied"})

    return AgentRuntimePrincipal(user=user, agent=agent)


def require_agent_usage_principal(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> AgentRuntimePrincipal:
    """Auth dependency for `GET /agents/{slug}/usage` ONLY (C2.4).

    Same owned-or-runnable chain as `require_agent_runtime_principal`,
    PLUS an admin READ fallback: an admin with no run grant on the agent
    may still view its usage totals. Deliberately NOT folded into
    `require_agent_runtime_principal` itself — that dependency also gates
    `/responses` (which actually RUNS the agent), and
    `agents_repo().get_runnable_by_slug` withholds admin god-mode there on
    purpose (its own docstring: "never an implicit run-any-agent grant").
    Viewing usage is a read, not a run, so it does not reopen that
    exclusion — the per-caller BREAKDOWN this principal unlocks is gated
    separately, in `get_agent_usage` itself, to owner-or-admin only (a
    plain runnable grantee still sees only the agent-level totals).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=403, detail={"code": "agent_runtime_requires_owner_credential"})

    agent = agents_repo().get_runnable_by_slug(user["id"], slug)
    if agent is None and is_user_admin(user["id"], conn):
        # Admin inspection fallback, by id only -- `slug` is only unique
        # within its OWNER's namespace (see `get_runnable_by_slug`'s
        # docstring), so an admin addressing a foreign agent by its bare
        # slug name would be guessing across owners; `get_by_id` is the
        # same unambiguous lookup the runnable path already uses for a
        # grantee.
        agent = agents_repo().get_by_id(slug)
    if agent is None or agent.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found"})

    pat_agent_id = agent_id_from_request(request)
    if pat_agent_id is not None and pat_agent_id != agent["id"]:
        raise HTTPException(status_code=403, detail={"code": "agent_pat_wrong_agent"})

    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(status_code=403, detail={"code": "chat_access_denied"})

    return AgentRuntimePrincipal(user=user, agent=agent)


def _job_payload_owner_id(job: dict) -> Optional[str]:
    return (job.get("payload_json") or {}).get("owner_user_id")


def _job_payload_agent_id(job: dict) -> Optional[str]:
    return (job.get("payload_json") or {}).get("agent_id")


def _serialize_error(raw_error: Optional[str]) -> Any:
    """`jobs.error` is a plain string for every kind (see
    `src/repositories/jobs.py::fail`) — for `agent_response` specifically, a
    `ConcurrencyCapHit` re-raised from `app/worker/kinds.py::_run_agent_response`
    carries a recognizable `_CONCURRENCY_CAP_ERROR_PREFIX`. Strip it back off
    into a structured `{"code": "concurrency_cap", "message": ...}` so a
    caller can branch on `error.code` instead of string-matching; any other
    error string is returned unchanged (plain string), preserving the
    existing wire shape for every other failure mode.

    A schema-validation failure on a background/degraded run (C13) carries
    `_SCHEMA_VALIDATION_ERROR_PREFIX` followed by a JSON object (`code`,
    `message`, `session_id`, `usage`, `raw_answer` — see
    `app/worker/kinds.py::_run_agent_response`'s own validation branch) —
    parsed back out the same way. A malformed/truncated JSON body (should be
    unreachable — it's always this module's own `json.dumps` on the way in)
    falls back to the raw string rather than raising."""
    if raw_error is not None and raw_error.startswith(_CONCURRENCY_CAP_ERROR_PREFIX):
        return {"code": "concurrency_cap", "message": raw_error[len(_CONCURRENCY_CAP_ERROR_PREFIX) :]}
    if raw_error is not None and raw_error.startswith(_SCHEMA_VALIDATION_ERROR_PREFIX):
        try:
            return json.loads(raw_error[len(_SCHEMA_VALIDATION_ERROR_PREFIX) :])
        except (ValueError, TypeError):
            return raw_error
    return raw_error


def _serialize_job(job: dict) -> dict:
    payload = job.get("payload_json") or {}
    result = payload.get("result")

    def _iso(v: Any) -> Optional[str]:
        return v.isoformat() if v is not None else None

    return {
        "id": job["id"],
        "status": _PUBLIC_STATUS_MAP.get(job["status"], job["status"]),
        "result": result,
        "error": _serialize_error(job.get("error")),
        "created_at": _iso(job.get("created_at")),
        "finished_at": _iso(job.get("finished_at")),
    }


@router.post("/agents/{slug}/responses")
async def create_agent_response(
    slug: str,
    body: AgentResponseRequest,
    request: Request,
    response: Response,
    principal: AgentRuntimePrincipal = Depends(require_agent_runtime_principal),
) -> dict:
    """Audit wrapper around `_create_agent_response_impl` (F2b — audit-
    full-coverage plan, Task 4): writes one `agent.invoke` row right after
    auth/scope resolution (`require_agent_runtime_principal` above already
    ran) and BEFORE the actual run/enqueue starts — so a crash inside
    `run_one_shot`/the background enqueue still leaves a row behind. A
    second row, with `result="error:<class>"`, is written only when the
    call fails (mirrors the `query.remote` error-path precedent,
    `app/api/query.py`). `client_ip`/`correlation_id`/`client_kind` are
    left to autofill (`src.audit_context`) — this is a plain HTTP handler.
    """
    user = principal.user
    mode = "job" if body.background else "sync"
    log_safe(
        user_id=user["id"],
        action="agent.invoke",
        resource=f"agent:{slug}",
        params={"session": None, "mode": mode},
    )
    try:
        return await _create_agent_response_impl(slug, body, request, response, principal)
    except Exception as exc:
        log_safe(
            user_id=user["id"],
            action="agent.invoke",
            resource=f"agent:{slug}",
            params={"mode": mode},
            result=f"error:{type(exc).__name__}",
        )
        raise


async def _create_agent_response_impl(
    slug: str,
    body: AgentResponseRequest,
    request: Request,
    response: Response,
    principal: AgentRuntimePrincipal,
) -> dict:
    user, agent = principal.user, principal.agent
    # `app.middleware.request_id.RequestIdMiddleware` (mounted globally in
    # `app/main.py`) already assigns/propagates the per-request id into this
    # contextvar AND appends it as the `x-request-id` RESPONSE header on
    # every request — this router echoes the SAME value into the body
    # rather than minting (and separately header-setting) its own, which
    # would otherwise land as a second, comma-joined `x-request-id` value.
    request_id = request_id_var.get() or uuid.uuid4().hex

    raw_body = await request.body()
    request_hash = hashlib.sha256(raw_body).hexdigest()
    idem_key = request.headers.get("Idempotency-Key")

    if idem_key:
        existing = idempotency_repo().get(idem_key, user["id"], agent["id"])
        if existing is not None:
            _raise_for_existing_idempotency_row(existing, request_hash)
            response.status_code = existing["status_code"]
            # The replayed body deliberately keeps the ORIGINAL call's
            # `request_id` (byte-identical replay contract) — it will differ
            # from the current call's `x-request-id` header, which the
            # middleware always stamps fresh per request.
            return json.loads(existing["response_body"])

        # Reserve the key BEFORE doing any of the actual work below — closes
        # the double-execution race where two concurrent requests under the
        # same key both miss the `get()` above. A conflict here means
        # another request won the race (or left a row `get()` itself would
        # have already caught, but the timing landed between our `get()` and
        # this `reserve()`); re-fetch to decide which 409 applies.
        reserved = idempotency_repo().reserve(idem_key, user["id"], agent["id"], request_hash)
        if not reserved:
            conflict = idempotency_repo().get(idem_key, user["id"], agent["id"])
            if conflict is not None:
                _raise_for_existing_idempotency_row(conflict, request_hash)
            # `conflict` is `None` here only in a vanishingly unlikely timing
            # window (the conflicting row expired/was cleared between our
            # failed `reserve()` and this re-fetch) — ask the caller to
            # retry rather than silently double-executing.
            raise HTTPException(status_code=409, detail={"code": "idempotency_key_in_flight"})

    timeout_s = _clamp_timeout(body.timeout_s)
    metadata = body.metadata or {}
    response_format = body.response_format

    # Prompt-steering (C12): when a schema is requested, append the
    # directive to the INPUT actually sent to the model — there is no
    # `send_user_message(response_format=...)` param on the chat runtime.
    # `response_format` itself still rides along in job payloads below so
    # the worker can validate the answer once the run completes; only the
    # prompt TEXT needs the directive appended, not the schema dict itself.
    effective_input = body.input
    if response_format is not None:
        effective_input = f"{body.input}\n\n{schema_directive(response_format)}"

    # NOTE: a reservation made above is deliberately left in place if
    # anything below raises (including a `ConcurrencyCapHit`-derived 429) —
    # it self-clears once `RESERVATION_TTL_S` elapses (see
    # `IdempotencyRepository.reserve`'s docstring), so a genuine retry under
    # the same key is never permanently blocked, just possibly rate-limited
    # to that window.
    if body.background:
        job = jobs_repo().enqueue(
            JOB_KIND,
            {
                "mode": "fresh",
                "owner_user_id": user["id"],
                "owner_email": user["email"],
                "agent_id": agent["id"],
                "prompt": effective_input,
                "metadata": metadata,
                "response_format": response_format,
            },
        )
        status_code = 202
        result_body: dict = {"job_id": job["id"]}
    else:
        manager = get_current_chat_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "chat_disabled"})

        try:
            run_result = await run_one_shot(
                manager,
                user_email=user["email"],
                agent_id=agent["id"],
                prompt=effective_input,
                timeout_s=timeout_s,
                owner_user_id=user["id"],
            )
        except ConcurrencyCapHit as exc:
            # Mirrors `app/api/chat.py::create_session`'s 429 for the same
            # underlying condition — this router's own `detail.code`
            # envelope shape (every other error here uses `code`, not
            # `chat.py`'s `kind`).
            raise HTTPException(status_code=429, detail={"code": "concurrency_cap", "hint": str(exc)}) from exc

        if run_result["timed_out"] and not run_result["answer"]:
            # Genuine timeout with nothing usable collected yet. The turn
            # keeps running server-side — this only bounds the WAIT.
            # Degrade to a background job that resumes waiting on the SAME
            # chat_id (no re-send of the prompt).
            job = jobs_repo().enqueue(
                JOB_KIND,
                {
                    "mode": "continue",
                    "owner_user_id": user["id"],
                    "owner_email": user["email"],
                    "agent_id": agent["id"],
                    "chat_id": run_result["chat_id"],
                    "metadata": metadata,
                    "response_format": response_format,
                },
            )
            status_code = 202
            result_body = {"job_id": job["id"]}
        else:
            # Either the turn genuinely completed, OR the wait timed out but
            # the sink had already collected an answer beforehand (e.g. the
            # "done" frame landed in the small window right after the last
            # assistant_message frame but before asyncio.wait_for's timeout
            # fired) — serve it now rather than degrading to a background
            # job for an answer already in hand.
            usage_accumulator_flush()
            usage = usage_for_session(agent["id"], run_result["chat_id"])

            # C13: a schema_validation_failed 422 must not orphan a paid run
            # — the run already spent tokens and has a session_id/usage/raw
            # answer in hand. Build a structured 422 body (not an
            # HTTPException) so it flows through the SAME idempotency-store
            # path below as every other terminal response: a retry under the
            # same Idempotency-Key replays this 422 verbatim instead of
            # re-running `run_one_shot`.
            ok, parsed, validation_error = validate(run_result["answer"], response_format)
            if response_format is not None and not ok:
                status_code = 422
                result_body = {
                    "code": "schema_validation_failed",
                    "message": validation_error,
                    "session_id": run_result["chat_id"],
                    "usage": usage,
                    "raw_answer": run_result["answer"],
                }
            else:
                result_body = {
                    "answer": run_result["answer"],
                    "session_id": run_result["chat_id"],
                    "response_id": uuid.uuid4().hex,
                    "usage": usage,
                    "agent_config_hash": agent_config_hash(agent),
                    "request_id": request_id,
                }
                if response_format is not None:
                    result_body["parsed"] = parsed
                status_code = 200

    response.status_code = status_code

    if idem_key:
        idempotency_repo().fulfill(
            idem_key,
            user["id"],
            agent["id"],
            request_hash,
            json.dumps(result_body),
            status_code,
            ttl_s=_idempotency_ttl_s(),
        )

    return result_body


def _raise_for_existing_idempotency_row(row: dict, request_hash: str) -> None:
    """Shared 409 decision for a live idempotency-key row that isn't a
    fulfilled replay hit yet: an in-flight reservation
    (`response_body IS NULL`, still within `RESERVATION_TTL_S`) is
    `idempotency_key_in_flight`; ANY row (reservation or fulfilled) whose
    `request_hash` doesn't match the incoming request is
    `idempotency_key_reuse`. A fulfilled row with a matching hash is left
    for the caller to replay — this only raises, it never returns a value."""
    if row["request_hash"] != request_hash:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reuse"})
    if row.get("response_body") is None:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_in_flight"})


def usage_accumulator_flush() -> None:
    """Flush the broker's batched `llm_usage` ledger before summing this
    session's usage — a just-finished turn's rows may still be sitting in
    the in-memory buffer otherwise. A thin, mockable indirection over
    `app.api.broker_agent_policy.usage_accumulator.flush()` (deferred
    import: this router must not carry an import-time dependency on the
    broker module)."""
    try:
        from app.api.broker_agent_policy import usage_accumulator

        usage_accumulator.flush()
    except Exception:
        logger.exception("usage_accumulator.flush() failed — usage totals may undercount this response")


#: `?period=YYYY-MM` query param format — same shape `llm_usage_repo()`'s
#: `strftime(created_at, '%Y-%m')` / `to_char(created_at, 'YYYY-MM')`
#: comparisons expect on both backends.
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _current_year_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@router.get("/agents/{slug}/usage")
async def get_agent_usage(
    slug: str,
    period: Optional[str] = None,
    principal: AgentRuntimePrincipal = Depends(require_agent_usage_principal),
) -> dict:
    """Per-agent monthly token usage against its budget (Task 8, `agnes
    agent usage` / MCP `agent_usage`), plus a per-caller breakdown for the
    agent's owner or an admin (C2.4, per-caller usage attribution).

    `period` defaults to the current UTC month (`YYYY-MM`); an explicitly
    passed value that doesn't match that shape is `400
    {"code": "invalid_period"}`. Auth is `require_agent_usage_principal`:
    owner-or-runnable-grantee (same chain every other
    `/api/v1/agents/{slug}/...` runtime route uses) PLUS an admin
    inspection fallback that the other routes deliberately do NOT have (see
    that dependency's docstring).

    The usage-shaped fields (`input_tokens`/`output_tokens`/
    `cache_read_tokens`/`cache_creation_tokens`) mirror Anthropic's own
    usage object. `total_tokens` is `input + output + cache_creation`
    (EXCLUDING `cache_read_tokens`, which is informational only) — the
    exact quantity `app.api.broker_agent_policy.check_budget` compares
    against `token_budget_monthly`, so `budget_remaining` (`budget_limit -
    total_tokens`, floored at 0, `None` for an unbounded agent) lines up
    with when a call against this agent would actually start 429ing with
    `budget_exhausted`. These AGGREGATE fields are visible to anyone this
    principal admits (owner, runnable grantee, or admin) — they say nothing
    about who spent the tokens, only how much the AGENT (budget's own unit)
    has spent.

    `by_caller` is the new field: a list of `{caller_user_id, input_tokens,
    output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens}`
    — one entry per distinct caller who ran this agent that month
    (`caller_user_id` is `None` for a row with no recorded caller: pre-C2.4
    rows, or the DuckDB backend, which has no column to distinguish callers
    at all — see `LlmUsageRepository.usage_breakdown_by_caller_for_month`).
    Present ONLY for the agent's owner or an admin — a plain runnable
    grantee (shared the agent, C2.3, but not its owner) gets `by_caller:
    null`: they may see how much the agent as a whole has spent, never a
    breakdown that would reveal OTHER callers' usage.

    Flushes the broker's batched usage ledger first (best-effort — see
    `usage_accumulator_flush`) so a just-finished call's rows aren't
    missing from the sum, same as the `/responses` sync path does before
    building its own `usage` field.
    """
    agent = principal.agent

    year_month = period if period is not None else _current_year_month()
    if not _PERIOD_RE.match(year_month):
        raise HTTPException(status_code=400, detail={"code": "invalid_period", "message": "period must be YYYY-MM"})

    usage_accumulator_flush()
    breakdown = llm_usage_repo().usage_breakdown_for_month(agent["id"], year_month)

    budget_limit = agent.get("token_budget_monthly")
    budget_remaining = max(0, budget_limit - breakdown["total_tokens"]) if budget_limit is not None else None

    is_owner_or_admin = principal.user["id"] == agent.get("owner_user_id") or is_user_admin(principal.user["id"])
    by_caller = (
        llm_usage_repo().usage_breakdown_by_caller_for_month(agent["id"], year_month) if is_owner_or_admin else None
    )

    return {
        "period": year_month,
        "agent_slug": slug,
        "input_tokens": breakdown["input_tokens"],
        "output_tokens": breakdown["output_tokens"],
        "cache_read_tokens": breakdown["cache_read_tokens"],
        "cache_creation_tokens": breakdown["cache_creation_tokens"],
        "total_tokens": breakdown["total_tokens"],
        "budget_limit": budget_limit,
        "budget_remaining": budget_remaining,
        "by_caller": by_caller,
    }


@router.get("/jobs/{job_id}")
async def get_agent_job(
    job_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """Owner-scoped job read. 404 (not 403) when the job exists but belongs
    to someone else — existence is not leaked to a non-owner, matching the
    posture `app/api/agents_admin.py` documents for `{id}` routes.

    Defense-in-depth (mirrors `require_agent_runtime_principal`'s binding
    on the responses endpoint): when the presented credential is an agent
    PAT, ALSO 404 unless the job's own payload `agent_id` matches the PAT's
    `agent_id` claim — an owner's agent-A PAT must not be able to read a job
    that was created against agent B, even though both jobs belong to the
    same owner user.
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})

    job = jobs_repo().get(job_id)
    if job is None or _job_payload_owner_id(job) != user.get("id"):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})

    pat_agent_id = agent_id_from_request(request)
    if pat_agent_id is not None and pat_agent_id != _job_payload_agent_id(job):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})

    return _serialize_job(job)
