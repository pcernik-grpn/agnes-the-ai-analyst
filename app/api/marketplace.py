"""Unified Marketplace API — backs the ``/marketplace`` browse page.

Combines two upstream sources into a single browse + detail surface:

  * **curated**  — admin-registered git marketplaces, RBAC-scoped via
    ``resource_grants`` (resource_type='marketplace_plugin').
  * **flea**     — community Store uploads (``store_entities``).

Per-tab listing endpoints (``/items``, ``/categories``) plus curated-side
detail and explicit-install (Model B) endpoints. Flea-side install/uninstall
keeps using the existing ``/api/store/entities/{id}/install`` POST/DELETE
endpoints — no need to duplicate.

Curated detail / install endpoints are gated by
``require_resource_access(MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}")``
so a user without the RBAC grant gets a 403 even on direct URL hit.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.access import (
    _user_group_ids,
    is_user_admin,
    require_resource_access,
)

from src.repositories import (
    audit_repo,
    marketplace_plugins_repo,
    marketplace_registry_repo,
    reports_repo,
    store_entities_repo,
    store_submissions_repo,
    user_curated_subscriptions_repo,
    user_store_installs_repo,
    users_repo,
)
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from app.services.journey import mark_journey
from app.utils import get_marketplace_cache_dir, get_marketplaces_dir
from src.marketplace import is_safe_plugin_name
from src.marketplace_filter import (
    _contained_plugin_dir,
    _resolve_raw,
    is_unserved_path,
    required_plugin_keys,
    resolve_allowed_plugins,
    resolve_manifest_name,
)
from src.marketplace_listing import _FRONTMATTER_RE, _parse_frontmatter
from src.marketplace_urls import internal_asset_url, mirrored_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

OWNER_TODO_PLACEHOLDER = "owner_todo"
"""Placeholder displayed in the UI when a curated plugin has no owner / curator
metadata. To be replaced once ``marketplace_plugins.curator_owner`` lands."""

# Visibility states an admin sees on the Browse shelf. Everything except
# ``archived`` — which is the soft-delete state written by
# ``store_entities.archive`` when an owner runs `agnes store delete` or hits
# Delete in the UI.
#
# The admin path used to pass no visibility filter at all, so every entity any
# user had ever deleted stayed on the admin's shelf — carrying the "Quarantined"
# badge, which means "blocked / under review", not "deleted" — and counted
# toward the Browse tab's total. Deleted items are not part of the shelf for
# anyone; an admin who needs them has the Archived tab on
# ``/admin/store/submissions``.
ADMIN_BROWSE_VISIBILITY = ["pending", "approved", "hidden"]

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class MarketplaceItem(BaseModel):
    id: str
    source: Literal["curated", "flea"]
    name: str
    type: Literal["skill", "agent", "plugin"]
    category: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    version: Optional[str] = None
    photo_url: Optional[str] = None
    added: Optional[str] = None
    installed: bool = False
    marketplace_slug: Optional[str] = None
    marketplace_name: Optional[str] = None
    detail_url: str
    # v32+ quarantine UX: surface the card's visibility + an
    # is_viewer_owner flag so the listing template can render an
    # "Under review" / "Quarantined" corner badge on the submitter's
    # own non-approved cards. Approved cards omit both fields.
    visibility_status: Optional[str] = None
    is_viewer_owner: bool = False
    # v39: drives the "Required" pill on the curated browse cards. Only
    # set on curated items (flea/store entities are never system).
    is_system: bool = False
    # Group-scoped Required tier: TRUE when any of the caller's groups
    # holds a ``requirement='required'`` grant for this plugin. Renders
    # the same locked "Required" state as ``is_system`` but only for the
    # granted groups' members, not globally.
    is_required: bool = False
    # Rich-content fields from marketplace-metadata.json (plugin-level only
    # for now; skill/agent rich content lands in a later phase). Frontend
    # falls back to `name` / `description` when these are missing, so the
    # card renders identically to pre-enrichment behaviour for any plugin
    # whose curator hasn't filled the new fields yet.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    # telemetry (v46): populated from usage_marketplace_item_window. Both
    # windows are surfaced so the listing can drive different sections
    # off different horizons (Most Popular → 7d, detail headline → 30d).
    invocations_30d: int = 0
    distinct_users_30d: int = 0
    invocations_7d: int = 0
    distinct_users_7d: int = 0
    trend_pct: Optional[float] = None
    # v104 trust line — one vocabulary across BOTH sources, which is what lets
    # the Curated / Flea shelves collapse into a single Publisher facet.
    #   * curated plugins are organization-published by construction: an admin
    #     registered the marketplace, and that act IS the institutional claim.
    #     Their upstream `author_name` is the AUTHOR, not the publisher — showing
    #     it as the publisher would imply someone in this instance vouched.
    #   * authored (flea) entities carry their stored publisher_kind.
    # `verification_state` is advisory and only ever meaningful on
    # user-published items; nothing here gates access.
    publisher_kind: str = "user"
    publisher_name: Optional[str] = None
    verification_state: str = "none"
    # stack_count = how many users have this item in their stack.
    # - Curated: COUNT(*) on user_plugin_optouts (post-v28 PRESENCE = subscribed).
    #   System pluginy are fanned out to every user via
    #   fanout_system_for_user, so the COUNT naturally includes them too.
    # - Flea: store_entities.install_count (bumped on /install).
    # Frontend renders this alongside active_users_30d as a funnel:
    # "12 stacked → 5 active → 143 calls".
    stack_count: int = 0


class ItemListResponse(BaseModel):
    items: List[MarketplaceItem]
    total: int
    page: int
    page_size: int
    # Sort options the listing UI should expose for this response. The
    # first three are always available; "trending" only joins when at
    # least one item in the response carries a non-null trend_pct (i.e.
    # the prior-week threshold cleared somewhere). Frontend hides the
    # trending dropdown option when it's missing instead of letting the
    # user select a sort that would render an empty grid.
    available_sorts: List[str] = ["recent", "most_used", "most_adopted"]


class CategoryEntry(BaseModel):
    name: str
    count: int
    icon_key: str


class CategoriesResponse(BaseModel):
    items: List[CategoryEntry]


class InnerItemSummary(BaseModel):
    name: str
    description: Optional[str] = None
    detail_url: Optional[str] = None  # nested detail for skill/agent only
    # v32: marketplace-metadata-driven cover photo for the skill/agent card on the
    # parent plugin detail page. Already in served-URL form (internal /asset/
    # endpoint, mirrored /mirrored/ endpoint, or pass-through external URL).
    # None → frontend renders initials placeholder ("SK" / "AG").
    cover_photo_url: Optional[str] = None
    # Per-item engagement signal so the plugin detail page's inner card grid
    # can render the same funnel chip the marketplace listing cards show.
    # Sourced from usage_marketplace_item_window + usage_marketplace_item_daily
    # via the bulk loader `ReportsRepository.inner_items_stats_by_parent`.
    # Zero/None when
    # the rollup has no row for this item (brand-new bundle, zero invocations) —
    # the frontend hides the chip on (parent_stack_count == 0 && calls == 0),
    # same gate as listing cards.
    invocations_30d: int = 0
    distinct_users_30d: int = 0
    trend_pct: Optional[float] = None
    # Adoption inherited from the parent plugin — inner items can't be
    # installed on their own, so the chip's "installed" segment reads the
    # parent's stack count (curated) / install_count (flea).
    parent_stack_count: int = 0


class CommandEntry(BaseModel):
    name: str
    description: Optional[str] = None


class HookEntry(BaseModel):
    name: str
    event: Optional[str] = None  # "—" rendered client-side when None
    description: Optional[str] = None


class McpEntry(BaseModel):
    name: str
    type: Optional[str] = None  # declared `type`, else inferred "stdio" / "http"
    description: Optional[str] = None


class FileEntry(BaseModel):
    """One file in a plugin / skill / agent bundle. Path is relative to the
    bundle root; size is bytes (rendered with ``humanbytes`` on the client)."""

    path: str
    size: int


class DocEntry(BaseModel):
    """One doc file shipped alongside a Store entity. URL points at the
    serving endpoint (``/api/store/entities/<id>/docs/<filename>``)."""

    name: str
    url: str


class UseCase(BaseModel):
    """One "When to use it" example from marketplace-metadata.json.
    Curator-authored: friendly title + 1-2-sentence description + the
    literal prompt the user would paste into Claude Code."""

    title: str
    description: str
    prompt: str


class SampleInteraction(BaseModel):
    """Example dialog shown in the "Sample interaction" section.

    ``assistant`` is markdown — the API pre-renders to safe HTML in
    ``assistant_html`` so the template can inject without a client-side
    markdown lib. ``assistant`` stays as the source for copy-paste / future
    re-rendering needs.
    """

    user: str
    assistant: str
    assistant_html: str


class PluginDetailResponse(BaseModel):
    """Unified detail response for a plugin (curated *or* flea).

    Frontend-side switching is purely cosmetic — `source` controls the
    Curated/Flea pill, photo URL fallback path, and owner label.
    """

    source: Literal["curated", "flea"]
    # IDs / breadcrumbs
    marketplace_id: Optional[str] = None  # curated only
    marketplace_name: Optional[str] = None  # curated only
    entity_id: Optional[str] = None  # flea only
    plugin_name: str
    manifest_name: str
    # Trust axis — the same two stored fields `MarketplaceItem` (the listing
    # row) already carries, so a detail page can spell the one trust
    # vocabulary (`macros/_trustmark.html`) instead of the retired
    # Curated/Flea split. Curated plugins are admin-registered, so they
    # report `organization` with verification left at `none` — identical to
    # what `_curated_to_item` puts on the listing row for the same plugin.
    # Defaults match that helper's, so a caller that ignores these fields
    # (and the blue/topnav pill, which still reads `source`) is unaffected.
    publisher_kind: str = "user"
    verification_state: str = "none"
    # Display
    description: Optional[str] = None
    version: Optional[str] = None
    category: Optional[str] = None
    author_name: Optional[str] = None  # curated curator / flea owner
    curator_email: Optional[str] = None  # curated only — surfaced for "contact curator"
    owner_display: Optional[str] = None  # flea: users.name → email → owner_username
    homepage: Optional[str] = None
    cover_photo_url: Optional[str] = None  # /api/store/.../photo for flea, marketplace-metadata for curated
    video_url: Optional[str] = None  # v32: external (YouTube/Vimeo/Loom) embed URL
    bundle_size: Optional[int] = None  # bytes; None when unknown
    install_count: int = 0  # flea only; curated leaves at 0
    # stack_count: how many users currently have this plugin in their
    # stack — curated reads user_plugin_optouts (post-v28 PRESENCE =
    # subscribed; system plugins included via fanout), flea reads
    # store_entities.install_count. Same field the listing chip
    # renders as "N installed" — surfaced here so the detail hero
    # can show the matching figure.
    stack_count: int = 0
    released_at: Optional[str] = None  # ISO timestamp
    updated_at: Optional[str] = None  # ISO timestamp
    installed: bool = False
    # Internal structure
    skills: List[InnerItemSummary] = []
    agents: List[InnerItemSummary] = []
    commands: List[CommandEntry] = []
    hooks: List[HookEntry] = []
    mcps: List[McpEntry] = []
    # Bundle contents — used by the redesigned skill/agent/plugin detail pages
    files: List[FileEntry] = []
    docs: List[DocEntry] = []
    # v32+ quarantine: surface the entity's visibility so the JS install
    # path can refuse to render the install button when non-approved.
    # Curated entries omit (always live).
    visibility_status: Optional[str] = None
    # Whether THIS caller's POST /install would be accepted. `visibility_status`
    # alone cannot answer that: `hidden` is written both by the author choosing
    # Private and by guardrail quarantine, and only the first is installable —
    # by its own author, forever, since a Private row never promotes to
    # `approved`. The front-end used to re-derive the rule as
    # `visibility_status !== 'approved'`, which permanently disabled the
    # install button on the author's own Private plugin even though the API
    # would have accepted it (#1178). Resolved server-side, next to the gate
    # it mirrors, so the two cannot drift apart again.
    installable: bool = True
    # Latest submission verdict for the linked entity — populated only
    # for the owner / admin (the same audiences that see the quarantine
    # banner). The banner's auto-refresh JS polls this field so it can
    # reload the page when an LLM review lands; visibility alone is
    # insufficient because `blocked_llm` keeps the entity at
    # `visibility_status='pending'`.
    submission_status: Optional[str] = None
    # v39: drives the disabled install button on the curated plugin
    # detail page. The same flag travels via /api/marketplace/items so
    # the browse cards can show a "Required" pill.
    is_system: bool = False
    # Group-scoped Required tier (resource_grants.requirement='required'
    # on any of the caller's groups) — same locked UI treatment as
    # ``is_system``, scoped to the granted groups instead of all users.
    is_required: bool = False
    # Rich-content fields from marketplace-metadata.json (plugin-level, curated
    # only — flea entities don't have a metadata layer). All optional; UI
    # sections render only when populated.
    #
    # `display_name` overrides the technical `manifest_name` on the hero h1
    # and window titlebar. `tagline` is the friendly subtitle (replacing
    # `description` as the hero summary on listing cards + hero).
    #
    # `description_long_html` is server-side rendered + sanitized markdown
    # (see app/markdown_render.render_safe). It powers the "What it does"
    # panel; the raw `description` from marketplace.json is the fallback.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    description_long_html: Optional[str] = None
    use_cases: List[UseCase] = []
    sample_interaction: Optional[SampleInteraction] = None
    # telemetry (Phase B.1)
    telemetry: Optional[Dict[str, Any]] = None


# Legacy alias kept so any unmigrated import continues to resolve. The new
# unified model supersedes the curated-only response shape.
CuratedDetailResponse = PluginDetailResponse


class InnerDetailResponse(BaseModel):
    marketplace_id: str
    plugin_name: str
    kind: Literal["skill", "agent"]
    name: str
    description: Optional[str] = None
    body: str
    relpath: str  # path inside plugin_dir for diagnostics
    # Parent plugin metadata + bundle contents — populated so the redesigned
    # skill/agent detail page can render hero badges, sidebar rows, and the
    # Files section without a second roundtrip to the parent plugin endpoint.
    marketplace_name: str = ""
    category: Optional[str] = None
    parent_author_name: Optional[str] = None
    parent_updated_at: Optional[str] = None
    # Trust axis — see the twin fields on `PluginDetailResponse`. An inner
    # skill/agent inherits its PARENT's trust: it ships inside that bundle and
    # is not separately publishable, so a per-inner claim would be a second
    # vocabulary saying something the store never recorded.
    publisher_kind: str = "user"
    verification_state: str = "none"
    # Parent plugin's `name` from `.claude-plugin/plugin.json` — same value
    # the synth marketplace.json uses, so /<manifest_name>:<inner_name> is
    # exactly what Claude Code accepts after install.
    manifest_name: str = ""
    # Parent plugin's curator-friendly display name from marketplace-metadata.json
    # (same value the plugin detail hero prefers over manifest_name). Null when
    # curator hasn't set it; the frontend then falls back to manifest_name.
    parent_display_name: Optional[str] = None
    bundle_size: Optional[int] = None
    files: List[FileEntry] = []
    # v32: per-skill / per-agent enrichment from marketplace-metadata.json sub-tree.
    # Read at request time from the cloned working tree (not cached in DB) so
    # curators can update one inner asset without paying the cost of a full
    # plugin-cache rewrite. Three-typed cover URL layout matches plugin
    # detail: internal → /asset/, mirror status not yet checked here so
    # external URLs pass through unmirrored (we don't run sync mid-request).
    cover_photo_url: Optional[str] = None
    video_url: Optional[str] = None
    docs: List[DocEntry] = []
    # Rich user-facing fields (parity with plugin-level rich content from
    # the 2026-05-12 redesign). All optional — UI hides each section when
    # the corresponding field is absent.
    #
    # `category` on the response is curator-override-OR-parent-fallback:
    # the API hands back the parent plugin's category when the skill/agent
    # didn't opt into its own. The override happens in the inner-detail
    # handler before the response leaves the API.
    #
    # `invocation`: curator-provided literal command string ("…&lt;arg&gt;"
    # forms welcome). When absent, the template falls back to its computed
    # "<manifest_name>:<inner_name>" string.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    description_long_html: Optional[str] = None
    use_cases: List[UseCase] = []
    sample_interaction: Optional[SampleInteraction] = None
    when_to_use_html: Optional[str] = None
    invocation: Optional[str] = None
    # v46: per-item telemetry — 30d invocations + distinct users for this
    # specific skill/agent (lookup keyed by parent_plugin + name in the
    # usage_marketplace_item_window snapshot). Always present so the
    # frontend chip can decide visibility from (parent_stack_count > 0
    # OR invocations_30d > 0), matching the plugin detail page.
    telemetry: Optional[Dict[str, object]] = None
    # parent_stack_count: how many users currently have the *parent
    # plugin* in their stack. Skills/agents inherit adoption from their
    # plugin — there's no per-skill subscription model — so the hero
    # chip surfaces the parent figure with a "Plugin:" label and a
    # tooltip explaining the relationship, completing the funnel:
    # "12 installed the plugin → 2 active on this skill".
    parent_stack_count: int = 0


class InstallActionResponse(BaseModel):
    installed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# _load_invocation_stats (the per-source browse-panel telemetry map) moved
# to ``reports_repo().invocation_stats(source)`` — see
# ``src/repositories/reports.py`` / ``reports_pg.py`` (#728). It used to run
# raw SQL directly on the always-DuckDB ``_get_db`` connection, so the
# browse-panel stats stayed empty on a Postgres instance even after the
# rollup producer went dual-backend (#773). Call sites below now go through
# the factory so the read follows the active backend.


# _load_plugin_daily_series, _load_inner_item_stats, and
# _load_inner_items_stats_by_parent moved to
# ``reports_repo().plugin_daily_series(...)`` /
# ``.inner_item_stats(...)`` / ``.inner_items_stats_by_parent(...)`` — see
# ``src/repositories/reports.py`` / ``reports_pg.py`` (#728). Same reason as
# the invocation_stats move above: raw SQL on the always-DuckDB ``_get_db``
# connection meant detail-page telemetry stayed empty on Postgres.


def _audit(
    actor_id: str,
    action: str,
    target: str,
    params: Optional[dict] = None,
) -> None:
    # Backend-aware: routes through audit_repo(); no connection needed.
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=target, params=params)
    except Exception:
        pass


def _invalidate_etag() -> None:
    try:
        from app.marketplace_server import packager

        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")
    try:
        from app.marketplace_server import cowork_packager

        cowork_packager.invalidate_cache()
    except Exception:
        logger.exception("failed to invalidate cowork zip cache")


def _frontmatter_body(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end() :].lstrip("\n") if m else text


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _icon_key_for(category: Optional[str]) -> str:
    """Stable key used by the frontend to look up the inline SVG icon
    (registered in ``src/category_icons.py``). Matches our taxonomy 1:1."""
    return category or "Other"


def _curated_detail_url(marketplace_id: str, plugin_name: str) -> str:
    return f"/marketplace/curated/{marketplace_id}/{plugin_name}"


def _flea_detail_url(entity_id: str) -> str:
    return f"/marketplace/flea/{entity_id}"


def _curated_stack_sets(conn: Optional[duckdb.DuckDBPyConnection], user_id: str) -> Tuple[set, set]:
    """``(in_stack, required)`` curated plugin key sets for a user.

    ``in_stack`` is the union ``resolve_user_marketplace`` actually serves:
    explicit subscriptions ∪ required-tier grant keys
    (``resource_grants.requirement='required'`` for any of the user's
    groups). ``required`` is surfaced separately so cards / detail pages
    can render the locked "Required" state for group-required plugins the
    same way they do for global ``is_system`` ones.

    ``conn`` is optional — it is only a DuckDB-backend fast path for the
    group-membership read (see ``app.auth.access._user_group_ids``), so a
    caller without a request-scoped connection passes ``None`` and gets the
    same answer through the repo factory. That is what lets the Library
    (``app.web.router.library_page``, which takes no ``conn``) derive its
    plugin rows' Stack state from THIS helper rather than reimplementing the
    union — the two surfaces disagreeing about the same plugin is exactly
    the drift this is the single source of truth for.
    """
    required = required_plugin_keys(conn, user_id)
    subs = user_curated_subscriptions_repo().subscribed_set(user_id)
    return subs | required, required


def _resolve_marketplace_meta(conn: duckdb.DuckDBPyConnection, marketplace_id: str) -> Dict[str, Optional[str]]:
    """Look up display name + curator metadata in one DB hit.

    Returns a dict with keys ``name``, ``curator_name``, ``curator_email``.
    Missing rows fall back to the marketplace_id as ``name`` and ``None``
    for the curator fields. Caller decides what to do with the absence
    (typically: surface the ``OWNER_TODO_PLACEHOLDER``).
    """
    row = marketplace_registry_repo().get(marketplace_id)
    if not row:
        return {
            "name": marketplace_id,
            "curator_name": None,
            "curator_email": None,
        }
    name = (row.get("name") or "").strip() or marketplace_id
    return {
        "name": name,
        "curator_name": (row.get("curator_name") or "").strip() or None,
        "curator_email": (row.get("curator_email") or "").strip() or None,
    }


def _curated_to_item(
    conn: duckdb.DuckDBPyConnection,
    plugin_row: dict,
    *,
    subs: set,
    marketplace_meta: Dict[str, Dict[str, Optional[str]]],
    stats: Optional[Dict[str, Dict]] = None,
    stack_counts: Optional[Dict[Tuple[str, str], int]] = None,
    required_keys: set = frozenset(),
) -> MarketplaceItem:
    marketplace_id = plugin_row["marketplace_id"]
    plugin_name = plugin_row["name"]
    meta = marketplace_meta.get(marketplace_id) or {
        "name": marketplace_id,
        "curator_name": None,
        "curator_email": None,
    }
    # v32: card "owner" is the curator from the marketplace registry — not the
    # upstream `marketplace.json::author.name` we historically used. Curator
    # is the human accountable for the plugin's presence in this Agnes
    # instance; upstream author belongs in the plugin source repo.
    owner = meta["curator_name"] or OWNER_TODO_PLACEHOLDER
    # Lazy enrichment read for the listing card — cached per (marketplace_id,
    # mtime) so a page of 24 plugins from the same marketplace triggers ONE
    # disk read, not 24. Empty dict when curator hasn't filled the fields →
    # listing card falls back to raw name + marketplace.json description.
    enrichment = _curated_plugin_enrichment(marketplace_id, plugin_name)
    # v46: stats keyed by plain plugin name (not `<mp>/<plugin>`) because
    # `usage_marketplace_item_window.name` carries the plain plugin name —
    # the marketplace_id is implicit in the curated source.
    stat = (stats or {}).get(plugin_name, {})
    from app.api.store import ORGANIZATION_PUBLISHER_LABEL

    return MarketplaceItem(
        id=f"curated-{marketplace_id}/{plugin_name}",
        source="curated",
        name=plugin_name,
        type="plugin",
        category=plugin_row.get("category") or None,
        description=plugin_row.get("description"),
        owner=owner,
        version=plugin_row.get("version"),
        # Cover photo URL is already in served form (internal `/asset/`,
        # mirrored `/mirrored/`, or pass-through external URL) — see
        # src.marketplace._refresh_plugin_cache for the resolution path.
        photo_url=plugin_row.get("cover_photo_url"),
        added=_to_iso(plugin_row.get("created_at")),
        installed=(marketplace_id, plugin_name) in subs,
        marketplace_slug=marketplace_id,
        marketplace_name=meta["name"],
        detail_url=_curated_detail_url(marketplace_id, plugin_name),
        is_system=bool(plugin_row.get("is_system")),
        is_required=(marketplace_id, plugin_name) in required_keys,
        display_name=enrichment.get("display_name"),
        tagline=enrichment.get("tagline"),
        invocations_30d=stat.get("invocations_30d", 0),
        distinct_users_30d=stat.get("distinct_users_30d", 0),
        invocations_7d=stat.get("invocations_7d", 0),
        distinct_users_7d=stat.get("distinct_users_7d", 0),
        trend_pct=stat.get("trend_pct"),
        stack_count=(stack_counts or {}).get((marketplace_id, plugin_name), 0),
        # Organization-published by construction — registering the marketplace
        # was the admin act. Curated items therefore carry no verification
        # state: "Organization" already is the stronger claim.
        publisher_kind="organization",
        publisher_name=ORGANIZATION_PUBLISHER_LABEL,
        verification_state="none",
    )


def _flea_to_item(
    entity: dict,
    *,
    installed_set: set,
    users_display: Dict[str, Tuple[Optional[str], Optional[str]]],
    viewer_id: Optional[str] = None,
    stats: Optional[Dict[str, Dict]] = None,
) -> MarketplaceItem:
    photo_url = (
        # ``?v=`` cache-busting fingerprint: flea entities have a monotonic
        # ``version_no`` (schema v37) bumped on every re-upload, so the URL
        # changes exactly when the underlying bytes change.
        f"/api/store/entities/{entity['id']}/photo?v={entity.get('version_no', 1)}"
        if entity.get("photo_path")
        else None
    )
    # v49 phase-3: invocation is the stored synthetic_name. The column is
    # NOT NULL (phase 1 migration + repo create/update/archive write
    # paths keep it in sync), so reading it directly is safe and a
    # missing value would be a real bug worth surfacing as KeyError
    # rather than masking with a recompute.
    invocation = entity["synthetic_name"]
    is_viewer_owner = bool(viewer_id and entity.get("owner_user_id") == viewer_id)
    # v49 phase-5: rollup `name` column carries the synthetic_name (the
    # post-rename keyspace used by `MarketplaceItemLookup`). Stats dict
    # built by `ReportsRepository.invocation_stats` is keyed by that same
    # value, so the lookup uses `entity["synthetic_name"]`.
    stat = (stats or {}).get(entity["synthetic_name"], {})
    # v49 phase-2: front the card with the user-friendly `title` (humanized,
    # acronym-aware) via the existing `display_name` field — JS already
    # has the chain `it.display_name || it.name` on cards. `tagline`
    # rides the same chain JS uses for curated. Owner display resolves
    # `users.name → users.email → owner_username` so cards no longer
    # leak the kebab-case username (e.g. `c-marustamyan`) when the user
    # has a real name on their account. Reads from a prefetched
    # ``users_display`` map (one IN-query per page) — see
    # ``_load_users_display`` callers in ``list_items``.
    owner_display = _owner_display_from_map(
        users_display,
        entity["owner_user_id"],
        entity.get("owner_username") or "",
    )
    from app.api.store import ORGANIZATION_PUBLISHER_LABEL

    return MarketplaceItem(
        id=f"flea-{entity['id']}",
        source="flea",
        name=invocation,
        type=entity["type"],
        category=entity.get("category") or None,
        description=entity.get("description"),
        owner=owner_display,
        display_name=entity.get("title"),
        tagline=entity.get("tagline"),
        version=entity.get("version"),
        photo_url=photo_url,
        added=_to_iso(entity.get("created_at")),
        installed=entity["id"] in installed_set,
        marketplace_slug=None,
        marketplace_name=None,
        detail_url=_flea_detail_url(entity["id"]),
        visibility_status=entity.get("visibility_status") or "approved",
        is_viewer_owner=is_viewer_owner,
        invocations_30d=stat.get("invocations_30d", 0),
        distinct_users_30d=stat.get("distinct_users_30d", 0),
        invocations_7d=stat.get("invocations_7d", 0),
        distinct_users_7d=stat.get("distinct_users_7d", 0),
        trend_pct=stat.get("trend_pct"),
        # Flea stack_count = persistent install counter on store_entities,
        # bumped by POST /api/store/entities/{id}/install and decremented
        # by DELETE. Mirrors the curated subscriber count semantically
        # (how many users have this in their stack).
        stack_count=int(entity.get("install_count") or 0),
        publisher_kind=entity.get("publisher_kind") or "user",
        publisher_name=(
            ORGANIZATION_PUBLISHER_LABEL
            if (entity.get("publisher_kind") or "user") == "organization"
            else owner_display
        ),
        verification_state=entity.get("verification_state") or "none",
    )


# ---------------------------------------------------------------------------
# GET /api/marketplace/items
# ---------------------------------------------------------------------------


def _build_telemetry(
    source: str,
    name: str,
) -> Optional[Dict[str, Any]]:
    """Build the plugin-level telemetry dict for detail endpoints.

    `name` is the plugin name (curated) or flea entity name. Returns
    None when ``invocations_30d == 0`` so the caller can omit the chip
    payload entirely; frontend hero / sidebar already handle a missing
    `d.telemetry` (`d.telemetry || {}` guard + `if (!d.telemetry || …)`
    on daily_series). When data IS present, ``daily_series`` is always a
    30-entry zero-padded list so the "Active days" / "Last used"
    derivations on the sidebar are trivial.
    """
    reports = reports_repo()
    stats = reports.invocation_stats(source)
    stat = stats.get(name)
    inv30 = int(stat["invocations_30d"]) if stat else 0
    if inv30 == 0:
        return None
    return {
        "invocations_30d": inv30,
        "distinct_users_30d": int(stat["distinct_users_30d"]) if stat else 0,
        "trend_pct": stat.get("trend_pct") if stat else None,
        "daily_series": reports.plugin_daily_series(source, name),
    }


def _load_curated_stack_counts() -> Dict[Tuple[str, str], int]:
    """Return {(marketplace_id, plugin_name): subscriber_count} for every
    curated plugin with at least one subscriber.

    Thin wrapper over ``UserCuratedSubscriptionsRepository.stack_counts`` /
    ``UserCuratedSubscriptionsPgRepository.stack_counts`` so callers stay
    backend-agnostic — one query per page render, avoids N+1.
    """
    return user_curated_subscriptions_repo().stack_counts()


def _available_sorts(stats_dicts: List[Dict[str, Dict]]) -> List[str]:
    """Decide which sort options the listing UI should expose.

    Recent / most_used / most_adopted always sort correctly (raw / count
    columns are populated even when zero). Trending only joins when at
    least one stat row carries a non-null trend_pct — that signals the
    `prior_7d >= 3` threshold cleared somewhere in this tab's data set.

    `stats_dicts` is the per-source stats map returned by
    `ReportsRepository.invocation_stats`; the my-tab passes both curated +
    flea stats so the option is available when *either* source has trend
    data.
    """
    base = ["recent", "most_used", "most_adopted"]
    for stats in stats_dicts:
        if any(s.get("trend_pct") is not None for s in stats.values()):
            base.append("trending")
            break
    return base


def _apply_sort(
    items: List[MarketplaceItem],
    sort: str,
) -> List[MarketplaceItem]:
    """Sort a list of MarketplaceItem objects in-place and return it.

    - ``recent``        — preserve existing order (no-op).
    - ``most_used``     — DESC by invocations_30d, then name ASC.
    - ``most_adopted``  — DESC by distinct_users_30d, then name ASC.
                          Same shape as most_used but keyed on the true
                          30d distinct user count — protects the listing
                          from power-user skew (one user × 100 invokes
                          can't beat 10 different users × 1 invoke).
    - ``trending``      — DESC by trend_pct; items with trend_pct=None
                          are excluded (the daily-fact threshold means
                          missing trend = noisy data, not zero growth).
    """
    if sort == "most_used":
        items.sort(key=lambda it: (-it.invocations_30d, it.name.lower()))
    elif sort == "most_adopted":
        items.sort(key=lambda it: (-it.distinct_users_30d, it.name.lower()))
    elif sort == "trending":
        items = [it for it in items if it.trend_pct is not None]
        items.sort(key=lambda it: -(it.trend_pct or 0.0))
    return items


#: The union the "unverified" FILTER value maps to. Deliberately not a label —
#: no card prints "Unverified" (see StoreEntitiesRepository's VERIFICATION_STATES).
_UNVERIFIED_VERIFICATION_STATES = ("none", "requested", "changes_requested")


async def _browse_items(
    *,
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    publisher: Optional[str],
    verification: Optional[str],
    q: Optional[str],
    category: Optional[str],
    type: Optional[str],
    sort: str,
    page: int,
    page_size: int,
) -> ItemListResponse:
    """One shelf, two sources — the unified browse listing (v104).

    Replaces the Curated / Flea tabs. Those tabs and the Publisher facet asked
    the same question ("who is this from?") with different answers, so a user
    filtering Publisher=Organization while standing on the Flea tab would see an
    empty grid even though organization-published authored items exist. The facet
    wins; the shelves go.

    Pagination is exact, not approximate. Each source reports its own total, and
    each is over-fetched to ``skip + page_size`` before the merge, so the merged
    slice is the same one a single UNION query would return — bounded by
    ``2 * (skip + page_size)`` rows in memory.

    Publisher facet → source selection:
      * ``organization`` — curated plugins ∪ organization-published entities.
      * ``me`` / ``other_users`` — authored entities only (a curated plugin can
        never be "mine" in the publishing sense).
    """
    from app.auth.access import is_user_admin

    skip = (page - 1) * page_size
    window = skip + page_size
    is_admin = is_user_admin(user["id"], conn)

    # ---- curated side -------------------------------------------------------
    # Only publisher ∈ {None, 'organization'} can include curated plugins, and a
    # `type` filter other than 'plugin' excludes them (they are always plugins).
    include_curated = publisher in (None, "organization") and type in (None, "plugin")
    # Verification is only ever meaningful on user-published items, so asking for
    # 'verified' or 'unverified' excludes the curated side entirely rather than
    # silently treating "no state" as one or the other.
    if verification is not None:
        include_curated = False
    curated_items: List[MarketplaceItem] = []
    curated_total = 0
    curated_stats = reports_repo().invocation_stats("curated")
    if include_curated:
        group_ids = _user_group_ids(user["id"], conn) or set()
        subs, required = _curated_stack_sets(conn, user["id"])
        curated_rows, curated_total = marketplace_plugins_repo().list_with_filters(
            group_ids=group_ids,
            search=q or None,
            category=category or None,
            skip=0,
            limit=10000 if sort != "recent" else window,
        )
        marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}
        for mp_id in {r["marketplace_id"] for r in curated_rows}:
            marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)
        curated_stack_counts = _load_curated_stack_counts()
        curated_items = [
            _curated_to_item(
                conn,
                r,
                subs=subs,
                marketplace_meta=marketplace_meta,
                stats=curated_stats,
                stack_counts=curated_stack_counts,
                required_keys=required,
            )
            for r in curated_rows
        ]

    # ---- authored side ------------------------------------------------------
    publisher_kind: Optional[str] = None
    facet_owner: Optional[str] = None
    exclude_owner: Optional[str] = None
    if publisher == "organization":
        publisher_kind = "organization"
    elif publisher == "me":
        publisher_kind = "user"
        facet_owner = user["id"]
    elif publisher == "other_users":
        publisher_kind = "user"
        exclude_owner = user["id"]
    verification_states: Optional[List[str]] = None
    if verification == "verified":
        verification_states = ["verified"]
    elif verification == "unverified":
        verification_states = list(_UNVERIFIED_VERIFICATION_STATES)

    if is_admin:
        # Everything except soft-deleted rows — see ADMIN_BROWSE_VISIBILITY.
        visibility_filter: Optional[List[str]] = list(ADMIN_BROWSE_VISIBILITY)
        include_owner = None
    else:
        visibility_filter = ["approved"]
        include_owner = user["id"]
    flea_rows, flea_total = store_entities_repo().list(
        skip=0,
        limit=10000 if sort != "recent" else window,
        type=type,
        category=category,
        search=q or None,
        owner_user_id=facet_owner,
        visibility_status=visibility_filter,
        include_owner_id=include_owner,
        publisher_kind=publisher_kind,
        exclude_owner_user_id=exclude_owner,
        verification_state=verification_states,
    )
    flea_stats = reports_repo().invocation_stats("flea")
    installed_set = {row["id"] for row in user_store_installs_repo().list_for_user(user["id"])}
    flea_users_display = _load_users_display((r["owner_user_id"] for r in flea_rows))
    flea_items = [
        _flea_to_item(
            r,
            installed_set=installed_set,
            users_display=flea_users_display,
            viewer_id=user["id"],
            stats=flea_stats,
        )
        for r in flea_rows
    ]

    items = curated_items + flea_items
    if sort == "recent":
        # Newest first across both sources; items with no `added` sort last
        # rather than crashing the comparison.
        items.sort(key=lambda it: (it.added or "", it.name.lower()), reverse=True)
    else:
        items = _apply_sort(items, sort)
    total = curated_total + flea_total
    if sort == "trending":
        # `trending` drops rows without trend data, so the source totals would
        # overstate the result set.
        total = len(items)
    return ItemListResponse(
        items=items[skip : skip + page_size],
        total=total,
        page=page,
        page_size=page_size,
        available_sorts=_available_sorts([curated_stats, flea_stats]),
    )


@router.get("/items", response_model=ItemListResponse)
async def list_items(
    tab: Literal["browse", "curated", "flea", "my"] = Query("curated"),
    publisher: Optional[Literal["organization", "me", "other_users"]] = Query(None),
    verification: Optional[Literal["verified", "unverified"]] = Query(None),
    q: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    type: Optional[Literal["skill", "agent", "plugin"]] = Query(None),
    sort: Literal["recent", "most_used", "most_adopted", "trending"] = Query("recent"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Paginated, RBAC-scoped item list for the ``/marketplace`` browse page.

    Per-tab dispatch:

    * ``curated`` → ``MarketplacePluginsRepository.list_with_filters`` scoped to
      the caller's ``user_group_members``. Results are tagged with the
      caller's subscription state.
    * ``flea`` → ``StoreEntitiesRepository.list`` (community Store).
    * ``my`` → direct read from ``user_curated_subscriptions`` (filtered
      against the caller's RBAC-allowed plugins) ∪ ``user_store_installs``
      (all flea types — skill / agent / plugin — surface as individual
      cards). We deliberately don't reuse ``resolve_user_marketplace``
      here: that resolver bundles flea skills/agents into a single
      synthetic ``store-bundle`` entry useful for the served Claude Code
      marketplace ZIP/git endpoints but wrong for /marketplace browsing,
      where each item should appear as its own card. Mirrors the
      ``/api/my-stack`` reading pattern.

    ``sort`` controls ordering after stats are joined:
      * ``recent``    — existing DB order (default, backward-compatible).
      * ``most_used`` — DESC by invocations_30d, ties by install_count then name.
      * ``trending``  — DESC by trend_pct; items with no trend data are excluded.
    """
    skip = (page - 1) * page_size
    needs_sort = sort != "recent"

    if tab == "browse":
        return await _browse_items(
            conn=conn,
            user=user,
            publisher=publisher,
            verification=verification,
            q=q,
            category=category,
            type=type,
            sort=sort,
            page=page,
            page_size=page_size,
        )

    if tab == "curated":
        group_ids = _user_group_ids(user["id"], conn) or set()
        subs, required = _curated_stack_sets(conn, user["id"])
        # When sorting, we need all rows to sort before paginating.
        if needs_sort:
            all_rows, total = marketplace_plugins_repo().list_with_filters(
                group_ids=group_ids,
                search=q or None,
                category=category or None,
                skip=0,
                limit=10000,
            )
        else:
            all_rows, total = marketplace_plugins_repo().list_with_filters(
                group_ids=group_ids,
                search=q or None,
                category=category or None,
                skip=skip,
                limit=page_size,
            )
        marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}
        if all_rows:
            distinct_ids = {r["marketplace_id"] for r in all_rows}
            for mp_id in distinct_ids:
                marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)
        curated_stats = reports_repo().invocation_stats("curated")
        curated_stack_counts = _load_curated_stack_counts()
        items = [
            _curated_to_item(
                conn,
                r,
                subs=subs,
                marketplace_meta=marketplace_meta,
                stats=curated_stats,
                stack_counts=curated_stack_counts,
                required_keys=required,
            )
            for r in all_rows
        ]
        if needs_sort:
            items = _apply_sort(items, sort)
            total = len(items)
            items = items[skip : skip + page_size]
        return ItemListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            available_sorts=_available_sorts([curated_stats]),
        )

    if tab == "flea":
        installed_set = {row["id"] for row in user_store_installs_repo().list_for_user(user["id"])}
        # Visibility filter: non-admin sees approved + their own
        # non-approved (so submitters spot what's still under review
        # in their own grid). Admin sees everything except soft-deleted
        # rows — see ADMIN_BROWSE_VISIBILITY.
        from app.auth.access import is_user_admin

        if is_user_admin(user["id"], conn):
            visibility_filter = list(ADMIN_BROWSE_VISIBILITY)
            include_owner = None
        else:
            visibility_filter = ["approved"]
            include_owner = user["id"]
        if needs_sort:
            all_flea_rows, total = store_entities_repo().list(
                skip=0,
                limit=10000,
                type=type,
                category=category,
                search=q or None,
                visibility_status=visibility_filter,
                include_owner_id=include_owner,
            )
        else:
            all_flea_rows, total = store_entities_repo().list(
                skip=skip,
                limit=page_size,
                type=type,
                category=category,
                search=q or None,
                visibility_status=visibility_filter,
                include_owner_id=include_owner,
            )
        flea_stats = reports_repo().invocation_stats("flea")
        flea_users_display = _load_users_display(
            (r["owner_user_id"] for r in all_flea_rows),
        )
        items = [
            _flea_to_item(
                r,
                installed_set=installed_set,
                users_display=flea_users_display,
                viewer_id=user["id"],
                stats=flea_stats,
            )
            for r in all_flea_rows
        ]
        if needs_sort:
            items = _apply_sort(items, sort)
            total = len(items)
            items = items[skip : skip + page_size]
        return ItemListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            available_sorts=_available_sorts([flea_stats]),
        )

    # tab == "my" — see docstring; read directly from source-of-truth tables.
    items: List[MarketplaceItem] = []

    granted = resolve_allowed_plugins(conn, user)
    subs, required = _curated_stack_sets(conn, user["id"])
    marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}

    # Pull the enriched rows the Curated tab uses (cover_photo_url, video_url,
    # category override, doc_links from marketplace-metadata.json) so the My Stack
    # cards look identical to the curated cards the user just clicked
    # "+ Add to my stack" on. ``resolve_allowed_plugins`` reads only the
    # upstream marketplace.json, which doesn't carry those columns; without
    # this lookup the same plugin renders with its cover photo on
    # ``?tab=curated`` and with a gradient placeholder on ``?tab=my``.
    plugin_repo = marketplace_plugins_repo()
    subscribed_mp_ids = {mp_id for (mp_id, _) in subs}
    enriched_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for mp_id in subscribed_mp_ids:
        for row in plugin_repo.list_for_marketplace(mp_id):
            enriched_lookup[(mp_id, row["name"])] = row

    curated_stats = reports_repo().invocation_stats("curated")
    flea_stats = reports_repo().invocation_stats("flea")
    curated_stack_counts = _load_curated_stack_counts()

    for p in granted:
        key = (p["marketplace_id"], p["original_name"])
        if key not in subs:
            continue
        mp_id = p["marketplace_id"]
        if mp_id not in marketplace_meta:
            marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)

        plugin_row = enriched_lookup.get(key)
        if plugin_row is None:
            # Fallback: plugin in RBAC + subscribed but not yet ingested into
            # marketplace_plugins (rare race — granted before the first sync
            # cycle runs). Build the bare shape from the on-disk manifest so
            # the card still renders, just without marketplace-metadata enrichment;
            # cover falls through to the gradient placeholder until the next
            # sync.
            author = p["raw"].get("author")
            plugin_row = {
                "marketplace_id": mp_id,
                "name": p["original_name"],
                "description": p["raw"].get("description"),
                "version": p.get("version"),
                "category": p["raw"].get("category"),
                "author_name": author.get("name") if isinstance(author, dict) else None,
                "cover_photo_url": None,
                "created_at": None,
            }
        items.append(
            _curated_to_item(
                conn,
                plugin_row,
                subs=subs,
                marketplace_meta=marketplace_meta,
                stats=curated_stats,
                stack_counts=curated_stack_counts,
                required_keys=required,
            )
        )

    flea_installs = user_store_installs_repo().list_for_user(user["id"])
    flea_installed_set = {row["id"] for row in flea_installs}
    flea_users_display = _load_users_display(
        (row["owner_user_id"] for row in flea_installs),
    )
    for entity in flea_installs:
        items.append(
            _flea_to_item(
                entity,
                installed_set=flea_installed_set,
                users_display=flea_users_display,
                stats=flea_stats,
            )
        )

    # Apply optional filters client-server-style for `my` tab (small N):
    if q:
        needle = q.lower()
        items = [
            it
            for it in items
            if needle in it.name.lower()
            or needle in (it.description or "").lower()
            or needle in (it.owner or "").lower()
            or needle in (it.category or "").lower()
        ]
    if category:
        items = [it for it in items if (it.category or "Other") == category]
    if type:
        items = [it for it in items if it.type == type]
    items = _apply_sort(items, sort)
    total = len(items)
    items = items[skip : skip + page_size]
    return ItemListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        available_sorts=_available_sorts([curated_stats, flea_stats]),
    )


# ---------------------------------------------------------------------------
# GET /api/marketplace/categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=CategoriesResponse)
async def list_categories(
    tab: Literal["browse", "curated", "flea", "my"] = Query("curated"),
    type: Optional[Literal["skill", "agent", "plugin"]] = Query(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-tab category list with non-zero counts.

    Source of categories: union of ``STORE_CATEGORIES`` and any non-empty
    ``marketplace_plugins.category`` values in the caller's RBAC scope.
    Categories with zero matching items are omitted (the frontend hides
    them this way).

    ``browse`` (v104, the unified shelf) sums BOTH sources so the category rail
    annotates the same result set the grid renders.
    """
    counts: Dict[str, int] = {}

    if tab == "browse" and (type is None or type == "plugin"):
        group_ids = _user_group_ids(user["id"], conn) or set()
        counts.update(marketplace_plugins_repo().category_counts(group_ids=group_ids))

    if tab in ("curated", "my"):
        group_ids = _user_group_ids(user["id"], conn) or set()
        if tab == "curated":
            counts.update(marketplace_plugins_repo().category_counts(group_ids=group_ids))
        else:  # my — direct read mirroring the items endpoint's `my` branch.
            granted = resolve_allowed_plugins(conn, user)
            subs, _required = _curated_stack_sets(conn, user["id"])
            # Curated plugins are always type='plugin'. When the type filter
            # is set to skill/agent, those rows won't show in the items grid
            # (filtered out by the items endpoint at line 579), so they must
            # not contribute to the category counts either — otherwise the
            # pill counts overstate what the user will actually see.
            if type is None or type == "plugin":
                for p in granted:
                    if (p["marketplace_id"], p["original_name"]) not in subs:
                        continue
                    cat = (p["raw"].get("category") or "").strip() or "Other"
                    counts[cat] = counts.get(cat, 0) + 1
            for row in user_store_installs_repo().list_for_user(user["id"]):
                if type and row.get("type") != type:
                    continue
                cat = (row.get("category") or "").strip() or "Other"
                counts[cat] = counts.get(cat, 0) + 1

    if tab in ("flea", "browse"):
        # Visibility filter (v32+/v35): non-admin counts approved + own
        # non-archived non-approved (mirrors the listing endpoint so the
        # category counts match what the user actually sees in the
        # grid). Admin counts everything.
        from app.auth.access import is_user_admin

        if is_user_admin(user["id"], conn):
            # Admin counts everything the admin grid shows — i.e. everything
            # except soft-deleted rows, so the facet totals match it.
            cat_counts = store_entities_repo().category_counts(
                type=type,
                visibility_status=list(ADMIN_BROWSE_VISIBILITY),
            )
        else:
            # Non-admin: approved for everyone, plus own non-archived
            # non-approved (mirrors the listing endpoint's visibility).
            cat_counts = store_entities_repo().category_counts(
                type=type,
                visibility_status=["approved"],
                owner_id=user["id"],
            )
        for cat, n in cat_counts.items():
            counts[str(cat)] = counts.get(str(cat), 0) + int(n)

    items = [
        CategoryEntry(name=name, count=count, icon_key=_icon_key_for(name))
        for name, count in sorted(counts.items())
        if count > 0
    ]
    return CategoriesResponse(items=items)


# ---------------------------------------------------------------------------
# Curated detail + install endpoints
# ---------------------------------------------------------------------------


def _list_inner_skills(plugin_root: Path) -> List[InnerItemSummary]:
    """Build ``InnerItemSummary`` objects from the shared listing helper.

    The shared helper (``src.marketplace_listing.list_inner_skills``) returns
    plain names; the API layer re-reads each SKILL.md to populate the
    ``description`` field used on the detail page.
    """
    out: List[InnerItemSummary] = []
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return out
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(
            InnerItemSummary(
                name=fm.get("name") or skill_dir.name,
                description=fm.get("description"),
                detail_url=None,  # populated by caller (needs marketplace_id + plugin_name)
            )
        )
    return out


def _list_inner_agents(plugin_root: Path) -> List[InnerItemSummary]:
    """Build ``InnerItemSummary`` objects from the shared listing helper."""
    out: List[InnerItemSummary] = []
    agents_dir = plugin_root / "agents"
    if not agents_dir.is_dir():
        return out
    for agent_path in sorted(agents_dir.iterdir()):
        if not agent_path.is_file() or agent_path.suffix != ".md":
            continue
        try:
            text = agent_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(
            InnerItemSummary(
                name=fm.get("name") or agent_path.stem,
                description=fm.get("description"),
                detail_url=None,
            )
        )
    return out


def _list_commands(plugin_root: Path) -> List[CommandEntry]:
    """Return ``commands/*.md`` as ``CommandEntry`` objects from frontmatter.

    Names from the shared ``src.marketplace_listing.list_commands`` helper
    already carry the leading ``/``; re-read here to pick up ``description``.
    """
    d = plugin_root / "commands"
    if not d.is_dir():
        return []
    out: List[CommandEntry] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix != ".md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        raw = (fm.get("name") or p.stem or "").strip()
        if not raw:
            continue
        name = raw if raw.startswith("/") else f"/{raw}"
        out.append(
            CommandEntry(
                name=name,
                description=fm.get("description"),
            )
        )
    return out


def _read_plugin_json(plugin_root: Path) -> dict:
    """Best-effort load of the plugin's own .claude-plugin/plugin.json."""
    pj = plugin_root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return {}
    try:
        import json

        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _list_hooks(plugin_root: Path) -> List[HookEntry]:
    """Combine hook declarations from plugin.json with files in ``hooks/``.

    Two declaration paths exist:
      1. ``plugin.json`` ``hooks`` field — ``{<EventName>: [{matcher, hooks: [...]}]}``
      2. Bare scripts in ``<plugin_root>/hooks/`` — event unknown, rendered
         as ``"—"`` so the table still surfaces them.
    """
    out: List[HookEntry] = []
    seen_names: set[str] = set()

    # Path 1: structured declaration in plugin.json.
    data = _read_plugin_json(plugin_root)
    hooks_field = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks_field, dict):
        for event, entries in hooks_field.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("hooks") or []
                if not isinstance(inner, list):
                    continue
                for h in inner:
                    if not isinstance(h, dict):
                        continue
                    name = h.get("command") or h.get("name") or h.get("type") or "(hook)"
                    name = str(name).strip() or "(hook)"
                    description = entry.get("matcher") or h.get("description")
                    out.append(
                        HookEntry(
                            name=name,
                            event=str(event),
                            description=str(description) if description else None,
                        )
                    )
                    seen_names.add(name)

    # Path 2: bare scripts in hooks/ directory not already covered.
    hooks_dir = plugin_root / "hooks"
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name in seen_names or str(p) in seen_names:
                continue
            out.append(HookEntry(name=p.name, event=None, description=None))
    return out


def _list_mcps(plugin_root: Path) -> List[McpEntry]:
    """Parse MCP server declarations from ``.mcp.json`` (or legacy
    ``mcp_servers.json``). Returns ``[(name, type, description)]``.

    Standard shape: ``{"mcpServers": {<name>: {<config>}}}``.

    Transport comes from ``<config>["type"]`` when the author declared one —
    that key is how both Claude Code's ``.mcp.json`` and the Agent Plugins
    ``mcp.json`` schema spell it, and an unrecognized value is surfaced as
    written rather than coerced into something we recognize.

    Only when ``type`` is absent do we infer from the config shape:
    ``command`` → ``stdio``, ``url`` → ``http``. A bare ``url`` is Streamable
    HTTP; HTTP+SSE is the deprecated predecessor transport, so labelling every
    URL server ``sse`` (as this did before) mislabels the common case.

    Description falls through to ``command`` / ``url`` so the operator at
    least sees what's launched.
    """
    candidates = [
        plugin_root / ".mcp.json",
        plugin_root / "mcp_servers.json",
    ]
    out: List[McpEntry] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                out.append(McpEntry(name=str(name)))
                continue
            declared = cfg.get("type")
            kind: Optional[str] = declared.strip() if isinstance(declared, str) and declared.strip() else None
            description: Optional[str] = None
            if "command" in cfg:
                kind = kind or "stdio"
                description = str(cfg.get("command"))
                args = cfg.get("args")
                if isinstance(args, list) and args:
                    description = description + " " + " ".join(str(a) for a in args)
            elif "url" in cfg:
                kind = kind or "http"
                description = str(cfg.get("url"))
            out.append(
                McpEntry(
                    name=str(name),
                    type=kind,
                    description=description,
                )
            )
        break  # first found candidate wins
    return out


def _bundle_size(plugin_root: Path) -> Optional[int]:
    """Sum of served file sizes under ``plugin_root``. None if path missing.

    Counts exactly what ``_walk_files`` lists — see there for why the
    unserved paths are skipped.
    """
    if not plugin_root.is_dir():
        return None
    total = 0
    for p in plugin_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            if is_unserved_path(p.relative_to(plugin_root).parts):
                continue
            total += p.stat().st_size
        except (OSError, ValueError):
            pass
    return total


def _walk_files(root: Path) -> List[FileEntry]:
    """Recursive file listing under ``root``.

    Returns sorted ``[FileEntry(path, size)]`` with paths relative to ``root``
    (forward-slash form for stable display). Directories are skipped — only
    leaf files appear, matching the way ``store_detail.html`` renders the
    Files section. Empty list if ``root`` is missing or not a directory.

    Paths the serve paths exclude (``marketplace_filter.is_unserved_path``:
    ``.git/**``, ``.agnes/**``, ``marketplace-metadata.json``) are skipped so
    the page describes what a user actually installs. This matters most for a
    root-source plugin (``source: "./"``), whose ``root`` IS the marketplace
    clone — an unfiltered walk lists every loose git object and inflates
    ``bundle_size`` by the whole VCS directory. Harmless at the other two
    call sites: a Store bundle's files go through ``_bundle_files``, and an
    inner skill dir through the same plugin walk, both of which already apply
    this exact exclusion.
    """
    if not root.is_dir():
        return []
    out: List[FileEntry] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(root).parts
            if is_unserved_path(rel_parts):
                continue
            out.append(FileEntry(path=Path(*rel_parts).as_posix(), size=p.stat().st_size))
        except (OSError, ValueError):
            continue
    return out


def _resolve_owner_display(
    owner_user_id: str,
    fallback: str,
) -> str:
    """Friendly owner display name — ``users.name → users.email → fallback``.

    Mirrors the inline lookup ``app/web/router.py::store_detail`` already does
    so the marketplace API surfaces the same string the Store page shows.

    Single-row variant for detail endpoints, routed through
    ``users_repo()`` so it stays correct on either state backend. List
    endpoints must use ``_load_users_display`` to avoid an N+1 against
    ``users``.
    """
    row = users_repo().get_by_id(owner_user_id)
    if not row:
        return fallback
    return row.get("name") or row.get("email") or fallback


def _load_users_display(
    user_ids: Iterable[str],
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Batch-fetch ``(name, email)`` for a set of user_ids — single round-trip.

    Returns a dict keyed by user_id. Use with ``_owner_display_from_map``
    inside list comprehensions to compose the same
    ``users.name → users.email → fallback`` resolution that
    ``_resolve_owner_display`` does per-row. Thin wrapper over
    ``users_repo().get_info_by_ids`` (already the N+1-avoidance batch
    lookup shared with Activity Center) reshaped to the ``(name, email)``
    tuple this module's callers expect.
    """
    ids = [u for u in {uid for uid in user_ids if uid}]
    if not ids:
        return {}
    info = users_repo().get_info_by_ids(ids)
    return {uid: (row.get("name"), row.get("email")) for uid, row in info.items()}


def _owner_display_from_map(
    users_display: Dict[str, Tuple[Optional[str], Optional[str]]],
    owner_user_id: str,
    fallback: str,
) -> str:
    """Resolve owner display from a prefetched map, mirroring
    ``_resolve_owner_display`` semantics."""
    row = users_display.get(owner_user_id)
    if not row:
        return fallback
    return row[0] or row[1] or fallback


def _get_plugin_row(
    conn: Optional[duckdb.DuckDBPyConnection],
    marketplace_id: str,
    plugin_name: str,
) -> Optional[dict]:
    """Fetch a single ``marketplace_plugins`` row as a dict, or ``None``.

    ``conn`` retained for signature stability; ignored. Looks up via the
    factory so DuckDB and PG paths share the same code.
    """
    rows = marketplace_plugins_repo().list_for_marketplace(marketplace_id)
    for row in rows:
        if row.get("name") == plugin_name:
            return row
    return None


def _require_visible_plugin(
    marketplace_id: str,
    plugin_name: str,
    *,
    detail: str = "plugin_not_found",
) -> dict:
    """Return the plugin row, 404ing when it is absent or admin-disabled.

    An admin-disabled plugin must be indistinguishable from a nonexistent one
    on every user-facing curated surface — for admins too (docs/marketplace.md:
    the only surface that still shows it is the /admin/marketplaces Details
    modal, served by app/api/marketplaces.py). The listing paths already
    filter ``admin_disabled`` in SQL (``list_with_filters`` /
    ``list_granted_for_groups``); this is the same rule for the single-plugin
    endpoints, which a caller holding a still-live ``resource_grants`` row
    could otherwise keep reading and installing from. ``detail`` lets each
    endpoint keep its own 404 vocabulary so the disabled case reads exactly
    like the nonexistent case on that endpoint.
    """
    row = marketplace_plugins_repo().get(marketplace_id, plugin_name)
    if row is None or row.get("admin_disabled"):
        raise HTTPException(status_code=404, detail=detail)
    return row


def _curated_plugin_root(
    marketplace_id: str,
    plugin_name: str,
    plugin_row: Optional[dict] = None,
) -> Optional[Path]:
    """On-disk root of a curated plugin, honoring the catalog's declared
    ``source`` — a root-source plugin (``source: "./"``) lives at the clone
    root, not under ``plugins/<name>/``. Delegates path containment to
    ``marketplace_filter._contained_plugin_dir`` (the same helper the serve
    paths use); returns ``None`` when there is no contained local directory
    (external dict source, hostile source, unsafe name)."""
    if plugin_row is None:
        plugin_row = _get_plugin_row(None, marketplace_id, plugin_name)
    source = _resolve_raw((plugin_row or {}).get("raw")).get("source")
    return _contained_plugin_dir(
        Path(get_marketplaces_dir()),
        marketplace_id,
        plugin_name,
        source=source,
    )


@router.get(
    "/curated/{marketplace_id}/{plugin_name}",
    response_model=PluginDetailResponse,
)
async def curated_detail(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the curated plugin detail + inner skill/agent/command/hook/mcp list.

    The 403 guard fires before this body runs (via ``require_resource_access``).
    A second ``get_current_user`` dependency is included so we still have the
    caller's user dict for the ``installed`` flag.
    """
    plugin_row = _require_visible_plugin(marketplace_id, plugin_name)

    _reject_unsafe_segment(marketplace_id, plugin_name)
    plugin_root = _curated_plugin_root(marketplace_id, plugin_name, plugin_row)
    skills = _list_inner_skills(plugin_root) if plugin_root else []
    agents = _list_inner_agents(plugin_root) if plugin_root else []

    # v32: enrich each skill/agent card with its marketplace-metadata cover photo
    # so the inner cards on the plugin detail page render the real image
    # instead of just initials. Read marketplace-metadata + mirror manifest once
    # (mtime cache makes the metadata read free on cache hit) and reuse for
    # all inner items in the plugin.
    inner_metadata = _read_metadata_cached(marketplace_id)
    inner_manifest = _load_mirror_manifest(marketplace_id)
    asset_version = _marketplace_asset_version(marketplace_id, conn)

    for s in skills:
        s.detail_url = f"/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{s.name}"
        s.cover_photo_url = _curated_inner_cover(
            marketplace_id,
            plugin_name,
            "skill",
            s.name,
            manifest=inner_manifest,
            metadata=inner_metadata,
            version=asset_version,
        )
    for a in agents:
        a.detail_url = f"/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{a.name}"
        a.cover_photo_url = _curated_inner_cover(
            marketplace_id,
            plugin_name,
            "agent",
            a.name,
            manifest=inner_manifest,
            metadata=inner_metadata,
            version=asset_version,
        )

    # Per-item telemetry — 1 query per plugin (not per item) via the bulk
    # loader. Same rollup tables the listing card / inner detail hero chip
    # read from. Adoption is inherited from the parent plugin's stack
    # count because inner items can't be installed standalone.
    inner_stats = reports_repo().inner_items_stats_by_parent("curated", plugin_name)
    parent_stack = _load_curated_stack_counts().get(
        (marketplace_id, plugin_name),
        0,
    )
    for s in skills:
        row = inner_stats.get((s.name, "skill")) or {}
        s.invocations_30d = int(row.get("invocations_30d") or 0)
        s.distinct_users_30d = int(row.get("distinct_users_30d") or 0)
        s.trend_pct = row.get("trend_pct")
        s.parent_stack_count = parent_stack
    for a in agents:
        row = inner_stats.get((a.name, "agent")) or {}
        a.invocations_30d = int(row.get("invocations_30d") or 0)
        a.distinct_users_30d = int(row.get("distinct_users_30d") or 0)
        a.trend_pct = row.get("trend_pct")
        a.parent_stack_count = parent_stack

    commands = _list_commands(plugin_root) if plugin_root else []
    hooks = _list_hooks(plugin_root) if plugin_root else []
    mcps = _list_mcps(plugin_root) if plugin_root else []

    subs, required = _curated_stack_sets(conn, user["id"])
    raw = plugin_row.get("raw") or {}
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}

    # v32: cover, video, and doc_links come from `marketplace-metadata.json` via the
    # sync pipeline (`src/marketplace.py::_refresh_plugin_cache`) — already in
    # served-URL form when written to the DB. We pass them through the
    # response model unchanged. Curator name + email come from the marketplace
    # registry and surface as the plugin's accountable owner; the upstream
    # `marketplace.json::author.name` is intentionally not used here so we
    # have a single source of truth for "who runs this".
    meta = _resolve_marketplace_meta(conn, marketplace_id)
    doc_link_entries: List[DocEntry] = []
    raw_links = plugin_row.get("doc_links")
    if isinstance(raw_links, list):
        for link in raw_links:
            if isinstance(link, dict) and link.get("name") and link.get("url"):
                doc_link_entries.append(DocEntry(name=str(link["name"]), url=str(link["url"])))

    # Plugin-level rich content (display_name, tagline, description_long_html,
    # use_cases, sample_interaction) — on-demand read from the working tree's
    # marketplace-metadata.json so curator edits land at next page refresh
    # without a sync cycle. Empty dict when curator hasn't filled any of the
    # new fields → PluginDetailResponse renders the historical fallback shape.
    enrichment = _curated_plugin_enrichment(marketplace_id, plugin_name)

    return PluginDetailResponse(
        source="curated",
        # Same claim `_curated_to_item` puts on this plugin's listing row.
        publisher_kind="organization",
        verification_state="none",
        marketplace_id=marketplace_id,
        marketplace_name=meta["name"],
        plugin_name=plugin_name,
        manifest_name=(raw.get("name") if isinstance(raw, dict) else None) or plugin_name,
        description=plugin_row.get("description"),
        version=plugin_row.get("version"),
        category=plugin_row.get("category"),
        author_name=meta["curator_name"] or OWNER_TODO_PLACEHOLDER,
        curator_email=meta["curator_email"],
        homepage=plugin_row.get("homepage"),
        cover_photo_url=plugin_row.get("cover_photo_url"),
        video_url=plugin_row.get("video_url"),
        bundle_size=_bundle_size(plugin_root) if plugin_root else None,
        released_at=_to_iso(plugin_row.get("created_at")),
        updated_at=_to_iso(plugin_row.get("updated_at")),
        installed=(marketplace_id, plugin_name) in subs,
        skills=skills,
        agents=agents,
        commands=commands,
        hooks=hooks,
        mcps=mcps,
        files=_walk_files(plugin_root) if plugin_root else [],
        docs=doc_link_entries,
        is_system=bool(plugin_row.get("is_system")),
        is_required=(marketplace_id, plugin_name) in required,
        **enrichment,
        telemetry=_build_telemetry("curated", plugin_name),
        # stack_count mirrors what the listing card shows ("N installed")
        # so the hero chip on this detail page renders the same figure.
        stack_count=_load_curated_stack_counts().get(
            (marketplace_id, plugin_name),
            0,
        ),
    )


@router.get(
    "/flea/{entity_id}/detail",
    response_model=PluginDetailResponse,
)
async def flea_detail(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Same shape as curated detail, sourced from a Store entity.

    Flea entities live at ``${DATA_DIR}/store/<entity_id>/plugin/`` —
    canonical Claude Code plugin tree, so the same parsers apply.
    """
    from app.api.store import _enforce_visibility
    from app.utils import get_store_dir

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    # Same gate as /api/store/entities/{id}: non-owner non-admin gets
    # 404 (not 403) for any non-approved entity. Without this, a user
    # who guesses an entity_id can still pull the bundle metadata
    # through the marketplace JSON feed even though it's quarantined
    # and excluded from the public listing.
    _enforce_visibility(entity, user, conn)

    plugin_root = Path(get_store_dir()) / entity_id / "plugin"

    if entity["type"] == "plugin":
        skills = _list_inner_skills(plugin_root)
        for s in skills:
            s.detail_url = f"/marketplace/flea/{entity_id}/skill/{s.name}"
        agents = _list_inner_agents(plugin_root)
        for a in agents:
            a.detail_url = f"/marketplace/flea/{entity_id}/agent/{a.name}"
        # Per-item telemetry — same shape as curated_detail. Adoption
        # inherits from the parent flea plugin's install_count (no
        # standalone install on inner items). v49 phase-5: rollup
        # `parent_plugin` for flea-plugin children carries the parent's
        # synthetic_name (= what Claude Code writes in the JSONL prefix).
        inner_stats = reports_repo().inner_items_stats_by_parent(
            "flea",
            entity["synthetic_name"],
        )
        flea_parent_stack = int(entity.get("install_count") or 0)
        for s in skills:
            row = inner_stats.get((s.name, "skill")) or {}
            s.invocations_30d = int(row.get("invocations_30d") or 0)
            s.distinct_users_30d = int(row.get("distinct_users_30d") or 0)
            s.trend_pct = row.get("trend_pct")
            s.parent_stack_count = flea_parent_stack
        for a in agents:
            row = inner_stats.get((a.name, "agent")) or {}
            a.invocations_30d = int(row.get("invocations_30d") or 0)
            a.distinct_users_30d = int(row.get("distinct_users_30d") or 0)
            a.trend_pct = row.get("trend_pct")
            a.parent_stack_count = flea_parent_stack
        commands = _list_commands(plugin_root)
        hooks = _list_hooks(plugin_root)
        mcps = _list_mcps(plugin_root)
    else:
        # skill / agent — inner structure isn't applicable; the dedicated
        # item-detail page will render those entities. We still return the
        # response so the frontend can decide which template to use.
        skills = []
        agents = []
        commands = []
        hooks = []
        mcps = []

    is_installed = user_store_installs_repo().is_installed(
        user["id"],
        entity_id,
    )

    cover_url: Optional[str] = None
    if entity.get("photo_path"):
        # ``?v=`` cache-busting fingerprint via ``version_no`` — see
        # ``app/api/store.py:get_entity_photo`` for the matching
        # ``Cache-Control: immutable`` header.
        cover_url = f"/api/store/entities/{entity_id}/photo?v={entity.get('version_no', 1)}"

    # Strip archive-rename suffix for human display; manifest_name keeps
    # the renamed-on-archive slug since that's what Claude Code resolves.
    from src.store_naming import strip_archive_suffix

    _flea_display_name = strip_archive_suffix(entity["name"])
    # v49 phase-3: read the stored synthetic_name (NOT NULL invariant).
    invocation = entity["synthetic_name"]

    # doc_paths is a JSON array of relative paths the uploader picked at upload
    # time; `app/api/store.py` serves them by basename via /api/store/.../docs/{filename}.
    docs: List[DocEntry] = []
    for raw_path in entity.get("doc_paths") or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        fname = Path(raw_path).name
        if not fname:
            continue
        docs.append(
            DocEntry(
                name=fname,
                url=f"/api/store/entities/{entity_id}/docs/{fname}",
            )
        )

    owner_display = _resolve_owner_display(
        entity["owner_user_id"],
        entity.get("owner_username") or "",
    )

    # Surface the latest submission verdict to the owner / admin so the
    # quarantine banner's auto-refresh JS has a signal to poll on.
    # Visibility alone is not enough because `blocked_llm` keeps the
    # entity at `visibility_status='pending'`.
    submission_status: Optional[str] = None
    is_owner = entity.get("owner_user_id") == user.get("id")
    is_admin_user = is_user_admin(user["id"], conn)
    if is_owner or is_admin_user:
        latest_sub = store_submissions_repo().latest_for_entity(entity_id)
        if latest_sub:
            submission_status = latest_sub.get("status")

    # Mirror POST /api/store/entities/{id}/install's gate so the detail page's
    # install button reflects what the server would actually do (#1178).
    from app.api.store import is_own_unflagged_private

    installable = (
        (entity.get("visibility_status") or "approved") == "approved"
        or is_admin_user
        or is_own_unflagged_private(entity, user.get("id") or "")
    )

    return PluginDetailResponse(
        source="flea",
        # Stored on the entity; same two fields the Library row and
        # `_flea_to_item` read for this entity.
        publisher_kind=entity.get("publisher_kind") or "user",
        verification_state=entity.get("verification_state") or "none",
        entity_id=entity_id,
        plugin_name=_flea_display_name,
        manifest_name=invocation,
        # v49 phase-2: surface the user-friendly title + short description
        # via the existing curated-side fields. JS heroTitle chain already
        # prefers `display_name`, and the hero-tagline element already
        # reads `d.tagline` — flea now feeds the same chain instead of
        # falling through to plugin_name (= kebab-case entity name).
        display_name=entity.get("title"),
        tagline=entity.get("tagline"),
        description=entity.get("description"),
        version=entity.get("version"),
        category=entity.get("category"),
        author_name=entity.get("owner_username"),
        owner_display=owner_display,
        homepage=None,
        cover_photo_url=cover_url,
        bundle_size=int(entity.get("file_size") or 0) or _bundle_size(plugin_root),
        install_count=int(entity.get("install_count") or 0),
        # Mirror what the listing card already does (see _build_marketplace_item
        # for flea): hero chip + telemetry visibility gate both read
        # `d.stack_count`, so populate it from entity.install_count rather
        # than letting it default to 0.
        stack_count=int(entity.get("install_count") or 0),
        released_at=_to_iso(entity.get("created_at")),
        updated_at=_to_iso(entity.get("updated_at")),
        installed=is_installed,
        skills=skills,
        agents=agents,
        commands=commands,
        hooks=hooks,
        mcps=mcps,
        files=_walk_files(plugin_root),
        docs=docs,
        visibility_status=entity.get("visibility_status") or "approved",
        installable=installable,
        submission_status=submission_status,
        # v49 phase-5: flea telemetry keyed by entity.synthetic_name
        # (rollup `name` column carries the post-rename keyspace, which
        # is the same string Claude Code writes in the JSONL local-part).
        telemetry=_build_telemetry("flea", entity["synthetic_name"]),
    )


@router.post(
    "/curated/{marketplace_id}/{plugin_name}/install",
    response_model=InstallActionResponse,
)
async def curated_install(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
    user: dict = Depends(get_current_user),
):
    """Subscribe the caller to a curated plugin (Model B opt-in).

    Idempotent — repeated calls are no-ops. The plugin must exist in
    ``marketplace_plugins``; otherwise 404. The RBAC guard already ensured
    the caller is allowed to install.
    """
    # Backend-aware: marketplace_plugins lives in Postgres on a PG-backed
    # instance; a raw DuckDB read here would 404 every plugin that exists.
    # Admin-disabled plugins are uninstallable for everyone — the RBAC guard
    # can pass (grants survive a disable) but the plugin must act nonexistent.
    _require_visible_plugin(marketplace_id, plugin_name)
    inserted = user_curated_subscriptions_repo().subscribe(
        user["id"],
        marketplace_id,
        plugin_name,
    )
    if inserted:
        _audit(
            user["id"],
            "marketplace.curated.install",
            f"plugin:{marketplace_id}/{plugin_name}",
        )
        _invalidate_etag()
    # The onboarding step is "Put knowledge in your stack" — a curated plugin
    # joining the stack is that, even though plugins keep their own resolver
    # and never pass through /api/stack/subscribe (design D1). Marked on every
    # successful call, not just the first insert, so readers who installed
    # before this existed tick the step on their next install.
    mark_journey(user.get("id"), stack_setup_done=True)
    return InstallActionResponse(installed=True)


@router.delete(
    "/curated/{marketplace_id}/{plugin_name}/install",
    status_code=204,
)
async def curated_uninstall(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
    user: dict = Depends(get_current_user),
):
    # v39: system plugins are mandatory for every user — refuse uninstall.
    # Backend-aware read (see curated_install) — raw DuckDB would miss the
    # is_system flag on a Postgres-backed instance.
    plugin = marketplace_plugins_repo().get(marketplace_id, plugin_name)
    if plugin and bool(plugin.get("is_system")):
        raise HTTPException(
            status_code=409,
            detail="cannot_uninstall_system_plugin",
        )

    # Group-scoped Required tier: a ``requirement='required'`` grant keeps
    # the plugin in every group member's served set regardless of the
    # subscription row, so uninstall would be a lie — the resolver would
    # keep serving it. Refuse, mirroring the is_system guard above.
    if (marketplace_id, plugin_name) in required_plugin_keys(None, user["id"]):
        raise HTTPException(
            status_code=409,
            detail="cannot_uninstall_required_plugin",
        )

    deleted = user_curated_subscriptions_repo().unsubscribe(
        user["id"],
        marketplace_id,
        plugin_name,
    )
    if deleted:
        _audit(
            user["id"],
            "marketplace.curated.uninstall",
            f"plugin:{marketplace_id}/{plugin_name}",
        )
        _invalidate_etag()


# ---------------------------------------------------------------------------
# Curated inner skill/agent detail
# ---------------------------------------------------------------------------


def _reject_unsafe_segment(*segments: str) -> None:
    """404 on a path segment that could escape its intended root.

    ``marketplace_id`` / ``plugin_name`` arrive as Starlette ``[^/]+`` path
    params — they can't contain ``/`` but CAN be ``..`` or carry ``\\``. Both
    are used verbatim to build the served root (``marketplaces_dir/<id>``,
    ``.../plugins/<name>``); a ``..`` there escapes the marketplaces dir and
    ``_safe_join`` then re-anchors on the ESCAPED root, so the traversal guard
    is defeated (audit L1). Enforce a strict slug and explicitly reject ``..``.

    The rule itself lives in ``src.marketplace.is_safe_plugin_name`` so this
    serve-time check, the ingest check (``read_plugins``), the path containment
    (``marketplace_filter._contained_plugin_dir``) and the asset mirror cannot
    drift apart. It deliberately does not strip, so an unstripped segment such
    as ``"acme "`` stays rejected here exactly as before.
    """
    for seg in segments:
        if not is_safe_plugin_name(seg or ""):
            raise HTTPException(status_code=404, detail="not_found")


def _safe_join(plugin_root: Path, *parts: str) -> Optional[Path]:
    """Join ``parts`` onto ``plugin_root`` and return the resolved path only if
    it actually lives under ``plugin_root``. Defends against ``..`` segments,
    Windows ``\\`` separators that slip past Starlette's ``[^/]+`` path-param
    regex, and symlinks planted inside a curated marketplace's git mirror that
    point outside the plugin's own tree.

    Returns ``None`` when the candidate doesn't exist, can't be resolved, or
    escapes ``plugin_root``. Callers translate that into a 404.
    """
    candidate = plugin_root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        root = plugin_root.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _read_inner(
    plugin_root: Path,
    sub: str,
    name: str,
    *,
    is_dir_layout: bool,
) -> Optional[tuple[str, str]]:
    """Return (text, relpath) for a skill (sub='skills', is_dir=True) or
    agent (sub='agents', is_dir=False) inside the plugin tree, or None if
    the file is missing or escapes ``plugin_root`` (see ``_safe_join``).
    """
    if is_dir_layout:
        candidate = _safe_join(plugin_root, sub, name, "SKILL.md")
    else:
        candidate = _safe_join(plugin_root, sub, f"{name}.md")
    if candidate is None:
        return None
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        rel = candidate.relative_to(plugin_root.resolve()).as_posix()
    except ValueError:
        rel = candidate.name
    return text, rel


def _curated_inner_parent_fields(
    conn: duckdb.DuckDBPyConnection,
    marketplace_id: str,
    plugin_name: str,
) -> Dict[str, Any]:
    """Pull the parent-plugin metadata that the redesigned skill/agent detail
    page renders in the hero badges, meta-row, and sidebar.

    Returns a dict with ``marketplace_name``, ``category``, ``parent_author_name``,
    ``parent_updated_at``. Missing fields fall through to safe defaults so the
    inner endpoint still succeeds when the parent row is absent (which would be
    a sync-skew bug, but shouldn't 500 the page).
    """
    plugin_row = _get_plugin_row(conn, marketplace_id, plugin_name) or {}
    _reject_unsafe_segment(marketplace_id, plugin_name)
    plugin_root = _curated_plugin_root(marketplace_id, plugin_name, plugin_row)
    meta = _resolve_marketplace_meta(conn, marketplace_id)
    # Pull the parent plugin's curator-friendly display name from the same
    # source the plugin detail hero uses (`_curated_plugin_enrichment`).
    # LRU-cached, so no per-request I/O cost.
    enrichment = _curated_plugin_enrichment(marketplace_id, plugin_name)
    return {
        "marketplace_name": meta["name"],
        "category": plugin_row.get("category"),
        "parent_author_name": meta["curator_name"] or OWNER_TODO_PLACEHOLDER,
        "parent_updated_at": _to_iso(plugin_row.get("updated_at")),
        "manifest_name": (resolve_manifest_name(plugin_root, fallback=plugin_name) if plugin_root else plugin_name),
        "parent_display_name": enrichment.get("display_name"),
    }


def _flea_inner_parent_fields(
    entity: dict,
) -> Dict[str, Any]:
    """Flea sibling of ``_curated_inner_parent_fields``: build the parent-plugin
    metadata block for an inner skill/agent that lives inside a flea plugin
    entity. Sourced entirely from ``store_entities`` columns — no curator
    enrichment file convention exists for flea bundles yet, so the same
    fallbacks the flea plugin detail hero uses (strip_archive_suffix on
    entity.name, owner display, entity.updated_at) populate the response.

    v49 phase-3: ``parent_display_name`` prefers the user-set ``title``
    column over the kebab-case ``name``. The frontend chain (breadcrumb,
    hero "part of …", sidebar "Parent plugin", helper "This skill is part
    of …") all read ``d.parent_display_name`` first, so a single source
    swap drives every surface to the friendly form.
    """
    from src.store_naming import strip_archive_suffix

    owner_display = _resolve_owner_display(
        entity["owner_user_id"],
        entity.get("owner_username") or "",
    )
    return {
        "marketplace_name": "Flea Market",
        "category": entity.get("category"),
        "parent_author_name": owner_display or OWNER_TODO_PLACEHOLDER,
        "parent_updated_at": _to_iso(entity.get("updated_at")),
        "manifest_name": entity["name"],
        "parent_display_name": entity.get("title") or strip_archive_suffix(entity["name"]),
    }


def _load_mirror_manifest(marketplace_id: str) -> Dict[Tuple[str, str], Any]:
    """Read the asset-mirror manifest for one marketplace, keyed by
    ``(plugin_name, url)``.

    Returns an empty dict when the cache directory or manifest doesn't exist
    (fresh install, marketplace never synced) — callers treat that as "no
    URL is mirrored" which collapses to the same path as a fetch failure.
    """
    from src.marketplace_asset_mirror import _load_manifest

    cache_dir = get_marketplace_cache_dir() / marketplace_id
    return _load_manifest(cache_dir)


def _resolve_external_via_mirror(
    marketplace_id: str,
    plugin_name: str,
    url: str,
    manifest: Dict[Tuple[str, str], Any],
    version: Optional[str] = None,
) -> Optional[str]:
    """Translate one external URL into the Agnes-served `/mirrored/` URL when
    the asset mirror successfully cached it for THIS plugin; ``None`` otherwise.

    Used by the inner-detail (skill/agent) enrichment to apply the same
    "drop entries Agnes can't deliver" rule that the plugin-level sync
    flow already enforces. When this returns None the caller drops the
    enrichment entry entirely (no broken external link surfaces in the UI).

    ``version`` is the cache-busting fingerprint appended as ``?v=`` —
    typically the 8-char prefix of ``marketplace_registry.last_commit_sha``.
    """
    entry = manifest.get((plugin_name, url))
    if entry is None or entry.status != "ok" or not entry.local:
        return None
    # Manifest stores `local` as `<plugin>/<rest>`; the /mirrored/ endpoint
    # expects just `<rest>` (the plugin segment is in the URL path). Same
    # transform as src/marketplace.py uses on the plugin-level path.
    rest = entry.local.split("/", 1)[1] if "/" in entry.local else entry.local
    return mirrored_url(marketplace_id, plugin_name, rest, version=version)


def _marketplace_asset_version(
    marketplace_id: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[str]:
    """8-char prefix of the cloned marketplace's git HEAD, or ``None``.

    Used as the ``?v=`` cache-busting fingerprint on every cover-photo URL
    composed at request time. Same upstream state → same SHA → browser keeps
    cached bytes; git fetch lands a new commit → fingerprint changes →
    browser refetches. Sync-time enrichment in ``src/marketplace.py``
    threads the same fingerprint into the DB-persisted ``cover_photo_url``
    for the grid, so request-time + sync-time URLs match for unchanged
    repos.
    """
    row = marketplace_registry_repo().get(marketplace_id)
    if not row:
        return None
    sha = (row.get("last_commit_sha") or "")[:8]
    return sha or None


def _safe_use_case(raw: Any) -> Optional[UseCase]:
    """Build a ``UseCase`` from curator JSON, skipping malformed entries.

    Curator-authored input — a missing or empty ``title`` / ``description`` /
    ``prompt`` returns ``None`` so the caller drops the card instead of
    500-ing the whole detail page on Pydantic's required-field validation.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    description = raw.get("description")
    prompt = raw.get("prompt")
    if not (title and description and prompt):
        return None
    return UseCase(title=title, description=description, prompt=prompt)


def _safe_sample_interaction(raw: Any) -> Optional[SampleInteraction]:
    """Build a ``SampleInteraction`` from curator JSON, skipping malformed input.

    Same rationale as :func:`_safe_use_case` — partial curator JSON should
    silently drop the section, not crash the endpoint.
    """
    from app.markdown_render import render_safe

    if not isinstance(raw, dict):
        return None
    user = raw.get("user")
    assistant = raw.get("assistant")
    if not (user and assistant):
        return None
    return SampleInteraction(
        user=user,
        assistant=assistant,
        assistant_html=render_safe(assistant),
    )


def _curated_inner_enrichment(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """Load marketplace-metadata.json sub-tree for a single skill/agent.

    Lives here (not in the sync pipeline) so curators can update marketplace-metadata
    on a working tree and see the change at the next page refresh, without
    waiting for a full plugin-cache rewrite. Read on every inner-detail
    request — marketplace-metadata.json is small enough that disk hit cost is
    negligible compared to the SKILL.md / agent.md frontmatter parse the
    endpoint already does.

    External URL handling matches the plugin-level sync flow:
    * cover_photo external → kept only when the asset mirror has a
      successful cached copy (URL resolves to /mirrored/...). Else dropped
      → the card / hero falls through to the gradient placeholder.
    * doc_links external → same; entries that aren't mirrored OK get
      dropped from the served list. Internal docs survive only when the
      file actually exists in the working tree.

    Returns a dict shaped for direct merge into the InnerDetailResponse:
    ``cover_photo_url`` (resolved served URL or None),
    ``video_url`` (str or None), ``docs`` (list of DocEntry).
    """
    from src.marketplace_metadata import resolve_inner_metadata

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    # Route through the mtime cache so skill / agent detail hits don't
    # re-parse the JSON on every request. Plugin listing already shares
    # this cache, so opening 5 inner-detail pages on a marketplace whose
    # metadata file hasn't changed = 0 disk reads beyond the first.
    metadata = _read_metadata_cached(marketplace_id)
    section_kind = "skills" if kind == "skill" else "agents"
    resolved = resolve_inner_metadata(metadata, plugin_name, section_kind, inner_name)
    if not resolved:
        return {"cover_photo_url": None, "video_url": None, "docs": []}

    manifest = _load_mirror_manifest(marketplace_id)

    cover_url: Optional[str] = None
    cover_ref = resolved.get("cover_photo_ref")
    if isinstance(cover_ref, tuple):
        ref_kind, target = cover_ref
        if ref_kind == "internal":
            local_path = repo_root / target
            if local_path.is_file():
                cover_url = internal_asset_url(
                    marketplace_id,
                    plugin_name,
                    target,
                    version=version,
                )
        elif ref_kind == "external":
            cover_url = _resolve_external_via_mirror(
                marketplace_id,
                plugin_name,
                target,
                manifest,
                version=version,
            )

    docs: List[DocEntry] = []
    for link in resolved.get("doc_links") or []:
        if not hasattr(link, "kind"):
            continue
        if link.kind == "internal":
            local_path = repo_root / link.path
            if not local_path.is_file():
                continue
            docs.append(
                DocEntry(
                    name=link.name,
                    url=(f"/api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{link.path}"),
                )
            )
        else:
            served = _resolve_external_via_mirror(
                marketplace_id,
                plugin_name,
                link.url,
                manifest,
            )
            if served is None:
                continue
            docs.append(DocEntry(name=link.name, url=served))

    from app.markdown_render import render_safe

    out: Dict[str, Any] = {
        "cover_photo_url": cover_url,
        "video_url": resolved.get("video_url"),
        "docs": docs,
    }
    # Rich user-facing fields — same shape as the plugin-level enrichment.
    # Presence check (``in``) rather than truthy check so the resolver's
    # contract is respected: if the resolver decided to include a key,
    # the API propagates it. Future falsy-but-valid fields (e.g. a bool
    # ``featured: false`` or numeric ``priority: 0``) inherit correctly
    # instead of silently falling back to the parent — the trap @cvrysanek
    # flagged in the multi-persona review. String fields stay truthy-
    # guarded by the resolver itself, so today's behaviour is unchanged.
    if "display_name" in resolved:
        out["display_name"] = resolved["display_name"]
    if "tagline" in resolved:
        out["tagline"] = resolved["tagline"]
    # Per-item category override — caller merges with the parent plugin's
    # category, preferring the override when set.
    if "category" in resolved:
        out["category"] = resolved["category"]
    if "description" in resolved:
        out["description_long_html"] = render_safe(resolved["description"])
    if "when_to_use" in resolved:
        out["when_to_use_html"] = render_safe(resolved["when_to_use"])
    if "invocation" in resolved:
        out["invocation"] = resolved["invocation"]

    use_cases_raw = resolved.get("use_cases") or []
    if use_cases_raw:
        cards = [_safe_use_case(uc) for uc in use_cases_raw]
        cards = [c for c in cards if c is not None]
        if cards:
            out["use_cases"] = cards
    sample = _safe_sample_interaction(resolved.get("sample_interaction"))
    if sample is not None:
        out["sample_interaction"] = sample
    return out


# Module-level cache for marketplace-metadata.json reads. Keyed by
# ``(marketplace_id, mtime_ns)`` so a curator's `git push` (which updates
# the file's mtime after the next sync's `git pull`) implicitly invalidates
# the cached parse. The 24-plugin listing endpoint reads from the same
# marketplace's metadata 24 times per page; without this cache that's 24
# disk reads + JSON parses per request. With cache: 1.
#
# OrderedDict + popitem(last=False) on overflow gives us a bounded LRU
# without a third-party dependency. Earlier versions used a per-marketplace
# eviction predicate that only swept stale entries for the CURRENT marketplace
# — at N>100 distinct marketplaces the inner predicate matched zero entries,
# so the cap silently failed and memory grew linearly. The bounded LRU here
# guarantees the dict size never exceeds _PLUGIN_METADATA_CACHE_MAX.
_PLUGIN_METADATA_CACHE_MAX = 256
_PLUGIN_METADATA_CACHE: "OrderedDict[Tuple[str, int], Dict[str, Any]]" = OrderedDict()


def _read_metadata_cached(marketplace_id: str) -> Dict[str, Any]:
    """Return the parsed marketplace-metadata.json for a given marketplace.

    Cached by mtime so curator edits land at next request without explicit
    invalidation. Missing / malformed files degrade to ``{}`` (delegated to
    :func:`src.marketplace_metadata.read_marketplace_metadata` which already
    swallows OSError / JSONDecodeError). Cache is bounded LRU; the oldest
    entry is dropped when the cap is reached.
    """
    from src.marketplace_metadata import (
        MARKETPLACE_METADATA_REL,
        read_marketplace_metadata,
    )

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    metadata_path = repo_root / MARKETPLACE_METADATA_REL
    try:
        mtime_ns = metadata_path.stat().st_mtime_ns
    except OSError:
        # File doesn't exist or unreadable — read_marketplace_metadata handles
        # the same case below; we just skip the cache lookup.
        return read_marketplace_metadata(repo_root)
    key = (marketplace_id, mtime_ns)
    cached = _PLUGIN_METADATA_CACHE.get(key)
    if cached is not None:
        # Touch the entry so the LRU bookkeeping treats it as recently used.
        _PLUGIN_METADATA_CACHE.move_to_end(key)
        return cached
    parsed = read_marketplace_metadata(repo_root)
    _PLUGIN_METADATA_CACHE[key] = parsed
    # Bounded LRU: drop oldest entries until we're back under the cap.
    while len(_PLUGIN_METADATA_CACHE) > _PLUGIN_METADATA_CACHE_MAX:
        _PLUGIN_METADATA_CACHE.popitem(last=False)
    return parsed


def _curated_plugin_enrichment(
    marketplace_id: str,
    plugin_name: str,
) -> Dict[str, Any]:
    """Load plugin-level rich content from marketplace-metadata.json.

    Returns a dict shaped for direct merge into ``PluginDetailResponse``:
    ``display_name``, ``tagline``, ``description_long_html``, ``use_cases``,
    ``sample_interaction`` — any subset of those keys may be missing when
    the curator hasn't filled the corresponding field.

    Markdown is rendered to safe HTML here (via
    :func:`app.markdown_render.render_safe`) so the template can inject the
    body with ``{{ x | safe }}`` without a client-side markdown library.

    This is the plugin-level analogue of ``_curated_inner_enrichment`` — both
    read on demand from the working tree so curator edits don't need a full
    sync cycle to land in the UI. The other persisted plugin-level fields
    (cover_photo_url, video_url, category, doc_links) continue to flow via
    ``marketplace_plugins`` rows written at sync time — only the NEW rich
    fields are read on-demand here.
    """
    from app.markdown_render import render_safe
    from src.marketplace_metadata import resolve_plugin_metadata

    metadata = _read_metadata_cached(marketplace_id)
    resolved = resolve_plugin_metadata(metadata, plugin_name)
    if not resolved:
        return {}

    out: Dict[str, Any] = {}
    # Presence check (`in`) so future falsy-but-valid resolver fields
    # (bool / int / "" with explicit-empty semantics) survive — see the
    # parallel comment in `_curated_inner_enrichment` for the trap rationale.
    if "display_name" in resolved:
        out["display_name"] = resolved["display_name"]
    if "tagline" in resolved:
        out["tagline"] = resolved["tagline"]
    if "description" in resolved:
        out["description_long_html"] = render_safe(resolved["description"])

    use_cases_raw = resolved.get("use_cases") or []
    if use_cases_raw:
        cards = [_safe_use_case(uc) for uc in use_cases_raw]
        cards = [c for c in cards if c is not None]
        if cards:
            out["use_cases"] = cards

    sample = _safe_sample_interaction(resolved.get("sample_interaction"))
    if sample is not None:
        out["sample_interaction"] = sample

    return out


def _curated_inner_cover(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
    manifest: Optional[Dict[Tuple[str, str], Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
) -> Optional[str]:
    """Cheap helper: just the cover URL for one inner item.

    Used by ``curated_detail`` to populate ``InnerItemSummary.cover_photo_url``
    on the parent-plugin's skills/agents card list. Called once per inner
    item; metadata + manifest are loaded once per plugin and passed in to
    avoid N disk reads.
    """
    from src.marketplace_metadata import resolve_inner_metadata

    if metadata is None:
        # Route through the mtime cache; this helper gets called once per
        # inner item on the parent-plugin's skills/agents card list, so
        # cache miss only happens on the first card.
        metadata = _read_metadata_cached(marketplace_id)
    if manifest is None:
        manifest = _load_mirror_manifest(marketplace_id)

    section_kind = "skills" if kind == "skill" else "agents"
    resolved = resolve_inner_metadata(metadata, plugin_name, section_kind, inner_name)
    cover_ref = resolved.get("cover_photo_ref") if resolved else None
    if not isinstance(cover_ref, tuple):
        return None
    ref_kind, target = cover_ref
    if ref_kind == "internal":
        local_path = Path(get_marketplaces_dir()) / marketplace_id / target
        if local_path.is_file():
            return internal_asset_url(
                marketplace_id,
                plugin_name,
                target,
                version=version,
            )
        return None
    return _resolve_external_via_mirror(
        marketplace_id,
        plugin_name,
        target,
        manifest,
        version=version,
    )


@router.get(
    "/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    response_model=InnerDetailResponse,
)
async def curated_skill_detail(
    marketplace_id: str,
    plugin_name: str,
    skill_name: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _reject_unsafe_segment(marketplace_id, plugin_name)
    _require_visible_plugin(marketplace_id, plugin_name, detail="skill_not_found")
    plugin_root = _curated_plugin_root(marketplace_id, plugin_name)
    if plugin_root is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    res = _read_inner(plugin_root, "skills", skill_name, is_dir_layout=True)
    skill_dir = _safe_join(plugin_root, "skills", skill_name)
    if res is None or skill_dir is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    parent = _curated_inner_parent_fields(conn, marketplace_id, plugin_name)
    enrichment = _curated_inner_enrichment(
        marketplace_id,
        plugin_name,
        "skill",
        skill_name,
        version=_marketplace_asset_version(marketplace_id, conn),
    )
    # Merge parent fallback fields with curator overrides BEFORE unpacking
    # — Python function-call `**a, **b` with overlapping keys raises
    # TypeError, it doesn't merge like a literal dict does. Today only
    # `category` overlaps (both parent + enrichment may set it), but the
    # explicit merge keeps the unpack future-proof against any new field
    # added to both layers.
    merged = {**parent, **enrichment}
    telemetry = reports_repo().inner_item_stats(
        "curated",
        parent_plugin=plugin_name,
        name=skill_name,
        item_type="skill",
    )
    parent_stack_count = _load_curated_stack_counts().get(
        (marketplace_id, plugin_name),
        0,
    )
    return InnerDetailResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        # Inherited from the parent curated plugin (admin-registered).
        publisher_kind="organization",
        verification_state="none",
        kind="skill",
        name=fm.get("name") or skill_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=_bundle_size(skill_dir),
        files=_walk_files(skill_dir),
        telemetry=telemetry,
        parent_stack_count=parent_stack_count,
        **merged,
    )


@router.get(
    "/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    response_model=InnerDetailResponse,
)
async def curated_agent_detail(
    marketplace_id: str,
    plugin_name: str,
    agent_name: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _reject_unsafe_segment(marketplace_id, plugin_name)
    _require_visible_plugin(marketplace_id, plugin_name, detail="agent_not_found")
    plugin_root = _curated_plugin_root(marketplace_id, plugin_name)
    if plugin_root is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    res = _read_inner(plugin_root, "agents", agent_name, is_dir_layout=False)
    agent_path = _safe_join(plugin_root, "agents", f"{agent_name}.md")
    if res is None or agent_path is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    # Agents are flat single-file .md — bundle = file size, files = one entry.
    try:
        agent_size = agent_path.stat().st_size
    except OSError:
        agent_size = 0
    parent = _curated_inner_parent_fields(conn, marketplace_id, plugin_name)
    enrichment = _curated_inner_enrichment(
        marketplace_id,
        plugin_name,
        "agent",
        agent_name,
        version=_marketplace_asset_version(marketplace_id, conn),
    )
    # See curated_skill_detail above — explicit merge avoids the
    # TypeError that `**parent, **enrichment` raises when both supply
    # an overlapping key (e.g. `category`).
    merged = {**parent, **enrichment}
    telemetry = reports_repo().inner_item_stats(
        "curated",
        parent_plugin=plugin_name,
        name=agent_name,
        item_type="agent",
    )
    parent_stack_count = _load_curated_stack_counts().get(
        (marketplace_id, plugin_name),
        0,
    )
    return InnerDetailResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        # Inherited from the parent curated plugin (admin-registered).
        publisher_kind="organization",
        verification_state="none",
        kind="agent",
        name=fm.get("name") or agent_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=agent_size,
        files=[FileEntry(path=f"{agent_name}.md", size=agent_size)],
        telemetry=telemetry,
        parent_stack_count=parent_stack_count,
        **merged,
    )


@router.get(
    "/flea/{entity_id}/skill/{skill_name}",
    response_model=InnerDetailResponse,
)
async def flea_skill_detail(
    entity_id: str,
    skill_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Inner skill detail for a skill nested inside a flea plugin entity.

    Mirror of ``curated_skill_detail`` for the flea bundle root layout
    (``${DATA_DIR}/store/<entity_id>/plugin/``). Visibility gate matches
    the standalone flea_detail handler — owner / admin see quarantined
    entities, everyone else gets 404.
    """
    from app.api.store import _enforce_visibility
    from app.utils import get_store_dir

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    _enforce_visibility(entity, user, conn)
    plugin_root = Path(get_store_dir()) / entity_id / "plugin"
    res = _read_inner(plugin_root, "skills", skill_name, is_dir_layout=True)
    skill_dir = _safe_join(plugin_root, "skills", skill_name)
    if res is None or skill_dir is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    parent = _flea_inner_parent_fields(entity)
    # v49 phase-5: rollup `parent_plugin` carries the parent's synthetic_name.
    telemetry = reports_repo().inner_item_stats(
        "flea",
        parent_plugin=entity["synthetic_name"],
        name=skill_name,
        item_type="skill",
    )
    return InnerDetailResponse(
        marketplace_id="",
        plugin_name=entity["name"],
        # Inherited from the parent flea entity — an inner asset is not
        # separately publishable, so it cannot carry its own claim.
        publisher_kind=entity.get("publisher_kind") or "user",
        verification_state=entity.get("verification_state") or "none",
        kind="skill",
        name=fm.get("name") or skill_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=_bundle_size(skill_dir),
        files=_walk_files(skill_dir),
        telemetry=telemetry,
        parent_stack_count=int(entity.get("install_count") or 0),
        **parent,
    )


@router.get(
    "/flea/{entity_id}/agent/{agent_name}",
    response_model=InnerDetailResponse,
)
async def flea_agent_detail(
    entity_id: str,
    agent_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Inner agent detail for an agent nested inside a flea plugin entity.

    Mirror of ``curated_agent_detail``. Agents are flat single-file .md
    so ``bundle_size`` is the file size and ``files`` is a single entry.
    """
    from app.api.store import _enforce_visibility
    from app.utils import get_store_dir

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    _enforce_visibility(entity, user, conn)
    plugin_root = Path(get_store_dir()) / entity_id / "plugin"
    res = _read_inner(plugin_root, "agents", agent_name, is_dir_layout=False)
    agent_path = _safe_join(plugin_root, "agents", f"{agent_name}.md")
    if res is None or agent_path is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    try:
        agent_size = agent_path.stat().st_size
    except OSError:
        agent_size = 0
    parent = _flea_inner_parent_fields(entity)
    # v49 phase-5: rollup `parent_plugin` carries the parent's synthetic_name.
    telemetry = reports_repo().inner_item_stats(
        "flea",
        parent_plugin=entity["synthetic_name"],
        name=agent_name,
        item_type="agent",
    )
    return InnerDetailResponse(
        marketplace_id="",
        plugin_name=entity["name"],
        # Inherited from the parent flea entity — an inner asset is not
        # separately publishable, so it cannot carry its own claim.
        publisher_kind=entity.get("publisher_kind") or "user",
        verification_state=entity.get("verification_state") or "none",
        kind="agent",
        name=fm.get("name") or agent_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=agent_size,
        files=[FileEntry(path=f"{agent_name}.md", size=agent_size)],
        telemetry=telemetry,
        parent_stack_count=int(entity.get("install_count") or 0),
        **parent,
    )


# ---------------------------------------------------------------------------
# Asset / doc / mirrored serving endpoints (v32)
# ---------------------------------------------------------------------------
#
# Three sibling endpoints that serve the binary content referenced from
# `marketplace-metadata.json`. All three:
#
#   * are gated by `require_resource_access(MARKETPLACE_PLUGIN, "{mp}/{plugin}")`
#     so a user without RBAC can't side-load assets even with a direct URL,
#   * resolve a candidate path with `Path.resolve(strict=True)` and verify the
#     result lives under the expected root via `is_relative_to()` — defense
#     against `..` / absolute paths / symlinks pointing out of the tree,
#   * use FastAPI's `FileResponse` so Content-Type detection comes from the
#     stdlib mimetypes module (good enough for the allowlisted set; binary
#     fallback for anything we don't recognize).


def _doc_disposition(filename: str) -> dict:
    """Force-download headers for the /doc and /mirrored doc paths.

    Browsers honor the frontend's `download` attribute only for same-origin
    URLs; the explicit Content-Disposition header makes the download
    behavior reliable regardless of how the link was opened (right-click →
    open, programmatic fetch, curl, …). Filename is the basename so the
    browser save dialog shows something readable.
    """
    safe = Path(filename).name or "download"
    return {"Content-Disposition": f'attachment; filename="{safe}"'}


# Mapping from validated image extension to the Content-Type we serve.
# Pinned (not stdlib mimetypes) so an unexpected file with a known extension
# can't push us into ``text/html`` territory — this endpoint NEVER serves
# anything but image bytes labelled as image bytes.
_ASSET_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Defense-in-depth headers applied to every /asset/ response. ``nosniff``
# stops browsers from second-guessing our Content-Type; the strict CSP is
# a belt-and-suspenders block — even if a future regression let HTML
# through, the browser still won't execute scripts/iframes/etc.
#
# Cache-Control: cover photos are render-blocking on the /marketplace grid
# (12-20+ per page). Bytes are paired with a ``?v=<commit-sha8>`` URL
# fingerprint (built in src/marketplace_urls.py, populated at sync time
# from marketplace_registry.last_commit_sha) so aggressive caching is safe:
# upstream repo unchanged → same fingerprint → browser keeps the cached
# bytes; sync that pulls new commits → new fingerprint → fresh fetch.
# 30-day max-age + immutable means browsers also skip the conditional GET
# on F5 refresh (Ctrl+F5 still bypasses cache).
_ASSET_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    "Cache-Control": "public, max-age=2592000, immutable",
}


@router.get("/curated/{marketplace_id}/{plugin_name}/asset/{path:path}")
async def curated_asset(
    marketplace_id: str,
    plugin_name: str,
    path: str,
    _user: dict = Depends(get_current_user),
):
    """Serve an internal image asset from the cloned marketplace working tree.

    Paths are repo-root-relative — ``{path}`` may be e.g.
    ``.agnes/cover.png`` or ``plugins/foo/icon.png``.

    **Auth model: login-only, no per-plugin RBAC.** Cover photos are
    curator-designed marketing visuals — they exist specifically to be seen
    and carry no PII / source / secrets. The previous
    ``require_resource_access`` check serialized every image request through
    a DuckDB join under ``_system_db_lock``, making the /marketplace grid
    (12-20 cover photos per render) pay N round-trips of auth+RBAC cost in
    sequence. Login (``get_current_user``) still required → no
    unauthenticated public access. See CHANGELOG entry under "Security" for
    the threat-model rationale.

    **Image-only by contract.** The endpoint is the source of cover photos
    referenced from ``marketplace-metadata.json`` and from inner skill / agent
    cards. A curator who could land an arbitrary file in the cloned repo
    (HTML, JS, SVG with inline ``<script>``) would otherwise have a
    same-origin XSS via this endpoint, since the response shares the
    cookie scope with ``/admin`` and ``/api/me/*``. Two layered checks:

    1. Extension must be in :data:`src.marketplace_asset_validation.IMAGE_EXTENSIONS`
       (``.png``/``.jpg``/``.jpeg``/``.webp``); anything else → 415.
    2. ``Content-Type`` is pinned from the extension table (not stdlib
       mimetypes), so the response is never served as ``text/html`` even
       if mimetypes were misconfigured.

    Magic-bytes body validation that previously ran per request was dropped
    after the perf audit found it re-read the file just to discard the
    bytes (``FileResponse`` reads it again to stream). The repo is fetched
    via ``git pull`` from a trusted upstream the operator registered in
    ``marketplace_registry`` — accepting curator content at sync time is
    the layer where adversarial-payload detection belongs, not at every
    GET. Extension + Content-Type pinning + path-traversal guard remain.

    SVG is intentionally not in the allowlist — ``<script>`` inside SVG
    executes in the browser. ``X-Content-Type-Options: nosniff`` plus a
    strict CSP harden the response further.

    Inline rendering (no ``Content-Disposition``) — covers display in
    ``<img>``, not as a download.
    """
    from src.marketplace_asset_validation import IMAGE_EXTENSIONS

    _reject_unsafe_segment(marketplace_id)
    # NOT gated on ``admin_disabled`` — deliberately. This endpoint carries no
    # per-plugin RBAC at all (see the auth model above): every logged-in caller
    # can already fetch any plugin's cover art, grant or no grant, because the
    # content is curator-designed marketing visuals with no PII / source /
    # secrets. A disabled-plugin check would therefore hide nothing an
    # unauthorized viewer could not already read, while costing one serialized
    # DuckDB round-trip per image on a render-blocking path this endpoint was
    # explicitly stripped of DB work for (12-20 covers per /marketplace grid).
    # Disabled plugins appear on no listing, so the URL is only reachable by a
    # caller who already knows it. The surfaces that serve plugin CONTENT or
    # STATE — detail, install, skill/agent detail, doc — are gated; see
    # ``_require_visible_plugin``. Pinned by
    # tests/test_admin_disabled_curated_surfaces.py.
    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _safe_join(repo_root, path)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="asset_not_found")

    ext = safe.suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_asset_extension: {ext or '(none)'}",
        )
    return FileResponse(
        safe,
        media_type=_ASSET_CONTENT_TYPE[ext],
        headers=_ASSET_SECURITY_HEADERS,
    )


@router.get("/curated/{marketplace_id}/{plugin_name}/doc/{path:path}")
async def curated_doc(
    marketplace_id: str,
    plugin_name: str,
    path: str,
    _user: dict = Depends(
        require_resource_access(
            ResourceType.MARKETPLACE_PLUGIN,
            "{marketplace_id}/{plugin_name}",
        )
    ),
):
    """Serve an internal doc path from the cloned marketplace working tree.

    Same path-traversal guard as ``/asset/``. Adds an allowlist check — the
    doc endpoint refuses to serve a file whose extension isn't in the
    documented PDF / Markdown / plain text set (HTTP 415). Defense-in-depth
    even though the marketplace-metadata parser already rejects out-of-allowlist
    extensions during the doc_link parse — a curator who edits the working
    tree directly (or whose JSON survived parsing because of a generic
    extension match elsewhere) shouldn't be able to land a .docx through
    a re-served doc URL.

    Force-download via Content-Disposition: attachment — clicking a doc
    link in the UI saves the file to disk rather than opening it in a tab.
    """
    from src.marketplace_asset_validation import DOC_EXTENSIONS

    _reject_unsafe_segment(marketplace_id, plugin_name)
    _require_visible_plugin(marketplace_id, plugin_name, detail="doc_not_found")
    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _safe_join(repo_root, path)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="doc_not_found")
    # M2: the RBAC grant is for THIS plugin, but `path` is repo-root-relative and
    # could point into a DIFFERENT plugin's tree. If the resolved doc lives under
    # plugins/<other>/, require <other> == the granted plugin_name. Files outside
    # any plugins/<X>/ dir (marketplace-shared, e.g. .agnes/) stay accessible —
    # they are not another plugin's private content. Without this, a grant to one
    # plugin reads any sibling plugin's docs in the same marketplace (audit M2).
    try:
        rel = safe.resolve().relative_to((repo_root / "plugins").resolve())
        if rel.parts and rel.parts[0] != plugin_name:
            raise HTTPException(status_code=404, detail="doc_not_found")
    except ValueError:
        pass  # not under plugins/<X>/ — marketplace-shared, allowed
    if safe.suffix.lower() not in DOC_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_doc_extension: {safe.suffix.lower() or '(none)'}",
        )
    return FileResponse(safe, headers=_doc_disposition(safe.name))


@router.get("/curated/{marketplace_id}/{plugin_name}/mirrored/{key:path}")
async def curated_mirrored(
    marketplace_id: str,
    plugin_name: str,
    key: str,
    _user: dict = Depends(get_current_user),
):
    """Serve a mirrored external asset from the marketplace cache.

    ``key`` is the ``local`` relpath stored in the cache manifest minus the
    leading ``<plugin>/`` prefix — the mirror writes files at
    ``${DATA_DIR}/marketplace-cache/<slug>/<plugin>/<rest>``, and this
    endpoint expects ``{plugin}/{rest}`` so the same path-resolution check
    catches escapes whether the caller provided ``cover.png`` or
    ``docs/abc-setup.pdf``.

    Doc-shaped paths (key starting with ``docs/``) get the same force-download
    treatment as the internal /doc/ endpoint. Cover photos under the cache
    root render inline so the <img> tag works.

    **Auth model: login-only for cover photos**, same rationale as
    ``curated_asset`` above. The doc branch below is still gated by login
    only at this point — RBAC was dropped here too because the mirrored
    cache is just the cover-photo serving complement, not user-facing
    document distribution. Tighter doc gating lives on the separate
    ``curated_doc`` endpoint which retains ``require_resource_access``.
    """
    _reject_unsafe_segment(marketplace_id, plugin_name)
    # Not gated on ``admin_disabled``, for the same reason as ``curated_asset``
    # — this is that endpoint's mirrored-cache complement and serves the same
    # grid cover photos under the same login-only, no-RBAC model.
    cache_root = get_marketplace_cache_dir() / marketplace_id / plugin_name
    if not cache_root.exists():
        raise HTTPException(status_code=404, detail="mirror_cache_missing")
    safe = _safe_join(cache_root, key)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="mirrored_asset_not_found")
    if key.startswith("docs/"):
        return FileResponse(safe, headers=_doc_disposition(safe.name))
    # Cover photos: aggressive cache. URL fingerprint via ``?v=`` from
    # src/marketplace.py sync enrich keeps cache coherent across upstream
    # commits.
    return FileResponse(
        safe,
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )
