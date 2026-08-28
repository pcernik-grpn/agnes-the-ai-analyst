"""Postgres-only repository for ``share_requests`` (Track C6 — agent-sharing
approval queue).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.share_requests_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler in ``app/main.py``).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_VALID_STATUSES = frozenset({"pending", "approved", "rejected"})
_DECISIONS = frozenset({"approved", "rejected"})


class ShareRequestsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        *,
        resource_type: str,
        resource_id: str,
        requested_group_id: str,
        requested_by: str,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue a new pending request, or return the existing PENDING one
        for the same ``(resource_type, resource_id, requested_group_id)``
        unchanged — idempotent under a double-submitted PUT, backed by the
        migration's partial unique index for the race window between the
        check and the insert."""
        existing = self.get_pending(resource_type, resource_id, requested_group_id)
        if existing is not None:
            return existing

        req_id = "shrq_" + secrets.token_hex(8)
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text(
                        """INSERT INTO share_requests
                            (id, resource_type, resource_id, requested_group_id,
                             requested_by, status, note)
                        VALUES (:id, :rt, :rid, :gid, :by, 'pending', :note)"""
                    ),
                    {
                        "id": req_id,
                        "rt": resource_type,
                        "rid": resource_id,
                        "gid": requested_group_id,
                        "by": requested_by,
                        "note": note,
                    },
                )
        except sa.exc.IntegrityError:
            # Lost the race against a concurrent identical request — the
            # partial unique index rejected us; the winner's row is what
            # the caller should see.
            row = self.get_pending(resource_type, resource_id, requested_group_id)
            if row is not None:
                return row
            raise
        return self.get(req_id)  # type: ignore[return-value]

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM share_requests WHERE id = :id"), {"id": id}).mappings().first()
        return dict(row) if row else None

    def get_pending(self, resource_type: str, resource_id: str, requested_group_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """SELECT * FROM share_requests
                            WHERE resource_type = :rt AND resource_id = :rid
                              AND requested_group_id = :gid AND status = 'pending'"""
                    ),
                    {"rt": resource_type, "rid": resource_id, "gid": requested_group_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_pending_for_resource(self, resource_type: str, resource_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """SELECT * FROM share_requests
                            WHERE resource_type = :rt AND resource_id = :rid AND status = 'pending'
                            ORDER BY created_at"""
                    ),
                    {"rt": resource_type, "rid": resource_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_admin(
        self,
        *,
        status: Optional[List[str]] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Admin queue listing, newest first. ``status`` defaults to every
        row when omitted (unlike the store-submissions sibling, there is no
        lifecycle-end state to fold away by default — every decided row
        stays a useful audit trail, so the caller filters explicitly)."""
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if status:
            keys: List[str] = []
            for i, s in enumerate(status):
                k = f"st_{i}"
                keys.append(f":{k}")
                params[k] = s
            clauses.append(f"status IN ({','.join(keys)})")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._engine.connect() as conn:
            total = conn.execute(sa.text(f"SELECT COUNT(*) FROM share_requests {where}"), params).scalar() or 0
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT * FROM share_requests {where} ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset"
                    ),
                    {**params, "limit": int(limit), "offset": int(skip)},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows], int(total)

    def decide(self, id: str, *, status: str, decided_by: str) -> Optional[Dict[str, Any]]:
        """Transition a PENDING request to ``approved``/``rejected``.

        Returns ``None`` (no-op) when the request doesn't exist or was
        already decided — an atomic ``WHERE status = 'pending'`` guard, so a
        double-click can never flip an already-approved row to rejected (or
        double-write the grant on a second approve)."""
        if status not in _DECISIONS:
            raise ValueError(f"invalid decision status: {status!r}")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    """UPDATE share_requests
                          SET status = :status, decided_by = :by, decided_at = :now
                        WHERE id = :id AND status = 'pending'
                    RETURNING id"""
                ),
                {"status": status, "by": decided_by, "now": now, "id": id},
            ).first()
        return self.get(id) if row else None
