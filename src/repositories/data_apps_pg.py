"""Postgres-backed repository for ``data_apps``.

Mirrors ``src/repositories/data_apps.py`` (the DuckDB impl) on the
``DataAppsRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_data_apps_contract.py``.

Implementation differences vs. DuckDB:

- ``list_idle``'s idle-window predicate uses ``make_interval(secs => ...)``
  in place of DuckDB's ``? * INTERVAL 1 SECOND`` arithmetic — same
  semantics, PG flavor.
- Slug uniqueness surfaces as ``sqlalchemy.exc.IntegrityError`` (UNIQUE
  constraint violation) rather than ``duckdb.ConstraintException``; the
  contract test asserts on the per-backend exception type.
- ``update``/``delete`` use ``rowcount`` to report success rather than a
  pre-SELECT existence check, following the ``memory_domains_pg`` idiom.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_UPDATABLE = {
    "name",
    "description",
    "repo_url",
    "repo_branch",
    "runtime_tag",
    "secrets_enc",
    "env",
    "cpu_limit",
    "mem_limit",
    "idle_timeout_s",
    "sleep_mode",
    "service_token_id",
}


class DataAppsPgRepository:
    """Postgres twin of ``DataAppsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        *,
        slug: str,
        name: str,
        owner_user_id: str,
        description: str = "",
        repo_mode: str = "internal",
        repo_url: str = "",
        repo_branch: str = "main",
        idle_timeout_s: int = 1800,
        sleep_mode: str = "recreate",
        env: str = "{}",
    ) -> str:
        """Insert a new data app; returns the generated id (``app_<uuid12>``).

        Raises ``sqlalchemy.exc.IntegrityError`` if ``slug`` collides — the
        UNIQUE constraint on ``data_apps.slug`` is the source of truth.
        """
        app_id = "app_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO data_apps
                      (id, slug, name, description, owner_user_id, repo_mode,
                       repo_url, repo_branch, idle_timeout_s, sleep_mode, env)
                    VALUES
                      (:id, :slug, :name, :description, :owner_user_id, :repo_mode,
                       :repo_url, :repo_branch, :idle_timeout_s, :sleep_mode, :env)
                    """
                ),
                {
                    "id": app_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "owner_user_id": owner_user_id,
                    "repo_mode": repo_mode,
                    "repo_url": repo_url,
                    "repo_branch": repo_branch,
                    "idle_timeout_s": idle_timeout_s,
                    "sleep_mode": sleep_mode,
                    "env": env,
                },
            )
        return app_id

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM data_apps WHERE id = :id"),
                    {"id": app_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM data_apps WHERE slug = :slug"),
                    {"slug": slug},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list(
        self,
        *,
        owner_user_id: Optional[str] = None,
        state: Optional[str] = None,
        include_drafts: bool = True,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        if owner_user_id is not None:
            clauses.append("owner_user_id = :owner_user_id")
            params["owner_user_id"] = owner_user_id
        if state is not None:
            clauses.append("state = :state")
            params["state"] = state
        if not include_drafts:
            clauses.append("is_draft = false")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM data_apps {where} ORDER BY created_at DESC LIMIT :limit"),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def create_draft(
        self,
        *,
        parent_app_id: str,
        slug: str,
        branch: str,
        owner_user_id: str,
        idle_timeout_s: int = 1800,
        sleep_mode: str = "recreate",
    ) -> str:
        """Insert a draft copy of ``parent_app_id``; returns the new app id.

        Mirrors ``DataAppsRepository.create_draft``.
        """
        app_id = "app_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO data_apps
                      (id, slug, name, owner_user_id, repo_mode, parent_app_id, is_draft,
                       draft_branch, idle_timeout_s, sleep_mode)
                    VALUES
                      (:id, :slug, :name, :owner_user_id, 'internal', :parent_app_id, true,
                       :draft_branch, :idle_timeout_s, :sleep_mode)
                    """
                ),
                {
                    "id": app_id,
                    "slug": slug,
                    "name": f"{slug} (draft)",
                    "owner_user_id": owner_user_id,
                    "parent_app_id": parent_app_id,
                    "draft_branch": branch,
                    "idle_timeout_s": idle_timeout_s,
                    "sleep_mode": sleep_mode,
                },
            )
        return app_id

    def list_drafts(self, parent_app_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM data_apps WHERE parent_app_id = :parent_app_id "
                        "AND is_draft = true ORDER BY created_at DESC"
                    ),
                    {"parent_app_id": parent_app_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def update(self, app_id: str, **fields: Any) -> bool:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        if not fields:
            return False
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        params = {**fields, "id": app_id}
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(f"UPDATE data_apps SET {sets}, updated_at = now() WHERE id = :id"),
                params,
            )
            return (result.rowcount or 0) > 0

    def set_state(self, app_id: str, state: str, detail: str = "") -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE data_apps SET state = :state, state_detail = :detail, updated_at = now() WHERE id = :id"
                ),
                {"state": state, "detail": detail, "id": app_id},
            )

    def record_deploy(self, app_id: str, sha: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE data_apps SET deployed_sha = :sha, last_deploy_at = now(), "
                    "updated_at = now() WHERE id = :id"
                ),
                {"sha": sha, "id": app_id},
            )

    def touch_last_request(self, app_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_apps SET last_request_at = now() WHERE id = :id"),
                {"id": app_id},
            )

    def list_idle(self, older_than_s: int) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM data_apps WHERE state = 'running' "
                        "AND last_request_at IS NOT NULL "
                        "AND last_request_at < now() - make_interval(secs => :older)"
                    ),
                    {"older": older_than_s},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete(self, app_id: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM data_apps WHERE id = :id"),
                {"id": app_id},
            )
            return (result.rowcount or 0) > 0

    # ── linked (externally-hosted) apps — v108 ─────────────────────────────

    @staticmethod
    def effective_description(row: Dict[str, Any]) -> str:
        """Admin override wins over the synced description (linked apps)."""
        return row.get("description_override") or row.get("description") or ""

    def get_by_source_ref(self, source_ref: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM data_apps WHERE source_ref = :sr"),
                    {"sr": source_ref},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def create_linked(
        self,
        *,
        slug: str,
        name: str,
        external_url: str,
        source_ref: str,
        description: str = "",
        owner_user_id: str = "system",
    ) -> str:
        """Insert a ``repo_mode='linked'`` row (no git repo / runtime).

        Mirrors ``DataAppsRepository.create_linked``.
        """
        app_id = "app_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO data_apps
                      (id, slug, name, description, owner_user_id, repo_mode, state,
                       managed, external_url, source_ref)
                    VALUES
                      (:id, :slug, :name, :description, :owner_user_id, 'linked', 'linked',
                       true, :external_url, :source_ref)
                    """
                ),
                {
                    "id": app_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "owner_user_id": owner_user_id,
                    "external_url": external_url,
                    "source_ref": source_ref,
                },
            )
        return app_id

    def upsert_linked(
        self,
        *,
        slug: str,
        source_ref: str,
        name: str,
        external_url: str,
        description: str = "",
        owner_user_id: str = "system",
    ) -> Dict[str, Any]:
        """Insert-or-update a linked app keyed by ``source_ref``. Mirrors
        ``DataAppsRepository.upsert_linked`` (never touches
        ``description_override``)."""
        if self.get_by_source_ref(source_ref) is not None:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE data_apps SET name = :name, description = :description, "
                        "external_url = :external_url, state = 'linked', updated_at = now() "
                        "WHERE source_ref = :sr"
                    ),
                    {
                        "name": name,
                        "description": description,
                        "external_url": external_url,
                        "sr": source_ref,
                    },
                )
        else:
            self.create_linked(
                slug=slug,
                name=name,
                description=description,
                external_url=external_url,
                source_ref=source_ref,
                owner_user_id=owner_user_id,
            )
        row = self.get_by_source_ref(source_ref)
        assert row is not None  # just upserted
        return row

    def list_linked(
        self,
        *,
        source_ref_prefix: Optional[str] = None,
        include_hidden: bool = False,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses = ["repo_mode = 'linked'"]
        params: Dict[str, Any] = {"limit": limit}
        if not include_hidden:
            clauses.append("state = 'linked'")
        if source_ref_prefix is not None:
            clauses.append("source_ref LIKE :prefix")
            params["prefix"] = source_ref_prefix + "%"
        where = "WHERE " + " AND ".join(clauses)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM data_apps {where} ORDER BY created_at DESC LIMIT :limit"),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def soft_delete_missing_linked(self, *, source_ref_prefix: str, keep_source_refs: List[str]) -> int:
        """Hide active linked rows for one connection whose ``source_ref`` is not
        kept. Mirrors ``DataAppsRepository.soft_delete_missing_linked``."""
        with self._engine.connect() as conn:
            active = (
                conn.execute(
                    sa.text(
                        "SELECT source_ref FROM data_apps WHERE repo_mode = 'linked' "
                        "AND state = 'linked' AND source_ref LIKE :prefix"
                    ),
                    {"prefix": source_ref_prefix + "%"},
                )
                .scalars()
                .all()
            )
        keep = set(keep_source_refs)
        to_hide = [sr for sr in active if sr not in keep]
        if to_hide:
            with self._engine.begin() as conn:
                for sr in to_hide:
                    conn.execute(
                        sa.text(
                            "UPDATE data_apps SET state = 'linked_hidden', updated_at = now() WHERE source_ref = :sr"
                        ),
                        {"sr": sr},
                    )
        return len(to_hide)

    def set_description_override(self, slug: str, text: Optional[str]) -> bool:
        """Set (or clear) the admin description override on a managed row.
        Returns False if the slug does not exist."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("UPDATE data_apps SET description_override = :t, updated_at = now() WHERE slug = :slug"),
                {"t": text, "slug": slug},
            )
            return (result.rowcount or 0) > 0

    def set_data_identity(self, slug: str, value: str) -> bool:
        """Set ``data_identity`` (``'owner'`` | ``'viewer'``) on a row —
        Postgres-only column (revision 0113); the DuckDB sibling raises
        ``RequiresPostgresBackend``. Returns False if the slug does not
        exist. ``ValueError`` on any other value: the column has no CHECK
        constraint, so this is where the vocabulary is enforced."""
        from src.data_apps.identity import DATA_IDENTITIES

        if value not in DATA_IDENTITIES:
            raise ValueError(f"data_identity must be one of {sorted(DATA_IDENTITIES)}, got {value!r}")
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("UPDATE data_apps SET data_identity = :v, updated_at = now() WHERE slug = :slug"),
                {"v": value, "slug": slug},
            )
            return (result.rowcount or 0) > 0
