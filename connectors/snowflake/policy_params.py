"""Bind an access policy's identity values through Snowflake's own
parameter mechanism (S2, RLS review issue #1979; table access policies
design §6.2, §7.1). Mirrors ``connectors/databricks/policy_params.py``, and
diverges from it in exactly one place -- see below.

``policied_relation(..., dialect="snowflake")`` hands back a policy body
whose ``$name`` placeholders sqlglot already rendered as ``:name`` --
sqlglot's generic named-placeholder token, the same one it uses for
Databricks. Snowflake's own execution paths, though, do NOT bind by name:

- The DuckDB ``snowflake`` community extension's ATTACH federation runs
  DuckDB SQL text with DuckDB's OWN native ``$name`` binding -- it never
  sees this module's output at all, because a caller referencing a
  Snowflake-registered table by its registry NAME already executes,
  correctly filtered, through the pre-existing ``dialect="duckdb"`` arm
  (see ``src/access_policy.py::_transpile_policy_to_snowflake``'s
  docstring).
- Every execution path that WOULD run genuinely Snowflake-native SQL text
  in this codebase -- the ``snowflake_query()`` DuckDB pass-through
  (``connectors/snowflake/semantic_ossie.py``), or a future
  ``snowflake-connector-python`` cursor -- binds parameters POSITIONALLY,
  via the connector's ``qmark`` (``?``) or ``numeric`` (``:1``, ``:2``, ...)
  paramstyle. Snowflake has no named-bind-variable syntax a driver accepts
  for an ordinary ``SELECT`` (``:name`` in Snowflake SQL is a *Snowflake
  Scripting* local-variable reference, a different thing entirely, valid
  only inside a stored procedure/scripting block -- not a bind marker an
  external driver resolves against request parameters).

So this module's ONE job, beyond what the Databricks module has to do, is
renumbering: EVERY placeholder -- not only the array-valued one -- is
rewritten to a numbered marker (``:1``, ``:2``, ...) in left-to-right
document order, with a Python list of raw bind values built to match. A
scalar policy variable referenced twice in one body (unusual, but legal SQL)
gets two markers and two copies of the same value -- positional binding has
no notion of "the same name, bound once", unlike Databricks' named
``parameters`` field.

``$user_groups`` is a list, and -- same story as Databricks -- there is no
array bind type on this path either. The fix is the same shape §7.1
documents for Databricks: replace the array-valued marker with an array
*expression* built from scalar markers. Snowflake accepts bracket literal
syntax (``[v1, v2, ...]``) as an array constructor -- the same construct
``ARRAY_CONSTRUCT(...)`` builds -- so the substitution needs no function
name at all, only numbered scalar markers inside brackets. An empty group
list becomes ``[]``, which needs no type cast to fail closed against
``ARRAY_CONTAINS`` (Snowflake's own ``[]`` infers a compatible type from
context, unlike Databricks' bare ``ARRAY()`` which is ambiguously
``ARRAY<VOID>`` and must be cast explicitly).

The substitution is AST-level, not textual, for the same reason the
Databricks module gives: a regex over ``:user_groups`` would also rewrite
the same characters inside a string literal, and a policy body is exactly
the kind of SQL that contains literals.

Dependency coupling worth knowing before bumping sqlglot (N4)
---------------------------------------------------------------
Every policy on this engine -- scalar or group-based -- rests on one
sqlglot behaviour, pinned the same way
``connectors/databricks/policy_params.py`` pins its own two: writing DuckDB
``$name`` to the ``snowflake`` dialect renders ``:name``, and reading that
back with ``dialect="snowflake"`` yields an ``exp.Placeholder`` whose
``.name`` is ``name``. Drift here makes this module find no markers at all,
which raises :class:`SnowflakePolicyBindingError` and denies every policy on
this engine -- fail-closed, not a leak, but a wide outage a dependency bump
could trigger. ``TestSqlglotCoupling`` (mirroring the Databricks module's
own class of the same name) pins both directions. Verified on sqlglot
30.17.0.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import sqlglot
from sqlglot import exp


class SnowflakePolicyBindingError(Exception):
    """The policy body could not be prepared for parameter binding.

    Callers turn this into the same table-scoped ``PolicyError`` every other
    resolution failure raises (§16, §17) -- never into a message quoting the
    policy body or the engine's own error.
    """


def bind_policy_parameters(relation_sql: str, params: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Return ``(sql, values)`` ready for a Snowflake driver's positional
    bind (``qmark``/``numeric`` paramstyle).

    ``relation_sql`` is a Snowflake-dialect policy body from
    ``policied_relation(..., dialect="snowflake")``; ``params`` is that
    relation's ``.params`` -- the subset of ``user_email`` / ``user_id`` /
    ``user_groups`` the body actually references.

    Every placeholder is renumbered to ``:1``, ``:2``, ... in document
    order; ``values[i - 1]`` is the value bound at marker ``:i``. A list
    value has its single marker replaced by a bracket array literal
    (``[:i, :i+1, ...]``) over freshly generated scalar markers -- or ``[]``
    for a caller in no groups, a legitimate, security-relevant state (must
    match nothing), never an error.
    """
    if not params:
        return relation_sql, []

    try:
        tree = sqlglot.parse_one(relation_sql, dialect="snowflake")
    except Exception as exc:  # noqa: BLE001 -- any parse failure denies
        raise SnowflakePolicyBindingError("policy body could not be parsed for parameter binding") from exc
    if tree is None:
        raise SnowflakePolicyBindingError("policy body parsed to nothing")

    array_params = {k: list(v) for k, v in params.items() if isinstance(v, (list, tuple, set))}
    scalar_params = {k: v for k, v in params.items() if k not in array_params}

    values: List[Any] = []
    found_names: set = set()
    counter = [0]

    def _next_marker() -> exp.Placeholder:
        counter[0] += 1
        return exp.Placeholder(this=str(counter[0]))

    def _replace(node: exp.Expression) -> exp.Expression:
        if not (isinstance(node, exp.Placeholder) and node.name in params):
            return node
        name = node.name
        found_names.add(name)
        if name in array_params:
            markers = []
            for value in array_params[name]:
                markers.append(_next_marker())
                values.append(value)
            return exp.Array(expressions=markers)
        marker = _next_marker()
        values.append(scalar_params[name])
        return marker

    rewritten = tree.transform(_replace)

    missing = set(params) - found_names
    if missing:
        # `params` only ever carries variables `_referenced_variables` found
        # in the ORIGINAL (DuckDB-dialect) body, so a marker that survives
        # transpilation must be findable here. If it is not, the body no
        # longer matches what we are about to bind -- deny rather than
        # execute a statement with an unbound marker (which Snowflake would
        # reject anyway) or, worse, one whose filter silently vanished.
        raise SnowflakePolicyBindingError(
            f"policy variable(s) {sorted(missing)!r} not found in the transpiled body"
        )

    return rewritten.sql(dialect="snowflake"), values
