"""Storage-independent core of the agent read-parity tools ``get_semantic_context``
and ``get_semantic_schema``.

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 4, "Agent read tools and skill"). Mirrors the upstream vendor
assistant's tool contract of the same names.

Both functions are pure -- no DB access, no HTTP, no import from ``app.`` or
``src.repositories`` -- and operate over the same document shape
``src/semantic_validation.py`` documents (a single ``semantic_model`` entry,
i.e. what will become one ``semantic_models.document_json["semantic_model"][i]``
row). Every surface (REST/CLI/MCP) wraps these functions rather than
duplicating the logic; the RBAC filtering (which documents a caller may read)
and the model_ids restriction both happen in the wiring layer, before these
functions ever see a document.

``get_semantic_schema`` is deliberately its own module-level function rather
than a method on the validator: it reflects the vendored Apache Ossie JSON
Schema (``src/semantic/document_validation.py``), not a document instance, so
it takes no ``documents`` argument at all.
"""

from __future__ import annotations

from typing import Any

# Public API type name -> the document key that type's objects live under in
# a semantic_model document (src/semantic_validation.py's documented shape).
SEMANTIC_TYPES: dict[str, str] = {
    "dataset": "datasets",
    "metric": "metrics",
    "relationship": "relationships",
}

# Public API type name -> the vendored Ossie JSON Schema ``$defs`` entry it
# corresponds to (src/semantic/document_validation.py). Kept as its own
# mapping (rather than derived from SEMANTIC_TYPES) since the two vocabularies
# -- "what's inside a document" vs "what's inside the schema" -- are only
# coincidentally the same set today.
_SCHEMA_DEF_NAMES: dict[str, str] = {
    "dataset": "Dataset",
    "metric": "Metric",
    "relationship": "Relationship",
}

_SUMMARY_MAX_CHARS = 160


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    return text[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _summary(obj: dict[str, Any]) -> str:
    """A short human-readable summary for the COMPACT form: the object's own
    ``description`` when present, else its ``ai_context`` (a bare string, or
    the ``instructions`` field of the structured form), else empty."""
    description = obj.get("description")
    if isinstance(description, str) and description.strip():
        return _truncate(description)
    ai_context = obj.get("ai_context")
    if isinstance(ai_context, str) and ai_context.strip():
        return _truncate(ai_context)
    if isinstance(ai_context, dict):
        instructions = ai_context.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            return _truncate(instructions)
    return ""


def get_semantic_context(
    documents: list[dict[str, Any]],
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve typed ``selections`` against ``documents``.

    ``documents`` is a list of semantic-model documents already filtered to
    the ones the caller may read and to any ``model_ids`` restriction (the
    wiring layer's job, same split as ``validate_query``'s ``documents``
    argument).

    ``selections`` is a list of ``{"semantic_type": str, "ids": [str, ...]?}``.
    A ``semantic_type`` not in ``SEMANTIC_TYPES`` is reported in
    ``unknown_types`` and otherwise skipped -- never raises, since this is
    the same "never guess, never crash on untrusted shape" posture as
    ``src/semantic_validation.py``.

    Absent/empty ``ids`` returns every object of that type COMPACTLY
    (``{"name", "summary", "model"}``); explicit ``ids`` returns the FULL
    object dict (every attribute the document declares) for just the named
    objects, plus ``"model"`` for provenance. Matching is case-insensitive,
    consistent with every other name comparison over imported document text
    in this codebase (see ``src/semantic_validation.py``).

    Returns ``{"results": [{"semantic_type", "mode", "objects"}, ...],
    "unknown_types": [...]}`` -- one results entry per selection, in the
    order given.
    """
    documents = [d for d in (documents or []) if isinstance(d, dict)]

    results: list[dict[str, Any]] = []
    unknown_types: list[str] = []

    for selection in selections or []:
        if not isinstance(selection, dict):
            continue
        semantic_type = selection.get("semantic_type")
        doc_key = SEMANTIC_TYPES.get(semantic_type) if isinstance(semantic_type, str) else None
        if doc_key is None:
            unknown_types.append(semantic_type if isinstance(semantic_type, str) else str(semantic_type))
            continue

        # Untrusted shape: coerce `ids` rather than crash or char-iterate a
        # bare string. A lone string is treated as one id; a non-iterable
        # (number, object) degrades to "no ids" (compact) — same never-guess/
        # never-crash posture this module keeps for bad selections/types
        # (Devin review on #1398).
        raw_ids = selection.get("ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        elif not isinstance(raw_ids, (list, tuple, set)):
            raw_ids = []
        wanted = {str(i).casefold() for i in raw_ids} if raw_ids else None
        mode = "full" if wanted is not None else "compact"

        objects: list[dict[str, Any]] = []
        for document in documents:
            model_name = document.get("name")
            for obj in document.get(doc_key) or []:
                if not isinstance(obj, dict) or not obj.get("name"):
                    continue
                if wanted is not None and str(obj["name"]).casefold() not in wanted:
                    continue
                entry: dict[str, Any] = dict(obj) if mode == "full" else {"name": obj["name"], "summary": _summary(obj)}
                entry["model"] = model_name
                objects.append(entry)

        results.append({"semantic_type": semantic_type, "mode": mode, "objects": objects})

    return {"results": results, "unknown_types": unknown_types}


def dataset_field_descriptions(document: dict[str, Any], table_id: str) -> dict[str, str]:
    """Field-name -> description for the dataset in ``document`` bound to
    ``table_id``.

    A dataset's binding is its own ``source`` (the upstream table it
    projects from), falling back to its ``name`` when ``source`` is absent —
    the same ``dataset.get("source") or dataset.get("name") or ""``
    derivation ``src/semantic/projection.py`` uses when it stamps a
    dataset's fields into ``column_metadata``, so a ``table_id`` that lines
    up there also resolves here. Returns ``{}`` when no dataset in this
    document binds to ``table_id``, or when the bound dataset declares no
    field descriptions.
    """
    for dataset in document.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        bound = dataset.get("source") or dataset.get("name") or ""
        if bound != table_id:
            continue
        out: dict[str, str] = {}
        for field in dataset.get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            description = field.get("description")
            if isinstance(name, str) and isinstance(description, str) and description.strip():
                out[name] = description.strip()
        return out
    return {}


def get_semantic_schema(semantic_types: list[str]) -> dict[str, Any]:
    """The vendored Apache Ossie JSON Schema, sliced to the requested
    ``semantic_types``.

    Served straight from ``src.semantic.document_validation``'s vendored,
    pinned schema -- never a hand-written copy, so it can never drift from
    what ``validate_document`` actually enforces. The returned ``$defs`` is
    the full vendored bag (schema definitions cross-reference each other --
    e.g. ``Dataset`` -> ``Field`` -> ``Expression`` -- so a per-type slice
    would otherwise carry dangling ``$ref``s); ``types`` maps each requested
    (recognized) type name to a ``$ref`` into it, so the result is itself a
    valid, self-contained JSON Schema document. A requested type this module
    does not recognize is reported in ``unknown_types`` rather than raising.
    """
    from src.semantic.document_validation import get_schema_defs

    defs = get_schema_defs()
    types: dict[str, Any] = {}
    unknown_types: list[str] = []

    for semantic_type in semantic_types or []:
        def_name = _SCHEMA_DEF_NAMES.get(semantic_type) if isinstance(semantic_type, str) else None
        if def_name is None or def_name not in defs:
            unknown_types.append(semantic_type if isinstance(semantic_type, str) else str(semantic_type))
            continue
        types[semantic_type] = {"$ref": f"#/$defs/{def_name}"}

    return {"$defs": defs, "types": types, "unknown_types": unknown_types}


__all__ = [
    "SEMANTIC_TYPES",
    "dataset_field_descriptions",
    "get_semantic_context",
    "get_semantic_schema",
]
