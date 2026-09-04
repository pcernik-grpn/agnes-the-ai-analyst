"""Repository for ``resource_grants`` — group → (resource_type, resource_id).

Each row is a single grant: members of ``group_id`` are allowed access to
``resource_id`` of kind ``resource_type``. The format of ``resource_id`` is
owned by the module that registered the resource type (see
``app.resource_types``); the repository treats it as an opaque string.

The resolver in ``app.auth.access`` reads this table on every authorization
check that isn't satisfied by Admin short-circuit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


# Maps resource_type string to the per-type column name (schema v60 / PG
# migration 0013). marketplace_plugin is absent — it uses only the legacy
# polymorphic resource_id column (composite slug/name, no surrogate FK).
_PER_TYPE_COLUMN: Dict[str, str] = {
    "table": "resource_id_table",
    "data_package": "resource_id_data_package",
    "memory_domain": "resource_id_memory_domain",
    "memory_item": "resource_id_memory_item",
    "recipe": "resource_id_recipe",
}


class ResourceGrantsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _SELECT_COLS = "id, group_id, resource_type, resource_id, assigned_at, assigned_by, requirement"

    def list_all(
        self,
        resource_type: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List grants joined with the group's name for display purposes."""
        where = []
        params: List[Any] = []
        if resource_type:
            where.append("g.resource_type = ?")
            params.append(resource_type)
        if group_id:
            where.append("g.group_id = ?")
            params.append(group_id)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            f"""SELECT g.id, g.group_id, ug.name AS group_name,
                       g.resource_type, g.resource_id,
                       g.assigned_at, g.assigned_by, g.requirement
                FROM resource_grants g
                JOIN user_groups ug ON ug.id = g.group_id
                {where_sql}
                ORDER BY ug.name, g.resource_type, g.resource_id""",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def list_for_groups(
        self,
        group_ids: List[str],
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """All grants held by any of the given groups, optionally type-scoped.

        Used by the marketplace filter to materialize a user's allowed plugins
        in one round trip — caller passes the user's full group set.
        """
        if not group_ids:
            return []
        placeholders = ",".join(["?"] * len(group_ids))
        type_clause = "AND resource_type = ?" if resource_type else ""
        params: List[Any] = [*group_ids]
        if resource_type:
            params.append(resource_type)
        rows = self.conn.execute(
            f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE group_id IN ({placeholders}) {type_clause}
                ORDER BY resource_type, resource_id""",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def list_resource_ids_for_user(
        self,
        user_id: str,
        resource_type: str,
    ) -> List[str]:
        """Distinct ``resource_id`` values of ``resource_type`` granted to
        any group the user belongs to.

        Joins ``resource_grants`` against ``user_group_members`` directly on
        ``user_id`` (rather than requiring the caller to first resolve the
        user's group ids) — the caller-granted-domains helper in
        ``app.api.memory`` used to run this as a raw ``conn.execute`` on the
        always-DuckDB connection; this is its repo-routed equivalent.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT rg.resource_id
               FROM resource_grants rg
               JOIN user_group_members m ON m.group_id = rg.group_id
               WHERE m.user_id = ?
                 AND rg.resource_type = ?""",
            [user_id, resource_type],
        ).fetchall()
        return [r[0] for r in rows]

    def get(self, grant_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM resource_grants WHERE id = ?",
            [grant_id],
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.description]
        return dict(zip(cols, row))

    def has_grant(
        self,
        group_ids: List[str],
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Single-purpose existence check used by ``can_access``.

        Returns True iff any of the given groups has a grant for the
        (resource_type, resource_id) pair. One DB hit, indexed on the
        UNIQUE (group_id, resource_type, resource_id) constraint.
        """
        if not group_ids:
            return False
        placeholders = ",".join(["?"] * len(group_ids))
        row = self.conn.execute(
            f"""SELECT 1 FROM resource_grants
                WHERE group_id IN ({placeholders})
                  AND resource_type = ?
                  AND resource_id = ?
                LIMIT 1""",
            [*group_ids, resource_type, resource_id],
        ).fetchone()
        return row is not None

    def create(
        self,
        group_id: str,
        resource_type: str,
        resource_id: str,
        assigned_by: Optional[str] = None,
        requirement: Optional[str] = None,
        source: Optional[str] = None,
    ) -> str:
        """Insert a new grant. Returns the assigned id.

        ``source`` names the SURFACE that wrote this grant
        (``src.grant_sources``). Accepted and DROPPED here: the column is
        Postgres-only (migration 0096_resource_grants_source) because the
        DuckDB ladder is frozen (A3), so this backend simply does not gain
        provenance and the API reports ``None`` — which the Access page
        already renders as an ordinary grant. The parameter exists so every
        caller can pass it without asking which backend is active.

        ``requirement`` defaults to the column default (``'available'``)
        when ``None``. Pass ``'required'`` to create a Required-tier
        grant in a single round-trip. Rejected by the column CHECK if
        the string is anything other than the two enum values.

        Raises ``duckdb.ConstraintException`` on duplicate
        (group_id, resource_type, resource_id) — caller surfaces as 409.
        """
        grant_id = str(uuid4())
        per_type_col = _PER_TYPE_COLUMN.get(resource_type)
        if requirement is None:
            if per_type_col:
                self.conn.execute(
                    f"""INSERT INTO resource_grants
                       (id, group_id, resource_type, resource_id, {per_type_col}, assigned_by)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [grant_id, group_id, resource_type, resource_id, resource_id, assigned_by],
                )
            else:
                self.conn.execute(
                    """INSERT INTO resource_grants
                       (id, group_id, resource_type, resource_id, assigned_by)
                       VALUES (?, ?, ?, ?, ?)""",
                    [grant_id, group_id, resource_type, resource_id, assigned_by],
                )
        else:
            if requirement not in ("available", "required"):
                raise ValueError(f"requirement must be 'available' or 'required', got {requirement!r}")
            if per_type_col:
                self.conn.execute(
                    f"""INSERT INTO resource_grants
                       (id, group_id, resource_type, resource_id, {per_type_col}, assigned_by, requirement)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [grant_id, group_id, resource_type, resource_id, resource_id, assigned_by, requirement],
                )
            else:
                self.conn.execute(
                    """INSERT INTO resource_grants
                       (id, group_id, resource_type, resource_id, assigned_by, requirement)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [grant_id, group_id, resource_type, resource_id, assigned_by, requirement],
                )
        return grant_id

    def update_requirement(
        self,
        grant_id: str,
        requirement: str,
    ) -> Optional[str]:
        """Update the ``requirement`` enum on a grant. Returns the prior
        value (None if grant missing) so callers can detect transitions.

        v49: ``requirement`` is one of ``'available'`` / ``'required'``.
        Callers handle the soft-downgrade subscription fan-out at the
        service layer (see app/api/access.py update_grant_requirement).
        """
        if requirement not in ("available", "required"):
            raise ValueError(f"requirement must be 'available' or 'required', got {requirement!r}")
        before = self.conn.execute(
            "SELECT requirement FROM resource_grants WHERE id = ?",
            [grant_id],
        ).fetchone()
        if before is None:
            return None
        prior = before[0]
        self.conn.execute(
            "UPDATE resource_grants SET requirement = ? WHERE id = ?",
            [requirement, grant_id],
        )
        return prior

    def ensure_grant(
        self,
        group_id: str,
        resource_type: str,
        resource_id: str,
        assigned_by: Optional[str] = None,
        source: Optional[str] = None,
    ) -> bool:
        """Create a grant if it does not already exist. Returns True iff the
        grant row exists after the call (whether newly inserted or already
        present) — mirrors the Postgres sibling's contract. The post-insert
        ``SELECT`` cannot distinguish a fresh insert from a pre-existing row,
        so callers must not treat the return value as "was inserted".

        Uses INSERT OR IGNORE so repeated calls (e.g. on every boot from the
        built-in marketplace seeder) are idempotent and cheap.

        ``source`` names the SURFACE that wrote this grant
        (``src.grant_sources``). Accepted and DROPPED here: the column is
        Postgres-only (migration 0096_resource_grants_source) because the
        DuckDB ladder is frozen (A3), so this backend simply does not gain
        provenance and the API reports ``None`` — which the Access page
        already renders as an ordinary grant. The parameter exists so every
        caller can pass it without asking which backend is active.
        """
        grant_id = str(uuid4())
        per_type_col = _PER_TYPE_COLUMN.get(resource_type)
        if per_type_col:
            self.conn.execute(
                f"""INSERT OR IGNORE INTO resource_grants
                   (id, group_id, resource_type, resource_id, {per_type_col}, assigned_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [grant_id, group_id, resource_type, resource_id, resource_id, assigned_by],
            )
        else:
            self.conn.execute(
                """INSERT OR IGNORE INTO resource_grants
                   (id, group_id, resource_type, resource_id, assigned_by)
                   VALUES (?, ?, ?, ?, ?)""",
                [grant_id, group_id, resource_type, resource_id, assigned_by],
            )
        # Check if we just inserted by verifying the row now exists.
        row = self.conn.execute(
            "SELECT 1 FROM resource_grants WHERE group_id = ? AND resource_type = ? AND resource_id = ? LIMIT 1",
            [group_id, resource_type, resource_id],
        ).fetchone()
        return row is not None

    def delete(self, grant_id: str) -> bool:
        """Remove a grant by id. Returns True iff a row was removed."""
        res = self.conn.execute(
            "DELETE FROM resource_grants WHERE id = ? RETURNING 1",
            [grant_id],
        ).fetchone()
        return res is not None

    def delete_by_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        """Remove every grant for a resource. Used when an entity is deleted
        upstream (e.g. a marketplace plugin disappears) so dangling grants
        don't accumulate. Returns the number of rows removed.
        """
        rows = self.conn.execute(
            """DELETE FROM resource_grants
               WHERE resource_type = ? AND resource_id = ?
               RETURNING 1""",
            [resource_type, resource_id],
        ).fetchall()
        return len(rows)

    def delete_for_marketplace_plugins(self, marketplace_id: str) -> int:
        """Remove every ``marketplace_plugin`` grant belonging to a marketplace.

        Used by the marketplace-delete cascade so grants don't outlive the
        plugins they reference. ``resource_id`` is
        ``"<marketplace_slug>/<plugin_name>"``; match the slug via
        ``split_part(resource_id, '/', 1)`` rather than a LIKE prefix —
        marketplace slugs may contain ``_`` (validated by
        ``[a-z0-9][a-z0-9_-]{0,63}``), which LIKE would treat as a single-char
        wildcard and silently drop grants from sibling marketplaces whose slug
        differs by exactly one character. Returns the number of rows removed.
        ``'marketplace_plugin'`` is the literal value of
        ``ResourceType.MARKETPLACE_PLUGIN`` (kept inline so the repo layer stays
        free of the resource_types import).
        """
        rows = self.conn.execute(
            """DELETE FROM resource_grants
               WHERE resource_type = 'marketplace_plugin'
                 AND split_part(resource_id, '/', 1) = ?
               RETURNING 1""",
            [marketplace_id],
        ).fetchall()
        return len(rows)

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every grant for ``group_id``. Used by the group-delete cascade."""
        rows = self.conn.execute(
            "DELETE FROM resource_grants WHERE group_id = ? RETURNING 1",
            [group_id],
        ).fetchall()
        return len(rows)

    def count_for_group(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM resource_grants WHERE group_id = ?",
            [group_id],
        ).fetchone()
        return int(row[0]) if row else 0

