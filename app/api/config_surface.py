"""Read-only config-surface introspection endpoint.

GET /api/admin/config-surface  (require_admin)

Returns the complete per-instance configuration surface in one call:

- ``knobs``: for each ``get_*`` resolver in ``app/instance_config.py`` —
  ``{key, resolver, env_var, yaml_path, default, current_value, source}``
  where ``source ∈ {env, yaml, default}``.
- ``initial_workspace``: ``{url, branch, last_sync_sha}`` or null when
  no template is registered.
- ``marketplaces``: ``[{name, url}]`` for every registered marketplace.
- ``infra_repo_url``: resolved value of the ``AGNES_INFRA_REPO_URL`` knob.

Computed from existing live reads (``instance_config`` resolvers,
``marketplace_registry``, ``initial_workspace`` config) — no new table.
This is the machine-readable form of ``docs/CONFIGURATION.md``; the
built-in ``agnes-operator`` plugin instructs operator Claudes to call
this tool to get instance-accurate pointers rather than hardcoding them.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# Knob catalogue — each entry maps a get_* resolver to its env var + yaml
# path + default, mirroring the table in docs/CONFIGURATION.md.
# Keeps this file in sync with the config reference: when a new knob is
# added to instance_config.py and documented in CONFIGURATION.md, add a
# corresponding entry here so it appears in the introspection output.
# ---------------------------------------------------------------------------

_KNOB_CATALOGUE: list[dict[str, Any]] = [
    # Branding & UI
    {
        "key": "instance_name",
        "resolver": "get_instance_name",
        "env_var": None,
        "yaml_path": "instance.name",
        "default": "AI Harness",
    },
    {
        "key": "instance_subtitle",
        "resolver": "get_instance_subtitle",
        "env_var": None,
        "yaml_path": "instance.subtitle",
        "default": "",
    },
    {
        "key": "instance_copyright",
        "resolver": "get_instance_copyright",
        "env_var": "AGNES_INSTANCE_COPYRIGHT",
        "yaml_path": "instance.copyright",
        "default": "",
    },
    {
        "key": "instance_brand",
        "resolver": "get_instance_brand",
        "env_var": "AGNES_INSTANCE_BRAND",
        "yaml_path": "instance.brand",
        "default": "Agnes",
    },
    {
        # Default derives from get_instance_brand() at runtime; "Agnes" is the
        # stock value (same derived-knob caveat as workspace_dir_name).
        "key": "instance_brand_short",
        "resolver": "get_instance_brand_short",
        "env_var": "AGNES_INSTANCE_BRAND_SHORT",
        "yaml_path": "instance.brand_short",
        "default": "Agnes",
    },
    {
        "key": "instance_logo_svg",
        "resolver": "get_instance_logo_svg",
        "env_var": "AGNES_INSTANCE_LOGO_SVG",
        "yaml_path": "instance.logo_svg",
        "default": "",
    },
    {
        # Only consulted when `instance_logo_svg` is set: with no custom lockup
        # the rail's collapsed strip renders the built-in orb, which is already
        # a mark. Unset with a lockup set = a derived initial (rail.css).
        "key": "instance_logo_mark_svg",
        "resolver": "get_instance_logo_mark_svg",
        "env_var": "AGNES_INSTANCE_LOGO_MARK_SVG",
        "yaml_path": "instance.logo_mark_svg",
        "default": "",
    },
    {
        # Unset resolves the built-in `img/agnes-orb.png` through `static_url()`,
        # so the value is the SERVED url (`/static/img/agnes-orb.png`) carrying a
        # `?v=<mtime>` cache-buster. The default is declared as that url without
        # the buster — a stable description of the knob rather than of one
        # build's mtime — and `cache_busted` tells `_source_for` to compare with
        # the suffix stripped. Without it the value would always differ from the
        # default and a clean instance would report `source: "yaml"` for a
        # favicon nobody configured, exactly the trap documented on
        # `instance_theme` below. What the operator SETS is still the bare path
        # (`instance.favicon: img/brand.png`); `yaml_path` is where they set it.
        "key": "instance_favicon",
        "cache_busted": True,
        "resolver": "get_instance_favicon",
        "env_var": "AGNES_INSTANCE_FAVICON",
        "yaml_path": "instance.favicon",
        "default": "/static/img/agnes-orb.png",
    },
    {
        # "paper" since Wave 0 (2026-08) — it is what get_instance_theme()
        # returns on an instance that configures nothing (the `redesign`
        # preset's implied default). This value is not cosmetic: `_source_for`
        # infers `yaml` from `current != default`, so leaving the old "blue"
        # here made a clean instance report `source: "yaml"` for a theme
        # nobody had configured — and this endpoint feeds the operator
        # tooling, which would then state the theme was set deliberately.
        "key": "instance_theme",
        "resolver": "get_instance_theme",
        "env_var": "AGNES_INSTANCE_THEME",
        "yaml_path": "instance.theme",
        "default": "paper",
    },
    {
        "key": "workspace_dir_name",
        "resolver": "get_workspace_dir_name",
        "env_var": "AGNES_WORKSPACE_DIR_NAME",
        "yaml_path": "instance.workspace_dir",
        "default": "",
    },
    {
        # False since the admin cleanup retired the Studio surface — keep this
        # in step with the switch's `default`, or `_source_for` infers `yaml`
        # from `current != default` and reports a Studio nobody configured as
        # deliberately set (the trap documented on `instance_theme` above).
        "key": "studio_enabled",
        "resolver": "get_studio_enabled",
        "env_var": "AGNES_STUDIO_ENABLED",
        "yaml_path": "studio.enabled",
        "default": False,
    },
    {
        "key": "news_enabled",
        "resolver": "get_news_enabled",
        "env_var": "AGNES_NEWS_ENABLED",
        "yaml_path": "features.news_enabled",
        "default": False,
    },
    {
        "key": "knowledge_digests_ui_enabled",
        "resolver": "get_knowledge_digests_ui_enabled",
        "env_var": "AGNES_KNOWLEDGE_DIGESTS_ENABLED",
        "yaml_path": "features.knowledge_digests_enabled",
        "default": False,
    },
    {
        "key": "contribute_skill_enabled",
        "resolver": "get_contribute_skill_enabled",
        "env_var": "AGNES_CONTRIBUTE_SKILL_ENABLED",
        "yaml_path": "features.contribute_skill_enabled",
        "default": False,
    },
    {
        "key": "store_moderation_enabled",
        "resolver": "get_store_moderation_enabled",
        "env_var": "AGNES_STORE_MODERATION_ENABLED",
        "yaml_path": "features.store_moderation_enabled",
        "default": False,
    },
    {
        "key": "agent_profiles_enabled",
        "resolver": "get_agent_profiles_enabled",
        "env_var": "AGNES_AGENT_PROFILES_ENABLED",
        "yaml_path": "agent_profiles.enabled",
        "default": True,
    },
    # Onboarding & /home
    {
        "key": "home_route",
        "resolver": "get_home_route",
        "env_var": "AGNES_HOME_ROUTE",
        "yaml_path": "instance.home_route",
        "default": "/dashboard",
    },
    {
        "key": "home_automode_visibility",
        "resolver": "get_home_automode_visibility",
        "env_var": "AGNES_HOME_SHOW_AUTOMODE",
        "yaml_path": "instance.home.show_automode",
        "default": True,
    },
    {
        "key": "home_status_frame_visibility",
        "resolver": "get_home_status_frame_visibility",
        "env_var": "AGNES_HOME_SHOW_STATUS_FRAME",
        "yaml_path": "instance.home.show_status_frame",
        "default": True,
    },
    {
        "key": "instance_overview",
        "resolver": "get_instance_overview",
        "env_var": "AGNES_INSTANCE_OVERVIEW",
        "yaml_path": "instance.overview",
        "default": "",
    },
    {
        "key": "instance_support",
        "resolver": "get_instance_support",
        "env_var": "AGNES_INSTANCE_SUPPORT",
        "yaml_path": "instance.support",
        "default": "",
    },
    {
        "key": "instance_custom_preamble",
        "resolver": "get_instance_custom_preamble",
        "env_var": "AGNES_INSTANCE_CUSTOM_PREAMBLE",
        "yaml_path": "instance.custom_preamble",
        "default": "",
    },
    {
        "key": "instance_admin_email",
        "resolver": "get_instance_admin_email",
        "env_var": "AGNES_INSTANCE_ADMIN_EMAIL",
        "yaml_path": "instance.admin_email",
        "default": "",
    },
    {
        "key": "infra_repo_url",
        "resolver": "get_infra_repo_url",
        "env_var": "AGNES_INFRA_REPO_URL",
        "yaml_path": "instance.infra_repo_url",
        "default": "",
    },
    {
        "key": "sync_interval",
        "resolver": "get_sync_interval",
        "env_var": None,
        "yaml_path": "instance.sync_interval",
        "default": "1 hour",
    },
    # Connector pre-provisioning
    {
        "key": "atlassian_base_url",
        "resolver": "get_atlassian_base_url",
        "env_var": "AGNES_ATLASSIAN_BASE_URL",
        "yaml_path": "instance.atlassian.base_url",
        "default": "",
    },
    # Data source, auth & structural
    {
        "key": "data_source_type",
        "resolver": "get_data_source_type",
        "env_var": "DATA_SOURCE",
        "yaml_path": "data_source.type",
        "default": "local",
    },
    {
        "key": "public_url",
        "resolver": "get_public_url",
        "env_var": "PUBLIC_URL",
        "yaml_path": "server.public_url",
        "default": "",
    },
    {
        "key": "slack_transport",
        "resolver": "get_slack_transport",
        "env_var": "SLACK_TRANSPORT",
        "yaml_path": "chat.slack.transport",
        "default": "http",
    },
]


def _strip_cache_buster(value: Any) -> Any:
    """Drop a ``?v=<mtime>`` suffix so a static-asset knob can be compared to
    the bare asset path its catalogue entry declares as the default."""
    if isinstance(value, str) and "?v=" in value:
        return value.split("?v=", 1)[0]
    return value


def _source_for(
    env_var: Optional[str],
    resolver_name: str,
    current_value: Any,
    default: Any,
    cache_busted: bool = False,
) -> str:
    """Determine which tier supplied current_value: env, yaml, or default.

    - ``env``: the env var is set and non-empty.
    - ``yaml``: current_value differs from the built-in default, so the yaml
      tier (static base + overlay) must have contributed it.
    - ``default``: current_value equals the built-in default and no env var
      is set.

    The yaml tier is inferred rather than read directly because parsing the
    merged yaml just to detect the source would duplicate the load already
    done by the resolver. The heuristic is accurate for all scalar knobs
    (strings, ints, bools) that don't derive from other resolvers.

    ``cache_busted`` covers the knobs whose resolver runs a static asset path
    through ``static_url()``: the resolved value carries a ``?v=<mtime>``
    suffix that no declarable default can match, so it is stripped from both
    sides before comparing. Without that, such a knob would report ``yaml`` on
    an instance that configured nothing.
    """
    if env_var and os.environ.get(env_var, "").strip():
        return "env"
    if cache_busted:
        current_value, default = _strip_cache_buster(current_value), _strip_cache_buster(default)
    if current_value != default:
        return "yaml"
    return "default"


def _build_knobs() -> list[dict[str, Any]]:
    """Resolve every catalogued knob and return the enriched list."""
    import app.instance_config as ic

    out: list[dict[str, Any]] = []
    for entry in _KNOB_CATALOGUE:
        resolver_name = entry["resolver"]
        fn = getattr(ic, resolver_name, None)
        if fn is None:
            logger.warning("config_surface: resolver %s not found in instance_config", resolver_name)
            continue
        try:
            current_value = fn()
        except Exception:
            logger.exception("config_surface: resolver %s raised", resolver_name)
            current_value = None

        source = _source_for(
            entry["env_var"],
            resolver_name,
            current_value,
            entry["default"],
            cache_busted=bool(entry.get("cache_busted")),
        )
        out.append(
            {
                "key": entry["key"],
                "resolver": resolver_name,
                "env_var": entry["env_var"],
                "yaml_path": entry["yaml_path"],
                "default": entry["default"],
                "current_value": current_value,
                "source": source,
            }
        )
    return out


def _build_initial_workspace() -> Optional[dict[str, Any]]:
    """Return the initial_workspace section as a summary dict, or None."""
    try:
        from app.api.initial_workspace import _read_section

        section = _read_section()
        if not section.get("url"):
            return None
        return {
            "url": section.get("url"),
            "branch": section.get("branch"),
            "last_sync_sha": section.get("last_commit_sha"),
        }
    except Exception:
        logger.exception("config_surface: could not read initial_workspace section")
        return None


def _build_marketplaces(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Return [{name, url}] for every admin-registered marketplace.

    The built-in marketplace is excluded: it ships with the wheel and carries
    a non-actionable sentinel URL (``builtin://agnes-builtin``), so it is noise
    for the operator-facing config surface, which exists to expose real,
    instance-configured pointers.
    """
    try:
        from src.repositories import marketplace_registry_repo

        rows = marketplace_registry_repo().list_non_builtin()
        return [{"name": r["name"], "url": r["url"]} for r in rows]
    except Exception:
        logger.exception("config_surface: could not list marketplaces")
        return []


# ---------------------------------------------------------------------------
# Pydantic response model
# ---------------------------------------------------------------------------


class KnobEntry(BaseModel):
    key: str
    resolver: str
    env_var: Optional[str]
    yaml_path: Optional[str]
    default: Any
    current_value: Any
    source: str  # "env" | "yaml" | "default"


class InitialWorkspaceSummary(BaseModel):
    url: str
    branch: Optional[str]
    last_sync_sha: Optional[str]


class MarketplaceSummary(BaseModel):
    name: str
    url: str


class ConfigSurfaceResponse(BaseModel):
    knobs: list[KnobEntry]
    initial_workspace: Optional[InitialWorkspaceSummary]
    marketplaces: list[MarketplaceSummary]
    infra_repo_url: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/config-surface",
    response_model=ConfigSurfaceResponse,
    summary="Instance config-surface introspection",
    description=(
        "Returns the complete per-instance configuration surface: every knob "
        "resolved with its current value and which tier supplied it (env/yaml/"
        "default), the registered Initial Workspace Template (if any), every "
        "registered marketplace, and the infra_repo_url knob. Admin only. "
        "No new state is written — pure read."
    ),
)
async def get_config_surface(
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> ConfigSurfaceResponse:
    from app.instance_config import get_infra_repo_url

    knobs = _build_knobs()
    initial_workspace = _build_initial_workspace()
    marketplaces = _build_marketplaces(conn)
    infra_repo_url = get_infra_repo_url()

    return ConfigSurfaceResponse(
        knobs=knobs,
        initial_workspace=(InitialWorkspaceSummary(**initial_workspace) if initial_workspace else None),
        marketplaces=[MarketplaceSummary(**m) for m in marketplaces],
        infra_repo_url=infra_repo_url,
    )
