"""
Jira webhook endpoints — FastAPI replacement for Flask Blueprint.

Receives Jira webhook notifications, verifies HMAC-SHA256 signatures,
and delegates processing to the Jira service.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import re

from app.auth.access import require_admin as _require_admin
from connectors.jira.service import Config, get_jira_service
from connectors.jira.validation import is_valid_issue_key, safe_join_under
from src.audit_helpers import log_safe

# webhookEvent is attacker-controlled; sanitize before using as a filename
# component. Real Jira webhookEvent values are like "jira:issue_updated" —
# alphanumeric + colon. We strip everything that isn't alphanumeric/underscore/dash
# (the colon → underscore mapping happens via sub). Dots are deliberately
# refused so `..` cannot survive sanitization as a directory component.
_WEBHOOK_EVENT_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["jira-webhooks"])

# Path for storing raw webhook events (debugging/audit)
WEBHOOK_LOG_DIR = Config.JIRA_DATA_DIR / "webhook_events"


def _verify_signature(payload: bytes, signature: str | None) -> bool:
    """Verify HMAC-SHA256 signature from Jira webhook.

    Fail-closed: callers must check ``Config.JIRA_WEBHOOK_SECRET`` is set
    before invoking. If it is not, this returns False (so a misconfigured
    deploy cannot accept unauthenticated webhooks). Issue #83.
    """
    secret = Config.JIRA_WEBHOOK_SECRET

    if not secret:
        return False

    if not signature:
        return False

    if signature.startswith("sha256="):
        signature = signature[7:]

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def _log_webhook_event(event_data: dict) -> None:
    """Log webhook event to file for debugging/audit.

    `webhookEvent` is attacker-controlled. Sanitize it through a strict
    whitelist before using as a filename component (issue #83) and apply
    `safe_join_under` to catch anything the regex misses.
    """
    try:
        WEBHOOK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        raw_event = event_data.get("webhookEvent", "unknown")
        if not isinstance(raw_event, str):
            raw_event = "unknown"
        # Replace any non-`[A-Za-z0-9_-]` run with a single `_` (dot
        # deliberately excluded — see _WEBHOOK_EVENT_SAFE_RE module
        # comment). Also clip to 64 chars to bound filename length on
        # hostile input.
        event_type = _WEBHOOK_EVENT_SAFE_RE.sub("_", raw_event)[:64] or "unknown"
        filename = f"{timestamp}_{event_type}.json"
        try:
            filepath = safe_join_under(WEBHOOK_LOG_DIR, filename)
        except ValueError as e:
            logger.warning(f"Refusing webhook log filename {filename!r}: {e}")
            return

        with open(filepath, "w") as f:
            json.dump(event_data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to log webhook event: {e}")


@router.post("/jira")
async def receive_jira_webhook(request: Request) -> Response:
    """Receive and process Jira webhook notifications."""
    # Refuse to process if the operator hasn't configured a webhook secret.
    # Returning 503 (not 401) signals "operator misconfiguration" rather
    # than "attacker guessed wrong". Issue #83.
    if not Config.JIRA_WEBHOOK_SECRET:
        logger.error("JIRA_WEBHOOK_SECRET not configured — refusing webhook")
        return JSONResponse(
            {"detail": "Webhook secret not configured"},
            status_code=503,
        )

    payload = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature")
    if not _verify_signature(payload, signature):
        logger.warning("Invalid webhook signature from %s", request.client.host if request.client else "unknown")
        log_safe(
            user_id=None,
            action="webhook.jira_rejected",
            resource="webhooks/jira",
            result="denied",
        )
        return JSONResponse({"detail": "Invalid signature"}, status_code=401)

    # Parse JSON
    if not payload:
        return JSONResponse({"detail": "Empty payload"}, status_code=400)

    try:
        event_data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        return JSONResponse({"detail": "Invalid JSON payload"}, status_code=400)

    if not event_data:
        return JSONResponse({"detail": "Empty payload"}, status_code=400)

    webhook_event = event_data.get("webhookEvent", "unknown")
    log_safe(
        user_id=None,
        action="webhook.jira_received",
        resource="webhooks/jira",
        params={"event": webhook_event},
    )
    # Defensive: some webhook senders pass `"issue": null` rather than
    # omitting the key. Normalise to {} so the next .get() doesn't
    # raise AttributeError on None.
    issue = event_data.get("issue") or {}
    issue_key = issue.get("key", "")
    # Some Jira webhook event types deliver the key at the top level
    # instead of `issue.key` (e.g. delete events historically).
    # `process_webhook_event` already supports this fallback at
    # connectors/jira/service.py — mirror it here so the handler
    # doesn't reject those events with 400 before they ever reach the
    # service layer.
    if not issue_key:
        issue_key = event_data.get("issue_key", "")

    # Validate issue_key format BEFORE any filesystem operation. Jira issue
    # keys follow `[A-Z][A-Z0-9]+-\d+`; anything else (path traversal,
    # SQL injection, control chars) is refused with 400. Issue #83.
    if not is_valid_issue_key(issue_key):
        logger.warning(
            "Webhook rejected: malformed issue key %r from %s",
            issue_key,
            request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {"detail": "Malformed or missing issue key"},
            status_code=400,
        )

    # Log event for debugging (after key validation so traversal attempts
    # don't end up named after attacker-supplied data).
    _log_webhook_event(event_data)

    logger.info(f"Received webhook: {webhook_event} for issue {issue_key}")

    jira_service = get_jira_service()

    if not jira_service.is_configured():
        logger.error("Jira service not configured, cannot process webhook")
        return JSONResponse(
            {"status": "error", "message": "Jira service not configured"},
            status_code=503,
        )

    # process_webhook_event's call chain is entirely synchronous (httpx,
    # file writes, parquet transform) and — since the 429 handling added for
    # comment pagination — can call time.sleep(). Running it directly here
    # would block the asyncio event loop for the whole app (chat, admin,
    # every other API), not just this request. Dispatch to the threadpool
    # instead (Devin Review on #1283).
    success = await run_in_threadpool(jira_service.process_webhook_event, event_data)

    if success:
        return JSONResponse({"status": "ok", "event": webhook_event, "issue": issue_key})
    else:
        return JSONResponse(
            {"status": "error", "message": "Failed to process event", "event": webhook_event, "issue": issue_key},
            status_code=500,
        )


@router.get("/jira/health")
async def jira_webhook_health(user: dict = Depends(_require_admin)) -> dict:
    """Health check for Jira webhook endpoint. Admin-only: exposes secret presence."""
    jira_service = get_jira_service()

    return {
        "status": "ok",
        "configured": jira_service.is_configured(),
        "webhook_secret_set": bool(Config.JIRA_WEBHOOK_SECRET),
    }
