"""sharepoint_connection_state.kind widened to accept 'acl_snapshot:<scope_id>'
rows (TCRD-296 gap #79 — SharePoint permissions captured as metadata for
every scope, independent of ``access_mode``).

Reuses the existing per-connection state table (``0096_sharepoint_
connection_state.py``, widened once already by ``0103_crawl_shards.py`` for
``'crawl:<state_key>'`` shard rows) rather than a new table: one row per
scope, keyed ``acl_snapshot:<source_scope_id>``, holding that scope's
informational permissions read (``connectors/sharepoint/acl_sync.py::
snapshot_principals``) — captured for MANUAL scopes too, unlike the grant-
mirroring half of the sync which only ever touches ``access_mode='mirrored'``
scopes. See ``src/repositories/sharepoint_state_pg.py`` (unchanged — ``kind``
is an opaque string to every method there) and ``connectors/sharepoint/
state_store.py`` for the PG-only posture (a DuckDB-backed instance simply
never writes this kind, same as the shard rows).

PG-first ratchet (A3): schema change on an already-PG-only app-state table,
Alembic-only — no matching DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION``
does not move.

Revision ID: 0108_sp_acl_snapshot_kind
Revises: 0107_merge_access_policy_revs
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0108_sp_acl_snapshot_kind"
down_revision: Union[str, None] = "0107_merge_access_policy_revs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KIND_CHECK = "kind IN ('crawl', 'facts') OR kind LIKE 'crawl:%'"
_NEW_KIND_CHECK = "kind IN ('crawl', 'facts') OR kind LIKE 'crawl:%' OR kind LIKE 'acl_snapshot:%'"


def upgrade() -> None:
    op.drop_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", type_="check")
    op.create_check_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", _NEW_KIND_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", type_="check")
    op.create_check_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", _OLD_KIND_CHECK)
