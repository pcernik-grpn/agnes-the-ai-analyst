"""Static (AST-level) ``masked`` marker for a table access policy body
(table access policies design doc §11).

``src.access_policy.effective_schema`` computes ``hidden`` by DESCRIBEing the
policy-wrapped relation and diffing it against the base table's own raw
schema -- a column absent from the wrapped result is gone. That trick cannot
answer "is this column *masked*": the design doc's own canonical example,
``md5(email) AS email``, DESCRIBEs as ``email VARCHAR`` on both sides of the
diff -- same name, same type, no signal at all that the value underneath was
rewritten. Detecting a mask needs a STATIC read of the policy body's own
``SELECT``-list expressions, not a runtime DESCRIBE diff.

``masked_output_columns`` does exactly that, one ``sqlglot`` parse of the
already-saved (already-validated-at-save-time) policy text: an output column
whose expression is anything other than a bare column reference to itself
(``email``, ``invoices.email``, or the no-op rename ``email AS email``) or a
``*`` / ``* EXCLUDE (...)`` pass-through is masked -- ``md5(email) AS email``,
a ``CASE ... END AS email`` redaction, ``agnes_hmac(email) AS email``, and
``CAST(NULL AS VARCHAR) AS ssn`` all qualify. A column that ``* EXCLUDE``
removes is not masked, it is ``hidden`` -- ``effective_schema``'s own DESCRIBE
diff already reports that, and this module has nothing to add there.

Fails closed the OTHER way from a security check: an unparseable policy body
(a hand-edited registry row, a future authoring path this walk doesn't know
yet) yields an EMPTY result rather than raising -- ``masked`` is an
informational marker for a schema surface, not an access-control decision, so
a parse failure here must never take down ``/api/v2/schema`` the way a
`PolicyError` from ``effective_schema``'s own DESCRIBE calls legitimately can.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

__all__ = ["masked_output_columns"]


def masked_output_columns(policy_sql: str) -> frozenset[str]:
    """Lower-cased output column names ``policy_sql``'s top-level ``SELECT``
    projection list computes rather than merely passes through.

    Only the OUTER select list is inspected -- a CTE's own body (§15's
    ``policy_mapping`` join idiom) never contributes an output column of the
    policy's own relation, matching the scope ``effective_schema``'s DESCRIBE
    diff already has.
    """
    try:
        statement = sqlglot.parse_one(policy_sql, read="duckdb")
    except Exception:
        return frozenset()
    if not isinstance(statement, exp.Select):
        # The save-time validator (`src.access_policy_validate`) only ever
        # accepts a single top-level SELECT, so this is defensive only: a
        # row written outside that path (hand-edited DB value) gets no
        # marker rather than a guess.
        return frozenset()

    masked: set[str] = set()
    for projection in statement.expressions:
        name, is_masked = _classify(projection)
        if is_masked and name:
            masked.add(name.lower())
    return frozenset(masked)


def _classify(projection: exp.Expression) -> tuple[str | None, bool]:
    """``(output_name, is_masked)`` for one top-level projection expression.

    ``output_name`` is ``None`` for a bare ``*`` (or an aliased ``*``, a shape
    sqlglot never actually produces but is handled the same way regardless):
    a star names no single column, and the base-schema diff
    ``effective_schema`` already runs is what accounts for a column ``*
    EXCLUDE`` removes.
    """
    if isinstance(projection, exp.Star):
        return None, False

    if isinstance(projection, exp.Alias):
        inner = projection.this
        if isinstance(inner, exp.Star):
            return None, False
        if isinstance(inner, exp.Column) and inner.name.lower() == (projection.alias or "").lower():
            # `email AS email` -- a no-op rename-to-itself, still a bare
            # pass-through of the base column's own value.
            return projection.alias, False
        # Anything else aliased -- a function call, a CASE, a CAST, a rename
        # to a DIFFERENT name -- computes (or relabels) rather than passing
        # the base column through under its own name.
        return projection.alias or None, True

    if isinstance(projection, exp.Column):
        # Bare column reference, no alias -- output name is the column's own
        # name, unmasked, whether or not it carries a table qualifier
        # (`invoices.email`).
        return projection.name, False

    # A bare, unaliased expression (e.g. `SELECT md5(email) FROM ...` with no
    # `AS`) -- masked, named by whatever sqlglot's own generated output name
    # is (often empty for a DuckDB-generated column name it cannot predict).
    return projection.output_name or None, True
