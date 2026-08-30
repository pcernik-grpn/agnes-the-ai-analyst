"""How many models does a semantic source actually own?

Product-owner finding on #1707 (2026-08-30 review): a source can sync
successfully and import nothing — a ``connection`` source pointed at a
database with no semantic views records ``last_sync_status='ok'``, owns zero
models, and reads exactly like a healthy one. "Upstream has nothing" and
"scoped wrong, silently" both look green. The count is what tells them apart,
and every surface that shows a source's sync state shows it: the REST list,
single-source GET and write responses, the ``/admin/semantic-sources`` page,
``agnes admin semantic source list``, and the ``sources`` block of the
semantic-layer health report (which also lists the silently-empty sources as
a finding of their own).

**Derived at read time, never stored.** ``semantic_sources`` is a frozen
pre-A3 DuckDB↔PG pair and the DuckDB migration ladder is frozen
(``src.db.FROZEN_DUCKDB_SCHEMA_VERSION``), so there is no column to add — and
a persisted counter would be one more thing that can disagree with the models
table after a prune, an admin delete, or a detach.

The join key is the provenance the import pipeline stamps, not a guess:
:func:`src.semantic.transports.resolve_provenance` decides the ``(source,
source_ref)`` pair and :func:`src.semantic.importer.import_documents` writes
every row under it.

"Owns" therefore means "is stamped with this source's provenance" — full
stop. It is NOT "its next sync could delete this many": the prune is keyed on
the same pair but is strictly narrower (Postgres skips
``sync_mode='detached'`` rows, and a ``safe_prune`` source skips the prune
altogether on a run with no valid documents). Invalid documents count too;
validity is ``invalid_models``' question in the health report, not this one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: The field name every surface publishes the count under. One spelling, so a
#: rename is one edit rather than four.
OWNED_MODEL_COUNT_FIELD = "owned_model_count"


def owned_model_counts(sources: Iterable[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """``{source id: how many semantic_models rows it owns}``, ``None`` when
    that cannot be determined.

    One grouped query for the whole list, not one COUNT per source.

    A source whose ``config.provenance`` override cannot be resolved reports
    ``None``, never ``0``. The resolver validates the claimed ref against
    LIVE state — the legacy Keboola branch asks
    :func:`connectors.keboola.semantic_layer.legacy_credentials_prune_scope`
    which connection the env credentials resolve to — so flipping the default
    connection can make a source that still owns rows fail to resolve. Saying
    "0 models / imported nothing" there would be a wrong number stated
    confidently; "cannot say" is the honest answer, and it is logged so the
    cause is findable. Raising is not an option either: one malformed row
    must not take down the list every other row is read from.
    """
    from src.repositories import semantic_model_repo
    from src.semantic.transports import resolve_provenance

    sources = list(sources)
    if not sources:
        return {}

    counts = semantic_model_repo().counts_by_provenance()
    out: Dict[str, Optional[int]] = {}
    for source in sources:
        source_id = source.get("id")
        try:
            key = resolve_provenance(source)
        except (ValueError, KeyError) as exc:
            logger.warning(
                "Semantic source %s: cannot resolve its provenance, so the owned-model count is "
                "unknown (reported as null, not 0): %s",
                source_id,
                exc,
            )
            out[source_id] = None
            continue
        out[source_id] = counts.get(key, 0)
    return out


def with_owned_model_count(sources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same rows, each with :data:`OWNED_MODEL_COUNT_FIELD` added — an
    integer, or ``None`` when the source's provenance could not be resolved.

    Returns copies — the repository row a caller passed in is left untouched,
    so annotating for a response can never leak a derived field back into a
    write path that spreads the row (``{**source, …}``).
    """
    sources = list(sources)
    counts = owned_model_counts(sources)
    return [{**s, OWNED_MODEL_COUNT_FIELD: counts.get(s.get("id"))} for s in sources]
