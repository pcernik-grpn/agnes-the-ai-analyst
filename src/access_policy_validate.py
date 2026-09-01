"""Save-time validator for table access-policy SQL.

An access policy (table access policies design doc, §14) is an admin-authored
``SELECT`` that Agnes substitutes for a table on every server-side read, with
the caller's identity available as bound ``$user_email`` / ``$user_id`` /
``$user_groups`` parameters. Because that SQL then runs on the server's
analytics connection on every analyst request (§14's closing paragraph), it
is validated once here, at save time -- never at query time, where a
rejection would be a live outage instead of a blocked write.

Allowlist-shaped throughout, never denylist -- the same choice §5.2 rule 5
makes for analyst SQL, and the one the internal connector already made for
its own filter clauses: a table reference, function, or node type that is
not explicitly recognized is rejected, whether or not it is *known* to be
dangerous. That is what keeps the boundary meaningful once §1.2's (currently
out of scope) non-admin authoring arrives.

``validate_policy_sql`` itself is pure static analysis over the parsed SQL
text and never executes anything. ``probe_policy`` -- the live ``LIMIT 0``
execution probe of §14.6 -- is the one deliberate exception: static rules
cannot catch a policy that references a column the underlying table has
since dropped (or never had), so it actually runs the candidate SQL, with
``LIMIT 0`` and a throwaway identity, against a connection the caller
supplies. Both are called from the same save-time path (``app/api/admin.py``'s
policy-write handler) -- ``probe_policy`` only after ``validate_policy_sql``
has already accepted the SQL's shape.
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


class PolicyValidationError(ValueError):
    """Raised by :func:`validate_policy_sql` for any rule violation.

    ``reason`` is a stable, machine-matchable code (rendered by the admin
    API/CLI/UI per §16); ``detail`` is the human-readable explanation, which
    names the offending table/variable/construct so a retry -- human or
    agent -- has something concrete to fix rather than a bare rejection.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"[{reason}] {detail}")


# --------------------------------------------------------------------------
# Rule 2 -- DDL/DML and ATTACH-family statements are never a policy body.
# ATTACH/DETACH/INSTALL/PRAGMA each have a dedicated sqlglot node; LOAD and
# CALL have none and fall back to the generic ``exp.Command`` -- and since a
# legitimate SELECT never produces a Command node, treating every Command as
# forbidden is safe, not merely convenient.
_FORBIDDEN_STATEMENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Copy,
    exp.Merge,
    exp.Alter,
    exp.Command,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.Pragma,
)

# --------------------------------------------------------------------------
# Rule 3 -- the closed set of structural node types a policy body may use.
# Function calls (``exp.Func`` -- this also covers ``exp.And``/``exp.Or``/
# ``exp.Xor``, which sqlglot models as functions, and the ``exp.Case``/
# ``exp.If`` pair behind CASE/IF/IIF) are deliberately NOT listed here; they
# are checked by NAME in ``_ALLOWED_FUNCTION_NAMES`` instead -- the same
# split ``app/api/where_validator.py::_walk_functions`` uses, because there
# is no closed set of *classes* for "every function sqlglot might parse".
_PERMITTED_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    # statement shape
    exp.Select,
    exp.From,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.With,
    exp.CTE,
    # table sources
    exp.Table,
    exp.TableAlias,
    exp.Join,
    exp.Subquery,
    # projection
    exp.Star,
    exp.Column,
    exp.Identifier,
    exp.Alias,
    # values
    exp.Literal,
    exp.Placeholder,
    exp.Boolean,
    exp.Null,
    exp.Paren,
    exp.Tuple,
    exp.DataType,
    # NOTE: neither exp.ColumnDef nor exp.DataTypeParam is listed here. Both
    # do appear inside a type declaration -- a STRUCT's field list parses into
    # ColumnDef, and DECIMAL(18,2) / TIMESTAMP(3) carry precision as
    # DataTypeParam -- but a flat entry would permit one anywhere in the body on
    # the strength of an assumption about where it can appear.
    # `_reject_disallowed_constructs` checks the position instead: under a
    # DataType, permitted; anywhere else, refused.
    # operators that are NOT exp.Func subclasses
    exp.Not,
    exp.Neg,
    exp.Distinct,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
    exp.SimilarTo,
)

# Function names (sqlglot's canonical ``sql_name()`` / ``exp.Anonymous``
# name, upper-cased) a policy body may call. Deliberately narrow: the
# masking/pseudonymization and group-membership primitives the design names
# explicitly (§1, §6.5), logical connectors, ordinary conditionals, and a
# small set of everyday value functions. Extend deliberately, not by
# precedent creep -- every addition widens what an admin's arbitrary SQL can
# do on every analyst request (§14's closing paragraph).
_ALLOWED_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        # logical connectors -- sqlglot parses AND/OR/XOR as exp.Func subclasses.
        "AND",
        "OR",
        "XOR",
        # masking / pseudonymization (§1, §21 -- md5 is a pseudonym, not a
        # mask, but it is the design doc's own documented example).
        "MD5",
        # group-membership idiom (§6.5) -- list_contains() parses to ArrayContains.
        "ARRAY_CONTAINS",
        # the discouraged-but-still-valid unnest idiom (§6.5), warned about
        # rather than rejected -- see _warn_group_membership_idiom.
        "EXPLODE",
        # pattern-matching functions -- allowed for LITERAL patterns; rule 5
        # separately rejects an identity variable used as their pattern arg.
        "REGEXP_LIKE",
        "REGEXP_FULL_MATCH",
        "REGEXP_EXTRACT",
        "REGEXP_REPLACE",
        # conditionals -- §13.1 explicitly discusses CASE-shaped policies
        # with a missing branch as the permissive-bug shape the preview
        # matrix exists to catch, so CASE must be authorable in the first place.
        "CASE",
        "IF",
        "COALESCE",
        "NULLIF",
        # everyday value functions safe in a row/column policy.
        "CAST",
        "LOWER",
        "UPPER",
        "TRIM",
        "LENGTH",
        "CONCAT",
        "SUBSTRING",
    }
)

# The only three identity values a policy may bind (§6.2).
_KNOWN_VARIABLES = frozenset({"user_email", "user_id", "user_groups"})

# Node types whose ``expression`` argument is a pattern that a LIKE-family
# operator or regex function matches against (§6.3). ``this`` on all of
# these is the subject being matched, never the pattern.
_PATTERN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Like,
    exp.ILike,
    exp.SimilarTo,
    exp.RegexpLike,
    exp.RegexpFullMatch,
    exp.RegexpExtract,
    exp.RegexpReplace,
)


def validate_policy_sql(
    sql: str,
    *,
    table_id: str,
    table_name: str,
    mapping_table_names: set[str],
    for_remote: bool,
) -> None:
    """Validate an admin-authored access-policy SQL body (design doc §14).

    Raises :class:`PolicyValidationError` on the first rule violation found;
    returns ``None`` if ``sql`` is a valid policy for ``table_name`` (plus
    any ``mapping_table_names``, §15). Never executes ``sql`` -- this is
    static analysis over the parsed tree only.
    """
    statement = _parse_as_select(sql)
    _reject_sql_string_table_functions(sql)
    _reject_disallowed_constructs(statement)
    _reject_bad_table_references(statement, table_name=table_name, mapping_table_names=mapping_table_names)
    _reject_bad_variables(statement)
    if for_remote:
        _reject_untranspilable(sql)
        _warn_group_membership_idiom(statement, table_id=table_id)


def _parse_as_select(sql: str) -> exp.Select:
    """Rules 1 + 2: exactly one statement, no DDL/DML/ATTACH-family node
    anywhere in it, and the statement itself is a SELECT.

    The forbidden-statement-type walk runs before the "is it a SELECT" check
    so a DROP/INSERT/ATTACH/... gets the more specific, more actionable
    ``policy_forbidden_statement`` reason instead of the generic
    "not a SELECT" -- an agent retrying a rejected write benefits from
    knowing *which* rule it hit (§16).
    """
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as exc:
        raise PolicyValidationError("policy_not_single_select", f"could not parse as SQL: {exc}") from exc
    statements = [s for s in (statements or []) if s is not None]
    if len(statements) != 1:
        raise PolicyValidationError(
            "policy_not_single_select",
            f"a policy must be exactly one SELECT statement; found {len(statements)}",
        )
    statement = statements[0]
    for node in statement.walk():
        if isinstance(node, _FORBIDDEN_STATEMENT_TYPES):
            raise PolicyValidationError(
                "policy_forbidden_statement",
                f"{type(node).__name__} is not allowed in an access policy (a policy is a single read-only SELECT)",
            )
    if not isinstance(statement, exp.Select):
        raise PolicyValidationError(
            "policy_not_single_select",
            f"a policy must be a single SELECT statement, got {type(statement).__name__}",
        )
    return statement


def _reject_sql_string_table_functions(sql: str) -> None:
    """Rule 3, part 1: reject SQL-as-string table functions.

    Reuses the #1264 guard (``query``/``query_table``/``bigquery_query``/...
    rejected globally on ``/api/query``) so a policy body cannot depend on
    that denylist staying complete independently of this one (§5.2 rule 5).
    """
    from app.api.query import _has_sql_string_table_function

    if _has_sql_string_table_function(sql):
        raise PolicyValidationError(
            "policy_sql_string_function",
            "table functions that take SQL as a string (query, query_table, "
            "bigquery_query, ...) are not allowed in an access policy",
        )


def _reject_disallowed_constructs(statement: exp.Select) -> None:
    """Rule 3, part 2: every node is either a recognized function call (by
    NAME, ``_ALLOWED_FUNCTION_NAMES``) or one of the structural node types in
    ``_PERMITTED_NODE_TYPES``. Allowlist, not denylist (§14 rule 2) -- an
    unrecognized construct is rejected even when it is not individually
    known to be dangerous.
    """
    from app.api.query import _anonymous_name

    for node in statement.walk():
        if isinstance(node, exp.Func):
            if isinstance(node, exp.Anonymous):
                name = (_anonymous_name(node) or "").upper()
            else:
                try:
                    name = (node.sql_name() or "").upper()
                except Exception:
                    name = ""
            if not name or name not in _ALLOWED_FUNCTION_NAMES:
                raise PolicyValidationError(
                    "policy_disallowed_construct",
                    f"function not allowed in an access policy: {name or type(node).__name__}",
                )
            continue
        if isinstance(node, (exp.ColumnDef, exp.DataTypeParam)):
            # A STRUCT type spells its fields as ColumnDef nodes:
            # ``CAST(NULL AS STRUCT(v VARCHAR, i BIGINT))`` parses to
            # Cast → DataType → ColumnDef; a parameterized type does the same
            # with DataTypeParam (DECIMAL(18,2), TIMESTAMP(3)). Both are type
            # declarations, not DDL, and the compiler emits exactly these shapes
            # for a hidden or nullified column of such a type — so refusing them
            # made the builder hand an admin SQL the save then rejected, with no
            # way out for that column. Permitted ONLY under a DataType: either
            # node anywhere else is still an unrecognized construct.
            if _is_inside_data_type(node):
                continue
            raise PolicyValidationError(
                "policy_disallowed_construct",
                f"construct not allowed in an access policy: {type(node).__name__} "
                "outside a type declaration",
            )
        if not isinstance(node, _PERMITTED_NODE_TYPES):
            raise PolicyValidationError(
                "policy_disallowed_construct",
                f"construct not allowed in an access policy: {type(node).__name__}",
            )


def _is_inside_data_type(node: exp.Expression) -> bool:
    """Whether ``node`` sits anywhere under an ``exp.DataType``."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.DataType):
            return True
        parent = parent.parent
    return False


def _reject_bad_table_references(statement: exp.Select, *, table_name: str, mapping_table_names: set[str]) -> None:
    """Rule 4: every table reference is the policy's own table or an
    explicitly marked mapping table (§15).

    CTE aliases defined by the policy's own ``WITH`` clause are local names,
    not table references, and are excluded -- collected tree-wide rather than
    only from the outer statement's ``WITH``, which is a deliberate v1
    simplification for admin-authored SQL (see the module docstring on the
    allowlist-not-denylist stance): the physical tables actually touched are
    still checked wherever they appear, CTE-nested or not.
    """
    cte_names = {c.alias_or_name.lower() for c in statement.find_all(exp.CTE) if c.alias_or_name}
    allowed = {table_name.lower()} | {n.lower() for n in mapping_table_names}
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # A $variable in table position (`FROM $t`) has no static name
            # to check here -- rule 5's identifier-position check rejects it.
            continue
        name = table.name
        if name.lower() in cte_names:
            continue
        if name.lower() not in allowed:
            raise PolicyValidationError(
                "policy_unlisted_table_reference",
                f"policy references table {name!r}, which is neither this "
                f"table ({table_name!r}) nor a table marked policy_mapping=true",
            )


def _reject_bad_variables(statement: exp.Select) -> None:
    """Rule 5: every ``$variable`` is a known identity variable, stands only
    in value position, and is never the pattern side of LIKE/ILIKE/SIMILAR TO
    or a regex function (§6.1, §6.3).
    """
    for placeholder in statement.find_all(exp.Placeholder):
        name = placeholder.name
        if _is_identifier_position(placeholder):
            raise PolicyValidationError(
                "policy_var_in_identifier_position",
                f"${name} cannot stand in an identifier position (table, alias, "
                "or excluded/replaced column name) -- a variable may only stand "
                "where a value stands (§6.2)",
            )
        if name not in _KNOWN_VARIABLES:
            raise PolicyValidationError(
                "policy_unknown_variable",
                f"${name} is not a recognized policy variable (known: {', '.join(sorted(_KNOWN_VARIABLES))})",
            )
        if _is_pattern_position(placeholder):
            raise PolicyValidationError(
                "policy_var_in_pattern_position",
                f"${name} cannot be used as a LIKE/ILIKE/SIMILAR TO or regex "
                "pattern -- group and user names are not validated against "
                "pattern metacharacters (§6.3)",
            )


def _is_identifier_position(node: exp.Placeholder) -> bool:
    """True if ``node`` sits where SQL expects a NAME rather than a VALUE:
    a table name, a table alias, a projection alias (which also covers
    ``REPLACE (expr AS $col)``'s existing-column-name slot), or a bare
    ``EXCLUDE ($col)`` entry. Every one of these positions holds a raw
    ``Placeholder`` directly -- DuckDB's grammar does not accept an
    arbitrary expression there, only a simple name -- so a direct
    parent/arg-key check is sufficient; it never needs to look deeper.
    """
    parent = node.parent
    key = node.arg_key
    if isinstance(parent, exp.Table) and key in ("this", "db", "catalog"):
        return True
    if isinstance(parent, exp.TableAlias) and key == "this":
        return True
    if isinstance(parent, exp.Alias) and key == "alias":
        return True
    if isinstance(parent, exp.Star) and key == "except_":
        return True
    return False


def _is_pattern_position(node: exp.Placeholder) -> bool:
    """True if ``node`` is anywhere inside the pattern (``expression``) side
    of a LIKE-family or regex node -- walking up the ancestor chain instead
    of checking only the direct parent, so a wrapped form such as
    ``owner LIKE CONCAT('%', $user_email)`` is caught too, not just the bare
    ``owner LIKE $user_email`` form.
    """
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, _PATTERN_NODES) and parent.args.get("expression") is child:
            return True
        child, parent = parent, parent.parent
    return False


def _reject_untranspilable(sql: str) -> None:
    """Rule 6, part 1: a remote-table policy must transpile to every remote
    engine's SQL without error (§7.2) -- the admin authors DuckDB SQL once,
    and sqlglot produces the form actually run against the source.

    Checked for EVERY remote engine at save time, not just the one this
    particular table happens to sit on. A policy that transpiles to BigQuery
    but not to Databricks (or Snowflake, S2 -- RLS review issue #1979) would
    save clean and then fail at read time on that engine's table -- and a
    policy read that fails, correctly, denies (§17), so the admin would have
    shipped an outage instead of an access rule. The save-time check is the
    only moment where the feedback is cheap.
    """
    for engine in ("bigquery", "databricks", "snowflake"):
        try:
            sqlglot.transpile(sql, read="duckdb", write=engine)
        except Exception as exc:
            raise PolicyValidationError("policy_untranspilable", f"could not transpile to {engine} SQL: {exc}") from exc


def _warn_group_membership_idiom(statement: exp.Select, *, table_id: str) -> None:
    """Rule 6, part 2: warn, don't reject, when ``$user_groups`` membership
    uses the ``IN (SELECT unnest($user_groups))`` idiom instead of
    ``list_contains`` (§6.5) -- both execute correctly; the unnest form just
    transpiles into a much longer GENERATE_ARRAY/CROSS JOIN construct on
    BigQuery.
    """
    for explode in statement.find_all(exp.Explode):
        for placeholder in explode.find_all(exp.Placeholder):
            if placeholder.name == "user_groups":
                logger.warning(
                    "access policy on table %s uses `IN (SELECT unnest($user_groups))` for "
                    "group membership; `list_contains($user_groups, <column>)` is equivalent "
                    "and transpiles to much simpler BigQuery SQL",
                    table_id,
                )
                return


# --------------------------------------------------------------------------
# §14.6 -- the live LIMIT 0 execution probe. Everything above this point is
# static analysis; this is the one function in the module that runs SQL.
# --------------------------------------------------------------------------


def probe_policy(sql: str, table_id: str, conn) -> list[dict]:
    """Execute ``sql`` -- a policy body that has already passed
    ``validate_policy_sql`` -- against the real table with ``LIMIT 0`` and a
    throwaway, empty-groups identity (§14.6), and return the effective
    column list it produces: ``[{"name": ..., "type": ...}, ...]``.

    Static analysis alone cannot catch a policy that references a column
    the underlying table has since dropped, or never had --
    ``SELECT nonexistent_col FROM t`` parses cleanly and satisfies every
    rule ``validate_policy_sql`` enforces, but fails the moment it actually
    runs. Running it here, at save time, turns that failure into a
    rejected write instead of the first analyst's request -- the module
    docstring's closing point about what a policy body actually is (SQL
    executed on the server's analytics connection on every analyst request
    from the moment it is saved) is exactly why this cannot wait.

    ``conn`` is borrowed, never opened or closed here: the production
    caller (``app/api/admin.py``'s policy-write handler) passes
    ``get_analytics_db_readonly()``, which already has every registered
    table's master view attached under its registry ``name`` -- the same
    name a valid policy's own ``FROM`` clause must reference, per
    ``validate_policy_sql``'s table-reference rule. A test passes its own
    fixture connection. Either way this function only ever runs a read
    against what it is given.

    The bound identity is a throwaway: this is a SHAPE check against the
    real table, not a real preview (§13.1's persona matrix owns that) --
    every ``$user_email`` / ``$user_id`` / ``$user_groups`` the policy text
    actually references (re-parsed here rather than reusing a caller's
    already-resolved set, since the candidate SQL being probed may not be
    the one already persisted on the row) is bound to a value no real row
    could plausibly match.

    Raises ``PolicyValidationError(reason="policy_probe_failed", ...)`` on
    a failure that is actually about the CANDIDATE POLICY -- the table no
    longer being registered, ``sql`` failing to parse, or the probe
    execution itself raising once the base table is known to exist.
    Unlike ``PolicyError`` elsewhere in this feature, the raw engine
    message IS surfaced in ``.detail`` here, deliberately: this runs on
    SQL an admin is actively editing, at save time, never on a live
    analyst request, so the engine's own "column X does not exist" is
    exactly the actionable detail the admin needs to fix the write -- not
    the leak §16 guards against for the ALREADY-SAVED body every other
    execution path runs.

    A table that is registered but has never synced -- the ordinary
    "register, then attach a policy, then run the first sync" admin
    workflow (pinned by
    ``tests/test_journey_access_policy_interlock.py``'s own happy path) --
    has no master view on ``conn`` yet, so there is no schema to validate
    a column reference against. That is NOT a probe failure: checked via a
    plain, policy-free ``DESCRIBE`` of the base table BEFORE running the
    candidate SQL at all, so this distinguishes "nothing to check yet"
    from "checked, and the policy is broken" without parsing engine error
    text to tell them apart. Every subsequent save re-probes, so a
    genuinely bad reference is still caught the moment real data (and
    therefore a real schema) lands.

    The "nothing to check yet" early return below also means the
    duplicate-output-column check further down (``_reject_duplicate_output_columns``)
    cannot run on this save either -- there is no resolved column list to
    check for duplicates against. Acceptable: with no columns there is
    nothing for a re-derived column to collide with, and the very next sync
    makes this function re-probe (and therefore re-check) for real.
    """
    from src.repositories import table_registry_repo
    from src.sql_ident import quote_ident

    repo = table_registry_repo()
    row = repo.get(table_id) or repo.get_by_name(table_id)
    if row is None:
        raise PolicyValidationError("policy_probe_failed", f"no such registered table: {table_id!r}")
    table_name = row["name"]

    try:
        conn.execute(f"DESCRIBE {quote_ident(table_name)}").fetchall()
    except Exception:
        return []

    try:
        statement = sqlglot.parse_one(sql, read="duckdb")
        referenced = {p.name for p in statement.find_all(exp.Placeholder) if p.name in _KNOWN_VARIABLES}
    except Exception as exc:
        raise PolicyValidationError("policy_probe_failed", f"could not parse policy SQL: {exc}") from exc

    params: dict[str, object] = {}
    if "user_email" in referenced:
        params["user_email"] = "__agnes_policy_probe__"
    if "user_id" in referenced:
        params["user_id"] = "__agnes_policy_probe__"
    if "user_groups" in referenced:
        params["user_groups"] = []

    # The duplicate-column check runs against a PLAIN `DESCRIBE (sql)` --
    # deliberately NOT the wrapped `probe_sql` computed below. DuckDB's
    # binder silently disambiguates a colliding output name (`email` ->
    # `email_1`) the moment it re-projects through an OUTER `SELECT *`, so
    # describing the wrapped form would never see the collision that is
    # exactly the leak this exists to catch -- confirmed directly: `DESCRIBE
    # SELECT *, md5(email) AS email FROM t` reports two columns literally
    # named `email`, but `DESCRIBE SELECT * FROM (SELECT *, md5(email) AS
    # email FROM t) AS x` reports `email` and `email_1`. A bare parenthesized
    # `DESCRIBE (sql)` never executes the query (DESCRIBE only ever resolves
    # the output schema, regardless of wrapping), so this needs no LIMIT of
    # its own and works whether or not the policy body has one already.
    try:
        raw_described = conn.execute(f"DESCRIBE ({sql})", params).fetchall()
    except Exception as exc:
        raise PolicyValidationError(
            "policy_probe_failed",
            f"policy failed to execute against {table_name!r}: {exc}",
        ) from exc

    _reject_duplicate_output_columns([r[0] for r in raw_described])

    # Wrapped in an outer SELECT rather than appending `LIMIT 0` to `sql`
    # directly -- a policy body is allowed its own LIMIT/OFFSET (Rule 3's
    # permitted node types), and `... LIMIT 5 LIMIT 0` is a syntax error.
    probe_sql = f"SELECT * FROM ({sql}) AS __agnes_policy_probe__ LIMIT 0"
    try:
        described = conn.execute(f"DESCRIBE {probe_sql}", params).fetchall()
    except Exception as exc:
        raise PolicyValidationError(
            "policy_probe_failed",
            f"policy failed to execute against {table_name!r}: {exc}",
        ) from exc

    return [{"name": r[0], "type": r[1]} for r in described]


def _reject_duplicate_output_columns(column_names: list[str]) -> None:
    """The confirmed leak this guards against: ``SELECT * EXCLUDE (x), md5(y)
    AS y FROM t`` -- the design doc's own canonical example (§1) before it was
    corrected -- re-derives a column under a name the star is STILL emitting,
    and DuckDB happily returns two columns both named ``y``: the star's own
    plaintext copy first, the masked one second. Every serializer downstream
    (``/api/query``'s positional row lists, pandas' ``fetchdf()`` behind
    ``/api/v2/sample`` and ``/api/mcp/query-table``) either keeps the first
    occurrence under the plain name or renames the second to ``y_1``, so
    ``row["y"]`` silently resolves to the UNMASKED value -- the exact one the
    policy exists to hide, with no visible sign anything is wrong.

    This can only run here, against ``DESCRIBE``'s resolved, post-``*``-
    expansion output column list -- ``validate_policy_sql`` is pure static
    analysis over the parsed SQL text and has no way to know what ``*``
    expands to (that requires knowing the underlying table's actual
    columns), so it cannot catch this shape on its own.

    Case-insensitive: DuckDB folds unquoted identifiers, so ``Email`` and
    ``email`` collide exactly the same way two lowercase ``email``s do.
    """
    seen: dict[str, str] = {}
    for name in column_names:
        key = name.lower()
        if key in seen:
            original = seen[key]
            raise PolicyValidationError(
                "policy_duplicate_output_column",
                f"policy produces two columns named {original!r}; exclude the "
                f"original before re-deriving it -- e.g. "
                f"`SELECT * EXCLUDE ({original}), <expr> AS {original}`",
            )
        seen[key] = name
