"""Auto-migration of the two legacy, connector-owned scheduled semantic
refreshes onto the ONE generic ``semantic_sources`` sweep (#1707 Block 3
steps 3-4).

Before this, the Keboola Metastore sync and the Databricks Unity Catalog
metric-view sync each had their own admin endpoint and their own scheduler
cadence, bypassing the registry entirely. Both are gone; the sweep in
``app/api/semantic_sources_refresh.py`` is the only scheduled trigger left.
The connectors' sync LOGIC did not move — the same adapters compose the same
documents and the same central projector writes them.

Two functions, both called by that sweep:

``ensure_legacy_semantic_sources`` — registers the ``semantic_sources`` rows
the retired triggers implied, so an instance that upgrades keeps syncing with
no admin action. Idempotent, never a get-or-replace, and never a second row
for a scope some row already claims. A migrated Keboola row carries a
provenance override (``config.provenance``) so the rows it writes stay owned
by ``(source='keboola_metastore', source_ref=<connection id>)`` — the exact
pair that path has always stamped. Continuity of the prune scope is the whole
point: importing the same upstream under a new label would leave every
existing metric orphaned and write a duplicate beside it. Databricks needs no
override — its own Track D6 cutover already moved it onto the generic
``ossie_connection`` provenance.

``claim_source_for_import`` / ``duplicate_upstream_reason`` — the two guards
the retired triggers had built in and a row-by-row sweep would otherwise
lose: the single-flight one a migrated Keboola source shares with the
login-triggered sync that still writes the same rows, and the per-run
"one upstream project, one importer" check the legacy multi-source loop
threaded through ``seen_project_keys``.

``reconcile_after_import`` — the post-sync legacy-row reconciliation each
connector used to run inside its own refresh: deleting the rows its
pre-cutover direct writer left behind under a provenance the current pipeline
can no longer reach. Idempotent and gated on the sync having actually written
rows, so a failed or empty upstream can never delete the last good copy of a
metric.

This module is where per-connector knowledge lives so the sweep itself stays
generic: it walks rows and calls ``import_source``, and knows nothing about
Keboola or Databricks.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from src.semantic.importer import ImportReport

logger = logging.getLogger(__name__)


def ensure_legacy_semantic_sources() -> List[Dict[str, Any]]:
    """Register the ``semantic_sources`` rows the retired legacy refreshes
    implied. Returns only the rows this call created.

    One connector failing (an unreadable vault, an upstream that will not
    answer) must not stop the other from being migrated, nor abort the sweep
    that called this — each is isolated and logged.
    """
    created: List[Dict[str, Any]] = []
    created.extend(_ensure(_ensure_keboola_sources, "keboola"))
    created.extend(_ensure(_ensure_databricks_source, "databricks"))
    if created:
        logger.info(
            "semantic sources auto-migration: registered %d legacy source(s): %s",
            len(created),
            ", ".join(row["id"] for row in created),
        )
    return created


def _ensure(fn: Any, connector: str) -> List[Dict[str, Any]]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one connector must not break the sweep
        logger.warning("semantic sources auto-migration: %s migration failed: %s", connector, exc)
        return []


def _ensure_keboola_sources() -> List[Dict[str, Any]]:
    from connectors.keboola.semantic_layer import ensure_semantic_sources

    return ensure_semantic_sources()


def _ensure_databricks_source() -> List[Dict[str, Any]]:
    from connectors.databricks.semantic_layer import DATABRICKS_SEMANTIC_SOURCE_ID, ensure_semantic_source
    from src.repositories import semantic_source_repo

    repo = semantic_source_repo()
    existed = repo.get(DATABRICKS_SEMANTIC_SOURCE_ID) is not None
    source_id = ensure_semantic_source()
    if source_id is None or existed:
        return []
    row = repo.get(source_id)
    return [row] if row else []


def _keboola_adapter() -> str:
    from connectors.keboola.semantic_layer import SEMANTIC_ADAPTER

    return SEMANTIC_ADAPTER


def new_sweep_state() -> Dict[str, Any]:
    """The per-run bookkeeping :func:`duplicate_upstream_reason` accumulates.
    One dict per sweep, never shared across runs — the legacy loop's
    ``seen_project_keys`` had exactly this lifetime."""
    return {"upstream_projects": {}}


@contextmanager
def claim_source_for_import(source: Dict[str, Any]) -> Iterator[bool]:
    """Claim, for the duration of one source's import, the single-flight slot
    that source shares with a NON-sweep writer of the same rows. Yields False
    when the other writer holds it — the caller then SKIPS this source, never
    queues behind it.

    Only the migrated Keboola path has such a writer:
    ``run_semantic_layer_refresh_background`` (the Keboola login-triggered
    sync) still writes the same
    ``(source='keboola_metastore', source_ref=<connection id>)`` rows through
    ``sync_semantic_layer``. Before the trigger moved, each guarded itself
    with its own lock in its own module, which guarded nothing about the
    other: an upsert+prune pass could land inside another's, pruning a scope
    against a half-written picture. Every other source yields True — there is
    no second writer to serialize against.
    """
    if (source.get("adapter") or "").strip() != _keboola_adapter():
        yield True
        return

    from src.semantic.refresh_guard import KEBOOLA_SEMANTIC_REFRESH

    with KEBOOLA_SEMANTIC_REFRESH.try_claim(f"sweep:{source.get('id')}") as claimed:
        if not claimed:
            logger.info(
                "semantic sources refresh: source %s shares its rows with an in-flight %s run (%s); "
                "skipping it this sweep",
                source.get("id"),
                KEBOOLA_SEMANTIC_REFRESH.name,
                KEBOOLA_SEMANTIC_REFRESH.holder,
            )
        yield claimed


def duplicate_upstream_reason(source: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    """Why this source must NOT import in this sweep — because another row
    already imported the same upstream — or ``None`` to go ahead.

    The legacy Keboola loop threaded ``seen_project_keys`` across its sources
    for one reason: nothing stops an admin registering ONE project under two
    connections (uniqueness is on connection name), and two sources importing
    it would write one project's metrics under two refs, each prune deleting
    the other's rows on every run. One row per connection makes that state
    permanent rather than flapping, which is worse, so the guard has to
    survive the migration.

    Best-effort by construction: a source whose identity cannot be resolved
    (unreadable vault, unreachable stack, no owner id upstream) is NOT
    skipped — it goes on to its own import, which fails there and records the
    error on the row, exactly as it would have without this check.
    """
    if (source.get("adapter") or "").strip() != _keboola_adapter():
        return None

    try:
        from connectors.keboola.semantic_layer import sweep_project_identity

        key = sweep_project_identity(source.get("config") or {})
    except Exception as exc:  # noqa: BLE001 - a pre-check must never be the failure
        logger.info(
            "semantic sources refresh: could not resolve the upstream project of source %s for the "
            "duplicate check (%s); importing it normally",
            source.get("id"),
            exc,
        )
        return None
    if key is None:
        return None

    claims: Dict[Any, str] = state.setdefault("upstream_projects", {})
    source_id = source.get("id")
    claimed_by = claims.get(key)
    if claimed_by is not None and claimed_by != source_id:
        return (
            f"semantic source {claimed_by!r} already imported the same upstream Keboola project "
            f"(owner {key[1]} on {key[0]}) in this sweep; skipping so one project's rows are not "
            "written under two provenance refs that then delete each other."
        )
    claims[key] = source_id
    return None


def reconcile_after_import(source: Dict[str, Any], report: ImportReport) -> Dict[str, int]:
    """Run the connector-specific legacy-row reconciliation for one source
    that just imported successfully. ``{}`` for every source that has none —
    which is every source except the two migrated legacy paths.

    Deliberately called by the sweep and not by ``import_source``: the
    pipeline stays free of connector knowledge, and a manual
    ``POST /api/admin/semantic-sources/{id}/sync`` simply leaves the (already
    idempotent) reconciliation for the next scheduled sweep.
    """
    adapter = (source.get("adapter") or "").strip()
    try:
        if adapter == "keboola_metastore":
            return _reconcile_keboola(source, report)
        if adapter == "databricks_metric_views":
            return _reconcile_databricks()
    except Exception as exc:  # noqa: BLE001 - reconciliation is best-effort, the sync stands
        logger.warning(
            "semantic sources refresh: legacy reconciliation for source %s failed: %s", source.get("id"), exc
        )
    return {}


def _reconcile_keboola(source: Dict[str, Any], report: ImportReport) -> Dict[str, int]:
    """Purge the pre-cutover ``source='keboola_semantic_layer'`` rows this
    project's own sync superseded — the same purge, with the same gates,
    ``connectors.keboola.semantic_layer._sync_one_source`` still runs for the
    login-triggered sync.

    Gates, restated because they are load-bearing: metrics only when this pass
    WROTE metrics, glossary only when it wrote glossary terms, neither when
    the pass was partial — a document failed validation and was dropped, OR a
    detached model was deliberately held back, so a model that belongs to this
    scope was never rewritten this pass and its legacy rows must stay.
    """
    from connectors.keboola.semantic_layer import (
        default_connection_id,
        legacy_credentials_prune_scope,
        purge_legacy_glossary_rows,
        purge_legacy_metric_rows,
    )
    from src.semantic.transports import resolve_provenance

    projection = report.projection
    if projection is None:
        return {}
    _label, source_ref = resolve_provenance(source)
    # Both halves of "this pass did not rewrite every model it owns": an
    # invalid document, and F3's detached hold-back. The projector's own prune
    # already narrows on both (`src/semantic/importer.py`); this purge reaches
    # rows that prune cannot, so it has to ask the same question.
    partial = bool(report.invalid) or bool(report.detached_excluded)
    purged: Dict[str, int] = {}

    # Prune scope, carried over verbatim from the orchestrator this replaced:
    # a per-connection source owns its own ref; the legacy env-credential
    # source owns "NULL or the default connection id" whichever of the two it
    # ended up stamping, because a downgrade (last master token removed) still
    # has to clean up the rows it previously owned. `adopt_null` is the same
    # rule about the unstamped rows written before provenance existed — only
    # the default/legacy origin may claim them.
    if (source.get("config") or {}).get("legacy_credentials"):
        scope_refs = legacy_credentials_prune_scope()
        adopt_null = True
    else:
        scope_refs = {source_ref}
        # `sync_semantic_layer`'s master-token loop passes exactly
        # `adopt_null=connection_id == default_id`. Deriving it from
        # `source_ref is None` instead — as this did — is never true for a
        # per-connection source, so the DEFAULT connection's migrated sweep
        # silently stopped purging the unstamped rows the login-triggered sync
        # still purges, leaving them as permanent duplicates of their
        # freshly-projected twins.
        adopt_null = source_ref is not None and source_ref == default_connection_id()

    if projection.metrics_written and not partial:
        count = purge_legacy_metric_rows(scope_refs=scope_refs, adopt_null=adopt_null)
        if count:
            purged["metrics"] = count
    if projection.glossary_written and not partial:
        count = purge_legacy_glossary_rows(scope_refs=scope_refs, adopt_null=adopt_null)
        if count:
            purged["glossary"] = count
    return purged


def _reconcile_databricks() -> Dict[str, int]:
    """Purge the rows the retired Databricks direct writer left behind
    (``source='databricks_semantic_layer'``). Unconditional and idempotent by
    its own contract — the ids it deletes can never collide with one the
    current pipeline writes."""
    from connectors.databricks.semantic_layer import purge_legacy_metric_rows

    count = purge_legacy_metric_rows()
    return {"metrics": count} if count else {}
