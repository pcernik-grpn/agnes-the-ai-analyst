"""Project a validated Ossie document into the flat tables queries actually
read (``metric_definitions``, ``glossary_terms``, ``column_metadata``).

The document itself is stored whole elsewhere (``semantic_models``, Task 3);
this module only derives the flat, query-shaped rows from it — stamped with
the document's own ``(source, source_ref)`` provenance and pruned only within
that scope, so two sources (or two refs of the same source) can never delete
each other's rows.

See ``docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.repositories import column_metadata_repo, glossary_repo, metric_repo
from src.semantic.dialect import resolve_expression_any

logger = logging.getLogger(__name__)

# Vendor name under which Agnes rides its own concepts (glossary entries,
# keywords, constraints — see Task 13) through Ossie's generic
# `custom_extensions` escape hatch. The Keboola metastore adapter emits it as
# the canonical `AGNES`, but the comparison casefolds — matching the read side
# (src/semantic_validation.py, app/web/semantic_layer_view.py) so a
# hand-authored document is not silently dropped from the flat projection for
# spelling the tag `agnes`. Stored casefolded for direct comparison.
_GLOSSARY_VENDOR = "agnes"


# Provenance of a hand-authored model stored through the admin API
# (app/api/semantic_models.py) — the one document source whose name collides
# with an EXISTING `column_metadata.source` value: the admin metadata API
# (app/api/metadata.py) writes the same `(table_id, column_name)` PK with
# `source='manual'` too, and a manual dataset's `source` IS an Agnes table id
# (that binding is what surfaces its field descriptions in /api/v2/schema).
_MANUAL_DOCUMENT_SOURCE = "manual"

# The distinct `column_metadata.source` a manual model's projection writes
# — and prunes — under instead, so `_prune_columns` scoped to it can never
# delete an admin-authored `source='manual'` row. See `_column_source`.
MANUAL_MODEL_COLUMN_SOURCE = "semantic_model"


def _column_source(source: str) -> str:
    """The ``column_metadata.source`` value a document with provenance
    ``source`` writes (and prunes) its dataset fields under.

    Identity for every synced source (``ossie_git``/``ossie_upload``/
    ``ossie_connection``, ``keboola_metastore``, …) — their provenance never
    collides with another ``column_metadata`` writer. Only the manual admin
    API path is remapped: keeping ``source='manual'`` there would make the
    projection's upsert overwrite — and its prune delete — admin-authored
    rows the metadata API stores under the very same key and source.
    Precedence is write-time and explicit: an existing row owned by any
    OTHER writer (admin ``'manual'``, ``'profiler'``, ``'ai_enrichment'``)
    wins over a manual model's projection and is left untouched (see the
    guard in :func:`project_document`); since ``(table_id, column_name)``
    holds a single row, that write-time precedence is also what every reader
    (e.g. ``/api/v2/schema``) sees.
    """
    return MANUAL_MODEL_COLUMN_SOURCE if source == _MANUAL_DOCUMENT_SOURCE else source


def _is_agnes_vendor(vendor_name) -> bool:
    """Case-insensitive match against the Agnes vendor tag — the same
    casefolded posture the query validator and the browse view take."""
    return isinstance(vendor_name, str) and vendor_name.casefold() == _GLOSSARY_VENDOR


def _agnes_payload(obj: dict) -> dict:
    """The AGNES `custom_extensions` payload of one object, merged.

    Ossie pins `additionalProperties: false` on every object and gives Metric
    no dataset link at all, so a metric's table binding, its dataset's grain
    and the model's constraints have nowhere to live but this escape hatch.
    `data` is a JSON-ENCODED STRING per the schema; an entry that fails to
    parse is skipped rather than raised, like `_glossary_entries`.
    """
    merged: dict = {}
    for ext in obj.get("custom_extensions") or []:
        if not _is_agnes_vendor(ext.get("vendor_name")):
            continue
        try:
            data = json.loads(ext.get("data") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _resolve_keboola_table_row(table_ref: str, lookup: dict) -> Optional[dict]:
    """The ``table_registry`` row a raw Keboola tableId (``bucket.table``)
    resolves to, via the ``(bucket, table) -> view name`` ``lookup``
    (:func:`connectors.keboola.semantic_layer.table_lookup_from_registry`).

    The one place that turns a Keboola tableId match into the row a caller
    needs — shared by :func:`resolve_dataset_table` (a one-off caller, which
    builds ``lookup`` fresh per call) and :func:`_table_binder`'s per-metric
    closure (which builds ``lookup`` ONCE for the whole
    :func:`project_document` call and reuses it here), so there is exactly
    one Keboola dataset-resolution implementation, not two.
    """
    from connectors.keboola.semantic_layer import resolve_table_name
    from src.repositories import table_registry_repo

    view_name = resolve_table_name(table_ref, lookup)
    if not view_name:
        return None
    return table_registry_repo().get_by_name(view_name)


def _generic_table_lookup() -> dict[tuple[str, str], str]:
    """``{(bucket, source_table): agnes_view_name}`` built from EVERY
    registered table, any ``source_type`` — the generic, non-Keboola sibling
    of :func:`connectors.keboola.semantic_layer.table_lookup_from_registry`
    (which is scoped to ``source_type == "keboola"`` and normalizes
    ``source_table`` against a Keboola-only wizard quirk).

    Keys are case-folded: ``--bucket``/``--source-table``
    (``cli/commands/admin.py``) are source-type-agnostic labels stored
    verbatim at registration time, but a Snowflake dataset's ``source`` is
    composed from its information-schema identifiers
    (``connectors/snowflake/semantic_ossie.py::_compose_dataset``), which
    Snowflake emits UPPERCASE unless the object was created quoted. A
    registered row and the document's identifier can disagree in case for
    the very same table, so the comparison must fold both sides rather than
    assume either preserves a particular case. Built fresh once per
    :func:`project_document` call, same cost posture as the Keboola lookup
    it sits alongside.
    """
    from src.repositories import table_registry_repo

    lookup: dict[tuple[str, str], str] = {}
    for row in table_registry_repo().list_all():
        bucket = row.get("bucket")
        source_table = row.get("source_table")
        name = row.get("name")
        if bucket and source_table and name:
            lookup.setdefault((bucket.casefold(), source_table.casefold()), name)
    return lookup


def _resolve_generic_table_row(table_ref: str, lookup: dict) -> Optional[dict]:
    """The ``table_registry`` row a table identifier resolves to via a
    generic LAST-TWO-SEGMENTS split — the Snowflake/Databricks-shaped
    sibling of :func:`_resolve_keboola_table_row`.

    Unlike a Keboola tableId (``bucket.table``, where ``bucket`` itself may
    contain dots and so must be split on the LAST dot only), a
    Snowflake/Databricks identifier is ``database.schema.table`` (3+
    segments, no embedded dots within a segment) — so the right split here
    is the two segments closest to the table, not the first dot. E.g.
    ``ESHOP_DEMO.RAW.ORDERS`` matches a row registered with
    ``bucket="RAW"``, ``source_table="ORDERS"``, ignoring the leading
    ``ESHOP_DEMO`` database/catalog segment. A 1-segment identifier (no dot
    at all) never matches, mirroring :func:`resolve_table_name`'s own guard.
    The split segments are case-folded before the lookup, matching
    :func:`_generic_table_lookup`'s case-folded keys.
    """
    from src.repositories import table_registry_repo

    parts = table_ref.split(".")
    if len(parts) < 2:
        return None
    bucket, source_table = parts[-2], parts[-1]
    name = lookup.get((bucket.casefold(), source_table.casefold()))
    if not name:
        return None
    return table_registry_repo().get_by_name(name)


def resolve_dataset_table(dataset: dict, source: str, conn=None) -> Optional[str]:
    """The ``table_registry.id`` a dataset resolves to, or ``None`` when it
    can't be resolved — source-agnostic, used by both the metric-binding
    leg of :func:`project_document` (via :func:`_table_binder`) and
    :func:`src.semantic_coverage.tables_without_semantic_coverage`.

    ``source`` is the document's own provenance (``semantic_models.source``,
    e.g. ``"keboola_metastore"``, ``"manual"``, ``"ossie_git"``):

    - ``source == "keboola_metastore"``: the Keboola metastore adapter
      composes a dataset's ``source`` field as the raw Keboola tableId
      (``bucket.table`` — see ``connectors/keboola/semantic_ossie.py::
      _compose_dataset``), never the registered Agnes name. Resolved via
      the existing ``table_lookup_from_registry()`` ->
      ``resolve_table_name()`` chain (see :func:`_resolve_keboola_table_row`).
    - every other source: tried in order —

      1. ``dataset.source`` (falling back to ``dataset.name``) matched
         LITERALLY against ``table_registry.id`` then ``table_registry.name``
         — correct for a genuinely-Agnes-native identifier (see the module
         docstring note near ``_MANUAL_DOCUMENT_SOURCE``: a manual dataset's
         ``source`` IS an Agnes table id in that case).
      2. When step 1 misses: the same generic multi-segment fallback
         :func:`_table_binder` uses (:func:`_generic_table_lookup` +
         :func:`_resolve_generic_table_row`) — a Snowflake/Databricks-shaped
         identifier (``schema.table`` or ``database.schema.table``) matched
         against EVERY registered table's ``(bucket, source_table)``,
         regardless of ``source_type``. This is what lets a hand-authored/
         uploaded model's dataset (e.g. ``ESHOP_DEMO.RAW.ORDERS``) resolve to
         a table registered via ``agnes admin register-table --bucket RAW
         --source-table ORDERS``.

      The Keboola path above and the ``dataset.source``-before-``.name``
      priority are both unchanged by this fallback.

    ``conn`` is accepted for signature stability (mirrors
    ``app.auth.scheduler_token.ensure_scheduler_user``) — actual repo access
    goes through the ``*_repo()`` factory, never a raw connection, so a
    Postgres-backed instance resolves correctly too.
    """
    del conn
    table_ref = dataset.get("source") or dataset.get("name") or ""
    if not table_ref:
        return None

    from src.repositories import table_registry_repo

    if source == "keboola_metastore":
        try:
            from connectors.keboola.semantic_layer import table_lookup_from_registry

            lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
        except Exception:  # pragma: no cover - a registry read failure must not raise
            return None
        if not lookup:
            return None
        row = _resolve_keboola_table_row(table_ref, lookup)
        return row["id"] if row else None

    row = table_registry_repo().get(table_ref) or table_registry_repo().get_by_name(table_ref)
    if row:
        return row["id"]

    # Generic fallback: this is a one-off, single-dataset call (unlike
    # _table_binder's per-metric closure reusing one lookup across a whole
    # project_document() call), so the lookup is built fresh here, on miss
    # only — the same cost posture the keboola_metastore branch above
    # already has (it also builds its lookup fresh per call). Callers of
    # resolve_dataset_table walk one dataset at a time (per model, per
    # coverage/autodraft pass), not a routine per-row sync, so an extra
    # registry scan per unresolved dataset is an acceptable cost here.
    try:
        generic_lookup = _generic_table_lookup()
    except Exception:  # pragma: no cover - a registry read failure must not raise
        return None
    if not generic_lookup:
        return None
    row = _resolve_generic_table_row(table_ref, generic_lookup)
    return row["id"] if row else None


def _column_table_id(dataset: dict, source: str) -> str:
    """The ``column_metadata`` table-id key one dataset's fields write
    under — :func:`resolve_dataset_table`'s result, falling back to the raw
    ``dataset.source``/``.name`` on a miss, EXCEPT for ``source == 'manual'``
    (never resolved).

    A manual dataset's ``source`` is already an Agnes table id by
    convention (see :data:`_MANUAL_DOCUMENT_SOURCE`'s note near the top of
    this module) — running it through resolution anyway can silently shift
    the key it lands under (``table_registry.id`` is derived from ``name``,
    e.g. ``request.name.strip().lower().replace(" ", "_")`` in
    ``app/api/admin.py``, so the two diverge whenever a table's display name
    has spaces/uppercase) or, on a literal-match miss, match an unrelated
    table via the generic multi-segment fallback that manual documents never
    went through before this helper existed — orphaning existing rows under
    the old raw key either way. The three callers that key ``column_
    metadata`` (the write leg of :func:`project_document`, :func:`prune_model`,
    :func:`_sibling_column_claims`) must all agree on this key, so they share
    this one function rather than repeating the ``source``-gated ternary.

    TODO(perf, Devin PR #1673): each call rebuilds `resolve_dataset_table`'s
    whole table-registry lookup from scratch (`list_by_source()` for
    Keboola, `_generic_table_lookup()` otherwise) — this function is called
    once per dataset, so one `project_document`/`prune_model` call over N
    models × M datasets does O(N*M) full registry scans instead of the ONE
    the metric leg's `_table_binder()` gets away with by building its lookup
    once and reusing it. Not a correctness bug (registries are small; a
    routine sync stays well within one request), but the fix is to build the
    lookup(s) once per outer call and thread them through here, in
    `prune_model`, and in `_sibling_column_claims`, mirroring `_table_binder`.
    """
    raw_table_id = dataset.get("source") or dataset.get("name") or ""
    if source == _MANUAL_DOCUMENT_SOURCE:
        return raw_table_id
    return resolve_dataset_table(dataset, source) or raw_table_id


def _table_binder():
    """Return ``resolve(table_id) -> view_name | None`` over every registered
    table this instance knows how to bind against, or ``None`` when nothing
    is registered.

    Two identifier shapes, tried in order, so an existing Keboola binding
    can never regress:

    1. **Keboola tableId** (``bucket.table``, ``bucket`` itself possibly
       dotted — e.g. ``in.c-shop.orders``): resolved via
       :func:`_resolve_keboola_table_row` against tables registered with
       ``source_type='keboola'``, exactly as before this function grew a
       second path. The metastore adapter is still the only writer that
       composes a `dataset` key this way.
    2. **Generic multi-segment identifier** (``schema.table`` or
       ``database.schema.table`` — Snowflake/Databricks shape, e.g.
       ``ESHOP_DEMO.RAW.ORDERS``): tried only when the Keboola path above
       didn't resolve, via :func:`_resolve_generic_table_row` against EVERY
       registered table regardless of ``source_type`` (a hand-authored/
       uploaded model has no adapter-known provenance to route on ahead of
       time).

    Imported lazily and behind this one seam so the core projector keeps no
    import-time dependency on a connector. Never raises: an instance with no
    registered tables at all simply binds nothing.

    Both lookup dicts are built ONCE here (not per metric), so a routine sync
    of a few hundred metrics stays two registry scans, not hundreds; each
    metric's ``resolve(table_id)`` call then costs at most one extra indexed
    ``table_registry`` point-lookup (name -> row) per attempted shape.
    """
    from src.repositories import table_registry_repo

    kb_lookup = None
    try:
        from connectors.keboola.semantic_layer import table_lookup_from_registry

        kb_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola")) or None
    except Exception:  # pragma: no cover - a registry read failure must not lose metrics
        kb_lookup = None

    try:
        generic_lookup = _generic_table_lookup() or None
    except Exception:  # pragma: no cover - a registry read failure must not lose metrics
        generic_lookup = None

    if not kb_lookup and not generic_lookup:
        return None

    def resolve(table_id: str) -> Optional[str]:
        row = _resolve_keboola_table_row(table_id, kb_lookup) if kb_lookup else None
        if row is None and generic_lookup:
            row = _resolve_generic_table_row(table_id, generic_lookup)
        return row["name"] if row else None

    return resolve


def _keboola_lookups():
    """``(table_lookup, column_lookup)`` for JOIN composition, or ``None`` when
    no Keboola tables are registered. Same seam as :func:`_table_binder`:
    Keboola-specific, imported lazily, and the finished JOIN needs Agnes's own
    registry + column metadata — data the adapter deliberately never had, which
    is why the JOIN is composed here rather than in the document."""
    try:
        from connectors.keboola.semantic_layer import table_lookup_from_registry
        from src.repositories import column_metadata_repo, table_registry_repo

        table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    except Exception:  # pragma: no cover - a registry read failure must not lose metrics
        return None
    if not table_lookup:
        return None
    col_repo = column_metadata_repo()
    column_lookup = {
        view: {c["column_name"] for c in col_repo.list_for_table(view)} for view in set(table_lookup.values())
    }
    return table_lookup, column_lookup


def _relationship_lookup_from_model(model: dict) -> dict:
    """``tableId -> [relationship attrs]`` for one model, rebuilt from each
    relationship's ``AGNES`` extension (which carries the raw tableIds, the
    on-clause and the type). Mirrors the legacy
    ``relationship_lookup_by_dataset``: a relationship is filed under BOTH its
    ``from`` and ``to`` tableId, and ``resolve_relationship`` decides which side
    the metric's dataset sits on."""
    lookup: dict = {}
    for rel in model.get("relationships") or []:
        ext = _agnes_payload(rel)
        from_id, to_id = ext.get("from_table"), ext.get("to_table")
        if not (from_id and to_id):
            continue
        attrs = {"from": from_id, "to": to_id, "on": ext.get("on") or "", "type": ext.get("type") or ""}
        lookup.setdefault(from_id, []).append(attrs)
        lookup.setdefault(to_id, []).append(attrs)
    return lookup


def _bind_metric(
    fragment: str, table_id: str, binder, kb_lookups, rel_lookup: dict
) -> Optional[tuple[str, Optional[str], Optional[list]]]:
    """Resolve one metric's SQL against its declared table binding.

    Returns ``(sql, table_name, tables)`` or ``None`` (caller SKIPS). Four
    outcomes, matching the legacy Keboola composer exactly:

    - **No binding declared** (``table_id`` empty) → ``(fragment, None, None)``.
      A plain upstream Ossie metric — e.g. from a git source — kept verbatim.
    - **Simple binding honored** → ``(SELECT … FROM "view" AS t, view, None)``.
    - **Foreign-alias binding resolvable via a relationship** →
      ``(join_sql, primary_view, [primary_view, joined_view])``, composed by the
      legacy ``try_join_composition`` (the one live-verified LEFT-JOIN case).
    - **Binding declared but cannot be honored** (unregistered table, embedded
      ``--`` comment, unresolvable foreign alias) → ``None``. The composer skips
      these; keeping them as bare fragments would make the flat-table cutover
      start surfacing unrunnable metrics.
    """
    if not table_id:
        return fragment, None, None
    if binder is None:
        return None
    from connectors.keboola.semantic_layer import (
        compose_sql,
        has_embedded_sql_comment,
        references_foreign_alias,
        try_join_composition,
    )

    if has_embedded_sql_comment(fragment):
        return None
    if references_foreign_alias(fragment):
        if kb_lookups is None:
            return None
        table_lookup, column_lookup = kb_lookups
        fields, _reason = try_join_composition(fragment, table_id, table_lookup, rel_lookup, column_lookup)
        if fields is None:
            return None
        return fields["sql"], fields["table_name"], fields.get("tables")
    table_name = binder(table_id)
    if not table_name:
        return None
    return compose_sql(fragment, table_name), table_name, None


def _constraints_for(metric_name: str, constraints: list) -> Optional[dict]:
    """The `validation` payload for one metric — the constraints whose
    `metrics[]` names it. Mirrors the legacy importer's `merge_constraints`
    output shape, which `agnes catalog --metrics --show` already renders."""
    rules = [
        {
            "name": c.get("name"),
            "constraint_type": c.get("constraint_type"),
            "rule": c.get("rule"),
            "severity": c.get("severity"),
        }
        for c in constraints
        if isinstance(c, dict) and metric_name in (c.get("metrics") or [])
    ]
    return {"rules": rules} if rules else None


def _check_name_collision(metric_name: str, metric_id: str, source: str, source_ref: Optional[str]) -> bool:
    """True when ``metric_name`` is already held by a DIFFERENT metric id
    from a DIFFERENT (source, source_ref) scope — logged as a WARN, never a
    skip.

    ``metric_definitions.name`` has no unique constraint (unlike ``id``, its
    primary key): two writers describing a metric with the same display name
    both get their own row. That is a pre-existing gap this projector does
    not close (closing it needs a product decision — which writer wins, or
    whether both should even be allowed — out of scope here); this is the
    "don't over-build it" minimum: a same-transaction check that surfaces the
    collision instead of the write silently proceeding unremarked, so an
    operator investigating an ambiguous `agnes catalog --metrics` lookup by
    name has a log line pointing at both ids involved. Scoped on
    ``(source, source_ref)``, not ``source`` alone: two source_refs of the
    SAME source (e.g. two Keboola projects) are two independent writers too —
    each owns its own prune scope and its own metric id — so a name they both
    happen to use is exactly as ambiguous as one shared across sources.
    """
    existing = metric_repo().find_by_name(metric_name)
    if existing is None or existing.get("id") == metric_id:
        return False
    if (existing.get("source") or "") == source and (existing.get("source_ref") or "") == (source_ref or ""):
        return False
    logger.warning(
        "Semantic projection (%s/%s): metric name %r is already used by metric id %r from source %r "
        "(source_ref=%r); writing %r as a separate row — metric_definitions.name has no uniqueness "
        "constraint, so both rows will exist and a name-only lookup may be ambiguous.",
        source,
        source_ref,
        metric_name,
        existing["id"],
        existing.get("source"),
        existing.get("source_ref"),
        metric_id,
    )
    return True


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics to a single underscore, strip
    leading/trailing underscores — glossary terms are natural-language
    phrases, unlike metric/field names which are already slugs."""
    return _NON_ALNUM_RE.sub("_", text.lower()).strip("_")


def _scoped_id(source: str, source_ref: Optional[str], *parts: str) -> str:
    """A stable id unique per (source, source_ref, *parts) — re-projecting the
    same document produces the same ids (upsert, not duplicate); two
    source_refs of the same source never collide even when their models or
    metrics share a name."""
    return "/".join([source, source_ref or "_", *parts])


def _model_key(model: dict) -> str:
    """The id component that identifies ONE model within a (source,
    source_ref) — the upstream object's stable identifier when the document
    carries one, its display name otherwise.

    A model's ``name`` is a display name: neither unique nor stable. Keying
    projected ids on it makes two like-named models collapse onto identical
    ids, and since ``metric_repo().create`` upserts on id, the later model
    silently OVERWRITES the earlier one — no prune, no skip, and
    ``metrics_written`` still counts both. The retired Keboola writer keyed
    on the immutable Metastore model UUID for exactly this reason, and the
    adapter still carries it: ``custom_extensions[AGNES].metastore_id`` (see
    ``connectors/keboola/semantic_ossie.py::_identity``, written for every
    model, since only models with an ``id`` are composed at all).

    A document from a source with no such identifier — a hand-authored or
    git-hosted Ossie file — falls back to the name, and
    :func:`project_document` reports a name collision there as an explicit
    skip rather than overwriting.
    """
    raw = _agnes_payload(model).get("metastore_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return model.get("name") or ""


def projected_metric_id(source: str, source_ref: Optional[str], model: dict, metric_name: str) -> str:
    """The ``metric_definitions.id`` :func:`project_document` writes for
    ``metric_name`` of ``model`` — the writer's own formula, exposed so a
    READER can map a flat row back to the document object it came from.

    The stored id is ``<source>/<source_ref or '_'>/<model key>/<name>``, and
    every one of those parts may itself contain a ``/`` (a metric name, a
    Keboola-style source_ref), so splitting a stored id apart is ambiguous.
    Recomputing it from the document is not: the UI deep link
    (``/catalog/semantics`` → ``/semantic-layer/{slug}/metric:{name}``) asks
    this function for the id it would expect and matches on equality, which
    keeps the link tied to the writer rather than to a parse of its output.
    """
    return _scoped_id(source, source_ref, _model_key(model), metric_name)


@dataclass
class ProjectionReport:
    metrics_written: int = 0
    glossary_written: int = 0
    columns_written: int = 0
    metrics_pruned: int = 0
    glossary_pruned: int = 0
    # A written metric's NAME already belonged to a different id under a
    # different source (see `_check_name_collision`) — `metric_definitions
    # .name` has no uniqueness constraint (Task: "don't over-build this"), so
    # this is a same-transaction, non-blocking check: the metric is still
    # written under its own id, both rows exist, and this only counts how
    # often that happened this pass.
    name_collisions: int = 0
    skipped: list[dict] = field(default_factory=list)
    # Metrics that DID project (they're in `metric_definitions`, counted in
    # `metrics_written` too) but whose only declared expression is a
    # warehouse-specific dialect (SNOWFLAKE, DATABRICKS, ...) rather than
    # DUCKDB/ANSI_SQL — the raw expression rides as-is, not locally runnable.
    # Kept separate from `skipped`, which is reserved for metrics that never
    # made it into `metric_definitions` at all.
    warehouse_only: list[dict] = field(default_factory=list)


def _synonyms_of(ai_context: Any) -> list[str]:
    """`ai_context.synonyms` — only the object form of `ai_context` carries
    them; the schema also allows a bare freeform string, which has none."""
    if isinstance(ai_context, dict):
        synonyms = ai_context.get("synonyms")
        if isinstance(synonyms, list):
            return [s for s in synonyms if isinstance(s, str)]
    return []


def _model_synonyms(model: dict) -> Optional[list[str]]:
    """Model-level and dataset-level `ai_context.synonyms`, combined onto
    every metric of the model.

    Ossie metrics are declared at model scope and may span multiple datasets
    (the spec: "Quantifiable measures spanning datasets") — there is no
    per-dataset metric link to key a narrower cascade off. So a dataset's
    synonym context enriches the whole model's metrics, the same as the
    model's own `ai_context.synonyms` — this is "today's behavior" the
    Keboola importer already has for its (single-dataset-per-metric) shape,
    generalized to Ossie's cross-dataset one.
    """
    combined: list[str] = []
    seen: set[str] = set()
    for syn in _synonyms_of(model.get("ai_context")):
        if syn not in seen:
            seen.add(syn)
            combined.append(syn)
    for dataset in model.get("datasets") or []:
        for syn in _synonyms_of(dataset.get("ai_context")):
            if syn not in seen:
                seen.add(syn)
                combined.append(syn)
    return combined or None


def _glossary_entries(model: dict) -> list[dict]:
    """Glossary terms riding `custom_extensions` under the Agnes vendor name.

    Core Ossie has no glossary object, so a document without this extension
    projects zero glossary rows — correct, not a bug. `data` is a
    JSON-ENCODED STRING per the schema (never a nested mapping); an entry
    that fails to parse is skipped rather than raised, consistent with the
    rest of this module reporting rather than crashing on unusable input.
    """
    entries: list[dict] = []
    for ext in model.get("custom_extensions") or []:
        if not _is_agnes_vendor(ext.get("vendor_name")):
            continue
        raw = ext.get("data")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        glossary = data.get("glossary") if isinstance(data, dict) else None
        if isinstance(glossary, list):
            entries.extend(g for g in glossary if isinstance(g, dict) and g.get("term"))
    return entries


def project_document(
    document_json: dict,
    *,
    source: str,
    source_ref: Optional[str],
    safe_prune: bool = False,
    partial: bool = False,
) -> ProjectionReport:
    """Project one Ossie document, then prune rows this (source, source_ref)
    previously wrote that the document no longer mentions.

    Never a global delete: pruning always reads and deletes within this
    document's own (source, source_ref) scope, so re-projecting a shrunk
    document cannot touch another source's — or another ref's — rows.

    ``safe_prune`` adds a full-wipe guard on top of that scoping: when the
    projection wrote ZERO metrics (resp. glossary terms) while in-scope rows
    already exist, the prune is skipped and logged rather than deleting every
    row. A document that legitimately shrinks to zero is indistinguishable
    from a transient upstream that returned an empty-but-valid document, and
    the second must not wipe an installation's whole metric registry in one
    pass. Off by default (a git source emptying a model is a real delete
    signal); the Keboola sync — whose upstream can 200 with nothing usable —
    opts in. Mirrors the legacy ``_sync_one_source`` valve the cutover retired.

    ``partial`` says "``document_json`` is NOT the complete picture for this
    (source, source_ref)" — one of several composed documents failed
    validation and was dropped before reaching here, so a model that belongs
    to this scope is missing. Pruning at full scope against that incomplete
    list would delete the dropped model's own previously-written rows, which
    upstream never asked to have removed. The prune then NARROWS to the models
    actually present in this call rather than being skipped outright: a model
    still present that genuinely lost a metric upstream is reconciled in the
    same pass, while the missing model is out of reach by construction. Off by
    default, because the full scope is what reclaims a model deleted upstream
    — a caller that knows its input is complete must keep it.
    """
    report = ProjectionReport()

    written_metric_ids: set[str] = set()
    written_glossary_ids: set[str] = set()
    # table_id -> field names written for it by *this* call. Used to prune
    # dropped fields within a table this document still mentions (see
    # `_prune_columns` for why this is narrower than the metric/glossary
    # prune).
    written_columns_by_table: dict[str, set[str]] = {}

    # Resolved once per call, not per metric: a registry read per metric would
    # turn a routine sync of a few hundred metrics into a few hundred queries.
    binder = _table_binder()
    kb_lookups = _keboola_lookups()

    # The `column_metadata.source` this document's dataset fields are written
    # and pruned under — identical to `source` for every synced source, but a
    # distinct value for the manual admin-API path, whose `source='manual'`
    # collides with the admin metadata API's own rows (see `_column_source`).
    column_source = _column_source(source)

    # One id prefix per model projected here. Used only when ``partial`` — see
    # the docstring — to keep the prune off models this call never saw.
    model_prefixes: set[str] = set()
    seen_model_keys: set[str] = set()

    for model in document_json.get("semantic_model") or []:
        model_name = model.get("name") or ""
        model_key = _model_key(model)
        if model_key in seen_model_keys:
            # Two models sharing an id component. With a stable upstream
            # identifier this cannot happen; without one (a hand-authored
            # document with two like-named models) every id would collide and
            # the later model would silently overwrite the earlier — reported
            # as a skip instead, because a silent overwrite is
            # indistinguishable from "the second model imported fine".
            logger.warning(
                "Semantic projection (%s/%s): model %r reuses the id key %r of an earlier model in this "
                "document; skipping it rather than overwriting the first model's rows.",
                source,
                source_ref,
                model_name,
                model_key,
            )
            report.skipped.append({"kind": "model", "name": model_name, "reason": "duplicate_model_key"})
            continue
        seen_model_keys.add(model_key)
        model_prefixes.add(_scoped_id(source, source_ref, model_key) + "/")

        synonyms = _model_synonyms(model)
        constraints = _agnes_payload(model).get("constraints") or []
        # Per model: its own relationships resolve its metrics' JOINs, never a
        # merged pool (a relationship in one model must not satisfy another's).
        rel_lookup = _relationship_lookup_from_model(model)
        # A dataset's grain describes the DATASET. It rides along as a note on
        # the metrics bound to it, never as `metric_definitions.grain`, which
        # would restate it as a fact about the metric's own time dimension.
        grain_by_table = {
            (d.get("source") or d.get("name") or ""): _agnes_payload(d).get("grain")
            for d in model.get("datasets") or []
        }

        for metric in model.get("metrics") or []:
            metric_name = metric.get("name")
            if not metric_name:
                continue
            sql, dialect_name, locally_runnable = resolve_expression_any(metric.get("expression") or {})
            if sql is None:
                report.skipped.append(
                    {"kind": "metric", "name": metric_name, "reason": "no expression in any usable dialect"}
                )
                continue
            # The bare aggregation fragment, before `_bind_metric` composes it
            # into a runnable `SELECT ... FROM ...` (or a JOIN). The legacy
            # composer stored this alongside the composed `sql` as
            # `metric_definitions.expression`; kept here so the "Expression"
            # block in catalog_semantics.html still has something to render.
            fragment = sql
            table_id = _agnes_payload(metric).get("dataset") or ""
            if locally_runnable:
                bound = _bind_metric(sql, table_id, binder, kb_lookups, rel_lookup)
                if bound is None:
                    # A binding was declared but cannot be honored — skip, as
                    # the legacy composer does, rather than write an
                    # unrunnable row.
                    report.skipped.append({"kind": "metric", "name": metric_name, "reason": "unresolved_binding"})
                    continue
                sql, table_name, tables = bound
                grain = grain_by_table.get(table_id)
                notes = [f"dataset grain: {grain}"] if grain else None
            else:
                # Only a warehouse-specific dialect was declared (SNOWFLAKE,
                # DATABRICKS, ...). Dropping the metric would make it vanish
                # from the catalog entirely even though the document itself
                # carries it fine — so it still projects, verbatim, with the
                # caveat that it cannot run through a local DuckDB query.
                # Mirrors the notes-based marker
                # `connectors/databricks/semantic_layer.py::build_metric_rows`
                # already uses for the same "needs server-side execution"
                # case (no `query_mode`/`remote` column on
                # `metric_definitions` to carry this instead).
                table_name, tables = None, None
                notes = [
                    f"{dialect_name} expression — not locally runnable; run server-side "
                    "(remote query or a materialized row) on the source warehouse."
                ]
                report.warehouse_only.append({"kind": "metric", "name": metric_name, "dialect": dialect_name})
            # Through the public helper, not `_scoped_id` inline: the readers
            # that map a flat row back to its document object call the same
            # function, so the two cannot drift.
            metric_id = projected_metric_id(source, source_ref, model, metric_name)
            if _check_name_collision(metric_name, metric_id, source, source_ref):
                report.name_collisions += 1
            metric_repo().create(
                id=metric_id,
                name=metric_name,
                display_name=metric_name,
                category=model_name or "semantic_model",
                sql=sql,
                expression=fragment,
                description=metric.get("description"),
                synonyms=synonyms,
                table_name=table_name,
                tables=tables,
                notes=notes,
                validation=_constraints_for(metric_name, constraints),
                source=source,
                source_ref=source_ref,
            )
            written_metric_ids.add(metric_id)
            report.metrics_written += 1

        for dataset in model.get("datasets") or []:
            # `_column_table_id` keys this the same way `prune_model` and
            # `_sibling_column_claims` do — see its docstring for why
            # `source == 'manual'` is never resolved.
            table_id = _column_table_id(dataset, source)
            field_names = written_columns_by_table.setdefault(table_id, set())
            for column in dataset.get("fields") or []:
                column_name = column.get("name")
                if not column_name:
                    continue
                # An existing row owned by a DIFFERENT writer (profiler, the
                # admin metadata API, ai_enrichment, or another semantic-layer
                # source) always wins over this projection's write. This used
                # to be gated to the manual path only (whose raw dataset id
                # was already a live Agnes table id, `_column_source`
                # remapped), but resolving `table_id` above now lets ANY
                # source's projection land on an id another writer already
                # owns — so the guard runs unconditionally. Without it, a
                # Keboola field with `description=None` (the common case)
                # would silently blank a previously-authored description on
                # every sync, and `_prune_columns` would then delete it
                # outright. Skipped rows are also out of `_prune_columns`'s
                # reach, which is scoped to `column_source`.
                existing = column_metadata_repo().get(table_id, column_name)
                if existing is not None and (existing.get("source") or "") != column_source:
                    continue
                column_metadata_repo().save(
                    table_id=table_id,
                    column_name=column_name,
                    basetype=column.get("datatype"),
                    description=column.get("description"),
                    source=column_source,
                    # Recorded here since the column was added; `_prune_columns`
                    # additionally SCOPES on it, on Postgres only (DuckDB's
                    # frozen app-state schema has no such column) — see its
                    # docstring. The precedence guard above still compares
                    # `source` alone: it decides ownership between DIFFERENT
                    # writer kinds (profiler vs. this projection), a question
                    # `source_ref` (which only distinguishes two instances of
                    # the SAME writer kind) does not answer.
                    source_ref=source_ref,
                )
                field_names.add(column_name)
                report.columns_written += 1

        for entry in _glossary_entries(model):
            term = entry.get("term")
            if not term:
                continue
            base_glossary_id = _scoped_id(source, source_ref, model_key, _slugify(term))
            # Two distinct terms can slugify identically ("Revenue (net)" and
            # "Revenue net" both -> "revenue_net"); without a dedup, the
            # second silently overwrites the first while `glossary_written`
            # counts both. Numeric-suffix on collision, first-seen order,
            # scoped to this call — mirrors the deleted `assign_glossary_id`.
            glossary_id = base_glossary_id
            suffix = 2
            while glossary_id in written_glossary_ids:
                glossary_id = f"{base_glossary_id}-{suffix}"
                suffix += 1
            # refresh_fts=False: rebuilding the BM25 index once per glossary
            # term makes a routine sync of N terms an O(N^2) full-index
            # rebuild (DuckDB's `PRAGMA create_fts_index` is a full rebuild,
            # not incremental). One rebuild after every write AND prune below
            # instead. `GlossaryPgRepository.create` accepts the same kwarg as
            # a no-op (Postgres computes `ts_rank` on the fly, no index to
            # rebuild), so this is safe on either backend.
            glossary_repo().create(
                id=glossary_id,
                term=term,
                definition=entry.get("definition") or "",
                see_also=entry.get("see_also"),
                source=source,
                source_ref=source_ref,
                refresh_fts=False,
            )
            written_glossary_ids.add(glossary_id)
            report.glossary_written += 1

    prune_prefixes = model_prefixes if partial else None
    report.metrics_pruned = _prune_metrics(
        source, source_ref, written_metric_ids, safe_prune=safe_prune, scope_prefixes=prune_prefixes
    )
    report.glossary_pruned = _prune_glossary(
        source, source_ref, written_glossary_ids, safe_prune=safe_prune, scope_prefixes=prune_prefixes
    )
    # Scoped to `column_source`, not `source` — for the manual path the two
    # differ precisely so this prune can never delete an admin-authored
    # `source='manual'` row for the same table (see `_column_source`).
    # A `partial` call additionally spares the columns SIBLING models of
    # this scope still claim: `column_metadata` rows carry no model
    # identity, so visiting a table two in-scope models share would
    # otherwise prune the absent model's live rows (`_sibling_column_claims`
    # is the column analogue of `model_prefixes` above). A full call needs
    # no such read — it carries the whole scope by definition.
    if partial:
        sibling_claims = _sibling_column_claims(source, source_ref, seen_model_keys)
        if sibling_claims is None:
            logger.warning(
                "Semantic projection (%s/%s): skipping the column prune — sibling claims unavailable, "
                "and pruning without them could delete a sibling model's live rows.",
                source,
                source_ref,
            )
        else:
            _prune_columns(column_source, written_columns_by_table, keep_by_table=sibling_claims, source_ref=source_ref)
    else:
        _prune_columns(column_source, written_columns_by_table, source_ref=source_ref)

    if report.glossary_written or report.glossary_pruned:
        glossary_repo().refresh_search_index()

    return report


def prune_model(document_json: dict, *, source: str, source_ref: Optional[str]) -> ProjectionReport:
    """Delete everything :func:`project_document` previously wrote for the
    model(s) declared in ``document_json`` — the write path's inverse, for
    when the document ITSELF is being deleted (not merely re-projected
    smaller, which ``project_document(..., partial=True)`` already handles
    by rewriting-then-pruning).

    Scoped exactly the way ``partial=True`` narrows a projection's prune:
    per model, to ``<source>/<source_ref or '_'>/<model_key>/`` — reusing
    the SAME ``_model_key``/``_scoped_id`` helpers ``project_document``
    itself uses to compute that prefix, so the two can never disagree on
    what one model "owns". A sibling model sharing ``(source, source_ref)``
    — every ``source='manual'`` row does — is therefore never touched, the
    identical guarantee a ``partial=True`` projection call gives on the
    write side.

    Column metadata is pruned per the model's OWN dataset table_ids —
    ``(table_id, source)`` — additionally sparing every column a SIBLING
    model of the same scope still claims (``_sibling_column_claims``):
    ``column_metadata`` rows carry no model identity, so two models binding
    the same ``table_id`` are otherwise indistinguishable there, and
    deleting model A must not take model B's projected columns with it.
    """
    report = ProjectionReport()
    models = [m for m in document_json.get("semantic_model") or [] if isinstance(m, dict)]
    # This call deletes ONE document while sibling models of the same scope
    # may live on — the delete analogue of a `partial` projection — so the
    # column prune must spare what those siblings still claim (a shared
    # `table_id`'s rows carry no model identity to tell them apart by).
    # `None` (read failure) skips the column prune entirely: stale leftover
    # rows beat deleting a sibling's live ones. Metric/glossary prunes are
    # unaffected either way — their rows carry the model-id prefix.
    sibling_claims = _sibling_column_claims(source, source_ref, {_model_key(m) for m in models})
    if sibling_claims is None:
        logger.warning(
            "Semantic projection (%s/%s): skipping the column prune on model delete — sibling claims "
            "unavailable, and pruning without them could delete a sibling model's live rows.",
            source,
            source_ref,
        )
    for model in models:
        prefix = _scoped_id(source, source_ref, _model_key(model)) + "/"
        report.metrics_pruned += _prune_metrics(source, source_ref, set(), scope_prefixes={prefix})
        report.glossary_pruned += _prune_glossary(source, source_ref, set(), scope_prefixes={prefix})

        written_by_table = {
            _column_table_id(dataset, source): set()
            for dataset in model.get("datasets") or []
            if isinstance(dataset, dict)
        }
        if written_by_table and sibling_claims is not None:
            # Same `column_source` remapping as the write side: a manual
            # model's rows live under `MANUAL_MODEL_COLUMN_SOURCE`, so this
            # prune deletes exactly what `project_document` wrote and can
            # never reach an admin-authored `source='manual'` row.
            _prune_columns(
                _column_source(source), written_by_table, keep_by_table=sibling_claims, source_ref=source_ref
            )

    if report.glossary_pruned:
        glossary_repo().refresh_search_index()

    return report


def _sibling_column_claims(
    source: str, source_ref: Optional[str], exclude_model_keys: set[str]
) -> Optional[dict[str, set[str]]]:
    """``table_id -> field names`` still claimed by the OTHER currently-stored
    valid models of this ``(source, source_ref)`` scope — the column prune's
    analogue of the ``model_prefixes`` narrowing the metric/glossary prunes
    get on a ``partial`` call.

    ``column_metadata`` rows carry no model identity (single ``source``
    column, no ``source_ref``), so a partial call cannot tell a SIBLING
    model's live rows from this model's stale ones by looking at the table
    alone. Two manual models binding datasets to the same ``table_id`` all
    write under one ``(source='manual' -> MANUAL_MODEL_COLUMN_SOURCE,
    source_ref=None)`` scope, and each admin-API write is its own
    ``partial=True`` projection — so without this read, re-projecting model
    A would prune model B's projected columns for the shared table. The
    claims are rebuilt from the stored documents themselves
    (``semantic_models``), excluding the models THIS call carries
    (``exclude_model_keys``, matched via the same :func:`_model_key` the
    writer uses) so a model's own dropped fields are still pruned.

    Returns ``None`` when the read fails — the caller must then SKIP the
    column prune rather than treat "unknown" as "no claims", because
    pruning against an empty claim set is exactly the sibling-deleting bug
    this helper exists to prevent (a stale leftover row beats a deleted
    live one).
    """
    try:
        from src.repositories import semantic_model_repo

        rows = semantic_model_repo().list_all(source=source, source_ref=source_ref)
        if source_ref is None:
            # `list_all(source_ref=None)` means "unfiltered", not "the NULL
            # origin" — same narrowing `src/semantic/importer.py` applies.
            rows = [r for r in rows if not r.get("source_ref")]
    except Exception:
        logger.warning(
            "Semantic projection (%s/%s): could not read sibling models to scope the column prune.",
            source,
            source_ref,
        )
        return None
    claims: dict[str, set[str]] = {}
    for row in rows:
        if row.get("status") != "valid" or not row.get("document_json"):
            continue
        for model in row["document_json"].get("semantic_model") or []:
            if not isinstance(model, dict) or _model_key(model) in exclude_model_keys:
                continue
            for dataset in model.get("datasets") or []:
                if not isinstance(dataset, dict):
                    continue
                table_id = _column_table_id(dataset, source)
                names = claims.setdefault(table_id, set())
                for column in dataset.get("fields") or []:
                    if isinstance(column, dict) and column.get("name"):
                        names.add(column["name"])
    return claims


def _in_prune_scope(row_id: str, scope_prefixes: Optional[set[str]]) -> bool:
    """Whether an in-(source, source_ref) row is also inside the narrowed
    prune scope. ``None`` means "no narrowing" — the whole (source,
    source_ref), which is what reclaims a model deleted upstream. A set of id
    prefixes restricts the prune to the models a partial projection actually
    carried; see ``project_document``'s ``partial``."""
    if scope_prefixes is None:
        return True
    return any(row_id.startswith(prefix) for prefix in scope_prefixes)


def _prune_metrics(
    source: str,
    source_ref: Optional[str],
    written: set[str],
    *,
    safe_prune: bool = False,
    scope_prefixes: Optional[set[str]] = None,
) -> int:
    repo = metric_repo()
    in_scope = {
        m["id"]
        for m in repo.list()
        if (m.get("source") or "") == source
        and (m.get("source_ref") or "") == (source_ref or "")
        and _in_prune_scope(m["id"], scope_prefixes)
    }
    if safe_prune and not written and in_scope:
        # Full-wipe guard: wrote nothing this pass while rows exist — a likely
        # empty-but-valid upstream, not a genuine "all metrics deleted". Skip
        # rather than delete every in-scope row (see project_document's
        # ``safe_prune``).
        logger.warning(
            "Semantic projection (%s/%s): wrote zero metrics while %d in-scope rows exist; "
            "skipping prune to avoid a full wipe. Existing rows retained.",
            source,
            source_ref,
            len(in_scope),
        )
        return 0
    pruned = 0
    for metric_id in in_scope - written:
        repo.delete(metric_id)
        pruned += 1
    return pruned


def _prune_glossary(
    source: str,
    source_ref: Optional[str],
    written: set[str],
    *,
    safe_prune: bool = False,
    scope_prefixes: Optional[set[str]] = None,
) -> int:
    repo = glossary_repo()
    # No list_all(): list(limit=...) with a high ceiling is the established
    # pattern for "give me every row" reads elsewhere (app/web/router.py).
    in_scope = {
        g["id"]
        for g in repo.list(limit=100_000)
        if (g.get("source") or "") == source
        and (g.get("source_ref") or "") == (source_ref or "")
        and _in_prune_scope(g["id"], scope_prefixes)
    }
    if safe_prune and not written and in_scope:
        logger.warning(
            "Semantic projection (%s/%s): wrote zero glossary terms while %d in-scope rows exist; "
            "skipping prune to avoid a full wipe. Existing rows retained.",
            source,
            source_ref,
            len(in_scope),
        )
        return 0
    pruned = 0
    for glossary_id in in_scope - written:
        repo.delete(glossary_id)
        pruned += 1
    return pruned


def _prune_columns(
    source: str,
    written_by_table: dict[str, set[str]],
    keep_by_table: Optional[dict[str, set[str]]] = None,
    *,
    source_ref: Optional[str] = None,
) -> None:
    """Prune fields dropped from a table this document still mentions.

    Scoped on ``(table_id, source)`` on DuckDB, and ``(table_id, source,
    source_ref)`` on Postgres. The two backends genuinely differ here rather
    than one being a stricter version of the other: the frozen DuckDB
    app-state schema (A3) has no ``column_metadata.source_ref`` column and
    cannot gain one, so a ``source_ref``-scoped prune only exists on
    Postgres (``migrations/versions/0075_column_meta_source_ref.py``);
    ``use_pg()`` selects the read at call time rather than the caller
    choosing.

    The collision this closes on Postgres, verified against this code: two
    writers sharing a ``source`` value but not a ``source_ref`` — two
    registered ``semantic_sources`` of the same kind (both ``ossie_git``,
    ``src/semantic/transports.py``) or two Keboola connections (both
    ``keboola_metastore``, ``connectors/keboola/semantic_layer.py``) — whose
    documents describe datasets resolving to the SAME ``table_id`` used to
    delete each other's field rows on every sync. Same-source_ref sibling
    models are already spared (:func:`_sibling_column_claims`); this was the
    cross-source_ref case that a bare ``source`` read could not see. DuckDB
    keeps the pre-existing, coarser ``(table_id, source)`` scoping — that is
    unchanged, not a regression: the schema simply has nowhere to record a
    ``source_ref`` to scope on.

    ``keep_by_table`` aside, this also only prunes tables the document
    still lists — a dataset dropped from the document entirely (not just
    emptied of fields) leaves its old columns in place, since there is no
    ``column_metadata`` read that enumerates "every table a given source
    has ever written to".

    ``keep_by_table`` spares additional columns per table — the claims of
    SIBLING models sharing this scope (:func:`_sibling_column_claims`),
    which a partial call must not treat as stale. ``None`` means "this call
    carries the whole scope": everything in-source not written here is
    genuinely stale.
    """
    from src.repositories import use_pg

    scope_by_ref = use_pg()
    repo = column_metadata_repo()
    for table_id, field_names in written_by_table.items():
        keep = field_names | (keep_by_table or {}).get(table_id, set())
        for existing in repo.list_for_table(table_id):
            if (existing.get("source") or "") != source:
                continue
            if scope_by_ref and (existing.get("source_ref") or "") != (source_ref or ""):
                continue
            if existing["column_name"] not in keep:
                repo.delete(table_id, existing["column_name"])
