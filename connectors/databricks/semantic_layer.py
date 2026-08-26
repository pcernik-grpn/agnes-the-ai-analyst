"""Databricks Unity Catalog semantic layer → Agnes ``metric_definitions``.

Unity Catalog *metric views* are Databricks's semantic layer: YAML-defined
first-class catalog objects declaring a source, dimensions and measures,
queried with the ``MEASURE()`` aggregate — which only Databricks compute can
evaluate. This module mirrors those definitions into Agnes's business-metric
registry so agents discover them through the standard rails
(``agnes catalog --metrics``) instead of inventing their own calculations,
with each stored ``sql`` written to run server-side on the warehouse (via a
``query_mode='materialized'`` row or a future remote passthrough).

Shape mirrors ``connectors/keboola/semantic_layer.py`` (the working
precedent for a connector-driven metrics sync):

- every row is stamped ``source='databricks_semantic_layer'`` +
  ``source_ref=<workspace host>``, and the prune only ever touches rows
  inside that (writer, ref) scope — manual/yaml/keboola rows are untouchable
  by construction;
- name ownership is sticky: a metric name already held by another writer is
  skipped (counted, never shadowed);
- an upstream fetch that yields zero usable measures while rows exist skips
  the prune and logs loudly instead of wiping the registry.

Discovery runs on the warehouse itself: ``information_schema.tables``
filtered to ``table_type = 'METRIC_VIEW'`` per configured catalog, then
``SHOW CREATE TABLE`` per view, whose statement embeds the YAML body between
``$$`` delimiters (``CREATE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$``).
An unrecognized table_type vocabulary or YAML shape degrades to counted
skips / an empty run — never to a prune.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

from connectors.databricks.client import (
    DatabricksApiError,
    DatabricksStatementClient,
)

logger = logging.getLogger(__name__)

SOURCE_LABEL = "databricks_semantic_layer"

_COUNTER_KEYS = (
    "created_or_updated",
    "pruned",
    "metric_views_seen",
    "skipped_unparseable",
    "skipped_conflict",
)

# YAML body between $$ delimiters in SHOW CREATE TABLE output. Non-greedy,
# DOTALL — the YAML itself cannot contain a bare `$$` (Databricks would have
# rejected the CREATE), so the first closing delimiter is the right one.
_YAML_BODY_RE = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)

# ``information_schema.tables.table_type`` values that denote a metric view.
# Both spellings are accepted for the same reason the BigQuery extractor
# normalises ``MATERIALIZED VIEW``/``MATERIALIZED_VIEW`` (see
# ``connectors/bigquery/extractor.py``): the underscore/space split is exactly
# where a vendor's INFORMATION_SCHEMA vocabulary has bitten this codebase
# before, and matching only one spelling degrades to a silent "0 metric views"
# — indistinguishable from a workspace that genuinely has none.
_METRIC_VIEW_TABLE_TYPES = ("METRIC_VIEW", "METRIC VIEW")


def _empty_counters() -> dict[str, int]:
    return {key: 0 for key in _COUNTER_KEYS}


def _error_result(message: str, code: str) -> dict[str, Any]:
    return {"status": "error", "error": message, "code": code, **_empty_counters()}


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _resolve_row_token(connection: dict[str, Any], token_env: str) -> str:
    """Vault-first (this connection's own vault slot), then the named env
    var / remote-attach vault fallback — same order the legacy
    instance-config path used, just with the connection's own vault slot
    checked first."""
    try:
        from src.repositories import connection_secrets_repo

        vault_value = connection_secrets_repo().get(connection["id"])
    except Exception:
        vault_value = None
    if vault_value:
        return vault_value
    token = os.environ.get(token_env, "")
    if token:
        return token
    try:
        from src.orchestrator_security import resolve_remote_attach_token

        return resolve_remote_attach_token(token_env) or ""
    except Exception:  # pragma: no cover - vault optional in dev contexts  # noqa: BLE001
        return ""


def _resolve_databricks_from_row(connection: dict[str, Any]) -> dict[str, Any] | None:
    """Settings from a ``source_connections`` row (``source_type='databricks'``).

    Non-secret coordinates come from ``config``; the token's env-var name is
    read from ``config`` first (a fresh wizard save), falling back to the
    row's top-level ``token_env`` column (the shape ``app.connections_seed``
    writes) and then the module default.
    """
    config = connection.get("config") or {}
    host = str(config.get("host") or "").strip()
    warehouse_id = str(config.get("warehouse_id") or "").strip()
    catalog = str(config.get("catalog") or "").strip()
    token_env = (
        str(config.get("token_env") or "").strip()
        or str(connection.get("token_env") or "").strip()
        or "DATABRICKS_TOKEN"
    )
    token = _resolve_row_token(connection, token_env)
    if not (host and warehouse_id and token):
        return None
    catalogs = config.get("semantic_layer_catalogs")
    if isinstance(catalogs, str):
        catalogs = [c.strip() for c in catalogs.split(",") if c.strip()]
    if not catalogs:
        catalogs = [catalog] if catalog else []
    return {
        "host": host,
        "warehouse_id": warehouse_id,
        "catalog": catalog,
        "catalogs": catalogs,
        "token": token,
    }


def _resolve_databricks_from_instance_config() -> dict[str, Any] | None:
    """Legacy path: ``data_source.databricks.*`` (instance.yaml / /admin/server-config).

    Kept byte-for-byte so an un-migrated instance (no databricks row yet)
    keeps resolving exactly as before D2.2.
    """
    from app.instance_config import get_value

    host = get_value("data_source", "databricks", "host", default="") or ""
    warehouse_id = get_value("data_source", "databricks", "warehouse_id", default="") or ""
    catalog = get_value("data_source", "databricks", "catalog", default="") or ""
    token_env = get_value("data_source", "databricks", "token_env", default="DATABRICKS_TOKEN") or "DATABRICKS_TOKEN"
    token = os.environ.get(token_env, "")
    if not token:
        try:
            from src.orchestrator_security import resolve_remote_attach_token

            token = resolve_remote_attach_token(token_env) or ""
        except Exception:  # pragma: no cover - vault optional in dev contexts  # noqa: BLE001
            token = ""
    if not (host and warehouse_id and token):
        return None
    catalogs = get_value("data_source", "databricks", "semantic_layer_catalogs", default=None)
    if isinstance(catalogs, str):
        catalogs = [c.strip() for c in catalogs.split(",") if c.strip()]
    if not catalogs:
        catalogs = [catalog] if catalog else []
    return {
        "host": host,
        "warehouse_id": warehouse_id,
        "catalog": catalog,
        "catalogs": catalogs,
        "token": token,
    }


def resolve_databricks_settings(connection: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read the instance's Databricks settings; ``None`` when unconfigured.

    Row-first: with no explicit ``connection``, looks up the type's default
    ``source_connections`` row (``resolve_source_connection("databricks")``)
    and resolves from it when one exists — ``config.{host, warehouse_id,
    catalog}``, token vault-first via :func:`_resolve_row_token`. Falls back
    to the legacy ``data_source.databricks.*`` instance-config path —
    byte-compatible — when no row is registered yet, which is also what
    every existing zero-arg call and monkeypatch-based test exercises on an
    instance that predates the connection registry.
    """
    if connection is None:
        from src.connection_resolver import resolve_source_connection

        connection = resolve_source_connection("databricks")
    if connection is not None:
        return _resolve_databricks_from_row(connection)
    return _resolve_databricks_from_instance_config()


def _source_ref_for_host(host: str) -> str:
    """Stable per-workspace provenance label: the workspace hostname."""
    parts = urlsplit(host if "://" in host else f"https://{host}")
    return parts.hostname or host


# ---------------------------------------------------------------------------
# metric-view parsing
# ---------------------------------------------------------------------------


def extract_yaml_from_create(create_stmt: str) -> str | None:
    """Pull the YAML body out of a ``SHOW CREATE TABLE`` statement for a
    metric view (``… WITH METRICS LANGUAGE YAML AS $$ <yaml> $$``)."""
    if not create_stmt:
        return None
    m = _YAML_BODY_RE.search(create_stmt)
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def _quote_dbx_ident(name: str) -> str:
    """Backtick-quote a Databricks identifier (doubling embedded backticks)."""
    return "`" + name.replace("`", "``") + "`"


def _escape_sql_literal(value: str) -> str:
    """Double single quotes for embedding inside a '...' SQL literal."""
    return value.replace("'", "''")


def build_metric_rows(
    catalog: str,
    schema: str,
    view: str,
    view_comment: str,
    yaml_text: str,
    *,
    source_ref: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Map one metric view's YAML definition to metric_definitions row dicts —
    one Agnes metric per declared measure.

    Returns ``(rows, None)`` on success or ``([], skip_reason)`` when the
    YAML cannot be interpreted (``skip_reason`` feeds the
    ``skipped_unparseable`` counter).
    """
    try:
        spec = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return [], f"yaml_error: {e}"
    if not isinstance(spec, dict):
        return [], "yaml_not_a_mapping"

    measures = spec.get("measures") or []
    if not isinstance(measures, list) or not measures:
        return [], "no_measures"
    raw_dimensions = spec.get("dimensions") or []
    dimension_names = [str(d.get("name")) for d in raw_dimensions if isinstance(d, dict) and d.get("name")]

    fqn = f"{catalog}.{schema}.{view}"
    quoted_fqn = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"
    rows: list[dict[str, Any]] = []
    for measure in measures:
        if not isinstance(measure, dict):
            continue
        name = measure.get("name")
        if not name or not isinstance(name, str):
            continue
        description = str(measure.get("description") or measure.get("comment") or "") or view_comment or ""
        expression = str(measure.get("expr") or "")
        sql = f"SELECT MEASURE({_quote_dbx_ident(name)}) FROM {quoted_fqn}"
        row: dict[str, Any] = {
            "id": f"databricks/{fqn}/{name}",
            "name": name,
            "display_name": name,
            "category": "databricks",
            "description": description,
            "expression": expression,
            "sql": sql,
            "source": SOURCE_LABEL,
            "notes": [
                f"Unity Catalog metric view {fqn} (source_type=databricks, workspace {source_ref}).",
                (
                    "MEASURE() only evaluates on a Databricks SQL warehouse — run this "
                    "server-side (a query_mode='materialized' row, or adapt the "
                    "materialized row's source_query); group by any listed dimension: "
                    f"SELECT <dimension>, MEASURE({_quote_dbx_ident(name)}) FROM {quoted_fqn} GROUP BY 1."
                ),
            ],
        }
        if dimension_names:
            row["dimensions"] = dimension_names
        rows.append(row)
    if not rows:
        return [], "no_usable_measures"
    return rows, None


# ---------------------------------------------------------------------------
# scope / prune
# ---------------------------------------------------------------------------


def _in_scope(row: dict[str, Any], scope_refs: set) -> bool:
    """True when an existing metric row belongs to this sync's prune scope:
    written by this connector AND stamped with this workspace's ref. Rows
    from other writers (manual, yaml_import, keboola_semantic_layer) or other
    workspaces are untouchable — orphaned-but-intact beats silently deleted."""
    if row.get("source") != SOURCE_LABEL:
        return False
    return row.get("source_ref") in scope_refs


def _is_owned_by_source(existing: dict[str, Any] | None, incoming_id: str, scope_refs: set) -> bool:
    """May this sync write a row under a name ``existing`` already holds?
    Ownership tracks the prune scope — the rows a source may delete are
    exactly the rows it may overwrite; any other writer keeps its name."""
    if existing is None:
        return True
    if existing.get("id") == incoming_id:
        return _in_scope(existing, scope_refs)
    return _in_scope(existing, scope_refs)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _list_metric_views(client: DatabricksStatementClient, catalog: str) -> list[tuple[str, str, str, str]]:
    """Enumerate metric views in one catalog as
    ``(catalog, schema, name, comment)`` tuples, privilege-filtered by the
    warehouse's own information_schema."""
    candidates = ", ".join(f"'{_escape_sql_literal(v)}'" for v in _METRIC_VIEW_TABLE_TYPES)
    sql = (
        "SELECT table_catalog, table_schema, table_name, comment "
        f"FROM {_quote_dbx_ident(catalog)}.information_schema.tables "
        f"WHERE table_type IN ({candidates})"
    )
    _columns, rows = client.execute_rows(sql)
    out: list[tuple[str, str, str, str]] = []
    for row in rows:
        if not row or len(row) < 3 or not row[0] or not row[1] or not row[2]:
            continue
        comment = row[3] if len(row) > 3 and row[3] else ""
        out.append((str(row[0]), str(row[1]), str(row[2]), str(comment)))
    return out


def _log_table_type_vocabulary(client: DatabricksStatementClient, catalogs: list[str]) -> None:
    """Best-effort diagnostic for a zero-metric-view run: report which
    ``table_type`` values the workspace actually publishes.

    Purely observational — never raises, never affects the sync result. If
    the list contains something metric-view-shaped that
    ``_METRIC_VIEW_TABLE_TYPES`` does not cover, that log line is the whole
    diagnosis; if it contains only TABLE/VIEW, the workspace simply has no
    metric views.
    """
    for catalog in catalogs:
        try:
            _cols, rows = client.execute_rows(
                f"SELECT DISTINCT table_type FROM {_quote_dbx_ident(catalog)}.information_schema.tables"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostic probe
            logger.debug("Databricks semantic layer: table_type probe skipped for %s (%s)", catalog, exc)
            continue
        found = sorted({str(r[0]) for r in rows if r and r[0]})
        logger.info(
            "Databricks semantic layer: no metric views matched %s in catalog %r. "
            "table_type values present upstream: %s. If one of those denotes a "
            "metric view, add it to _METRIC_VIEW_TABLE_TYPES in "
            "connectors/databricks/semantic_layer.py.",
            list(_METRIC_VIEW_TABLE_TYPES),
            catalog,
            found or "<none readable>",
        )


def sync_semantic_layer(client: DatabricksStatementClient | None = None) -> dict[str, Any]:
    """Sync the configured workspace's metric views into metric_definitions.

    Pass ``client`` to override construction (tests, future named
    connections); by default the instance's ``data_source.databricks``
    settings + ``DATABRICKS_TOKEN`` are used. Returns a counters dict shaped
    like the Keboola sync result (``status`` + counter keys), with error
    codes the refresh endpoint maps to HTTP statuses.
    """
    settings = resolve_databricks_settings()
    if settings is None:
        return _error_result(
            "Databricks is not configured — set data_source.databricks.host + "
            "warehouse_id (instance.yaml or /admin/server-config) and the "
            "DATABRICKS_TOKEN env var / vault secret.",
            "credentials_not_configured",
        )
    if not settings["catalogs"]:
        return _error_result(
            "data_source.databricks.catalog (or semantic_layer_catalogs) is not set — "
            "the sync needs at least one catalog to enumerate metric views from.",
            "credentials_not_configured",
        )

    if client is None:
        client = DatabricksStatementClient(
            host=settings["host"],
            token=settings["token"],
            warehouse_id=settings["warehouse_id"],
        )
    source_ref = _source_ref_for_host(settings["host"])
    scope_refs = {source_ref}

    from src.repositories import metric_repo

    repo = metric_repo()
    counters = _empty_counters()
    seen_ids: set = set()
    retained_ids: set = set()
    claimed_names: set = set()

    try:
        views: list[tuple[str, str, str, str]] = []
        for cat in settings["catalogs"]:
            views.extend(_list_metric_views(client, cat))

        counters["metric_views_seen"] = len(views)
        if not views:
            # Zero views is either "this workspace has none" (fine) or "the
            # table_type vocabulary drifted past `_METRIC_VIEW_TABLE_TYPES`"
            # (a silent no-op nobody would diagnose from a counter alone).
            # One cheap information_schema probe tells the operator which,
            # and only ever runs on the zero-result path.
            _log_table_type_vocabulary(client, settings["catalogs"])

        for catalog, schema, view, comment in views:
            fqn_quoted = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"
            try:
                _cols, create_rows = client.execute_rows(f"SHOW CREATE TABLE {fqn_quoted}")
            except DatabricksApiError as e:
                logger.warning(
                    "Databricks semantic layer: SHOW CREATE TABLE failed for %s.%s.%s: %s", catalog, schema, view, e
                )
                counters["skipped_unparseable"] += 1
                continue
            create_stmt = str(create_rows[0][0]) if create_rows and create_rows[0] else ""
            yaml_text = extract_yaml_from_create(create_stmt)
            if not yaml_text:
                logger.warning(
                    "Databricks semantic layer: no YAML body found in SHOW CREATE TABLE for %s.%s.%s — skipping",
                    catalog,
                    schema,
                    view,
                )
                counters["skipped_unparseable"] += 1
                continue
            rows, skip_reason = build_metric_rows(catalog, schema, view, comment, yaml_text, source_ref=source_ref)
            if skip_reason is not None:
                logger.warning(
                    "Databricks semantic layer: metric view %s.%s.%s skipped (%s)",
                    catalog,
                    schema,
                    view,
                    skip_reason,
                )
                counters["skipped_unparseable"] += 1
                continue
            for row in rows:
                if row["name"] in claimed_names or not _is_owned_by_source(
                    repo.find_by_name(row["name"]), row["id"], scope_refs
                ):
                    logger.warning(
                        "Databricks semantic metric %r already exists under a different owner; skipping",
                        row["name"],
                    )
                    counters["skipped_conflict"] += 1
                    retained_ids.add(row["id"])
                    continue
                repo.create(**row, source_ref=source_ref)
                seen_ids.add(row["id"])
                claimed_names.add(row["name"])
                counters["created_or_updated"] += 1
    except DatabricksApiError as e:
        code = "upstream_client_error" if (e.status is not None and 400 <= e.status < 500) else "upstream_error"
        return _error_result(str(e), code)

    existing = [m for m in repo.list() if _in_scope(m, scope_refs)]
    if not seen_ids and existing:
        # Zero usable measures while rows exist — a vocabulary/shape drift
        # upstream is far likelier than "every metric view was deleted".
        # Mirror the Keboola guard: skip the prune, log loudly.
        logger.warning(
            "Databricks semantic layer: upstream returned zero usable measures "
            "while %d existing rows are present for workspace %s; skipping prune "
            "to avoid a full wipe. Existing rows retained.",
            len(existing),
            source_ref,
        )
    else:
        for m in existing:
            if m["id"] not in seen_ids and m["id"] not in retained_ids:
                repo.delete(m["id"])
                counters["pruned"] += 1

    return {"status": "ok", "source_ref": source_ref, **counters}
