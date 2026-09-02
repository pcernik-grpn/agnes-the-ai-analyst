"""Postgres-only repository for ``access_policy_revisions`` — one row per
saved state of a table's access policy (#1979 K1-sweep finding 1).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.access_policy_revisions_repo()``; on a DuckDB-backed
instance that factory call raises ``RequiresPostgresBackend`` (translated to
a ``501`` by the app-wide handler in ``app/main.py``).

**Why a table and not the audit trail.** The obvious home for policy history
is ``audit_log`` — and it is where the modal's history panel originally read
from. It cannot serve this feature: commit 34ffc42 deliberately redacts
``access_policy_sql`` out of ``audit_log.params`` (content never enters the
trail), so an audit row can say a policy changed, by whom and when, but
never what it WAS. Restoring a version needs the body. This store keeps
bodies; the audit trail keeps events; neither replaces the other, and the
history panel shows the audit-derived version when this store is absent.

**Writes are never load-bearing.** The caller
(``app/api/admin.py::_record_access_policy_revision``) treats a failure to
record as a missing revision, not a failed policy save: an admin narrowing
access to a table must never be blocked because its history could not be
written.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: How many revisions a single ``list_for_table`` call may return. The
#: history panel asks for ten; this is the ceiling on what any caller can
#: ask for, so a modal can never be handed an unbounded scan of a table
#: whose policy has been edited for years.
_MAX_LIMIT = 50

#: What ``list_for_table`` returns when the caller states no preference —
#: the "last ~10 edits" the panel renders.
_DEFAULT_LIMIT = 10

#: Namespace for this module's advisory locks — the FIRST of the two int4
#: keys ``pg_advisory_xact_lock(int4, int4)`` takes, with ``hashtext(table_id)``
#: as the second. The two-key form (rather than a single ``hashtext``) keeps
#: this lock class in its own address space: any future advisory lock in
#: Agnes picks its own namespace and can never collide with a table id whose
#: hash happens to equal that lock's key. Within the namespace, a hash
#: collision between two table ids only costs a little needless
#: serialization -- never a missed lock. The value is the issue number.
_LOCK_NAMESPACE = 1979


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    saved_at = row.get("saved_at")
    if saved_at is not None and not isinstance(saved_at, str):
        row["saved_at"] = saved_at.isoformat()
    row["policy_mapping"] = bool(row.get("policy_mapping"))
    # Derived, never stored: a NULL body IS the "policy removed" event, and
    # every reader (API shape, modal renderer) must agree on that rule
    # rather than each re-deriving it from a falsiness check.
    row["cleared"] = row.get("policy_sql") is None
    return row


class AccessPolicyRevisionsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- serialization ----------------------------------------------------

    @contextmanager
    def policy_write_lock(self, table_id: str) -> Iterator[None]:
        """Serialize "save this table's policy, then append its revision".

        Two concurrent PUTs on one table could otherwise commit policy A,
        commit policy B, record B's revision, then record A's — leaving a
        history whose newest row (A) is not what the table actually stores
        (B), which is precisely the claim the panel makes and the body an
        admin would restore FROM.

        A **transaction-scoped** advisory lock, so the release is Postgres's
        job, not the caller's: the lock goes away when this transaction
        commits or rolls back, including when the ``with`` body raises or
        the process dies mid-save. Nothing is written in this transaction —
        it exists only to hold the lock; the policy write and the revision
        append keep their own transactions, so this ORDERS them without
        COUPLING them (a failed history append still never rolls back a
        policy save — see ``app/api/admin.py::_record_access_policy_revision``).

        Cross-process by construction, which the in-process alternative
        (a ``threading.Lock``) is not: an api and a worker replica saving
        the same table are the shape the ordering bug actually takes.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:ns, hashtext(:table_id))"),
                {"ns": _LOCK_NAMESPACE, "table_id": table_id},
            )
            yield

    # -- write ------------------------------------------------------------

    def record(
        self,
        *,
        table_id: str,
        policy_sql: Optional[str],
        policy_note: Optional[str],
        policy_mapping: bool = False,
        saved_by: Optional[str] = None,
        saved_at: Optional[datetime] = None,
    ) -> str:
        """Append the state a policy was just saved in, and return its id.

        ``policy_sql=None`` records a CLEAR — the row that says protection
        was removed, which is the one an inheriting admin most needs to find.

        ``saved_at`` is for backfilling a baseline revision from the
        already-stored ``table_registry.access_policy_updated_at``; it is
        then stored verbatim, because a history that re-dates the past is
        worse than one that is short. Left unset, the stamp is "now",
        nudged forward past this table's newest existing revision when the
        clock has not moved: two saves inside one tick must still order
        deterministically, since "newest first" is a claim about EDIT order
        and a tie would render (and offer for restore) the older body on top.
        """
        rev_id = "apr_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            if saved_at is None:
                newest = conn.execute(
                    sa.text("SELECT MAX(saved_at) FROM access_policy_revisions WHERE table_id = :table_id"),
                    {"table_id": table_id},
                ).scalar()
                saved_at = _now()
                if newest is not None:
                    if newest.tzinfo is None:
                        newest = newest.replace(tzinfo=timezone.utc)
                    if saved_at <= newest:
                        saved_at = newest + timedelta(microseconds=1)
            conn.execute(
                sa.text(
                    "INSERT INTO access_policy_revisions "
                    "(id, table_id, policy_sql, policy_note, policy_mapping, saved_at, saved_by) "
                    "VALUES (:id, :table_id, :policy_sql, :policy_note, :policy_mapping, :saved_at, :saved_by)"
                ),
                {
                    "id": rev_id,
                    "table_id": table_id,
                    "policy_sql": policy_sql,
                    "policy_note": policy_note,
                    "policy_mapping": bool(policy_mapping),
                    "saved_at": saved_at,
                    "saved_by": saved_by,
                },
            )
        return rev_id

    def delete_for_table(self, table_id: str) -> int:
        """Drop every revision for ``table_id`` and return how many went.

        Called when a table is unregistered: table ids are derived from
        names, so re-registering the same name yields the same id — without
        this, a brand-new table would inherit (and offer for restore) the
        policy bodies of the one that used to live at that id.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM access_policy_revisions WHERE table_id = :table_id"),
                {"table_id": table_id},
            )
        return int(result.rowcount or 0)

    # -- read -------------------------------------------------------------

    def get(self, revision_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM access_policy_revisions WHERE id = :id"),
                    {"id": revision_id},
                )
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list_for_table(self, table_id: str, *, limit: int = _DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """Most recent revisions first — the history panel's rows.

        ``limit`` is clamped into ``1..50``: an explicit ``0`` asks for
        nothing, which is a caller bug, and silently answering it with the
        default (the ``limit or DEFAULT`` idiom) hides that bug behind ten
        rows nobody asked for. ``None`` is the one spelling of "no
        preference".
        """
        limit = _DEFAULT_LIMIT if limit is None else int(limit)
        limit = max(1, min(limit, _MAX_LIMIT))
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM access_policy_revisions WHERE table_id = :table_id "
                        "ORDER BY saved_at DESC LIMIT :limit"
                    ),
                    {"table_id": table_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def count_for_table(self, table_id: str) -> int:
        """Total revisions recorded for this table — so a truncated panel
        can say "10 of 34" rather than silently showing a prefix."""
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT COUNT(*) FROM access_policy_revisions WHERE table_id = :table_id"),
                {"table_id": table_id},
            ).scalar()
        return int(value or 0)
