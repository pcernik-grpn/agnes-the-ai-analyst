"""Agent Builder assistant — turn a description into agent configuration.

``POST /api/agents/{agent_id}/builder/turn`` takes what the owner just typed
plus the conversation so far, and returns (a) a prose reply and (b) a
**config patch** that has already been applied to the agent. The ``/agents``
builder renders the reply in its Create pane and re-renders the Configuration
panel from the returned agent row, which is how a conversation fills the form
without the owner typing into it.

Design notes
------------

**Stateless turns.** The transcript lives in the page and is replayed on each
call; the server keeps no conversation state, so this needed no schema change
(A3: new app-state would be Postgres-only, and a chat log is not worth a
table until drafts need to survive a reload).

**One LLM call per turn**, through the existing ``connectors.llm`` structured
extractor — the same seam corporate memory and knowledge digests use. There
is no sandbox and no tool loop: what the agent may be grounded in is handed
to the model as a candidate list, and it picks ids from that list. A
sandbox-backed engine with real catalog tools can replace ``_llm_turn`` later
without the wire contract changing.

**The model's output is untrusted.** ``_sanitize_patch`` is the trust
boundary: unknown keys are dropped, ids not present in the candidate lists
are dropped, tone must be one of the four the UI offers, surfaces must be
known keys with boolean values, and the survivors are re-validated by
``AgentUpdate`` (which enforces the column lengths) before anything is
written. The patch is then applied through the ordinary
:func:`app.api.agents.update_agent` path, so the builder-declaration →
enforced-scope derivation (`_sync_builder_scope`) runs exactly as it does for
a hand edit.

Even a patch that slipped a bogus id past all of that would convey no
authority: an agent's live authority is ``owner grants ∩ agent scope``,
recomputed per request (``src/agent_scope_intersection.py``). Declaring an
id the owner does not hold widens nothing.

**Degrading without a model.** With no LLM credential configured the endpoint
answers ``503 builder_llm_unavailable`` and the page falls back to the
hand-editable panel, which is fully functional on its own — the assistant is
an accelerator, never the only door. Under ``LOCAL_DEV_MODE``/``TESTING`` a
scripted stub stands in so the surface can be exercised end-to-end with no
key (mirrors ``services/kai_engine_stub`` for chat).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.agents import AgentUpdate, _writable, update_agent
from app.auth.access import require_agent_profiles_enabled
from app.auth.dependencies import get_current_user
from app.services.agent_ingredients import knowledge_sources_for

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

#: Tones the builder UI offers. The model may not invent a fifth.
TONES = ("concise", "friendly", "formal", "playful")

#: Surface keys the builder UI offers.
SURFACES = ("web", "slack", "telegram", "cli", "mcp")

#: Fields the assistant may write. Deliberately excludes `status`,
#: `is_default` and every `*_mode` column: promoting a draft to ready and
#: widening a scope axis are owner decisions, not conversational side effects.
PATCHABLE = ("name", "role", "instructions", "tone", "greeting", "knowledge", "plugins", "surfaces")

#: Transcript cap. Long enough for a real design conversation, short enough
#: that one turn cannot be made arbitrarily expensive by a client that keeps
#: appending to `history`.
MAX_HISTORY = 40
MAX_MESSAGE_CHARS = 4000

#: Candidate lists sent to the model, capped so the prompt stays bounded.
MAX_CANDIDATES = 60


class BuilderMessage(BaseModel):
    role: str = Field(max_length=16)
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)


class PluginCandidate(BaseModel):
    """A capability the page is offering in its picker.

    Sent by the client because the marketplace ids the picker uses are
    assembled there (curated ∪ store, with their display prefixes); the
    server validates the model's choices against this same list. Safe as a
    candidate set for the reason in the module docstring: a declared scope id
    grants nothing on its own.
    """

    id: str = Field(max_length=200)
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=400)


class BuilderTurnRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    history: List[BuilderMessage] = Field(default_factory=list)
    plugin_candidates: List[PluginCandidate] = Field(default_factory=list)


def _candidate_block(rows: List[Dict[str, Any]], empty: str) -> str:
    if not rows:
        return empty
    return "\n".join(
        "- id={id} kind={kind} name={name} — {desc} ({meta})".format(
            id=r.get("id", ""),
            kind=r.get("kind", ""),
            name=r.get("name", ""),
            desc=(r.get("description") or "no description")[:200],
            meta=r.get("meta", ""),
        )
        for r in rows[:MAX_CANDIDATES]
    )


SYSTEM = """You are the agent builder inside Agnes, a governed data platform.

An owner describes an assistant they want; you turn that into configuration.
You are talking to the owner, not to end users of the agent.

Rules:
- Ask at most ONE question per turn, and only when the answer would change
  the configuration. Otherwise propose and move on — the owner can edit
  every field by hand.
- Fill in what you can infer immediately. A vague ask still deserves a
  concrete first draft; an empty form is not a safe default.
- Ground the agent only in the candidate knowledge sources listed below,
  by id. Never invent an id. If nothing fits, say so plainly and leave
  knowledge empty rather than guessing — an agent grounded in nothing is
  honest, an agent claiming data it cannot reach is not.
- `instructions` is the agent's system prompt: its role, how it should
  answer, what it must refuse or avoid. Write it in the second person
  ("You are…"), a few short paragraphs at most. It is visible to the
  agent's users, so never put secrets in it.
- `greeting` is the agent's first line to its users. One sentence.
- `role` is a one-line description shown before anyone opens it.
- Keep `reply` short — two or three sentences, plain text, no markdown
  headings and no bullet lists. Say what you set and what is still open.

Return only the fields you are changing this turn."""

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "What to say to the owner."},
        "patch": {
            "type": "object",
            "description": "Configuration fields to change this turn.",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "instructions": {"type": "string"},
                "tone": {"type": "string", "enum": list(TONES)},
                "greeting": {"type": "string"},
                "knowledge": {"type": "array", "items": {"type": "string"}},
                "plugins": {"type": "array", "items": {"type": "string"}},
                "surfaces": {
                    "type": "object",
                    "properties": {k: {"type": "boolean"} for k in SURFACES},
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "suggestions": {
            "type": "array",
            "description": "Up to three short follow-ups the owner might say next.",
            "items": {"type": "string"},
        },
    },
    "required": ["reply"],
    "additionalProperties": False,
}


def _prompt(
    *,
    message: str,
    history: List[BuilderMessage],
    config: Dict[str, Any],
    knowledge: List[Dict[str, Any]],
    plugins: List[PluginCandidate],
) -> str:
    lines: List[str] = []
    lines.append("## Knowledge sources this owner can ground the agent in")
    lines.append(_candidate_block(knowledge, "(none — this owner has no data packages, memory domains or artefact collections)"))
    lines.append("")
    lines.append("## Capabilities this owner can give the agent")
    if plugins:
        lines.append(
            "\n".join(
                f"- id={p.id} name={p.name} — {(p.description or 'no description')[:200]}"
                for p in plugins[:MAX_CANDIDATES]
            )
        )
    else:
        lines.append("(none available)")
    lines.append("")
    lines.append("## Current configuration")
    lines.append(json.dumps(config, indent=2, sort_keys=True))
    lines.append("")
    lines.append("## Conversation")
    for m in history[-MAX_HISTORY:]:
        who = "Owner" if m.role == "user" else "You"
        lines.append(f"{who}: {m.text}")
    lines.append(f"Owner: {message}")
    return "\n".join(lines)


def _sanitize_patch(
    raw: Any,
    *,
    knowledge_ids: set,
    plugin_ids: set,
) -> Dict[str, Any]:
    """Reduce a model-proposed patch to what it is allowed to change.

    Silent dropping is deliberate: a hallucinated id or an invented tone is a
    model error, not an owner error, and failing the whole turn over it would
    lose the good half of the patch. What survived is echoed back to the page
    in the response, so the owner sees exactly what changed.
    """
    if not isinstance(raw, dict):
        return {}
    patch: Dict[str, Any] = {}
    for key in PATCHABLE:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("name", "role", "instructions", "greeting"):
            if isinstance(value, str) and value.strip():
                patch[key] = value.strip()
        elif key == "tone":
            if value in TONES:
                patch[key] = value
        elif key == "knowledge":
            if isinstance(value, list):
                patch[key] = [v for v in value if isinstance(v, str) and v in knowledge_ids]
        elif key == "plugins":
            if isinstance(value, list):
                patch[key] = [v for v in value if isinstance(v, str) and v in plugin_ids]
        elif key == "surfaces":
            if isinstance(value, dict):
                clean = {k: bool(v) for k, v in value.items() if k in SURFACES and isinstance(v, bool)}
                # Web chat is the base surface the builder itself previews on;
                # an assistant turning it off would silently break Preview.
                if clean:
                    clean["web"] = True
                    patch[key] = clean
    if not patch:
        return {}
    # Second gate: the column-length constraints, enforced by the same model
    # the hand-edit PATCH validates against.
    return AgentUpdate(**patch).model_dump(exclude_unset=True, exclude_none=True)


def _stub_enabled() -> bool:
    """Scripted turns for local dev / tests, never in a real deployment."""
    return os.getenv("LOCAL_DEV_MODE") == "1" or os.getenv("TESTING") == "1"


def _stub_turn(message: str, config: Dict[str, Any], knowledge: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A deterministic stand-in for the model.

    Not a simulation of quality — it exists so the whole path (page → API →
    sanitize → PATCH → re-render) can be exercised, screenshotted and pinned
    in CI without a credential. Keyword-driven so a demo reads sensibly.
    """
    text = (message or "").lower()
    if "revenue" in text or "finance" in text or "margin" in text:
        topic, name, tone = "revenue", "Revenue Analyst", "concise"
    elif "hr" in text or "policy" in text or "handbook" in text:
        topic, name, tone = "policy", "Policy Helper", "friendly"
    elif "pipeline" in text or "sales" in text:
        topic, name, tone = "pipeline", "Pipeline Analyst", "concise"
    else:
        topic, name, tone = "general", "Data Assistant", "concise"

    patch: Dict[str, Any] = {}
    if not (config.get("name") or "").strip():
        patch["name"] = name
        patch["role"] = f"Answers {topic} questions from governed data."
        patch["tone"] = tone
        patch["greeting"] = f"Hi — ask me anything about {topic}."
        patch["instructions"] = (
            f"You are {name}. You answer {topic} questions for the team.\n\n"
            "Always use the canonical metric definitions rather than inventing a "
            "calculation. Cite the table you queried. If the data cannot answer "
            "the question, say so instead of estimating."
        )
        if knowledge:
            patch["knowledge"] = [knowledge[0]["id"]]
        reply = (
            f"Set it up as {name} — {topic} questions, {tone} tone. "
            + (f"Grounded it in {knowledge[0]['name']}. " if knowledge else "It has no data yet. ")
            + "Anything it should refuse to answer?"
        )
        suggestions = ["It should never guess a number", "Add a second data source", "Make the tone friendlier"]
    else:
        reply = "Updated the instructions with that. Try it in Preview when you are ready."
        instructions = (config.get("instructions") or "").rstrip()
        patch["instructions"] = (instructions + "\n\n" + message.strip()).strip()
        suggestions = ["Show me a preview", "Change the greeting"]
    return {"reply": reply, "patch": patch, "suggestions": suggestions}


def _llm_turn(prompt: str) -> Dict[str, Any]:
    """One structured call. Raises ``ValueError`` when nothing is configured."""
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
        json_schema=RESPONSE_SCHEMA,
        schema_name="agent_builder_turn",
        system=SYSTEM,
    )


def _current_config(row: dict) -> Dict[str, Any]:
    from app.api.agents import _decode

    return {
        "name": row.get("name") or "",
        "role": row.get("role") or "",
        "instructions": row.get("system_prompt") or "",
        "tone": row.get("tone") or "concise",
        "greeting": row.get("greeting") or "",
        "knowledge": _decode(row.get("knowledge"), []),
        "plugins": _decode(row.get("plugins"), []),
        "surfaces": _decode(row.get("surfaces"), {}),
    }


@router.post("/{agent_id}/builder/turn")
async def builder_turn(
    agent_id: str,
    payload: BuilderTurnRequest,
    user: dict = Depends(get_current_user),
):
    """Run one builder turn against an agent the caller owns."""
    row = _writable(agent_id, user)
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    knowledge = knowledge_sources_for(user)
    knowledge_ids = {str(k["id"]) for k in knowledge}
    plugin_ids = {p.id for p in payload.plugin_candidates}
    config = _current_config(row)

    if _stub_enabled():
        result: Dict[str, Any] = _stub_turn(message, config, knowledge)
    else:
        prompt = _prompt(
            message=message,
            history=payload.history,
            config=config,
            knowledge=knowledge,
            plugins=payload.plugin_candidates,
        )
        try:
            result = await asyncio.to_thread(_llm_turn, prompt)
        except ValueError as e:
            # Nothing configured — say so in a way the page can act on, and
            # keep the hand-editable panel as the working path.
            logger.warning("agent builder: no LLM configured: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "builder_llm_unavailable",
                    "hint": "No AI credential is configured on this instance — "
                    "fill the configuration in by hand, or ask an admin to set one up.",
                },
            ) from e
        except Exception as e:
            logger.warning("agent builder: turn failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail={"kind": "builder_turn_failed", "hint": "The assistant could not answer. Try again."},
            ) from e

    patch = _sanitize_patch(
        result.get("patch"),
        knowledge_ids=knowledge_ids,
        plugin_ids=plugin_ids,
    )
    agent: Optional[Dict[str, Any]] = None
    if patch:
        agent = await update_agent(agent_id, AgentUpdate(**patch), user)

    reply = result.get("reply")
    suggestions = result.get("suggestions")
    return {
        "reply": (reply if isinstance(reply, str) else "") or "Updated the configuration.",
        "patch": patch,
        "agent": agent,
        "suggestions": [s for s in (suggestions or []) if isinstance(s, str)][:3],
    }
