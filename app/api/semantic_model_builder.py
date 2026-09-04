"""One turn of the semantic-model builder's conversation.

The fifth builder-turn endpoint, after ``entity_builder``, ``agent_builder``,
``package_builder`` and ``mcp_builder``. Same shape as its siblings — a
message in, a sanitized patch out, model output treated as untrusted input —
built on the shared contract in ``app/api/builder_core.py``.

Two things make this one different from every sibling so far:

**It grounds against real data, not just real ids.** ``package_builder``
constrains a proposed group/table id to what the instance actually has; this
adapter does the same for a dataset's ``source`` (a registered table id) AND
goes one step further by grounding a dataset's *fields* against that table's
REAL columns (``app/api/v2_schema.py::build_schema``, the same RBAC-enforcing
lookup ``agnes schema`` uses). Never inventing a table path was already the
rule; never inventing a column name is the same rule one layer down, and it
also closes a real gap in the hand-edit panel this conversation replaces the
Start tab of — that panel always sent ``fields: []``.

**Its patch nests lists of objects.** ``package_builder``'s ``tables``/
``groups`` are flat id lists; a dataset or metric is a small object (name,
source, description, fields). A turn's ``patch.datasets``/``patch.metrics``
therefore carry only the entries this turn is ADDING or CHANGING — never an
echo of the whole list, the same "omit what you are not changing" contract
every adapter's schema already states, just extended to list items — and the
page merges each entry into its working draft BY NAME. Because
``builder_core.merged_draft`` does a flat ``dict.update`` (right for a scalar
field, wrong for a list you want upserted rather than replaced), this module
keeps its own :func:`_merged_draft_for_progress` so the ``slots`` a turn
reports describe the ACCUMULATED draft, not just this turn's delta.

Everything else follows the established shape: stateless (the draft travels
in the request, same as ``entity_builder`` — a model has no row until Save),
open to anyone signed in (matching ``POST /api/semantic-models/apply``'s own
asymmetry: a non-admin's document is still worth drafting even though Save
will queue it for review), and the sanitizer is the trust boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import duckdb
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
    slots_prompt_section,
    stub_enabled,
    turn_failure,
    turn_response,
)
from app.auth.dependencies import _get_db, get_current_user
from connectors.bigquery.access import BqAccess, get_bq_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/semantic-models", tags=["semantic-models"])

#: Fields a turn may propose. ``datasets``/``metrics`` are handled specially
#: (see module docstring) rather than as plain overwritable scalars.
PATCHABLE = ("name", "description", "datasets", "metrics")

MAX_NAME_CHARS = 120
#: Mirrors package_builder.MAX_CANDIDATES's reasoning: bounded so the prompt
#: stays bounded on a large instance.
MAX_CANDIDATES = 100
#: Column-level detail is a narrowing step, not part of the bulk table
#: block — see package_builder._candidates()'s own docstring for why that
#: split exists. A table this wide is rare; the cap is a backstop, not a
#: real limit on any table this grounds well.
MAX_COLUMNS_PER_TABLE = 60
_MAX_METRICS_PER_TABLE = 6


class DatasetFieldDraft(BaseModel):
    name: str = Field(default="")
    datatype: str = Field(default="")
    description: str = Field(default="")
    is_time: bool = Field(default=False)
    is_primary_key: bool = Field(default=False)


class DatasetDraft(BaseModel):
    name: str = Field(default="")
    source: str = Field(default="")
    description: str = Field(default="")
    fields: List[DatasetFieldDraft] = Field(default_factory=list)


class MetricDraft(BaseModel):
    name: str = Field(default="")
    expression: str = Field(default="")
    dialect: str = Field(default="duckdb")
    description: str = Field(default="")


class SemanticModelDraft(BaseModel):
    """The builder panel's unsaved state, as the page currently has it.

    Untrusted, used only to build the prompt — nothing here is written
    anywhere, and everything that comes back is re-validated on the way out.
    """

    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=4000)
    datasets: List[DatasetDraft] = Field(default_factory=list)
    metrics: List[MetricDraft] = Field(default_factory=list)


class SemanticModelTurnRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    history: List[BuilderMessage] = Field(default_factory=list)
    draft: Optional[SemanticModelDraft] = None


SYSTEM = """You are the builder inside Agnes, a governed data platform, helping
someone author a SEMANTIC MODEL — a document describing which datasets exist,
what their columns mean, and which metrics are computed from them, so agents
answer data questions consistently instead of guessing.

You lead. Work through what is still unknown, one thing at a time, in the
order you are given.

GROUNDING IS NOT OPTIONAL. You are given the instance's real registered
tables, and — once a dataset names one — that table's real columns. Only ever
propose a dataset `source` from the candidate tables you were given, and only
ever propose a field `name` from that table's real columns once you have
them. Never invent a table path or a column name; if nothing is registered
that matches, say so and ask, or leave it for the author to fill in by hand.

You are filling in a form the author can see and edit beside you. Every field
you write appears in their panel immediately; they can change or undo
anything. Never claim to have saved or published anything — you cannot.
Saving is a button only they can press.

Rules:
- Every turn must leave the panel further along. Filling something in from a
  real candidate and saying what you picked beats asking, whenever you can.
- One question per turn, at most, and only about the slot you were given.
- Never re-ask something already settled or already answered in the
  transcript.
- A plain pass-through field's expression is just its own column name.
- Metrics are optional — datasets alone are a useful model. Do not invent one
  unless the author asks for it.
- Write plainly. Two or three sentences. No headings, no bullet lists.
"""


def _has(draft: Dict[str, Any], key: str, *, chars: int = 1) -> bool:
    value = draft.get(key)
    return isinstance(value, str) and len(value.strip()) >= chars


def _named_datasets(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [d for d in (draft.get("datasets") or []) if isinstance(d, dict) and (d.get("name") or "").strip()]


_SLOTS: tuple = (
    Slot(
        key="datasets",
        label="what tables it describes",
        known=lambda d: any((ds.get("source") or "").strip() for ds in _named_datasets(d)),
        ask="which registered tables this model should describe. Propose them by id from the candidates given.",
    ),
    Slot(
        key="dataset_fields",
        label="the columns that matter",
        known=lambda d: bool(_named_datasets(d)) and all(bool(ds.get("fields")) for ds in _named_datasets(d)),
        ask=(
            "which columns matter for each dataset — dimensions, a time column, the primary key — grounded in "
            "the real columns you were given for that table."
        ),
    ),
    Slot(
        key="name",
        label="a name for the model",
        known=lambda d: _has(d, "name"),
        ask="what to call this model. Propose one from the datasets it covers; do not ask.",
    ),
    Slot(
        key="description",
        label="what an agent should know before using it",
        known=lambda d: _has(d, "description", chars=40),
        ask="the kind of question this model answers — what an agent reads before reaching for it.",
    ),
)


# ---------------------------------------------------------------------------
# Grounding: real tables, real columns — fetched server-side, RBAC-filtered
# ---------------------------------------------------------------------------


def _table_candidates(user: dict, conn: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """The registered tables THIS CALLER can read — never accepted from the
    caller (same reasoning as ``package_builder._candidates()``), and
    RBAC-filtered (unlike that admin-only sibling): a non-admin authoring a
    model must never be offered, or allowed to bind a dataset to, a table
    they cannot read.
    """
    from src.rbac import get_accessible_tables
    from src.repositories import metric_repo, table_registry_repo

    accessible = get_accessible_tables(user, conn)  # None => admin/all

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
        # Grounding is an accelerator, never a precondition — an instance
        # whose metric_definitions read fails must still get a turn.
        logger.debug("semantic model builder: metrics unavailable for grounding", exc_info=True)

    out: List[Dict[str, Any]] = []
    for row in table_registry_repo().list_all():
        table_id = str(row.get("id") or "")
        if not table_id:
            continue
        if accessible is not None and table_id not in accessible:
            continue
        table_name = str(row.get("name") or table_id)
        table_metrics = sorted(set(metrics_by_table.get(table_name) or metrics_by_table.get(table_id) or []))
        out.append(
            {
                "id": table_id,
                "name": table_name,
                "description": str(row.get("description") or "")[:160],
                "source_type": str(row.get("source_type") or ""),
                "query_mode": str(row.get("query_mode") or "local"),
                "metrics": table_metrics[:_MAX_METRICS_PER_TABLE],
                "metrics_total": len(table_metrics),
            }
        )
    return out


def _column_candidates(
    user: dict, conn: duckdb.DuckDBPyConnection, bq: BqAccess, table_id: str
) -> List[Dict[str, Any]]:
    """A table's real columns, RBAC-enforced by the same ``build_schema``
    ``agnes schema`` calls. Best-effort: a failure here costs grounding for
    that one dataset, never the turn."""
    from app.api.v2_schema import build_schema

    try:
        schema = build_schema(conn, user, table_id, bq=bq)
    except Exception:
        logger.debug("semantic model builder: column grounding unavailable for %s", table_id, exc_info=True)
        return []
    columns = schema.get("columns") if isinstance(schema, dict) else None
    if not isinstance(columns, list):
        return []
    out = []
    for c in columns[:MAX_COLUMNS_PER_TABLE]:
        if isinstance(c, dict) and c.get("name"):
            out.append(
                {
                    "name": str(c["name"]),
                    "type": str(c.get("type") or ""),
                    "description": str(c.get("description") or ""),
                }
            )
    return out


def _grounded_columns(
    user: dict, conn: duckdb.DuckDBPyConnection, bq: BqAccess, draft: Dict[str, Any], candidate_ids: set
) -> Dict[str, List[Dict[str, Any]]]:
    """Column candidates for every table the draft's datasets already name —
    a narrowing step over the shortlist, not part of the bulk table block
    (see ``package_builder._candidates()``'s own docstring for the same
    split)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ds in draft.get("datasets") or []:
        if not isinstance(ds, dict):
            continue
        source = str(ds.get("source") or "").strip()
        if source and source in candidate_ids and source not in out:
            out[source] = _column_candidates(user, conn, bq, source)
    return out


# ---------------------------------------------------------------------------
# Prompt + structured-output schema
# ---------------------------------------------------------------------------


def _prompt(
    *,
    message: str,
    history: List[BuilderMessage],
    draft: Dict[str, Any],
    tables: List[Dict[str, Any]],
    columns_by_table: Dict[str, List[Dict[str, Any]]],
) -> str:
    lines = ["The panel currently reads:"]
    lines.append(f"- name: {(draft.get('name') or '').strip() or '(empty)'}")
    lines.append(f"- description: {(draft.get('description') or '').strip() or '(empty)'}")

    datasets = [d for d in (draft.get("datasets") or []) if isinstance(d, dict)]
    if datasets:
        lines.append("- datasets:")
        for ds in datasets:
            fields = [f.get("name") for f in (ds.get("fields") or []) if isinstance(f, dict) and f.get("name")]
            lines.append(
                f"  - {ds.get('name') or '(unnamed)'}: source={ds.get('source') or '(none yet)'} "
                f"fields=[{', '.join(fields) or 'none yet'}]"
            )
    else:
        lines.append("- datasets: (none yet)")

    metrics = [m for m in (draft.get("metrics") or []) if isinstance(m, dict)]
    if metrics:
        lines.append("- metrics:")
        for m in metrics:
            lines.append(f"  - {m.get('name') or '(unnamed)'}: {m.get('expression') or '(no expression yet)'}")
    else:
        lines.append("- metrics: (none yet — optional, datasets alone are a useful model)")

    lines.append("")
    lines.append("Registered tables you may bind a dataset to (never propose one not listed here):")
    for t in tables[:MAX_CANDIDATES]:
        facts = [f"id={t['id']}", f"name={t['name']}"]
        if t.get("source_type"):
            facts.append(f"source={t['source_type']}")
        if t.get("metrics"):
            shown = ",".join(t["metrics"])
            hidden = int(t.get("metrics_total") or len(t["metrics"])) - len(t["metrics"])
            facts.append(f"metrics={shown}" + (f"(+{hidden} more)" if hidden > 0 else ""))
        lines.append("- " + " ".join(facts) + f" — {t['description'] or 'no description'}")
    if len(tables) > MAX_CANDIDATES:
        lines.append(f"…and {len(tables) - MAX_CANDIDATES} more not listed. Ask rather than guessing.")
    if not tables:
        lines.append("(none registered, or none this caller can read — build fields from what the author tells you.)")

    for table_id, cols in columns_by_table.items():
        lines.append("")
        lines.append(f"Real columns of {table_id} (never propose a field name not listed here):")
        if cols:
            for c in cols:
                suffix = f" — {c['description']}" if c.get("description") else ""
                lines.append(f"- {c['name']} ({c['type'] or 'unknown type'}){suffix}")
        else:
            lines.append("(column list unavailable right now — ask the author, or leave fields for them to fill in.)")

    lines += slots_prompt_section(_SLOTS, draft)
    lines += history_prompt_section(history)
    lines.append("")
    lines.append(OPENING_JOB if is_opening_turn(message, history) else f"Author: {message}")
    return "\n".join(lines)


def _schema(table_ids: List[str]) -> Dict[str, Any]:
    source_prop: Dict[str, Any] = {"type": "string", "description": "a registered table id from the candidates"}
    if table_ids:
        source_prop["enum"] = table_ids

    field_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "a real column name from that table's candidates"},
            "datatype": {"type": "string"},
            "description": {"type": "string"},
            "is_time": {"type": "boolean"},
            "is_primary_key": {"type": "boolean"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    dataset_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "source": source_prop,
            "description": {"type": "string"},
            "fields": {"type": "array", "items": field_schema},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    metric_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "expression": {"type": "string"},
            "dialect": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "What to say to the author."},
            "patch": {
                "type": "object",
                "description": (
                    "Fields to change this turn. Omit what you are not changing. `datasets`/`metrics` carry "
                    "only NEW or CHANGED entries, matched by name — never echo the whole list back."
                ),
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "datasets": {"type": "array", "items": dataset_schema},
                    "metrics": {"type": "array", "items": metric_schema},
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


# ---------------------------------------------------------------------------
# The sanitizer — the trust boundary
# ---------------------------------------------------------------------------


def _sanitize_field(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    out: Dict[str, Any] = {"name": name.strip()[:MAX_NAME_CHARS]}
    if isinstance(raw.get("datatype"), str):
        out["datatype"] = raw["datatype"].strip()[:60]
    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"].strip()[:2000]
    if isinstance(raw.get("is_time"), bool):
        out["is_time"] = raw["is_time"]
    if isinstance(raw.get("is_primary_key"), bool):
        out["is_primary_key"] = raw["is_primary_key"]
    return out


def _sanitize_dataset(raw: Any, *, table_ids: set) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    out: Dict[str, Any] = {"name": name.strip()[:MAX_NAME_CHARS]}

    source = raw.get("source")
    if isinstance(source, str) and source.strip():
        source = source.strip()
        # A source not among the real candidates is dropped WHOLE, not kept
        # with a bad path — unless nothing was offered to ground against (no
        # registered tables this caller can read), where grounding degrades
        # to "unavailable" rather than blocking authoring by hand.
        if table_ids and source not in table_ids:
            return None
        out["source"] = source

    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"].strip()[:2000]

    fields = raw.get("fields")
    if isinstance(fields, list):
        cleaned = [f for f in (_sanitize_field(item) for item in fields) if f is not None]
        if cleaned:
            out["fields"] = cleaned
    return out


def _sanitize_metric(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    out: Dict[str, Any] = {"name": name.strip()[:MAX_NAME_CHARS]}
    if isinstance(raw.get("expression"), str):
        out["expression"] = raw["expression"].strip()[:2000]
    if isinstance(raw.get("dialect"), str) and raw["dialect"].strip():
        out["dialect"] = raw["dialect"].strip()[:40]
    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"].strip()[:2000]
    return out


def _sanitize_patch(raw: Any, *, table_ids: set) -> Dict[str, Any]:
    """Model output is untrusted input. Unknown keys are dropped; every
    dataset/metric entry is validated on its own, and a bad one is dropped
    whole rather than partially kept."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    if isinstance(raw.get("name"), str):
        out["name"] = raw["name"].strip()[:MAX_NAME_CHARS]
    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"].strip()[:4000]

    datasets = raw.get("datasets")
    if isinstance(datasets, list):
        cleaned = [d for d in (_sanitize_dataset(item, table_ids=table_ids) for item in datasets) if d is not None]
        if cleaned:
            out["datasets"] = cleaned

    metrics = raw.get("metrics")
    if isinstance(metrics, list):
        cleaned_m = [m for m in (_sanitize_metric(item) for item in metrics) if m is not None]
        if cleaned_m:
            out["metrics"] = cleaned_m

    return out


# ---------------------------------------------------------------------------
# Progress: the ACCUMULATED draft, not the delta
# ---------------------------------------------------------------------------


def _keyed_upsert(existing: List[Dict[str, Any]], delta: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge ``delta`` entries into ``existing`` by ``name`` — the same
    algorithm the page performs on the patch it receives. Used here only to
    compute what the draft will look like AFTER this patch, so the ``slots``
    a turn reports describe the state this turn produced rather than the
    delta alone."""
    out = [dict(e) for e in existing if isinstance(e, dict)]
    by_name = {str(e.get("name") or ""): i for i, e in enumerate(out) if e.get("name")}
    for item in delta:
        key = str(item.get("name") or "")
        if key and key in by_name:
            out[by_name[key]] = {**out[by_name[key]], **item}
        else:
            out.append(item)
    return out


def _merged_draft_for_progress(draft: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deliberately NOT ``builder_core.merged_draft`` — that does a flat
    ``dict.update``, which is right for a scalar field and wrong for a list
    the caller means to upsert into rather than replace wholesale."""
    merged = dict(draft)
    for key, value in patch.items():
        if key in ("datasets", "metrics"):
            continue
        merged[key] = value
    if "datasets" in patch:
        merged["datasets"] = _keyed_upsert(draft.get("datasets") or [], patch["datasets"])
    if "metrics" in patch:
        merged["metrics"] = _keyed_upsert(draft.get("metrics") or [], patch["metrics"])
    return merged


# ---------------------------------------------------------------------------
# The stub — a deterministic stand-in, never invents an ungrounded source
# ---------------------------------------------------------------------------


def _stub_turn(message: str, draft: Dict[str, Any], tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not message.strip():
        from app.api.builder_core import open_slots

        still_open = open_slots(_SLOTS, draft)
        first = still_open[0].label if still_open else None
        return {
            "reply": (
                f"[stub] Let's build a semantic model. First thing I need: {first}."
                if first
                else "[stub] Let's build a semantic model. Tell me what it describes."
            ),
            "patch": {},
            "suggestions": [],
        }

    patch: Dict[str, Any] = {}
    if not _named_datasets(draft):
        words = {w.lower() for w in message.split() if len(w) > 3}
        hit = next((t for t in tables if any(w in t["name"].lower() for w in words)), None)
        if hit:
            patch["datasets"] = [{"name": hit["name"], "source": hit["id"], "description": message.strip()[:160]}]
    if not (draft.get("name") or "").strip():
        patch["name"] = (message.strip()[:60] or "untitled").lower().replace(" ", "_")
    if not (draft.get("description") or "").strip() and len(message.strip()) >= 20:
        patch["description"] = message.strip()[:400]

    return {
        "reply": "[stub] Drafted that into the panel. Edit anything on the right, or tell me what to change.",
        "patch": patch,
        "suggestions": ["Add a metric for revenue", "Describe the primary key", "Use daily grain"],
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
        max_tokens=3000,
        json_schema=schema,
        schema_name="semantic_model_builder_turn",
        system=SYSTEM,
    )


@router.post("/builder/turn")
async def semantic_model_builder_turn(
    payload: SemanticModelTurnRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    """Run one turn of the semantic-model builder. Proposes; never writes.

    Stateless like ``entity_builder`` — a model has no row until *Save*
    (``POST /api/semantic-models/apply``), so the draft travels in the
    request and nothing here reads or writes ``semantic_models``. Open to
    anyone signed in: an admin's eventual Save publishes, anyone else's
    queues for review, but drafting is not itself gated on that.
    """
    message = (payload.message or "").strip()
    if not message and not is_opening_turn(message, payload.history):
        raise HTTPException(status_code=400, detail={"kind": "empty_message"})

    draft = (payload.draft or SemanticModelDraft()).model_dump()
    tables = _table_candidates(user, conn)
    candidate_ids = {t["id"] for t in tables}
    columns_by_table = _grounded_columns(user, conn, bq, draft, candidate_ids)

    engine = ENGINE_STUB if stub_enabled() else ENGINE_MODEL
    if engine == ENGINE_STUB:
        result: Dict[str, Any] = _stub_turn(message, draft, tables)
    else:
        prompt = _prompt(
            message=message,
            history=payload.history,
            draft=draft,
            tables=tables,
            columns_by_table=columns_by_table,
        )
        schema = _schema(sorted(candidate_ids))
        try:
            result = await asyncio.to_thread(_llm_turn, prompt, schema)
        except ValueError as e:
            logger.warning("semantic model builder: no LLM configured: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "builder_llm_unavailable",
                    "hint": "No AI credential is configured on this instance — "
                    "fill the model in by hand, or ask an admin to set one up.",
                },
            ) from e
        except Exception as e:
            raise turn_failure(e, label="semantic model builder") from e

    patch = _sanitize_patch(result.get("patch"), table_ids=candidate_ids)
    return turn_response(
        result,
        patch=patch,
        engine=engine,
        slots=_SLOTS,
        draft=_merged_draft_for_progress(draft, patch),
        fallback_reply="Updated the draft.",
    )
