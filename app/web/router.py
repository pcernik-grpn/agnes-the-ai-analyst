"""Web UI routes — Jinja2 templates served by FastAPI.

Replicates all Flask webapp routes with DuckDB-backed data.
"""

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import duckdb

import jinja2

from app.auth.access import is_user_admin, require_admin
from app.web.studio import STUDIO_DOMAINS, get_domain as get_studio_domain
from app.auth.dependencies import get_current_user, get_optional_user, _get_db
from app.instance_config import (
    FEATURE_FLAGS,
    get_instance_name,
    get_instance_subtitle,
    get_theme_css_overrides,
    get_home_route,
    get_home_automode_visibility,
    get_instance_brand,
    get_instance_brand_short,
    get_instance_copyright,
    get_privacy_policy_url,
    get_workspace_dir_name,
    get_workspace_launcher_word,
    get_instance_logo_svg,
    get_instance_favicon,
    get_instance_overview,
    get_instance_support,
    get_hidden_login_features,
    get_instance_custom_preamble,
    get_instance_theme,
    get_ui_layout,
    get_custom_scripts,
    get_data_apps_config,
    get_studio_enabled,
    get_agent_profiles_enabled,
    get_mcp_connector_ui_enabled,
    feature_enabled,
)
from src.repositories import (
    audit_repo,
    corpus_files_repo,
    data_packages_repo,
    file_corpora_repo,
    glossary_repo,
    knowledge_repo,
    memory_domains_repo,
    metric_repo,
    news_template_repo,
    notifications_telegram_repo,
    profile_repo,
    recipes_repo,
    resource_grants_repo,
    semantic_model_repo,
    store_entities_repo,
    store_lint_repo,
    store_submissions_repo,
    sync_state_repo,
    table_registry_repo,
    usage_repo,
    user_group_members_repo,
    user_groups_repo,
    user_stack_subscriptions_repo,
    user_store_installs_repo,
    users_repo,
)
from src.semantic.keboola_sources import KEBOOLA_SEMANTIC_LAYER_SOURCES
from app.api.me_debug import (
    require_debug_auth_enabled,
    _read_session_token,
    _decoded_claims,
    _token_fingerprint,
    _last_sync_summary,
)


def _resolved_home_route() -> str:
    """Lazy wrapper so tests/monkeypatch on env vars are honoured per-request."""
    return get_home_route()


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _static_url(path: str) -> str:
    """Build /static/<path> with a cache-buster query string.

    Appends ``?v=<file_mtime_int>`` so a redeploy that changes a CSS/JS file
    invalidates browser + proxy caches without operator intervention.
    Missing files return the bare URL — FastAPI's StaticFiles will surface
    the 404 normally. Cheap (one ``os.stat`` per template variable use).
    """
    full = _STATIC_DIR / path
    try:
        v = int(full.stat().st_mtime)
        return f"/static/{path}?v={v}"
    except OSError:
        return f"/static/{path}"


logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _template_directories() -> list[str]:
    """Built-in templates first, then any deployment plugin template dirs (app/plugins.py).

    The configured dirs come from the operator's instance.yaml; missing ones are dropped.
    Defensive: a config-read failure falls back to the built-in dir only — this runs at
    import time, so it must never break app bootstrap.
    """
    from app.instance_config import get_value
    from app.plugins import extra_template_dirs

    try:
        extra = extra_template_dirs(get_value("plugins", "template_dirs", default=[]) or [])
    except Exception:
        extra = []
    return [str(TEMPLATES_DIR), *(str(d) for d in extra)]


templates = Jinja2Templates(directory=_template_directories())


# Make templates tolerant of missing variables (renders empty string instead of error)
class _SilentUndefined(jinja2.Undefined):
    """Silently handle any access on undefined variables — returns empty/falsy."""

    def __str__(self):
        return ""

    def __iter__(self):
        return iter([])

    def __bool__(self):
        return False

    def __len__(self):
        return 0

    def __getattr__(self, name):
        return self

    def __getitem__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __int__(self):
        return 0


templates.env.undefined = _SilentUndefined

# Add custom JSON filter that handles _SilentUndefined and _FlexDict
import json as _json  # noqa: E402


class _SafeEncoder(_json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (_SilentUndefined, _FlexDict)):
            if isinstance(obj, _FlexDict) and dict.__len__(obj) > 0:
                return dict(obj)
            return None
        return super().default(obj)


templates.env.policies["json.dumps_function"] = lambda obj, **kw: _json.dumps(obj, cls=_SafeEncoder, **kw)


def _humanbytes(value, precision: int = 2) -> str:
    """Render a byte count as the largest binary-prefixed unit it fits in.

    Below 1 KiB → integer bytes; otherwise ``precision`` decimal places of
    KB / MB / GB / TB (binary, 1024-based). Used by the Store detail
    template (default 2-decimal precision for fine-grained file sizes) and
    by the /dashboard stat tiles (1-decimal precision for headline numbers).
    Intentionally permissive about input type so missing / undefined values
    render as ``0 B`` rather than crashing the page.
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.{precision}f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.{precision}f} MB"
    gb = mb / 1024
    if gb < 1024:
        return f"{gb:.{precision}f} GB"
    tb = gb / 1024
    return f"{tb:.{precision}f} TB"


templates.env.filters["humanbytes"] = _humanbytes


def _store_display_name(name: str | None) -> str:
    """Strip the archive-rename suffix from a store entity's display
    name so admin queue / my-stack / detail templates show the
    original label instead of the internal `__archived__<epoch>`
    marker. Safe on plain (non-archived) names — no-op."""
    from src.store_naming import strip_archive_suffix

    return strip_archive_suffix(name or "")


templates.env.filters["store_display_name"] = _store_display_name


# ---- PostHog template wiring ----
# Two Jinja globals injected into every render so the `_posthog.html` partial
# (included from `base.html` and `base_login.html`) can render the browser
# snippet — or render nothing when the integration is disabled.
#
#   posthog_config              process-level static config (host, project key,
#                               replay flag, extra mask selector). Resolved
#                               once on first access.
#   posthog_user_block(request) per-request identify payload honoring the
#                               operator-chosen identify mode. Returns None
#                               for anonymous renders.
def _posthog_config_global() -> dict:
    from src.observability import get_posthog

    pc = get_posthog()
    if not pc.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "host": pc.host,
        "api_key_public": pc.api_key_public,
        "replay_enabled": pc.replay_enabled,
        "replay_mask_selector_extra": pc.replay_mask_selector_extra,
        "environment": pc.environment,
        "release": pc.release,
    }


def _posthog_user_block(request: Optional[Request]) -> Optional[dict]:
    from src.observability import get_posthog

    pc = get_posthog()
    if not pc.enabled:
        return None
    mode = pc.identify_mode
    if mode == "none":
        return None
    user = None
    if request is not None:
        try:
            user = getattr(request.state, "user", None)
        except Exception:
            user = None
    if not user:
        return None

    def _get(attr: str):
        if isinstance(user, dict):
            return user.get(attr)
        return getattr(user, attr, None)

    distinct_id = _get("id") or _get("user_id") or _get("email")
    if not distinct_id:
        return None
    props: dict = {}
    if mode in ("email", "full"):
        email = _get("email")
        if email:
            props["email"] = str(email)
    if mode == "full":
        name = _get("name") or _get("full_name")
        if name:
            props["name"] = str(name)
    return {"distinct_id": str(distinct_id), "props": props}


templates.env.globals["posthog_config"] = _posthog_config_global()
templates.env.globals["posthog_user_block"] = _posthog_user_block
# Stateless asset helper — register as a global so EVERY template resolves CSS/JS
# URLs even on routes that build a minimal context (e.g. the studio pages).
# Without this, base_ds.html emits <link href=""> and the page renders unstyled.
templates.env.globals["static_url"] = _static_url


def _data_apps_nav_enabled() -> bool:
    """Whether the "Apps" primary-nav entry should render. Registered as a
    Jinja global (like `static_url` above) rather than threaded through
    per-route context, so `_app_rail.html` — shared by both `base.html`
    (built via `_build_context`) and `base_ds.html`/`base_page.html` (built
    via `_chrome_ctx`) — gates consistently regardless of which context
    builder the current page uses. Re-read on every call (not cached at
    import time) so an admin flipping `data_apps.enabled` via
    /admin/server-config takes effect without a process restart."""
    try:
        return feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False)
    except Exception:
        return False


templates.env.globals["data_apps_enabled"] = _data_apps_nav_enabled


def _is_paper_theme() -> bool:
    """Whether the paper theme is active. Registered as a Jinja global for the
    same reason as `data_apps_enabled` above, plus one specific to the trust
    markers: the surfaces that render them live in MACRO files
    (`macros/_trustmark.html`, `_catalog_card.html`, `_stack_card.html`,
    `_detail.html`), and several are imported WITHOUT `with context`. Inside
    such a macro `instance_theme` resolves to Undefined — falsey, so a gate
    written against it would silently gate nothing. A global resolves
    everywhere regardless of import style.

    Re-read per call (not cached at import) so the env-var override the theme
    tests set takes effect without a process restart."""
    try:
        return get_instance_theme() == "paper"
    except Exception:
        return False


templates.env.globals["is_paper"] = _is_paper_theme


# Grouped /admin sidebar (issue #896 follow-up mock) — data + active-state
# resolver registered as globals (like `static_url`/`is_paper` above) so
# `_admin_nav.html` (included from base_admin.html / base_admin_page.html)
# resolves them regardless of which context builder the enclosing page uses.
from app.web.admin_nav import (  # noqa: E402
    ADMIN_NAV_DOCS,
    ADMIN_NAV_HOME,
    ADMIN_NAV_SECTIONS,
    resolve_active_href,
    resolve_active_section_key,
    resolve_home_active,
    resolve_section_tabs,
)

templates.env.globals["admin_nav_sections"] = ADMIN_NAV_SECTIONS
templates.env.globals["admin_nav_docs"] = ADMIN_NAV_DOCS
templates.env.globals["admin_nav_home"] = ADMIN_NAV_HOME
templates.env.globals["admin_nav_active_href"] = resolve_active_href
templates.env.globals["admin_nav_active_section_key"] = resolve_active_section_key
templates.env.globals["admin_nav_home_active"] = resolve_home_active
# Tier two — the per-section tab strip (`_admin_tabs.html`). A global for the
# same reason the rest are: the partial is included from a page's own content
# block, so it must resolve regardless of which context builder that page uses.
templates.env.globals["admin_nav_tabs"] = resolve_section_tabs


# The ONE default behind `library.show_unverified_trust`, read off the registry
# rather than restated at each read site. Three callsites resolve this flag (the
# Jinja global below plus /library and the store-item detail route), and each
# used to carry its own `default=False` literal — a comment asked them not to
# drift, which is not a mechanism. Flipping the registry entry then changed
# nothing, because all three overrode it. Sourcing the literal here means the
# registry entry is the default, and `tests/test_feature_flags.py` pins it.
_LIBRARY_TRUST_DEFAULT: bool = next(
    (f.default for f in FEATURE_FLAGS if f.name == "library_show_unverified_trust"),
    True,
)


def _show_unverified_trust() -> bool:
    """Whether the Community trust marker renders. Registered as a global for the
    same reason as `is_paper` above, and for one more that matters here: an
    instance-level switch resolved per template would let a stray literal
    default override the operator's setting either way — when this flag was
    briefly opt-out, exactly that happened.

    It briefly read `library_show_unverified_trust|default(true)` in
    `marketplace_item_detail.html`, to stop the marker vanishing on a route that
    forgot to pass the value. That fixed the disappearance and broke the off
    switch: any route omitting the variable rendered the marker on an instance
    that had explicitly disabled it — a silent failure, where the bug it replaced
    was at least visible. Resolving the flag here removes both failure modes,
    because there is no per-route value left to forget.

    Same keys and env var as the two per-route callsites, and all three now take
    their default from `_LIBRARY_TRUST_DEFAULT` (the registry entry) instead of
    each restating a literal, so they cannot drift. Re-read per call so an admin
    flipping it in /admin/server-config takes effect without a restart."""
    try:
        return feature_enabled(
            "library",
            "show_unverified_trust",
            env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
            default=_LIBRARY_TRUST_DEFAULT,
        )
    except Exception:
        # Fall back to the declared default, not to a hardcoded off: the only way
        # here is a malformed config, which is no reason to silently drop a
        # provenance level the instance never asked to hide.
        return _LIBRARY_TRUST_DEFAULT


templates.env.globals["show_unverified_trust_enabled"] = _show_unverified_trust


#: Where a detail page's back link goes in the RAIL layout, per Library
#: section key. The rail retired Marketplace (/catalog) as a destination —
#: it is not in the nav and a caller never arrives from it — so a detail
#: page that sent them "back" there dropped them on a surface they had
#: never seen. /library is the rail's one browse surface and lists every
#: one of these kinds, so that is where back goes; `?section=` opens the
#: matching band (library.html folds them by default). Keys are the
#: `type_key` vocabulary /library groups by (`_SECTION_LABELS`), plus
#: `semantics` — see below.
_RAIL_DETAIL_BACK: dict[str, tuple[str, str]] = {
    "data_package": ("/library?section=data_package", "All data packages"),
    # Memory folds into the Library under rail (spec 2026-08-12): the
    # standalone /corporate-memory page 302s to this section, so back links
    # point straight at the band rather than bouncing through the redirect.
    # Library arrivals still override via ?source=library
    # (memory_domain_detail.html).
    "memory_domain": ("/library?section=memory_domain", "All memory"),
    "recipe": ("/library?section=recipe", "All recipes"),
    # Same reasoning as memory_domain: /apps 302s into the Library under
    # rail; apps live in the Files band, so back opens that section.
    "data_app": ("/library?section=files", "Library"),
    "plugin": ("/library?section=plugin", "All plugins"),
    "skill": ("/library?section=skill", "All skills"),
    "agent": ("/library?section=agent", "All agents"),
    "files": ("/library?section=files", "Library"),
    # /catalog/semantics is not a `type_key`: the Definitions block is an
    # adjacent destination BELOW the inventory, not one of the bands, so it
    # has no `?section=` to open. It gets an anchor instead — without one the
    # bare /library the fallback returns lands the reader at the top of the
    # page, with the whole inventory between them and the block they clicked.
    # `#lib-defs` exists exactly when that block rendered (library.html emits
    # it under `if definitions_footer`, set only when the caller can see at
    # least one metric or glossary term); when it did not, the anchor is inert
    # and the browser stays at the top, which is what a bare /library did
    # anyway.
    "semantics": ("/library#lib-defs", "Library"),
}


def _detail_back(kind: str, href: str, label: str) -> dict[str, str]:
    """Resolve a detail page's back link.

    Always the /library section for the kind (see `_RAIL_DETAIL_BACK`),
    falling back to the Library itself for a kind with no mapping. The
    caller-supplied `href`/`label` are the PRE-RAIL destinations (the classic
    Catalog, Marketplace); they were honest while the topnav carried those
    rows, and this returned them unchanged on that chrome. Wave 0 (2026-08)
    retired it, so the branch that read them was unreachable — they are kept
    in the signature because every call site passes them positionally and
    they document where the page used to sit.

    Registered as a Jinja global rather than threaded through each route's
    context so every detail template resolves it the same way, including the
    ones rendered from a minimal context.
    """
    rail = _RAIL_DETAIL_BACK.get(kind)
    if not rail:
        return {"href": "/library", "label": "Library"}
    return {"href": rail[0], "label": rail[1]}


templates.env.globals["detail_back"] = _detail_back
# The Keboola semantic-layer writer source(s) (`src.semantic.keboola_sources`)
# — pre- and post-flat-table-cutover — so a template badge/filter matches both
# without hard-coding the retired literal.
templates.env.globals["keboola_semantic_layer_sources"] = KEBOOLA_SEMANTIC_LAYER_SOURCES


class _FlexDict(dict):
    """Dict that returns empty _FlexDict for missing keys and attributes.
    Prevents Jinja2 UndefinedError when templates access missing nested values."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            return _FlexDict()

    def __bool__(self):
        return bool(dict.__len__(self))

    def __str__(self):
        return ""

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __iter__(self):
        return iter(dict.values(self)) if dict.__len__(self) else iter([])

    def __len__(self):
        return dict.__len__(self)

    def __call__(self, *args, **kwargs):
        return ""

    def __add__(self, other):
        return other

    def __radd__(self, other):
        return other

    def __sub__(self, other):
        return 0 - other if isinstance(other, (int, float)) else self

    def __rsub__(self, other):
        return other

    def __mul__(self, other):
        return 0

    def __rmul__(self, other):
        return 0

    def __truediv__(self, other):
        return 0

    def __rtruediv__(self, other):
        return 0

    def __mod__(self, other):
        return 0

    def __eq__(self, other):
        return False if dict.__len__(self) == 0 else dict.__eq__(self, other)

    def __ne__(self, other):
        return True if dict.__len__(self) == 0 else dict.__ne__(self, other)

    def __lt__(self, other):
        return False

    def __gt__(self, other):
        return False

    def __le__(self, other):
        return True

    def __ge__(self, other):
        return True

    def __contains__(self, item):
        return dict.__contains__(self, item) if dict.__len__(self) else False


def _flex(d):
    """Recursively convert dicts to _FlexDict for template compatibility."""
    if isinstance(d, dict) and not isinstance(d, _FlexDict):
        return _FlexDict({k: _flex(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_flex(i) for i in d]
    return d


_URL_MAP = {
    # Flask-style endpoint names → FastAPI URL paths
    "dashboard": "/dashboard",
    "catalog": "/catalog",
    "corporate_memory": "/corporate-memory",
    "corporate_memory_admin": "/admin/corporate-memory",
    "activity_center": "/activity-center",
    "admin_activity": "/admin/activity",
    "index": "/",
    "auth.login": "/login",
    "auth.logout": "/login",  # No logout route — redirect to login
    "password_auth.login_email": "/auth/password/login",
    "password_auth.reset_request": "/auth/password/reset",
    "password_auth.request_access": "/auth/password/setup",
    "email_auth.login_email_form": "/login/email",
    "email_auth.send_magic_link": "/auth/email/send-link",
    "email_auth.send_magic_link_web": "/auth/email/send-link/web",
    "register": "/auth/password/setup",
    "setup": "/first-time-setup",
}


def _url_for_shim(endpoint: str, **kw) -> str:
    """Flask url_for compatibility — maps endpoint names to FastAPI paths."""
    if endpoint == "static":
        filename = kw.get("filename", "")
        return f"/static/{filename}"
    return _URL_MAP.get(endpoint, f"/{endpoint}")


def _read_agnes_ca_pem() -> Optional[str]:
    """Read the Agnes server's TLS fullchain for inlining into the setup prompt.

    Returns the PEM string when the cert needs trust-bootstrapping —
    self-signed (leaf issuer == subject), private-CA chain that doesn't
    terminate in a `certifi`-known root, or any case where we can't
    cheaply prove the OS would trust it. Returns None when the chain in
    the served fullchain.pem terminates in a publicly-trusted root that
    `certifi` already ships (Let's Encrypt's ISRG Root X1, DigiCert,
    etc.) — clients (Bun-compiled `claude.exe`, system git, Python with
    certifi) all accept the chain without help.

    Chain validation walks every cert in the served fullchain and
    succeeds the first time any cert's issuer matches a `certifi` root
    subject. That captures the standard fullchain shape (leaf +
    intermediate(s)) where `intermediate.issuer == publicly_trusted_root`,
    even though the leaf's *immediate* issuer is the intermediate (which
    is rarely shipped in trust stores — only roots are).

    Inlining a publicly-trusted cert is harmless (clients already trust
    it via OS roots), but it bloats the prompt and steers users into
    setting SSL_CERT_FILE unnecessarily, which narrows their Python TLS
    trust to just this host. So skip when we can confirm broad trust.

    Path is configurable via AGNES_TLS_FULLCHAIN_PATH (defaults to
    `/data/state/certs/fullchain.pem`, the location `agnes-tls-rotate.sh`
    writes on every VM and `docker-compose.host-mount.yml` rbinds into
    the app container). Missing / unreadable / unparseable → None, and
    the setup prompt falls back to its pre-cert behavior.
    """
    path = Path(os.environ.get("AGNES_TLS_FULLCHAIN_PATH", "/data/state/certs/fullchain.pem"))
    try:
        if not path.is_file():
            return None
        pem = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if "-----BEGIN CERTIFICATE-----" not in pem:
        return None

    try:
        from cryptography import x509

        chain = x509.load_pem_x509_certificates(pem.encode("utf-8"))
        if not chain:
            return None
        leaf = chain[0]

        if leaf.issuer == leaf.subject:
            # Self-signed — definitely needs bootstrap on the client.
            return pem

        # CA-signed leaf: walk every cert in the served fullchain (leaf +
        # intermediates) and check whether ANY of their issuers is in
        # `certifi`'s trust store. The first match means the chain
        # terminates in a publicly-trusted root, so the client OS / Bun
        # bundle / certifi already accept it.
        try:
            import certifi

            with open(certifi.where(), "rb") as fh:
                trust_pem = fh.read()
        except Exception:
            return pem  # can't enumerate trust → assume bootstrap needed

        trusted_subjects = {ca.subject.rfc4514_string() for ca in x509.load_pem_x509_certificates(trust_pem)}
        for cert in chain:
            if cert.issuer.rfc4514_string() in trusted_subjects:
                return None  # publicly trusted; client OS already accepts
        return pem
    except Exception:  # pragma: no cover — defensive: bad PEM / x509 error
        logger.exception("Failed to evaluate Agnes TLS cert; skipping inline")
        return None


# Sentinel distinguishing "caller omitted conn" from "caller passed conn=None".
# On Postgres a supplied request conn is ALWAYS None (the system DuckDB is never
# opened), so None cannot tell an authenticated DB-backed caller (conn=Depends(
# _get_db) → None on PG) apart from an anonymous page that passed no conn at all.
# The sentinel captures that intent so the two stay distinguishable on Postgres.
_CONN_UNSET: Any = object()


def _compute_can_chat(request: Request, user: Optional[dict]) -> bool:
    """Cloud-chat nav visibility, shared by every page-context builder.

    The /chat link is shown only when chat is enabled AND one of the viewer's
    groups holds an explicit chat grant. We deliberately use
    `has_explicit_grant` (NOT `can_access`) so the link tracks actual rollout
    state, not effective access: admins do NOT see it until chat is granted to
    a group they're in, even though god-mode still lets them reach /chat by
    URL (the route guard uses can_access). This is UX only — the hard gate is
    on the route + API.

    Computed on EVERY page — both `_build_context` and `_chrome_ctx` must set
    it, otherwise the link flickers out on the pages using the other builder
    (the studio pages regressed on exactly this). `has_explicit_grant` is
    backend-aware (it routes through the repo factory), so no connection is
    threaded here — it reads the active backend itself. Defaults False when
    chat is disabled or there's no user.
    """
    try:
        _cc = getattr(request.app.state, "chat_config", None)
        if user and _cc is not None and _cc.enabled:
            from app.auth.access import has_explicit_grant
            from app.resource_types import ResourceType

            return bool(has_explicit_grant(user["id"], ResourceType.CHAT.value, "chat"))
    except Exception:
        return False
    return False


def _config_proxy() -> type:
    """Template-facing ``config`` object, shared by every page-context builder.

    Defined as a class built at call time so every attribute is re-read per
    request (operators can flip env vars / instance.yaml without a restart).
    Both `_build_context` and `_chrome_ctx` must expose it as ``config`` —
    templates read e.g. ``config.INSTANCE_NAME`` in ``<title>`` blocks and
    the shared header logo, which rendered empty on the pages whose builder
    skipped it (the studio pages regressed on exactly this).
    """

    class ConfigProxy:
        INSTANCE_NAME = get_instance_name()
        INSTANCE_SUBTITLE = get_instance_subtitle()
        # Was hardcoded "" here, which made the documented `instance.copyright`
        # key inert: every footer reads `config.INSTANCE_COPYRIGHT or 'AI
        # Harness'`, so all six chromes rendered the fallback literal instead
        # of the operator's credit. Empty stays meaningful — `_footer.html`
        # omits the attribution line rather than inventing one.
        INSTANCE_COPYRIGHT = get_instance_copyright()
        # The footer's left side names the PRODUCT (not the deployment) and
        # reads it off `config` rather than the `instance_brand` context key,
        # because the partial renders on chromes whose builders don't set it.
        INSTANCE_BRAND = get_instance_brand()
        LOGO_SVG = get_instance_logo_svg()
        INSTANCE_OVERVIEW = get_instance_overview()
        INSTANCE_SUPPORT = get_instance_support()
        HIDE_LOGIN_FEATURES = get_hidden_login_features()
        TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
        SSH_ALIAS = "data-analyst"
        SERVER_HOST = os.environ.get("SERVER_HOST", "")
        PROJECT_DIR = "data-analyst"
        # Drives whether the user dropdown renders the "Auth debug" link.
        # Same env var the route guard checks — keep them in lock-step so
        # the link never appears when the route would 404, and vice versa.
        DEBUG_AUTH_ENABLED = os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # Google Workspace prefix-mapping config — surfaced into templates
        # so client-side JS can derive a friendly display name from the
        # full Workspace email stored as the group's `name` (admin UI
        # strips the prefix and `@domain` for the big line, keeps the
        # full email as subtitle). Read at template render time so an
        # operator can flip these via env without an image rebuild.
        AGNES_GOOGLE_GROUP_PREFIX = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "")
        AGNES_GROUP_ADMIN_EMAIL = os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "")
        AGNES_GROUP_EVERYONE_EMAIL = os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "")

        @staticmethod
        def theme_overrides():
            # CSS variable overrides for the operator `theme:` block — both
            # the legacy `--*` family and the matching `--ds-*` design-system
            # tokens. See app.instance_config.get_theme_css_overrides.
            return get_theme_css_overrides()

    return ConfigProxy


def _build_context(
    request: Request,
    user: Optional[dict] = None,
    conn: Any = _CONN_UNSET,
    **extra,
) -> dict:
    """Build template context with config, user, and theme.

    Composes `_chrome_ctx` — the single owner of every chrome-level key (nav,
    branding, theme, feature toggles, …; see its docstring) — then layers on
    the heavier, page-specific payloads only `_build_context` callers need:
    the setup-prompt clipboard script and `server_url`. A chrome key added to
    `_chrome_ctx` therefore reaches both builders automatically; add a key
    HERE only if it is genuinely specific to the heavy payload, not shared
    chrome (#996).

    `conn` is optional: when supplied alongside a logged-in `user`, the
    setup-prompt preview/clipboard payload is rendered with that user's
    RBAC-allowed Claude Code marketplace plugins inlined as install
    commands. Routes that don't render the env-setup-cta block can omit it.
    """
    ctx_server_url = str(request.base_url).rstrip("/")

    # Lines for the "Setup a new Claude Code" preview/clipboard partial.
    #
    # When a DB connection is available, we go through render_agent_prompt_banner
    # which checks for an admin override first (stored in welcome_template) and
    # falls back to the live default from setup_instructions.resolve_lines().
    # This guarantees that both /setup and /dashboard clipboard CTA always reflect
    # the same content — the override is honoured everywhere.
    #
    # When no conn is supplied (e.g. public pages that don't need a DB round-trip)
    # we fall back to resolve_lines() directly with anonymous/no-plugin context.
    #
    # On Postgres the request conn is None (the system DuckDB must never be
    # opened), but render_agent_prompt_banner → resolve_prompt routes through
    # the repository factory with conn=None, so the admin-override path must
    # still run under use_pg() to stay at parity with DuckDB — but ONLY for
    # callers that actually opted into the DB-backed prompt by supplying the
    # `conn` kwarg. Unauthenticated pages (/login, /first-time-setup,
    # /login/password) omit conn entirely and must get the anonymous default on
    # BOTH backends; gating the use_pg() branch on `conn is not None` alone would
    # leak the admin install-prompt override to anonymous visitors on Postgres
    # (where a supplied conn is always None). See PR #878 review.
    from src.repositories import use_pg as _use_pg_banner

    conn_supplied = conn is not _CONN_UNSET
    conn = None if not conn_supplied else conn

    if conn is not None or (conn_supplied and _use_pg_banner()):
        from src.welcome_template import render_agent_prompt_banner

        _script_text = render_agent_prompt_banner(conn, user=user, server_url=ctx_server_url)
        setup_instructions_lines = _script_text.split("\n")
    else:
        # No DB connection — use the unauthenticated default (no override
        # possible). The thin prompt has no per-caller inputs left: plugins
        # and connectors are resolved by `agnes onboard` at run time, so the
        # anonymous render is identical to the signed-in default.
        from app.web.setup_instructions import resolve_lines

        server_host = request.url.netloc
        ca_pem = _read_agnes_ca_pem()

        setup_instructions_lines = resolve_lines(
            "agnes.whl",
            server_host=server_host,
            ca_pem=ca_pem,
            instance_brand=get_instance_brand(),
            workspace_dir=get_workspace_dir_name(),
            custom_preamble=get_instance_custom_preamble(),
        )

    ctx = {
        **_chrome_ctx(request, user),
        "static_url": _static_url,
        "setup_instructions_lines": setup_instructions_lines,
        "server_url": ctx_server_url,
    }
    # Flex all extra context values for template compatibility
    # (but skip ones we just populated — extras with the same key win)
    for k, v in extra.items():
        ctx[k] = _flex(v) if isinstance(v, (dict, list)) else v
    return ctx


# ---- Navigation ----


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        from app.instance_config import get_home_route

        return RedirectResponse(url=get_home_route(), status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/first-time-setup", response_class=HTMLResponse)
async def setup_wizard(request: Request):
    """First-time setup wizard. Redirects to login if users already exist.

    Counts users through the repo factory, not a raw ``_get_db`` connection:
    on a Postgres instance the users live in PG, so a raw DuckDB count returned
    0 and the wizard stayed open forever even on a fully-provisioned instance.
    """
    try:
        from src.repositories import users_repo

        if users_repo().count_all() > 0:
            return RedirectResponse(url="/login", status_code=302)
    except Exception:
        pass  # No users table yet — show setup
    return templates.TemplateResponse(request, "setup.html", _build_context(request))


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    """What this instance does with data — deliberately UNAUTHENTICATED.

    Every connector directory asks for a privacy policy URL and fetches it
    without credentials; an unreachable one is an automatic rejection. The
    same content already existed as ``/how-it-works#privacy``, but that route
    requires a signed-in session, so handing out that anchor produced a
    login redirect rather than a policy.

    Two shapes, one URL. When the operator has published their own policy
    (``instance.privacy_policy_url``) this redirects there, because on a
    self-hosted deployment *they* are the data controller and their document
    is the authoritative one. Otherwise it renders the built-in page, which
    states that plainly and describes only what the software itself does —
    the honest limit of what a vendor can say about someone else's instance.

    No auth, no DB read: it must answer for a reviewer, a crawler, and a
    logged-out user on an instance whose database is down.
    """
    policy_url = get_privacy_policy_url()
    if policy_url:
        return RedirectResponse(url=policy_url, status_code=302)
    return templates.TemplateResponse(request, "privacy.html", _build_context(request))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    from app.auth.dependencies import is_local_dev_mode, _get_local_dev_user

    if is_local_dev_mode():
        # Only short-circuit to the home route if the dev user is actually
        # seeded. Otherwise a 401 there would bounce back to /login and loop.
        from src.db import get_system_db
        from src.repositories import use_pg

        # _get_local_dev_user is factory-routed and ignores conn; on Postgres
        # pass None so the system DuckDB is never opened (forbidden invariant).
        conn = None if use_pg() else get_system_db()
        try:
            if _get_local_dev_user(conn):
                return RedirectResponse(url=get_home_route(), status_code=302)
        finally:
            if conn is not None:
                conn.close()
        # Fall through to the normal login form so the missing-seed error is visible.

    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    from app.auth.provider_registry import provider_allowed

    providers = []
    try:
        from app.auth.providers.google import is_available as google_available

        if google_available() and provider_allowed("google"):
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    if provider_allowed("password"):
        providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    try:
        from app.auth.providers.email import is_available as email_available

        if email_available() and provider_allowed("email"):
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
    except Exception:
        pass
    try:
        from app.auth.providers.keboola import is_available as keboola_available

        if keboola_available() and provider_allowed("keboola"):
            providers.append({"name": "keboola", "display_name": "Keboola", "icon": "keboola"})
    except Exception:
        pass
    try:
        from app.auth.providers.microsoft import is_available as microsoft_available

        if microsoft_available() and provider_allowed("microsoft"):
            providers.append({"name": "microsoft", "display_name": "Microsoft", "icon": "microsoft"})
    except Exception:
        pass

    # Convert to login_buttons format expected by template
    login_buttons = []
    for p in providers:
        if p["name"] == "google":
            _url = "/auth/google/login"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Google", "css_class": "btn-primary", "icon_html": ""}
            )
        elif p["name"] == "password":
            _url = "/login/password"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Email & Password", "css_class": "btn-secondary", "icon_html": ""}
            )
        elif p["name"] == "email":
            _url = "/login/email"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Email Link", "css_class": "btn-secondary", "icon_html": ""}
            )
        elif p["name"] == "keboola":
            _url = "/auth/keboola/login"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Keboola", "css_class": "btn-primary", "icon_html": ""}
            )
        elif p["name"] == "microsoft":
            _url = "/auth/microsoft/login"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Microsoft", "css_class": "btn-primary", "icon_html": ""}
            )

    keboola_expected_project = ""
    if request.query_params.get("error") == "keboola_project_mismatch":
        try:
            from app.auth.providers import keboola_verify as _kv

            keboola_expected_project = _kv.configured_project_id() or ""
        except Exception:
            pass

    ctx = _build_context(
        request,
        providers=providers,
        login_buttons=login_buttons,
        next_path=next_path,
        keboola_expected_project=keboola_expected_project,
    )
    return templates.TemplateResponse(request, "login.html", ctx)


@router.get("/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request):
    """Password login form (email + password)."""
    from app.auth.provider_registry import provider_allowed

    if not provider_allowed("password"):
        raise HTTPException(status_code=404, detail="Not Found")
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available

        google_ok = google_available() and provider_allowed("google")
    except Exception:
        pass
    ctx = _build_context(request, google_available=google_ok, next_path=next_path)
    return templates.TemplateResponse(request, "login_email.html", ctx)


@router.get("/login/email", response_class=HTMLResponse)
async def login_email_page(request: Request):
    """Email magic link login form.

    Renders `login_magic_link.html` — the actual magic-link form, whose
    "Send Sign-In Link" button posts to `/auth/email/send-link/web`. This
    route used to render `login_email.html` (the password form, which posts
    to `/auth/password/*`) by mistake: with `auth.providers: [email]`, the
    entire `/auth/password` router 404s, so the wrong template locked out
    the web UI end to end.
    """
    from app.auth.provider_registry import provider_allowed

    if not provider_allowed("email"):
        raise HTTPException(status_code=404, detail="Not Found")
    # Don't render a "send me a link" form when no mail transport is configured
    # — it would take the email and claim a link was sent that can never arrive.
    # is_available() is True in local-dev (the link is logged), so dev still works.
    from app.auth.providers.email import is_available as email_available

    if not email_available():
        return RedirectResponse(url="/login?error=email_not_configured", status_code=302)
    from app.auth._common import safe_next_path

    next_path = safe_next_path(request.query_params.get("next", ""), default="")
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available

        google_ok = google_available() and provider_allowed("google")
    except Exception:
        pass
    from app.instance_config import get_allowed_domains

    ctx = _build_context(
        request,
        google_available=google_ok,
        next_path=next_path,
        allowed_domains=get_allowed_domains(),
    )
    return templates.TemplateResponse(request, "login_magic_link.html", ctx)


def _compute_data_stats() -> dict:
    """Headline data-estate stats shared by /dashboard and /stack.

    `tables` counts REGISTERED non-internal business tables (not synced
    ones — a registry of 30 with 0 synced would otherwise read as "0").
    Columns / rows / size come from sync_state, the canonical record of
    what is actually on disk locally. Extracted so the two surfaces that
    render this strip can never drift apart.
    """
    all_states = sync_state_repo().get_all_states()
    total_tables = table_registry_repo().count_non_internal()
    total_rows = sum(s.get("rows", 0) or 0 for s in all_states)
    total_columns = sum(s.get("columns", 0) or 0 for s in all_states)
    total_size_bytes = sum(s.get("file_size_bytes", 0) or 0 for s in all_states)
    last_updated = max(
        (s.get("last_sync") for s in all_states if s.get("last_sync")),
        default=None,
    )
    # Trim microseconds for display — "2026-07-21 09:11:30" reads cleaner than
    # the raw "...:30.466650". Defensive against str or datetime inputs.
    last_updated_display = str(last_updated).split(".")[0] if last_updated else None
    return {
        "tables": total_tables,
        "total_tables": total_tables,
        "columns": total_columns,
        "rows_display": f"{total_rows:,}" if total_rows else "0",
        "size_display": _humanbytes(total_size_bytes, precision=1) if total_size_bytes else "0 MB",
        "total_rows": total_rows,
        "size_bytes": total_size_bytes,
        "last_updated": last_updated,
        "last_updated_display": last_updated_display,
        "remote_tables": 0,
        "local_tables": total_tables,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: dict = Depends(get_current_user),
):
    # A REDIRECT, not a page. Deliberately a comment and not a docstring: a
    # handler docstring becomes the endpoint's public OpenAPI `description`
    # (tests/snapshots/openapi.json), and this is internal rationale.
    #
    # The Dashboard IS Chat's pre-conversation state — /chat with no active
    # conversation renders the greeting, composer, activity panels and guided
    # task starters (chat.html's empty-state blocks) — so this 302s there and
    # the two surfaces can never drift apart.
    #
    # It used to fall through to a historical table-inventory render for the
    # topnav chrome. That chrome was retired in Wave 0 (2026-08), which made
    # the layout test above it unconditionally true and the 53-line body below
    # it unreachable; both are gone, along with the `dashboard.html` template
    # that had no other renderer.
    #
    # `can_chat` mirrors the rail nav's own predicate exactly (see
    # _build_context: chat enabled AND has_explicit_grant) so the LANDING and
    # the NAV agree. The dashboard exists to start Agnes conversations, so
    # without a chat grant it would be a dead shell — those users 302 to the
    # Library instead. That landing used to be My Stack, but /stack is no
    # longer a rail destination (#1088), so grant-less callers would have
    # landed on a surface the rail neither links to nor highlights, with the
    # rail logo (href = home_route = /dashboard) bouncing them right back to
    # it. The Library is the nearest thing to a data-estate home that IS in
    # the nav. has_explicit_grant is stricter than /chat's own can_access
    # guard, so the /chat redirect is loop-safe. 302 (not 308) so a later
    # grant flip isn't cached permanently by the browser.
    from app.auth.access import has_explicit_grant
    from app.resource_types import ResourceType

    chat_cfg = getattr(request.app.state, "chat_config", None)
    can_chat = bool(chat_cfg and chat_cfg.enabled and has_explicit_grant(user["id"], ResourceType.CHAT.value, "chat"))
    return RedirectResponse(url="/chat" if can_chat else "/library", status_code=302)


def _time_of_day_greeting(hour: int | None = None) -> str:
    """Salutation for the rail chat-dashboard greeting ("Good morning" /
    "Good afternoon" / "Good evening"). Server-clock based; chat_dashboard.js
    re-derives it from the browser clock after load so users in a different
    timezone than the server see the right salutation."""
    h = datetime.now().hour if hour is None else hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 18:
        return "Good afternoon"
    return "Good evening"


@router.get("/home", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """State-aware /home — full inline install for not-onboarded users,
    clean nav hub once onboarded. The boolean drives template selection;
    no auto-transition (manual reload picks up the flip after
    ``agnes init`` POSTs ``/api/me/onboarded``).

    See origin: docs/brainstorms/home-page-requirements.md.
    """
    # Read onboarded through the backend-aware repo factory, NOT the raw
    # `conn` (which is always DuckDB via `_get_db`). On a Postgres-backed
    # instance the source of truth is Postgres: POST /api/me/onboarded
    # writes there via `users_repo()`, but a raw DuckDB read here returns the
    # stale pre-migration value — so the "Mark me as onboarded" button (and
    # `agnes init`) would flip the flag in Postgres yet /home keeps rendering
    # the setup view forever. Routing the read through `users_repo()` keeps
    # write and read on the same backend.
    urow = users_repo().get_by_id(user["id"])
    onboarded = bool(urow.get("onboarded")) if urow else False

    # Pull the latest published news intro for the bottom-of-page section.
    # Template renders the section only when intro is non-empty, so an
    # instance that has never published news shows nothing extra.
    news = news_template_repo().get_current_published()
    news_intro = news["intro"] if (news and news.get("intro")) else ""

    # Homepage status frame (Last sync, Sessions, Prompts, Tokens, Projects).
    # Gated on (a) operator flag instance.home.show_status_frame /
    # AGNES_HOME_SHOW_STATUS_FRAME (default on), AND (b) the user being
    # onboarded — first-day users see a clean install-hero before zero-value
    # stat cards. When either gate is closed we skip the DB read entirely.
    from app.api.me import compute_home_stats
    from app.instance_config import get_home_status_frame_visibility

    status_frame_enabled = get_home_status_frame_visibility()
    home_stats = compute_home_stats(user, "24h") if (status_frame_enabled and onboarded) else None

    # Single template renders both states. The post-onboarding view keeps
    # the install-steps + connector prompts + auto-mode card visible —
    # they stay relevant for adding a second machine, a missing connector,
    # or re-running auto-mode setup. Hero copy + the self-mark control
    # branch on the boolean.
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        onboarded=onboarded,
        is_admin=is_user_admin(user["id"], conn),
        news_intro=news_intro,
        home_stats=home_stats,
        status_frame_enabled=status_frame_enabled,
    )
    return templates.TemplateResponse(request, "home_not_onboarded.html", ctx)


@router.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """The single orientation page: what {brand} is, what it knows for you,
    where you can use it, how to connect each surface, and where your data
    goes — one scroll with a sticky table of contents.

    Consolidates what used to be split across /home's "Four places, one
    workspace" + first-session narrative and the standalone AI Connector
    page. The connector content is not summarized here, it LIVES here
    (``#connect`` / ``#cli`` / ``#reference``), so there is exactly one
    source of truth for "how do I connect" — the two pages had already
    drifted ("four places" vs. six tool tabs). ``#connect`` is one section
    with two large tabs, the MCP connector and the CLI; ``#cli`` is the
    anchor of the second tab, which the page's script opens on arrival.
    ``/me/ai-connector`` and its legacy aliases redirect to ``#connect``.

    Any authenticated user; nothing on the page is admin-gated.
    """
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.api.v2_marketplace import _accessible_plugins, _skills_for_plugin
    from app.services.journey import mark_journey
    from src.repositories import mcp_sources_repo

    # "Use Agnes outside this tab" is earned by ARRIVING here — this page is where
    # every connector lives, and it is the checklist row's own destination. The
    # row used to tick itself the instant it was clicked, before the reader had
    # seen anything; the tour's "Connect my AI tools" button already marks it the
    # same way (tour.js::markUseAnywhereDone). Best-effort and swallowed (see
    # app/services/journey.py) — a bookkeeping write must never fail a render.
    mark_journey(user.get("id"), use_anywhere=True)

    # Backend-aware reads (mcp_sources / tool grants live in Postgres on a PG
    # instance) — a raw DuckDB conn here showed no MCP tools on Cowork.
    source_names = {s["id"]: s["name"] for s in mcp_sources_repo().list_all(enabled_only=True)}
    raw_tools = _visible_passthrough_tools(user)
    passthrough_tools = []
    for t in raw_tools:
        sname = source_names.get(t["source_id"])
        if sname:
            passthrough_tools.append(
                {
                    "exposed_name": t["exposed_name"],
                    "description": t.get("description"),
                    "source_name": sname,
                }
            )

    skills = []
    for plugin in _accessible_plugins(user):
        skills.extend(_skills_for_plugin(plugin["marketplace_id"], plugin["name"]))

    _brand = get_instance_brand()
    static_tools = [
        {"name": "server_info", "description": f"Check {_brand} connectivity and your account email."},
        {"name": "catalog", "description": "List all tables available to you — name, query_mode, row count."},
        {"name": "schema", "description": "Show column names and types for a table."},
        {"name": "describe", "description": "Schema + sample rows for a table in one call."},
        {"name": "query", "description": f"Execute SQL against {_brand} data (DuckDB or BigQuery dialect)."},
        {"name": "skills", "description": "List marketplace skills you can access — includes full SKILL.md body."},
    ]

    server_url = str(request.base_url).rstrip("/")
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
        static_tools=static_tools,
        passthrough_tools=passthrough_tools,
        skills=skills,
        server_url=server_url,
    )
    return templates.TemplateResponse(request, "how_it_works.html", ctx)


@router.get("/me/ai-connector", response_class=HTMLResponse)
async def me_ai_connector_page(
    user: dict = Depends(get_current_user),
):
    """AI Connector — consolidated into /how-it-works#connect.

    Redirects there unconditionally (302, not 301: a permanent redirect is
    cached by the browser forever, so it would be very hard to walk back if
    the consolidation is revisited).

    Checked first (#1024): with the MCP connector UI hidden
    (``mcp.connector_ui_enabled: false`` — a VPN/intranet-only instance whose
    cloud-side MCP clients can never reach the endpoint), this whole page IS
    the surface being hidden, so it bounces home instead."""
    from fastapi.responses import RedirectResponse

    if not get_mcp_connector_ui_enabled():
        return RedirectResponse("/", status_code=302)

    return RedirectResponse("/how-it-works#connect", status_code=302)


@router.get("/me/mcp", response_class=HTMLResponse)
@router.get("/me/cowork", response_class=HTMLResponse)
async def me_mcp_redirect(request: Request):
    """Legacy aliases → /me/ai-connector.

    302, not 301: a permanent redirect is cached by the browser forever, so
    it would be very hard to walk back if the routing is revisited.
    """
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/me/ai-connector", status_code=302)


@router.get("/mcp-connect", response_class=HTMLResponse)
async def mcp_connect_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Headless MCP editor connect page — generates a PAT + config snippets
    for Cursor, VS Code / GitHub Copilot, and any client accepting a URL.

    Any authenticated user (not admin-only) can reach this page.
    """
    # Entry points: the AI Connector page (`/me/ai-connector`) links here as the
    # token fallback to its OAuth flow, plus a Cmd/Ctrl-K palette entry. It is
    # deliberately NOT a nav item — "connect an AI client" is one job and
    # `/me/ai-connector` owns it. Both links are guarded by
    # `tests/test_web_nav_agents.py`; don't drop them. (Comment, not docstring:
    # FastAPI copies docstrings into the OpenAPI description.)
    #
    # Same #1024 gate as /me/ai-connector — this page IS install instructions
    # for the surface being hidden.
    if not get_mcp_connector_ui_enabled():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/", status_code=302)

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "mcp_connect.html", ctx)


@router.get("/me/connections", response_class=HTMLResponse)
async def me_connections_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Self-service page: connect / replace / test / remove your own credential
    for the per_user MCP sources you are granted. Any authenticated user.

    ``auth_method='oauth'`` sources (2026-07-30 outbound MCP OAuth sources
    spec §3) report against ``mcp_user_oauth_tokens`` instead of the
    vault-backed per-user secrets table — same card, a Connect/Disconnect
    button in place of the paste-field (template branches on
    ``s.auth_kind``)."""
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.markdown_render import render_safe
    from src.repositories import mcp_sources_repo, mcp_user_oauth_tokens_repo, per_user_secrets_repo

    granted_ids = {t["source_id"] for t in _visible_passthrough_tools(user)}
    # Caller-relative (`granted_ids`) and source-level (`sourced_ids`) are two
    # different questions, and the card needs both: "this source has no tools
    # yet" is a property of the source, while "no tools are granted to me" is a
    # property of the viewer. They coincide for an admin, which is how the
    # conflation survived — for a revoked non-admin, whose card this PR
    # deliberately keeps visible, it told them the source was unfinished rather
    # than that they had lost access (Devin Review on #1167).
    from src.repositories import tool_registry_repo
    from src.repositories.tool_registry import PASSTHROUGH

    sourced_ids = {t["source_id"] for t in tool_registry_repo().list_by_mode(PASSTHROUGH, enabled_only=True)}
    caller_is_admin = is_user_admin(user["id"], conn)
    sources = []
    for src in mcp_sources_repo().list_all(enabled_only=True):
        if (src.get("scope") or "shared").lower() != "per_user":
            continue
        is_oauth = (src.get("auth_method") or "").lower() == "oauth"
        if is_oauth:
            from app.api.mcp_policy import oauth_connection_usable

            token_row = mcp_user_oauth_tokens_repo().get(src["id"], user["id"])
            # Same validity rule the server enforces at call time — a lapsed,
            # unrenewable token must show Connect, not a green Connected pill
            # (Devin Review on #1130).
            has_secret = oauth_connection_usable(src["id"], user["id"])
            # `stored` diverges from `has_secret` exactly for a lapsed,
            # unrenewable token: not usable, but the row still exists and the
            # user must be able to Disconnect it (Devin Review on #1130).
            stored = token_row is not None
            updated_at = token_row["updated_at"].isoformat() if token_row and token_row.get("updated_at") else None
            expires_at = token_row["expires_at"].isoformat() if token_row and token_row.get("expires_at") else None
        else:
            has_secret = per_user_secrets_repo().has(src["id"], user["id"])
            stored = has_secret
            updated_at = per_user_secrets_repo().get_updated_at(src["id"], user["id"])
            expires_at = None
        # Visibility: granted tools, OR the caller's own stored credential
        # (a connection you made must never be invisible — you need Test /
        # Disconnect), OR admin (the register → connect → introspect
        # bootstrap happens before any tools exist; hiding the source made
        # a fresh connect look like nothing happened — UX round on #1130).
        if src["id"] not in granted_ids and not stored and not caller_is_admin:
            continue
        sources.append(
            {
                "id": src["id"],
                "name": src["name"],
                "transport": src.get("transport"),
                "hint_html": render_safe(src.get("connect_hint")),
                "auth_kind": "oauth" if is_oauth else "secret",
                "has_secret": has_secret,
                "stored": stored,
                "has_tools": src["id"] in granted_ids,
                "source_has_tools": src["id"] in sourced_ids,
                # One definition for "this viewer has authority here", used by
                # every control whose endpoint calls `_require_source_grant`
                # unconditionally — Connect/Reconnect, Test, and the paste
                # field + Save. Deciding that per control is what let the same
                # dead-end bug be reported three separate times on this PR;
                # Disconnect/Remove stay outside it because their own-credential
                # carve-out means they work without a grant.
                "can_act": src["id"] in granted_ids or caller_is_admin,
                "updated_at": updated_at,
                "expires_at": expires_at,
            }
        )
    # Both banners render only fixed text: connect_error arrives as a short
    # code mapped through CONNECT_ERROR_MESSAGES (unknown → generic fallback),
    # and connected only renders when the caller really has a stored OAuth
    # connection for that id — a crafted link can never put its own words in
    # an Agnes banner. Checked against the token row, NOT this page's
    # tool-derived source list: a freshly registered source has no tools yet
    # and the admin's post-connect banner must still show (Devin Review on
    # #1130).
    from app.api.mcp_oauth_connect import CONNECT_ERROR_FALLBACK, CONNECT_ERROR_MESSAGES

    error_code = request.query_params.get("connect_error") or ""
    connected = request.query_params.get("connected") or ""
    connected_name = ""
    if connected:
        if mcp_user_oauth_tokens_repo().get(connected, user["id"]) is None:
            connected = ""
        else:
            src_row = mcp_sources_repo().get(connected)
            # Show the human name, not a UUID (UX round on #1130).
            connected_name = (src_row or {}).get("name") or connected
    # `retry` names the source a failed connect can be retried against —
    # rendered as a one-click "Try again" link. Validated against the cards
    # the caller can actually authorize against, NOT merely the listed ones:
    # since this page started showing a card for a stored-but-ungranted
    # source, "listed" stopped implying "connectable", and retrying a
    # not_granted failure would just fail again.
    retry = request.query_params.get("retry") or ""
    if not error_code or retry not in {s["id"] for s in sources if s["can_act"]}:
        retry = ""
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=caller_is_admin,
        connect_sources=sources,
        highlight_source=request.query_params.get("source") or "",
        connected_source=connected,
        connected_name=connected_name,
        connect_error=CONNECT_ERROR_MESSAGES.get(error_code, CONNECT_ERROR_FALLBACK) if error_code else "",
        retry_source=retry,
    )
    return templates.TemplateResponse(request, "me_connections.html", ctx)


@router.get("/me/activity", response_class=HTMLResponse)
async def me_activity_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unified personal-activity page — consolidated replacement for
    the old ``/me/stats`` + ``/profile/sessions`` split.  Four tabs
    (Sessions / Token usage / Data access / Sync activity) backed by
    ``/api/me/stats/*`` endpoints.  The Sessions tab merges usage
    metrics with verification-pipeline status and download links.
    """
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "me_activity.html", ctx)


@router.get("/me/stats", response_class=HTMLResponse)
async def me_stats_redirect(request: Request):
    """Legacy redirect — ``/me/stats`` → ``/me/activity``."""
    return RedirectResponse(url="/me/activity", status_code=301)


@router.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Permalink page for the latest published news. Renders empty-state
    copy when no version is published. Authed-only (same as /home).
    """
    news = news_template_repo().get_current_published()
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
        news=news,
    )
    return templates.TemplateResponse(request, "news.html", ctx)


@router.get("/admin/news", response_class=HTMLResponse)
async def admin_news_editor(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin authoring surface — current published banner, draft editor,
    versions table. JS hits the /api/admin/news/* endpoints for the
    write paths."""
    repo = news_template_repo()
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=True,
        news_current=repo.get_current_published(),
        news_draft=repo.get_active_draft(),
        news_versions=repo.list_versions(limit=50),
    )
    return templates.TemplateResponse(request, "admin/news_editor.html", ctx)


@router.get("/setup-advanced", response_class=HTMLResponse)
async def setup_advanced_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Advanced setup reference — VS Code layout, recommended plugins,
    multi-model second opinions, custom skills, cost guidance.

    Pulls the deeper Chief-of-Stuff guide content out of /home so /home
    stays scannable for first-hour onboarding. Linked from /home's
    "Want to look around first?" explore card and from any deep-link
    anchors emitted by other pages (e.g. /home's auto-mode block points
    at #yolo).
    """
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "setup_advanced.html", ctx)


def _resolve_in_stack_is_local(explicit: Optional[bool]) -> bool:
    """Whether `in_stack` on these cards means "a local copy exists".

    True only under auto-membership, where membership follows from the grant
    and the subscribe control is left governing the download alone. Under the
    classic subscribe model the key means membership again, so the wording and
    the removal semantics keyed off this flag (#1206) must stay off: "Remove
    local copy" on a control that unsubscribes tells the user they are freeing
    disk while they are giving up access.

    Callers may pass an explicit value; ``None`` asks the instance.
    """
    if explicit is not None:
        return explicit
    from app.instance_config import get_stack_auto_membership

    return get_stack_auto_membership()


def _data_package_entry_dict(
    entry,
    drilldown_url: str,
    table_count: int = 0,
    source_types: Optional[list] = None,
    is_admin_view: bool = False,
    in_stack_is_local: Optional[bool] = None,
) -> dict:
    """Adapt a ResourceEntry → template entry dict for the _stack_card macro.

    Always renders a meta line (`N tables` — even `0 tables`) and a
    description fallback so packages without an admin-authored
    description don't render as half-empty cards.

    Empty-package CTA: when ``table_count == 0`` AND the viewer is admin,
    the meta line becomes an inline link to ``/admin/tables?assign_to=<id>``
    so admins can jump straight into the bulk-assign flow without first
    having to discover the chip-input hidden in each table's edit modal.

    Auto-membership: every entry reaching this adapter is already granted
    (in the caller's stack), so the dict's ``in_stack`` key is sourced from
    ``entry.materialized`` rather than ``entry.in_stack`` (always True post
    auto-membership) — this keeps the legacy ``_stack_card`` macro's
    Add/Remove toggle meaningful: it now drives the LOCAL DOWNLOAD state
    (subscribe = keep a local copy), not stack membership.
    """
    description = entry.description or (
        f"Bundle of {table_count} table{'s' if table_count != 1 else ''}. "
        f"Download locally so `agnes pull` syncs the data to your workspace."
    )
    out = {
        "id": entry.id,
        "name": entry.name,
        "description": description,
        "icon": entry.icon or "📦",
        "color": entry.color or "#e0f2fe",
        # v50: cover image (admin-uploaded JPG/PNG/WebP). _stack_card.html
        # renders it as <img> when set, falling back to the flat-color +
        # initials banner when None. Closes the visual gap with
        # /marketplace cards that have always shown real cover photos.
        "cover_image_url": getattr(entry, "cover_image_url", None),
        # v51: lifecycle status + classification category. Drive the
        # cover-corner status pill and the eyebrow line above the title.
        "status": getattr(entry, "status", None) or "prod",
        "category": getattr(entry, "category", None),
        "requirement": entry.requirement,
        "in_stack": getattr(entry, "materialized", False),
        # #1206: says OUT LOUD what the line above did quietly. Under
        # auto-membership `in_stack` no longer means "in the caller's
        # stack" — every granted resource already is — it means "a local
        # copy exists". The card macro cannot infer that from the key
        # name, so it read the old meaning and invited you to "Add to
        # stack" a package listed under My Stack.
        #
        # Gated on the membership mode rather than hardcoded True. Under the
        # classic subscribe model `in_stack` means membership again, and the
        # flag would have the card offer "Remove local copy" for a control
        # that actually unsubscribes — the user loses ACCESS believing they
        # are freeing disk. Consumers that do not re-point the key omit the
        # flag and keep the old wording; classic is one of them.
        "in_stack_is_local": _resolve_in_stack_is_local(in_stack_is_local),
        "meta": f"{table_count} table{'s' if table_count != 1 else ''}",
        # The same fact as a NUMBER, for callers that compose their own meta
        # line (the admin index card reads "3 tables · Finance · Data Team")
        # or that flag the empty package as the state it is.
        "table_count": table_count,
        # v56: source-type pills (auto-derived) come first per the spec
        # convention; admin-authored category tags follow. Concatenated
        # into the single ``tags`` field the macro renders. Duplicates
        # collapsed via dict-order-preserving filter.
        "tags": list(dict.fromkeys(list(source_types or []) + list(getattr(entry, "tags", None) or []))),
        # v56: extended attribution + derived badges. Macro reads these
        # via class hooks (data-card-owner, data-badge="...").
        "owner_name": getattr(entry, "owner_name", None),
        "owner_team": getattr(entry, "owner_team", None),
        "badges": getattr(entry, "badges", None) or [],
        # v113: the stored trust claim, so the card renders the SAME shared
        # marker as the Library row and the detail hero. It replaced a derived
        # `curated` badge that read the creator's current Admin-group
        # membership, which is how a card and its own detail page could
        # disagree. None for resource types with no publisher axis.
        "publisher_kind": getattr(entry, "publisher_kind", None),
        "drilldown_url": drilldown_url,
        "footer_left": (f"View {table_count} table{'s' if table_count != 1 else ''} →" if table_count else "Open →"),
    }
    if table_count == 0 and is_admin_view:
        # `entry.id` is a server-generated uuid (data_packages.id), safe to
        # inline. `assign_to` is read by admin_tables.html on load to auto-
        # open the Bulk Assign modal with this package pre-selected.
        out["meta_html"] = (
            f'0 tables — <a href="/admin/tables?assign_to={entry.id}" '
            f'style="color:var(--ds-primary);">assign some →</a>'
        )
    return out


def _facts_repo_if_available() -> Optional[Any]:
    """The facts repo when the ``facts`` feature flag is on AND the active
    backend is Postgres, else ``None`` — the single "is the facts UI even
    reachable" gate every facts-aware web surface uses (spec §13.2: "flag
    off / DuckDB backend / zero facts -> the section simply absent, no
    error, no empty shell"). Swallows ``RequiresPostgresBackend`` (frozen
    DuckDB app-state) and any other resolution error so a facts-adjacent
    page never 500s over what is, this round, an optional surface."""
    from app.instance_config import feature_enabled

    if not feature_enabled("facts", "enabled", env_var="AGNES_FACTS_ENABLED", default=False):
        return None
    try:
        from src.repositories import facts_repo

        return facts_repo()
    except Exception:
        return None


# ── Unified catalog-card normalizers ─────────────────────────────────
# Adapt an entry dict -> the single `c` contract consumed by the reusable
# catalog_card() macro (templates/macros/_catalog_card.html) and its JS
# twin. The data-package and memory adapters died with the /catalog browse
# shell (folded into /library); the upload adapter below is the survivor
# (the Library's Artefacts band renders through it).


def _catalog_card_upload(c: dict) -> dict:
    """Private artefact → catalog_card `c`. An artefact is a `file_corpora`
    container, but its presentation ADAPTS to how many files it holds:

    - exactly one file → it reads as **that file** (title = filename, single-
      document glyph, ``TYPE · size`` meta, label "File"). A lone dropped file
      never looks like "a collection with 1 file".
    - two or more → it reads as a **Collection** (title = name, two-sheet glyph,
      ``N files`` meta, label "Collection").

    Artefacts aren't stack-toggled (they're owned files); the action opens the
    detail page, where adding a second file promotes a File into a Collection.

    ``fact_count`` (spec §13.2 "Library" — a collection card reads "N files ·
    M facts"), when > 0, is appended to the meta line on both branches. It is
    the caller-scoped `facts` count the route already computed via
    `count_visible_facts_for_collection` (feature-flagged, PG-only, absent
    entirely when there are no facts) — never re-derived here."""
    n = c.get("file_count", 0) or 0
    fact_count = c.get("fact_count", 0) or 0
    fact_suffix = f" · {fact_count} fact{'s' if fact_count != 1 else ''}" if fact_count else ""
    ff = c.get("first_file") or None
    if n == 1 and ff:
        size = _human_size(ff.get("size_bytes") or 0)
        fname = ff.get("filename")
        # Title is the artefact's NAME (what the caller typed), not the
        # filename — otherwise several single-file artefacts with distinct
        # names all render as the same filename. The filename + size move to
        # the meta line so the file's identity stays visible.
        meta = (f"{fname} · {size}" if fname else size) + fact_suffix
        return {
            "kind": "library",
            "glyph": "doc",  # single-document glyph — see kind_glyph()
            "kind_label": "File",
            "title": c["name"] or fname,
            "href": f"/library/{c['slug']}",
            "curator": None,
            "category": None,
            "description": c.get("description") or "A private file — searchable by your agents.",
            "tags": [],
            "meta_icon": "doc",
            "meta_text": meta,
            "action": {"mode": "link", "href": f"/library/{c['slug']}", "label": "Open"},
        }
    return {
        "kind": "library",
        "glyph": "library",  # two-sheet "collection of files" glyph
        "kind_label": "Collection",
        "title": c["name"],
        "href": f"/library/{c['slug']}",
        "curator": None,
        "category": None,
        "description": c.get("description") or "A private collection of files — searchable by your agents.",
        "tags": [],
        "meta_icon": "doc",
        "meta_text": f"{n} file{'s' if n != 1 else ''}" + fact_suffix,
        "action": {"mode": "link", "href": f"/library/{c['slug']}", "label": "Open"},
    }


def _catalog_card_stack_artefact(
    col: dict,
    *,
    visibility: str,
    visibility_label: str,
    owner_label: str,
    accessible: bool,
) -> dict:
    """Artefact-in-My-Stack → catalog_card `c`. Modeled on
    `_catalog_card_upload` for the file-vs-collection title/glyph/meta_text
    logic, but the action is Remove-from-Stack (never Delete — removing a
    Stack membership must never touch the underlying artefact) instead of a
    plain "Open" link, and it carries the visibility/owner metadata the
    `stack_row` macro's Source column needs.

    ``accessible=False`` means the artefact is still IN the caller's Stack
    (a membership row is never dropped silently just because access
    changed — see requirement 7) but the caller can no longer reach the
    underlying collection; the row's description is overridden to explain
    that and the caller-facing card sets ``unavailable`` so the template can
    render the badge. Remove-from-Stack stays available either way.
    """
    n = col.get("file_count", 0) or 0
    ff = col.get("first_file") or None
    rid = col["id"]
    if n == 1 and ff:
        size = _human_size(ff.get("size_bytes") or 0)
        fname = ff.get("filename")
        meta_text = f"{fname} · {size}" if fname else size
        kind_label = "File"
        title = col.get("name") or fname
        glyph = "doc"
    else:
        meta_text = f"{n} file{'s' if n != 1 else ''}"
        kind_label = "Collection"
        title = col.get("name")
        glyph = "library"

    description = col.get("description") or ""
    if not accessible:
        description = "You no longer have access to this artefact."

    c: dict = {
        "kind": "library",
        "glyph": glyph,
        "kind_label": kind_label,
        "title": title,
        "href": f"/library/{col.get('slug')}",
        "curator": None,
        "category": None,
        "shared_by": None,
        "description": description,
        "tags": [],
        "meta_icon": "doc",
        "meta_text": meta_text,
        "unavailable": not accessible,
        "action": {"mode": "stack", "remove_url": f"/api/stack/artefacts/{rid}"},
    }
    # Drives the stack_row macro's Source column (stack_source()): the scope
    # words are a category label; "shared with me" names the owner; "shared by
    # me" (caller owns it, shared to a non-Everyone group) sets category too,
    # since shared_by would resolve to "You" and fall through to the macro's
    # em-dash branch.
    #
    # Same words as the Library, off the same map: this column and the Library's
    # Sharing badge state the same fact about the same item, so an item cannot be
    # "Everyone" on one page and "Workspace" on the other.
    from app.services.artefact_access import VISIBILITY_LABELS

    if visibility in ("workspace", "private"):
        c["category"] = VISIBILITY_LABELS[visibility]
    elif owner_label == "You":
        c["category"] = VISIBILITY_LABELS["shared"]
    else:
        c["shared_by"] = owner_label
    return c


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """The Catalog is folded into the Library.

    It browsed the same rows the Library already renders — data packages,
    memory domains, marketplace plugins and recipes, off the same
    ``StackResolver.browse()`` — sliced by kind tabs where the Library groups
    by type into collapsible sections. Two destinations for one list, which is
    the split the Library/Memory/Data-apps merge already named as the problem
    (spec 2026-08-12) and never got round to closing here.

    The one thing this page had that the list did not is the "what could I
    add" question, which is now the Library's "Not in stack yet" filter —
    the `?scope=available` this sends arrives with that toggle applied.

    302, not 308, so a reversal is not cached permanently — same reasoning as
    the /corporate-memory, /apps and /stack retirements. The route stays
    registered so bookmarks and old links do not 404, and every detail route
    under it (``/catalog/p/{slug}``, ``/catalog/t/{id}``, ``/catalog/r/{slug}``,
    ``/catalog/semantics``) is untouched: this folds the browse SHELL only.
    """
    return RedirectResponse(url="/library?scope=available", status_code=302)


@router.get("/stack", response_class=HTMLResponse)
async def my_stack_page(user: dict = Depends(get_current_user)):
    """My Stack is retired — folded into the Library (#1088).

    Direction (a) from the issue thread: "the Library absorbs the Stack. No
    migration of functionality is required — it is already there." /library
    already renders every kind My Stack did (data packages, memory domains,
    marketplace plugins, uploads) off the same ``StackResolver.browse()``
    call, with per-row ``stack_state`` membership controls, and its "In
    stack only" toggle (the ``?stack=in_stack`` preset this redirect targets)
    answers the exact question this page existed to answer.

    302, not 308, so a later reversal is not cached permanently — same
    reasoning as the /corporate-memory and /apps retirements (#1278). Route
    kept registered (not removed) so old links, bookmarks and the onboarding
    tour's stored history don't 404.
    """
    return RedirectResponse(url="/library?stack=in_stack", status_code=302)


# Artefact type facets for the /artefacts toolbar filter. A single-file
# artefact filters by its file's kind (so "Images", "Spreadsheets" etc. group
# naturally); a multi-file artefact is always a "Collection". Keys are stable
# filter tokens (emitted as data-type); labels are what the dropdown shows.
_ARTEFACT_TYPE_FACETS: dict[str, tuple[str, str]] = {
    "pdf": ("pdf", "PDF"),
    "doc": ("document", "Document"),
    "docx": ("document", "Document"),
    "txt": ("document", "Document"),
    "md": ("document", "Document"),
    "rtf": ("document", "Document"),
    "odt": ("document", "Document"),
    "csv": ("spreadsheet", "Spreadsheet"),
    "tsv": ("spreadsheet", "Spreadsheet"),
    "xls": ("spreadsheet", "Spreadsheet"),
    "xlsx": ("spreadsheet", "Spreadsheet"),
    "ods": ("spreadsheet", "Spreadsheet"),
    "ppt": ("presentation", "Presentation"),
    "pptx": ("presentation", "Presentation"),
    "png": ("image", "Image"),
    "jpg": ("image", "Image"),
    "jpeg": ("image", "Image"),
    "gif": ("image", "Image"),
    "svg": ("image", "Image"),
    "webp": ("image", "Image"),
    "heic": ("image", "Image"),
    "json": ("data", "Data"),
    "ndjson": ("data", "Data"),
    "parquet": ("data", "Data"),
}


def _file_ext(first_file: dict | None) -> str:
    """Lowercased extension for a stored file, from its recorded ``file_type`` or
    else from its filename. "" when neither carries one."""
    if not first_file:
        return ""
    ext = (first_file.get("file_type") or "").lower().lstrip(".")
    if not ext:
        fn = first_file.get("filename") or ""
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    return ext


def _artefact_type(file_count: int, first_file: dict | None) -> tuple[str, str]:
    """(filter_key, label) for the type facet. Multi-file → Collection; a lone
    file → its extension's facet, falling back to a generic File."""
    if file_count != 1 or not first_file:
        return ("collection", "Collection")
    return _ARTEFACT_TYPE_FACETS.get(_file_ext(first_file), ("file", "File"))


def _artefact_format(first_file: dict | None) -> str:
    """The file's FORMAT as the Library's Name cell shows it — "PNG", "SVG",
    "PDF". This is what a file row prints where every other kind prints a
    description: for a file, "A private file — searchable by your agents." was
    boilerplate identical on every row, while the format is the one fact about a
    file the name may not already carry.

    Deliberately the concrete format, not `_artefact_type`'s facet label: the
    facet buckets svg/png/heic all as "Image", which the row's own glyph already
    conveys. "" when there is no extension to read, so the caller can fall back
    to the description rather than print an empty line. Bounded to a plausible
    extension (short, alphanumeric) so a dotted filename can't push an arbitrary
    slice of itself into the cell."""
    ext = _file_ext(first_file)
    return ext.upper() if ext and len(ext) <= 8 and ext.isalnum() else ""


# ---------------------------------------------------------------------------
# Library — the caller's own things: artefacts (files/images/documents) and
# skills. Agents live on /agents. See ``library_page``.
# ---------------------------------------------------------------------------

#: Store-entity visibility_status -> (visibility_key, label) for a skill row.
#: A store entity is readable by every authenticated user once approved, so
#: "approved" IS workspace-wide sharing; anything else is still private to its
#: owner. Keys match the artefact/agent visibility vocabulary so one filter and
#: one chip style covers all three kinds (the label vocabulary itself is owned
#: by ``collection_visibility`` — see its docstring for why "Everyone" and not
#: "Workspace").
#:
#: The two REVIEW states carry their own word rather than the scope word, and
#: that is the point: "In review" and "Archived" are the most actionable things
#: this slot ever says, and they are only true of a store entity. They are also
#: why the slot must never be overwritten with an editability phrase like
#: "Shared with you" — see ``_library_row_base`` callers below.
_SKILL_VISIBILITY: dict[str, tuple[str, str]] = {
    "approved": ("workspace", "Everyone"),
    "pending": ("private", "In review"),
    # 'hidden' is now the state the builder writes for "Private" access.
    "hidden": ("private", "Private"),
    "archived": ("private", "Archived"),
}


#: Tooltips for a membership the caller cannot drop. Two kinds of row reach
#: these — an auto-membership grant (data package / memory domain / recipe,
#: where the grant IS the membership) and the subset of plugin rows the
#: uninstall API refuses (``is_system`` or required-tier). Module constants
#: rather than literals at each site because they are the same sentence
#: making the same promise, and ``tests/test_web_library.py`` asserts them
#: verbatim so the shipped copy cannot drift from the spec.
_LOCKED_STACK_TOOLTIP = "Required by your admin and cannot be removed from your stack."
_GRANTED_STACK_TOOLTIP = "Granted to your group — only an admin can remove it from your stack."


def _library_row_base(
    *,
    item_id: str,
    kind: str,
    title: str,
    description: str,
    href: str,
    glyph: str,
    type_key: str,
    type_label: str,
    origin: str,
    origin_label: str,
    added_iso: str | None,
    owner_label: str,
    ownership: str,
    visibility: str,
    visibility_label: str,
    meta_text: str,
    share_type: str | None,
    extra_search: str = "",
    requirement: str = "optional",
    tags: list | None = None,
    owner_key: str | None = None,
) -> dict:
    """Assemble one Library row.

    Every kind (artefact / skill / agent) funnels through this so the table,
    the grid projection and the toolbar facets read the same field names
    regardless of which registry the item came from.

    ``share_type`` is the ``resource_grants`` resource type the Share dialog
    should PUT to, or ``None`` for a kind that isn't grant-shareable (skills —
    see ``app/services/library_sharing.py``).
    """
    return {
        "id": item_id,
        "kind": kind,
        # Stack state. "in_stack" = the default agent can already use this;
        # "available" = the caller can reach it but it isn't in their Stack.
        # EVERY row that can be filtered carries one of the two — a row with no
        # state would be silently dropped by the "In stack only" toggle while its
        # own pill claimed membership. The row renders the state as the
        # presence/absence of the "In stack" badge, so the filter never hides
        # anything for an invisible reason.
        "stack_state": "",
        "stack_title": "",
        # What the pill READS on the row, kept separate from ``stack_state`` so
        # the wording can differ from the filtered value. Empty → the template
        # falls back to "In stack".
        "stack_pill": "",
        # Membership the caller cannot drop — any group grant, whichever tier.
        # It reads the SAME "In stack" as any other member (it is one) and is
        # marked by a LOCK plus a tooltip naming who *can* remove it. The tier
        # is an attribute of the membership, not a different state, and the
        # separate Optional/Required facet is where the tier is filterable.
        # Driving the lock off this flag rather than off the pill's text keeps
        # the wording and the affordance independent; driving the FLAG off
        # droppability rather than off the tier is what keeps a non-removable
        # row from wearing the removable row's rest state.
        "stack_locked": False,
        # What Add/Remove writes to, and which of the two the caller may do.
        # Artefacts and store entities have different membership APIs, so the row
        # carries its own endpoint rather than the template guessing from `kind`.
        "stack_endpoint": "",
        "stack_addable": False,
        "stack_removable": False,
        "title": title,
        "description": description,
        "href": href,
        "glyph": glyph,
        "type_key": type_key,
        "type_label": type_label,
        "origin": origin,
        "origin_label": origin_label,
        "added_iso": added_iso,
        "owner_label": owner_label,
        "ownership": ownership,
        "visibility": visibility,
        "visibility_label": visibility_label,
        "meta_text": meta_text,
        "share_type": share_type,
        "shareable": share_type is not None,
        # Facet fields. ``requirement`` is the grant tier an admin set, shown
        # in the vocabulary every admin surface speaks: "required" (the API
        # enum) reads **Automatic** — in members' stacks without an add —
        # and everything else reads **Optional**. ``tags`` reuses
        # whatever the source registry already carries (data-package tags,
        # store-entity category) — no generic tagging table exists, so kinds
        # without tags simply never match a Tags filter. ``owner_key`` is the
        # stable value the Owner facet groups on (the label is what's shown).
        "requirement": requirement,
        "requirement_label": "Automatic" if requirement == "required" else "Optional",
        "tags": tags or [],
        "owner_key": owner_key or "",
        "search": " ".join(s for s in (title, description, type_label, owner_label, extra_search) if s).lower(),
    }


def _has_readable_semantic_model(user: dict, conn, *, surface: str) -> bool:
    """Whether to offer this caller the ``/semantic-layer`` browse pages.

    The same ``_can_read_model`` gate those pages apply, so a caller who can
    read nothing is never sent to an empty page. Deliberately NOT gated on the
    flat projection's counts: a model with no metrics or glossary terms
    projected yet — the default state of an upstream document that declares
    datasets and relationships but no aggregations — still counts, because
    those objects are the one thing the browse UI exists to show.

    Best-effort by contract: every caller renders a LINK off this, so a
    ``semantic_models`` read failure must degrade to "no link" and leave the
    rest of the page intact rather than 500 it. ``surface`` only labels the log.

    Shared by ``/library``'s Definitions footer and ``/catalog/semantics``'
    header + empty state. One reader, because the two disagreeing is exactly
    how the standalone page came to claim "no metrics registered yet" on an
    instance whose Library was already offering the document next door.
    """
    try:
        from app.api.semantic_models import _can_read_model

        return any(_can_read_model(user, row, conn) for row in semantic_model_repo().list_all())
    except Exception as e:  # noqa: BLE001 - the link is best-effort
        logger.warning("%s: semantic-model existence check failed: %s", surface, e)
        return False


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Library — everything the caller has: artefacts, skills, and agents.

    This is the renamed and widened former ``/artefacts`` surface. It answers
    "what do I have?" across every kind Agnes governs — the caller's own things
    plus everything shared with them:

      - **Artefacts** — ``file_corpora`` uploads: images, documents, data
        files, and multi-file collections, whether *uploaded* by a person or
        *generated* by an agent (the Source facet).
      - **Skills** — the caller's own store entities of type ``skill``, i.e.
        what the Skill builder (/skills) publishes.
      - **Granted resources** — governed **data packages**, **memory
        domains**, **recipes** and curated **marketplace plugins** granted to
        one of the caller's groups, plus the community-store items they have
        **installed**. These are not the caller's to re-share, so they carry no
        Share action; the grant belongs to an admin.

    Agents are deliberately NOT here — they have their own home at ``/agents``
    (they are still real server-side rows in the v103 ``agents`` registry, and
    ``/api/v1/agents`` reads honour agent grants; the Library just isn't their
    listing surface).

    Scope is grant-aware: items you OWN plus anything shared into a group you
    belong to, tagged ``mine`` / ``shared_with_me`` / ``shared_by_me`` so the
    toolbar can slice by ownership. Deliberately NOT admin god-mode — an admin
    still sees their own Library, not every item in the instance (the audit
    view is /admin/access).

    Every row carries its real visibility (Private / Shared / Workspace) and,
    for the grant-backed kinds, a Share action writing through
    ``PUT /api/sharing/{resource_type}/{resource_id}``. Skills are the
    exception: an approved store entity is already readable by every
    authenticated user, so the row reports that state rather than offering a
    group grant nothing would read.

    Rendering is server-side; the toolbar (search, ownership segments, Type +
    Source facets, sort, and the table ⇄ grid switch) is client-side over
    those rows.
    """

    from app.resource_types import ResourceType
    from app.services.artefact_access import (
        VISIBILITY_LABELS,
        build_artefact_access_context,
        collection_visibility,
    )
    from app.services.journey import mark_journey
    from src.db import SYSTEM_EVERYONE_GROUP

    uid = user.get("id") or ""
    ct = ResourceType.COLLECTION.value

    # The onboarding step is literally "Explore your Library" — so looking at it
    # completes it. It used to need a click on the checklist row instead, which
    # made the row a box to tick rather than a thing to do: someone who had spent
    # ten minutes in here still had it listed as outstanding. Best-effort and
    # swallowed (see app/services/journey.py) — a bookkeeping write must never
    # fail a page render.
    mark_journey(uid, explored_stack=True)

    access_ctx = build_artefact_access_context(uid)
    granted_to_me = access_ctx.granted_to_me
    shared_ids = access_ctx.shared_ids
    owner_name = access_ctx.owner_name

    # Per-file sharing state, tallied in ONE pass. A file inside a folder is
    # independently shareable, so each needs its own private/shared/workspace
    # verdict — resolving that with a visibility_for() call per row would
    # re-read every resource_grants row once per file.
    cft = ResourceType.CORPUS_FILE.value
    try:
        _everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        _everyone_id = _everyone["id"] if _everyone else None
    except Exception:
        _everyone_id = None
    file_shared_groups: dict = {}
    try:
        for g in resource_grants_repo().list_all(resource_type=cft):
            file_shared_groups.setdefault(g["resource_id"], set()).add(g["group_id"])
    except Exception as e:
        logger.warning("/library: could not resolve per-file grants: %s", e)

    def file_visibility(file_id: str) -> str:
        groups = file_shared_groups.get(file_id)
        if not groups:
            return "private"
        if _everyone_id and _everyone_id in groups:
            return "workspace"
        return "shared"

    # Artefacts already in the caller's Stack — drives the "Add to stack" vs
    # quiet "In stack" badge on artefact rows.
    try:
        in_stack_ids = set(user_stack_subscriptions_repo().list_for_user(uid, ct))
    except Exception as e:
        logger.warning("/library: could not resolve stack membership: %s", e)
        in_stack_ids = set()

    items: list = []

    # ── Artefacts (file_corpora) ──────────────────────────────────────────
    fc_repo = file_corpora_repo()
    cf_repo = corpus_files_repo()
    # Resolved ONCE, not per collection: the flag/backend check is the same
    # for every row, and a fresh count query per row is only worth paying
    # when the surface is actually on (spec §13.2 "Library" — "N files ·
    # M facts").
    facts_repo_ = _facts_repo_if_available()
    try:
        for col in fc_repo.list():
            owned = col.get("created_by") == uid
            if not owned and col["id"] not in granted_to_me:
                continue  # not yours and not shared with you -> invisible here
            try:
                files = cf_repo.list_for_corpus(col["id"])
            except Exception:
                files = []
            file_count = len(files)
            first_file = None
            if file_count == 1:
                f0 = files[0]
                first_file = {
                    "filename": f0.get("filename"),
                    "file_type": f0.get("file_type"),
                    "size_bytes": f0.get("size_bytes"),
                }
            fact_count = 0
            if facts_repo_ is not None:
                try:
                    fact_count = facts_repo_.count_visible_facts_for_collection(user, col["id"])
                except Exception as e:
                    logger.warning("/library: fact count failed for %s: %s", col["id"], e)
            c = _catalog_card_upload(
                {
                    "id": col["id"],
                    "name": col.get("name") or col.get("slug"),
                    "description": col.get("description") or "",
                    "slug": col.get("slug"),
                    "file_count": file_count,
                    "first_file": first_file,
                    "fact_count": fact_count,
                }
            )
            shared = col["id"] in shared_ids
            if not owned:
                ownership = "shared_with_me"
                owner_label = owner_name.get(col.get("created_by"), "Someone")
            elif shared:
                ownership = "shared_by_me"
                owner_label = "You"
            else:
                ownership = "mine"
                owner_label = "You"
            visibility, visibility_label = collection_visibility(access_ctx, col["id"])
            file_type_key, file_type_label = _artefact_type(file_count, first_file)
            origin = col.get("origin") or "uploaded"
            created = col.get("created_at")
            fname = first_file.get("filename") if first_file else ""
            is_folder = file_count != 1
            row = _library_row_base(
                item_id=col["id"],
                kind="artefact",
                title=c.get("title") or "",
                description=c.get("description") or "",
                href=f"/library/{col.get('slug')}",
                # A collection sits beside loose files in the Files table, so it
                # wears a FOLDER glyph there — the container it actually is, and
                # the shape that reads as "you can drop a file on me". Its own
                # detail page keeps the two-sheet 'library' hero glyph.
                glyph="folder" if is_folder else (c.get("glyph") or "doc"),
                # Files and folders share ONE section; the per-row Type label
                # still names the real thing (Image / PDF / Collection / …) so
                # the merge doesn't cost the reader any information.
                type_key="files",
                type_label=file_type_label,
                origin=origin,
                origin_label="Generated" if origin == "generated" else "Uploaded",
                added_iso=created.isoformat() if created is not None else None,
                owner_label=owner_label,
                ownership=ownership,
                visibility=visibility,
                visibility_label=visibility_label,
                meta_text=c.get("meta_text") or "",
                share_type=ct,
                extra_search=fname or "",
                owner_key="me" if owned else (col.get("created_by") or ""),
            )
            # Artefact-only affordances: Stack membership + file-count sort key.
            row["in_stack"] = col["id"] in in_stack_ids
            row["stack_state"] = "in_stack" if row["in_stack"] else "available"
            row["stack_title"] = (
                "The default agent can use this artefact"
                if row["in_stack"]
                else "You can reach this, but the default agent can't until you add it"
            )
            # An artefact is the one kind whose membership IS the caller's to
            # set (no admin grant tier exists for a personal upload), so its
            # pill is a real toggle and the template supplies the button copy.
            # This value is what the *child* rows fall back to — a file inside
            # a folder shows its folder's state as a plain badge.
            row["stack_pill"] = "In stack"
            # Membership here is a `user_stack_subscriptions` row, and it is the
            # caller's to add or drop either way. Children deliberately inherit
            # neither flag: Stack membership is per collection, so a file inside a
            # folder is added by adding its folder.
            row["stack_endpoint"] = f"/api/stack/artefacts/{col['id']}"
            row["stack_addable"] = not row["in_stack"]
            row["stack_removable"] = bool(row["in_stack"])
            row["file_count"] = file_count
            # Folder vs loose file. A folder is a drop target and expands to its
            # children; a loose file is draggable INTO a folder. Both keep their
            # own detail page and their own sharing.
            row["is_folder"] = is_folder
            row["file_kind"] = file_type_key
            # What a LOOSE FILE's row prints on its second line, in place of the
            # description every file shared word for word. A collection prints its
            # file count there instead, so it needs none.
            row["file_format"] = "" if is_folder else _artefact_format(first_file)
            # A loose file's ROW id is its collection id (a single-file artefact
            # IS its collection), but moving it needs the corpus_files id — so
            # carry that separately rather than making the drag guess.
            row["file_id"] = files[0]["id"] if (not is_folder and files) else ""
            # The slug + the file's own name are what a client-side move needs to
            # rebuild the moved row (its per-file URL is /library/{slug}/f/{id},
            # and as a child it is titled by its FILENAME, not the artefact name)
            # without a round-trip to re-render the page.
            row["slug"] = col.get("slug") or ""
            row["file_name"] = fname or ""
            # The collection's OWN description, undefaulted. A collection row shows
            # its file COUNT where the description would go, so the real thing has
            # to travel separately for the client-side 1-file transition to restore
            # it (the file default differs from the collection default).
            row["own_description"] = col.get("description") or ""
            row["children"] = []
            if is_folder:
                slug = col.get("slug")
                for f in files:
                    ftype_key, ftype_label = _artefact_type(1, f)
                    fsize = f.get("size_bytes")
                    child = _library_row_base(
                        item_id=f["id"],
                        kind="artefact",
                        title=f.get("filename") or "Untitled file",
                        description="",
                        # Per-file detail page (files inside a folder had none).
                        href=f"/library/{slug}/f/{f['id']}",
                        glyph="doc",
                        type_key="files",
                        type_label=ftype_label,
                        origin=origin,
                        origin_label="Generated" if origin == "generated" else "Uploaded",
                        added_iso=(f.get("created_at").isoformat() if f.get("created_at") is not None else None),
                        owner_label=owner_label,
                        ownership=ownership,
                        # A file's own sharing is independent of its folder's.
                        visibility=file_visibility(f["id"]),
                        visibility_label="",
                        meta_text=_human_size(fsize) if fsize else "",
                        share_type=ResourceType.CORPUS_FILE.value,
                        owner_key="me" if owned else (col.get("created_by") or ""),
                    )
                    child["file_kind"] = ftype_key
                    # A file inside a folder is titled by its FILENAME and carries
                    # no description at all, so before this its second line was
                    # blank. It gets the same format line as a loose file — the
                    # nested rows are files too, and the retired Type column is
                    # where their format used to show.
                    child["file_format"] = _artefact_format(f)
                    child["file_id"] = f["id"]
                    child["file_name"] = f.get("filename") or ""
                    child["slug"] = slug or ""
                    child["is_folder"] = False
                    # Stack membership is per collection, so a file inherits its
                    # folder's state rather than claiming an independent one.
                    child["stack_state"] = row["stack_state"]
                    child["stack_title"] = row["stack_title"]
                    child["stack_pill"] = row["stack_pill"]
                    child["parent_id"] = col["id"]
                    # Same vocabulary as every other row — read off the shared
                    # map rather than restated here, which is how this slot came
                    # to hold a fourth spelling of the same three states.
                    child["visibility_label"] = VISIBILITY_LABELS.get(child["visibility"], VISIBILITY_LABELS["private"])
                    row["children"].append(child)
            items.append(row)
    except Exception as e:
        logger.warning("/library: could not enumerate artefacts: %s", e)

    # ── Store entities the caller may see: SKILLS and PLUGINS ─────────────
    # The Library is the single source of truth for what a user can reach, so it
    # lists the store the same way the store itself decides visibility — the rule
    # /api/marketplace browse already uses: entries whose review state is
    # ``approved`` (readable by every authenticated user, i.e. shared with
    # everyone) UNION the caller's own entries whatever state they are in. The
    # repo's ``include_owner_id`` knob is exactly that union, and it already
    # excludes the owner's archived rows.
    #
    # Deliberately NOT swept: pending or hidden entries belonging to someone
    # else. An admin may be *able* to read those, but they are submissions in
    # review, not things shared with anyone — moderation lives at /admin/store.
    # AGENTS are also not swept: they keep their own surface at /agents. An agent
    # the caller INSTALLED still lists (further down), as it always has.
    #
    # Visibility and Stack membership stay two separate properties: visibility is
    # who can DISCOVER the entity (above), while an install row is what makes the
    # default agent actually USE it. So authoring an entity does not put it in
    # your Stack, and "Add to stack" / "Remove from stack" on these rows write
    # POST / DELETE /api/store/entities/{id}/install.
    installed_store: dict = {}
    try:
        for inst in user_store_installs_repo().list_for_user(uid):
            installed_store[inst["id"]] = inst
    except Exception as e:
        logger.warning("/library: could not resolve store installs: %s", e)

    for _etype, _type_label in (("skill", "Skill"), ("plugin", "Plugin")):
        try:
            _entities, _total = store_entities_repo().list(
                type=_etype,
                visibility_status=["approved"],
                include_owner_id=uid,
                limit=1000,
            )
        except Exception as e:
            logger.warning("/library: could not enumerate %ss: %s", _etype, e)
            continue
        for s in _entities:
            status = s.get("visibility_status") or "pending"
            if status == "archived":
                continue  # soft-deleted; hidden from every listing
            owned = (s.get("owner_user_id") or "") == uid
            version = s.get("version") or ""
            meta_bits = [b for b in (s.get("category") or "", f"v{version}" if version else "") if b]
            created = s.get("created_at")
            if owned:
                # The caller's own entry reports its real store state, which is
                # the honest answer for an unpublished one ("In review",
                # "Private") as well as a published one ("Everyone").
                #
                # These rows are read-only in the Library (there is no
                # access-change endpoint for a store entity yet — see the TODO
                # on the badge in library.html), and they are exactly why
                # "cannot change" must NOT be labelled "Shared with you": this
                # entry is the caller's OWN, so that phrase would be false, and
                # it would erase "In review" — the one state whose whole value
                # is being visible at a glance.
                visibility, visibility_label = _SKILL_VISIBILITY.get(status, ("private", "Private"))
                owner_label, owner_key = "You", "me"
                ownership = "shared_by_me" if visibility == "workspace" else "mine"
                origin, origin_label = "built", "Built here"
            else:
                # Someone else's approved entry: shared with everyone here.
                # The label says the SCOPE ("Everyone"), not how the caller came
                # by it: an approved store entity is readable by every
                # authenticated user, so "Shared with you" would understate it —
                # it implies somebody picked this caller. The scope is known
                # here, so the more specific true thing wins.
                visibility, visibility_label = "workspace", "Everyone"
                owner_label = owner_name.get(s.get("owner_user_id"), "Someone")
                owner_key = s.get("owner_user_id") or ""
                ownership = "shared_with_me"
                origin, origin_label = "shared", "Shared with everyone"
            # v104: an organization-published item speaks for the instance, so
            # the Owner column names the organization rather than whoever
            # uploaded the bundle — including for the uploader themselves, since
            # publisher is a property of the item, not of who is looking.
            #
            # Deliberately NO Publisher facet on this page: the Ownership segment
            # (mine / shared with me / shared by me) already asks "whose is
            # this", and two controls for one question is what the marketplace
            # shelves got wrong. The Library gets the trust LABEL only.
            _publisher_kind = s.get("publisher_kind") or "user"
            if _publisher_kind == "organization":
                owner_label = "Your organization"
            items.append(
                _library_row_base(
                    item_id=s["id"],
                    kind=_etype,
                    title=s.get("name") or "",
                    description=s.get("description") or "",
                    href=f"/marketplace/flea/{s['id']}?from=library",
                    glyph="plugins" if _etype == "plugin" else "doc",
                    type_key=_etype,
                    type_label=_type_label,
                    origin=origin,
                    origin_label=origin_label,
                    added_iso=created.isoformat() if created is not None else None,
                    owner_label=owner_label,
                    ownership=ownership,
                    visibility=visibility,
                    visibility_label=visibility_label,
                    meta_text=" · ".join(meta_bits),
                    # Store visibility, not a group grant — the badge reports the
                    # model rather than offering a grant nothing would read.
                    share_type=None,
                    tags=[s["category"]] if s.get("category") else [],
                    owner_key=owner_key,
                )
            )
            items[-1]["publisher_kind"] = _publisher_kind
            _is_verified = (s.get("verification_state") or "none") == "verified"
            items[-1]["verified"] = _is_verified
            # Explicit trust level for the 3-state chip: 'org' for organization
            # publishers, 'verified' for community items the instance has
            # reviewed, 'unverified' for everything else. The unverified branch
            # renders only when the opt-in flag is set (see template).
            if _publisher_kind == "organization":
                items[-1]["trust_level"] = "org"
            elif _is_verified:
                items[-1]["trust_level"] = "verified"
            else:
                items[-1]["trust_level"] = "unverified"
            # Stack membership: the install row, addable and removable by the
            # caller either way — including on their own entity.
            _inst = installed_store.get(s["id"])
            items[-1]["stack_endpoint"] = f"/api/store/entities/{s['id']}/install"
            if _inst:
                items[-1]["stack_state"] = "in_stack"
                items[-1]["stack_pill"] = "In stack"
                items[-1]["stack_removable"] = True
                items[-1]["stack_title"] = "The default agent can use this — click to remove it"
            else:
                items[-1]["stack_state"] = "available"
                items[-1]["stack_addable"] = True
                items[-1]["stack_title"] = (
                    "Yours, but not part of your Stack"
                    if owned
                    else "Available to you, but the default agent can't use it until you add it"
                )

    # ── Everything else the caller has ACCESS to ──────────────────────────
    # Beyond their own files/skills, the Library also lists what has been
    # SHARED WITH them: governed data packages, memory domains, recipes and
    # curated marketplace plugins granted to one of their groups, plus the
    # community-store items they have installed. None of these are theirs to
    # re-share, so they carry no Share action (``share_type=None``) — the grant
    # is an admin's to change. Their "Source" facet says where they came from.
    #
    # Deliberately NOT included: the whole public community store. An approved
    # store entity is readable by every authenticated user, so listing all of
    # them would make every user's Library a copy of /marketplace rather than
    # "the things I have". Installed items are the honest subset.
    from app.services.stack_resolver import StackResolver
    from src.repositories import marketplace_plugins_repo

    resolver = StackResolver()

    def _granted_ids(rt: str) -> set:
        try:
            return set(resource_grants_repo().list_resource_ids_for_user(uid, rt))
        except Exception as e:
            logger.warning("/library: could not resolve %s grants: %s", rt, e)
            return set()

    def _add_shared_row(
        *,
        item_id,
        title,
        description,
        href,
        glyph,
        type_key,
        type_label,
        origin,
        origin_label,
        added,
        meta_text,
        owner_label,
        requirement="optional",
        tags=None,
        owner_key=None,
        in_stack=True,
        droppable=False,
    ) -> None:
        """Append one access-granted row (never owner-shareable).

        ``droppable``: the membership is the caller's own subscription
        (classic mode, optional tier) — render the REMOVE control, exactly
        as /catalog offers for the same membership. Callers whose
        membership is the grant itself (auto-membership, recipes, plugins)
        leave it False and get the locked pill."""
        items.append(
            _library_row_base(
                item_id=item_id,
                kind=type_key,
                title=title or "",
                description=description or "",
                href=href,
                glyph=glyph,
                type_key=type_key,
                type_label=type_label,
                origin=origin,
                origin_label=origin_label,
                added_iso=added.isoformat() if added is not None else None,
                owner_label=owner_label,
                ownership="shared_with_me",
                visibility="shared",
                visibility_label="Shared with you",
                meta_text=meta_text,
                share_type=None,
                requirement=requirement,
                tags=tags,
                owner_key=owner_key or "workspace",
            )
        )
        # Membership is the caller's mode-resolved reality, not the grant
        # (Devin Review on #1199): under auto-membership every granted row IS
        # in the Stack (``in_stack`` arrives True, rendering exactly as
        # before); under the classic default a granted-but-unsubscribed
        # ``available`` resource is NOT a member — claiming "In stack" there
        # would label rows the agent cannot actually query (membership also
        # drives ``get_accessible_tables``). Callers whose membership
        # genuinely is the grant (recipes, plugins) omit the argument.
        if in_stack and droppable:
            # Classic self-subscription: the caller added it, the caller can
            # remove it — HERE, not just on /catalog. This row used to render
            # the locked pill ("only an admin can remove it"), which was
            # false for a self-subscription and read as a required mandate;
            # /catalog offered Remove for the very same membership. The lock
            # is driven by droppability, and this membership IS droppable.
            import json as _json

            items[-1]["stack_state"] = "in_stack"
            items[-1]["stack_pill"] = "In stack"
            items[-1]["stack_removable"] = True
            # Remove is a path-param DELETE; re-add (after a remove, without
            # a reload) POSTs the generic subscribe endpoint with a body —
            # the row carries both so the click handler can cycle.
            items[-1]["stack_endpoint"] = "/api/stack/subscribe"
            items[-1]["stack_body"] = _json.dumps({"resource_type": type_key, "resource_id": item_id})
            items[-1]["stack_remove_endpoint"] = f"/api/stack/subscription/{type_key}/{item_id}"
            items[-1]["stack_title"] = "Added by you — click to remove it from your stack"
        elif in_stack:
            items[-1]["stack_state"] = "in_stack"
            # Every non-droppable member row says the same thing about
            # membership — "In stack" — and is LOCKED: there is no per-user
            # membership to drop, only a grant an admin can revoke (required
            # tier, or auto-membership where the grant IS the membership).
            # The lock is driven by *droppability*, not by the grant tier:
            # keying it on ``requirement == 'required'`` (as this once did)
            # left an optional grant rendering the success-tinted check that
            # a REMOVABLE row wears at rest. The tier stays legible in the
            # tooltip and the Optional/Required facet.
            items[-1]["stack_pill"] = "In stack"
            items[-1]["stack_locked"] = True
            if requirement == "required":
                items[-1]["stack_title"] = _LOCKED_STACK_TOOLTIP
            else:
                items[-1]["stack_title"] = _GRANTED_STACK_TOOLTIP
        else:
            # Classic non-member: a real Add control, not a dead pill (Devin
            # Review on #1199, round 4). The generic subscribe endpoint takes
            # a JSON body, so the row carries it (`data-stack-body`) for the
            # shared click handler. The post-add state is the REMOVABLE
            # member above (the handler switches on data-stack-remove-endpoint),
            # matching what a reload would render — the old locked-after
            # contract claimed an admin mandate the caller had just created
            # themselves.
            import json as _json

            items[-1]["stack_state"] = "available"
            items[-1]["stack_addable"] = True
            items[-1]["stack_endpoint"] = "/api/stack/subscribe"
            items[-1]["stack_body"] = _json.dumps({"resource_type": type_key, "resource_id": item_id})
            items[-1]["stack_remove_endpoint"] = f"/api/stack/subscription/{type_key}/{item_id}"
            items[-1]["stack_title"] = "Granted to you, but not in your stack — add it to make it queryable"

    # Governed data packages + memory domains — StackResolver.browse() is
    # exactly "required ∪ available for my groups" for these two types.
    # Both drill-downs are slug-keyed (/catalog/p/{slug}, /memory/d/{slug}),
    # so map id -> slug: a Library row is ONE package / ONE memory domain and
    # must open that item, not the generic listing page.
    try:
        pkg_slugs = {r["id"]: r.get("slug") for r in data_packages_repo().list(limit=100000)}
    except Exception:
        pkg_slugs = {}
    try:
        dom_slugs = {r["id"]: r.get("slug") for r in memory_domains_repo().list(limit=100000)}
    except Exception:
        dom_slugs = {}
    # Per-domain item/required counts — the same numbers the standalone
    # /corporate-memory cards carry (that page 302s here under rail, so the
    # band inherits its duties). One grouped COUNT query, not a per-domain
    # item load: pulling every knowledge item's full body to display two
    # numbers made the Library's render cost scale with the knowledge base
    # (Devin review on this PR). ``None`` means the counts are UNKNOWN (the
    # read failed) — the rows below then render without counts and, crucially,
    # without the empty-domain hiding rule, because "we could not count" must
    # not read as "there is nothing here" and silently drop granted knowledge.
    dom_counts: dict[str, tuple[int, int]] | None
    try:
        dom_counts = memory_domains_repo().count_items_by_domain()
    except Exception as e:
        logger.warning("/library: could not count memory-domain items: %s", e)
        dom_counts = None
    # Membership mode decides droppability below: classic optional members
    # are the caller's own subscriptions (removable here, as on /catalog);
    # under auto-membership the grant IS the membership, nothing to drop.
    from app.instance_config import get_stack_auto_membership

    _auto_membership = get_stack_auto_membership()
    for rt, type_key, type_label, glyph in (
        (ResourceType.DATA_PACKAGE, "data_package", "Data package", "data"),
        (ResourceType.MEMORY_DOMAIN, "memory_domain", "Memory", "memory"),
    ):
        try:
            for e in resolver.browse(uid, rt):
                if type_key == "data_package":
                    slug = pkg_slugs.get(e.id)
                    href = f"/catalog/p/{slug}" if slug else "/catalog"
                    row_meta = e.category or ""
                else:
                    slug = dom_slugs.get(e.id)
                    # ?source=library so the drill-down's back link returns
                    # HERE instead of to the memory listing the caller never
                    # visited (also feeds the memory_domain.view event).
                    href = f"/memory/d/{slug}?source=library" if slug else f"/corporate-memory#{e.id}"
                    if dom_counts is None:
                        # Counts unknown — render the row WITHOUT them rather
                        # than treating the failure as emptiness and hiding
                        # knowledge the caller is granted.
                        row_meta = e.category or ""
                    else:
                        n_items, n_required = dom_counts.get(e.id, (0, 0))
                        # A domain with KNOWN-zero content has nothing to opt
                        # into — same rule as the standalone page
                        # (_has_content): hidden unless the mandate itself is
                        # required. Admins manage empty placeholders at
                        # /admin/corporate-memory#domains.
                        if n_items == 0 and e.requirement != "required":
                            continue
                        row_meta = f"{n_items} item{'s' if n_items != 1 else ''}"
                        if n_required:
                            row_meta += f" · {n_required} required"
                _add_shared_row(
                    item_id=e.id,
                    title=e.name,
                    description=e.description,
                    href=href,
                    glyph=glyph,
                    type_key=type_key,
                    type_label=type_label,
                    origin="granted",
                    origin_label="Shared with you",
                    added=None,
                    meta_text=row_meta,
                    owner_label=e.owner_name or "Your workspace",
                    # StackResolver's non-required tier is "available";
                    # collapse it into "optional" so the facet has exactly two
                    # values rather than a duplicate pair.
                    requirement=("required" if e.requirement == "required" else "optional"),
                    tags=list(e.tags or []),
                    # Mode-resolved membership: auto → always True (rendering
                    # unchanged); classic → required ∪ subscribed only.
                    in_stack=e.in_stack,
                    droppable=(not _auto_membership and e.in_stack and e.requirement != "required"),
                )
        except Exception as e:
            logger.warning("/library: could not resolve %s: %s", rt.value, e)

    # Recipes — granted, resolved straight off the repo (no _fetch_entries
    # support for this type in StackResolver).
    try:
        recipe_ids = _granted_ids(ResourceType.RECIPE.value)
        if recipe_ids:
            for r in recipes_repo().list(limit=100000):
                if r["id"] not in recipe_ids:
                    continue
                _add_shared_row(
                    item_id=r["id"],
                    title=r.get("title") or r.get("slug"),
                    description=r.get("description"),
                    href=f"/catalog/r/{r.get('slug') or r['id']}",
                    glyph="doc",
                    type_key="recipe",
                    type_label="Recipe",
                    origin="granted",
                    origin_label="Shared with you",
                    added=r.get("created_at"),
                    meta_text="",
                    owner_label="Your workspace",
                )
    except Exception as e:
        logger.warning("/library: could not resolve recipes: %s", e)

    # Curated marketplace plugins — grant resource_id is the canonical
    # "<marketplace_slug>/<plugin_name>" path, so match on that.
    try:
        plugin_paths = _granted_ids(ResourceType.MARKETPLACE_PLUGIN.value)
        if plugin_paths:
            # The grant's resource_id is "<marketplace_id>/<plugin_name>" — the
            # SAME key `require_resource_access(MARKETPLACE_PLUGIN,
            # "{marketplace_id}/{plugin_name}")` gates the API with, and
            # `marketplace_plugins.marketplace_id` already IS that id. This
            # used to indirect through a `{id: row["slug"]}` map, but
            # `marketplace_registry` has no `slug` column (its PRIMARY KEY id
            # *is* the slug), so every lookup produced None → "None/<plugin>",
            # matched no grant, and silently dropped EVERY curated plugin from
            # the Library — invisibly, because a non-matching path is not an
            # exception the enclosing handler could report.
            #
            # Plugins are the one granted kind whose membership is NOT
            # automatic, so they override the stack fields `_add_shared_row`
            # sets: for a plugin the grant is only ELIGIBILITY. Model B (v28+)
            # has `resolve_user_marketplace` serve `subscriptions ∪
            # required-tier grants`, so a plugin granted at the `available`
            # tier and never subscribed is genuinely absent from the caller's
            # served set — its skills and commands are NOT loaded in their
            # Claude Code. Treating the grant as membership (as this did) made
            # the Library claim a locked "In stack" for every eligible plugin:
            # it contradicted both the /marketplace card and the agent's own
            # `marketplace_search`, and — because the row rendered locked — it
            # removed the only affordance that could have fixed the state.
            # Deriving it from `_curated_stack_sets`, the same helper
            # `GET /api/marketplace/items` computes its `installed` flag from,
            # is what keeps the two surfaces from drifting again.
            from app.api.marketplace import _curated_stack_sets
            from app.api.store import ORGANIZATION_PUBLISHER_LABEL

            plugin_in_stack, plugin_required = _curated_stack_sets(None, uid)
            for pl in marketplace_plugins_repo().list_all():
                # Admin-disabled is instance-wide "does not exist" for every
                # user-facing surface, grants notwithstanding — same
                # post-filter as the v2 /skills admin branch
                # (app/api/v2_marketplace.py). Without it the Library kept
                # rendering the card with a working "+ Add to stack" button.
                if pl.get("admin_disabled"):
                    continue
                mid, pname = pl.get("marketplace_id"), pl.get("name")
                path = f"{mid}/{pname}"
                if path not in plugin_paths:
                    continue
                key = (mid, pname)
                _add_shared_row(
                    item_id=path,
                    title=pl.get("display_name") or pl.get("name"),
                    description=pl.get("description"),
                    href=f"/marketplace/curated/{mid}/{pname}",
                    glyph="plugins",
                    type_key="plugin",
                    type_label="Plugin",
                    origin="granted",
                    origin_label="Shared with you",
                    added=None,
                    meta_text=pl.get("category") or "",
                    # A curated plugin is served off an admin-registered
                    # marketplace: the organization stands behind it, exactly as
                    # `/api/marketplace/items` reports it (`publisher_kind=
                    # "organization"`, `publisher_name=ORGANIZATION_PUBLISHER_LABEL`).
                    # The Library used to call the same item "Your workspace" and
                    # emit no trust marker, so the one class of item that IS
                    # organization-published was the one class showing no
                    # Organization marker — the two surfaces contradicted each
                    # other on the same row.
                    owner_label=ORGANIZATION_PUBLISHER_LABEL,
                    # The tier is real for plugins too, so the Optional/Required
                    # facet slices them the way it slices data packages.
                    requirement=("required" if key in plugin_required else "optional"),
                )
                row = items[-1]
                # Same three fields the store-entity rows carry, so the trust
                # marker macro reads one vocabulary across every Library row.
                # A curated plugin has no per-item verification state — the
                # organization publishing it outranks verification anyway (see
                # `level_for()` in macros/_trustmark.html).
                row["publisher_kind"] = "organization"
                row["verified"] = False
                row["trust_level"] = "org"
                # Same endpoint both ways: POST subscribes, DELETE unsubscribes
                # (`curated_install` / `curated_uninstall`). The Library's toggle
                # is kind-agnostic — it POSTs/DELETEs whatever the row names.
                row["stack_endpoint"] = f"/api/marketplace/curated/{mid}/{pname}/install"
                # Droppable unless an admin pinned it globally (`is_system`) or
                # required-tier-granted it to one of the caller's groups. Those
                # are precisely the two cases `curated_uninstall` answers 409
                # to, so the lock promises exactly what the API enforces.
                locked = bool(pl.get("is_system")) or key in plugin_required
                if key in plugin_in_stack:
                    row["stack_state"] = "in_stack"
                    row["stack_pill"] = "In stack"
                    row["stack_locked"] = locked
                    row["stack_removable"] = not locked
                    row["stack_title"] = (
                        _LOCKED_STACK_TOOLTIP if locked else "The default agent can use this — click to remove it"
                    )
                else:
                    row["stack_state"] = "available"
                    row["stack_pill"] = ""
                    row["stack_locked"] = False
                    row["stack_addable"] = True
                    row["stack_title"] = "Granted to you, but the default agent can't use it until you add it"
    except Exception as e:
        logger.warning("/library: could not resolve marketplace plugins: %s", e)

    # Installed AGENTS. Skills and plugins are already covered by the store sweep
    # above — whether installed or not — so listing them here again would double
    # every row. Agents are not swept (they have their own surface at /agents),
    # so an installed one is surfaced here, as it always has been.
    try:
        for inst in installed_store.values():
            if (inst.get("type") or "").lower() != "agent":
                continue
            _add_shared_row(
                item_id=inst["id"],
                title=inst.get("name"),
                description=inst.get("description"),
                href=f"/marketplace/flea/{inst['id']}?from=library",
                glyph="doc",
                type_key="agent",
                type_label="Agent",
                origin="installed",
                origin_label="From the marketplace",
                added=inst.get("installed_at") or inst.get("created_at"),
                meta_text=inst.get("category") or "",
                owner_label=inst.get("owner_display_name") or inst.get("owner_username") or "Someone",
                tags=[inst["category"]] if inst.get("category") else [],
                owner_key=inst.get("owner_user_id") or "",
            )
            # Installing a store item IS its Stack membership, and the caller may
            # undo it — the same install endpoint, removed.
            items[-1]["stack_state"] = "in_stack"
            items[-1]["stack_pill"] = "In stack"
            items[-1]["stack_removable"] = True
            items[-1]["stack_endpoint"] = f"/api/store/entities/{inst['id']}/install"
            items[-1]["stack_title"] = "The default agent can use this — click to remove it"
    except Exception as e:
        logger.warning("/library: could not resolve installed agents: %s", e)

    # ── Hosted data apps ───────────────────────────────────────────────
    # Same visibility set as the /apps page (data_apps_list_page): the
    # caller's own apps plus apps granted to their groups, minus drafts and
    # `linked_hidden` rows. No stack membership — data-app access is
    # grant-driven, not stack-driven — and no lifecycle actions here; the
    # detail page owns start/stop/logs. The rows group into the FILES band
    # (_SECTION_OF below) — apps sit among the caller's artifacts — and under
    # the rail chrome they are the ONLY way to the apps inventory (/apps 302s
    # to that section — see data_apps_list_page), so removing them strands
    # the surface.
    if _data_apps_nav_enabled():
        from app.api.data_apps import _serialize as _da_serialize
        from src.repositories import data_apps_repo

        try:
            _da_cfg = get_data_apps_config()
            _da_users = users_repo()
            # Grant-scoped (OWNERSHIP ∪ explicit grant), deliberately NOT the
            # API's ``_can_view`` (and not ``can_access`` either — both
            # short-circuit on Admin): this page's contract (docstring above)
            # is no admin god-mode, an admin's Library lists what THEY have,
            # not every user's private app. The instance-wide inventory stays
            # on the API/CLI list and the admin surfaces (Devin review on
            # this PR). Grants on this type are keyed by SLUG (see
            # ``_can_view``), fetched ONCE like the recipe/plugin siblings —
            # same shape PR #1272 lands on main, so the two branches keep one
            # definition of "what an admin sees in the Library".
            _app_grants = _granted_ids(ResourceType.DATA_APP.value)
            # ALL grants on the type (any group), slug-keyed — one query, the
            # `file_shared_groups` idiom above. An owned row's Sharing badge
            # must reflect the grants that actually exist on it; hardcoding
            # "Private" showed an already-shared app as unshared right next
            # to the control that shares it (Devin Review on PR #1272's
            # follow-ups).
            _app_shared_groups: dict = {}
            try:
                for g in resource_grants_repo().list_all(resource_type=ResourceType.DATA_APP.value):
                    _app_shared_groups.setdefault(g["resource_id"], set()).add(g["group_id"])
            except Exception as e:
                logger.warning("/library: could not resolve data-app grants: %s", e)

            def _app_visibility(slug: str) -> tuple:
                groups = _app_shared_groups.get(slug)
                if not groups:
                    return "private", "Private"
                if _everyone_id and _everyone_id in groups:
                    return "workspace", "Everyone"
                return "shared", "Specific groups"

            # ``limit=100000`` like every sibling listing in this function:
            # the repo defaults to 1000 newest-first, and that cap applies
            # BEFORE the ownership/grant filter — older apps would drop off
            # their owner's Library silently.
            for da in data_apps_repo().list(include_drafts=False, limit=100000):
                _da_mine = da["owner_user_id"] == uid
                if da.get("state") == "linked_hidden" or not (_da_mine or da["slug"] in _app_grants):
                    continue
                _da = _da_serialize(da, _da_cfg)
                _da_owner = _da_users.get_by_id(da["owner_user_id"]) or {}
                _da_meta = " · ".join(
                    b for b in (_da.get("state") or "", "linked" if _da.get("kind") == "linked" else "") if b
                )
                if _da_mine:
                    _da_vis, _da_vis_label = _app_visibility(da["slug"])
                else:
                    _da_vis, _da_vis_label = "shared", "Shared with you"
                _da_created = da.get("created_at")
                items.append(
                    _library_row_base(
                        # `kind` only surfaces as the row's inert `data-kind`
                        # attribute today (no CSS/JS reads it — the band
                        # accent rides the SECTION's kind), but say what the
                        # row is: the sibling app block on main said
                        # "data_app", and a future consumer keying on
                        # data-kind must not find apps filed as "library"
                        # (Devin review on PR #1278).
                        item_id=da["slug"],
                        kind="data_app",
                        title=da.get("name") or da["slug"],
                        description=_da.get("effective_description") or "",
                        href=f"/apps/detail/{da['slug']}",
                        glyph="app",
                        type_key="data_app",
                        type_label="Data app",
                        origin="built" if _da_mine else "granted",
                        origin_label="Built here" if _da_mine else "Shared with you",
                        added_iso=_da_created.isoformat() if hasattr(_da_created, "isoformat") else None,
                        # The owner's EMAIL on a grantee's row — deliberate,
                        # though sibling granted kinds say "Your workspace".
                        # Every other surface a granted viewer reaches already
                        # names the owner: /apps renders owner_email in its
                        # Owner column and /apps/detail/<slug> (this row's
                        # href, both chromes) shows the same field, all behind
                        # the same `_can_view` visibility this row mirrors. An
                        # app grant is an act by a person — hiding the email
                        # only here would have the Library disagree with the
                        # page it links to (Devin review on PR #1278).
                        owner_label="You" if _da_mine else (_da_owner.get("email") or da["owner_user_id"]),
                        ownership=(
                            "shared_by_me"
                            if (_da_mine and _da_vis != "private")
                            else ("mine" if _da_mine else "shared_with_me")
                        ),
                        visibility=_da_vis,
                        visibility_label=_da_vis_label,
                        meta_text=_da_meta,
                        # Data apps ARE grant-shareable (`ResourceType.DATA_APP`
                        # is a real `resource_grants` type and the proxy's
                        # `_can_view` honours it), so an owner's row carries the
                        # same Share control every other owner-held kind does,
                        # wired to the slug-keyed grant (Devin Review on PR
                        # #1272's follow-ups). Grantee rows render the read-only
                        # badge — the template keys that on `ownership`, and the
                        # sharing API enforces owner-or-admin regardless.
                        share_type=ResourceType.DATA_APP.value,
                        requirement="optional",
                        tags=[],
                        owner_key="me" if _da_mine else (da["owner_user_id"] or "workspace"),
                    )
                )
        except Exception as e:
            logger.warning("/library: could not list data apps: %s", e)

    # ── Definitions — the semantic layer, as a page FOOTER ────────────────
    # Deliberately NOT rows in the list above. Metrics and glossary terms are
    # the one thing here nobody owns, shares, installs, drops or edits: they
    # are the organization's agreed vocabulary, maintained by an admin and
    # readable by everyone unconditionally. Modelled as inventory they had to
    # neuter all four of the table's columns at once — Owner said "Your
    # workspace" (true of nothing in particular), Sharing said "Workspace" but
    # refused to change, Stack said "In stack" but locked, Actions was empty —
    # and four special-cased columns is the table saying the object is not one
    # of its rows. A data package looks similar but is genuinely different:
    # access to it VARIES per caller, which is what makes it "what I have".
    # Everyone has the whole glossary, so there is no having involved.
    #
    # So it closes the page instead: an adjacent destination under the
    # inventory, carrying its two counts and a door into each tab. The counts
    # are computed here (RBAC-filtered on the metric side exactly as
    # /catalog/semantics filters it, so the page never advertises definitions
    # the caller cannot open); the glossary is deliberately ungated there
    # (business vocabulary, not data), so its count is instance-wide.
    definitions_footer: dict = {}
    try:
        from app.api.metrics import _first_inaccessible_table
        from src.rbac import get_accessible_tables

        # No `conn` argument: /library takes no raw ``Depends(_get_db)``
        # connection, and passing one would be the backend-split bug class on a
        # Postgres instance. The default path reads through the repo factory.
        _accessible = get_accessible_tables(user)
        _allowed = None if _accessible is None else set(_accessible)
        _visible_metrics = [m for m in metric_repo().list() if _first_inaccessible_table(m, _allowed) is None]
        # 500 is GET /api/glossary's own max limit and the repo has no
        # unbounded mode (it bounds a full-table scan) — above the scale this
        # feature targets, so an exact count in practice.
        _glossary_terms = glossary_repo().list(limit=500)

        # What the page's search box matches the footer on. The reader types
        # the TERM they want — "ARR", "active account" — not the word
        # "definitions", so the block carries its contents' vocabulary: every
        # metric name, display name and synonym (synonyms are what make "MRR"
        # reach "Monthly Recurring Revenue"), and every glossary term.
        #
        # Names only, never the definition bodies. This ships in an attribute
        # on every page load, and matching on prose would surface the block for
        # words that merely appear inside some definition. The metric side
        # inherits the RBAC filter above for free.
        def _index_words(values) -> str:
            seen: dict[str, None] = {}
            for v in values:
                for word in str(v or "").split():
                    w = word.strip().lower()
                    if w:
                        seen.setdefault(w, None)
            return " ".join(seen)

        # Whether to offer the "Browse the semantic layer" link below — a
        # readable-model check scoped to what THIS caller can reach, the same
        # `_can_read_model` gate the /semantic-layer browse pages apply. It
        # answers "does this caller have a semantic model to browse at all", so
        # a caller who can read nothing gets neither the link nor a
        # "0 metrics · 0 terms" footer pointing at an empty page. A model with
        # no metrics/glossary projected yet (or a purely native, browse-only
        # model) still counts — gating on the flat projection's counts would
        # hide the one thing this UI exists to browse. Read in its own guard so
        # a semantic_models failure leaves the metric/glossary footer already
        # computed above intact instead of suppressing it.
        _has_readable_model = _has_readable_semantic_model(user, conn, surface="/library")

        if _visible_metrics or _glossary_terms or _has_readable_model:
            definitions_footer = {
                "metric_count": len(_visible_metrics),
                "glossary_count": len(_glossary_terms),
                # Only THIS gates the "Browse the semantic layer" link: the
                # metric/glossary links ride the flat projection above, but the
                # browse page needs a readable document, so a caller with
                # visible metrics yet no readable model must not be sent there.
                "has_semantic_models": _has_readable_model,
                "search": _index_words(
                    [m.get("display_name") for m in _visible_metrics]
                    + [m.get("name") for m in _visible_metrics]
                    + [s for m in _visible_metrics for s in (m.get("synonyms") or [])]
                    + [g.get("term") for g in _glossary_terms]
                ),
            }
    except Exception as e:
        logger.warning("/library: could not resolve the semantic layer: %s", e)

    # Default order = recently added first (undated rows sort last).
    items.sort(key=lambda c: c.get("added_iso") or "", reverse=True)

    # ── Facets ────────────────────────────────────────────────────────────
    # Only values actually present are offered, so the Filter menu never shows
    # an option that matches nothing. Type is NOT a facet any more — items are
    # GROUPED by type into collapsible sections instead, which is both the
    # filter and the navigation.
    def _present(attr_key: str, label_key: str) -> list:
        counts: dict = {}
        labels: dict = {}
        for c in items:
            k = c.get(attr_key)
            if not k:
                continue
            counts[k] = counts.get(k, 0) + 1
            labels[k] = c.get(label_key) or k
        return sorted(((k, labels[k], n) for k, n in counts.items()), key=lambda x: x[1])

    library_origins = _present("origin", "origin_label")
    library_requirements = _present("requirement", "requirement_label")
    library_owners = _present("owner_key", "owner_label")

    # Stack is a single "In stack only" TOGGLE, not a category: its two states
    # are complementary, so an Available/In-Stack submenu was a longer way to
    # say "everything" — picking both equals picking neither, and picking
    # "Available" is the unfiltered page minus what the toggle keeps. The count
    # is what the toggle leaves standing, locked admin-required memberships
    # included: a locked membership IS a membership (its tier is filterable on
    # its own Optional/Required category).
    library_in_stack_count = sum(1 for c in items if c.get("stack_state") == "in_stack")
    # What the Filter menu's "Not in stack yet" toggle can act on — strictly
    # the rows whose Add control would do something (stack_state 'available');
    # rows with no stack membership at all (files, apps) are neither in nor
    # addable. Zero means the toggle doesn't render: no dead filters.
    library_available_count = sum(1 for c in items if c.get("stack_state") == "available")

    # Tags are multi-valued per row, so they need their own tally.
    tag_counts: dict = {}
    for c in items:
        for tag in c.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    library_tags = sorted(((k, k, n) for k, n in tag_counts.items()), key=lambda x: x[1].lower())

    # A single-valued facet is dead weight (every row matches), so drop it.
    if len(library_requirements) < 2:
        library_requirements = []
    if len(library_owners) < 2:
        library_owners = []
    # Same rule for the Stack toggle: it renders only when flipping it would
    # actually change the page — something is in the Stack, and something isn't.
    library_stack_toggle = 0 < library_in_stack_count < len(items)

    # ── Grouping: one collapsible section per type ─────────────────────────
    # Section order is deliberate, and reads outward from what an agent is built
    # on: the governed DATA first, then the capabilities that act on it
    # (plugins, then the skills they bundle, then the agents and recipes made
    # from both), then the caller's own files, and curated Memory last. The
    # sections render folded, so this order is what the caller actually reads on
    # arrival — a list of what the Library holds — which is why the shared,
    # organization-wide inventory leads and personal uploads sit below it.
    # Unlisted types fall to the end, alphabetically.
    _SECTION_ORDER = [
        "data_package",
        "data_app",
        "plugin",
        "skill",
        "agent",
        "recipe",
        # Loose files + collections-as-folders (and hosted data apps).
        "files",
        "memory_domain",
    ]
    #: Kinds that land INSIDE another kind's section instead of getting their
    #: own. Data apps live among the caller's artifacts in Files — the
    #: original "Data apps coming soon" badge on that band promised exactly
    #: this — while their rows keep ``type_key="data_app"`` so the Type facet
    #: and the row's own label stay honest.
    _SECTION_OF = {"data_app": "files"}
    grouped: dict = {}
    for c in items:
        grouped.setdefault(_SECTION_OF.get(c["type_key"], c["type_key"]), []).append(c)

    def _section_rank(type_key: str) -> tuple:
        try:
            return (0, _SECTION_ORDER.index(type_key), "")
        except ValueError:
            return (1, 0, type_key)

    # Section headings name the CATEGORY, so they're plural regardless of how
    # many rows are in them ("Skills", not "Skill"). Spelled out rather than
    # suffixing "s" because several are irregular (PDFs, Memory, Data files).
    _SECTION_LABELS = {
        # Documents, images and every other format share one section, with
        # collections nested inside it as folders — and hosted data apps as a
        # trailing block (_SECTION_OF): everything the caller or their agent
        # made, hence the umbrella name.
        "files": "Artefacts",
        "skill": "Skills",
        "plugin": "Plugins",
        "agent": "Agent templates",
        "recipe": "Recipes",
        "data_package": "Data packages",
        "data_app": "Apps",
        "memory_domain": "Memory",
    }
    #: One line per group, in the header beside the label — the same slot My
    #: Stack uses to say why a group exists ("Optional resources you added.").
    #: Now that the groups are bands inside ONE list rather than separate
    #: tables, the label alone no longer has a heading's worth of space around
    #: it to explain itself. Kept to a short clause; a group with no hint simply
    #: renders none.
    _SECTION_HINTS = {
        "files": "Files you upload, outputs your agent generates, and your hosted data apps.",
        "skill": "Skills built here.",
        "plugin": "Bundles of skills and commands.",
        "agent": "Assistants you installed.",
        "recipe": "Prepared analyses you can run.",
        "data_package": "Governed data you can query.",
        "data_app": "Hosted apps running next to your data.",
        "memory_domain": "Curated organizational knowledge.",
    }
    #: Marker for a kind that will land INSIDE an existing section rather than
    #: getting its own. Rendered by `group_toggle`, so the table and the grid
    #: pick it up from one place.
    #: "<kind> coming soon" badges, keyed by the section the kind will ship
    #: INTO. Empty because the one entry it carried — "Data apps coming soon",
    #: on the Files band — has shipped: apps now list inside that band,
    #: and leaving the badge would have the same page list your apps and tell
    #: you they do not exist yet (Devin Review on this PR). The mechanism stays
    #: for the next kind; the badge is meant to delete itself when the kind
    #: lands, which is what this is.
    _SECTION_SOON: dict[str, str] = {}
    _SECTION_SOON_TIP: dict[str, str] = {}
    #: Each section wears the SAME accent its members' detail pages wear, so a
    #: type is recognizable by colour before the label is read. Values are the
    #: `--ds-kind-*` vocabulary the detail hero resolves through
    #: (macros/_detail.html sets `--kind: var(--ds-kind-{kind})`), paired with
    #: that kind's canonical glyph for the section heading.
    _SECTION_KINDS = {
        "files": ("library", "library"),
        "skill": ("skill", "skill"),
        "plugin": ("plugin", "plugins"),
        "agent": ("agent", "agent"),
        "recipe": ("recipe", "recipes"),
        "data_package": ("data", "data"),
        "data_app": ("app", "app"),
        "memory_domain": ("memory", "memory"),
    }

    def _section_rows(key: str, rows: list) -> list:
        """Row order inside one section.

        Files is the only mixed section: collections (folders) and loose files
        share it, so the folders come FIRST as their own block — the reader sees
        the containers before the loose contents, and the drop targets are all
        in one place. Everything else keeps the global recency order.
        """
        if key != "files":
            return rows
        # Three stable blocks: folders (containers first, drop targets in one
        # place), then loose files, then data apps — the sub-kinds of the
        # Artefacts umbrella stay grouped instead of interleaving by recency.
        folders = [r for r in rows if r.get("is_folder")]
        apps = [r for r in rows if r.get("type_key") == "data_app"]
        loose = [r for r in rows if not r.get("is_folder") and r.get("type_key") != "data_app"]
        return folders + loose + apps

    library_sections = []
    for key, rows in sorted(grouped.items(), key=lambda kv: _section_rank(kv[0])):
        kind, glyph = _SECTION_KINDS.get(key, ("library", "doc"))
        library_sections.append(
            {
                "key": key,
                "label": _SECTION_LABELS.get(key) or (rows[0]["type_label"] + "s"),
                "hint": _SECTION_HINTS.get(key, ""),
                "soon": _SECTION_SOON.get(key, ""),
                "soon_tip": _SECTION_SOON_TIP.get(key, ""),
                "rows": _section_rows(key, rows),
                "kind": kind,
                "glyph": glyph,
                # Top-level entries only — a folder counts once, not once per
                # file inside it (its own count rides the folder row).
                "count": len(rows),
            }
        )

    from app.instance_config import feature_enabled

    ctx = _build_context(
        request,
        user=user,
        library_items=items,
        library_sections=library_sections,
        definitions_footer=definitions_footer,
        library_origins=library_origins,
        library_requirements=library_requirements,
        library_in_stack_count=library_in_stack_count,
        library_available_count=library_available_count,
        library_stack_toggle=library_stack_toggle,
        library_owners=library_owners,
        library_tags=library_tags,
        # Highlight target after "Save to Library" (see the builders).
        library_new_id=request.query_params.get("new") or "",
        # Band to open on arrival — a detail page's back link returns here as
        # /library?section=<type_key> (router._detail_back) and the bands are
        # folded by default, so without this the caller lands on a closed
        # list. Validated against the real section keys so the value reaching
        # the page's JS is always one of ours.
        library_open_section=(
            request.query_params.get("section") if request.query_params.get("section") in _SECTION_LABELS else ""
        ),
        # Arrive with "In stack only" already pressed — /library?stack=in_stack.
        # The chat empty state's Stack status line ("Using N knowledge sources
        # and M capabilities from your stack") points here instead of at
        # the de-railed /stack page (#1088); this list spans every kind that
        # page did, and the toggle narrows it to what the line counts. The value
        # is compared against the facet's one legal value, so what reaches the
        # page's JS is a boolean, never caller text.
        library_stack_only=request.query_params.get("stack") == "in_stack",
        # Scope preset — `/library?scope=mine|available`. This is where the
        # retired Catalog and Marketplace browse pages land: they were the
        # "what could I add" and "what have I got" halves of one list. The
        # page's JS maps `mine` onto the In stack segment and `available`
        # onto the Filter menu's "Not in stack yet" toggle (the acquisition
        # question was demoted off the bar when the segment went two-state).
        # Validated against the two legal values, so what reaches the page's
        # JS is one of ours and never caller text; anything else falls back
        # to `all`, which is what the Library opens on.
        library_scope=(
            request.query_params.get("scope") if request.query_params.get("scope") in ("mine", "available") else ""
        ),
        # Default OFF (upgrade parity): an unverified Store item is marked by
        # the absence of a marker unless the instance opts into the positive
        # trust vocabulary. Must stay in step with the FEATURE_FLAGS registry
        # default — `feature_enabled` takes the CALLSITE default, so the
        # registry entry is display metadata only (guarded by
        # tests/test_feature_flags.py).
        show_unverified_trust=feature_enabled(
            "library",
            "show_unverified_trust",
            env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
            default=_LIBRARY_TRUST_DEFAULT,
        ),
    )
    return templates.TemplateResponse(request, "library.html", ctx)


@router.get("/artefacts", include_in_schema=False)
async def artefacts_redirect():
    """``/artefacts`` was renamed to ``/library``.

    Kept as a temporary (307, not 308) redirect so existing links, bookmarks
    and the onboarding tour keep working without pinning the old path in
    browser caches.
    """
    return RedirectResponse(url="/library", status_code=307)


# Entry points: "My agents" in the user dropdown
# (`app/web/templates/_app_rail.html`) plus a Cmd/Ctrl-K palette entry — a
# per-user resource list, so deliberately not primary nav and not the admin
# mega-menu (instance-level agent authoring is Studio's /admin/studio/agent).
# Both links are guarded by `tests/test_web_nav_agents.py`; don't drop them.
# Kept as a comment rather than a docstring: FastAPI copies docstrings into the
# OpenAPI description, and internal nav notes don't belong in the public schema.
@router.get("/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Agents — build a focused assistant out of the caller's own stack.

    Work-in-progress surface (rail item carries a WIP badge). The builder's
    ingredient lists are REAL and RBAC-scoped: knowledge sources are the data
    packages + memory domains resolved from the caller's stack (same
    ``StackResolver`` reads as /stack), and capabilities hydrate client-side
    from ``/api/marketplace/items?tab=my`` (the caller's subscribed plugins).
    Agent definitions themselves persist in the browser for now — a server
    registry is the next iteration, so the page states that plainly rather
    than pretending drafts are shared."""
    if not get_agent_profiles_enabled():
        return RedirectResponse("/", status_code=302)

    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    resolver = StackResolver()
    knowledge_sources: list = []
    try:
        pkg_repo = data_packages_repo()
        for e in resolver.stack(user["id"], ResourceType.DATA_PACKAGE):
            tables = 0
            try:
                tables = len(pkg_repo.list_tables(e.id))
            except Exception:
                tables = 0
            knowledge_sources.append(
                {
                    "id": e.id,
                    "kind": "data",
                    "name": e.name,
                    "description": e.description or "",
                    "meta": f"{tables} table{'' if tables == 1 else 's'}",
                }
            )
    except Exception as e:
        logger.warning("/agents: could not resolve data stack: %s", e)
    try:
        domains_repo = memory_domains_repo()
        for e in resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN):
            items_count = 0
            try:
                items_count = len(domains_repo.list_items_of_domain(e.id, limit=10000))
            except Exception:
                items_count = 0
            knowledge_sources.append(
                {
                    "id": e.id,
                    "kind": "memory",
                    "name": e.name,
                    "description": e.description or "",
                    "meta": f"{items_count} item{'' if items_count == 1 else 's'}",
                }
            )
    except Exception as e:
        logger.warning("/agents: could not resolve memory stack: %s", e)
    # Artefacts (file collections) the caller can reach — owned ∪ shared with a
    # group they belong to (admin → all). These are a third knowledge kind the
    # agent can be grounded in, alongside governed data + memory. Same access
    # resolution the /artefacts page uses, so the builder never offers a file
    # the caller can't actually open.
    try:
        from app.auth.access import accessible_collection_ids

        allowed = accessible_collection_ids(user)  # None => admin sees all
        cf_repo = corpus_files_repo()
        # TODO: the per-collection file count below is an N+1 (one query per
        # reachable collection, so per *every* collection for an admin). It was
        # incidentally bounded while this read the 200-capped list(); switching
        # to list_all() to stop hiding reachable collections removes that
        # ceiling. A bulk `counts_by_corpus()` on both corpus_files backends
        # would collapse it into one query.
        for col in file_corpora_repo().list_all():
            if allowed is not None and col["id"] not in allowed:
                continue
            try:
                fcount = len(cf_repo.list_for_corpus(col["id"]))
            except Exception:
                fcount = 0
            knowledge_sources.append(
                {
                    "id": col["id"],
                    "kind": "file",
                    "name": col.get("name") or col.get("slug"),
                    "description": col.get("description") or "",
                    "meta": f"{fcount} file{'' if fcount == 1 else 's'}",
                }
            )
    except Exception as e:
        logger.warning("/agents: could not resolve artefacts: %s", e)

    ctx = _build_context(
        request,
        user=user,
        knowledge_sources=knowledge_sources,
    )
    return templates.TemplateResponse(request, "agents.html", ctx)


@router.get("/skills", response_class=HTMLResponse)
async def skills_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Builder — one authoring surface for skills, plugins and agent templates.

    Formerly the single-type Skill Builder. Two in-page steps: a TYPE PICKER,
    then a type-adapted BUILDER that keeps one shell (identity, access,
    numbered sections, live preview, advisory check) and swaps only the
    content section and its validation.

    Markdown types (skill, agent) publish to
    ``POST /api/store/entities/from-markdown``; plugins are real ZIP bundles
    and publish to the multipart ``POST /api/store/entities``. Both honour the
    builder's private/everyone ``access`` choice, and both run the same quota,
    guardrail and review pipeline. Category options come from the shared store
    taxonomy.

    The route keeps its ``/skills`` path: it is the target of the Marketplace
    "submit" CTA, the ``?from=skills`` detail back-link and the tour anchors.
    ``?type=skill|plugin|agent`` deep-links past the picker."""
    from src.store_categories import STORE_CATEGORIES

    from app.instance_config import get_guardrails_enabled, get_guardrails_llm_provider_ready

    _guardrails_enabled = get_guardrails_enabled()
    ctx = _build_context(
        request,
        user=user,
        store_categories=list(STORE_CATEGORIES),
        guardrails_enabled=_guardrails_enabled,
        guardrails_llm_ready=_guardrails_enabled and get_guardrails_llm_provider_ready(),
    )
    return templates.TemplateResponse(request, "skills.html", ctx)


@router.get("/catalog/semantics", response_class=HTMLResponse)
async def catalog_semantics(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Read-only browser for the semantic layer — business metrics
    (`metric_definitions`) and the glossary (`glossary_terms`) in one page
    (issue #853 + the Keboola glossary import, #920).

    Analyst-facing tier: ``get_current_user``, no admin gate and no
    per-resource grant — matches the RBAC tier of the underlying
    ``GET /api/metrics`` / ``GET /api/glossary*`` endpoints this page reuses,
    and mirrors /catalog's own gate.

    Metrics are RBAC-filtered the same way ``GET /api/metrics`` is (#953):
    a metric whose ``table_name``/``tables`` reference a table outside the
    caller's Data Package stack is omitted before grouping, so a category
    left with zero visible metrics never renders its header either. Glossary
    is intentionally NOT gated this way (business vocabulary, not data).

    Metrics are server-rendered (grouped by category, same reading order as
    ``agnes catalog --metrics``) — the scale is tens-to-low-hundreds so a
    client-side substring filter over the rendered rows is enough; no new
    search endpoint. Glossary starts empty and is populated client-side via
    the existing ``GET /api/glossary`` / ``GET /api/glossary/search``.
    """
    from app.api.metrics import _first_inaccessible_table, stores_html
    from app.markdown_render import render_plain, render_safe
    from src.rbac import get_accessible_tables

    accessible_ids = get_accessible_tables(user, conn)
    allowed = None if accessible_ids is None else set(accessible_ids)
    metrics = [m for m in metric_repo().list() if _first_inaccessible_table(m, allowed) is None]

    def _variants(raw) -> dict:
        """``sql_variants`` as a mapping the template can iterate.

        The repository serializes this column on write but does not
        deserialize on read, so it arrives as a JSON *string* — on which
        ``.items()`` silently yields nothing in Jinja, which is how the
        variants stayed invisible. Parsed here rather than in the repo:
        changing the read shape there would ripple through both backends and
        their contract tests. Anything that is not an object is dropped,
        since the template renders one labelled block per key.
        """
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except ValueError:
                return {}
        return raw if isinstance(raw, dict) else {}

    # Two projections of the description: sanitized HTML for the expanded
    # detail, plain text for the one-line row preview and the client-side
    # filter index. Metric descriptions carry the business definition; the
    # detail must show it, not just the SQL.
    #
    # This column holds two dialects, and ``stores_html`` decides per row which
    # renderer applies (keyed on the writer recorded in ``source``, never on
    # what the text looks like — see its docstring). Rendered as pure markdown,
    # an HTML-dialect description escaped into entities and then unescaped back
    # into visible `<p><strong>` characters in both projections.
    metrics = [
        {
            **m,
            "description_html": render_safe(m.get("description"), html_source=stores_html(m)),
            "description_text": render_plain(m.get("description"), html_source=stores_html(m)),
            "sql_variants": _variants(m.get("sql_variants")),
        }
        for m in metrics
    ]
    by_category: dict[str, list[dict]] = {}
    for m in metrics:
        by_category.setdefault(m.get("category") or "uncategorized", []).append(m)
    metric_categories = [
        {"name": cat, "metrics": sorted(items, key=lambda m: m.get("name") or "")}
        for cat, items in sorted(by_category.items())
    ]

    # Total glossary count for the tab label. GlossaryRepository.list() has
    # no unlimited mode (deliberately, to bound a full-table scan) — 500 is
    # the endpoint's own max `limit` (app/api/glossary.py), comfortably above
    # the "tens-to-low-hundreds" scale this feature targets, so it's an
    # exact count in practice rather than a true cap.
    glossary_count = len(glossary_repo().list(limit=500))

    ctx = _build_context(
        request,
        user=user,
        metric_categories=metric_categories,
        metric_count=len(metrics),
        glossary_count=glossary_count,
        # The door to /semantic-layer. Both pages are titled "Semantic layer"
        # and this is the reachable one, so without the link a document with
        # datasets and relationships but no metrics rendered as "there is no
        # semantic layer here". Same gate /library's Definitions footer uses —
        # a readable document, never this page's own metric/glossary counts.
        has_semantic_models=_has_readable_semantic_model(user, conn, surface="/catalog/semantics"),
    )
    return templates.TemplateResponse(request, "catalog_semantics.html", ctx)


# ---------------------------------------------------------------------------
# Semantic layer browse UI (wave 4.2 of the 2026-08-14 UI/agent-parity
# design) — read-only rendering of the stored Ossie document itself, three
# levels: model list, model detail (one tab per object type), object detail.
# Deliberately separate from /catalog/semantics (the flat metric_definitions
# projection) and /admin/semantic-layer (the sync-ops view) — neither is
# touched by this feature. RBAC tier matches the rest of the read surface in
# app/api/semantic_models.py: any authenticated user, filtered through
# `_can_read_model` (a Data Package grant or a direct `semantic_model` grant
# — never admin-only). Editing is a later increment; nothing on any of the
# three pages below offers a write affordance.
# ---------------------------------------------------------------------------

_SEMANTIC_LAYER_TABS = ("datasets", "metrics", "constraints", "relationships", "glossary")


def _semantic_layer_tab_label(tab: str) -> str:
    return {
        "datasets": "Datasets",
        "metrics": "Metrics",
        "constraints": "Constraints",
        "relationships": "Relationships",
        "glossary": "Glossary",
    }[tab]


def _readable_model_by_slug(slug: str, user: dict, conn) -> Optional[dict]:
    """Resolve a slug to the newest ``semantic_models`` row the CALLER CAN READ.

    Slugs are unique only per ``(source, source_ref)`` (``upsert`` prunes only
    within that scope), so two models can share one — a hand-authored
    ``manual`` and an imported ``ossie_git``, say. The repo's ``get_by_slug``
    returns the newest row OVERALL, which for this browse UI means a card the
    caller can read could resolve to a *different* row: a 404 when they lack a
    grant on the newest, or the wrong document when they can read both. Picking
    the newest row THIS caller can read keeps the click on a model they were
    actually shown (Devin #1398); RBAC stays applied to the row finally served.
    The residual — a caller who can read two same-slug rows still reaches only
    the newer from either card — needs a unique-per-row URL and is left with
    the pre-existing export endpoint that shares the slug-only resolution.
    """
    from app.api.semantic_models import _can_read_model

    candidates = [
        r for r in semantic_model_repo().list_all() if r.get("slug") == slug and _can_read_model(user, r, conn)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return candidates[0]


@router.get("/semantic-layer", response_class=HTMLResponse)
async def semantic_layer_list(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Level 1 — every semantic model the caller can read, with its object
    counts, SQL dialect(s) and validation status.

    A ``status='invalid'`` row still lists (an invalid import must be
    visible, not silent) and renders its stored ``validation_errors``
    instead of object counts, since an invalid document's ``document_json``
    may be stale or absent.
    """
    from app.api.semantic_models import _can_read_model
    from app.web.semantic_layer_view import is_imported, model_dialects, model_of, object_counts, source_label

    # One card per slug — the newest readable row, matching what the
    # drill-down (`_readable_model_by_slug`) resolves to. Two models can share
    # a slug (unique only per source/source_ref); listing both would render a
    # second card that silently opens the first, since every card links to the
    # same `/semantic-layer/{slug}` (Devin #1398). Deduping loses no
    # reachability — the older row is unreachable from this UI either way — and
    # removes the misleading card. Full disambiguation (both reachable) needs a
    # unique-per-row URL, the follow-up noted on `_readable_model_by_slug`.
    newest_by_slug: dict[str, dict] = {}
    for row in semantic_model_repo().list_all():
        if not _can_read_model(user, row, conn):
            continue
        current = newest_by_slug.get(row["slug"])
        if current is None or str(row.get("updated_at") or "") > str(current.get("updated_at") or ""):
            newest_by_slug[row["slug"]] = row

    models = []
    for row in newest_by_slug.values():
        model = model_of(row)
        models.append(
            {
                "slug": row["slug"],
                "name": row.get("name") or model.get("name") or row["slug"],
                "description": row.get("description") or model.get("description"),
                "dialects": model_dialects(model),
                "source": row.get("source"),
                "source_label": source_label(row.get("source")),
                # Through the shared helper, not a raw `!= 'manual'` in the
                # template: `is_imported` treats a falsy source as NATIVE
                # (`(source or "manual") != "manual"`), and the detail and object
                # pages already route the decision through it. A raw comparison
                # called an empty source imported and then labelled it
                # "Imported from Native", contradicting the detail page for the
                # same row.
                "is_imported": is_imported(row.get("source")),
                "status": row.get("status"),
                "validation_errors": row.get("validation_errors") or [],
                "counts": object_counts(model),
            }
        )

    ctx = _build_context(request, user=user, models=models)
    return templates.TemplateResponse(request, "semantic_layer_list.html", ctx)


@router.get("/semantic-layer/{slug}", response_class=HTMLResponse)
async def semantic_layer_detail(
    slug: str,
    request: Request,
    tab: str = Query("datasets"),
    q: str = Query(""),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Level 2 — one model, one tab per object type (datasets default).

    ``q`` is the cross-link prefilter carried from another tab (a metric
    row links to its constraints with ``?tab=constraints&q=<metric name>``,
    a dataset row links to its metrics with ``?tab=metrics&q=<dataset
    name>``) — a plain case-insensitive substring match scoped to whichever
    tab is active, not a new search endpoint.
    """
    from app.web.semantic_layer_view import (
        agnes_extension_payload,
        is_imported,
        model_constraints,
        model_glossary,
        model_of,
        object_counts,
        source_label,
        warehouse_only_metric_count,
    )

    row = _readable_model_by_slug(slug, user, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")

    active_tab = tab if tab in _SEMANTIC_LAYER_TABS else "datasets"
    model = model_of(row)

    datasets = [d for d in model.get("datasets") or [] if isinstance(d, dict)]
    metrics = [m for m in model.get("metrics") or [] if isinstance(m, dict)]
    constraints = model_constraints(model)
    relationships = [r for r in model.get("relationships") or [] if isinstance(r, dict)]
    glossary = model_glossary(model)

    needle = q.strip().lower()
    if needle:
        if active_tab == "datasets":
            datasets = [d for d in datasets if needle in str(d.get("name") or "").lower()]
        elif active_tab == "metrics":
            # A metric's Agnes payload binds it to a dataset by that dataset's
            # source id, not its friendly name (the Keboola metastore adapter
            # stores the raw tableId there — connectors/keboola/semantic_ossie.py).
            # The per-dataset cross-link and the search box both pass the
            # friendly `name`, so resolve name → source: a needle that names a
            # dataset also matches every metric bound to that dataset's source.
            # Read the binding through the view's `agnes_extension_payload`,
            # which casefolds the vendor tag like the query validator, the
            # projector and the rest of this browse module — so any casing of
            # the `agnes`/`AGNES` tag resolves (Devin #1398).
            def _metric_dataset(m: dict) -> str:
                return str(agnes_extension_payload(m).get("dataset") or "").lower()

            _needle_dataset_sources = {
                str(d.get("source") or "").lower()
                for d in datasets
                if needle in str(d.get("name") or "").lower() or needle in str(d.get("source") or "").lower()
            }
            _needle_dataset_sources.discard("")
            metrics = [
                m
                for m in metrics
                if needle in str(m.get("name") or "").lower()
                or needle in _metric_dataset(m)
                or _metric_dataset(m) in _needle_dataset_sources
            ]
        elif active_tab == "constraints":
            # Match the constraint's own name (the first, linked column) as well
            # as the metrics it applies to. `metrics` rides the opaque Agnes
            # custom_extensions payload the Ossie schema does not validate, so a
            # non-string element must be coerced, not `" ".join`ed into a 500.
            constraints = [
                c
                for c in constraints
                if needle in str(c.get("name") or "").lower()
                or needle in " ".join(str(m) for m in (c.get("metrics") or [])).lower()
            ]
        elif active_tab == "relationships":
            relationships = [
                r
                for r in relationships
                if needle in str(r.get("name") or "").lower()
                or needle in str(r.get("from") or "").lower()
                or needle in str(r.get("to") or "").lower()
            ]
        elif active_tab == "glossary":
            glossary = [g for g in glossary if needle in str(g.get("term") or "").lower()]

    # A relationship's from/to name a dataset, but the Ossie schema validates
    # only that they are strings — and `model_of` may aggregate models whose
    # relationships reference a dataset declared in a sibling. So resolve each
    # side against the document's dataset names (case-insensitively, matching
    # find_object) and let the template link only the resolvable ones; an
    # undeclared side renders as plain text instead of a link that 404s, the
    # same guard the object-detail page already applies (Devin #1398).
    _dataset_names_cf = {
        str(d.get("name") or "").casefold() for d in (model.get("datasets") or []) if isinstance(d, dict)
    }
    relationships = [
        {
            **r,
            "from_linkable": str(r.get("from") or "").casefold() in _dataset_names_cf,
            "to_linkable": str(r.get("to") or "").casefold() in _dataset_names_cf,
        }
        for r in relationships
    ]

    tabs = [
        {
            "key": t,
            "label": _semantic_layer_tab_label(t),
            "href": f"/semantic-layer/{slug}?tab={t}",
            "active": t == active_tab,
        }
        for t in _SEMANTIC_LAYER_TABS
    ]

    ctx = _build_context(
        request,
        user=user,
        slug=slug,
        model_name=row.get("name") or model.get("name") or slug,
        model_description=row.get("description") or model.get("description"),
        source=row.get("source"),
        source_label=source_label(row.get("source")),
        is_imported=is_imported(row.get("source")),
        status=row.get("status"),
        validation_errors=row.get("validation_errors") or [],
        active_tab=active_tab,
        tabs=tabs,
        q=q,
        datasets=datasets,
        metrics=metrics,
        constraints=constraints,
        relationships=relationships,
        glossary=glossary,
        counts=object_counts(model),
        warehouse_only_metric_count=warehouse_only_metric_count(model),
    )
    return templates.TemplateResponse(request, "semantic_layer_detail.html", ctx)


@router.get("/semantic-layer/{slug}/{object_id:path}", response_class=HTMLResponse)
async def semantic_layer_object(
    slug: str,
    object_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Level 3 — one object, rendering everything the flat projection drops:
    a dataset's ``fields[]`` as a Name/Type/Role/Description table, the
    ``ai_context`` block in five groups (keywords, synonyms, anti_keywords,
    hints, warnings), a metric's SQL fragment(s) with their dialect, a
    relationship with both sides linked.

    ``object_id`` is ``"<type>:<name>"`` — a ``:path`` parameter, not a single
    segment, so a ``name``/``term`` carrying a ``/`` (a glossary phrase like
    "ARR/MRR") is captured whole instead of 404ing; ``partition(":")`` splits
    on the FIRST colon, so a colon in the name survives too. ``type`` one of
    dataset/metric/relationship/constraint/ glossary, ``name`` the object's
    ``name`` (``term`` for glossary),
    case-insensitively matched.
    """
    from app.web.semantic_layer_view import (
        OBJECT_TYPE_LABELS,
        OBJECT_TYPE_TAB,
        ai_groups,
        ai_instructions_and_examples,
        dataset_field_rows,
        find_object,
        is_imported,
        metric_expressions,
        model_of,
        source_label,
    )

    row = _readable_model_by_slug(slug, user, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")

    object_type, _, object_name = object_id.partition(":")
    if object_type not in OBJECT_TYPE_LABELS or not object_name:
        raise HTTPException(status_code=404, detail=f"Unknown semantic object '{object_id}'")

    model = model_of(row)
    obj = find_object(model, object_type, object_name)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{object_type} '{object_name}' not found in '{slug}'")

    # Relationships render the AI-context panel too (see `ai=` below), so their
    # instructions/examples must be read alongside the five groups — otherwise a
    # relationship carrying `ai_context.instructions` shows a panel that says
    # "None declared." while its prose is silently dropped (Devin #1398).
    instructions, examples = (
        ai_instructions_and_examples(obj) if object_type in ("dataset", "metric", "relationship") else (None, [])
    )

    # Keyed case-insensitively to match find_object's resolution — otherwise a
    # relationship spelling a dataset with different casing renders as unlinked
    # text even though its target page resolves fine (Devin #1398).
    datasets_by_name = {
        str(d.get("name")).casefold(): d for d in model.get("datasets") or [] if isinstance(d, dict) and d.get("name")
    }

    ctx = _build_context(
        request,
        user=user,
        slug=slug,
        model_name=row.get("name") or model.get("name") or slug,
        is_imported=is_imported(row.get("source")),
        source_label=source_label(row.get("source")),
        object_type=object_type,
        object_type_label=OBJECT_TYPE_LABELS[object_type],
        back_tab=OBJECT_TYPE_TAB[object_type],
        obj=obj,
        object_name=obj.get("name") or obj.get("term") or object_name,
        fields=dataset_field_rows(obj) if object_type == "dataset" else None,
        ai=ai_groups(obj) if object_type in ("dataset", "metric", "relationship") else None,
        ai_instructions=instructions,
        ai_examples=examples,
        expressions=metric_expressions(obj) if object_type == "metric" else None,
        from_dataset=datasets_by_name.get(str(obj.get("from") or "").casefold())
        if object_type == "relationship"
        else None,
        to_dataset=datasets_by_name.get(str(obj.get("to") or "").casefold()) if object_type == "relationship" else None,
    )
    return templates.TemplateResponse(request, "semantic_layer_object.html", ctx)


@router.get("/catalog/p/{slug}", response_class=HTMLResponse)
async def catalog_package_detail(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-package drill-down — header + table list (Task 8.3 of v49 plan).

    RBAC: admin god-mode or grant on this package. The page mirrors the
    surface of ``GET /api/data-packages/{slug}`` (which carries the
    telemetry emit + audit-log path) — the JS side also issues GET on
    that endpoint so behavior is identical regardless of entry point.
    """
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver

    pkg_repo = data_packages_repo()
    pkg = pkg_repo.get_by_slug(slug)
    if not pkg:
        raise HTTPException(status_code=404, detail="data_package_not_found")

    # Admin bypass via is_user_admin; otherwise require a grant (any tier).
    # The detail token is DISTINCT (same pattern as admin_elevation_paused)
    # so error.html can answer with language and a request-access action
    # instead of echoing a machine string. It carries the package NAME —
    # the 403 already confirms existence (deliberate for this kind: packages
    # 403, collections 404 — see the leak-sensitivity split), so naming it
    # costs nothing and makes the copy-request text worth sending.
    if not (
        is_user_admin(user["id"], conn) or can_access(user["id"], ResourceType.DATA_PACKAGE.value, pkg["id"], conn)
    ):
        raise HTTPException(status_code=403, detail=f"package_not_shared:{pkg['name']}")

    # Telemetry: emit data_package.view (Section 9.2). source=browse|my-stack
    # passed as ?source=…; default 'direct' for typed/bookmarked navigation.
    source_hint = request.query_params.get("source", "direct")
    try:
        usage_repo().emit_server_event(
            event_type="data_package.view",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props={"slug": slug, "source": source_hint},
        )
    except Exception:
        logger.warning("usage_events emit failed for data_package.view")

    resolver = StackResolver(conn)
    effective_required = resolver.is_required(user["id"], ResourceType.DATA_PACKAGE, pkg["id"])
    # In-stack iff required OR a subscription row exists.
    in_stack = effective_required or user_stack_subscriptions_repo().is_subscribed(
        user["id"], ResourceType.DATA_PACKAGE.value, pkg["id"]
    )

    # Hydrate tables with query_mode + last_sync + v56 extended docs.
    # The extended fields (grain, platforms, partition_col, history,
    # gotchas) feed the collapsible per-table extended-detail section
    # on the package page; description carries the ≤200 char card-line.
    table_rows = pkg_repo.list_tables(pkg["id"])
    table_repo = table_registry_repo()
    sync_states = {s["table_id"]: s for s in sync_state_repo().get_all_states()}
    tables = []
    for tr in table_rows:
        full = table_repo.get(tr["id"]) or {}
        st = sync_states.get(tr["id"]) or {}
        size = st.get("file_size_bytes") or 0
        tables.append(
            {
                "id": tr["id"],
                "name": tr["name"],
                "description": full.get("description"),
                "query_mode": full.get("query_mode") or "local",
                "source_type": full.get("source_type"),
                "last_sync_display": (str(st.get("last_sync"))[:19] if st.get("last_sync") else None),
                "size_display": _human_size(size) if size else None,
                "size_bytes": size,
                # v56 extended per-table docs for the package-detail expand.
                "grain": full.get("grain"),
                "platforms": full.get("platforms") or [],
                "partition_col": full.get("partition_col"),
                "history": full.get("history"),
                "gotchas": full.get("gotchas") or [],
                "sample_questions": full.get("sample_questions") or [],
            }
        )

    # v56 virtual badges, v113: ONE badge left. The router still owns the policy
    # (a 30-day window) and the template stays presentational.
    #
    # `curated` used to be derived right here from "is the creator currently in
    # the Admin group" — a second copy of the same derivation in
    # data_packages._badges_for, which is precisely how the two could disagree.
    # Both are gone: the trust claim is now the STORED publisher_kind, read off
    # the row like any other column, and rendered by the shared trust marker
    # every other surface uses (the amber `pkg-badge--curated` chip lived only
    # on the frozen pre-redesign page, retired with it).
    from datetime import datetime, timedelta, timezone as _tz

    badges: list[str] = []

    created_at = pkg.get("created_at")
    if isinstance(created_at, datetime):
        ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=_tz.utc)
        if (datetime.now(_tz.utc) - ts) < timedelta(days=30):
            badges.append("new")

    total_size = sum(t["size_bytes"] for t in tables)
    ctx = _build_context(
        request,
        user=user,
        pkg=pkg,
        tables=tables,
        effective_requirement="required" if effective_required else "available",
        in_stack=in_stack,
        total_size_bytes=total_size,
        total_size_display=_human_size(total_size) if total_size else None,
        badges=badges,
    )
    return templates.TemplateResponse(request, "catalog_package_detail.html", ctx)


@router.get("/library/{slug}/f/{file_id}", response_class=HTMLResponse)
async def library_file_detail(
    slug: str,
    file_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """One file inside a collection — its own detail page.

    A single-file artefact IS its collection, so it keeps using
    ``/library/{slug}``; this route serves the files *inside* a folder, which
    previously had no page of their own. Reachable by anyone who can reach the
    parent collection OR who holds a grant on the file itself (per-file
    sharing), so a file shared out of a folder is actually openable.

    404 for missing AND for no-access, matching the collection contract, so the
    URL space can't be probed.
    """
    from app.auth.access import can_access_collection
    from app.resource_types import ResourceType
    from app.services.library_sharing import visibility_for

    col = file_corpora_repo().get_by_slug(slug)
    if not col:
        raise HTTPException(status_code=404, detail="collection_not_found")
    row = corpus_files_repo().get(file_id)
    if not row or row.get("corpus_id") != col["id"]:
        raise HTTPException(status_code=404, detail="file_not_found")

    is_admin = is_user_admin(user["id"], conn)
    can_parent = is_admin or can_access_collection(user["id"], col["id"], conn)
    file_granted = False
    if not can_parent:
        try:
            file_granted = file_id in set(
                resource_grants_repo().list_resource_ids_for_user(user["id"], ResourceType.CORPUS_FILE.value)
            )
        except Exception:
            file_granted = False
    if not (can_parent or file_granted):
        raise HTTPException(status_code=404, detail="file_not_found")

    size = row.get("size_bytes")
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_admin,
        collection=col,
        file=row,
        file_size_display=_human_size(size) if size else None,
        file_visibility=visibility_for(ResourceType.CORPUS_FILE.value, file_id),
        # An owner (or admin) may change this one file's sharing from here.
        can_share=is_admin or col.get("created_by") == user["id"],
    )
    return templates.TemplateResponse(request, "library_file_detail.html", ctx)


# Facts section page size (spec §13.2 "Collection detail" — a paged per-fact
# row list). Small on purpose: each row can carry a conflict block, so the
# page stays a state read-out, not a data dump.
_FACTS_SECTION_PAGE_SIZE = 20


@router.get("/library/{slug}", response_class=HTMLResponse)
async def library_detail(
    slug: str,
    request: Request,
    facts_page: int = 1,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Collection detail — files + per-file processing status + search box."""
    from app.api.store import _resolve_owner_display
    from app.auth.access import can_access_collection
    from app.resource_types import ResourceType
    from app.services.library_sharing import visibility_for

    col = file_corpora_repo().get_by_slug(slug)
    # Return 404 for both "missing" and "access denied" so an unprivileged
    # caller can't distinguish the two and probe for collection existence
    # (matches the GET /api/collections/{id} contract).
    if not col:
        raise HTTPException(status_code=404, detail="collection_not_found")
    is_admin = is_user_admin(user["id"], conn)
    # Owner-aware: the creator can open their private upload without a grant.
    if not is_admin and not can_access_collection(user["id"], col["id"], conn):
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(col["id"])
    # Owner + sharing are rail facts on every resource detail page (see the page
    # contract in macros/_detail.html); the collection page was the one artefact
    # surface that stated neither, so "who can see this folder?" was only
    # answerable from the Library table it was opened from.
    owner_id = col.get("created_by")

    # Facts section (spec §13.2 "Collection detail" — state, not settings):
    # flag off / DuckDB backend / zero facts -> `facts_summary` stays None and
    # the template simply omits the section, no error, no empty shell. Scoped
    # to the ACTUAL CALLER (`user`), never `owner_id` — a caller who can open
    # this page via a group grant must see exactly what their own grants
    # cover, not the owner's.
    facts_summary = None
    facts_repo_ = _facts_repo_if_available()
    if facts_repo_ is not None:
        try:
            page = max(1, facts_page)
            summary = facts_repo_.collection_facts_summary(
                user,
                col["id"],
                limit=_FACTS_SECTION_PAGE_SIZE,
                offset=(page - 1) * _FACTS_SECTION_PAGE_SIZE,
            )
            if summary["total"] > 0:
                facts_summary = {**summary, "page": page, "page_size": _FACTS_SECTION_PAGE_SIZE}
        except Exception as e:
            logger.warning("/library/%s: facts summary failed: %s", slug, e)

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_admin,
        collection=col,
        files=files,
        owner_name=(_resolve_owner_display(owner_id) if owner_id else None),
        collection_visibility=visibility_for(ResourceType.COLLECTION.value, col["id"]),
        can_share=is_admin or owner_id == user["id"],
        facts_summary=facts_summary,
    )
    return templates.TemplateResponse(request, "library_detail.html", ctx)


@router.get("/catalog/t/{table_id}", response_class=HTMLResponse)
async def catalog_table_detail(
    table_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-table drill-down — sample questions, columns, things to know,
    pairs-well-with. Closes the "/catalog detail bounces into /admin"
    UX gap: this is the user-facing surface for table docs, and admins
    edit those docs inline on the same page instead of round-tripping
    through /admin/tables.

    RBAC: admin god-mode or grant on ANY data package containing this
    table. Falls back to 403 otherwise — analysts only see tables that
    belong to packages they're granted on.
    """
    from src.rbac import get_accessible_ids
    from src.access_policy import effective_schema
    from app.resource_types import ResourceType

    table_repo = table_registry_repo()
    table = table_repo.get(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="table_not_found")

    # Display healing: rows written by the pre-fix Data-sources wizard carry
    # the full `bucket.table` id in source_table; the template renders
    # `bucket`.`source_table`, which would double the bucket prefix. The sync
    # path strips the prefix at use (normalize_source_table) — mirror it here
    # so the page shows the id the export actually targets.
    if table.get("bucket") and table.get("source_table"):
        from connectors.keboola.storage_api import normalize_source_table

        table = {**table, "source_table": normalize_source_table(table["bucket"], table["source_table"])}

    # Find every package that includes this table; gate access on
    # admin god-mode OR a grant on ANY of those packages. Resolve the
    # caller's accessible DATA_PACKAGE set ONCE (was a per-package
    # `can_access` call) and the {package_id -> [table_id, ...]} member
    # map in one bulk query (was a `list_tables(pkg_id)` call per
    # package — the worst N+1 in this file).
    pkg_repo = data_packages_repo()
    accessible_pkg_ids = get_accessible_ids(user, ResourceType.DATA_PACKAGE.value, conn)  # None => admin/all
    is_admin = accessible_pkg_ids is None
    parent_packages = []
    has_grant = False
    try:
        # Bulk {package_id -> [table_id, ...]} membership map in one query
        # (was a `list_tables(pkg_id)` call per package). Kept INSIDE the
        # try/except so a bulk-query failure degrades gracefully (logged +
        # fail-closed below) instead of 500ing the route — same contract as
        # the per-package lookup it replaced.
        member_map = pkg_repo.list_member_ids_bulk()
        # Walk packages (instances are small enough that this is fine) —
        # only to preserve name-ordering of `parent_packages` and find the
        # ones that contain this table; membership itself is now a dict
        # lookup, not a query.
        for p in pkg_repo.list(limit=10000):
            mem_ids = member_map.get(p["id"], [])
            if table_id not in mem_ids:
                continue
            parent_packages.append({"slug": p["slug"], "name": p["name"]})
            if not has_grant and not is_admin:
                if p["id"] in accessible_pkg_ids:
                    has_grant = True
    except Exception:
        logger.warning("could not enumerate parent packages for %s", table_id, exc_info=True)
    if not (is_admin or has_grant):
        raise HTTPException(status_code=403, detail="access_denied")

    # Resolve any pairs_well_with ids to (id, name) pairs the template
    # can render as links. Unknown ids (deleted tables) silently dropped.
    pairs = []
    for related_id in table.get("pairs_well_with") or []:
        related = table_repo.get(related_id)
        if related:
            pairs.append({"id": related["id"], "name": related["name"]})

    # Columns from /api/admin/tables/{id}/profile if it exists in
    # table_profiles, else empty. Cheap read; non-admin doesn't need
    # the full profile, just the column list.
    columns = []
    try:
        prof = profile_repo().get(table_id)
        if prof:
            for col in prof.get("columns") or []:
                columns.append(
                    {
                        "name": col.get("name"),
                        "type": col.get("type"),
                        "nullable": col.get("nullable", True),
                    }
                )
    except Exception:
        logger.warning("could not load profile for %s", table_id, exc_info=True)

    # Fallback: when table_profiles has no row (table never synced, or
    # profile was wiped), introspect schema via `build_schema` — the same
    # RBAC- and policy-aware wrapper /api/v2/schema uses — rather than
    # `build_schema_uncached` directly, which by its own docstring skips
    # BOTH RBAC and the Task 9 effective-schema override ("call only from
    # contexts where those are unnecessary (warmup) or already enforced
    # upstream"). Handles every source type — internal via
    # connectors.internal, BigQuery remote via the BQ extension, local +
    # materialized via DESCRIBE on the parquet. Best-effort — any failure
    # (parquet missing, BQ creds absent, etc.) leaves the columns section
    # in its "run a sync" empty state.
    if not columns:
        try:
            from app.api.v2_schema import build_schema
            from connectors.bigquery.access import BqAccess

            sch = build_schema(conn, user, table_id, bq=BqAccess())
            for col in sch.get("columns") or []:
                columns.append(
                    {
                        "name": col.get("name"),
                        "type": col.get("type"),
                        "nullable": col.get("nullable", True),
                    }
                )
        except Exception:
            logger.warning("schema introspection fallback failed for %s", table_id, exc_info=True)

    # Table access policies (§11): the profile-derived column list above is
    # UNFILTERED raw schema — `table_profiles` is computed from the
    # physical table regardless of any later-attached policy — so an
    # EXCLUDE'd column's name would otherwise survive into "What's inside"
    # for a non-admin even after the fallback above started using the
    # effective-schema-aware `build_schema`. One filter here, applied AFTER
    # either source has populated `columns`, closes both paths at once.
    # Fails closed: a resolution problem suppresses the whole column list
    # rather than risk showing a name this caller cannot actually read.
    #
    # `profile_repo()` also carries min/max/sample_values/top_values (§11's
    # "sharper leak") for every surviving column, but this route never
    # forwards those fields into `columns` or the template context — only
    # name/type/nullable are extracted above — so there is nothing further
    # to suppress here; see `app/api/catalog.py`'s `GET /profile/{id}` and
    # `POST /profile/{id}/refresh`, which DO serve that stats payload and
    # are gated the same way.
    if table.get("access_policy_sql"):
        try:
            effective_cols = effective_schema(table_id, user)
        except Exception:
            logger.warning(
                "effective-schema check failed for policied table %s; suppressing columns",
                table_id,
                exc_info=True,
            )
            columns = []
        else:
            if effective_cols is not None:
                visible_names = {c["name"] for c in effective_cols if not c.get("hidden")}
                columns = [c for c in columns if c["name"] in visible_names]

    last_sync_state = sync_state_repo().get_table_state(table_id) or {}

    def _fmt_bytes(n):
        if n is None or n <= 0:
            return None
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024:
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
            n /= 1024
        return f"{n:.1f} PiB"

    rows_count = last_sync_state.get("rows")
    size_bytes = last_sync_state.get("file_size_bytes") or last_sync_state.get("uncompressed_size_bytes")

    ctx = _build_context(
        request,
        user=user,
        table=table,
        parent_packages=parent_packages,
        pairs_well_with=pairs,
        columns=columns,
        last_sync_display=(str(last_sync_state.get("last_sync"))[:19] if last_sync_state.get("last_sync") else None),
        rows_display=(f"{rows_count:,}" if rows_count else None),
        size_display=_fmt_bytes(size_bytes),
        sample_questions=(table.get("sample_questions") or []),
        things_to_know=table.get("things_to_know") or "",
    )
    return templates.TemplateResponse(request, "catalog_table_detail.html", ctx)


@router.get("/catalog/r/{slug}", response_class=HTMLResponse)
async def catalog_recipe_detail(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-recipe drill-down — title, description, SQL template, related
    tables. Admins see every recipe (incl. drafts); non-admins see only
    ``prod`` recipes their groups have a ``resource_grants`` row for.
    Returns 404 (not 403) so unprivileged callers can't probe for the
    existence of a recipe they aren't allowed to know about.
    """
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    recipe = recipes_repo().get_by_slug(slug)
    if not recipe:
        raise HTTPException(status_code=404, detail="recipe_not_found")
    is_admin = is_user_admin(user["id"], conn)
    if not is_admin:
        if (recipe.get("status") or "prod") != "prod":
            raise HTTPException(status_code=404, detail="recipe_not_found")
        if not can_access(user["id"], ResourceType.RECIPE.value, recipe["id"], conn):
            raise HTTPException(status_code=404, detail="recipe_not_found")

    table_repo = table_registry_repo()
    related_tables = []
    for tid in recipe.get("related_table_ids") or []:
        full = table_repo.get(tid)
        if full:
            related_tables.append({"id": full["id"], "name": full["name"]})

    ctx = _build_context(
        request,
        user=user,
        recipe=recipe,
        related_tables=related_tables,
    )
    return templates.TemplateResponse(request, "catalog_recipe_detail.html", ctx)


def _human_size(n: int) -> str:
    """Format bytes as a short human string. Mirrors the format used on
    the marketplace card meta line."""
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} PB"


def _memory_domain_entry_dict(
    entry,
    drilldown_url: str,
    items_count: int = 0,
    required_count: int = 0,
    in_stack_is_local: Optional[bool] = None,
) -> dict:
    """Adapt a ResourceEntry (memory_domain) → template entry dict.

    Always renders a meta line (`N items · K required` — even `0 items`)
    and a description fallback so seeded canonical domains without an
    admin-authored description don't render as half-empty cards.

    Auto-membership: see ``_data_package_entry_dict``'s docstring — this
    dict's ``in_stack`` key mirrors ``entry.materialized`` (local-download
    state), not raw stack membership (always True post auto-membership).
    """
    meta = f"{items_count} item{'s' if items_count != 1 else ''}"
    if required_count:
        meta += f" · {required_count} required"
    description = entry.description or (f"Curated knowledge for the {entry.name} domain.")
    return {
        "id": entry.id,
        "name": entry.name,
        "description": description,
        "icon": entry.icon or "🎯",
        "color": entry.color or "#e0f2fe",
        # v50: see _data_package_entry_dict for the cover_image_url contract.
        "cover_image_url": getattr(entry, "cover_image_url", None),
        # v51: status surfaces as the cover-corner pill. Memory Domains
        # have no per-card category (the domain IS the category).
        "status": getattr(entry, "status", None) or "prod",
        "category": None,
        "requirement": entry.requirement,
        "in_stack": getattr(entry, "materialized", False),
        # #1206: says OUT LOUD what the line above did quietly. Under
        # auto-membership `in_stack` no longer means "in the caller's
        # stack" — every granted resource already is — it means "a local
        # copy exists". The card macro cannot infer that from the key
        # name, so it read the old meaning and invited you to "Add to
        # stack" a package listed under My Stack.
        #
        # Gated on the membership mode rather than hardcoded True. Under the
        # classic subscribe model `in_stack` means membership again, and the
        # flag would have the card offer "Remove local copy" for a control
        # that actually unsubscribes — the user loses ACCESS believing they
        # are freeing disk. Consumers that do not re-point the key omit the
        # flag and keep the old wording; classic is one of them.
        "in_stack_is_local": _resolve_in_stack_is_local(in_stack_is_local),
        "meta": meta,
        # The same fact as a NUMBER — see `table_count` on the package dict.
        "items_count": items_count,
        "tags": [],
        "drilldown_url": drilldown_url,
        "footer_left": (f"View {items_count} item{'s' if items_count != 1 else ''} →" if items_count else "Open →"),
    }


@router.get("/corporate-memory", response_class=HTMLResponse)
async def corporate_memory(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Curated Memory web view — any authenticated user.

    v49 (Task 8.4): the top-level page is a Browse of memory domains
    using the shared `_stack_card.html` macro; the per-item richness
    (votes, contributors, tags, edit, dismiss) moves to /memory/d/<slug>
    (Task 8.5). The admin review queue lives separately at
    /admin/corporate-memory behind require_admin.

    Gating matches the underlying ``/api/memory/*`` endpoints, which
    already run on ``get_current_user`` — CLI / agent flows that POST a
    knowledge item or read ``/api/memory`` work for any authenticated
    user, so the web view does too. Admin-only affordances on this page
    (the pending-review banner) stay gated server-side: ``is_admin_view``
    zeroes ``pending_review_count`` for non-admins.
    """
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    # Rail: the Library's Memory band IS this page now (counts, add-to-stack,
    # the empty-domain rule all moved there — spec 2026-08-12). 302, not 308,
    # so a later layout flip is not cached permanently. Topnav serves the
    # frozen pre-redesign page below, untouched.
    if get_ui_layout() == "rail":
        # ...but ONLY when that band will actually contain a row for this
        # caller. The band drops a granted domain whose known item count is
        # zero unless the mandate is `required`, so a caller with no grants —
        # or only empty optional ones — would land on a Library with no Memory
        # band and nothing saying where the page went. `/apps` already guards
        # its twin redirect this way; asymmetry there was the finding.
        # Anything unreadable counts as "redirect" rather than stranding the
        # caller here: the band renders rows without counts when the count
        # read fails, so it is the surface with more to say, not less.
        try:
            _c = memory_domains_repo().count_items_by_domain()
        except Exception:
            _c = None
        try:
            _band_has_row = any(
                _c is None or _c.get(e.id, (0, 0))[0] > 0 or e.requirement == "required"
                for e in StackResolver(conn).browse(user["id"], ResourceType.MEMORY_DOMAIN)
            )
        except Exception:
            _band_has_row = True
        if _band_has_row:
            return RedirectResponse(url="/library?section=memory_domain", status_code=302)

    resolver = StackResolver(conn)
    domains_repo = memory_domains_repo()
    repo = knowledge_repo()

    # Per-domain counts (items + required) computed once and indexed by id.
    dom_meta: dict[str, dict] = {}
    try:
        for d in domains_repo.list(limit=10000):
            summaries = domains_repo.list_items_of_domain(d["id"], limit=10000)
            required = sum(1 for s in summaries if s.get("is_required"))
            dom_meta[d["id"]] = {
                "items_count": len(summaries),
                "required_count": required,
                "slug": d["slug"],
            }
    except Exception as e:
        logger.warning("could not enumerate memory_domains: %s", e)

    is_admin_view = is_user_admin(user["id"], conn)

    # Stack-membership mode — same fork as /catalog (spec
    # 2026-08-07-default-chrome-ux-parity): classic (default) restores the
    # pre-redesign behavior verbatim (admin god-mode Browse, full granted
    # list); auto-membership browses grant-scoped for everyone (auditing
    # lives at /admin/data-packages) and reshapes Browse to addable-only.
    # For MY STACK we always call the resolver — admins who POST
    # /api/stack/subscribe expect to see those subscriptions in their
    # stack tab.
    from app.instance_config import get_stack_auto_membership

    auto_membership = get_stack_auto_membership()
    if is_admin_view and not auto_membership:
        browse_entries = resolver.browse_admin(user["id"], ResourceType.MEMORY_DOMAIN)
    else:
        browse_entries = resolver.browse(user["id"], ResourceType.MEMORY_DOMAIN)
    stack_entries = resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN)

    # Required-first grouping mirrors /catalog (first-demo feedback);
    # under auto-membership it applies to BOTH grids — see /catalog's
    # ``_req_first_key`` comment — while classic keeps the pre-redesign
    # contract (Browse only).
    _req_first_key = lambda e: (0 if e.requirement == "required" else 1, e.name or "")  # noqa: E731
    browse_entries = sorted(browse_entries, key=_req_first_key)
    if auto_membership:
        stack_entries = sorted(stack_entries, key=_req_first_key)

    # Catalog reshape (auto-membership only): every granted domain is
    # already in_stack=True there, so the Browse grid — "things you can
    # ADD" — only shows entries NOT already in the caller's stack. Classic
    # renders the full granted set, pre-redesign style.
    addable_entries = [e for e in browse_entries if not e.in_stack] if auto_membership else browse_entries

    def _adapt(e):
        meta = dom_meta.get(e.id, {})
        slug = meta.get("slug")
        return _memory_domain_entry_dict(
            e,
            drilldown_url=f"/memory/d/{slug}" if slug else f"/corporate-memory#{e.id}",
            items_count=meta.get("items_count", 0),
            required_count=meta.get("required_count", 0),
        )

    # Hide empty domains from the user-facing browse list — a domain with
    # zero items has nothing for an analyst to opt-into. Admins manage
    # empty placeholders from /admin/corporate-memory#domains. Required
    # domains (items_count == 0 but still mandated) stay visible so the
    # mandate is honored even if the items were just deleted.
    def _has_content(e):
        meta = dom_meta.get(e.id, {})
        return meta.get("items_count", 0) > 0 or e.requirement == "required"

    entries = [_adapt(e) for e in addable_entries if _has_content(e)]
    stack_entries_adapted = [_adapt(e) for e in stack_entries if _has_content(e)]

    # Pending banner contract (issue #176) — admin-only, counts items in
    # status='pending'. Kept identical to the legacy route so the page test
    # (test_corporate_memory_page.py) keeps passing.
    pending_count = 0
    if is_admin_view:
        try:
            pending_count = repo.count_items(statuses=["pending"])
        except Exception:
            pending_count = 0

    ctx = _build_context(
        request,
        user=user,
        entries=entries,
        stack_entries=stack_entries_adapted,
        pending_review_count=pending_count,
        is_km_admin=is_admin_view,
    )
    return templates.TemplateResponse(request, "corporate_memory.html", ctx)


@router.get("/memory/d/{slug}", response_class=HTMLResponse)
async def memory_domain_detail(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-domain drill-down — header + per-item richness (Task 8.5).

    Preserves the full per-item affordance set from the legacy /corporate-
    memory page: votes, contributors, tags, category/source/required
    badges, dismiss/undismiss, mark-personal toggle, admin edit link.
    """
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver

    domains_repo = memory_domains_repo()
    repo = knowledge_repo()
    domain = domains_repo.get_by_slug(slug)
    if not domain:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    if not (
        is_user_admin(user["id"], conn) or can_access(user["id"], ResourceType.MEMORY_DOMAIN.value, domain["id"], conn)
    ):
        raise HTTPException(status_code=403, detail="access_denied")

    source_hint = request.query_params.get("source", "direct")
    try:
        usage_repo().emit_server_event(
            event_type="memory_domain.view",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props={"slug": slug, "source": source_hint},
        )
    except Exception:
        logger.warning("usage_events emit failed for memory_domain.view")

    resolver = StackResolver(conn)
    effective_required = resolver.is_required(user["id"], ResourceType.MEMORY_DOMAIN, domain["id"])
    in_stack = effective_required or user_stack_subscriptions_repo().is_subscribed(
        user["id"], ResourceType.MEMORY_DOMAIN.value, domain["id"]
    )

    # Hydrate items with votes + contributors + dismissed-by-me + tags.
    summaries = domains_repo.list_items_of_domain(domain["id"], limit=10000)
    dismissed_set = set(repo.list_dismissed_ids(user["id"])) if user.get("id") else set()
    items: list[dict] = []
    required_count = 0
    for s in summaries:
        it = repo.get_by_id(s["id"])
        if not it:
            continue
        if it.get("is_required"):
            required_count += 1
        votes = repo.get_votes(it["id"])
        it["upvotes"] = votes["upvotes"]
        it["downvotes"] = votes["downvotes"]
        it["dismissed_by_me"] = it["id"] in dismissed_set
        # Contributor avatars from source_user (single contributor today).
        su = (it.get("source_user") or "").strip()
        if su:
            name = su.split("@", 1)[0]
            parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
            if len(parts) >= 2:
                initials = (parts[0][0] + parts[1][0]).upper()
            elif parts:
                initials = parts[0][:2].upper()
            else:
                initials = name[:2].upper()
            it["contributors_display"] = [{"name": name, "initials": initials}]
        else:
            it["contributors_display"] = []
        items.append(it)

    # Sort: required first, then by created_at desc (stable + predictable).
    items.sort(
        key=lambda r: (
            not r.get("is_required"),
            -((r.get("created_at") or 0).timestamp() if hasattr(r.get("created_at") or 0, "timestamp") else 0),
        )
    )

    # Tag user with is_admin flag for template-side admin affordances.
    user_render = dict(user)
    user_render["is_admin"] = is_user_admin(user["id"], conn)

    ctx = _build_context(
        request,
        user=user_render,
        domain=domain,
        items=items,
        required_count=required_count,
        effective_requirement="required" if effective_required else "available",
        in_stack=in_stack,
        # Where the visitor came from, so the hero's back link returns THERE
        # rather than always to the memory listing (the Library links in with
        # ?source=library). Same value the view event already carries.
        source=source_hint,
    )
    return templates.TemplateResponse(request, "memory_domain_detail.html", ctx)


def _chrome_ctx(request: Request, user: Optional[dict]) -> dict:
    """Single owner of every chrome-level template-context key (#996).

    Routes that render ``base_ds.html``/``base_page.html`` MUST spread this
    in — otherwise the navbar, theme, branding, and url helpers render empty
    (the studio pages regressed on exactly this: no top menu, no styling).
    ``_build_context`` composes this same dict for its (heavier) pages, so a
    chrome key only ever needs to be added HERE to reach both — see its
    docstring. ``is_admin`` is deliberately NOT a chrome key: unlike the keys
    below, it isn't needed by the shared header/rail (which reads
    ``session.user.is_admin`` instead) and most ``_build_context`` callers
    compute it themselves (often reusing a request-scoped ``conn``); adding
    it here would silently grant it to every page and cost an extra,
    cache-less ``is_user_admin()`` lookup none of them asked for. The one
    ``_chrome_ctx`` page whose *own* template reads top-level ``is_admin``
    (``/admin/studio/{domain}``) sets it explicitly, the same way
    ``_build_context`` callers do.
    """
    return {
        "request": request,
        "user": _flex(user) if user else _FlexDict(),
        "now": datetime.now,
        "get_flashed_messages": lambda **kw: [],
        "url_for": lambda endpoint, **kw: _url_for_shim(endpoint, **kw),
        "session": _FlexDict({"user": user}) if user else _FlexDict(),
        "home_route": _resolved_home_route(),
        "instance_name": get_instance_name(),
        "instance_brand": get_instance_brand(),
        "instance_brand_short": get_instance_brand_short(),
        "workspace_dir": get_workspace_dir_name(),
        "workspace_launcher_word": get_workspace_launcher_word(),
        "instance_theme": get_instance_theme(),
        # Resolved to a ready-to-use `<link rel="icon">` href (env/YAML
        # value as-is for a data:/absolute URL, otherwise static_url()-
        # wrapped) — see get_instance_favicon(). Set here, not duplicated in
        # _build_context, same as instance_brand/instance_theme above: this
        # dict is composed into _build_context's context too (#996).
        "instance_favicon": get_instance_favicon(),
        "home_automode": {"show": get_home_automode_visibility()},
        "custom_scripts": get_custom_scripts(),
        # Set here too (not only in _build_context) so the Studio nav link
        # survives on pages that render via _chrome_ctx — including the studio
        # pages themselves and the command palette.
        "can_studio": get_studio_enabled(),
        # "My agents" nav entry visibility — instance-level toggle, mirrors
        # can_studio (the hard gate lives on the /agents route + the API
        # routers, this only hides the entry point).
        "can_agent_profiles": get_agent_profiles_enabled(),
        # MCP connector surface visibility (#1024), same reason as
        # can_agent_profiles above — set independently here so it survives on
        # every _chrome_ctx page too.
        "can_mcp_connector_ui": get_mcp_connector_ui_enabled(),
        # Same `config` object as _build_context — templates read
        # config.INSTANCE_NAME in <title> blocks and the header logo, which
        # rendered empty on _chrome_ctx pages ("Studio — " title).
        "config": _config_proxy(),
        # Same visibility rule as _build_context — the shared header hides
        # the Chat nav link when this key is missing/False, so skipping it
        # here made the link vanish on every _chrome_ctx page (/admin/studio*).
        "can_chat": _compute_can_chat(request, user),
    }


# ---------------------------------------------------------------------------
# Hosted data apps — /apps web UI (Task 12)
# ---------------------------------------------------------------------------
#
# A DEDICATED router, not routes on the main ``router`` above. The ingress
# proxy (``app/api/data_apps_proxy.py``) registers
# ``GET /apps/{slug}`` (redirect to trailing slash) and
# ``GET/POST/... /apps/{slug}/{path:path}`` (the actual proxy), and
# ``app/main.py`` includes ``data_apps_proxy_router`` BEFORE the main
# ``web_router``. Starlette matches routes in registration-list order, not
# by specificity, so a literal ``/apps/detail/{slug}`` living on the main
# ``router`` would never be reached: the proxy's ``{path:path}`` catch-all
# matches ``/apps/detail/<slug>`` first (slug="detail", path="<slug>").
# ``apps_web_router`` is included in ``app/main.py`` BEFORE
# ``data_apps_proxy_router`` specifically so these two literal routes win
# that match race; the bare ``GET /apps`` (list page) route doesn't
# actually collide with anything (both proxy routes require at least one
# path segment after ``apps``), but it lives here too for locality.
apps_web_router = APIRouter(tags=["web-data-apps"])

# State -> `.badge--*` accent modifier (design-system vocabulary, see
# style-custom.css's "Badges — accent vocabulary" block). `created` and
# `stopped` get the neutral base `.badge` (no modifier) — nothing to
# highlight, they're just "not currently running".
_STATE_BADGE_CLASS = {
    "running": "badge--success",
    "deploying": "badge--info",
    "sleeping": "badge--warn",
    "error": "badge--danger",
}


def _state_badge_class(state: str) -> str:
    return _STATE_BADGE_CLASS.get(state, "")


@apps_web_router.get("/apps", response_class=HTMLResponse)
async def data_apps_list_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """List the hosted data apps the caller may view.

    Reuses ``app.api.data_apps``'s own serializer/visibility helpers (no
    parallel RBAC/shape logic) — server-rendered, matching the other
    inventory-style pages (``studio_index``, ``admin_marketplaces``) rather
    than a client-side ``fetch('/api/data-apps')``.

    When the feature is disabled, renders an empty-state note instead of
    404ing — the nav item is already hidden via ``data_apps_enabled()``, so
    a direct hit here (bookmark, typed URL) should explain why nothing is
    here rather than look like a broken link.
    """
    from app.api.data_apps import _can_view, _serialize
    from src.repositories import data_apps_repo, users_repo

    enabled = feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False)
    # Rail: the apps inventory lives in the Library's Artefacts band now —
    # data apps sit among the caller's artifacts (spec 2026-08-12, revised:
    # rows keep type_key=data_app for the Type facet, but Files is their
    # home). Redirect ONLY when the Library will actually show the caller an
    # app row (the same grant-scoped visibility the Library band applies —
    # owner or granted, no admin god-mode): a caller with nothing visible
    # would land on a Library whose band never rendered, with nothing
    # explaining where the inventory went, so they keep this page's
    # explicit empty state instead (Devin review on this PR). Feature off →
    # same reasoning, page renders its explanatory note. 302, not 308
    # (layout/visibility flips must not be cached).
    if enabled and get_ui_layout() == "rail":
        from app.resource_types import ResourceType

        # Same grant-scoped visibility as the Library band (owner or explicit
        # grant, no admin god-mode) — the redirect must predict exactly what
        # the band will render, or an admin with nothing of their own would
        # bounce onto a Library with no app rows. The grant set is fetched
        # ONCE, exactly as the band fetches it (`_granted_ids` in
        # `library_page`) — a per-row `has_explicit_grant` call here was one
        # grant lookup per registered app on every /apps hit (Devin review
        # on PR #1278).
        try:
            _app_grants = set(
                resource_grants_repo().list_resource_ids_for_user(user["id"], ResourceType.DATA_APP.value)
            )
        except Exception as e:
            logger.warning("/apps: could not resolve data-app grants: %s", e)
            _app_grants = set()
        _visible_any = any(
            r.get("state") != "linked_hidden" and (r["owner_user_id"] == user["id"] or r["slug"] in _app_grants)
            # `limit=100000`, matching the Library band. The repo's default
            # cap is 1000 and it is applied in SQL BEFORE the ownership/grant
            # filter, so on a large instance an owner's older app drops out of
            # this predicate while the band would still list it — the redirect
            # would strand exactly the person it exists to forward.
            for r in data_apps_repo().list(include_drafts=False, limit=100000)
        )
        if _visible_any:
            return RedirectResponse(url="/library?section=files", status_code=302)

    cfg = get_data_apps_config()
    apps: list[dict] = []
    if enabled:
        u_repo = users_repo()
        # `linked_hidden` = a linked app that disappeared upstream (row +
        # grants kept for lossless re-link). The API list/detail/PATCH
        # surfaces already exclude it — mirror that here so a granted user
        # doesn't keep seeing an "Open ↗" onto a dead external URL.
        rows = [
            r
            for r in data_apps_repo().list(include_drafts=False)
            if r.get("state") != "linked_hidden" and _can_view(user, r)
        ]
        for row in rows:
            serialized = _serialize(row, cfg)
            owner = u_repo.get_by_id(row["owner_user_id"])
            serialized["owner_email"] = (owner or {}).get("email") or row["owner_user_id"]
            serialized["badge_class"] = _state_badge_class(row["state"])
            apps.append(serialized)

    return templates.TemplateResponse(
        request,
        "data_apps.html",
        {**_chrome_ctx(request, user), "apps": apps, "data_apps_feature_enabled": enabled},
    )


@apps_web_router.get("/apps/detail/{slug}", response_class=HTMLResponse)
async def data_app_detail_page(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Detail page for a single hosted data app.

    Metadata + state render server-side; the logs `<pre>` and the
    Deploy/Stop buttons are client-side ``fetch`` calls against the existing
    control-plane API (``app/api/data_apps.py``) — this route only decides
    what to SHOW (``can_manage`` gates the mutating controls + the logs
    section, since ``GET .../logs`` is owner/Admin-only server-side too;
    hiding it for a viewer avoids a page-load fetch that would 403).
    """
    from app.api.data_apps import _can_view, _serialize
    from src.repositories import data_apps_repo, users_repo

    row = data_apps_repo().get_by_slug(slug)
    # Same hidden-state 404 as the API's _get_row_or_404: a soft-deleted
    # linked row (gone upstream) must not render a detail page with a live
    # link onto a dead external URL.
    if not row or row.get("state") == "linked_hidden":
        raise HTTPException(status_code=404, detail="data_app_not_found")
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")

    is_admin = is_user_admin(user["id"])
    is_owner = user["id"] == row["owner_user_id"]
    can_manage = is_owner or is_admin

    owner = users_repo().get_by_id(row["owner_user_id"])
    serialized = _serialize(row)
    serialized["owner_email"] = (owner or {}).get("email") or row["owner_user_id"]
    serialized["badge_class"] = _state_badge_class(row["state"])

    return templates.TemplateResponse(
        request,
        "data_app_detail.html",
        {
            **_chrome_ctx(request, user),
            "app": serialized,
            "is_owner": is_owner,
            "is_admin": is_admin,
            "can_manage": can_manage,
        },
    )


@router.get("/me/memory-mining", response_class=HTMLResponse)
async def me_memory_mining(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """User-facing privacy control: opt in/out of having one's own session
    transcripts mined into shared corporate memory (design spec §4.4)."""
    return templates.TemplateResponse(request, "me_memory_mining.html", _chrome_ctx(request, user))


@router.get("/admin/store/lint", response_class=HTMLResponse)
async def store_lint_admin_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Curator view of advisory skill-lint findings (#687).

    Findings are rendered server-side (grouped per entity); the page's JS only
    drives the "Audit now" and per-row Dismiss mutations against the admin lint
    API. Dismissed findings are hidden unless ``?include_dismissed=true``.
    """
    include_dismissed = request.query_params.get("include_dismissed") == "true"
    repo = store_lint_repo()
    findings = repo.all_latest_findings(include_dismissed=include_dismissed)
    entities = store_entities_repo()
    groups: list[dict] = []
    by_entity: dict[str, dict] = {}
    for f in findings:
        eid = f["entity_id"]
        group = by_entity.get(eid)
        if group is None:
            ent = entities.get(eid)
            group = {
                "entity_id": eid,
                "name": (ent or {}).get("name") or eid,
                "type": (ent or {}).get("type") or "skill",
                "findings": [],
            }
            by_entity[eid] = group
            groups.append(group)
        group["findings"].append(f)
    return templates.TemplateResponse(
        request,
        "admin_store_lint.html",
        {
            **_chrome_ctx(request, user),
            "groups": groups,
            "last_run": repo.last_run(),
            "include_dismissed": include_dismissed,
        },
    )


@router.get("/admin/studio", response_class=HTMLResponse)
async def studio_index(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Studio landing page — a card grid linking to every authoring domain.

    When Studio is enabled, available to all signed-in users (same gate as
    ``/admin/studio/{domain}``, not admin-only — most domains route non-admins
    through the suggestions queue instead of blocking them outright); when the
    instance-level toggle is off, every viewer is redirected home.
    Registered as a static path
    alongside (and before) ``/admin/studio/{domain}`` so it does not fall
    through to the dynamic domain matcher.
    """
    if not get_studio_enabled():
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request,
        "admin_studio_index.html",
        {
            **_chrome_ctx(request, user),
            "domains": list(STUDIO_DOMAINS.values()),
            "is_admin": is_user_admin(user["id"], conn),
        },
    )


@router.get("/admin/studio/suggestions", response_class=HTMLResponse)
async def studio_suggestions_admin(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin moderation queue for authoring-studio suggestions.

    Registered BEFORE ``/admin/studio/{domain}`` so the static ``suggestions``
    path wins over the dynamic domain matcher.
    """
    if not get_studio_enabled():
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "admin_studio_suggestions.html", _chrome_ctx(request, user))


@router.get("/admin/studio/{domain}", response_class=HTMLResponse)
async def studio(
    domain: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Authoring-agent studio — available to all signed-in users while the
    instance-level Studio toggle is on (off → redirect home).

    A generic form-based builder with an embedded assistant panel. The domain
    config (``app/web/studio.py``) drives the fields, the chat profile, and the
    create endpoint, so all five authoring agents share one surface. Most
    domains: admins create directly, non-admins submit a suggestion to the
    moderation queue (the page renders the right action via ``is_admin``).
    Domains with ``submit_directly=True`` (e.g. the Skill Builder) skip the
    queue entirely — everyone posts straight to ``endpoint``, which runs its
    own guardrail/review pipeline instead.
    """
    if not get_studio_enabled():
        return RedirectResponse("/")
    spec = get_studio_domain(domain)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown studio domain")
    return templates.TemplateResponse(
        request,
        "admin_studio.html",
        {
            **_chrome_ctx(request, user),
            "domain": spec,
            "profile_slug": spec.profile,
            # Not a chrome key (see _chrome_ctx's docstring) — this page's own
            # template is the one place that needs it, so it's set here.
            "is_admin": is_user_admin(user["id"]),
        },
    )


@router.get("/admin/corporate-memory", response_class=HTMLResponse)
async def corporate_memory_admin(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Curated Memory review queue — admin-only.

    The governance surface paired with the user-facing ``/corporate-memory``
    page: pending items awaiting review, contradictions, duplicate
    candidates, and the audit trail. Reached from the Admin nav dropdown.
    """
    repo = knowledge_repo()
    pending = repo.list_items(statuses=["pending"], limit=100)
    all_items = repo.list_items(limit=10000)
    status_counts = {}
    for item in all_items:
        s = item.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    # Contradictions tab is server-rendered (no JS fetch on this tab — see
    # admin_corporate_memory.html). Fetch the unresolved set and enrich each
    # entry with the title/sensitivity of both sides so the template doesn't
    # need to re-query per row.
    contradictions = repo.list_contradictions(resolved=False)
    item_lookup = {it["id"]: it for it in all_items}
    for c in contradictions:
        for side in ("item_a_id", "item_b_id"):
            base = item_lookup.get(c.get(side)) or {}
            target = "item_a" if side == "item_a_id" else "item_b"
            c[target] = {
                "title": base.get("title", ""),
                "content": base.get("content", ""),
                "domain": base.get("domain"),
                "sensitivity": base.get("sensitivity"),
                "status": base.get("status"),
                "hidden": base.get("is_personal", False),
            }

    # Duplicate-candidate badge count (issue #62) — unresolved relations only.
    duplicates_count = repo.count_relations(relation_type="likely_duplicate", resolved=False)

    # Mandate-form audience picker needs RBAC user_groups, not the
    # `corporate_memory.groups` YAML section — those are unrelated.
    # Template expects an array of {name, members_count} so it can render
    # `<option value="group:<name>">` rows in the per-item mandate form;
    # the previous shape (`{}` from the YAML config) crashed renderItemCard
    # with "GROUPS.map is not a function" the moment any pending item rendered.
    _groups_repo = user_groups_repo()
    _members_repo = user_group_members_repo()
    user_groups_for_ui = [
        {"name": g["name"], "members_count": _members_repo.count_members(g["id"])} for g in _groups_repo.list_all()
    ]

    # Existing-value pools for the per-item edit form pickers. Before, Category /
    # Audience / Tags were free-text required inputs — admins had to remember the
    # exact category slug or audience expression, and tags couldn't be discovered.
    # We surface what's already in the store as `<datalist>` suggestions (Category
    # / Tags) and a `<select>` (Audience built from RBAC groups) without losing
    # free-text entry for fresh values.
    edit_categories = sorted({i.get("category") for i in all_items if i.get("category")})
    edit_tags = sorted({t for i in all_items for t in (i.get("tags") or []) if t})

    knowledge_json_exists = (
        Path(os.environ.get("DATA_DIR", "./data")) / "corporate-memory" / "knowledge.json"
    ).exists()

    ctx = _build_context(
        request,
        user=user,
        pending_items=pending,
        stats={
            "total": len(all_items),
            "by_status": status_counts,
            "pending": len(pending),
            "pending_count": status_counts.get("pending", 0),
            "approved_count": status_counts.get("approved", 0),
            # v49: 'mandatory' as a status is gone — count items with the
            # ``is_required`` flag set instead. ``status_counts`` is built off
            # the status column so it can never produce a 'mandatory' bucket
            # again; project from the items list directly.
            "mandatory_count": sum(1 for i in all_items if i.get("is_required") is True),
            "knowledge_count": len(all_items),
            "contradictions": len(contradictions),
            "duplicates": duplicates_count,
        },
        groups=user_groups_for_ui,
        edit_categories=edit_categories,
        edit_tags=edit_tags,
        contradictions=contradictions,
        audit_entries=[],
        knowledge_json_exists=knowledge_json_exists,
    )
    return templates.TemplateResponse(request, "admin_corporate_memory.html", ctx)


@router.get("/activity-center")
async def activity_center_redirect():
    """Legacy URL — redirect to /admin/activity."""
    return RedirectResponse(url="/admin/activity", status_code=308)


@router.get("/admin/activity", response_class=HTMLResponse)
async def admin_activity(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Unified observability page — KPI cards, faceted filter bar, full
    audit_log table with sort/search/saved-views. All data loads
    client-side from /api/admin/observability/* + /api/admin/activity."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "activity_center.html", ctx)


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Setup instructions for the local agent (CLI + Claude Code).

    Single unified flow for everyone — admin-vs-analyst is no longer a
    layout branch. The marketplace + plugins block appears iff the
    caller has plugin grants in `resource_grants` (resolved inside
    `compute_default_agent_prompt`).

    When an admin override is saved, the override replaces the
    auto-generated setup_instructions output everywhere (both the
    /setup page display and the dashboard clipboard CTA). When no
    override is set, the live default from
    setup_instructions.resolve_lines() is used.
    """
    from src.welcome_template import compute_default_agent_prompt, _sanitize_banner_html
    from jinja2 import TemplateError

    from src.prompt_render import make_prompt_env

    base_url = str(request.base_url).rstrip("/")

    # Determine the script text: override (Jinja2-rendered) or live default.
    # The override is per-instance, applies to every caller — admins who set
    # an override are opting into the exact text they wrote. #622: resolution
    # honors the install prompt's source_mode toggle (editor DB override vs a
    # git-bound IWT file).
    from src.initial_workspace import resolve_prompt

    override_content, _mode = resolve_prompt("install", conn)
    if override_content:
        # Admin override — render Jinja2 placeholders server-side.
        # {server_url} and {token} survive because Jinja2 only processes
        # double-brace {{ }} syntax; single-brace {x} pass through unchanged.
        try:
            from src.welcome_template import build_context as _build_banner_ctx

            # Security audit F4: a non-sandboxed Environment lets an app-Admin's
            # install-prompt override execute arbitrary Python at render time
            # (SSTI → RCE on the FastAPI host). make_prompt_env() returns a
            # SandboxedEnvironment that blocks unsafe attribute/builtin access so
            # a payload like {{ cycler.__init__.__globals__[...] }} raises. The
            # SAME override is rendered by src/welcome_template.py and the
            # welcome/prompts preview endpoints — all route through the shared
            # factory so no sibling path is left unsandboxed.
            env = make_prompt_env()
            template = env.from_string(override_content)
            ctx_vars = _build_banner_ctx(user=user, server_url=base_url)
            setup_script_text = _sanitize_banner_html(template.render(**ctx_vars))
        except (TemplateError, Exception) as exc:
            logger.warning("setup_page: override render failed (%s); falling back to default", exc)
            setup_script_text = compute_default_agent_prompt(
                conn,
                user=user,
                server_url=base_url,
            )
    else:
        setup_script_text = compute_default_agent_prompt(
            conn,
            user=user,
            server_url=base_url,
        )

    # Split for the legacy setup_instructions_lines list variable that the
    # Jinja2 partial (_claude_setup_instructions.jinja) uses.
    setup_instructions_lines = setup_script_text.split("\n")

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        server_url=base_url,
        agnes_version=os.environ.get("AGNES_VERSION", "dev"),
        banner_html="",  # no separate banner — the script IS the content
        # Override both variables so the partial and the JS array stay in sync.
        setup_instructions_lines=setup_instructions_lines,
        setup_script_text=setup_script_text,
    )
    return templates.TemplateResponse(request, "install.html", ctx)


_WEB_CSRF_COOKIE = "web_csrf"


def _get_or_mint_web_csrf(request: Request) -> str:
    """Double-submit CSRF token for state-changing web form POSTs (F2).

    Reuses the caller's existing ``web_csrf`` cookie value when present so
    several open tabs keep working; otherwise mints a fresh random token. The
    issuing page must set the cookie via :func:`_set_web_csrf_cookie` on the
    same response that embeds the token in the form / page JS.
    """
    return request.cookies.get(_WEB_CSRF_COOKIE) or secrets.token_urlsafe(32)


def _set_web_csrf_cookie(response: Response, request: Request, token: str) -> None:
    """Set the ``web_csrf`` double-submit cookie (HttpOnly, SameSite=Strict)."""
    response.set_cookie(
        _WEB_CSRF_COOKIE,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def _refresh_web_csrf_cookie(response: Response, request: Request, token: str) -> None:
    """Set the cookie only when the caller did not already have one.

    Used on REJECTION paths. Because the cookie is ``SameSite=Strict`` a
    cross-site POST arrives without it, so unconditionally setting it there
    would let any site rotate a signed-in admin's token and break the tabs
    they already have open — a nuisance the CSRF check itself is supposed to
    prevent, not create (review finding on #1142).
    """
    if request.cookies.get(_WEB_CSRF_COOKIE):
        return
    _set_web_csrf_cookie(response, request, token)


def _web_csrf_ok(request: Request, supplied: str) -> bool:
    """True when the supplied token matches the ``web_csrf`` cookie (F2).

    A cross-site attacker cannot read the cookie, so cannot forge a matching
    hidden form field / ``X-CSRF-Token`` header; ``compare_digest`` keeps the
    comparison timing-safe. Compared as UTF-8 bytes: the str overload raises
    TypeError on non-ASCII input, and both values are caller-controlled.
    """
    cookie_token = request.cookies.get(_WEB_CSRF_COOKIE, "")
    return (
        bool(supplied)
        and bool(cookie_token)
        and secrets.compare_digest(supplied.encode("utf-8"), cookie_token.encode("utf-8"))
    )


_SLACK_BIND_CSRF_COOKIE = "slack_bind_csrf"


@router.get("/slack/bind", response_class=HTMLResponse)
async def slack_bind(
    request: Request,
    code: str = "",
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render the Slack-identity binding CONFIRMATION page (no state change).

    Security audit F2: the actual bind used to happen on this GET, gated only by
    a ``SameSite=Lax`` auth cookie — a classic CSRF sink. An attacker who owns a
    Slack identity could DM the bot for a code, then send a logged-in victim
    ``/slack/bind?code=<attacker_code>``; the top-level GET rode the victim's
    cookie and bound the ATTACKER's Slack to the VICTIM's Agnes account
    (cross-user impersonation). This GET now only shows a confirm button that
    POSTs back with a per-request double-submit CSRF token; the redeem lives in
    :func:`slack_bind_confirm`.

    An unauthenticated visitor is bounced to sign-in and lands back here (``next=``).
    """
    if user is None:
        nxt = quote(f"/slack/bind?code={code}", safe="")
        return RedirectResponse(url=f"/login?next={nxt}", status_code=302)

    if not code:
        ctx = _build_context(request, user=user, conn=conn, bind_status="missing")
        return templates.TemplateResponse(request, "slack_bind.html", ctx)

    # Mint a double-submit CSRF token: same value in a cookie and the form. A
    # cross-site attacker cannot read the cookie, so cannot forge a matching
    # form field. The cookie is short-lived + SameSite=Strict.
    csrf_token = secrets.token_urlsafe(32)
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        bind_status="confirm",
        bind_code=code.strip(),
        csrf_token=csrf_token,
    )
    response = templates.TemplateResponse(request, "slack_bind.html", ctx)
    response.set_cookie(
        _SLACK_BIND_CSRF_COOKIE,
        csrf_token,
        max_age=600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/slack/bind",
    )
    return response


@router.post("/slack/bind", response_class=HTMLResponse)
async def slack_bind_confirm(
    request: Request,
    code: str = Form(""),
    csrf_token: str = Form(""),
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Redeem a Slack binding code — the only state-changing bind path (F2).

    Requires a valid double-submit CSRF token (form field must equal the
    ``slack_bind_csrf`` cookie set by the GET confirmation page). Auth-gated so
    the bind completes only for the signed-in Agnes account.
    """
    if user is None:
        return RedirectResponse(url="/login", status_code=302)

    cookie_token = request.cookies.get(_SLACK_BIND_CSRF_COOKIE, "")
    if (
        not code
        or not csrf_token
        or not cookie_token
        # UTF-8 bytes: the str overload of compare_digest raises TypeError on
        # non-ASCII input, and the form field is caller-controlled.
        or not secrets.compare_digest(csrf_token.encode("utf-8"), cookie_token.encode("utf-8"))
    ):
        ctx = _build_context(request, user=user, conn=conn, bind_status="csrf")
        resp = templates.TemplateResponse(request, "slack_bind.html", ctx, status_code=400)
        resp.delete_cookie(_SLACK_BIND_CSRF_COOKIE, path="/slack/bind")
        return resp

    from services.slack_bot.binding import (
        BindingThrottled,
        redeem_verification_code,
    )

    status = "missing"
    try:
        ok = redeem_verification_code(conn, user_email=user["email"], code=code.strip())
        status = "ok" if ok else "invalid"
    except BindingThrottled:
        status = "throttled"
    except Exception:
        logger.exception("slack bind redeem failed")
        status = "error"

    ctx = _build_context(request, user=user, conn=conn, bind_status=status)
    resp = templates.TemplateResponse(request, "slack_bind.html", ctx)
    resp.delete_cookie(_SLACK_BIND_CSRF_COOKIE, path="/slack/bind")
    return resp


@router.get("/install", response_class=HTMLResponse)
async def install_redirect(request: Request):
    """Backwards-compat redirect: /install → /setup (302).

    Using 302 (temporary) rather than 301 (permanent) so browsers/proxies
    don't cache indefinitely — if the path ever changes again, cached 301s
    require manual cache clearing to recover.
    """
    return RedirectResponse(url="/setup", status_code=302)


# ---------------------------------------------------------------------------
# Store + My AI Stack — community marketplace + per-user composition page.
# ---------------------------------------------------------------------------


def _guardrail_thresholds() -> dict[str, int]:
    """Live admin-configurable thresholds surfaced into the upload UI.

    Each render reads the current value so the disclosure / counter /
    examples-table copy stays in lock-step with the
    /admin/server-config patch — no app restart required.
    """
    from app.instance_config import (
        get_guardrails_min_body_chars,
        get_guardrails_min_command_description_chars,
        get_guardrails_min_description_chars,
        get_guardrails_min_distinct_words,
    )

    return {
        "min_description_chars": get_guardrails_min_description_chars(),
        "min_command_description_chars": get_guardrails_min_command_description_chars(),
        "min_distinct_words": get_guardrails_min_distinct_words(),
        "min_body_chars": get_guardrails_min_body_chars(),
    }


@router.get("/store/new", response_class=HTMLResponse)
async def store_new(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.store_categories import STORE_CATEGORIES
    from src.store_naming import TITLE_ACRONYMS, sanitize_username

    try:
        owner_username = sanitize_username(user.get("email") or "")
    except ValueError:
        owner_username = ""
    from app.instance_config import get_guardrails_enabled, get_guardrails_llm_provider_ready

    _guardrails_enabled = get_guardrails_enabled()
    ctx = _build_context(
        request,
        user=user,
        categories=list(STORE_CATEGORIES),
        guardrail=_guardrail_thresholds(),
        title_acronyms=TITLE_ACRONYMS,
        owner_username=owner_username,
        guardrails_enabled=_guardrails_enabled,
        guardrails_llm_ready=_guardrails_enabled and get_guardrails_llm_provider_ready(),
    )
    return templates.TemplateResponse(request, "store_upload.html", ctx)


@router.get("/store/examples", response_class=HTMLResponse)
async def store_examples(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Examples of well-formed flea-market submissions.

    Linked from the content-guardrail rejection banner so a submitter
    whose bundle failed review can see what 'good' looks like
    side-by-side with the rule that bit them.
    """
    ctx = _build_context(request, user=user, guardrail=_guardrail_thresholds())
    return templates.TemplateResponse(request, "store_examples.html", ctx)


@router.get("/marketplace/flea/{entity_id}/edit", response_class=HTMLResponse)
async def store_edit(
    entity_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Edit page for a flea-market entity (v37 edit feature).

    Owner or admin only. Pre-fills metadata + lets the submitter
    optionally upload a new bundle (creates v<N+1>). Skipping the
    bundle field updates only metadata. Edit is blocked while a
    prior version is under review — the form surfaces a banner and
    disables Save in that case (the API gate also enforces 409
    server-side).
    """
    from app.auth.access import is_user_admin
    from src.store_categories import STORE_CATEGORIES

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    is_admin = is_user_admin(user["id"], conn)
    if entity["owner_user_id"] != user["id"] and not is_admin:
        # Same 404-no-leak as _enforce_visibility — strangers don't
        # learn of the entity's existence.
        raise HTTPException(status_code=404, detail="entity_not_found")

    pending_sub = None
    if entity.get("visibility_status") == "pending":
        latest = store_submissions_repo().latest_for_entity(entity_id)
        if latest and latest.get("status") in ("pending_inline", "pending_llm"):
            pending_sub = latest

    from src.store_naming import TITLE_ACRONYMS

    ctx = _build_context(
        request,
        user=user,
        entity=entity,
        is_admin=is_admin,
        is_owner=entity["owner_user_id"] == user["id"],
        categories=list(STORE_CATEGORIES),
        pending_sub=pending_sub,
        title_acronyms=TITLE_ACRONYMS,
        owner_username=entity.get("owner_username") or "",
        lint_findings=store_lint_repo().latest_findings(entity_id, include_dismissed=False),
    )
    return templates.TemplateResponse(request, "store_edit.html", ctx)


# Legacy /store/{id}, /store, and /my-ai-stack page surfaces all
# removed. The unified /marketplace?tab=flea + /marketplace?tab=my views
# replaced the listing pages, /marketplace/flea/{id} is the canonical
# detail surface, and /store/new (the upload wizard) survives as the
# only /store/* page route. Stale external bookmarks to the deleted
# pages 404 — accepted in dev-mode cleanup.


# ---------------------------------------------------------------------------
# Marketplace — unified browse + detail pages.
# ---------------------------------------------------------------------------


@router.get("/marketplace", response_class=HTMLResponse)
async def marketplace_listing(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """The Marketplace browse is folded into the Library.

    Its two tabs were Browse and My Stack — "everything there is" and "what I
    have" — over store entities and curated plugins the Library already lists
    in full (`store_entities_repo().list(visibility_status=['approved'])`, plus
    every granted `marketplace_plugin`). That is the same list under a second
    roof, and the tab pair is the same question the Library now asks of one
    list: `?tab=my` maps to `scope=mine` (the In stack segment) and the
    default browse tab to `scope=available` (the "Not in stack yet" filter),
    so an old link lands where it meant to.

    302 and the route stays registered, per the /corporate-memory, /apps and
    /stack retirements. Every detail and sub-route is untouched —
    `/marketplace/curated/{...}`, `/marketplace/flea/{id}`, the edit pages —
    because this folds the browse SHELL, not the store.
    """
    tab = (request.query_params.get("tab") or "").strip()
    target = "/library?scope=mine" if tab == "my" else "/library?scope=available"
    return RedirectResponse(url=target, status_code=302)


@router.get("/marketplace/flea/{entity_id}", response_class=HTMLResponse)
async def marketplace_flea_detail(
    request: Request,
    entity_id: str,
    from_source: str | None = Query(None, alias="from"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Pick the right detail template based on the entity type:
    plugins reuse the unified plugin layout; skills / agents render the
    item-detail layout (matches curated nested skill / agent).

    Visibility (v32+): non-owner non-admin gets 404 on any non-approved
    entity. Owner + admin see the page with a quarantine banner + the
    owner-actions strip (Edit / Delete with locked variants).
    """
    from app.api.store import _enforce_visibility
    from app.auth.access import is_user_admin

    repo = store_entities_repo()
    # Owner/admin get a version-status decorated entity so the versions
    # card can gate the Restore button on past-version approval state
    # (#316). Plain viewers don't see the versions card at all, so the
    # cheaper plain get() suffices.
    base_entity = repo.get(entity_id)
    if not base_entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    # Refuse early — same gate as the API + the asset endpoints. 404
    # (not 403) so the entity's existence isn't leaked.
    _enforce_visibility(base_entity, user, conn)

    is_owner = base_entity.get("owner_user_id") == user.get("id")
    is_admin = is_user_admin(user["id"], conn)

    entity = repo.get_with_version_approvals(entity_id) if (is_owner or is_admin) else base_entity

    # Pull the latest submission so the quarantine banner can render
    # the most recent verdict (inline_checks + llm_findings). v37:
    # always load for owner/admin, even when the entity itself is
    # approved at a prior version — under deferred promotion, a v2+
    # edit can leave the latest submission in `review_error` /
    # `blocked_llm` while the entity row stays approved. The banner
    # partial's gates (in `_quarantine_banner.html`) decide whether to
    # render; the handler just has to supply the data. Gating the
    # fetch on `visibility_status != 'approved'` silently hid the
    # failure from the owner — that was the regression #316 fixed.
    quarantine_sub = None
    if is_owner or is_admin:
        quarantine_sub = store_submissions_repo().latest_for_entity(entity_id)

    # v37: the Edit button locks while a submission is under review.
    edit_in_flight = bool(quarantine_sub and quarantine_sub.get("status") in ("pending_inline", "pending_llm"))

    # #1177: `hidden` means either "the author chose Private" or "guardrails
    # quarantined this", and only the submission history separates them. The
    # owner-actions strip used to read `hidden` as quarantine and lock Archive
    # — permanently, because a Private row never promotes off `hidden`. Resolve
    # it with the same predicate the API gate uses so the button and the
    # endpoint can never disagree.
    from app.api.store import is_own_unflagged_private

    own_private = is_own_unflagged_private(entity, user.get("id") or "")

    # The same verdict the DELETE endpoint reaches: an owner may withdraw an
    # upload that review has not objected to (`pending`, `review_error`).
    # Leaving the button disabled while the API allows the call is the same
    # bug one layer up — and the button is the only surface the author sees.
    # (Devin Review on #1263.)
    from app.api.store import entity_has_adverse_verdict

    owner_may_withdraw = (
        bool(entity.get("owner_user_id") and entity.get("owner_user_id") == (user.get("id") or ""))
        and entity.get("visibility_status") not in ("approved", "archived")
        and not entity_has_adverse_verdict(entity.get("id") or "")
    )

    # v104 trust strip. `entity_owner_label` resolves the byline the same way
    # the card does (display name → email → username) so the detail page never
    # shows a kebab-case username where the grid showed a real name.
    from app.api.store import _resolve_owner_display
    from app.instance_config import get_store_verification_enabled

    entity_owner_label = _resolve_owner_display(entity["owner_user_id"]) or entity.get("owner_username") or "Someone"

    common = dict(
        source="flea",
        entity=entity,
        entity_id=entity_id,
        is_owner=is_owner,
        is_admin=is_admin,
        entity_owner_label=entity_owner_label,
        store_verification_enabled=get_store_verification_enabled(),
        # v113: the same flag /library resolves, so this entity's detail page and
        # its Library row agree about whether the Community marker shows. They
        # previously could not: the detail page rendered only the two positive
        # claims and had no notion of the third.
        library_show_unverified_trust=feature_enabled(
            "library",
            "show_unverified_trust",
            env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
            default=_LIBRARY_TRUST_DEFAULT,
        ),
        quarantine_sub=quarantine_sub,
        edit_in_flight=edit_in_flight,
        own_private=own_private,
        owner_may_withdraw=owner_may_withdraw,
        # Where the visitor came from, so the detail page's back link can point
        # home to the right surface (e.g. ?from=skills → the Skill builder).
        from_source=from_source,
    )

    if entity["type"] == "plugin":
        ctx = _build_context(
            request,
            user=user,
            plugin_name=entity["name"],
            **common,
        )
        return templates.TemplateResponse(
            request,
            "marketplace_plugin_detail.html",
            ctx,
        )

    ctx = _build_context(
        request,
        user=user,
        kind=entity["type"],
        item_name=entity["name"],
        **common,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_item_detail.html",
        ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-renders only the shell — the page hydrates via
    ``GET /api/marketplace/curated/{slug}/{plugin}`` which carries the
    real RBAC guard. Direct URL access for users without the grant lands on
    a shell that 403s on the first XHR; UX-level the page renders an empty
    state and a back link."""
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_plugin_detail.html",
        ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_skill_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    skill_name: str,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        kind="skill",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        inner_name=skill_name,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_item_detail.html",
        ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_agent_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    agent_name: str,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        kind="agent",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        inner_name=agent_name,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_item_detail.html",
        ctx,
    )


@router.get(
    "/marketplace/flea/{entity_id}/skill/{skill_name}",
    response_class=HTMLResponse,
)
async def marketplace_flea_skill_detail(
    request: Request,
    entity_id: str,
    skill_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Inner skill detail page for a skill nested inside a flea plugin.

    Mirrors ``marketplace_curated_skill_detail`` but uses the standalone
    flea visibility gate (``_enforce_visibility``) — owner / admin see
    quarantined entities, everyone else gets 404 (entity existence not
    leaked).
    """
    from app.api.store import _enforce_visibility
    from app.auth.access import is_user_admin

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    _enforce_visibility(entity, user, conn)
    is_owner = entity.get("owner_user_id") == user.get("id")
    is_admin = is_user_admin(user["id"], conn)
    ctx = _build_context(
        request,
        user=user,
        source="flea",
        kind="skill",
        entity_id=entity_id,
        plugin_name=entity["name"],
        inner_name=skill_name,
        entity=entity,
        is_owner=is_owner,
        is_admin=is_admin,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_item_detail.html",
        ctx,
    )


@router.get(
    "/marketplace/flea/{entity_id}/agent/{agent_name}",
    response_class=HTMLResponse,
)
async def marketplace_flea_agent_detail(
    request: Request,
    entity_id: str,
    agent_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Inner agent detail page for an agent nested inside a flea plugin.

    Mirrors ``marketplace_flea_skill_detail``; kind="agent".
    """
    from app.api.store import _enforce_visibility
    from app.auth.access import is_user_admin

    entity = store_entities_repo().get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    _enforce_visibility(entity, user, conn)
    is_owner = entity.get("owner_user_id") == user.get("id")
    is_admin = is_user_admin(user["id"], conn)
    ctx = _build_context(
        request,
        user=user,
        source="flea",
        kind="agent",
        entity_id=entity_id,
        plugin_name=entity["name"],
        inner_name=agent_name,
        entity=entity,
        is_owner=is_owner,
        is_admin=is_admin,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_item_detail.html",
        ctx,
    )


@router.get("/marketplace/guide/curated", response_class=HTMLResponse)
async def marketplace_guide_curated(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        guide_title="Submit a skill or plugin to Curated Marketplace",
        guide_kind="curated",
    )
    return templates.TemplateResponse(request, "marketplace_guide.html", ctx)


@router.get("/marketplace/guide/flea", response_class=HTMLResponse)
async def marketplace_guide_flea(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        guide_title="Upload to Flea Market",
        guide_kind="flea",
    )
    return templates.TemplateResponse(request, "marketplace_guide.html", ctx)


@router.get("/marketplace/format-guide", response_class=HTMLResponse)
async def marketplace_format_guide(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Render docs/curated-marketplace-format.md as a logged-in HTML page.

    The Markdown source is the canonical reference for upstream curators —
    living it next to docs/ in the repo means it's also discoverable on the
    public GitHub mirror, so an external maintainer can read it without
    needing an Agnes account. The web rendering exists for the in-product
    flow (link from /admin/marketplaces) and uses Python's ``markdown``
    library with the standard extensions for fenced code + tables.

    Auth: ``Depends(get_current_user)`` only — no admin requirement. The
    audience is "anyone authoring or reviewing a curated marketplace,"
    which is broader than admins and could include non-admin curators.
    """
    # markdown-it-py is already a transitive dep (rich → markdown-it-py),
    # so no new pinning is needed. Commonmark preset + the table extension
    # gives us fenced code blocks (rendered as <pre><code class="language-X">)
    # and GFM-style tables — enough to render the format guide cleanly.
    from markdown_it import MarkdownIt
    from pathlib import Path

    md_path = Path(__file__).resolve().parent.parent.parent / "docs" / "curated-marketplace-format.md"
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError:
        md_text = "# Format guide unavailable\n\nThe source markdown file is missing from this deployment."
    rendered = MarkdownIt("commonmark", {"breaks": False}).enable("table").render(md_text)
    ctx = _build_context(
        request,
        user=user,
        rendered_html=rendered,
    )
    return templates.TemplateResponse(
        request,
        "marketplace_format_guide.html",
        ctx,
    )


@router.get("/documentation/api", response_class=HTMLResponse)
async def documentation_api(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Render docs/api-reference.md as a logged-in HTML page.

    Same pattern and rationale as /marketplace/format-guide above: the
    Markdown source lives in docs/ so it's readable on the GitHub mirror;
    the web rendering is the in-product entry point (Admin menu →
    Documentation). Auth is ``get_current_user`` only — the audience is
    "anyone scripting against the API", which is broader than admins.
    Freshness is enforced by tests/test_api_docs_coverage.py, which fails
    CI when a public /api/* route is missing from the document.
    """
    from markdown_it import MarkdownIt
    from pathlib import Path

    from app.version import APP_VERSION

    md_path = Path(__file__).resolve().parent.parent.parent / "docs" / "api-reference.md"
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError:
        md_text = "# API reference unavailable\n\nThe source markdown file is missing from this deployment."
    rendered = MarkdownIt("commonmark", {"breaks": False}).enable("table").render(md_text)
    ctx = _build_context(
        request,
        user=user,
        rendered_html=rendered,
        app_version=APP_VERSION,
    )
    return templates.TemplateResponse(
        request,
        "documentation_api.html",
        ctx,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_hub(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin dashboard — what needs the admin's attention, on landing.

    This page used to be a card grid of every /admin/* surface. Once the
    grouped sidebar shipped (`admin_nav.py`) the grid became a second copy of
    the navigation rendered beside the first, answering "where do I go" for a
    question the column already answered better. The sidebar is now the only
    admin navigation (the three API-doc links the grid carried moved into it
    as `ADMIN_NAV_DOCS`), and this page answers "what needs me?" instead.

    Only the "Needs you" zone — approval queues, all COUNT-shaped — resolves
    here. "Needs fixing" reads the unbounded audit/history tables and is
    fetched after first paint from /api/admin/dashboard/signals, so an
    instance with a large audit log doesn't pay for it on every render. Row
    inventory: `app/web/admin_signals.py`.
    """
    from app.services.admin_dashboard import resolve_journey, resolve_needs_you

    ctx = _build_context(
        request,
        user=user,
        needs_you=resolve_needs_you(),
        # Setup path + People/Data/Access gap cards — the "where am I, what's
        # next" layer in front of the two signal zones. Resolved inline: every
        # count is a cheap repo read (see resolve_journey's docstring).
        journey=resolve_journey(),
    )
    return templates.TemplateResponse(request, "admin_hub.html", ctx)


@router.get("/admin/data-packages", response_class=HTMLResponse)
async def admin_data_packages(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin audit view — every Data Package + Memory Domain, regardless of
    grant.

    The Catalog reshape (follow-up to auto-membership) removed
    ``browse_admin`` god-mode from the user-facing /catalog and
    /corporate-memory pages — both now show the same grant-scoped,
    addable-only view for every visitor. This page is where the old
    "see everything" audit affordance moved to: admins still need a full
    inventory (which packages/domains exist, how many tables they bundle,
    whether an empty package needs table assignment) independent of their
    own group grants.
    """
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    resolver = StackResolver(conn)
    pkg_repo = data_packages_repo()
    domains_repo = memory_domains_repo()

    # `list_tables` returns only (id, name) — the source type lives on the
    # registry row, so join it here rather than widening a repository method
    # (and its _pg sibling) for one caller, exactly as the detail handler
    # below does. Without the join the source-type tags this page renders were
    # silently always empty: every `t.get("source_type")` read a key the
    # junction query never selects.
    registry_types: dict[str, str] = {}
    try:
        registry_types = {t["id"]: (t.get("source_type") or "") for t in table_registry_repo().list_all()}
    except Exception as e:
        logger.warning("admin data-packages: could not read the table registry: %s", e)

    pkg_meta: dict[str, dict] = {}
    try:
        for pkg in pkg_repo.list():
            tables = pkg_repo.list_tables(pkg["id"])
            source_types = sorted({registry_types.get(t["id"], "") for t in tables} - {""})
            pkg_meta[pkg["id"]] = {
                "table_count": len(tables),
                "source_types": source_types,
            }
    except Exception as e:
        logger.warning("admin data-packages: could not enumerate data_packages: %s", e)

    def _adapt_pkg(e):
        meta = pkg_meta.get(e.id, {})
        return _data_package_entry_dict(
            e,
            # The package's OWN admin page, not the analyst-facing
            # /catalog/p/<slug>. This card is the admin's index of packages;
            # its drilldown used to leave the admin area entirely, so "what is
            # in Revenue, and who gets it" was answered by a read-only page
            # written for a different reader. `View as analyst` on the detail
            # page keeps the catalog view one click away, which is the right
            # depth for "how does this look to them".
            drilldown_url=f"/admin/data-packages/{e.id}",
            table_count=meta.get("table_count", 0),
            source_types=meta.get("source_types", []),
            is_admin_view=True,
        )

    pkg_entries = resolver.browse_admin(user["id"], ResourceType.DATA_PACKAGE)
    pkg_entries = sorted(pkg_entries, key=lambda e: e.name or "")
    package_cards = [_adapt_pkg(e) for e in pkg_entries]

    dom_meta: dict[str, dict] = {}
    try:
        for d in domains_repo.list(limit=10000):
            summaries = domains_repo.list_items_of_domain(d["id"], limit=10000)
            dom_meta[d["id"]] = {
                "items_count": len(summaries),
                "required_count": sum(1 for s in summaries if s.get("is_required")),
                "slug": d["slug"],
            }
    except Exception as e:
        logger.warning("admin data-packages: could not enumerate memory_domains: %s", e)

    def _adapt_domain(e):
        meta = dom_meta.get(e.id, {})
        slug = meta.get("slug")
        return _memory_domain_entry_dict(
            e,
            drilldown_url=f"/memory/d/{slug}" if slug else f"/corporate-memory#{e.id}",
            items_count=meta.get("items_count", 0),
            required_count=meta.get("required_count", 0),
        )

    domain_entries = resolver.browse_admin(user["id"], ResourceType.MEMORY_DOMAIN)
    domain_entries = sorted(domain_entries, key=lambda e: e.name or "")
    domain_cards = [_adapt_domain(e) for e in domain_entries]

    # ── Sharing state, per package — who can use it, at which tier. ──
    # The grant matrix on a group's detail page stays the canonical editor;
    # this page adds the OTHER direction ("who can use Revenue?"), which
    # previously had no answer anywhere in the product. Same rows, same API
    # (/api/admin/grants) — rendered from the package's side.
    #
    # Read-only here. The card states WHO can use each package and stops —
    # editing sharing from an index card meant a grant could be written from
    # a surface that shows none of its consequences (how many people that
    # actually reaches, whether any of them have pulled). The package's own
    # page owns the edit, next to the delivery read-out that answers for it.
    pkg_sharing: dict[str, list[dict]] = {}
    dom_sharing: dict[str, list[dict]] = {}
    try:
        for rt, sink in (("data_package", pkg_sharing), ("memory_domain", dom_sharing)):
            for g in resource_grants_repo().list_all(resource_type=rt):
                sink.setdefault(g["resource_id"], []).append(
                    {
                        "grant_id": g["id"],
                        "group_id": g["group_id"],
                        "group_name": g.get("group_name") or g["group_id"],
                        # 'available' | 'required' in the API; the page words them
                        # Optional / Automatic — what each one DOES.
                        "requirement": g.get("requirement") or "available",
                    }
                )
    except Exception as e:
        logger.warning("admin data-packages: could not enumerate sharing state: %s", e)

    # ── The unpackaged tray — distributable tables no analyst can pull. ──
    # Same fold as the /admin gap card: blank query_mode reads as local,
    # `remote` rows are excluded (they answer server-side without a package).
    unpackaged_tables: list[dict] = []
    try:
        packaged_ids: set[str] = set()
        for ids in pkg_repo.list_member_ids_bulk().values():
            packaged_ids.update(ids)
        for t in table_registry_repo().list_all():
            if (t.get("source_type") or "") == "internal":
                continue
            if (t.get("query_mode") or "") not in ("", "local", "materialized"):
                continue
            if t["id"] not in packaged_ids:
                unpackaged_tables.append({"id": t["id"], "name": t.get("name") or t["id"]})
        unpackaged_tables.sort(key=lambda t: t["name"])
    except Exception as e:
        logger.warning("admin data-packages: could not enumerate unpackaged tables: %s", e)

    ctx = _build_context(
        request,
        user=user,
        package_cards=package_cards,
        domain_cards=domain_cards,
        pkg_sharing=pkg_sharing,
        dom_sharing=dom_sharing,
        unpackaged_tables=unpackaged_tables,
    )
    return templates.TemplateResponse(request, "admin_data_packages.html", ctx)


# ── Plain language for `query_mode`, shared by every Data surface ──────────
#
# The label LEADS with what the mode does; the system word rides along as a
# mono chip so the CLI/API vocabulary (`agnes catalog`, `query_mode=remote`)
# stays learnable rather than being hidden behind a friendly synonym. One
# mapping, so the package detail page and the Tables lens can never word the
# same row differently. A blank `query_mode` reads as local — that is what the
# schema default and every consumer already assume (see the unpackaged-tray
# fold at `admin_data_packages` above).
_QUERY_MODE_WORDS: dict[str, tuple[str, str]] = {
    "": ("Synced copy", "local"),
    "local": ("Synced copy", "local"),
    "remote": ("Live query", "remote"),
    "materialized": ("Saved query", "materialized"),
    "server_only": ("Server only", "server_only"),
}


def _mode_words(query_mode: str | None) -> dict[str, str]:
    """``{'label', 'word'}`` for a table's ``query_mode``.

    An unknown mode is passed through verbatim on both keys rather than
    guessed at — a new mode should read as itself until someone words it,
    never as the wrong one.
    """
    key = (query_mode or "").strip()
    label, word = _QUERY_MODE_WORDS.get(key, (key or "—", key or "—"))
    return {"label": label, "word": word}


@router.get("/admin/data-packages/{package_id}", response_class=HTMLResponse)
async def admin_package_detail(
    request: Request,
    package_id: str,
    user: dict = Depends(require_admin),
):
    """ONE package, end to end — what is in it, who can use it, who has it.

    The package is the unit an analyst actually receives, and until this page
    existed it was the one object in the Data section with no home: its
    composition was edited inside the package-grouped layout on /admin/tables,
    its sharing on /admin/data-packages, and the card's own drilldown left the
    admin area entirely for the analyst-facing /catalog/p/<slug>. Three places,
    none of them about the package.

    Everything here rides EXISTING repositories and endpoints — the page adds
    no repo method, so there is no DuckDB↔Postgres parity sibling to write
    (CLAUDE.md). Composition writes through ``/api/admin/data-packages/{id}
    /tables``; sharing writes the same ``/api/admin/grants`` rows a group's
    Access tab does, so the two ends of one relationship can never disagree.

    The delivery read-out is the part the product could not draw anywhere:
    a grant is a PERMISSION, and the data only lands once `agnes pull` runs.
    ``users.last_pull_at`` (stamped by app/api/sync.py on every human pull) is
    what turns "shared with 14 people" into "11 of them actually have it".
    """
    from datetime import timedelta, timezone

    from src.repositories import (
        resource_grants_repo,
        sync_state_repo,
        table_registry_repo,
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    pkg_repo = data_packages_repo()
    pkg = pkg_repo.get(package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="data_package_not_found")

    now = datetime.now(timezone.utc)

    def _aware(ts):
        """Timestamps come back naive from DuckDB and aware from Postgres;
        comparing the two raises. Normalise to UTC at every read."""
        if ts is None:
            return None
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    # ── What is in it ────────────────────────────────────────────────────
    # `list_tables` returns only (id, name) — the registry row carries the
    # bucket / mode / source the page needs, so join rather than widening a
    # repository method (and its _pg sibling) for one caller.
    member_ids = [t["id"] for t in pkg_repo.list_tables(package_id)]
    try:
        registry = {t["id"]: t for t in table_registry_repo().list_all()}
    except Exception as e:  # noqa: BLE001 — an unreadable registry is an empty list, not a 500
        logger.warning("package detail: could not read the table registry: %s", e)
        registry = {}
    try:
        states = {s["table_id"]: s for s in sync_state_repo().get_all_states()}
    except Exception:  # noqa: BLE001
        states = {}

    tables: list[dict] = []
    newest_sync = None
    for tid in member_ids:
        row = registry.get(tid)
        if row is None:
            # Registered in the junction but gone from the registry. Show it
            # rather than dropping it silently — a member the admin cannot see
            # is a member they cannot remove.
            tables.append({"id": tid, "name": tid, "missing": True, "mode": _mode_words(None)})
            continue
        st = states.get(tid) or {}
        last = _aware(st.get("last_sync"))
        if last is not None and (newest_sync is None or last > newest_sync):
            newest_sync = last
        tables.append(
            {
                "id": tid,
                "name": row.get("name") or tid,
                "bucket": row.get("bucket") or "",
                "source_type": row.get("source_type") or "",
                "mode": _mode_words(row.get("query_mode")),
                "last_sync": last.isoformat() if last else None,
                "age_minutes": int((now - last).total_seconds() // 60) if last else None,
                "status": st.get("status") or "",
                "missing": False,
            }
        )
    tables.sort(key=lambda t: t["name"])
    source_types = sorted({t["source_type"] for t in tables if t.get("source_type")})

    # Everything this package could still take, for the Add-tables picker.
    # Server-rendered rather than a second fetch: the registry is already read
    # above, and a picker that cannot open because one more request failed is
    # a worse failure than a page that is 30 kB heavier. `internal` rows
    # (agnes_* bookkeeping tables) are never package material.
    #
    # Each row carries what the picker's toolbar filters and sorts ON, because
    # an instance with three hundred registered tables cannot be worked with a
    # substring match alone: the source and the mode are what an admin slices
    # by ("the Keboola tables", "the live-query ones"), `packaged` is the one
    # that answers "what has nobody bundled yet", and `last_sync` is an ISO
    # string so it sorts lexicographically without a parse (never-synced
    # sorts first, which is the order that surfaces the problems).
    member_set = set(member_ids)
    packaged_elsewhere: set[str] = set()
    try:
        for pid, ids in pkg_repo.list_member_ids_bulk().items():
            if pid != package_id:
                packaged_elsewhere.update(ids)
    except Exception as e:  # noqa: BLE001 — the picker still works without the flag
        logger.warning("package detail: could not read package membership: %s", e)

    candidate_tables = []
    for t in sorted(registry.values(), key=lambda r: (r.get("name") or r["id"]).lower()):
        if t["id"] in member_set or (t.get("source_type") or "") == "internal":
            continue
        st = states.get(t["id"]) or {}
        last = _aware(st.get("last_sync"))
        candidate_tables.append(
            {
                "id": t["id"],
                "name": t.get("name") or t["id"],
                "bucket": t.get("bucket") or "",
                "source_type": t.get("source_type") or "",
                "mode": _mode_words(t.get("query_mode")),
                "rows": int(st.get("rows") or 0),
                "last_sync": last.isoformat() if last else "",
                "age_minutes": int((now - last).total_seconds() // 60) if last else None,
                "packaged": t["id"] in packaged_elsewhere,
            }
        )

    # The picker's facet vocabularies, each with the count the option covers —
    # built from the candidates themselves so a filter can never offer a value
    # that matches nothing.
    def _facet(key: str) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for c in candidate_tables:
            v = c["mode"]["word"] if key == "mode" else (c.get(key) or "")
            if v:
                counts[v] = counts.get(v, 0) + 1
        return sorted(counts.items())

    candidate_facets = {
        "source": _facet("source_type"),
        "mode": _facet("mode"),
        "bucket": _facet("bucket"),
    }

    # ── Who can use it ───────────────────────────────────────────────────
    groups_by_id: dict[str, dict] = {}
    all_groups: list[dict] = []
    sharing: list[dict] = []
    try:
        members_repo = user_group_members_repo()
        for grp in user_groups_repo().list_all():
            is_everyone = bool(grp.get("is_system")) and grp["name"] == "Everyone"
            entry = {
                "id": grp["id"],
                "name": grp["name"],
                "is_system": bool(grp.get("is_system")),
                "is_everyone": is_everyone,
                "member_count": members_repo.count_members(grp["id"]),
            }
            groups_by_id[grp["id"]] = entry
            all_groups.append(entry)
        for g in resource_grants_repo().list_all(resource_type="data_package"):
            if g["resource_id"] != package_id:
                continue
            grp = groups_by_id.get(g["group_id"], {})
            sharing.append(
                {
                    "grant_id": g["id"],
                    "group_id": g["group_id"],
                    "group_name": g.get("group_name") or grp.get("name") or g["group_id"],
                    # 'available' | 'required' on the wire; Optional | Automatic
                    # on the page — what each one DOES to a workspace.
                    "requirement": g.get("requirement") or "available",
                    "member_count": grp.get("member_count", 0),
                    "is_everyone": grp.get("is_everyone", False),
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("package detail: could not read sharing state: %s", e)
    sharing.sort(key=lambda s: (not s["is_everyone"], s["group_name"]))

    # ── Who actually has it ──────────────────────────────────────────────
    # Automatic grants are the ones that land WITHOUT the analyst doing
    # anything, so they are the population whose delivery we can assert.
    # Optional grants only make the package offerable — counting those as
    # "reached" would overstate it, which is the exact confusion this panel
    # exists to end.
    delivery = {"auto_people": 0, "pulled": 0, "stale": 0, "optional_only": 0, "cutoff_days": 7}
    try:
        users_by_id = {u["id"]: u for u in users_repo().list_all() if u.get("active", True)}
        members_repo = user_group_members_repo()

        def _people(group_ids: set[str]) -> set[str]:
            """User ids reached by a set of groups. Everyone is auto-membership
            — its junction rows may not exist — so it resolves to the whole
            active roster rather than to an empty member list."""
            reached: set[str] = set()
            for gid in group_ids:
                if groups_by_id.get(gid, {}).get("is_everyone"):
                    reached.update(users_by_id.keys())
                    continue
                try:
                    reached.update(m["id"] for m in members_repo.list_members_for_group(gid))
                except Exception:  # noqa: BLE001
                    pass
            return reached & users_by_id.keys()

        auto = _people({s["group_id"] for s in sharing if s["requirement"] == "required"})
        optional = _people({s["group_id"] for s in sharing if s["requirement"] != "required"})
        cutoff = now - timedelta(days=delivery["cutoff_days"])
        pulled = sum(
            1 for uid in auto if (_aware(users_by_id[uid].get("last_pull_at")) or now - timedelta(days=3650)) >= cutoff
        )
        delivery = {
            "auto_people": len(auto),
            "pulled": pulled,
            "stale": len(auto) - pulled,
            "optional_only": len(optional - auto),
            "cutoff_days": 7,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("package detail: could not compute delivery state: %s", e)

    # ── Arrival context (?from=simulate&user=) ───────────────────────────
    # The Simulate lens's "Share it →" lands here carrying WHO the admin came
    # to fix. Resolved server-side to a name + their groups so the banner can
    # say "Jane — Everyone, product-team" instead of echoing a uuid, and the
    # back link returns to the preview with the same person still selected.
    # Unknown/garbage ids resolve to None and the page renders normally.
    preview_ctx = None
    if request.query_params.get("from") == "simulate":
        _puid = request.query_params.get("user") or ""
        if _puid:
            try:
                _pu = users_repo().get_by_id(_puid)
            except Exception:  # noqa: BLE001 — the banner is chrome, never a 500
                _pu = None
            if _pu:
                try:
                    _pgroups = list(user_group_members_repo().list_group_names_for_user(_puid))
                except Exception:  # noqa: BLE001
                    _pgroups = []
                preview_ctx = {
                    "user_id": _puid,
                    "name": _pu.get("name") or _pu.get("email") or _puid,
                    "groups": [g for g in _pgroups if g],
                    "back_href": f"/admin/access?lens=simulate&user={_puid}",
                }

    ctx = _build_context(
        request,
        user=user,
        pkg=pkg,
        tables=tables,
        candidate_tables=candidate_tables,
        candidate_facets=candidate_facets,
        source_types=source_types,
        sharing=sharing,
        all_groups=all_groups,
        delivery=delivery,
        preview_ctx=preview_ctx,
        newest_sync=newest_sync.isoformat() if newest_sync else None,
        newest_sync_age_minutes=(int((now - newest_sync).total_seconds() // 60) if newest_sync else None),
    )
    return templates.TemplateResponse(request, "admin_package_detail.html", ctx)


def _table_delivery() -> dict[str, dict]:
    """Per table: the package(s) carrying it and how many people that reaches.

    The last two columns of the Tables lens, and the reason the lens is worth
    having. A table in no package reaches NOBODY — that is the product's
    central distribution rule, and before this it was stated only as a
    sentence in a page subtitle while every row stayed silent about whether
    it obeyed. Here each row carries its own answer.

    Mirrors the ``feeds`` cell of `_source_pipelines` deliberately: same
    packages → grants → groups → members walk, so a source card and a table
    row can never disagree about who is reached. ``people = -1`` is the same
    "Everyone" sentinel, worded by the template.

    Also carries ``last_sync`` (epoch ms), which the lens sorts by. That is
    here rather than read off the registry response for a real reason:
    ``/api/admin/registry`` joins sync_state on the table's NAME
    (app/api/admin.py, "table_id == name by convention") while the orchestrator
    and `_source_pipelines` key it by table ID — so on instances where those
    differ the registry's `last_sync` is null for every row, and a sort built
    on it would silently order nothing. This map uses the ID, like every other
    consumer.

    Returns ``{table_id: {"packages": [{id, name}], "people": int,
    "last_sync": int|None}}``; a table absent from the map is in no package
    and has never synced.
    """
    from src.repositories import (
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )

    out: dict[str, dict] = {}
    try:
        pkg_repo = data_packages_repo()
        packages = {p["id"]: p for p in pkg_repo.list()}
        members_by_pkg = pkg_repo.list_member_ids_bulk()
    except Exception as e:  # noqa: BLE001
        logger.warning("table delivery: could not enumerate packages: %s", e)
        return out

    try:
        pkg_grants: dict[str, list] = {}
        for g in resource_grants_repo().list_all(resource_type="data_package"):
            pkg_grants.setdefault(g["resource_id"], []).append(g)
        groups = {g["id"]: g for g in user_groups_repo().list_all()}
        members_repo = user_group_members_repo()
    except Exception as e:  # noqa: BLE001
        logger.warning("table delivery: could not enumerate grants: %s", e)
        pkg_grants, groups, members_repo = {}, {}, None

    everyone_ids = {g["id"] for g in groups.values() if g.get("is_system") and g.get("name") == "Everyone"}

    # People reached BY A PACKAGE, memoised — a table in three packages must
    # not pay for three identical membership walks, and instances routinely
    # have far more table→package rows than packages.
    reach_cache: dict[str, int] = {}

    def _reach(pkg_id: str) -> int:
        if pkg_id in reach_cache:
            return reach_cache[pkg_id]
        group_ids = {g["group_id"] for g in pkg_grants.get(pkg_id, [])}
        if group_ids & everyone_ids:
            reach_cache[pkg_id] = -1
        elif members_repo is None:
            reach_cache[pkg_id] = 0
        else:
            reached: set[str] = set()
            for gid in group_ids:
                try:
                    reached.update(m["id"] for m in members_repo.list_members_for_group(gid))
                except Exception:  # noqa: BLE001
                    pass
            reach_cache[pkg_id] = len(reached)
        return reach_cache[pkg_id]

    for pkg_id, table_ids in members_by_pkg.items():
        pkg = packages.get(pkg_id)
        if pkg is None:
            continue
        people = _reach(pkg_id)
        for tid in table_ids:
            row = out.setdefault(tid, {"packages": [], "people": 0, "last_sync": None})
            row["packages"].append({"id": pkg_id, "name": pkg.get("name") or pkg_id})
            # A table in two packages reaches the UNION, which we cannot take
            # from two counts. Everyone wins outright; otherwise the larger
            # count is the honest floor, and it is labelled "at least" when
            # more than one package carries the row.
            if row["people"] == -1 or people == -1:
                row["people"] = -1
            else:
                row["people"] = max(row["people"], people)

    # Freshness for EVERY table, packaged or not — a row's sync state is not a
    # function of whether anyone receives it.
    try:
        for s in sync_state_repo().get_all_states():
            ts = s.get("last_sync")
            if ts is None:
                continue
            row = out.setdefault(s["table_id"], {"packages": [], "people": 0, "last_sync": None})
            row["last_sync"] = int(ts.timestamp() * 1000)
    except Exception as e:  # noqa: BLE001 — no freshness is a missing sort key, not a 500
        logger.warning("table delivery: could not read sync state: %s", e)
    return out


def _source_connection_names() -> dict[str, str]:
    """``{connection_id: display name}`` for every registered source project.

    The Tables lens hydrates from ``/api/admin/registry``, which carries a
    row's ``connection_id`` but not the project's NAME — so without this the
    Source cell could say "keboola" but never *which* Keboola project, and
    there would be nothing to build a Project filter out of. One map beats
    widening the registry response for one consumer.
    """
    from src.repositories import source_connections_repo

    try:
        return {c["id"]: (c.get("name") or c["id"]) for c in source_connections_repo().list()}
    except Exception as e:  # noqa: BLE001 — an unreadable list means no project labels, not a 500
        logger.warning("tables lens: could not list source connections: %s", e)
        return {}


def _connected_source_types() -> list[str]:
    """Every source type with at least one registered connection.

    The Tables lens decided "Keboola is not connected" from the legacy
    ``data_source.type`` scalar alone, which says nothing about the
    multi-connection registry. An instance whose default type is something else
    but which has Keboola projects connected was told — on the tab next to the
    one listing those projects — that Keboola is not connected and its tables
    cannot sync, sending the admin off to re-enter credentials it already had.
    Connectedness is a property of the registry, so it is read from there.
    """
    from src.repositories import source_connections_repo

    try:
        return sorted(
            {
                (c.get("source_type") or "").strip().lower()
                for c in source_connections_repo().list()
                if (c.get("source_type") or "").strip()
            }
        )
    except Exception as e:  # noqa: BLE001 — unreadable means "cannot claim connected", not a 500
        logger.warning("tables lens: could not list source connection types: %s", e)
        return []


def _connected_sources() -> list[str]:
    """Every source type this instance can prove is reachable — sorted,
    lowercased source-type names.

    "Is source type X connected?" has two stores and neither is correct
    alone. `_connected_source_types()` reads the `source_connections`
    registry, which is right for a project added through the multi-connection
    wizard (mainly Keboola) but says nothing about Snowflake or Databricks —
    those are credentialed once at the INSTANCE level and no registry row is
    ever seeded for them (`app/connections_seed.py` seeds keboola + bigquery
    only). The legacy `data_source.type` scalar (`get_data_source_type()`) is
    right for whatever an instance was first configured with, but `"local"`
    — and its CLI-facing alias `"csv"` — is that scalar's UNSET SENTINEL, not
    an assertion that local files are connected; treating it as a real value
    is exactly the bug class this closes (the next contributor's default
    assumption otherwise), so neither is ever added here.

    The union, in order:
    - every `source_type` already in the registry (`_connected_source_types`);
    - the legacy scalar, but only when it names a real source (not
      `local`/`csv`);
    - each of BigQuery/Snowflake/Databricks, proven credentialed at the
      instance level by this file's own `_bigquery_credentialed()` /
      `_snowflake_credentialed()` / `_databricks_credentialed()` — which the
      registry and the scalar both miss for a connector set up without ever
      registering a project.

    Every read degrades independently to "cannot claim connected" rather
    than a 500 — an unreadable registry, an unreadable legacy scalar, or a
    throwing credential probe must never break a page that is only asking
    this question.

    KNOWN OMISSION — Keboola is the one connector with an instance-level
    credential probe that this union does NOT call. `_keboola_credentialed()`
    exists in this module and the `/admin/data-sources` cards already use it,
    so an instance whose Keboola lives only in `data_source.keboola.*` +
    `KEBOOLA_STORAGE_TOKEN` — no registry row, and a legacy scalar naming some
    other source — still reads "Keboola is not connected" on `/admin/tables`.
    Adding the probe here is deliberately NOT done, because this one list
    drives two different decisions and only one of them would become correct:

    TODO: split the signal before adding it. The banner copy asks "should we
    tell the admin Keboola is unreachable?" (answer: no, it is credentialed),
    while the Discover / List-tables / Use-as-base buttons the same flag
    enables ask "is there a usable discover route for it?" — and
    `GET /api/admin/discover-tables` routes on the legacy
    `get_data_source_type()` scalar alone, so on such an instance it answers
    `{"tables": [], "error": "Discovery not implemented for source_type=..."}`.
    Probing Keboola here without a separate "usable discover route" flag would
    only trade a wrong banner for three buttons that silently return nothing.
    """
    types = set(_connected_source_types())

    try:
        from app.instance_config import get_data_source_type

        primary = (get_data_source_type() or "").strip().lower()
    except Exception as e:  # noqa: BLE001 — unreadable means "cannot claim connected", not a 500
        logger.warning("connected sources: could not read legacy data_source.type: %s", e)
        primary = ""
    if primary and primary not in ("local", "csv"):
        types.add(primary)

    for stype, probe in (
        ("bigquery", _bigquery_credentialed),
        ("snowflake", _snowflake_credentialed),
        ("databricks", _databricks_credentialed),
    ):
        try:
            if probe():
                types.add(stype)
        except Exception as e:  # noqa: BLE001 — a throwing probe is "not proven", not a 500
            logger.warning("connected sources: %s credential probe failed: %s", stype, e)

    return sorted(types)


@router.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from app.instance_config import get_data_source_type

    # Branch the register-modal layout server-side so the JS doesn't have
    # to round-trip /api/admin/server-config to learn the source type.
    data_source_type = get_data_source_type() or "keboola"
    ctx = _build_context(
        request,
        user=user,
        data_source_type=data_source_type,
        # The end of each table's chain — which package carries it and how
        # many people that reaches. The page hydrates its rows client-side
        # from /api/admin/registry, but reach is a grants × group-membership
        # join no browser-side endpoint exposes, so it is resolved here and
        # handed over as one map.
        table_delivery=_table_delivery(),
        # {connection_id: name} — turns a row's `connection_id` into the
        # project it came from ("Keboola Test"), in the Source cell and in the
        # Project filter. The registry response carries the id but not the
        # name, and one lookup map is cheaper than widening that endpoint.
        source_connections=_source_connection_names(),
        # Which source types have a `source_connections` registry row —
        # SUPERSEDED by `connected_sources` below (registry-only, misses
        # instance-level-only Snowflake/Databricks/legacy-scalar). Kept
        # only until every template consumer migrates off this key.
        connected_source_types=_connected_source_types(),
        # Every source type this instance can prove is reachable — registry
        # rows + the legacy scalar (when not the `local`/`csv` unset
        # sentinel) + instance-level credential probes. See
        # `_connected_sources()`.
        connected_sources=_connected_sources(),
    )
    return templates.TemplateResponse(request, "admin_tables.html", ctx)


@router.get("/admin/sync", response_class=HTMLResponse)
async def admin_sync_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Sync status dashboard — per-table extraction state + manual trigger."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_sync.html", ctx)


@router.get("/admin/server-config", response_class=HTMLResponse)
async def admin_server_config_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Server configuration editor — instance.yaml fields grouped by section.

    Shell-only page. The form is populated client-side from
    GET /api/admin/server-config (which redacts secrets) and submitted
    section-by-section to POST /api/admin/server-config. Auth/server
    sections require an explicit confirmation dialog before save (see
    ``_DANGER_SECTIONS`` in the API). Saves trigger the "restart required"
    banner — hot-reload is out of scope for #91.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_server_config.html", ctx)


@router.get("/admin/datasource-credentials", response_class=HTMLResponse)
async def admin_datasource_credentials_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Instance-level secrets (Google Workspace, BigQuery) via the server
    vault. Keboola project connect/browse/rotate lives on
    /admin/data-sources — this page carries a callout linking there.

    Passes ``vault_key_configured`` so the template can render a blocking
    banner when ``AGNES_VAULT_KEY`` is absent. Secret values are never read
    here — the JS loads presence/source status from
    ``GET /api/admin/datasource-secrets`` and writes via PUT/DELETE.
    """
    from app.secrets_vault import vault_key_configured

    ctx = _build_context(request, user=user)
    ctx["vault_key_configured"] = vault_key_configured()
    return templates.TemplateResponse(request, "admin_datasource_credentials.html", ctx)


@router.get("/admin/data-sources", response_class=HTMLResponse)
async def admin_data_sources_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """ "Add data source" wizard (#755): paste a Keboola project's connection
    URL + storage token, validate, then browse buckets/tables and register
    the ones you want — no SSH, no config-file edits. Also the home for
    per-project "Set as default" and "Rotate token" controls — the single
    place to manage a Keboola connection end to end.

    Distinct from /admin/mcp-sources (MCP tool servers Agnes calls at
    runtime) and from /admin/datasource-credentials (GWS/BQ instance
    secrets) — this page owns Keboola project connect/browse/register/
    default/rotate. Passes ``vault_key_configured`` so the template can
    render the same blocking banner as /admin/datasource-credentials when
    ``AGNES_VAULT_KEY`` is absent (the wizard can't store a secret without
    it).
    """
    from app.secrets_vault import vault_key_configured

    ctx = _build_context(request, user=user)
    ctx["vault_key_configured"] = vault_key_configured()

    # No semantic-layer status here: Semantic is its own tab, and each source
    # card carries its own semantic step (see `_source_pipelines`), so the
    # page-wide status strip this context fed was a third copy of the same
    # fact. Status lives at /admin/semantic-layer.
    # Per-connection pipeline strip: connected → synced → semantic → feeding
    # whom, in one glance on each source card. See `_source_pipelines`.
    inventory = _source_inventory()
    ctx["source_pipelines"] = inventory["pipelines"]
    # Connectors that are live on this instance but keep no connection row —
    # BigQuery, Jira, uploaded files. Rendered as cards in the SAME list, so
    # "Sources" is every source. See `_source_inventory`.
    ctx["derived_sources"] = inventory["derived"]
    return templates.TemplateResponse(request, "admin_data_sources.html", ctx)


def _gib(n: int) -> str:
    """A byte cap as the operator wrote it in `instance.yaml` — "5 GiB"."""
    if not n:
        return "off"
    gib = n / (1024**3)
    return f"{gib:.0f} GiB" if gib >= 1 and abs(gib - round(gib)) < 0.05 else f"{gib:.1f} GiB"


# Connectors that produce tables WITHOUT keeping a `source_connections` row:
# BigQuery / Snowflake / Databricks are credentialed once at instance level,
# Jira arrives over webhooks, and `local` is whatever an admin dropped in the
# extracts directory. They were invisible on this page — the list asked the
# API for `?source_type=keboola` and the heading said "Keboola projects" —
# while the Add-data drawer happily offered all four connectors. That is the
# inconsistency this table closes: a connector that can be ADDED here must be
# VISIBLE here, so each of these gets a card built from the tables it
# actually registered, and its card names where its credentials really live
# instead of pretending they live on the card.
#
# Keboola also earns an entry — NOT because it never keeps a connection row
# (the multi-connection wizard normally seeds one), but because an instance
# configured the OLD way (first-time-setup / server-config, before that
# wizard existed) can be fully credentialed at instance level with no
# registry row at all. `_source_inventory()` only ever builds a Keboola card
# from this entry when no real `source_connections` row of that type exists,
# so a real connection's card is never duplicated.
_DERIVED_SOURCES: dict[str, dict] = {
    "keboola": {
        "name": "Keboola",
        "subtitle": "Instance-level connection · token from env or vault",
        "settings_href": "/admin/server-config",
        "settings_label": "Server config",
    },
    "bigquery": {
        "name": "BigQuery",
        "subtitle": "Live queries · service account in instance secrets",
        "settings_href": "/admin/datasource-credentials",
        "settings_label": "Instance secrets",
    },
    "snowflake": {
        "name": "Snowflake",
        "subtitle": "Live or materialized · SQL warehouse",
        "settings_href": "/admin/server-config",
        "settings_label": "Server config",
    },
    "databricks": {
        "name": "Databricks",
        "subtitle": "Live queries & materialized · Unity Catalog",
        "settings_href": "/admin/server-config",
        "settings_label": "Server config",
    },
    "jira": {
        "name": "Jira",
        "subtitle": "Webhook-driven · incremental",
        "settings_href": "/admin/sync",
        "settings_label": "Sync status",
    },
    "local": {
        "name": "Uploaded files",
        "subtitle": "CSV / Parquet in the extracts directory",
        "settings_href": "/admin/tables",
        "settings_label": "Tables",
    },
}


def _source_pipelines() -> dict:
    """The pipeline strip for every source card, keyed by connection id.

    Thin wrapper over `_source_inventory()` — kept because the strip is the
    part other callers and the guards name.
    """
    return _source_inventory()["pipelines"]


def _source_inventory() -> dict:
    """Every source on this instance: its pipeline strip, and the cards that
    have no connection row of their own.

    ``{"pipelines": {source_id: cells}, "derived": [row, …]}``.

    **Pipelines.** Four cells per source — tables, sync, semantic layer, who
    it feeds — computed server-side because each one is a fold over a
    different table and the client would otherwise need four more round-trips
    per card. The strip is what makes a source card answer "is this project
    healthy AND is anyone getting its data", which previously took four pages
    (Data sources, Tables, Sync, Semantic layer) to assemble by hand.

    Per-connector by construction rather than a fixed four: the semantic cell
    is Keboola-only (the Metastore is a Keboola API) and the cost cell is
    BigQuery-only, so each source carries only the keys that are true of it
    and the template renders the cells present.

    **Derived rows.** A connector with registered tables but no
    `source_connections` row (see `_DERIVED_SOURCES`) is synthesized into a
    row of the same shape, with its tables attributed to it, so ONE renderer
    draws every card and "Sources" means every source. They are marked
    ``derived: True``; the page offers them the actions that are real for
    them (add tables, open their tables) and links out for the rest instead
    of showing controls that would do nothing.

    Every cell degrades independently — a raising repo yields no key rather
    than a 500 — for the same reason the /admin dashboard isolates its
    resolvers: this page is where an admin lands when a source is already
    broken.
    """
    from datetime import timezone

    from src.repositories import (
        data_packages_repo,
        resource_grants_repo,
        source_connections_repo,
        sync_state_repo,
        table_registry_repo,
        user_group_members_repo,
        user_groups_repo,
    )

    out: dict[str, dict] = {}
    derived: list[dict] = []
    try:
        connections = source_connections_repo().list()
    except Exception as e:
        logger.warning("data-sources pipelines: could not list connections: %s", e)
        return {"pipelines": out, "derived": derived}

    try:
        tables = table_registry_repo().list_all()
    except Exception as e:
        logger.warning("data-sources pipelines: could not list tables: %s", e)
        tables = []

    # ── tables per connection. `connection_id` is the precise link, but rows
    # registered before it existed (or through the CLI) carry only
    # `source_type`. Those are UNATTRIBUTABLE when a type has several
    # connections, and reporting them as "no tables yet" was the misleading
    # answer — an instance with eleven registered tables read as empty on
    # every card. They are counted separately and named as unlinked instead;
    # with exactly one connection of that type the attribution is safe, so
    # they simply belong to it.
    by_conn: dict[str, list] = {}
    unlinked_by_type: dict[str, list] = {}
    conns_per_type: dict[str, int] = {}
    for c in connections:
        st = c.get("source_type") or ""
        conns_per_type[st] = conns_per_type.get(st, 0) + 1
    for t in tables:
        st = t.get("source_type") or ""
        if st == "internal":
            continue
        if t.get("connection_id"):
            by_conn.setdefault(t["connection_id"], []).append(t)
        else:
            unlinked_by_type.setdefault(st, []).append(t)

    # ── The connectors that keep no connection row. A type with tables and no
    # connection of its own is not "unlinked" — it is a source whose identity
    # simply lives elsewhere, so it becomes a card and its tables are ITS
    # tables. BigQuery earns a card from a configured service account alone,
    # because an admin who set the credential and registered nothing yet is
    # exactly the one who needs somewhere to press "Add tables".
    for stype, meta in _DERIVED_SOURCES.items():
        if conns_per_type.get(stype):
            continue  # a real connection of this type owns the card
        own_tables = unlinked_by_type.pop(stype, [])
        # Computed once per iteration (and only actually calls the probe when
        # `stype == "keboola"`, via short-circuit) so the keboola branch below
        # can reuse the same boolean instead of calling `_keboola_credentialed()`
        # a second time to decide whether the "Import" button gets to render.
        keboola_credentialed = stype == "keboola" and _keboola_credentialed()
        if not own_tables and not (
            keboola_credentialed
            or (stype == "bigquery" and _bigquery_credentialed())
            or (stype == "snowflake" and _snowflake_credentialed())
            or (stype == "databricks" and _databricks_credentialed())
        ):
            continue
        did = f"derived:{stype}"
        by_conn[did] = own_tables
        row = {"id": did, "source_type": stype, "derived": True, **meta}
        if stype == "keboola":
            # The "Import as managed connection" button on this card needs
            # these two values to POST straight to
            # `/api/admin/source-connections` without a second round trip —
            # see `_keboola_instance_config()`. `credentialed` gates the
            # button itself: a stack_url with no working token anywhere has
            # nothing to import, and rendering the button in that state
            # created a connection with no credential and a badge that
            # claimed otherwise.
            row["stack_url"], row["token_env"] = _keboola_instance_config()
            row["credentialed"] = keboola_credentialed
            # `create_connection` rejects an unallowlisted `token_env` before
            # anything else (`_reject_disallowed_token_env`) — a card can be
            # credentialed via the generic env var or the instance vault
            # while `data_source.keboola.token_env` is still some custom,
            # unallowlisted name, in which case Import would 400 even though
            # the card looks ready. Gate the button on this too rather than
            # advertise a one-click path that dead-ends for that config.
            from src.orchestrator_security import is_token_env_allowed

            row["token_env_allowlisted"] = is_token_env_allowed(row["token_env"])
        derived.append(row)

    try:
        states = {s["table_id"]: s for s in sync_state_repo().get_all_states()}
    except Exception:
        states = {}
    try:
        pkg_members = data_packages_repo().list_member_ids_bulk()
        packages = {p["id"]: p for p in data_packages_repo().list()}
    except Exception:
        pkg_members, packages = {}, {}
    try:
        pkg_grants: dict[str, list] = {}
        for g in resource_grants_repo().list_all(resource_type="data_package"):
            pkg_grants.setdefault(g["resource_id"], []).append(g)
    except Exception:
        pkg_grants = {}
    try:
        groups = {g["id"]: g for g in user_groups_repo().list_all()}
        members_repo = user_group_members_repo()
    except Exception:
        groups, members_repo = {}, None

    # Semantic-layer counts, per `source_ref` (== connection id).
    sem_metrics: dict[str, int] = {}
    sem_terms: dict[str, int] = {}
    try:
        from src.repositories import glossary_repo, metric_repo

        for m in metric_repo().list():
            if m.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and m.get("source_ref"):
                sem_metrics[m["source_ref"]] = sem_metrics.get(m["source_ref"], 0) + 1
        for t in glossary_repo().list(limit=100000):
            if t.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and t.get("source_ref"):
                sem_terms[t["source_ref"]] = sem_terms.get(t["source_ref"], 0) + 1
    except Exception as e:
        logger.warning("data-sources pipelines: semantic counts unavailable: %s", e)

    def _has_master(conn_id: str) -> bool:
        try:
            from app.api.admin_source_connections import master_secret_key
            from src.repositories import connection_secrets_repo

            return bool(connection_secrets_repo().has(master_secret_key(conn_id)))
        except Exception:
            return False

    now = datetime.now(timezone.utc)
    for conn in [*connections, *derived]:
        cid = conn["id"]
        cells: dict[str, dict] = {}

        # ── Tables
        stype = conn.get("source_type") or ""
        own = list(by_conn.get(cid, []))
        unlinked = unlinked_by_type.get(stype, [])
        basis = "connection"
        if conns_per_type.get(stype, 0) == 1 and unlinked:
            # Only one connection of this type — the pre-tracking rows can
            # only be its own, so attribute rather than report them as
            # orphans the admin cannot place.
            own += unlinked
            basis = "source_type"
            unlinked = []
        cells["tables"] = {"count": len(own), "basis": basis, "unlinked": len(unlinked)}

        # ── Sync: the freshest run across this source's tables, and how many
        # are currently in error. `internal`/remote rows have no sync state,
        # so a source with none reads "not synced" rather than an error.
        errors = 0
        latest = None
        for t in own:
            st = states.get(t["id"]) or {}
            if (st.get("status") or "") not in ("", "ok", "skipped"):
                errors += 1
            ts = st.get("last_sync")
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if latest is None or ts > latest:
                    latest = ts
        cells["sync"] = {
            "errors": errors,
            "last_sync": latest.isoformat() if latest else None,
            "age_minutes": int((now - latest).total_seconds() // 60) if latest else None,
        }

        # ── Semantic layer (Keboola only — the Metastore is a Keboola API).
        if stype == "keboola":
            cells["semantic"] = {
                "token": _has_master(cid),
                "metrics": sem_metrics.get(cid, 0),
                "terms": sem_terms.get(cid, 0),
            }

        # ── Cost guard (BigQuery / Snowflake). A remote source never syncs,
        # so the cell in the sync slot has to be the thing that CAN go wrong
        # with it: what a query is allowed to scan before the server refuses
        # it. Both caps are read from live config, so an operator who raised one
        # sees their number here rather than the documented default.
        if stype == "bigquery":
            cells["cost"] = {
                "scan": _gib(_bq_cap("bq_max_scan_bytes", 5_368_709_120)),
                "materialize": _gib(_bq_cap("max_bytes_per_materialize", 10_737_418_240)),
            }
        if stype == "snowflake":
            has_materialized = any(t.get("query_mode") == "materialized" for t in own)
            materialize_cap = _gib(_sf_cap("max_bytes_per_materialize", 10_737_418_240))
            if has_materialized:
                # Materialized rows sync, so show the sync cell and explain the
                # cost guard in its title instead of replacing it.
                cells["sync"]["title"] = (
                    f"Live queries run on Snowflake directly (no local scan cap). "
                    f"Materialized rows are refused above {materialize_cap}. Editable in server config."
                )
            else:
                cells["cost"] = {
                    "scan": "remote",
                    "materialize": materialize_cap,
                    "title": "Live queries run on Snowflake directly (no local scan cap). Materialized rows are refused above the materialize cap. Editable in server config.",
                }

        if stype == "databricks":
            has_materialized = any(t.get("query_mode") == "materialized" for t in own)
            remote_cap = _gib(_db_cap("max_bytes_per_remote_query", 1_073_741_824))
            materialize_cap = _gib(_db_cap("max_bytes_per_materialize", 10_737_418_240))
            if has_materialized:
                cells["sync"]["title"] = (
                    f"Databricks queries run on the SQL warehouse. "
                    f"Remote results are capped at {remote_cap}; materialized rows are refused above {materialize_cap}. Editable in server config."
                )
            else:
                cells["cost"] = {
                    "scan": remote_cap,
                    "materialize": materialize_cap,
                    "title": "Databricks queries run on the SQL warehouse. Remote results are capped at the scan limit and materialized rows are refused above the materialize cap. Editable in server config.",
                }

        # ── Feeds: packages holding this source's tables → groups granted →
        # people reached. The end of the chain the redesign cares about; a
        # source with tables in no package reads "0 packages", which is the
        # honest answer to "who is getting this data".
        own_ids = {t["id"] for t in own}
        feeding = [pid for pid, tids in pkg_members.items() if own_ids.intersection(tids) and pid in packages]
        granted_group_ids = {g["group_id"] for pid in feeding for g in pkg_grants.get(pid, [])}
        everyone_ids = {g["id"] for g in groups.values() if g.get("is_system") and g.get("name") == "Everyone"}
        people = 0
        if granted_group_ids & everyone_ids:
            people = -1  # sentinel: everyone (the template words it)
        elif members_repo is not None:
            reached: set[str] = set()
            for gid in granted_group_ids:
                try:
                    reached.update(m["id"] for m in members_repo.list_members_for_group(gid))
                except Exception:
                    pass
            people = len(reached)
        cells["feeds"] = {
            "packages": len(feeding),
            "groups": len(granted_group_ids),
            "people": people,
        }

        out[cid] = cells
    return {"pipelines": out, "derived": derived}


def _bq_cap(key: str, default: int) -> int:
    """A BigQuery cost cap from live config, falling back to the documented
    default the API seeds it with (`_BQ_OPTIONAL_FIELD_DEFAULTS`)."""
    try:
        from app.instance_config import get_value

        raw = get_value("data_source", "bigquery", key, default=default)
        return int(raw) if raw is not None else default
    except Exception:
        return default


def _bigquery_credentialed() -> bool:
    """Whether this instance has a BigQuery service account at all — in the
    vault or in the environment. Either one is enough for the card to be true."""
    import os

    if os.getenv("BIGQUERY_SERVICE_ACCOUNT_JSON"):
        return True
    try:
        from src.repositories import system_secrets_repo

        return bool(system_secrets_repo().has("BIGQUERY_SERVICE_ACCOUNT_JSON"))
    except Exception:
        return False


def _snowflake_credentialed() -> bool:
    """Whether this instance has Snowflake coordinates + password available.

    The password may live in the env or in the vault, and the non-secret
    coordinates must be set in instance.yaml / /admin/server-config.
    """
    try:
        from connectors.snowflake.settings import resolve_snowflake_settings

        return bool(resolve_snowflake_settings())
    except Exception:
        return False


def _sf_cap(key: str, default: int) -> int:
    """A Snowflake materialize cap from live config."""
    try:
        from app.instance_config import get_value

        raw = get_value("data_source", "snowflake", key, default=default)
        return int(raw) if raw is not None else default
    except Exception:
        return default


def _databricks_credentialed() -> bool:
    """Whether this instance has Databricks host + warehouse + token."""
    try:
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        return bool(resolve_databricks_settings())
    except Exception:
        return False


def _db_cap(key: str, default: int) -> int:
    """A Databricks cost cap from live config."""
    try:
        from app.instance_config import get_value

        raw = get_value("data_source", "databricks", key, default=default)
        return int(raw) if raw is not None else default
    except Exception:
        return default


def _keboola_instance_config() -> tuple[str, str]:
    """The instance-level Keboola `stack_url` + `token_env`, however they got
    there (`instance.yaml` or `/admin/server-config`).

    Shared by `_keboola_credentialed()` (which only needs the boolean half)
    and the derived Keboola card's "Import as managed connection" button
    (`_source_inventory()`), which needs the actual values to POST to
    `POST /api/admin/source-connections` without a second round trip.
    """
    from app.instance_config import get_value

    stack_url = (get_value("data_source", "keboola", "stack_url", default="") or "").strip()
    token_env = (
        get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN") or "KEBOOLA_STORAGE_TOKEN"
    ).strip()
    return stack_url, token_env


def _keboola_credentialed() -> bool:
    """Whether this instance has an instance-level Keboola stack URL + token
    — the pre-flight half of the same check
    `app.api.admin_keboola_test.test_connection` runs before it makes a
    network round trip, reused here because a card render cannot afford one.

    A `source_connections` registry row proves a Keboola PROJECT was
    connected through the multi-connection wizard; this proves the OLDER,
    still-supported path — `data_source.keboola.*` in instance.yaml /
    server-config plus a token in the environment or the vault — is usable
    even when no such row exists. That gap is exactly what left
    `_DERIVED_SOURCES` without a Keboola entry despite
    `app/connections_seed.py` seeding one on first boot only when both are
    already present.

    The 3-step fallback itself lives in
    `app.datasource_secrets.keboola_instance_token` — shared with the
    derived Keboola card's "Import as managed connection" vault-seeding
    step (`app/api/admin_source_connections.py`), which also needs to know
    WHICH of the three actually holds the value, not just whether one does.
    """
    stack_url, token_env = _keboola_instance_config()
    if not stack_url:
        return False

    from app.datasource_secrets import keboola_instance_token

    value, _provenance = keboola_instance_token(token_env)
    return value is not None


def _orphan_reason(connection_id: str) -> str:
    """Why this connection dropped out of the semantic-layer sync.

    `_enumerate_master_sources()` skips a connection for THREE different
    reasons, and the page reported all of them as "master token missing" — so
    an admin whose connection was missing a stack URL, or whose token the
    server could no longer decrypt, was sent to re-add a token that was
    already there, and the rows still did not refresh.
    (Devin Review on this PR.)
    """
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo, source_connections_repo

    row = source_connections_repo().get(connection_id)
    if row is None:
        return "the connection no longer exists"
    if not ((row.get("config") or {}).get("stack_url") or ""):
        return "no connection URL on this project — add one at"
    try:
        token = connection_secrets_repo().get(master_secret_key(connection_id)) or ""
    except Exception:  # noqa: BLE001 — an unreadable secret is itself the answer
        return "its master token cannot be read (vault key changed?) — re-add it at"
    if not token:
        return "master token missing — add it at"
    return "it did not sync on the last run — check its status at"


@router.get("/admin/semantic-layer", response_class=HTMLResponse)
async def admin_semantic_layer_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Per-source breakdown for the Keboola semantic-layer sync (#853/#920/
    #953, task 7): one row per connection enumerated by
    ``_enumerate_master_sources()`` (master-token Keboola projects), each
    with its own metric/glossary counts and the last sync's per-source
    result. NULL ``source_ref`` rows (legacy, pre-provenance) fold into the
    default connection's row since that's the only connection legacy rows
    can belong to. Rows whose ``source_ref`` no longer matches any
    enumerated source (connection deleted/rotated away) surface separately
    as "orphaned" rather than silently vanishing.

    Master tokens are never put into the template context — only
    name/id/stack_url leave ``_enumerate_master_sources()``.
    """
    from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary
    from connectors.keboola.semantic_layer import _default_keboola_connection, _enumerate_master_sources
    from src.repositories import source_connections_repo

    ctx = _build_context(request, user=user)

    metrics = metric_repo().list()
    terms = glossary_repo().list(limit=100000)

    def _counts(ref: Optional[str]) -> tuple[int, int]:
        m = sum(1 for x in metrics if x.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and x.get("source_ref") == ref)
        g = sum(1 for x in terms if x.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and x.get("source_ref") == ref)
        return m, g

    raw_sources = _enumerate_master_sources()  # names/ids/stack_url only — token stripped below
    default_conn = _default_keboola_connection()
    default_id = default_conn["id"] if default_conn else None

    summary = get_last_refresh_summary()
    last_result = summary.get("last_result")
    last_by_ref: dict[Any, dict] = {}
    if isinstance(last_result, dict) and isinstance(last_result.get("sources"), list):
        for entry in last_result["sources"]:
            last_by_ref[entry.get("connection_id")] = entry

    sources = []
    null_absorbed = False
    for source in raw_sources:
        connection_id = source["connection_id"]
        metric_count, glossary_count = _counts(connection_id)
        if connection_id == default_id:
            null_metric_count, null_glossary_count = _counts(None)
            metric_count += null_metric_count
            glossary_count += null_glossary_count
            null_absorbed = True
        stack_url = source["stack_url"]
        # The stack host alone does not identify anything: several projects on
        # one stack render as the same string, so a page listing two sources
        # gave no way to tell which project each row's metrics came from. The
        # project id is the only unambiguous handle Keboola offers.
        host = urlsplit(stack_url).netloc or stack_url
        project_id = source.get("project_id")
        detail = host
        if project_id is not None:
            project_name = source.get("project_name") or "unnamed"
            detail = f"{project_name} (project {project_id}) · {host}"
        sources.append(
            {
                "connection_id": connection_id,
                "label": source["name"],
                "detail": detail,
                "project_id": project_id,
                "metric_count": metric_count,
                "glossary_count": glossary_count,
                "last": last_by_ref.get(connection_id),
            }
        )

    known_ids = {s["connection_id"] for s in raw_sources}

    # Every Keboola connection that exists but holds no master token. Without
    # this the page could only say "no projects have a master token yet",
    # which reads as "no project is connected" to an admin looking at a
    # working Keboola connection — the state every wizard-connected instance
    # starts in, since the master token is a SEPARATE slot from the storage
    # token the wizard fills.
    #
    # Each carries WHY it isn't syncing, because "no master token" is only one
    # of three reasons `_enumerate_master_sources()` skips a connection, and
    # telling an admin to add a token they already added — while the real
    # cause is a missing stack URL or a token no longer decryptable under the
    # current AGNES_VAULT_KEY — sends them to fix the wrong thing entirely
    # (Devin Review on #1242). `has()` is an existence check, so naming the
    # reason costs no decrypt.
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo

    keboola_connections = source_connections_repo().list(source_type="keboola")
    secrets = connection_secrets_repo()
    connections_without_master = []
    for c in keboola_connections:
        if c["id"] in known_ids:
            continue
        try:
            has_master = secrets.has(master_secret_key(c["id"]))
        except Exception:
            has_master = False
        if not has_master:
            reason = "no master (owner) token"
        elif not ((c.get("config") or {}).get("stack_url") or "").strip():
            reason = "master token set, but the connection has no stack URL"
        else:
            reason = "master token set, but it cannot be read — AGNES_VAULT_KEY changed since it was stored"
        connections_without_master.append({"id": c["id"], "name": c.get("name") or c["id"], "reason": reason})
    connection_names = {c["id"]: (c.get("name") or c["id"]) for c in keboola_connections}

    all_refs = {
        m.get("source_ref")
        for m in metrics
        if m.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and m.get("source_ref")
    }
    all_refs |= {
        t.get("source_ref") for t in terms if t.get("source") in KEBOOLA_SEMANTIC_LAYER_SOURCES and t.get("source_ref")
    }
    orphaned = []
    for ref in sorted(all_refs - known_ids):
        metric_count, glossary_count = _counts(ref)
        # A ref that still names a live connection is not a mystery UUID — it
        # is "this project lost its master token", which is both the common
        # case and the one with an obvious next step. Only a ref with no
        # connection left behind it stays an opaque id.
        orphaned.append(
            {
                "source_ref": ref,
                "label": connection_names.get(ref, ref),
                "connection_exists": ref in connection_names,
                "reason": _orphan_reason(ref) if ref in connection_names else None,
                "metric_count": metric_count,
                "glossary_count": glossary_count,
            }
        )

    # NULL-source_ref rows (legacy, pre-provenance) normally fold into the
    # default connection's row above. When the default connection has no
    # master token — so it's never enumerated by `_enumerate_master_sources()`
    # and never appears in `sources` — those rows would otherwise be counted
    # nowhere: the truthy `source_ref` filter above excludes them from
    # `all_refs` too. Surface them here instead, so they're never invisible.
    if not null_absorbed:
        null_metric_count, null_glossary_count = _counts(None)
        if null_metric_count or null_glossary_count:
            orphaned.append(
                {
                    "source_ref": None,
                    "label": "legacy / unattributed",
                    "metric_count": null_metric_count,
                    "glossary_count": null_glossary_count,
                }
            )

    # Datasets whose Keboola table isn't registered here, deduped across
    # sources. Every metric hanging off one is dropped as
    # `skipped_unresolved_table`, and until now that count went nowhere the
    # admin could see: a sync reporting "9 glossary, 0 metrics" gave no hint
    # that 50 metrics died on 12 unregistered tables.
    unresolved_tables: list[str] = []
    for entry in last_by_ref.values():
        for tid in entry.get("unresolved_tables") or []:
            if tid not in unresolved_tables:
                unresolved_tables.append(tid)

    ctx["sources"] = sources
    ctx["orphaned"] = orphaned
    ctx["connections_without_master"] = connections_without_master
    ctx["unresolved_tables"] = sorted(unresolved_tables)
    # Whether the list above is a SUBSET — deliberately a boolean, not a count.
    # The list is de-duplicated across projects (two projects can report the
    # same table) while any total would be summed per project, so the two do
    # not measure the same thing: a table reported twice made the page claim
    # tables were hidden when none were. And a true union total is not
    # available, because each project's list is already capped before it gets
    # here. So the page says a subset is shown, without a number it cannot
    # compute honestly. (Devin Review on this PR.)
    ctx["unresolved_tables_truncated"] = any(
        int(e.get("unresolved_tables_total") or 0) > len(e.get("unresolved_tables") or []) for e in last_by_ref.values()
    )
    ctx["skipped_unresolved_total"] = sum(int(e.get("skipped_unresolved_table") or 0) for e in last_by_ref.values())
    ctx["default_connection_id"] = default_id
    ctx["semantic_refresh_summary"] = summary
    return templates.TemplateResponse(request, "admin_semantic_layer.html", ctx)


@router.get("/admin/database", response_class=HTMLResponse)
async def admin_database_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """DB backend state machine — current backend, allowed transitions,
    active migration progress. Standalone page (not buried in
    /admin/server-config) so the operator workflow is one click from
    the admin menu.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_database.html", ctx)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for user management."""
    ctx = _build_context(request, user=user)
    # Server-rendered first paint: the total-users metric and the
    # group-filter dropdown options. The table rows themselves are fetched
    # client-side from GET /api/users (recency window + search/group filter).
    ctx["total_users"] = users_repo().count_all()
    groups = user_groups_repo().list_all()
    ctx["groups"] = groups
    # Pre-built option list for the ds.dropdown() visual replacement (#1055)
    # of #group-filter — Jinja's expression grammar has no list-comprehension
    # syntax, so the {id, name} → {value, label} mapping happens here.
    ctx["group_filter_options"] = [{"value": "", "label": "All groups"}] + [
        {"value": g["id"], "label": g["name"]} for g in groups
    ]
    # Identity provider, for the strip above the table. Google Workspace
    # produces PEOPLE (sign-in + nightly group sync), not tables — so its
    # status belongs on the page showing the people it produces, not in Data.
    # `google_group_count` is what makes the strip checkable rather than
    # decorative: it is the number of groups Workspace actually owns here, so
    # "why isn't Maria in Finance yet?" starts with a real number.
    from app.instance_config import get_allowed_domains

    ctx["idp_domains"] = get_allowed_domains()
    ctx["google_group_count"] = sum(
        1 for g in groups if str(g.get("created_by") or "").startswith("system:google-sync")
    )
    return templates.TemplateResponse(request, "admin_users.html", ctx)


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail_page(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-user detail page — core role + module capabilities + effective-roles debug.

    Renders shell HTML; the JS bootstraps all role data via the admin REST API
    (/api/admin/internal-roles, /api/admin/users/{id}/role-grants,
    /api/admin/users/{id}/effective-roles). Server-side we only need the
    target user's email + name so the page header renders before the API
    round-trips finish; everything role-related is loaded client-side so an
    admin reload picks up state changes from a sibling tab without a
    full-page reload elsewhere.
    """
    repo = users_repo()
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    ctx = _build_context(request, user=user, target_user=target)
    return templates.TemplateResponse(request, "admin_user_detail.html", ctx)


@router.get("/admin/usage")
async def admin_usage_redirect(_user: dict = Depends(require_admin)):
    """Legacy URL — 308 to /admin/telemetry. The page was renamed in the
    platform-telemetry epic to match what's actually shown (tool/skill
    invocations from session JSONLs). Old bookmarks land on the right
    place without breaking."""
    return RedirectResponse(url="/admin/telemetry", status_code=308)


@router.get("/admin/telemetry", response_class=HTMLResponse)
async def admin_telemetry_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Interactive Telemetry page — filter / group-by / search on usage_events.

    All data loads client-side from /api/admin/telemetry/* (facets, kpis,
    query) so the page state lives in the URL and the server doesn't
    preload a fixed window's snapshot.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_usage.html", ctx)


@router.get("/admin/sessions", response_class=HTMLResponse)
async def admin_sessions_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Global Sessions browser — every collected session JSONL across all
    users. The list page is a shell; data loads client-side via
    /api/admin/sessions/{list,kpis,facets}."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_sessions.html", ctx)


@router.get("/admin/adoption", response_class=HTMLResponse)
async def admin_adoption_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Adoption dashboard — system-wide KPI cards (24h/7d/30d), 30-day
    daily trend charts, top skills, and a users-by-activity list. A shell;
    data loads client-side from /api/admin/adoption/*."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_adoption.html", ctx)


@router.get("/admin/adoption/users/{user_id}", response_class=HTMLResponse)
async def admin_adoption_user_page(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Per-user adoption drill-down. Resolves the target user (404 if
    unknown) and renders a shell; data loads from
    /api/admin/adoption/users/{id}/*."""
    target = users_repo().get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    ctx = _build_context(
        request,
        user=user,
        target_user_id=user_id,
        target_user_email=target.get("email") or "",
        target_user_name=target.get("name") or "",
    )
    return templates.TemplateResponse(request, "admin_adoption_user.html", ctx)


@router.get("/admin/sessions/{username}/{session_file}", response_class=HTMLResponse)
async def admin_session_detail(
    request: Request,
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
):
    """Session transcript viewer. Username + session_file are revalidated by
    the API route (regex + path-escape guard) when /transcript is fetched;
    here we just render the shell."""
    ctx = _build_context(request, user=user, username=username, session_file=session_file)
    return templates.TemplateResponse(request, "admin_session_detail.html", ctx)


@router.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups_redirect(request: Request, user: dict = Depends(require_admin)):
    """The group LIST is the Access workspace's left column.

    This URL was a full-width table of ``user_groups`` — name, description,
    origin, member and grant counts, created, edit/delete. Every one of
    those is on ``/admin/access`` now: the counts and the origin ride the
    selector rows, the description and the created date are the pane's
    header, and edit/delete are the header's two buttons. What the table
    could not do is the thing the page is for — change what a group can
    use — so the section had two tabs that were both a list of groups.

    ``#table:<id>`` (the /admin/tables deep link) is a fragment, so it never
    reaches the server; the workspace rewrites it to its own ``?resource=``
    spelling client-side. Kept as a 308 rather than deleted: this URL is
    linked from a dozen places, is in the admin keyboard shortcuts, and is
    what an operator's muscle memory types.
    """
    return RedirectResponse(url="/admin/access", status_code=308)


@router.get("/admin/groups/{group_id}", response_class=HTMLResponse)
async def admin_group_detail_redirect(
    group_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """A single group is the Access workspace with that group selected.

    The detail page carried a header plus Members and Access tabs. Those
    two tabs are the workspace's two panes, side by side rather than behind
    a switch, and the header is the pane's header — so this page was the
    same three editors reached by a second route, over one pair of tables
    (``user_group_members``, ``resource_grants``). The full roster it owned,
    including each member's origin, is the pane's "Show all N" disclosure.

    ``?group=`` is the shape the workspace already accepted, so the
    selection survives the hop. The 404 on an unknown id stays here: it is
    cheaper and more honest than redirecting to a page that would silently
    open on a different group.
    """
    if not user_groups_repo().get(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    return RedirectResponse(url=f"/admin/access?group={quote(group_id, safe='')}", status_code=308)


@router.get("/admin/access", response_class=HTMLResponse)
async def admin_access_page(request: Request, user: dict = Depends(require_admin)):
    """The Access surface — the third leg of People → Data → Access, and
    the ONE place a group is edited.

    This URL has had four lives. It was a standalone grant matrix; it was
    retired into the group detail page's Access tab (``resource_grants``
    keys on ``group_id``, so the group's own page looked like where the
    editor belonged); it came back as a two-pane workspace beside that tab;
    and it is now the only one of the three left standing.

    The consolidation is the point. A group is one object with two sides —
    an audience, and a bundle of what that audience can use — and the
    product had grown FOUR editors over that one object: this page, the
    group list, the group detail page, and the create drawer's own copies
    of both panes. Four surfaces, one pair of tables
    (``user_group_members``, ``resource_grants``), and every one of them
    free to drift from the others.

    So this page absorbed what the other three uniquely had, rather than
    dropping it:

      * the list's origin pill and member/grant counts → the selector rows;
        its description, created date, rename and delete → the pane header
      * the detail page's full roster, with each member's origin and the
        rule that only admin-added membership can be removed here → the
        audience block's "Show all N"; its resource filter and its
        ``#table:`` deep link → the pane's filter and ``?resource=``
      * the drawer keeps the one job nothing else does (naming a group)
        and lost its duplicate people/access steps

    What stays elsewhere is only the TRANSPOSE — a resource's Share panel
    (which groups get this package) and a person's memberships on their own
    page. Those answer different questions; they are not second copies of
    this one.

    ``?group=<id>`` selects that group — the shape ``/admin/groups/{id}``
    now redirects to. ``?resource=<type>:<id>`` pre-filters the grant tree,
    which is how /admin/tables' "Manage access" arrives.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_access.html", ctx)


@router.get("/admin/grants", response_class=HTMLResponse)
async def admin_grants_redirect(request: Request, user: dict = Depends(require_admin)):
    """Backward-compat redirect for the page's oldest URL.

    Carries ``?group=`` through, which the Access page reads to preselect a
    group. Keeps the admin gate the destination has, so a non-admin gets 403
    here rather than a 308 naming an internal URL.
    """
    gid = request.query_params.get("group", "").strip()
    target = f"/admin/access?group={quote(gid, safe='')}" if gid else "/admin/access"
    return RedirectResponse(url=target, status_code=308)


@router.get("/admin/marketplaces", response_class=HTMLResponse)
async def admin_marketplaces_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for marketplace git repositories (register / sync / delete)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_marketplaces.html", ctx)


@router.get("/admin/linked-apps", response_class=HTMLResponse)
async def admin_linked_apps_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Guided admin flow for linking externally-hosted (Keboola) data apps:
    pick an MCP source → materialize its data-app lister → select the ingested
    apps and grant them to a group. Wires existing admin APIs (mcp-sources,
    mcp-tools, materialize, data-apps ?kind=linked, access/grants) — no new
    control-plane surface beyond the page itself."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_linked_apps.html", ctx)


@router.get("/admin/contribute-skill", response_class=HTMLResponse)
async def admin_contribute_skill_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Paste a generated SKILL.md and publish it into the contributed
    marketplace. This is the landing target for an external "Load skill to
    Agnes" button: the external tool copies the skill to the clipboard and
    opens this page (optionally with ?prefill=1 to auto-read the clipboard)."""
    from src.repositories import user_groups_repo

    ctx = _build_context(request, user=user)
    ctx["groups"] = user_groups_repo().list_all()
    csrf_token = _get_or_mint_web_csrf(request)
    ctx["csrf_token"] = csrf_token
    response = templates.TemplateResponse(request, "contribute_skill.html", ctx)
    _set_web_csrf_cookie(response, request, csrf_token)
    return response


@router.post("/admin/contribute-skill", response_class=HTMLResponse)
def admin_contribute_skill_submit(
    request: Request,
    user: dict = Depends(require_admin),
    skill_md: str = Form(...),
    grant_group: str = Form("Admin"),
    csrf_token: str = Form(""),
):
    """Publish the pasted SKILL.md, then re-render the page with a deep link
    to the new plugin (the "open it in Agnes" advert loop).

    Declared ``def`` (not ``async def``) so FastAPI dispatches it to the thread
    pool: ``contribute_skill()`` acquires the process-wide marketplace ``_lock``,
    which the nightly bulk sync can hold for minutes across git clones. Blocking
    on it from the event-loop thread would freeze every concurrent request, so
    the blocking work must run off-loop — same rationale as ``trigger_sync_all``
    (app/api/marketplaces.py)."""
    from app.marketplace_server.packager import invalidate_etag_cache
    from src.skill_contribution import SkillContributionError, contribute_skill

    ctx = _build_context(request, user=user)
    ctx["groups"] = user_groups_repo().list_all()
    issued_token = _get_or_mint_web_csrf(request)
    ctx["csrf_token"] = issued_token
    if not _web_csrf_ok(request, csrf_token):
        ctx["error"] = "Security check failed (missing or stale form token) — reload the page and try again."
        ctx["skill_md"] = skill_md
        resp = templates.TemplateResponse(request, "contribute_skill.html", ctx, status_code=400)
        _refresh_web_csrf_cookie(resp, request, issued_token)
        return resp
    try:
        result = contribute_skill(
            skill_md,
            registered_by=user.get("email") or user.get("id"),
            grant_group=(grant_group or "Admin").strip(),
        )
        invalidate_etag_cache()
        # Assign result only after every fallible step succeeded, so a failure
        # never leaves both the success and error banners rendered at once.
        ctx["result"] = result
    except SkillContributionError as e:
        ctx["error"] = str(e)
        ctx["skill_md"] = skill_md
    except Exception as e:  # noqa: BLE001 — surface any failure in the page
        logger.exception("contribute-skill failed")
        ctx["error"] = f"Unexpected error: {e}"
        ctx["skill_md"] = skill_md
    return templates.TemplateResponse(request, "contribute_skill.html", ctx)


@router.post("/admin/contribute-skill/{name}/delete", response_class=HTMLResponse)
def admin_contribute_skill_delete(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
    csrf_token: str = Form(""),
):
    """Delete a contributed skill plugin and redirect back to the list page."""
    import json as _json
    import shutil

    from fastapi.responses import RedirectResponse

    from app.marketplace_server.packager import invalidate_etag_cache
    from app.utils import get_marketplaces_dir
    from src.marketplace import _lock, _refresh_plugin_cache
    from src.skill_contribution import CONTRIBUTED_MARKETPLACE_SLUG

    if not _web_csrf_ok(request, csrf_token):
        from src.repositories import user_groups_repo

        ctx = _build_context(request, user=user)
        ctx["groups"] = user_groups_repo().list_all()
        issued_token = _get_or_mint_web_csrf(request)
        ctx["csrf_token"] = issued_token
        ctx["error"] = "Security check failed (missing or stale form token) — reload the page and try again."
        resp = templates.TemplateResponse(request, "contribute_skill.html", ctx, status_code=400)
        _refresh_web_csrf_cookie(resp, request, issued_token)
        return resp

    repo_root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
    plugins_dir = repo_root / "plugins"
    plugin_dir = (plugins_dir / name).resolve()
    if not str(plugin_dir).startswith(str(plugins_dir.resolve())):
        from src.repositories import user_groups_repo

        ctx = _build_context(request, user=user)
        ctx["groups"] = user_groups_repo().list_all()
        ctx["csrf_token"] = _get_or_mint_web_csrf(request)
        ctx["error"] = "Invalid plugin name."
        return templates.TemplateResponse(request, "contribute_skill.html", ctx)

    with _lock:
        if not plugin_dir.exists():
            from src.repositories import user_groups_repo

            ctx = _build_context(request, user=user)
            ctx["groups"] = user_groups_repo().list_all()
            ctx["csrf_token"] = _get_or_mint_web_csrf(request)
            ctx["error"] = f"Plugin '{name}' not found."
            return templates.TemplateResponse(request, "contribute_skill.html", ctx)
        shutil.rmtree(plugin_dir)
        manifest_path = repo_root / ".claude-plugin" / "marketplace.json"
        if manifest_path.is_file():
            try:
                data = _json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    old_plugins = data.get("plugins")
                    if isinstance(old_plugins, list):
                        data["plugins"] = [
                            p for p in old_plugins if not (isinstance(p, dict) and p.get("name") == name)
                        ]
                    manifest_path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass
        _refresh_plugin_cache(CONTRIBUTED_MARKETPLACE_SLUG)
        resource_grants_repo().delete_by_resource("marketplace_plugin", f"{CONTRIBUTED_MARKETPLACE_SLUG}/{name}")
        invalidate_etag_cache()

    return RedirectResponse("/admin/contribute-skill", status_code=303)


@router.get("/admin/initial-workspace", response_class=HTMLResponse)
async def admin_initial_workspace_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for the Initial Workspace Template repo (register / sync /
    delete + per-file prompt provenance). Relocated from /admin/server-config
    (#622 Slice 3 PR-B)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_initial_workspace.html", ctx)


# ── Inbound MCP source admin (RFC keboola/agnes-the-ai-analyst#461) ──
#
# Shell-only routes — every dynamic bit is fetched client-side from the
# REST API under /api/admin/mcp-sources and /api/admin/mcp-tools (built in
# parallel; contract pinned in the RFC §5). Keeping the server side this
# thin means a contract drift only requires touching the templates' JS.
@router.get("/admin/mcp-sources", response_class=HTMLResponse)
async def admin_mcp_sources_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """List page for registered MCP sources."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_mcp_sources.html", ctx)


@router.get("/admin/mcp-sources/{source_id}", response_class=HTMLResponse)
async def admin_mcp_source_detail_page(
    source_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Detail page for a single MCP source — config, introspect, curation."""
    ctx = _build_context(request, user=user, source_id=source_id)
    return templates.TemplateResponse(request, "admin_mcp_source_detail.html", ctx)


@router.get("/admin/mcp-tools/{tool_id}/grants", response_class=HTMLResponse)
async def admin_mcp_tool_grants_page(
    tool_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Grant-management page for a passthrough MCP tool."""
    ctx = _build_context(request, user=user, tool_id=tool_id)
    return templates.TemplateResponse(request, "admin_mcp_tool_grants.html", ctx)


# ── Maintained digests admin (K4, #799) ──
#
# Shell-only route — data + CRUD are fetched client-side against
# /api/admin/knowledge-digests (app/api/knowledge_digests.py), the same
# posture as admin_mcp_sources_page above. Read/distribution grants are
# managed separately on /admin/access (ResourceType.KNOWLEDGE_DIGEST).
@router.get("/admin/knowledge-digests", response_class=HTMLResponse)
async def admin_knowledge_digests_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """List page for admin-defined maintained digests."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_knowledge_digests.html", ctx)


# Scheduler-driven admin actions audited by app/api/admin.py and
# app/api/marketplaces.py. Keep in sync with the JOBS list in
# services/scheduler/__main__.py.
#
# `data-refresh` (POST /api/sync/trigger) and `script-runner`
# (POST /api/scripts/run-due) are scheduler jobs but they do NOT write
# audit_log today, so they can't appear here. If you add audit calls to
# those endpoints, add the matching action strings to this list.
SCHEDULER_AUDIT_ACTIONS = [
    "run_session_collector",
    "run_session_processor:verification",
    "run_session_processor:usage",
    "run_corporate_memory",
    "marketplace.sync_all",
    "run_blocked_purge",
    # Initial Workspace Template nightly auto-sync (#622 Slice 3 PR-B) —
    # written by _do_sync via the /sync-if-configured scheduler job.
    "initial_workspace.sync",
    "initial_workspace.sync_failed",
]


@router.get("/admin/store", response_class=HTMLResponse)
async def admin_moderation_hub_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Moderation & Trust — one admin surface for entity verification,
    submission review, and marketplace curation.

    Lists Store entities awaiting verification (``verification_state='requested'``)
    with a direct link to each entity's detail page, where the Verify /
    Request changes / Archive / Override actions live. The pre-publish
    submission queue and marketplace curation are surfaced as links (count +
    jump-off), not rebuilt here. ``/admin/store`` is the natural parent of the
    ``/admin/store/submissions`` review queue.
    """
    from app.instance_config import get_store_verification_enabled

    verification_enabled = get_store_verification_enabled()
    verification_requests: list = []
    verification_total = 0
    verification_limit = 200
    if verification_enabled:
        # Admin view: no visibility_status filter, so a requested entity
        # surfaces regardless of its lifecycle state. Keep the repo's real
        # total (not len(rows)) so the page can flag when more are waiting
        # than the page shows.
        verification_requests, verification_total = store_entities_repo().list(
            verification_state=["requested"],
            limit=verification_limit,
        )

    # "Needs review" count — the exact verdict set the submission queue's
    # "Needs review" chip uses (admin_store_submissions.html), so this number
    # matches what the admin sees after clicking through. `blocked_inline`
    # (the Rescan-flow state) MUST be included — dropping it undercounts.
    _, pending_submissions_total = store_submissions_repo().list_for_admin(
        status=["blocked_inline", "blocked_llm", "review_error"],
        limit=1,
    )

    ctx = _build_context(
        request,
        user=user,
        verification_requests=verification_requests,
        verification_total=verification_total,
        verification_limit=verification_limit,
        pending_submissions_total=pending_submissions_total,
        store_verification_enabled=verification_enabled,
    )
    return templates.TemplateResponse(request, "admin_moderation_hub.html", ctx)


@router.get("/admin/store/submissions", response_class=HTMLResponse)
async def admin_store_submissions_page(
    request: Request,
    status: Optional[str] = None,
    submitter: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 — FastAPI query-param name
    name: Optional[str] = None,
    version: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Triage page for flea-market guardrail submissions.

    Lists every submission row newest-first with the inline-check verdicts,
    LLM findings, and override action buttons. Server-side render keeps the
    page accessible without JS for the read-only inspect path; mutating
    actions (override, retry, delete) hit the JSON admin endpoints under
    ``/api/admin/store/submissions``.

    Filters AND together; URL is bookmarkable. Pagination via ``skip`` /
    ``limit`` (default 50, clamped to [1, 200] for the UI page-size
    selector).
    """

    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
    valid_type = type if type in {"skill", "agent", "plugin"} else None
    limit = max(1, min(int(limit), 200))
    skip = max(0, int(skip))

    # v36+ chip routing — see app/api/admin.py:admin_list_store_submissions
    # for the same logic on the JSON endpoint. Lifecycle tokens
    # ('archived', 'deleted') route to the JOIN-based filter; verdict
    # tokens pass through.
    lifecycle = None
    if statuses == ["archived"]:
        lifecycle = "archived"
        statuses = None
    elif statuses == ["deleted"]:
        lifecycle = "deleted"
        statuses = None

    valid_sort = sort if sort in {"created_at", "file_size", "status", "name"} else None
    valid_order = order if order in {"asc", "desc"} else None
    items, total = store_submissions_repo().list_for_admin(
        status=statuses,
        submitter_id=submitter or None,
        type_=valid_type,
        name_substr=name or None,
        version_substr=version or None,
        sort_by=valid_sort,
        sort_order=valid_order,
        lifecycle=lifecycle,
        limit=limit,
        skip=skip,
    )

    # Resolve submitter_id → email for the active-filter chip when set.
    # (The submitter id is opaque to admins; show the human label instead.)
    submitter_email = ""
    if submitter:
        urow = users_repo().get_by_id(submitter)
        if urow:
            submitter_email = urow.get("email") or submitter

    pages = max(1, (int(total) + limit - 1) // limit)
    current_page = (skip // limit) + 1

    ctx = _build_context(
        request,
        user=user,
        items=items,
        total=total,
        status_filter=status or "",
        submitter_filter=submitter or "",
        submitter_email=submitter_email,
        type_filter=valid_type or "",
        name_filter=name or "",
        version_filter=version or "",
        sort_filter=valid_sort or "",
        order_filter=valid_order or "",
        limit=limit,
        skip=skip,
        pages=pages,
        current_page=current_page,
    )
    return templates.TemplateResponse(request, "admin_store_submissions.html", ctx)


@router.get("/admin/store/submissions/{submission_id}", response_class=HTMLResponse)
async def admin_store_submission_detail_page(
    submission_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-submission detail with full verdict + override + retry actions."""

    sub = store_submissions_repo().get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")

    # Live entity lifecycle, separate from the submission's verdict.
    # Verdict (sub.status) is immutable forensic record; lifecycle
    # (entity.visibility_status) reflects current state — see plan
    # "Admin Submissions Filter: Use Entity Visibility, Not Denormalized Status".
    # Resolve THIS submission's version_no via submission_id (NOT
    # hash — multiple history entries can share a hash when the user
    # re-uploads byte-identical bundles, and the hash-match-first-wins
    # loop always picked v1, mislabeling every reupload as v1). Same
    # fix as PR #330 for the runner / override paths; we missed this
    # display site at the time.
    entity_visibility_status = None
    entity_version_no = None
    submission_version_no = None
    sibling_submissions: list = []
    if sub.get("entity_id"):
        ent = store_entities_repo().get(sub["entity_id"])
        if ent:
            entity_visibility_status = ent.get("visibility_status")
            entity_version_no = ent.get("version_no")
            from app.api.store import _version_no_for_submission

            submission_version_no = _version_no_for_submission(
                ent,
                submission_id,
            )
            # Build a version-switcher: every submission row linked to
            # this entity, sorted newest first, with its derived v#.
            # Admin clicks a row → jumps to that submission's detail.
            # Surfaces multi-version entities clearly + lets admin
            # compare verdicts across versions without bouncing back
            # to the queue.
            history = ent.get("version_history") or []
            history_by_sub: dict = {}
            for entry in history:
                sid = entry.get("submission_id")
                if sid:
                    try:
                        history_by_sub[sid] = int(entry.get("n"))
                    except (TypeError, ValueError):
                        continue
            # list_for_admin doesn't filter by entity_id and we don't want
            # to add a parameter for this one display need. list_for_entity
            # orders by created_at DESC so newest is first in the switcher.
            ent_sub_rows = store_submissions_repo().list_for_entity(sub["entity_id"])
            for row in ent_sub_rows:
                sibling_submissions.append(
                    {
                        "id": row["id"],
                        "status": row.get("status"),
                        "version": row.get("version"),
                        "created_at": row.get("created_at"),
                        "version_no": history_by_sub.get(row["id"]),
                        "reviewed_by_model": row.get("reviewed_by_model"),
                        "is_current": row["id"] == submission_id,
                    }
                )

    other_count = store_submissions_repo().count_for_submitter(
        sub["submitter_id"],
        exclude_id=submission_id,
    )

    user_repo = users_repo()
    override_email = ""
    if sub.get("override_by"):
        urow = user_repo.get_by_id(sub["override_by"])
        if urow:
            override_email = urow.get("email") or sub["override_by"]

    # Activity timeline — pull every audit_log row scoped to this
    # submission OR its linked entity. Resolves actor user_id → email
    # so the timeline reads naturally. Cached in-memory per-render so
    # we don't fan out N user lookups on a 100-row history.
    #
    # Four resource patterns matter:
    #   * "store_submission:{id}" — admin actions (override / rescan
    #     / retry / delete / bundle download) + post-fix runner audits
    #   * "store_entity:{id}"     — when {id} is a submission_id, this
    #     is what the legacy `_audit` helper in app/api/store.py emits
    #     for submission-scoped events because the helper hardcodes
    #     the `store_entity:` prefix. Surface them under the timeline
    #     so accepted / approved / blocked_inline audits are visible.
    #   * "{id}" (bare submission id) — older runner.py rows from
    #     before the prefix fix; kept for back-compat.
    #   * "store_entity:{entity_id}" — entity-scoped events
    #     (creation, hard delete). entity_id stays on submission
    #     rows even after hard delete (tombstone), so the linkage
    #     survives — see mark_deleted_for_entity.
    submission_resources = [
        f"store_submission:{submission_id}",
        f"store_entity:{submission_id}",
        submission_id,
    ]
    submission_audit_rows = audit_repo().query_for_resources(
        submission_resources,
        limit=100,
    )
    entity_audit_rows: list = []
    if sub.get("entity_id"):
        entity_audit_rows = audit_repo().query_for_resources(
            [f"store_entity:{sub['entity_id']}"],
            limit=100,
        )
        # Drop entity-scoped rows that are actually submission audits for
        # OTHER versions of the same entity (the helper writes them at
        # resource=store_entity:{sub_id} for ALL submissions). Keep only
        # rows whose action is a true entity-scoped event so admins see
        # entity lifecycle (archive / install / delete) here without
        # other versions' verdict noise leaking in.
        entity_audit_rows = [
            r for r in entity_audit_rows if not (r.get("action") or "").startswith("store.submission.")
        ]
    actor_cache: dict = {}

    def _resolve_actor(rows):
        for row in rows:
            uid = row.get("user_id")
            if not uid:
                row["actor_email"] = ""
                continue
            if uid not in actor_cache:
                urow = user_repo.get_by_id(uid)
                actor_cache[uid] = (urow or {}).get("email") or uid
            row["actor_email"] = actor_cache[uid]

    _resolve_actor(submission_audit_rows)
    _resolve_actor(entity_audit_rows)
    # Combine for back-compat with the existing template var name.
    audit_rows = submission_audit_rows

    from app.instance_config import (
        get_guardrails_enabled,
        get_guardrails_llm_provider_ready,
    )

    ctx = _build_context(
        request,
        user=user,
        sub=sub,
        other_count=other_count,
        override_email=override_email,
        audit_rows=audit_rows,
        submission_audit_rows=submission_audit_rows,
        entity_audit_rows=entity_audit_rows,
        entity_visibility_status=entity_visibility_status,
        entity_version_no=entity_version_no,
        submission_version_no=submission_version_no,
        sibling_submissions=sibling_submissions,
        guardrails_llm_ready=get_guardrails_enabled() and get_guardrails_llm_provider_ready(),
    )
    return templates.TemplateResponse(request, "admin_store_submission_detail.html", ctx)


@router.get("/admin/scheduler-runs")
async def admin_scheduler_runs_redirect(_user: dict = Depends(require_admin)):
    """Scheduler runs is now a filter on the unified Activity page, not a
    standalone view — see the unification done in the platform-telemetry
    epic. Keep the URL as a 308 so existing bookmarks land on the right
    pre-filtered view.
    """
    return RedirectResponse(url="/admin/activity?source=scheduler", status_code=308)


@router.get("/admin/prompts", response_class=HTMLResponse)
async def admin_prompts_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unified admin page for managed prompts (#622): two cards (install +
    workspace), each with a Git⇄Editor source toggle, a CodeMirror editor for
    editor mode, and a repo-path bind field for git mode. All dynamic state is
    fetched client-side from /api/admin/prompts/{kind}; the route only needs to
    know whether an IWT repo is registered so the git toggle can be disabled
    when there's nothing to bind to."""
    from src.initial_workspace import is_configured

    ctx = _build_context(request, user=user, iwt_configured=is_configured())
    return templates.TemplateResponse(request, "admin_prompts.html", ctx)


@router.get("/admin/agent-prompt", response_class=HTMLResponse)
async def admin_agent_prompt_page(request: Request):
    """Superseded by /admin/prompts (#622). 308 keeps bookmarks alive."""
    return RedirectResponse(url="/admin/prompts", status_code=308)


@router.get("/admin/workspace-prompt", response_class=HTMLResponse)
async def admin_workspace_prompt_page(request: Request):
    """Superseded by /admin/prompts (#622). 308 keeps bookmarks alive."""
    return RedirectResponse(url="/admin/prompts", status_code=308)


@router.get("/admin/tokens", response_class=HTMLResponse)
async def admin_tokens_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin — list of ALL tokens for incident response + offboarding.

    Admin-only. No create form here (admins mint their own PATs via /me/profile).
    URL param ?user=<email> pre-fills the owner filter (deep-link from
    /admin/users "Tokens" action).
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_tokens.html", ctx)


@router.get("/me/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """User profile — self-service view of identity and group memberships.

    Renders the user's account info plus a list of group memberships joined
    against ``user_groups`` (with the source label so users can tell which
    were added by an admin, by Google sync, or seeded at deploy).
    """
    memberships = user_group_members_repo().list_groups_with_meta_for_user(user["id"])
    # Project the same chip metadata the /admin/users/{id} page derives:
    # origin (single source of truth via app.api.access._derive_origin),
    # plus a display_name that shortens raw Workspace emails for
    # google_sync rows (`grp_acme_legal@workspace.example.com` → `Legal`). The
    # Jinja template just renders these without env lookups.
    from app.api.access import _derive_origin

    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    for m in memberships:
        m["origin"] = _derive_origin(m)
        if m["origin"] == "google_sync" and m["name"] and m["name"] not in ("Admin", "Everyone"):
            local = m["name"].split("@", 1)[0]
            if prefix and local.lower().startswith(prefix):
                local = local[len(prefix) :]
            local = local.lstrip("_- \t")
            if not local:
                local = m["name"].split("@", 1)[0]
            m["display_name"] = local[:1].upper() + local[1:]
        else:
            m["display_name"] = m["name"]

    # Session-diagnostics context (formerly the /me/debug page). The
    # troubleshooting section renders the caller's OWN decoded JWT +
    # Google-sync snapshot — their own data, no debug gate on the read.
    _SENSITIVE_USER_COLUMNS = ("password_hash", "setup_token", "reset_token")
    user_record_safe = {k: v for k, v in user.items() if k not in _SENSITIVE_USER_COLUMNS}
    raw_token = _read_session_token(request)
    # Double-submit CSRF token for the refetch-groups POST below (F2); the
    # troubleshooting partial sends it back as the X-CSRF-Token header.
    csrf_token = _get_or_mint_web_csrf(request)

    from app.auth.elevation import elevation_paused

    # Notification channels (moved off the retired /dashboard landing, #896).
    # Telegram link state is read for real via the backend-aware repo factory
    # (the dashboard rendered a hardcoded not-linked stub); desktop stays a
    # static private-beta row until it grows a self-serve link flow.
    try:
        _tg_link = notifications_telegram_repo().get_link(user["id"])
    except Exception:
        _tg_link = None
    telegram_status = {"linked": bool(_tg_link)}
    desktop_status = {"linked": False}

    ctx = _build_context(
        request,
        user=user,
        memberships=memberships,
        is_admin=is_user_admin(user["id"], conn),
        elevation_paused=elevation_paused(),
        user_record=user_record_safe,
        claims=_decoded_claims(raw_token),
        token_fingerprint=_token_fingerprint(raw_token),
        sync_summary=_last_sync_summary(user["id"]),
        telegram_status=telegram_status,
        desktop_status=desktop_status,
        # Display-only — keep original case (no .lower()), unlike the
        # refetch-groups handler below which lowercases for set comparison.
        google_group_prefix=os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip(),
        csrf_token=csrf_token,
    )
    response = templates.TemplateResponse(request, "profile.html", ctx)
    _set_web_csrf_cookie(response, request, csrf_token)
    return response


@router.post("/me/profile/refetch-groups", name="me_profile_refetch_groups")
async def me_profile_refetch_groups(
    request: Request,
    _: None = Depends(require_debug_auth_enabled),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-issue ``fetch_user_groups`` for the current user and return a
    dry-run diff against the cached ``user_group_members`` snapshot,
    writing nothing. Gated behind AGNES_DEBUG_AUTH — a dry-run admin
    debug action, not user-facing content.

    Requires the ``X-CSRF-Token`` header to match the ``web_csrf`` cookie
    issued by the profile page (F2 double-submit; the cookie fallback in the
    auth layer means even JSON POSTs are not automatically CSRF-exempt)."""
    if not _web_csrf_ok(request, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=403, detail="csrf_check_failed")
    from app.auth.group_sync import fetch_user_groups

    fetched = fetch_user_groups(user["email"])
    soft_failed = fetched is None
    fetched_list = list(fetched) if fetched else []

    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    if prefix:
        relevant = [g.lower() for g in fetched_list if g.lower().startswith(prefix)]
    else:
        relevant = [g.lower() for g in fetched_list]

    current_rows = user_group_members_repo().list_google_sync_groups_for_user(user["id"])
    # The repo abstracts the information_schema external_id probe: rows carry
    # external_id=None when the column is absent (Postgres, or DuckDB without
    # the column). `has_ext` mirrors the old probe — when no row carries a
    # non-NULL external_id we suppress would_remove exactly as the legacy
    # column-absent branch did (current_external_ids is empty in that case
    # anyway, so the diff is identical across backends).
    has_ext = any(r.get("external_id") for r in current_rows)
    current_external_ids = {r["external_id"].lower() for r in current_rows if r.get("external_id")}
    current_names = [r["name"] for r in current_rows]

    fetched_set = set(relevant)
    would_add = sorted(fetched_set - current_external_ids)
    would_remove = sorted(current_external_ids - fetched_set) if has_ext else []

    return {
        "soft_failed": soft_failed,
        "prefix": prefix or None,
        "fetched": fetched_list,
        "fetched_relevant": relevant,
        "current_names": current_names,
        "current_external_ids": sorted(current_external_ids),
        "would_add": would_add,
        "would_remove": would_remove,
        "applied": False,
    }


@router.get("/profile/sessions", response_class=HTMLResponse)
async def profile_sessions_redirect(request: Request):
    """Legacy redirect — ``/profile/sessions`` → ``/me/activity?tab=sessions``."""
    return RedirectResponse(url="/me/activity?tab=sessions", status_code=301)


@router.get("/profile/sessions/{filename}")
async def profile_session_download(
    filename: str,
    user: dict = Depends(get_current_user),
):
    """Download a single jsonl session file owned by the caller.

    Path safety: filename is single-component (no separators, no `..`,
    must end in `.jsonl`); the served path is built under
    `${DATA_DIR}/user_sessions/<current_user.id>/` and must resolve into
    that directory. Any deviation yields 404 — never 403, so we don't
    leak the existence of files belonging to other users.
    """
    import pathlib

    if "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(status_code=404, detail="Not found")
    if not filename.endswith(".jsonl"):
        raise HTTPException(status_code=404, detail="Not found")

    user_id = user["id"]
    data_dir = pathlib.Path(os.environ.get("DATA_DIR", "/data")).resolve()
    user_dir = (data_dir / "user_sessions" / user_id).resolve()
    target = (user_dir / filename).resolve()

    try:
        target.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path=str(target),
        filename=filename,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/_debug/throw/http/{code:int}", response_class=HTMLResponse, include_in_schema=False)
async def _debug_throw_http(request: Request, code: int):
    """Dev helper — raise an HTTPException with the given status code.

    Only mounted when DEBUG=1 (gated below). Lets you eyeball the error
    page chrome + debug-toolbar panels for any HTTP status code:
      /_debug/throw/http/404  → 404 page
      /_debug/throw/http/418  → 418 page (custom title falls back to "Error")
      /_debug/throw/http/500  → 500 page rendered via the StarletteHTTPException
                                handler (NOT the unhandled-exception handler —
                                use /_debug/throw/exc for that)
    """
    if not _is_debug():
        raise HTTPException(status_code=404, detail="Not found")
    raise HTTPException(status_code=code, detail=f"Forced {code} via /_debug/throw/http/{code}")


@router.get("/_debug/throw/exc", response_class=HTMLResponse, include_in_schema=False)
async def _debug_throw_exc(request: Request):
    """Dev helper — raise an unhandled exception to exercise the 500 path."""
    if not _is_debug():
        raise HTTPException(status_code=404, detail="Not found")
    # Force a real traceback so the DEBUG-only `<details>Traceback</details>`
    # block in error.html shows something interesting (not just "RuntimeError").
    payload = {"a": 1}
    return payload["nope"]  # KeyError with a useful traceback


def _is_debug() -> bool:
    return os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def _chats_rows(request: Request, user: dict) -> tuple[list[dict], dict[str, int], list[tuple]]:
    """Project the caller's conversations into Chats-page rows.

    Returns ``(rows, bucket_counts, agent_options)``. Rows are what the template
    renders and the client-side toolbar filters over; ``bucket_counts`` fills the
    four segment badges; ``agent_options`` is the Filter menu's Agent category.

    Scope is the caller's OWN sessions — archived ones included, because the page
    is the only surface that can list and undo that state — plus co-drive
    sessions someone else owns and shared with them (``shared_with_me``). Those
    last ones are read-only here: pin / rename / archive / delete are all
    owner-only server-side (404 for anyone else), so the row offers none of them
    rather than showing four controls that would fail.
    """
    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        return [], {"all": 0, "pinned": 0, "shared": 0, "archived": 0}, []

    email = user.get("email") or ""
    try:
        own = repo.list_sessions(email, include_archived=True)
    except Exception:
        logger.exception("/chats: listing own sessions failed")
        own = []
    # Co-drive sessions the caller takes part in. The owner is a participant of
    # their own co-session too (see fork_session_as_co_session), so this list
    # overlaps `own` — dedupe on id and keep the owned reading, which is the one
    # that carries the row actions.
    try:
        shared_with_me = [s for s in repo.list_sessions_for_participant(email) if s.user_email != email]
    except Exception:
        logger.exception("/chats: listing shared sessions failed")
        shared_with_me = []

    # Agent id → its name, so the Agent column reads as a name rather than an id.
    # A session predating the v96 `agents` table (or forked as a co-session) has
    # no agent_id at all; those rows say "Default agent", which is what ran them.
    from src.repositories import agents_repo

    agent_names: dict[str, str] = {}
    try:
        for a in agents_repo().list_for_user(user.get("id") or ""):
            agent_names[a["id"]] = a.get("name") or "Agent"
    except Exception:
        logger.exception("/chats: resolving agent names failed")

    SURFACE_LABELS = {
        "web": "Web",
        "slack_dm": "Slack DM",
        "slack_thread": "Slack thread",
        "teams_dm": "Teams DM",
        "api": "API",
    }

    rows: list[dict] = []
    seen: set[str] = set()
    for s, owned in [(s, True) for s in own] + [(s, False) for s in shared_with_me]:
        if s.id in seen:
            continue
        seen.add(s.id)
        pinned = owned and s.pinned_at is not None
        archived = bool(s.archived)
        shared = bool(s.is_co_session) or not owned
        # The segment set (see filter_toolbar.js `segments.multi`). `all` is a
        # real token, not a wildcard, which is what keeps archived conversations
        # out of every other view without a special case in the engine. Archived
        # is deliberately exclusive: an archived chat is put away, so it should
        # not also be sitting in Pinned.
        buckets = ["archived"] if archived else ["all"]
        if not archived:
            if pinned:
                buckets.append("pinned")
            if shared:
                buckets.append("shared")
        agent_label = agent_names.get(s.agent_id or "", "Default agent")
        updated = s.last_message_at or s.started_at
        title = (s.title or "").strip() or "Untitled chat"
        rows.append(
            {
                "id": s.id,
                "title": title,
                "href": f"/chat?session={quote(s.id, safe='')}",
                "agent_label": agent_label,
                "agent_key": agent_label.lower(),
                "surface": s.surface.value,
                "surface_label": SURFACE_LABELS.get(s.surface.value, "Web"),
                # No message count: it says nothing about WHICH conversation a
                # row is, and as a column of small numbers it was the kind of
                # furniture that made the list read as a table.
                "updated_iso": updated.isoformat() if updated else "",
                "pinned": pinned,
                "archived": archived,
                "shared": shared,
                "owned": owned,
                "buckets": "|".join(buckets),
                # What the page's search box matches on — lowercased here so the
                # engine's own lowercased query is a plain substring test.
                "search": " ".join([title, agent_label, SURFACE_LABELS.get(s.surface.value, "")]).lower(),
            }
        )

    # Pinned first, then most-recent-first — the same order the rail's list uses,
    # so the page opens on the ordering the caller already knows. One `reverse`
    # covers both keys because both want descending (True before False, and ISO
    # 8601 sorts lexicographically). Archived rows keep their recency order
    # inside their own segment.
    rows.sort(key=lambda r: (r["pinned"], r["updated_iso"] or ""), reverse=True)

    counts = {
        "all": sum(1 for r in rows if not r["archived"]),
        "pinned": sum(1 for r in rows if r["pinned"] and not r["archived"]),
        "shared": sum(1 for r in rows if r["shared"] and not r["archived"]),
        "archived": sum(1 for r in rows if r["archived"]),
    }

    # Facet options carry the UNFILTERED tally per value, matching how every
    # other Filter menu in the app counts (see the Library's categories).
    agent_tally: dict[str, int] = {}
    for r in rows:
        agent_tally[r["agent_label"]] = agent_tally.get(r["agent_label"], 0) + 1
    agent_options = [(label.lower(), label, n) for label, n in sorted(agent_tally.items())]
    return rows, counts, agent_options


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Chats — the caller's conversations, in one manageable list.

    The rail carries New chat, the pinned shelf and a handful of recent
    conversations; past that a 240px column cannot answer "where is the analysis
    I ran three weeks ago" or "clear out everything I started and abandoned".
    This page is that surface, and it deliberately reuses the Library's
    structure — prominent search in the header, one filter/sort dock, scannable
    rows — so the two inventories read as the same product.

    Same gate as /chat: chat has to be enabled AND the caller must hold the chat
    grant; both failures bounce home rather than 403, matching the chat page (the
    rail hides the link for them too, so this guards a direct URL hit).

    Rendering is server-side; search, the four segments (All / Pinned / Shared /
    Archived), the Agent + Source facets, sort, the table ⇄ grid switch, the
    row actions and the bulk bar are all client-side over those rows
    (static/js/chats_page.js).
    """
    if not request.app.state.chat_config.enabled:
        return RedirectResponse("/")
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        return RedirectResponse("/")

    rows, counts, agent_options = _chats_rows(request, user)
    surface_tally: dict[str, tuple[str, int]] = {}
    for r in rows:
        key = r["surface"]
        label, n = surface_tally.get(key, (r["surface_label"], 0))
        surface_tally[key] = (label, n + 1)
    surface_options = [(k, label, n) for k, (label, n) in sorted(surface_tally.items())]

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        current_user=user,
        chat_rows=rows,
        chat_bucket_counts=counts,
        chat_agent_options=agent_options,
        # Only offered when it can change the list: a caller who has only ever
        # used the web composer would get a one-option category that filters
        # nothing.
        chat_surface_options=surface_options if len(surface_options) > 1 else [],
    )
    return templates.TemplateResponse(request, "chats.html", ctx)


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web chat UI — streams Claude Code sessions over WebSocket.

    Goes through ``_build_context`` so the page inherits the standard
    Agnes chrome from ``base_ds.html``: ``_app_rail.html`` (nav),
    ``static_url(...)``-resolved CSS, ``config.INSTANCE_NAME``,
    ``session.user.is_admin`` for the admin dropdown, footer copyright.
    Without this, the head's four ``<link rel="stylesheet" href="">``
    tags render with empty href and the nav block short-circuits on
    ``{% if session.user %}``.
    """
    if not request.app.state.chat_config.enabled:
        return RedirectResponse("/")
    # Cloud chat is an RBAC resource (default-deny). Non-granted users (and
    # everyone but admins until a grant exists) are bounced to home — the nav
    # link is hidden for them too, this guards a direct URL hit.
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        return RedirectResponse("/")
    # Rail pre-conversation state = the Dashboard (issue #896): greeting,
    # the real composer, a "Using N knowledge sources and M capabilities
    # from your stack" context line, activity panels, and
    # guided task starters — rendered by chat.html's rail empty-state
    # blocks and hidden the moment a conversation starts. The counts are
    # the caller's ACTUAL Stack contents (same reads as the /stack page
    # the line links to), not everything RBAC lets them browse. Best-
    # effort: a repo failure degrades them to 0 (the context line hides)
    # instead of taking down the page.
    try:
        knowledge_source_count = _stack_knowledge_source_count(user)
    except Exception:
        logger.exception("chat empty state: knowledge source count failed")
        knowledge_source_count = 0
    try:
        capability_count = _stack_capability_count(conn, user)
    except Exception:
        logger.exception("chat empty state: capability count failed")
        capability_count = 0

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        current_user=user,
        greeting=_time_of_day_greeting(),
        knowledge_source_count=knowledge_source_count,
        capability_count=capability_count,
    )
    ctx["chat_capabilities"] = _chat_capability_snapshot(conn, user)
    # Deep link: /chat?session=<id>. We DO NOT validate the id here (no
    # 404 on unknown/forbidden) — the page always renders and RBAC is
    # enforced when chat.js calls the session-scoped endpoints
    # (POST /sessions/{id}/ticket, GET /sessions/{id}/messages), which
    # carry the existing ownership guards. A bad id fails those calls and
    # surfaces an error status in the UI; the page itself still renders.
    ctx["initial_session_id"] = request.query_params.get("session")
    return templates.TemplateResponse(request, "chat.html", ctx)


def _chat_capability_snapshot(conn: duckdb.DuckDBPyConnection, user: dict) -> dict:
    """Compute the empty-state capability panel data server-side.

    The previous shape called ``/api/catalog`` + ``/api/marketplaces`` from
    JS. Those URLs were wrong (``/api/catalog`` 404s — the real endpoint is
    ``/api/catalog/tables``; ``/api/marketplaces`` is admin-only and 403s
    for normal users), so the panel always rendered "unavailable" /
    "no plugins". Resolving here side-steps both: we already have ``user``
    + ``conn`` from the route's Depends, both RBAC-filter helpers are
    sync, and rendering becomes a single round-trip with no client-side
    fetch races. JSON gets embedded by the template via ``| tojson``.
    """
    from src.rbac import get_accessible_tables
    from src.marketplace_filter import resolve_allowed_plugins

    by_source: dict[str, int] = {}
    try:
        all_tables = table_registry_repo().list_all()
        accessible_ids = get_accessible_tables(user, conn)  # None => admin/all
        allowed = None if accessible_ids is None else set(accessible_ids)
        for t in all_tables:
            if allowed is not None and t["id"] not in allowed:
                continue
            src = t.get("source_type") or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
        tables_total = sum(by_source.values())
    except Exception:
        logger.exception("chat capability snapshot: tables query failed")
        tables_total = 0
        by_source = {}

    try:
        plugins = resolve_allowed_plugins(conn, user)
        # Keep only the fields the template renders to keep the embedded
        # JSON small; ``plugin_dir`` is a Path which doesn't survive
        # ``tojson``, ``raw`` is upstream marketplace.json and can be MB.
        plugin_summaries = [
            {
                "name": p.get("manifest_name") or p.get("original_name"),
                "marketplace": p.get("marketplace_slug"),
                "tagline": (p.get("raw") or {}).get("description"),
            }
            for p in plugins
        ]
        marketplace_count = len({p["marketplace"] for p in plugin_summaries})
    except Exception:
        logger.exception("chat capability snapshot: plugins query failed")
        plugin_summaries = []
        marketplace_count = 0

    return {
        "tables_total": tables_total,
        "tables_by_source": by_source,
        "plugins": plugin_summaries,
        "marketplace_count": marketplace_count,
    }


def _stack_knowledge_source_count(user: dict) -> int:
    """Count of knowledge sources actually IN the caller's Stack — data
    packages + memory domains through the same ``StackResolver.stack()``
    reads the /stack page renders, so the number agrees with the page the
    context line links to. (Replaces the retired /ask landing count, which
    summed everything the caller could *browse* — admin god-mode counted
    every package in the instance — plus the Library surfaces; those
    numbers never matched /stack.)

    No ``conn``: like the /stack route, the resolver goes through the
    factory repos so it observes just-written subscription rows.

    Best-effort per resource type: a repo failure counting one type must
    not blank the whole line, so each block is logged rather than
    propagated.
    """
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    resolver = StackResolver()
    total = 0
    for rt in (ResourceType.DATA_PACKAGE, ResourceType.MEMORY_DOMAIN):
        try:
            total += len(resolver.stack(user["id"], rt))
        except Exception:
            logger.warning("chat empty state: stack count failed for %s", rt.value)
    return total


def _stack_capability_count(conn: duckdb.DuckDBPyConnection, user: dict) -> int:
    """Count of capabilities actually IN the caller's Stack — the same
    roster ``GET /api/marketplace/items?tab=my`` serves to the Library's
    Plugins section: curated plugins the caller subscribed to (or is
    required into via a group grant), intersected with what RBAC actually
    resolves for them, plus their Store installs. NOT
    ``resolve_allowed_plugins`` alone — that is everything the caller
    *could* add, not what's in the Stack.
    """
    from src.marketplace_filter import required_plugin_keys, resolve_allowed_plugins
    from src.repositories import user_curated_subscriptions_repo, user_store_installs_repo

    granted = resolve_allowed_plugins(conn, user)
    # Same (rbac ∩ (subscriptions ∪ required)) composition as
    # ``resolve_user_marketplace`` — but counted per item (each Store
    # install counts one), matching the ?tab=my card count.
    in_stack = user_curated_subscriptions_repo().subscribed_set(user["id"]) | required_plugin_keys(conn, user["id"])
    curated = sum(1 for p in granted if (p["marketplace_id"], p["original_name"]) in in_stack)
    store = len(user_store_installs_repo().list_for_user(user["id"]))
    return curated + store


@router.get("/ask", include_in_schema=False)
async def ask_landing(user: dict = Depends(get_current_user)):
    """Retired surface (#896). ``/ask`` was a visual-only landing hero whose
    composer just forwarded to ``/chat`` — a cosmetic doorstep in front of the
    real chat, and a dead-end for users without a chat grant. The rail IA now
    lands users on the working chat (``/chat``) or the Library, so ``/ask``
    has no job. Kept as a 302 to ``/`` (not deleted) so any bookmarked/linked
    ``/ask`` resolves through the canonical home route instead of 404ing.
    Its context-line idea lives on in ``/chat``'s empty state, now counting
    the caller's actual Stack (``_stack_knowledge_source_count`` /
    ``_stack_capability_count``) instead of everything browsable.
    """
    return RedirectResponse(url="/", status_code=302)


@router.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def _catch_all_404(request: Request, full_path: str):
    """Catch-all 404 for unmatched routes.

    Provides a matched route so fastapi-debug-toolbar can inject its panels —
    the toolbar bails out of injection when ``matched_route(request)`` is None
    (the case on truly unrouted paths). The actual rendering is delegated to
    ``app.main._html_auth_redirect_handler`` via the raised ``HTTPException``,
    which routes API paths to JSON and HTML paths to the ``error.html``
    template.
    """
    raise HTTPException(status_code=404, detail="Page not found")
