"""Microsoft Graph change-notification receiver for SharePoint connections —
``POST /api/webhooks/sharepoint/{connection_id}``.

Agnes today only learns about a SharePoint change through the scheduled
sweep (``extraction.schedule`` -> ``POST /api/admin/sharepoint/extraction/
run-due``, see ``app/api/admin_sharepoint.py``). The external producer
already implements Graph drive subscriptions; its subscriptions module
deliberately leaves the HTTP listener to the deployment — THIS module is
that listener. Agnes never creates, renews, or deletes the Graph
subscription itself (the producer's ``subscriptions.py create --url
<receiver_url>`` does that, against the URL + secret ``POST /api/admin/
sharepoint/connections/{connection_id}/webhook`` mints); this module only
answers the two calls Graph makes against a subscription that already
exists.

Two request shapes, both ``POST`` to the same route (Graph's own contract):

- **Validation handshake** — a ``?validationToken=`` query param is present
  (sent when a subscription is created/renewed). Echo it back verbatim as
  ``text/plain`` within Graph's 10-second window; no side effects, no body
  parsing, no connection lookup.
- **Notification delivery** — a JSON body ``{"value": [{..., "clientState":
  "..."}]}``. Every element's ``clientState`` is verified in CONSTANT TIME
  (``hmac.compare_digest``) against this connection's own ``config.
  webhook_secret`` (minted by the admin endpoint above). A mismatch is
  dropped SILENTLY — the response is ``202`` regardless of whether the
  connection exists, the secret is configured, or any notification
  verified, so this public route is never an existence/secret oracle
  (security playbook §"filesystem paths"/"never an oracle" class of rule,
  applied here to connection existence rather than a path). On at least one
  verified notification, enqueues the SAME ``corpus-extraction`` job kind
  the manual trigger uses
  (``app/api/admin_sharepoint.py::_extraction_idempotency_key``), with
  ``run_after`` a short debounce in the future so a burst of Graph
  notifications for the same drive collapses onto ONE job — ``enqueue()``'s
  own idempotency dedup (a matching ``queued``/``running`` job for this
  connection) does the collapsing; a fresh ``run_after`` is only set on the
  very first job of a burst, never rewritten by a later dedup hit, which is
  exactly the debounce this route wants.

Feature-gated by ``extraction_webhook.enabled`` (default OFF) via
:func:`app.auth.access.require_extraction_webhook_enabled` — the whole
router 404s when off, same "surface doesn't exist" posture as
``app/api/facts.py``. This is deliberately the ONLY gate: Graph is the
caller, so there is no session/PAT to require, and admission control is the
per-notification ``clientState`` check above, not a ``Depends`` chain.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.auth.access import require_extraction_webhook_enabled
from src.audit_helpers import log_safe
from src.repositories import source_connections_repo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/webhooks",
    tags=["sharepoint-webhooks"],
    dependencies=[Depends(require_extraction_webhook_enabled)],
)

#: Real Graph delivery payloads are small JSON documents (a handful of
#: resourceData-less change notifications per delivery) — this is a hard
#: ceiling against a hostile/misbehaving caller sending an oversized body,
#: not a working-set estimate. Enforced by STREAMING the body (never
#: buffering past the cap first) — same discipline as
#: ``app/api/upload.py::_stream_to_temp``.
_MAX_WEBHOOK_BODY_SIZE = 1 * 1024 * 1024  # 1 MiB
_READ_CHUNK_SIZE = 64 * 1024

#: A burst of Graph notifications for the same drive (a save, a rename, a
#: batch of file drops) coalesces onto ONE ``corpus-extraction`` run rather
#: than one per notification — the idempotency-keyed dedup in
#: ``JobsRepository.enqueue`` collapses every call within this window onto
#: the first job it creates, so this is "wait this long after the FIRST
#: notification in a burst before actually running", not a per-call delay.
_WEBHOOK_DEBOUNCE_SECONDS = 60


async def _read_bounded_body(request: Request) -> bytes:
    """Stream the request body, aborting with ``413`` the moment the
    cumulative size exceeds :data:`_MAX_WEBHOOK_BODY_SIZE` — never buffers
    an oversized body in memory first."""
    total = 0
    chunks: List[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_WEBHOOK_BODY_SIZE:
            raise HTTPException(status_code=413, detail="webhook_payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _sharepoint_connection(connection_id: str) -> Dict[str, Any] | None:
    row = source_connections_repo().get(connection_id)
    if row is None or row.get("source_type") != "sharepoint":
        return None
    return row


def _verified_notifications(notifications: List[Any], secret: str) -> List[Dict[str, Any]]:
    """Every element of ``value`` whose ``clientState`` matches ``secret`` in
    constant time. Checks EVERY element (never short-circuits on the first
    match) so the walk's shape does not itself leak how many notifications
    verified."""
    verified = []
    for item in notifications:
        if not isinstance(item, dict):
            continue
        client_state = item.get("clientState")
        if not isinstance(client_state, str):
            continue
        # Compare as BYTES. `hmac.compare_digest` raises TypeError on a str
        # that is not ASCII-only, and `clientState` is fully caller-supplied
        # — so comparing the str directly turns any non-ASCII value into an
        # unhandled 500. That would defeat this module's whole "never an
        # oracle" property: only a request that reaches this line has a real
        # connection WITH a secret configured, so a 500-vs-202 split would
        # answer exactly the question every other path refuses to. Encoding
        # both sides keeps the comparison constant-time and total.
        if hmac.compare_digest(client_state.encode("utf-8"), secret.encode("utf-8")):
            verified.append(item)
    return verified


@router.post("/sharepoint/{connection_id}")
async def receive_sharepoint_webhook(connection_id: str, request: Request) -> Response:
    """Graph validation handshake + change-notification delivery — see the
    module docstring for the full contract. Always ``200``/``202``/``413``;
    never a status that reveals whether ``connection_id`` names a real
    SharePoint connection or a configured secret."""
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        # Handshake only — no body read, no connection lookup, no side
        # effects (Graph's own contract: echo verbatim within 10s).
        return PlainTextResponse(validation_token, status_code=200)

    body = await _read_bounded_body(request)

    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}

    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        notifications = []

    row = _sharepoint_connection(connection_id)
    secret = (row.get("config") or {}).get("webhook_secret") if row else None

    if row is not None and notifications and secret:
        verified = _verified_notifications(notifications, secret)
        if verified:
            _dispatch_extraction(connection_id, row, verified_count=len(verified))
            return JSONResponse({"status": "accepted"}, status_code=202)
        if notifications:
            # At least one notification arrived for a connection that DOES
            # have a secret configured, but none of them matched — a real
            # rejection worth a row, unlike the "nothing to check against"
            # paths below (unknown connection, no secret configured yet,
            # empty/malformed body), which stay silent — see module
            # docstring: never an oracle at the response layer, but the
            # audit log is admin-only and this route carries no user
            # identity, so log_safe(user_id=None, ...) is the entire audit
            # signal (mirrors app/api/jira_webhooks.py's
            # webhook.jira_rejected).
            log_safe(
                user_id=None,
                action="webhook.sharepoint_rejected",
                resource=f"source_connection:{connection_id}",
                params={"notification_count": len(notifications)},
                result="denied",
            )

    return JSONResponse({"status": "accepted"}, status_code=202)


def _dispatch_extraction(connection_id: str, row: Dict[str, Any], *, verified_count: int) -> None:
    """Enqueue the existing ``corpus-extraction`` job kind for this
    connection, with the same idempotency key + dispatch bookkeeping the
    manual admin trigger uses, plus a short debounce
    (:data:`_WEBHOOK_DEBOUNCE_SECONDS`) so a burst of notifications
    coalesces onto one run. Silently skips the enqueue when the feature
    isn't usable yet (``extraction.enabled`` off, or no producer
    configured) — same readiness gate the manual trigger checks BEFORE
    enqueueing, so a webhook burst never pollutes the queue with jobs
    doomed to fail (`app/worker/kinds.py::_run_corpus_extraction` would
    raise on the same conditions anyway)."""
    from app.api.admin_sharepoint import (
        _extraction_idempotency_key,
        _extraction_readiness,
        _record_extraction_dispatch,
    )

    usable, _reason = _extraction_readiness()
    if not usable:
        return

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue(
        "corpus-extraction",
        {"connection_id": connection_id},
        idempotency_key=_extraction_idempotency_key(connection_id),
        run_after=datetime.now(timezone.utc) + timedelta(seconds=_WEBHOOK_DEBOUNCE_SECONDS),
    )
    log_safe(
        user_id=None,
        action="webhook.sharepoint_received",
        resource=f"source_connection:{connection_id}",
        params={"notification_count": verified_count, "job_id": job["id"], "deduped": job["deduped"]},
    )
    if not job["deduped"]:
        _record_extraction_dispatch(row, job["id"])
    logger.info(
        "sharepoint webhook: connection %s -> corpus-extraction job %s (deduped=%s)",
        connection_id,
        job["id"],
        job["deduped"],
    )
