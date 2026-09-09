"""Seed the internal-source rows into ``table_registry`` at startup.

The internal connector has no extraction step (data lives in
``system.duckdb``), so the rows can't be created via the usual admin
``POST /api/admin/register-table`` flow. Instead, we idempotently insert
them on app boot — same pattern Agnes uses for the seeded ``Admin`` /
``Everyone`` groups.

Idempotency: ``TableRegistryRepository.register`` uses
``ON CONFLICT (id) DO UPDATE`` so re-running this on every startup just
re-applies the canonical description / display name (operators can't
accidentally edit the rows away).

Backend-agnostic: the repo is resolved from the ``src.repositories``
factory, so the seed lands in whichever backend (DuckDB or Postgres) the
deployment runs on.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from connectors.internal.access import INTERNAL_TABLES, InternalTable
from src.repositories import data_packages_repo, table_registry_repo, use_pg

logger = logging.getLogger(__name__)

#: Internal tables whose PHYSICAL source table exists only on the Postgres
#: app-state backend. ``usage_turns`` arrived after the A3 freeze (Alembic
#: revision ``0094_usage_turns``; the DuckDB ladder deliberately has no
#: matching ``_vN_to_v(N+1)`` step), so on a DuckDB-backed instance a
#: ``table_registry`` row for it would advertise a table that is not there —
#: /catalog would list it and every query against it would fail. The id is
#: therefore not registered at all there, and an existing row is pruned.
#: ``extraction_runs`` (Alembic ``0094_extraction_runs``) and
#: ``facts_ingest_runs`` (Alembic ``0078_facts_ingest_runs``) are the same
#: story — both are PG-only app-state tables added after the freeze.
#: ``agnes_issues`` / ``agnes_issue_comments`` (issue reporting, step 1;
#: Alembic ``0114_issue_reports``) are the same story too.
PG_ONLY_INTERNAL_TABLE_IDS: frozenset[str] = frozenset(
    {
        "agnes_turns",
        "agnes_extraction_runs",
        "agnes_facts_ingest_runs",
        "agnes_issues",
        "agnes_issue_comments",
    }
)

#: Stable identity of the seeded package that carries the internal tables.
#: The slug — not the name or the generated id — is what grants, tests and
#: operator docs refer to, so it must never change.
USAGE_PACKAGE_SLUG = "agnes-usage"
USAGE_PACKAGE_NAME = "Agnes Usage"
USAGE_PACKAGE_DESCRIPTION = (
    "Your own Agnes usage data — Claude Code sessions, tool and skill "
    "telemetry, and the audit trail of actions performed against this "
    "instance — plus, for admins, the extraction and fact-ingest pipelines' "
    "own operational history. Members see only their own rows on the "
    "usage tables; admins see everything, including the two admin-only "
    "operational tables no member row ever appears on."
)
# The guidance fields below are what the catalog UI and any LLM (chat agent,
# local Claude Code) read to decide WHEN and HOW to use these tables — keep
# them accurate over decorative. Server-side only is the load-bearing fact:
# these tables never enter `agnes pull` manifests.
USAGE_PACKAGE_LONG_DESCRIPTION = (
    "Self-service usage analytics over your own Agnes activity, plus (admins "
    "only) the extraction pipelines' own operational history. Eight tables: "
    "`agnes_sessions` (one row per Claude Code or chat session — activity "
    "counters plus summed input/output/cache tokens), `agnes_turns` (one row "
    "per assistant turn with exact token usage incl. prompt cache; "
    "Postgres-backed instances only), `agnes_telemetry` (one row per "
    "tool/skill/sub-agent/MCP event), `agnes_audit` (server-side audit "
    "trail of your actions), `agnes_issues` (one row per problem, wrong "
    "answer, missing thing or question you reported through 'Report a "
    "problem', `agnes issue report` or the report_issue tool; Postgres-"
    "backed instances only), `agnes_issue_comments` (public replies on your "
    "issue reports, from you or an admin; Postgres-backed instances only), "
    "`agnes_extraction_runs` (one row per built-in extraction/crawl run — "
    "status, progress, LLM token usage; Postgres-backed instances only) and "
    "`agnes_facts_ingest_runs` (one row per fact-graph ingest batch, "
    "including the real LLM spend ledger; Postgres-backed instances only). "
    "The first six are filtered to YOUR rows — admins see everyone. The "
    "last two are admin/operator data, not a per-user table: they carry no "
    "row for anyone (not even an admin's own activity) unless the caller is "
    "an admin, because a run belongs to a connection or a set of "
    "collections, never to a person. All eight tables are server-side only: "
    'query them with `agnes query "SELECT …"` (auto-routes to the server); '
    "they never appear in `agnes pull` and have no local parquet. New "
    "activity is visible within seconds of a session upload, chat turn or "
    "issue report."
)
USAGE_PACKAGE_WHEN_TO_USE = [
    "Analyzing your own token spend — by day, model, session, or turn",
    "Understanding prompt-cache efficiency (cache_read vs fresh input tokens)",
    "Auditing which tools, skills and MCP servers you actually use, and which fail",
    "Reviewing your own action history on this instance (agnes_audit)",
    "Admins: auditing an extraction run's or a fact-ingest batch's true LLM cost (agnes_extraction_runs.usage, agnes_facts_ingest_runs.llm_usage) instead of trusting one dashboard's arithmetic",
]
USAGE_PACKAGE_WHEN_NOT_TO_USE = [
    "Team- or instance-wide reporting — you only see your own rows; admins use /admin/telemetry and /admin/adoption",
    "Local analysis of these tables via `agnes pull` — they are server-side only; use `agnes query` instead",
    "Reading conversation content — transcripts are admin-only; these tables hold metadata and token counts",
    "A non-admin looking for extraction/ingest run history — agnes_extraction_runs and agnes_facts_ingest_runs return zero rows for anyone who is not an admin, regardless of this grant",
]
USAGE_PACKAGE_EXAMPLE_QUESTIONS = [
    "How many tokens did I spend this week, split by model?",
    "Which of my sessions used the most output tokens this month?",
    "What share of my input tokens was served from the prompt cache?",
    "Which tools error most often in my sessions?",
    "Admin: what did the last SharePoint extraction run actually spend on LLM tokens?",
    "What issues have I reported that are still open?",
    "What changed on my issue #42 since yesterday?",
]
USAGE_PACKAGE_TAGS = ["usage", "tokens", "telemetry", "audit", "extraction", "facts", "issues"]


def internal_table_available(table_id: str) -> bool:
    """True when *table_id* can exist in ``table_registry`` on THIS instance.

    False only for a :data:`PG_ONLY_INTERNAL_TABLE_IDS` member while the
    active app-state backend is DuckDB. Callers that need to explain a denial
    (``src.rbac.table_not_in_stack_message``) use this to say "not available
    here" rather than pointing at a Data Package that cannot carry the table.
    """
    return table_id not in PG_ONLY_INTERNAL_TABLE_IDS or use_pg()


def registrable_internal_tables() -> tuple[InternalTable, ...]:
    """The internal tables this instance's backend can actually serve.

    Read from the module-level ``INTERNAL_TABLES`` at CALL time (never
    captured at import) so the set follows the active backend — and so a test
    that patches the tuple still sees its own value.
    """
    return tuple(t for t in INTERNAL_TABLES if internal_table_available(t.registry_id))


def ensure_internal_tables_registered() -> set[str]:
    """Insert / refresh the internal-source rows in ``table_registry``.

    Safe to call on every boot. Operators see these in /admin/tables
    flagged as ``source_type='internal'`` and can't accidentally delete
    them without an explicit admin action; the next boot puts them back.

    Also evicts stale internal-source rows whose id no longer matches
    ``INTERNAL_TABLES`` — used when an internal table is renamed
    (e.g. agnes_usage → agnes_telemetry). Without this the old row
    would linger in /catalog forever.

    The set is narrowed to :func:`registrable_internal_tables` — on a
    DuckDB-backed instance a Postgres-only id (``agnes_turns``) is neither
    registered nor kept: it drops out of ``canonical_ids`` too, so a row left
    behind by an instance that used to run on Postgres is pruned by the same
    eviction that handles a rename. A registered id whose source table does
    not exist is the one outcome to avoid — it would advertise the table in
    /catalog and fail on every read.

    Returns the ids this call inserted for the FIRST time (a row that did
    not exist beforehand). That set is the add-once key
    :func:`ensure_internal_package_seeded` reconciles on — see its
    docstring. An id whose registration raised is never reported.
    """
    repo = table_registry_repo()
    registrable = registrable_internal_tables()
    canonical_ids = [t.registry_id for t in registrable]
    try:
        repo.delete_internal_except(canonical_ids)
    except Exception:
        logger.exception(
            "ensure_internal_tables_registered: stale-row cleanup failed; "
            "renamed internal tables may still appear under their old ids"
        )
    newly_registered: set[str] = set()
    for table in registrable:
        # Read BEFORE the upsert: `register` is ON CONFLICT (id) DO UPDATE,
        # so afterwards a first insert is indistinguishable from the boot-th
        # refresh (it even resets `registered_at`). "Did the row exist?" is
        # the only durable first-seen signal available without new schema.
        try:
            existed = repo.get(table.registry_id) is not None
        except Exception:
            # Can't tell — assume it existed. Being wrong this way skips a
            # package member; being wrong the other way would re-add one an
            # admin removed, which is the failure that actually harms.
            logger.exception(
                "ensure_internal_tables_registered: could not probe %s; treating it as already registered",
                table.registry_id,
            )
            existed = True
        try:
            repo.register(
                id=table.registry_id,
                name=table.display_name,
                description=table.description,
                source_type="internal",
                # `bucket` is a display-only label here, shown verbatim
                # in admin surfaces (e.g. next to source_type on
                # /admin/sync), so a more readable string than the
                # lowercase "agnes" is worth setting. It feeds no
                # analyst-facing grouping: the synthetic "Agnes
                # Internal" card on Data Packages was removed in #333;
                # since the seeded `agnes-usage` package (below) the
                # tables are packageable like any other registered row.
                bucket="Agnes Internal",
                source_table=table.source_table,
                query_mode="internal",
                profile_after_sync=False,
                registered_by="system_seed",
            )
        except Exception:
            # Logged but not fatal — startup must continue even if the
            # registry insert glitches (e.g. on a half-migrated DB).
            logger.exception(
                "ensure_internal_tables_registered: failed to register %s",
                table.registry_id,
            )
        else:
            if not existed:
                newly_registered.add(table.registry_id)
    return newly_registered


def ensure_internal_package_seeded(*, newly_registered: Collection[str] | None = None) -> None:
    """Seed the ``agnes-usage`` data package that carries the internal tables.

    Call right after :func:`ensure_internal_tables_registered` — the junction
    has an FK onto ``table_registry``, so the rows must exist first.

    Internal tables are reachable through a package like any other table, so
    an admin controls who may query usage data by granting this package. The
    row-level filter is unchanged and independent: a member sees only their
    own rows, an admin sees everything.

    Four things this must never do:

    * **Duplicate.** Creation is keyed on the stable slug, not the name.
    * **Resurrect.** A soft-deleted package stays deleted — the delete was an
      admin decision and ``POST /api/admin/data-packages/{id}/restore`` is the
      way back. Detected via ``get_by_slug(..., include_deleted=True)``,
      because the slug is UNIQUE across live and deleted rows alike, so a
      missing *live* row alone cannot distinguish the two cases.
    * **Re-add a member an admin removed.** Membership is add-once per id.
    * **Offer a table this backend cannot serve.** Membership follows
      :func:`registrable_internal_tables`, so a Postgres-only id never
      reaches the FK on a DuckDB instance.

    The add-once mechanism: an id is offered to an existing package **only on
    the boot that first inserted its ``table_registry`` row** (what
    *newly_registered* carries). Every later boot passes an empty set for it,
    so a junction row an admin deleted is never written again. A brand-new
    internal table shipped by a later release is registered for the first time
    on the upgrade boot and therefore joins the package exactly once. The
    durable fact behind it is the existence of the table's registry row, which
    needs no bookkeeping table of its own.

    Default ``newly_registered=None`` means "reconcile nothing" — the safe
    direction: a caller with no information adds no members rather than
    resurrecting removed ones.

    Never fatal: any failure is logged and startup continues without the
    package (the tables stay registered, only ungranted).
    """
    fresh = set(newly_registered or ())
    try:
        repo = data_packages_repo()
        pkg = repo.get_by_slug(USAGE_PACKAGE_SLUG)
        if pkg is None:
            if repo.get_by_slug(USAGE_PACKAGE_SLUG, include_deleted=True) is not None:
                logger.debug(
                    "ensure_internal_package_seeded: '%s' is soft-deleted; leaving it deleted",
                    USAGE_PACKAGE_SLUG,
                )
                return
            pkg_id = _create_usage_package(repo)
            if pkg_id is None:
                return
            # First creation owns the full membership — of the tables this
            # backend actually registered. The junction has an FK onto
            # ``table_registry``, so offering an unregistered Postgres-only id
            # here would just log a constraint violation every boot.
            member_ids = [t.registry_id for t in registrable_internal_tables()]
        else:
            pkg_id = pkg["id"]
            member_ids = [t.registry_id for t in registrable_internal_tables() if t.registry_id in fresh]
        for table_id in member_ids:
            # Per-member so one failure doesn't cost the others their only
            # chance: add-once means a member skipped here is never retried.
            try:
                repo.add_table(pkg_id, table_id, added_by="system_seed")
            except Exception:
                logger.exception(
                    "ensure_internal_package_seeded: could not add %s to '%s'; "
                    "add it from /admin/data-packages if it is still missing",
                    table_id,
                    USAGE_PACKAGE_SLUG,
                )
    except Exception:
        logger.exception(
            "ensure_internal_package_seeded: seeding the '%s' package failed; continuing",
            USAGE_PACKAGE_SLUG,
        )


def _create_usage_package(repo) -> str | None:
    """Create the package, tolerating a lost slug race.

    Role-split deployments boot api / gateway / worker against one database at
    the same time, so two processes can reach the create together. The UNIQUE
    constraint on ``slug`` decides; the loser re-resolves rather than logging a
    scary traceback for a state that is in fact correct. ``None`` means "give
    up quietly this boot".
    """
    try:
        return str(
            repo.create(
                name=USAGE_PACKAGE_NAME,
                slug=USAGE_PACKAGE_SLUG,
                description=USAGE_PACKAGE_DESCRIPTION,
                icon=None,
                color=None,
                created_by="system_seed",
                status="prod",
                publisher_kind="organization",
                long_description=USAGE_PACKAGE_LONG_DESCRIPTION,
                when_to_use=USAGE_PACKAGE_WHEN_TO_USE,
                when_not_to_use=USAGE_PACKAGE_WHEN_NOT_TO_USE,
                example_questions=USAGE_PACKAGE_EXAMPLE_QUESTIONS,
                tags=USAGE_PACKAGE_TAGS,
            )
        )
    except Exception:
        winner = repo.get_by_slug(USAGE_PACKAGE_SLUG)
        if winner is not None:
            logger.debug(
                "ensure_internal_package_seeded: '%s' created concurrently; using the existing row",
                USAGE_PACKAGE_SLUG,
            )
            # The winner seeded the full membership; nothing to add here.
            return None
        raise
