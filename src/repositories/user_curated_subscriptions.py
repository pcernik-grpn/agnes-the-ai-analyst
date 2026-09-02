"""Repository for per-user curated marketplace subscriptions (Model B opt-in).

Backed by the historically-named ``user_plugin_optouts`` table. Pre-v28 a row
represented an opt-OUT against an admin-granted plugin; v28 inverts the
semantic — row PRESENCE now means the user is subscribed. The DDL rename was
intentionally skipped to avoid migration churn on running operator instances;
the v28 migration wipes the rows so the inverted reading starts clean.

Used by ``src/marketplace_filter.py:resolve_user_marketplace`` to compute the
served plugin set as ``(rbac_grants ∩ (subscriptions ∪ required)) ∪
store_installs`` — required-tier grant keys come from ``resource_grants``,
not this table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

import duckdb


class UserCuratedSubscriptionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def subscribe(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        """Idempotent. Returns True iff a new row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_plugin_optouts WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO user_plugin_optouts "
            "(user_id, marketplace_id, plugin_name) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING",
            [user_id, marketplace_id, plugin_name],
        )
        return True

    def unsubscribe(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        """Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_plugin_optouts WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM user_plugin_optouts WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        )
        return True

    def is_subscribed(self, user_id: str, marketplace_id: str, plugin_name: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_plugin_optouts WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
                [user_id, marketplace_id, plugin_name],
            ).fetchone()
        )

    def subscribed_set(self, user_id: str) -> Set[Tuple[str, str]]:
        """Return the user's subscriptions as a ``{(marketplace_id, plugin_name)}``
        set — the shape ``resolve_user_marketplace`` filters against.
        """
        rows = self.conn.execute(
            "SELECT marketplace_id, plugin_name FROM user_plugin_optouts WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return {(r[0], r[1]) for r in rows}

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return the user's subscriptions ordered newest-first."""
        rows = self.conn.execute(
            "SELECT marketplace_id, plugin_name, opted_out_at "
            "FROM user_plugin_optouts WHERE user_id = ? "
            "ORDER BY opted_out_at DESC",
            [user_id],
        ).fetchall()
        return [
            {
                "marketplace_id": r[0],
                "plugin_name": r[1],
                "subscribed_at": r[2],
            }
            for r in rows
        ]

    def delete_for_plugin(self, marketplace_id: str, plugin_name: str) -> int:
        """Drop all users' subscriptions for a given plugin.

        Called when a plugin's RBAC grant is revoked or the parent marketplace
        is deleted. Returns count of rows deleted (audit telemetry).
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM user_plugin_optouts WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        )
        return int(before)

    def delete_for_marketplace(self, marketplace_id: str) -> int:
        """Drop all subscriptions for every plugin in a marketplace.

        Called from ``DELETE /api/marketplaces/{id}`` cleanup path.
        Returns count of rows deleted.
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = ?",
            [marketplace_id],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM user_plugin_optouts WHERE marketplace_id = ?",
            [marketplace_id],
        )
        return int(before)

    def subscribe_group_members(self, group_id: str, marketplace_id: str, plugin_name: str) -> int:
        """Subscribe every current member of ``group_id`` to the plugin.

        Soft-downgrade fan-out for marketplace_plugin grants (mirrors
        ``UserStackSubscriptionsRepository.subscribe_group_members`` for
        stack resource types): when a grant moves required → available
        the plugin must stay in each member's served set, so a
        subscription row is materialized per member. Idempotent via
        ON CONFLICT DO NOTHING. Returns the number of newly-created rows.
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO user_plugin_optouts
               (user_id, marketplace_id, plugin_name)
               SELECT m.user_id, ?, ? FROM user_group_members m
               WHERE m.group_id = ?
               ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING""",
            [marketplace_id, plugin_name, group_id],
        )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        return max(0, int(after) - int(before))


    def stack_counts(self) -> Dict[Tuple[str, str], int]:
        """Return ``{(marketplace_id, plugin_name): subscriber_count}`` for
        every curated plugin with at least one subscriber.

        Post-v28, row PRESENCE in ``user_plugin_optouts`` means the user is
        subscribed. An Automatic-for-everyone (``is_system``) plugin has NO
        rows here — the flag is resolved at read time rather than fanned out —
        so it counts 0 and the caller overlays the total user count (see
        ``app.api.marketplace._load_curated_stack_counts``). Backs the marketplace
        listing/detail pages' subscriber-count badge
        (``app.api.marketplace._load_curated_stack_counts``) — one query per
        page render, avoiding N+1.
        """
        rows = self.conn.execute(
            """
            SELECT marketplace_id, plugin_name, COUNT(DISTINCT user_id)
            FROM user_plugin_optouts
            GROUP BY marketplace_id, plugin_name
            """
        ).fetchall()
        return {(r[0], r[1]): int(r[2]) for r in rows}

