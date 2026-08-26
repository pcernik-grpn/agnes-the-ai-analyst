"""Databricks Unity Catalog semantic layer → Apache Ossie documents.

Unity Catalog *metric views* are Databricks's semantic layer: YAML-defined
first-class catalog objects declaring a source, dimensions and measures,
queried with the ``MEASURE()`` aggregate — which only Databricks compute can
evaluate. ``sync_semantic_layer`` mirrors those definitions into Agnes's
semantic layer so agents discover them through the standard rails (the
semantic-model browse/export surfaces, ``validate_semantic_query``) instead
of inventing their own calculations.

Since the semantic-phase-1 cutover (mirroring the Keboola one —
``connectors/keboola/semantic_layer.py::_sync_one_source`` /
``connectors/keboola/semantic_ossie.py``), the mapping itself lives in
``connectors/databricks/semantic_ossie.py``: it composes one Ossie document
per metric view, stored whole under ``source='databricks_metrics'`` in
``semantic_models``, then run through
``src.semantic.projection.project_document`` — the SINGLE writer of the flat
query tables since the cutover. Every measure is tagged with ONLY the
``DATABRICKS`` Ossie dialect (``MEASURE()`` isn't valid DuckDB syntax), the
same choice ``connectors/snowflake/semantic_ossie.py`` already made for its
own warehouse-only metrics — so the projector never composes a
``metric_definitions`` row for one (see ``sync_semantic_layer``'s own
docstring for the full rationale). This module keeps only the pieces shared
across both the old (deleted) and new mapping: settings resolution, the
workspace-host provenance label, and the warehouse discovery/parsing
primitives (``_list_metric_views``, ``extract_yaml_from_create``,
``_quote_dbx_ident``) the adapter imports.

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

from connectors.databricks.client import (
    DatabricksApiError,
    DatabricksStatementClient,
)

logger = logging.getLogger(__name__)

# The current writer's `semantic_models.source` label (Ossie/projection path,
# since the cutover).
SOURCE_LABEL = "databricks_metrics"

# The RETIRED flat composer's label. `sync_semantic_layer` purges any row
# still stamped with it, within this sync's own (workspace) scope, the same
# one-time-retirement pattern `connectors/keboola/semantic_layer.py
# ::_sync_one_source` uses for `keboola_semantic_layer`.
_LEGACY_SOURCE_LABEL = "databricks_semantic_layer"

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


def resolve_databricks_settings() -> dict[str, Any] | None:
    """Read the instance's Databricks settings; ``None`` when unconfigured.

    ``data_source.databricks.{host, warehouse_id, catalog}`` from the
    effective instance.yaml (admin-overlay aware), token from the env var
    named by ``token_env`` (default ``DATABRICKS_TOKEN``) with the vault
    (``datasource_secret``) as fallback — the same resolution order the
    Keboola materialized path uses.
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


# ---------------------------------------------------------------------------
# discovery (shared with connectors/databricks/semantic_ossie.py)
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
    """Sync the configured workspace's Unity Catalog metric views into
    Agnes's semantic layer, via the Ossie document path.

    Composes one Ossie document per metric view
    (``connectors.databricks.semantic_ossie.extract_documents``), stores them
    whole under ``source='databricks_metrics'``/``source_ref=<workspace
    host>`` in ``semantic_models``, then runs them through
    ``src.semantic.projection.project_document`` — the SINGLE writer of the
    flat query tables since the cutover (mirrors
    ``connectors/keboola/semantic_layer.py::_sync_one_source``).

    Every measure's expression is composed as the FULL runnable statement
    (``SELECT MEASURE(...) FROM <metric view>``, see
    ``connectors.databricks.semantic_ossie._compose_metric``) and tagged
    ONLY with the ``DATABRICKS`` Ossie dialect — never ``DUCKDB``/``ANSI_SQL``,
    because ``MEASURE()`` is not valid DuckDB syntax at all. This is the SAME
    choice ``connectors/snowflake/semantic_ossie.py`` already made for its own
    warehouse-only metrics (see that module's docstring): ``src.semantic
    .dialect.resolve_expression`` therefore skips composing a
    ``metric_definitions`` row for every measure here, same as it does for
    every Snowflake semantic-view metric. These metrics are NOT missing —
    they are fully readable (catalog, expression, description) through the
    semantic-model document surfaces (``agnes catalog --metrics --show`` on
    the stored document, export, ``validate_semantic_query`` — which reads
    ``expression.dialects`` off the document, not off ``metric_definitions``,
    and correctly reports them as not locally executable) — just not through
    the ``metric_definitions`` flat listing, which promises a row's ``sql`` is
    DuckDB-runnable. Splicing a warehouse-only dialect into that table under a
    different label would be exactly the "parses but silently means something
    else" trap ``resolve_expression``'s own docstring warns against.

    Because of that, this sync's counters describe the DOCUMENT-level unit of
    work (metric views), not `metric_definitions` rows:
    ``created_or_updated``/``pruned`` count ``semantic_models`` upserts/prunes
    (mirrors ``ImportReport.models_written``/``models_pruned`` in the generic
    pipeline — the flat-table equivalents would be permanently zero here).
    A ``documents == []`` pass from ``extract_documents`` (zero metric views
    found, or every view's ``SHOW CREATE TABLE`` call failing transiently —
    neither raises ``DatabricksApiError``) never prunes previously-stored
    ``semantic_models`` documents for this workspace; it is indistinguishable
    from "upstream genuinely has none now", so the prune is skipped and
    logged instead — mirrors ``connectors/keboola/semantic_layer.py
    ::_sync_one_source``'s own ``if not models`` guard. (``project_document``'s
    own ``safe_prune=True`` below is a SEPARATE guard, scoped to the flat
    ``metric_definitions``/glossary tables it writes — it does not, by
    itself, protect ``semantic_models``.)

    Any row still stamped with the retired
    ``source='databricks_semantic_layer'`` label is purged within this
    workspace's own scope once at least one metric view's document was
    actually stored this pass AND no view was dropped along the way
    (``partial_composition``) — the same one-time-legacy-retirement guard the
    Keboola cutover uses, so a broken or partially-broken upstream fetch can
    never delete the last good copy of a metric.

    Pass ``client`` to override construction (tests, future named
    connections); by default the instance's ``data_source.databricks``
    settings + ``DATABRICKS_TOKEN`` are used. Returns a counters dict shaped
    like the pre-cutover sync result (``status`` + counter keys), with error
    codes the refresh endpoint maps to HTTP statuses. ``skipped_conflict`` is
    always 0 for this connector post-cutover (nothing reaches
    ``metric_definitions`` to conflict over) — kept in the shape for backward
    API compatibility rather than removed; see
    ``src/semantic/projection.py::_check_name_collision`` for the generic
    (Keboola/Snowflake-reachable) replacement this connector no longer needs.
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
    counters = _empty_counters()

    from connectors.databricks.semantic_ossie import extract_documents

    try:
        documents, discovery_counters = extract_documents(client, settings["catalogs"])
    except DatabricksApiError as e:
        code = "upstream_client_error" if (e.status is not None and 400 <= e.status < 500) else "upstream_error"
        return _error_result(str(e), code)
    counters["metric_views_seen"] = discovery_counters["metric_views_seen"]
    counters["skipped_unparseable"] = discovery_counters["skipped_unparseable"]

    import hashlib
    from datetime import datetime, timezone

    from src.repositories import metric_repo, semantic_model_repo
    from src.semantic.document_validation import validate_document
    from src.semantic.projection import project_document

    repo = semantic_model_repo()
    existing_by_slug = {m["slug"]: m for m in repo.list_all(source=SOURCE_LABEL, source_ref=source_ref)}
    keep_slugs: list[str] = []
    parsed_documents: list[dict] = []

    for text in documents:
        result = validate_document(text)
        if not result.ok:
            # No slug to key storage on or protect from prune — logged and
            # dropped, consistent with import_documents' own handling of an
            # invalid document.
            logger.warning(
                "Databricks semantic layer: composed Ossie document failed validation for workspace %s: %s",
                source_ref,
                "; ".join(result.errors),
            )
            continue
        models = (result.parsed or {}).get("semantic_model") or []
        slug = models[0].get("name") if models else None
        if not slug:
            continue
        # A metric view's fqn (catalog.schema.view) is globally unique within
        # one workspace by construction — unlike Keboola's model NAME, which
        # can collide across models and needs the disambiguation
        # `_store_ossie_documents` applies.
        keep_slugs.append(slug)
        parsed_documents.append(result.parsed)

        content_hash = hashlib.sha256(text.encode()).hexdigest()
        existing = existing_by_slug.get(slug)
        if existing is not None and existing.get("content_hash") == content_hash:
            continue
        repo.upsert(
            id="/".join([SOURCE_LABEL, source_ref, slug]),
            slug=slug,
            name=slug,
            description=None,
            document=text,
            document_json=result.parsed,
            spec_version=result.spec_version,
            content_hash=content_hash,
            source=SOURCE_LABEL,
            source_ref=source_ref,
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(timezone.utc),
        )
        counters["created_or_updated"] += 1

    # `validate_document` above logs-and-drops any single composed document
    # that fails schema validation. When that happens the merged model list
    # below is a PARTIAL view of what `extract_documents` actually composed
    # — projecting it with pruning at full scope would delete the dropped
    # view's own previously-written rows (in particular its `column_metadata`
    # rows, if it shares an underlying table with a view that DID survive),
    # which upstream never asked to have removed. `partial=True` NARROWS the
    # projector's prune to the views this call actually carried rather than
    # skipping it wholesale — mirrors `connectors/keboola/semantic_layer.py`'s
    # own `partial_composition` handling for exactly this scenario.
    #
    # This projection call MUST run before the `semantic_models`
    # `delete_missing` below: `partial=True` makes the projector protect a
    # dropped view's already-projected `column_metadata` rows by reading the
    # OTHER currently-stored valid models' claims for the same table from
    # `semantic_models` (`_sibling_column_claims`) — if the dropped view's
    # own `semantic_models` row were already deleted first, there would be
    # no sibling row left to find its claimed columns in, and the
    # protection could never work regardless of `partial`.
    partial_composition = discovery_counters["skipped_unparseable"] > 0 or len(parsed_documents) < len(documents)
    if partial_composition:
        logger.warning(
            "Databricks semantic layer: %d of %d composed metric-view document(s) failed validation "
            "and/or %d metric view(s) could not be composed at all this pass (SHOW CREATE TABLE "
            "failure or unparseable YAML, source_ref=%s); narrowing the prune to the views that "
            "survived, so the dropped view(s)' previously-written rows are left intact.",
            len(documents) - len(parsed_documents),
            len(documents),
            discovery_counters["skipped_unparseable"],
            source_ref,
        )

    merged: dict[str, list] = {"semantic_model": []}
    for doc in parsed_documents:
        merged["semantic_model"].extend(doc.get("semantic_model") or [])
    # safe_prune=True: an upstream fetch that returns zero usable measures
    # while rows exist must not wipe the registry — same full-wipe guard the
    # retired flat sync carried (see project_document's own docstring).
    # `report.metrics_written`/`metrics_pruned` are not read here — see the
    # module/function docstring: every Databricks measure is DATABRICKS-only
    # dialect, so the projector never writes a metric_definitions row for it.
    report = project_document(
        merged, source=SOURCE_LABEL, source_ref=source_ref, safe_prune=True, partial=partial_composition
    )
    counters["skipped_conflict"] = report.name_collisions

    # A `documents == []` pass — zero metric views found, or every view's
    # `SHOW CREATE TABLE` call failing transiently and being swallowed into
    # `skipped_unparseable` by `extract_documents` (neither raises
    # `DatabricksApiError`, so the `except` above never fires) — is
    # indistinguishable here from "upstream genuinely has zero metric views
    # now". Calling `delete_missing` with an empty `keep_slugs` in that case
    # would wipe every previously-stored document for this workspace on what
    # may be a transient fetch failure. Mirrors
    # `connectors/keboola/semantic_layer.py::_sync_one_source`'s own
    # `if not models: return empty_result` guard — skip only the prune (the
    # rest of this pass, e.g. discovery counters, still runs) and log loudly
    # instead of silently deleting good data.
    #
    # A `partial_composition` pass (SOME but not all views dropped, either
    # here or earlier in `extract_documents`) gets the SAME skip: `keep_slugs`
    # only lists the views that survived THIS pass, so a view missing from it
    # is indistinguishable from "genuinely removed upstream" vs "still
    # dropping transiently" — exactly the ambiguity `partial=True` already
    # narrows `project_document`'s prune around, above. Without this, a
    # transient per-view failure (e.g. a persistent `SHOW CREATE TABLE`
    # permission issue on one view) would still delete that view's own
    # `semantic_models` document outright, even though `project_document`
    # was just told to spare its `column_metadata` rows — defeating the
    # point of that protection.
    #
    # Runs AFTER `project_document` above (see that call's comment for why):
    # this document-level prune only removes the now-stale `semantic_models`
    # row itself, once the projector has already read it to protect the
    # dropped view's `column_metadata` rows.
    if (not documents or partial_composition) and existing_by_slug:
        if not documents:
            logger.warning(
                "Databricks semantic layer: upstream returned zero metric-view documents for "
                "workspace %s while %d document(s) were previously stored — skipping the "
                "semantic_models prune this pass instead of risking a wipe on a transient fetch "
                "failure (see connectors/databricks/semantic_ossie.py::extract_documents).",
                source_ref,
                len(existing_by_slug),
            )
        else:
            logger.warning(
                "Databricks semantic layer: one or more metric view(s) were dropped this pass — "
                "%d failed validation, %d could not be composed at all (SHOW CREATE TABLE failure "
                "or unparseable YAML) — for workspace %s while %d document(s) were previously "
                "stored; skipping the semantic_models prune this pass instead of risking deletion "
                "of a view that only failed transiently.",
                len(documents) - len(parsed_documents),
                discovery_counters["skipped_unparseable"],
                source_ref,
                len(existing_by_slug),
            )
        pruned_slugs: list[str] = []
    else:
        pruned_slugs = repo.delete_missing(source=SOURCE_LABEL, source_ref=source_ref, keep_slugs=keep_slugs)
    counters["pruned"] = len(pruned_slugs)

    # One-time retirement of the pre-cutover source, scoped to this
    # workspace. Gated on this pass having actually stored at least one
    # metric view's document — an empty/failed upstream fetch (0 documents)
    # must never delete the last good copy of a legacy row — AND on
    # `not partial_composition`, the same guard the Keboola twin's purge uses
    # (`connectors/keboola/semantic_layer.py::_sync_one_source`): when one
    # view stored fine while another dropped transiently this pass,
    # `keep_slugs` is non-empty but the dropped view's own document was never
    # rewritten — purging would delete ITS legacy row too, and nothing this
    # pass recreates the metric. Idempotent: a later fully-valid sync finds
    # the rows and retires them.
    if keep_slugs and not partial_composition:
        legacy_repo = metric_repo()
        for m in legacy_repo.list():
            if (m.get("source") or "") == _LEGACY_SOURCE_LABEL and (m.get("source_ref") or "") == source_ref:
                legacy_repo.delete(m["id"])
                counters["pruned"] += 1

    return {"status": "ok", "source_ref": source_ref, **counters}
