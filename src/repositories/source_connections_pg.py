"""Postgres-backed SourceConnectionsRepository.

Mirrors ``src/repositories/source_connections.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SourceConnectionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        token_env: Optional[str] = None,
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> None:
        with self._engine.begin() as cx:
            if is_default:
                cx.execute(
                    sa.text("UPDATE source_connections SET is_default = FALSE WHERE source_type = :st"),
                    {"st": source_type},
                )
            cx.execute(
                sa.text(
                    """INSERT INTO source_connections
                       (id, name, source_type, config, token_env, is_default, created_by)
                       VALUES (:id, :name, :st, :config, :token_env, :is_default, :created_by)"""
                ),
                {
                    "id": id,
                    "name": name,
                    "st": source_type,
                    "config": json.dumps(config),
                    "token_env": token_env,
                    "is_default": is_default,
                    "created_by": created_by,
                },
            )

    def _fetch_one(self, sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as cx:
            row = cx.execute(sa.text(sql), params).mappings().fetchone()
        return self._decode(dict(row) if row else None)

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE id = :id", {"id": connection_id})

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE name = :n", {"n": name})

    def get_default(self, source_type: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = :st AND is_default ORDER BY created_at LIMIT 1",
            {"st": source_type},
        )

    def list(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM source_connections"
        params: Dict[str, Any] = {}
        if source_type:
            sql += " WHERE source_type = :st"
            params["st"] = source_type
        sql += " ORDER BY name"
        with self._engine.connect() as cx:
            rows = cx.execute(sa.text(sql), params).mappings().fetchall()
        return [self._decode(dict(r)) for r in rows]  # type: ignore[misc]

    def update(
        self,
        connection_id: str,
        *,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        token_env: Optional[str] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        # `name` backs the "Add data source" wizard's post-test rename
        # (#755) — see the DuckDB sibling's docstring for the rationale.
        with self._engine.begin() as cx:
            if name is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET name = :n WHERE id = :id"),
                    {"n": name, "id": connection_id},
                )
            if config is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET config = :c WHERE id = :id"),
                    {"c": json.dumps(config), "id": connection_id},
                )
            if token_env is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET token_env = :t WHERE id = :id"),
                    {"t": token_env, "id": connection_id},
                )
            if is_default is not None:
                if is_default:
                    # Promote: demote every other connection of the same
                    # source_type first (is_default is unique per source_type,
                    # enforced here — mirrors create()).
                    row = (
                        cx.execute(
                            sa.text("SELECT source_type FROM source_connections WHERE id = :id"),
                            {"id": connection_id},
                        )
                        .mappings()
                        .fetchone()
                    )
                    if row:
                        cx.execute(
                            sa.text("UPDATE source_connections SET is_default = FALSE WHERE source_type = :st"),
                            {"st": row["source_type"]},
                        )
                    cx.execute(
                        sa.text("UPDATE source_connections SET is_default = TRUE WHERE id = :id"),
                        {"id": connection_id},
                    )
                else:
                    cx.execute(
                        sa.text("UPDATE source_connections SET is_default = FALSE WHERE id = :id"),
                        {"id": connection_id},
                    )

    def config_patch(self, connection_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Mirrors the DuckDB sibling — see its docstring for the invariant
        this closes (NB-1: two writers race a config read-modify-write).

        ``config`` is stored as JSON-as-text here (``src/models/connections
        .py``'s ``Text`` column), not native ``jsonb``, so this can't lean
        on Postgres's ``||`` jsonb-concatenation operator to merge
        server-side in one statement. Instead it takes the row lock
        explicitly (``SELECT ... FOR UPDATE``) inside the same transaction
        as the UPDATE, so a concurrent ``config_patch`` on the SAME row
        blocks until this one commits rather than racing it — Postgres
        needs no separate retry loop the way the DuckDB sibling does.
        """
        with self._engine.begin() as cx:
            row = cx.execute(
                sa.text("SELECT config FROM source_connections WHERE id = :id FOR UPDATE"),
                {"id": connection_id},
            ).first()
            if row is None:
                return None
            current = row[0]
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except (json.JSONDecodeError, TypeError):
                    current = {}
            elif not isinstance(current, dict):
                current = {}
            merged = {**current, **patch}
            cx.execute(
                sa.text("UPDATE source_connections SET config = :c WHERE id = :id"),
                {"c": json.dumps(merged), "id": connection_id},
            )
            updated = (
                cx.execute(
                    sa.text("SELECT * FROM source_connections WHERE id = :id"),
                    {"id": connection_id},
                )
                .mappings()
                .first()
            )
        return self._decode(dict(updated) if updated else None)

    def delete(self, connection_id: str) -> None:
        with self._engine.begin() as cx:
            cx.execute(
                sa.text("DELETE FROM source_connections WHERE id = :id"),
                {"id": connection_id},
            )
