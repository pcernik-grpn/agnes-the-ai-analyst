"""Phase 0 — HTTP events endpoint must ack before the slow dispatch runs.

Regression for the latent duplicate-session bug: the old code did
`await dispatch_event(...)` before returning 200, so a >3s _handle_dm
(sandbox spawn) blew Slack's 3s budget and triggered retries. We assert the
handler returns near-instantly and dispatches exactly once.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.slack import router as slack_router

_SECRET = "ack-async-secret"


def _signed_post(client, payload: dict):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return client.post(
        "/api/slack/events",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/json",
        },
    )


def test_events_endpoint_acks_before_slow_dispatch(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)

    dispatched: list[dict] = []

    async def slow_dispatch(app, event):
        # 5s mock dispatch — must stay > the 3.0s assertion below to catch a regression back to await
        await asyncio.sleep(5)  # simulate sandbox spawn > 3s budget
        dispatched.append(event)

    # Patch the symbol used inside the endpoint module.
    import app.api.slack as slack_api
    monkeypatch.setattr(slack_api, "dispatch_event", slow_dispatch)

    app = FastAPI()
    app.include_router(slack_router)

    with TestClient(app) as client:
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "channel_type": "im",
                      "channel": "D1", "user": "U1", "ts": "1.1", "text": "hi"},
        }
        start = time.monotonic()
        resp = _signed_post(client, payload)
        elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The ack must beat Slack's 3s budget despite the 5s dispatch body.
    assert elapsed < 3.0, f"ack took {elapsed:.2f}s — should not await dispatch"


def test_url_verification_still_returns_challenge(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(slack_router)
    with TestClient(app) as client:
        resp = _signed_post(client, {"type": "url_verification", "challenge": "xyz"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "xyz"}


def test_bad_signature_rejected(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(slack_router)
    with TestClient(app) as client:
        body = json.dumps({"type": "event_callback", "event": {}}).encode()
        resp = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=deadbeef",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401
