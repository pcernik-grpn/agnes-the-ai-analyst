"""Shared support for the semantic-layer auto-draft sweep (semantic-phase5,
wave 2): the trigger prompt a headless ``semantic-model-builder`` session
receives for one uncovered table, and the ``table_registry`` dedup-flag
clearing shared by the ``authoring_suggestions`` approve AND reject paths.

The sweep endpoint itself (``POST /api/admin/semantic-auto-draft-sweep``)
lives in ``app/api/semantic_models.py`` next to the coverage read it drives
off of (``tables_without_semantic_coverage``, wave 1) — this module holds
only the two pieces that are useful independent of the HTTP layer and are
exercised directly by unit tests.
"""

from __future__ import annotations

from typing import Any


def build_trigger_prompt(table: dict[str, Any]) -> str:
    """The prompt a headless auto-draft session receives for one uncovered
    table.

    Two things this MUST do that a normal chat-authored request never has
    to, because there is no human on the other end of a scheduler-triggered
    run:

    1. Explicitly override the ``semantic-model-builder`` persona's "show
       the full document and get an explicit go-ahead before applying it"
       instruction — followed literally in a headless session, it drafts a
       proposal, asks a question, and ends its turn having called
       ``apply_semantic_model`` never, silently producing nothing. Calling
       apply here does NOT skip review: this session authenticates as a
       non-admin identity, so the call is queued into the
       ``authoring_suggestions`` moderation queue for an admin to approve
       or reject — the exact checkpoint the normal instruction protects,
       just asynchronous instead of in-chat.
    2. Tell the agent it is fine — preferred, even — to submit a minimal,
       explicitly-flagged-for-review model rather than invent structure or
       meaning the data does not actually support.
    """
    table_id = table.get("id") or ""
    name = table.get("name") or table_id
    source_type = table.get("source_type") or "unknown"
    return (
        f"Draft a semantic model for the table `{table_id}` (display name "
        f"`{name}`, source type `{source_type}`). It currently has zero "
        "semantic-layer coverage.\n\n"
        "Follow the usual survey -> schema -> draft -> validate -> apply "
        f"flow: run `agnes schema {table_id}` and `agnes describe {table_id}` "
        "to see its real columns and sample values before writing any "
        "structure. Never invent a dataset, field, relationship or metric "
        "you have not actually seen in the data.\n\n"
        "IMPORTANT — this is a headless, unattended run: there is no human "
        "reading this conversation. Do NOT end your turn by asking a "
        "question or waiting for a go-ahead, and do not stop once you have "
        "a drafted-but-unapplied document. As soon as you have a "
        "schema-valid draft, call `apply_semantic_model` "
        "(`POST /api/semantic-models/apply`) yourself, in this same turn. "
        "This does not bypass review: you are authenticated as a non-admin "
        "identity, so the call is queued into the authoring-suggestions "
        "moderation queue for an admin to approve or reject — the exact "
        "checkpoint the normal 'get an explicit go-ahead' instruction "
        "protects, just asynchronous instead of in-chat.\n\n"
        "If the data does not give you enough to draft with real "
        "confidence — ambiguous or unlabeled columns, no clear grain or "
        "relationships — it is fine, and preferred, to submit a minimal "
        "model that documents only what you are sure of, with a note that "
        "it needs review, rather than guessing at semantics you cannot "
        "verify from the data itself."
    )


def clear_pending_for_document(document_json: dict, *, source: str = "manual") -> None:
    """Clear ``table_registry.semantic_draft_pending_at`` for every table a
    semantic-layer document's datasets resolve to.

    Shared by the ``authoring_suggestions`` approve AND reject paths
    (``app/api/authoring_suggestions.py``): a pending auto-draft for a
    table is "no longer pending" the moment ANY verdict lands on the
    suggestion that covered it, favorable or not — a rejected draft must
    be eligible for a fresh sweep just as much as an approved one.

    ``source`` defaults to ``"manual"`` because every semantic-layer
    ``authoring_suggestions`` resolution — human-submitted or auto-drafted
    — replays through ``apply_manual_model``
    (``app/api/semantic_models.py``), which always stores the resulting
    document under ``source='manual'``; :func:`resolve_dataset_table`
    needs that provenance to pick the right resolution strategy.

    No-op on a DuckDB-backend instance (A3 PG-first ratchet):
    ``table_registry.semantic_draft_pending_at`` is a Postgres-only column,
    so there is nothing to clear there — this is not an error, just a
    capability the frozen DuckDB app-state backend does not have. Every
    OTHER semantic-layer suggestion this function is called for on approve
    AND reject (``app/api/authoring_suggestions.py``) — human-submitted,
    unrelated to auto-drafting — must still resolve normally regardless.
    """
    from src.repositories import table_registry_repo, use_pg
    from src.semantic.projection import resolve_dataset_table

    if not use_pg():
        return

    registry = table_registry_repo()
    for model in document_json.get("semantic_model") or []:
        if not isinstance(model, dict):
            continue
        for dataset in model.get("datasets") or []:
            if not isinstance(dataset, dict):
                continue
            table_id = resolve_dataset_table(dataset, source)
            if table_id:
                registry.clear_semantic_draft_pending(table_id)
