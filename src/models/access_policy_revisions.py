"""SQLAlchemy model for ``access_policy_revisions`` (PG-only, A3 ratchet —
no DuckDB sibling).

One row per saved state of a table's access policy (#1979 K1-sweep finding
1). It exists because the audit trail deliberately cannot serve as one:
``audit_log.params`` redacts ``access_policy_sql`` (content never enters the
trail — see ``app/api/admin.py::_SECRET_FIELDS``), so the audit row can say
that a policy changed and who changed it, but never WHAT it was. "Restore
this version" needs the body, and a store that keeps bodies is a different
thing from a store that keeps events.

Two properties the columns exist to preserve:

* **A cleared policy is a revision, not a gap.** ``policy_sql IS NULL``
  records the moment protection was removed — the single most important row
  in the history — rather than leaving it inferable only from a missing row.
* **A revision is dated by when it was SAVED, never when it was read or
  backfilled.** ``saved_at`` is written once; the backfilled baseline
  revision (the policy already stored on a table before this table existed)
  carries the original ``table_registry.access_policy_updated_at``.

Growth is bounded by admin behaviour, not by a sweeper: a row is written
only when an admin saves or clears a policy through
``PUT /api/admin/registry/{id}``. Nothing prunes it — a deliberately
retained record of who narrowed or widened access to a table, and to what.
Reads are capped instead (``AccessPolicyRevisionsPgRepository._MAX_LIMIT``).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class AccessPolicyRevision(Base):
    __tablename__ = "access_policy_revisions"
    __table_args__ = (sa.Index("idx_access_policy_revisions_table_saved", "table_id", "saved_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    #: ``table_registry.id``. Deliberately NOT a foreign key: unregistering a
    #: table drops its revisions explicitly (so a re-registered id cannot
    #: inherit a stranger's policy bodies), and a FK would additionally make
    #: this post-A3 PG-only table a hard dependency of a frozen pre-A3 one.
    table_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The policy body as saved. ``NULL`` means this revision IS the clear —
    #: the row records that protection was removed, with a note and an actor.
    policy_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The mandatory "why does this policy exist" (API-enforced whenever
    #: ``policy_sql`` is set), snapshotted so restoring a revision restores
    #: its reasoning too, not just its SQL.
    policy_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Whether the table was marked referenceable-from-other-policies at the
    #: time. Snapshot only — restoring a revision does not flip it back; it
    #: is here so a reader can see the shape the policy was written against.
    policy_mapping: Mapped[bool] = mapped_column(Boolean, server_default=sa.text("false"), nullable=False)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)
    #: The admin's email. Nullable: a backfilled baseline whose
    #: ``access_policy_updated_by`` was never recorded is still a real
    #: revision, and "unknown" is an honest answer where an invented one is
    #: not.
    saved_by: Mapped[str | None] = mapped_column(String, nullable=True)
