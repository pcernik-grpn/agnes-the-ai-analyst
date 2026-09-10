"""Shared data shapes for source-agnostic table-metadata providers.

Lives under `app/api/` because the primary consumer is
`app/api/v2_catalog.py`. Connector-side providers in `connectors/<source>/`
import upward into this module — the inverse layering would force
`v2_catalog.py` to depend on `connectors/__init__.py`, which is the
wrong direction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataRequest:
    """Narrow input passed to a metadata provider's `fetch()`.

    `bucket` and `source_table` are pre-validated by the dispatcher
    (`validate_quoted_identifier`) before construction, so the provider
    can interpolate them into SQL/URL paths without re-checking. Frozen
    so the (provider, request)-keyed cache lookup is stable.
    """
    table_id: str
    bucket: str
    source_table: str
    # The row's own BigQuery project, from `bq_row_target` (issue #343).
    # `None` means "use the source's configured project" — the pre-v51
    # behaviour, and the reason this defaults rather than being required:
    # non-BQ providers never set it and existing construction sites keep
    # working unchanged.
    project: str | None = None


@dataclass
class TableMetadata:
    """Source-agnostic metadata bundle. Every field optional — providers
    fill what they can cheaply get; callers tolerate `None`. Adding a new
    field here is a non-breaking change: existing CLI consumers don't
    even render `rough_size_hint` (verified `grep -rn rough_size_hint cli/`
    is empty), let alone the new fields.

    ``entity_type`` for BigQuery mirrors INFORMATION_SCHEMA.TABLES.table_type
    (``BASE TABLE`` / ``VIEW`` / ``MATERIALIZED VIEW`` / ``EXTERNAL`` /
    ``SNAPSHOT`` / ``CLONE``). Catalog uses it to hide misleading
    ``rows=0, size_bytes=0`` for VIEWs (which __TABLES__ reports as zero)
    and to inject a "LIMIT doesn't push into view body" hint into
    cost-guard errors when a remote query targets a VIEW.

    ``known_columns`` is the list of column names from the same refresh
    that populated this row. Catalog endpoint filters generic
    ``where_examples`` templates against this list — drops example
    predicates that reference columns the table doesn't have.
    """
    rows: int | None = None
    size_bytes: int | None = None
    partition_by: str | None = None
    clustered_by: list[str] | None = None
    entity_type: str | None = None
    known_columns: list[str] | None = None
