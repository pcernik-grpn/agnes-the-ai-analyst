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

from connectors.internal.access import INTERNAL_TABLES
from src.repositories import table_registry_repo

logger = logging.getLogger(__name__)


def ensure_internal_tables_registered() -> None:
    """Insert / refresh the internal-source rows in ``table_registry``.

    Safe to call on every boot. Operators see these in /admin/tables
    flagged as ``source_type='internal'`` and can't accidentally delete
    them without an explicit admin action; the next boot puts them back.

    Also evicts stale internal-source rows whose id no longer matches
    ``INTERNAL_TABLES`` — used when an internal table is renamed
    (e.g. agnes_usage → agnes_telemetry). Without this the old row
    would linger in /catalog forever.
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
    for table in INTERNAL_TABLES:
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
