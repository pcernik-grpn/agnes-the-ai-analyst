"""The access-policy resolver — the single junction every enforcement point
binds against (table access policies design doc §5, §6, §12).

``policied_relation(table_id, principal, *, dialect="duckdb")`` turns a
registered table and the calling principal into a :class:`PoliciedRelation`:
an *unexecuted*, parenthesizable ``SELECT`` a caller can read from, plus the
bind parameters for it. This module never runs that SQL — each enforcement
surface (Task 6's AST rewrite for SQL surfaces, Task 8's ``FROM``-builder for
``table_id`` surfaces, Task 10's BigQuery jobs-API path) does, binding
``params`` through the engine's own named-parameter mechanism, never string
interpolation (§6.2).

Two outcomes:

- **Passthrough** (``policied=False``) — the table has no
  ``access_policy_sql``, or it does but the caller is an unrestricted admin
  (§12: admin bypass follows the credential *surface*, not merely group
  membership). ``relation_sql`` is a bare ``SELECT * FROM <base view>``.
- **Policied** (``policied=True``) — a policy is attached and the caller has
  a resolvable identity. On ``dialect="duckdb"`` (the default) ``relation_sql``
  is the policy body *verbatim* — its ``$name`` placeholders left as bind
  markers, never rewritten, because DuckDB binds named parameters natively
  (§6.2). On ``dialect="bigquery"`` it is the SAME body transpiled to
  BigQuery Standard SQL (§7.2) — ``$name`` survives the transpile as
  BigQuery's own ``@name`` named-parameter syntax, so the binding guarantee
  holds on both engines from one authored policy. Either way ``params``
  carries only the ``user_email`` / ``user_id`` / ``user_groups`` keys the
  policy text actually references — identical Python values on both
  dialects; converting them to a BigQuery ``QueryParameter`` is the
  enforcement site's job, not this resolver's.

Identity resolution (§12): a plain user dict binds itself; an
``AgentPrincipal`` binds its *caller*'s identity — the user actually
running the (possibly shared) agent — falling back to the owner only when
no distinct caller is set (the owner running their own agent). A shared
agent runs *as* its caller, so row policies filter by the caller's
``$user_email``/``$user_id``/``$user_groups``, never the owner's; an
agent's declared scope narrows which tables it reaches, never who it
reaches them as. A ``SessionPrincipal`` (co-drive, several live
participants, no single identity) has nothing to bind and is refused
outright rather than guessed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from src.sql_ident import quote_ident

# The three identity values a policy body may bind (§6.2). Duplicated here
# rather than imported from ``src.access_policy_validate`` because the two
# modules ask different questions: the validator asks "is every ``$name`` in
# this SQL text one of these, in a safe position" (save time, on arbitrary
# untrusted-until-proven text); this module asks "which of these does this
# ALREADY-VALIDATED policy actually reference, so only those get looked up
# and bound" (every request, on text that passed the validator once).
_KNOWN_VARIABLES = frozenset({"user_email", "user_id", "user_groups"})

# SQL LIKE/ILIKE/SIMILAR-TO wildcard metacharacters. Group names are not
# validated against any character class elsewhere in the system (§6.3) — a
# Workspace-synced or admin-created group literally named ``%`` would
# silently widen every ``list_contains($user_groups, ...)`` policy for every
# member of it. The save-time validator (Task 3) already refuses to let an
# identity variable stand in LIKE/ILIKE/SIMILAR TO pattern position for one
# specific policy body; this is a second, independent check on the VALUE
# about to be bound — defense in depth that does not depend on knowing every
# way a policy (present or future) might turn out to depend on group-name
# shape.
_PATTERN_METACHARACTERS = ("%", "_")


@dataclass(frozen=True)
class PoliciedRelation:
    relation_sql: str  # a parenthesizable SELECT yielding the rows to read.
    # policied=False → "SELECT * FROM <base_view_name>"
    # policied=True  → the policy body verbatim ($vars kept as bind markers)
    params: dict  # bind values for the $vars actually referenced (subset of user_email/user_id/user_groups)
    policied: bool
    table_id: str


class PolicyIdentityUnresolvable(Exception):
    """The principal has no single identity to bind a policy against — a
    co-drive ``SessionPrincipal`` (§12), or any other principal shape this
    resolver does not recognize. Fails closed: an unrecognized principal is
    refused, never silently treated as passthrough or bound to a guess.
    """


class PolicyMappingEmpty(Exception):
    """A ``policy_mapping`` table (§15) a policy joins against has zero
    rows — indistinguishable, from the analyst's side, from "you legitimately
    have no data" unless surfaced explicitly (§15.1). Declared here as the
    shared reason code; raised by the surfaces that actually execute a
    policy's relation (Tasks 7/8), not by this module, which never runs SQL.
    """

    def __init__(self, mapping_table: str, last_sync: Any) -> None:
        self.mapping_table = mapping_table
        self.last_sync = last_sync
        super().__init__(f"access mapping {mapping_table!r} is empty (last_sync={last_sync!r})")


class PolicyError(Exception):
    """A policy failed to resolve or execute for ``table_id`` (§16, §17).

    Deliberately carries no engine-level detail: the whole point of this
    reason code is that a raw DuckDB/BigQuery error for a failing policy can
    quote literal values out of the policy body (§16's closing paragraph),
    so callers render a table-scoped message instead of
    ``str(underlying_exception)``.
    """

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        super().__init__(f"access policy for table {table_id!r} failed to resolve or execute")


def assert_unique_output_columns(column_names, table_id: str) -> None:
    """Fail closed when a POLICIED read produced duplicate output column names.

    A masking policy authored as ``SELECT * EXCLUDE (national_id), md5(email)
    AS email`` yields TWO columns literally named ``email`` — the star's own
    *plaintext* copy first, the masked one second. DuckDB permits this; the
    plaintext then leaks (raw positional column lists in ``/api/query``, or
    ``pandas.to_dict`` silently renaming the *second* dup to ``email_1`` so
    ``row['email']`` returns the plaintext). ``probe_policy`` rejects such a
    policy at save time, but only once the base table has a resolvable schema
    — a policy attached BEFORE the table syncs slips through and leaks once
    data arrives. This read-path guard closes that gap unconditionally.

    Callers MUST invoke this only for a policied read (``policied_table_ids``
    non-empty / ``relation.policied``), on the RAW executed column names,
    BEFORE any pandas conversion (pandas dedups the names, hiding the
    collision). A legitimate non-policied query that returns same-named
    columns (e.g. a self-join) is deliberately untouched.

    Comparison is case-insensitive because DuckDB resolves column names that
    way, so ``email`` and ``Email`` would collide at read time.
    """
    seen: set[str] = set()
    for name in column_names or []:
        key = str(name).lower()
        if key in seen:
            raise PolicyError(table_id)
        seen.add(key)


def assert_policied_reads_unique(conn, policied_table_ids, principal) -> None:
    """Read-path dup-column guard for the AST-rewrite surface (`/api/query`).

    The rewrite substitutes each policied table as ``(<policy body>) AS
    <alias>``; the caller's outer ``SELECT`` then re-projects it, and DuckDB's
    binder silently renames a duplicate output name to ``<name>_1`` — so the
    executed query's own column list never shows the collision even though the
    plaintext value still ships. To catch it we DESCRIBE each policy body
    **unwrapped** (its ``FROM <name>`` resolves against the analytics master
    view) and fail closed on a duplicate. BigQuery-remote policies need no such
    guard: BigQuery itself rejects a query whose result has duplicate column
    names, so a leaky transpiled policy fails at the jobs API (→ PolicyError).
    """
    for table_id in policied_table_ids or []:
        relation = policied_relation(table_id, principal)
        if not relation.policied:
            continue
        try:
            described = conn.execute(f"DESCRIBE ({relation.relation_sql})", relation.params).fetchall()
        except Exception as exc:  # noqa: BLE001 — any resolve/describe failure denies
            raise PolicyError(table_id) from exc
        assert_unique_output_columns([r[0] for r in described], table_id)


def policied_relation(table_id: str, principal, *, dialect: str = "duckdb") -> PoliciedRelation:
    """Resolve ``(table_id, principal)`` to a :class:`PoliciedRelation` (§5).

    ``table_id`` accepts either the registry ``id`` or its ``name`` (§5.3) —
    every caller who only knows one of the two can call this directly; the
    returned ``.table_id`` is always the normalized registry ``id``.

    ``dialect='bigquery'`` (Task 10, §7.2) shares every step below with the
    default ``'duckdb'`` arm — table resolution, no-policy passthrough,
    admin bypass (§12), identity resolution, and which of the three
    variables get bound — and differs ONLY in the final ``relation_sql``:
    the policy body is transpiled to BigQuery Standard SQL via
    ``sqlglot.transpile(..., read="duckdb", write="bigquery")`` instead of
    returned verbatim. ``$name`` placeholders survive the transpile as
    BigQuery's own ``@name`` named-parameter syntax (verified on sqlglot
    30.6.0), so ``params`` carries the SAME identity values on both
    dialects — the enforcement site (Task 10's BQ jobs-API path) converts
    them to ``bigquery.QueryParameter`` objects, never string-interpolates
    them.

    Never executes ``relation_sql`` — that is each enforcement surface's job.
    """
    if dialect not in ("duckdb", "bigquery", "databricks"):
        raise ValueError(f"unknown dialect: {dialect!r}")

    row = _resolve_table_row(table_id)
    resolved_id = row["id"]
    base_view_sql = f"SELECT * FROM {quote_ident(row['name'])}"
    policy_sql = row.get("access_policy_sql")

    if not policy_sql:
        return PoliciedRelation(relation_sql=base_view_sql, params={}, policied=False, table_id=resolved_id)

    if _is_admin_bypass(principal):
        return PoliciedRelation(relation_sql=base_view_sql, params={}, policied=False, table_id=resolved_id)

    user_id, user_email, live_groups = _resolve_identity(principal, table_id=resolved_id)
    referenced = _referenced_variables(policy_sql, table_id=resolved_id)

    params: dict[str, Any] = {}
    if "user_email" in referenced:
        params["user_email"] = user_email
    if "user_id" in referenced:
        params["user_id"] = user_id
    if "user_groups" in referenced:
        groups = live_groups()
        for name in groups:
            if any(ch in name for ch in _PATTERN_METACHARACTERS):
                raise PolicyError(resolved_id)
        params["user_groups"] = groups

    if dialect == "bigquery":
        relation_sql = _transpile_policy_to_bigquery(policy_sql, table_id=resolved_id)
    elif dialect == "databricks":
        relation_sql = _transpile_policy_to_databricks(policy_sql, table_id=resolved_id)
    else:
        relation_sql = policy_sql

    return PoliciedRelation(relation_sql=relation_sql, params=params, policied=True, table_id=resolved_id)


def _transpile_policy_to_bigquery(policy_sql: str, *, table_id: str) -> str:
    """§7.2 — transpile an admin-authored, DuckDB-dialect policy body to
    BigQuery Standard SQL via sqlglot.

    Verified end to end on sqlglot 30.6.0: ``EXCLUDE`` → ``EXCEPT``,
    ``md5(x)`` → ``TO_HEX(MD5(x))``, ``list_contains($g, col)`` →
    ``EXISTS(SELECT 1 FROM UNNEST(@g) AS _col WHERE _col = col)`` — and,
    the part that makes the whole feature work on this engine, every
    ``$name`` placeholder → BigQuery's own ``@name`` named-parameter
    syntax, so the binding guarantee (§6.2) holds on both engines from one
    authored policy. The transpiled body's own ``FROM <name>`` stays a
    bare registry name here, exactly like the DuckDB arm's verbatim
    ``relation_sql`` — resolving it to the table's physical
    ``bq.<dataset>.<table>`` path is the enforcement site's job (§7.3),
    not this resolver's; ``policied_relation`` only ever answers "what
    should a caller read", never "where does that physically live".

    A transpile failure is a ``PolicyError`` — the admin never writes BQ
    SQL directly (§7.2: only the DuckDB-dialect body is authored, and the
    save-time preview shows the transpiled form, §13), so a failure here
    means the policy body uses a construct sqlglot cannot carry across
    dialects. Raising the SAME reason code every other resolution failure
    uses (rather than leaking sqlglot's own exception text) keeps §16's
    contract — no engine detail in a policy failure — true for this new
    failure mode too.
    """
    try:
        statements = sqlglot.transpile(policy_sql, read="duckdb", write="bigquery")
    except Exception as exc:
        raise PolicyError(table_id) from exc
    if not statements:
        raise PolicyError(table_id)
    return statements[0]


def _transpile_policy_to_databricks(policy_sql: str, *, table_id: str) -> str:
    """Transpile an admin-authored, DuckDB-dialect policy body to Databricks
    SQL — the third dialect, and the one where the binding guarantee (§6.2)
    survives for a reason worth stating.

    sqlglot renders every ``$name`` placeholder as ``:name``, which is exactly
    the named-parameter marker the Databricks Statement Execution API binds
    through its ``parameters`` request field. So the same authored policy body
    keeps its values *out of the SQL text* on all three engines: DuckDB binds
    ``$name`` natively, BigQuery gets ``@name``, Databricks gets ``:name``.
    ``list_contains($user_groups, col)`` lands as
    ``ARRAY_CONTAINS(:user_groups, col)``.

    One asymmetry the enforcement site has to finish: the Statement Execution
    API binds *scalar* parameters only, so the array-valued ``$user_groups``
    marker cannot be bound as-is. ``connectors.databricks.policy_params``
    rewrites that one marker into an ``ARRAY(...)`` of scalar markers before
    execution. This resolver stays engine-shaped, not transport-shaped, and
    does not do that here — its contract is still "what should a caller read".

    A transpile failure is a ``PolicyError`` for the same reason as the
    BigQuery arm: the admin never authors Databricks SQL directly, so a
    failure here means the body uses a construct sqlglot cannot carry across
    dialects, and §16 forbids leaking the engine's own message.
    """
    try:
        statements = sqlglot.transpile(policy_sql, read="duckdb", write="databricks")
    except Exception as exc:
        raise PolicyError(table_id) from exc
    if not statements:
        raise PolicyError(table_id)
    return statements[0]


def _resolve_table_row(table_id: str) -> dict:
    """id-or-name lookup (§5.3) — ``id`` checked first (registry PK, exact
    match), then ``name`` (what master views and SQL ``FROM`` clauses name).
    Neither resolving is treated the same as any other resolution failure:
    ``PolicyError`` ("failed to resolve"), never a silent passthrough.
    """
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    row = repo.get(table_id)
    if row is None:
        row = repo.get_by_name(table_id)
    if row is None:
        raise PolicyError(table_id)
    return row


def _is_admin_bypass(principal) -> bool:
    """§12 — admin bypass follows the credential *surface*, not merely group
    membership: a ``surface='stack'`` PAT (the ``agnes init`` default) is
    filtered like any analyst even when its holder is an Admin. Only a plain
    user dict can be admin; a restricted ``Principal`` (agent/session) is
    "never admin" — the same rule ``can_access_table`` applies.
    """
    if not isinstance(principal, dict):
        return False
    user_id = principal.get("id")
    if not user_id:
        return False

    from app.auth.access import is_user_admin
    from src.rbac import _credential_surface

    return is_user_admin(user_id) and _credential_surface(principal) == "all"


def _resolve_identity(principal, *, table_id: str):
    """Resolve ``principal`` to ``(user_id, user_email, live_groups)`` (§12).

    ``live_groups`` is a zero-arg callable rather than an already-fetched
    list so a policy that never references ``$user_groups`` doesn't pay for
    the live group-membership read at all — ``policied_relation`` only
    calls it when the policy text needs it.
    """
    from app.auth.session_principal import AgentPrincipal, SessionPrincipal

    if isinstance(principal, SessionPrincipal):
        raise PolicyIdentityUnresolvable(
            f"table {table_id!r} has a per-user access policy; this session has "
            "multiple participants and no single identity to bind it against -- "
            "open the table in a solo session"
        )
    if isinstance(principal, AgentPrincipal):
        # C2.3, shared-agent runtime: `$user_*` binds to the CALLER — whoever
        # is actually driving this turn — never the agent's owner. For an
        # owner running their own agent the two are the same identity, so
        # this is a no-op change there; the whole point is a shared agent's
        # row policy filtering by the GRANTEE who is asking, not the person
        # who built it. `caller_user_id`/`caller_email` default to None only
        # for constructors that predate C2.3 (older tests) — every real
        # production `AgentPrincipal` (`app.auth.pat_resolver`) always
        # supplies both, so the owner fallback below is never exercised
        # there.
        user_id = principal.caller_user_id or principal.owner_user_id
        user_email = principal.caller_email or principal.owner_email
        return user_id, user_email, lambda: _live_groups(user_id)
    if isinstance(principal, dict):
        user_id, user_email = principal.get("id"), principal.get("email")
        return user_id, user_email, lambda: _live_groups(user_id)

    # Not a shape this resolver recognizes -- fail closed rather than guess.
    raise PolicyIdentityUnresolvable(
        f"table {table_id!r} has a per-user access policy; the caller has no "
        f"identity this resolver recognizes ({type(principal).__name__})"
    )


def _live_groups(user_id: str | None) -> list[str]:
    """§6.4 — read through the SAME live path ``get_accessible_tables`` /
    ``StackResolver`` already use for table-grain authorization, so
    ``$user_groups`` never diverges from what that check just decided.
    """
    if not user_id:
        return []

    from src.repositories import user_group_members_repo

    return user_group_members_repo().list_group_names_for_user(user_id)


def _referenced_variables(policy_sql: str, *, table_id: str) -> set[str]:
    """Which of the three known variables ``policy_sql`` actually
    references, so ``params`` only carries the keys the policy text uses.

    The save-time validator (Task 3) already proved every ``$name`` in a
    saved policy is one of ``_KNOWN_VARIABLES`` in value position —
    re-parsing here (rather than a substring search over the raw text) is
    what stays correct if that ever stops holding for a given row (a
    hand-edited DB value, a future authoring path), and never mistakes a
    variable *name* appearing inside a string literal or comment for a
    reference.
    """
    try:
        statement = sqlglot.parse_one(policy_sql, read="duckdb")
    except Exception as exc:
        raise PolicyError(table_id) from exc
    return {p.name for p in statement.find_all(exp.Placeholder) if p.name in _KNOWN_VARIABLES}


# ---------------------------------------------------------------------------
# Task 6 -- AST substitution for SQL read surfaces (§5.2). The other
# consumer of `policied_relation` (Task 8's `table_id`-shaped FROM builder)
# needs none of this: it never has a caller SQL tree to rewrite.
# ---------------------------------------------------------------------------

# A bare "word" -- the widest a SQL identifier or keyword can be. Used only
# by the last-resort scan over SQL sqlglot could not parse at all (rule 3
# below): every Agnes table name is representable by this pattern, so
# probing each unique token through `resolve` finds a policied reference
# without needing a full registry listing.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PolicyNameCollision(Exception):
    """A caller-introduced name -- a CTE alias or a subquery/derived-table
    alias (§5.2 rule 4) -- is spelled identically to a policied table's name.

    ``WITH invoices AS (SELECT ... FROM invoices ...) SELECT * FROM invoices``
    is the single most common analyst idiom and the default shape an LLM
    writes, so this is never resolved by guessing which occurrence the
    caller meant -- it is refused outright, with a structured reason (§16)
    so the caller (an LLM in practice) renames the CTE and retries instead
    of looping on the same ambiguity.
    """

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        super().__init__(
            f"a CTE or subquery alias in this query is spelled identically to "
            f"the policied table {table_id!r}; rename it and retry"
        )


def _scan_unparseable_for_policied_table(sql: str, principal, resolve) -> str | None:
    """Best-effort answer to "does this SQL -- which failed to parse --
    reference a policied table" (§5.2 rule 3; §19's tripwire example is
    ``SELECT * FROM t SAMPLE 50%``, which DuckDB accepts and sqlglot does
    not parse).

    No AST is available, so there is no candidate-table list other than the
    raw text itself. Every word-shaped token is a candidate; ``PolicyError``
    -- ``_resolve_table_row``'s exact signal for "no such registered table"
    -- is swallowed as "not a match" so a query that merely mentions
    unregistered names keeps failing exactly as it did before this feature
    existed. Any OTHER exception (an identity/mapping problem on a genuine
    match) is a real, table-scoped failure and is not swallowed.
    """
    seen: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(sql):
        word = match.group(0)
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            relation = resolve(word, principal)
        except PolicyError:
            continue
        if relation.policied:
            return relation.table_id
    return None


def rewrite_sql(
    sql: str,
    principal,
    *,
    resolve=policied_relation,
    dialect: str = "duckdb",
) -> tuple[str, dict, list[str]]:
    """Substitute every policied table reference in ``sql`` with its resolved
    relation (§5.2) -- the AST-rewrite half of the resolver's two consumers
    (§5; the other is Task 8's ``table_id``-shaped ``FROM`` builder).

    Returns ``(rewritten_sql, merged_params, policied_table_ids)``:

    - ``rewritten_sql`` is executable DuckDB SQL with every policied
      ``exp.Table`` node replaced by ``(<relation_sql>) AS <alias>`` --
      the alias preserved verbatim if the caller wrote one, else the
      table's own name (rule 2). A query that touches no policied table is
      returned byte-for-byte unchanged, not merely semantically unchanged
      -- enforcement is inert until a policy is attached (plan
      Architecture), and this is the only place that promise is upheld for
      every read that reaches this function, not only the ones a caller
      already suspects are policied.
    - ``merged_params`` unions each substituted relation's bind params --
      safe, because identity values are identical for every table in one
      request.
    - ``policied_table_ids`` lists the registry ids substituted, in the
      order first encountered, for the disclosure envelope (Task 11).

    Applied exactly once, non-recursively (rule 1): only ``exp.Table`` nodes
    already present in the ORIGINAL parse of the caller's SQL are ever
    considered -- a policy body's own ``FROM <table>`` never enters this
    scan, because ``relation.relation_sql`` is spliced into the output as
    literal text (a sentinel placed by the AST, swapped for the real text by
    a plain string replace AFTER generation) rather than being re-parsed and
    walked. Re-parsing it would also risk sqlglot normalizing the text it
    round-trips (e.g. ``list_contains`` -> ``ARRAY_CONTAINS``), silently
    drifting from what the admin wrote and what Task 3 validated -- the
    verbatim property Task 5 documents at length.

    Raises ``PolicyNameCollision`` if a caller CTE or subquery/derived-table
    alias shadows a policied table's name (rule 4), and ``PolicyError`` if
    the SQL references a policied table but does not parse (rule 3) -- the
    same reason code ``policied_relation`` itself uses for "failed to
    resolve", per §16's four-reason table (there is no fifth "unparseable"
    reason). Callers map both to an HTTP 400 naming the table (Task 7).

    ``dialect`` is the flavor the CALLER's statement is parsed and re-rendered
    in; it does not touch the policy body, which is spliced as literal text.
    ``"duckdb"`` (the default) keeps every pre-existing caller -- the local
    SQL surfaces and the BigQuery remote path -- byte-identical. The
    Databricks remote path passes ``"databricks"`` because it must: a
    Databricks statement round-tripped through the DuckDB dialect comes out
    *wrong* rather than merely different (``RLIKE`` is rewritten to
    ``REGEXP_MATCHES``, a DuckDB function the warehouse does not have), and
    ``MEASURE(`Total Revenue`)`` -- the metric-view idiom that is the whole
    point of the remote mode -- does not parse as DuckDB at all, so a policied
    table in such a query would deny instead of filtering.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        # Rule 3: unparseable SQL is rejected outright when it touches a
        # policied table (the fail-closed property a TEMP VIEW would have
        # given for free, §5.1) and left alone otherwise, so a query
        # sqlglot merely lags DuckDB on does not regress for everyone else.
        table_id = _scan_unparseable_for_policied_table(sql, principal, resolve)
        if table_id is not None:
            raise PolicyError(table_id)
        return sql, {}, []

    table_nodes = list(statement.find_all(exp.Table))

    # Rule 4: a caller-chosen CTE or subquery/derived-table alias spelled
    # identically to a policied table's name -- checked on BOTH node shapes
    # because sqlglot places a derived-table alias on ``exp.Subquery``, not
    # ``exp.CTE`` (``SELECT * FROM t, (SELECT 1) invoices`` never produces
    # an ``exp.Table`` named "invoices" at all, so this cannot be folded
    # into the table-node loop below).
    shadow_names = {cte.alias for cte in statement.find_all(exp.CTE) if cte.alias}
    shadow_names |= {sub.alias for sub in statement.find_all(exp.Subquery) if sub.alias}
    shadow_names_lower = {name.lower() for name in shadow_names}

    # Rule 5 / §5.3: match by name, case-insensitively (DuckDB folds
    # unquoted identifiers) -- resolve every DISTINCT name once, whether it
    # appears as a real table reference, a shadowing alias, or both (the
    # ``WITH invoices AS (SELECT ... FROM invoices ...)`` idiom is both at
    # once, and must still raise the collision).
    candidate_names: dict[str, str] = {}
    for table in table_nodes:
        if table.name:
            candidate_names.setdefault(table.name.lower(), table.name)
    for name in shadow_names:
        candidate_names.setdefault(name.lower(), name)

    relations: dict[str, PoliciedRelation] = {}
    for lower_name, original_name in candidate_names.items():
        try:
            relation = resolve(original_name, principal)
        except PolicyError:
            # A name that resolves to no registered table is not this
            # function's concern -- a CTE name, an information_schema
            # view, anything sqlglot modeled as `exp.Table` that the
            # registry has never heard of. Swallowing keeps every OTHER
            # query working; rule 5's allowlist is enforced upstream
            # (#1264, the registry gate), not here.
            continue
        if not relation.policied:
            continue
        if lower_name in shadow_names_lower:
            raise PolicyNameCollision(relation.table_id)
        relations[lower_name] = relation

    if not relations:
        return sql, {}, []

    merged_params: dict[str, Any] = {}
    policied_table_ids: list[str] = []
    seen_ids: set[str] = set()
    sentinel_relation_sql: dict[str, str] = {}

    for table in table_nodes:
        relation = relations.get(table.name.lower()) if table.name else None
        if relation is None:
            continue  # rule 1/2: non-policied tables are left untouched

        # Reuse the original alias node (or, unaliased, the table's own
        # name identifier) so quoting/casing survive -- rule 2.
        alias_node = table.args.get("alias")
        alias_node = alias_node.copy() if alias_node is not None else exp.TableAlias(this=table.this.copy())

        sentinel = f"__agnes_policy_{uuid.uuid4().hex}__"
        sentinel_relation_sql[sentinel] = relation.relation_sql
        table.replace(exp.Subquery(this=exp.Var(this=sentinel), alias=alias_node))

        merged_params.update(relation.params)
        if relation.table_id not in seen_ids:
            seen_ids.add(relation.table_id)
            policied_table_ids.append(relation.table_id)

    rewritten_sql = statement.sql(dialect=dialect)
    for sentinel, relation_sql in sentinel_relation_sql.items():
        rewritten_sql = rewritten_sql.replace(sentinel, relation_sql)

    return rewritten_sql, merged_params, policied_table_ids


# ---------------------------------------------------------------------------
# Task 8 -- shared FROM builder for `table_id`-shaped surfaces (§5).
# `/api/v2/sample`, `/api/v2/scan`'s local branch, and
# `mcp_per_table`'s `_build_select` have no caller SQL tree to substitute a
# policied table into -- unlike the SQL surfaces above (`rewrite_sql`), each
# builds its own `FROM <source>` from scratch: a throwaway `read_parquet(...)`
# in a fresh `:memory:` connection with nothing else attached, or a bare view
# reference on an already-open analytics connection. Neither has the base
# table's registry NAME resolvable as a relation the way the AST rewrite's
# target connection does, so a policy body's own `FROM <name>` would bind to
# nothing (or the wrong thing) without this wrap. These surfaces call
# `policied_relation` directly and hand the result here instead of going
# anywhere near `rewrite_sql`.
# ---------------------------------------------------------------------------


def _leading_with_node(statement: exp.Expression) -> exp.With | None:
    """Return the outer ``WITH`` clause node if ``statement``'s TOP-LEVEL
    query carries one -- either because ``statement`` itself parsed as an
    ``exp.With``, or (the shape sqlglot actually produces for
    ``WITH ... SELECT ...``: the with-clause is an arg of the top
    ``Select``/``Union``/etc. node, not a wrapping node) it is attached
    directly to one of ``statement``'s own args. ``None`` for a plain,
    WITH-less body -- the ordinary case, and the only thing a WITH clause
    nested inside a subquery of ``statement`` (never a direct arg of the
    TOP node) can produce here, which is correct: that inner WITH belongs
    to the subquery's own scope, not the policy body's outer one.
    """
    if isinstance(statement, exp.With):
        return statement
    for value in statement.args.values():
        if isinstance(value, exp.With):
            return value
    return None


def policied_from_sql(relation: PoliciedRelation, *, table_name: str, source_sql: str) -> str:
    """Wrap a policied relation so its own ``FROM <table_name>`` resolves
    against ``source_sql`` -- the calling surface's OWN read of its physical
    source -- instead of the analytics-catalog master view none of these
    surfaces has open.

    Only call this when ``relation.policied``: the passthrough relation
    (``SELECT * FROM <name>``) names a catalog entry these surfaces don't
    have, so callers keep their pre-existing ``source_sql``-only execution
    path for that case completely untouched -- the inert case must stay
    byte-identical to what ran before this feature existed, not merely
    produce an equivalent result through this function.

    ``source_sql`` MUST be a FROM-able fragment carrying no ``?`` placeholder
    of its own -- a policy body binds named ``$user_email`` / ``$user_id`` /
    ``$user_groups`` parameters (§6.2), and DuckDB 1.5.2 refuses to mix
    positional and named parameters in one statement (verified empirically).
    A caller-controlled value that would otherwise be a bind parameter (a
    parquet path) must already be embedded as an escaped string literal;
    every caller here only ever passes a server-resolved path or a
    ``quote_ident``ed registry/view name, never request-controlled text.

    Returns a parenthesized derived-table expression, directly usable as a
    ``FROM {result}`` target or a ``DESCRIBE {result}`` subject (both
    verified against a parenthesized ``WITH ... SELECT ...`` body).

    §15's flagship ``policy_mapping`` join idiom is authored as
    ``WITH allowed AS (...) SELECT ... FROM <table> JOIN allowed ...`` -- a
    policy body that ALREADY opens with its own ``WITH`` clause, a shape
    the save-time validator's allowlist explicitly permits (``exp.With`` /
    ``exp.CTE`` are both in ``_PERMITTED_NODE_TYPES``). Simply
    concatenating ``WITH <table_name> AS (...) `` in front of
    ``relation.relation_sql`` verbatim -- what this function used to do
    unconditionally -- then produces TWO adjacent ``WITH`` keywords, which
    DuckDB's parser rejects outright; every one of this function's callers
    broke on that shape, each differently, because none of them executes
    the string this function hands back until well past any point that
    could have caught a malformed policy at save time.

    When the body opens with its own ``WITH``, the two CTE lists are
    merged into ONE ``WITH`` clause instead: this function's own CTE is
    PREPENDED, a comma joins it to the policy's own CTE list, and
    everything from there on -- the rest of the policy's own CTEs
    (``RECURSIVE``, if the policy used it, kept in its only valid grammar
    position: directly after ``WITH``, applying to the whole merged list),
    and the policy's final ``SELECT`` -- is spliced in as the ORIGINAL
    TEXT, untouched, located via the tokenizer (the lexical stage of the
    same ``read="duckdb"`` parser that already validated this text) rather
    than a hand-rolled string search, so a case difference (``with``),
    unusual whitespace, or a leading comment token can never mis-locate the
    split point. This is deliberately NOT a re-parse-and-regenerate of the
    policy body: sqlglot's own ``duckdb`` generator silently rewrites
    constructs it round-trips (e.g. ``list_contains(...)`` ->
    ``ARRAY_CONTAINS(...)``, verified empirically), which would drift from
    what the admin wrote and what the save-time validator approved -- the
    same verbatim property ``rewrite_sql`` documents at length for its
    own, sentinel-based splice.
    """
    if not relation.policied:
        raise ValueError("policied_from_sql() called on a non-policied relation -- use source_sql directly instead")

    body = relation.relation_sql
    source_cte = f"{quote_ident(table_name)} AS (SELECT * FROM {source_sql})"

    try:
        with_node = _leading_with_node(sqlglot.parse_one(body, read="duckdb"))
    except Exception:
        with_node = None

    if with_node is None:
        # The common case: a plain SELECT. Byte-identical to what this
        # function has always produced.
        return f"(WITH {source_cte} {relation.relation_sql})"

    is_recursive = bool(with_node.args.get("recursive"))
    skip_tokens = 2 if is_recursive else 1
    tokens = sqlglot.Dialect.get_or_raise("duckdb").tokenizer().tokenize(body)
    rest = body[tokens[skip_tokens - 1].end + 1 :]
    recursive_kw = "RECURSIVE " if is_recursive else ""
    return f"(WITH {recursive_kw}{source_cte}, {rest})"


# ---------------------------------------------------------------------------
# Task 9 -- effective schema (§11). `/api/v2/schema` (and, eventually, the
# where-validator) reads the RAW, unfiltered column list today, so a
# policy's `EXCLUDE (col)` is invisible to it -- an analyst sees a column
# that no longer exists on any read surface. `effective_schema` closes that
# gap the same way Task 8's surfaces read rows: resolve, then DESCRIBE the
# wrapped relation via `policied_from_sql`, against the analytics
# connection (none of these callers has a raw parquet path handy the way
# `/api/v2/sample`'s local branch does).
# ---------------------------------------------------------------------------


def effective_schema(table_id: str, principal) -> list[dict] | None:
    """Per-column ``hidden`` markers for a policied table (§11), derived
    from a live ``DESCRIBE`` of the resolved relation rather than the raw,
    unfiltered schema every read surface used before this feature existed.

    Returns ``None`` when the table carries no policy, or when
    ``policied_relation`` resolves ``principal`` to the admin bypass (§12)
    -- either way there is nothing to correct, and the caller
    (``/api/v2/schema``) keeps whatever raw schema it already built. §11
    exists ONLY to stop a policied table's schema surface from advertising
    a column the caller can never actually read; the inert/admin case is
    not this function's concern.

    Runs TWO ``DESCRIBE``s against the analytics connection, both scoped to
    the registry row's own ``.name`` -- what ``policied_relation``'s own
    passthrough calls "the base view" (§5.3), and what a policy body's own
    ``FROM <name>`` resolves against once wrapped by ``policied_from_sql``:
    one over the raw, unfiltered view (the reference column set) and one
    over the policy-wrapped relation (what a caller actually receives). A
    base column absent from the wrapped ``DESCRIBE``'s name set is
    ``hidden`` -- the security-critical marker (§11), and the only one this
    function computes.

    ``masked`` is deliberately NOT attempted. The obvious heuristic --
    comparing type per matching name -- misses the design doc's own
    canonical example (``md5(email) AS email``: VARCHAR in, VARCHAR out,
    same name, no type signal at all), and can't even be applied cleanly
    when a policy body's ``SELECT *`` isn't ALSO excluding the column it
    re-derives: DuckDB accepts that (two columns literally named the same),
    and this function dedupes by keeping the first occurrence rather than
    guessing which one is "the masked one" from a post-hoc DESCRIBE diff.
    Reliable masked-detection needs a static read of the policy body's own
    SELECT-list expressions, not a DESCRIBE diff -- left for a follow-up
    rather than shipping a marker that can be wrong on the exact example
    the design doc leads with.
    """
    from src.db import get_analytics_db_readonly

    relation = policied_relation(table_id, principal)
    if not relation.policied:
        return None

    row = _resolve_table_row(relation.table_id)
    base_ref = quote_ident(row["name"])

    conn = get_analytics_db_readonly()
    try:
        try:
            base_rows = conn.execute(f"DESCRIBE {base_ref}").fetchall()
        except Exception as exc:
            raise PolicyError(relation.table_id) from exc

        wrapped = policied_from_sql(relation, table_name=row["name"], source_sql=base_ref)
        try:
            effective_rows = conn.execute(f"DESCRIBE {wrapped}", relation.params).fetchall()
        except Exception as exc:
            raise PolicyError(relation.table_id) from exc
    finally:
        conn.close()

    # Duplicate output names ARE possible (see the docstring above) --
    # first occurrence wins, deterministically, rather than crashing or
    # silently preferring whichever a dict comprehension iterated last.
    effective_by_name: dict[str, tuple] = {}
    for r in effective_rows:
        effective_by_name.setdefault(r[0], r)

    columns: list[dict] = []
    seen_names: set[str] = set()
    for r in base_rows:
        name = r[0]
        seen_names.add(name)
        hit = effective_by_name.get(name)
        if hit is None:
            columns.append({"name": name, "type": r[1], "nullable": r[2] == "YES", "description": "", "hidden": True})
        else:
            columns.append(
                {"name": hit[0], "type": hit[1], "nullable": hit[2] == "YES", "description": "", "hidden": False}
            )

    # A policy can also ADD a column no base column carries (e.g. pulled in
    # from a `policy_mapping` join, §15) -- keep it, appended after the
    # base-derived list, rather than silently dropping something the
    # policy body deliberately returns.
    for r in effective_rows:
        if r[0] not in seen_names:
            columns.append({"name": r[0], "type": r[1], "nullable": r[2] == "YES", "description": "", "hidden": False})
            seen_names.add(r[0])

    return columns


# ---------------------------------------------------------------------------
# Task 11 -- disclosure (§10). "Silent partial scope is forbidden"
# (command-ux.md) applies with particular force to row filtering: an analyst
# (or an agent, with more confidence) who sums a policied table's own column
# and reports the total has no way to know it was never the whole table.
# Tasks 7/8 already collect `policied_table_ids` at every enforcement site
# (`rewrite_sql`'s third return value; a policied `PoliciedRelation.table_id`
# for the `table_id`-shaped surfaces) -- this is the ONE place that turns
# that list into the `row_scope` envelope every read surface exposes, so the
# sentence an analyst sees on `/api/query`, `/api/v2/sample`, the
# `X-Agnes-Row-Scope` header `/api/v2/scan` sets in place of a JSON body, and
# the CLI's `[scope]` line is authored once and cannot drift across surfaces.
# ---------------------------------------------------------------------------


def row_scope_payload(policied_table_ids: "list[str] | tuple[str, ...] | None") -> dict | None:
    """Build the ``row_scope`` disclosure envelope (§10) for a response that
    read through one or more policied tables.

    Returns ``None`` -- never an empty-list envelope -- when
    ``policied_table_ids`` is empty (or ``None``), so every read surface can
    gate disclosure with a plain ``if row_scope:`` and a JSON caller sees an
    absent/null key rather than a misleading ``{"policied_tables": [], ...}``.

    De-dupes while preserving first-seen order (a query touching the same
    policied table via two aliases still names it once).
    """
    ids = list(dict.fromkeys(policied_table_ids or []))
    if not ids:
        return None
    names = ", ".join(f"'{i}'" for i in ids)
    return {
        "policied_tables": ids,
        "note": f"rows in {names} are filtered by an access policy — this is your slice, not the whole table",
    }


# ---------------------------------------------------------------------------
# Task 12 -- response-cache identity keying (§9). `_sample_cache`
# (app/api/v2_sample.py) and `_schema_cache` (app/api/v2_schema.py) are both
# process-global, keyed on `table_id` (plus `n` for sample) alone -- exactly
# right for a non-policied table, where every caller gets the identical
# response, but wrong the moment a table carries a policy: the response
# becomes CALLER-dependent, so a shared key would serve team A's cached
# slice to team B for up to the cache's TTL. §5.1 names this precise
# shape -- a caller who would have been correctly filtered on a live read
# instead gets someone else's rows because a cache entry beat them to it --
# "worse than no policy". `policy_cache_identity` is the one place both
# endpoints derive the extra key component that closes it.
# ---------------------------------------------------------------------------


def policy_cache_identity(principal, *, table_id: str) -> tuple[str | None, tuple[str, ...]]:
    """``(user_id, sorted-group-tuple)`` -- the identity component a
    policied table's response-cache key must carry (§9), so one caller's
    filtered/masked slice out of ``_sample_cache`` / ``_schema_cache`` is
    never served to another.

    Reuses ``_resolve_identity``'s own principal-shape handling rather than
    inventing a second one: a plain user dict binds itself, an
    ``AgentPrincipal`` binds its CALLER -- the same identity
    ``policied_relation`` would actually execute the policy as for it, so
    an agent's cached slice is correctly shared with (and only with) other
    requests reading as that same caller (never leaked across grantees) --
    and a ``SessionPrincipal`` raises
    ``PolicyIdentityUnresolvable`` here exactly as ``policied_relation``
    would a moment later: refused before a cache lookup ever computes a key
    from an identity the resolver is about to refuse outright, rather than
    silently falling back to some shared or guessed key.

    Always includes the caller's LIVE group list, whether or not the
    table's policy text currently references ``$user_groups`` -- unlike
    ``policied_relation``'s own lazy ``params`` (only the variables ONE
    specific policy body references), a cache key must stay correct across
    an admin editing that policy body to start referencing groups it did
    not before, without an old, narrower-keyed cache entry ever being
    mistaken for a hit against the new policy.
    """
    user_id, _user_email, live_groups = _resolve_identity(principal, table_id=table_id)
    return (user_id, tuple(sorted(live_groups())))


# ---------------------------------------------------------------------------
# Task 18 -- snapshot policy fingerprint (§3.4, §10.3). `agnes snapshot
# create` deliberately puts a policied table's rows on the laptop, bypassing
# every live-read enforcement point above -- the parquet keeps answering
# from whatever slice was current at fetch time even after the policy
# tightens. `policy_fingerprint` is the value `/api/v2/scan` stamps onto the
# `X-Agnes-Policy-Fingerprint` response header and `app/api/sync.py`'s
# manifest recomputes per caller (`_table_manifest_entry`); `agnes pull`
# (`cli/lib/pull.py`) compares the two and withholds a mismatched snapshot's
# view through the SAME `snapshot_views_blocked` mechanism #1129 already
# built for a de-authorized or newly-`server_only` table.
# ---------------------------------------------------------------------------


def policy_fingerprint(table_id: str, principal) -> str | None:
    """``sha256(access_policy_sql + '|' + repr(sorted(caller_group_names)))``
    -- a fingerprint of the policy text a table currently carries plus the
    caller's live group membership (§10.3: "hash of the policy SQL + the
    caller's bound group set").

    ``None`` -- the same "nothing to protect" passthrough
    :func:`policied_relation` itself uses -- when the table carries no
    policy, or ``principal`` is the admin bypass (§12): an admin's read is
    never filtered, so a snapshot taken as admin was never filtered either,
    and there is no slice whose staleness needs tracking.

    Reuses :func:`policy_cache_identity` for the identity half rather than
    inlining a fresh ``_resolve_identity`` + live-groups read: a fingerprint
    must invalidate on ANY group-membership change, not only when the
    CURRENT policy text happens to reference ``$user_groups`` -- the exact
    reasoning ``policy_cache_identity`` already documents for its own
    caller (response-cache keys, §9) applies here unchanged, since an admin
    could edit the policy tomorrow to start referencing groups it does not
    reference today.
    """
    row = _resolve_table_row(table_id)
    policy_sql = row.get("access_policy_sql")
    if not policy_sql:
        return None
    if _is_admin_bypass(principal):
        return None

    _, groups = policy_cache_identity(principal, table_id=row["id"])
    digest_input = f"{policy_sql}|{sorted(groups)!r}"
    return hashlib.sha256(digest_input.encode()).hexdigest()
