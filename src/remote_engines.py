"""Which external engine — if any — must execute a given analyst SQL.

Until Databricks arrived the answer was always BigQuery: ``/api/query`` found
its remote rows by scanning the registry for ``source_type='bigquery'`` and
every guardrail below that point (dry-run cost estimate, backtick rewriter,
jobs-API execution) was written against the BQ jobs API. A second engine whose
rows are *detected* the same way but *execute* somewhere else entirely needs
the detection — and the "exactly one engine per statement" rule — to live in
one place, so a third engine plugs in without another ``if source_type ==``
branch in the query handler.

What lives here
---------------
Detection and arbitration only:

- which registered ``query_mode='remote'`` rows a statement references, per
  engine (``referenced_remote_rows``);
- the rule that one statement may not straddle two engines
  (``resolve_single_engine`` → :class:`CrossEngineError`).

What does *not* live here is execution. Each engine keeps its own cost guard,
SQL rewriter and transport, because those have almost nothing in common:
BigQuery can price a statement before running it (``dry_run``) while Databricks
cannot, and can only cap the bytes a finished statement is allowed to return.
Pretending otherwise behind a shared ``estimate()`` would have to lie about one
of them.

The identifier primitives (``mask_backticks``, ``name_reference_re``) are the
same ones ``app/api/query.py`` uses for its RBAC name scans; they are defined
here and imported there so a single regex governs "does this SQL reference the
table called X" everywhere. They are deliberately conservative: over-matching
costs an unnecessary grant check, under-matching leaks data.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

_BACKTICK_SEGMENT = re.compile(r"`[^`]*`")


def mask_backticks(sql: str) -> str:
    """Replace each `…`-quoted segment with spaces of equal length so
    word-boundary regexes find positions outside backticks but ignore
    everything inside. Preserves all character offsets, so ``re.search`` on the
    masked string reports matches at the same positions as on the original.

    Issue #201: ``\\b`` matches *inside* backtick segments because both ``.``
    and `` ` `` are non-word characters, so a registered bare name like
    ``unit_economics`` would otherwise match inside a user-supplied full path
    ``\\`<project>.<dataset>.unit_economics\\``` and get falsely rewritten.
    """
    return _BACKTICK_SEGMENT.sub(lambda m: " " * len(m.group(0)), sql)


@functools.lru_cache(maxsize=8192)
def name_reference_re(name: str) -> "re.Pattern":
    """Compiled "is this table name referenced" pattern for one name.

    Memoized because this is a per-request, per-registered-name scan: ``re``'s
    own cache holds 512 entries and evicts FIFO, so past that many views every
    pattern recompiles on every request (measured 17.5 ms at 1000 views against
    0.5 ms here). Keys are registry/catalog names, bounded by the instance's
    view count, never free-form user input.

    The one position it skips is a name immediately followed by ``BY``: the
    keyword half of ORDER BY / GROUP BY / PARTITION BY, which no table
    reference can occupy (both DuckDB and BigQuery reject a bare ``by`` as an
    identifier). Without it, a registered table named ``order`` would match
    every sorted query — issue #1322, where that false match then corrupted
    the rewriter's substitution downstream.
    """
    return re.compile(rf"\b{re.escape(name)}\b(?!\s+by\b)")


@functools.lru_cache(maxsize=32)
def qualified_path_re(prefix: str) -> "re.Pattern":
    """``<prefix>."<bucket>"."<table>"`` (and the unquoted variant) matcher.

    Mirrors ``app/api/query.py``'s long-standing ``BQ_PATH`` shape so a second
    engine's direct-path syntax is gated by identical rules. The leading
    ``(?<![\\w.])`` keeps ``foo.bq.x.y`` from matching the ``bq.x.y`` tail.
    """
    p = re.escape(prefix)
    # Assembled by concatenation, not an f-string: a literal `"{p}"` inside one
    # matches the hand-quoted-SQL-identifier shape that
    # `tests/test_security_audit_20260805.py` ratchets against. Here the shape
    # is exactly backwards — this pattern *parses* quoted SQL and emits none —
    # but an excused entry on that allowlist costs every future reader a
    # verification, and two lines of concatenation cost nothing.
    quoted_prefix = '"' + p + '"'
    return re.compile(
        r"(?<![\w.])(?:" + quoted_prefix + "|" + p + r")\s*\.\s*"
        r'("[^"]+"|\w+)\s*\.\s*("[^"]+"|\w+)(?=\W|$)',
        re.IGNORECASE,
    )


# Reserved SQL keywords a registered table may legally be named after — the
# register-table id rule is `[a-z_][a-z0-9_]*`, which admits every word below.
# Union of BigQuery's reserved-keyword list, Databricks/Spark SQL's, and the
# DuckDB-only words that can appear bare in a SELECT (ASOF/ANTI/SEMI/POSITIONAL
# joins, PIVOT/UNPIVOT, QUALIFY, SAMPLE, …). Membership only ever NARROWS where
# a name is substituted by an engine's rewriter, so over-inclusion is the safe
# direction; a missing word merely leaves that name on the substitute-
# everywhere path.
SQL_RESERVED_NAMES = frozenset(
    """
    all and anti any array as asc asof at between by case cast cluster collate
    columns contains create cross cube current default define desc distinct
    distribute else end enum escape except exclude exists extract false fetch
    following for from full glob group grouping groups hash having if ignore
    ilike in inner intersect interval into is join lateral left like limit
    lookup map merge natural new no not null nulls of offset on or order outer
    over pivot positional preceding proto qualify range recursive replace
    respect right rollup rows sample select semi set similar some sort struct
    table tablesample then to treat true unbounded union unnest unpivot using
    values when where window with within
    """.split()
)

# A bare table name can only follow one of these tokens in a SELECT-only
# statement: `FROM x`, `… JOIN x`, or `FROM a, x` (old-style comma join).
# Anchored with `\Z` and applied to the text PRECEDING a candidate match, so it
# answers "is this occurrence in a table-reference position?".
#
# Whitespace and SQL comments may sit between the token and the name. Written
# with single-character `\s` (not `\s+`) inside the outer `*` so the alternation
# has no nested quantifier to backtrack over — the SQL here is analyst-supplied,
# and the security playbook requires linear-time regexes.
TABLE_REF_PREFIX_RE = re.compile(
    r"(?:\bfrom\b|\bjoin\b|,)(?:\s|/\*(?:[^*]|\*(?!/))*\*/|--[^\n]*\n)*\Z",
    re.IGNORECASE,
)


def rewrite_bare_names(sql: str, name_to_target: Dict[str, str]) -> str:
    """Substitute registered bare table names with engine-native paths.

    Shared by every engine's rewriter because the hazards are identical and
    each was a real bug once:

    - **Single pass.** One alternation regex sorted longest-first, not one
      ``re.sub`` per name. Iterating let pass N match *inside* the replacement
      text pass N-1 had just inserted (repro: project ``my-ue-project``,
      registered names ``orders`` + ``ue`` — rewriting ``orders`` inserts a
      path containing ``my-ue-project``, whose ``ue`` the next iteration then
      matched).
    - **Outside backticks only.** ``re.split`` on `` `…` `` keeps the rewrite
      out of user-supplied fully-qualified paths, whose final segment often
      equals a registered bare name (issue #201).
    - **Keyword-named tables anchor to a table-reference position.** A table
      named ``order`` otherwise matches the keyword half of ``ORDER BY`` and
      re.sub corrupts the statement (issue #1322). The check runs inside the
      replacement callback so the pass stays single.

    ``name_to_target`` maps lower-cased registered name → replacement text
    (already quoted in the engine's own flavor).
    """
    if not name_to_target:
        return sql

    sorted_names = sorted(name_to_target.keys(), key=len, reverse=True)
    pattern = r"\b(" + "|".join(re.escape(n) for n in sorted_names) + r")\b(?!\s+by\b)"

    def _repl(m: "re.Match") -> str:
        matched = m.group(1)
        if matched.lower() in SQL_RESERVED_NAMES and not TABLE_REF_PREFIX_RE.search(m.string[: m.start(1)]):
            return matched
        return name_to_target[matched.lower()]

    # `re.split` with a captured group returns [outside, backtick, outside, …].
    parts = re.split(r"(`[^`]*`)", sql)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = re.sub(pattern, _repl, part, flags=re.IGNORECASE)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteEngineSpec:
    """Static description of one remote-execution engine.

    ``source_type`` matches ``table_registry.source_type``; ``path_prefix`` is
    the pseudo-catalog an analyst may type to address the engine directly
    (``bq."dataset"."table"``, ``dbx."catalog.schema"."table"``). ``cli_hint``
    is appended to "you can't do that here" errors so the analyst learns the
    supported shape instead of guessing.
    """

    source_type: str
    label: str
    path_prefix: str
    cli_hint: str


_ENGINES: Dict[str, RemoteEngineSpec] = {}


def register_engine(spec: RemoteEngineSpec) -> None:
    """Register (or replace) an engine spec. Idempotent — import-time safe."""
    _ENGINES[spec.source_type] = spec


def get_engine(source_type: str) -> RemoteEngineSpec | None:
    return _ENGINES.get(source_type)


def registered_engines() -> Tuple[RemoteEngineSpec, ...]:
    return tuple(_ENGINES.values())


register_engine(
    RemoteEngineSpec(
        source_type="bigquery",
        label="BigQuery",
        path_prefix="bq",
        cli_hint='agnes query --remote "SELECT … FROM <registered_name>"',
    )
)
register_engine(
    RemoteEngineSpec(
        source_type="databricks",
        label="Databricks",
        path_prefix="dbx",
        cli_hint='agnes query --remote "SELECT … FROM <registered_name>"',
    )
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


#: Stand-in for "this engine was referenced by a path no registry row matches".
#: Deliberately empty: detection only needs the engine's identity, and letting
#: this dict grow a path field would invite a caller to read it instead of the
#: engine gate's own, precisely-named answer.
_UNREGISTERED_PATH_MARKER: Dict[str, str] = {}


def _rows_referenced(
    spec: RemoteEngineSpec,
    sql: str,
    sql_lower_masked: str,
    remote_rows: List[dict],
) -> List[dict]:
    """Registered ``query_mode='remote'`` rows of one engine this SQL touches.

    Matches on the display ``name`` (what ``agnes catalog`` shows and what an
    analyst types) outside backtick segments, or on a direct
    ``<prefix>."x"."y"`` path whose bucket + source_table hit a registered row.
    Access is NOT decided here — the caller's guardrail owns that, because a
    reference the caller may not read must still be *detected* so it can be
    refused rather than silently skipped.
    """
    hits: List[dict] = []
    seen: set = set()

    for r in remote_rows:
        name = r.get("name")
        if not name:
            continue
        if name_reference_re(str(name).lower()).search(sql_lower_masked):
            key = r.get("id") or name
            if key not in seen:
                seen.add(key)
                hits.append(r)

    for m in qualified_path_re(spec.path_prefix).finditer(sql):
        bucket = m.group(1).strip('"').lower()
        table = m.group(2).strip('"').lower()
        for r in remote_rows:
            if (r.get("bucket") or "").lower() == bucket and (r.get("source_table") or "").lower() == table:
                key = r.get("id") or r.get("name")
                if key not in seen:
                    seen.add(key)
                    hits.append(r)
                break
        else:
            # An unregistered direct path. Record the *engine* as referenced
            # so the caller's registry gate gets the chance to answer
            # "<path> is not registered" instead of the statement falling
            # through to local execution and dying on a confusing "table does
            # not exist".
            #
            # The marker carries no path: this list is only ever asked "is this
            # engine referenced", and the gate re-derives the offending path
            # from the statement itself, where it can name it precisely.
            hits.append(_UNREGISTERED_PATH_MARKER)

    return hits


def referenced_remote_rows(sql: str, sql_lower: str) -> Dict[str, List[dict]]:
    """``{source_type: [registry rows]}`` for every engine this SQL references.

    Engines with no reference are absent from the result, so an all-local
    statement yields ``{}`` and callers skip remote handling entirely.

    One registry read for all engines, not one per engine: this runs on every
    ``/api/query`` request including the all-local ones, which are the majority
    on any instance, and the BigQuery guardrail downstream already reads the
    registry twice more.
    """
    from src.repositories import table_registry_repo

    try:
        all_rows = table_registry_repo().list_all()
    except Exception:  # pragma: no cover - registry outage surfaces upstream
        logger.warning("remote-engine detection: registry read failed", exc_info=True)
        return {}

    by_source: Dict[str, List[dict]] = {}
    for r in all_rows:
        if (r.get("query_mode") or "") != "remote":
            continue
        st = r.get("source_type") or ""
        if st in _ENGINES:
            by_source.setdefault(st, []).append(r)
    if not by_source:
        return {}

    masked = mask_backticks(sql_lower)
    out: Dict[str, List[dict]] = {}
    for spec in registered_engines():
        rows = by_source.get(spec.source_type)
        if not rows:
            continue
        hits = _rows_referenced(spec, sql, masked, rows)
        if hits:
            out[spec.source_type] = hits
    return out


def references_non_engine_tables(sql_lower: str, engine_source_type: str) -> List[str]:
    """Registered table names this SQL touches that the given engine cannot see.

    "Cannot see" means every registered row except that engine's own
    ``query_mode='remote'`` rows: a local parquet, a materialized parquet (even
    one materialized *from* this engine — the parquet lives here now), or
    another engine's remote row. Shipping such a statement whole to the engine
    would resolve the local half against nothing.

    Returns the offending names so the caller can name them in the error rather
    than making the analyst bisect their own query. Mirrors
    ``_bq_remote_execution_plan``'s Skip 3, including its keyword-collision
    suppression (issue #1322): a *local* table named ``order`` must not make
    every remote query containing an innocent ``ORDER BY`` look cross-source.
    """
    from src.repositories import table_registry_repo

    try:
        rows = table_registry_repo().list_all()
    except Exception:  # pragma: no cover - registry outage surfaces upstream
        return []

    masked = mask_backticks(sql_lower)
    hits: List[str] = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        if (r.get("source_type") or "") == engine_source_type and (r.get("query_mode") or "") == "remote":
            continue
        if name_reference_re(str(name).lower()).search(masked):
            hits.append(str(name))
    return hits


class CrossEngineError(Exception):
    """One statement referenced remote tables on two different engines.

    There is no join layer between BigQuery and Databricks: each engine can
    only be handed SQL it can resolve end to end. Refusing is the honest
    answer — the alternative is shipping half the query to one engine and
    silently dropping the other half's rows.
    """

    def __init__(self, engines: List[str]):
        self.engines = sorted(engines)
        super().__init__(f"query references remote tables on multiple engines: {', '.join(self.engines)}")

    def detail(self) -> dict:
        labels = [(get_engine(e).label if get_engine(e) else e) for e in self.engines]
        return {
            "reason": "remote_cross_engine_unsupported",
            "engines": self.engines,
            "message": (
                f"This query references remote tables on {' and '.join(labels)}. "
                "A single remote statement runs on exactly one engine."
            ),
            "hint": (
                "Materialize one side first — `agnes snapshot create <table> "
                "--where …` — then join it locally with `agnes query`."
            ),
        }


def resolve_single_engine(refs: Dict[str, List[dict]]) -> str | None:
    """The one engine this statement runs on, or ``None`` when it is all local.

    Raises :class:`CrossEngineError` when more than one engine is referenced.
    """
    if not refs:
        return None
    if len(refs) > 1:
        raise CrossEngineError(list(refs))
    return next(iter(refs))


def strip_one_trailing_semicolon(sql: str) -> str:
    """``sql`` without a single trailing statement terminator.

    A terminator is legal at the top level of every engine Agnes talks to and
    fatal the moment the statement is embedded — ``SELECT * FROM (<body>) AS
    _q LIMIT n`` is a syntax error if ``<body>`` ends in ``;``. Six call sites
    need the same one-character rule: the ``SELECT``-only validators
    (``app/api/query.py``, ``src/remote_query.py`` ×2) so a terminator is not
    read as a second statement, and the subquery wrappers
    (``src/remote_query.py:register_bq``, ``connectors/databricks/remote.py``,
    ``connectors/internal/access.py``, ``cli/mcp/server.py``) so it is gone
    before the embed.

    It exists because those sites were first written as five separate copies
    of the rule, in two slightly different idioms — and the sixth,
    ``cli/mcp/server.py``'s ``query_local``, was missed, so the MCP tool an
    agent reaches for kept raising a raw DuckDB ``ParserException`` on exactly
    the input the other five had learned to accept. One helper, so a seventh
    embed site is a one-line call rather than a rediscovery.

    Exactly ONE terminator is removed. ``SELECT 1;;`` keeps a ``;`` and is
    still refused as multi-statement by the validators — the guards must not
    become loopable.
    """
    body = sql.rstrip()
    return body[:-1] if body.endswith(";") else body
