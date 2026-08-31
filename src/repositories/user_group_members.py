"""Repository for user → group membership.

Each row binds one user to one group with a `source` label tracking who
created the row. The source matters because multiple writers populate this
table:

  - ``google_sync``    — OAuth callback rewrites the user's Google-derived
                         memberships on every login (DELETE+INSERT scoped to
                         this source).
  - ``microsoft_sync``  — OAuth callback rewrites the user's Entra
                         ID-derived memberships on every login, same
                         DELETE+INSERT shape, config-gated (off by default
                         — see app.auth.microsoft_group_sync).
  - ``admin``          — admin UI/CLI manual additions; survives sync.
  - ``system_seed``    — deploy-time seeds (Admin grant for SEED_ADMIN_EMAIL);
                         survives sync and refuses removal via the
                         admin path. The auto-Everyone seed for every new
                         user was removed when Google-prefix mapping landed
                         — explicit grants only.

``replace_synced_groups`` is the shared bulk-replace primitive;
``replace_google_sync_groups`` / ``replace_microsoft_sync_groups`` are thin,
source-pinned wrappers over it, one per provider. ``add_member`` /
``remove_member`` cover admin actions.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import duckdb

# Same-user concurrent logins both rewrite the user's google_sync rows. The
# shared singleton connection (get_system_db) gives each request its own
# cursor with an independent transaction, but DuckDB uses optimistic
# concurrency: two transactions deleting the same (user_id, source) tuples
# don't block — the loser raises a TransactionException ("Conflict on tuple
# deletion!"). Retry a few times so a racing login isn't silently dropped by
# the fail-soft OAuth caller. Postgres serializes these via row locks, so its
# sibling needs no retry.
_SYNC_CONFLICT_RETRIES = 3
_SYNC_CONFLICT_BACKOFF_S = 0.05


class UserGroupMembersRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def list_groups_for_user(self, user_id: str) -> List[str]:
        """Group IDs this user belongs to (any source)."""
        rows = self.conn.execute(
            "SELECT group_id FROM user_group_members WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]

    def list_group_names_for_user(self, user_id: str) -> List[str]:
        """Group ``name`` values (not ids) this user belongs to, any source.

        Powers audience-filtering — callers build ``group:<name>`` tokens
        against ``knowledge_items.audience``. Kept separate from
        ``list_groups_with_meta_for_user`` (which returns full dict rows and
        is ordered for display) since audience-filtering just needs the bare
        name list, unordered.
        """
        rows = self.conn.execute(
            """SELECT g.name FROM user_group_members m
               JOIN user_groups g ON m.group_id = g.id
               WHERE m.user_id = ?""",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]

    def list_members_for_group(self, group_id: str) -> List[Dict[str, Any]]:
        """All users in a group, joined with users table for display data."""
        rows = self.conn.execute(
            """SELECT u.id, u.email, u.name, u.active,
                      m.source, m.added_at, m.added_by
               FROM user_group_members m
               JOIN users u ON u.id = m.user_id
               WHERE m.group_id = ?
               ORDER BY u.email""",
            [group_id],
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def has_membership(self, user_id: str, group_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM user_group_members WHERE user_id = ? AND group_id = ?",
            [user_id, group_id],
        ).fetchone()
        return row is not None

    def add_member(
        self,
        user_id: str,
        group_id: str,
        source: str,
        added_by: Optional[str] = None,
    ) -> None:
        """Insert a membership row. Idempotent on (user_id, group_id) PK.

        Re-adding an existing pair is a silent no-op — the source/added_by of
        the existing row stays. Use ``replace_google_sync_groups`` if you
        want google_sync rows to refresh wholesale.
        """
        try:
            self.conn.execute(
                """INSERT INTO user_group_members
                   (user_id, group_id, source, added_by)
                   VALUES (?, ?, ?, ?)""",
                [user_id, group_id, source, added_by],
            )
        except duckdb.ConstraintException:
            pass  # already a member; preserve original source

    def remove_member(
        self,
        user_id: str,
        group_id: str,
        require_source: Optional[str] = None,
    ) -> bool:
        """Delete a membership row. Returns True if a row was deleted.

        ``require_source`` blocks the delete unless the row matches that
        source — admin UI passes ``'admin'`` so it cannot accidentally undo
        a system seed or a Google sync (Google sync rolls itself back via
        ``replace_google_sync_groups``).
        """
        if require_source is not None:
            res = self.conn.execute(
                """DELETE FROM user_group_members
                   WHERE user_id = ? AND group_id = ? AND source = ?
                   RETURNING 1""",
                [user_id, group_id, require_source],
            ).fetchone()
        else:
            res = self.conn.execute(
                """DELETE FROM user_group_members
                   WHERE user_id = ? AND group_id = ?
                   RETURNING 1""",
                [user_id, group_id],
            ).fetchone()
        return res is not None

    def replace_synced_groups(
        self,
        user_id: str,
        group_ids: List[str],
        source: str,
        added_by: str,
    ) -> None:
        """Authoritative refresh of this user's ``source``-tagged memberships.

        DELETEs every row with this ``source`` for this user, then INSERTs
        one row per ``group_ids``. Rows of every OTHER source (admin,
        system_seed, a different provider's sync) are untouched. Called from
        a provider's OAuth callback on every login so the membership
        reflects that provider's current directory state — the shared
        primitive behind ``replace_google_sync_groups`` and
        ``replace_microsoft_sync_groups`` below; call this directly for a
        source those wrappers don't cover.

        Wrapped in a single transaction so concurrent readers never observe
        the post-DELETE / pre-INSERT window where the user has *no*
        ``source`` groups. ``get_system_db()`` hands every caller a cursor
        on one shared connection, so a non-atomic rebuild leaks an empty
        intermediate state to anything reading membership mid-refresh — e.g.
        the marketplace git endpoint resolving a user's served plugin set,
        which would transiently drop every plugin granted via a synced
        group until the re-INSERTs commit. Mirrors the PG repo's
        ``self._engine.begin()`` atomicity (cross-engine parity).

        Retries on a DuckDB write-write conflict (see ``_SYNC_CONFLICT_*``)
        so two concurrent logins for the same user don't lose a refresh.
        """
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_SYNC_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    "DELETE FROM user_group_members WHERE user_id = ? AND source = ?",
                    [user_id, source],
                )
                for group_id in group_ids:
                    # ON CONFLICT DO NOTHING: an Admin / system_seed row (or a
                    # DIFFERENT provider's sync row) may already own this
                    # (user_id, group_id) pair — the user is a member through
                    # a higher-priority/other source, leave it. Using the
                    # conflict clause instead of catching ConstraintException
                    # keeps the surrounding transaction alive (a raised
                    # constraint error would otherwise abort it). Matches PG.
                    self.conn.execute(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT (user_id, group_id) DO NOTHING""",
                        [user_id, group_id, source, added_by],
                    )
                self.conn.execute("COMMIT")
                return
            except duckdb.TransactionException as e:
                # Lost an optimistic-concurrency race with a concurrent
                # same-user login. Roll back (best-effort — the txn may
                # already be aborted) and retry with a short backoff.
                self._safe_rollback()
                last_err = e
                time.sleep(_SYNC_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        # Exhausted retries — surface the last conflict to the caller.
        if last_err is not None:
            raise last_err

    def replace_group_members_for_source(self, group_id: str, user_ids: List[str], source: str, added_by: str) -> None:
        """Authoritative refresh of this GROUP's ``source``-tagged membership —
        the group-oriented transpose of :meth:`replace_synced_groups` for
        resource-driven syncs (one connection sweep computes one group's full
        member set; the user-oriented primitive would clobber concurrent
        connections' rows for shared users). Same source-segregation invariant,
        same single-transaction atomicity, same conflict-retry loop."""
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_SYNC_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    "DELETE FROM user_group_members WHERE group_id = ? AND source = ?",
                    [group_id, source],
                )
                for user_id in user_ids:
                    self.conn.execute(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT (user_id, group_id) DO NOTHING""",
                        [user_id, group_id, source, added_by],
                    )
                self.conn.execute("COMMIT")
                return
            except duckdb.TransactionException as e:
                self._safe_rollback()
                last_err = e
                time.sleep(_SYNC_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        if last_err is not None:
            raise last_err

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

    def _safe_rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    def remove_user_from_all_groups(self, user_id: str) -> int:
        """Hard delete every membership for a user. Used on user deletion.

        Returns the number of rows removed. Doesn't filter by source — the
        user is going away, every reference goes with them.
        """
        rows = self.conn.execute(
            "DELETE FROM user_group_members WHERE user_id = ? RETURNING 1",
            [user_id],
        ).fetchall()
        return len(rows)

    def count_members(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM user_group_members WHERE group_id = ?",
            [group_id],
        ).fetchone()
        return int(row[0]) if row else 0

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every membership row pointing at ``group_id``.

        Used by group-delete cascade in ``app/api/access.py`` so a group
        row's removal doesn't leave dangling membership rows.
        """
        rows = self.conn.execute(
            "DELETE FROM user_group_members WHERE group_id = ? RETURNING 1",
            [group_id],
        ).fetchall()
        return len(rows)

    def list_groups_with_meta_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return groups the user is in joined with the groups table.

        Each row: ``{group_id, id, name, description, is_system,
        created_by, source, added_at}`` (``id`` aliases ``group_id`` for
        callers that key off the group's own id). Powers the user-detail
        endpoints in ``app.api.users`` and the ``/me/profile`` page that
        need the membership graph + group metadata in a single round-trip.
        """
        rows = self.conn.execute(
            """SELECT g.id, g.name, g.description, g.is_system, g.created_by,
                      m.source, m.added_at
               FROM user_group_members m
               JOIN user_groups g ON g.id = m.group_id
               WHERE m.user_id = ?
               ORDER BY g.is_system DESC, g.name""",
            [user_id],
        ).fetchall()
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

        Each row: ``{name, external_id}``. ``user_groups`` may not carry an
        ``external_id`` column on every schema (Postgres has none) — probe
        ``information_schema`` and SELECT it only if present, else NULL.
        """
        has_ext = self.conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'user_groups' AND column_name = 'external_id'"
        ).fetchone()
        select_ext = "g.external_id" if has_ext else "NULL"
        rows = self.conn.execute(
            f"""SELECT g.name, {select_ext} AS external_id
                  FROM user_group_members m
                  JOIN user_groups g ON g.id = m.group_id
                 WHERE m.user_id = ? AND m.source = 'google_sync'
                 ORDER BY g.name""",
            [user_id],
        ).fetchall()
        return [{"name": r[0], "external_id": r[1]} for r in rows]

    def has_any_google_sync_membership(self, user_id: str) -> bool:
        """Whether the user has any prior `source='google_sync'` row.

        Used by the OAuth callback to distinguish a brand-new login (where
        an empty fetch from Cloud Identity might mean the user genuinely
        has no Workspace groups) from a returning user with a previously
        cached membership snapshot. Returning users get a pass-through on
        empty fetch (transient API failures must not lock them out); a
        fresh-login no-cache empty fetch is treated identically by the
        current callback (pass-through), so this helper is presently
        diagnostic — kept here so a future tightening of the gate can
        flip the branch without a new query path.
        """
        row = self.conn.execute(
            "SELECT 1 FROM user_group_members WHERE user_id = ? AND source = 'google_sync' LIMIT 1",
            [user_id],
        ).fetchone()
        return row is not None

    def google_sync_summary(self, user_id: str) -> Dict[str, Any]:
        """Count + most recent ``added_at`` of the user's
        ``source='google_sync'`` rows.

        Backs the /me/profile diagnostic's "when did Agnes last hear from
        Google about me?" panel (``app.api.me_debug._last_sync_summary``).
        Not authoritative timestamps — Google sync writes DELETE+INSERT
        every login, so all rows share the same ``added_at`` — but
        sufficient for that purpose.
        """
        # Deliberately NOT ``SELECT COUNT(*), MAX(added_at) … WHERE user_id = ?
        # AND source = 'google_sync'``. On DuckDB 1.5.2 that exact shape —
        # a MIN/MAX aggregate over an ART index scan on the (user_id,
        # group_id) primary key, plus a second predicate that eliminates
        # every row the index found — crashes in the optimizer with
        # ``INTERNAL Error: Attempted to access index 0 within vector of
        # size 0`` (it fails at EXPLAIN time, so it is a planning bug, not
        # a data one). That is the COMMON case, not an edge: any user who
        # has group memberships but none from Google sync. It took the
        # whole /me/profile page down with a 500.
        #
        # Folding the rows in Python sidesteps the aggregate entirely and
        # costs nothing — a user has a handful of memberships.
        rows = self.conn.execute(
            """SELECT added_at
                 FROM user_group_members
                WHERE user_id = ? AND source = 'google_sync'""",
            [user_id],
        ).fetchall()
        stamps = [r[0] for r in rows if r[0] is not None]
        return {"count": len(rows), "last_added_at": max(stamps) if stamps else None}
