"""Agent management API — `/api/v1/agents` (CRUD + scope + agent-PAT issuance).

Owner-scoped CRUD over agent profiles (v96, `docs/superpowers/specs/
2026-07-21-agent-profiles-and-agent-api-design.md` §2), per-agent scope
grants, and agent PAT issuance. Every route requires an interactive session —
`require_session_token` already rejects every PAT flavor (plain PAT and
agent PAT alike), matching the spec's "Management endpoints require
interactive owner auth" rule.

Ownership (normative, per the spec's auth matrix):
  - non-owner, non-admin -> 404 on every `{id}` route (existence is not
    leaked to a caller who isn't the owner).
  - admin -> GET allowed (read-only governance); mutations and token
    minting on a foreign agent -> 403 (admin never mints a PAT for
    someone else's agent).

Agent PATs cannot be issued for `'all'`-mode agents (including the default
agent) — token issuance requires all four scope modes to be `'selected'`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc

from app.auth.access import is_user_admin, require_agent_profiles_enabled
from app.auth.dependencies import _get_db, require_session_or_user_pat, require_session_token
from app.auth.jwt import create_access_token
from app.resource_types import ResourceType
from src.object_store import object_store
from src.repositories import (
    access_token_repo,
    agent_artifacts_repo,
    agent_memories_repo,
    agent_schedules_repo,
    agent_webhooks_repo,
    agents_repo,
    audit_repo,
    resource_grants_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agents"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

# Lowercase kebab-case, max 64 chars. "default" is reserved for the one
# seeded-per-owner default agent (created via `agents_repo().get_or_create_default`,
# never through this API).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RESERVED_SLUGS = frozenset({"default"})

_SELECTED_MODE_FIELDS = ("plugins_mode", "connections_mode", "tables_mode", "memory_mode")
# `slack_channel` is a ROUTING item, not a data-authority one: holding
# ('slack_channel', <channel_id>) makes @mentions in that channel run this
# agent (services/slack_bot/events.py). The scope-intersection axes each read
# their own item_type, so a binding grants no plugin/table/connection reach.
# At most one non-deleted agent may hold a given channel — enforced below.
# `data_package` and `collection` are DATA-authority items governed by
# `tables_mode`, exactly like `table` — they are what the /agents builder
# declares (`app/api/agents.py::_KNOWLEDGE_ITEM_TYPES`), and a declared
# package additionally stands for its member tables (expanded live in
# `src/agent_scope_intersection.py`). Accepted here so the governance API and
# the builder describe one scope model rather than two.
_ITEM_TYPES = frozenset(
    {"plugin", "connection", "table", "data_package", "collection", "memory_domain", "slack_channel"}
)

_SCOPE_MODE_VALUES = frozenset({"all", "selected"})
_MEMORY_WRITE_MODE_VALUES = frozenset({"off", "propose", "auto"})

# Mirrors `src.repositories.agents._UPDATABLE` minus `slug` (immutable via
# this API — see `update_agent`; the draft-slug-follow rule still writes it
# internally, just never from a client-supplied value).
_UPDATABLE_FIELDS = frozenset(
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
        # Builder-shape projections (Task C1.1) — the same superset columns
        # `app.api.agents.update_agent` writes for the /agents builder.
        "role",
        "tone",
        "greeting",
        "knowledge",
        "plugins",
        "surfaces",
        "status",
    }
)


class CreateAgentRequest(BaseModel):
    name: str
    # Optional (Task C1.1): auto-derived from `name` when omitted, the same
    # way `app.api.agents._unique_slug` does for the /agents builder — a
    # caller supplying one explicitly keeps the pre-existing contract.
    slug: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    token_budget_monthly: Optional[int] = None
    # --- Builder-shape projections (Task C1.1) — the /agents builder's own
    # wire fields, accepted here so ONE surface can do everything either
    # used to. `instructions` aliases the canonical `system_prompt` column
    # (an explicit `system_prompt` wins if both are sent); the rest are
    # literally the same column names the builder writes.
    instructions: Optional[str] = None
    role: Optional[str] = None
    tone: Optional[str] = None
    greeting: Optional[str] = None
    knowledge: Optional[List[str]] = None
    plugins: Optional[List[str]] = None
    surfaces: Optional[Dict[str, bool]] = None
    #: 'draft' | 'ready'. Omitted keeps the pre-existing v1 default
    #: ('ready') — a v1 client that never mentions status is unaffected.
    status: Optional[str] = None
    #: Start from a Library Agent Template — mirrors
    #: `app.api.agents.AgentCreate.template_entity_id` (behaviour only, see
    #: `app.api.agents._template_prefill`).
    template_entity_id: Optional[str] = None


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    # Accepted only so a PUT that supplies it can be rejected with
    # `slug_immutable` — never applied.
    slug: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    token_budget_monthly: Optional[int] = None
    plugins_mode: Optional[str] = None
    connections_mode: Optional[str] = None
    tables_mode: Optional[str] = None
    memory_mode: Optional[str] = None
    memory_write_mode: Optional[str] = None
    # --- Builder-shape projections (Task C1.1), see CreateAgentRequest.
    instructions: Optional[str] = None
    role: Optional[str] = None
    tone: Optional[str] = None
    greeting: Optional[str] = None
    knowledge: Optional[List[str]] = None
    plugins: Optional[List[str]] = None
    surfaces: Optional[Dict[str, bool]] = None
    status: Optional[str] = None


class ScopeItem(BaseModel):
    item_type: str
    item_id: str


class SetScopeRequest(BaseModel):
    items: List[ScopeItem] = []


class CreateAgentTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(actor: str, action: str, target: str, params: Optional[dict] = None) -> None:
    try:
        audit_repo().log(user_id=actor, action=action, resource=f"agent:{target}", params=params)
    except Exception:
        pass


def _serialize(row: Dict[str, Any], *, scope_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Wire shape for one agent row.

    Builder-shape projections (Task C1.1): `knowledge`/`plugins`/`surfaces`
    are opaque JSON TEXT columns — decoded here into real lists/dicts so a
    caller sees the same round-trip shape the /agents builder shows, not an
    unparsed string. `knowledge`/`plugins` are additionally hydrated from
    `agent_scope` when the JSON columns are empty (`app.api.agents.
    _hydrate_builder_axes`) — an agent scoped through `agnes agent scope
    set` or the `PUT .../scope` route never touches those columns, so
    without this a v1 read would show "no declaration" for an agent that in
    fact has real scope. `instructions` aliases the canonical
    `system_prompt` column under the builder's own field name.
    `scope_rows` lets a caller (the list endpoint) supply a batched read so
    hydrating every row does not turn a listing into an N+1 — see
    `app.api.agents.list_agents`'s own `scope_by_agent` pattern.
    """
    from app.api.agents import _decode, _hydrate_builder_axes

    out = dict(row)
    for key in ("created_at", "updated_at", "deleted_at"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    out["instructions"] = out.get("system_prompt") or ""
    knowledge, plugins = _hydrate_builder_axes(
        row["id"],
        _decode(row.get("knowledge"), []),
        _decode(row.get("plugins"), []),
        scope_rows=scope_rows,
    )
    out["knowledge"] = knowledge
    out["plugins"] = plugins
    out["surfaces"] = _decode(row.get("surfaces"), {})
    return out


def _validate_new_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise _err(
            400,
            "invalid_slug",
            "slug must be lowercase kebab-case (^[a-z0-9][a-z0-9-]{0,63}$)",
        )
    if slug in _RESERVED_SLUGS:
        raise _err(400, "slug_reserved", f"slug '{slug}' is reserved for the seeded default agent")


def _validate_mode_values(updates: Dict[str, Any]) -> None:
    """400 `invalid_mode` for any of the five mode fields set to a value
    outside its domain (`_UPDATABLE_FIELDS`/Pydantic only checks presence
    and type, not the enumerated value set)."""
    for field in _SELECTED_MODE_FIELDS:
        value = updates.get(field)
        if value is not None and value not in _SCOPE_MODE_VALUES:
            raise _err(
                400,
                "invalid_mode",
                f"{field} must be one of {sorted(_SCOPE_MODE_VALUES)}, got '{value}'",
            )
    memory_write_mode = updates.get("memory_write_mode")
    if memory_write_mode is not None and memory_write_mode not in _MEMORY_WRITE_MODE_VALUES:
        raise _err(
            400,
            "invalid_mode",
            f"memory_write_mode must be one of {sorted(_MEMORY_WRITE_MODE_VALUES)}, got '{memory_write_mode}'",
        )


def _is_token_live(token: Dict[str, Any]) -> bool:
    """A token is live if it isn't revoked and (has no expiry, or hasn't
    expired yet). Mirrors the expiry-comparison hardening in
    `app.auth.pat_resolver.resolve_token_to_user` (naive-datetime /
    ISO-string rows from either backend)."""
    if token.get("revoked_at") is not None:
        return False
    expires_at = token.get("expires_at")
    if expires_at is None:
        return True
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) <= expires_at


def _load_agent(
    agent_id: str,
    user: dict,
    conn: Optional[duckdb.DuckDBPyConnection],
    *,
    require_owner: bool,
) -> Dict[str, Any]:
    """Fetch `agent_id`, enforcing the ownership/admin/grantee auth matrix.

    404s for anyone who isn't the owner, an admin, or (READ-only, Task
    C1.1) a grantee — existence of another user's agent is never leaked.
    Admins pass the existence check (so GET works for governance) but
    `require_owner=True` (every mutating route, including token issuance)
    still 403s them on a foreign agent. A grantee (a `ResourceType.AGENT`
    row via one of the caller's groups — the /agents builder's own sharing
    reach, `app.api.agents._granted_agent_ids`) may likewise only READ:
    `require_owner=True` 404s a grantee exactly like any other non-owner,
    non-admin caller — a share conveys *use*, never *manage*.
    """
    row = agents_repo().get_by_id(agent_id)
    if not row or row.get("deleted_at") is not None:
        raise _err(404, "agent_not_found", "Agent not found")
    is_owner = row["owner_user_id"] == user["id"]
    if not is_owner:
        if is_user_admin(user["id"], conn):
            if require_owner:
                raise _err(403, "agent_not_owned", "Admins may inspect but not modify another user's agent")
        else:
            from app.api.agents import _granted_agent_ids

            if require_owner or agent_id not in _granted_agent_ids(user["id"]):
                raise _err(404, "agent_not_found", "Agent not found")
    return row


@router.post("", status_code=201)
async def create_agent(
    payload: CreateAgentRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = payload.name.strip()
    if not name:
        raise _err(400, "invalid_name", "name is required")

    prefill: Dict[str, str] = {}
    if payload.template_entity_id:
        from app.api.agents import _template_prefill

        prefill = _template_prefill(payload.template_entity_id, user)

    def _field(key: str, given: Optional[str], fallback: str = "") -> str:
        """Caller's value, else the template's, else a fallback — mirrors
        `app.api.agents.create_agent`'s own closure of the same name."""
        if (given or "").strip():
            return given
        return prefill.get(key) or fallback

    slug = (payload.slug or "").strip()
    if slug:
        _validate_new_slug(slug)
    else:
        # Auto-derive, exactly like the /agents builder does when the
        # caller has no opinion on the address (Task C1.1).
        from app.api.agents import _auto_slug, _unique_slug

        slug = _unique_slug(_auto_slug(name), user["id"])

    repo = agents_repo()
    if repo.get_by_slug(user["id"], slug) is not None:
        raise _err(409, "slug_taken", f"slug '{slug}' is already in use")

    # status: 'ready' unless the caller explicitly asks for 'draft' (the
    # builder-shape create). A pre-existing v1 client never sends `status`,
    # so it keeps landing on 'ready' — this route's original rationale
    # (an explicit, caller-chosen or auto-derived slug means the agent is
    # published) still holds by default. Opting into 'draft' is what makes
    # the slug-follow rule below live: `_draft_slug_rename` only re-derives
    # the slug for a `status='draft'` row, so a 'ready' agent's slug is
    # frozen exactly as before. See `_v114_to_v115` (src/db.py) for the
    # one-time backfill the original builder-address bug required.
    status = (payload.status or "").strip() or "ready"

    system_prompt_given = payload.system_prompt if payload.system_prompt is not None else payload.instructions
    system_prompt = _field("instructions", system_prompt_given, "") or None

    agent_id = str(uuid.uuid4())
    try:
        # API-created agents default all four scope modes to 'selected'
        # (spec §1) — the repo's own defaults are 'all', which is only
        # correct for the seeded default agent, so pass them explicitly.
        repo.create(
            id=agent_id,
            owner_user_id=user["id"],
            name=name,
            slug=slug,
            description=payload.description,
            system_prompt=system_prompt,
            model=payload.model,
            token_budget_monthly=payload.token_budget_monthly,
            plugins_mode="selected",
            connections_mode="selected",
            tables_mode="selected",
            memory_mode="selected",
            status=status,
            # Builder-shape projections (Task C1.1) — same columns, same
            # opaque JSON-text encoding `app.api.agents.create_agent` uses.
            role=_field("role", payload.role, ""),
            tone=_field("tone", payload.tone, "concise"),
            greeting=_field("greeting", payload.greeting, ""),
            knowledge=json.dumps(payload.knowledge) if payload.knowledge is not None else None,
            plugins=json.dumps(payload.plugins) if payload.plugins is not None else None,
            surfaces=json.dumps(payload.surfaces) if payload.surfaces is not None else None,
        )
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # Covers the tombstoned-slug race the pre-check above can't see
        # (get_by_slug only matches deleted_at IS NULL rows) — UNIQUE
        # (owner_user_id, slug) is unconditional, so a tombstoned slug still
        # raises here. DuckDB raises ConstraintException, Postgres (via
        # SQLAlchemy) raises IntegrityError — anything else is a genuine
        # 500, not a slug conflict, and must propagate.
        raise _err(409, "slug_taken", f"slug '{slug}' is already in use")

    # SAME scope-write path as the /agents builder (Task C1.1) — one
    # enforcement mapping regardless of which surface created the agent.
    # A no-op for a plain (non-builder-shape) create: a brand-new agent has
    # no existing `agent_scope` rows to replace.
    from app.api.agents import _sync_builder_scope

    _sync_builder_scope(agent_id, payload.knowledge or [], payload.plugins or [])

    row = repo.get_by_id(agent_id)
    _audit(user["id"], "agent.create", agent_id, {"slug": slug})
    return _serialize(row)  # type: ignore[arg-type]


@router.get("")
async def list_agents(user: dict = Depends(require_session_or_user_pat(allow_stack_surface=True))):
    """The caller's own agents plus any shared into a group they belong to
    (Task C1.1 — same reach as the /agents builder's `list_agents`, via the
    same `ResourceType.AGENT` grant)."""
    from app.api.agents import _granted_agent_ids

    repo = agents_repo()
    uid = user["id"]
    rows: List[Dict[str, Any]] = list(repo.list_for_user(uid))
    seen = {r["id"] for r in rows}
    for agent_id in _granted_agent_ids(uid):
        if agent_id in seen:
            continue
        row = repo.get_by_id(agent_id)
        if row and row.get("deleted_at") is None:
            rows.append(row)
            seen.add(agent_id)
    # ONE scope read for the whole page — `_serialize` hydrates an empty
    # knowledge/plugins declaration from `agent_scope`, and per-agent reads
    # would turn this listing into an N+1 (mirrors `app.api.agents.
    # list_agents`'s own batched read, Devin Review on #1520).
    scope_by_agent = repo.get_scope_for_agents([r["id"] for r in rows]) if rows else {}
    return {
        "data": [_serialize(r, scope_rows=scope_by_agent.get(r["id"], [])) for r in rows],
        "has_more": False,
        "next_cursor": None,
    }


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    user: dict = Depends(require_session_or_user_pat(allow_stack_surface=True)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=False)
    # One scope read, reused both for the builder-shape hydration in
    # `_serialize` and for the raw `scope` list below.
    scope_rows = agents_repo().get_scope(agent_id)
    out = _serialize(row, scope_rows=scope_rows)
    # Detail view carries the scope items so callers (the CLI's replace-not-
    # merge warning, the wiring runbooks) can see what a scope PUT would drop
    # without a second bespoke endpoint. List view stays lean.
    out["scope"] = scope_rows
    return out


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: UpdateAgentRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    before = _load_agent(agent_id, user, conn, require_owner=True)

    supplied = payload.model_dump(exclude_unset=True)
    if "slug" in supplied:
        raise _err(400, "slug_immutable", "slug cannot be changed after creation")

    # Builder-shape wire fields (Task C1.1): `instructions` aliases the
    # canonical `system_prompt` column (an explicit `system_prompt` in the
    # same payload wins); `knowledge`/`plugins`/`surfaces` are opaque JSON
    # text on the row, encoded here exactly like `app.api.agents.
    # update_agent` encodes them for the /agents builder.
    updates: Dict[str, Any] = {k: v for k, v in supplied.items() if k != "instructions"}
    if "instructions" in supplied and "system_prompt" not in updates:
        updates["system_prompt"] = supplied["instructions"]
    for key in ("knowledge", "plugins", "surfaces"):
        if key in updates:
            updates[key] = json.dumps(updates[key])

    # Same forced-narrowing default as the /agents builder's own PATCH
    # (`app.api.agents.update_agent`'s `rescope` block) — closing the
    # reopened half of #1520. A knowledge/plugins edit re-derives the
    # enforced scope, and any mode axis the caller did NOT set explicitly in
    # THIS request must not keep sitting at 'all' just because this route
    # forgot to touch it — that is exactly how the seeded default agent
    # (born at mode='all' on all four axes,
    # `src.repositories.agents.get_or_create_default`, reachable through
    # this route since only DELETE is guarded against it) turns a "narrow to
    # one package" PUT into cosmetic `knowledge` with the owner's whole
    # stack still riding underneath every untouched axis. An axis the caller
    # DID set explicitly in this same request (`setdefault` no-ops) is left
    # alone — explicit is intentional, `_validate_mode_values` below still
    # gates it and the widen-to-'all' guard below still applies to it.
    rescope = "knowledge" in supplied or "plugins" in supplied
    if rescope:
        for field in _SELECTED_MODE_FIELDS:
            updates.setdefault(field, "selected")

    # Belt-and-suspenders: today `set(updates)` (Pydantic's exclude_unset
    # field set, minus slug/instructions) is always a subset of
    # _UPDATABLE_FIELDS by construction — UpdateAgentRequest declares no
    # other fields. Keeps this guard live so a future field added to the
    # request model without a matching _UPDATABLE_FIELDS entry fails loudly
    # instead of silently reaching `agents_repo().update()`.
    bad = set(updates) - _UPDATABLE_FIELDS
    if bad:
        raise _err(400, "invalid_field", f"cannot update field(s): {sorted(bad)}")

    _validate_mode_values(updates)

    # Widen-to-'all' guard (spec §2): agent PATs are issuable only while all
    # four scope modes are 'selected', but that's an issuance-time check —
    # 'all' mirrors the owner's LIVE stack, so widening any scope mode to
    # 'all' *after* a PAT already exists would silently upgrade that PAT
    # into a full-user credential (every plugin/connection/table/memory
    # domain the owner has or ever installs). Issuance-time gating alone
    # doesn't survive a later widen, so re-check here: reject the widen
    # while a live (non-revoked, non-expired) agent PAT exists.
    widened_to_all = [field for field in _SELECTED_MODE_FIELDS if updates.get(field) == "all"]
    if widened_to_all and any(_is_token_live(t) for t in access_token_repo().list_for_agent(agent_id)):
        raise _err(
            409,
            "agent_has_live_tokens",
            "revoke agent tokens before widening scope to 'all'",
        )
    if widened_to_all:
        # Same after-the-fact widening hazard as the PAT rule above, for
        # Slack bindings: a binding is refused on an all-'all' agent at
        # scope-PUT time, but widening every mode AFTER binding would land
        # in the same place — channel turns riding the owner's plain
        # identity. Re-check here.
        current = agents_repo().get_by_id(agent_id) or {}
        effective = {f: updates.get(f, current.get(f)) for f in _SELECTED_MODE_FIELDS}
        if all(v == "all" for v in effective.values()) and any(
            i.get("item_type") == "slack_channel" for i in agents_repo().get_scope(agent_id)
        ):
            raise _err(
                409,
                "agent_has_slack_binding",
                "remove the agent's slack_channel binding(s) before widening every "
                "scope mode to 'all' — a bound channel must never run turns under "
                "the owner's plain identity",
            )

    # Renaming a draft re-derives its slug, exactly like the /agents
    # builder's own PATCH (`app.api.agents._draft_slug_rename`, Task
    # C1.1). Inert for every pre-existing v1 agent — this route only ever
    # created `status='ready'` rows until this task, and 'ready' never
    # re-derives.
    from app.api.agents import _draft_slug_rename

    new_slug = _draft_slug_rename(before, updates.get("name"), before.get("owner_user_id") or user["id"])
    if new_slug:
        updates["slug"] = new_slug

    if updates:
        agents_repo().update(agent_id, **updates)
        _audit(user["id"], "agent.update", agent_id, {"fields": sorted(updates)})

    # SAME scope-write path as the /agents builder (Task C1.1): a
    # knowledge/plugins PUT replaces the builder-owned `agent_scope` rows
    # while preserving governance-owned ones (slack_channel/table/
    # connection) — `_sync_builder_scope` handles both. The unsent axis is
    # read back off the SAME hydrated view the builder shows
    # (`_hydrate_builder_axes`), not the raw JSON columns, so a PUT
    # touching only one axis cannot silently wipe the other.
    if rescope:
        from app.api.agents import _decode, _hydrate_builder_axes, _sync_builder_scope

        held_knowledge, held_plugins = _hydrate_builder_axes(
            agent_id,
            _decode(before.get("knowledge"), []),
            _decode(before.get("plugins"), []),
        )
        _sync_builder_scope(
            agent_id,
            supplied.get("knowledge", held_knowledge),
            supplied.get("plugins", held_plugins),
        )

    return _serialize(agents_repo().get_by_id(agent_id))  # type: ignore[arg-type]


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=True)
    if row.get("is_default"):
        raise _err(400, "default_agent_undeletable", "the default agent cannot be deleted")

    agents_repo().soft_delete(agent_id)
    # Revoke every PAT minted for this agent — a deleted agent must not
    # leave live credentials behind. Belt-and-suspenders, not the sole
    # guard: `soft_delete` and `revoke_for_agent` are two separate
    # connections/transactions, so if this call fails after the soft-delete
    # above already committed, an orphaned agent PAT would otherwise keep
    # authenticating. `app.auth.pat_resolver.resolve_token_to_user` closes
    # that gap independently — on the `typ="agent_pat"` path it loads the
    # agent row by the JWT's `agent_id` claim and rejects
    # (`"agent_pat_agent_deleted"`) when it is missing or soft-deleted, so a
    # deleted agent's PAT dies even if this revoke call never runs.
    access_token_repo().revoke_for_agent(agent_id)
    _cascade_delete_agent_resources(agent_id)
    # Same cleanup the /agents builder's own delete does (Task C1.1): drop
    # any sharing grants too, so a later agent can never inherit a dangling
    # grant through id reuse and /admin/access shows no orphan row.
    try:
        resource_grants_repo().delete_by_resource(ResourceType.AGENT.value, agent_id)
    except Exception:
        logger.warning("agents_admin: grant cleanup failed for %s", agent_id, exc_info=True)
    _audit(user["id"], "agent.delete", agent_id)


def _cascade_delete_agent_resources(agent_id: str) -> None:
    """Delete standing resources owned by a just-deleted agent: outbound
    webhook registrations (C14, agent-api V1b Task 8), harvested sandbox
    artifacts (both their `agent_artifacts` rows and their object-store
    blobs, same task), and the agent's private memory notebook (C5,
    agent-api V1c Task 2).

    Best-effort on the blob deletes only — a single `delete_object` failure
    is logged and skipped rather than aborting the cascade (an orphaned
    blob under a deleted agent's `agent-artifacts/{session_id}/...` prefix
    is a cheap, non-sensitive leak; leaving the agent half-deleted because
    one blob's DELETE 5xx'd is worse). The `agent_artifacts` / `agent_memories`
    row deletes themselves are NOT best-effort — those are ordinary DB
    statements on the same connection/transaction discipline as the rest of
    this route.
    """
    webhook_count = len(agent_webhooks_repo().list_for_agent(agent_id))
    if webhook_count:
        agent_webhooks_repo().delete_for_agent(agent_id)

    artifact_rows = agent_artifacts_repo().list_for_agent(agent_id)
    if artifact_rows:
        store = object_store()
        if store is not None:
            for row in artifact_rows:
                try:
                    store.delete_object(row["object_key"])
                except Exception:
                    logger.exception(
                        "agent.delete cascade: failed to delete object-store blob %s for agent %s — "
                        "leaving it orphaned, continuing",
                        row.get("object_key"),
                        agent_id,
                    )
        agent_artifacts_repo().delete_for_agent(agent_id)

    memory_count = len(agent_memories_repo().list_for_agent(agent_id))
    if memory_count:
        agent_memories_repo().delete_for_agent(agent_id)

    # Schedules die with the agent (agent-schedules design, v119) — without
    # this the rows sit orphaned forever, invisibly: the run-due sweep skips
    # soft-deleted agents, so nothing ever surfaces the leak.
    agent_schedules_repo().delete_for_agent(agent_id)


@router.put("/{agent_id}/scope")
async def set_agent_scope(
    agent_id: str,
    payload: SetScopeRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _load_agent(agent_id, user, conn, require_owner=True)

    items: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.items:
        if item.item_type not in _ITEM_TYPES:
            raise _err(
                400,
                "invalid_item_type",
                f"item_type must be one of {sorted(_ITEM_TYPES)}, got '{item.item_type}'",
            )
        key = (item.item_type, item.item_id)
        # Dedupe (item_type, item_id) pairs, preserving first-seen order —
        # the composite PK on `agent_scope` means a duplicate pair in one
        # request would otherwise 500 on the second INSERT.
        if key in seen:
            continue
        seen.add(key)
        items.append(key)

    has_binding = any(item_type == "slack_channel" for item_type, _ in items)
    if has_binding:
        from src.agent_scope_intersection import agent_is_passthrough

        agent_row = agents_repo().get_by_id(agent_id)
        if agent_row is not None and agent_is_passthrough(agent_row):
            # A routed session runs AS the owner; for an all-'all' agent the
            # broker's passthrough optimization would mint the owner's PLAIN
            # identity (admin short-circuit included) — binding one would
            # lend every gated channel member the owner's full authority.
            # Require at least one 'selected' mode so routed turns always
            # carry the enforced AgentPrincipal.
            raise _err(
                400,
                "binding_requires_selected_scope",
                "an agent with every scope mode set to 'all' cannot hold a slack_channel "
                "binding — set at least one of plugins/connections/tables/memory to "
                "'selected' first, so channel turns run under the enforced agent scope "
                "instead of the owner's plain identity",
            )

    for item_type, item_id in items:
        if item_type != "slack_channel":
            continue
        holder = agents_repo().agent_for_scope_item("slack_channel", item_id)
        if holder is not None and holder["id"] != agent_id:
            # Name the holder only when the caller could see it anyway
            # (their own agent, or an admin) — the module invariant is that
            # a foreign agent's existence/slug is never leaked, and the slug
            # doubles as the public /responses address.
            if holder.get("owner_user_id") == user["id"] or is_user_admin(user["id"]):
                who = f"agent '{holder.get('slug') or holder['id']}'"
            else:
                who = "another user's agent"
            raise _err(
                409,
                "slack_channel_taken",
                f"slack channel '{item_id}' is already bound to {who} — unbind it there first (one agent per channel)",
            )

    agents_repo().set_scope(agent_id, items)
    _audit(user["id"], "agent.scope.set", agent_id, {"count": len(items)})
    return {"items": [{"item_type": t, "item_id": i} for t, i in items]}


@router.post("/{agent_id}/tokens")
async def create_agent_token(
    agent_id: str,
    payload: CreateAgentTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=True)

    if not all(row.get(field) == "selected" for field in _SELECTED_MODE_FIELDS):
        raise _err(
            403,
            "agent_not_selected_mode",
            "agent PATs require all four scope modes (plugins/connections/tables/memory) "
            "to be 'selected' — never for 'all'-mode agents",
        )

    name = payload.name.strip()
    if not name:
        raise _err(400, "invalid_name", "name is required")
    if payload.expires_in_days is not None and payload.expires_in_days <= 0:
        raise _err(400, "invalid_expiry", "expires_in_days must be a positive integer")
    if payload.expires_in_days is not None and payload.expires_in_days > 3650:
        raise _err(400, "invalid_expiry", "expires_in_days must not exceed 3650 (10 years)")

    omit_exp = payload.expires_in_days is None
    expires_delta = timedelta(days=payload.expires_in_days) if payload.expires_in_days is not None else None
    expires_at = datetime.now(timezone.utc) + expires_delta if expires_delta is not None else None

    # jti / prefix / hash mechanics mirror app/api/tokens.py::create_token
    # exactly. CRITICAL: the DB row's agent_id and the JWT's agent_id claim
    # must both be set to the SAME agent_id — kept in sync by construction
    # here (one `agent_id` local var feeds both `extra_claims` and
    # `repo.create`), not by two independently-derived values.
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="agent_pat",
        expires_delta=expires_delta,
        omit_exp=omit_exp,
        extra_claims={"agent_id": agent_id},
    )
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name=name,
        token_hash=token_hash,
        prefix=prefix,
        expires_at=expires_at,
        agent_id=agent_id,
    )
    _audit(user["id"], "agent.token.create", token_id, {"agent_id": agent_id, "name": name})

    return {
        "id": token_id,
        "name": name,
        "prefix": prefix,
        "agent_id": agent_id,
        "token": jwt_token,  # returned EXACTLY ONCE; never retrievable again
        "expires_at": str(expires_at) if expires_at else None,
        "created_at": str(datetime.now(timezone.utc)),
    }


# ---------------------------------------------------------------------------
# Memory management — inspect/approve/archive/delete (agent-api V1c Task 5)
# ---------------------------------------------------------------------------

_MEMORY_ACTIONS = frozenset({"approve", "archive"})


class MemoryActionRequest(BaseModel):
    action: str


def _in_budget_ids(agent_id: str) -> set:
    """The set of memory ids that actually materialize into a fresh spawn
    right now — see the C4 binding addition in the V1c Task 5 brief:
    "active" alone doesn't mean "in effect" once the active set exceeds
    `app.chat.agent_profile._MEMORY_BUDGET_CHARS`. Reuses the exact same
    `select_in_budget` split `materialize_memories` uses at spawn time, so
    this list can never drift from what actually lands in a session."""
    from app.chat.agent_profile import _MEMORY_BUDGET_CHARS, select_in_budget

    active_rows = agent_memories_repo().list_active(agent_id)
    in_budget, _shadowed = select_in_budget(active_rows, _MEMORY_BUDGET_CHARS)
    return {m["id"] for m in in_budget}


def _serialize_memory(row: Dict[str, Any], in_budget_ids: set) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "content": row["content"],
        "status": row["status"],
        "source_session_id": row.get("source_session_id"),
        "created_at": str(row["created_at"]) if row.get("created_at") is not None else None,
        "activated_at": str(row["activated_at"]) if row.get("activated_at") is not None else None,
        "archived_at": str(row["archived_at"]) if row.get("archived_at") is not None else None,
    }
    # Only meaningful for active rows — pending/archived memories never
    # materialize into a spawn regardless of budget, so the key is omitted
    # rather than misleadingly reporting `false`.
    if row["status"] == "active":
        out["in_budget"] = row["id"] in in_budget_ids
    return out


def _load_agent_memory(agent_id: str, memory_id: str) -> Dict[str, Any]:
    row = agent_memories_repo().get(memory_id)
    if row is None or row["agent_id"] != agent_id:
        raise _err(404, "memory_not_found", "Memory not found")
    return row


@router.get("/{agent_id}/memories")
async def list_agent_memories(
    agent_id: str,
    status: Optional[str] = None,
    user: dict = Depends(require_session_or_user_pat()),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Read-only — admins may inspect (require_owner=False), mirrors get_agent.
    _load_agent(agent_id, user, conn, require_owner=False)
    rows = agent_memories_repo().list_for_agent(agent_id, status=status)
    in_budget_ids = _in_budget_ids(agent_id)
    return {
        "data": [_serialize_memory(r, in_budget_ids) for r in rows],
        "has_more": False,
        "next_cursor": None,
    }


@router.patch("/{agent_id}/memories/{memory_id}")
async def update_agent_memory(
    agent_id: str,
    memory_id: str,
    payload: MemoryActionRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _load_agent(agent_id, user, conn, require_owner=True)
    _load_agent_memory(agent_id, memory_id)

    if payload.action not in _MEMORY_ACTIONS:
        raise _err(
            400,
            "invalid_action",
            f"action must be one of {sorted(_MEMORY_ACTIONS)}, got '{payload.action}'",
        )

    repo = agent_memories_repo()
    if payload.action == "approve":
        repo.approve(memory_id)
    else:
        repo.archive(memory_id)
    _audit(user["id"], f"agent.memory.{payload.action}", memory_id, {"agent_id": agent_id})

    updated = repo.get(memory_id)
    assert updated is not None  # just mutated above, same transaction/connection
    return _serialize_memory(updated, _in_budget_ids(agent_id))


@router.delete("/{agent_id}/memories/{memory_id}", status_code=204)
async def delete_agent_memory(
    agent_id: str,
    memory_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _load_agent(agent_id, user, conn, require_owner=True)
    _load_agent_memory(agent_id, memory_id)

    agent_memories_repo().delete(memory_id)
    _audit(user["id"], "agent.memory.delete", memory_id, {"agent_id": agent_id})
