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

    def add_member(
        self,
        user_id: str,
        group_id: str,
        source: str,
        added_by: Optional[str] = None,
    ) -> None:
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
