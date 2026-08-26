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


def _native_semantic_status(conn: Dict[str, Any], adapter: str) -> Dict[str, Any]:
    """Semantic status for a source type served by a first-party adapter.

    Read straight off ``semantic_sources`` / ``semantic_models`` — no
    upstream round-trip, because a native adapter has none of the
    token-identity problem K0.5 exists to detect. A source row is linked to
    this connection by ``config.connection_id``; a row that records no
    connection is intentionally attributed to NO connection rather than to
    every one of that type, which would credit one project's model to its
    neighbour.
    """
    from src.repositories import semantic_model_repo, semantic_source_repo

    linked = [
        s
        for s in semantic_source_repo().list_all()
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
    models = [m for m in semantic_model_repo().list_all() if m.get("source_ref") in refs]
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
    return _native_semantic_status(conn, adapter)


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
        source_connections_repo,
        table_registry_repo,
    )

    tags_repo = resource_source_tags_repo()

    connections = source_connections_repo().list()
    tables = table_registry_repo().list_all()
    metrics = metric_repo().list()
    terms = glossary_repo().list(limit=100_000)

    bound_tables = _metric_tables(metrics)
    tables_by_connection: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for table in tables:
        tables_by_connection.setdefault(table.get("connection_id"), []).append(table)

    terms_by_ref: Dict[Optional[str], int] = {}
    for term in terms:
        ref = term.get("source_ref")
        terms_by_ref[ref] = terms_by_ref.get(ref, 0) + 1

    # Only pay for the Keboola provider's upstream round-trips when a Keboola
    # connection actually exists.
    keboola_coverage: Dict[str, Dict[str, Any]] = {}
    if any((c.get("source_type") or "") == "keboola" for c in connections):
        keboola_coverage = _keboola_coverage_by_connection()

    sources: List[Dict[str, Any]] = []
    for conn in connections:
        conn_id = conn["id"]
        tags = tags_repo.list_for_source(conn_id)
        sources.append(
            {
                "source_id": conn_id,
                "source_type": conn.get("source_type") or "",
                "name": conn.get("name") or conn_id,
                "domains": {
                    "semantic": _semantic_status(conn, keboola_coverage),
                    "metrics": _metrics_status(tables_by_connection.get(conn_id, []), bound_tables),
                    "glossary": _glossary_status(terms_by_ref.get(conn_id, 0)),
                    "skill": _tag_status(conn_id, "skill", tags),
                    "agent": _tag_status(conn_id, "agent", tags),
                    "knowledge_base": _tag_status(conn_id, "knowledge_base", tags),
                },
            }
        )

    local_tables = tables_by_connection.get(None, [])
    local_terms = terms_by_ref.get(None, 0)
    if local_tables or local_terms:
        sources.append(_local_bucket(local_tables, local_terms, bound_tables, semantic_model_repo()))

    if source_id is not None:
        sources = [s for s in sources if s["source_id"] == source_id]

    return {"sources": sources}


def _local_bucket(
    local_tables: List[Dict[str, Any]],
    local_terms: int,
    bound_tables: set[str],
    model_repo: Any,
) -> Dict[str, Any]:
    """The synthetic row for everything attached to no connection.

    The three tag-backed domains are ``not_applicable`` here and not by
    oversight: a tag points at a ``source_connections.id``, and this bucket
    is by definition the rows that have none — offering "tag a skill" would
    link to a form that cannot be submitted.
    """
    manual_models = [m for m in model_repo.list_all() if (m.get("source") or "") == "manual"]
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
