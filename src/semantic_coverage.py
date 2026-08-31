"""Source-agnostic semantic-layer coverage check: which registered tables
have NO valid semantic model describing them at all.

Deliberately separate from ``connectors/keboola/semantic_layer.py``'s
``compute_semantic_coverage`` — that one predicts, live against a connected
Keboola project's Metastore, how many of ITS metrics can bind to a
registered table (Keboola-only, upstream-facing). This module asks a
narrower, source-agnostic question against what is already stored in
``semantic_models``: does a registered table appear in ANY valid model's
datasets at all, regardless of which source (Keboola, git, manual, upload,
connection) wrote that model.

Built on :func:`src.semantic.projection.resolve_dataset_table`, the single
dataset -> ``table_registry.id`` resolver ``project_document`` itself uses —
so a Keboola-bound dataset (bound via its raw tableId, not a name a naive
text match could recognize) is never misreported as uncovered.
"""

from __future__ import annotations

from typing import Any

from src.repositories import semantic_model_repo, table_registry_repo
from src.semantic.projection import resolve_dataset_table


def tables_without_semantic_coverage(conn: Any = None) -> list[dict[str, Any]]:
    """Registered tables (full ``table_registry`` rows) with zero
    ``semantic_models`` rows (``status='valid'``) referencing any of their
    datasets.

    Walks every dataset of every model of every valid stored document
    (``SemanticModelsRepository.list_all()`` already deserializes
    ``document_json`` to Python dicts — no SQL JSON querying, so no
    DuckDB/PG syntax divergence), resolving each dataset via
    :func:`resolve_dataset_table` to build the set of covered table ids.
    Every registered table whose id is not in that set is returned.

    ``conn`` is accepted for signature stability (mirrors
    ``app.auth.scheduler_token.ensure_scheduler_user`` /
    ``src.semantic.projection.resolve_dataset_table``) — actual repo access
    goes through the ``*_repo()`` factory.
    """
    del conn
    covered: set[str] = set()
    for model in semantic_model_repo().list_all():
        if model.get("status") != "valid":
            continue
        document = model.get("document_json") or {}
        source = model.get("source") or ""
        for semantic_model in document.get("semantic_model") or []:
            if not isinstance(semantic_model, dict):
                continue
            for dataset in semantic_model.get("datasets") or []:
                if not isinstance(dataset, dict):
                    continue
                table_id = resolve_dataset_table(dataset, source)
                if table_id:
                    covered.add(table_id)

    return [row for row in table_registry_repo().list_all() if row["id"] not in covered]
