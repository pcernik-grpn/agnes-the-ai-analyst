"""Backend-split guard — a ratchet that pins the DuckDB→Postgres migration debt.

Two bug classes broke the Postgres app-state backend after the module-by-module
migration (see #499/#513/#518/#522 and the repo method-parity guard in
``tests/db_pg/test_repo_method_parity.py``):

* **#513** — constructing a backend-aware repo directly (``XRepository(conn)``)
  instead of going through the ``src.repositories`` factory. On a PG instance
  the factory returns the ``*Pg`` repo; a direct ``XRepository(conn)`` hits the
  always-DuckDB connection instead.
* **#518** — calling ``get_system_db()`` (always DuckDB) in a request/handler
  path and reading/writing system state off it, bypassing the factory.

You cannot *prove* by inspection that every such site was found — sampling
always misses some (a 125-agent audit did). The only mechanical guarantee is a
ratchet: this test enumerates every current site, pins them in an allow-list,
and FAILS when a NEW one appears. Two further invariants keep it honest:

* the allow-list may not contain a *stale* entry — once a site is migrated to
  the factory, its entry must be removed (the residual list always reflects
  reality), so the allow-list shrinking to legit-only is the definition of
  "migration finished";
* a planted violation must be detected (the detector actually works).

NOTE on legitimacy: a grandfathered entry is NOT automatically a bug. Some are
genuinely DuckDB-only by design (analytics rebuild in ``src/orchestrator.py`` /
``src/profiler.py``; cloud-chat PG-only persistence;
DuckDB-only CLI maintenance commands). The migration is "done" when every
remaining entry is one of those — verified by deleting entries as their callers
are routed through the factory (or confirmed DuckDB-only) until only the
permanent ones remain. Today the lists are the full residual.
"""

from __future__ import annotations

import ast
import re
import subprocess
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "cli", "services", "src", "connectors")

# Repository infra that legitimately owns the raw connection / getter.
_INFRA_EXCLUDE = ("/repositories/", "src/db.py", "src/db_pg.py")


# ---------------------------------------------------------------------------
# detectors (parametrised so the meta-tests can run them on a synthetic file)
# ---------------------------------------------------------------------------


def _rel(p: Path) -> str:
    p = p.resolve()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # Path outside the repo (e.g. a tmp file in the meta-tests) — key by its
        # absolute path; it will never match a residual allow-list entry.
        return p.as_posix()


def backend_aware_repo_classes() -> set[str]:
    """Every repository class that has a DuckDB+PG pair (so a direct
    constructor bypasses the backend switch)."""
    repo_dir = REPO_ROOT / "src" / "repositories"
    pg_stems = {p.stem[:-3] for p in repo_dir.glob("*_pg.py")}
    classes: set[str] = set()
    for stem in pg_stems:
        for fname in (f"{stem}.py", f"{stem}_pg.py"):
            f = repo_dir / fname
            if not f.exists():
                continue
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.ClassDef) and node.name.endswith("Repository"):
                    classes.add(node.name)
    return classes


def scan_direct_instantiations(files, classes) -> dict[str, set[str]]:
    """``{relpath: {RepoClassName, ...}}`` for direct ``XRepository(...)`` calls."""
    found: dict[str, set[str]] = {}
    for p in files:
        p = Path(p)
        try:
            tree = ast.parse(p.read_text())
        except (SyntaxError, OSError):
            continue
        hits = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in classes
        }
        if hits:
            found[_rel(p)] = hits
    return found


def _production_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            rp = p.as_posix()
            if "/repositories/" in rp or "/tests/" in rp:
                continue
            out.append(p)
    return out


def _get_system_db_caller_files() -> set[str]:
    result = subprocess.run(
        ["grep", "-rlE", "--include=*.py", r"get_system_db\(\)", *SCAN_DIRS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    files = set()
    for rp in result.stdout.splitlines():
        if "/tests/" in rp or any(x in rp for x in _INFRA_EXCLUDE):
            continue
        files.add(rp)
    return files


# ---------------------------------------------------------------------------
# the residual allow-lists (the live migration debt — shrink, never grow)
# ---------------------------------------------------------------------------

_GRANDFATHERED_DIRECT_INSTANTIATION: dict[str, set[str]] = {
    # cli_auth.py — PAT minting migrated to access_token_repo(); entry removed.
    # cowork_bundle.py — fully migrated to the factory (setup_tokens_repo /
    # users_repo / access_token_repo / audit_repo); entry removed.
    # admin_chat.py, admin_mcp.py, data_packages.py, memory_domain_suggestions.py,
    # recipes.py — AuditRepository call sites migrated to audit_repo(); entries removed.
    # mcp/tools_generator.py — passthrough registration migrated to
    # mcp_sources_repo()/tool_registry_repo(); entry removed.
    # mcp_per_table.py — registry lookup migrated to table_registry_repo(); entry removed.
    # mcp_user_secrets.py — migrated to mcp_sources_repo()/per_user_secrets_repo(); entry removed.
    # memory.py — mark_mandatory/mark_unmandatory/admin_get_item/domain markdown
    # export migrated to knowledge_repo(); entry removed.
    # memory_domains.py — add_item_to_domain migrated to knowledge_repo(); entry removed.
    # stack.py — _emit_event migrated to usage_repo(); entry removed.
    # stack_views.py — _emit_view/view_memory_domain migrated to usage_repo()/
    # knowledge_repo(); entry removed.
    "app/auth/access.py": {
        "ResourceGrantsRepository",
        "UserGroupMembersRepository",
        "UserGroupsRepository",
    },
    # Sanctioned `if not use_pg(): XRepository(conn) else: x_repo()` escape hatch
    # (test-isolation / DuckDB-mode), same pattern as app/auth/access.py — NOT a
    # raw backend-split. On Postgres these route through the factory.
    "app/services/stack_resolver.py": {
        "DataPackagesRepository",
        "MemoryDomainsRepository",
        "ResourceGrantsRepository",
        "UserGroupMembersRepository",
        "UserGroupsRepository",
        "UserStackSubscriptionsRepository",
    },
    # cloud-chat PG-only persistence (legit by design, see module docstring):
    # ChatRepository.__init__ instantiates the *Pg repos only under `use_pg()`
    # and dispatches every method through them — not a backend-split.
    "app/chat/persistence.py": {
        "ChatMessagePgRepository",
        "ChatSessionPgRepository",
        "UserWorkdirPgRepository",
        "ChatSessionParticipantPgRepository",
    },
    # main.py lifespan seed-admin: group membership routes through
    # user_group_members_repo() (factory); the cowork-bundle workspace-prompt
    # read was migrated to users_repo() too (#518) — entry removed.
    # Sanctioned `if conn is not None and not use_pg(): UserRepository(conn) else:
    # users_repo()` escape hatch (same pattern as app/auth/access.py). On Postgres
    # the read routes through the factory.
    "src/grant_intersection.py": {"UserRepository"},
    # app/web/router.py — migrated to table_registry_repo()/sync_state_repo()
    # (catalog detail pages); entry removed as the residual shrank.
    # cli/commands/admin_data_semantics.py — the entry claimed the sanctioned
    # escape hatch, but the gate was actually MISSING (the five catalog repos
    # were instantiated on the raw get_system_db() conn unconditionally — the
    # data-semantics pack froze at cutover on PG instances). Migrated to the
    # factory getters; entry removed.
    # Slack bot — user-identity lookups (commands.py/events.py) migrated to
    # users_repo(); channel-allowlist grant lookup routed through the factory.
    # The DuckDB-only binding-code tables stay on repo._conn by design.
    # Entries removed.
    # src/catalog_export.py — migrated to table_registry_repo(); entry removed.
    # Later deleted entirely (dead OpenMetadata export, D6c) — never
    # re-added since deletion.
    # src/claude_md.py — render_claude_md moved off direct instantiation onto
    # resolve_prompt() (#622); entry removed.
    # src/initial_workspace.py — resolve_prompt() binds the DuckDB repo to the
    # CALLER's conn (not get_system_db()) so the renderer sees the connection
    # the request is already using, matching the old render_claude_md contract
    # and the renderer unit tests that pass an isolated conn. _prompt_repo
    # gates that direct binding on `not use_pg()` — on Postgres the factory
    # ALWAYS wins, even when a (DuckDB) conn is passed, because FastAPI
    # handlers hand over get_system_db() conns regardless of backend (#638
    # review). The direct DuckDB-conn instantiation is intentional and
    # conn-scoped. (#622)
    "src/initial_workspace.py": {"ClaudeMdTemplateRepository", "WelcomeTemplateRepository"},
    # src/orchestrator.py — rebuild/sync_state/view_ownership migrated to the
    # factory; entry removed.
    # src/profiler.py — get_table_map migrated to metric_repo(); entry removed.
    # src/store_guardrails/purge.py — migrated to store_submissions_repo()/
    # store_entities_repo() + find_purge_candidates(); entry removed.
    # surfaced when "connectors" joined SCAN_DIRS (previously unscanned — the
    # 2026-07 audit's guard-coverage gap). Extractors run server-side where
    # DATABASE_URL may be set; direct DuckDB repo construction there is the
    # same backend-split class. Live debt — verify/fix, then delete.
    # keboola/extractor.py — registry/sync-state reads migrated to the factory; entry removed.
    # internal/registry.py — ensure_internal_tables_registered migrated to
    # table_registry_repo() + delete_internal_except(); entry removed.
    # bigquery/extractor.py — kept: sanctioned `not use_pg()` conn escape hatch;
    # on Postgres the factory always wins.
    "connectors/bigquery/extractor.py": {"TableRegistryRepository"},
    # mcp/extractor.py — kept: sanctioned `not use_pg()` conn escape hatch
    # (_sources_repo/_tools_repo); on Postgres the factory always wins.
    "connectors/mcp/extractor.py": {"MCPSourceRepository", "ToolRegistryRepository"},
    # src/store_guardrails/reaper.py + runner.py — both moved off direct
    # DuckDB-conn repo instantiation onto the src.repositories factory
    # (store_submissions_repo / store_entities_repo / audit_repo). The
    # direct-conn path was the Postgres no-op bug: the reaper reaped 0 and
    # run_llm_review logged "submission vanished" because the rows lived in
    # PG while the conn pointed at an empty DuckDB. Entries removed.
    # src/welcome_template.py — render_agent_prompt_banner moved off direct
    # instantiation onto resolve_prompt() (#622); entry removed.
}

_GRANDFATHERED_GET_SYSTEM_DB: set[str] = {
    "app/api/admin.py",
    # app/api/bq_metadata_refresh.py — vestigial conn params removed; the file
    # is fully factory-routed (bq_metadata_cache_repo / table_registry_repo).
    "app/api/cache_warmup.py",
    "app/api/health.py",
    "app/api/mcp_http.py",
    # app/api/mcp_streamable.py — passthrough registration migrated to the
    # factory; vestigial get_system_db() scaffolding deleted. Entry removed.
    # app/api/query.py — the bq-metadata VIEW-hint lookup (_view_targets_in)
    # now routes through the factory; no remaining get_system_db caller.
    # app/api/scripts.py — the script-runner tick + background-task audit/status
    # writes dropped their unused get_system_db() handles (audit_repo() /
    # notifications_script_repo() are factory-routed). Entry removed.
    "app/api/sync.py",
    # app/api/upload.py — the session-upload audit write dropped its unused
    # get_system_db() handle; audit_repo() is factory-routed. Entry removed.
    # app/api/v2_sample.py — internal-table sampling now routes through
    # connectors.internal.access.sample_internal_rows (use_pg() dispatch).
    # app/auth/access.py — mint_session_jwt migrated off get_system_db() onto
    # users_repo() (#518); entry removed.
    "app/auth/dependencies.py",
    # app/auth/pat_resolver.py — co-session resolution now routes through
    # chat_session_participants_repo()/chat_session_repo() (factory); entry
    # removed as the residual shrank.
    "app/auth/providers/google.py",
    # app/auth/router.py — the _audit helper dropped its unused get_system_db()
    # handle; audit_repo() is factory-routed. Entry removed.
    # app/chat/workspace_prompt.py — deliberately NOT here. The shared
    # workspace-prompt renderer takes an optional conn and opens none itself
    # (`resolve_prompt` resolves through the factory when given nothing), so
    # the list keeps shrinking instead of growing for a new module. Devin
    # review on PR #1235.
    "app/chat/agent_profile.py",
    "app/main.py",
    "app/marketplace_server/git_router.py",
    "app/web/router.py",
    "cli/commands/admin.py",
    # cli/commands/admin_data_semantics.py — migrated to factory getters
    # (see the matching note in _GRANDFATHERED_DIRECT_INSTANTIATION); removed.
    "cli/commands/admin_metrics.py",
    "services/verification_detector/__main__.py",
    "src/rbac.py",
    # surfaced when "connectors" joined SCAN_DIRS — see the matching note in
    # _GRANDFATHERED_DIRECT_INSTANTIATION. internal/access.py is verified
    # use_pg()-gated (data reads materialize PG rows into ephemeral DuckDB).
    # bigquery/extractor.py — migrated to the factory; entry removed.
    "connectors/internal/access.py",
    "connectors/keboola/extractor.py",
}


# ---------------------------------------------------------------------------
# detector #3 (#518 follow-up): a request handler that takes
# ``conn = Depends(_get_db)`` (always DuckDB) and then runs raw
# ``conn.execute("... <state table> ...")`` bypasses the factory exactly like a
# direct ``get_system_db()`` call — but the static get_system_db scan can't see
# it (the connection arrives via FastAPI DI, not a literal call). This AST scan
# closes that blind spot: it flags any ``<conn>.execute(<sql literal>)`` where
# ``<conn>`` is a ``Depends(_get_db)`` parameter and the SQL names a state table.
# ---------------------------------------------------------------------------


# State tables that have a Postgres backend — reading/writing them off the
# always-DuckDB ``_get_db`` connection is the backend-split bug. Derived from
# the SQLAlchemy model metadata (``src/models/`` — the Alembic source of
# truth), so a new model automatically extends the guard; no hand-maintained
# table list to drift.
@lru_cache(maxsize=1)
def _pg_state_tables() -> tuple[str, ...]:
    import src.models  # noqa: F401 — registers every model on Base.metadata
    from src.db_pg import Base

    return tuple(sorted(Base.metadata.tables.keys()))


def _is_depends_get_db(default) -> bool:
    return (
        isinstance(default, ast.Call)
        and isinstance(default.func, ast.Name)
        and default.func.id == "Depends"
        and any(isinstance(a, ast.Name) and a.id == "_get_db" for a in default.args)
    )


def _conn_param_names(fn) -> set[str]:
    """Names of parameters defaulted to ``Depends(_get_db)`` in ``fn``."""
    names: set[str] = set()
    a = fn.args
    posargs = a.posonlyargs + a.args
    for arg, default in zip(posargs[len(posargs) - len(a.defaults) :], a.defaults):
        if _is_depends_get_db(default):
            names.add(arg.arg)
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        if default is not None and _is_depends_get_db(default):
            names.add(arg.arg)
    return names


def _extract_sql_literal(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string — keep the literal parts
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _sql_hits_state(sql: str) -> bool:
    low = sql.lower()
    return any(f" {t}" in low or f"\n{t}" in low or f"\t{t}" in low for t in _pg_state_tables())


def scan_depends_get_db_raw_sql(files) -> dict[str, set[str]]:
    """``{relpath: {function_name, ...}}`` for handlers that run raw
    ``<conn>.execute(<sql touching a state table>)`` on a ``Depends(_get_db)``
    connection."""
    found: dict[str, set[str]] = {}
    for p in files:
        p = Path(p)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            conn_names = _conn_param_names(fn)
            if not conn_names:
                continue
            for call in ast.walk(fn):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in conn_names
                    and call.args
                    and _sql_hits_state(_extract_sql_literal(call.args[0]))
                ):
                    found.setdefault(_rel(p), set()).add(fn.name)
                    break
    return found


# Residual handlers that still run raw state SQL on the ``_get_db`` connection.
# Out of scope for the #518 RBAC-page fix; pinned here so the class cannot grow.
# Shrink (never grow): when a handler is routed through the factory, delete its
# entry. Some are analytics/telemetry reads that are DuckDB-only by design.
_GRANDFATHERED_DEPENDS_GET_DB_RAW_SQL: dict[str, set[str]] = {
    # surfaced when the state-table list went metadata-driven (knowledge_votes,
    # table_profiles, knowledge_item_relations were missing from the old
    # hand-maintained tuple). Live backend-split debt — fix, then delete.
    # memory.py — get_my_votes/list_knowledge migrated to knowledge_repo(); entry removed.
    # web/router.py — catalog profile + memory-relations count migrated to
    # profile_repo()/knowledge_repo(); entry removed.
}


# ---------------------------------------------------------------------------
# detector #4: raw state SQL through ANY connection outside the repository
# layer. Detectors #1-#3 all key on HOW the connection was obtained (repo
# constructor / get_system_db() / Depends(_get_db)) — so a helper that merely
# *receives* a conn as a plain parameter and runs raw SQL on a state table is
# invisible to all of them. That helper-param pattern produced the 2026-07
# backend-split batch (session-pipeline user attribution, marketplace usage
# attribution, home-page stats, activity health pulse, claude_md rendering).
# This detector keys on the SQL itself: any ``<x>.execute(<literal SQL that
# names a PG-backed state table>)`` outside src/repositories/ + db infra is
# flagged, regardless of where ``<x>`` came from. Table names come from the
# SQLAlchemy model metadata, so "has a PG backend" is mechanically derived.
# ---------------------------------------------------------------------------

# Files that legitimately own raw state SQL: the repo layer, the DB engines,
# and app/chat/persistence.py — the *registered* DuckDB chat repository (it
# lives outside src/repositories/ but IS a factory-dispatched repo).
_RAW_STATE_SQL_EXCLUDE = (
    "/repositories/",
    "src/db.py",
    "src/db_pg.py",
    "app/chat/persistence.py",
    # the registered DuckDB secrets repos (factory-dispatched, live outside
    # src/repositories/ for historical reasons — see _REGISTRY per_user_secrets)
    "app/secrets_vault.py",
)


@lru_cache(maxsize=1)
def _state_sql_pattern() -> re.Pattern:
    tables = "|".join(re.escape(t) for t in _pg_state_tables())
    return re.compile(
        r'\b(?:from|into|update|join|delete\s+from)\s+("?)(' + tables + r")\1\b",
        re.IGNORECASE,
    )


def scan_raw_state_sql(files) -> dict[str, set[str]]:
    """``{relpath: {enclosing_function, ...}}`` for raw ``.execute()`` /
    ``.executemany()`` calls whose SQL literal names a PG-backed state table,
    anywhere outside the repository layer."""
    found: dict[str, set[str]] = {}
    for p in files:
        p = Path(p)
        rp = _rel(p)
        if any(x in rp for x in _RAW_STATE_SQL_EXCLUDE):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        # map every call to its innermost enclosing function (module-level → <module>)
        parents: dict[ast.AST, str] = {}

        def _walk(node, fname):
            for child in ast.iter_child_nodes(node):
                cf = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fname
                parents[child] = cf
                _walk(child, cf)

        parents[tree] = "<module>"
        _walk(tree, "<module>")
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("execute", "executemany")
                and call.args
                and _state_sql_pattern().search(_extract_sql_literal(call.args[0]))
            ):
                found.setdefault(rp, set()).add(parents.get(call, "<module>"))
    return found


# Residual raw-state-SQL sites (file → enclosing functions). The live
# backend-split debt detector #1-#3 cannot see. Shrink, never grow: when a
# site is routed through the factory (its SQL moves into the repo pair), its
# entry must be deleted. A minority are DuckDB-only by design and stay.
_GRANDFATHERED_RAW_STATE_SQL: dict[str, set[str]] = {
    # activity.py, marketplace.py, me.py, me_debug.py, web/router.py — health
    # pulse, owner display, curated counts, home stats, google-sync summary,
    # catalog profile, memory-relations count all migrated to the factory;
    # entries removed.
    # memory.py — RBAC helpers + vote reads migrated to the factory; entry removed.
    # chat/audit.py, chat/copresence_summary.py, internal/registry.py,
    # session_pipeline/runner.py, session_processors/usage_lib.py,
    # verification_detector/__main__.py, slack_bot/{binding,events}.py,
    # claude_md.py, store_guardrails/purge.py — all migrated to the factory
    # (2026-07 backend-split batch); entries removed. The list is empty: every
    # raw state-SQL site now lives inside the repository layer.
}


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_no_new_direct_backend_aware_repo_instantiation():
    """No NEW ``XRepository(conn)`` for a factory-backed repo. New backend-aware
    state access must go through ``src.repositories`` factory functions."""
    classes = backend_aware_repo_classes()
    found = scan_direct_instantiations(_production_files(), classes)
    new = {
        f: sorted(hits - _GRANDFATHERED_DIRECT_INSTANTIATION.get(f, set()))
        for f, hits in found.items()
        if hits - _GRANDFATHERED_DIRECT_INSTANTIATION.get(f, set())
    }
    assert not new, (
        "New direct backend-aware repository instantiation(s) detected — route "
        "these through the src.repositories factory (e.g. users_repo()), they "
        "bypass the DuckDB/Postgres backend switch:\n" + "\n".join(f"  {f}: {cs}" for f, cs in sorted(new.items()))
    )


def test_direct_instantiation_allowlist_has_no_stale_entries():
    """Every grandfathered (file, class) must still exist. When a site is
    migrated to the factory its entry must be deleted — that is how the residual
    shrinks and how 'migration finished' becomes mechanically verifiable."""
    classes = backend_aware_repo_classes()
    found = scan_direct_instantiations(_production_files(), classes)
    stale = {
        f: sorted(allowed - found.get(f, set()))
        for f, allowed in _GRANDFATHERED_DIRECT_INSTANTIATION.items()
        if allowed - found.get(f, set())
    }
    assert not stale, (
        "Stale allow-list entries — these were migrated/removed; delete them "
        "from _GRANDFATHERED_DIRECT_INSTANTIATION so the residual stays honest:\n"
        + "\n".join(f"  {f}: {cs}" for f, cs in sorted(stale.items()))
    )


def test_no_new_get_system_db_callers():
    """No NEW ``get_system_db()`` caller outside repo/db infra. Handlers must
    read system state through the backend-aware factory, not the always-DuckDB
    system-db getter."""
    callers = _get_system_db_caller_files()
    new = sorted(callers - _GRANDFATHERED_GET_SYSTEM_DB)
    assert not new, (
        "New get_system_db() caller(s) — on a Postgres instance this reads/writes "
        "the wrong backend. Use the src.repositories factory instead:\n" + "\n".join(f"  {f}" for f in new)
    )


def test_get_system_db_allowlist_has_no_stale_entries():
    callers = _get_system_db_caller_files()
    stale = sorted(_GRANDFATHERED_GET_SYSTEM_DB - callers)
    assert not stale, "Stale get_system_db allow-list entries — delete them to keep the residual honest:\n" + "\n".join(
        f"  {f}" for f in stale
    )


def test_no_new_depends_get_db_raw_sql():
    """No NEW handler may run raw state SQL on a ``Depends(_get_db)`` (always
    DuckDB) connection. Read/write system state through the backend-aware
    factory instead — on Postgres a raw conn.execute here hits the stale
    DuckDB system file (#518)."""
    found = scan_depends_get_db_raw_sql(_production_files())
    new = {
        f: sorted(fns - _GRANDFATHERED_DEPENDS_GET_DB_RAW_SQL.get(f, set()))
        for f, fns in found.items()
        if fns - _GRANDFATHERED_DEPENDS_GET_DB_RAW_SQL.get(f, set())
    }
    assert not new, (
        "New Depends(_get_db) + raw state-SQL handler(s) detected — route system "
        "state through the src.repositories factory (e.g. users_repo()), not a "
        "raw conn.execute on the always-DuckDB connection:\n"
        + "\n".join(f"  {f}: {fns}" for f, fns in sorted(new.items()))
    )


def test_depends_get_db_raw_sql_allowlist_has_no_stale_entries():
    """Every grandfathered (file, function) must still exist — when a handler is
    routed through the factory its entry must be deleted so the residual stays
    honest (the list shrinking to empty is 'this class fully migrated')."""
    found = scan_depends_get_db_raw_sql(_production_files())
    stale = {
        f: sorted(fns - found.get(f, set()))
        for f, fns in _GRANDFATHERED_DEPENDS_GET_DB_RAW_SQL.items()
        if fns - found.get(f, set())
    }
    assert not stale, (
        "Stale Depends(_get_db) raw-SQL allow-list entries — these were "
        "migrated/removed; delete them from _GRANDFATHERED_DEPENDS_GET_DB_RAW_SQL:\n"
        + "\n".join(f"  {f}: {fns}" for f, fns in sorted(stale.items()))
    )


def test_no_new_raw_state_sql_outside_repositories():
    """No NEW raw ``.execute(<SQL naming a PG-backed state table>)`` outside
    the repository layer — no matter how the connection was obtained (this is
    the helper-param blind spot of detectors #1-#3). Move the SQL into the
    repo pair and route the call site through the src.repositories factory."""
    found = scan_raw_state_sql(_production_files())
    new = {
        f: sorted(fns - _GRANDFATHERED_RAW_STATE_SQL.get(f, set()))
        for f, fns in found.items()
        if fns - _GRANDFATHERED_RAW_STATE_SQL.get(f, set())
    }
    assert not new, (
        "New raw state-SQL site(s) outside src/repositories/ — on a Postgres "
        "instance these read/write the wrong backend regardless of which conn "
        "they run on. Move the SQL into the repo pair (X.py + X_pg.py) and use "
        "the factory:\n" + "\n".join(f"  {f}: {fns}" for f, fns in sorted(new.items()))
    )


def test_raw_state_sql_allowlist_has_no_stale_entries():
    found = scan_raw_state_sql(_production_files())
    stale = {
        f: sorted(fns - found.get(f, set()))
        for f, fns in _GRANDFATHERED_RAW_STATE_SQL.items()
        if fns - found.get(f, set())
    }
    assert not stale, (
        "Stale raw-state-SQL allow-list entries — these were migrated/removed; "
        "delete them from _GRANDFATHERED_RAW_STATE_SQL:\n"
        + "\n".join(f"  {f}: {fns}" for f, fns in sorted(stale.items()))
    )


# ---------------------------------------------------------------------------
# meta-tests: prove the detector actually catches violations
# ---------------------------------------------------------------------------


def test_detector_flags_a_planted_violation(tmp_path):
    """A synthetic module that constructs a backend-aware repo directly must be
    flagged — guards against the detector silently matching nothing."""
    classes = backend_aware_repo_classes()
    assert "UserRepository" in classes, "expected a known backend-aware repo class"
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from src.repositories.users import UserRepository\n"
        "def handler(conn):\n"
        "    return UserRepository(conn).get_by_id('x')\n"
    )
    found = scan_direct_instantiations([planted], classes)
    assert any("UserRepository" in hits for hits in found.values()), (
        "detector failed to flag a planted direct instantiation"
    )


def test_raw_sql_detector_flags_a_planted_violation(tmp_path):
    """A helper that receives a conn as a plain parameter and runs raw state
    SQL must be flagged — this is exactly the pattern detectors #1-#3 miss."""
    planted = tmp_path / "planted_helper.py"
    planted.write_text(
        "def resolve(conn, user_id):\n"
        '    return conn.execute("SELECT id, email FROM users WHERE id = ?", [user_id]).fetchone()\n'
    )
    found = scan_raw_state_sql([planted])
    assert any("resolve" in fns for fns in found.values()), (
        "detector #4 failed to flag a planted helper-param raw state-SQL site"
    )


def test_raw_sql_detector_ignores_non_state_sql(tmp_path):
    """Analytics SQL (parquet views, ATTACH, non-state tables) must NOT be flagged."""
    clean = tmp_path / "clean_analytics.py"
    clean.write_text(
        "def rebuild(conn):\n"
        "    conn.execute(\"CREATE VIEW v AS SELECT * FROM read_parquet('x.parquet')\")\n"
        '    conn.execute("SELECT event_date FROM web_sessions_example LIMIT 5")\n'
    )
    found = scan_raw_state_sql([clean])
    assert not found, f"analytics SQL wrongly flagged: {found}"


def test_detector_ignores_factory_call(tmp_path):
    """The factory function form (``users_repo()``) must NOT be flagged."""
    classes = backend_aware_repo_classes()
    clean = tmp_path / "clean.py"
    clean.write_text(
        "from src.repositories import users_repo\ndef handler():\n    return users_repo().get_by_id('x')\n"
    )
    found = scan_direct_instantiations([clean], classes)
    assert not found, f"factory call wrongly flagged: {found}"


# ---------------------------------------------------------------------------
# AC-G-noinject: chat sandbox spawn env must never carry the real Anthropic
# key or Agnes token — both are brokered via short-lived tickets pushed over
# stdin (2026-07-14 secret-broker hardening) instead of injected into the
# sandbox process environment.
# ---------------------------------------------------------------------------


def test_no_real_secret_in_sandbox_spawn_env():
    """Static scan of the two functions that build a chat sandbox's env: the
    real ``ANTHROPIC_API_KEY``/``AGNES_TOKEN`` values must never be assigned
    into that env dict — only a dummy (or absence) is allowed."""
    targets = [
        (REPO_ROOT / "app/chat/manager.py", "_spawn_runner"),
        (REPO_ROOT / "app/chat/runner.py", "_agnes_mcp_servers"),
    ]
    for path, func_name in targets:
        src = path.read_text()
        tree = ast.parse(src)
        matches = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
        ]
        assert matches, f"expected a function named {func_name!r} in {path}"
        seg = ast.get_source_segment(src, matches[0])
        assert seg is not None
        assert 'os.environ.get("ANTHROPIC_API_KEY"' not in seg or "dummy" in seg.lower(), (
            f"{path}:{func_name} must not forward the real ANTHROPIC_API_KEY into the sandbox env"
        )
        assert '"AGNES_TOKEN": token' not in seg, f"{path}:{func_name} must not inject the real AGNES_TOKEN"
