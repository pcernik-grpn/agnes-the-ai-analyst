"""Repository tracking which Store entities each user has installed.

Composite PK ``(user_id, entity_id)``. Install rows are surfaced into the
served Claude Code marketplace by ``src/marketplace_filter.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import duckdb

from src.repositories.store_submissions import BLOCKING_SUBMISSION_STATUS_SQL


class UserStoreInstallsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def install(self, user_id: str, entity_id: str) -> bool:
        """Insert idempotently. Returns True iff a new row was created."""
        existing = self.conn.execute(
            "SELECT 1 FROM user_store_installs WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        ).fetchone()
        if existing:
            return False
        self.conn.execute(
            "INSERT INTO user_store_installs (user_id, entity_id) VALUES (?, ?)",
            [user_id, entity_id],
        )
        return True

    def uninstall(self, user_id: str, entity_id: str) -> bool:
        """Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_store_installs WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM user_store_installs WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        )
        return True

    def install_for_group_members(self, group_id: str, entity_id: str) -> int:
        """Install ``entity_id`` for every current member of ``group_id``.

        The Required tier's fan-out. Curated plugins get "always in the stack"
        from ``user_plugin_optouts`` presence semantics, but a store entity is
        served through this per-person install table — so making one Required has
        to materialize a row per member, or the lock would be a label with
        nothing behind it.

        Members who join the group later are picked up by
        :meth:`install_required_for_user` at their next resolve. Idempotent via
        ON CONFLICT DO NOTHING; returns the number of newly-created rows.
        """
        before = self.installer_count(entity_id)
        self.conn.execute(
            """INSERT INTO user_store_installs (user_id, entity_id)
               SELECT m.user_id, ? FROM user_group_members m
               WHERE m.group_id = ?
               ON CONFLICT (user_id, entity_id) DO NOTHING""",
            [entity_id, group_id],
        )
        return max(0, self.installer_count(entity_id) - before)

    def install_required_for_user(self, user_id: str, entity_ids: List[str]) -> int:
        """Install every entity in ``entity_ids`` for ``user_id``.

        The join-later half of the Required fan-out: a user added to a group
        after the grant was written still has to end up with the item. Idempotent;
        returns the number of newly-created rows.
        """
        created = 0
        for entity_id in entity_ids:
            if self.install(user_id, entity_id):
                created += 1
        return created

    def list_for_user(self, user_id: str, granted_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """Joins store_entities so a single round-trip returns everything the
        UI / marketplace builder needs.

        Filters to approved + archived entities:

        * **approved** — current public entries.
        * **archived** — owner soft-deleted (or admin-archived) entries
          that previously-installed users keep getting served. Pulling
          them from the marketplace.zip would silently break a user's
          existing setup; archive intentionally preserves the install.

        * **hidden and granted to one of the caller's groups, never blocked by
          review** — ``granted_ids`` is that set, resolved by the caller (this
          layer does not read grants). Without it a ``store_entity`` grant was
          accepted and meant nothing: the row was written and the group was
          still served nothing. Passing an empty sequence is the old
          behaviour exactly.

        * **hidden, owned by the caller, never blocked by review** — an
          entity kept Private on purpose (``access='private'`` on upload, or
          the builder's Private choice). It is served only to its own author,
          so "Private skill in my own Stack" is reachable at all; nobody
          else's ``list_for_user`` can see it, because the branch is gated on
          ``se.owner_user_id = user_id``. No escalation: the author could
          equally have written the same file into their own workspace by hand.

        **Excluded** — pending / blocked, plus any hidden entity with a
        blocking submission in its history. A previously-installed entity
        that subsequently gets blocked by guardrail review must NOT continue
        serving until an admin override re-approves it, otherwise a known-bad
        bundle keeps reaching Claude Code. ``hidden`` is the status BOTH the
        Private choice and guardrail quarantine write, so the blocked-review
        probe is what separates the two — deliberately over ANY submission in
        the entity's history rather than only the latest one, so the safe
        direction wins on an ambiguous chain.
        """
        # Parameterized one-per-id, never interpolated: these arrive from a
        # grant table but they are still values in a query.
        granted = [str(g) for g in (granted_ids or [])]
        granted_sql = (
            " OR (se.visibility_status = 'hidden' AND se.id IN ("
            + ",".join("?" for _ in granted)
            + ") AND NOT EXISTS (SELECT 1 FROM store_submissions ss2 WHERE ss2.entity_id = se.id"
            + f" AND ss2.status IN ({BLOCKING_SUBMISSION_STATUS_SQL})))"
            if granted
            else ""
        )
        rows = self.conn.execute(
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
               WHERE usi.user_id = ?
                 AND (
                   se.visibility_status IN ('approved', 'archived')
                   OR (
                     se.visibility_status = 'hidden'
                     AND se.owner_user_id = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM store_submissions ss
                       WHERE ss.entity_id = se.id
                         AND ss.status IN ({BLOCKING_SUBMISSION_STATUS_SQL})
                     )
                   )
                   {granted_sql}
                 )
               ORDER BY usi.installed_at DESC, se.id""",
            [user_id, user_id] + granted,
        ).fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def is_installed(self, user_id: str, entity_id: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_store_installs WHERE user_id = ? AND entity_id = ?",
                [user_id, entity_id],
            ).fetchone()
        )

    def installer_count(self, entity_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        ).fetchone()
        return int(row[0]) if row else 0

    def delete_all_for_entity(self, entity_id: str) -> int:
        """Used by the entity-delete code path. Returns rows deleted."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        ).fetchone()
        before = row[0] if row else 0
        self.conn.execute(
            "DELETE FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        )
        return int(before)
