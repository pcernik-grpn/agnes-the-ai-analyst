"""Compile a structured builder spec into the canonical DuckDB policy SQL.

The stored policy is always SQL -- ``src/access_policy.py`` runs the body
verbatim -- so this module is the no-SQL builder's *generator*, not a second
source of truth. Its hard invariants:

1. The projection is **explicit** and **fixed at save time**; no ``SELECT *``
   survives. A new source column added after the policy is saved is therefore
   never returned (deny-by-omission), and a removed source column causes the
   policy to fail closed at execution time (deny-by-error).
2. A masked column is listed exactly once in the projection, so the
   two-column plaintext leak ``SELECT *, md5(col) AS col`` can never be
   produced.
3. ``unmask`` masks preserve the original column type for allowed groups and
   return ``'*****'`` for text-like columns / ``NULL`` for all other types when
   the caller is not in any allowed group.
4. Every mask is **type-preserving** and keeps the column's own output name, so
   attaching a policy never changes what ``DESCRIBE`` (and therefore
   ``agnes schema``, the catalog, and the effective-schema surfaces) reports.
   The partial masks (``last4``, ``email_partial``) are string surgery and buy
   this by being text-only: on any other column type the compile is refused
   rather than quietly casting the output to text.

Pure and HTTP-free so it unit-tests without a request and can be reused by the
CLI later.

The spec shape (all keys optional except ``table``)::

    {
      "table": "invoices",
      "row_rules": [{"column": str, "op": ROW_OP, "value": Any}],
      "row_combine": "and" | "or",
      "column_masks": {col: MASK | {"choice": MASK, "group": str} |
                              {"choice": MASK, "groups": [str, ...]}},
    }

``ROW_OP`` is one of ``in_caller_groups`` (row's column is one of the caller's
live groups), ``eq_caller_email`` / ``eq_caller_id`` (self-owned rows), ``eq``
/ ``in`` (literal match). ``MASK`` is ``show`` | ``hide`` | ``nullify`` |
``hash`` | ``unmask`` | ``last4`` | ``email_partial`` (``unmask`` needs a
``group`` or ``groups`` list; ``last4`` and ``email_partial`` are text-only and
carry no group allowlist of their own).

``columns`` is the table's real column list from a DESCRIBE; each entry may be
a column name string, a ``(name, type)`` tuple, or a ``{"name": ..., "type": ...}``
dict.

A spec reference to a column the table does not have is handled by which way it
fails, not by a single blanket rule:

* a **mask** on an unknown column is dropped with a warning -- fail-closed,
  because the projection is assembled from ``columns`` only, so that column is
  never projected at all and there is no plaintext copy the dropped mask was
  meant to cover;
* a **row rule** on an unknown column raises ``ValueError`` -- dropping it is
  fail-OPEN, since a spec whose only rule names a since-renamed column would
  compile to a WHERE-less policy handing every caller the whole table (and a
  dropped rule beside a surviving one silently widens the policy instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from src.sql_ident import quote_ident

# Row operators the builder can emit. Kept explicit so an unknown op is a loud
# error, not a silently-dropped rule.
_ID_TOKENS = {
    "eq_caller_email": "$user_email",
    "eq_caller_id": "$user_id",
}

# DuckDB types that should be treated as text for the unmask fallback.
_TEXT_TYPE_KEYWORDS = ("VARCHAR", "TEXT", "STRING")

# ---------------------------------------------------------------------------
# Partial masks (`last4`, `email_partial`). Every constant below ends up in the
# emitted SQL, so each is named once here rather than spelled inline.
#
# The asterisk runs are deliberately a FIXED width, never one derived from the
# value: a redaction whose length tracked the original's would publish that
# length, which for a national id or an account number is often most of what is
# left to guess. Both expressions stay inside the save-time validator's existing
# function allowlist (`src/access_policy_validate.py`) -- CASE, LENGTH, CONCAT,
# SUBSTRING, REGEXP_REPLACE and LIKE -- on purpose: a new mask kind must not
# widen what an admin's arbitrary policy SQL may do on every analyst request.
_LAST4_KEEP = 4
_LAST4_PREFIX = "****"
_EMAIL_LOCAL_REDACTION = "*****"
# Matches the local part of an address, anchored at the start, with NO capture
# group. Backreference spelling is the one place the three engines genuinely
# disagree (DuckDB `\1`, BigQuery `\\1`, Databricks `$1`), and `REGEXP_EXTRACT`
# reads its third argument as a position on BigQuery but a group index on
# Databricks -- a group-free `REGEXP_REPLACE` to the empty string sidesteps both.
_EMAIL_LOCAL_PART_PATTERN = "^[^@]*"
# At least one character, then an `@`. A value that fails this -- no `@` at all,
# an empty local part, or the empty string -- is redacted whole rather than
# partially, so the mask never half-reveals a value it cannot properly split.
_EMAIL_SHAPE_PATTERN = "_%@%"


@dataclass
class CompiledPolicy:
    sql: str
    excluded: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mask_choice(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("choice", ""))
    return str(m)


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):  # bool is an int subclass -- guard before the numeric branch
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _normalize_columns(columns: Sequence[Any]) -> list[tuple[str, str]]:
    """Convert flexible column descriptors into (name, type) pairs.

    Strings are accepted for backwards compatibility in unit tests, defaulting
    type to VARCHAR. Callers that have real DESCRIBE output should always pass
    (name, type) tuples or dicts.
    """
    out: list[tuple[str, str]] = []
    for c in columns:
        if isinstance(c, str):
            out.append((c, "VARCHAR"))
        elif isinstance(c, dict):
            out.append((str(c["name"]), str(c.get("type", "VARCHAR"))))
        elif isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((str(c[0]), str(c[1])))
        else:
            raise ValueError(f"unsupported column descriptor: {c!r}")
    return out


def _is_text_type(col_type: str) -> bool:
    if not col_type:
        return False
    # Anchor on the whole base type name: composite types such as `VARCHAR[]`,
    # `STRUCT(x VARCHAR)` or `MAP(VARCHAR, VARCHAR)` merely mention a text
    # keyword and must NOT take the string-redaction fallback (its CASE branches
    # would then have incompatible types).
    upper = col_type.strip().upper()
    # Arrays, generic collections, and struct/map/list containers are never
    # plain scalar text even when their inner type is text.
    if "[" in upper or "<" in upper or upper.startswith(("STRUCT", "MAP", "LIST", "ARRAY")):
        return False
    # Strip optional length/size suffixes like `VARCHAR(255)` before matching.
    base = upper.split("(", 1)[0].strip()
    return base in _TEXT_TYPE_KEYWORDS


def _unmask_condition(groups: list[str]) -> str:
    """Build the WHEN clause for an unmask mask.

    Empty allowlist means no one is allowed to see the real value, so the
    condition is ``FALSE``.
    """
    if not groups:
        return "FALSE"
    tests = [f"list_contains($user_groups, {_sql_literal(g)})" for g in groups]
    return " OR ".join(tests) if len(tests) > 1 else tests[0]


def _unmask_groups(raw: Any) -> list[str]:
    """Extract the allowlist group(s) from an unmask mask entry."""
    if isinstance(raw, dict):
        if "groups" in raw:
            groups = raw["groups"]
            return [g for g in (groups if isinstance(groups, (list, tuple)) else [groups]) if g]
        if raw.get("group"):
            return [raw["group"]]
    return []


def _masked_fallback(col_type: str) -> str:
    """The fallback value for an unmask mask when the caller is not allowed.

    Text-like columns get a fixed redaction string; everything else gets a
    type-preserving NULL so the CASE expression keeps the original column type.
    """
    if _is_text_type(col_type):
        return _sql_literal("*****")
    return f"CAST(NULL AS {col_type})"


def _last4_expr(q: str) -> str:
    """Keep the last four characters, replace everything before them with a
    fixed asterisk run (``123456789`` -> ``****6789``).

    The explicit ``IS NULL`` arm is load-bearing: DuckDB's ``CONCAT`` treats NULL
    as the empty string, so without it a NULL would surface as the mask string
    ``'****'`` -- a real-looking value where there is none. The ``LENGTH <= 4``
    arm is what stops a short value from being echoed back whole, and it also
    confines the negative ``SUBSTRING`` start to the length range where DuckDB,
    BigQuery and Databricks agree exactly.
    """
    return (
        f"CASE WHEN {q} IS NULL THEN NULL "
        f"WHEN LENGTH({q}) <= {_LAST4_KEEP} THEN {_sql_literal(_LAST4_PREFIX)} "
        f"ELSE CONCAT({_sql_literal(_LAST4_PREFIX)}, SUBSTRING({q}, -{_LAST4_KEEP})) END"
    )


def _email_partial_expr(q: str) -> str:
    """``john.doe@example.com`` -> ``j*****@example.com``.

    The domain survives verbatim -- that is the point of the mask (an analyst can
    still segment by domain) and also its limit: this is a partial mask, not
    anonymization, and on a small domain the surviving first character can be
    enough to re-identify. The ``IS NULL`` arm comes first because ``NULL LIKE
    ...`` is NULL, which would otherwise fall through to the ELSE and turn a NULL
    into ``'*****'``.
    """
    redaction = _sql_literal(_EMAIL_LOCAL_REDACTION)
    kept_domain = f"REGEXP_REPLACE({q}, {_sql_literal(_EMAIL_LOCAL_PART_PATTERN)}, '')"
    return (
        f"CASE WHEN {q} IS NULL THEN NULL "
        f"WHEN {q} LIKE {_sql_literal(_EMAIL_SHAPE_PATTERN)} "
        f"THEN CONCAT(SUBSTRING({q}, 1, 1), {redaction}, {kept_domain}) "
        f"ELSE {redaction} END"
    )


# Masks that are string surgery and therefore text-only. Applying one to a
# numeric/temporal/composite column would have to either CAST (silently changing
# the output column's type, which every DESCRIBE-based schema surface downstream
# then reports) or emit nonsense, so the compiler refuses instead.
_TEXT_ONLY_MASKS = {
    "last4": _last4_expr,
    "email_partial": _email_partial_expr,
}


def _predicate(rule: dict) -> str:
    col = quote_ident(rule["column"])
    op = rule.get("op")
    if op == "in_caller_groups":
        # The transpile-safe idiom the design doc mandates (never `col IN
        # (unnest($user_groups))`, which the validator only warns on and
        # BigQuery bloats).
        return f"list_contains($user_groups, {col})"
    if op in _ID_TOKENS:
        return f"{col} = {_ID_TOKENS[op]}"
    if op == "eq":
        return f"{col} = {_sql_literal(rule.get('value'))}"
    if op == "in":
        vals = rule.get("value") or []
        return f"{col} IN (" + ", ".join(_sql_literal(v) for v in vals) + ")"
    raise ValueError(f"unknown row op: {op!r}")


def compile_policy(spec: dict, columns: Sequence[Any]) -> CompiledPolicy:
    """Turn a structured builder ``spec`` into canonical policy SQL."""
    col_info = _normalize_columns(columns)
    known = {c[0] for c in col_info}
    col_type_by_name = {c[0]: c[1] for c in col_info}
    warnings: list[str] = []
    excluded: list[str] = []
    derived: list[str] = []
    projections: list[str] = []

    # Build an expression for each masked column, then assemble the final
    # projection in the table's native column order. This preserves the output
    # schema order, avoids duplicate projections, and never falls back to `*`.
    masked_exprs: dict[str, str] = {}
    for col, raw in (spec.get("column_masks") or {}).items():
        if col not in known:
            warnings.append(f"unknown column ignored: {col}")
            continue
        choice = _mask_choice(raw)
        q = quote_ident(col)
        col_type = col_type_by_name.get(col, "VARCHAR") or "VARCHAR"
        if choice == "show":
            continue
        excluded.append(col)
        if choice == "hide":
            # Hidden columns are omitted from the fixed projection entirely.
            continue
        if choice == "nullify":
            # Cast keeps the column type unchanged for the caller.
            expr = f"CAST(NULL AS {col_type}) AS {q}"
            derived.append(col)
        elif choice == "hash":
            expr = f"md5({q}) AS {q}"
            derived.append(col)
        elif choice == "unmask":
            groups = _unmask_groups(raw)
            fallback = _masked_fallback(col_type)
            expr = f"CASE WHEN {_unmask_condition(groups)} THEN {q} ELSE {fallback} END AS {q}"
            derived.append(col)
        elif choice in _TEXT_ONLY_MASKS:
            if not _is_text_type(col_type):
                raise ValueError(
                    f"mask {choice!r} applies to text columns only; column {col!r} is {col_type} "
                    "-- use 'nullify', 'hash' or 'hide' for a non-text column"
                )
            expr = f"{_TEXT_ONLY_MASKS[choice](q)} AS {q}"
            derived.append(col)
        else:
            raise ValueError(f"unknown mask: {choice!r}")
        masked_exprs[col] = expr

    projections = []
    for name, _ in col_info:
        if name in masked_exprs:
            projections.append(masked_exprs[name])
        elif name not in excluded:
            projections.append(quote_ident(name))

    if not projections:
        # Fail closed: a policy that would project nothing cannot become `SELECT *`.
        raise ValueError("policy would select no columns; leave at least one column visible")

    # Fail closed on a row rule whose column the table does not have. Dropping it
    # (the previous behaviour) is the one unknown-column case that fails OPEN:
    # with the rule gone the compiled body either loses its WHERE clause
    # entirely -- handing every caller the whole table -- or, beside a surviving
    # rule, quietly widens to a broader row set than the admin authored. Neither
    # is visible in the generated SQL, which is why a warning is not enough here.
    rules = list(spec.get("row_rules") or [])
    for r in rules:
        if r.get("column") not in known:
            raise ValueError(
                f"row rule references unknown column {r.get('column')!r} (op {r.get('op')!r}); "
                "dropping it would widen the policy, so fix or remove the rule "
                "-- the column may have been renamed or removed upstream"
            )

    where = ""
    if rules:
        joiner = " OR " if spec.get("row_combine") == "or" else " AND "
        where = " WHERE " + joiner.join(_predicate(r) for r in rules)

    sql = f"SELECT {', '.join(projections)} FROM {quote_ident(spec['table'])}{where}"
    if not rules and not excluded and not derived:
        warnings.append("This policy returns the full table to every caller -- nothing is filtered or masked.")
    return CompiledPolicy(sql=sql, excluded=excluded, derived=derived, warnings=warnings)
