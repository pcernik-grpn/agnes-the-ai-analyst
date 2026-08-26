"""The access-policy surface ratchet (table access policies design doc §8,
§23.2; plan Task 13) -- the one acceptance gate for the whole feature.

Tasks 7-9 wired specific, hand-picked surfaces (``/api/query``, the
``table_id``-shaped surfaces, ``/api/v2/schema``) against the resolver in
``src/access_policy.py``. A hand-written list of "the surfaces that read
table data" is exactly the shape that produced #513/#518 (the DuckDB/PG
backend-split bug class ``tests/test_backend_split_guard.py`` ratchets) and
the spec's own §8 opening: a first draft that called six surfaces exhaustive
was not, "and the proposed test derived from the same hand-written list
would have inherited the blind spot by construction."

So this is a RATCHET, not a list, same shape as ``test_backend_split_guard.py``:

1. Statically scan every ``.py`` file under ``app/api/`` (recursively) and
   ``app/web/router.py`` for a call to one of the functions that read a
   *registered table's* rows/columns or the RBAC gate in front of them --
   ``can_access_table`` / ``get_accessible_tables`` / ``get_analytics_db_readonly``
   / ``profile_repo`` (table_profiles -- §11's "sharper leak": min/max/
   sample_values/top_values) -- or a raw local-parquet read
   (``read_parquet(...)`` / the shared ``LOCAL_PARQUET_READ_EXPR`` constant).
   Each hit is keyed ``"{relpath}::{enclosing_function}"`` -- the function
   TEXTUALLY containing the call, mirroring ``test_backend_split_guard.py``'s
   own ``scan_raw_state_sql`` detector, not a full call-graph resolution
   (which this codebase's helper-delegation style -- route handler calls a
   shared builder calls a private helper -- makes far too fragile to trust
   mechanically; a human still has to read each surface once to classify it,
   exactly as the backend-split guard's own module docstring says).
2. Every discovered node must be in exactly one of ``COVERED`` (its own body
   also calls one of the resolver's four functions -- ``policied_relation``,
   ``rewrite_sql``, ``policied_from_sql``, ``effective_schema``, from
   ``src/access_policy.py``) or ``EXEMPT`` (a one-line justification comment
   sits next to every entry below). An unclassified node fails the test,
   naming it -- so a brand new read surface is a blocking failure until
   someone classifies it, not a silent gap.
3. Both lists are checked for STALE entries too (mirroring
   ``test_backend_split_guard.py``'s own "allow-list has no stale entries"
   tests): once a node is removed or renamed, its entry must be deleted, so
   the two sets stay an honest description of the current codebase rather
   than accreting dead references.

Three proxy surfaces spec §8 names by hand are deliberately NOT scanned nodes
here, and this is not an oversight -- see the comment block at the bottom of
this file ("Manual audit of the three proxy surfaces...") for why each is
out of this file's scope and what was actually checked instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# `app/api/**` recursively (per the task's own file-scope instruction) plus
# the single `app/web/router.py` file -- web pages elsewhere in `app/web/`
# render admin/catalog/library chrome, not table row/column data, and are
# out of this ratchet's stated scope.
_SCAN_ROOTS = (REPO_ROOT / "app" / "api", REPO_ROOT / "app" / "web" / "router.py")

# The four RBAC/data-read primitives a route touches on the way to a
# registered table's rows or columns (src/rbac.py + src/repositories, plus
# the raw-parquet-read constant every `table_id`-shaped surface's local
# branch uses). Deliberately does NOT include `get_accessible_ids` (the
# GENERIC resource_grants gate used for non-TABLE resource types --
# DATA_PACKAGE, RECIPE, COLLECTION, ...) or `can_access`/`can_access_session`
# -- those answer "can this caller see this ENTITY exists", not "does this
# response carry a registered table's row/column content", and including
# them would flood this ratchet with routes that have nothing to do with
# table data (recipe detail pages, collection sharing, ...).
_TARGET_CALL_NAMES = frozenset(
    {
        "can_access_table",
        "get_accessible_tables",
        "get_analytics_db_readonly",
        "profile_repo",
    }
)

# The resolver's own four functions (src/access_policy.py) -- documented
# here only (never re-derived by a second AST pass): whether a COVERED
# node's body calls one of these, possibly through one same-file private
# helper (`v2_schema.py::build_schema` -> `_apply_effective_schema` ->
# `effective_schema`), is verified by reading the source once, the same way
# every `COVERED` classification below was reached, not mechanically
# reconstructed from a call graph this codebase's helper-delegation style
# would make fragile to trust.
RESOLVER_FUNCTIONS = ("policied_relation", "rewrite_sql", "policied_from_sql", "effective_schema")


def _rel(p: Path) -> str:
    p = p.resolve()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # Outside the repo (a tmp_path fixture in the meta-tests below) --
        # key by its absolute path; it will never match a real classification
        # entry, matching test_backend_split_guard.py's own fallback.
        return p.as_posix()


def _files_to_scan() -> list[Path]:
    out: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.is_dir():
            out.extend(sorted(root.rglob("*.py")))
        else:
            out.append(root)
    return out


def _enclosing_function_map(tree: ast.AST) -> dict[ast.AST, str]:
    """``{node: enclosing_function_name}`` for every node in ``tree``,
    ``"<module>"`` for anything at module scope. Mirrors
    ``test_backend_split_guard.py``'s ``scan_raw_state_sql`` walker exactly
    -- the innermost ``def``/``async def`` TEXTUALLY containing the node,
    not a call-graph resolution.
    """
    parents: dict[ast.AST, str] = {}

    def walk(node: ast.AST, fname: str) -> None:
        for child in ast.iter_child_nodes(node):
            cf = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fname
            parents[child] = cf
            walk(child, cf)

    parents[tree] = "<module>"
    walk(tree, "<module>")
    return parents


def _call_name(node: ast.Call) -> str | None:
    """Bare-name call target (``foo(...)``), or ``None`` for a method/
    attribute call (``x.foo(...)``) -- every target symbol here is imported
    and called by its bare name throughout this codebase (``can_access_table
    (user, table_id, conn)``, ``profile_repo().get(...)`` -- the CALL this
    matches is ``profile_repo()`` itself, a bare name; ``.get(...)`` is a
    separate, uninteresting attribute call on its result)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _string_literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def scan_policy_relevant_nodes(files: list[Path] | None = None) -> dict[str, set[str]]:
    """``{"relpath::function": {matched_symbol, ...}}`` for every function
    that calls a target RBAC/data-read primitive, references the shared
    ``LOCAL_PARQUET_READ_EXPR`` local-parquet-read constant, or contains a
    string literal naming ``read_parquet(`` directly (some call sites splice
    the escaped path into an f-string instead of using the shared constant --
    ``v2_sample.py``/``v2_scan.py``'s policied branches do this on purpose,
    see ``src/access_policy.py::policied_from_sql``'s own docstring)."""
    found: dict[str, set[str]] = {}
    for path in files if files is not None else _files_to_scan():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        parents = _enclosing_function_map(tree)
        rel = _rel(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in _TARGET_CALL_NAMES:
                    found.setdefault(f"{rel}::{parents.get(node, '<module>')}", set()).add(name)
            elif isinstance(node, ast.Name) and node.id == "LOCAL_PARQUET_READ_EXPR":
                found.setdefault(f"{rel}::{parents.get(node, '<module>')}", set()).add("LOCAL_PARQUET_READ_EXPR")
            elif isinstance(node, (ast.Constant, ast.JoinedStr)) and "read_parquet(" in _string_literal_text(node):
                found.setdefault(f"{rel}::{parents.get(node, '<module>')}", set()).add("read_parquet_literal")
    return found


# ---------------------------------------------------------------------------
# COVERED -- the node's own body calls one of the resolver's four functions
# (directly, or through exactly one same-file private helper -- noted where
# that applies). Each entry names which Task wired it and, for the two fixed
# in THIS task, what was wrong before.
# ---------------------------------------------------------------------------

COVERED: frozenset[str] = frozenset(
    {
        # Task 7 -- rewrite_sql substitutes every policied table reference
        # in the caller's own SQL text before it reaches DuckDB.
        "app/api/query.py::execute_query",
        "app/api/query.py::run_remote_select_to_arrow",
        # Task 8 -- table_id-shaped surfaces: policied_relation +
        # policied_from_sql build the FROM clause (these have no caller SQL
        # tree to rewrite). The BQ live-query branch of each (`_fetch_bq_sample`
        # / `run_scan`'s `use_bq` branch) had NO enforcement at all until this
        # task -- Task 10 only wired `policied_relation(dialect="bigquery")`
        # into `/api/query`'s AST-rewrite surface, never into these two; see
        # the `# Task 13` comments at the call sites for the fail-closed fix
        # and the follow-up TODO for full BigQuery jobs-API wiring.
        "app/api/v2_sample.py::build_sample",
        "app/api/v2_scan.py::run_scan",
        "app/api/mcp_per_table.py::query_table",
        # Task 9 -- build_schema calls can_access_table directly and routes
        # the policied case through `_apply_effective_schema` (a same-file,
        # single-call-site private helper) -> `effective_schema`.
        "app/api/v2_schema.py::build_schema",
        # Task 13 -- app/api/catalog.py's profile endpoints called
        # profile_repo() directly and returned the stored profile
        # (min/max/sample_values/top_values -- §11's "sharper leak")
        # completely unfiltered by any policy on the table. Both now call
        # `policied_relation` to decide admin-bypass vs. suppress.
        "app/api/catalog.py::get_table_profile",
        "app/api/catalog.py::refresh_profile",
        # Task 13 -- catalog_table_detail called `build_schema_uncached`
        # DIRECTLY (skips RBAC AND the Task 9 effective-schema override by
        # its own docstring) and read `profile_repo()`'s raw column list
        # with no policy awareness -- a non-admin saw an EXCLUDE'd column
        # name in "What's inside". Now calls `effective_schema` to filter
        # the column list from EITHER source (profile-derived or the
        # `build_schema` fallback), and `policied_relation` was reachable
        # already via the schema fallback but is now also the basis for
        # the (see EXEMPT note) profile-stat suppression this route itself
        # never rendered in the first place.
        "app/web/router.py::catalog_table_detail",
        # Task 17 -- the effective-access self-audit (§10.2): GET
        # /api/me/effective-access and GET /api/admin/users/{id}/effective-
        # access report a per-table `policy` diagnosis (applies/rows_visible/
        # reason). `_table_access_diagnoses` calls `get_accessible_tables` to
        # get the STACK-GATED table set, then for each row calls the
        # same-file `_table_policy_diagnosis`, which calls `policied_relation`
        # directly. `_count_through_relation` doesn't call the resolver
        # itself -- it takes an ALREADY-RESOLVED `PoliciedRelation` from its
        # one caller (`_table_policy_diagnosis`) and executes ONLY that
        # relation's own SQL (never a raw, unfiltered read of the table) --
        # the same "operates on a pre-resolved relation, not a fresh read"
        # shape as `src/access_policy.py::policied_from_sql` itself.
        "app/api/access.py::_table_access_diagnoses",
        "app/api/access.py::_count_through_relation",
    }
)

# ---------------------------------------------------------------------------
# EXEMPT -- never calls the resolver; each entry's comment is the
# justification the ratchet exists to demand. Grouped by shape, not by file,
# because the justification classes repeat across files.
# ---------------------------------------------------------------------------

EXEMPT: frozenset[str] = frozenset(
    {
        # ── admin-only (require_admin gates the whole route; §12's admin
        # bypass is not an omission for these, it is the design) ──────────
        # PUT /registry/{id} -- the get_analytics_db_readonly() call is
        # Task 12's save-time LIMIT-0 policy probe, run by the admin
        # AUTHORING the policy against the table they are about to gate,
        # not a caller reading its content.
        "app/api/admin.py::update_table",
        # Task 14 -- POST /registry/{id}/policy/preview (design doc §13.1).
        # UNLIKE update_table's probe above, this endpoint DOES intentionally
        # hand back real row content -- that is the feature, not an
        # accidental leak: an admin choosing a persona (as_user/as_groups)
        # to check a stored or candidate policy before trusting it, audited
        # (`access_policy.preview`) per §13.1's "who looked at whose data".
        # It never calls the resolver's four functions BY DESIGN, not by
        # omission: `policied_relation`'s admin bypass (§12) follows the
        # CALLING admin's own credential surface, which is exactly wrong
        # here -- the whole point is to run the policy as the CHOSEN
        # persona regardless of who is asking, so it binds that persona's
        # identity/groups directly and reads through `probe_policy` +
        # `get_analytics_db_readonly()` instead.
        "app/api/admin.py::preview_table_policy",
        # access-policy-builder-ux plan, Tasks 2/3 -- GET .../policy/columns
        # and its shared `_policy_builder_describe` DESCRIBE helper. Same
        # admin-authoring posture as `preview_table_policy` right above:
        # require_admin-gated, reads the table's OWN schema/profile so an
        # admin can pick columns to mask BEFORE any policy exists, never a
        # caller-facing content read. `policy_builder_compile` (the
        # POST .../policy/compile handler) calls this same helper but is not
        # itself a scanned node -- it never calls a target primitive
        # directly, only through this already-classified helper.
        "app/api/admin.py::_policy_builder_describe",
        # Same builder endpoint -- its own body also calls `profile_repo()`
        # directly (sample values for the columns list). The profile shown
        # here is the table's OWN stored profile, read by the admin who is
        # about to attach a policy TO this exact table -- not the
        # caller-facing `catalog.py::get_table_profile` this ratchet's Task
        # 13 entry already covers (which suppresses profile stats for a
        # non-admin caller once a policy IS attached).
        "app/api/admin.py::policy_builder_columns",
        # POST /api/query/hybrid -- spec §8 names this one explicitly: "out
        # of scope by §12's admin bypass, not by omission".
        "app/api/query_hybrid.py::hybrid_query",
        # Live sample for a non-BQ `query_mode='remote'` row -- the exact
        # twin of `_fetch_bq_sample` (which the scanner never sees only
        # because `bq.duckdb_session()` is not a target primitive; this one
        # reads through `get_analytics_db_readonly()`, which is). Single
        # call site: `build_sample`'s remote branch (COVERED), which runs
        # the same Task-13 fail-closed guard as the BQ branch -- a policied
        # row's non-admin caller 500s before this fetch is reached, and the
        # helper reads exactly the one view `can_access_table` already
        # authorized.
        "app/api/v2_sample.py::_fetch_remote_view_sample",
        # Databricks remote routing probe: reads `information_schema.tables`
        # to answer "does a master view exist for the remote rows this SQL
        # names", i.e. whether DuckDB can resolve the statement locally at
        # all. Catalog SHAPE, never a row and never a column list, and its
        # answer only picks which execution path runs -- both of which apply
        # the policy in their own right (`execute_query` is COVERED; the
        # remote branch it guards refuses a policied table outright via
        # `_assert_no_policied_remote_engine`, since Agnes cannot enforce a
        # policy on a statement that executes on an external warehouse).
        "app/api/query.py::_databricks_attach_views_available",
        # ── catalog / listing surfaces: table-card metadata (id, name,
        # description, source_type, query_mode, row COUNT), never row
        # content or a column list. §10.1 explicitly keeps a policied
        # table's unfiltered row count on the catalog badge -- "per-caller
        # counts would mean running the policy on every catalog page load,
        # real money on a BQ-backed table" -- so a bare count here is the
        # designed behavior, not a gap. ─────────────────────────────────
        "app/api/catalog.py::list_catalog_tables",
        "app/api/sync.py::_build_manifest_for_user",
        "app/web/router.py::_chat_capability_snapshot",
        "app/web/router.py::library_page",
        # knowledge_search's table hits are catalog cards built by
        # `src/search/unified.py::_table_scores` (id/name/description/
        # pivot_hint) -- `columns_json` (admin-authored doc metadata, not a
        # live schema read) is consulted only to SCORE relevance; no
        # column name it contributes is ever placed in the returned hit.
        "app/api/knowledge_search.py::knowledge_search",
        # Task 13 -- v2_catalog.py's build_catalog: `where_examples` /
        # `partition_by` / `clustered_by` are column-NAME-shaped hints
        # sourced from the UNFILTERED `bq_metadata_cache` (a scheduler read
        # of the physical table, independent of any later-attached policy)
        # -- an EXCLUDE'd column could appear in a WHERE-clause suggestion
        # or a partitioning hint even though Task 9's effective_schema
        # already hides it everywhere else. Fixed by SUPPRESSING those
        # three fields for a policied table's non-admin caller (mirrors
        # `hint.get(...)` -> `[]`/`None` at the call site) rather than
        # routing through the resolver: this endpoint's own module
        # docstring states its design goal as "never touches BQ" / stays
        # cheap per request for up to ~100+ rows, and a live
        # `policied_relation` + `effective_schema` DESCRIBE per
        # policied-remote row would break that. Row count / size_bytes /
        # entity_type stay unfiltered, same §10.1 aggregate-metadata
        # precedent as the manifest/knowledge-search entries above.
        "app/api/v2_catalog.py::build_catalog",
        # ── metric/glossary surfaces: return the metric DEFINITION (SQL
        # template text, description, dimension names) gated by whether the
        # tables it REFERENCES are in the caller's stack -- never executes
        # that SQL or returns a row. ─────────────────────────────────────
        "app/api/metrics.py::list_metrics",
        "app/api/metrics.py::get_metric",
        "app/web/router.py::catalog_semantics",
        # ── estimate-only: a BQ dry-run byte/row/cost NUMBER, never row
        # content or a column list. select/where/order_by are validated
        # against `build_schema`'s (COVERED) effective schema via
        # `_resolve_schema`, so an EXCLUDE'd column can't even be
        # requested through this endpoint. The dry-run itself still scans
        # the raw physical table for the byte estimate (same known gap as
        # `run_scan`'s BQ branch, `_build_bq_sql`'s own comment) -- left
        # alone here because it returns no content, only a cost number
        # (§10.1's own row-count precedent: an unfiltered AGGREGATE is
        # accepted, content is not).
        "app/api/v2_scan.py::estimate",
        # ── distribution-interlock-protected: the Task 4 interlock
        # guarantees a policied table is ALWAYS server_only=True or
        # query_mode='remote' for as long as the policy is attached, and
        # `_distribution_refusal` 403s BOTH of those shapes unconditionally
        # (server_only rows outright; 'remote' rows because it is not in
        # `_DISTRIBUTABLE_QUERY_MODES = {local, materialized}`) -- so
        # neither handler can ever stream a policied table's parquet bytes,
        # structurally, independent of RBAC. ────────────────────────────
        "app/api/data.py::check_access",
        "app/api/data.py::download_table",
        # ── attachment binaries: the SAME server_only interlock as data.py's
        # parquet route, one indirection removed. `_lookup_stored_path` reads
        # `local_path` from the catalogue view, but its ONLY caller is
        # `download_attachment`, which 403s a `server_only` table BEFORE the
        # lookup runs (the "these bytes do not leave the server" gate at the
        # `reg_row.get("server_only")` check, mirroring `_distribution_refusal`).
        # A policy attaches only to `server_only=true` or `query_mode='remote'`
        # (the Task 4 interlock): the former is refused before
        # `_lookup_stored_path` is ever reached, and the latter can never be an
        # attachment source — the declared sources
        # (`src/attachment_sources.py::_SOURCES`) are a fixed dict whose only
        # entry is Jira's local `attachments` table, so `get_attachment_source`
        # returns None for a remote-policied table long before any catalogue
        # read. Neither node can therefore reach a policied table's rows. ────
        "app/api/attachments.py::_lookup_stored_path",
        "app/api/attachments.py::download_attachment",
        # ── writes-only / not a registry-table read at all ──────────────
        # Scheduler/background sync job (POST /api/sync/trigger or the
        # cron tick) -- writes the RAW profile to storage (needed so the
        # admin/no-policy case in app/api/catalog.py's profile endpoints
        # has something to show); returns nothing to any HTTP caller.
        "app/api/sync.py::_run_sync",
        # Materializes an UPLOADED FILE into a brand-new, per-user
        # extract.duckdb -- explicitly documented as never touching
        # table_registry ("This intentionally does NOT mutate the
        # server-side admin table_registry"). Table access policies apply
        # to REGISTERED tables; this path creates one that was never
        # registered in the first place.
        "app/api/chat_uploads.py::_register_workspace_table",
        # ── internal helper, not itself an HTTP route: named "uncached",
        # skips RBAC by its own docstring ("Skips RBAC and cache-hit
        # short-circuit -- call only from contexts where those are
        # unnecessary (warmup) or already enforced upstream (build_schema)").
        # Every caller either wraps it in `build_schema` (COVERED) or --
        # catalog_table_detail, fixed in this task -- applies its own
        # effective_schema post-filter after calling it. ────────────────
        "app/api/v2_schema.py::build_schema_uncached",
    }
)


def test_every_data_read_surface_is_policy_covered_or_exempt():
    """The ratchet. A NEW node here -- a route that starts calling one of
    the target primitives -- is a blocking failure until it is added to
    COVERED (wired to the resolver) or EXEMPT (justified in a comment
    above), never silently left unclassified."""
    nodes = set(scan_policy_relevant_nodes())
    unclassified = nodes - COVERED - EXEMPT
    assert not unclassified, (
        "unwired/unclassified access-policy surface(s) -- add each to COVERED "
        "(wire it to src.access_policy's resolver) or EXEMPT (one-line "
        "justification comment) in tests/test_access_policy_surface_ratchet.py:\n"
        + "\n".join(f"  {n}" for n in sorted(unclassified))
    )


def test_covered_and_exempt_are_disjoint():
    overlap = COVERED & EXEMPT
    assert not overlap, f"node(s) classified as BOTH covered and exempt: {sorted(overlap)}"


def test_classification_has_no_stale_entries():
    """Every COVERED/EXEMPT entry must still be a node the scanner finds --
    once code moves (a function renamed, a call site removed, a file
    deleted) the entry must be deleted too, so the classification stays an
    honest description of the current codebase rather than accreting dead
    references that look like coverage but classify nothing real. Mirrors
    ``test_backend_split_guard.py``'s own allow-list-staleness checks."""
    nodes = set(scan_policy_relevant_nodes())
    stale = (COVERED | EXEMPT) - nodes
    assert not stale, (
        "stale classification entries -- these no longer match any scanned "
        "node (renamed/removed); delete them from COVERED/EXEMPT so the "
        "ratchet's residual stays honest:\n" + "\n".join(f"  {n}" for n in sorted(stale))
    )


def test_detector_flags_a_planted_violation(tmp_path):
    """A synthetic route that calls a target primitive with no
    classification must be flagged -- guards against the scanner silently
    matching nothing (the exact failure mode `test_backend_split_guard.py`'s
    own meta-tests guard against)."""
    planted = tmp_path / "planted_route.py"
    planted.write_text(
        "from src.rbac import can_access_table\n\n\ndef leaky_handler(user, table_id, conn):\n"
        "    if not can_access_table(user, table_id, conn):\n        raise PermissionError\n"
        "    return {'rows': []}\n"
    )
    found = scan_policy_relevant_nodes([planted])
    assert any(k.endswith("::leaky_handler") for k in found), "detector failed to flag a planted unclassified route"


def test_detector_ignores_unrelated_calls(tmp_path):
    """A function that calls something else entirely must not be flagged --
    the scanner should be specific to the four target primitives, not any
    call whatsoever."""
    clean = tmp_path / "clean_route.py"
    clean.write_text("def unrelated_handler(x):\n    return sorted(x)\n")
    found = scan_policy_relevant_nodes([clean])
    assert not found, f"unrelated call wrongly flagged: {found}"


def test_detector_finds_the_known_covered_and_exempt_nodes():
    """Sanity check the scanner actually reaches the two real files this
    task edited -- both a COVERED and an EXEMPT node from each should show
    up, so a scan-root typo (e.g. scanning the wrong directory) fails loudly
    here instead of silently passing an empty-set ratchet."""
    nodes = set(scan_policy_relevant_nodes())
    assert "app/web/router.py::catalog_table_detail" in nodes
    assert "app/api/catalog.py::list_catalog_tables" in nodes
    assert "app/api/v2_sample.py::build_sample" in nodes
    assert "app/api/data.py::download_table" in nodes


# ---------------------------------------------------------------------------
# Manual audit of the three proxy surfaces spec §8 names by hand
# (broker replay / stdio MCP / Cowork stdio shim), plus the REST-proxying
# MCP foundation tools the spec calls out as "not a separate surface". None
# of these appear as scanned nodes above -- not an oversight, each was read
# end to end and confirmed to never touch table content directly:
#
# - ``app/api/broker.py`` (``POST /api/broker/agnes-api`` /
#   ``/agnes-mcp``) -- resolves the caller's ticket to their REAL identity,
#   mints an ordinary session JWT for it, and replays the described
#   ``{method, path, body}`` request IN-PROCESS through the same FastAPI app
#   (``httpx.ASGITransport``, per the module's own docstring: "This keeps
#   every access-control check ... exactly as live as a direct call -- the
#   broker adds no privilege of its own"). A table-data request routed
#   through the broker lands on one of the SAME endpoint functions this
#   ratchet already classifies, with the caller's real identity and every
#   Depends() gate (RBAC, and now the access-policy resolver) intact.
#
# - ``cli/mcp/server.py`` (the stdio MCP server ``agnes mcp`` starts) --
#   every tool calls the Agnes REST API over HTTP via ``cli.client`` /
#   ``cli.v2_client`` using the caller's own PAT (``GET /api/v2/schema``,
#   ``GET /api/v2/sample``, ``POST /api/query``, ...) -- the same
#   already-classified endpoints. Its LOCAL execution path only ever reads
#   parquets ``agnes pull`` synced, and the distribution interlock (Task 4)
#   guarantees a CURRENTLY-policied table is never `local`/`materialized`
#   distributed, so there is nothing local to read for one. A STALE local
#   copy left over from before a table became policied is a client-side
#   state problem, not a server read surface this file's scan root
#   (`app/api/**` + `app/web/router.py`) covers -- it is explicitly Task
#   18's (not-yet-built) snapshot-fingerprint concern (design doc §10.3).
#
# - ``app/api/cowork_bundle.py`` -- generates a downloadable setup ZIP whose
#   ``mcp_server.py`` is a pure-stdlib script that RUNS ON THE ANALYST'S OWN
#   MACHINE (not server-side) and forwards each MCP tool call to Agnes's
#   REST API over HTTPS using the caller's PAT -- same shape as
#   ``cli/mcp/server.py`` above, one level further removed (the proxy code
#   is a string template shipped in a ZIP, not code that runs on the
#   server at all).
#
# - ``app/api/mcp/foundation_tools.py`` -- spec §8 states this directly:
#   "not a separate surface -- its schema/describe/query are HTTP proxies to
#   the REST endpoints, so enforcing at REST covers it". Confirmed: every
#   tool opens an ``httpx.AsyncClient()`` and calls the same REST paths
#   (``/api/v2/schema``, ``/api/v2/sample``, ``/api/query``) already
#   classified above.
# ---------------------------------------------------------------------------
