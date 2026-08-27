"""Resource types that can be granted to user groups.

A *resource type* identifies a class of entity admins can hand out access to
(e.g. marketplace plugins, datasets). Concrete instances live in their own
domain tables (`marketplace_plugins`, `table_registry`, …); access to a
specific instance is recorded as a row in `resource_grants` with this enum
value as ``resource_type`` and a module-defined path string as ``resource_id``.

Adding a new type — single place, no separate wiring step:

  1. Add a member to :class:`ResourceType`.
  2. Write a ``list_blocks() -> list[Block]`` delegate that reads through the
     ``src.repositories`` factory and projects the domain tables into the
     (block → items) tree the admin /access page consumes. Each item must
     include ``resource_id`` matching the path string used in
     ``resource_grants.resource_id``.
  3. Register a :class:`ResourceTypeSpec` in :data:`RESOURCE_TYPES`. The
     dataclass requires ``list_blocks`` — the type checker forces step 2.
  4. Wire endpoints with
     ``Depends(require_resource_access(ResourceType.X, "<path>"))``.

No DB migration needed — this is application-level configuration. Membership
in the enum + registry is the source of truth; the DB just stores the string
value verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, List


class ResourceType(StrEnum):
    """Resource categories that the access-control layer understands.

    Values are persisted verbatim in ``resource_grants.resource_type``.
    Renaming a member is a breaking change — existing grants reference the
    string. Add a new member and migrate via SQL UPDATE if needed.
    """

    MARKETPLACE_PLUGIN = "marketplace_plugin"
    TABLE = "table"
    DATA_PACKAGE = "data_package"
    SEMANTIC_MODEL = "semantic_model"
    MEMORY_DOMAIN = "memory_domain"
    MEMORY_ITEM = "memory_item"
    RECIPE = "recipe"
    CHAT = "chat"
    SLACK_CHANNEL = "slack_channel"
    COLLECTION = "collection"
    KNOWLEDGE_DIGEST = "knowledge_digest"
    DATA_APP = "data_app"
    AGENT = "agent"
    CORPUS_FILE = "corpus_file"
    STORE_ENTITY = "store_entity"


# Shape returned by ``list_blocks`` delegates. Kept as plain ``dict`` to keep
# the registry decoupled from any specific ORM/repo type — UI consumes JSON.
Block = dict[str, Any]
# Backend-agnostic: delegates read through the ``src.repositories`` factory
# (which honors ``use_pg()``), NOT a raw system-DB connection. Passing a
# DuckDB connection here was the backend-split bug (#518) — on a Postgres
# instance it read the stale, frozen DuckDB system file.
ListBlocksFn = Callable[[], List[Block]]

# The /admin/access projection must list EVERY grantable resource — an admin
# can't grant access to something the page doesn't render. The repo ``list()``
# helpers default to a paginated 200; for these admin-curated, low-cardinality
# entity types we pass an effectively-unbounded cap so nothing is silently
# hidden (table_registry / marketplace use their unbounded ``list_all()``).
_GRANT_PROJECTION_LIMIT = 100_000


@dataclass(frozen=True)
class ResourceTypeSpec:
    """Self-contained definition of a resource type.

    Bundles UI copy with the projection delegate so that adding a new type
    in :data:`RESOURCE_TYPES` is the single place that needs editing — no
    forgotten branch in ``access-overview`` or the admin UI.

    Attributes:
        key: The enum member; ``key.value`` is what gets persisted.
        display_name: Plural label rendered as a section header on the
            admin /access page.
        description: One-liner shown in the create-grant form's helper text.
        id_format: Human-readable hint for ``resource_id`` shape — e.g.
            ``"<marketplace_slug>/<plugin_name>"``. Surfaced as input
            placeholder.
        list_blocks: Delegate (no args) that reads through the
            ``src.repositories`` factory and returns
            ``[{id, name, items: [{resource_id, name, ...}]}]`` — one block
            per parent entity (e.g. marketplace), one item per grantable
            resource (e.g. plugin). Items must carry ``resource_id`` that
            matches the path string written into ``resource_grants``.
    """

    key: ResourceType
    display_name: str
    description: str
    id_format: str
    list_blocks: ListBlocksFn


# ---------------------------------------------------------------------------
# Marketplace plugin projection
# ---------------------------------------------------------------------------


def _marketplace_plugin_blocks() -> List[Block]:
    """Project marketplace_registry + marketplace_plugins into the
    hierarchical (block → items) shape the admin UI renders.

    One block per marketplace_registry row. Items inside are plugins;
    ``resource_id`` encodes the canonical path ``<marketplace_slug>/<plugin_name>``
    that ``resource_grants.resource_id`` matches against.

    Reads through the repository factory so a Postgres instance projects the
    live PG registry, not the frozen DuckDB system file (#518). The old raw
    LEFT JOIN is reproduced by grouping plugins under their marketplace in
    Python.
    """
    from src.repositories import (
        marketplace_plugins_repo,
        marketplace_registry_repo,
    )

    blocks: dict[str, Block] = {}
    for mr in marketplace_registry_repo().list_all():
        blocks[mr["id"]] = {"id": mr["id"], "name": mr["name"], "items": []}
    for p in marketplace_plugins_repo().list_all():
        # Admin-disabled plugins are hidden from the RBAC grant UI, like every
        # other served surface (browse / my-stack / synthetic feed). The only
        # place a disabled plugin stays visible is the /admin/marketplaces
        # details modal, where an admin can re-enable it.
        if p.get("admin_disabled"):
            continue
        block = blocks.get(p.get("marketplace_id"))
        if block is None:
            continue
        block["items"].append(
            {
                "resource_id": f"{p['marketplace_id']}/{p['name']}",
                "name": p["name"],
                "version": p.get("version"),
                "category": p.get("category"),
                "description": p.get("description"),
                "source_type": p.get("source_type"),
                # v39: drives the SYSTEM pill + disabled checkbox in
                # /admin/access. The grant row exists for every group on a
                # system plugin (materialized by mark_system) — we just
                # prevent admins from revoking it via the UI to keep the
                # mandatory-tier semantic honest.
                "is_system": bool(p.get("is_system")),
            }
        )
    return list(blocks.values())


# ---------------------------------------------------------------------------
# Table projection
# ---------------------------------------------------------------------------


def _table_blocks() -> List[Block]:
    """Project table_registry into the (block → items) shape the admin UI
    renders.

    One block per ``bucket`` value, ordered by bucket then table name.
    Items inside are tables; ``resource_id`` is the ``table_registry.id``
    primary key — that is the path string that ``resource_grants.resource_id``
    matches against. Bucket is purely a UI grouping and does not enter the
    resource_id (mirrors the marketplace/plugin pattern).

    Tables with NULL/empty bucket fall into a synthetic ``"(no bucket)"``
    block so they are still grantable.
    """
    from src.repositories import table_registry_repo

    # Filter out source_type='internal' rows (agnes_sessions /
    # agnes_telemetry / agnes_audit). Their RBAC is row-level, enforced
    # in the query path; the table-grain `resource_grants` gate is
    # bypassed for them (see can_access). Surfacing them on
    # /admin/access would let admins assign grants that do nothing,
    # which is exactly the confusion this filter prevents.
    # ``!= 'internal'`` keeps NULL source_type rows (matches the old
    # ``IS DISTINCT FROM 'internal'`` SQL semantics).
    rows = [r for r in table_registry_repo().list_all() if r.get("source_type") != "internal"]
    rows.sort(key=lambda r: ((r.get("bucket") or ""), r.get("name") or ""))
    blocks: dict[str, Block] = {}
    for r in rows:
        block_key = r.get("bucket") or "(no bucket)"
        block = blocks.setdefault(
            block_key,
            {
                "id": block_key,
                "name": block_key,
                "items": [],
            },
        )
        block["items"].append(
            {
                "resource_id": r["id"],
                "name": r.get("name"),
                "category": r.get("query_mode"),
                "source_type": r.get("source_type"),
                "description": r.get("description"),
            }
        )
    return list(blocks.values())


# ---------------------------------------------------------------------------
# Data package projection
# ---------------------------------------------------------------------------


def _data_package_blocks() -> List[Block]:
    """Project ``data_packages`` into the (block → items) shape rendered by
    the admin /access page.

    Data packages are admin-curated bundles of tables (v49) and form the
    grantable unit on the /catalog Browse + Stack views. One synthetic
    block ``"Data packages"`` holds them; ``resource_id`` is
    ``data_packages.id``.
    """
    from src.repositories import data_packages_repo

    rows = data_packages_repo().list(limit=_GRANT_PROJECTION_LIMIT)  # all live rows; see _GRANT_PROJECTION_LIMIT
    if not rows:
        return []
    return [
        {
            "id": "data_packages",
            "name": "Data packages",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "category": "data_package",
                    "description": r.get("description"),
                    "icon": r.get("icon"),
                    "color": r.get("color"),
                    "slug": r.get("slug"),
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Semantic model projection
# ---------------------------------------------------------------------------


def _semantic_model_blocks() -> List[Block]:
    """Project ``semantic_models`` into the (block → items) shape rendered
    by the admin /access page. ``resource_id`` is ``semantic_models.id``.

    This registers ``SEMANTIC_MODEL`` as a directly grantable resource type,
    following the ``DATA_PACKAGE`` entry above, and the export/search RBAC
    gate reads it: ``_can_access_semantic_model``
    (``app/api/semantic_models.py``) checks a direct
    ``resource_grants(SEMANTIC_MODEL, ...)`` row first and falls back to the
    linked-package grant via ``list_packages_for_model`` — the same way
    ``can_access_table`` layers per-table grants under the data-package
    stack. So a grant created here through /admin/access does take effect.

    Pinned by ``test_export_succeeds_via_a_direct_model_grant`` in
    ``tests/test_semantic_models_api.py``, whose own docstring records why
    the guard is worth having: a control that is offered but never read is
    worse than one that is absent.
    """
    from src.repositories import semantic_model_repo

    rows = semantic_model_repo().list_all()
    if not rows:
        return []
    return [
        {
            "id": "semantic_models",
            "name": "Semantic models",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "category": "semantic_model",
                    "description": r.get("description"),
                    "slug": r.get("slug"),
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Data app projection
# ---------------------------------------------------------------------------


def _data_app_blocks() -> List[Block]:
    """Project ``data_apps`` into grant-picker blocks (resource_id = slug)."""
    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    rows = repo.list(include_drafts=False, limit=_GRANT_PROJECTION_LIMIT)
    # `linked_hidden` = a linked app gone upstream (soft-deleted; row + grants
    # kept for lossless re-link). Every read surface 404s/omits it — the grant
    # picker must not offer apps that cannot be seen anywhere. Show the
    # admin-pinned description override where present, like the app pages do.
    rows = [r for r in rows if r.get("state") != "linked_hidden"]
    if not rows:
        return []
    return [
        {
            "id": "data_apps",
            "name": "Data apps",
            "items": [
                {
                    "resource_id": r["slug"],
                    "name": r["name"],
                    "category": "data_app",
                    "description": repo.effective_description(r),
                    "slug": r["slug"],
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Memory domain projection
# ---------------------------------------------------------------------------


def _memory_domain_blocks() -> List[Block]:
    """Project ``memory_domains`` rows into the (block → items) shape the
    admin /access page renders.

    Pre-v49 the domain set was a fixed hardcoded enum mirroring
    ``VALID_DOMAINS`` in ``app/api/memory.py``. v49 replaces the scalar
    ``knowledge_items.domain`` column with a junction onto a row-backed
    ``memory_domains`` table — admins can now CRUD domains. ``resource_id``
    is the ``memory_domains.id`` (e.g. ``md_finance``), no longer the slug.
    """
    from src.repositories import memory_domains_repo

    rows = memory_domains_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    if not rows:
        return []
    return [
        {
            "id": "memory_domains",
            "name": "Memory domains",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "category": "memory_domain",
                    "description": r.get("description"),
                    "icon": r.get("icon"),
                    "color": r.get("color"),
                    "slug": r.get("slug"),
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Memory item projection — per-group item-level Required override (v49)
# ---------------------------------------------------------------------------


def _memory_item_blocks() -> List[Block]:
    """Project ``knowledge_items`` into the (block → items) shape for the
    rare per-item grant override.

    Default Required tier for a memory item is driven by
    ``knowledge_items.is_required`` (the global flag). This resource type
    exists for the per-group override: a group can be granted MEMORY_ITEM
    on a specific item with ``requirement='required'`` (force-include) or
    ``'available'`` (force-exclude — counter-acts the global flag for that
    group). Surfaced as a flat list of approved or pending non-personal
    items so admins can pick.
    """
    from src.repositories import knowledge_repo

    rows = knowledge_repo().list_items(
        statuses=["approved", "pending"],
        exclude_personal=True,
        limit=1000,
    )
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r.get("title") or r["id"])
    return [
        {
            "id": "memory_items",
            "name": "Memory items",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r.get("title") or r["id"],
                    "category": "memory_item",
                    "description": None,
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Recipe projection — admin-curated query templates (v53 RBAC, v55)
# ---------------------------------------------------------------------------


def _recipe_blocks() -> List[Block]:
    """Project ``recipes`` rows into the (block → items) shape rendered by
    the admin /access page.

    Recipes are SQL templates admins curate (v53). Default tier is
    ``available``: with no grants the recipe is invisible to non-admins.
    Soft-deleted rows (``deleted_at IS NOT NULL``) are filtered out so the
    admin grant UI doesn't accidentally hand out access to rows the
    Recipes tab can no longer show. ``resource_id`` is the
    ``recipes.id`` (e.g. ``rec_top_revenue``).
    """
    from src.repositories import recipes_repo

    rows = recipes_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    if not rows:
        return []
    return [
        {
            "id": "recipes",
            "name": "Recipes",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["title"],
                    "category": "recipe",
                    "description": r.get("description"),
                    "icon": r.get("icon"),
                    "color": r.get("color"),
                    "slug": r.get("slug"),
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Cloud-chat projection
# ---------------------------------------------------------------------------


def _chat_blocks() -> List[Block]:
    """Singleton feature resource: one grantable item gating the whole
    cloud-chat surface (web ``/chat`` + the Slack DM bot).

    No DB entity backs it — the chat feature is a single toggle — so the
    block is static. With no grant the feature is denied to everyone except
    the ``Admin`` god-mode group; admins grant ``(group, chat, chat)`` on
    /admin/access to turn it on for a group.
    """
    return [
        {
            "id": "cloud_chat",
            "name": "Cloud chat",
            "items": [
                {
                    "resource_id": "chat",
                    "name": "Cloud chat",
                    "description": ("Access to the cloud-hosted Claude chat (the /chat web UI and the Slack DM bot)."),
                }
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Slack channel allowlist projection
# ---------------------------------------------------------------------------


def _slack_channel_blocks() -> List[Block]:
    """Project the per-channel mention allowlist.

    There is **no domain table** — the ``resource_grants`` rows themselves are
    the allowlist. An admin enables Agnes in a channel by pasting its channel
    id (e.g. ``C0123ABCD``) into the create-grant form on /admin/access; that
    writes ``(Everyone, slack_channel, <channel_id>)``. We project the distinct
    granted channel ids so the admin UI can list what is currently enabled.
    Scoped to the ``Everyone`` group to mirror enforcement
    (``is_channel_allowlisted`` only honors Everyone) — an accidental grant to
    another group does not falsely show a channel as enabled. Empty allowlist
    → no block (default-deny).
    """
    from src.repositories import resource_grants_repo, user_groups_repo

    everyone = user_groups_repo().get_by_name("Everyone")
    if not everyone:
        return []
    grants = resource_grants_repo().list_all(resource_type="slack_channel", group_id=everyone["id"])
    channel_ids = sorted({g["resource_id"] for g in grants})
    if not channel_ids:
        return []
    return [
        {
            "id": "slack_channels",
            "name": "Slack channels",
            "items": [
                {
                    "resource_id": cid,
                    "name": cid,
                    "category": "slack_channel",
                    "description": "Channel where @agnes mentions are answered.",
                }
                for cid in channel_ids
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Collection projection (v77)
# ---------------------------------------------------------------------------


def _corpus_file_blocks() -> List[Block]:
    """Project ``corpus_files`` into the (block → items) shape the admin
    /access page renders — one block per parent collection.

    A file inside a multi-file collection is independently shareable (the
    Library lets its owner share one file without sharing the whole folder), so
    its grants need to be visible and correctable here. Single-file artefacts
    are NOT listed: they are shared as their collection, which is the same
    thing, and listing them twice would invite contradictory grants.
    """
    from src.repositories import corpus_files_repo, file_corpora_repo

    blocks: List[Block] = []
    cf_repo = corpus_files_repo()
    for col in file_corpora_repo().list(limit=_GRANT_PROJECTION_LIMIT):
        try:
            files = cf_repo.list_for_corpus(col["id"])
        except Exception:
            continue
        if len(files) < 2:
            continue  # a lone file IS its collection
        blocks.append(
            {
                "id": col["id"],
                "name": col.get("name") or col.get("slug"),
                "items": [
                    {
                        "resource_id": f["id"],
                        "name": f.get("filename") or f["id"],
                        "slug": None,
                        "description": None,
                    }
                    for f in files
                ],
            }
        )
    return blocks


def _agent_blocks() -> List[Block]:
    """Project ``agents`` into the (block → items) shape rendered by the
    admin /access page.

    Agents are assistants a user composes in the Agent builder (v103). One
    synthetic block ``"Agents"`` holds all live (non-soft-deleted) agents; the
    ``resource_id`` is ``agents.id``. Unlike most resource types, agent grants
    are usually written by the OWNER through the Library's Share action rather
    than by an admin here — this projection exists so an admin can still see
    and correct them.
    """
    from src.repositories import agents_repo

    rows = agents_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    if not rows:
        return []
    return [
        {
            "id": "agents",
            "name": "Agents",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "slug": r.get("slug"),
                    "description": r.get("role") or r.get("instructions") or "",
                }
                for r in rows
            ],
        }
    ]


def _store_entity_blocks() -> List[Block]:
    """Project ``store_entities`` into the (block → items) shape rendered by
    the admin /access page.

    One block per entity type (Skills / Agents / Plugins) so the page groups the
    way the Library does; ``resource_id`` is ``store_entities.id``.

    The grant that matters most here is the ``requirement='required'`` tier —
    "In stack, locked". It is admissible ONLY on an organization-published
    entity (see ``app/api/access.py``), so each item carries ``publisher_kind``
    and the UI can disable the Required control on user-published rows rather
    than letting an admin write a grant the API will refuse.

    Archived rows are excluded: they are soft-deleted and must not appear as
    something an admin can newly hand out.
    """
    from src.repositories import store_entities_repo

    rows, _ = store_entities_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    blocks: dict[str, Block] = {}
    # Display names only — `type` stays 'agent' on the wire and in the DB.
    labels = {"skill": "Skills", "agent": "Agent Templates", "plugin": "Plugins"}
    for r in rows:
        if r.get("visibility_status") == "archived":
            continue
        kind = r.get("type") or "skill"
        block = blocks.setdefault(
            kind,
            {"id": f"store_{kind}", "name": labels.get(kind, kind.title()), "items": []},
        )
        block["items"].append(
            {
                "resource_id": r["id"],
                "name": r.get("title") or r["name"],
                "description": r.get("tagline") or r.get("description") or "",
                "publisher_kind": r.get("publisher_kind") or "user",
                "verification_state": r.get("verification_state") or "none",
                # Only organization-published items may be made Required.
                "can_require": (r.get("publisher_kind") or "user") == "organization",
            }
        )
    return [blocks[k] for k in ("skill", "agent", "plugin") if k in blocks]


def _collection_blocks() -> List[Block]:
    """Project ``file_corpora`` into the (block → items) shape rendered by
    the admin /access page.

    Collections are bring-your-files containers (v77). One synthetic block
    ``"Collections"`` holds all live (non-soft-deleted) corpora; the
    ``resource_id`` is ``file_corpora.id``.
    """
    from src.repositories import file_corpora_repo

    rows = file_corpora_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    if not rows:
        return []
    return [
        {
            "id": "collections",
            "name": "Collections",
            "items": [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "slug": r.get("slug"),
                    "description": r.get("description"),
                }
                for r in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Maintained-digest projection (K4, #799)
# ---------------------------------------------------------------------------


def _knowledge_digest_blocks() -> List[Block]:
    """Project ``knowledge_digests`` into the (block → items) shape rendered
    by the admin /access page.

    Single synthetic block ``"Maintained digests"``; one item per digest.
    ``resource_id`` is the digest id — the exact string the manifest
    section builder and the content endpoint check against via
    ``can_access``/``can_access_session``.
    """
    from src.repositories import knowledge_digests_repo

    rows = knowledge_digests_repo().list()
    if not rows:
        return []
    return [
        {
            "id": "knowledge_digests",
            "name": "Maintained digests",
            "items": [
                {
                    "resource_id": d["id"],
                    "name": d["title"],
                    "category": "knowledge_digest",
                    "slug": d.get("slug"),
                    "status": d.get("status"),
                }
                for d in rows
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Registry — the one place that gets edited when adding a new resource type
# ---------------------------------------------------------------------------


RESOURCE_TYPES: dict[ResourceType, ResourceTypeSpec] = {
    ResourceType.MARKETPLACE_PLUGIN: ResourceTypeSpec(
        key=ResourceType.MARKETPLACE_PLUGIN,
        display_name="Marketplace plugins",
        description="A plugin from a registered marketplace.",
        id_format="<marketplace_slug>/<plugin_name>",
        list_blocks=_marketplace_plugin_blocks,
    ),
    ResourceType.TABLE: ResourceTypeSpec(
        key=ResourceType.TABLE,
        display_name="Tables",
        description=(
            "Does NOT grant analyst visibility — the unified-stack design routes "
            "all analyst table access through Data Packages instead (grant the "
            "package, not the table). This grant only sets the ceiling agent "
            "scoping (`tables_mode='selected'`) and co-session grant intersection "
            "narrow against; a table absent here can never appear in a scoped "
            "agent's or co-session's effective table set, regardless of package "
            "membership."
        ),
        id_format="<table_id>",
        list_blocks=_table_blocks,
    ),
    ResourceType.DATA_PACKAGE: ResourceTypeSpec(
        key=ResourceType.DATA_PACKAGE,
        display_name="Data packages",
        description="An admin-curated bundle of data tables.",
        id_format="<package_id>",
        list_blocks=_data_package_blocks,
    ),
    ResourceType.SEMANTIC_MODEL: ResourceTypeSpec(
        key=ResourceType.SEMANTIC_MODEL,
        display_name="Semantic models",
        description="A semantic model — datasets, fields, relationships and metrics.",
        id_format="<model_id>",
        list_blocks=_semantic_model_blocks,
    ),
    ResourceType.MEMORY_DOMAIN: ResourceTypeSpec(
        key=ResourceType.MEMORY_DOMAIN,
        display_name="Memory domains",
        description=(
            "A corporate-memory domain. A grant is ADDITIVE — it makes the "
            "domain's items visible to the granted group; it does NOT hide them "
            "from anyone else. Visibility is restricted only by an item's "
            "`audience` (an item with the default audience is visible to every "
            "authenticated user regardless of which domain it belongs to). To "
            "keep sensitive corporate-memory items private, set their audience "
            "to the intended group — granting a domain alone does not restrict."
        ),
        id_format="<memory_domain_id>",
        list_blocks=_memory_domain_blocks,
    ),
    ResourceType.MEMORY_ITEM: ResourceTypeSpec(
        key=ResourceType.MEMORY_ITEM,
        display_name="Memory items",
        description=(
            "Per-group override of an individual knowledge item's Required "
            "flag — rare path; the global flag covers the common case."
        ),
        id_format="<knowledge_item_id>",
        list_blocks=_memory_item_blocks,
    ),
    ResourceType.RECIPE: ResourceTypeSpec(
        key=ResourceType.RECIPE,
        display_name="Recipes",
        description=(
            "An admin-curated SQL recipe analysts copy + adapt. With no "
            "grant the recipe is hidden from non-admin viewers."
        ),
        id_format="<recipe_id>",
        list_blocks=_recipe_blocks,
    ),
    ResourceType.CHAT: ResourceTypeSpec(
        key=ResourceType.CHAT,
        display_name="Cloud chat",
        description=(
            "The cloud-hosted Claude chat feature. With no grant it is "
            "denied to everyone (except admins); grant it to a group to "
            "turn it on for that group."
        ),
        id_format="chat",
        list_blocks=_chat_blocks,
    ),
    ResourceType.SLACK_CHANNEL: ResourceTypeSpec(
        key=ResourceType.SLACK_CHANNEL,
        display_name="Slack channels",
        description=(
            "A Slack channel where @agnes mentions are answered. Grant "
            "(Everyone, slack_channel, <channel_id>) to enable Agnes there; "
            "with no grant the channel is silent (default-deny)."
        ),
        id_format="<channel_id>",
        list_blocks=_slack_channel_blocks,
    ),
    ResourceType.COLLECTION: ResourceTypeSpec(
        key=ResourceType.COLLECTION,
        display_name="Collections",
        description=(
            "A user-uploaded file collection. Grant a group access to a "
            "collection so its members can query the ingested documents."
        ),
        id_format="<corpus_id>",
        list_blocks=_collection_blocks,
    ),
    ResourceType.KNOWLEDGE_DIGEST: ResourceTypeSpec(
        key=ResourceType.KNOWLEDGE_DIGEST,
        display_name="Maintained digests",
        description=(
            "An admin-defined digest document regenerated from its source "
            "collections. Grant a group access so `agnes pull` delivers it "
            "to members as .claude/rules/ka_<slug>.md."
        ),
        id_format="<digest_id>",
        list_blocks=_knowledge_digest_blocks,
    ),
    ResourceType.DATA_APP: ResourceTypeSpec(
        key=ResourceType.DATA_APP,
        display_name="Data apps",
        description="A hosted user web application served behind instance auth.",
        id_format="<slug>",
        list_blocks=_data_app_blocks,
    ),
    ResourceType.CORPUS_FILE: ResourceTypeSpec(
        key=ResourceType.CORPUS_FILE,
        display_name="Files in collections",
        description=(
            "A single file inside a multi-file collection. Grant a group access "
            "to share ONE file without sharing its whole collection. Owners "
            "normally write these grants themselves from the Library."
        ),
        id_format="<corpus_file_id>",
        list_blocks=_corpus_file_blocks,
    ),
    ResourceType.AGENT: ResourceTypeSpec(
        key=ResourceType.AGENT,
        display_name="Agents",
        description=(
            "An assistant composed in the Agent builder. Grant a group access "
            "so its members can use the agent. Owners normally write these "
            "grants themselves via the Library's Share action."
        ),
        id_format="<agent_id>",
        list_blocks=_agent_blocks,
    ),
    ResourceType.STORE_ENTITY: ResourceTypeSpec(
        key=ResourceType.STORE_ENTITY,
        display_name="Skills, agents & plugins",
        description=(
            "An item authored in this instance and published to the Library. "
            "Grant a group access to it, or mark it Required so its members "
            "always have it in their stack — Required is admissible only on "
            "organization-published items."
        ),
        id_format="<store_entity_id>",
        list_blocks=_store_entity_blocks,
    ),
}


def is_resource_type_enabled(rt: ResourceType) -> bool:
    """Whether a resource type is exposed to the admin UI + grant API.

    All resource types are unconditionally enabled in v19. The
    ``AGNES_ENABLE_TABLE_GRANTS`` env-gate that previously held back
    ``ResourceType.TABLE`` was removed when ``can_access_table`` was
    rewired onto ``app.auth.access.can_access``.
    """
    return True


def enabled_resource_types() -> list[ResourceTypeSpec]:
    """The subset of RESOURCE_TYPES currently surfaced to admins."""
    return [spec for rt, spec in RESOURCE_TYPES.items() if is_resource_type_enabled(rt)]


def list_resource_types() -> list[dict[str, str]]:
    """Flat projection for /api/admin/resource-types.

    Shape: ``[{key, display_name, description, id_format}]``. The
    ``list_blocks`` delegate is intentionally omitted — the UI consumes
    blocks via ``/api/admin/access-overview`` instead.
    """
    return [
        {
            "key": spec.key.value,
            "display_name": spec.display_name,
            "description": spec.description,
            "id_format": spec.id_format,
        }
        for spec in enabled_resource_types()
    ]
