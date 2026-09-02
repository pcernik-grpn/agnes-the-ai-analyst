"""Postgres-backed app state.

This module is the Postgres equivalent of ``src/db.py::get_system_db()``
for everything that's *not* analytics. Repositories under
``src/repositories/*_pg.py`` import ``Base`` to declare models and
``get_engine`` / ``get_session`` to obtain a connection.

The engine is a process-wide singleton (matching the DuckDB pattern at
``src/db.py:937-959``); the first call creates the pool, subsequent
calls reuse it. ``dispose()`` tears it down — used by tests for
per-test isolation.

URL resolution priority:
  1. ``DATABASE_URL`` environment variable (preferred; 12-factor convention)
  2. ``AGNES_DB_URL`` environment variable (deprecated alias — logs a warning)

No defaulting to ``sqlite:///./tmp.db`` or similar — a missing URL is a
configuration error, not something to paper over.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator, Optional, TypedDict

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for every Postgres-backed model in Agnes.

    SQLAlchemy 2.0 style — models use ``Mapped[...]`` + ``mapped_column``.
    Alembic's ``target_metadata`` in ``migrations/env.py`` is bound to
    ``Base.metadata``; autogenerate compares against it.
    """


_engine: Optional[sa.Engine] = None
_session_factory: Optional[sessionmaker] = None
_lock = threading.Lock()


def _resolve_url() -> str:
    """Return the Postgres URL using fallback chain:

      1. ``instance.yaml::database.url`` (admin-controlled, runtime-mutable).
      2. ``DATABASE_URL`` env var (12-factor convention).
      3. ``AGNES_DB_URL`` env var (deprecated alias — warning logged).

    Raises RuntimeError when no URL is configured.
    """
    import logging

    logger = logging.getLogger(__name__)

    # 1. instance.yaml
    try:
        from src.db_state_machine import read_backend_state

        _state, yaml_url = read_backend_state()
        if yaml_url:
            return yaml_url
    except Exception:
        # State module may be unavailable during early startup; fall
        # through to env vars.
        pass

    # 2. DATABASE_URL
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url

    # 3. AGNES_DB_URL (legacy)
    legacy = os.environ.get("AGNES_DB_URL")
    if legacy:
        logger.warning("AGNES_DB_URL is deprecated — rename to DATABASE_URL (12-factor convention)")
        return legacy

    raise RuntimeError(
        "Postgres URL is unset: set instance.yaml::database.url via /api/admin/db/migrate, or set DATABASE_URL env var"
    )


def get_engine() -> sa.Engine:
    """Return the process-wide Engine, creating it on first call.

    Connection pool tuning is conservative (5 + overflow 10) to match
    Cloud SQL's per-instance connection caps. Repository code holding
    sessions for long stretches should chunk work and release.
    """
    global _engine, _session_factory
    with _lock:
        if _engine is None:
            url = _resolve_url()
            _engine = sa.create_engine(
                url,
                future=True,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )
            _session_factory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
            # Attach the dev debug-toolbar query capture (idempotent; a no-op on
            # every non-debug request and in prod — see app/debug/postgres_panel.py).
            try:
                from app.debug.postgres_panel import instrument_engine

                instrument_engine(_engine)
            except Exception:
                pass
        return _engine


#: Env escape hatch — set to ``1`` to skip the startup Alembic revision
#: check and boot anyway. For emergency boots only (e.g. an operator
#: needs the app up to reach the admin UI / API and apply migrations by
#: hand). Mirrors the manual workaround in issue #636.
_SKIP_REVISION_CHECK_ENV = "AGNES_SKIP_PG_REVISION_CHECK"

#: Set to ``0`` to disable startup auto-migration and keep the fail-closed
#: behavior of ``assert_pg_at_head`` — for deployments whose pipeline owns
#: migrations (compose ``migrate`` one-shot, CI). Default is on: the PG
#: backend self-migrates at startup exactly like the DuckDB ladder does on
#: connect (issue #636).
_AUTO_MIGRATE_ENV = "AGNES_PG_AUTO_MIGRATE"

#: Fixed application-wide advisory-lock key serializing the startup
#: auto-migration across replicas (app + any sibling container sharing the
#: DB). Arbitrary constant — must simply never be reused for another lock
#: in this codebase.
_PG_MIGRATE_LOCK_KEY = 636_636_636_636


def _alembic_config():
    """Build the Alembic ``Config`` bound to this repo's ``alembic.ini``.

    ``script_location`` is set explicitly to the repo's ``migrations/``
    dir (matching ``migrations/env.py`` + the test fixtures) so the
    resolution is robust regardless of the process cwd at boot.
    """
    from pathlib import Path

    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return cfg


def _pg_revisions() -> tuple[Optional[str], Optional[str], bool]:
    """Return ``(db_current, script_head, db_ahead)``.

    ``db_current`` is the revision stamped in ``alembic_version`` (None
    when never stamped); ``script_head`` is the head of the migration
    scripts shipped in this image. ``db_ahead`` is True when the DB's
    revision is unknown to this image's scripts — either a genuine
    app-rollback (a newer image already migrated past this one) or a
    database stranded by a since-renumbered/deleted revision id (issue
    #2086). This function does not distinguish the two; ``assert_pg_at_head``
    does, using the repo's strict ``NNNN_name`` numbering (see
    ``_unknown_revision_is_stranded`` below) — the remedies are opposite
    (roll the image forward vs. apply the registered repair), so conflating
    them would send an operator the wrong way.
    """
    import sqlalchemy as sa
    from alembic.script import ScriptDirectory

    # Read the stamped revision with a plain SELECT rather than
    # MigrationContext.configure(): the latter logs two INFO lines on the
    # ``alembic.runtime.migration`` logger ("Context impl …" / "… transactional
    # DDL") every call, and the 30s ``/api/health`` probe calls this
    # continuously — thousands of noise lines/day drowning real app logs. A
    # never-stamped DB (``alembic_version`` table absent) reads as None,
    # matching ``get_current_revision()``'s old contract.
    #
    # ``scalar_one_or_none()`` (not ``scalar()``) keeps the old fail-closed
    # behavior on a divergent DB: an ``alembic_version`` with >1 row (multiple
    # heads / a botched manual stamp) raises rather than silently taking the
    # first row. And only SQLSTATE 42P01 (UndefinedTable) is swallowed to None —
    # psycopg maps 42501 (InsufficientPrivilege) onto the same ``ProgrammingError``
    # class, so a permission failure must re-raise and surface as "unreachable"
    # rather than a masked "never stamped" (which would read as false drift).
    engine = get_engine()
    with engine.connect() as conn:
        try:
            current = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        except sa.exc.ProgrammingError as exc:
            if getattr(exc.orig, "sqlstate", None) != "42P01":
                raise
            current = None

    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()

    # A revision this image's migration scripts don't know means the DB is
    # AHEAD, not behind — the operator rolled the app back after a newer
    # image already migrated. The remedies differ, so say so.
    db_ahead = False
    if current is not None and current != head:
        try:
            script.get_revision(current)
        except Exception:
            db_ahead = True
    return current, head, db_ahead


def _revision_number_prefix(revision_id: str) -> Optional[int]:
    """Return the leading 4-digit numeric prefix of a strict ``NNNN_name``
    revision id (e.g. ``77`` for ``"0077_ontology_drafts"``), or ``None``
    when the id doesn't follow that shape (a raw hex id from a botched
    manual stamp, or any other id this repo never generated).
    """
    prefix = revision_id[:4]
    if len(revision_id) < 5 or revision_id[4] != "_" or not prefix.isdigit():
        return None
    return int(prefix)


def _unknown_revision_is_stranded(current: str, head: str) -> Optional[bool]:
    """Classify a DB revision this image's ``ScriptDirectory`` doesn't know.

    Returns ``True`` when the id's numeric prefix proves it CANNOT have come
    from a newer image (its prefix is <= this image's head prefix) — i.e. it
    was shipped once, under this exact string, and later renumbered or
    deleted out from under a database that had already applied it (issue
    #2086). Such a database is STRANDED, not ahead: no image, past or
    future, ships a script under that id any more.

    Returns ``False`` when the prefix is strictly greater than head's — the
    ordinary app-rollback case (a newer image already migrated past this
    one; rolling forward again is the fix).

    Returns ``None`` when either id's prefix cannot be parsed — this repo's
    numbering convention doesn't cover every historical id (a manually
    stamped hex value, for instance), and guessing either way risks sending
    an operator to the wrong remedy with false confidence.
    """
    current_prefix = _revision_number_prefix(current)
    head_prefix = _revision_number_prefix(head)
    if current_prefix is None or head_prefix is None:
        return None
    return current_prefix <= head_prefix


class _RenumberedRevisionRepair(TypedDict):
    """One ``RENUMBERED_REVISION_REPAIRS`` entry — see that map's docstring."""

    apply: tuple[str, ...]
    stamp: str


#: Registered repairs for a database stamped at a shipped-then-renumbered
#: revision id (issue #2086). Keyed by the STRANDED id — the string some
#: already-migrated database may still have in ``alembic_version``.
#: ``"apply"`` lists, in order, the CURRENT revision module(s) whose
#: ``upgrade()`` reproduces the DDL the stranded id's chain never applied
#: (the migration(s) that were inserted BEFORE it when it got renumbered);
#: ``"stamp"`` is the id that occupies the stranded id's old position in
#: the CURRENT chain — what ``alembic_version`` is re-stamped to once that
#: DDL has landed, since a database that has both is schema-equivalent to
#: one that walked the current chain in the current order.
#:
#: ``ensure_pg_at_head()`` applies a matching entry automatically at
#: startup; ``assert_pg_at_head()`` never does — it only names the repair
#: (see its STRANDED message). This map is append-only, like
#: ``migrations/shipped_revision_ids.txt``: a past renumbering incident
#: stays listed forever, because an operator's database may still be
#: sitting on the stranded id years later.
RENUMBERED_REVISION_REPAIRS: dict[str, _RenumberedRevisionRepair] = {
    "0077_facts_ingest_runs": _RenumberedRevisionRepair(
        apply=("0077_ontology_drafts",),
        stamp="0078_facts_ingest_runs",
    ),
}


def assert_pg_at_head() -> None:
    """Fail closed unless the Postgres DB is at the head Alembic revision.

    The DuckDB backend self-migrates on every connect (``src/db.py``
    ladder via ``get_system_db``); the Postgres backend does NOT —
    ``alembic upgrade head`` runs only from the compose ``migrate``
    one-shot and the ``/api/admin/db/migrate`` flow, both one-time. A
    fresh image that expects a newer revision against a PG stamped at an
    older one boots "healthy" but 500s every write touching a post-stamp
    column (issue #636). This converts that silent drift into an
    operator-visible boot refusal.

    Reads the DB's current revision (a plain ``SELECT`` from
    ``alembic_version`` via ``_pg_revisions()``) and the script head via
    ``ScriptDirectory.get_current_head()``. Raises
    ``RuntimeError`` when they disagree (including the never-stamped
    ``current is None`` case) naming both revisions and the manual
    remediation. A no-op when they match.

    A revision unknown to this image's scripts (``_pg_revisions()``'s
    ``db_ahead``) is further split by ``_unknown_revision_is_stranded``:
    a STRANDED database (a shipped id later renumbered/deleted out from
    under it — issue #2086) gets a distinct message naming the id and
    pointing at ``RENUMBERED_REVISION_REPAIRS`` / the docs/migrations.md
    runbook, never the AHEAD wording — rolling the app image forward or
    backward does not fix a stranded database, and telling an operator to
    do so wastes a deploy cycle finding that out. This function never
    applies a repair itself; only ``ensure_pg_at_head()`` does.

    Honors the ``AGNES_SKIP_PG_REVISION_CHECK=1`` escape hatch for
    emergency boots. This check is PG-only by design — DuckDB needs no
    equivalent because it self-migrates.
    """
    import logging

    logger = logging.getLogger(__name__)

    if os.environ.get(_SKIP_REVISION_CHECK_ENV) == "1":
        logger.warning(
            "%s=1 — skipping the Postgres Alembic revision check. "
            "The DB may be behind the app's expected schema; writes to "
            "newer columns/tables may 500. Apply `alembic upgrade head` "
            "(or the compose `migrate` one-shot) and unset this flag.",
            _SKIP_REVISION_CHECK_ENV,
        )
        return

    current, head, db_ahead = _pg_revisions()

    if current == head:
        return

    if db_ahead:
        # _pg_revisions() only sets db_ahead=True when current is a real,
        # non-None stamped value; head is None only for an empty script
        # directory, which never happens in a real checkout. Asserting here
        # (rather than widening the helper to Optional[str]) keeps the
        # narrowing local to the one call site that needs it.
        assert current is not None and head is not None
        stranded = _unknown_revision_is_stranded(current, head)

        if stranded:
            raise RuntimeError(
                f"Postgres schema is stamped at Alembic revision {current!r}, "
                "which this image's migration scripts do not contain — but "
                f"its numbering ({current[:4]!r} <= this image's head "
                f"{head[:4]!r}) rules out an app rollback, since no newer "
                "image could ship a lower-numbered head. This id existed in "
                "an earlier release's migration chain and was renumbered or "
                "removed since it shipped (issue #2086): the database is "
                "STRANDED, not AHEAD — no image, old or new, ships a script "
                "under this exact id any more, so rolling the app image "
                "forward or backward will not fix it. Check "
                f"RENUMBERED_REVISION_REPAIRS in src/db_pg.py for a "
                f"registered repair for {current!r} (ensure_pg_at_head() "
                "applies it automatically at startup), and see "
                'docs/migrations.md -> "A database stranded by a renumbered '
                'revision" for the manual recovery recipe if none is '
                "registered yet. Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot "
                "anyway (emergency only)."
            )

        stranded_hint = (
            " (this id also doesn't follow this repo's NNNN_name numbering, "
            "so it could instead be a database stranded by a past "
            "renumbering — issue #2086 — rather than a genuine rollback; "
            "check RENUMBERED_REVISION_REPAIRS in src/db_pg.py for a "
            "registered repair before assuming a rollback)"
            if stranded is None
            else ""
        )
        raise RuntimeError(
            "Postgres schema is AHEAD of the application: the DB is at "
            f"Alembic revision {current!r}, which this image's migration "
            f"scripts do not contain (its head is {head!r}) — typically an "
            "app rollback after a newer image already migrated (issue "
            f"#636){stranded_hint}. Roll the app image forward to one that "
            "knows this revision (preferred), or restore the DB backup "
            "matching this image. Set AGNES_SKIP_PG_REVISION_CHECK=1 to "
            "boot anyway (emergency only)."
        )

    current_label = current if current is not None else "<none — never stamped>"
    raise RuntimeError(
        "Postgres schema is behind the application: the DB is at Alembic "
        f"revision {current_label!r} but this image expects head {head!r}. "
        "Writes touching columns/tables added after the DB's revision will "
        "fail (issue #636). Apply the pending migrations before serving:\n"
        "  - one-shot:  alembic upgrade head\n"
        "  - compose:   docker compose -f docker-compose.postgres.yml run --rm migrate\n"
        "Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot anyway (emergency only)."
    )


#: SQLSTATEs meaning "the object this DDL statement would create already
#: exists" — duplicate table/index (Postgres treats an index as a relation
#: too, same code as a table), duplicate column, and the generic
#: duplicate-object class (named constraint, type, …). Caught ONLY while
#: applying a ``RENUMBERED_REVISION_REPAIRS`` entry's DDL, so a previous
#: half-applied repair attempt (crashed between statements) or a manual
#: operator fix (per the docs/migrations.md recipe) does not crash the boot
#: loop on retry.
_ALREADY_EXISTS_SQLSTATES = frozenset({"42P07", "42701", "42710"})


def _apply_renumbered_revision_repair(engine: sa.Engine, current: str) -> None:
    """Repair a database stamped at a shipped-then-renumbered id (#2086).

    Applies each ``RENUMBERED_REVISION_REPAIRS[current]["apply"]`` revision's
    ``upgrade()`` against a real connection, then atomically re-stamps
    ``alembic_version`` to the entry's ``stamp`` id. Called only from
    ``ensure_pg_at_head()``, under ``_PG_MIGRATE_LOCK_KEY`` — never from
    ``assert_pg_at_head()``, which only names this repair.

    Each ``upgrade()`` is bound to the connection via
    ``MigrationContext.configure(conn)`` + ``Operations.context(ctx)`` — the
    same mechanism ``alembic upgrade`` uses internally to install the global
    ``alembic.op`` proxy a revision module's ``upgrade()`` body calls into —
    run directly against the target revision's module rather than through
    ``alembic upgrade <rev>``, because that command resolves its *starting*
    point from the DB's own stamped revision, which here is the stranded id
    ``ScriptDirectory`` cannot locate at all.

    Idempotent against a partially-applied prior attempt: each listed
    revision runs inside its own SAVEPOINT, and a duplicate-object error
    (``_ALREADY_EXISTS_SQLSTATES`` — e.g. a previous boot got as far as
    creating the table before crashing, or an operator applied the DDL by
    hand) is logged and treated as "already done" rather than propagated.
    Any other error is a genuine failure and aborts the whole repair —
    the SAVEPOINT rolls back only that revision's own statements, so the
    surrounding transaction (and therefore the re-stamp below) never
    commits, leaving the DB at ``current`` for the next boot attempt or a
    manual fix rather than half-migrated.

    Narrow, documented edge case: if a crash landed BETWEEN two DDL
    statements inside one listed revision's own ``upgrade()`` (e.g. its
    table exists but an index the same revision also creates does not), the
    first statement's duplicate-object error is caught and the rest of that
    revision's body is skipped along with it — a partial object created by
    that specific revision is not independently detected or completed. This
    map is a one-off, cataloged repair for a known incident, not a general
    schema-diffing engine; a database caught in that narrower state is
    exactly what the manual recipe in docs/migrations.md is for.

    Raises ``RuntimeError`` if the final re-stamp does not affect exactly
    one row (expected to always affect exactly one — callers hold
    ``_PG_MIGRATE_LOCK_KEY`` for the whole operation, so a concurrent writer
    changing ``current`` from under it is not the ordinary case).
    """
    import logging

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    logger = logging.getLogger(__name__)
    repair = RENUMBERED_REVISION_REPAIRS[current]
    apply_ids = repair["apply"]
    stamp_id = repair["stamp"]

    logger.warning(
        "Postgres alembic_version is stamped at %r, a revision id that "
        "shipped in an earlier release and was renumbered to %r since "
        "(issue #2086). Applying the registered repair: running %s, then "
        "re-stamping to %r.",
        current,
        stamp_id,
        ", ".join(apply_ids),
        stamp_id,
    )

    script = ScriptDirectory.from_config(_alembic_config())
    with engine.begin() as conn:
        migration_ctx = MigrationContext.configure(conn)
        for rev_id in apply_ids:
            rev = script.get_revision(rev_id)
            try:
                with conn.begin_nested():
                    with Operations.context(migration_ctx):
                        rev.module.upgrade()
            except sa.exc.ProgrammingError as exc:
                if getattr(exc.orig, "sqlstate", None) not in _ALREADY_EXISTS_SQLSTATES:
                    raise
                logger.warning(
                    "Renumbered-revision repair: %r looks already applied "
                    "(duplicate-object error on retry) — skipping.",
                    rev_id,
                )

        result = conn.execute(
            sa.text("UPDATE alembic_version SET version_num = :new WHERE version_num = :old"),
            {"new": stamp_id, "old": current},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"Renumbered-revision repair for {current!r} applied its DDL "
                f"but could not re-stamp alembic_version to {stamp_id!r} "
                f"(matched {result.rowcount} row(s), expected exactly 1) — "
                "refusing to continue on a schema whose applied DDL and "
                "stamped revision may now disagree."
            )


def ensure_pg_at_head() -> None:
    """Bring the Postgres schema to the head Alembic revision at startup.

    Part 2 of issue #636. ``assert_pg_at_head`` turned silent
    write-breakage into a boot refusal; on deployments with no migrate
    step that refusal is a crash-loop on every release carrying a
    migration. This closes the loop — when the DB is BEHIND, apply the
    pending migrations in-process, mirroring the DuckDB ladder's
    self-migration on connect (``src/db.py``).

    Safety properties:

    - **AHEAD stays fail-closed.** A revision unknown to this image AND not
      covered by a registered repair means either an app rollback after a
      newer image migrated, or a stranded database with no repair on file
      yet; auto-rollback is never safe and an unregistered repair cannot be
      guessed, so this delegates to ``assert_pg_at_head`` and refuses to
      boot.
    - **A registered renumbering repair (issue #2086) is applied
      automatically.** When the DB's stamped revision matches a
      ``RENUMBERED_REVISION_REPAIRS`` key exactly, the repair runs (see
      ``_apply_renumbered_revision_repair``) before the normal
      behind-head upgrade, under the same advisory lock and re-check
      discipline as that upgrade. ``assert_pg_at_head`` itself never does
      this — it only names the repair.
    - **Replica-safe.** The upgrade (and any repair) runs under a
      session-scoped Postgres advisory lock (``_PG_MIGRATE_LOCK_KEY``) and
      re-checks the revision after acquiring it, so concurrent replicas
      serialize and the late acquirer no-ops instead of double-applying.
    - **Opt-out.** ``AGNES_PG_AUTO_MIGRATE=0`` restores the fail-closed
      check for pipeline-controlled deployments;
      ``AGNES_SKIP_PG_REVISION_CHECK=1`` still skips everything
      (emergency boots).
    - **Fail-closed on error.** If the upgrade itself fails (broken
      migration, missing DDL privileges), the boot aborts with the
      original remediation guidance — never serve on a half-migrated
      schema.
    """
    import logging

    logger = logging.getLogger(__name__)

    if os.environ.get(_SKIP_REVISION_CHECK_ENV) == "1":
        assert_pg_at_head()  # logs the skip warning and returns
        return
    if os.environ.get(_AUTO_MIGRATE_ENV, "1") == "0":
        assert_pg_at_head()
        return

    current, head, db_ahead = _pg_revisions()
    if current not in RENUMBERED_REVISION_REPAIRS and (current == head or db_ahead):
        assert_pg_at_head()  # no-op, or the AHEAD/STRANDED refusal
        return

    engine = get_engine()
    with engine.connect() as lock_conn:
        lock_conn.execute(
            sa.text("SELECT pg_advisory_lock(:key)"),
            {"key": _PG_MIGRATE_LOCK_KEY},
        )
        try:
            # Re-check under the lock — a sibling replica may have finished
            # the repair and/or upgrade while this one waited.
            current, head, db_ahead = _pg_revisions()
            if current in RENUMBERED_REVISION_REPAIRS:
                try:
                    _apply_renumbered_revision_repair(engine, current)
                except Exception as exc:
                    raise RuntimeError(
                        f"Automatic repair of renumbered revision {current!r} "
                        f"failed ({exc}); refusing to serve on a database "
                        "whose repair may be half-applied (issue #2086). "
                        'See docs/migrations.md -> "A database stranded by '
                        'a renumbered revision" for the manual recovery '
                        "recipe. Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot "
                        "anyway (emergency only)."
                    ) from exc
                # The repair re-stamped alembic_version to a real, known
                # id (the map's "stamp") — recompute so the ordinary
                # behind-head branch below picks up from there.
                current, head, db_ahead = _pg_revisions()

            if current != head and not db_ahead:
                logger.warning(
                    "Postgres schema is behind the application (%s -> %s) — "
                    "auto-applying pending Alembic migrations (set %s=0 to "
                    "disable and fail closed instead; issue #636).",
                    current if current is not None else "<never stamped>",
                    head,
                    _AUTO_MIGRATE_ENV,
                )
                from alembic import command

                cfg = _alembic_config()
                # migrations/env.py resolves the URL from cfg.attributes
                # first — pass the app-resolved URL explicitly so overlay
                # (instance.yaml) deployments work without DATABASE_URL in
                # the environment, and keep env.py's hands off the app's
                # already-configured logging.
                cfg.attributes["sqlalchemy.url"] = _resolve_url()
                cfg.attributes["configure_logger"] = False
                try:
                    command.upgrade(cfg, "head")
                except Exception as exc:
                    raise RuntimeError(
                        "Automatic Alembic upgrade to head failed "
                        f"({exc}); refusing to serve on a half-migrated "
                        "schema (issue #636). Apply the migrations "
                        "manually:\n"
                        "  - one-shot:  alembic upgrade head\n"
                        "  - compose:   docker compose -f "
                        "docker-compose.postgres.yml run --rm migrate\n"
                        "Set AGNES_SKIP_PG_REVISION_CHECK=1 to boot anyway "
                        "(emergency only)."
                    ) from exc
                logger.warning("Postgres schema auto-migrated to head %s.", head)
        finally:
            lock_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"),
                {"key": _PG_MIGRATE_LOCK_KEY},
            )

    assert_pg_at_head()


def dispose_engine() -> None:
    """Dispose the singleton engine + clear the cache.

    Next ``get_engine()`` call will re-resolve the URL and rebuild the
    engine. Called by ``POST /api/admin/db/migrate`` after a successful
    backend flip to make new repository operations land on the new
    backend without an app restart (though the app DOES restart on
    most migrations — this is a defence-in-depth runtime path).
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None
        _session_factory = None


@contextlib.contextmanager
def get_session() -> Iterator[Session]:
    """Yield a Session bound to the singleton engine.

    Commits or rolls back at exit; the session is always closed. Use
    this when you need transactional repository work.
    """
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose() -> None:
    """Drop the singleton engine and clear the session factory.

    Call between test runs or after a config reload. Production code
    does NOT call this on normal request paths — it's reserved for
    explicit lifecycle events.
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


#: Session-scoped PG advisory lock id for the startup seed block —
#: "AGNS" packed as an int, distinct from ``_PG_MIGRATE_LOCK_KEY``.
_SEED_LEASE_ID = 0x41474E53


def _lease_use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


@contextlib.contextmanager
def seed_lease() -> Iterator[None]:
    """Serialize the startup seed block across concurrently-booting replicas.

    Several replicas can reach the lifespan's seed block at once on a
    Postgres backend (e.g. a rolling deploy or a cold multi-replica
    boot). The seeds themselves are idempotent, but running them
    unserialized invites duplicate-insert races on tables without a
    unique constraint to lean on. This wraps the block in a session-scoped
    Postgres advisory lock: losers block until the winner finishes, then
    run the (idempotent) seeds themselves rather than skipping them —
    correctness over throughput, and startup-only so the extra latency
    is a one-time cost.

    No-op on the DuckDB backend — Task 2's startup guard already
    restricts DuckDB app-state to a single process, so there is nothing
    to serialize.
    """
    if not _lease_use_pg():
        yield
        return
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": _SEED_LEASE_ID})
        try:
            yield
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _SEED_LEASE_ID})


#: Session-scoped PG advisory lock id for the orchestrator rebuild critical
#: section — "AGNT" packed as an int, distinct from ``_SEED_LEASE_ID`` and
#: ``_PG_MIGRATE_LOCK_KEY``.
_REBUILD_LEASE_ID = 0x41474E54


@contextlib.contextmanager
def rebuild_lease() -> Iterator[None]:
    """Serialize ``SyncOrchestrator.rebuild()``/``rebuild_source()`` across processes.

    In a role-split topology (a dedicated ``api`` process handling
    ``/api/sync/trigger`` + the Jira webhook, and a separate ``worker``
    process running enqueued jobs) both processes can independently reach
    the orchestrator's rebuild critical section. ``SyncOrchestrator``'s
    ``_rebuild_lock`` (a ``threading.Lock``) only serializes rebuilds
    *within* one process — it is invisible across processes — so a
    job-triggered rebuild in the worker and an HTTP-triggered rebuild in
    the api process can concurrently ATTACH/swap ``analytics.duckdb``,
    which is the known DuckDB corruption class this repo guards against
    elsewhere (see ``docs/architecture.md``).

    This wraps the rebuild critical section in a session-scoped Postgres
    advisory lock, blocking (not failing) until the current holder
    finishes, so the caller can just wait its turn: the in-process
    ``_rebuild_lock`` still runs first (cheap, avoids reaching Postgres
    when nothing outside this process can contend), and this lease adds
    the cross-process guarantee.

    No-op on the DuckDB backend — DuckDB app-state deployments are
    single-process (Task 2's startup guard), so there is no second
    process to serialize against.
    """
    if not _lease_use_pg():
        yield
        return
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": _REBUILD_LEASE_ID})
        try:
            yield
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _REBUILD_LEASE_ID})
