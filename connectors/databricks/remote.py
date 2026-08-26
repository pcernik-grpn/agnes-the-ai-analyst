"""Interactive `agnes query --remote` execution against a Databricks SQL warehouse.

Phase 1 gave Databricks rows one shape only: ``query_mode='materialized'`` —
the scheduler runs the registered SQL on the warehouse and writes a parquet
that everything downstream reads locally. That is the right default (cheap,
cached, distributable) but it cannot answer an ad-hoc question about a table
nobody materialized, and it cannot run ``MEASURE()`` at all, since a Unity
Catalog metric view only evaluates on Databricks compute.

This module is the other half: the analyst's statement ships to the warehouse
as-is and the rows come back. It mirrors what ``app/api/query.py`` does for
BigQuery — registry gating, RBAC, name rewrite, cost cap — with one structural
difference that shapes the whole file.

Cost control differs from BigQuery, on purpose
----------------------------------------------
BigQuery prices a statement *before* running it (``dry_run``), so Agnes refuses
an over-cap query without spending anything. The Databricks Statement Execution
API has no dry-run primitive. What it has is ``byte_limit``: the warehouse stops
*producing result bytes* past the cap and flags the manifest ``truncated``.

So the guarantee here is narrower and is stated plainly to the analyst: the cap
bounds what comes back, not what the warehouse scanned to produce it. A
truncated result is refused outright (never returned as if it were the answer)
and the analyst is pointed at a filtered materialized table instead. Compute on
the warehouse is bounded by the statement timeout, which is the other half of
the guardrail.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.remote_engines import (
    mask_backticks,
    name_reference_re,
    qualified_path_re,
    rewrite_bare_names,
    strip_one_trailing_semicolon,
)

logger = logging.getLogger(__name__)

#: Pseudo-catalog an analyst may type to address a Databricks table directly:
#: ``dbx."<catalog>.<schema>"."<table>"``. Registry-gated exactly like ``bq.*``.
PATH_PREFIX = "dbx"

#: Unity Catalog names are permissive (spaces, dashes, unicode) but Agnes only
#: rewrites what it can quote unambiguously. Anything outside this alphabet has
#: to be reached through a registered materialized row instead of a bare name.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


class DatabricksRemoteError(Exception):
    """A typed failure the API layer turns into an HTTP response.

    Carries the structured ``detail`` shape the CLI already knows how to read
    (``reason`` + ``hint``), so ``agnes query`` prints a next step rather than a
    stack trace.
    """

    def __init__(self, reason: str, message: str, *, status: int = 400, **extra: Any):
        self.reason = reason
        self.message = message
        self.status = status
        self.extra = extra
        super().__init__(message)

    def detail(self) -> dict:
        return {"reason": self.reason, "message": self.message, **self.extra}


# ---------------------------------------------------------------------------
# Identifier plumbing
# ---------------------------------------------------------------------------


def quote_dbx_path(catalog: str, schema: str, table: str) -> str:
    """Backtick-quote a three-part Unity Catalog path.

    Raises :class:`DatabricksRemoteError` on a segment the safe alphabet does
    not cover: a backtick inside an identifier would let a crafted registry row
    (admin-registered, but still) break out of the quoting and append arbitrary
    SQL to every query that names it. Refusing beats escaping here because the
    registry row is fixable and the blast radius of a mistake is the whole
    workspace.
    """
    for label, segment in (("catalog", catalog), ("schema", schema), ("table", table)):
        if not segment or not _SAFE_SEGMENT_RE.match(segment):
            raise DatabricksRemoteError(
                "databricks_unsafe_identifier",
                f"registered Databricks {label} segment {segment!r} contains characters Agnes will not "
                "inline into SQL; register the table with a materialized source_query instead.",
                status=400,
            )
    return f"`{catalog}`.`{schema}`.`{table}`"


def row_target(row: dict, default_catalog: str) -> Tuple[str, str, str]:
    """``(catalog, schema, table)`` for a registry row.

    ``bucket`` is the schema inside the configured default catalog; a dotted
    ``catalog.schema`` bucket pins its own catalog — the same rule the
    materialized extractor's ``split_bucket`` applies, kept identical so a row
    means one physical table regardless of which path reads it.
    """
    from connectors.databricks.extractor import split_bucket

    catalog, schema = split_bucket(str(row.get("bucket") or ""), default_catalog)
    return catalog, schema, str(row.get("source_table") or "")


# ---------------------------------------------------------------------------
# Registry gate + RBAC
# ---------------------------------------------------------------------------


def _resolved_targets(rows: Sequence[dict], default_catalog: str) -> Dict[Tuple[str, str, str], dict]:
    """``{(catalog, schema, table): row}`` for every registered remote row."""
    out: Dict[Tuple[str, str, str], dict] = {}
    for r in rows:
        if not (r.get("bucket") and r.get("source_table")):
            continue
        catalog, schema, table = row_target(r, default_catalog)
        out[(catalog.lower(), schema.lower(), table.lower())] = r
    return out


def _policied_targets(rows: Sequence[dict], default_catalog: str) -> Dict[Tuple[str, str, str], dict]:
    """``{(catalog, schema, table): row}`` for every row carrying a policy.

    Two deliberate differences from :func:`_resolved_targets`, which is the
    *resolution* table rather than the *policy* one.

    It is built from EVERY Databricks row, not only the ``query_mode='remote'``
    ones, because a policy may sit on a ``server_only`` materialized row over
    the same Unity Catalog table. The §3.2 twin interlock refuses that pair at
    write time, but instances that already carry one keep serving reads — so
    the read gate cannot assume the policy lives on a remote row.

    And it keys on the resolved ``(catalog, schema, table)`` rather than the
    raw ``bucket`` string (the ``sf``/``bq`` twin scan in
    ``_policied_row_over_physical_source`` can compare buckets directly, this
    one cannot): a dotted bucket pins its own catalog, so ``sales`` under
    default catalog ``main`` and ``main.sales`` are one physical table spelled
    two ways.
    """
    out: Dict[Tuple[str, str, str], dict] = {}
    for r in rows:
        if not (r.get("access_policy_sql") and r.get("bucket") and r.get("source_table")):
            continue
        catalog, schema, table = row_target(r, default_catalog)
        out.setdefault((catalog.lower(), schema.lower(), table.lower()), r)
    return out


def _gate_table_references(
    sql: str,
    rows: Sequence[dict],
    *,
    accessible: Optional[set],
    is_admin: bool,
    default_catalog: str,
    policied_by_target: Optional[Dict[Tuple[str, str, str], dict]] = None,
) -> Optional[dict]:
    """Refuse the statement unless EVERY table it names is registered + granted.

    This is the security boundary of the whole remote path, and a regex over
    identifiers cannot hold it. The statement executes on the warehouse under
    Agnes's service PAT, which can typically read the entire workspace — so a
    reference Agnes does not recognise is not a broken query, it is a read
    Agnes never authorised.

    The concrete attack the bare-name/`dbx.*` regex passes let through: a
    fully-qualified path riding along with a legitimate one.

        SELECT * FROM orders JOIN `main`.`hr`.`payroll` USING (id)

    ``orders`` is registered, so the statement routes to Databricks; the
    backticked path is left verbatim by the rewriter (correctly — it is
    already warehouse-native) and the warehouse happily reads ``payroll``
    under the service PAT. Same for a bare two-part ``hr.payroll``, which the
    warehouse resolves against its default catalog. Neither shape is a name
    the registry ever saw, which is exactly why enumerating shapes is the
    wrong defence: the rule has to be "everything is refused unless
    recognised", not "these spellings are refused".

    So the statement is parsed (sqlglot, ``databricks`` dialect) and every
    ``exp.Table`` must resolve to a registered row the caller may read. CTE
    names defined in the same statement are legal references and are skipped.

    A statement sqlglot cannot parse is REFUSED, not waved through: an
    unparseable statement is precisely the one whose references cannot be
    checked. Returns a structured detail dict, or ``None`` when everything
    resolves.
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, dialect="databricks")
    except Exception as e:
        logger.info("databricks remote gate: unparseable statement refused (%s)", e)
        return {
            "reason": "databricks_sql_unparseable",
            "message": (
                "Agnes could not parse this statement, so it cannot verify which Databricks "
                "tables it reads — and it runs under a workspace credential that can see more "
                "than you can. Refused."
            ),
            "hint": "Simplify the statement, or register the tables it needs and reference them by name.",
        }
    if tree is None:
        return {
            "reason": "databricks_sql_unparseable",
            "message": "Agnes could not parse this statement.",
            "hint": "Simplify the statement, or register the tables it needs and reference them by name.",
        }

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE) if c.alias_or_name}
    by_name = {str(r["name"]).lower(): r for r in rows if r.get("name")}
    by_target = _resolved_targets(rows, default_catalog)

    for tbl in tree.find_all(exp.Table):
        name = (tbl.name or "").lower()
        db = (tbl.db or "").lower()
        catalog = (tbl.catalog or "").lower()
        if not name:
            continue

        # A bare reference may be a CTE defined in this same statement.
        if not db and not catalog and name in cte_names:
            continue

        display = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
        row = None
        qualified = True
        target_key: Optional[Tuple[str, str, str]] = None
        if not db and not catalog:
            row = by_name.get(name)
            qualified = False
        elif catalog == PATH_PREFIX:
            # dbx."<bucket>"."<table>" — `bucket` follows the same rule as on a
            # registry row (a dotted bucket pins its own catalog), so resolve it
            # with the same helper rather than a second copy of the convention.
            from connectors.databricks.extractor import split_bucket

            bucket_catalog, bucket_schema = split_bucket(db, default_catalog)
            target_key = (bucket_catalog.lower(), bucket_schema.lower(), name)
            row = by_target.get(target_key)
        else:
            target_key = ((catalog or default_catalog).lower(), db, name)
            row = by_target.get(target_key)

        if row is None:
            return {
                "reason": "databricks_table_not_registered",
                "table": display,
                "message": (
                    f"'{display}' is not a registered Databricks table. Remote statements may only "
                    "reference tables registered in Agnes — the query runs under a workspace "
                    "credential whose reach is wider than yours."
                ),
                "hint": (
                    "Register it with `agnes admin register-table --source-type databricks "
                    "--query-mode remote --bucket <schema> --source-table <table>`, or use the "
                    "registered name from `agnes catalog`."
                ),
            }

        # Grant check. A qualified path honours the admin bypass exactly like
        # BigQuery's direct-path pass; a bare name never needs it, because a
        # full-surface admin arrives with `accessible is None`.
        if qualified and is_admin:
            continue
        if accessible is not None and row.get("id") not in accessible:
            return {
                "reason": "databricks_access_denied",
                "table": display,
                "registered_as": row.get("name"),
                "message": f"You do not have access to the Databricks table '{row.get('name')}'.",
            }

        # A qualified path names the PHYSICAL source, and `rewrite_sql`
        # substitutes policied tables by registry NAME (§5.2) — so for this
        # spelling the rewrite never fires, `policied_table_ids` comes back
        # empty, `_apply_databricks_policies` sits the request out, and the raw
        # statement ships to the warehouse under Agnes's service PAT. Registration
        # and the grant, both proved above, say nothing about a policy, and the
        # row a path resolves to need not even be the policied one when a source
        # is registered twice. Mirrors `sf_path_policied` / `bq_path_policied`
        # (`_policied_row_over_physical_source` in `app/api/query.py`), and sits
        # AFTER the grant check for the same fail-closed ordering.
        #
        # Restricted to a `qualified` reference on two counts. A bare name is
        # already covered — that is the spelling `rewrite_sql` does substitute.
        # And it is what keeps the policy re-gate out of this branch: that pass
        # (`_apply_databricks_policies`, `allowed=None`, `is_admin=True`)
        # re-parses a statement whose policy body has ALREADY been rewritten to
        # backticked native paths, every one of which reads as a qualified
        # reference to the very row whose policy produced it — and every one of
        # them is skipped by `qualified and is_admin` above, before reaching
        # here. Re-gating a substituted statement must not undo its own
        # substitution.
        if qualified and policied_by_target and target_key is not None:
            policied = policied_by_target.get(target_key)
            if policied is not None:
                return {
                    "reason": "dbx_path_policied",
                    "table": display,
                    "registered_as": policied.get("name"),
                    "message": (
                        f"'{display}' carries an access policy, which is enforced under its "
                        f"registered name '{policied.get('name')}'. A direct Databricks path names "
                        "the physical table, so the policy would not apply — refused."
                    ),
                    "hint": (f"Query {str(policied.get('name'))!r} instead of the direct path."),
                }

    return None


def guardrail_inputs(
    sql: str,
    sql_lower: str,
    *,
    allowed: Optional[Sequence[str]],
    is_admin: bool,
    default_catalog: str,
) -> Tuple[List[Tuple[str, str, str, str]], Optional[dict]]:
    """Gate every Databricks reference in ``sql``; return the rewrite table.

    Returns ``(name_lookups, blocked)``:

    - ``name_lookups`` — ``(registered_name, catalog, schema, table)`` per
      referenced remote row, feeding :func:`rewrite_to_native`.
    - ``blocked`` — a structured 403 detail when the statement names a table
      Agnes does not recognise or the caller may not read; ``None`` when
      everything checks out. See :func:`_gate_table_references` for why that
      check is a parse and not a set of regexes.

    ``allowed`` is ``get_accessible_tables(...)`` — registry **ids**, not
    display names (they diverge whenever ``id != name``), so the grant check
    keys on ``row["id"]`` while the SQL match keys on ``row["name"]``.

    ``is_admin`` must already account for restricted principals: an agent or
    co-session principal is never admin even when its owner is. The caller
    resolves that; this function only consumes the answer.
    """
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    all_rows = repo.list_by_source("databricks")
    rows = [r for r in all_rows if (r.get("query_mode") or "") == "remote"]

    accessible = set(allowed) if allowed is not None else None

    # The gate: every table this statement names must be registered and
    # readable by the caller. Runs first — nothing below it is a security
    # decision, and a reference the gate does not recognise never reaches the
    # warehouse.
    blocked = _gate_table_references(
        sql,
        rows,
        accessible=accessible,
        is_admin=is_admin,
        default_catalog=default_catalog,
        # Built from ALL Databricks rows, not the `remote`-filtered `rows`: a
        # policy may live on a `server_only` materialized row over the same
        # physical table (see `_policied_targets`).
        policied_by_target=_policied_targets(all_rows, default_catalog),
    )
    if blocked is not None:
        return [], blocked

    # The rewrite table. Regex over the raw SQL rather than the parse tree,
    # because the substitution itself is positional (`rewrite_bare_names`
    # rewrites text, and sqlglot would reformat the statement and drop the
    # analyst's comments if we round-tripped through it). Safe to be
    # approximate here in a way it would NOT be above: the gate has already
    # established that every reference is registered and granted, so the worst
    # a missed match costs is an unresolved name the warehouse rejects.
    #
    # Every matching row is recorded, not de-duplicated by physical target:
    # two registry rows may alias the same Unity Catalog table under different
    # names, and the rewriter needs an entry for each name.
    sql_lower_masked = mask_backticks(sql_lower)
    name_lookups: List[Tuple[str, str, str, str]] = []
    for r in rows:
        name = r.get("name")
        if not (name and r.get("bucket") and r.get("source_table")):
            continue
        if accessible is not None and r.get("id") not in accessible:
            continue
        if name_reference_re(str(name).lower()).search(sql_lower_masked):
            catalog, schema, table = row_target(r, default_catalog)
            name_lookups.append((str(name), catalog, schema, table))

    return name_lookups, None


def rewrite_to_native(sql: str, name_lookups: Sequence[Tuple[str, str, str, str]], default_catalog: str) -> str:
    """Rewrite analyst SQL into what the warehouse should run.

    Two substitutions, both confined to outside-backtick text so a caller who
    already wrote a fully-qualified path is left alone:

    1. registered bare name → `` `catalog`.`schema`.`table` ``
    2. ``dbx."<catalog>.<schema>"."<table>"`` → the same backticked form

    Everything else — CTEs, window functions, ``MEASURE()`` over a metric view
    — passes through untouched, because Databricks SQL *is* the target dialect
    here. That is the point of the remote path: no transpilation, no DuckDB
    semantics leaking in, so a query an analyst tested in the Databricks UI
    behaves identically through Agnes.

    Textual by design, and correct for an analyst statement *because* the
    policy path excludes the policied name before calling this. A policy BODY
    cannot make that exclusion and must use
    :func:`rewrite_policy_body_to_native` instead — see its docstring.
    """
    name_to_target = {
        name.lower(): quote_dbx_path(catalog, schema, table) for name, catalog, schema, table in name_lookups
    }
    out = rewrite_bare_names(sql, name_to_target)

    def _path_repl(m: "re.Match") -> str:
        bucket_raw = m.group(1).strip('"')
        table_raw = m.group(2).strip('"')
        if "." in bucket_raw:
            catalog, _, schema = bucket_raw.partition(".")
        else:
            catalog, schema = default_catalog, bucket_raw
        return quote_dbx_path(catalog, schema, table_raw)

    return qualified_path_re(PATH_PREFIX).sub(_path_repl, out)


class DatabricksPolicyRewriteError(Exception):
    """A policy body could not be rewritten to native paths.

    Separate from :class:`DatabricksRemoteError` on purpose: that one carries a
    ``reason``/``hint`` the CLI prints to the analyst, and everything that can
    go wrong in a policy body is admin-authored detail the caller must not see
    (§16). The API layer collapses this into the table-scoped policy error.
    """


def rewrite_policy_body_to_native(
    sql: str, name_lookups: Sequence[Tuple[str, str, str, str]], default_catalog: str
) -> str:
    """:func:`rewrite_to_native` for a POLICY BODY — same result for a table
    reference, but confined to table *position*.

    The textual rewriter replaces every occurrence of a registered name outside
    backticks, which is right for an analyst statement (the policied name is
    excluded there, so what is left really is a table reference) and wrong
    here: a policy body must have its own table rewritten, and its own name is
    also what qualifies its columns. ``WHERE orders_raw.country = 'CZ'`` came
    out as ``WHERE `main`.`sales`.`orders_raw`.country = 'CZ'`` — a four-part
    column reference. Spark happens to accept it, but it is the same hazard the
    outer pass guards against by excluding the policied name, surviving only
    because the exclusion cannot apply to the body (the body is precisely where
    that table *must* be rewritten).

    Working over the AST removes the ambiguity rather than trading one
    exclusion for another: ``exp.Table`` nodes are rewritten, ``exp.Column``
    qualifiers and string literals are not, and a CTE the policy defines itself
    keeps its local name even when it collides with a registered table.

    Rewriting only the table leaves ``orders_raw.country`` qualifying
    ``main.sales.orders_raw``, which resolves — Spark matches a qualifier
    against the relation's simple name — and is what a hand-written Databricks
    query looks like.

    Raises :class:`DatabricksPolicyRewriteError` if the body does not parse, so
    an unrewritten body can never ship to the warehouse to resolve against
    whatever the default context holds (§17: every failure denies).
    """
    import sqlglot
    from sqlglot import exp

    name_to_target = {name.lower(): (catalog, schema, table) for name, catalog, schema, table in name_lookups if name}

    try:
        tree = sqlglot.parse_one(sql, dialect="databricks")
    except Exception as exc:  # noqa: BLE001 — any parse failure denies
        raise DatabricksPolicyRewriteError("policy body could not be parsed for path rewriting") from exc
    if tree is None:
        raise DatabricksPolicyRewriteError("policy body parsed to nothing")

    # A CTE the policy defines is a local name, not a table — the same
    # exclusion `_reject_bad_table_references` makes when validating the body
    # at save time.
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE) if c.alias_or_name}

    def _native(catalog: str, schema: str, table: str) -> exp.Table:
        # Called for the safe-alphabet check as much as for the string: it is
        # what refuses a registry segment carrying a backtick, and dropping it
        # here would reintroduce that hole on the policy path alone.
        quote_dbx_path(catalog, schema, table)
        return exp.Table(
            this=exp.to_identifier(table, quoted=True),
            db=exp.to_identifier(schema, quoted=True),
            catalog=exp.to_identifier(catalog, quoted=True),
        )

    for node in list(tree.find_all(exp.Table)):
        if not isinstance(node.this, exp.Identifier):
            continue
        name = node.name
        if not name:
            continue

        if node.catalog and node.catalog.lower() == PATH_PREFIX:
            # `dbx."<catalog>.<schema>"."<table>"` — the explicit path form,
            # handled here rather than by the regex so the body never needs a
            # textual pass at all.
            bucket = node.db or ""
            if "." in bucket:
                catalog, _, schema = bucket.partition(".")
            else:
                catalog, schema = default_catalog, bucket
            replacement = _native(catalog, schema, name)
        elif not node.catalog and not node.db and name.lower() not in cte_names:
            target = name_to_target.get(name.lower())
            if target is None:
                continue
            replacement = _native(*target)
        else:
            # Already a qualified native path, or a CTE — leave it alone, the
            # same way the textual rewriter skips backticked text.
            continue

        if node.args.get("alias"):
            replacement.set("alias", node.args["alias"].copy())
        node.replace(replacement)

    return tree.sql(dialect="databricks")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


#: Databricks SQL flavor cues for `agnes schema`, so an agent writing a
#: predicate for a remote row does not reach for DuckDB or BigQuery syntax.
DIALECT_HINTS = {
    "date_literal": "DATE '2026-01-01'",
    "timestamp_literal": "TIMESTAMP '2026-01-01 00:00:00'",
    "interval_subtract": "DATE_SUB(CURRENT_DATE(), 30)",
    "regex": "col RLIKE 'pattern'",
    "cast": "CAST(x AS BIGINT)",
}


def fetch_schema(
    row: dict,
    *,
    settings: Dict[str, Any],
    timeout_s: float = 60.0,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """Column list for one remote row, read from Unity Catalog.

    Without this, ``agnes schema <table>`` 404s on a remote Databricks row —
    it has no parquet to describe — and the documented agent rails ("run
    ``agnes schema`` before writing any query") send every agent into a dead
    end on exactly the tables where guessing a column name is most expensive.

    One bounded ``information_schema.columns`` query through the INLINE
    disposition; the result is a column list, so it cannot outgrow the inline
    limit in any realistic schema.
    """
    from connectors.databricks.client import DatabricksApiError, DatabricksStatementClient

    catalog, schema, table = row_target(row, str(settings.get("catalog") or ""))
    # Validate before interpolating, then bind as parameters anyway: the values
    # are admin-controlled, not analyst-controlled, but a registry row is still
    # the kind of thing that gets edited by hand at 2am.
    quote_dbx_path(catalog, schema, table)

    if client is None:
        client = DatabricksStatementClient(
            host=settings["host"],
            token=settings["token"],
            warehouse_id=settings["warehouse_id"],
        )
    sql = (
        "SELECT column_name, full_data_type, is_nullable, comment "
        f"FROM `{catalog}`.information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    )
    try:
        _cols, rows = client.execute_rows(sql, timeout_s=timeout_s)
    except DatabricksApiError as exc:
        raise DatabricksRemoteError(
            "databricks_schema_unavailable",
            f"Could not read the Databricks schema for {catalog}.{schema}.{table}: {exc}",
            status=502,
        ) from exc

    return [
        {
            "name": r[0],
            "type": (r[1] or "").upper(),
            "nullable": str(r[2]).upper() in ("YES", "TRUE"),
            "description": r[3] or "",
        }
        for r in rows
    ]


def wrap_with_limit(sql: str, limit: int) -> str:
    """Bound the result set the warehouse is asked to produce.

    ``/api/query`` returns at most ``limit`` rows anyway, so shipping an
    unbounded ``SELECT *`` and truncating locally would pay full egress for
    rows nobody sees. ``limit + 1`` preserves the endpoint's existing
    "truncated" signal: getting N+1 back is how the caller knows more exist.

    The outer wrap is legal Spark SQL for any SELECT, including one that
    already carries its own ``LIMIT`` (the inner limit simply wins when it is
    smaller).

    A single trailing semicolon is a legal top-level statement terminator but
    an illegal one once embedded inside this subquery wrap — Spark SQL parses
    it as ending the statement early. ``/api/query``'s SELECT-only guard
    tolerates exactly one trailing ``;`` (routine SQL formatting), so strip it
    here too or that tolerated query fails at the warehouse instead of running.
    """
    body = strip_one_trailing_semicolon(sql)
    return f"SELECT * FROM (\n{body}\n) AS agnes_remote_q LIMIT {int(limit)}"


#: Databricks manifest ``type_name`` → Arrow type. Only matters for a
#: zero-row result, where there is no batch to carry the schema; a result with
#: rows brings its own Arrow types off the wire and never consults this.
#: Anything unmapped (STRUCT, MAP, ARRAY, VARIANT, INTERVAL, …) falls back to
#: string, which is what an empty column of a shape Agnes cannot model locally
#: is worth.
_ARROW_TYPE_BY_DATABRICKS_NAME: Dict[str, str] = {
    "BOOLEAN": "bool_",
    "BYTE": "int8",
    "SHORT": "int16",
    "INT": "int32",
    "LONG": "int64",
    "FLOAT": "float32",
    "DOUBLE": "float64",
    "DATE": "date32",
    "STRING": "string",
    "CHAR": "string",
    "BINARY": "binary",
}


def arrow_schema_from_manifest(schema_columns: Sequence[Dict[str, Any]]):
    """Build a ``pyarrow.Schema`` from the statement manifest's column list."""
    import pyarrow as pa

    fields = []
    for column in schema_columns or []:
        name = str(column.get("name", ""))
        type_name = str(column.get("type_name") or "").upper()
        if type_name == "DECIMAL":
            # Precision/scale ride alongside; a decimal with unknown scale is
            # worse than a float64 for the empty case, and the manifest does
            # not always carry them.
            arrow_type = pa.float64()
        elif type_name in ("TIMESTAMP", "TIMESTAMP_NTZ"):
            arrow_type = pa.timestamp("us")
        else:
            factory = _ARROW_TYPE_BY_DATABRICKS_NAME.get(type_name, "string")
            arrow_type = getattr(pa, factory)()
        fields.append(pa.field(name, arrow_type))
    return pa.schema(fields)


def _build_client(settings: Dict[str, Any]) -> Any:
    from connectors.databricks.client import DatabricksStatementClient

    return DatabricksStatementClient(
        host=settings["host"],
        token=settings["token"],
        warehouse_id=settings["warehouse_id"],
    )


def _translate_submit_failure(exc: Exception, *, timeout_s: Optional[float]) -> "DatabricksRemoteError":
    """Map a client-level failure onto the typed error the API layer renders.

    Shared by the interactive (``execute_select``) and materialize
    (``execute_scan_to_arrow``) paths so a warehouse timeout or an upstream
    5xx reads the same on both — the two differ in how much they fetch, never
    in what a failure means.

    ``timeout_s`` may be ``None`` (deadline disabled), in which case a timeout
    error cannot have been raised — but the message reads the deadline off the
    exception that actually expired rather than off this argument, so the two
    can never disagree.
    """
    from connectors.databricks.client import DatabricksApiError, DatabricksStatementTimeoutError

    if isinstance(exc, DatabricksStatementTimeoutError):
        expired = getattr(exc, "timeout_s", None)
        if expired is None:
            expired = timeout_s
        return DatabricksRemoteError(
            "remote_statement_timeout",
            f"The Databricks statement did not finish within {float(expired or 0):.0f}s and was cancelled.",
            status=504,
            hint=(
                "Narrow the query, or register it as a materialized table so the "
                "scheduler runs it off the interactive path."
            ),
        )
    if isinstance(exc, DatabricksApiError):
        # Databricks classifies user SQL errors as 400s. Anything else is the
        # workspace's problem, not the analyst's, and must not read like a
        # syntax error in their query.
        status = 400 if (exc.status or 0) < 500 else 502
        return DatabricksRemoteError(
            "databricks_query_failed" if status == 400 else "databricks_upstream_error",
            str(exc),
            status=status,
        )
    raise exc


def _too_large(cap_bytes: int, *, cap_label: str) -> "DatabricksRemoteError":
    return DatabricksRemoteError(
        "remote_scan_too_large",
        (
            f"The Databricks result exceeded the {cap_bytes:,}-byte {cap_label} "
            "and was refused (a truncated result is not an answer)."
        ),
        status=400,
        cap_bytes=cap_bytes,
        hint=(
            "Add a narrower WHERE / fewer columns, or register the query as a "
            "materialized table (`agnes admin register-table --query-mode materialized`) "
            "so it syncs on a schedule instead."
        ),
    )


def execute_scan_to_arrow(
    sql: str,
    *,
    settings: Dict[str, Any],
    cap_bytes: int,
    timeout_s: Optional[float],
    parameters: Optional[List[Dict[str, Any]]] = None,
    client: Any = None,
):
    """Run a Databricks-native SELECT and return the FULL result as a
    ``pyarrow.Table`` — the materialize shape ``/api/v2/scan`` and
    ``agnes snapshot create`` need.

    Deliberately not ``execute_select`` with a large limit. That path wraps the
    statement in ``LIMIT n + 1`` to detect "more rows exist", which is exactly
    wrong here: a snapshot wants every row the predicate selects, and an
    ``n + 1`` probe on a materialize-sized fetch would silently cap it. The
    caller's own byte cap (``api.scan.max_result_bytes``) is the bound instead,
    and a result that hits it is refused rather than shortened — same rule as
    the interactive path, different limit.

    ``parameters`` binds Databricks named-parameter markers (``:name``); the
    access-policy path uses it so a policy's identity values never reach the
    warehouse as spliced SQL text.
    """
    import pyarrow as pa

    from connectors.databricks.client import DatabricksApiError

    if client is None:
        client = _build_client(settings)

    try:
        result = client.execute_to_arrow_batches(
            sql,
            byte_limit=cap_bytes if cap_bytes and cap_bytes > 0 else None,
            timeout_s=timeout_s,
            parameters=parameters,
        )
    except Exception as exc:
        raise _translate_submit_failure(exc, timeout_s=timeout_s) from exc

    if result.truncated:
        raise _too_large(cap_bytes, cap_label="scan result cap")

    batches = []
    try:
        for batch in result.iter_batches():
            batches.append(batch)
    except DatabricksApiError as exc:
        raise DatabricksRemoteError(
            "databricks_result_fetch_failed",
            f"The Databricks statement succeeded but its result could not be fetched: {exc}",
            status=502,
            hint="Retry the query; presigned result links are short-lived.",
        ) from exc

    if batches:
        return pa.Table.from_batches(batches)
    # A zero-row result still has to carry its schema — names AND types. The
    # names alone are not enough: both callers persist this. `/api/v2/scan`
    # serializes it to Arrow IPC and `agnes snapshot create` writes it to a
    # parquet and registers a view over it, so typing everything as string
    # would leave a snapshot whose numeric and date columns are text, and the
    # analyst's next local aggregate over it fails or silently compares
    # lexically. The manifest carries the real types; use them.
    return pa.Table.from_batches([], schema=arrow_schema_from_manifest(result.schema_columns))


def execute_select(
    sql: str,
    *,
    settings: Dict[str, Any],
    limit: int,
    cap_bytes: int,
    timeout_s: float,
    parameters: Optional[List[Dict[str, Any]]] = None,
    client: Any = None,
) -> Tuple[List[str], List[List[Any]], bool, int]:
    """Run a Databricks-native SELECT; return ``(columns, rows, truncated, bytes)``.

    ``truncated`` means "more rows exist than were returned" — the same signal
    ``/api/query`` reports for a local query — and is derived from the ``limit
    + 1`` probe row, which is dropped before returning.

    A ``byte_limit`` truncation is a different thing entirely and never reaches
    the caller as data: it raises ``remote_scan_too_large``. Returning a
    silently shortened result would be the worst possible failure mode for an
    analyst — a plausible number that is simply wrong.

    ``parameters`` binds Databricks named-parameter markers (``:name``); the
    access-policy path uses it so a policy's identity values never reach the
    warehouse as spliced SQL text.

    ``client`` is injectable for tests; production builds one from ``settings``.
    """
    from connectors.databricks.client import DatabricksApiError

    if client is None:
        client = _build_client(settings)

    statement = wrap_with_limit(sql, limit + 1)
    try:
        result = client.execute_to_arrow_batches(
            statement,
            byte_limit=cap_bytes if cap_bytes and cap_bytes > 0 else None,
            timeout_s=timeout_s,
            parameters=parameters,
        )
    except Exception as exc:
        raise _translate_submit_failure(exc, timeout_s=timeout_s) from exc

    if result.truncated:
        raise _too_large(cap_bytes, cap_label="remote-query cap")

    columns: List[str] = [str(c.get("name", "")) for c in result.schema_columns]
    rows: List[List[Any]] = []
    # Fetching happens lazily inside the loop — each chunk's presigned link is
    # resolved and downloaded as it is reached, and those links expire in
    # minutes — so transport failures surface HERE, not from the submit call
    # above. Translating them is not optional: an unwrapped DatabricksApiError
    # escapes as a 500 with a raw vendor message.
    try:
        for batch in result.iter_batches():
            if not columns:
                columns = list(batch.schema.names)
            if batch.num_columns:
                rows.extend(list(r) for r in zip(*[col.to_pylist() for col in batch.columns]))
            if len(rows) > limit:
                # Stop pulling chunks the caller will never see. The remaining
                # presigned links are simply left unfetched.
                break
    except DatabricksApiError as exc:
        raise DatabricksRemoteError(
            "databricks_result_fetch_failed",
            f"The Databricks statement succeeded but its result could not be fetched: {exc}",
            status=502,
            hint="Retry the query; presigned result links are short-lived.",
        ) from exc

    truncated = len(rows) > limit
    return columns, rows[:limit], truncated, int(result.total_byte_count or 0)
