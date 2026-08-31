"""GET /api/v2/catalog — list tables visible to caller (spec §3.1).

History note
------------
0.47.0 enriched remote rows with BigQuery metadata (rows / size_bytes /
partition_by / clustered_by) by fetching from BQ *inside the request*
through a per-table TTL cache. On a cold cache that fanned out to O(N)
sequential BQ jobs API roundtrips and reliably exceeded the CLI's 30 s
``httpx.ReadTimeout`` against partitioned tables. This module now reads
those fields exclusively from the persistent ``bq_metadata_cache`` table
(populated by ``app/api/bq_metadata_refresh.py`` on a scheduler tick).
The request path never calls BQ.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import duckdb
from fastapi import APIRouter, Depends

from app.api.v2_cache import TTLCache
from app.auth.dependencies import _get_db, get_current_user
from src.audit_helpers import client_kind_from_user, identity_for_audit
from src.rbac import get_accessible_tables

from src.repositories import (
    audit_repo,
    bq_metadata_cache_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

# Global cache of the raw table_registry rows. RBAC is enforced PER REQUEST
# against this list, mirroring v2_schema.py / v2_sample.py — caching the
# RBAC-filtered payload per user used to leave revoked users seeing tables
# for up to TTL after a permission flip. Cache is single-keyed; the TTL
# matches the documented `api.catalog_cache_ttl_seconds` default at
# `config/instance.yaml.example`. The config knob isn't wired through yet
# (same status as schema/sample caches), so changing it in instance.yaml is
# a no-op — tracked separately.
_table_rows_cache = TTLCache(maxsize=1, ttl_seconds=300)
_TABLE_ROWS_KEY = "all"


def _flavor_for(source_type: str, query_mode: str = "") -> str:
    """Which SQL dialect a caller should write for this row.

    Follows the execution engine, not the source: a materialized Databricks
    row is a local parquet queried by DuckDB, so it is `duckdb` — only a
    `query_mode='remote'` row's statement actually reaches the warehouse. The
    schema endpoint applies the same rule (`app/api/v2_schema.py`); the two
    must agree or an agent is told one dialect and validated against another.
    """
    if source_type == "bigquery":
        return "bigquery"
    if source_type == "databricks" and query_mode == "remote":
        return "databricks"
    return "duckdb"


# Generic ``where_examples`` templates the catalog surfaces as a starting
# point for AI consumers. Each entry is a tuple of ``(predicate_text,
# required_columns)``: the template is only included in the response when
# every required column is present in the table's actual schema (from
# ``bq_metadata_cache.known_columns``). This prevents the old behavior of
# always advertising ``country_code = 'CZ'`` on tables that have no
# ``country_code`` column at all.
_BQ_WHERE_TEMPLATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("event_date > DATE '2026-01-01'", ("event_date",)),
    ("country_code = 'CZ' AND platform = 'web'", ("country_code", "platform")),
)


def _examples_for(source_type: str, known_columns: list[str] | None) -> list[str]:
    """Return generic ``where_examples`` filtered against the table's
    actual columns. ``known_columns`` comes from the persistent metadata
    cache; when it is unknown (None) or empty, return an empty list
    instead of a possibly-wrong template — silence is better than
    misleading hints for AI consumers."""
    if source_type != "bigquery":
        return []
    if not known_columns:
        return []
    cols = set(known_columns)
    return [predicate for predicate, required in _BQ_WHERE_TEMPLATES if all(c in cols for c in required)]


def _fetch_hint(table_id: str, source_type: str, server_only: bool = False, query_mode: str = "") -> str:
    if server_only:
        # Materialized/local on the server but NOT synced to the analyst laptop
        # (`agnes pull` skips server_only rows), so it has no local view — the
        # only way to query it is server-side via --remote. Takes precedence
        # over source_type: a bigquery-materialized table can also be
        # server_only, and the snapshot-create hint would be wrong there too.
        return "server-only — not synced locally; query via `agnes query --remote`"
    if source_type == "bigquery":
        return f"agnes snapshot create {table_id} --select <cols> --where '<BQ predicate>' --limit <N>"
    if query_mode == "remote":
        # A remote row on any other engine has no parquet and no local view,
        # so "already local" below would send the caller straight into a
        # failing local query — the same misroute #898 fixed for internal
        # tables. Snapshot-create is BigQuery-only, hence the separate hint.
        return "remote — no local copy; query via `agnes query --remote`"
    if source_type == "internal":
        # Internal tables live in the server state backend and are never
        # distributed to analyst laptops: `agnes pull` skips them and the
        # sync API refuses to sign parquet URLs for them, so there is NO
        # local view — "already local" would misroute a client straight
        # into a failing local query (#898). `agnes query` auto-routes
        # server-side.
        return "server-side only — query via `agnes query` (auto-routes to the server)"
    return "already local — query directly via `agnes query`"


# Coarse size buckets for `rough_size_hint`. Boundaries chosen so an analyst
# Claude can decide tool by inspection: anything `large` or worse implies
# `agnes snapshot create` over `agnes query --remote`. Numbers reflect the
# default `bq_max_scan_bytes` 5 GiB ceiling — at "large" you're already at
# half the per-query gate and a naive `--remote` is likely to refuse.
_SIZE_BUCKETS = (
    (10 * 2**20, "small"),  # ≤10 MiB
    (100 * 2**20, "small"),  # ≤100 MiB still small (analyst-laptop scale)
    (1 * 2**30, "medium"),  # ≤1 GiB
    (10 * 2**30, "large"),  # ≤10 GiB
)


def _bucket_size(byte_count: int) -> str:
    for cap, label in _SIZE_BUCKETS:
        if byte_count <= cap:
            return label
    return "very_large"


def _materialized_parquet_size_bucket(
    table_id: str,
    source_type: str,
    query_mode: str,
    *,
    registry_name: str | None = None,
) -> str | None:
    """Size hint for rows whose data is on the server filesystem
    (``local`` or ``materialized``). Cheap ``Path.stat()``; never blocks.

    Resolves the parquet by source-name-agnostic lookup: the extract directory
    is not necessarily the ``source_type`` (e.g. the bundled `demo` extract
    registers tables as 'local' but lives under ``extracts/demo/``), so keying
    the path off ``source_type`` silently lost the size hint for such rows.

    A PARTITIONED table is a directory of per-period parquets, so there is no
    single file to ``stat()`` — its size is the sum over the parts
    (``local_parquet_size_bytes``), which is what the extractor and the sync
    state already record for it. Before that it had no hint at all, and an
    analyst-facing agent reading the catalog saw a fully-synced table as
    sizeless (Devin Review on #1189).

    ``registry_name`` is the row's ``name`` — the key the sync actually files
    the parquet under. Without it the hint was null for every row whose id was
    slugified from a differing name, while ``/schema``, ``/scan`` and
    ``/sample`` resolved those rows fine (Devin Review on #1516).
    """
    if not source_type:
        return None
    try:
        from app.utils import local_parquet_size_bytes

        size = local_parquet_size_bytes(table_id, source_type, registry_name=registry_name)
        if size is None:
            return None
        return _bucket_size(size)
    except Exception:
        # Filesystem stat() race / permissions / weird DATA_DIR — fall back
        # to null rather than crash the whole catalog response.
        return None


def _hint_for_row(
    row: dict[str, Any],
    bq_cache_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve the per-row metadata bundle the catalog response surfaces.

    Branches:
      - ``local`` / ``materialized`` → on-disk parquet ``stat()`` (cheap).
      - ``remote`` (BigQuery) → pre-computed row from ``bq_metadata_cache``,
        populated by the scheduler-driven refresh. Never touches BQ here.

    Always returns ``metadata_freshness`` (``fresh`` / ``stale`` /
    ``never_fetched`` / ``error`` / ``not_applicable``) so AI consumers can
    decide whether to trust ``rows`` / ``size_bytes`` or treat them as
    advisory.
    """
    table_id = row["id"]
    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or "local"

    if query_mode in ("local", "materialized"):
        return {
            "rough_size_hint": _materialized_parquet_size_bucket(
                table_id,
                source_type,
                query_mode,
                registry_name=row.get("name"),
            ),
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": "not_applicable",
        }

    if query_mode != "remote":
        return {
            "rough_size_hint": None,
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": "not_applicable",
        }

    # A remote row on an engine with no metadata cache. `never_fetched` would
    # imply a refresh is pending; nothing is scheduled to fetch this, so say so
    # — the BQ metadata cache is BigQuery's, and Databricks exposes no
    # equivalent size/partition metadata through the Statement Execution API.
    if source_type != "bigquery":
        return {
            "rough_size_hint": None,
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": "not_applicable",
        }

    # Remote: read from the persistent cache; never call BQ here.
    from app.api.bq_metadata_refresh import compute_freshness

    cache_row = bq_cache_index.get(table_id)
    freshness = compute_freshness(cache_row)

    if cache_row is None:
        return {
            "rough_size_hint": None,
            "rows": None,
            "size_bytes": None,
            "partition_by": None,
            "clustered_by": [],
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": freshness,
        }

    size_bytes = cache_row.get("size_bytes")
    return {
        "rough_size_hint": _bucket_size(size_bytes) if size_bytes is not None else None,
        "rows": cache_row.get("rows"),
        "size_bytes": size_bytes,
        "partition_by": cache_row.get("partition_by"),
        "clustered_by": cache_row.get("clustered_by") or [],
        "entity_type": cache_row.get("entity_type"),
        "known_columns": cache_row.get("known_columns") or [],
        "metadata_freshness": freshness,
    }


#: Coordination-backend pub/sub channel carrying cache-invalidation events
#: across api-serving replicas. Payload is ``json.dumps({"scope": "table"|
#: "all", "table": <id or None>})``. Subscribed in ``app.main``'s lifespan
#: (every api-serving process); see ``_on_cache_invalidate`` there.
CACHE_INVALIDATE_CHANNEL = "cache-invalidate"


def _publish_cache_invalidate(*, scope: str, table: str | None) -> None:
    """Broadcast an invalidation on :data:`CACHE_INVALIDATE_CHANNEL` so every
    OTHER api-serving replica drops the same local caches.

    Best-effort: a coordination-backend hiccup must never fail an admin
    registry mutation whose local caches (this process's) are already
    cleared — the other replicas simply keep serving their existing TTL a
    little longer, same as today's single-process behavior when there is
    only one replica. Memory backend: publish() only reaches subscribers in
    THIS process, so this is a harmless no-op fan-out when running as a
    single all-in-one instance (no other replica to reach).
    """
    import json

    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    try:
        coordination().publish(CACHE_INVALIDATE_CHANNEL, json.dumps({"scope": scope, "table": table}))
    except CoordinationUnavailable:
        logger.warning("cache-invalidate publish failed (coordination backend unavailable)")


def invalidate_for_table(table_id: str, *, _publish: bool = True) -> None:
    """Drop every per-table cache so the next /api/v2/* request reflects
    the just-registered / updated / unregistered row immediately. Owned
    by the catalog module so admin.py doesn't need to know which caches
    exist.

    The persistent ``bq_metadata_cache`` row is NOT invalidated here —
    the scheduler-driven refresh owns that lifecycle. Admins who need
    an immediate refresh after a registry edit should hit
    ``POST /api/v2/metadata-cache/refresh?table=<id>``.

    ``_publish`` (default ``True``): also broadcast this invalidation via
    the coordination backend so every other api-serving replica's local
    caches are dropped too. The lifespan subscriber in ``app.main`` calls
    this function with ``_publish=False`` when reacting to an INCOMING
    event from another replica — that keeps the local-drop path in one
    place ("route into the same invalidate functions") without ever
    re-publishing what it just received, so there is no echo loop back
    onto the channel.
    """
    from app.api import v2_sample, v2_schema

    _table_rows_cache.clear()
    v2_schema._schema_cache.invalidate(table_id)
    # A POLICIED table's schema is cached per caller identity, under
    # `f"{table_id}|policy:{policy_cache_identity(...)!r}"` (table access
    # policies §9) — keys the exact-key `invalidate` above can never match.
    # Without this the caller kept the PRE-edit column list for the full
    # hour of `_schema_cache`'s TTL after an admin hid a column, and
    # `/api/v2/scan`'s where/select validator (same payload, via
    # `_resolve_schema`) went on treating the hidden column as
    # referenceable. The delimiter is part of the prefix so a table named
    # `orders` doesn't evict `orders_archive`'s entries.
    v2_schema._schema_cache.invalidate_prefix(f"{table_id}|")
    # Sample cache key is `f"{table_id}|{n}"`; clearing the whole sample
    # cache is heavier than precise invalidation, but registry-change
    # frequency (handful per day on a typical instance) doesn't justify
    # adding a prefix-invalidation primitive to TTLCache.
    v2_sample._sample_cache.clear()
    if _publish:
        _publish_cache_invalidate(scope="table", table=table_id)


def invalidate_all(*, _publish: bool = True) -> None:
    """Registry-wide analogue of ``invalidate_for_table`` — drop every per-table
    catalog cache (rows, schema, sample). For operations that rebuild many tables
    at once (e.g. ``POST /api/admin/registry/rebuild``), where clearing one schema
    entry at a time would miss tables and a catalog read taken before the rebuild
    could otherwise serve a stale (no-view) schema until TTL expiry. As with
    ``invalidate_for_table``, the persistent ``bq_metadata_cache`` is left to the
    scheduler-driven refresh.

    ``_publish`` — see :func:`invalidate_for_table`; identical echo-loop guard.
    """
    from app.api import v2_sample, v2_schema

    _table_rows_cache.clear()
    v2_schema._schema_cache.clear()
    v2_sample._sample_cache.clear()
    if _publish:
        _publish_cache_invalidate(scope="all", table=None)


def build_catalog(conn: duckdb.DuckDBPyConnection, user: dict) -> dict:
    rows = _table_rows_cache.get(_TABLE_ROWS_KEY)
    if rows is None:
        repo = table_registry_repo()
        rows = repo.list_all()
        _table_rows_cache.set(_TABLE_ROWS_KEY, rows)

    # One DB read for all remote-row metadata. Indexed by table_id so the
    # per-row loop below stays O(N).
    bq_cache_index: dict[str, dict[str, Any]] = {r["table_id"]: r for r in bq_metadata_cache_repo().list_all()}

    # RBAC is enforced fresh per request. Revoking a user's access to a
    # table takes effect on their next call to this endpoint, not after the
    # cache TTL expires.
    #
    # Resolve the accessible set ONCE for the whole request instead of
    # calling can_access_table() per row (was ~8-9 serialized Postgres
    # round-trips x ~115 rows = 600+ round-trips / request). `None` means
    # admin/all; otherwise membership is a plain set lookup.
    _accessible_ids = get_accessible_tables(user, conn)
    allowed = None if _accessible_ids is None else set(_accessible_ids)

    visible = []
    for r in rows:
        if not (allowed is None or r["id"] in allowed):
            continue
        hint = _hint_for_row(r, bq_cache_index)
        # Table access policies (§11): `where_examples` / `partition_by` /
        # `clustered_by` are column-NAME-shaped hints sourced from
        # `hint["known_columns"]` — the UNFILTERED `bq_metadata_cache`, a
        # scheduler read of the physical table, independent of any policy
        # attached later. An EXCLUDE'd column could still surface here (a
        # WHERE-clause suggestion, a partitioning hint) even though Task 9's
        # effective_schema already hides it on every other schema surface.
        #
        # `allowed is None` mirrors `policied_relation`'s own admin-bypass
        # check — `get_accessible_tables` applies the SAME
        # `is_user_admin(...) and _credential_surface(...) == "all"` test
        # (src/rbac.py) — so this reuses data already computed above rather
        # than a second `policied_relation` resolve per row. Deliberately
        # SUPPRESSION, not full effective-schema derivation: this module's
        # own docstring states its design goal as never calling BQ / staying
        # cheap per request for the whole (~100+ row) listing, and a live
        # `policied_relation` + `effective_schema` DESCRIBE per
        # policied-remote row would break that. Row count / size_bytes /
        # entity_type stay unfiltered — same §10.1 aggregate-metadata
        # precedent as the row-count badge (an unfiltered COUNT is
        # accepted; column-shaped CONTENT is not).
        policy_restricted = bool(r.get("access_policy_sql")) and allowed is not None
        visible.append(
            {
                "id": r["id"],
                "name": r.get("name") or r["id"],
                "description": r.get("description") or "",
                "source_type": r.get("source_type") or "",
                "query_mode": r.get("query_mode") or "local",
                # Distribution flag, decoupled from query_mode (#607): a
                # server_only row is materialized/local on the server but excluded
                # from `agnes pull`, so it must be queried via --remote. Surfaced
                # as structured metadata so tooling doesn't have to parse the
                # free-text description.
                "server_only": bool(r.get("server_only")),
                "sql_flavor": _flavor_for(r.get("source_type") or "", r.get("query_mode") or ""),
                "where_examples": (
                    [] if policy_restricted else _examples_for(r.get("source_type") or "", hint.get("known_columns"))
                ),
                "fetch_via": _fetch_hint(
                    r["id"],
                    r.get("source_type") or "",
                    bool(r.get("server_only")),
                    r.get("query_mode") or "",
                ),
                "rough_size_hint": hint.get("rough_size_hint"),
                "rows": hint.get("rows"),
                "size_bytes": hint.get("size_bytes"),
                "partition_by": None if policy_restricted else hint.get("partition_by"),
                "clustered_by": [] if policy_restricted else (hint.get("clustered_by") or []),
                "entity_type": hint.get("entity_type"),
                "metadata_freshness": hint.get("metadata_freshness"),
            }
        )

    return {
        "tables": visible,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/catalog")
def catalog(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Plain ``def`` so FastAPI auto-offloads to the anyio thread pool —
    # the request path is pure local I/O (DuckDB reads + filesystem
    # stat()) and uses a sync DuckDB cursor.
    t0 = time.monotonic()
    # Bookkeeping identity only — a restricted principal (co-session /
    # agent-session) has no ``.get``, and both audit writes below sit inside
    # an ``except Exception`` that would swallow the AttributeError and drop
    # the row. Never an authorization input.
    audit_user_id, _audit_email = identity_for_audit(user)
    try:
        result = build_catalog(conn, user)
        try:
            audit_repo().log(
                user_id=audit_user_id,
                action="catalog.list",
                resource="catalog",
                params={
                    "rows_returned": len(result.get("tables", [])),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.list; continuing")
        return result
    except Exception as exc:
        try:
            audit_repo().log(
                user_id=audit_user_id,
                action="catalog.list",
                resource="catalog",
                params={"error": str(exc)[:200], "duration_ms": int((time.monotonic() - t0) * 1000)},
                result=f"error.{type(exc).__name__}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.list; continuing")
        raise
