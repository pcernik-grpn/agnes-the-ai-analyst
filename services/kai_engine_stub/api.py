"""A scriptable stand-in for the kai-agent turn engine — DEVELOPMENT ONLY.

``chat.provider: kai-agent`` points Agnes at an engine that lives outside
this repository, so until now there was no way to exercise that provider
locally: the unit tests fake the engine with an in-process
``httpx.AsyncBaseTransport`` (``tests/test_kai_engine_provider.py``), which
covers the SSE→frame translation but never the wire, the manager, the
WebSocket or the renderer. A whole class of defect lives in exactly that gap
— issue #1504's out-of-order tool calls was one, invisible to every existing
test and reproducible only by hand.

This service closes it. It speaks the subset of the engine's HTTP surface the
provider actually calls, over real HTTP, with deterministic scripted turns
and no LLM, no Anthropic key and no database:

    POST /api/chat                  → text/event-stream of AI-SDK UI events
    POST /api/chat/{id}/stop        → {"stopped": true}
    POST /api/chat/{id}/approval    → {"success": true, ...}

What it deliberately does NOT do: run a model, execute a tool, or persist a
transcript. It is a protocol fixture, so a turn's shape is chosen by keyword
from the user's message (see ``SCENARIOS``) rather than inferred. That makes
"reproduce the interleaved-tool-call bug" a typed sentence instead of a
hand-rolled JavaScript injection.

Fidelity where it is cheap and matters:

* the ``Authorization: Bearer`` session JWT is **verified** (HS256 against
  ``KAI_HOST_JWT_SECRET``, plus ``iss``/``aud``/``exp``), so the mint contract
  in ``app.api.kai.mint_engine_session_token`` is genuinely under test — a
  drifted issuer or a secret with a trailing newline fails here the way it
  would at the real engine, instead of passing locally and 401-ing in an
  environment nobody can debug from a laptop. Set ``KAI_STUB_REQUIRE_AUTH=0``
  to accept any bearer while debugging the transport itself.
* records are emitted as ``data:`` lines terminated by a blank line, and a
  ``:ping`` comment rides between turns' phases, because both are things the
  provider's SSE reader has specific handling for.

Run it with ``python -m services.kai_engine_stub`` (port 3000, the default
``chat.kai_agent_url`` host port), or via the ``kai-stub`` compose profile
where it answers to the hostname ``kai-agent``. See
``docs/kai-agent-local-dev.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="kai-agent engine stub (development only)")

#: Pause between SSE records. Long enough that a human watching the browser
#: sees text stream, a card appear, then more text — the ordering this stub
#: exists to make visible — and short enough not to slow an automated run.
_STEP_DELAY_SECONDS = float(os.environ.get("KAI_STUB_STEP_DELAY", "0.35"))


def _require_auth() -> bool:
    raw = os.environ.get("KAI_STUB_REQUIRE_AUTH", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _verify_session_jwt(authorization: Optional[str]) -> dict:
    """Verify the host-minted session JWT exactly as the real engine would.

    Deliberately hand-rolled rather than pulled from PyJWT: this stub must be
    able to run from a checkout with only the server extra installed, and the
    algorithm is fixed at HS256 by the host's own signer. `hmac.compare_digest`
    keeps the comparison constant-time — the stub is a development tool, but a
    signature check that teaches the wrong pattern is worse than none.
    """
    if not _require_auth():
        return {}
    secret = os.environ.get("KAI_HOST_JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=500,
            detail=(
                "stub misconfigured: KAI_HOST_JWT_SECRET is unset, so the host's "
                "session token cannot be verified. Set the SAME value here and on "
                "the Agnes process, or set KAI_STUB_REQUIRE_AUTH=0."
            ),
        )
    token = (authorization or "").removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="malformed_session_token")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_decode(parts[2]), expected):
        raise HTTPException(status_code=401, detail="bad_session_token_signature")
    claims = json.loads(_b64url_decode(parts[1]))
    issuer = os.environ.get("KAI_HOST_JWT_ISSUER", "agnes").strip() or "agnes"
    audience = os.environ.get("KAI_HOST_JWT_AUDIENCE", "kai-agent").strip() or "kai-agent"
    if claims.get("iss") != issuer:
        raise HTTPException(status_code=401, detail=f"issuer_mismatch: {claims.get('iss')!r} != {issuer!r}")
    if claims.get("aud") != audience:
        raise HTTPException(status_code=401, detail=f"audience_mismatch: {claims.get('aud')!r} != {audience!r}")
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise HTTPException(status_code=401, detail="session_token_expired")
    return claims


# ---------------------------------------------------------------- scenarios
# Each scenario is a list of engine events in the order the real engine would
# emit them. Keys are matched against the lowercased user message; "default"
# is the fallback. `_tool` and `_text` keep the event shapes in one place so a
# scenario reads as the SHAPE of a turn, which is what a reader is here for.


def _text(delta: str, *, part: str = "t1") -> dict:
    return {"type": "text-delta", "id": part, "delta": delta}


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"type": "tool-input-available", "toolCallId": call_id, "toolName": name, "input": args}


def _tool_output(call_id: str, output: Any) -> dict:
    return {"type": "tool-output-available", "toolCallId": call_id, "output": output}


def _tool_error(call_id: str, message: str) -> dict:
    return {"type": "tool-output-error", "toolCallId": call_id, "errorText": message}


#: An MCP text envelope, the shape the real engine forwards verbatim for an
#: MCP tool. Kept because the renderer has to unwrap it (#1504) and a stub
#: that pre-joined the blocks would hide that entirely.
def _mcp_envelope(payload: Any) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=1)
    return {"content": [{"type": "text", "text": text}]}


SCENARIOS: dict[str, list[dict]] = {
    # The turn shape from #1504: text, a tool, more text, another tool, a
    # closing paragraph. Anything that renders this out of order is broken.
    "interleav": [
        _text("Let me check the server status first. "),
        _tool_call("call_health", "server_info", {}),
        _tool_output(
            "call_health",
            _mcp_envelope({"authenticated": True, "health": {"status": "ok", "db_schema": "ok"}}),
        ),
        _text("\n\nThe server is healthy. Now the sessions by country:\n\n"),
        _tool_call(
            "call_query",
            "Bash",
            {"command": 'agnes query "SELECT country, COUNT(*) FROM web_sessions GROUP BY 1"'},
        ),
        _tool_output(
            "call_query",
            {
                "columns": ["country", "sessions"],
                "rows": [["CZ", 1204], ["DE", 998], ["US", 542], ["FR", 301], ["PL", 220], ["AT", 105]],
            },
        ),
        _text("\n\n**CZ leads** with 1204 sessions, ahead of DE at 998."),
        {"type": "finish"},
    ],
    "table": [
        _text("Pulling the numbers.\n\n"),
        _tool_call("call_t", "Bash", {"command": 'agnes query "SELECT * FROM orders LIMIT 400"'}),
        _tool_output(
            "call_t",
            {"columns": ["id", "total"], "rows": [[i, i * 3] for i in range(1, 401)]},
        ),
        _text("\n\n400 rows, capped in the preview."),
        {"type": "finish"},
    ],
    "fail": [
        _text("Trying the query.\n\n"),
        _tool_call("call_bad", "Bash", {"command": 'agnes query "SELECT * FROM nope"'}),
        _tool_error("call_bad", "Catalog Error: Table with name nope does not exist!"),
        _text("\n\nThat table does not exist — check `agnes catalog`."),
        {"type": "finish"},
    ],
    "approval": [
        _text("This one needs your sign-off.\n\n"),
        _tool_call("call_rm", "Bash", {"command": "rm -rf /data/scratch"}),
        {"type": "tool-approval-request", "toolCallId": "call_rm"},
        _tool_output("call_rm", "removed 12 files"),
        _text("\n\nDone — scratch space cleared."),
        {"type": "finish"},
    ],
    "error": [
        _text("Starting…"),
        {"type": "error", "errorText": "upstream model overloaded"},
    ],
    "markdown": [
        _text("Here is a table the model wrote itself:\n\n"),
        _tool_call("call_md", "Bash", {"command": "agnes catalog"}),
        _tool_output("call_md", "| table | rows |\n|---|---|\n| orders | 12043 |\n| users | 881 |"),
        _text("\n\nTwo tables registered."),
        {"type": "finish"},
    ],
    "default": [
        _text("You said something I have no script for, so here is the default turn. "),
        _tool_call("call_d", "server_info", {}),
        _tool_output("call_d", _mcp_envelope({"authenticated": True, "health": {"status": "ok"}})),
        _text("\n\nTry `interleaved`, `table`, `fail`, `approval`, `error` or `markdown`."),
        {"type": "finish"},
    ],
}


def _pick_scenario(message: str) -> list[dict]:
    low = (message or "").lower()
    for key, events in SCENARIOS.items():
        if key != "default" and key in low:
            return events
    return SCENARIOS["default"]


def _user_text(body: dict) -> str:
    parts = ((body.get("message") or {}).get("parts")) or []
    return " ".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


def _record(event: dict) -> bytes:
    """One SSE record: a `data:` line plus the blank line that dispatches it."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


@app.post("/api/chat")
async def chat(request: Request, authorization: Optional[str] = Header(default=None)):
    _verify_session_jwt(authorization)
    body = await request.json()
    if not body.get("id"):
        raise HTTPException(status_code=400, detail="missing_session_id")
    events = _pick_scenario(_user_text(body))
    approvals_supported = bool(body.get("supportsApprovalRequestedEvent"))
    chat_id = str(body["id"])

    async def _stream() -> AsyncIterator[bytes]:
        # `start` then the scripted body: the provider ignores both `start`
        # and `finish`, and emitting them anyway keeps the stub honest about
        # what the real stream contains.
        yield _record({"type": "start"})
        for event in events:
            if event.get("type") == "tool-approval-request":
                if not approvals_supported:
                    # The real engine parks the approval on a UI heuristic
                    # when the client does not advertise the event; for a
                    # stub, skipping it is the honest equivalent.
                    continue
                yield _record(event)
                # Wait for POST /api/chat/{id}/approval, the way the engine
                # blocks its own turn on the decision.
                decision = await _await_approval(chat_id, event["toolCallId"])
                if decision != "allow":
                    yield _record(_tool_error(event["toolCallId"], "denied by the operator"))
                    yield _record({"type": "finish"})
                    return
                continue
            yield b": ping\n\n"
            yield _record(event)
            await asyncio.sleep(_STEP_DELAY_SECONDS)

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ------------------------------------------------------------- approvals
#: chat_id → {tool_call_id: Future[str]}. In-process only; the stub serves one
#: developer at a time by design.
_pending: dict[str, dict[str, asyncio.Future]] = {}
#: A decision that arrives BEFORE its request is awaited (fast client, or a
#: replayed card) must not be lost — park it here and let the wait drain it.
_decided: dict[str, dict[str, str]] = {}
_APPROVAL_TIMEOUT_SECONDS = float(os.environ.get("KAI_STUB_APPROVAL_TIMEOUT", "300"))


async def _await_approval(chat_id: str, tool_call_id: str) -> str:
    early = _decided.get(chat_id, {}).pop(tool_call_id, None)
    if early is not None:
        return early
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending.setdefault(chat_id, {})[tool_call_id] = future
    try:
        return await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return "deny"
    finally:
        _pending.get(chat_id, {}).pop(tool_call_id, None)


@app.post("/api/chat/{chat_id}/approval")
async def approval(chat_id: str, request: Request, authorization: Optional[str] = Header(default=None)):
    _verify_session_jwt(authorization)
    body = await request.json()
    tool_call_id = str(body.get("toolUseId") or body.get("toolCallId") or "")
    approved = bool(body.get("approved", body.get("decision") in ("allow", "allow_session")))
    decision = "allow" if approved else "deny"
    future = _pending.get(chat_id, {}).get(tool_call_id)
    if future is not None and not future.done():
        future.set_result(decision)
    else:
        _decided.setdefault(chat_id, {})[tool_call_id] = decision
    return {"success": True, "toolUseId": tool_call_id, "approved": approved}


@app.post("/api/chat/{chat_id}/stop")
async def stop(chat_id: str, authorization: Optional[str] = Header(default=None)):
    _verify_session_jwt(authorization)
    # Release anything blocked on an approval so the turn's generator can
    # unwind instead of sitting on the timeout after the user cancelled.
    for future in list(_pending.get(chat_id, {}).values()):
        if not future.done():
            future.set_result("deny")
    return {"stopped": True}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "kai-engine-stub", "scenarios": sorted(SCENARIOS)}
