"""Postgres repository for ``issue_reports`` / ``issue_comments``.

PG-ONLY (A3 PG-first ratchet). No DuckDB sibling; registered ``PG``-only in
:data:`src.repositories._REGISTRY`; resolving it on a DuckDB-backed instance
raises :class:`src.repositories.RequiresPostgresBackend` (a typed ``501`` at
the API). ``created_by`` is the USER ID, which is what the internal-table
row filter compares against.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_NUMBER_RE = re.compile(r"^#?(\d{1,9})$")

_ROW_SELECT = """
    SELECT r.*,
           (SELECT COUNT(*) FROM issue_comments c WHERE c.issue_id = r.id) AS comment_count
    FROM issue_reports r
"""


class IssueAlreadyResolved(Exception):
    """``resolve()`` on a row that is already ``resolved`` — the first
    resolver's signature must not be overwritten."""


def _row(mapping: Any) -> dict[str, Any]:
    d = dict(mapping)
    ctx = d.get("context_json")
    if isinstance(ctx, str):
        d["context_json"] = json.loads(ctx)
    return d


class IssueReportsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # -- reads ---------------------------------------------------------
    def get(self, issue_ref: str) -> dict[str, Any] | None:
        m = _NUMBER_RE.match(issue_ref.strip())
        where = "r.number = :n" if m else "r.id = :id"
        params = {"n": int(m.group(1))} if m else {"id": issue_ref.strip()}
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(_ROW_SELECT + f" WHERE {where}"), params).mappings().first()
        return _row(row) if row else None

    def list_for_user(self, user_id: str, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("r.created_by = :uid", {"uid": user_id}, status=status, limit=limit)

    def list_all(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._list("TRUE", {}, status=status, limit=limit)

    def count_for_user(self, user_id: str, *, status: str | None = None) -> int:
        return self._count("created_by = :uid", {"uid": user_id}, status)

    def count_all(self, *, status: str | None = None) -> int:
        return self._count("TRUE", {}, status)

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM issue_comments WHERE issue_id = :id ORDER BY created_at, id"),
                    {"id": issue_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    # -- writes --------------------------------------------------------
    def create(
        self,
        *,
        title: str,
        body: str | None,
        kind: str,
        created_by: str,
        created_by_email: str | None,
        source_surface: str,
        page_url: str | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        issue_id = f"iss_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO issue_reports
                      (id, title, body, kind, status, created_by, created_by_email, source_surface, page_url, context_json,
                       created_at, updated_at, last_activity_at)
                    VALUES
                      (:id, :title, :body, :kind, 'open', :created_by, :created_by_email, :source_surface, :page_url,
                       CAST(:context AS jsonb), current_timestamp, current_timestamp, current_timestamp)
                    """
                ),
                {
                    "id": issue_id,
                    "title": title,
                    "body": body,
                    "kind": kind,
                    "created_by": created_by,
                    "created_by_email": created_by_email,
                    "source_surface": source_surface,
                    "page_url": page_url,
                    "context": json.dumps(context) if context is not None else None,
                },
            )
        return self.get(issue_id)  # type: ignore[return-value]

    def add_comment(
        self, issue_id: str, *, author_id: str | None, author_email: str | None, author_kind: str, body: str
    ) -> dict[str, Any]:
        comment_id = f"isc_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            owner = conn.execute(
                sa.text("SELECT created_by FROM issue_reports WHERE id = :id"), {"id": issue_id}
            ).scalar()
            if owner is None:
                raise KeyError(issue_id)
            conn.execute(
                sa.text(
                    """
                    INSERT INTO issue_comments (id, issue_id, issue_owner_id, author_id, author_email, author_kind, body, created_at)
                    VALUES (:id, :issue_id, :owner, :author_id, :author_email, :author_kind, :body, current_timestamp)
                    """
                ),
                {
                    "id": comment_id,
                    "issue_id": issue_id,
                    "owner": owner,
                    "author_id": author_id,
                    "author_email": author_email,
                    "author_kind": author_kind,
                    "body": body,
                },
            )
            conn.execute(
                sa.text(
                    "UPDATE issue_reports SET last_activity_at = current_timestamp, updated_at = current_timestamp WHERE id = :id"
                ),
                {"id": issue_id},
            )
            row = (
                conn.execute(sa.text("SELECT * FROM issue_comments WHERE id = :id"), {"id": comment_id})
                .mappings()
                .first()
            )
        return dict(row)

    def resolve(self, issue_id: str, *, resolved_by: str, resolution_note: str | None) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            status = conn.execute(sa.text("SELECT status FROM issue_reports WHERE id = :id"), {"id": issue_id}).scalar()
            if status is None:
                return None
            if status == "resolved":
                raise IssueAlreadyResolved(issue_id)
            conn.execute(
                sa.text(
                    """
                    UPDATE issue_reports
                       SET status = 'resolved', resolved_at = current_timestamp, resolved_by = :by,
                           resolution_note = :note, updated_at = current_timestamp, last_activity_at = current_timestamp
                     WHERE id = :id AND status <> 'resolved'
                    """
                ),
                {"id": issue_id, "by": resolved_by, "note": resolution_note},
            )
        return self.get(issue_id)

    def set_screenshot(self, issue_id: str, relative_path: str) -> None:
        self._update(issue_id, "screenshot_path = :v", {"v": relative_path})

    def mark_webhook_delivered(self, issue_id: str) -> None:
        self._update(issue_id, "webhook_delivered_at = current_timestamp", {})

    # -- helpers -------------------------------------------------------
    def _list(self, where: str, params: dict[str, Any], *, status: str | None, limit: int) -> list[dict[str, Any]]:
        if status:
            where += " AND r.status = :status"
            params = {**params, "status": status}
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        _ROW_SELECT + f" WHERE {where} ORDER BY r.last_activity_at DESC, r.number DESC LIMIT :limit"
                    ),
                    {**params, "limit": max(1, min(int(limit), 500))},
                )
                .mappings()
                .all()
            )
        return [_row(r) for r in rows]

    def _count(self, where: str, params: dict[str, Any], status: str | None) -> int:
        if status:
            where += " AND status = :status"
            params = {**params, "status": status}
        with self._engine.connect() as conn:
            return int(conn.execute(sa.text(f"SELECT COUNT(*) FROM issue_reports WHERE {where}"), params).scalar() or 0)

    def _update(self, issue_id: str, set_clause: str, params: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE issue_reports SET {set_clause}, updated_at = current_timestamp WHERE id = :id"),
                {**params, "id": issue_id},
            )
