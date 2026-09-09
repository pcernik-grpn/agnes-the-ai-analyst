"""LLM auto-documentation of table descriptions (#399).

Most registered tables ship with no ``description``, which makes ``agnes
catalog`` weaker for AI agents. This module turns a table's columns + a few
sample rows into a short, factual description using the existing
``connectors.llm`` ``StructuredExtractor`` (Haiku by default).

It is deliberately a *pure* helper: the caller (``agnes admin autodoc-tables``)
supplies the extractor and the already-sampled data, and this module only
builds the prompt and parses the structured result. That keeps it free of any
``app.``/DB/network dependency and unit-testable with a fake extractor — no
live LLM call required.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Descriptions are short; cap the response budget tightly.
MAX_TOKENS = 400

# How many sample rows to show the model. More rarely helps the description
# and just costs tokens.
SAMPLE_ROWS = 5

DESCRIPTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "One or two factual sentences: what the table contains and its "
                "grain (one row per what). No marketing tone."
            ),
        }
    },
    "required": ["description"],
    "additionalProperties": False,
}


def _format_columns(columns: Optional[List[Dict[str, Any]]]) -> str:
    lines: List[str] = []
    for c in columns or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("column_name")
        if not name:
            continue
        typ = c.get("type") or c.get("basetype") or c.get("dtype") or "?"
        lines.append(f"- {name} ({typ})")
    return "\n".join(lines) or "(no column metadata)"


def _format_sample_rows(sample_rows: Optional[List[Dict[str, Any]]], limit: int = SAMPLE_ROWS) -> str:
    rows = [r for r in (sample_rows or []) if isinstance(r, dict)][:limit]
    if not rows:
        return "(no sample rows)"
    return json.dumps(rows, ensure_ascii=False, default=str, indent=2)


def build_prompt(
    table_name: str,
    columns: Optional[List[Dict[str, Any]]],
    sample_rows: Optional[List[Dict[str, Any]]],
    *,
    source: Optional[str] = None,
) -> str:
    """Compose the extraction prompt from a table's columns + sample rows."""
    src = f" (source: {source})" if source else ""
    return (
        "You are documenting a data-warehouse table for an analytics catalog.\n"
        f"Table: {table_name}{src}\n\n"
        f"Columns:\n{_format_columns(columns)}\n\n"
        f"Sample rows (up to {SAMPLE_ROWS}):\n{_format_sample_rows(sample_rows)}\n\n"
        "Write a concise, factual description (1-2 sentences, <= 240 characters) "
        "of what this table contains and its grain (one row per what). Base it only "
        "on the column names and sample values shown — do not invent columns or "
        "business meaning that isn't evident. No marketing tone."
    )


def generate_description(
    extractor: Any,
    table_name: str,
    columns: Optional[List[Dict[str, Any]]],
    sample_rows: Optional[List[Dict[str, Any]]],
    *,
    source: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Ask the model for a one/two-sentence description; return it stripped.

    ``extractor`` is any :class:`connectors.llm.base.StructuredExtractor`.
    Raises whatever the extractor raises (``LLMError`` subclasses) — the caller
    decides whether to skip or fail. Returns ``""`` if the model produced no
    usable string.
    """
    from src.observability import llm_context

    prompt = build_prompt(table_name, columns, sample_rows, source=source)
    with llm_context(workload="semantic_layer", purpose="table_autodoc", subject_id=table_name):
        result = extractor.extract_json(
            prompt=prompt,
            max_tokens=max_tokens,
            json_schema=DESCRIPTION_SCHEMA,
            schema_name="table_description",
        )
    desc = (result or {}).get("description", "")
    return desc.strip() if isinstance(desc, str) else ""


__all__ = [
    "MAX_TOKENS",
    "SAMPLE_ROWS",
    "DESCRIPTION_SCHEMA",
    "build_prompt",
    "generate_description",
]
