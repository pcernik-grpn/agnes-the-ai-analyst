"""Repository for owner-scoped agent profiles + scope + scope snapshots (v96)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb

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
        # Renaming a DRAFT re-derives its slug off the new name
        # (app/api/agents_builder_shared.py::_draft_slug_rename) — the
        # builder creates the row before the user types anything, so the
        # slug would otherwise
        # stay the literal "agent" placeholder that the public address
        # (POST /api/v1/agents/{slug}/responses) is built from. Writers must
        # keep the (owner_user_id, slug) UNIQUE intact: resolve through
        # ``_unique_slug`` scoped to the agent's OWNER (not the caller — an
        # admin may be editing someone else's agent), never assign a raw
        # value here.
        "slug",
    }
)


class AgentsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

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
        self.conn.execute(
            """INSERT INTO agents
            (id, owner_user_id, name, slug, description, system_prompt, model,
             token_budget_monthly, plugins_mode, connections_mode, tables_mode,
             memory_mode, memory_write_mode, is_default,
             role, tone, greeting, knowledge, plugins, surfaces, status,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE(?, ''), COALESCE(?, 'concise'), COALESCE(?, ''),
                    COALESCE(?, '[]'), COALESCE(?, '[]'), COALESCE(?, '{}'),
                    COALESCE(?, 'draft'), ?, ?)""",
            [
                id,
                owner_user_id,
                name,
                slug,
                description,
                system_prompt,
                model,
                token_budget_monthly,
                plugins_mode,
                connections_mode,
                tables_mode,
                memory_mode,
                memory_write_mode,
                is_default,
                role,
                tone,
                greeting,
                knowledge,
                plugins,
                surfaces,
                status,
                now,
                now,
            ],
        )

    def get_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Includes soft-deleted rows so slug tombstoning is inspectable."""
        row = self.conn.execute("SELECT * FROM agents WHERE id = ?", [agent_id]).fetchone()
        return self._row_to_dict(row)

    def get_by_slug(self, owner_user_id: str, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        # include_deleted spans soft-deleted rows because the (owner_user_id,
        # slug) UNIQUE constraint does too — the builder's slug picker must see
        # tombstones or a create-delete-create reuses a slug and hits the
        # constraint.
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id = ? AND slug = ?" + clause,
            [owner_user_id, slug],
        ).fetchone()
        return self._row_to_dict(row)

    def list_for_user(self, owner_user_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM agents
            WHERE owner_user_id = ? AND deleted_at IS NULL
            ORDER BY is_default DESC, name""",
            [owner_user_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """All live (non-soft-deleted) agents across owners, ordered by name.

        Used by the ``/admin/access`` AGENT grant projection (see
        ``app/resource_types.py``) so an admin can see and correct agent
        grants that owners usually write through the Library's Share action.
        """
        sql = "SELECT * FROM agents WHERE deleted_at IS NULL ORDER BY name"
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def update(self, agent_id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
        if not fields:
            return
        set_clauses = [f"{col} = ?" for col in fields]
        params: List[Any] = list(fields.values())
        set_clauses.append("updated_at = ?")
        params.append(datetime.now(timezone.utc))
        params.append(agent_id)
        self.conn.execute(
            f"UPDATE agents SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )

    def soft_delete(self, agent_id: str) -> None:
        self.conn.execute(
            "UPDATE agents SET deleted_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), agent_id],
        )

    def _free_default_slug(self, owner_user_id: str) -> str:
        """First unused slug in ``default``, ``default-2``, ``default-3``, …

        Scans slugs INCLUDING soft-deleted rows: the ``(owner_user_id, slug)``
        UNIQUE spans them, so a live-rows-only search would report a taken
        slug as free and drive the INSERT straight into a constraint error.
        Mirrors ``app/api/agents_admin.py``'s slug search for user-created
        agents, which the seeded default never goes through.
        """
        taken = {
            r[0]
            for r in self.conn.execute(
                "SELECT slug FROM agents WHERE owner_user_id = ? AND slug LIKE 'default%'",
                [owner_user_id],
            ).fetchall()
        }
        if "default" not in taken:
            return "default"
        for n in range(2, 1000):
            candidate = f"default-{n}"
            if candidate not in taken:
                return candidate
        # Pathological — a random suffix beats raising on the chat path.
        return f"default-{uuid.uuid4().hex[:8]}"

    def get_or_create_default(self, owner_user_id: str) -> Dict[str, Any]:
        """The owner's default agent, seeding one on first touch.

        Every web chat session resolves this first
        (``app/api/chat.py::_default_agent_id``), so it must never be able to
        fail permanently. Two states used to make it do exactly that, because
        the lookup filters on ``is_default AND deleted_at IS NULL`` while the
        INSERT's hardcoded ``slug='default'`` collides with ANY row holding
        that slug:

        * the default agent was soft-deleted (its row keeps its slug) — revive
          it, preserving the id sessions were already attributed to;
        * a non-default agent holds ``slug='default'`` — leave it alone (it is
          the owner's own agent, not ours to promote or resurrect) and seed
          under the next free slug.

        The revive predicate keys on ``is_default``, NOT on the literal
        ``slug='default'``: ``_free_default_slug`` can seed a default under
        ``default-2``, and a slug-keyed lookup would miss that tombstone,
        stranding the id ``chat_sessions.agent_id`` points at and seeding a
        duplicate on every cycle. ``is_default`` is only ever set here, so it
        identifies the seeded default on its own. Most-recent first, so
        repeated pre-fix cycles resolve deterministically to the newest.
        """
        row = self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id = ? AND is_default AND deleted_at IS NULL",
            [owner_user_id],
        ).fetchone()
        existing = self._row_to_dict(row)
        if existing is not None:
            return existing

        stale = self.conn.execute(
            "SELECT id FROM agents "
            "WHERE owner_user_id = ? AND is_default AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT 1",
            [owner_user_id],
        ).fetchone()
        if stale is not None:
            self.conn.execute(
                "UPDATE agents SET deleted_at = NULL, is_default = TRUE, updated_at = ? WHERE id = ?",
                [datetime.now(timezone.utc), stale[0]],
            )
            return self.get_by_id(stale[0])  # type: ignore[return-value]

        agent_id = str(uuid.uuid4())
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
        )
        return self.get_by_id(agent_id)  # type: ignore[return-value]

    def set_scope(self, agent_id: str, items: List[Tuple[str, str]]) -> None:
        self.conn.execute("DELETE FROM agent_scope WHERE agent_id = ?", [agent_id])
        if items:
            self.conn.executemany(
                "INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (?, ?, ?)",
                [[agent_id, item_type, item_id] for item_type, item_id in items],
            )

    def get_scope(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT item_type, item_id FROM agent_scope WHERE agent_id = ? ORDER BY item_type, item_id",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_scope_for_agents(self, agent_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """``{agent_id: [{item_type, item_id}, ...]}`` for many agents at once.

        The list endpoint projects every agent's declaration, which needs the
        scope rows when the JSON columns are empty; calling :meth:`get_scope`
        per row made that an N+1 (Devin Review on #1520). Ids with no rows are
        absent from the mapping, so callers should use ``.get(id, [])``.
        """
        if not agent_ids:
            return {}
        placeholders = ", ".join("?" for _ in agent_ids)
        rows = self.conn.execute(
            f"SELECT agent_id, item_type, item_id FROM agent_scope "
            f"WHERE agent_id IN ({placeholders}) ORDER BY agent_id, item_type, item_id",
            list(agent_ids),
        ).fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for agent_id, item_type, item_id in rows:
            out.setdefault(agent_id, []).append({"item_type": item_type, "item_id": item_id})
        return out

    def agent_for_scope_item(self, item_type: str, item_id: str) -> Optional[Dict[str, Any]]:
        """The non-deleted agent holding scope item ``(item_type, item_id)``,
        or None. Routing lookup (e.g. Slack `slack_channel` bindings) — the
        API layer enforces at most one non-deleted holder per item, so a
        deterministic single row is returned (oldest first on the off chance
        a deleted-then-rebound race left two)."""
        row = self.conn.execute(
            """SELECT a.* FROM agents a
                 JOIN agent_scope s ON s.agent_id = a.id
                WHERE s.item_type = ? AND s.item_id = ? AND a.deleted_at IS NULL
                ORDER BY a.created_at LIMIT 1""",
            [item_type, item_id],
        ).fetchone()
        return self._row_to_dict(row)

    def record_scope_snapshot(self, id: str, session_id: str, agent_id: str, effective_scope: str) -> None:
        self.conn.execute(
            """INSERT INTO agent_scope_snapshots
            (id, session_id, agent_id, effective_scope, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            [id, session_id, agent_id, effective_scope, datetime.now(timezone.utc)],
        )

    def list_scope_snapshots(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_scope_snapshots WHERE session_id = ? ORDER BY created_at",
            [session_id],
        ).fetchall()
        return self._rows_to_dicts(rows)
