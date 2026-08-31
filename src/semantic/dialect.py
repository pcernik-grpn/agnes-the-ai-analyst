"""Pick the SQL a DuckDB-backed instance can actually run.

The vendored, pinned Ossie schema's ``$defs.Dialect`` enum only accepts
``ANSI_SQL``, ``SNOWFLAKE``, ``MDX``, ``TABLEAU``, ``DATABRICKS``, ``MAQL``,
``BIGQUERY`` — there is no ``DUCKDB`` value, so no document can actually
declare ``dialect: DUCKDB`` today (it would fail schema validation). ANSI_SQL
is therefore the only dialect that is both authorable and locally runnable.

``_PREFERRED`` still lists DUCKDB ahead of ANSI_SQL as a dormant preference:
cheap forward-compat for if/when the vendored schema grows a DUCKDB value, at
zero cost today since it can never match. See
``tests/test_semantic_dialect.py::test_vendored_schema_does_not_offer_duckdb_as_a_dialect``
for the pin that flags the day this needs conscious re-activation.

Anything not locally runnable is reported as unusable WITH ITS REASON rather
than spliced into a query: a warehouse-specific fragment that happens to parse
is more dangerous than one that fails.
"""

from __future__ import annotations

_PREFERRED = ("DUCKDB", "ANSI_SQL")  # DUCKDB is dormant — see module docstring


def resolve_expression(expression: dict) -> tuple[str | None, str | None]:
    dialects = (expression or {}).get("dialects") or []
    by_name = {
        d.get("dialect"): d.get("expression")
        for d in dialects
        if d.get("expression") and isinstance(d.get("dialect"), str)
    }

    for name in _PREFERRED:
        if by_name.get(name):
            return by_name[name], None

    if not by_name:
        return None, "no expression in any usable dialect"
    offered = ", ".join(sorted(by_name))
    return None, f"only warehouse-specific dialects offered ({offered}); no DUCKDB or ANSI_SQL"


def resolve_expression_any(expression: dict) -> tuple[str | None, str | None, bool]:
    """Resolve one metric expression the way :func:`resolve_expression` does,
    but instead of reporting a warehouse-only expression as unusable, fall
    back to it — so a caller (the projector) can still store the raw SQL
    rather than dropping the metric.

    Returns ``(sql, dialect, locally_runnable)``:

    - a local dialect (DUCKDB, then ANSI_SQL) is offered: its SQL, its name,
      ``True`` — identical precedence and value to :func:`resolve_expression`.
    - only warehouse-specific dialect(s) offered: the first one declared (by
      document order — not alphabetical, not arbitrary dict order), its name,
      ``False``. The raw expression is warehouse-flavour SQL, not something
      this instance can run locally; the caller is responsible for saying so.
    - no expression in any dialect: ``(None, None, False)``.
    """
    dialects = (expression or {}).get("dialects") or []
    by_name = {
        d.get("dialect"): d.get("expression")
        for d in dialects
        if d.get("expression") and isinstance(d.get("dialect"), str)
    }

    for name in _PREFERRED:
        if by_name.get(name):
            return by_name[name], name, True

    for d in dialects:
        name = d.get("dialect")
        sql = d.get("expression")
        if sql and isinstance(name, str):
            return sql, name, False

    return None, None, False


def count_warehouse_only_metrics(metrics: list) -> int:
    """How many of ``metrics`` project into ``metric_definitions``
    (``src/semantic/projection.py::project_document``, via
    :func:`resolve_expression_any`) with only a warehouse-specific dialect
    (SNOWFLAKE, DATABRICKS, ...) rather than DUCKDB/ANSI_SQL.

    These metrics DO appear in the metrics catalog — they are not skipped —
    they just cannot run through a local DuckDB query and need server-side
    (remote/materialized) execution instead, per the ``notes`` entry
    ``project_document`` stamps on their row. Renamed from the earlier
    ``count_dialect_skipped_metrics``: before the projector learned to
    project a warehouse-only expression (rather than drop it), this count
    genuinely meant "silently missing from the catalog"; it no longer does.

    A metric with no expression at all is a different, pre-existing problem
    (an incomplete document, still a genuine skip — see
    ``ProjectionReport.skipped``) and is deliberately not counted here.
    """
    count = 0
    for metric in metrics or []:
        if not isinstance(metric, dict):
            continue
        sql, _dialect, locally_runnable = resolve_expression_any(metric.get("expression") or {})
        if sql is not None and not locally_runnable:
            count += 1
    return count
