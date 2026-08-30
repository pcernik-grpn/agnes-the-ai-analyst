"""Admin observability for chat sessions."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.audit_helpers import log_safe
from src.repositories import audit_repo
from app.chat.readiness import (
    ENV_ANTHROPIC,
    get_llm_runtime_diagnostic,
    secret_status,
    test_anthropic_key,
    test_docker_sandbox,
    test_vertex_credentials,
    test_wif_credentials,
)
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/chat", tags=["admin-chat"])


# WS auth tickets for the admin tail route ride the coordination backend
# (single-use KV with TTL) — same mechanism as the chat-WS ticket pattern in
# `app/api/chat.py`: a short-TTL one-shot token gates the WebSocket open.
# Without this, the tail route streamed any session's run.log to any
# anonymous WS caller — a confidentiality bypass. Riding the coordination
# backend (rather than a module-level dict) makes tickets visible across
# replicas when `coordination.backend=redis` is configured.
_ADMIN_TICKET_TTL_SEC = 60
_ADMIN_TICKET_KEY_PREFIX = "admin-tail-ticket:"


def _issue_admin_ticket(user_id: str) -> str:
    ticket = secrets.token_urlsafe(32)
    coordination().kv_set(f"{_ADMIN_TICKET_KEY_PREFIX}{ticket}", user_id, ttl_s=_ADMIN_TICKET_TTL_SEC)
    return ticket


def _consume_admin_ticket(ticket: str) -> Optional[str]:
    return coordination().kv_delete(f"{_ADMIN_TICKET_KEY_PREFIX}{ticket}")


@router.get("")
async def list_active(request: Request, admin: dict = Depends(require_admin)):
    """List active chat sessions (or render the dashboard shell).

    Content-negotiated: browsers (``Accept: text/html``) get the
    ``admin_chat.html`` shell which then re-fetches this endpoint with
    ``Accept: application/json`` to populate the table.  Programmatic
    callers and the dashboard JS get the JSON payload directly.

    Single-endpoint design (per Task B.3 + architect finding #8) — the
    dashboard URL must match the JSON URL so admins typing /admin/chat
    in the address bar see something, not a 404.
    """
    # Content-negotiated route. Browsers (Accept: text/html) get the admin_chat.html
    # template; XHR / tooling get JSON {"sessions": [...]}. If you add a new
    # /admin/chat/{subpath} route, mirror this pattern: don't add a separate HTML
    # route in app/web/router.py — it would never match because this prefix wins.
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        from app.web.router import templates as _templates, _build_context as _build_ctx

        ctx = _build_ctx(request, user=admin)
        return _templates.TemplateResponse(request, "admin_chat.html", ctx)
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        return {"sessions": [], "warning": "chat_disabled"}
    sessions = []
    for live in mgr.list_live():
        sessions.append(
            {
                "id": live.chat_id,
                "user_email": live.user_email,
                "state": live.state.value,
                "pid": live.handle.pid if live.handle else None,
                "started_at": live.started_at.isoformat(),
                "last_activity": live.last_activity.isoformat(),
                "crash_count": live.crash_count,
            }
        )
    return {"sessions": sessions}


# --------------------------------------------------------------------------
# Chat readiness — secret presence + live key validation
# --------------------------------------------------------------------------
# Chat needs ANTHROPIC_API_KEY in the server env, plus a real
# JWT_SECRET_KEY (and per-provider backing: KAI_HOST_JWT_SECRET for
# kai-agent, APPS_RUNNER_TOKEN for docker). When any is missing the startup
# gates leave chat_manager=None and chat 503s. These admin-only endpoints
# surface that state (presence, never the value), let an admin set the keys
# from the UI (persisted to the env-overlay), and live-test that the keys
# actually work — so a present-but-invalid key is caught here, not at the
# first user's sandbox spawn.


class ChatSecretsBody(BaseModel):
    anthropic_api_key: Optional[str] = None


@router.get("/readiness")
async def chat_readiness(request: Request, _admin: dict = Depends(require_admin)):
    """Presence-only readiness snapshot (no secret values leak).

    ``llm_runtime`` carries the last classified LLM-credential failure the chat
    broker hit at runtime (``{reason, detail, status_code, at}``) or ``None``
    when healthy — so the admin banner distinguishes an invalid/expired key, an
    unfunded account, and a provider outage instead of guessing from an opaque
    chat error (#884)."""
    status = secret_status(getattr(request.app.state, "chat_config", None))
    status["llm_runtime"] = get_llm_runtime_diagnostic(request.app.state)
    return status


@router.post("/secrets")
async def set_chat_secrets(
    body: ChatSecretsBody,
    request: Request,
    admin: dict = Depends(require_admin),
    conn=Depends(_get_db),
):
    """Persist chat provider secrets to the env-overlay (survives restart).

    Only non-empty values are written; omitted / blank fields leave the
    existing secret untouched (so the UI can save one key without clobbering
    the other). Setting a key updates ``os.environ`` immediately, but the
    startup gates already decided whether to build ``ChatManager`` — so a
    restart is required to actually turn chat on. Audited without the value.
    """
    from app.secrets import persist_overlay_token

    changed: list[str] = []
    if body.anthropic_api_key and body.anthropic_api_key.strip():
        persist_overlay_token(ENV_ANTHROPIC, body.anthropic_api_key.strip())
        changed.append("anthropic_api_key")
    if not changed:
        raise HTTPException(422, detail="no secret provided")

    try:
        audit_repo().log(
            user_id=admin.get("id"),
            action="chat.secrets.update",
            resource="chat",
            params={"changed": changed},  # names only — never the values
            result="success",
        )
    except Exception:
        logger.exception("failed to audit chat.secrets.update")

    return {
        "changed": changed,
        "restart_required": True,
        "status": secret_status(getattr(request.app.state, "chat_config", None)),
    }


@router.post("/secrets/test")
async def test_chat_secrets(request: Request, _admin: dict = Depends(require_admin)):
    """Live-probe the currently-configured credentials. Per-key ``{ok, detail}``.

    The ``anthropic_api_key`` slot probes whatever the LLM provider/auth mode
    actually uses: the static key in ``api_key`` mode, the workload-identity
    federation (mint a token + confirm the API accepts it) in
    ``workload_identity`` mode, or Google ADC + a Vertex completion when
    ``chat.llm.provider`` is ``vertex`` — so the admin "test connection"
    surface works in every mode. The slot name stays ``anthropic_api_key``
    for UI compatibility; the detail string identifies which probe ran.

    The sandbox slot is provider-aware: ``docker_sandbox`` (sidecar + daemon +
    image, via the apps-runner probe) on a docker deployment; a kai-agent
    deployment gets only the anthropic probe — the engine owns its own sandbox
    backing, and a red row for a credential the instance will never use is
    noise, not a signal.
    """
    chat_config = getattr(request.app.state, "chat_config", None)
    llm_auth = getattr(chat_config, "llm_auth", "api_key")
    llm_provider = getattr(chat_config, "llm_provider", "anthropic")
    if llm_provider == "vertex":
        anthropic_probe = await test_vertex_credentials(
            getattr(chat_config, "vertex_project_id", ""),
            getattr(chat_config, "vertex_region", ""),
        )
    elif llm_auth == "workload_identity":
        anthropic_probe = await test_wif_credentials()
    else:
        anthropic_probe = await test_anthropic_key()
    result: dict = {"anthropic_api_key": anthropic_probe}
    if getattr(chat_config, "provider", "kai-agent") == "docker":
        result["docker_sandbox"] = await test_docker_sandbox(getattr(chat_config, "docker_image", ""))
    return result


@router.delete("/{chat_id}", status_code=204)
async def admin_kill(chat_id: str, request: Request, _admin: dict = Depends(require_admin)):
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(503, detail="chat_disabled")
    await mgr.kill(chat_id, reason="admin_kill")


@router.get("/{chat_id}/debug")
async def admin_debug(
    chat_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Admin-only introspection of per-session in-process counters.

    Used by the E2E suite (notably ``tests/e2e/test_bq_budget.py``) to
    read counters that previously had to be poked via ``docker exec
    python -c ...`` against module globals. Under a remote-sandbox
    provider there is no ``docker exec`` into the runner, so the test
    reads from this endpoint
    instead. The shape is intentionally narrow: just the counters the
    suite needs to assert on.
    """
    # bq_bytes — process-local accumulator inside app/api/query.py.
    try:
        from app.api.query import _per_session_bq_bytes

        bq_bytes = int(_per_session_bq_bytes.get(chat_id, 0))
    except Exception:
        bq_bytes = 0
    # session_state — live-manager view, if attached
    mgr = getattr(request.app.state, "chat_manager", None)
    live = None
    if mgr is not None:
        live = next(
            (s for s in mgr.list_live() if s.chat_id == chat_id),
            None,
        )
    return {
        "chat_id": chat_id,
        "bq_bytes": bq_bytes,
        "live": (
            {
                "state": live.state.value,
                "crash_count": live.crash_count,
                "started_at": live.started_at.isoformat(),
                "last_activity": live.last_activity.isoformat(),
            }
            if live is not None
            else None
        ),
    }


@router.post("/{chat_id}/tail-ticket")
async def tail_ticket(
    chat_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Issue a short-TTL one-shot ticket for the tail WebSocket.

    The WebSocket itself can't carry the admin's session cookie/Authorization
    reliably across browsers (Safari in particular strips cookies on WS
    upgrades from `fetch`), so we mint a ticket here under the normal admin
    auth flow and the JS hands it to the WS as a query parameter.

    POST, not GET, for two reasons that are the same reason: minting a
    credential is a state change, and the brokered admin-READ surface
    (``app/api/broker.py``) replays every ``GET``/``HEAD`` admin route under
    the caller's resolved identity precisely BECAUSE "never mutate on GET" is
    supposed to hold. As a GET this route was the counterexample — a chat
    sandbox could ask the broker for it and get back a live ticket for
    ``/admin/chat/{any_chat_id}/tail``, i.e. read another user's live session.
    The sandbox runs an agent that any document it reads can prompt-inject, so
    that is a real path, not a theoretical one. Guarded by
    ``tests/test_broker_routes.py::test_no_admin_get_route_mints_a_credential``.
    """
    # Verify the session exists so 404 surfaces here rather than mid-WS.
    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="chat_disabled")
    if repo.get_session(chat_id) is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    ticket = _issue_admin_ticket(admin["id"])
    return {
        "ticket": ticket,
        "ws_url": f"/admin/chat/{chat_id}/tail?ticket={ticket}",
    }


@router.websocket("/{chat_id}/tail")
async def admin_tail(ws: WebSocket, chat_id: str, ticket: str = ""):
    # Ticket auth BEFORE accept() — invalid callers get close(4401) without
    # ever seeing protocol-upgrade success. A coordination-backend blip
    # (e.g. Redis unreachable) surfaces as 4503 rather than propagating an
    # uncaught exception out of the WS handler.
    try:
        user_id = _consume_admin_ticket(ticket)
    except CoordinationUnavailable:
        await ws.close(code=4503, reason="coordination_unavailable")
        return
    if user_id is None:
        log_safe(
            user_id=None,
            action="chat.session.tail_rejected",
            resource=f"chat_session:{chat_id}",
            params={"reason": "invalid_or_expired_ticket"},
            result="denied",
            client_kind="web",
        )
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    repo = getattr(ws.app.state, "chat_repo", None)
    if repo is None:
        await ws.close(code=4404)
        return
    s = repo.get_session(chat_id)
    if s is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    # The admin is now streaming ANOTHER user's live chat log. The ticket
    # issuance (a separate POST) records that permission was granted; only
    # this row records that the content was actually watched, which is the
    # event a "who read whose conversation" question is really asking about.
    log_safe(
        user_id=user_id,
        action="chat.session.tail_view",
        resource=f"chat_session:{chat_id}",
        params={"subject_email": s.user_email},
        result="success",
        client_kind="web",
    )
    chat_data_dir = getattr(ws.app.state, "chat_data_dir", None)
    if chat_data_dir is None:
        await ws.send_json({"type": "no_log", "reason": "chat_data_dir_not_configured"})
        await ws.close()
        return
    log_path = Path(chat_data_dir) / "users" / s.user_email / "sessions" / chat_id / "run.log"
    if not log_path.exists():
        await ws.send_json({"type": "no_log"})
        await ws.close()
        return
    with log_path.open("r") as f:
        f.seek(0, 2)  # tail: start from end
        try:
            while True:
                line = f.readline()
                if line:
                    await ws.send_json({"type": "line", "text": line.rstrip()})
                else:
                    await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
