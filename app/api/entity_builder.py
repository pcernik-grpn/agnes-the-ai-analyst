"""One turn of the /skills builder's conversation.

The sibling of ``app/api/agent_builder.py``, and deliberately close to it —
the two builders are one product, so a turn should behave the same way in
both. What differs is forced by the entities themselves:

**It is stateless.** An agent's row is minted up front, so its turn endpoint
addresses the agent by id. A Library entity has no row until the author saves
it to the Library; the draft lives in the browser. So the draft travels in
the request and nothing here reads or writes the store.

**It never applies.** ``agent_builder`` defaults ``apply=True`` because it
predates explicit saving and other callers rely on it. There is no such
history here and no row to write to: a turn returns a patch, the page merges
it into the draft, and *Save to Library* is the only thing that creates
anything. That is not a default — it is the whole contract.

**The patch is per-type.** A skill and an agent template are markdown plus
metadata, so the body is writable. A plugin is a ``.zip``: its metadata is
editable but its contents are not, and no amount of conversation should
produce a bundle. The model is told so, and the sanitizer enforces it whether
or not the model listened.

The sanitizer is the trust boundary, same as next door: unknown keys are
dropped, the category must be one the server actually offers, and nothing
outside the per-type field list survives. Model output is untrusted input.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/store", tags=["store"])

#: The three things this builder authors. Wire values, not display names —
#: `agent` is an agent TEMPLATE (a Library resource others build from), not
#: one of the agents on /agents.
TYPES = ("skill", "plugin", "agent")

#: Fields a turn may write, per type. `body` is absent for `plugin` on
#: purpose: the content of a plugin is an uploaded archive, and a patch that
#: claimed otherwise would silently discard what the author uploaded.
PATCHABLE: Dict[str, tuple] = {
    "skill": ("name", "description", "category", "body"),
    "agent": ("name", "description", "category", "body"),
    "plugin": ("name", "description", "category"),
}

#: Caps. Same reasoning as the agent builder: a client that keeps appending
#: to `history` must not be able to make one turn arbitrarily expensive.
MAX_HISTORY = 40
MAX_MESSAGE_CHARS = 4000
#: A skill body is a document, so this is far larger than a message — but it
#: is still bounded, and it is what the column will accept.
MAX_BODY_CHARS = 40000
MAX_NAME_CHARS = 64


class BuilderMessage(BaseModel):
    role: str = Field(max_length=16)
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)


class EntityDraft(BaseModel):
    """The unsaved draft, as the page currently has it.

    Untrusted, and used only to build the prompt — nothing here is written
    anywhere, and the fields that come back are re-validated on the way out.
    """

    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=4000)
    category: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)


class EntityTurnRequest(BaseModel):
    type: str = Field(max_length=16)
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    history: List[BuilderMessage] = Field(default_factory=list)
    draft: Optional[EntityDraft] = None


SYSTEM = """You are the builder inside Agnes, a governed data platform, helping
someone author a Library item.

You are filling in a form the author can see and edit beside you. Every field
you write appears in their panel immediately; they can change or undo anything.
Never claim to have saved, published or shared something — you cannot. Saving
is a button only they can press.

Write plainly. Prefer a short reply that says what you changed and asks the
one question that would most improve the item.
"""

#: What each type IS, in the words the model should reason about.
TYPE_BRIEF = {
    "skill": (
        "A SKILL is a step-by-step recipe an agent loads on demand — how to run "
        "a report, how to shape a query, how this team does a particular thing. "
        "Its body is Markdown, written as instructions to the agent that will "
        "follow them. The description is a TRIGGER: it says when an agent should "
        "reach for this skill, so it should start 'Use when …'."
    ),
    "agent": (
        "An AGENT TEMPLATE is a starting point other people build their own "
        "agent from — a named role with its own brief. Its body is Markdown "
        "describing behaviour: who the agent is, what it should do, what it "
        "should refuse. It carries NO data access of its own; whoever installs "
        "it brings their own. Never write instructions that assume particular "
        "tables or credentials exist."
    ),
    "plugin": (
        "A PLUGIN is a packaged bundle — commands, hooks, MCP servers — that the "
        "author uploads as a .zip. You CANNOT write its contents and must never "
        "pretend to. You are helping only with how it is described in the "
        "Library: its name, its description, its category. If the author asks "
        "you to write the plugin itself, say plainly that the bundle is theirs "
        "to upload and offer to describe it instead."
    ),
}


def _schema(entity_type: str, categories: List[str]) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "name": {"type": "string", "description": "lowercase-hyphenated handle"},
        "description": {"type": "string"},
    }
    if categories:
        props["category"] = {"type": "string", "enum": categories}
    if "body" in PATCHABLE[entity_type]:
        props["body"] = {"type": "string", "description": "the Markdown body"}
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "What to say to the author."},
            "patch": {
                "type": "object",
                "description": "Fields to change this turn. Omit what you are not changing.",
                "properties": props,
                "additionalProperties": False,
            },
            "suggestions": {
                "type": "array",
                "description": "Up to three short follow-ups the author might say next.",
                "items": {"type": "string"},
            },
        },
        "required": ["reply"],
        "additionalProperties": False,
    }


def _prompt(
    *,
    entity_type: str,
    message: str,
    history: List[BuilderMessage],
    draft: Dict[str, Any],
    categories: List[str],
) -> str:
    lines = [TYPE_BRIEF[entity_type], ""]
    lines.append("The panel currently reads:")
    for key in PATCHABLE[entity_type]:
        value = (draft.get(key) or "").strip()
        if key == "body" and len(value) > 2000:
            value = value[:2000] + "\n…(truncated)"
        lines.append(f"- {key}: {value or '(empty)'}")
    if categories:
        lines.append("")
        lines.append("Categories you may choose from: " + ", ".join(categories))
    if history:
        lines.append("")
        lines.append("The conversation so far:")
        for m in history[-MAX_HISTORY:]:
            who = "Author" if m.role == "user" else "You"
            lines.append(f"{who}: {m.text}")
    lines.append("")
    lines.append(f"Author: {message}")
    return "\n".join(lines)


def _sanitize_patch(raw: Any, *, entity_type: str, categories: List[str]) -> Dict[str, Any]:
    """The trust boundary. Model output is untrusted input.

    Only the fields this TYPE allows survive, strings must be strings, the
    category must be one the server actually offers, and everything is length
    -capped to what the column will take. A key we do not recognise is
    dropped silently — there is nothing useful to say to the author about a
    field that does not exist.
    """
    if not isinstance(raw, dict):
        return {}
    caps = {"name": MAX_NAME_CHARS, "description": 4000, "category": 120, "body": MAX_BODY_CHARS}
    out: Dict[str, Any] = {}
    for key in PATCHABLE[entity_type]:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str):
            continue
        value = value.strip() if key != "body" else value
        if key == "category":
            # An invented category would be rejected by the store on save and
            # would meanwhile show the author a choice that does not exist.
            if value not in categories:
                continue
        out[key] = value[: caps[key]]
    return out


def _stub_enabled() -> bool:
    """Scripted turns for local dev / tests, never in a real deployment."""
    return os.getenv("LOCAL_DEV_MODE") == "1" or os.getenv("TESTING") == "1"


def _slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:MAX_NAME_CHARS]


def _stub_turn(message: str, entity_type: str, draft: Dict[str, Any]) -> Dict[str, Any]:
    """A deterministic stand-in for the model.

    Not a simulation of quality — it exists so the whole path (page → API →
    sanitize → merge → re-render) can be exercised, screenshotted and pinned
    by tests with no credential and no network.
    """
    patch: Dict[str, Any] = {}
    words = [w for w in message.split() if w.isalpha()]
    if not (draft.get("name") or "").strip() and words:
        patch["name"] = _slugify("-".join(words[:3]))
    if not (draft.get("description") or "").strip():
        patch["description"] = (
            f"Use when … {message.strip()[:160]}" if entity_type == "skill" else message.strip()[:160]
        )
    if entity_type == "plugin":
        reply = "Described it from that. The bundle itself is yours to upload — I cannot write a plugin's contents."
        suggestions = ["Make the description shorter", "Suggest a category"]
    else:
        if "body" in PATCHABLE[entity_type] and not (draft.get("body") or "").strip():
            patch["body"] = f"## Steps\n\n1. {message.strip()[:200]}\n2. …\n"
        reply = "Drafted that into the panel. Edit anything on the right, or tell me what to change."
        suggestions = ["Add a worked example", "Make it stricter about numbers", "Shorten the body"]
    return {"reply": reply, "patch": patch, "suggestions": suggestions}


def _llm_turn(prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
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
        max_tokens=4000,
        json_schema=schema,
        schema_name="entity_builder_turn",
        system=SYSTEM,
    )


@router.post("/entities/builder/turn")
async def entity_builder_turn(
    payload: EntityTurnRequest,
    user: dict = Depends(get_current_user),
):
    """Run one builder turn for a Library entity draft.

    Returns ``{reply, patch, suggestions}``. Writes nothing: the draft is the
    caller's, and only *Save to Library* creates anything.
    """
    entity_type = (payload.type or "").strip()
    if entity_type not in TYPES:
        raise HTTPException(status_code=400, detail={"kind": "unknown_type"})

    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    from src.store_categories import STORE_CATEGORIES

    categories = list(STORE_CATEGORIES)
    draft = (payload.draft or EntityDraft()).model_dump()

    if _stub_enabled():
        result: Dict[str, Any] = _stub_turn(message, entity_type, draft)
    else:
        prompt = _prompt(
            entity_type=entity_type,
            message=message,
            history=payload.history,
            draft=draft,
            categories=categories,
        )
        schema = _schema(entity_type, categories)
        try:
            result = await asyncio.to_thread(_llm_turn, prompt, schema)
        except ValueError as e:
            # Nothing configured — say so in a way the page can act on, and
            # keep the hand-editable panel as the working path.
            logger.warning("entity builder: no LLM configured: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "builder_llm_unavailable",
                    "hint": "No AI credential is configured on this instance — "
                    "fill the form in by hand, or ask an admin to set one up.",
                },
            ) from e
        except Exception as e:
            logger.warning("entity builder: turn failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail={"kind": "builder_turn_failed", "hint": "The assistant could not answer. Try again."},
            ) from e

    patch = _sanitize_patch(result.get("patch"), entity_type=entity_type, categories=categories)
    reply = result.get("reply")
    suggestions = result.get("suggestions")
    return {
        "reply": (reply if isinstance(reply, str) else "") or "Updated the draft.",
        "patch": patch,
        "suggestions": [s for s in (suggestions or []) if isinstance(s, str)][:3],
    }
