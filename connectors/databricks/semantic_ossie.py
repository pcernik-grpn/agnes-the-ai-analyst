"""Databricks Unity Catalog metric views -> Apache Ossie document adapter.

Unity Catalog *metric views* are Databricks's semantic layer: YAML-defined
first-class catalog objects declaring a source, dimensions and measures,
queried with the ``MEASURE()`` aggregate — which only Databricks compute can
evaluate. This module composes one Ossie document per metric view instead of
writing ``metric_definitions`` directly (the retired
``connectors/databricks/semantic_layer.py::sync_semantic_layer``); the
document is imported through the standard semantic-source pipeline
(``src/semantic/transports.py``), which projects it into
``metric_definitions`` alongside every other source
(``src/semantic/projection.py``).

Discovery runs on the warehouse itself, unchanged from the retired direct
writer: ``information_schema.tables`` filtered to ``table_type =
'METRIC_VIEW'`` per configured catalog, then ``SHOW CREATE TABLE`` per view,
whose statement embeds the YAML body between ``$$`` delimiters (``CREATE VIEW
… WITH METRICS LANGUAGE YAML AS $$ … $$``).

Expressions are tagged ``DATABRICKS``, mirroring
``connectors/snowflake/semantic_ossie.py``'s ``SNOWFLAKE`` tagging: a
warehouse-specific dialect makes the metric visible in the catalog but
deliberately not spliceable into a local DuckDB query
(``src/semantic/dialect.py``). A measure's declared ``expr`` (e.g.
``SUM(amount)``) is not by itself a runnable query — ``MEASURE()`` only
evaluates against its owning metric view — so the dialect expression composed
here is the full, actionable statement an agent or a ``query_mode=
'materialized'`` row can run server-side: ``SELECT MEASURE(\\`name\\`) FROM
\\`catalog\\`.\\`schema\\`.\\`view\\``` (exactly what the retired direct
writer stored as ``metric_definitions.sql``).

An adapter's only job is to return documents as text (see
``src/semantic/adapters/__init__.py``); it never writes to ``semantic_models``,
``metric_definitions``, ``glossary_terms`` or ``column_metadata`` itself.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from connectors.databricks.client import DatabricksApiError, DatabricksStatementClient
from src.semantic.document_validation import SPEC_VERSION

logger = logging.getLogger(__name__)

_AGNES_VENDOR = "AGNES"

# Every expression composed here is Databricks-flavour. See module docstring.
_DIALECT = "DATABRICKS"

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


def _custom_extension(payload: Dict[str, Any]) -> Dict[str, str]:
    return {"vendor_name": _AGNES_VENDOR, "data": json.dumps(payload)}


def _quote_dbx_ident(name: str) -> str:
    """Backtick-quote a Databricks identifier (doubling embedded backticks)."""
    return "`" + name.replace("`", "``") + "`"


def _escape_sql_literal(value: str) -> str:
    """Double single quotes for embedding inside a '...' SQL literal."""
    return value.replace("'", "''")


def extract_yaml_from_create(create_stmt: str) -> Optional[str]:
    """Pull the YAML body out of a ``SHOW CREATE TABLE`` statement for a
    metric view (``… WITH METRICS LANGUAGE YAML AS $$ <yaml> $$``)."""
    if not create_stmt:
        return None
    m = _YAML_BODY_RE.search(create_stmt)
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def _list_metric_views(client: DatabricksStatementClient, catalog: str) -> List[Tuple[str, str, str, str]]:
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
    out: List[Tuple[str, str, str, str]] = []
    for row in rows:
        if not row or len(row) < 3 or not row[0] or not row[1] or not row[2]:
            continue
        comment = row[3] if len(row) > 3 and row[3] else ""
        out.append((str(row[0]), str(row[1]), str(row[2]), str(comment)))
    return out


def _log_table_type_vocabulary(client: DatabricksStatementClient, catalogs: List[str]) -> None:
    """Best-effort diagnostic for a zero-metric-view run: report which
    ``table_type`` values the workspace actually publishes.

    Purely observational — never raises, never affects what is returned. If
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
            logger.debug("Databricks semantic adapter: table_type probe skipped for %s (%s)", catalog, exc)
            continue
        found = sorted({str(r[0]) for r in rows if r and r[0]})
        logger.info(
            "Databricks semantic adapter: no metric views matched %s in catalog %r. "
            "table_type values present upstream: %s. If one of those denotes a "
            "metric view, add it to _METRIC_VIEW_TABLE_TYPES in "
            "connectors/databricks/semantic_ossie.py.",
            list(_METRIC_VIEW_TABLE_TYPES),
            catalog,
            found or "<none readable>",
        )


def _compose_field(name: str, dim: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    expression = str(dim.get("expr") or "").strip() or _quote_dbx_ident(name)
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": _DIALECT, "expression": expression}]},
    }
    description = str(dim.get("description") or dim.get("comment") or "").strip()
    if description:
        out["description"] = description
    return out


def _compose_metric(name: str, measure: Dict[str, Any], *, quoted_fqn: str, view_comment: str) -> Dict[str, Any]:
    # `MEASURE()` only evaluates against the metric view that declares it —
    # the raw `expr` (e.g. `SUM(amount)`) is not by itself something an agent
    # could run anywhere. The dialect expression is therefore the composed,
    # actionable statement, matching what the retired direct writer stored as
    # `metric_definitions.sql` (see module docstring).
    expression = f"SELECT MEASURE({_quote_dbx_ident(name)}) FROM {quoted_fqn}"
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": _DIALECT, "expression": expression}]},
    }
    description = str(measure.get("description") or measure.get("comment") or "").strip() or view_comment
    if description:
        out["description"] = description
    raw_expr = str(measure.get("expr") or "").strip()
    if raw_expr:
        # Ossie's Metric has no slot for the measure's own aggregation
        # fragment (only the composed, runnable expression above) — carried
        # rather than dropped, the same way the Snowflake adapter carries
        # `access_modifier` and `table` under `custom_extensions`.
        out["custom_extensions"] = [_custom_extension({"measure_expr": raw_expr})]
    return out


def compose_document(catalog: str, schema: str, view: str, view_comment: str, yaml_text: str) -> Optional[str]:
    """Compose one metric view's YAML definition into an Ossie document.

    Returns ``None`` when the YAML cannot be interpreted as a metric view
    with at least one usable measure — mirrors the retired direct writer's
    ``skipped_unparseable`` / ``no_measures`` / ``no_usable_measures`` skip
    reasons, just without the counters (the generic importer counts its own
    way: an empty document list from ``extract`` for one view is simply one
    fewer document to import).
    """
    try:
        spec = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        logger.warning("Databricks semantic adapter: %s.%s.%s YAML is invalid: %s; skipping", catalog, schema, view, e)
        return None
    if not isinstance(spec, dict):
        logger.warning(
            "Databricks semantic adapter: %s.%s.%s YAML body is not a mapping; skipping", catalog, schema, view
        )
        return None

    raw_measures = spec.get("measures") or []
    if not isinstance(raw_measures, list) or not raw_measures:
        logger.warning("Databricks semantic adapter: %s.%s.%s declares no measures; skipping", catalog, schema, view)
        return None

    fqn = f"{catalog}.{schema}.{view}"
    quoted_fqn = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"

    metrics: List[Dict[str, Any]] = []
    for measure in raw_measures:
        if not isinstance(measure, dict):
            continue
        name = measure.get("name")
        if not name or not isinstance(name, str):
            continue
        metrics.append(_compose_metric(name, measure, quoted_fqn=quoted_fqn, view_comment=view_comment))
    if not metrics:
        logger.warning(
            "Databricks semantic adapter: %s.%s.%s declares no usable (named) measures; skipping", catalog, schema, view
        )
        return None

    fields: List[Dict[str, Any]] = []
    raw_dimensions = spec.get("dimensions") or []
    if isinstance(raw_dimensions, list):
        for dim in raw_dimensions:
            if not isinstance(dim, dict):
                continue
            name = dim.get("name")
            if not name or not isinstance(name, str):
                continue
            field = _compose_field(name, dim)
            if field is not None:
                fields.append(field)

    dataset: Dict[str, Any] = {"name": view, "source": str(spec.get("source") or "").strip() or fqn}
    if fields:
        dataset["fields"] = fields

    # Fully qualified, not the bare view name: the importer keys storage on
    # the model name and collapses duplicates, so two same-named metric views
    # in different schemas would silently overwrite each other.
    semantic_model: Dict[str, Any] = {"name": fqn, "datasets": [dataset], "metrics": metrics}
    if view_comment:
        semantic_model["description"] = view_comment
    semantic_model["custom_extensions"] = [_custom_extension({"metric_view": fqn})]

    document = {"version": SPEC_VERSION, "semantic_model": [semantic_model]}
    return yaml.safe_dump(document, sort_keys=False)


class DatabricksMetricViewAdapter:
    """Reads the configured Databricks workspace's Unity Catalog metric views
    and composes one Ossie document each.

    ``config`` carries only SCOPE — an optional ``catalogs`` (list or
    comma-separated string) narrowing which catalogs are enumerated, defaults
    to the connection's own configured catalogs
    (``resolve_databricks_settings()["catalogs"]``). Credentials are never
    taken from it: they resolve from the instance's Databricks connection
    exactly as every other Databricks code path does, so a semantic source
    row never becomes a second place a warehouse credential is stored.
    """

    def unconfigured_reason(self, config: Dict[str, Any]) -> Optional[str]:
        """The optional pre-flight hook (``src/semantic/adapters/__init__.py``):
        ``None`` when this instance has a Databricks workspace to read, a
        reason when it does not.

        This is what a source that OUTLIVED its configuration needs.
        ``ensure_semantic_source()`` refuses to create the Databricks source
        row on an unconfigured instance, but it returns an existing row's id
        before that gate — so a workspace deconfigured after registration
        (credentials rotated out, connection removed) left the row importing
        on every sweep, raising :meth:`extract`'s "not configured" every time,
        forever. The sweep asks this first and skips the row instead; the row
        itself is untouched, because an existing row is one an admin shaped
        and an outage is not consent to delete it.
        """
        # Imported at call time for the same reason `extract` does it below.
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        if resolve_databricks_settings():
            return None
        return (
            "Databricks is not configured on this instance (data_source.databricks.host + "
            "warehouse_id, and the DATABRICKS_TOKEN env var / vault secret); skipping this "
            "source until it is configured again"
        )

    def extract(self, config: Dict[str, Any]) -> List[str]:
        # Imported at call time, not module scope, so a test patching the
        # defining module reaches this lookup (same reason as the Snowflake
        # adapter's local import of resolve_snowflake_settings).
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        settings = resolve_databricks_settings()
        if not settings:
            # Still fatal HERE: a manual `POST /semantic-sources/{id}/sync` an
            # admin asked for must say why it did nothing, and the pre-flight
            # hook above is only consulted by the scheduled sweep.
            raise RuntimeError(
                "Databricks is not configured (data_source.databricks.host + warehouse_id, "
                "and the DATABRICKS_TOKEN env var / vault secret); refusing to sync semantic views"
            )

        catalogs = config.get("catalogs") or settings.get("catalogs") or []
        if isinstance(catalogs, str):
            catalogs = [c.strip() for c in catalogs.split(",") if c.strip()]
        if not catalogs:
            raise RuntimeError(
                "no Databricks catalog configured (data_source.databricks.catalog / "
                "semantic_layer_catalogs, or this source's config.catalogs); refusing to sync"
            )

        client = self._client(settings)
        views: List[Tuple[str, str, str, str]] = []
        for catalog in catalogs:
            views.extend(_list_metric_views(client, catalog))
        if not views:
            # Zero views is either "this workspace has none" (fine) or the
            # `table_type` vocabulary drifted past `_METRIC_VIEW_TABLE_TYPES`
            # (a silent no-op nobody would diagnose from an empty list alone).
            _log_table_type_vocabulary(client, catalogs)
            return []

        documents: List[str] = []
        for catalog, schema, view, comment in views:
            fqn_quoted = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"
            try:
                _cols, create_rows = client.execute_rows(f"SHOW CREATE TABLE {fqn_quoted}")
            except DatabricksApiError as e:
                logger.warning(
                    "Databricks semantic adapter: SHOW CREATE TABLE failed for %s.%s.%s: %s; skipping",
                    catalog,
                    schema,
                    view,
                    e,
                )
                continue
            create_stmt = str(create_rows[0][0]) if create_rows and create_rows[0] else ""
            yaml_text = extract_yaml_from_create(create_stmt)
            if not yaml_text:
                logger.warning(
                    "Databricks semantic adapter: no YAML body found in SHOW CREATE TABLE for %s.%s.%s; skipping",
                    catalog,
                    schema,
                    view,
                )
                continue
            text = compose_document(catalog, schema, view, comment, yaml_text)
            if text is not None:
                documents.append(text)
        return documents

    def _client(self, settings: Dict[str, Any]) -> DatabricksStatementClient:
        return DatabricksStatementClient(
            host=settings["host"],
            token=settings["token"],
            warehouse_id=settings["warehouse_id"],
        )
