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
from typing import Collection, Optional

from connectors.internal.access import INTERNAL_TABLES
from src.repositories import data_packages_repo, table_registry_repo

logger = logging.getLogger(__name__)

#: Stable identity of the seeded package that carries the internal tables.
#: The slug — not the name or the generated id — is what grants, tests and
#: operator docs refer to, so it must never change.
USAGE_PACKAGE_SLUG = "agnes-usage"
USAGE_PACKAGE_NAME = "Agnes Usage"
USAGE_PACKAGE_DESCRIPTION = (
    "Your own Agnes usage data: Claude Code sessions, tool and skill "
    "telemetry, and the audit trail of actions performed against this "
    "instance. Members see only their own rows; admins see everything."
)


def ensure_internal_tables_registered() -> set[str]:
    """Insert / refresh the internal-source rows in ``table_registry``.

    Safe to call on every boot. Operators see these in /admin/tables
    flagged as ``source_type='internal'`` and can't accidentally delete
    them without an explicit admin action; the next boot puts them back.

    Also evicts stale internal-source rows whose id no longer matches
    ``INTERNAL_TABLES`` — used when an internal table is renamed
    (e.g. agnes_usage → agnes_telemetry). Without this the old row
    would linger in /catalog forever.

    Returns the ids this call inserted for the FIRST time (a row that did
    not exist beforehand). That set is the add-once key
    :func:`ensure_internal_package_seeded` reconciles on — see its
    docstring. An id whose registration raised is never reported.
    """
    repo = table_registry_repo()
    canonical_ids = [t.registry_id for t in INTERNAL_TABLES]
    try:
        repo.delete_internal_except(canonical_ids)
    except Exception:
        logger.exception(
            "ensure_internal_tables_registered: stale-row cleanup failed; "
            "renamed internal tables may still appear under their old ids"
        )
    newly_registered: set[str] = set()
    for table in INTERNAL_TABLES:
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
                # Internal" card on Data Packages was removed in #333,
                # and internal tables are excluded from all package
                # surfaces since.
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


def ensure_internal_package_seeded(*, newly_registered: Optional[Collection[str]] = None) -> None:
    """Seed the ``agnes-usage`` data package that carries the internal tables.

    Call right after :func:`ensure_internal_tables_registered` — the junction
    has an FK onto ``table_registry``, so the rows must exist first.

    Internal tables are reachable through a package like any other table, so
    an admin controls who may query usage data by granting this package. The
    row-level filter is unchanged and independent: a member sees only their
    own rows, an admin sees everything.

    Three things this must never do:

    * **Duplicate.** Creation is keyed on the stable slug, not the name.
    * **Resurrect.** A soft-deleted package stays deleted — the delete was an
      admin decision and ``POST /api/admin/data-packages/{id}/restore`` is the
      way back. Detected via ``get_by_slug(..., include_deleted=True)``,
      because the slug is UNIQUE across live and deleted rows alike, so a
      missing *live* row alone cannot distinguish the two cases.
    * **Re-add a member an admin removed.** Membership is add-once per id.

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
            # First creation owns the full membership.
            member_ids = [t.registry_id for t in INTERNAL_TABLES]
        else:
            pkg_id = pkg["id"]
            member_ids = [t.registry_id for t in INTERNAL_TABLES if t.registry_id in fresh]
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


def _create_usage_package(repo) -> Optional[str]:
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
