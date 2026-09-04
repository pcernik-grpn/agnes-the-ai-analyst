"""Postgres-backed user-group-members repository.

Mirrors ``src/repositories/user_group_members.py``. Uses PG's
``ON CONFLICT DO NOTHING`` for idempotent inserts instead of DuckDB's
catch-IntegrityError pattern.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class UserGroupMembersPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def list_groups_for_user(self, user_id: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT group_id FROM user_group_members WHERE user_id = :u"),
                {"u": user_id},
            ).all()
        return [r[0] for r in rows]

    def list_group_names_for_user(self, user_id: str) -> List[str]:
        """Group ``name`` values (not ids) this user belongs to, any source.

        Mirrors the DuckDB sibling — same join, same (unordered) shape.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT g.name FROM user_group_members m
                       JOIN user_groups g ON m.group_id = g.id
                       WHERE m.user_id = :u"""
                ),
                {"u": user_id},
            ).all()
        return [r[0] for r in rows]

    def list_members_for_group(self, group_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """SELECT u.id, u.email, u.name, u.active,
                              m.source, m.added_at, m.added_by
                       FROM user_group_members m
                       JOIN users u ON u.id = m.user_id
                       WHERE m.group_id = :g
                       ORDER BY u.email"""
                    ),
                    {"g": group_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def has_membership(self, user_id: str, group_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM user_group_members WHERE user_id = :u AND group_id = :g"),
                {"u": user_id, "g": group_id},
            ).first()
        return row is not None

    def move_all_members(self, from_group_id: str, to_group_id: str) -> int:
        """Move every membership from one group to another, ``source`` intact.

        The frozen DuckDB ladder's half of migration 0098's step 2. Keeping
        ``source`` is what leaves the nightly sync owning the rows it owns
        and an admin-added member admin-added. Idempotent on the target — a
        user already in it is left alone. Returns rows moved.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_group_members "
                    "(user_id, group_id, source, added_at, added_by) "
                    "SELECT user_id, :tgt, source, added_at, added_by "
                    "FROM user_group_members WHERE group_id = :src "
                    "ON CONFLICT (user_id, group_id) DO NOTHING"
                ),
                {"tgt": to_group_id, "src": from_group_id},
            )
            res = conn.execute(
                sa.text("DELETE FROM user_group_members WHERE group_id = :src"),
                {"src": from_group_id},
            )
        return int(res.rowcount or 0)

    def add_all_users(self, group_id: str, source: str, added_by: str) -> int:
        """Put every existing user in ``group_id``. Returns rows written.

        Only safe on a group that grants NOTHING. Its one caller
        (``src.system_plugin_reconcile``) runs it immediately after moving
        the seeded group's grants out, and only then — on a group that still
        holds grants this hands every account those grants, which is a
        widening rather than a backfill.
        """
        with self._engine.begin() as conn:
            res = conn.execute(
                sa.text(
                    # Casts, because the same bind is a SELECT-list value AND
                    # a comparison operand below: Postgres deduces `text` from
                    # one and `character varying` from the other and refuses
                    # with AmbiguousParameter.
                    "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                    "SELECT u.id, CAST(:g AS varchar), CAST(:s AS varchar), "
                    "       CAST(:ab AS varchar) FROM users u "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM user_group_members m "
                    "  WHERE m.user_id = u.id AND m.group_id = CAST(:g AS varchar))"
                ),
                {"g": group_id, "s": source, "ab": added_by},
            )
        return int(res.rowcount or 0)

    def add_member(
        self,
        user_id: str,
        group_id: str,
        source: str,
        added_by: Optional[str] = None,
    ) -> None:
        """Insert a membership row (mirrors the DuckDB sibling exactly).

        Raises ``src.service_accounts.ServiceAccountAdminGroupForbidden``
        when ``group_id`` is the system Admin group and ``user_id`` names a
        ``kind='service'`` row (issue #1534) — see
        :meth:`_refuse_if_service_account_targets_admin_group`.
        """
        self._refuse_if_service_account_targets_admin_group(user_id, group_id)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (:u, :g, :s, :b)
                       ON CONFLICT (user_id, group_id) DO NOTHING"""
                ),
                {"u": user_id, "g": group_id, "s": source, "b": added_by},
            )

    def _refuse_if_service_account_targets_admin_group(self, user_id: str, group_id: str) -> None:
        """Guard 2 (issue #1534) — see the DuckDB sibling's docstring for the
        full rationale (one shared choke point inside ``add_member`` rather
        than N call-site checks). Live on this backend: ``users.kind`` is a
        real PG-only column, so this is where a service account actually
        gets refused."""
        from src.db import SYSTEM_ADMIN_GROUP
        from src.service_accounts import ServiceAccountAdminGroupForbidden, is_service_account

        with self._engine.connect() as conn:
            group_row = conn.execute(sa.text("SELECT name FROM user_groups WHERE id = :g"), {"g": group_id}).first()
            if not group_row or group_row[0] != SYSTEM_ADMIN_GROUP:
                return
            user_row = conn.execute(sa.text("SELECT * FROM users WHERE id = :u"), {"u": user_id}).mappings().first()
        if user_row and is_service_account(dict(user_row)):
            raise ServiceAccountAdminGroupForbidden(user_id)

    def remove_member(
        self,
        user_id: str,
        group_id: str,
        require_source: Optional[str] = None,
    ) -> bool:
        with self._engine.begin() as conn:
            if require_source is not None:
                row = conn.execute(
                    sa.text(
                        """DELETE FROM user_group_members
                           WHERE user_id = :u AND group_id = :g AND source = :s
                           RETURNING 1"""
                    ),
                    {"u": user_id, "g": group_id, "s": require_source},
                ).first()
            else:
                row = conn.execute(
                    sa.text(
                        """DELETE FROM user_group_members
                           WHERE user_id = :u AND group_id = :g
                           RETURNING 1"""
                    ),
                    {"u": user_id, "g": group_id},
                ).first()
        return row is not None

    def replace_synced_groups(
        self,
        user_id: str,
        group_ids: List[str],
        source: str,
        added_by: str,
    ) -> None:
        """Shared bulk-replace primitive behind ``replace_google_sync_groups``
        / ``replace_microsoft_sync_groups`` — mirrors the DuckDB sibling."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_group_members WHERE user_id = :u AND source = :s"),
                {"u": user_id, "s": source},
            )
            for group_id in group_ids:
                conn.execute(
                    sa.text(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (:u, :g, :s, :b)
                           ON CONFLICT (user_id, group_id) DO NOTHING"""
                    ),
                    {"u": user_id, "g": group_id, "s": source, "b": added_by},
                )

    def replace_group_members_for_source(self, group_id: str, user_ids: List[str], source: str, added_by: str) -> None:
        """Authoritative refresh of this GROUP's ``source``-tagged membership —
        mirrors the DuckDB sibling's ``replace_group_members_for_source``
        (the group-oriented transpose of ``replace_synced_groups``)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_group_members WHERE group_id = :g AND source = :s"),
                {"g": group_id, "s": source},
            )
            for user_id in user_ids:
                conn.execute(
                    sa.text(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (:u, :g, :s, :b)
                           ON CONFLICT (user_id, group_id) DO NOTHING"""
                    ),
                    {"u": user_id, "g": group_id, "s": source, "b": added_by},
                )

    def replace_google_sync_groups(
        self,
        user_id: str,
        group_ids: List[str],
        added_by: str = "system:google-sync",
    ) -> None:
        """``replace_synced_groups`` pinned to ``source='google_sync'``."""
        self.replace_synced_groups(user_id, group_ids, source="google_sync", added_by=added_by)

    def replace_microsoft_sync_groups(
        self,
        user_id: str,
        group_ids: List[str],
        added_by: str = "system:microsoft-sync",
    ) -> None:
        """``replace_synced_groups`` pinned to ``source='microsoft_sync'``."""
        self.replace_synced_groups(user_id, group_ids, source="microsoft_sync", added_by=added_by)

    def remove_user_from_all_groups(self, user_id: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM user_group_members WHERE user_id = :u RETURNING 1"),
                {"u": user_id},
            ).all()
        return len(rows)

    def count_members(self, group_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM user_group_members WHERE group_id = :g"),
                {"g": group_id},
            ).first()
        return int(row[0]) if row else 0

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every membership row pointing at ``group_id``."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM user_group_members WHERE group_id = :g RETURNING 1"),
                {"g": group_id},
            ).all()
        return len(rows)

    def list_groups_with_meta_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return groups the user is in joined with the groups table.

        Each row: ``{group_id, id, name, description, is_system,
        created_by, source, added_at}``. Mirror of the DuckDB version —
        same shape, same ordering.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT g.id, g.name, g.description, g.is_system,
                              g.created_by, m.source, m.added_at
                       FROM user_group_members m
                       JOIN user_groups g ON g.id = m.group_id
                       WHERE m.user_id = :u
                       ORDER BY g.is_system DESC, g.name"""
                ),
                {"u": user_id},
            ).all()
        return [
            {
                "group_id": r[0],
                "id": r[0],
                "name": r[1],
                "description": r[2],
                "is_system": bool(r[3]),
                "created_by": r[4],
                "source": r[5],
                "added_at": r[6],
            }
            for r in rows
        ]

    def list_google_sync_groups_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return the user's ``source='google_sync'`` groups for the
        refetch-groups dry-run diff.

        Each row: ``{name, external_id}``. ``user_groups`` has no
        ``external_id`` column on Postgres, so ``external_id`` is always
        ``None`` here — parity with the DuckDB sibling, which probes
        ``information_schema`` and falls back to NULL when the column is
        absent.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT g.name
                         FROM user_group_members m
                         JOIN user_groups g ON g.id = m.group_id
                        WHERE m.user_id = :u AND m.source = 'google_sync'
                        ORDER BY g.name"""
                ),
                {"u": user_id},
            ).all()
        return [{"name": r[0], "external_id": None} for r in rows]

    def has_any_google_sync_membership(self, user_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM user_group_members WHERE user_id = :u AND source = 'google_sync' LIMIT 1"),
                {"u": user_id},
            ).first()
        return row is not None

    def google_sync_summary(self, user_id: str) -> Dict[str, Any]:
        """Mirrors ``UserGroupMembersRepository.google_sync_summary`` — same
        return contract, deliberately NOT the same query shape.

        The DuckDB sibling folds the rows in Python instead of aggregating in
        SQL, to dodge an optimizer crash on DuckDB 1.5.2 (see the comment on
        that method). Postgres has no such problem, so it keeps the honest
        ``COUNT(*) / MAX(...)``. Do not "de-drift" these into one shape: pushing
        the aggregate back into DuckDB's SQL reintroduces the crash for installs
        still on 1.5.2. Both backends are pinned by the same contract test.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT COUNT(*) AS n, MAX(added_at) AS last_at
                         FROM user_group_members
                        WHERE user_id = :u AND source = 'google_sync'"""
                ),
                {"u": user_id},
            ).first()
        n, last_at = row if row else (0, None)
        return {"count": int(n or 0), "last_added_at": last_at}
