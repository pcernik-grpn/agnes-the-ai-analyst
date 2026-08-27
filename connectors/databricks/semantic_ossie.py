"""Databricks Unity Catalog metric views -> Apache Ossie document adapter.

`connectors/databricks/semantic_layer.py` used to flatten a Unity Catalog
metric view's YAML definition straight into `metric_definitions`, one Agnes
metric row per declared measure, under `source='databricks_semantic_layer'`.
This module composes the SAME discovery (`information_schema.tables` filtered
to `METRIC_VIEW`, `SHOW CREATE TABLE` for the YAML body) into a canonical
Ossie document instead — one document per metric view, mirroring
`connectors/keboola/semantic_ossie.py::compose_document` (one document per
`semantic-model`).

An adapter's only job is to return documents as text (see
`src/semantic/adapters/__init__.py`); it never writes to `semantic_models`,
`metric_definitions`, `glossary_terms` or `column_metadata` itself.

A Unity Catalog metric view has no analogue to Keboola's relationships,
constraints or glossary — its YAML declares only a `source`, `dimensions[]`
and `measures[]` — so this adapter is a small subset of the Keboola one: one
model, one dataset, no relationships/constraints/glossary.

Every composed metric's expression is tagged with the vendored Ossie
`DATABRICKS` dialect (never `DUCKDB`/`ANSI_SQL`), because `MEASURE()` is a
Databricks-only aggregate — no local DuckDB engine can evaluate it. Tagging it
this way is what makes the existing `src.semantic_validation.check_dialects` /
`src.semantic.dialect.resolve_expression` correctly report a query using one
of these metrics as not locally executable, with no change to their logic.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.semantic.document_validation import SPEC_VERSION

logger = logging.getLogger(__name__)

# The vendored Ossie Dialect enum value every measure expression this adapter
# composes is tagged with (src/semantic/schema/osi-schema.json `Dialect` def).
DATABRICKS_DIALECT = "DATABRICKS"


def _compose_field(dimension: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One `dimensions[]` entry -> an Ossie `Field`.

    `Field.expression` is REQUIRED by the vendored schema; a Unity Catalog
    dimension always declares `expr` (verified against every live-observed
    metric-view YAML — see the Databricks metric-view YAML reference), so this
    falls back to the bare dimension name only for a malformed entry missing
    it, rather than dropping the field outright.
    """
    name = dimension.get("name")
    if not name or not isinstance(name, str):
        return None
    expr = dimension.get("expr")
    expr = str(expr) if expr else name
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": DATABRICKS_DIALECT, "expression": expr}]},
    }
    description = dimension.get("description") or dimension.get("comment")
    if description:
        out["description"] = str(description)
    return out


def _compose_metric(measure: Dict[str, Any], *, quoted_fqn: str) -> Optional[Dict[str, Any]]:
    """One `measures[]` entry -> an Ossie `Metric`.

    The composed expression is the FULL runnable statement
    (`SELECT MEASURE(...) FROM <metric view>`), not a bare aggregation
    fragment: unlike a Keboola metric (bound to a registered Agnes table and
    composed by the projector via `_bind_metric`), a Databricks metric view is
    a warehouse-side catalog object with nothing in Agnes's own table_registry
    to bind against. Composing the whole statement here — and declaring no
    `dataset` binding in the AGNES custom_extensions — makes the projector
    take the "no binding declared" branch (`src/semantic/projection.py
    ::_bind_metric`) and store this text as `metric_definitions.sql` verbatim.
    """
    from connectors.databricks.semantic_layer import _quote_dbx_ident

    name = measure.get("name")
    if not name or not isinstance(name, str):
        return None
    sql = f"SELECT MEASURE({_quote_dbx_ident(name)}) FROM {quoted_fqn}"
    description = str(measure.get("description") or measure.get("comment") or "")
    note = (
        "MEASURE() only evaluates on a Databricks SQL warehouse — this is not "
        "locally executable in DuckDB; run it server-side."
    )
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": DATABRICKS_DIALECT, "expression": sql}]},
        "description": f"{description}\n\n{note}" if description else note,
    }
    return out


def compose_document(
    catalog: str, schema: str, view: str, comment: str, yaml_text: str
) -> Tuple[Optional[str], Optional[str]]:
    """Compose one metric view's YAML definition into an Ossie document (YAML
    text), or ``(None, skip_reason)`` when the YAML cannot be interpreted.

    ``skip_reason`` values (``yaml_error``, ``yaml_not_a_mapping``,
    ``no_measures``, ``no_usable_measures``) mirror the ones the retired flat
    composer (`connectors/databricks/semantic_layer.py::build_metric_rows`)
    reported under its `skipped_unparseable` counter.
    """
    try:
        spec = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return None, f"yaml_error: {e}"
    if not isinstance(spec, dict):
        return None, "yaml_not_a_mapping"

    raw_measures = spec.get("measures") or []
    if not isinstance(raw_measures, list) or not raw_measures:
        return None, "no_measures"

    from connectors.databricks.semantic_layer import _quote_dbx_ident

    fqn = f"{catalog}.{schema}.{view}"
    quoted_fqn = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"

    metrics = [
        m
        for m in (_compose_metric(measure, quoted_fqn=quoted_fqn) for measure in raw_measures if isinstance(measure, dict))
        if m is not None
    ]
    if not metrics:
        return None, "no_usable_measures"

    raw_dimensions = spec.get("dimensions") or []
    fields = (
        [f for f in (_compose_field(d) for d in raw_dimensions if isinstance(d, dict)) if f is not None]
        if isinstance(raw_dimensions, list)
        else []
    )

    dataset: Dict[str, Any] = {"name": view, "source": str(spec.get("source") or fqn)}
    if comment:
        dataset["description"] = comment
    if fields:
        dataset["fields"] = fields

    # `fqn` (not the bare view name) is the model's `name` — Ossie models are
    # keyed by name for prune/id purposes (src/semantic/projection.py
    # ::_model_key falls back to `name` when a document carries no explicit
    # identity extension), and two catalogs both holding a view named
    # `orders_metrics` must not collapse onto one document.
    semantic_model: Dict[str, Any] = {"name": fqn, "datasets": [dataset], "metrics": metrics}
    if comment:
        semantic_model["description"] = comment

    document = {"version": SPEC_VERSION, "semantic_model": [semantic_model]}
    return yaml.safe_dump(document, sort_keys=False), None


def extract_documents(client, catalogs: List[str]) -> Tuple[List[str], Dict[str, int]]:
    """Discover every metric view across ``catalogs`` and compose one Ossie
    document per view. Returns ``(documents, counters)`` —
    ``metric_views_seen`` / ``skipped_unparseable`` — for callers (the sync
    entrypoint) that report them; `DatabricksMetricViewAdapter.extract`
    discards the counters and returns just the documents, per the adapter
    contract.

    A `DatabricksApiError` raised while listing metric views (the discovery
    query itself failing) propagates — the caller aborts the whole run rather
    than reaching a prune with an empty document list. A `SHOW CREATE TABLE`
    failure for ONE view is caught and counted instead: one broken/dropped
    view must not abort every other view's sync.
    """
    from connectors.databricks.client import DatabricksApiError
    from connectors.databricks.semantic_layer import (
        _list_metric_views,
        _log_table_type_vocabulary,
        _quote_dbx_ident,
        extract_yaml_from_create,
    )

    counters = {"metric_views_seen": 0, "skipped_unparseable": 0}
    views: List[Tuple[str, str, str, str]] = []
    for cat in catalogs:
        views.extend(_list_metric_views(client, cat))
    counters["metric_views_seen"] = len(views)

    if not views:
        # Zero views is either "this workspace has none" (fine) or a
        # table_type vocabulary drift nobody would otherwise diagnose from a
        # counter alone — mirrors the retired flat sync's own probe.
        _log_table_type_vocabulary(client, catalogs)
        return [], counters

    documents: List[str] = []
    for catalog, schema, view, comment in views:
        fqn_quoted = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"
        try:
            _cols, create_rows = client.execute_rows(f"SHOW CREATE TABLE {fqn_quoted}")
        except DatabricksApiError as e:
            logger.warning(
                "Databricks Ossie adapter: SHOW CREATE TABLE failed for %s.%s.%s: %s", catalog, schema, view, e
            )
            counters["skipped_unparseable"] += 1
            continue
        create_stmt = str(create_rows[0][0]) if create_rows and create_rows[0] else ""
        yaml_text = extract_yaml_from_create(create_stmt)
        if not yaml_text:
            logger.warning(
                "Databricks Ossie adapter: no YAML body found in SHOW CREATE TABLE for %s.%s.%s — skipping",
                catalog,
                schema,
                view,
            )
            counters["skipped_unparseable"] += 1
            continue
        text, skip_reason = compose_document(catalog, schema, view, comment, yaml_text)
        if text is None:
            logger.warning(
                "Databricks Ossie adapter: metric view %s.%s.%s skipped (%s)", catalog, schema, view, skip_reason
            )
            counters["skipped_unparseable"] += 1
            continue
        documents.append(text)
    return documents, counters


class DatabricksMetricViewAdapter:
    """Fetches a Databricks workspace's Unity Catalog metric views and
    composes one Ossie document per metric view.

    ``config`` is ``{"host", "warehouse_id", "token", "catalogs"}`` — the same
    connection shape `resolve_databricks_settings()` already resolves per
    configured instance (``catalogs`` may also be given as the single
    ``"catalog"`` key, for symmetry with that resolver's return shape).
    ``config["client"]``, when given, overrides construction (tests). This
    adapter owns its own statement client the same way the Keboola adapter
    owns its own Metastore client — a self-contained "hand it connection
    config, get documents back" contract.
    """

    def extract(self, config: Dict[str, Any]) -> List[str]:
        from connectors.databricks.client import DatabricksStatementClient

        host = config.get("host")
        token = config.get("token")
        warehouse_id = config.get("warehouse_id")
        if not (host and token and warehouse_id):
            raise ValueError(
                "DatabricksMetricViewAdapter requires config['host'], config['warehouse_id'] and config['token']"
            )
        catalogs = config.get("catalogs") or ([config["catalog"]] if config.get("catalog") else [])
        if not catalogs:
            raise ValueError("DatabricksMetricViewAdapter requires config['catalogs'] (or config['catalog'])")

        client = config.get("client") or DatabricksStatementClient(host=host, token=token, warehouse_id=warehouse_id)
        documents, _counters = extract_documents(client, catalogs)
        return documents
