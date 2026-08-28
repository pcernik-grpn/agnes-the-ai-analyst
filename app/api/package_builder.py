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
    slots_prompt_section,
    stub_enabled,
    turn_response,
)
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

MAX_NAME_CHARS = 120
#: Candidate lists sent to the model, capped so the prompt stays bounded. A
#: large instance can have thousands of registered tables.
MAX_CANDIDATES = 120
#: Metric names carried per candidate table. A warehouse table can back dozens;
#: a handful is enough to say what it is for, and the count travels with them.
_MAX_METRICS_PER_TABLE = 6


#: What a package needs before an admin can sensibly press Create.
#: `groups` is deliberately absent: a private package is a legitimate final
#: state ("leave this closed and the package is private until you share it"),
#: so a slot for it could never be settled — and this is the one builder where
#: an unsettled slot would be nagging an admin toward writing a grant.
_SLOTS = (
    Slot(
        key="contents",
        label="what data it carries",
        known=lambda d: bool(d.get("tables")),
        ask="which registered tables belong in it. Propose them by id from the candidates above.",
    ),
    Slot(
        key="name",
        label="a name",
        known=lambda d: isinstance(d.get("name"), str) and len(d["name"].strip()) > 0,
        ask="what it should be called — name it after what it carries. Propose one; do not ask.",
    ),
    Slot(
        key="description",
        label="what an analyst reads",
        known=lambda d: isinstance(d.get("description"), str) and len(d["description"].strip()) >= 40,
        ask="what is in here and who it is for, as the analyst deciding whether to add it will read it.",
    ),
)


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


def _candidates() -> Dict[str, List[Dict[str, Any]]]:
    """The tables and groups this instance actually has.

    Fetched server-side on purpose — see the module docstring. A caller
    cannot enlarge the set of groups a turn is allowed to propose.

    Each table carries the few facts that decide whether it BELONGS in a
    package rather than merely whether its name matches the ask. Before this,
    a candidate was `id`, `name` and 160 characters of description, so a
    request like "the opportunity tables for sales" could only ever be
    answered by string-matching names — a table whose name does not say what
    it holds was invisible, and the package came out wrong or empty.

    What is added is what costs nothing extra to know:

    - `source_type` / `query_mode` come free off the registry row.
    - `distributable` folds the manifest rule into one boolean (below).
    - `metrics` names what this instance already computes over the table.
      That is the strongest signal available for what a table is actually
      about when its description is thin, and it is one bulk read for the
      whole list rather than one per table.

    Column-level detail is deliberately NOT here: it is a read per table and
    the list is capped at ``MAX_CANDIDATES``, so schemas belong to a
    narrowing step that asks about a shortlist, not to this bulk block.
    """
    from src.repositories import metric_repo, table_registry_repo, user_groups_repo

    # Metric rows key on the registry NAME, via `table_name` or the `tables`
    # list. Best-effort: an instance whose metric_definitions table does not
    # exist yet (or whose read fails) must still get a turn — grounding is an
    # accelerator here, never a precondition.
    metrics_by_table: Dict[str, List[str]] = {}
    try:
        for m in metric_repo().list():
            metric_name = str(m.get("name") or "").strip()
            if not metric_name:
                continue
            for target in [m.get("table_name"), *(m.get("tables") or [])]:
                if target:
                    metrics_by_table.setdefault(str(target), []).append(metric_name)
    except Exception:
        logger.debug("package builder: metrics unavailable for grounding", exc_info=True)

    tables: List[Dict[str, Any]] = []
    for row in table_registry_repo().list_all():
        table_id = str(row.get("id") or "")
        table_name = str(row.get("name") or table_id)
        query_mode = str(row.get("query_mode") or "local")
        table_metrics = sorted(set(metrics_by_table.get(table_name) or metrics_by_table.get(table_id) or []))
        tables.append(
            {
                "id": table_id,
                "name": table_name,
                "description": str(row.get("description") or "")[:160],
                "source_type": str(row.get("source_type") or ""),
                "query_mode": query_mode,
                # A package is how governed data reaches an analyst's laptop,
                # and only these modes appear in the manifest `agnes pull`
                # reads (``app/api/data.py::_DISTRIBUTABLE_QUERY_MODES``).
                # Packaging a remote/server_only row is still ALLOWED —
                # data_packages.py does not refuse it — so this is a fact for
                # the model to weigh, never a rule it must obey. Encoding it
                # as a prohibition would be wrong about the API.
                "distributable": query_mode in ("local", "materialized") and not bool(row.get("server_only")),
                # Capped for prompt size, but the cap announces itself in the
                # rendered block via `metrics_total` — a silently truncated
                # list would read to the model as "these are all of them",
                # which is the same failure MAX_CANDIDATES already avoids.
                "metrics": table_metrics[:_MAX_METRICS_PER_TABLE],
                "metrics_total": len(table_metrics),
            }
        )
    groups: List[Dict[str, Any]] = [
        {"id": str(row.get("id") or ""), "name": str(row.get("name") or "")} for row in user_groups_repo().list_all()
    ]
    return {"tables": tables, "groups": groups}


def _schema(tables: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> Dict[str, Any]:
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
                "description": SUGGESTIONS_DESCRIPTION,
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
    tables: List[Dict[str, Any]],
    groups: List[Dict[str, Any]],
) -> str:
    lines = ["The drawer currently reads:"]
    lines.append(f"- name: {(draft.get('name') or '').strip() or '(empty)'}")
    lines.append(f"- description: {(draft.get('description') or '').strip() or '(empty)'}")
    lines.append(f"- tables: {', '.join(draft.get('tables') or []) or '(none yet)'}")
    lines.append(f"- groups: {', '.join(draft.get('groups') or []) or '(none yet)'}")
    lines.append("")
    lines.append("Registered tables you may include:")
    for t in tables[:MAX_CANDIDATES]:
        facts = [f"id={t['id']}", f"name={t['name']}"]
        if t.get("source_type"):
            facts.append(f"source={t['source_type']}")
        facts.append(f"mode={t.get('query_mode') or 'local'}")
        if not t.get("distributable", True):
            facts.append("NOT-synced-to-laptops")
        if t.get("metrics"):
            shown = ",".join(t["metrics"])
            hidden = int(t.get("metrics_total") or len(t["metrics"])) - len(t["metrics"])
            facts.append(f"metrics={shown}" + (f"(+{hidden} more)" if hidden > 0 else ""))
        lines.append("- " + " ".join(facts) + f" — {t['description'] or 'no description'}")
    if len(tables) > MAX_CANDIDATES:
        lines.append(f"…and {len(tables) - MAX_CANDIDATES} more not listed. Ask rather than guessing.")
    if tables:
        # Only advise about facts that are actually above this line: on an
        # instance with nothing registered the block would otherwise explain
        # how to read a list that is not there.
        lines.append(
            "Read those facts before matching on the name: `metrics=` says what this "
            "instance already computes over a table, which is often what the table is "
            "for. A table marked NOT-synced-to-laptops lives only on the server — "
            "packaging it is allowed, but analysts get no local copy of it, so prefer "
            "a synced table unless the admin asked for that one."
        )
    lines.append("")
    lines.append("Groups you may grant to:")
    for g in groups[:MAX_CANDIDATES]:
        lines.append(f"- id={g['id']} name={g['name']}")
    lines += slots_prompt_section(_SLOTS, draft)
    lines += history_prompt_section(history)
    lines.append("")
    lines.append(OPENING_JOB if is_opening_turn(message, history) else f"Admin: {message}")
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


def _stub_turn(message: str, draft: Dict[str, Any], tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not message.strip():
        from app.api.builder_core import open_slots

        still_open = open_slots(_SLOTS, draft)
        first = still_open[0].label if still_open else None
        return {
            "reply": (
                f"[stub] Let's build a data package. First thing I need: {first}."
                if first
                else "[stub] Let's build a data package. Tell me what it should carry."
            ),
            "patch": {},
            "suggestions": [],
        }
    return _stub_turn_body(message, draft, tables)


def _stub_turn_body(message: str, draft: Dict[str, Any], tables: List[Dict[str, Any]]) -> Dict[str, Any]:
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
    # An empty message on the FIRST turn is the builder opening the
    # conversation. Later it is a client bug.
    if not message and not is_opening_turn(message, payload.history):
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    pool = _candidates()
    tables, groups = pool["tables"], pool["groups"]
    draft = (payload.draft or PackageDraft()).model_dump()

    engine = ENGINE_STUB if stub_enabled() else ENGINE_MODEL
    if engine == ENGINE_STUB:
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
    return turn_response(
        result,
        patch=patch,
        engine=engine,
        slots=_SLOTS,
        draft=merged_draft(draft, patch),
        fallback_reply="Updated the draft.",
    )
