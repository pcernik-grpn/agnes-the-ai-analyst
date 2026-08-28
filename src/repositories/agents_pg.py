"""Postgres-backed repository for ``agents`` + ``agent_scope`` +
``agent_scope_snapshots`` (v96).

Mirrors ``src/repositories/agents.py`` (the DuckDB impl) on the
``AgentsRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_agents_contract.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_UPDATABLE = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "model",
        "token_budget_monthly",
        "plugins_mode",
        "connections_mode",
        "tables_mode",
        "memory_mode",
        "memory_write_mode",
        # v110 paper-theme agent-builder superset (knowledge/plugins/surfaces
        # are JSON text the caller encodes).
        "role",
        "tone",
        "greeting",
        "knowledge",
        "plugins",
        "surfaces",
        "status",
        # Parity with the DuckDB twin — see its comment: naming an unnamed
        # draft re-derives the slug, always through ``_unique_slug`` so the
        # (owner_user_id, slug) UNIQUE holds.
        "slug",
    }
)


class AgentsPgRepository:
    """Postgres twin of ``AgentsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        id: str,
        owner_user_id: str,
        name: str,
        slug: str,
        description: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        token_budget_monthly: Optional[int] = None,
        plugins_mode: str = "all",
        connections_mode: str = "all",
        tables_mode: str = "all",
        memory_mode: str = "all",
        memory_write_mode: str = "propose",
        is_default: bool = False,
        # v110 paper-theme agent-builder superset. knowledge/plugins/surfaces
        # are opaque JSON text the caller encodes; None falls back to the
        # column DEFAULT.
        role: Optional[str] = None,
        tone: Optional[str] = None,
        greeting: Optional[str] = None,
        knowledge: Optional[str] = None,
        plugins: Optional[str] = None,
        surfaces: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agents
                      (id, owner_user_id, name, slug, description, system_prompt, model,
                       token_budget_monthly, plugins_mode, connections_mode, tables_mode,
                       memory_mode, memory_write_mode, is_default,
                       role, tone, greeting, knowledge, plugins, surfaces, status,
                       created_at, updated_at)
                    VALUES
                      (:id, :owner_user_id, :name, :slug, :description, :system_prompt, :model,
                       :token_budget_monthly, :plugins_mode, :connections_mode, :tables_mode,
                       :memory_mode, :memory_write_mode, :is_default,
                       COALESCE(:role, ''), COALESCE(:tone, 'concise'), COALESCE(:greeting, ''),
                       COALESCE(:knowledge, '[]'), COALESCE(:plugins, '[]'), COALESCE(:surfaces, '{}'),
                       COALESCE(:status, 'draft'), :created_at, :updated_at)
                    """
                ),
                {
                    "id": id,
                    "owner_user_id": owner_user_id,
                    "name": name,
                    "slug": slug,
                    "description": description,
                    "system_prompt": system_prompt,
                    "model": model,
                    "token_budget_monthly": token_budget_monthly,
                    "plugins_mode": plugins_mode,
                    "connections_mode": connections_mode,
                    "tables_mode": tables_mode,
                    "memory_mode": memory_mode,
                    "memory_write_mode": memory_write_mode,
                    "is_default": is_default,
                    "role": role,
                    "tone": tone,
                    "greeting": greeting,
                    "knowledge": knowledge,
                    "plugins": plugins,
                    "surfaces": surfaces,
                    "status": status,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def get_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Includes soft-deleted rows so slug tombstoning is inspectable."""
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM agents WHERE id = :id"), {"id": agent_id}).mappings().first()
        return dict(row) if row else None

    def get_by_slug(self, owner_user_id: str, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        # include_deleted spans soft-deleted rows because the (owner_user_id,
        # slug) UNIQUE constraint does too — the builder's slug picker must see
        # tombstones or a create-delete-create reuses a slug and hits the
        # constraint.
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM agents WHERE owner_user_id = :owner_user_id AND slug = :slug" + clause),
                    {"owner_user_id": owner_user_id, "slug": slug},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_runnable_by_slug(self, user_id: str, slug: str) -> Optional[Dict[str, Any]]:
        """PG mirror of ``AgentsRepository.get_runnable_by_slug`` — see that
        docstring for the slug-vs-id resolution rule and why the Admin
        god-mode short-circuit is deliberately not applied here."""
        owned = self.get_by_slug(user_id, slug)
        if owned is not None:
            return owned

        agent = self.get_by_id(slug)
        if agent is None or agent.get("deleted_at") is not None:
            return None
        if agent["owner_user_id"] == user_id:
            return agent

        from src.repositories.resource_grants_pg import ResourceGrantsPgRepository

        # "agent" mirrors ``ResourceType.AGENT.value`` — kept inline, see the
        # DuckDB sibling's docstring for why the repo layer avoids importing
        # app.resource_types.
        granted_ids = ResourceGrantsPgRepository(self._engine).list_resource_ids_for_user(user_id, "agent")
        if agent["id"] in granted_ids:
            return agent
        return None

    def list_for_user(self, owner_user_id: str) -> List[Dict[str, Any]]:
        """Every agent this user owns — EXCEPT scratch rows.

        A `status='scratch'` agent is not an agent anyone made; it is the
        throwaway identity a Preview runs as while someone is authoring an
        agent TEMPLATE on /skills (see app/api/entity_builder.py). It has to
        be a real row because a chat session runs as an agent id, but it is
        machinery, and showing it in the owner's list — or an admin's — would
        be showing them a thing they cannot explain and did not create.

        Filtered here rather than at the two call sites so a third caller
        cannot forget. Fetch-by-id and by-slug deliberately still find it:
        that is how the preview session resolves.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                    SELECT * FROM agents
                    WHERE owner_user_id = :owner_user_id AND deleted_at IS NULL
                      AND (status IS NULL OR status <> 'scratch')
                    ORDER BY is_default DESC, name
                    """
                    ),
                    {"owner_user_id": owner_user_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """All live (non-soft-deleted) agents across owners, ordered by name.

        Used by the ``/admin/access`` AGENT grant projection (see
        ``app/resource_types.py``) so an admin can see and correct agent
        grants that owners usually write through the Library's Share action.
        """
        sql = "SELECT * FROM agents WHERE deleted_at IS NULL ORDER BY name"
        params: Dict[str, Any] = {}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def update(self, agent_id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
        if not fields:
            return
        set_clauses = [f"{col} = :{col}" for col in fields]
        params: Dict[str, Any] = dict(fields)
        set_clauses.append("updated_at = :updated_at")
        params["updated_at"] = datetime.now(timezone.utc)
        params["agent_id"] = agent_id
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE agents SET {', '.join(set_clauses)} WHERE id = :agent_id"),
                params,
            )

    def soft_delete(self, agent_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE agents SET deleted_at = :deleted_at WHERE id = :id"),
                {"deleted_at": datetime.now(timezone.utc), "id": agent_id},
            )

    def _free_default_slug(self, owner_user_id: str) -> str:
        """First unused slug in ``default``, ``default-2``, ``default-3``, …

        See ``src/repositories/agents.py``'s sibling — the scan must include
        soft-deleted rows because ``uq_agents_owner_slug`` spans them.
        """
        with self._engine.connect() as conn:
            taken = {
                r[0]
                for r in conn.execute(
                    sa.text("SELECT slug FROM agents WHERE owner_user_id = :owner_user_id AND slug LIKE 'default%'"),
                    {"owner_user_id": owner_user_id},
                ).all()
            }
        if "default" not in taken:
            return "default"
        for n in range(2, 1000):
            candidate = f"default-{n}"
            if candidate not in taken:
                return candidate
        return f"default-{uuid4().hex[:8]}"

    def get_or_create_default(self, owner_user_id: str) -> Dict[str, Any]:
        """The owner's default agent, seeding one on first touch.

        Parity sibling of ``src/repositories/agents.py`` — see that docstring
        for why the soft-deleted-default revive and the free-slug fallback
        exist (a permanent 500 on every web chat session create), and why the
        default is seeded ``status='ready'`` with an older draft one promoted
        on first touch instead of by a migration step.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM agents "
                        "WHERE owner_user_id = :owner_user_id AND is_default AND deleted_at IS NULL"
                    ),
                    {"owner_user_id": owner_user_id},
                )
                .mappings()
                .first()
            )
        if row:
            existing = dict(row)
            if (existing.get("status") or "") != "ready":
                with self._engine.begin() as conn:
                    conn.execute(
                        sa.text("UPDATE agents SET status = 'ready' WHERE id = :id"),
                        {"id": existing["id"]},
                    )
                existing["status"] = "ready"
            return existing

        with self._engine.begin() as conn:
            stale = conn.execute(
                sa.text(
                    "SELECT id FROM agents WHERE owner_user_id = :owner_user_id "
                    "AND is_default AND deleted_at IS NOT NULL "
                    "ORDER BY deleted_at DESC LIMIT 1"
                ),
                {"owner_user_id": owner_user_id},
            ).first()
            if stale:
                conn.execute(
                    sa.text(
                        "UPDATE agents SET deleted_at = NULL, is_default = TRUE, "
                        "status = 'ready', updated_at = :updated_at WHERE id = :id"
                    ),
                    {"updated_at": datetime.now(timezone.utc), "id": stale[0]},
                )
        if stale:
            return self.get_by_id(stale[0])  # type: ignore[return-value]

        agent_id = str(uuid4())
        self.create(
            id=agent_id,
            owner_user_id=owner_user_id,
            name="Default",
            slug=self._free_default_slug(owner_user_id),
            plugins_mode="all",
            connections_mode="all",
            tables_mode="all",
            memory_mode="all",
            memory_write_mode="propose",
            is_default=True,
            status="ready",
        )
        result = self.get_by_id(agent_id)
        assert result is not None
        return result

    def set_scope(
        self,
        agent_id: str,
        items: List[Tuple[str, str]],
        granted_by: Optional[str] = None,
    ) -> None:
        """Replace the whole scope set for ``agent_id``.

        ``granted_by`` is the writer's user id, recorded on every row this
        call inserts that is genuinely NEW. Column added by migration 0073
        (remediation Track C, C2.1) — see the DuckDB sibling's docstring for
        why it has no counterpart there.

        A ``(item_type, item_id)`` pair that was ALREADY present before this
        call (a full-replace call re-declaring a row unchanged — the normal
        shape of ``app/api/agents_builder_shared.py::_sync_builder_scope``'s
        "preserved" rows, which read the current scope back and pass it
        straight through) keeps its EXISTING ``granted_by`` instead of being
        re-attributed to this call's writer. Without this, a later builder
        save by the (non-admin) owner would silently downgrade an
        admin-granted row to owner-granted — D-C2's "admin-granted =
        unconditioned" half only holds if the grant's origin survives an
        unrelated re-save (remediation-program C2.2 must-handle:
        ``docs/superpowers/plans/2026-08-26-one-agent-model.md`` §C2.2). A
        pair that is new (not present before) is attributed to
        ``granted_by`` exactly as before.
        """
        with self._engine.begin() as conn:
            existing = (
                conn.execute(
                    sa.text("SELECT item_type, item_id, granted_by FROM agent_scope WHERE agent_id = :agent_id"),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
            prior_granted_by = {(r["item_type"], r["item_id"]): r["granted_by"] for r in existing}

            conn.execute(
                sa.text("DELETE FROM agent_scope WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )
            for item_type, item_id in items:
                row_granted_by = prior_granted_by.get((item_type, item_id), granted_by)
                conn.execute(
                    sa.text(
                        "INSERT INTO agent_scope (agent_id, item_type, item_id, granted_by) "
                        "VALUES (:agent_id, :item_type, :item_id, :granted_by)"
                    ),
                    {
                        "agent_id": agent_id,
                        "item_type": item_type,
                        "item_id": item_id,
                        "granted_by": row_granted_by,
                    },
                )

    def get_scope(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT item_type, item_id, granted_by FROM agent_scope "
                        "WHERE agent_id = :agent_id ORDER BY item_type, item_id"
                    ),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def get_scope_for_agents(self, agent_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """``{agent_id: [{item_type, item_id, granted_by}, ...]}`` — see the
        DuckDB sibling for why the list endpoint needs a batched read
        instead of an N+1."""
        if not agent_ids:
            return {}
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT agent_id, item_type, item_id, granted_by FROM agent_scope "
                        "WHERE agent_id = ANY(:agent_ids) ORDER BY agent_id, item_type, item_id"
                    ),
                    {"agent_ids": list(agent_ids)},
                )
                .mappings()
                .all()
            )
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["agent_id"], []).append(
                {"item_type": r["item_type"], "item_id": r["item_id"], "granted_by": r["granted_by"]}
            )
        return out

    def agent_for_scope_item(self, item_type: str, item_id: str) -> Optional[Dict[str, Any]]:
        """The non-deleted agent holding scope item ``(item_type, item_id)``,
        or None. Routing lookup (e.g. Slack `slack_channel` bindings) — the
        API layer enforces at most one non-deleted holder per item, so a
        deterministic single row is returned (oldest first on the off chance
        a deleted-then-rebound race left two)."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT a.* FROM agents a "
                        "JOIN agent_scope s ON s.agent_id = a.id "
                        "WHERE s.item_type = :item_type AND s.item_id = :item_id "
                        "AND a.deleted_at IS NULL "
                        "ORDER BY a.created_at LIMIT 1"
                    ),
                    {"item_type": item_type, "item_id": item_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def record_scope_snapshot(self, id: str, session_id: str, agent_id: str, effective_scope: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agent_scope_snapshots
                      (id, session_id, agent_id, effective_scope, created_at)
                    VALUES
                      (:id, :session_id, :agent_id, :effective_scope, :created_at)
                    """
                ),
                {
                    "id": id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "effective_scope": effective_scope,
                    "created_at": datetime.now(timezone.utc),
                },
            )

    def list_scope_snapshots(self, session_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM agent_scope_snapshots WHERE session_id = :session_id ORDER BY created_at"),
                    {"session_id": session_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def prune_scope_snapshots_older_than(self, days: int) -> int:
        """Mirrors ``AgentsRepository.prune_scope_snapshots_older_than``."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM agent_scope_snapshots "
                    "WHERE created_at < (CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)
