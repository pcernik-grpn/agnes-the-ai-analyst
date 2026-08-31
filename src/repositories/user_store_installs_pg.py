"""Postgres-backed user_store_installs repository.

Mirrors ``src/repositories/user_store_installs.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.store_submissions import BLOCKING_SUBMISSION_STATUS_SQL


class UserStoreInstallsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def install(self, user_id: str, entity_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "INSERT INTO user_store_installs (user_id, entity_id) "
                    "VALUES (:u, :e) "
                    "ON CONFLICT (user_id, entity_id) DO NOTHING "
                    "RETURNING 1"
                ),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def install_for_group_members(self, group_id: str, entity_id: str) -> int:
        """PG sibling of the DuckDB ``install_for_group_members`` — the Required
        tier's fan-out. See that docstring."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """INSERT INTO user_store_installs (user_id, entity_id)
                       SELECT m.user_id, :e FROM user_group_members m
                       WHERE m.group_id = :g
                       ON CONFLICT (user_id, entity_id) DO NOTHING
                       RETURNING 1"""
                ),
                {"e": entity_id, "g": group_id},
            ).all()
        return len(rows)

    def install_required_for_user(self, user_id: str, entity_ids: List[str]) -> int:
        """PG sibling of the DuckDB ``install_required_for_user``."""
        created = 0
        for entity_id in entity_ids:
            if self.install(user_id, entity_id):
                created += 1
        return created

    def uninstall(self, user_id: str, entity_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("DELETE FROM user_store_installs WHERE user_id = :u AND entity_id = :e RETURNING 1"),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def list_for_user(self, user_id: str, granted_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """PG sibling of the DuckDB ``list_for_user`` — see that docstring for
        which visibility states serve, why a hidden entity serves to its own
        author, and what ``granted_ids`` is."""
        granted = [str(g) for g in (granted_ids or [])]
        params: Dict[str, Any] = {"u": user_id}
        granted_sql = ""
        if granted:
            keys = []
            for i, gid in enumerate(granted):
                key = f"g{i}"
                params[key] = gid
                keys.append(f":{key}")
            granted_sql = (
                " OR (se.visibility_status = 'hidden' AND se.id IN (" + ",".join(keys) + ")"
                " AND NOT EXISTS (SELECT 1 FROM store_submissions ss2 WHERE ss2.entity_id = se.id"
                f" AND ss2.status IN ({BLOCKING_SUBMISSION_STATUS_SQL})))"
            )
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"""SELECT
                           se.id, se.owner_user_id, se.owner_username, se.type,
                           se.name, se.description, se.category, se.version,
                           se.photo_path, se.video_url, se.file_size,
                           se.install_count, se.created_at, se.updated_at,
                           se.visibility_status,
                           se.title, se.tagline, se.synthetic_name,
                           usi.installed_at
                       FROM user_store_installs usi
                       JOIN store_entities se ON se.id = usi.entity_id
                       WHERE usi.user_id = :u
                         AND (
                           se.visibility_status IN ('approved', 'archived')
                           OR (
                             se.visibility_status = 'hidden'
                             AND se.owner_user_id = :u
                             AND NOT EXISTS (
                               SELECT 1 FROM store_submissions ss
                               WHERE ss.entity_id = se.id
                                 AND ss.status IN ({BLOCKING_SUBMISSION_STATUS_SQL})
                             )
                           )
                           {granted_sql}
                         )
                       ORDER BY usi.installed_at DESC, se.id"""
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def is_installed(self, user_id: str, entity_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM user_store_installs WHERE user_id = :u AND entity_id = :e"),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def installer_count(self, entity_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM user_store_installs WHERE entity_id = :e"),
                {"e": entity_id},
            ).first()
        return int(row[0]) if row else 0

    def delete_all_for_entity(self, entity_id: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM user_store_installs WHERE entity_id = :e RETURNING 1"),
                {"e": entity_id},
            ).all()
        return len(rows)
