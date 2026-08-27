"""Read-only view helpers for the semantic-layer browse UI (wave 4.2 of the
2026-08-14 UI/agent-parity design).

Pure functions over one stored ``semantic_models`` row's ``document_json`` —
no DB access, no HTTP, nothing mutated. Mirrors the read-only posture of
``src/semantic_context.py`` / ``src/semantic_validation.py``: every surface
(the three ``/semantic-layer*`` routes in ``app/web/router.py``) wraps these
functions rather than re-deriving the same reads inline.

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 1, "UI"). This module renders; it never writes back to a document —
editing is a later increment.
"""

from __future__ import annotations

import json
from typing import Optional

# custom_extensions[].vendor_name Agnes rides its own concepts under. Compared
# casefolded here — matching the query validator (src/semantic_validation.py::
# extract_constraints) and the projector (src/semantic/projection.py::
# _is_agnes_vendor), which all read the tag case-insensitively. The Keboola
# adapter emits the canonical `AGNES`; any casing resolves everywhere.
_AGNES_VENDOR = "agnes"

# The object types this browse UI knows how to list/drill into, and the
# document key each lives under (mirrors src/semantic_context.py's
# SEMANTIC_TYPES for dataset/metric/relationship, extended with the two
# Agnes-only concepts that ride custom_extensions instead of a core Ossie
# slot: constraints and glossary).
OBJECT_TYPE_LABELS: dict[str, str] = {
    "dataset": "Dataset",
    "metric": "Metric",
    "relationship": "Relationship",
    "constraint": "Constraint",
    "glossary": "Glossary term",
}

# tab query-param value each object type's model-detail tab lives under.
OBJECT_TYPE_TAB: dict[str, str] = {
    "dataset": "datasets",
    "metric": "metrics",
    "relationship": "relationships",
    "constraint": "constraints",
    "glossary": "glossary",
}

# ai_context / ai block groups this UI always renders, in this order — even
# when a group is empty. anti_keywords has no current producer anywhere in
# the import pipeline (see docstring on `ai_groups` below); it still renders,
# because it is the one negative signal the design spec calls out as "must be
# rendered, not dropped" rather than silently omitted from the layout.
AI_GROUPS = ("keywords", "synonyms", "anti_keywords", "hints", "warnings")

# Friendly labels for the `source` values a stored semantic_models row
# carries (src/semantic/transports.py::import_source,
# connectors/keboola/semantic_layer.py::_store_ossie_documents). Anything not
# listed here (including an unrecognized future source) falls back to the raw
# value — never hidden.
_SOURCE_LABELS: dict[str, str] = {
    "manual": "Native",
    "keboola_metastore": "Keboola",
    "ossie_git": "Git",
    "ossie_upload": "Upload",
    "ossie_connection": "Connection",
}


def source_label(source: Optional[str]) -> str:
    """Human label for a stored row's ``source`` column."""
    key = (source or "manual").strip()
    return _SOURCE_LABELS.get(key, key or "Native")


def is_imported(source: Optional[str]) -> bool:
    """Whether ``source`` marks a row as owned by a sync rather than
    hand-authored — the read-only rule (design spec, "Read-only rule"):
    only ``'manual'`` is native/editable in a later increment; everything
    else shows the "Imported from …" badge and (in THIS increment) no page
    ever offers an edit affordance at all."""
    return (source or "manual") != "manual"


def agnes_extension_payload(obj: dict) -> dict:
    """The merged AGNES ``custom_extensions`` payload of one object (model,
    dataset, metric, relationship, …).

    Mirrors ``src/semantic/projection.py::_agnes_payload`` — duplicated
    rather than imported: that helper is private to the projector module,
    and this is a read-only view concern, not a projection one. ``data`` is a
    JSON-encoded string per the schema; an entry that fails to parse is
    skipped rather than raised, same posture as every other reader of this
    extension in the codebase.
    """
    merged: dict = {}
    for ext in obj.get("custom_extensions") or []:
        if not isinstance(ext, dict):
            continue
        vendor = ext.get("vendor_name")
        if not isinstance(vendor, str) or vendor.casefold() != _AGNES_VENDOR:
            continue
        raw = ext.get("data")
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                continue
        elif isinstance(raw, dict):
            data = raw
        else:
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def model_of(row: dict) -> dict:
    """The stored row's ``document_json`` unwrapped into one browsable model —
    ``{}`` when the row has no parsed document.

    A stored row's ``document_json`` is ``{"semantic_model": [...]}`` (the
    Ossie top level), never a model dict itself. A document can declare more
    than one model; this AGGREGATES every entry into one view — datasets,
    metrics, relationships and ``custom_extensions`` (constraints/glossary)
    concatenated in document order — so a multi-model row shows all of its
    objects, matching what ``app/api/semantic_models.py::_accessible_valid_
    documents`` (and therefore the REST/CLI/MCP read tools) flatten for the
    same row. ``name``/``description`` come from the first model for display;
    the browse UI keys rows by the stored row's own ``slug``/``name`` anyway.
    """
    doc = row.get("document_json") or {}
    if not isinstance(doc, dict):
        return {}
    models = [m for m in (doc.get("semantic_model") or []) if isinstance(m, dict)]
    if not models:
        return {}
    if len(models) == 1:
        return models[0]

    merged: dict = {}
    # First-model scalars for display (name/description/etc.); later models'
    # object lists are concatenated onto them below.
    merged.update(models[0])
    for key in ("datasets", "metrics", "relationships", "custom_extensions"):
        combined: list = []
        for m in models:
            values = m.get(key)
            if isinstance(values, list):
                combined.extend(values)
        merged[key] = combined
    return merged


def model_dialects(model: dict) -> list[str]:
    """Distinct SQL dialect labels declared across the model's metrics'
    expressions, first-seen order.

    A model carries no dialect field of its own in the vendored Ossie schema
    — ``connectors/keboola/semantic_ossie.py`` resolves one only to COMPOSE
    each field/metric expression at import time, never stores it back onto
    the model — so this is derived from what the metrics actually declare.
    """
    seen: dict[str, None] = {}
    for metric in model.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        expression = metric.get("expression")
        if not isinstance(expression, dict):
            continue
        for dialect_entry in expression.get("dialects") or []:
            if isinstance(dialect_entry, dict) and dialect_entry.get("dialect"):
                seen.setdefault(str(dialect_entry["dialect"]), None)
    return list(seen)


def model_constraints(model: dict) -> list[dict]:
    """Constraints riding the model's AGNES ``custom_extensions`` — the core
    Ossie schema has no constraint slot (see
    ``src/semantic_validation.py::extract_constraints``, the same read).

    Iterates the extension entries and EXTENDS (like :func:`model_glossary`)
    rather than reading the merged payload, so a model carrying more than one
    AGNES extension — which :func:`model_of` produces when it aggregates a
    multi-model document — contributes every extension's constraints instead
    of only the last one a dict ``update`` would keep.
    """
    out: list[dict] = []
    for ext in model.get("custom_extensions") or []:
        if not isinstance(ext, dict):
            continue
        vendor = ext.get("vendor_name")
        if not isinstance(vendor, str) or vendor.casefold() != _AGNES_VENDOR:
            continue
        raw = ext.get("data")
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else None
            except ValueError:
                continue
        elif isinstance(raw, dict):
            data = raw
        else:
            continue
        constraints = data.get("constraints") if isinstance(data, dict) else None
        if isinstance(constraints, list):
            out.extend(c for c in constraints if isinstance(c, dict) and c.get("name"))
    return out


def model_glossary(model: dict) -> list[dict]:
    """Glossary entries riding the model's AGNES ``custom_extensions`` —
    mirrors ``src/semantic/projection.py::_glossary_entries``, duplicated for
    the same private-helper reason as :func:`agnes_extension_payload`."""
    entries: list[dict] = []
    for ext in model.get("custom_extensions") or []:
        if not isinstance(ext, dict):
            continue
        vendor = ext.get("vendor_name")
        if not isinstance(vendor, str) or vendor.casefold() != _AGNES_VENDOR:
            continue
        raw = ext.get("data")
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else None
            except ValueError:
                continue
        elif isinstance(raw, dict):
            data = raw
        else:
            continue
        glossary = data.get("glossary") if isinstance(data, dict) else None
        if isinstance(glossary, list):
            entries.extend(g for g in glossary if isinstance(g, dict) and g.get("term"))
    return entries


def warehouse_only_metric_count(model: dict) -> int:
    """How many of the model's metrics project into ``metric_definitions``
    with only a warehouse-specific dialect (SNOWFLAKE-only, DATABRICKS-only,
    etc.) rather than DUCKDB/ANSI_SQL — the same check
    ``src/semantic/projection.py`` makes at write time, recomputed here since
    a stored ``semantic_models`` row carries no column of its own to persist
    it in.

    These metrics ARE in the catalog (unlike a genuinely skipped metric —
    e.g. one with no expression at all); they just cannot run through a
    local DuckDB query and need server-side (remote/materialized) execution
    instead.
    """
    from src.semantic.dialect import count_warehouse_only_metrics

    return count_warehouse_only_metrics(model.get("metrics") or [])


def object_counts(model: dict) -> dict[str, int]:
    """Per-type object counts for the model-list row."""
    return {
        "datasets": len(model.get("datasets") or []),
        "metrics": len(model.get("metrics") or []),
        "constraints": len(model_constraints(model)),
        "relationships": len(model.get("relationships") or []),
        "glossary": len(model_glossary(model)),
    }


def ai_groups(obj: dict) -> dict[str, list[str]]:
    """The five-group AI-context read-out for one dataset/metric/relationship
    object: keywords, synonyms, anti_keywords, hints, warnings.

    ``ai_context`` is Ossie's typed slot (a bare string, or an object with
    ``instructions``/``synonyms``/``examples`` plus ``additionalProperties:
    true`` — ``src/semantic/schema/osi-schema.json``'s ``AIContext`` def), so
    a document MAY declare any of the five group names directly as extra
    keys there. The one real producer today — the Keboola metastore adapter
    (``connectors/keboola/semantic_ossie.py::_compose_dataset``) — instead
    rides ``keywords`` on the AGNES ``custom_extensions`` payload, since the
    vendored ``Dataset`` schema has no top-level slot for it
    (``additionalProperties: false``); that fallback is checked too.
    ``anti_keywords`` has no current producer anywhere in the import
    pipeline — it still renders (empty when absent), the one negative signal
    the design spec calls out as "must be rendered, not dropped".
    """
    ai_context = obj.get("ai_context")
    base = ai_context if isinstance(ai_context, dict) else {}
    extension = agnes_extension_payload(obj)

    out: dict[str, list[str]] = {}
    for group in AI_GROUPS:
        values = base.get(group)
        if not isinstance(values, list) and group == "keywords":
            values = extension.get("keywords")
        out[group] = [str(v) for v in values if isinstance(v, (str, int, float))] if isinstance(values, list) else []
    return out


def ai_instructions_and_examples(obj: dict) -> tuple[Optional[str], list[str]]:
    """The two ``ai_context`` fields the core Ossie schema DOES define
    (``instructions``, ``examples``) — rendered alongside the five groups
    above so real imported content (the Keboola adapter folds its ``hints``
    + ``warnings`` into ``instructions``, see ``_compose_ai_context``) is
    never silently dropped just because it doesn't fit one of the five
    named groups."""
    ai_context = obj.get("ai_context")
    if isinstance(ai_context, str):
        return (ai_context.strip() or None), []
    if not isinstance(ai_context, dict):
        return None, []
    instructions = ai_context.get("instructions")
    instructions = instructions.strip() if isinstance(instructions, str) and instructions.strip() else None
    examples = ai_context.get("examples")
    examples = [str(e) for e in examples] if isinstance(examples, list) else []
    return instructions, examples


def dataset_field_rows(dataset: dict) -> list[dict]:
    """A dataset's ``fields[]`` as Name/Type/Role/Description rows.

    Role: "Primary key" for a field named in the dataset's ``primary_key``,
    "Time dimension" for ``dimension.is_time: true``, "Dimension" for any
    other populated ``dimension`` block, "Field" otherwise.
    """
    primary_key = {str(k) for k in (dataset.get("primary_key") or []) if k}
    rows: list[dict] = []
    for field in dataset.get("fields") or []:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        name = str(field["name"])
        dimension = field.get("dimension")
        if name in primary_key:
            role = "Primary key"
        elif isinstance(dimension, dict) and dimension.get("is_time"):
            role = "Time dimension"
        elif dimension:
            role = "Dimension"
        else:
            role = "Field"
        rows.append(
            {
                "name": name,
                "datatype": field.get("datatype") or "—",
                "role": role,
                "description": field.get("description") or "",
            }
        )
    return rows


def metric_expressions(metric: dict) -> list[dict]:
    """A metric's declared ``{dialect, expression}`` pairs, in document
    order — the SQL fragment(s) an object-detail page renders."""
    expression = metric.get("expression")
    if not isinstance(expression, dict):
        return []
    dialects = expression.get("dialects")
    if not isinstance(dialects, list):
        return []
    return [
        {"dialect": str(d.get("dialect")), "expression": str(d.get("expression") or "")}
        for d in dialects
        if isinstance(d, dict) and d.get("dialect")
    ]


def find_object(model: dict, object_type: str, name: str) -> Optional[dict]:
    """Look up one object of ``object_type`` by its display name (or, for
    glossary, its ``term``) — case-insensitive, matching every other name
    join over imported document text in this codebase."""
    if object_type == "dataset":
        pool, key = model.get("datasets") or [], "name"
    elif object_type == "metric":
        pool, key = model.get("metrics") or [], "name"
    elif object_type == "relationship":
        pool, key = model.get("relationships") or [], "name"
    elif object_type == "constraint":
        pool, key = model_constraints(model), "name"
    elif object_type == "glossary":
        pool, key = model_glossary(model), "term"
    else:
        return None
    needle = name.casefold()
    for obj in pool:
        if isinstance(obj, dict) and str(obj.get(key) or "").casefold() == needle:
            return obj
    return None


def object_id(object_type: str, name: str) -> str:
    """Build the single-path-segment ``{object_id}`` the object-detail route
    expects: ``"<type>:<name>"``."""
    return f"{object_type}:{name}"


__all__ = [
    "AI_GROUPS",
    "OBJECT_TYPE_LABELS",
    "OBJECT_TYPE_TAB",
    "agnes_extension_payload",
    "ai_groups",
    "ai_instructions_and_examples",
    "dataset_field_rows",
    "find_object",
    "is_imported",
    "metric_expressions",
    "model_constraints",
    "model_dialects",
    "model_glossary",
    "model_of",
    "object_counts",
    "object_id",
    "source_label",
    "warehouse_only_metric_count",
]
