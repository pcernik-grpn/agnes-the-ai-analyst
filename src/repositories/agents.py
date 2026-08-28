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

    def get_runnable_by_slug(self, user_id: str, slug: str) -> Optional[Dict[str, Any]]:
        """Agent *user_id* may RUN at a `/api/v1/agents/{slug}/...` runtime
        route: owned, or reachable via a ``ResourceType.AGENT`` grant
        through one of the caller's groups (remediation-program C2.3,
        shared-agent runtime).

        ``slug`` is resolved first in the caller's OWN slug namespace —
        ``(owner_user_id, slug)`` is the only UNIQUE key on ``agents.slug``
        (see :meth:`get_by_slug`), so a slug string is meaningless outside
        its owner's namespace. A caller who does not own an agent under
        ``slug`` cannot address someone else's agent by a borrowed name;
        for a shared (not owned) agent, ``slug`` is instead resolved as the
        target agent's globally-unique ``id`` — the shape the v1 list's
        ``runnable=true`` filter hands back to a non-owner caller. The
        owner may ALSO address their own agent by id this way (falls
        through the same branch, short-circuited by the ownership check
        before the grant lookup) — "runtime paths accept slug or id" holds
        for every caller, not only grantees.

        Deliberately does NOT apply the Admin god-mode short-circuit
        (``app.auth.access.can_access``): per the agent-runtime auth
        matrix, admin god-mode covers management/inspection only, never an
        implicit "run any agent" grant, so this method reads
        ``resource_grants`` directly instead of going through
        ``can_access``.
        """
        owned = self.get_by_slug(user_id, slug)
        if owned is not None:
            return owned

        agent = self.get_by_id(slug)
        if agent is None or agent.get("deleted_at") is not None:
            return None
        if agent["owner_user_id"] == user_id:
            return agent

        from src.repositories.resource_grants import ResourceGrantsRepository

        # "agent" mirrors ``ResourceType.AGENT.value`` (kept inline so the
        # repo layer stays free of the app.resource_types import — same
        # convention as ResourceGrantsRepository.delete_for_marketplace_plugins).
        granted_ids = ResourceGrantsRepository(self.conn).list_resource_ids_for_user(user_id, "agent")
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
        rows = self.conn.execute(
            """SELECT * FROM agents
            WHERE owner_user_id = ? AND deleted_at IS NULL
              AND (status IS NULL OR status <> 'scratch')
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

        The default is seeded ``status='ready'``, and an older one that
        predates that is promoted here on first touch. It is the one agent
        nobody builds — it exists so a new instance can be chatted with before
        anyone opens the builder — so "draft" was never true of it, and every
        surface that separates ready agents from unfinished ones (the
        ``/agents`` index, the composer's agent picker) was filing the only
        always-usable agent under drafts.

        Promoting it is safe for the slug rule it used to be entangled with:
        ``_draft_slug_rename`` returns early on ``is_default`` — BEFORE it
        looks at ``status`` — because the default's slug is a reserved address
        (``POST /api/v1/agents/default/responses``, ``_RESERVED_SLUGS``). So
        that freeze never depended on the draft status, and dropping it moves
        nothing. (``_v114_to_v115`` excludes ``is_default`` for the opposite
        reason and its comment says the default is a PERMANENT draft; that
        step is a one-time backfill of a different cohort and stays as it is —
        it simply no longer describes the default.)

        Healed here rather than in a migration step because the DuckDB ladder
        is frozen (A3) and a PG-only Alembic revision would leave DuckDB
        instances behind. Every web chat session resolves this method first,
        so the repair lands on first touch on either backend, and it is
        naturally idempotent — an already-ready row fails the ``if``.
        ``updated_at`` is deliberately NOT bumped: this corrects a value that
        was always meant to be ``'ready'``, and touching the timestamp would
        reshuffle a recency-ordered list for an edit the owner never made.
        """
        row = self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id = ? AND is_default AND deleted_at IS NULL",
            [owner_user_id],
        ).fetchone()
        existing = self._row_to_dict(row)
        if existing is not None:
            if (existing.get("status") or "") != "ready":
                self.conn.execute(
                    "UPDATE agents SET status = 'ready' WHERE id = ?", [existing["id"]]
                )
                existing["status"] = "ready"
            return existing

        stale = self.conn.execute(
            "SELECT id FROM agents "
            "WHERE owner_user_id = ? AND is_default AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT 1",
            [owner_user_id],
        ).fetchone()
        if stale is not None:
            self.conn.execute(
                "UPDATE agents SET deleted_at = NULL, is_default = TRUE, "
                "status = 'ready', updated_at = ? WHERE id = ?",
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
            status="ready",
        )
        return self.get_by_id(agent_id)  # type: ignore[return-value]

    def set_scope(
        self,
        agent_id: str,
        items: List[Tuple[str, str]],
        granted_by: Optional[str] = None,
    ) -> None:
        """Replace the whole scope set for ``agent_id``.

        ``granted_by`` is accepted for signature symmetry with the Postgres
        repo (``AgentsPgRepository.set_scope``) so call sites work
        identically regardless of the active backend, but it is a no-op
        here: ``agent_scope.granted_by`` is a genuine schema change under
        the A3 PG-first ratchet, so it landed Postgres-only
        (``migrations/versions/0073_agent_scope_granted_by.py``) — the
        DuckDB side of this pair does not gain the capability that depends
        on it (see ``.claude/skills/agnes-conventions/references/
        migration.md``). ``get_scope``/``get_scope_for_agents`` always read
        back ``granted_by: None`` here.
        """
        self.conn.execute("DELETE FROM agent_scope WHERE agent_id = ?", [agent_id])
        if items:
            self.conn.executemany(
                "INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (?, ?, ?)",
                [[agent_id, item_type, item_id] for item_type, item_id in items],
            )

    def get_scope(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT item_type, item_id, NULL AS granted_by FROM agent_scope "
            "WHERE agent_id = ? ORDER BY item_type, item_id",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_scope_for_agents(self, agent_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """``{agent_id: [{item_type, item_id, granted_by}, ...]}`` for many
        agents at once.

        The list endpoint projects every agent's declaration, which needs the
        scope rows when the JSON columns are empty; calling :meth:`get_scope`
        per row made that an N+1 (Devin Review on #1520). Ids with no rows are
        absent from the mapping, so callers should use ``.get(id, [])``.
        ``granted_by`` is always ``None`` here — see :meth:`set_scope`.
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
            out.setdefault(agent_id, []).append({"item_type": item_type, "item_id": item_id, "granted_by": None})
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

    def prune_scope_snapshots_older_than(self, days: int) -> int:
        """Delete ``agent_scope_snapshots`` rows older than ``days``, by
        ``created_at``. Returns the deleted-row count.

        Scoped to ``agent_scope_snapshots`` only — never touches the live
        ``agents`` row an owner is still using; this is forensic audit
        trail, not agent state.

        The caller (``src/audit_retention.py``) owns the "days<=0 = keep
        forever, skip entirely" short-circuit — this method always executes
        the DELETE it's given, no matter the value (mirrors
        ``AuditRepository.prune_older_than``)."""
        rows = self.conn.execute(
            "DELETE FROM agent_scope_snapshots WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY) RETURNING id",
            [days],
        ).fetchall()
        return len(rows)
