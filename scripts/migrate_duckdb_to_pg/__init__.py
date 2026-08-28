"""One-shot DuckDB → Postgres data migration.

Usage:

    # Dry-run all tasks (no PG writes)
    python -m scripts.migrate_duckdb_to_pg --dry-run

    # Live migration of every registered table
    python -m scripts.migrate_duckdb_to_pg

    # Just one table, then validate
    python -m scripts.migrate_duckdb_to_pg --only users --validate

The framework:

  - :func:`build_task_list` iterates ``Base.metadata.sorted_tables`` and
    returns a :class:`~tasks.GenericCopyTask` for every table, substituting
    an explicit override from :data:`~tasks.EXPLICIT_TASKS` when one exists.
  - Each task's ``.run()`` selects rows from DuckDB and INSERTs into PG with
    ``ON CONFLICT DO NOTHING`` so re-runs are idempotent.
  - Each task's ``.validate()`` compares row counts + a SHA-256 checksum over
    the PK column set — bit-for-bit equality is too noisy given timestamp
    precision differences, but PK-set equality + count parity is the
    strongest practical signal.

Backwards-compatible shim layer:
  The public names ``MigrationTask``, ``TASKS``, ``run_task``,
  ``validate_task``, and ``run_all`` are preserved so that existing tests
  and operator scripts continue to work unchanged.  ``MigrationTask`` is an
  alias for :class:`~tasks.GenericCopyTask`.  ``TASKS`` is the full ordered
  list produced by :func:`build_task_list`.

After cutover, the DuckDB ``system.duckdb`` file becomes a one-time
snapshot — never written to again. Analytics keep using their own
``analytics.duckdb`` and ``extract.duckdb`` files, unaffected.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from sqlalchemy.engine import Engine

from scripts.migrate_duckdb_to_pg.tasks import EXPLICIT_TASKS, GenericCopyTask
from scripts.migrate_duckdb_to_pg.tasks import _build_insert as _build_insert
from scripts.migrate_duckdb_to_pg.tasks import _checksum as _checksum

# re-exported for any external consumers
from scripts.migrate_duckdb_to_pg.tasks import _JSON_COLUMNS as _JSON_COLUMNS
from scripts.migrate_duckdb_to_pg.tasks import _normalize_for_pg as _normalize_for_pg
from scripts.migrate_duckdb_to_pg.tasks import _resolved_columns as _resolved_columns
from src.sql_ident import quote_ident

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type alias — kept for backwards compatibility
# ---------------------------------------------------------------------------

#: Alias so ``from scripts.migrate_duckdb_to_pg import MigrationTask`` keeps
#: working in the existing tests.
MigrationTask = GenericCopyTask


# ---------------------------------------------------------------------------
# PK column map — drives validate_task for every table
# ---------------------------------------------------------------------------

# Composite PKs can't be inferred from Base.metadata cheaply at runtime
# (the inspector would need a live connection), so we maintain an explicit
# map for tables whose PK is NOT a single column named "id".
_PK_COLUMNS: Dict[str, List[str]] = {
    "chat_broker_tickets": ["token"],
    "user_group_members": ["user_id", "group_id"],
    "sync_state": ["table_id"],
    "instance_templates": ["key"],
    "view_ownership": ["view_name"],
    "column_metadata": ["table_id", "column_name"],
    "data_package_semantic_models": ["package_id", "model_id"],
    "bq_metadata_cache": ["table_id"],
    "user_sync_settings": ["user_id", "dataset"],
    "table_profiles": ["table_id"],
    "telegram_links": ["user_id"],
    "pending_codes": ["code"],
    "session_processor_state": ["processor_name", "session_file"],
    "session_extraction_state": ["session_file"],
    "usage_session_summary": ["session_file"],
    "usage_tool_daily": ["day", "tool_name", "source"],
    "usage_marketplace_item_daily": ["day", "source", "type", "parent_plugin", "name"],
    "usage_marketplace_item_window": ["period_label", "source", "type", "parent_plugin", "name"],
    "marketplace_plugins": ["marketplace_id", "name"],
    "user_store_installs": ["user_id", "entity_id"],
    "store_entity_votes": ["entity_id", "user_id"],
    "store_lint_dismissals": ["entity_id", "rule_id"],
    "store_lint_entity_state": ["entity_id"],
    "user_plugin_optouts": ["user_id", "marketplace_id", "plugin_name"],
    "knowledge_item_relations": ["item_a_id", "item_b_id", "relation_type"],
    "knowledge_votes": ["item_id", "user_id"],
    "knowledge_item_user_dismissed": ["user_id", "item_id"],
    "knowledge_item_domains": ["item_id", "domain_id"],
    "data_package_tables": ["package_id", "table_id"],
    "user_stack_subscriptions": ["user_id", "resource_type", "resource_id"],
    # v63-v67 MCP / Cowork tables
    "tool_registry": ["tool_id"],
    "tool_grants": ["tool_id", "group_id"],
    "mcp_secrets": ["source_id"],
    "mcp_user_secrets": ["source_id", "user_id"],
    "system_secrets": ["name"],
    "data_package_tools": ["package_id", "tool_id"],
    # v68 cloud-chat tables (chat_sessions / chat_messages use id PK)
    "user_workdirs": ["user_email"],
    # v81 memory-mining consent — PK is the user's email, not an id.
    "memory_mining_consent": ["user_email"],
    # v79 named source connections (source_connections uses id PK)
    "connection_secrets": ["connection_id"],
    # v80 OAuth 2.1 MCP connector — non-`id` primary keys.
    "oauth_clients": ["client_id"],
    "oauth_auth_codes": ["code"],
    "oauth_access_tokens": ["token"],
    "oauth_refresh_tokens": ["token"],
    # v92 chat-driven onboarding — journey state keyed by user_id.
    "user_journey_state": ["user_id"],
    # v96 agent profiles + agent-as-API — composite PKs.
    "agent_scope": ["agent_id", "item_type", "item_id"],
    "idempotency_keys": ["key", "owner_user_id", "agent_id"],
    # v109 outbound MCP OAuth data layer — non-`id` primary keys.
    "mcp_source_oauth_clients": ["source_id"],
    "mcp_user_oauth_tokens": ["source_id", "user_id"],
    "mcp_oauth_flows": ["nonce"],
    # fact-graph-over-Collections §6 prerequisite — PG-only, no DuckDB
    # source table at all (see GenericCopyTask's "table absent in DuckDB"
    # handling); PK is the mapped corpus_files.id, not a generated "id".
    "corpus_file_sources": ["corpus_file_id"],
    # fact-graph facts tables (0076) — likewise PG-only with no DuckDB
    # source; registered here so validate() never falls back to SELECT id.
    "fact_aliases": ["type", "natural_key"],
    "corrections": ["subject_kind", "subject_id"],
    # external SSO login (0079) — PG-only with no DuckDB source; the
    # identity table's PK IS the bound user (one external identity per
    # user), and sso_config keeps its default "id" PK.
    "user_external_identities": ["user_id"],
}


# ---------------------------------------------------------------------------
# Task builder
# ---------------------------------------------------------------------------


def build_task_list() -> List[GenericCopyTask]:
    """Return migration tasks for every PG table, ordered by FK depth.

    Uses ``Base.metadata.sorted_tables`` for topological ordering.  Each
    table gets a :class:`GenericCopyTask`; tables in :data:`EXPLICIT_TASKS`
    use their registered override instead.
    """
    import src.models  # noqa: F401 — ensure all models are registered
    from src.db_pg import Base

    tasks: List[GenericCopyTask] = []
    for table in Base.metadata.sorted_tables:
        explicit = EXPLICIT_TASKS.get(table.name)
        if explicit is not None:
            tasks.append(explicit)
        else:
            pk_cols = _PK_COLUMNS.get(table.name, ["id"])
            tasks.append(GenericCopyTask(table_name=table.name, pk_columns=pk_cols))
    return tasks


# ---------------------------------------------------------------------------
# Public interface: all_table_names_handled
# ---------------------------------------------------------------------------


def all_table_names_handled() -> set[str]:
    """Names of PG tables this script can migrate.

    Used by
    ``tests/db_pg/test_data_migration.py::test_every_pg_model_has_a_migration_task``
    to catch new models added without migration coverage.  Returns the full
    set regardless of :data:`EXPLICIT_TASKS` — every table in
    ``Base.metadata`` is reachable (either via an explicit override or the
    generic copy loop).
    """
    import src.models  # noqa: F401
    from src.db_pg import Base

    return {t.name for t in Base.metadata.sorted_tables}


# ---------------------------------------------------------------------------
# Lazy TASKS list (backwards-compatible public attribute)
# ---------------------------------------------------------------------------

# Built once on first access via module-level assignment.  The existing tests
# do ``from scripts.migrate_duckdb_to_pg import TASKS`` and iterate it; the
# list must be fully populated at import time.
TASKS: List[GenericCopyTask] = build_task_list()


# ---------------------------------------------------------------------------
# Backwards-compatible shim functions (run_task / validate_task / run_all)
# ---------------------------------------------------------------------------


def run_task(
    task: GenericCopyTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    dry_run: bool = False,
) -> int:
    """Copy ``task.source_table`` from DuckDB into ``task.target_table`` in PG.

    Returns the number of rows considered.  ``ON CONFLICT (pk) DO NOTHING``
    may drop PK duplicates silently, so this is NOT the rows-inserted count.
    """
    return task.run(duck_conn, pg_engine, dry_run=dry_run)


def validate_task(
    task: GenericCopyTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
) -> Dict[str, Any]:
    """Compare row counts + PK-set checksums between DuckDB and PG."""
    return task.validate(duck_conn, pg_engine)


def reset_target_state_tables(pg_engine: Engine) -> int:
    """Empty every PG state table before a DuckDB→PG copy (B1 — retry safety).

    The copy uses bare ``INSERT … ON CONFLICT DO NOTHING`` (deliberately
    bare, to honour every UNIQUE constraint — see ``tasks._build_insert``).
    That makes a RETRY into a NON-EMPTY target silently keep the PREVIOUS
    attempt's content: any row edited in the source between a failed first
    attempt and the retry collides on its key, is skipped by ``DO NOTHING``,
    and keeps its stale value — and a ``COUNT(*)``-only verify can't see it
    because the counts still match.

    When the target is meant to be a fresh mirror of the source (an explicit
    one-time cutover), truncate it first: a first attempt truncates
    near-empty tables (only alembic's small seeds), a retry discards the
    partial/stale copy so the following INSERT lands every current value.
    Truncating all present tables in one statement (+ CASCADE) satisfies
    inter-table foreign keys.

    Only tables that actually EXIST in the target are truncated — a partial
    alembic state or a dropped table must not crash the reset; the copy that
    follows surfaces a genuinely-missing table as a per-table error.

    ⚠️ Callers must gate this on an explicit one-time-cutover intent. The
    docker-compose ``data-migrate`` one-shot re-runs on every ``compose up``
    and must NOT truncate live post-cutover data — hence ``run_all`` only
    resets when ``reset_target=True`` (the ``--reset-target`` CLI flag /
    the applier path), never by default.

    Returns the number of rows discarded.
    """
    import sqlalchemy as sa

    import src.models  # noqa: F401 — register every model on Base.metadata
    from src.db_pg import Base

    tables = list(Base.metadata.sorted_tables)
    if not tables:
        return 0
    with pg_engine.begin() as conn:
        existing = set(
            conn.execute(
                sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            ).scalars()
        )
        present = [t for t in tables if t.name in existing]
        discarded = 0
        for t in present:
            n = conn.execute(sa.text(f"SELECT COUNT(*) FROM {quote_ident(t.name)}")).scalar()
            discarded += int(n or 0)
        log.info(
            "reset target before DuckDB→PG copy: discarding %d pre-existing "
            "row(s) across %d PG table(s) so the copy starts from an empty "
            "mirror (no stale row can survive a retry — B1).",
            discarded,
            len(present),
        )
        if present:
            quoted = ", ".join(quote_ident(t.name) for t in present)
            conn.execute(sa.text(f"TRUNCATE {quoted} CASCADE"))
    return discarded


def run_all(
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    only: Optional[List[str]] = None,
    dry_run: bool = False,
    validate: bool = True,
    progress_callback: Optional[Any] = None,
    reset_target: bool = False,
) -> List[Dict[str, Any]]:
    """Run every registered task (or a subset by ``only``).

    Halt-on-first-failure semantics (H6): once any task's copy or
    validate step raises, subsequent tasks are NOT executed. They
    still produce a per-task entry — ``{"table": ..., "skipped":
    True, "reason": "halted after prior task failure"}`` — so callers
    see the full inventory at-a-glance, but no further INSERTs are
    issued against the target PG. The migrator's caller (``main()``)
    then refuses to flip_backend, so a partial-state PG never goes
    live.

    Per-task entry contract:
    - Copy failure: ``{"table": ..., "error": ...}``
    - Validate failure: ``{"table": ..., "error": ...}``
    - Skipped after prior failure: ``{"table": ..., "skipped": True, "reason": ...}``
    - Success with validate=True: full validate report (``duckdb_rows``,
      ``pg_rows``, ``checksum_match``, etc.).
    - Success with validate=False: ``{"table": ..., "ok": True}``.

    Callers like :func:`~scripts.db_state_migrator.copy_duckdb_to_pg`
    split reports into ok / err / skipped buckets by inspecting which
    of those keys is present.

    Idempotency: ``ON CONFLICT DO NOTHING`` means a successful retry
    after the operator fixes the failing table is safe — re-running
    overwrites nothing in already-migrated tables and resumes the
    halted ones.

    Optional ``progress_callback`` (C.1): called once per task as
    ``cb(target_table, tables_done, tables_total)`` BEFORE the task
    runs. ``tables_done`` is the count of tasks already attempted
    (0-indexed at first call); ``tables_total`` is the size of the
    selected task list. Halted tasks are still counted because the
    caller (e.g. JobWriter.update_table_progress) uses the value to
    drive the UI progress bar, which should reflect "we've made it
    this far through the inventory" rather than "this many succeeded".
    Keep the migrator subscript independently callable: when
    ``progress_callback`` is None the function behaves identically to
    pre-C.1.
    """
    # B1 — when the caller asserts a one-time cutover (``reset_target``),
    # empty the target first so the ``ON CONFLICT DO NOTHING`` copy can't
    # keep stale rows from a prior failed attempt. Never on by default: the
    # compose ``data-migrate`` one-shot re-runs every boot and must not
    # truncate live post-cutover data.
    if reset_target and not dry_run:
        reset_target_state_tables(pg_engine)

    selected = [t for t in TASKS if not only or t.target_table in only]
    total = len(selected)
    reports: List[Dict[str, Any]] = []
    halted = False
    for i, task in enumerate(selected):
        if progress_callback is not None:
            try:
                progress_callback(task.target_table, i, total)
            except Exception:
                # Progress reporting is best-effort — a broken callback
                # must not interrupt the migration. Log and continue.
                log.exception("progress_callback raised for %s", task.target_table)
        if halted:
            reports.append(
                {
                    "table": task.target_table,
                    "skipped": True,
                    "reason": "halted after prior task failure",
                }
            )
            continue
        try:
            run_task(task, duck_conn, pg_engine, dry_run=dry_run)
        except Exception as exc:
            log.exception("task %s failed: %s", task.source_table, exc)
            reports.append({"table": task.target_table, "error": str(exc)})
            halted = True
            continue
        if validate:
            try:
                reports.append(validate_task(task, duck_conn, pg_engine))
            except Exception as exc:
                log.exception("validate %s failed: %s", task.source_table, exc)
                reports.append({"table": task.target_table, "error": str(exc)})
                halted = True
        else:
            reports.append({"table": task.target_table, "ok": True})
    # Final ping so callers see done==total at the end of the loop.
    if progress_callback is not None and selected:
        try:
            progress_callback(selected[-1].target_table, total, total)
        except Exception:
            log.exception("progress_callback raised on final tick")
    return reports
