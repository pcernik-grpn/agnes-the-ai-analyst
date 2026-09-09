"""Repository for ``data_apps`` (v96) — the hosted user web apps registry.

Task 1 of the Data Apps feature: a data app is a user-owned web app hosted
by Agnes (internal template or an external git repo), deployed to a
runtime container, and put to sleep after an idle timeout. This repo is
the foundation everything else in the feature builds on — deploy
orchestration, idle-reaper, and the admin/API surface all go through
``data_apps_repo()``.

``create`` follows the same ``<prefix>_<uuid12>`` id-generation idiom as
``MemoryDomainsRepository`` (``md_``) — here ``app_``. Slug uniqueness is
enforced by the ``UNIQUE`` constraint on the column, surfaced as
``duckdb.ConstraintException`` on collision (see ``memory_domains.create``
for the same pattern).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb

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


class DataAppsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "name",
        "description",
        "owner_user_id",
        "repo_mode",
        "repo_url",
        "repo_branch",
        "deployed_sha",
        "runtime_tag",
        "state",
        "state_detail",
        "secrets_enc",
        "env",
        "cpu_limit",
        "mem_limit",
        "idle_timeout_s",
        "sleep_mode",
        "service_token_id",
        "parent_app_id",
        "is_draft",
        "draft_branch",
        "external_url",
        "source_ref",
        "managed",
        "description_override",
        "last_request_at",
        "last_deploy_at",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    @staticmethod
    def effective_description(row: Dict[str, Any]) -> str:
        """Admin override wins over the synced description (linked apps)."""
        return row.get("description_override") or row.get("description") or ""

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

        Raises ``duckdb.ConstraintException`` if ``slug`` collides — UNIQUE
        on the column is the source of truth.
        """
        app_id = "app_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_apps"
            "(id, slug, name, description, owner_user_id, repo_mode,"
            " repo_url, repo_branch, idle_timeout_s, sleep_mode, env) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                app_id,
                slug,
                name,
                description,
                owner_user_id,
                repo_mode,
                repo_url,
                repo_branch,
                idle_timeout_s,
                sleep_mode,
                env,
            ],
        )
        return app_id

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM data_apps WHERE id = ?", [app_id]).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM data_apps WHERE slug = ?", [slug]).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def list(
        self,
        *,
        owner_user_id: Optional[str] = None,
        state: Optional[str] = None,
        include_drafts: bool = True,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if not include_drafts:
            clauses.append("is_draft = FALSE")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

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

        The draft row shares the ``app_<uuid12>`` id scheme with ``create``
        but sets ``is_draft=True``, ``parent_app_id``, and ``draft_branch``
        so it is excluded from ``list(include_drafts=False)`` and can be
        looked up via ``list_drafts``.
        """
        app_id = "app_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_apps"
            "(id, slug, name, owner_user_id, repo_mode, parent_app_id, is_draft,"
            " draft_branch, idle_timeout_s, sleep_mode) "
            "VALUES (?, ?, ?, ?, 'internal', ?, TRUE, ?, ?, ?)",
            [
                app_id,
                slug,
                f"{slug} (draft)",
                owner_user_id,
                parent_app_id,
                branch,
                idle_timeout_s,
                sleep_mode,
            ],
        )
        return app_id

    def list_drafts(self, parent_app_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE parent_app_id = ? "
            "AND is_draft = TRUE ORDER BY created_at DESC",
            [parent_app_id],
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def update(self, app_id: str, **fields: Any) -> bool:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        if not fields:
            return False
        if self.get(app_id) is None:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE data_apps SET {sets}, updated_at = now() WHERE id = ?", [*fields.values(), app_id])
        return True

    def set_state(self, app_id: str, state: str, detail: str = "") -> None:
        self.conn.execute(
            "UPDATE data_apps SET state = ?, state_detail = ?, updated_at = now() WHERE id = ?", [state, detail, app_id]
        )

    def record_deploy(self, app_id: str, sha: str) -> None:
        self.conn.execute(
            "UPDATE data_apps SET deployed_sha = ?, last_deploy_at = now(), updated_at = now() WHERE id = ?",
            [sha, app_id],
        )

    def touch_last_request(self, app_id: str) -> None:
        self.conn.execute("UPDATE data_apps SET last_request_at = now() WHERE id = ?", [app_id])

    def list_idle(self, older_than_s: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE state = 'running' "
            "AND last_request_at IS NOT NULL "
            "AND last_request_at < now() - (? * INTERVAL 1 SECOND)",
            [older_than_s],
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def delete(self, app_id: str) -> bool:
        existed = self.get(app_id) is not None
        self.conn.execute("DELETE FROM data_apps WHERE id = ?", [app_id])
        return existed

    # ── linked (externally-hosted) apps — v108 ─────────────────────────────

    def get_by_source_ref(self, source_ref: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM data_apps WHERE source_ref = ?", [source_ref]).fetchone()
        return dict(zip(self._COLS, row)) if row else None

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

        ``managed=TRUE`` marks it sync-owned; ``state='linked'`` is fixed (linked
        apps have no deploy lifecycle). Returns the generated ``app_<uuid12>`` id.
        """
        app_id = "app_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_apps"
            "(id, slug, name, description, owner_user_id, repo_mode, state, managed,"
            " external_url, source_ref) "
            "VALUES (?, ?, ?, ?, ?, 'linked', 'linked', TRUE, ?, ?)",
            [app_id, slug, name, description, owner_user_id, external_url, source_ref],
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
        """Insert-or-update a linked app keyed by ``source_ref``.

        On update: refresh ``name``/``description``/``external_url`` and
        reactivate (``state='linked'``) a previously-hidden row; NEVER touch
        ``description_override`` (admin's edit wins) or the app's grants.
        """
        if self.get_by_source_ref(source_ref) is not None:
            self.conn.execute(
                "UPDATE data_apps SET name = ?, description = ?, external_url = ?, "
                "state = 'linked', updated_at = now() WHERE source_ref = ?",
                [name, description, external_url, source_ref],
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
        params: List[Any] = []
        if not include_hidden:
            clauses.append("state = 'linked'")
        if source_ref_prefix is not None:
            clauses.append("source_ref LIKE ?")
            params.append(source_ref_prefix + "%")
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def soft_delete_missing_linked(self, *, source_ref_prefix: str, keep_source_refs: List[str]) -> int:
        """Hide active linked rows for one connection whose ``source_ref`` is not
        in ``keep_source_refs`` (i.e. the app disappeared upstream). Scoped by
        ``source_ref_prefix`` so one connection's reconcile never touches
        another's rows. Returns the number hidden."""
        active = self.conn.execute(
            "SELECT source_ref FROM data_apps WHERE repo_mode = 'linked' AND state = 'linked' AND source_ref LIKE ?",
            [source_ref_prefix + "%"],
        ).fetchall()
        keep = set(keep_source_refs)
        to_hide = [r[0] for r in active if r[0] not in keep]
        if to_hide:
            # One statement so the batch is all-or-nothing (same atomicity
            # guarantee as the PG sibling, which loops per-row inside one
            # engine.begin() transaction): a mid-hide failure never leaves
            # the connection's linked set partially reconciled.
            placeholders = ",".join("?" for _ in to_hide)
            self.conn.execute(
                f"UPDATE data_apps SET state = 'linked_hidden', updated_at = now() "
                f"WHERE source_ref IN ({placeholders})",
                to_hide,
            )
        return len(to_hide)

    def set_description_override(self, slug: str, text: Optional[str]) -> bool:
        """Set (or clear, with ``None``) the admin description override on a
        managed row. Returns False if the slug does not exist."""
        if self.get_by_slug(slug) is None:
            return False
        self.conn.execute(
            "UPDATE data_apps SET description_override = ?, updated_at = now() WHERE slug = ?",
            [text, slug],
        )
        return True

    def set_data_identity(self, slug: str, value: str) -> bool:
        """``data_identity`` is a Postgres-only column (A3 ratchet — Alembic
        revision 0113, no DuckDB ladder step). Present on this side only so
        the method-parity contract holds; it always refuses, typed, and the
        app-wide handler turns that into a clean 501 rather than a 500.
        Rows read from this backend lack the key and every reader defaults
        it to ``'owner'`` (``src.data_apps.identity.data_identity_of``)."""
        from src.repository_errors import RequiresPostgresBackend

        raise RequiresPostgresBackend("data_app.data_identity")
