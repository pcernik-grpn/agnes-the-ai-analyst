"""Orphaned semantic bindings — Block 5 of #1707.

Deleting a registered table (or renaming it to a new id, since
``table_registry`` has no rename-in-place) leaves no cascade behind for two
projections that reference it: ``metric_definitions`` rows bound to it, and
``column_metadata`` rows profiled against it. Nothing enforced that binding
and nothing detected when it broke — this module is the detection half only.
**It never deletes, prunes, or blocks a delete** — see the health check that
surfaces its findings (``orphaned_table_bindings``,
``src/semantic/coverage.py::compute_semantic_layer_health``) for where an
admin actually sees this.

Backend-agnostic: every read goes through a ``*_repo()`` factory
(``table_registry_repo``, ``metric_repo``, ``column_metadata_repo``), never a
repo class instantiated directly, and none of the three is Postgres-gated —
this runs the same way on a DuckDB- or a Postgres-backed instance.

Two different bindings, two different keys against ``table_registry``:

* A metric binds to a table by NAME. ``metric_definitions.table_name`` plus
  every entry of the multi-table ``tables[]`` array (a JOIN metric binds to
  two) — the same convention ``src/semantic/coverage.py::_metric_tables``
  reads for the cross-domain coverage report. ``table_name`` is filled from
  ``table_registry.name`` at projection time
  (``src/semantic/projection.py``'s ``_bind_metric``/``resolve()``), never
  from ``table_registry.id``.
* A profiled column binds to a table by ID. ``column_metadata.table_id`` is
  ``table_registry.id`` verbatim
  (``src/semantic/projection.py::_column_table_id``'s docstring spells out
  why the two are NOT interchangeable: ``id`` is a normalized slug of
  ``name``, e.g. ``request.name.strip().lower().replace(" ", "_")`` in
  ``app/api/admin.py``, and can diverge from it whenever the display name has
  spaces or uppercase).

Because the two bindings use different keys, a metric orphan and a column
orphan are never merged into one finding even when they happen to name "the
same" deleted table — a coincidence of the id and the name looking alike is
not evidence they are.
"""

from __future__ import annotations

from typing import Any


def _bound_table_names(metric: dict[str, Any]) -> set[str]:
    """Every table name one metric binds to.

    Mirrors ``src/semantic/coverage.py::_metric_tables``, but per-metric
    rather than across the whole list, so each metric's own missing
    binding(s) can be reported instead of only the aggregate.
    """
    names: set[str] = set()
    if metric.get("table_name"):
        names.add(metric["table_name"])
    for extra in metric.get("tables") or []:
        if extra:
            names.add(extra)
    return names


def find_orphaned_metrics(
    metrics: list[dict[str, Any]], known_table_names: set[str]
) -> list[dict[str, Any]]:
    """``metric_definitions`` rows bound to a table name no live
    ``table_registry`` row carries.

    A metric with no table binding at all (``table_name`` is ``None`` and
    ``tables`` is empty — a constant or dimension-only metric, or one not
    yet projected) is never flagged: it was never bound, so a table delete
    cannot have orphaned it.
    """
    orphans: list[dict[str, Any]] = []
    for metric in metrics:
        bound = _bound_table_names(metric)
        if not bound:
            continue
        missing = sorted(bound - known_table_names)
        if missing:
            orphans.append(
                {
                    "metric_id": metric["id"],
                    "name": metric.get("name"),
                    "missing_tables": missing,
                }
            )
    return orphans


def find_orphaned_columns(
    columns: list[dict[str, Any]], known_table_ids: set[str]
) -> list[dict[str, Any]]:
    """``column_metadata`` rows whose ``table_id`` names no live
    ``table_registry.id``.

    Grouped one finding per orphaned table — a deleted table that had 40
    profiled columns is one finding with ``column_count=40``, not 40 rows
    repeating the same dead table_id.
    """
    by_table: dict[str, int] = {}
    for column in columns:
        table_id = column.get("table_id")
        if table_id and table_id not in known_table_ids:
            by_table[table_id] = by_table.get(table_id, 0) + 1
    return [{"table_id": table_id, "column_count": count} for table_id, count in sorted(by_table.items())]


def find_orphaned_table_bindings() -> dict[str, list[dict[str, Any]]]:
    """Every semantic object still bound to a ``table_registry`` row that is
    gone — a delete, or a rename that landed under a new id/name with no
    cascade.

    Reaches its repos through the ``*_repo()`` factories, never
    instantiated directly, and works against whichever backend is active:
    nothing here is Postgres-gated.
    """
    from src.repositories import column_metadata_repo, metric_repo, table_registry_repo

    tables = table_registry_repo().list_all()
    known_table_ids = {t["id"] for t in tables}
    known_table_names = {t["name"] for t in tables if t.get("name")}

    metrics = metric_repo().list()
    columns = column_metadata_repo().list_all()

    return {
        "orphaned_metrics": find_orphaned_metrics(metrics, known_table_names),
        "orphaned_columns": find_orphaned_columns(columns, known_table_ids),
    }
