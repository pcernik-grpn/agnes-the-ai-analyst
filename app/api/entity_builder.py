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
    panel_prompt_section,
    slots_prompt_section,
    stub_enabled,
    turn_response,
)
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

#: A skill body is a document, so this is far larger than a message — but it
#: is still bounded, and it is what the column will accept.
MAX_BODY_CHARS = 40000
MAX_NAME_CHARS = 64


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

You lead. The author came here because they know what they want and not what a
good one of these needs — so do not ask them to describe the whole thing and
then fill a form from it. Work through what is still unknown, one thing at a
time, in the order you are given.

You are filling in a form the author can see and edit beside you. Every field
you write appears in their panel immediately; they can change or undo anything.
Never claim to have saved, published or shared something — you cannot. Saving
is a button only they can press.

Rules:
- Every turn must leave the panel further along. Filling something in and
  saying what you assumed beats asking, whenever you can infer it well.
- One question per turn, at most, and only about the slot you were given.
- Never re-ask something already settled or already answered in the
  transcript.
- Write plainly. Two or three sentences. No headings, no bullet lists.
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


def _has(draft: Dict[str, Any], key: str, *, chars: int = 1) -> bool:
    value = draft.get(key)
    return isinstance(value, str) and len(value.strip()) >= chars


#: What each type needs to know, in the order that unblocks the most.
#:
#: The thresholds are not arbitrary: the store's own content guardrail rejects
#: a description under ~60 characters or 5 distinct words, and a body under
#: ~200 (src/store_guardrails/content_check.py). A slot that counted a
#: three-word description as settled would hand the author a draft that Check
#: then refuses — the interview would be leading them into a wall.
_SLOTS: Dict[str, tuple] = {
    "skill": (
        Slot(
            key="purpose",
            label="what it does",
            known=lambda d: _has(d, "body", chars=200) or _has(d, "description", chars=40),
            ask="the job this skill performs — a report to run, a query to shape, a way this team does something.",
        ),
        Slot(
            key="trigger",
            label="the trigger",
            known=lambda d: _has(d, "description", chars=60),
            ask="when an agent should reach for it. The description IS the trigger and should start 'Use when …'.",
        ),
        Slot(
            key="steps",
            label="the steps",
            known=lambda d: _has(d, "body", chars=200),
            ask="the actual recipe, as instructions to the agent that will follow them.",
        ),
        Slot(
            key="name",
            label="a name",
            known=lambda d: _has(d, "name"),
            ask="a lowercase-hyphenated handle. Propose one from what it does; do not ask.",
        ),
        Slot(
            key="category",
            label="a category",
            known=lambda d: _has(d, "category"),
            ask="which of the offered categories it belongs in. Pick one; do not ask.",
        ),
    ),
    "agent": (
        Slot(
            key="role",
            label="the role",
            known=lambda d: _has(d, "body", chars=200) or _has(d, "description", chars=40),
            ask="who this agent is and what it is for.",
        ),
        Slot(
            key="description",
            label="what it is for",
            known=lambda d: _has(d, "description", chars=60),
            ask="what the agent does and the situation that calls for it — what someone reads before installing it.",
        ),
        Slot(
            key="behaviour",
            label="how it works",
            known=lambda d: _has(d, "body", chars=200),
            ask="how it should answer and what it must refuse. Remember it carries no data access of its own.",
        ),
        Slot(
            key="name",
            label="a name",
            known=lambda d: _has(d, "name"),
            ask="a lowercase-hyphenated handle. Propose one; do not ask.",
        ),
        Slot(
            key="category",
            label="a category",
            known=lambda d: _has(d, "category"),
            ask="which of the offered categories it belongs in. Pick one; do not ask.",
        ),
    ),
    # A plugin's contents are not yours to write, so its interview is only
    # about how it reads in the Library.
    "plugin": (
        Slot(
            key="what",
            label="what it carries",
            known=lambda d: _has(d, "description", chars=60),
            ask="what this plugin adds and who should install it.",
        ),
        Slot(
            key="name",
            label="a name",
            known=lambda d: _has(d, "name"),
            ask="a lowercase-hyphenated handle. Propose one; do not ask.",
        ),
        Slot(
            key="category",
            label="a category",
            known=lambda d: _has(d, "category"),
            ask="which of the offered categories it belongs in. Pick one; do not ask.",
        ),
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
                "description": SUGGESTIONS_DESCRIPTION,
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
    lines += panel_prompt_section(PATCHABLE[entity_type], draft, truncate={"body": 2000})
    if categories:
        lines.append("")
        lines.append("Categories you may choose from: " + ", ".join(categories))
    lines += slots_prompt_section(_SLOTS[entity_type], draft)
    lines += history_prompt_section(history)
    lines.append("")
    lines.append(OPENING_JOB if is_opening_turn(message, history) else f"Author: {message}")
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
    by tests with no credential and no network. Every response it produces is
    labelled ``engine: "stub"`` on the way out, so nobody mistakes it for one.
    """
    if not message.strip():
        # The opening turn. Named the same way a real one would be, so the
        # unprompted-first-turn path is exercisable without a credential.
        first = _open_slot_label(entity_type, draft)
        return {
            "reply": (
                f"[stub] Let's build a {entity_type}. First thing I need: {first}."
                if first
                else f"[stub] Let's build a {entity_type}. Tell me what it should do."
            ),
            "patch": {},
            "suggestions": [],
        }
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


def _open_slot_label(entity_type: str, draft: Dict[str, Any]) -> Optional[str]:
    from app.api.builder_core import open_slots

    still_open = open_slots(_SLOTS[entity_type], draft)
    return still_open[0].label if still_open else None


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
    # An empty message is the builder opening the conversation — allowed only
    # on the FIRST turn. Later, it is a client bug, and answering it would let
    # a page spend tokens on whitespace.
    if not message and not is_opening_turn(message, payload.history):
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    from src.store_categories import STORE_CATEGORIES

    categories = list(STORE_CATEGORIES)
    draft = (payload.draft or EntityDraft()).model_dump()

    engine = ENGINE_STUB if stub_enabled() else ENGINE_MODEL
    if engine == ENGINE_STUB:
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
    return turn_response(
        result,
        patch=patch,
        engine=engine,
        slots=_SLOTS[entity_type],
        # Progress is reported against the draft the page will HOLD after this
        # turn, not the one it sent — otherwise a turn that fills two slots
        # reports the state it started from.
        draft=merged_draft(draft, patch),
        fallback_reply="Updated the draft.",
    )


#: The one scratch agent a user's template previews run as. A fixed slug, so
#: there is at most ONE per user however many templates they try: each preview
#: overwrites it. That bounds what a browser that dies mid-preview can leave
#: behind to a single invisible row, which is why there is no delete endpoint
#: to race against the next preview.
PREVIEW_SLUG = "template-preview"

#: Not a lifecycle value the builder offers — see `list_for_user` in
#: src/repositories/agents.py for why these rows are filtered out of every
#: list. Fetch-by-slug still finds it; that is how the session resolves.
SCRATCH_STATUS = "scratch"


class PreviewAgentRequest(BaseModel):
    """The draft template to try out. Body only — a template carries no data
    access of its own, and the preview must not invent any."""

    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)


@router.post("/entities/builder/preview-agent")
async def preview_agent(
    payload: PreviewAgentRequest,
    user: dict = Depends(get_current_user),
):
    """Point the caller's scratch agent at this draft template and return its
    slug, so the page can open a normal chat session against it.

    An agent template IS a system prompt, so previewing one means running an
    agent with that prompt. That needs a row, because a session runs as an
    agent id — hence a scratch agent rather than some parallel un-agent path
    that would drift from how templates actually behave once installed.

    Deliberately inherits NOTHING from the author's own agents: default
    scope modes, no knowledge, no plugins. A template carries no data access,
    so a preview that quietly ran with the author's would flatter it —
    someone would install a template that worked in the preview and does
    nothing for them.
    """
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail={"kind": "empty_body"})

    from src.repositories import agents_repo

    repo = agents_repo()
    name = (payload.name or "").strip() or "Template preview"
    row = repo.get_by_slug(user["id"], PREVIEW_SLUG)
    if row:
        repo.update(str(row["id"]), name=name, system_prompt=body, status=SCRATCH_STATUS)
        agent_id = str(row["id"])
    else:
        import uuid

        agent_id = f"agt_{uuid.uuid4().hex}"
        repo.create(
            id=agent_id,
            owner_user_id=user["id"],
            name=name,
            slug=PREVIEW_SLUG,
            system_prompt=body,
            status=SCRATCH_STATUS,
        )
    return {"slug": PREVIEW_SLUG, "id": agent_id}
