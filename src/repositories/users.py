"""Repository for user management."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


# The report projects these columns and no others. `users` also carries
# `password_hash`, `setup_token` and `reset_token`: a `SELECT *` here would put
# password hashes and live one-time-link tokens into `--json`, which an operator
# reconciling accounts is likely to redirect to a file or paste into a ticket.
# `last_pull_at` and `has_password` earn their place — they are how you tell
# which duplicate is the one actually in use.
DUPLICATE_REPORT_COLUMNS = (
    "id",
    "email",
    "name",
    "active",
    "created_at",
    "updated_at",
    "deactivated_at",
    "deactivated_by",
    "onboarded",
    "last_pull_at",
)

_DUPLICATE_SELECT = ", ".join(DUPLICATE_REPORT_COLUMNS) + ", (password_hash IS NOT NULL) AS has_password"

# The fold that decides what counts as "the same address" is done in Python, not
# SQL, and the row set is deliberately unfiltered.
#
# The obvious shape — GROUP BY lower(trim(email)) HAVING COUNT(*) > 1 — is
# subtly wrong, because SQL `trim()` strips spaces ONLY. A pair padded with a
# tab or a newline lands in two group keys of one row each, `HAVING` drops both,
# and the collision is never reported: exactly the unreachable-row class this
# report exists for, silently missing. Python's `str.strip()` covers all
# whitespace, so folding there makes the two backends agree by construction
# rather than by matching two dialects' idea of `trim` — the property that
# failed here once already.
#
# The cost is reading the users table whole. This is an operator diagnostic run
# by hand against an org-sized table, so that is the right trade for a fold that
# cannot drift.
_DUPLICATE_SQL = f"SELECT {_DUPLICATE_SELECT} FROM users"


def _order_key(row: Dict[str, Any]):
    """``ORDER BY created_at NULLS LAST, id`` — the get_by_email_ci tie-break,
    in Python. The ``0`` stands in for a missing timestamp and is only ever
    compared against another ``0``: the leading flag differs first whenever one
    side is NULL, so a datetime is never compared with an int."""
    ts = row.get("created_at")
    return (ts is None, ts if ts is not None else 0, str(row.get("id") or ""))


def _group_case_variants(rows) -> List[Dict[str, Any]]:
    """Fold a stream of user rows into duplicate groups.

    Shared by the DuckDB and Postgres repositories, and it does the whole job —
    folding, ordering and the >1 filter — so the two backends cannot disagree
    about which rows collide. Takes rows in any order.

    ``resolved_id`` is the row a sign-in actually lands on, and that is NOT
    simply the first row of the group. ``get_by_email_ci`` matches
    ``lower(email)`` without trimming, so a stored address carrying whitespace
    matches nothing at all — the caller strips its *input*, not the column. Such
    a row is reachable by no door, which is why it is grouped here (it is the
    same address to a person) and flagged rather than treated as the winner.
    A group where every row is padded has ``resolved_id = None``: nobody can
    sign in to that address.
    """
    by_address: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_address.setdefault((row.get("email") or "").strip().lower(), []).append(row)

    groups: List[Dict[str, Any]] = [
        {"email": folded, "users": sorted(rows_, key=_order_key)}
        for folded, rows_ in sorted(by_address.items())
        if len(rows_) > 1
    ]
    for g in groups:
        g["count"] = len(g["users"])
        for u in g["users"]:
            # The exact predicate get_by_email_ci runs, evaluated per row.
            u["unreachable_by_sign_in"] = (u.get("email") or "").lower() != g["email"]
        reachable = [u for u in g["users"] if not u["unreachable_by_sign_in"]]
        g["resolved_id"] = reachable[0]["id"] if reachable else None
    return groups


class UserRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
        return self._row_to_dict(result)

    def get_by_ids(self, user_ids: List[str]) -> Dict[str, Optional[str]]:
        """Bulk map ``user_id → email`` for the given ids. Missing rows are
        absent from the dict; an empty input returns ``{}``. Used by callers
        that previously ran a raw ``SELECT id, email ... WHERE id IN (...)`` on
        a system connection (#518) — routing through the factory keeps the read
        on the active backend."""
        ids = list(user_ids)
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        rows = self.conn.execute(f"SELECT id, email FROM users WHERE id IN ({placeholders})", ids).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_info_by_ids(self, user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Bulk map ``user_id → {'email', 'name'}`` for the given ids. Missing
        rows are absent from the dict; an empty input returns ``{}``. Wider
        sibling of :meth:`get_by_ids` (which returns only ``{id: email}``) used
        by callers that need a readable label (``name <email>``) — previously a
        raw ``SELECT id, email, name ... WHERE id IN (...)`` on a system
        connection (app/api/activity.py); routing through the factory keeps the
        read on the active backend."""
        ids = list(user_ids)
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        rows = self.conn.execute(f"SELECT id, email, name FROM users WHERE id IN ({placeholders})", ids).fetchall()
        return {r[0]: {"email": r[1], "name": r[2]} for r in rows}

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
        return self._row_to_dict(result)

    def get_by_email_ci(self, email: str) -> Optional[Dict[str, Any]]:
        """Resolve a user by email, case-insensitively.

        ``get_by_email`` is an exact string match (``=`` is case-sensitive on
        DuckDB and Postgres alike), so one person signing in through two
        providers that normalize the claim differently — Microsoft lower-cases
        it, Google passes the raw ``email`` claim through — would otherwise get
        two accounts. Backs ``app.auth.provisioning.ensure_user``. Historic
        rows may already differ only in case: the OLDEST wins, so the answer is
        deterministic and the original account keeps the identity. ``created_at``
        is not unique — rows written together tie — so ``id`` breaks the tie and
        both engines land on the same row.

        The ordering deliberately does NOT prefer an active row. Callers feed
        this row straight into a deactivated gate, and ranking active rows
        first would mean an operator who disables the account they can see
        silently hands the person the other, still-enabled variant — a
        different account id with different group memberships. A stale disabled
        variant shadowing the live account is the opposite failure, and it is
        the safe one: a wrongly-refused sign-in is visible and fixable, a
        bypassed deactivation is neither. Instances carrying such duplicates
        want a reconciliation pass; a read-only report that lists them is the
        queued follow-up."""
        result = self.conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?) ORDER BY created_at NULLS LAST, id LIMIT 1",
            [email],
        ).fetchone()
        return self._row_to_dict(result)

    def list_by_email_ci(self, email: str) -> List[Dict[str, Any]]:
        """Every row whose email matches case-insensitively, oldest first.

        ``get_by_email_ci`` answers "which account is this address" and so
        returns exactly one row. This answers "which rows collide on this
        address" — needed wherever a specific row must be identified by
        something other than the address (the reset-token peek) and by the
        operator-facing duplicate report."""
        rows = self.conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?) ORDER BY created_at NULLS LAST, id",
            [email],
        ).fetchall()
        return [d for d in (self._row_to_dict(r) for r in rows) if d is not None]

    def list_case_variant_duplicates(self) -> List[Dict[str, Any]]:
        """Every address held by more than one account, grouped.

        The reconciliation report queued by :meth:`get_by_email_ci`. That
        method has to pick ONE row when case variants coexist, and it picks the
        oldest — deterministic, but it means a person can own a second account
        that no sign-in will ever resolve to, and an operator who deactivates
        the row they can see may not have disabled the identity at all. Nothing
        surfaces that today; ``users`` is UNIQUE on ``email``, so the collision
        is invisible to every constraint and every list view sorted by address.

        Returns one entry per colliding address::

            {"email": <folded address>, "count": N, "resolved_id": <id>,
             "users": [<full row>, ...]}

        ``users`` is ordered exactly as :meth:`get_by_email_ci` orders, so
        ``users[0]`` is the row that sign-in resolves to and ``resolved_id``
        names it without the caller re-deriving the tie-break. Groups come back
        ordered by folded address so two runs — and two backends — agree.

        Read-only by design: which row to keep is a judgement call (group
        memberships, PATs and sessions hang off the id), so this reports and
        the operator merges."""
        rows = self.conn.execute(_DUPLICATE_SQL).fetchall()
        return _group_case_variants(d for d in (self._row_to_dict(r) for r in rows) if d is not None)

    def get_by_email_prefix(self, local_part: str) -> Optional[Dict[str, Any]]:
        """Resolve the single user whose email's local part (before ``@``)
        matches *local_part* exactly, picking the most recently updated when
        multiple domains share the same local part. Backs
        ``services.session_pipeline.runner.resolve_user_identity`` — maps a
        session-directory name (OS-username convention) to a user when it
        isn't a UUID. SQL LIKE metacharacters in *local_part* are escaped so
        literal underscores / percents in a username don't act as wildcards."""
        escaped = local_part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        result = self.conn.execute(
            "SELECT * FROM users WHERE email LIKE ? || '@%' ESCAPE '\\' ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            [escaped],
        ).fetchone()
        return self._row_to_dict(result)

    def get_by_slack_user_id(self, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """Resolve the account bound to a Slack ``user_id`` (NULL until the
        analyst redeems a /agnes verification code). Used by the Slack bot to
        map an incoming Slack identity to an Agnes user."""
        result = self.conn.execute("SELECT * FROM users WHERE slack_user_id = ?", [slack_user_id]).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> List[Dict[str, Any]]:
        """Return EVERY user row. Used by bootstrap-lock + startup
        warning paths that need to inspect the whole table (see
        ``app/auth/router.py::bootstrap`` and ``app/main.py``'s
        no-password-set warning). Do NOT add a LIMIT here — the
        bootstrap check ``[u for u in list_all() if u.get('password_hash')]``
        re-opens the endpoint if any password-holder gets paginated
        out, which would let an unauthenticated caller claim admin
        on instances with >LIMIT users. API-surface pagination uses
        ``list_paginated()`` below.
        """
        results = self.conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_paginated(self, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated user listing for the admin API surface (#336
        ADV-009). Safe to bound — callers explicitly opt into the
        windowed shape and the API enforces ``limit <= 10000`` at
        the Query()-validation layer. Do not call from bootstrap /
        startup paths that need exhaustive enumeration; use
        ``list_all()`` for those.
        """
        results = self.conn.execute("SELECT * FROM users ORDER BY email LIMIT ? OFFSET ?", [limit, offset]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def search_recent(
        self,
        limit: int = 10,
        search: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The N most recently registered users (``created_at`` DESC),
        optionally narrowed by a free-text ``search`` (email OR name, case
        -insensitive) and/or membership in ``group_id``.

        Backs the /admin/users page: the table shows only this
        bounded, server-filtered window instead of pulling every account to
        the client. ``EXISTS`` (not a JOIN) keeps the row set free of
        duplicates when a user holds the same group via multiple sources.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if search:
            clauses.append("(u.email ILIKE ? OR u.name ILIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        if group_id:
            clauses.append("EXISTS (SELECT 1 FROM user_group_members m WHERE m.user_id = u.id AND m.group_id = ?)")
            params.append(group_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        results = self.conn.execute(
            f"SELECT u.* FROM users u{where} ORDER BY u.created_at DESC NULLS LAST, u.email LIMIT ?",
            params,
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def count_all(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def any_password_holder(self, exclude_email: Optional[str] = None) -> bool:
        """Whether at least one user row holds a password hash — a
        ``SELECT 1 … LIMIT 1`` existence probe, NOT an enumeration.

        Backs the zero-door email rescue in ``app.auth.provider_registry``,
        which runs on every unauthenticated ``/login`` render of a no-OAuth
        instance — the whole point of this method is that such a hot,
        unauthenticated path must not pull the entire users table the way a
        ``list_all()`` scan would. ``exclude_email`` skips a synthetic
        account (the scheduler user) that carries a hash but cannot sign in
        interactively; the predicate mirrors the caller's previous Python
        filter exactly: a NULL or empty hash does not count, and a NULL
        email is never equal to the exclusion (``IS DISTINCT FROM``, which
        both engines support with identical semantics).
        """
        sql = "SELECT 1 FROM users WHERE password_hash IS NOT NULL AND password_hash <> ''"
        params: List[Any] = []
        if exclude_email is not None:
            sql += " AND email IS DISTINCT FROM ?"
            params.append(exclude_email)
        sql += " LIMIT 1"
        return self.conn.execute(sql, params).fetchone() is not None

    def create(
        self,
        id: str,
        email: str,
        name: str,
        password_hash: Optional[str] = None,
        must_change_password: bool = False,
    ) -> None:
        """Create a user. Group memberships are populated separately.

        Admin promotion happens via ``user_group_members`` (Admin system
        group), not a column on the user row — see ``app.auth.access`` and
        ``UserGroupMembersRepository``.

        New users are NOT auto-added to Everyone: the implicit membership
        was removed when Google-prefix mapping landed because access
        deployments need every membership to be traceable to a real source
        (admin grant, Google sync, or explicit system seed). If you need
        the previous "every new user is in Everyone" behavior, add a
        ``system_seed`` row in the caller after ``create``.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO users (id, email, name, password_hash, must_change_password, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, email, name, password_hash, must_change_password, now, now],
        )

    def update(self, id: str, **kwargs) -> None:
        # Group membership is materialized in `user_group_members`; writers
        # there go through `UserGroupMembersRepository` instead of `update`.
        # The legacy `role` column was dropped in v19.
        allowed = {
            "email",
            "name",
            "password_hash",
            "setup_token",
            "setup_token_created",
            "reset_token",
            "reset_token_created",
            "active",
            "deactivated_at",
            "deactivated_by",
            # v26: explicit "I've finished init" signal flipped by
            # /api/me/onboarded — kept out of the legacy allow-list
            # historically because the endpoint used raw conn.execute.
            "onboarded",
            # v44: per-user pull timestamp — bumped on /api/sync/manifest.
            "last_pull_at",
            # v71: Slack identity binding — set when the analyst redeems a
            # /agnes verification code (services/slack_bot/binding.py).
            "slack_user_id",
            # v77: forced-password-change flag (set on seeded/admin-set
            # passwords, cleared when the user sets their own).
            "must_change_password",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def consume_reset_token(self, *, email: str, token: str, cutoff, consume_id: str) -> Optional[str]:
        """Atomically consume a password-reset token: stamp it with ``consume_id``
        iff it is the valid, unexpired token for an active ``email`` (matched
        case-insensitively, like every other identity read).

        Returns the id of the row it stamped, or ``None`` when this call did not
        win (truthy/falsy either way, so a boolean caller still reads correctly).
        Returning the ROW is what keeps token ownership and identity resolution
        on the same account: the CAS finds whichever case variant actually holds
        the token, while ``get_by_email_ci`` deterministically returns the
        oldest — and a token minted by user id (admin-issued reset) can live on
        a newer variant. Resolving the account by address after the CAS would
        then mint a session for an account the token was never issued for. Goes through the
        repo (not a raw connection) so it runs on the ACTIVE backend — a raw
        DuckDB cursor here silently failed on Postgres instances.

        On a concurrent verify, DuckDB raises a TransactionContext conflict on
        the losing UPDATE; that means another caller won the CAS, so we report
        a loss (``False``) rather than letting the conflict surface as a 500.
        Postgres serializes the two UPDATEs instead (the loser matches zero
        rows), so it reaches the same ``False`` without raising."""
        try:
            self.conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_created = NULL "
                "WHERE lower(email) = lower(?) AND reset_token = ? AND reset_token_created IS NOT NULL "
                "AND reset_token_created >= ? AND active = TRUE",
                [consume_id, email, token, cutoff],
            )
        except Exception as exc:  # noqa: BLE001 — DuckDB optimistic-concurrency conflict
            err = str(exc).lower()
            if "conflict" in err or "transaction" in err:
                return False
            raise
        # Match on the stamp, not on "the row for this email": the address is
        # matched case-insensitively (the sign-in paths resolve identity with
        # get_by_email_ci, so the token was minted on whichever case-variant row
        # is the account), and a bare per-email SELECT would read an arbitrary
        # one of several variants and report a loss.
        row = self.conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(?) AND reset_token = ?",
            [email, consume_id],
        ).fetchone()
        return row[0] if row else None

    def count_admins(self, active_only: bool = True) -> int:
        """Count active users in the Admin system group."""
        sql = """
            SELECT COUNT(DISTINCT u.id)
            FROM users u
            JOIN user_group_members m ON m.user_id = u.id
            JOIN user_groups g ON g.id = m.group_id
            WHERE g.name = 'Admin'
        """
        if active_only:
            sql += " AND COALESCE(u.active, TRUE) = TRUE"
        result = self.conn.execute(sql).fetchone()
        return int(result[0]) if result else 0

    def update_display_name(self, user_id: str, name: str) -> None:
        """Persist a user-supplied display name for *user_id*.

        Self-service path (issue #1036): the caller has already validated that
        it owns the row (``get_current_user`` gate at the API layer). Only
        ``users.name`` and ``updated_at`` are touched — email, password hash,
        group memberships, and every other column are left unchanged.

        Google OAuth only sets ``name`` at account creation, not on subsequent
        logins, so calling this method does not risk being overwritten by a
        future sign-in or group-sync run.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
            [name, now, user_id],
        )

    def revoke_sessions(self, user_id: str) -> None:
        """Documented no-op on DuckDB (A3 ratchet — see
        ``migrations/versions/0082_session_revoked_before.py``).

        The PG sibling persists a ``session_revoked_before`` floor that
        ``app.auth.pat_resolver.resolve_token_to_user`` compares every
        ``typ="session"`` token's ``iat`` against. DuckDB's ``users`` table
        has no such column — kept here only for call-site symmetry (``POST
        /auth/logout`` calls ``users_repo().revoke_sessions(...)`` on either
        backend without branching), same pattern as
        ``LlmUsageRepository.insert_batch``'s ``caller_user_id`` no-op."""
        return None

    def delete(self, user_id: str) -> None:
        """Delete user + cascade their group memberships."""
        self.conn.execute(
            "DELETE FROM user_group_members WHERE user_id = ?",
            [user_id],
        )
        self.conn.execute("DELETE FROM users WHERE id = ?", [user_id])
