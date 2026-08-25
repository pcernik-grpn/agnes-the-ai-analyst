"""Slack Events webhook + identity binding endpoint."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from urllib.parse import parse_qs

from app.auth.dependencies import get_current_user
# events' and commands' _run_logged/_schedule are INCOMPATIBLE variants:
# events' _run_logged takes on_failure=(callback); commands' takes
# response_url= and itself posts the recovery ephemeral. The commands pair
# is aliased _cmd_* so the two can't be accidentally cross-used.
from services.slack_bot.commands import (
    _help_body,
    _run_logged as _cmd_run_logged,
    _schedule as _cmd_schedule,
    dispatch_command,
)
from services.slack_bot.events import _run_logged, _schedule, dispatch_event
from services.slack_bot.interactivity import dispatch_interaction, parse_interaction
from services.slack_bot.secrets import slack_secret
from services.slack_bot.sigverify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slack", tags=["slack"])


@router.post("/events")
async def slack_events(request: Request):
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = slack_secret("SLACK_SIGNING_SECRET") or ""
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    payload = await request.json()
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    if payload.get("type") == "event_callback":
        # Ack-then-async: schedule the (slow, sandbox-spawning) dispatch and
        # return the 200 immediately so Slack's 3s budget is never blown.
        # A failure inside the detached task is handled by _run_logged, not
        # by a Slack retry (we already acked). The DM handler emits its own
        # binding/error replies inline, so no top-level on_failure is needed
        # here (the recovery seam is used by later, context-bearing phases).
        _schedule(_run_logged(dispatch_event(request.app, payload["event"])))
        return {"ok": True}
    return {"ok": True}


@router.post("/commands")
async def slack_commands(request: Request):
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = slack_secret("SLACK_SIGNING_SECRET") or ""
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    command = (form.get("command") or "").strip()
    text = (form.get("text") or "").strip()
    # /agnes help answers synchronously in the 3 s ack — no session, no async.
    if command == "/agnes" and text in ("", "help"):
        return {"response_type": "ephemeral", "text": _help_body()}
    # Wrap the dispatch in _cmd_run_logged so an UNHANDLED handler exception
    # posts a best-effort ephemeral instead of vanishing (per-command errors
    # already reach response_url via the sink — this is the backstop).
    _cmd_schedule(_cmd_run_logged(dispatch_command(request.app, form),
                                  response_url=form.get("response_url")))
    return {"response_type": "ephemeral", "text": "_Working on it…_"}


@router.post("/interactivity")
async def slack_interactivity(request: Request):
    body = await request.body()                       # raw bytes — Slack signs these
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = slack_secret("SLACK_SIGNING_SECRET") or ""
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    raw_payload = form.get("payload")
    if not raw_payload:
        # Signed but malformed (no payload field) → graceful 400, not a 500.
        raise HTTPException(400, "missing_payload")
    interaction = parse_interaction(json.loads(raw_payload))
    _schedule(_run_logged(dispatch_interaction(request.app, interaction)))
    return Response(status_code=200)                  # empty 200 ack; message unchanged


class BindBody(BaseModel):
    code: str


@router.post("/bind")
async def bind_slack(body: BindBody, request: Request, user: dict = Depends(get_current_user)):
    from services.slack_bot.binding import redeem_verification_code, BindingThrottled
    repo = request.app.state.chat_repo
    try:
        ok = redeem_verification_code(repo._conn, user_email=user["email"], code=body.code)
    except BindingThrottled:
        # Per-caller redeem rate-limit hit — too many failed attempts in the
        # window. 429 (not 500) with a clear, non-leaky message.
        raise HTTPException(
            429,
            "too_many_attempts: too many failed code attempts; wait a few minutes and try again",
        )
    if not ok:
        raise HTTPException(400, "invalid_or_expired_code")
    return {"ok": True}
