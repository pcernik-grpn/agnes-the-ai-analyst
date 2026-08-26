"""One turn of the data-package drawer's conversation.

The third builder turn endpoint, after ``agent_builder`` and
``entity_builder``. Same shape — a message in, a sanitized patch out — and
the same trust boundary: model output is untrusted input.

What makes this one different is what a data package IS. It is the unit
governed data reaches analysts through, and creating one writes **grants**:
"share it with the sales team" is a sentence that widens who can see tables.
So this endpoint has a rule its siblings do not need:

    IT ONLY EVER PROPOSES. There is no `apply` flag, not even one defaulting
    to false. A turn returns a patch for the drawer to hold, and the admin
    presses Create — having seen the access matrix they are about to write.
    An admin should never learn what a conversation granted by reading it
    back afterwards.

The candidate lists are fetched HERE rather than accepted from the caller.
Its siblings take the page's own picker contents, which is fine when the
worst case is naming a plugin you were not offered. Here the worst case is a
group, so the set of grantable groups is the server's answer, not the
client's — and the sanitizer checks proposed ids against it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/data-packages",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

#: Fields a turn may propose. `slug` is absent: it is derived from the name
#: and the drawer already keeps them in step, so a model writing one directly
#: could only desynchronise them.
PATCHABLE = ("name", "description", "tables", "groups")

MAX_HISTORY = 40
MAX_MESSAGE_CHARS = 4000
MAX_NAME_CHARS = 120
#: Candidate lists sent to the model, capped so the prompt stays bounded. A
#: large instance can have thousands of registered tables.
MAX_CANDIDATES = 120


class BuilderMessage(BaseModel):
    role: str = Field(max_length=16)
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)


class PackageDraft(BaseModel):
    """The drawer's unsaved state. Used only to build the prompt."""

    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=4000)
    tables: List[str] = Field(default_factory=list)
    groups: List[str] = Field(default_factory=list)


class PackageTurnRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    history: List[BuilderMessage] = Field(default_factory=list)
    draft: Optional[PackageDraft] = None


SYSTEM = """You are helping an administrator of Agnes, a governed data platform,
put together a DATA PACKAGE.

A data package is the unit governed data reaches analysts through: it bundles
registered tables, and it is granted to groups. An analyst can only reach a
table through a package granted to a group they are in.

You are filling in a form the admin can see and edit beside you. You never
save, create or share anything — you propose, and they press Create. Say so
plainly if asked to do it for them.

Be conservative with access. Suggest the narrowest set of groups that matches
what they asked for, and if they have not said who it is for, ask rather than
guessing. Widening access is the one mistake here that is expensive to undo.
"""


def _candidates() -> Dict[str, List[Dict[str, str]]]:
    """The tables and groups this instance actually has.

    Fetched server-side on purpose — see the module docstring. A caller
    cannot enlarge the set of groups a turn is allowed to propose.
    """
    from src.repositories import table_registry_repo, user_groups_repo

    tables = [
        {
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or row.get("id") or ""),
            "description": str(row.get("description") or "")[:160],
        }
        for row in table_registry_repo().list_all()
    ]
    groups = [
        {"id": str(row.get("id") or ""), "name": str(row.get("name") or "")} for row in user_groups_repo().list_all()
    ]
    return {"tables": tables, "groups": groups}


def _schema(tables: List[Dict[str, str]], groups: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "What to say to the admin."},
            "patch": {
                "type": "object",
                "description": "Fields to propose this turn. Omit what you are not changing.",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tables": {
                        "type": "array",
                        "description": "Registered table ids to include.",
                        "items": {"type": "string", "enum": [t["id"] for t in tables]}
                        if tables
                        else {"type": "string"},
                    },
                    "groups": {
                        "type": "array",
                        "description": "Group ids to grant this package to.",
                        "items": {"type": "string", "enum": [g["id"] for g in groups]}
                        if groups
                        else {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
            "suggestions": {
                "type": "array",
                "description": "Up to three short follow-ups the admin might say next.",
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
    draft: Dict[str, Any],
    tables: List[Dict[str, str]],
    groups: List[Dict[str, str]],
) -> str:
    lines = ["The drawer currently reads:"]
    lines.append(f"- name: {(draft.get('name') or '').strip() or '(empty)'}")
    lines.append(f"- description: {(draft.get('description') or '').strip() or '(empty)'}")
    lines.append(f"- tables: {', '.join(draft.get('tables') or []) or '(none yet)'}")
    lines.append(f"- groups: {', '.join(draft.get('groups') or []) or '(none yet)'}")
    lines.append("")
    lines.append("Registered tables you may include:")
    for t in tables[:MAX_CANDIDATES]:
        lines.append(f"- id={t['id']} name={t['name']} — {t['description'] or 'no description'}")
    if len(tables) > MAX_CANDIDATES:
        lines.append(f"…and {len(tables) - MAX_CANDIDATES} more not listed. Ask rather than guessing.")
    lines.append("")
    lines.append("Groups you may grant to:")
    for g in groups[:MAX_CANDIDATES]:
        lines.append(f"- id={g['id']} name={g['name']}")
    if history:
        lines.append("")
        lines.append("The conversation so far:")
        for m in history[-MAX_HISTORY:]:
            who = "Admin" if m.role == "user" else "You"
            lines.append(f"{who}: {m.text}")
    lines.append("")
    lines.append(f"Admin: {message}")
    return "\n".join(lines)


def _sanitize_patch(raw: Any, *, table_ids: set, group_ids: set) -> Dict[str, Any]:
    """The trust boundary.

    An id the instance does not have is dropped rather than corrected — for
    tables because the package would fail to save, and for groups because a
    fabricated id is the one thing here that must never reach a grant.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    if isinstance(raw.get("name"), str):
        out["name"] = raw["name"].strip()[:MAX_NAME_CHARS]
    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"].strip()[:4000]
    for key, allowed in (("tables", table_ids), ("groups", group_ids)):
        value = raw.get(key)
        if not isinstance(value, list):
            continue
        # Order-preserving dedupe; anything not on the instance is dropped.
        seen: set = set()
        kept: List[str] = []
        for item in value:
            if isinstance(item, str) and item in allowed and item not in seen:
                seen.add(item)
                kept.append(item)
        out[key] = kept
    return out


def _stub_enabled() -> bool:
    """Scripted turns for local dev / tests, never in a real deployment."""
    return os.getenv("LOCAL_DEV_MODE") == "1" or os.getenv("TESTING") == "1"


def _stub_turn(message: str, draft: Dict[str, Any], tables: List[Dict[str, str]]) -> Dict[str, Any]:
    """A deterministic stand-in so the whole path can be exercised with no
    credential and no network. Deliberately proposes NO groups: the scripted
    engine must not be the thing that teaches this flow to hand out access."""
    patch: Dict[str, Any] = {}
    if not (draft.get("name") or "").strip():
        patch["name"] = message.strip()[:60] or "New package"
    if not (draft.get("description") or "").strip():
        patch["description"] = f"Tables for {message.strip()[:120]}"
    words = {w.lower() for w in message.split()}
    hits = [t["id"] for t in tables if any(w in t["name"].lower() for w in words if len(w) > 3)]
    if hits:
        patch["tables"] = hits[:5]
    return {
        "reply": (
            "Proposed that into the drawer — review it and press Create. "
            "Tell me who it is for and I will suggest the groups; I do not grant anything myself."
        ),
        "patch": patch,
        "suggestions": ["It is for the sales team", "Add the invoice tables", "Describe it for an analyst"],
    }


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
        max_tokens=2000,
        json_schema=schema,
        schema_name="package_builder_turn",
        system=SYSTEM,
    )


@router.post("/builder/turn")
async def package_builder_turn(payload: PackageTurnRequest):
    """Run one turn of the data-package builder. Proposes; never writes.

    Returns ``{reply, patch, suggestions}``. Nothing here creates a package
    or writes a grant — see the module docstring for why that is a rule
    rather than a default.
    """
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    pool = _candidates()
    tables, groups = pool["tables"], pool["groups"]
    draft = (payload.draft or PackageDraft()).model_dump()

    if _stub_enabled():
        result: Dict[str, Any] = _stub_turn(message, draft, tables)
    else:
        try:
            result = await asyncio.to_thread(
                _llm_turn,
                _prompt(message=message, history=payload.history, draft=draft, tables=tables, groups=groups),
                _schema(tables, groups),
            )
        except ValueError as e:
            logger.warning("package builder: no LLM configured: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "builder_llm_unavailable",
                    "hint": "No AI credential is configured on this instance — "
                    "fill the package in by hand, or set one up in server config.",
                },
            ) from e
        except Exception as e:
            logger.warning("package builder: turn failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail={"kind": "builder_turn_failed", "hint": "The assistant could not answer. Try again."},
            ) from e

    patch = _sanitize_patch(
        result.get("patch"),
        table_ids={t["id"] for t in tables},
        group_ids={g["id"] for g in groups},
    )
    reply = result.get("reply")
    suggestions = result.get("suggestions")
    return {
        "reply": (reply if isinstance(reply, str) else "") or "Updated the draft.",
        "patch": patch,
        "suggestions": [s for s in (suggestions or []) if isinstance(s, str)][:3],
    }
