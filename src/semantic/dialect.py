"""Pick the SQL a DuckDB-backed instance can actually run.

Preference order is DUCKDB, then ANSI_SQL. Anything else is reported as
unusable WITH ITS REASON rather than spliced into a query: a warehouse-specific
fragment that happens to parse is more dangerous than one that fails.
"""

from __future__ import annotations

from typing import Optional, Tuple

_PREFERRED = ("DUCKDB", "ANSI_SQL")


def resolve_expression(expression: dict) -> Tuple[Optional[str], Optional[str]]:
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


# The `resolve_expression` reason prefix that marks a metric which DID
# declare an expression, just not in a dialect this instance can run —
# distinct from "no expression in any usable dialect" (an incomplete
# document, a different problem). Matched by `count_dialect_skipped_metrics`
# below.
_UNSUPPORTED_DIALECT_PREFIX = "only warehouse-specific dialects offered"


def count_dialect_skipped_metrics(metrics: list) -> int:
    """How many of ``metrics`` were (or would be) silently dropped at
    projection because every dialect they declare is warehouse-specific —
    the same check ``src/semantic/projection.py`` runs per metric via
    :func:`resolve_expression`.

    A metric with no expression at all is a different, pre-existing problem
    (an incomplete document) and is deliberately not counted — this counts
    only the "had SQL, none of it runnable here" case the dialect skip is
    about.
    """
    count = 0
    for metric in metrics or []:
        if not isinstance(metric, dict):
            continue
        sql, reason = resolve_expression(metric.get("expression") or {})
        if sql is None and isinstance(reason, str) and reason.startswith(_UNSUPPORTED_DIALECT_PREFIX):
            count += 1
    return count
