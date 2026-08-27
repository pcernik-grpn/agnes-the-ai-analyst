"""One turn of the MCP-source builder's conversation.

The fourth turn endpoint, and the one where the interview earns the most.
Registering an MCP source is not a form, it is a lifecycle: eleven fields on
``CreateMCPSourceRequest``, then introspect → classify → test → materialize,
plus OAuth client registration, a secret, and per-tool grants. An admin is
expected to know that order and nothing tells them.

What makes this different from its three siblings, and better suited to being
led: **the model is not inventing content, it is running a known procedure and
reporting.** The exact values still arrive by paste — a URL, a secret's env var
name — because those are values from another system and prose would add a
transcription step and a chance to invent a plausible wrong string (the
criterion in the conversational-entity-builder spec). The conversation
sequences the work and names the next unknown; the panel takes the values.

Same trust boundary as everywhere else: ``_sanitize_patch`` is where model
output stops being untrusted. Two rules it enforces that the others do not
need:

* **A URL is never invented.** The model may only propose a url the admin has
  already typed into the panel — it can reformat or correct nothing. An
  endpoint the admin did not choose is a request to an arbitrary host made in
  the instance's name.
* **A secret is never a value.** ``auth_secret_env`` names an environment
  variable; a model proposing anything that looks like a token instead of a
  variable name has it dropped.

It proposes and never writes, like the package builder and for a related
reason: registering a source and granting its tools widens what agents can
reach.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.builder_core import (
    ENGINE_MODEL,
    ENGINE_STUB,
    MAX_MESSAGE_CHARS,
    OPENING_JOB,
    SUGGESTIONS_DESCRIPTION,
    BuilderMessage,
    Slot,
    history_prompt_section,
    is_opening_turn,
    merged_draft,
    open_slots,
    panel_prompt_section,
    slots_prompt_section,
    stub_enabled,
    turn_response,
)
from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/mcp-sources",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

#: Fields a turn may propose. Deliberately excludes `enabled` (an operator
#: decision), `auth_secret_env`'s VALUE, and anything about grants — a
#: conversation adjacent to widening tool reach proposes, and the admin presses
#: the button having seen what it would write.
PATCHABLE = ("name", "transport", "url", "command", "args", "auth_method", "auth_secret_env", "scope")

TRANSPORTS = ("stdio", "http", "sse")
AUTH_METHODS = ("", "bearer", "oauth")
SCOPES = ("shared", "per_user")

MAX_NAME_CHARS = 120

#: An env var name, which is what `auth_secret_env` holds. A model that
#: proposes a token here instead has it dropped — see the module docstring.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _filled(draft: Dict[str, Any], key: str) -> bool:
    value = draft.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _endpoint_known(draft: Dict[str, Any]) -> bool:
    """A source is reachable when its transport's own address is set."""
    if draft.get("transport") == "stdio":
        return _filled(draft, "command")
    return _filled(draft, "url")


def _auth_settled(draft: Dict[str, Any]) -> bool:
    """Auth is settled when the admin has DECIDED, including deciding on none.

    ``auth_method`` is unset until they choose, and "" is a real choice, so the
    draft carries ``auth_decided`` rather than inferring "no auth" from a blank
    field — which would settle this slot before it was ever asked.
    """
    if not draft.get("auth_decided"):
        return False
    if draft.get("auth_method") == "bearer":
        return _filled(draft, "auth_secret_env")
    return True


#: The order the work actually unblocks in: you cannot introspect without an
#: address, cannot see the tools without auth, and cannot grant what you have
#: not seen.
_SLOTS = (
    Slot(
        key="endpoint",
        label="where it lives",
        known=_endpoint_known,
        ask=(
            "the server's address — a URL for http/sse, or the command for stdio. "
            "Ask for it; never invent or complete one."
        ),
    ),
    Slot(
        key="auth",
        label="how it authenticates",
        known=_auth_settled,
        ask=(
            "whether it needs a token, and if so which environment variable holds "
            "it. Name the VARIABLE, never a token value."
        ),
    ),
    Slot(
        key="name",
        label="a name",
        known=lambda d: _filled(d, "name"),
        ask="what to call it in the source list. Propose one from the URL or command; do not ask.",
    ),
    Slot(
        key="tools",
        label="which tools to expose",
        known=lambda d: bool(d.get("introspected")),
        ask=(
            "what it exposes. This is not yours to guess: tell the admin to press "
            "Check connection, which introspects the server and lists its real tools."
        ),
    ),
)

SYSTEM = """You are the MCP builder inside Agnes, a governed data platform,
helping an admin connect a tool server.

You lead a PROCEDURE. You are not inventing anything here — you are sequencing
work the admin has to do in a particular order, and reporting what the server
says back. Address, then auth, then what it exposes, then who may use it.

Rules:
- Exact values arrive by paste. A URL, a command, an environment variable name
  are values from another system: ask for them, never guess, never complete a
  partial one, and never correct one. A plausible wrong endpoint is worse than
  no endpoint.
- NEVER put a secret in a field or in your reply. `auth_secret_env` is the NAME
  of an environment variable (like ACME_MCP_TOKEN), never the token itself. If
  the admin pastes a token, tell them to put it in the vault field instead and
  give you the variable name.
- You cannot see the server's tools until it has been introspected, and you
  cannot introspect it yourself. When that is the open slot, say what pressing
  Check connection will do.
- One question per turn, about the open slot you were given. Fill what you can
  infer — a name from the host, the transport from the URL's shape — and say
  what you assumed.
- Two or three sentences, plain text, no markdown."""


class McpDraft(BaseModel):
    """The panel as it stands. Untrusted; used only to build the prompt."""

    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    transport: str = Field(default="http", max_length=16)
    url: str = Field(default="", max_length=2048)
    command: str = Field(default="", max_length=512)
    args: List[str] = Field(default_factory=list)
    auth_method: str = Field(default="", max_length=32)
    auth_secret_env: str = Field(default="", max_length=64)
    scope: str = Field(default="shared", max_length=16)
    #: Set by the panel once the admin has chosen an auth method, so "none" is
    #: distinguishable from "not asked yet".
    auth_decided: bool = False
    #: Set once Check connection has returned a tool list.
    introspected: bool = False
    tool_names: List[str] = Field(default_factory=list)


class McpTurnRequest(BaseModel):
    message: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    history: List[BuilderMessage] = Field(default_factory=list)
    draft: Optional[McpDraft] = None


def _schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "What to say to the admin."},
            "patch": {
                "type": "object",
                "description": "Panel fields to change this turn. Omit what you are not changing.",
                "properties": {
                    "name": {"type": "string"},
                    "transport": {"type": "string", "enum": list(TRANSPORTS)},
                    "url": {"type": "string", "description": "Only a url the admin already supplied."},
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "auth_method": {"type": "string", "enum": list(AUTH_METHODS)},
                    "auth_secret_env": {
                        "type": "string",
                        "description": "The NAME of an environment variable. Never a token.",
                    },
                    "scope": {"type": "string", "enum": list(SCOPES)},
                },
                "additionalProperties": False,
            },
            "suggestions": {
                "type": "array",
                "description": SUGGESTIONS_DESCRIPTION,
                "items": {"type": "string"},
            },
        },
        "required": ["reply"],
        "additionalProperties": False,
    }


def _prompt(*, message: str, history: List[BuilderMessage], draft: Dict[str, Any]) -> str:
    lines = [
        "An MCP source is a tool server this instance connects to. Its tools "
        "become callable by agents that are granted them, so registering one "
        "widens what the workspace can reach.",
        "",
    ]
    lines += panel_prompt_section(
        ("name", "transport", "url", "command", "auth_method", "auth_secret_env", "scope"), draft
    )
    if draft.get("introspected"):
        names = draft.get("tool_names") or []
        lines.append("")
        lines.append(
            f"The server has been introspected and exposes {len(names)} tools: " + (", ".join(names[:40]) or "(none)")
        )
    lines += slots_prompt_section(_SLOTS, draft)
    lines += history_prompt_section(history)
    lines.append("")
    lines.append(OPENING_JOB if is_opening_turn(message, history) else f"Admin: {message}")
    return "\n".join(lines)


def _sanitize_patch(raw: Any, *, draft: Dict[str, Any]) -> Dict[str, Any]:
    """The trust boundary. Model output is untrusted input.

    Beyond the usual field/enum/length checks, two refusals specific to what
    this endpoint configures:

    * a ``url`` the admin has not already typed is dropped — the model may not
      choose which host the instance dials, and "correcting" a URL is the same
      act as choosing one;
    * an ``auth_secret_env`` that is not shaped like an environment variable
      name is dropped, which is what stops a pasted token being written into a
      field that is stored and displayed.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in PATCHABLE:
        if key not in raw:
            continue
        value = raw[key]
        if key == "args":
            if isinstance(value, list):
                out["args"] = [v.strip() for v in value if isinstance(v, str) and v.strip()][:32]
            continue
        if not isinstance(value, str):
            continue
        value = value.strip()
        if key == "transport" and value not in TRANSPORTS:
            continue
        if key == "auth_method" and value not in AUTH_METHODS:
            continue
        if key == "scope" and value not in SCOPES:
            continue
        if key == "url" and value and value != (draft.get("url") or "").strip():
            continue
        if key == "auth_secret_env" and value and not _ENV_NAME_RE.match(value):
            continue
        out[key] = value[:2048]
    return out


def _stub_turn(message: str, draft: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic stand-in — exercises the path, simulates no quality."""
    still_open = open_slots(_SLOTS, draft)
    first = still_open[0].label if still_open else None
    if not message.strip():
        return {
            "reply": (
                f"[stub] Let's connect a tool server. First thing I need: {first}."
                if first
                else "[stub] Let's connect a tool server."
            ),
            "patch": {},
            "suggestions": [],
        }
    patch: Dict[str, Any] = {}
    if not (draft.get("name") or "").strip():
        host = re.sub(r"^https?://", "", (draft.get("url") or "").strip()).split("/")[0]
        if host:
            patch["name"] = host
    return {
        "reply": (f"[stub] Noted. Next: {first}." if first else "[stub] That is everything I need."),
        "patch": patch,
        "suggestions": ["It needs a bearer token", "There is no auth"],
    }


def _llm_turn(prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    from app.instance_config import load_instance_config
    from connectors.llm import create_extractor_from_env_or_config

    try:
        instance_config = load_instance_config()
    except (ValueError, FileNotFoundError):
        instance_config = {}
    extractor = create_extractor_from_env_or_config((instance_config or {}).get("ai"))
    return extractor.extract_json(
        prompt=prompt,
        max_tokens=2000,
        json_schema=schema,
        schema_name="mcp_builder_turn",
        system=SYSTEM,
    )


@router.post("/builder/turn")
async def mcp_builder_turn(payload: McpTurnRequest):
    """Run one turn of the MCP builder. Proposes into the panel; writes nothing.

    Registering the source, storing its secret and granting its tools are all
    the admin's own button presses, made against a panel they can read.
    """
    message = (payload.message or "").strip()
    if not message and not is_opening_turn(message, payload.history):
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    draft = (payload.draft or McpDraft()).model_dump()

    engine = ENGINE_STUB if stub_enabled() else ENGINE_MODEL
    if engine == ENGINE_STUB:
        result: Dict[str, Any] = _stub_turn(message, draft)
    else:
        try:
            result = await asyncio.to_thread(
                _llm_turn, _prompt(message=message, history=payload.history, draft=draft), _schema()
            )
        except ValueError as e:
            logger.warning("mcp builder: no LLM configured: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "builder_llm_unavailable",
                    "hint": "No AI credential is configured on this instance — "
                    "fill the connection in by hand, or set one up in server config.",
                },
            ) from e
        except Exception as e:
            logger.warning("mcp builder: turn failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail={"kind": "builder_turn_failed", "hint": "The assistant could not answer. Try again."},
            ) from e

    patch = _sanitize_patch(result.get("patch"), draft=draft)
    return turn_response(
        result,
        patch=patch,
        engine=engine,
        slots=_SLOTS,
        draft=merged_draft(draft, patch),
        fallback_reply="Updated the connection.",
    )
