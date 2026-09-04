"""Postgres-backed user_curated_subscriptions repository.

Mirrors ``src/repositories/user_curated_subscriptions.py``. The backing
table is still the historically-named ``user_plugin_optouts``; row
presence means the user is subscribed (v28 semantic).
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class UserCuratedSubscriptionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def subscribe(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "INSERT INTO user_plugin_optouts "
                    "(user_id, marketplace_id, plugin_name) VALUES (:u, :m, :p) "
                    "ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING "
                    "RETURNING 1"
                ),
                {"u": user_id, "m": marketplace_id, "p": plugin_name},
            ).first()
        return row is not None

    def unsubscribe(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "DELETE FROM user_plugin_optouts "
                    "WHERE user_id = :u AND marketplace_id = :m AND plugin_name = :p "
                    "RETURNING 1"
                ),
                {"u": user_id, "m": marketplace_id, "p": plugin_name},
            ).first()
        return row is not None

    def is_subscribed(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM user_plugin_optouts WHERE user_id = :u AND marketplace_id = :m AND plugin_name = :p"
                ),
                {"u": user_id, "m": marketplace_id, "p": plugin_name},
            ).first()
        return row is not None

    def subscribed_set(self, user_id: str) -> Set[Tuple[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT marketplace_id, plugin_name FROM user_plugin_optouts WHERE user_id = :u"),
                {"u": user_id},
            ).all()
        return {(r[0], r[1]) for r in rows}

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT marketplace_id, plugin_name, opted_out_at "
                    "FROM user_plugin_optouts WHERE user_id = :u "
                    "ORDER BY opted_out_at DESC"
                ),
                {"u": user_id},
            ).all()
        return [
            {
                "marketplace_id": r[0],
                "plugin_name": r[1],
                "subscribed_at": r[2],
            }
            for r in rows
        ]

    def delete_for_plugin(self, marketplace_id: str, plugin_name: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM user_plugin_optouts WHERE marketplace_id = :m AND plugin_name = :p RETURNING 1"),
                {"m": marketplace_id, "p": plugin_name},
            ).all()
        return len(rows)

    def delete_for_marketplace(self, marketplace_id: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM user_plugin_optouts WHERE marketplace_id = :m RETURNING 1"),
                {"m": marketplace_id},
            ).all()
        return len(rows)

    def subscribe_group_members(self, group_id: str, marketplace_id: str, plugin_name: str) -> int:
        """Soft-downgrade fan-out — see the DuckDB sibling's docstring."""
        with self._engine.begin() as conn:
            before = (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = :m AND plugin_name = :p"),
                    {"m": marketplace_id, "p": plugin_name},
                ).scalar()
                or 0
            )
            conn.execute(
                sa.text(
                    """INSERT INTO user_plugin_optouts
                       (user_id, marketplace_id, plugin_name)
                       SELECT m.user_id, :m, :p FROM user_group_members m
                       WHERE m.group_id = :g
                       ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING"""
                ),
                {"m": marketplace_id, "p": plugin_name, "g": group_id},
            )
            after = (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = :m AND plugin_name = :p"),
                    {"m": marketplace_id, "p": plugin_name},
                ).scalar()
                or 0
            )
        return max(0, int(after) - int(before))



    def stack_counts(self) -> Dict[Tuple[str, str], int]:
        """Mirrors ``UserCuratedSubscriptionsRepository.stack_counts``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT marketplace_id, plugin_name, COUNT(DISTINCT user_id)
                    FROM user_plugin_optouts
                    GROUP BY marketplace_id, plugin_name
                    """
                )
            ).all()
        return {(r[0], r[1]): int(r[2]) for r in rows}
