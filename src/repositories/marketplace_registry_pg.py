"""Postgres-backed marketplace_registry repository.

Mirrors ``src/repositories/marketplace_registry.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MarketplaceRegistryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def register(
        self,
        id: str,
        name: str,
        url: str,
        branch: Optional[str] = None,
        token_env: Optional[str] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
        curator_name: Optional[str] = None,
        curator_email: Optional[str] = None,
        is_builtin: bool = False,
        ref: Optional[str] = None,
    ) -> None:
        # is_builtin is excluded from ON CONFLICT SET — immutable after initial
        # seed so re-seeding on upgrade cannot flip admin-registered rows.
        # `ref` is always overwritten on conflict, mirroring `branch` — see
        # the DuckDB sibling for the rationale.
        #
        # last_error self-heal: a built-in row has no git remote, so it can
        # never legitimately fail a sync — any last_error sitting on one is
        # a fossil from before sync_one() refused built-in rows up front
        # (see MarketplaceNotSyncable). Clear it on every re-register of a
        # built-in row (i.e. every boot's seed_builtin_marketplace() call)
        # so a stale error can't get permanently stuck with no way to clear
        # it. Non-builtin rows are untouched — an admin edit must not wipe
        # a real sync failure off the board.
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO marketplace_registry
                        (id, name, url, branch, token_env, description, registered_by,
                         registered_at, curator_name, curator_email, is_builtin, ref)
                    VALUES (:id, :name, :url, :branch, :te, :desc, :rb, :now, :cn, :ce, :ib, :ref)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        url = EXCLUDED.url,
                        branch = EXCLUDED.branch,
                        token_env = EXCLUDED.token_env,
                        description = EXCLUDED.description,
                        curator_name = COALESCE(EXCLUDED.curator_name, marketplace_registry.curator_name),
                        curator_email = COALESCE(EXCLUDED.curator_email, marketplace_registry.curator_email),
                        ref = EXCLUDED.ref,
                        last_error = CASE WHEN EXCLUDED.is_builtin THEN NULL ELSE marketplace_registry.last_error END"""
                ),
                {
                    "id": id,
                    "name": name,
                    "url": url,
                    "branch": branch,
                    "te": token_env,
                    "desc": description,
                    "rb": registered_by,
                    "now": now,
                    "cn": curator_name,
                    "ce": curator_email,
                    "ib": is_builtin,
                    "ref": ref,
                },
            )

    def unregister(self, marketplace_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM marketplace_registry WHERE id = :id"),
                {"id": marketplace_id},
            )

    def get(self, marketplace_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM marketplace_registry WHERE id = :id"),
                    {"id": marketplace_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM marketplace_registry ORDER BY name")).mappings().all()
        return [dict(r) for r in rows]

    def list_builtin(self) -> List[Dict[str, Any]]:
        """Return only rows where is_builtin=TRUE, ordered by name."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text("SELECT * FROM marketplace_registry WHERE is_builtin = TRUE ORDER BY name"))
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_non_builtin(self) -> List[Dict[str, Any]]:
        """Return only admin-registered (non-built-in) rows, ordered by name.

        Used by the nightly git-sync path so it never tries to git-clone the
        built-in marketplace (which has no remote URL).
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text("SELECT * FROM marketplace_registry WHERE is_builtin = FALSE ORDER BY name"))
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def update_sync_status(
        self,
        marketplace_id: str,
        *,
        commit_sha: Optional[str] = None,
        synced_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        sets: List[str] = []
        params: Dict[str, Any] = {"id": marketplace_id}
        if synced_at is not None:
            sets.append("last_synced_at = :sa")
            params["sa"] = synced_at
        if commit_sha is not None:
            sets.append("last_commit_sha = :sha")
            params["sha"] = commit_sha
        if commit_sha is not None and error is None:
            sets.append("last_error = NULL")
        elif error is not None:
            sets.append("last_error = :err")
            params["err"] = error
        if not sets:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE marketplace_registry SET {', '.join(sets)} WHERE id = :id"),
                params,
            )
