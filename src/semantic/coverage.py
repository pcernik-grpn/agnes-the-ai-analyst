"""Cross-domain coverage — "what does each data source still lack?"

One row per connected data source, one column per domain: does it have a
semantic model, metrics, glossary terms, a skill, a specialized agent, a
knowledge base? Deliberately cross-SOURCE and cross-DOMAIN, which is what
separates it from the Keboola-specific binding-coverage engine
(``connectors.keboola.semantic_layer.compute_semantic_coverage``, "K0.5").
That engine is not replaced here — it is the richest of the semantic
providers below, wrapped, never rewritten.

Two design rules worth stating, because both were deliberate:

* **``not_applicable`` is not ``missing``.** A BigQuery connection has no
  semantic-layer adapter in this build at all (three exist:
  ``src/semantic/adapters/__init__.py``), so reporting it as "missing" —
  next to an action link into a create flow that does not exist for it —
  would invent work nobody can do. Only a source whose domain COULD be
  filled reports ``missing``.
* **A source's detail is as rich as its connector can compute, never as
  rich as its brand.** The Keboola rows carry a fat ``raw`` payload (token
  identity mismatches, metrics blocked by their own definition,
  unregistered dataset tables) because K0.5 computes all of that; a
  Snowflake row's ``raw`` is thin because its adapter does not compute it
  yet. Same place, same component, same shape — one field, filled to
  whatever depth exists.

**POSTGRES-ONLY** (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"): the skill/agent/knowledge-base columns read
``resource_source_tags``, which has no DuckDB implementation. The repository
is resolved UP FRONT, before any per-source work, so a DuckDB-backed
instance fails immediately and identically with the typed
``RequiresPostgresBackend`` (translated to ``501`` by ``app/main.py``)
instead of the answer depending on whether any source happens to be
connected.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Domain status vocabulary. No percentage, no score, no total — a roll-up is
# free to weight these later (`ok`=1, `partial`=0.5, `missing`=0) without the
# shape changing, but the report itself does not gamify.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
STATUS_NOT_APPLICABLE = "not_applicable"

#: Every domain a source is scored on, in render order.
DOMAINS = ("semantic", "metrics", "glossary", "skill", "agent", "knowledge_base")

#: Which semantic adapter (``src/semantic/adapters``) can read a given
#: ``source_connections.source_type``. A source type absent from this map has
#: no adapter in this build, so its semantic column is ``not_applicable``
#: rather than ``missing``.
SEMANTIC_ADAPTER_BY_SOURCE_TYPE: Dict[str, str] = {
    "keboola": "keboola_metastore",
    "snowflake": "snowflake_semantic",
}

#: ``resource_source_tags.resource_type`` -> the coverage domain it fills.
#: The three types reuse the ``app.resource_types.ResourceType`` vocabulary
#: (and its ``resource_id`` path conventions) so a tag and a grant name the
#: same object the same way.
TAG_DOMAIN_BY_RESOURCE_TYPE: Dict[str, str] = {
    "marketplace_plugin": "skill",
    "agent": "agent",
    "memory_domain": "knowledge_base",
}
TAG_RESOURCE_TYPE_BY_DOMAIN: Dict[str, str] = {v: k for k, v in TAG_DOMAIN_BY_RESOURCE_TYPE.items()}

#: The synthetic row for registered tables that belong to no
#: ``source_connections`` row (``table_registry.connection_id IS NULL``):
#: locally-loaded files, hand-registered tables, and rows registered before
#: connections existed. Without it those tables are in no source's column and
#: silently disappear from the report — the same "counted nowhere" hole the
#: retired page's "legacy / unattributed" bucket existed to close.
LOCAL_BUCKET_NAME = "Local / no connection"

#: The id that synthetic row answers to. Named rather than spelled inline
#: because it is not a ``source_connections.id``: anything validating a source
#: id against that table (F4.3's mute scopes) has to special-case exactly this
#: value, and a magic string copied into a second module is how the two drift.
LOCAL_BUCKET_ID = "__local__"

_BUILDER_HREF = "/admin/studio/semantic-layer"
_DATA_SOURCES_HREF = "/admin/data-sources"


def _tag_form_href(source_id: str, domain: str) -> str:
    resource_type = TAG_RESOURCE_TYPE_BY_DOMAIN[domain]
    return f"/admin/semantic-layer?tab=coverage&tag_source={source_id}&tag_type={resource_type}"


def _domain_result(
    status: str,
    detail: str,
    *,
    action: Optional[Dict[str, str]] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One cell of the report. ``action`` is only ever set when a create flow
    genuinely exists for that source — see the module docstring."""
    return {"status": status, "detail": detail, "action": action, "raw": raw or {}}


# ---------------------------------------------------------------------------
# semantic
# ---------------------------------------------------------------------------


def _keboola_missing_master_reason(conn: Dict[str, Any]) -> str:
    """Why a Keboola connection is absent from K0.5's enumeration.

    ``_enumerate_master_sources()`` skips a connection for three different
    reasons, and reporting all three as "no master token" sent admins to
    re-add a token that was never gone while the real cause was a missing
    stack URL or a token no longer decryptable under the current vault key.
    ``has()`` is an existence check, so naming the reason costs no decrypt.
    """
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo

    try:
        has_master = connection_secrets_repo().has(master_secret_key(conn["id"]))
    except Exception:  # noqa: BLE001 — an unreadable vault is itself the answer
        has_master = False
    if not has_master:
        return "no owner (master) token — the semantic-layer sync cannot run"
    if not ((conn.get("config") or {}).get("stack_url") or "").strip():
        return "owner token stored, but the connection has no stack URL"
    return "owner token stored, but it cannot be read — the vault key changed since it was written"


def _keboola_coverage_by_connection() -> Dict[str, Dict[str, Any]]:
    """K0.5's per-connection record, keyed by ``connection_id``.

    Wrapped, never rewritten: ``compute_semantic_coverage`` enumerates only
    Keboola connections that already hold a master token, which is exactly
    right for what it computes and exactly wrong for a completeness report —
    a connection with no token is the state every wizard-connected instance
    STARTS in, and it was invisible here. The caller pairs this map against
    the full connection list and fills the gap itself (see
    :func:`_semantic_status`), so K0.5 keeps its own contract untouched.
    """
    from connectors.keboola.semantic_layer import compute_semantic_coverage

    try:
        report = compute_semantic_coverage()
    except Exception as exc:  # noqa: BLE001 — one connector must not sink the report
        logger.warning("cross-domain coverage: the Keboola semantic provider failed: %s", exc)
        return {}
    return {entry.get("connection_id"): entry for entry in report.get("sources") or [] if entry.get("connection_id")}


def _keboola_semantic_status(conn: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if entry is None:
        return _domain_result(
            STATUS_MISSING,
            _keboola_missing_master_reason(conn),
            action={"label": "Add the owner token", "href": _DATA_SOURCES_HREF},
        )

    raw = dict(entry)
    metrics = entry.get("metrics") or {}
    upstream = int(metrics.get("upstream") or 0)
    importable = int(metrics.get("importable") or 0)
    models = entry.get("models") or []
    blocked = entry.get("blocked") or []
    unregistered = entry.get("unregistered_tables") or []

    if entry.get("error"):
        return _domain_result(
            STATUS_MISSING,
            str(entry["error"]),
            action={"label": "Check the connection", "href": _DATA_SOURCES_HREF},
            raw=raw,
        )
    if not models:
        return _domain_result(
            STATUS_MISSING,
            "this project publishes no semantic model",
            action={"label": "Author a model", "href": _BUILDER_HREF},
            raw=raw,
        )
    if upstream and not importable:
        return _domain_result(
            STATUS_MISSING,
            f"none of the {upstream} metric(s) this project publishes bind to a registered table",
            action={"label": "Register the tables", "href": _DATA_SOURCES_HREF},
            raw=raw,
        )
    if blocked or unregistered:
        return _domain_result(
            STATUS_PARTIAL,
            f"{importable} of {upstream} metric(s) land here"
            + (f"; {len(blocked)} blocked by their own definition" if blocked else "")
            + (f"; {len(unregistered)} dataset(s) have no registered table" if unregistered else ""),
            action={"label": "Register the tables", "href": _DATA_SOURCES_HREF} if unregistered else None,
            raw=raw,
        )
    return _domain_result(
        STATUS_OK,
        f"{len(models)} model(s), {importable} metric(s) land here",
        raw=raw,
    )


def _native_semantic_status(
    conn: Dict[str, Any],
    adapter: str,
    semantic_sources: List[Dict[str, Any]],
    semantic_models: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Semantic status for a source type served by a first-party adapter.

    Read straight off ``semantic_sources`` / ``semantic_models`` — no
    upstream round-trip, because a native adapter has none of the
    token-identity problem K0.5 exists to detect. A source row is linked to
    this connection by ``config.connection_id``; a row that records no
    connection is intentionally attributed to NO connection rather than to
    every one of that type, which would credit one project's model to its
    neighbour.

    Both lists are read ONCE by the caller and passed in: this runs per
    connection, and re-listing every source and every model inside the loop
    made the report O(connections x models).
    """
    linked = [
        s
        for s in semantic_sources
        if (s.get("adapter") or "") == adapter and ((s.get("config") or {}).get("connection_id")) == conn["id"]
    ]
    raw: Dict[str, Any] = {"semantic_sources": [{"id": s["id"], "name": s.get("name")} for s in linked]}
    if not linked:
        return _domain_result(
            STATUS_MISSING,
            "no semantic source is linked to this connection — register one with `agnes admin semantic-source add`",
            raw=raw,
        )

    refs = {s["id"] for s in linked}
    models = [m for m in semantic_models if m.get("source_ref") in refs]
    raw["models"] = len(models)
    if not models:
        errors = [s.get("last_sync_error") for s in linked if s.get("last_sync_error")]
        return _domain_result(
            STATUS_PARTIAL,
            errors[0] if errors else "a semantic source is registered but has imported no model yet",
            action={"label": "Sync the source", "href": _DATA_SOURCES_HREF},
            raw=raw,
        )
    return _domain_result(STATUS_OK, f"{len(models)} model(s) imported", raw=raw)


def _semantic_status(
    conn: Dict[str, Any],
    keboola_coverage: Dict[str, Dict[str, Any]],
    semantic_sources: List[Dict[str, Any]],
    semantic_models: List[Dict[str, Any]],
) -> Dict[str, Any]:
    source_type = (conn.get("source_type") or "").strip()
    adapter = SEMANTIC_ADAPTER_BY_SOURCE_TYPE.get(source_type)
    if adapter is None:
        return _domain_result(
            STATUS_NOT_APPLICABLE,
            f"no semantic-layer adapter exists for {source_type or 'this source type'} yet",
        )
    if adapter == "keboola_metastore":
        return _keboola_semantic_status(conn, keboola_coverage.get(conn["id"]))
    return _native_semantic_status(conn, adapter, semantic_sources, semantic_models)


# ---------------------------------------------------------------------------
# metrics / glossary
# ---------------------------------------------------------------------------


def _metric_tables(metrics: List[Dict[str, Any]]) -> set[str]:
    """Every table name any metric is bound to — ``table_name`` plus every
    entry of the multi-table ``tables`` array (a JOIN metric binds to two)."""
    bound: set[str] = set()
    for metric in metrics:
        name = metric.get("table_name")
        if name:
            bound.add(name)
        for extra in metric.get("tables") or []:
            if extra:
                bound.add(extra)
    return bound


def _metrics_status(tables: List[Dict[str, Any]], bound_tables: set[str]) -> Dict[str, Any]:
    if not tables:
        return _domain_result(STATUS_NOT_APPLICABLE, "no tables are registered for this source")
    covered = [t for t in tables if t.get("name") in bound_tables]
    uncovered = len(tables) - len(covered)
    if not covered:
        return _domain_result(
            STATUS_MISSING,
            f"none of the {len(tables)} registered table(s) has a metric",
            action={"label": "Add a metric", "href": _BUILDER_HREF},
        )
    if uncovered:
        return _domain_result(
            STATUS_PARTIAL,
            f"{uncovered} of {len(tables)} registered table(s) have no metric",
            action={"label": "Add a metric", "href": _BUILDER_HREF},
        )
    return _domain_result(STATUS_OK, f"every one of the {len(tables)} registered table(s) has a metric")


def _semantic_source_ids_by_connection(
    semantic_sources: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """``source_connections.id`` -> the ``semantic_sources.id``s linked to it.

    Deliberately NOT filtered by adapter, unlike :func:`_native_semantic_status`:
    that function answers "does this connection have a model its own adapter
    imported", while this map answers "which provenance refs can carry rows
    belonging to this connection" — and a row's provenance does not stop being
    this connection's because it arrived through some other adapter.
    """
    by_connection: Dict[str, List[str]] = {}
    for source in semantic_sources:
        connection_id = (source.get("config") or {}).get("connection_id")
        if connection_id:
            by_connection.setdefault(connection_id, []).append(source["id"])
    return by_connection


def _glossary_status(term_count: int) -> Dict[str, Any]:
    if not term_count:
        return _domain_result(
            STATUS_MISSING,
            "no glossary terms",
            action={"label": "Add glossary terms", "href": _BUILDER_HREF},
        )
    return _domain_result(STATUS_OK, f"{term_count} term(s)")


# ---------------------------------------------------------------------------
# tag-backed domains (skill / agent / knowledge base)
# ---------------------------------------------------------------------------

_TAG_DOMAIN_LABELS = {
    "skill": ("skill", "Tag a skill"),
    "agent": ("agent", "Tag an agent"),
    "knowledge_base": ("knowledge domain", "Tag a knowledge domain"),
}


def _tag_status(source_id: str, domain: str, tags: List[Dict[str, Any]]) -> Dict[str, Any]:
    noun, label = _TAG_DOMAIN_LABELS[domain]
    resource_type = TAG_RESOURCE_TYPE_BY_DOMAIN[domain]
    matching = [t for t in tags if (t.get("resource_type") or "") == resource_type]
    if not matching:
        return _domain_result(
            STATUS_MISSING,
            f"no {noun} is tagged to this source",
            action={"label": label, "href": _tag_form_href(source_id, domain)},
        )
    return _domain_result(
        STATUS_OK,
        f"{len(matching)} {noun}(s) tagged",
        raw={"resource_ids": [t.get("resource_id") for t in matching]},
    )


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def compute_cross_domain_coverage(source_id: Optional[str] = None) -> Dict[str, Any]:
    """One row per data source, one status per domain.

    ``source_id`` narrows the report to a single source (the CLI's
    ``--source``); the synthetic local bucket answers to ``"__local__"``.
    An unknown id yields an empty ``sources`` list rather than an error —
    the caller asked "what is the coverage of X", and "X has none because X
    is not here" is answered by the empty list plus the id echoed back.
    """
    # PG gate FIRST — before any per-source work, so the failure does not
    # depend on how many sources happen to be connected. See the module
    # docstring; let RequiresPostgresBackend propagate.
    from src.repositories import (
        glossary_repo,
        metric_repo,
        resource_source_tags_repo,
        semantic_model_repo,
        semantic_source_repo,
        source_connections_repo,
        table_registry_repo,
    )

    tags_repo = resource_source_tags_repo()

    connections = source_connections_repo().list()
    tables = table_registry_repo().list_all()
    metrics = metric_repo().list()
    terms = glossary_repo().list(limit=100_000)

    bound_tables = _metric_tables(metrics)

    # Registration paths for some source types (e.g. Snowflake, before this
    # fix) can leave `connection_id` NULL on a table that nonetheless has a
    # real connection backing its source_type — the same fallback
    # `src/connection_resolver.py::resolve_connection` uses at query time
    # (NULL -> the source_type's default connection). Without it, every
    # such table lands in the synthetic "no connection" bucket below and a
    # real, populated connection incorrectly reports no tables. Built from
    # the already-fetched `connections` list (not `get_default()` per table)
    # to avoid N extra repo round-trips; mirrors `get_default()`'s own
    # `ORDER BY created_at LIMIT 1` tie-break.
    default_conn_id_by_source_type: Dict[str, str] = {}
    for conn in sorted(connections, key=lambda c: c.get("created_at") or ""):
        source_type = conn.get("source_type")
        if conn.get("is_default") and source_type and source_type not in default_conn_id_by_source_type:
            default_conn_id_by_source_type[source_type] = conn["id"]

    tables_by_connection: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for table in tables:
        conn_id = table.get("connection_id")
        if conn_id is None:
            conn_id = default_conn_id_by_source_type.get(table.get("source_type"))
        tables_by_connection.setdefault(conn_id, []).append(table)

    # `glossary_terms.source_ref` carries TWO namespaces, and reading it as one
    # made this column lie. The Keboola metastore sync stamps the
    # `source_connections.id` itself, but `src/semantic/importer.py` stamps the
    # `semantic_sources.id` of the registered source the document came through
    # (`import_source()` -> `"source_ref": source_id`). Looking the connection
    # id up alone therefore reported `missing` for every source fed by a
    # registered semantic source — a Snowflake connection with an imported
    # glossary read as having none — and those terms were counted in NO bucket
    # at all, since the synthetic local row only claims `source_ref IS NULL`.
    # Resolving both namespaces per connection is what makes the column true.
    terms_by_ref: Dict[Optional[str], int] = {}
    for term in terms:
        ref = term.get("source_ref")
        terms_by_ref[ref] = terms_by_ref.get(ref, 0) + 1

    semantic_sources = semantic_source_repo().list_all()
    semantic_models = semantic_model_repo().list_all()
    source_ids_by_connection = _semantic_source_ids_by_connection(semantic_sources)

    # Only pay for the Keboola provider's upstream round-trips when a Keboola
    # connection actually exists.
    keboola_coverage: Dict[str, Dict[str, Any]] = {}
    if any((c.get("source_type") or "") == "keboola" for c in connections):
        keboola_coverage = _keboola_coverage_by_connection()

    sources: List[Dict[str, Any]] = []
    for conn in connections:
        conn_id = conn["id"]
        tags = tags_repo.list_for_source(conn_id)
        glossary_refs = [conn_id, *source_ids_by_connection.get(conn_id, [])]
        sources.append(
            {
                "source_id": conn_id,
                "source_type": conn.get("source_type") or "",
                "name": conn.get("name") or conn_id,
                "domains": {
                    "semantic": _semantic_status(conn, keboola_coverage, semantic_sources, semantic_models),
                    "metrics": _metrics_status(tables_by_connection.get(conn_id, []), bound_tables),
                    "glossary": _glossary_status(sum(terms_by_ref.get(ref, 0) for ref in glossary_refs)),
                    "skill": _tag_status(conn_id, "skill", tags),
                    "agent": _tag_status(conn_id, "agent", tags),
                    "knowledge_base": _tag_status(conn_id, "knowledge_base", tags),
                },
            }
        )

    local_tables = tables_by_connection.get(None, [])
    local_terms = terms_by_ref.get(None, 0)
    if local_tables or local_terms:
        sources.append(_local_bucket(local_tables, local_terms, bound_tables, semantic_models))

    if source_id is not None:
        sources = [s for s in sources if s["source_id"] == source_id]

    return {"sources": sources}


def _sync_status(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "source_id": s["id"],
            "name": s.get("name") or s["id"],
            "last_sync_status": s.get("last_sync_status"),
            "last_sync_at": s.get("last_sync_at"),
            "last_sync_error": s.get("last_sync_error"),
        }
        for s in sources
    ]


def _configured_databricks_host() -> Optional[str]:
    """The currently-configured Databricks workspace host, independent of
    whether a token can be resolved for it.

    :func:`connectors.databricks.semantic_layer.resolve_databricks_settings`
    returns ``None`` whenever the token isn't resolvable (env var unset,
    vault unreachable) — correct for the sync path, which cannot do anything
    without a token, but wrong for a liveness check: a health-report process
    that simply lacks Databricks credentials would then flag every live
    ``databricks_metrics`` model as orphaned, reviving the exact false-orphan
    bug this module exists to fix. Liveness only needs the host, so this
    mirrors the same row-first / else-legacy-instance-config order without
    ever touching the token or warehouse id.
    """
    from src.connection_resolver import resolve_source_connection

    connection = resolve_source_connection("databricks")
    if connection is not None:
        host = str((connection.get("config") or {}).get("host") or "").strip()
        return host or None

    from app.instance_config import get_value

    host = str(get_value("data_source", "databricks", "host", default="") or "").strip()
    return host or None


def _orphaned_models(models: List[Dict[str, Any]], known_source_ids: set) -> List[Dict[str, Any]]:
    """Models whose ``source_ref`` names no live upstream.

    ``source='manual'`` models are excluded on purpose: they were never fed by
    a source and have no ``source_ref`` to go stale, so including them would
    flag every hand-authored model as "disconnected" from a source it never
    had. ``DELETE /api/admin/semantic-sources/{id}`` does not cascade to the
    models it fed (K0.12) — this is the source-agnostic successor to the
    retired page's Keboola-only "orphaned" count (see the module docstring),
    over the canonical document instead of the flat projections.

    "Live" does not mean the same thing for every ``source``: only documents
    synced through the registered semantic-source flow (git/upload/
    connection) ever get a ``semantic_sources`` row at all. ``keboola_
    metastore`` and ``databricks_metrics`` (see each connector's module
    docstring) stamp ``source_ref`` with something else entirely — a
    ``source_connections.id`` and a warehouse hostname, respectively — so
    checking either against ``known_source_ids`` always misses and flags
    every model those two providers ever produce. The two closures below
    dispatch each ``source`` to the check that matches what it actually
    stamps — the same branching shape as
    :func:`src.semantic.projection.resolve_dataset_table`, a different
    question over the same ``source`` dispatch — each resolving its shared,
    per-report state (the live Keboola connection ids; the configured
    Databricks host) lazily on first use and reusing it for every remaining
    model of that source, rather than re-querying per model.
    """
    live_keboola_connection_ids: Optional[set] = None
    live_databricks_ref: Optional[str] = None
    databricks_host_resolved = False

    def keboola_is_live(source_ref: Optional[str]) -> bool:
        nonlocal live_keboola_connection_ids
        if source_ref is None:
            return False
        if live_keboola_connection_ids is None:
            from src.repositories import source_connections_repo

            live_keboola_connection_ids = {c["id"] for c in source_connections_repo().list(source_type="keboola")}
        return source_ref in live_keboola_connection_ids

    def databricks_is_live(source_ref: Optional[str]) -> bool:
        nonlocal live_databricks_ref, databricks_host_resolved
        if source_ref is None:
            return False
        if not databricks_host_resolved:
            databricks_host_resolved = True
            host = _configured_databricks_host()
            if host:
                from connectors.databricks.semantic_layer import _source_ref_for_host

                live_databricks_ref = _source_ref_for_host(host)
        return live_databricks_ref is not None and source_ref == live_databricks_ref

    checks: Dict[str, Any] = {"keboola_metastore": keboola_is_live, "databricks_metrics": databricks_is_live}

    orphans = []
    for m in models:
        source = m.get("source") or "manual"
        if source == "manual":
            continue
        source_ref = m.get("source_ref")
        check = checks.get(source)
        is_live = check(source_ref) if check is not None else source_ref in known_source_ids
        if not is_live:
            orphans.append(
                {"model_id": m["id"], "slug": m.get("slug"), "source": m.get("source"), "source_ref": source_ref}
            )
    return orphans


def _invalid_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"model_id": m["id"], "slug": m.get("slug"), "validation_errors": m.get("validation_errors")}
        for m in models
        if (m.get("status") or "") == "invalid"
    ]


def _metrics_missing_description(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A metric with no ``description`` is a measure wearing a metric's name.

    ``SUM(order_amount)`` is a measure; "Revenue" is a metric, and revenue
    means gross or net depending who you ask — the business decision that
    makes it one lives in the description, not the SQL. No description means
    that decision was never written down.
    """
    return [{"metric_id": m["id"], "name": m.get("name")} for m in metrics if not (m.get("description") or "").strip()]


def _duplicate_metric_names(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same metric name defined more than once, with a different formula.

    Two rows with the same name and the SAME sql are one metric imported
    twice (harmless, common with multi-source syncs); two rows with the same
    name and DIFFERENT sql are the "four sources of truth" anti-pattern — an
    agent or a dashboard picking whichever it resolves first gets a different
    number than a colleague who picked the other.
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for m in metrics:
        by_name.setdefault(m.get("name") or "", []).append(m)
    findings = []
    for name, rows in by_name.items():
        if not name or len(rows) < 2:
            continue
        expressions = {(r.get("sql") or "").strip() for r in rows}
        if len(expressions) > 1:
            findings.append(
                {
                    "name": name,
                    "sources": [r.get("source") for r in rows],
                    "expressions": sorted(expressions),
                }
            )
    return findings


def _dataset_names_touched(expression: Dict[str, Any], dataset_names: set) -> set:
    """Which declared dataset names appear as a ``name.column`` prefix in a
    metric's SQL — a substring heuristic, not a parser.

    Exact SQL parsing would need a dialect-aware grammar for every engine the
    document declares (ANSI_SQL/SNOWFLAKE/DATABRICKS/…); a metric this check
    is worth running on almost always table-qualifies its columns (that is
    what makes a cross-dataset JOIN readable at all), so the substring match
    catches the real cases cheaply. It can both under- and over-match on
    adversarial input — a name that is also a common word, or a metric that
    skips qualification — which is why this feeds an ADVISORY finding, never
    a hard failure.
    """
    text = ""
    for dialect in (expression or {}).get("dialects") or []:
        text += " " + str(dialect.get("expression") or "")
    return {name for name in dataset_names if f"{name}." in text}


def _metrics_missing_relationships(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A metric whose SQL spans two datasets with no declared relationship
    between them.

    Reads the DOCUMENT, not the flat ``metric_definitions`` projection: a
    projected metric's multi-table ``tables[]`` is only ever populated when
    the projector already resolved a relationship (``src/semantic/
    projection.py``'s foreign-alias join composition) — checking the
    projection would find nothing, because a relationship-less cross-dataset
    metric never reaches it (K0.5/the projector silently drops it). The gap
    only shows up in the source document a human or an importer wrote, before
    anything filtered it.
    """
    findings: List[Dict[str, Any]] = []
    for model in models:
        if (model.get("status") or "") != "valid" or not model.get("document_json"):
            continue
        doc = model["document_json"]
        semantic_models = doc.get("semantic_model") or []
        for sm in semantic_models:
            dataset_names = {d.get("name") for d in sm.get("datasets") or [] if d.get("name")}
            connected: set = set()
            for rel in sm.get("relationships") or []:
                a, b = rel.get("from"), rel.get("to")
                if a and b:
                    connected.add(frozenset((a, b)))
            for metric in sm.get("metrics") or []:
                touched = _dataset_names_touched(metric.get("expression") or {}, dataset_names)
                if len(touched) < 2:
                    continue
                touched_list = sorted(touched)
                has_link = any(
                    frozenset((touched_list[i], touched_list[j])) in connected
                    for i in range(len(touched_list))
                    for j in range(i + 1, len(touched_list))
                )
                if not has_link:
                    findings.append(
                        {
                            "model_id": model["id"],
                            "metric_name": metric.get("name"),
                            "datasets": touched_list,
                        }
                    )
    return findings


def _coverage_summary(report: Dict[str, Any]) -> Dict[str, int]:
    missing = partial = 0
    for source in report.get("sources") or []:
        for cell in (source.get("domains") or {}).values():
            if cell.get("status") == STATUS_MISSING:
                missing += 1
            elif cell.get("status") == STATUS_PARTIAL:
                partial += 1
    return {"missing_count": missing, "partial_count": partial}


def compute_semantic_layer_health() -> Dict[str, Any]:
    """Is the semantic layer itself trustworthy right now?

    Cross-domain coverage (:func:`compute_cross_domain_coverage`) answers
    "what exists"; this answers "is what exists broken, stale, or internally
    inconsistent" — sync failures, models that lost their source, documents
    that failed validation, and three cheap static quality checks over the
    documents themselves (no description, the same name defined twice, a
    cross-dataset metric with no declared relationship). All of it feeds one
    admin screen, one CLI command, one MCP tool.

    **Postgres-only**: the mute overlay (F4.3) reads ``semantic_health_
    mutes``, which has no DuckDB implementation. Resolved FIRST, before any
    of the (backend-agnostic) checks below run, so a DuckDB-backed instance
    fails the same documented way every other PG-only route in this module
    does — a whole report that silently omitted which findings are muted
    would be exactly the anonymous disappearance F4.3 exists to prevent.
    """
    from src.repositories import (
        metric_repo,
        semantic_health_mutes_repo,
        semantic_model_repo,
        semantic_source_repo,
    )

    mutes_repo = semantic_health_mutes_repo()  # PG gate first — see docstring

    sources = semantic_source_repo().list_all()
    models = semantic_model_repo().list_all()
    metrics = metric_repo().list()

    known_source_ids = {s["id"] for s in sources}

    return {
        "sources": _sync_status(sources),
        "orphaned_models": _orphaned_models(models, known_source_ids),
        "invalid_models": _invalid_models(models),
        "metrics_missing_description": _metrics_missing_description(metrics),
        "duplicate_metric_names": _duplicate_metric_names(metrics),
        "metrics_missing_relationships": _metrics_missing_relationships(models),
        "coverage_summary": _coverage_summary(compute_cross_domain_coverage()),
        "mutes": mutes_repo.list_active(),
    }


def _local_bucket(
    local_tables: List[Dict[str, Any]],
    local_terms: int,
    bound_tables: set[str],
    semantic_models: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The synthetic row for everything attached to no connection.

    The three tag-backed domains are ``not_applicable`` here and not by
    oversight: a tag points at a ``source_connections.id``, and this bucket
    is by definition the rows that have none — offering "tag a skill" would
    link to a form that cannot be submitted.
    """
    manual_models = [m for m in semantic_models if (m.get("source") or "") == "manual"]
    if manual_models:
        semantic = _domain_result(STATUS_OK, f"{len(manual_models)} hand-authored model(s)")
    else:
        semantic = _domain_result(
            STATUS_MISSING,
            "no hand-authored semantic model covers these tables",
            action={"label": "Author a model", "href": _BUILDER_HREF},
        )
    return {
        "source_id": LOCAL_BUCKET_ID,
        "source_type": "local",
        "name": LOCAL_BUCKET_NAME,
        "domains": {
            "semantic": semantic,
            "metrics": _metrics_status(local_tables, bound_tables),
            "glossary": _glossary_status(local_terms),
            "skill": _domain_result(STATUS_NOT_APPLICABLE, "no connection to tag a skill to"),
            "agent": _domain_result(STATUS_NOT_APPLICABLE, "no connection to tag an agent to"),
            "knowledge_base": _domain_result(STATUS_NOT_APPLICABLE, "no connection to tag a knowledge domain to"),
        },
    }
