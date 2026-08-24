"""Canonical key resolution for ``sync_state.table_id`` (remediation B1).

Before this module existed, every ``sync_state`` writer keyed rows by the
table's raw name (``_meta.table_name`` for connector syncs, ``table_registry.
name`` for the materialized pass) while several admin-facing readers joined
``sync_state`` against ``table_registry`` on the ``id`` column instead. The
two agree only when a table's registry id happens to equal its display name
(the common case — a name already shaped like ``lower_snake_case``); a table
registered as ``"Web Sessions"`` (id ``web_sessions``) showed healthy sync
status on one admin surface and "never synced" on another, from the exact
same underlying data.

``resolve_sync_state_key`` is the single place a writer decides what to
persist in ``sync_state.table_id`` / ``sync_history.table_id``: the matching
``table_registry.id`` when the name resolves to a registered row, otherwise
the name unchanged (an unregistered or since-renamed table still gets a
sync_state row rather than losing its bookkeeping) — logged, since that row
will not agree with an id-keyed reader until the registry catches up.

The parquet artifact on disk is a SEPARATE, unrelated convention — the
extractor / materialize pass names files after the table's registry `name`,
not its `id` — so a reader that needs the actual bytes (the manifest builder,
the distribution mirror job) must resolve the registry row itself and use its
`name` field for the filename, never `sync_state.table_id` directly. This
module intentionally does not touch that; see `app/api/sync.py::_build_
manifest_for_user` and `app/worker/kinds.py::_run_distribution_mirror` for
the read-side half.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_sync_state_key_for_row(name: str, registry_row: Optional[dict[str, Any]]) -> str:
    """Pure resolver: given a table's name and its (possibly ``None``,
    already-looked-up) ``table_registry`` row, return the value a writer
    should persist as ``sync_state.table_id`` / ``sync_history.table_id``.

    Split out from :func:`resolve_sync_state_key` for callers that already
    hold the registry row (avoids a redundant lookup) — e.g. the
    materialized-sync loop iterates ``table_registry_repo().list_all()``
    directly, and the filesystem-fallback publish path already fetches the
    row to decide whether the table is materialized.
    """
    if registry_row and registry_row.get("id"):
        return registry_row["id"]
    logger.warning(
        "sync_state: no table_registry row named %r; writing sync_state.table_id "
        "under the name instead of the registry id (legacy fallback — this row "
        "will not agree with an id-keyed reader until it is registered)",
        name,
    )
    return name


def resolve_sync_state_key(name: str) -> str:
    """Resolve the ``sync_state.table_id`` to write for a table named
    *name* (a ``_meta.table_name`` / materialized registry row name), doing
    the ``table_registry`` lookup itself.

    See :func:`resolve_sync_state_key_for_row` for the resolution rule and
    the fallback's logging contract.
    """
    from src.repositories import table_registry_repo

    return resolve_sync_state_key_for_row(name, table_registry_repo().get_by_name(name))
