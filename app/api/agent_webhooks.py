"""Outbound agent webhook registration — `/api/v1/agents/{slug}/webhooks`
(V1b Task 6, agent-api). Owner-scoped standing config — `require_session_token`
rejects every PAT flavor (plain PAT and agent PAT alike), same posture
`app/api/agents_admin.py` documents for agent management: registering a
webhook mutates a standing config, not a runtime call, so an agent PAT
issued to automate *calls to* the agent must not also be able to redirect
where *notifications about* it are delivered.

SSRF hardening: `POST` runs `app.chat.webhook_delivery.validate_and_resolve`
at CREATE time — `400 webhook_url_forbidden` denies a private/loopback/
metadata-resolving target up front — but that is a courtesy check, not the
actual guard: `webhook_delivery.deliver` re-runs the FULL resolve-and-pin on
every delivery attempt (see that module's docstring for why a create-time-only
check would leave a DNS-rebind window open).

Secret handling: the HMAC signing secret is returned ONLY in the `POST`
response body (`secret`), exactly once — like an agent PAT
(`app/api/agents_admin.py::create_agent_token`) — and never again; `GET`
omits it entirely. Unlike a PAT, the secret cannot be stored as a one-way
hash: HMAC signing needs the raw secret on every delivery, so the
`agent_webhooks.secret` column keeps it in the clear (server-side only,
never re-exposed over the API after creation).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.auth.access import require_agent_profiles_enabled
from app.auth.dependencies import _get_db, require_session_token
from app.chat.webhook_delivery import validate_and_resolve
from src.audit_helpers import log_safe
from src.repositories import agent_webhooks_repo, agents_repo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agent-webhooks"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

_VALID_EVENTS = ("job.completed", "job.failed")
_DEFAULT_EVENTS = list(_VALID_EVENTS)


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _load_agent_by_slug(slug: str, user: dict) -> Dict[str, Any]:
    """404 (never 403) for an unknown or not-owned slug — existence of
    another owner's agent is never leaked, matching
    `require_agent_runtime_principal` in `app/api/agent_runtime.py`."""
    agent = agents_repo().get_by_slug(user["id"], slug)
    if agent is None or agent.get("deleted_at") is not None:
        raise _err(404, "agent_not_found", "Agent not found")
    return agent


def _serialize(row: Dict[str, Any], *, include_secret: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "url": row["url"],
        "events": [e for e in (row.get("events") or "").split(",") if e],
        "active": bool(row.get("active", True)),
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "created_at": str(row["created_at"]) if row.get("created_at") is not None else None,
    }
    if include_secret:
        out["secret"] = row["secret"]  # shown exactly once, at creation
    return out


class CreateWebhookRequest(BaseModel):
    url: str
    events: Optional[List[str]] = None

    @field_validator("url")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must be non-empty")
        return v.strip()


@router.get("/{slug}/webhooks")
async def list_webhooks(
    slug: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    agent = _load_agent_by_slug(slug, user)
    rows = agent_webhooks_repo().list_for_agent(agent["id"])
    return {"data": [_serialize(r) for r in rows], "has_more": False, "next_cursor": None}


@router.post("/{slug}/webhooks", status_code=201)
async def create_webhook(
    slug: str,
    payload: CreateWebhookRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    agent = _load_agent_by_slug(slug, user)

    try:
        validate_and_resolve(payload.url)
    except ValueError as exc:
        raise _err(400, "webhook_url_forbidden", str(exc)) from exc

    events = payload.events if payload.events else _DEFAULT_EVENTS
    invalid = sorted(set(events) - set(_VALID_EVENTS))
    if invalid:
        raise _err(400, "invalid_event", f"unknown event(s) {invalid}; valid values are {list(_VALID_EVENTS)}")

    webhook_id = uuid.uuid4().hex
    # Raw secret — HMAC signing needs it on every delivery, so it can't be
    # stored as a one-way hash the way a PAT is (see module docstring).
    webhook_secret = secrets.token_hex(32)
    agent_webhooks_repo().create(
        id=webhook_id,
        agent_id=agent["id"],
        owner_user_id=user["id"],
        url=payload.url,
        secret=webhook_secret,
        events=",".join(events),
    )
    row = agent_webhooks_repo().get(webhook_id)
    assert row is not None  # just created above, same transaction/connection
    log_safe(
        user_id=user["id"],
        action="agent.webhook.create",
        resource=f"agent_webhook:{webhook_id}",
        params={"agent_id": agent["id"], "events": events},
    )
    return _serialize(row, include_secret=True)


@router.delete("/{slug}/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    slug: str,
    webhook_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> None:
    agent = _load_agent_by_slug(slug, user)
    row = agent_webhooks_repo().get(webhook_id)
    if row is None or row["agent_id"] != agent["id"]:
        raise _err(404, "webhook_not_found", "Webhook not found")
    agent_webhooks_repo().delete(webhook_id)
    log_safe(
        user_id=user["id"],
        action="agent.webhook.delete",
        resource=f"agent_webhook:{webhook_id}",
        params={"agent_id": agent["id"]},
    )
