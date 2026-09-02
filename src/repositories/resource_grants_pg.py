"""Postgres-backed resource-grants repository.

Mirrors ``src/repositories/resource_grants.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from src.grant_scopes import EVERYONE as SCOPE_EVERYONE
from src.grant_scopes import normalize as normalize_scope


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
    #
    # `scope` rides them for the same reason and one more: a caller that does
    # not read it cannot tell an everyone-grant from a grant on the carrier
    # group, and would answer "who can see this" with the carrier's member
    # list. PG-only too (migration 0097).
    _SELECT_COLS = (
        "id, group_id, resource_type, resource_id, assigned_at, assigned_by, "
        "requirement, source, scope"
    )

    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _audience_clause(
        group_ids: List[str],
        params: Dict[str, Any],
        *,
        include_everyone: bool,
    ) -> str:
        """SQL for "reaches this audience", and the bound ids for it.

        An everyone-scoped grant reaches an account regardless of which
        groups it is in — including an account in no group at all, which is
        why this cannot be expressed by adding a group id to the IN list.
        Mutates ``params`` with the ``a_<i>`` keys it binds.

        Returns ``FALSE`` when there is nothing to match, so the caller gets
        an empty result from the database rather than having to special-case
        an empty list into a syntactically invalid ``IN ()``.
        """
        terms: List[str] = []
        if group_ids:
            in_keys: List[str] = []
            for i, gid in enumerate(group_ids):
                k = f"a_{i}"
                in_keys.append(f":{k}")
                params[k] = gid
            terms.append(f"group_id IN ({','.join(in_keys)})")
        if include_everyone:
            terms.append("scope = :everyone_scope")
            params["everyone_scope"] = SCOPE_EVERYONE
        if not terms:
            return "FALSE"
        return "(" + " OR ".join(terms) + ")"

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
                       g.assigned_at, g.assigned_by, g.requirement, g.source,
                       g.scope
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
        include_everyone: bool = True,
    ) -> List[Dict[str, Any]]:
        """Every grant that reaches an account in ``group_ids``.

        ``include_everyone`` defaults to True because that is what preserves
        today's answer: before 0098 an everyone-grant WAS a grant on a group
        holding every account, so every caller of this method already saw it.
        A caller that opts out is asking a narrower question — "what does
        this group itself grant" — and only the admin surfaces that attribute
        rows to a group have any business asking it.

        Works with an empty ``group_ids``: an account in no group still
        receives everyone-scoped grants, which is precisely the case the old
        group model could not express.
        """
        params: Dict[str, Any] = {}
        audience = self._audience_clause(group_ids, params, include_everyone=include_everyone)
        type_clause = ""
        if resource_type:
            type_clause = "AND resource_type = :rtype"
            params["rtype"] = resource_type

        sql = f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE {audience} {type_clause}
                ORDER BY resource_type, resource_id"""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def list_resource_ids_for_user(
        self,
        user_id: str,
        resource_type: str,
        include_everyone: bool = True,
    ) -> List[str]:
        """Distinct ``resource_id`` values of ``resource_type`` this user
        reaches — through a group they belong to, or through scope.

        The everyone-scoped half is a separate SELECT rather than an OR on
        the join: joined to ``user_group_members`` it would return nothing
        for an account with no memberships, which is exactly the account an
        everyone-grant is supposed to reach.
        """
        sql = """SELECT DISTINCT rg.resource_id
                 FROM resource_grants rg
                 JOIN user_group_members m ON m.group_id = rg.group_id
                 WHERE m.user_id = :u
                   AND rg.resource_type = :rtype"""
        if include_everyone:
            sql += """
                 UNION
                 SELECT DISTINCT rg.resource_id
                 FROM resource_grants rg
                 WHERE rg.scope = :everyone_scope
                   AND rg.resource_type = :rtype"""
        params: Dict[str, Any] = {"u": user_id, "rtype": resource_type}
        if include_everyone:
            params["everyone_scope"] = SCOPE_EVERYONE
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).all()
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
        include_everyone: bool = True,
    ) -> bool:
        """Whether this audience reaches ``(resource_type, resource_id)``.

        Same ``include_everyone`` default and reasoning as
        :meth:`list_for_groups`. This one backs ``can_access``, so a caller
        that opted out would be denying access the grants say exists.
        """
        params: Dict[str, Any] = {"rtype": resource_type, "rid": resource_id}
        audience = self._audience_clause(group_ids, params, include_everyone=include_everyone)
        sql = f"""SELECT 1 FROM resource_grants
                WHERE {audience}
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
        scope: Optional[str] = None,
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

        ``scope`` names WHO the grant reaches (``src.grant_scopes``): ``None``
        for the members of ``group_id``, ``'everyone'`` for every account.
        Pass ``group_id=carrier_group_id()`` with an everyone-scope — the
        column stays NOT NULL and is ignored on read, and using one carrier
        for every everyone-grant is what makes the UNIQUE index reject a
        second one for the same resource. Postgres-only (migration 0097).
        """
        if requirement is not None and requirement not in ("available", "required"):
            raise ValueError(f"requirement must be 'available' or 'required', got {requirement!r}")
        scope = normalize_scope(scope)
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
        if scope is not None:
            cols.append("scope")
            vals.append(":scope")
            params["scope"] = scope

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
        scope: Optional[str] = None,
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

        ``scope`` — see :meth:`create`. The ON CONFLICT target is
        ``(group_id, resource_type, resource_id)``, so an everyone-grant is
        idempotent for the same reason a group grant is: every everyone-grant
        for a resource shares one carrier group.
        """
        scope = normalize_scope(scope)
        grant_id = str(uuid4())
        per_type_col = _PER_TYPE_COLUMN.get(resource_type)
        params: Dict[str, Any] = {
            "id": grant_id,
            "g": group_id,
            "rt": resource_type,
            "ri": resource_id,
            "ab": assigned_by,
            "src": source,
            "scope": scope,
        }
        cols = ["id", "group_id", "resource_type", "resource_id"]
        vals = [":id", ":g", ":rt", ":ri"]
        if per_type_col:
            cols.append(per_type_col)
            vals.append(":ri")
        cols += ["assigned_by", "source", "scope"]
        vals += [":ab", ":src", ":scope"]
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"INSERT INTO resource_grants ({', '.join(cols)}) "
                        f"VALUES ({', '.join(vals)}) "
                        f"ON CONFLICT (group_id, resource_type, resource_id) DO NOTHING"
                    ),
                    params,
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
        """Grants this group itself confers.

        Everyone-scoped rows are excluded even though the carrier group holds
        them: the carrier does not decide their reach, the scope does, and
        counting them here would report the carrier as the largest grantee on
        the instance while revoking one of its "grants" would take access
        away from people who are not its members.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM resource_grants "
                    "WHERE group_id = :gid AND scope IS NULL"
                ),
                {"gid": group_id},
            ).first()
        return int(row[0]) if row else 0

    def count_everyone_scoped(self) -> int:
        """How many grants reach every account. The counterpart to
        :meth:`count_for_group` for the audience that is not a group."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM resource_grants WHERE scope = :s"),
                {"s": SCOPE_EVERYONE},
            ).first()
        return int(row[0]) if row else 0

    def list_everyone_scoped(
        self,
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Every grant that reaches all accounts, optionally type-scoped.

        The read the Access page needs to render "everyone" as an audience
        without pretending it is the carrier group.
        """
        params: Dict[str, Any] = {"s": SCOPE_EVERYONE}
        type_clause = ""
        if resource_type:
            type_clause = "AND resource_type = :rtype"
            params["rtype"] = resource_type
        sql = f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE scope = :s {type_clause}
                ORDER BY resource_type, resource_id"""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]
