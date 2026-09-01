"""Postgres-backed resource-grants repository.

Mirrors ``src/repositories/resource_grants.py``. ``fanout_system_for_group``
is soft-failed when ``marketplace_plugins`` isn't migrated yet (Phase F
in progress); once the table lands, the try/except becomes unnecessary.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


# Maps resource_type string to the per-type FK column name (migration 0013).
# marketplace_plugin is absent — it uses the legacy polymorphic resource_id
# only (composite slug/name path, no surrogate FK possible).
_PER_TYPE_COLUMN: Dict[str, str] = {
    "table": "resource_id_table",
    "data_package": "resource_id_data_package",
    "memory_domain": "resource_id_memory_domain",
    "memory_item": "resource_id_memory_item",
    "recipe": "resource_id_recipe",
}


class ResourceGrantsPgRepository:
    # `source` rides every read so the Access page can say WHERE a grant came
    # from, not just who wrote it. PG-only (migration 0095) — the DuckDB
    # sibling has no such column and its rows simply carry no key.
    _SELECT_COLS = (
        "id, group_id, resource_type, resource_id, assigned_at, assigned_by, requirement, source"
    )

    def __init__(self, engine: Engine):
        self._engine = engine

    def list_all(
        self,
        resource_type: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: Dict[str, Any] = {}
        if resource_type:
            where.append("g.resource_type = :rtype")
            params["rtype"] = resource_type
        if group_id:
            where.append("g.group_id = :gid")
            params["gid"] = group_id
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # `g.source` rides this read too — it is the one the Access overview
        # uses, and it has its own column list rather than `_SELECT_COLS`
        # (it joins the group name), so adding the column in one place was
        # not enough.
        sql = f"""SELECT g.id, g.group_id, ug.name AS group_name,
                       g.resource_type, g.resource_id,
                       g.assigned_at, g.assigned_by, g.requirement, g.source
                FROM resource_grants g
                JOIN user_groups ug ON ug.id = g.group_id
                {where_sql}
                ORDER BY ug.name, g.resource_type, g.resource_id"""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def list_for_groups(
        self,
        group_ids: List[str],
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not group_ids:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {}
        for i, gid in enumerate(group_ids):
            k = f"g_{i}"
            in_keys.append(f":{k}")
            params[k] = gid
        type_clause = ""
        if resource_type:
            type_clause = "AND resource_type = :rtype"
            params["rtype"] = resource_type

        sql = f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE group_id IN ({",".join(in_keys)}) {type_clause}
                ORDER BY resource_type, resource_id"""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def list_resource_ids_for_user(
        self,
        user_id: str,
        resource_type: str,
    ) -> List[str]:
        """Distinct ``resource_id`` values of ``resource_type`` granted to
        any group the user belongs to. Mirrors the DuckDB sibling.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT DISTINCT rg.resource_id
                       FROM resource_grants rg
                       JOIN user_group_members m ON m.group_id = rg.group_id
                       WHERE m.user_id = :u
                         AND rg.resource_type = :rtype"""
                ),
                {"u": user_id, "rtype": resource_type},
            ).all()
        return [r[0] for r in rows]

    def get(self, grant_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(f"SELECT {self._SELECT_COLS} FROM resource_grants WHERE id = :id"),
                    {"id": grant_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def has_grant(
        self,
        group_ids: List[str],
        resource_type: str,
        resource_id: str,
    ) -> bool:
        if not group_ids:
            return False
        in_keys: List[str] = []
        params: Dict[str, Any] = {"rtype": resource_type, "rid": resource_id}
        for i, gid in enumerate(group_ids):
            k = f"g_{i}"
            in_keys.append(f":{k}")
            params[k] = gid
        sql = f"""SELECT 1 FROM resource_grants
                WHERE group_id IN ({",".join(in_keys)})
                  AND resource_type = :rtype
                  AND resource_id = :rid
                LIMIT 1"""
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params).first()
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

        ``requirement`` defaults to the column default (``'available'``) when
        ``None``. Pass ``'required'`` to create a Required-tier grant in a
        single round-trip (parity with the DuckDB repo). Rejected if it is
        anything other than the two enum values.

        ``source`` names the SURFACE that wrote this grant
        (``src.grant_sources``) — ``assigned_by`` answers who, which is a
        different question when a fanout stamps every row with the admin who
        clicked on another page. Postgres-only (migration 0095); the DuckDB
        sibling accepts it and drops it, because that ladder is frozen (A3).
        ``None`` is stored as NULL, which the API reports as no provenance.
        """
        if requirement is not None and requirement not in ("available", "required"):
            raise ValueError(f"requirement must be 'available' or 'required', got {requirement!r}")
        grant_id = str(uuid4())
        per_type_col = _PER_TYPE_COLUMN.get(resource_type)

        cols = ["id", "group_id", "resource_type", "resource_id"]
        vals = [":id", ":gid", ":rtype", ":rid"]
        params: Dict[str, Any] = {
            "id": grant_id,
            "gid": group_id,
            "rtype": resource_type,
            "rid": resource_id,
            "ab": assigned_by,
        }
        if per_type_col:
            cols.append(per_type_col)
            vals.append(":rid")
        cols.append("assigned_by")
        vals.append(":ab")
        if requirement is not None:
            cols.append("requirement")
            vals.append(":req")
            params["req"] = requirement
        if source is not None:
            cols.append("source")
            vals.append(":src")
            params["src"] = source

        sql = sa.text(f"INSERT INTO resource_grants ({', '.join(cols)}) VALUES ({', '.join(vals)})")
        with self._engine.begin() as conn:
            conn.execute(sql, params)
        return grant_id

    def update_requirement(self, grant_id: str, requirement: str) -> Optional[str]:
        """Update the ``requirement`` enum on a grant. Returns the prior value
        (None if the grant is missing) so callers can detect transitions
        (parity with the DuckDB repo).
        """
        if requirement not in ("available", "required"):
            raise ValueError(f"requirement must be 'available' or 'required', got {requirement!r}")
        with self._engine.begin() as conn:
            before = conn.execute(
                sa.text("SELECT requirement FROM resource_grants WHERE id = :id"),
                {"id": grant_id},
            ).first()
            if before is None:
                return None
            conn.execute(
                sa.text("UPDATE resource_grants SET requirement = :req WHERE id = :id"),
                {"req": requirement, "id": grant_id},
            )
        return before[0]

    def ensure_grant(
        self,
        group_id: str,
        resource_type: str,
        resource_id: str,
        assigned_by: Optional[str] = None,
        source: Optional[str] = None,
    ) -> bool:
        """Create a grant if it does not already exist. Returns True iff the
        grant row exists after the call (whether newly inserted or pre-existing).

        Uses INSERT … ON CONFLICT DO NOTHING so repeated calls on every boot
        are idempotent and cheap.

        ``source`` names the SURFACE that wrote this grant
        (``src.grant_sources``) — ``assigned_by`` answers who, which is a
        different question when a fanout stamps every row with the admin who
        clicked on another page. Postgres-only (migration 0095); the DuckDB
        sibling accepts it and drops it, because that ladder is frozen (A3).
        ``None`` is stored as NULL, which the API reports as no provenance.
        """
        grant_id = str(uuid4())
        per_type_col = _PER_TYPE_COLUMN.get(resource_type)
        try:
            with self._engine.begin() as conn:
                if per_type_col:
                    conn.execute(
                        sa.text(
                            f"INSERT INTO resource_grants "
                            f"(id, group_id, resource_type, resource_id, {per_type_col}, assigned_by, source) "
                            f"VALUES (:id, :g, :rt, :ri, :ri2, :ab, :src) "
                            f"ON CONFLICT (group_id, resource_type, resource_id) DO NOTHING"
                        ),
                        {
                            "id": grant_id,
                            "g": group_id,
                            "rt": resource_type,
                            "ri": resource_id,
                            "ri2": resource_id,
                            "ab": assigned_by,
                            "src": source,
                        },
                    )
                else:
                    conn.execute(
                        sa.text(
                            "INSERT INTO resource_grants "
                            "(id, group_id, resource_type, resource_id, assigned_by, source) "
                            "VALUES (:id, :g, :rt, :ri, :ab, :src) "
                            "ON CONFLICT (group_id, resource_type, resource_id) DO NOTHING"
                        ),
                        {
                            "id": grant_id,
                            "g": group_id,
                            "rt": resource_type,
                            "ri": resource_id,
                            "ab": assigned_by,
                            "src": source,
                        },
                    )
        except IntegrityError:
            pass
        return True

    def delete(self, grant_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("DELETE FROM resource_grants WHERE id = :id RETURNING 1"),
                {"id": grant_id},
            ).first()
        return row is not None

    def delete_by_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """DELETE FROM resource_grants
                       WHERE resource_type = :rtype AND resource_id = :rid
                       RETURNING 1"""
                ),
                {"rtype": resource_type, "rid": resource_id},
            ).all()
        return len(rows)

    def delete_for_marketplace_plugins(self, marketplace_id: str) -> int:
        """PG sibling of the DuckDB ``delete_for_marketplace_plugins`` — drop
        every ``marketplace_plugin`` grant belonging to a marketplace. See the
        DuckDB docstring for the slug-prefix (``split_part``, not LIKE)
        rationale. Returns the number of rows removed."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """DELETE FROM resource_grants
                       WHERE resource_type = 'marketplace_plugin'
                         AND split_part(resource_id, '/', 1) = :mid
                       RETURNING 1"""
                ),
                {"mid": marketplace_id},
            ).all()
        return len(rows)

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every grant for ``group_id``. Used by the group-delete cascade."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM resource_grants WHERE group_id = :gid RETURNING 1"),
                {"gid": group_id},
            ).all()
        return len(rows)

    def count_for_group(self, group_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM resource_grants WHERE group_id = :gid"),
                {"gid": group_id},
            ).first()
        return int(row[0]) if row else 0

    def fanout_system_for_group(
        self,
        group_id: str,
        assigned_by: Optional[str] = None,
    ) -> int:
        """Grant every active system marketplace_plugin to ``group_id``.

        Only plugins with ``is_system=TRUE`` and ``admin_disabled=FALSE`` are
        granted — a disabled plugin stays hidden instance-wide, so a new group
        must not inherit a grant that would activate on re-enable. Mirrors the
        DuckDB sibling and ``fanout_system_for_user``.

        Soft-fail if ``marketplace_plugins`` isn't migrated yet (Phase F
        in progress). Once that port lands, drop the try/except.
        """
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT marketplace_id, name FROM marketplace_plugins "
                        "WHERE is_system = TRUE AND admin_disabled = FALSE"
                    ),
                ).all()
        except Exception:
            return 0

        inserted = 0
        for marketplace_id, plugin_name in rows:
            resource_id = f"{marketplace_id}/{plugin_name}"
            try:
                with self._engine.begin() as conn:
                    result = conn.execute(
                        sa.text(
                            """INSERT INTO resource_grants
                               (id, group_id, resource_type, resource_id, assigned_by)
                               VALUES (:id, :gid, 'marketplace_plugin', :rid, :ab)
                               ON CONFLICT ON CONSTRAINT uq_resource_grants_group_type_id
                                 DO NOTHING"""
                        ),
                        {
                            "id": str(uuid4()),
                            "gid": group_id,
                            "rid": resource_id,
                            "ab": assigned_by,
                        },
                    )
                    # ON CONFLICT DO NOTHING suppresses the duplicate (no
                    # exception raised), so a pre-existing grant yields
                    # rowcount 0. Count only real inserts — otherwise the
                    # diagnostic return value over-reports newly-granted groups
                    # on every idempotent re-run. Matches the DuckDB sibling,
                    # which relies on a ConstraintException to skip the bump.
                    if result.rowcount:
                        inserted += 1
            except IntegrityError:
                continue
        return inserted
