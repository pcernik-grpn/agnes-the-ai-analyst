"""BigQuery metadata provider — populates `TableMetadata` for a remote
BQ-backed registry row.

Two queries (different INFORMATION_SCHEMA scopes — TABLE_STORAGE is
region-scoped, COLUMNS is dataset-scoped, can't be combined):

  1. INFORMATION_SCHEMA.TABLE_STORAGE — total_rows + active+long_term
     bytes. Region-portable per Google's docs; only valid via
     `<project>.region-<region>.INFORMATION_SCHEMA.TABLE_STORAGE`
     (verified live 2026-05-07; dataset-scoped TABLE_STORAGE doesn't
     exist).

  2. INFORMATION_SCHEMA.COLUMNS — partition_by + clustered_by. Reuses
     the consolidated `fetch_bq_columns_full` helper that v2_schema also
     calls; one shared shape, one round-trip.

Region resolution chain: `instance.yaml.data_source.bigquery.location` →
`bq.client().get_dataset(...)` → fall back to legacy `__TABLES__`
(dataset-scoped, no region required).

VIEW handling: TABLE_STORAGE returns no rows for entries whose
`table_type='VIEW'`; the legacy `__TABLES__` fallback also doesn't list
views. The provider returns `TableMetadata(rows=None, size_bytes=None,
partition_by=<from COLUMNS>, clustered_by=<from COLUMNS>)` — analyst
Claude reads `null` size and applies the existing CLAUDE.md guidance.

`size_bytes` reports `active_logical_bytes + long_term_logical_bytes`
(a full BQ scan reads both — reporting only active undercounts aged
partitioned tables).
"""

from __future__ import annotations

import logging

from app.api._metadata_models import MetadataRequest, TableMetadata
from app.instance_config import get_value
from connectors.bigquery.access import (
    BqAccessError,
    fetch_bq_columns_full,
    get_bq_access,
)

logger = logging.getLogger(__name__)


def _req_project(bq, req: MetadataRequest) -> str:
    """The project this request's table actually lives in.

    ``req.project`` carries the row's own ``bq_fqn`` project (issue #343);
    ``None`` means the row predates it and the configured data project is
    correct. Every path builder below goes through here so a cross-project
    row cannot be half-resolved — its columns read from one project and its
    row count from another.
    """
    return req.project or bq.projects.data


def fetch(req: MetadataRequest) -> TableMetadata | None:
    try:
        bq = get_bq_access()
    except BqAccessError:
        return None

    if not bq.projects.data:
        return None

    rows_size = _fetch_rows_and_size(bq, req)
    columns = fetch_bq_columns_full(bq, req.bucket, req.source_table, project=_req_project(bq, req))
    part_clust = _derive_partition_cluster(columns) if columns else None
    entity_type = _fetch_entity_type(bq, req)
    known_columns = [c["name"] for c in columns] if columns else None

    if rows_size is None and part_clust is None and entity_type is None and not known_columns:
        return None

    # For VIEW / MATERIALIZED VIEW the __TABLES__ fallback returns
    # ``(0, 0)`` for ``row_count`` and ``size_bytes`` — accurate for the
    # storage layer (views have no own storage) but misleading for
    # analysts. Surface ``None`` so catalog consumers see explicit
    # "unknown" rather than a confidently-wrong zero.
    if entity_type in ("VIEW", "MATERIALIZED VIEW"):
        rows_value = None
        size_value = None
    else:
        rows_value = (rows_size or {}).get("rows")
        size_value = (rows_size or {}).get("size_bytes")

    return TableMetadata(
        rows=rows_value,
        size_bytes=size_value,
        partition_by=(part_clust or {}).get("partition_by"),
        clustered_by=(part_clust or {}).get("clustered_by"),
        entity_type=entity_type,
        known_columns=known_columns,
    )


def _run_bq_sql(bq, sql: str, params: list[str], *, location: str | None = None):
    """Run a BQ-native positional-parameter (``?``) query through the
    python SDK and return the first row (or ``None``).

    Transport rationale: these metadata lookups used to ride the DuckDB
    extension (``bigquery_query()`` on a pooled extension session) at
    7–13 s per call — and the extension's statically linked libcurl
    intermittently fails new TLS handshakes with ``CURL error 77`` under
    fd pressure. ``bq.client()`` answers the same SQL in ~0.3 s over the
    SDK's certifi-verified channel. ``location`` pins the job region for
    region-scoped INFORMATION_SCHEMA queries.
    """
    from google.cloud import bigquery as gbq

    job_config = gbq.QueryJobConfig(query_parameters=[gbq.ScalarQueryParameter(None, "STRING", p) for p in params])
    result = bq.client().query(sql, job_config=job_config, location=location).result()
    return next(iter(result), None)


def _fetch_entity_type(bq, req: MetadataRequest) -> str | None:
    """Look up ``INFORMATION_SCHEMA.TABLES.table_type`` for the table.

    Single dataset-scoped query, no region required. Returns one of the
    documented BQ values (``BASE TABLE``, ``VIEW``, ``MATERIALIZED VIEW``,
    ``EXTERNAL``, ``SNAPSHOT``, ``CLONE``) or ``None`` if the lookup
    fails / the row isn't found.

    ``req.bucket`` and ``req.source_table`` are pre-validated by
    `app/api/v2_catalog._build_metadata_request`, so direct interpolation
    into the backtick-quoted path is safe.
    """
    try:
        bq_sql = (
            f"SELECT table_type FROM `{_req_project(bq, req)}.{req.bucket}.INFORMATION_SCHEMA.TABLES` WHERE table_name = ?"
        )
        row = _run_bq_sql(bq, bq_sql, [req.source_table])
    except Exception as e:
        logger.warning(
            "BQ INFORMATION_SCHEMA.TABLES lookup failed for %s.%s.%s: %s",
            _req_project(bq, req),
            req.bucket,
            req.source_table,
            e,
        )
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _derive_partition_cluster(columns: list[dict]) -> dict | None:
    """Mirror v2_schema._fetch_bq_table_options derivations from the
    shared columns-full result."""
    if not columns:
        return None
    partition_by = next(
        (c["name"] for c in columns if c["is_partitioning_column"]),
        None,
    )
    clustered = sorted(
        (c for c in columns if c["clustering_ordinal_position"] is not None),
        key=lambda c: c["clustering_ordinal_position"],
    )
    clustered_by = [c["name"] for c in clustered]
    return {"partition_by": partition_by, "clustered_by": clustered_by}


def _fetch_rows_and_size(bq, req: MetadataRequest) -> dict | None:
    """Resolve rows + size_bytes via TABLE_STORAGE → __TABLES__ fallthrough.

    See module docstring + spec Open Question §1 for view-path nuance.
    """
    location = _resolve_bq_location(bq, req)
    if location:
        result = _fetch_via_table_storage(bq, req, location)
        if result is not None:
            return result
        # TABLE_STORAGE returned None despite having a location: could
        # be a typo in `data_source.bigquery.location`, a multi-region
        # dataset operator misclassified, the table is a VIEW, or a
        # transient permission gap. Try __TABLES__ before giving up.
    return _fetch_via_legacy_tables(bq, req)


def _resolve_bq_location(bq, req: MetadataRequest) -> str | None:
    """instance.yaml.location → REST get_dataset → None.

    The REST fallback is best-effort: it requires the SA to have
    ``bigquery.datasets.get`` on the data project. Most cross-project
    setups grant ``bigquery.tables.get`` (data viewer) but NOT dataset-
    level metadata, so this 404s silently for the exact deployments
    that most need region detection. Configuring
    ``data_source.bigquery.location`` skips the REST round-trip entirely
    and makes the path deterministic — strongly recommended for any
    non-trivial setup. Issue #343.
    """
    cfg_location = (get_value("data_source", "bigquery", "location") or "").strip()
    if cfg_location:
        return cfg_location
    try:
        ds = bq.client().get_dataset(f"{_req_project(bq, req)}.{req.bucket}")
        return ds.location
    except Exception as e:
        logger.warning(
            "BQ dataset.get fell back for %s.%s: %s. To skip this REST "
            "round-trip on every metadata refresh (and silence cases "
            "where the SA lacks bigquery.datasets.get), set "
            "data_source.bigquery.location in /admin/server-config to the "
            "dataset's region (e.g. 'us-central1' or 'EU').",
            _req_project(bq, req),
            req.bucket,
            e,
        )
        return None


def _fetch_via_table_storage(bq, req: MetadataRequest, location: str) -> dict | None:
    """Region-scoped INFORMATION_SCHEMA.TABLE_STORAGE — preferred path.

    `validate_quoted_identifier` accepts `us-central1`, `europe-west1`,
    `EU`, `us` etc. (regex `^[a-zA-Z0-9_][a-zA-Z0-9_.\\-]{0,127}$`).
    Refuses anything that could break out of the backtick-quoted path.

    Returns None on no-row (table is a VIEW, or different region than
    configured) — caller decides whether to fall through.

    `size_bytes` is `active + long_term` logical bytes (a full BQ scan
    reads both; reporting only active undercounts aged partitioned tables).
    """
    from src.identifier_validation import validate_quoted_identifier

    if not validate_quoted_identifier(location, "BQ region"):
        return None
    # `req.bucket` / `req.source_table` are pre-validated by the
    # dispatcher; `location` is validated locally above because it
    # originates from instance.yaml, not from the registry row.
    try:
        bq_sql = (
            f"SELECT total_rows, "
            f"IFNULL(active_logical_bytes, 0) + IFNULL(long_term_logical_bytes, 0) "
            f"FROM `{_req_project(bq, req)}.region-{location}.INFORMATION_SCHEMA.TABLE_STORAGE` "
            f"WHERE table_schema = ? AND table_name = ?"
        )
        # `location=` pins the SDK job to the region the region-scoped
        # INFORMATION_SCHEMA view lives in — without it the job runs in
        # the API default region and returns nothing.
        row = _run_bq_sql(bq, bq_sql, [req.bucket, req.source_table], location=location)
    except Exception as e:
        logger.warning(
            "BQ TABLE_STORAGE fetch failed for %s.%s.%s: %s",
            _req_project(bq, req),
            req.bucket,
            req.source_table,
            e,
        )
        return None
    if row is None:
        return None  # VIEW or wrong region
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }


def _fetch_via_legacy_tables(bq, req: MetadataRequest) -> dict | None:
    """Last-resort dataset-scoped __TABLES__ — works without region."""
    # `req.bucket` and `req.source_table` are pre-validated by
    # `app/api/v2_catalog._build_metadata_request` via
    # `validate_quoted_identifier` before MetadataRequest construction;
    # safe to interpolate into the backtick-quoted path here.
    try:
        bq_sql = f"SELECT row_count, size_bytes FROM `{_req_project(bq, req)}.{req.bucket}.__TABLES__` WHERE table_id = ?"
        row = _run_bq_sql(bq, bq_sql, [req.source_table])
    except Exception as e:
        logger.warning(
            "BQ __TABLES__ fetch failed for %s.%s.%s: %s",
            _req_project(bq, req),
            req.bucket,
            req.source_table,
            e,
        )
        return None
    if row is None:
        return None
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }
