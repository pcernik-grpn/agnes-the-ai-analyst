"""Desktop/browser notification WebSocket — the GATEWAY role's WS endpoint.

Wave-2F task 6: absorbs the standalone `services/ws_gateway` process (an
aiohttp server on 127.0.0.1:8765 with its own in-memory `connections` dict
and a Unix-socket HTTP dispatch endpoint) into the main FastAPI app. The
auth handshake (a JSON `{"type": "auth", "token": ...}` message signed with
``DESKTOP_JWT_SECRET``, HS256), the per-user connection cap, and the
ping/pong heartbeat are ported as-is from ``services/ws_gateway/{auth,
gateway}.py`` so the existing desktop client needs no protocol changes.

What changed: delivery. The old service kept ONE global, single-process
`connections: dict[user, list[ws]]` and producers POSTed to it over a Unix
socket — invisible across replicas and across containers without a shared
socket mount. Now:

- Each gateway process keeps its OWN local `_connections` registry (this
  module's module-level dict, used only to enforce the per-user connection
  cap) — correct because delivery no longer depends on a single global
  registry.
- Each connected socket subscribes to the coordination pub/sub channel
  ``notify:{user}`` for its own user on connect, and unsubscribes on
  disconnect (one subscription per socket, not shared across a user's
  multiple sockets — keeps each subscription's delivery self-contained to
  the one WebSocket + event loop it was created on).
- A producer anywhere calls ``publish_notification(user, payload)``
  (`app/notifications.py`). Every socket — on this process or any other
  gateway replica — currently subscribed to that user's channel receives
  it and delivers to itself.
- Memory-mode coordination backend: `publish()` invokes every subscribed
  handler synchronously in the same call stack — i.e. today's single-
  process behavior, byte-for-byte. Redis-backed coordination: the listener
  thread invokes handlers from a different thread than any given
  connection's event loop, so delivery is bounced onto that connection's
  own loop via `asyncio.run_coroutine_threadsafe` (also safe, if slightly
  redundant, in the memory-mode same-thread case).

Role gating: only processes with the GATEWAY role serve this route (same
"process participates or it doesn't" story as chat — see app/api/chat.py's
module docstring and app/roles.py). A non-gateway process closes the socket
with code 4503 before ever accepting the WS upgrade, mirroring the
CoordinationUnavailable 4503 pattern used elsewhere in the chat WS routes.

Per-replica connection cap: MAX_CONNECTIONS_PER_USER is enforced PER gateway
replica, not globally. If gateway scales horizontally (N replicas), one user
can have up to N × MAX_CONNECTIONS_PER_USER concurrent connections across
all replicas. This is acceptable for current single-gateway topologies;
revisit this design if the gateway scales.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Optional

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.coordination.factory import coordination
from app.notifications import notify_channel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_JWT_ALGORITHM = "HS256"

# Same env-var names + defaults as the standalone services/ws_gateway/config.py
# (WS_GATEWAY_HOST/PORT are dropped — this route rides the app's own host:port
# now, there is no separate listener to configure).
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "30"))
HEARTBEAT_TIMEOUT_MISSED = int(os.environ.get("HEARTBEAT_TIMEOUT_MISSED", "3"))
MAX_CONNECTIONS_PER_USER = int(os.environ.get("MAX_CONNECTIONS_PER_USER", "5"))

# Per-process, per-user connection registry — LOCAL to this gateway replica,
# used only to enforce MAX_CONNECTIONS_PER_USER (delivery rides the
# coordination pub/sub subscription each socket holds independently, see
# module docstring).
_connections: dict[str, list[WebSocket]] = defaultdict(list)


def _desktop_jwt_secret() -> Optional[str]:
    """Resolve the desktop-app JWT signing secret.

    Read directly from the environment. Returns ``None`` (not raise) when
    unset — desktop notifications are an optional feature; a missing secret
    means every connection attempt fails auth, not that the app fails to
    boot.

    NOTE: nothing in this repo currently *mints* tokens signed with this
    secret — the browser pairing flow that used to (``/desktop/link`` →
    deep link → ``/api/desktop/refresh``) was deleted with the legacy
    webapp and never ported. Until issue #412 settles how clients obtain a
    token (accept PAT-chain credentials here, a PAT→token exchange, or a
    revived pairing flow), the only issuer is an operator hand-signing one
    with the shared secret.
    """
    return os.environ.get("DESKTOP_JWT_SECRET") or None


def validate_desktop_token(token: str) -> Optional[dict]:
    """Validate a desktop-app JWT and return its payload, or ``None``.

    Ported from ``services/ws_gateway/auth.py::validate_token`` — same
    HS256 decode, same "must carry a `sub` claim" rule, same "expired or
    otherwise invalid -> None" behavior. Also returns ``None`` (rather than
    raising) when no secret is configured, so an unconfigured deployment
    fails closed instead of crashing the WS handler.
    """
    secret = _desktop_jwt_secret()
    if secret is None:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.warning("desktop JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("invalid desktop JWT token: %s", e)
        return None
    if "sub" not in payload:
        logger.warning("desktop JWT missing 'sub' claim")
        return None
    return payload


def _total_connections() -> int:
    return sum(len(ws_list) for ws_list in _connections.values())


def _register_connection(user: str, ws: WebSocket) -> None:
    _connections[user].append(ws)


def _remove_connection(user: str, ws: WebSocket) -> None:
    conns = _connections.get(user)
    if conns is None:
        return
    try:
        conns.remove(ws)
    except ValueError:
        pass
    if not conns:
        _connections.pop(user, None)


async def _deliver(ws: WebSocket, raw: str) -> None:
    """Forward one published notification to this connection's socket."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("dropped malformed notification payload")
        return
    try:
        await ws.send_json({"type": "notification", **payload})
    except Exception:
        logger.warning("failed to deliver notification")


def _audit_ws_connect(user_id: str) -> None:
    """One row per successful handshake (audit-coverage wave 2, Task 3) —
    this WS route had no audit trail at all before this task."""
    from src.audit_helpers import log_safe

    log_safe(user_id=user_id, action="notifications.ws_connect", resource=f"user:{user_id}", result="success")


def _audit_ws_rejected(reason: str, user_id: Optional[str] = None) -> None:
    """One row per refused handshake. ``user_id`` is only known once a
    (possibly invalid) token has already been decoded far enough to carry a
    ``sub`` claim — every earlier rejection (timeout, malformed JSON, wrong
    message shape, a token that fails validation) has none, and the row is
    still worth keeping (unattributed, ``result="denied"``)."""
    from src.audit_helpers import log_safe

    log_safe(
        user_id=user_id,
        action="notifications.ws_rejected",
        resource=f"user:{user_id}" if user_id else None,
        params={"reason": reason},
        result="denied",
    )


async def _heartbeat_loop(user: str, ws: WebSocket) -> None:
    """Send periodic pings and disconnect on missed pongs.

    Ported from ``services/ws_gateway/gateway.py::_heartbeat_loop``: this
    task is cancelled and restarted whenever a pong arrives (see the reader
    loop below) — connection cleanup itself happens in the caller's
    `finally` block, not here.
    """
    missed = 0
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                await ws.send_json({"type": "ping"})
                missed += 1
            except Exception:
                break
            if missed >= HEARTBEAT_TIMEOUT_MISSED:
                logger.warning("user %s missed %d heartbeats, disconnecting", user, missed)
                await ws.close()
                break
    except asyncio.CancelledError:
        pass


@router.websocket("/ws")
async def notifications_ws(ws: WebSocket) -> None:
    from app.roles import Role, role_enabled

    if not role_enabled(Role.GATEWAY):
        await ws.close(code=4503, reason="not_gateway_role")
        return

    await ws.accept()
    user: Optional[str] = None
    registered = False
    heartbeat_task: Optional[asyncio.Task] = None
    unsubscribe = None

    try:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        except asyncio.TimeoutError:
            _audit_ws_rejected("auth_timeout")
            await ws.send_json({"type": "auth_error", "message": "Auth timeout"})
            await ws.close()
            return

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _audit_ws_rejected("invalid_json")
            await ws.send_json({"type": "auth_error", "message": "Invalid JSON"})
            await ws.close()
            return

        if data.get("type") != "auth" or "token" not in data:
            _audit_ws_rejected("invalid_auth_message")
            await ws.send_json({"type": "auth_error", "message": "Expected auth message with token"})
            await ws.close()
            return

        payload = validate_desktop_token(data["token"])
        if payload is None:
            _audit_ws_rejected("invalid_token")
            await ws.send_json({"type": "auth_error", "message": "Invalid token"})
            await ws.close()
            return

        user = payload["sub"]

        if len(_connections.get(user, ())) >= MAX_CONNECTIONS_PER_USER:
            _audit_ws_rejected("too_many_connections", user_id=user)
            await ws.send_json({"type": "auth_error", "message": "Too many connections"})
            await ws.close()
            return

        _register_connection(user, ws)
        registered = True
        _audit_ws_connect(user)

        # Subscribe THIS socket to its own notify:{user} channel. The
        # handler may run on this coroutine's own event loop (memory
        # backend, direct call from publish()) or on the Redis listener
        # thread (redis backend) — run_coroutine_threadsafe is safe either
        # way, since it targets the loop captured right here.
        loop = asyncio.get_running_loop()

        def _handler(raw_message: str, _ws: WebSocket = ws, _loop: asyncio.AbstractEventLoop = loop) -> None:
            asyncio.run_coroutine_threadsafe(_deliver(_ws, raw_message), _loop)

        unsubscribe = coordination().subscribe(notify_channel(user), _handler)

        await ws.send_json({"type": "auth_ok", "username": user})
        logger.info("user %s connected (total: %d)", user, _total_connections())

        heartbeat_task = asyncio.create_task(_heartbeat_loop(user, ws))

        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if data.get("type") == "pong":
                heartbeat_task.cancel()
                heartbeat_task = asyncio.create_task(_heartbeat_loop(user, ws))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("error in notifications WS handler")
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        if unsubscribe is not None:
            unsubscribe()
        if registered:
            _remove_connection(user, ws)
            logger.info("user %s disconnected (total: %d)", user, _total_connections())
