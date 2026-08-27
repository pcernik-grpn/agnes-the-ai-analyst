"""Postgres repository for ``semantic_health_mutes``.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/semantic_health_mutes.py`` DuckDB
sibling: the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving it
on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)".

A mute answers "an admin already knows about this check and does not want it
shouting". Every read below returns the whole row — ``muted_by``, ``muted_at``
and ``reason`` included — because a mute this layer could report WITHOUT its
author would be exactly the anonymous disappearance the feature exists to
prevent. There is no ``update``: changing your mind is unmute + mute, which
leaves the new author's name on the new judgement instead of quietly
overwriting whose it was.

Expiry is evaluated against the DATABASE clock (``current_timestamp``), not the
caller's: ``muted_at`` is written by the same clock, and mixing the two would
let a skewed app server report a mute as active a minute after the row it wrote
says it lapsed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Newest first, id as the tie-break so two mutes written in the same
# transaction-clock tick keep a stable order — a list that reshuffles between
# page loads reads as if rows appeared and vanished.
_ORDER = "ORDER BY muted_at DESC, id DESC"

_ACTIVE = "(expires_at IS NULL OR expires_at > current_timestamp)"


class SemanticHealthMutesPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        *,
        scope: str,
        muted_by: str,
        reason: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Mute one scope; returns the stored row.

        ``muted_by`` is keyword-required and has no default: a repository that
        could write an authorless mute is one bad callsite away from the silent
        disappearance this table exists to make impossible. ``expires_at``
        ``None`` means permanent — until somebody unmutes it.

        No uniqueness on ``scope``, deliberately: an expired mute stays as a
        record of who silenced what, and a UNIQUE would make that record
        permanently block re-muting the same check. "Already muted" is a
        question about ACTIVE mutes, which is :meth:`find_active_for_scope`.
        """
        mute_id = f"shm_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO semantic_health_mutes
                      (id, scope, reason, muted_by, muted_at, expires_at)
                    VALUES
                      (:id, :scope, :reason, :muted_by, current_timestamp, :expires_at)
                    """
                ),
                {
                    "id": mute_id,
                    "scope": scope,
                    "reason": reason,
                    "muted_by": muted_by,
                    "expires_at": expires_at,
                },
            )
        row = self.get(mute_id)
        assert row is not None  # just inserted in the same engine
        return row

    def get(self, mute_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM semantic_health_mutes WHERE id = :id"),
                    {"id": mute_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_active(self) -> List[Dict[str, Any]]:
        """Every mute still silencing something — the health roll-up's read."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text(f"SELECT * FROM semantic_health_mutes WHERE {_ACTIVE} {_ORDER}")).mappings().all()
            )
        return [dict(r) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        """Every mute ever written, expired ones included.

        The silence ends at the expiry; the record of who chose it does not.
        "Who muted this last quarter, and did they say why" is a question an
        admin inheriting a semantic layer asks first, and a list that dropped
        lapsed rows could not answer it.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(f"SELECT * FROM semantic_health_mutes {_ORDER}")).mappings().all()
        return [dict(r) for r in rows]

    def find_active_for_scope(self, scope: str) -> Optional[Dict[str, Any]]:
        """The active mute on ``scope``, if any — how a duplicate is caught.

        Two active rows for one scope would read as two independent judgements
        when it is one, and unmuting either would leave the check still silent
        with no visible reason why.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(f"SELECT * FROM semantic_health_mutes WHERE scope = :scope AND {_ACTIVE} {_ORDER}"),
                    {"scope": scope},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def delete(self, mute_id: str) -> bool:
        """Unmute. ``False`` when nothing matched, so the endpoint can answer
        404 instead of reporting an unmute that unmuted nothing."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM semantic_health_mutes WHERE id = :id"),
                {"id": mute_id},
            )
        return bool(result.rowcount)
