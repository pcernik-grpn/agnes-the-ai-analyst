"""Shared helpers behind the `/agents` builder's declaration -> enforced
scope mapping.

Originally lived in `app/api/agents.py`, the FastAPI router that served the
builder's own `/api/agents*` CRUD surface. Task C1.1 (remediation-program
Track C1, "one agent model") folded that surface's request/response shape
into `/api/v1/agents*` (`app/api/agents_admin.py`); Task C1.2 then deleted
the now-redundant router outright (LD3: no deprecation window — see
`docs/superpowers/plans/2026-08-26-one-agent-model.md`). These helpers moved
here, unchanged, because `agents_admin.py` still needs them and a router
module is the wrong home for functions with no route of their own.

The builder's `knowledge` / `plugins` columns are the UI's declaration;
`agent_scope` + the four `*_mode` columns are what the runtime actually
enforces (`src/agent_scope_intersection.py`). `_sync_builder_scope` keeps the
two in agreement on every create/update through either surface, so the JSON
columns stay authoritative for what the page renders while never drifting
from what the runtime honours.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.resource_types import ResourceType
from src.repositories import agents_repo, resource_grants_repo

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
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
    literal ``agent`` placeholder, since that is what a blank-name create
    derives — the placeholder is not a special case, it is just the slug of
    a nameless agent.
    """
    base = _auto_slug((name or "").strip() if isinstance(name, str) else "")
    return slug == base or re.fullmatch(rf"{re.escape(base)}-\d+", slug) is not None


def _draft_slug_rename(before: Dict[str, Any], new_name: Any, owner_user_id: str) -> Optional[str]:
    """The slug to move to when renaming a draft, else ``None``.

    The builder creates the row on "New agent", before the user types a
    name, so a blank-name create falls back to the slug ``agent`` (then
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
    the runtime intersects every declared id with the granter's live grants
    (`resolve_agent_authority` / `compute_agent_intersection`), so an id the
    granter cannot reach is inert rather than dangerous, and refusing it here
    would 422 a builder save for a package whose grant is merely being
    reorganized.

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
        return set(resource_grants_repo().list_resource_ids_for_user(user_id, ResourceType.AGENT.value))
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
    and never touch the JSON columns; the builder-shape create/update writes
    both. The same agent is listed either way (``list_for_user`` returns
    every owned agent), so a governance-created agent rendered "0 sources ·
    0 tools" while holding real scope — and the first declaration edit wiped
    all four builder-owned axes through :func:`_sync_builder_scope`, with the
    user never having seen what they were destroying.

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
        except Exception as e:  # a scope read must never 500 the caller
            logger.warning("agents: could not hydrate builder axes for %s: %s", agent_id, e)
            return knowledge, plugins
    if not knowledge:
        knowledge = [r["item_id"] for r in rows if r.get("item_type") in _KNOWLEDGE_ITEM_TYPES]
    if not plugins:
        plugins = [r["item_id"] for r in rows if r.get("item_type") == "plugin"]
    return knowledge, plugins


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
