"""Agents API — CRUD for the caller's composed assistants.

Endpoints:

  GET    /api/agents                 auth (owned ∪ shared-with-my-groups)
  POST   /api/agents                 auth (owned by creator)
  GET    /api/agents/{agent_id}      owner, grantee, or admin
  PATCH  /api/agents/{agent_id}      owner or admin
  DELETE /api/agents/{agent_id}      owner or admin

RBAC model: **create** = any authenticated user (the agent is owned by its
creator and private until shared); **read** = owner, admin, or any user whose
groups hold a ``resource_grants`` row for ``(agent, <agent_id>)``;
**update/delete** = owner or admin. Sharing itself is written through
``/api/sharing`` (``app/api/sharing.py``), so a grant made there is honored
here — the agent read path is the reader that makes agent grants real rather
than decorative.

Fail-closed: an agent the caller cannot read returns 404 (not 403), matching
the collections contract, so callers cannot probe for existence.

This is the paper-theme agent-BUILDER surface. When the paper-theme branch
merged into main it stopped owning its own ``agents`` table: main's
agent-as-API subsystem is the canonical owner, and the builder's authored
fields ride it as a SUPERSET (``src/db.py`` v110 / ``src/models/agents.py``).
This module is the thin adapter between the builder's wire shape and
``AgentsRepository`` — it maps the builder's ``created_by`` → the table's
``owner_user_id`` and ``instructions`` → ``system_prompt``, and JSON-encodes
the opaque ``knowledge`` / ``plugins`` / ``surfaces`` id-lists into the
TEXT columns.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import is_user_admin, require_agent_profiles_enabled
from app.auth.dependencies import get_current_user
from app.resource_types import ResourceType
from app.services.journey import mark_journey
from src.repositories import agents_repo, resource_grants_repo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
    # Same instance-level kill switch as the five /api/v1 agent routers —
    # this builder CRUD API works the same `agents` table, so leaving it
    # open would let a disabled instance keep managing agent profiles.
    dependencies=[Depends(require_agent_profiles_enabled)],
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RT = ResourceType.AGENT.value
# Reserved for the per-owner seeded default agent (``agents_repo().
# get_or_create_default``), never claimable by a user-created one. Mirrors
# ``app/api/agents_admin.py``'s ``_RESERVED_SLUGS``.
_DEFAULT_AGENT_SLUG = "default"


def _auto_slug(name: str) -> str:
    """URL-safe slug from an agent name.

    Falls back to ``"agent"`` for names with no alphanumerics (e.g. "!!!"),
    which would otherwise collapse to an empty slug and cause spurious
    collisions on the second such name. Mirrors
    ``app/api/collections.py::_auto_slug``.
    """
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "agent"


def _slug_tracks_name(slug: str, name: Any) -> bool:
    """Is ``slug`` what this module would derive from ``name``?

    Accepts the uniqueness suffix (``revenue-analyst-2``), so an agent that
    lost a name race still counts as tracking. An unnamed agent tracks the
    literal ``agent`` placeholder, since that is what ``create_agent``
    derives from an empty name — the placeholder is not a special case, it
    is just the slug of a nameless agent.
    """
    base = _auto_slug((name or "").strip() if isinstance(name, str) else "")
    return slug == base or re.fullmatch(rf"{re.escape(base)}-\d+", slug) is not None


def _draft_slug_rename(before: Dict[str, Any], new_name: Any, owner_user_id: str) -> Optional[str]:
    """The slug to move to when renaming a draft, else ``None``.

    The builder creates the row on "New agent", before the user types a
    name, so ``create_agent`` falls back to the slug ``agent`` (then
    ``agent-2``, …). Nothing re-derived it afterwards, so an agent
    displayed as "Revenue Analyst" kept answering on ``/agent`` — and the
    slug is the public address (``POST /api/v1/agents/{slug}/responses``,
    ``agnes chat <slug>``), which the builder shows nowhere.

    The slug follows the name for as long as BOTH hold:

    - the agent is still a ``draft``. Marking it ready is what publishes
      the address, and from then on it may be wired into a script, so it
      freezes — placeholder or not.
    - its slug still tracks its current name. A slug set by any other
      means has stopped being a function of the name, so a rename must not
      silently relocate it.

    Re-deriving on EVERY draft rename, rather than once off the
    placeholder, is what the builder's save behaviour requires: it PATCHes
    each field edit behind a short debounce, so pausing mid-word flushes a
    partial name. A once-only rule would latch onto that fragment and hand
    the finished agent the address ``rev`` — worse than the placeholder it
    replaced, and not fixable from the UI (Devin Review on #1225).

    Returns ``None`` when the re-derived slug would equal the current one,
    so an unsluggable name ("!!!", which falls back to ``agent``) cannot
    push the agent onto ``agent-2`` via the uniqueness search.
    """
    name = (new_name or "").strip() if isinstance(new_name, str) else ""
    if not name:
        return None
    if before.get("is_default"):
        # The seeded default agent's slug is a RESERVED address —
        # `POST /api/v1/agents/default/responses`, `_RESERVED_SLUGS` in
        # agents_admin.py, and every web chat's attribution
        # (`app/api/chat.py::_default_agent_id`) resolve through it. It is
        # also seeded with no status, which COALESCEs to `draft`, so it is a
        # PERMANENT draft the builder happily renames: without this exemption
        # the rule would relocate that address and never freeze it again.
        return None
    if (before.get("status") or "") != "draft":
        return None
    current = before.get("slug") or ""
    if not _slug_tracks_name(current, before.get("name")):
        return None
    if _slug_tracks_name(current, name):
        # Already an acceptable slug for the NEW name — including a suffixed
        # form. Bailing here is what stops the slug walking upward: the
        # builder re-sends the unchanged `name` on every field edit, and
        # `_unique_slug` has no notion of the row being updated, so it counts
        # this agent's own `revenue-analyst-2` as taken and would answer
        # `-3`, then `-4` on the next save, up to the 999 cap. It also covers
        # the unsluggable rename ("!!!" -> the `agent` placeholder the row
        # already holds).
        return None
    resolved = _unique_slug(_auto_slug(name), owner_user_id)
    return None if resolved == current else resolved


def _unique_slug(base: str, owner_user_id: str) -> str:
    """First free slug in ``base``, ``base-2``, ``base-3``, … for one owner.

    Agents are user-named and duplicates are ordinary (one person naming two
    agents "Analyst"), so a name clash must not surface as a 409 the way an
    admin-curated slug would. The table's ``(owner_user_id, slug)`` UNIQUE is
    per-owner, so the search is scoped to the owner.

    ``include_deleted=True`` is load-bearing: ``delete`` only sets
    ``deleted_at`` while the UNIQUE spans deleted rows, so searching live rows
    only would report a soft-deleted agent's slug as free and drive the INSERT
    straight into a ConstraintException.

    ``"default"`` is reserved for the per-owner seeded default agent, exactly as
    ``app/api/agents_admin.py``'s ``_RESERVED_SLUGS`` treats it. The governance
    router rejects it outright; here the slug is derived from a user-typed name,
    so an agent called "Default" is suffixed to ``default-2`` rather than 400'd.
    Without this an ordinary name could claim the slug before the owner's first
    chat seeded the real default — which then lands on ``default-2``, and
    ``POST /api/v1/agents/default/responses`` would address the user's agent
    instead of the default.
    """
    repo = agents_repo()
    if base != _DEFAULT_AGENT_SLUG and repo.get_by_slug(owner_user_id, base, include_deleted=True) is None:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"[:100].strip("-")
        if repo.get_by_slug(owner_user_id, candidate, include_deleted=True) is None:
            return candidate
    # Pathological (999 same-named agents) — fall back to a random suffix
    # rather than raising, so creation never hard-fails on naming alone.
    import secrets

    return f"{base}-{secrets.token_hex(4)}"[:100]


class AgentCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=500)
    instructions: str = ""
    #: `None` (omitted) means "caller has no opinion" — distinct from an
    #: explicit `"concise"`, which is what the builder's blank-agent shell
    #: sends and must still win over a template's own tone. `create_agent`'s
    #: `_field()` falls back to the template's prefill, then to `"concise"`.
    tone: Optional[str] = Field(default=None, max_length=50)
    greeting: str = ""
    knowledge: List[str] = Field(default_factory=list)
    plugins: List[str] = Field(default_factory=list)
    surfaces: Optional[Dict[str, bool]] = None
    status: str = Field(default="draft", max_length=30)
    #: Start from a Library Agent Template (store entity, ``type='agent'``).
    #: Prefills BEHAVIOUR only — see ``_template_prefill``.
    template_entity_id: Optional[str] = Field(default=None, max_length=200)


class AgentUpdate(BaseModel):
    """All fields optional — a PATCH sends only what changed."""

    name: Optional[str] = Field(default=None, max_length=200)
    role: Optional[str] = Field(default=None, max_length=500)
    instructions: Optional[str] = None
    tone: Optional[str] = Field(default=None, max_length=50)
    greeting: Optional[str] = None
    knowledge: Optional[List[str]] = None
    plugins: Optional[List[str]] = None
    surfaces: Optional[Dict[str, bool]] = None
    status: Optional[str] = Field(default=None, max_length=30)


def _decode(raw: Any, fallback: Any) -> Any:
    """Decode a JSON-text column (knowledge/plugins/surfaces) to its object."""
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


# --- Builder declaration -> enforced scope -------------------------------
#
# The builder's `knowledge` / `plugins` columns are the UI's declaration;
# `agent_scope` + the four `*_mode` columns are what the runtime actually
# enforces (`src/agent_scope_intersection.py`). Before this, the builder
# wrote only the former, leaving every mode at the repo default 'all' — so
# an agent the UI showed as scoped to two packages ran with its owner's
# ENTIRE stack. The two are kept in agreement by deriving the latter from
# the former on every create/update; the JSON columns stay authoritative for
# what the page renders, so they cannot drift out of sync.

#: Knowledge-section item types, in probe order. A knowledge id is one of
#: these three kinds (see `agents_page`'s `knowledge_sources`), and only the
#: id reaches the API, so the server re-derives the kind by lookup.
_KNOWLEDGE_ITEM_TYPES = ("data_package", "memory_domain", "collection")

#: Scope rows the builder OWNS and may therefore replace wholesale. Any other
#: item_type — `slack_channel` routing bindings, `table` / `connection` rows
#: set through `/api/v1/agents` or `agnes agent scope set` — belongs to the
#: governance surface and must survive a builder edit untouched.
_BUILDER_ITEM_TYPES = frozenset({*_KNOWLEDGE_ITEM_TYPES, "plugin"})


def _classify_knowledge(ids: List[str]) -> List[tuple]:
    """Map builder knowledge ids onto ``(item_type, item_id)`` scope rows.

    Deliberately does NOT check whether the caller can reach the resource:
    the runtime intersects every declared id with the owner's live grants
    (`compute_agent_intersection`), so an id the owner cannot reach is inert
    rather than dangerous, and refusing it here would 422 a builder save for
    a package whose grant is merely being reorganized.

    An id matching no registry is dropped with a log line — storing it would
    be an enforced-scope row that can never resolve.
    """
    from src.repositories import data_packages_repo, file_corpora_repo, memory_domains_repo

    out: List[tuple] = []
    for raw in ids:
        item_id = (raw or "").strip()
        if not item_id:
            continue
        for item_type, lookup in (
            ("data_package", lambda i: data_packages_repo().get(i)),
            ("memory_domain", lambda i: memory_domains_repo().get(i)),
            ("collection", lambda i: file_corpora_repo().get(i)),
        ):
            try:
                if lookup(item_id):
                    out.append((item_type, item_id))
                    break
            except Exception as e:  # a registry blip must not fail the save
                logger.warning("agents: %s lookup failed for %s: %s", item_type, item_id, e)
        else:
            logger.warning("agents: knowledge id %s matches no known resource — not scoped", item_id)
    return out


def _sync_builder_scope(agent_id: str, knowledge: List[str], plugins: List[str]) -> None:
    """Rewrite the agent's builder-owned ``agent_scope`` rows from a
    declaration, preserving every governance-owned row.

    ``set_scope`` replaces the whole set, so the preserved rows have to be
    read and passed back through — dropping a `slack_channel` binding here
    would silently unroute a channel whose turns then fall back to the
    mentioning user's own authority.
    """
    repo = agents_repo()
    preserved = [
        (i["item_type"], i["item_id"])
        for i in repo.get_scope(agent_id)
        if i.get("item_type") not in _BUILDER_ITEM_TYPES
    ]
    declared = _classify_knowledge(knowledge or [])
    declared += [("plugin", p.strip()) for p in (plugins or []) if (p or "").strip()]
    # Dedupe, preserving order: `agent_scope`'s composite PK rejects a
    # repeated pair, and `set_scope` inserts row by row.
    seen: set = set()
    items: List[tuple] = []
    for pair in preserved + declared:
        if pair in seen:
            continue
        seen.add(pair)
        items.append(pair)
    repo.set_scope(agent_id, items)


def _granted_agent_ids(user_id: str) -> set:
    """Agent ids granted to one of ``user_id``'s groups."""
    try:
        return set(resource_grants_repo().list_resource_ids_for_user(user_id, _RT))
    except Exception as e:
        logger.warning("agents: could not resolve grants for %s: %s", user_id, e)
        return set()


def _hydrate_builder_axes(
    agent_id: str,
    knowledge: List[str],
    plugins: List[str],
    scope_rows: Optional[List[dict]] = None,
) -> tuple:
    """Fill empty ``knowledge``/``plugins`` from the stored ``agent_scope`` rows.

    The two writers disagree about where a builder-axis scope lives.
    ``/api/v1/agents`` and ``agnes agent scope set`` write ``agent_scope`` rows
    and never touch the JSON columns; the builder writes both. The same agent
    is listed in ``/agents`` either way (``list_for_user`` returns every owned
    agent), so a governance-created agent rendered "0 sources · 0 tools" while
    holding real scope — and the first declaration edit wiped all four
    builder-owned axes through :func:`_sync_builder_scope`, with the user never
    having seen what they were destroying.

    Hydrating on read makes the screen agree with what the runtime enforces and
    makes a save round-trip lossless. Only EMPTY lists are filled: once the
    builder has written a declaration, that declaration is the truth and an
    axis the user cleared must stay cleared.

    ``scope_rows`` lets a caller supply this agent's rows from a batched read.
    The list endpoint projects every agent the caller can see, so leaving each
    to fetch its own made the listing an N+1 (Devin Review on #1520). Omitted,
    the rows are read here for the single-agent path.
    """
    if knowledge and plugins:
        return knowledge, plugins
    if scope_rows is not None:
        rows = scope_rows
    else:
        try:
            rows = agents_repo().get_scope(agent_id)
        except Exception as e:  # a scope read must never 500 the builder
            logger.warning("agents: could not hydrate builder axes for %s: %s", agent_id, e)
            return knowledge, plugins
    if not knowledge:
        knowledge = [r["item_id"] for r in rows if r.get("item_type") in _KNOWLEDGE_ITEM_TYPES]
    if not plugins:
        plugins = [r["item_id"] for r in rows if r.get("item_type") == "plugin"]
    return knowledge, plugins


def _agent_out(row: dict, *, uid: str, scope_rows: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Wire shape — the builder's in-browser object 1:1, projected off main's
    canonical row (``owner_user_id`` → ``created_by``, ``system_prompt`` →
    ``instructions``, JSON-text id-lists decoded), plus server-side ownership
    so the Library can label rows without a second call.
    """
    owner = row.get("owner_user_id")
    knowledge, plugins = _hydrate_builder_axes(
        row["id"],
        _decode(row.get("knowledge"), []),
        _decode(row.get("plugins"), []),
        scope_rows=scope_rows,
    )
    return {
        "id": row["id"],
        "slug": row.get("slug"),
        "name": row.get("name") or "",
        "role": row.get("role") or "",
        "instructions": row.get("system_prompt") or "",
        "tone": row.get("tone") or "concise",
        "greeting": row.get("greeting") or "",
        "knowledge": knowledge,
        "plugins": plugins,
        "surfaces": _decode(row.get("surfaces"), {}),
        "status": row.get("status") or "draft",
        # The seeded default agent cannot be deleted (see ``delete_agent``), so
        # the page needs to know which row that is — otherwise it renders a
        # delete control that always 400s.
        "is_default": bool(row.get("is_default")),
        "mine": owner == uid,
        "created_by": owner,
        "created_at": row["created_at"].isoformat() if row.get("created_at") is not None else None,
        "updated": row["updated_at"].isoformat() if row.get("updated_at") is not None else None,
    }


def _live(agent_id: str) -> Optional[dict]:
    """Fetch a non-soft-deleted agent by id (``get_by_id`` includes tombstones)."""
    row = agents_repo().get_by_id(agent_id)
    if not row or row.get("deleted_at") is not None:
        return None
    return row


def _readable(agent_id: str, user: dict) -> dict:
    """Fetch an agent the caller may READ, else 404.

    Readable = owner, admin, or a grantee via one of their groups.
    """
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    uid = user["id"]
    if row.get("owner_user_id") == uid:
        return row
    if is_user_admin(uid):
        return row
    if agent_id in _granted_agent_ids(uid):
        return row
    raise HTTPException(status_code=404, detail="agent_not_found")


def _writable(agent_id: str, user: dict) -> dict:
    """Fetch an agent the caller may MUTATE, else 404.

    A grant conveys *use*, not authorship — only the owner (or an admin) may
    edit or delete, so a shared agent can't be rewritten under its author.
    """
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    if row.get("owner_user_id") != user["id"] and not is_user_admin(user["id"]):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return row


@router.get("")
async def list_agents(user: dict = Depends(get_current_user)):
    """The caller's agents plus any shared into a group they belong to.

    Deliberately NOT admin god-mode: an admin sees their own agent list here,
    not every agent in the instance (that audit view is /admin/access).
    """
    uid = user["id"]
    repo = agents_repo()
    rows: List[dict] = []
    seen: set = set()
    try:
        for row in repo.list_for_user(uid):
            rows.append(row)
            seen.add(row["id"])
    except Exception as e:
        logger.warning("agents: could not enumerate for %s: %s", uid, e)
    for agent_id in _granted_agent_ids(uid):
        if agent_id in seen:
            continue
        row = _live(agent_id)
        if row:
            rows.append(row)
    # ONE scope read for the whole page. `_agent_out` hydrates an empty
    # declaration axis from these rows, so letting each agent fetch its own
    # turned the listing into an N+1 (Devin Review on #1520). A failure here
    # degrades to per-agent reads inside `_hydrate_builder_axes`, never to a
    # 500 and never to a silently empty declaration.
    scope_by_agent: Dict[str, List[dict]] = {}
    try:
        scope_by_agent = repo.get_scope_for_agents([r["id"] for r in rows])
    except Exception as e:
        logger.warning("agents: batched scope read failed for %s: %s", uid, e)
    out: List[Dict[str, Any]] = [
        _agent_out(
            r,
            uid=uid,
            scope_rows=scope_by_agent.get(r["id"], []) if scope_by_agent else None,
        )
        for r in rows
    ]
    return {"agents": out}


def _template_prefill(entity_id: str, user: dict) -> Dict[str, str]:
    """Read an Agent Template's markdown into builder fields.

    BEHAVIOUR ONLY, and that is the design rather than an omission. A template
    is portable between users and instances, so it cannot name a data package
    or memory domain — the id would mean something else, or nothing, wherever
    it lands. The template brings the role; the person brings their own data.
    ``knowledge``/``tables_mode``/``connections_mode`` are therefore never
    prefilled from one.

    Visibility is enforced with the store's own gate, so a template the caller
    cannot read 404s here exactly as it would on the store detail route.
    """
    from app.api.store import (
        _FRONTMATTER_RE,
        _enforce_visibility,
        _entity_dir,
        _parse_frontmatter,
    )
    from app.auth.dependencies import _get_db
    from src.repositories import store_entities_repo

    entity = store_entities_repo().get(entity_id)
    if not entity or entity.get("type") != "agent":
        raise HTTPException(status_code=404, detail="template_not_found")

    conn = next(_get_db())
    try:
        _enforce_visibility(entity, user, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    agents_dir = _entity_dir(entity_id) / "plugin" / "agents"
    docs = sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    if not docs:
        # The row exists but its content does not — a template with no body
        # would silently create a blank agent, which looks like the feature
        # failing rather than the template being empty.
        raise HTTPException(status_code=422, detail="template_has_no_content")

    text = docs[0].read_text(encoding="utf-8", errors="replace")
    fm = _parse_frontmatter(text) or {}
    body = _FRONTMATTER_RE.sub("", text, count=1).strip() if _FRONTMATTER_RE.match(text) else text.strip()

    out: Dict[str, str] = {"instructions": body}
    # `description` is the closest thing a template carries to a role. The
    # remaining fields are only prefilled when a template actually declares
    # them — most do not, and inventing a tone would be putting words in the
    # author's mouth.
    for field, key in (("role", "role"), ("tone", "tone"), ("greeting", "greeting")):
        val = (fm.get(key) or "").strip()
        if val:
            out[field] = val
    if "role" not in out:
        out["role"] = (entity.get("description") or "").strip()[:500]
    return out


@router.post("", status_code=201)
async def create_agent(payload: AgentCreate, user: dict = Depends(get_current_user)):
    """Create an agent owned by (and private to) the caller.

    An unnamed agent is legal — the builder creates the row first and the user
    names it as they go, so a blank name must not 422 the whole flow.

    ``template_entity_id`` starts the agent from a Library Agent Template. It
    only ever fills fields the caller left blank, so an explicit value in the
    payload still wins — the template is a starting point, not an override.
    """
    uid = user["id"]
    name = (payload.name or "").strip()
    slug = _unique_slug(_auto_slug(name or "agent"), uid)

    prefill: Dict[str, str] = {}
    if payload.template_entity_id:
        prefill = _template_prefill(payload.template_entity_id, user)

    def _field(key: str, given: Optional[str], fallback: str = "") -> str:
        """Caller's value, else the template's, else the existing default."""
        if (given or "").strip():
            return given
        return prefill.get(key) or fallback

    # Builder ids carry the ``agt_`` prefix (the redesign's convention, asserted
    # by the Library sharing tests) so a builder-authored agent is
    # distinguishable at a glance from the agent-as-API rows that main's
    # get_or_create_default seeds with a bare UUID.
    agent_id = "agt_" + uuid.uuid4().hex
    agents_repo().create(
        id=agent_id,
        owner_user_id=uid,
        name=name,
        slug=slug,
        system_prompt=_field("instructions", payload.instructions),
        role=_field("role", payload.role),
        tone=_field("tone", payload.tone, "concise"),
        greeting=_field("greeting", payload.greeting),
        # NOT prefilled from a template, ever — see _template_prefill.
        knowledge=json.dumps(payload.knowledge or []),
        plugins=json.dumps(payload.plugins or []),
        # A new agent is web-enabled by default (the builder's convention); an
        # explicit surfaces payload overrides it.
        surfaces=json.dumps(payload.surfaces if payload.surfaces is not None else {"web": True}),
        status=payload.status or "draft",
        # All four axes enforced from birth, matching /api/v1/agents. The
        # repo's own defaults are 'all' (correct only for the seeded default
        # agent), which made every builder agent a passthrough riding its
        # owner's whole stack. A blank new agent therefore starts with an
        # EMPTY enforced scope — fail-closed is the right shape for a row the
        # user has not yet put anything in, and each save widens it to exactly
        # what they picked. `connections_mode` is 'selected' with no rows
        # because the builder has no connections section: an axis the UI never
        # offers must not silently pass through.
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    _sync_builder_scope(agent_id, payload.knowledge or [], payload.plugins or [])
    logger.info("agent created id=%s slug=%s by=%s", agent_id, slug, user.get("email"))
    mark_journey(uid, agent_created=True)
    row = _live(agent_id)
    return _agent_out(row or {}, uid=uid)


@router.get("/{agent_id}")
async def get_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """One agent — owner, grantee, or admin."""
    return _agent_out(_readable(agent_id, user), uid=user["id"])


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    user: dict = Depends(get_current_user),
):
    """Patch an agent the caller owns. Only supplied fields change.

    Renaming a draft also re-derives its slug, so the address it answers
    on matches what it is called. Marking the agent ready freezes the
    slug — from then on it is an address other things may hold.
    """
    # Implementation of the slug rule above: `_draft_slug_rename`.
    before = _writable(agent_id, user)
    supplied = payload.model_dump(exclude_unset=True, exclude_none=True)
    # Map the builder's wire names onto the canonical columns.
    fields: Dict[str, Any] = {}
    for key, value in supplied.items():
        if key == "instructions":
            fields["system_prompt"] = value
        elif key in ("knowledge", "plugins", "surfaces"):
            fields[key] = json.dumps(value)
        else:
            fields[key] = value
    # The owner's id, not the caller's: `_writable` lets an admin PATCH
    # someone else's agent, and `_unique_slug` searches per owner. Scoped to
    # the admin it would call a slug free that the real owner already holds,
    # and the UPDATE would hit the (owner_user_id, slug) UNIQUE as a 500.
    new_slug = _draft_slug_rename(before, fields.get("name"), before.get("owner_user_id") or user["id"])
    if new_slug:
        fields["slug"] = new_slug
    # Editing the declaration re-derives the enforced scope, and forces the
    # axes it governs to 'selected' — a pre-fix row still sitting at 'all'
    # must not keep passing its owner's whole stack through just because the
    # migration has not run yet (an instance can be upgraded mid-session).
    # Both lists are needed: `set_scope` replaces the builder-owned set
    # wholesale, so the half the PATCH did not send is read off the row —
    # through :func:`_hydrate_builder_axes`, NOT off the raw JSON columns.
    # For an agent scoped through `agnes agent scope set` those columns are
    # empty while real `agent_scope` rows exist, so deriving the unsent axis
    # from them replaced it with nothing. The read fix made that worse rather
    # than better: `GET` now shows the true scope, so the screen the user acts
    # on looks correct right up until a partial save drops half of it
    # (Devin Review on #1520).
    rescope = "knowledge" in supplied or "plugins" in supplied
    if rescope:
        fields.setdefault("plugins_mode", "selected")
        fields.setdefault("connections_mode", "selected")
        fields.setdefault("tables_mode", "selected")
        fields.setdefault("memory_mode", "selected")
    if fields:
        agents_repo().update(agent_id, **fields)
    if rescope:
        # Hydrated BEFORE the rewrite — `_sync_builder_scope` is what replaces
        # the rows this reads.
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
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return _agent_out(row, uid=user["id"])


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """Soft-delete an agent the caller owns, and drop its grants.

    Grants are removed too, so a later agent can never inherit a dangling
    grant through id reuse and /admin/access shows no orphan rows.

    The seeded default agent is exempt: it is infrastructure every web chat
    session is attributed to (``app/api/chat.py::_default_agent_id``), not a
    user artifact, so deleting it would make the agent vanish from the Library
    and silently reappear on the next chat. `/api/v1/agents` refuses this for
    the same reason (``agents_admin.py::delete_agent``).
    """
    row = _writable(agent_id, user)
    if row.get("is_default"):
        raise HTTPException(status_code=400, detail="default_agent_undeletable")
    agents_repo().soft_delete(agent_id)
    try:
        resource_grants_repo().delete_by_resource(_RT, agent_id)
    except Exception as e:
        logger.warning("agents: grant cleanup failed for %s: %s", agent_id, e)
    logger.info("agent deleted id=%s by=%s", agent_id, user.get("email"))
    return None
