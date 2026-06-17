"""Web UI routes — Jinja2 templates served by FastAPI.

Replicates all Flask webapp routes with DuckDB-backed data.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import duckdb

import jinja2

from app.auth.access import is_user_admin, require_admin
from app.web.studio import get_domain as get_studio_domain
from app.auth.dependencies import get_current_user, get_optional_user, _get_db
from app.instance_config import (
    get_instance_name,
    get_instance_subtitle,
    get_datasets,
    get_theme,
    get_corporate_memory_config,
    get_home_route,
    get_home_automode_visibility,
    get_instance_brand,
    get_workspace_dir_name,
    get_instance_logo_svg,
    get_instance_overview,
    get_instance_support,
    get_instance_theme,
    get_custom_scripts,
)
from src.repositories import (
    audit_repo,
    corpus_files_repo,
    data_packages_repo,
    file_corpora_repo,
    knowledge_repo,
    memory_domains_repo,
    news_template_repo,
    profile_repo,
    recipes_repo,
    store_entities_repo,
    store_submissions_repo,
    sync_settings_repo,
    sync_state_repo,
    table_registry_repo,
    usage_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)
from src.connectors_manifest import load_manifest
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
import json as _json


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

# Onboarding / guided-tour steps. Exposed as a Jinja global so the global
# `_tour.html` partial (included by both base layouts) can render the
# audience-filtered step list as JSON without each route having to thread it
# through its own context. The single source of truth lives in
# app/web/onboarding.py — see tests/test_onboarding_not_outdated.py.
from app.web.onboarding import steps_for as _onboarding_steps_for

templates.env.globals["onboarding_steps"] = _onboarding_steps_for


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


def _build_context(
    request: Request,
    user: Optional[dict] = None,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    **extra,
) -> dict:
    """Build template context with config, user, and theme.

    `conn` is optional: when supplied alongside a logged-in `user`, the
    setup-prompt preview/clipboard payload is rendered with that user's
    RBAC-allowed Claude Code marketplace plugins inlined as install
    commands. Routes that don't render the env-setup-cta block can omit it.
    """

    class ConfigProxy:
        INSTANCE_NAME = get_instance_name()
        INSTANCE_SUBTITLE = get_instance_subtitle()
        INSTANCE_COPYRIGHT = ""
        LOGO_SVG = get_instance_logo_svg()
        INSTANCE_OVERVIEW = get_instance_overview()
        INSTANCE_SUPPORT = get_instance_support()
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
            theme = get_theme()
            # Return dict of CSS variable overrides (only non-empty values)
            if isinstance(theme, dict):
                return {k: v for k, v in theme.items() if v}
            return {}

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
    if conn is not None:
        from src.welcome_template import render_agent_prompt_banner

        _script_text = render_agent_prompt_banner(conn, user=user, server_url=ctx_server_url)
        setup_instructions_lines = _script_text.split("\n")
    else:
        # No DB connection — use the unauthenticated default (no override possible,
        # no marketplace plugins).
        from app.web.setup_instructions import resolve_lines
        from app.api.cli_artifacts import _find_wheel

        _wheel = _find_wheel()
        _wheel_filename = _wheel.name if _wheel else "agnes.whl"

        server_host = request.url.netloc
        ca_pem = _read_agnes_ca_pem()

        # Connector manifest sourced from the seed (operator IWT clone first,
        # bundled snapshot in the wheel as fallback). Operator GWS OAuth /
        # Atlassian base URL etc. now live in `~/.claude/agnes/.env` written
        # by `agnes init`; the seed-resident SKILL.md bodies read those at
        # install time. Renderer just needs the metadata to build tiles.
        _connector_manifest = load_manifest()

        setup_instructions_lines = resolve_lines(
            _wheel_filename,
            plugin_install_names=[],
            server_host=server_host,
            ca_pem=ca_pem,
            connector_manifest=_connector_manifest,
            instance_brand=get_instance_brand(),
            workspace_dir=get_workspace_dir_name(),
        )

    ctx = {
        "request": request,
        "config": ConfigProxy,
        "user": _flex(user) if user else _FlexDict(),
        "now": datetime.now,
        "static_url": _static_url,
        # Flask compatibility shims for templates
        "get_flashed_messages": lambda **kwargs: [],
        "url_for": lambda endpoint, **kw: _url_for_shim(endpoint, **kw),
        "session": _FlexDict({"user": user}) if user else _FlexDict(),
        "setup_instructions_lines": setup_instructions_lines,
        "server_url": ctx_server_url,
        # Resolved per AGNES_HOME_ROUTE env > instance.home_route YAML >
        # /dashboard. The shared navbar's "Dashboard" link uses this so a
        # single env flip routes the primary nav target between /home
        # (state-aware landing) and /dashboard (legacy table inventory).
        "home_route": _resolved_home_route(),
        # Branding: `instance_name` is the deploying org's display name
        # (page titles); `instance_brand` is the product name used in body
        # copy and CTAs ("Setup {brand}", "{brand} runs SELECT…"); `workspace_dir`
        # is the filesystem-safe folder name shown in `~/<workspace_dir>` and
        # baked into the clipboard setup script. All three default to the
        # Agnes-flavored values out of the box; Terraform can flip them via
        # env vars (AGNES_INSTANCE_BRAND / AGNES_WORKSPACE_DIR_NAME).
        "instance_name": get_instance_name(),
        "instance_brand": get_instance_brand(),
        "workspace_dir": get_workspace_dir_name(),
        # Active palette — drives `<html data-theme="...">` in
        # base.html so `--ds-*` tokens flip via CSS without
        # touching markup. "blue" (default) = brand-blue palette;
        # "navy" = darker opt-in palette. Admin toggles via
        # /admin/server-config.
        "instance_theme": get_instance_theme(),
        # Whether /home renders the "Step 3 — turn on auto-accept mode"
        # install-block. Operator can hide it via AGNES_HOME_SHOW_AUTOMODE=0
        # for cautious rollouts; same content stays on /setup-advanced.
        "home_automode": {"show": get_home_automode_visibility()},
        # Operator-injected HTML/JS blocks rendered into base.html at
        # head_start / head_end / body_end. Admin-only (instance.yaml,
        # gated by require_admin) — used for feedback widgets
        # (Marker.io), analytics, error capture. Empty default keeps
        # the OSS vendor-neutral.
        "custom_scripts": get_custom_scripts(),
    }
    # Cloud-chat nav visibility. The /chat link is shown only when chat is
    # enabled AND one of the viewer's groups holds an explicit chat grant. We
    # deliberately use `has_explicit_grant` (NOT `can_access`) so the link
    # tracks actual rollout state, not effective access: admins do NOT see it
    # until chat is granted to a group they're in, even though god-mode still
    # lets them reach /chat by URL (the route guard uses can_access). This is
    # UX only — the hard gate is on the route + API.
    #
    # Computed on EVERY page. `has_explicit_grant` is backend-aware (it routes
    # through the repo factory), so no connection is threaded here — it reads
    # the active backend itself. Defaults False when chat is disabled or
    # there's no user.
    ctx["can_chat"] = False
    try:
        _cc = getattr(request.app.state, "chat_config", None)
        if user and _cc is not None and _cc.enabled:
            from app.auth.access import has_explicit_grant
            from app.resource_types import ResourceType

            ctx["can_chat"] = bool(has_explicit_grant(user["id"], ResourceType.CHAT.value, "chat"))
    except Exception:
        ctx["can_chat"] = False
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


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    from app.auth.dependencies import is_local_dev_mode, _get_local_dev_user

    if is_local_dev_mode():
        # Only short-circuit to the home route if the dev user is actually
        # seeded. Otherwise a 401 there would bounce back to /login and loop.
        from src.db import get_system_db

        conn = get_system_db()
        try:
            if _get_local_dev_user(conn):
                return RedirectResponse(url=get_home_route(), status_code=302)
        finally:
            conn.close()
        # Fall through to the normal login form so the missing-seed error is visible.

    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    providers = []
    try:
        from app.auth.providers.google import is_available as google_available

        if google_available():
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    try:
        from app.auth.providers.email import is_available as email_available

        if email_available():
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
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

    ctx = _build_context(request, providers=providers, login_buttons=login_buttons, next_path=next_path)
    return templates.TemplateResponse(request, "login.html", ctx)


@router.get("/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request):
    """Password login form (email + password)."""
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available

        google_ok = google_available()
    except Exception:
        pass
    ctx = _build_context(request, google_available=google_ok, next_path=next_path)
    return templates.TemplateResponse(request, "login_email.html", ctx)


@router.get("/login/email", response_class=HTMLResponse)
async def login_email_page(request: Request):
    """Email magic link login form."""
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available

        google_ok = google_available()
    except Exception:
        pass
    ctx = _build_context(request, google_available=google_ok, next_path=next_path)
    return templates.TemplateResponse(request, "login_email.html", ctx)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    sync_repo = sync_state_repo()
    settings_repo = sync_settings_repo()
    profiles = profile_repo()

    all_states = sync_repo.get_all_states()
    enabled_datasets = settings_repo.get_enabled_datasets(user["id"])
    datasets = get_datasets()

    # Stats. `total_tables` counts REGISTERED business tables, not synced
    # ones (a registry of 30 with 0 ever synced would otherwise render as
    # "0"). Internal source_type tables (agnes_*) live in their own card on
    # /catalog and are excluded from the headline counter. Columns + size
    # come from sync_state, which is the canonical source for "what's
    # actually on disk locally".
    total_tables = conn.execute(
        "SELECT COUNT(*) FROM table_registry WHERE COALESCE(source_type, '') != 'internal'"
    ).fetchone()[0]
    total_rows = sum(s.get("rows", 0) or 0 for s in all_states)
    total_columns = sum(s.get("columns", 0) or 0 for s in all_states)
    total_size_bytes = sum(s.get("file_size_bytes", 0) or 0 for s in all_states)

    # Build user_info object expected by dashboard template
    is_admin = is_user_admin(user["id"], conn)

    class UserInfo:
        def __init__(self):
            self.exists = True
            self.is_admin = is_admin
            # Legacy fields kept so existing templates don't blow up — admin is
            # implicitly analyst/privileged, non-admins are not. Granular roles
            # collapsed in v12.
            self.is_analyst = is_admin
            self.is_privileged = is_admin
            self.username = user.get("email", "").split("@")[0]
            self.home_dir = ""
            self.groups = []

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        user_info=UserInfo(),
        username=user.get("email", "").split("@")[0],
        total_tables=total_tables,
        total_rows=total_rows,
        sync_states=all_states,
        enabled_datasets=enabled_datasets,
        datasets=datasets,
        account_status="active",
        account_details=None,
        telegram_status={"linked": False},
        data_stats={
            "tables": total_tables,
            "total_tables": total_tables,
            "columns": total_columns,
            "rows_display": f"{total_rows:,}" if total_rows else "0",
            "size_display": _humanbytes(total_size_bytes, precision=1) if total_size_bytes else "0 MB",
            "total_rows": total_rows,
            "last_updated": max(
                (s.get("last_sync") for s in all_states if s.get("last_sync")),
                default=None,
            ),
            "remote_tables": 0,
            "local_tables": total_tables,
        },
        categories=[],
        metrics_data=[],
        desktop_status={"linked": False},
        activity_summary={"total_sessions": 0, "total_queries": 0},
        knowledge_stats={"total": 0, "approved": 0},
        user_knowledge_stats={"authored": 0, "votes_given": 0},
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


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
    home_stats = compute_home_stats(conn, user, "24h") if (status_frame_enabled and onboarded) else None

    # Single template renders both states. The post-onboarding view keeps
    # the install-steps + connector prompts + auto-mode card visible —
    # they stay relevant for adding a second machine, a missing connector,
    # or re-running auto-mode setup. Hero copy + the self-mark control
    # branch on the boolean. The legacy `home_onboarded.html` is kept on
    # disk for a release as a fallback but no route renders it.
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


@router.get("/me/cowork", response_class=HTMLResponse)
async def me_cowork_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """User-facing AI Cowork page: setup bundle, MCP connection info, and available tools."""
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.api.v2_marketplace import _accessible_plugins, _skills_for_plugin
    from src.repositories import mcp_sources_repo

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

    static_tools = [
        {"name": "server_info", "description": "Check Agnes connectivity and your account email."},
        {"name": "catalog", "description": "List all tables available to you — name, query_mode, row count."},
        {"name": "schema", "description": "Show column names and types for a table."},
        {"name": "describe", "description": "Schema + sample rows for a table in one call."},
        {"name": "query", "description": "Execute SQL against Agnes data (DuckDB or BigQuery dialect)."},
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
    return templates.TemplateResponse(request, "me_cowork.html", ctx)


@router.get("/me/mcp", response_class=HTMLResponse)
async def me_mcp_redirect(request: Request):
    """Legacy redirect — /me/mcp → /me/cowork."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/me/cowork", status_code=301)


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


def _data_package_entry_dict(
    entry, drilldown_url: str, table_count: int = 0, source_types: Optional[list] = None, is_admin_view: bool = False
) -> dict:
    """Adapt a ResourceEntry → template entry dict for the _stack_card macro.

    Always renders a meta line (`N tables` — even `0 tables`) and a
    description fallback so packages without an admin-authored
    description don't render as half-empty cards.

    Empty-package CTA: when ``table_count == 0`` AND the viewer is admin,
    the meta line becomes an inline link to ``/admin/tables?assign_to=<id>``
    so admins can jump straight into the bulk-assign flow without first
    having to discover the chip-input hidden in each table's edit modal.
    """
    description = entry.description or (
        f"Bundle of {table_count} table{'s' if table_count != 1 else ''}. "
        f"Add to your stack so `agnes pull` syncs the data locally."
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
        "in_stack": entry.in_stack,
        "meta": f"{table_count} table{'s' if table_count != 1 else ''}",
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
        "drilldown_url": drilldown_url,
        "footer_left": (f"View {table_count} table{'s' if table_count != 1 else ''} →" if table_count else "Open →"),
    }
    if table_count == 0 and is_admin_view:
        # `entry.id` is a server-generated uuid (data_packages.id), safe to
        # inline. `assign_to` is read by admin_tables.html on load to auto-
        # open the Bulk Assign modal with this package pre-selected.
        out["meta_html"] = (
            f'0 tables — <a href="/admin/tables?assign_to={entry.id}" style="color:#0073D1;">assign some →</a>'
        )
    return out


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # v49 — unified Browse + My Stack tabs (Task 8.2). The old per-source
    # source-card / per-table list moved into /catalog/p/<slug> (Task 8.3).
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    resolver = StackResolver(conn)
    pkg_repo = data_packages_repo()

    # Pre-compute per-package table counts + source-type tag set in one pass
    # so we don't repeat the join per card.
    pkg_meta: dict[str, dict] = {}
    try:
        for pkg in pkg_repo.list():
            tables = pkg_repo.list_tables(pkg["id"])
            source_types = sorted({(t.get("source_type") or "") for t in tables if t.get("source_type")})
            pkg_meta[pkg["id"]] = {
                "table_count": len(tables),
                "source_types": source_types,
            }
    except Exception as e:
        logger.warning("could not enumerate data_packages: %s", e)

    is_admin_view = is_user_admin(user["id"], conn)
    if is_admin_view:
        # Admin god-mode for BROWSE only: surface every package regardless
        # of group grants so admins can audit the full set. ``browse_admin``
        # runs the same v51/v56 enrichment pass as ``browse`` (status,
        # category, owner_name, tags, derived badges) — re-implementing
        # it inline silently dropped those fields, leaving admin cards
        # empty of v56 chrome. For MY STACK we still call the resolver —
        # admins legitimately subscribe to packages and expect to see them
        # in their stack tab.
        browse_entries = resolver.browse_admin(user["id"], ResourceType.DATA_PACKAGE)
        stack_entries = resolver.stack(user["id"], ResourceType.DATA_PACKAGE)
    else:
        browse_entries = resolver.browse(user["id"], ResourceType.DATA_PACKAGE)
        stack_entries = resolver.stack(user["id"], ResourceType.DATA_PACKAGE)

    # Group ``required`` packages first so they cluster together at the
    # top of the Browse grid instead of being scattered by creation
    # order — first-demo feedback (2026-05-19): "bylo by dobre ty
    # required mit vzdy nekde seskupene spolu na jedne strane".
    # Secondary order falls back to the resolver's name-ordered output.
    browse_entries = sorted(
        browse_entries,
        key=lambda e: (0 if e.requirement == "required" else 1, e.name or ""),
    )

    def _adapt(e):
        slug = None
        try:
            full = pkg_repo.get(e.id)
            if full:
                slug = full.get("slug")
        except Exception:
            slug = None
        meta = pkg_meta.get(e.id, {})
        return _data_package_entry_dict(
            e,
            drilldown_url=f"/catalog/p/{slug}" if slug else f"/catalog#{e.id}",
            table_count=meta.get("table_count", 0),
            source_types=meta.get("source_types", []),
            is_admin_view=is_admin_view,
        )

    entries = [_adapt(e) for e in browse_entries]
    stack_entries_adapted = [_adapt(e) for e in stack_entries]

    # Aggregate distinct source types across the user's visible packages —
    # drives the per-source chip row in catalog.html.
    source_type_chips = sorted({st for e in entries for st in (e.get("tags") or [])})

    # Empty-state hint: when no packages exist, the page tells admins how
    # many tables are already registered (so the CTA "go to /admin/tables
    # and group them" lands with concrete context). Non-internal tables
    # only — the agnes_* internal rows aren't analyst-facing.
    total_registered_tables = 0
    try:
        total_registered_tables = conn.execute(
            "SELECT COUNT(*) FROM table_registry WHERE COALESCE(source_type, '') != 'internal'"
        ).fetchone()[0]
    except Exception:
        total_registered_tables = 0

    # Direct (unbundled) tables on /catalog were dropped per user feedback:
    # "nemít Direct Tables zvlášť. Potřebujeme to mít celé v nějaké
    # skupině v těch data packages." Everything an analyst sees here must
    # belong to a Data Package — admin's job is to package unbundled
    # tables via Group-by-bucket (one-click) or Bulk-assign on
    # /admin/tables. The manifest endpoint at /api/sync/manifest still
    # emits `direct_tables[]` so existing CLI clients with `table`-typed
    # RBAC grants keep working (BC, not a web surface).

    ctx = _build_context(
        request,
        user=user,
        entries=entries,
        stack_entries=stack_entries_adapted,
        source_type_chips=source_type_chips,
        total_registered_tables=total_registered_tables,
    )
    return templates.TemplateResponse(request, "catalog.html", ctx)


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
    if not (
        is_user_admin(user["id"], conn) or can_access(user["id"], ResourceType.DATA_PACKAGE.value, pkg["id"], conn)
    ):
        raise HTTPException(status_code=403, detail="access_denied")

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
    in_stack = effective_required or bool(
        conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = 'data_package' AND resource_id = ?",
            [user["id"], pkg["id"]],
        ).fetchone()
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

    # v56 virtual badges. Derived in-template-aware here so the router
    # owns the policy (creator-in-admin + 30-day window) and the
    # template stays presentational.
    from datetime import datetime, timedelta, timezone as _tz

    badges: list[str] = []
    created_by = pkg.get("created_by")
    if created_by:
        # Backend-aware (mirrors data_packages._badges_for): resolve creator +
        # Admin membership through the factory, not a raw DuckDB conn — the
        # JOIN was empty on a Postgres instance so the badge silently vanished.
        # is_user_admin is module-imported (line 20); no local import (would
        # shadow it and break the earlier access check in this function).
        u = users_repo().get_by_id(created_by) or users_repo().get_by_email(created_by)
        if u and is_user_admin(u["id"]):
            badges.append("curated")
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


@router.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Library — the caller's accessible file Collections (bring-your-files)."""
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    is_admin = is_user_admin(user["id"], conn)
    cf_repo = corpus_files_repo()
    cards = []
    for col in file_corpora_repo().list():
        if not is_admin and not can_access(user["id"], ResourceType.COLLECTION.value, col["id"], conn):
            continue
        files = cf_repo.list_for_corpus(col["id"])
        cards.append({**col, "file_count": len(files)})
    ctx = _build_context(request, user=user, conn=conn, is_admin=is_admin, collections=cards)
    return templates.TemplateResponse(request, "library.html", ctx)


@router.get("/library/{slug}", response_class=HTMLResponse)
async def library_detail(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Collection detail — files + per-file processing status + search box."""
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    col = file_corpora_repo().get_by_slug(slug)
    # Return 404 for both "missing" and "access denied" so an unprivileged
    # caller can't distinguish the two and probe for collection existence
    # (matches the GET /api/collections/{id} contract).
    if not col:
        raise HTTPException(status_code=404, detail="collection_not_found")
    is_admin = is_user_admin(user["id"], conn)
    if not is_admin and not can_access(user["id"], ResourceType.COLLECTION.value, col["id"], conn):
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(col["id"])
    ctx = _build_context(request, user=user, conn=conn, is_admin=is_admin, collection=col, files=files)
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
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    table_repo = table_registry_repo()
    table = table_repo.get(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="table_not_found")

    # Find every package that includes this table; gate access on
    # admin god-mode OR a grant on ANY of those packages.
    pkg_repo = data_packages_repo()
    parent_packages = []
    is_admin = is_user_admin(user["id"], conn)
    has_grant = False
    try:
        # Walk packages (instances are small enough that this is fine).
        for p in pkg_repo.list(limit=10000):
            mem_ids = {t["id"] for t in pkg_repo.list_tables(p["id"])}
            if table_id not in mem_ids:
                continue
            parent_packages.append({"slug": p["slug"], "name": p["name"]})
            if not has_grant and not is_admin:
                if can_access(user["id"], ResourceType.DATA_PACKAGE.value, p["id"], conn):
                    has_grant = True
    except Exception:
        logger.warning("could not enumerate parent packages for %s", table_id)
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
        prof_row = conn.execute(
            "SELECT profile FROM table_profiles WHERE table_id = ?",
            [table_id],
        ).fetchone()
        if prof_row and prof_row[0]:
            import json as _json

            prof = _json.loads(prof_row[0]) if isinstance(prof_row[0], str) else prof_row[0]
            for col in prof.get("columns") or []:
                columns.append(
                    {
                        "name": col.get("name"),
                        "type": col.get("type"),
                        "nullable": col.get("nullable", True),
                    }
                )
    except Exception:
        logger.warning("could not load profile for %s", table_id)

    # Fallback: when table_profiles has no row (table never synced, or
    # profile was wiped), introspect schema via the same code path the
    # /api/v2/schema endpoint uses. Handles every source type — internal
    # via connectors.internal, BigQuery remote via the BQ extension,
    # local + materialized via DESCRIBE on the parquet. Best-effort —
    # any failure (parquet missing, BQ creds absent, etc.) leaves the
    # columns section in its "run a sync" empty state.
    if not columns:
        try:
            from app.api.v2_schema import build_schema_uncached
            from connectors.bigquery.access import BqAccess

            sch = build_schema_uncached(conn, table_id, bq=BqAccess(), row=table)
            for col in sch.get("columns") or []:
                columns.append(
                    {
                        "name": col.get("name"),
                        "type": col.get("type"),
                        "nullable": col.get("nullable", True),
                    }
                )
        except Exception:
            logger.warning("schema introspection fallback failed for %s", table_id)

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


def _memory_domain_entry_dict(entry, drilldown_url: str, items_count: int = 0, required_count: int = 0) -> dict:
    """Adapt a ResourceEntry (memory_domain) → template entry dict.

    Always renders a meta line (`N items · K required` — even `0 items`)
    and a description fallback so seeded canonical domains without an
    admin-authored description don't render as half-empty cards.
    """
    meta = f"{items_count} item{'s' if items_count != 1 else ''}"
    if required_count:
        meta += f" · {required_count} required"
    description = entry.description or (
        f"Curated knowledge for the {entry.name} domain. Add to your stack to include items in agnes pull."
    )
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
        "in_stack": entry.in_stack,
        "meta": meta,
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

    resolver = StackResolver(conn)
    domains_repo = memory_domains_repo()
    repo = knowledge_repo()

    # Per-domain counts (items + required) computed once and indexed by id.
    dom_meta: dict[str, dict] = {}
    try:
        for d in domains_repo.list(limit=10000):
            summaries = domains_repo.list_items_of_domain(d["id"], limit=10000)
            item_ids = [s["id"] for s in summaries]
            required = 0
            if item_ids:
                placeholders = ",".join(["?"] * len(item_ids))
                required = conn.execute(
                    f"SELECT COUNT(*) FROM knowledge_items WHERE id IN ({placeholders}) AND is_required = TRUE",
                    item_ids,
                ).fetchone()[0]
            dom_meta[d["id"]] = {
                "items_count": len(summaries),
                "required_count": required,
                "slug": d["slug"],
            }
    except Exception as e:
        logger.warning("could not enumerate memory_domains: %s", e)

    is_admin_view = is_user_admin(user["id"], conn)

    # Admin god-mode for BROWSE only: surface every domain regardless of
    # group grants so admins can audit the full set. ``browse_admin`` runs
    # the v51 enrichment pass (status) plus v56 derived badges so admin
    # cards stay visually consistent with non-admin browse. For MY STACK
    # we still call the resolver — admins who POST /api/stack/subscribe
    # expect to see those subscriptions in their stack tab.
    if is_admin_view:
        browse_entries = resolver.browse_admin(user["id"], ResourceType.MEMORY_DOMAIN)
        stack_entries = resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN)
    else:
        browse_entries = resolver.browse(user["id"], ResourceType.MEMORY_DOMAIN)
        stack_entries = resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN)

    # Required-first grouping mirrors /catalog (first-demo feedback).
    browse_entries = sorted(
        browse_entries,
        key=lambda e: (0 if e.requirement == "required" else 1, e.name or ""),
    )

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

    entries = [_adapt(e) for e in browse_entries if _has_content(e)]
    stack_entries_adapted = [_adapt(e) for e in stack_entries if _has_content(e)]

    # Pending banner contract (issue #176) — admin-only, counts items in
    # status='pending'. Kept identical to the legacy route so the page test
    # (test_corporate_memory_page.py) keeps passing.
    pending_count = 0
    if is_admin_view:
        try:
            pending_count = conn.execute("SELECT COUNT(*) FROM knowledge_items WHERE status = 'pending'").fetchone()[0]
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
    in_stack = effective_required or bool(
        conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = 'memory_domain' AND resource_id = ?",
            [user["id"], domain["id"]],
        ).fetchone()
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
    )
    return templates.TemplateResponse(request, "memory_domain_detail.html", ctx)


def _chrome_ctx(request: Request, user: Optional[dict]) -> dict:
    """Base context every base_ds page needs for the shared nav + footer chrome.

    Routes that render ``base_ds.html`` MUST spread this in — otherwise the
    navbar, theme, branding, and url helpers render empty (the studio pages
    regressed on exactly this: no top menu, no styling). Mirrors the canonical
    context the home/setup routes build.
    """
    return {
        "request": request,
        "user": _flex(user) if user else _FlexDict(),
        "is_admin": bool(user) and is_user_admin(user.get("id")),
        "now": datetime.now,
        "get_flashed_messages": lambda **kw: [],
        "url_for": lambda endpoint, **kw: _url_for_shim(endpoint, **kw),
        "session": _FlexDict({"user": user}) if user else _FlexDict(),
        "home_route": _resolved_home_route(),
        "instance_name": get_instance_name(),
        "instance_brand": get_instance_brand(),
        "workspace_dir": get_workspace_dir_name(),
        "instance_theme": get_instance_theme(),
        "home_automode": {"show": get_home_automode_visibility()},
        "custom_scripts": get_custom_scripts(),
    }


@router.get("/me/memory-mining", response_class=HTMLResponse)
async def me_memory_mining(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """User-facing privacy control: opt in/out of having one's own session
    transcripts mined into shared corporate memory (design spec §4.4)."""
    return templates.TemplateResponse(request, "me_memory_mining.html", _chrome_ctx(request, user))


@router.get("/admin/studio/suggestions", response_class=HTMLResponse)
async def studio_suggestions_admin(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin moderation queue for authoring-studio suggestions.

    Registered BEFORE ``/admin/studio/{domain}`` so the static ``suggestions``
    path wins over the dynamic domain matcher.
    """
    return templates.TemplateResponse(request, "admin_studio_suggestions.html", _chrome_ctx(request, user))


@router.get("/admin/studio/{domain}", response_class=HTMLResponse)
async def studio(
    domain: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Authoring-agent studio — available to all signed-in users.

    A generic form-based builder with an embedded assistant panel. The domain
    config (``app/web/studio.py``) drives the fields, the chat profile, and the
    create endpoint, so all four authoring agents share one surface. Admins
    create directly; non-admins submit a suggestion to the moderation queue
    (the page renders the right action via ``is_admin``).
    """
    spec = get_studio_domain(domain)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown studio domain")
    return templates.TemplateResponse(
        request,
        "admin_studio.html",
        {**_chrome_ctx(request, user), "domain": spec, "profile_slug": spec.profile},
    )


@router.get("/admin/corporate-memory", response_class=HTMLResponse)
async def corporate_memory_admin(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
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
    duplicates_count = conn.execute(
        "SELECT COUNT(*) FROM knowledge_item_relations WHERE relation_type = 'likely_duplicate' AND resolved = FALSE"
    ).fetchone()[0]

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
        governance=get_corporate_memory_config(),
        groups=user_groups_for_ui,
        edit_categories=edit_categories,
        edit_tags=edit_tags,
        contradictions=contradictions,
        audit_entries=[],
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
    from jinja2 import Environment, StrictUndefined, TemplateError

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

            env = Environment(undefined=StrictUndefined, autoescape=False)
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


@router.get("/slack/bind", response_class=HTMLResponse)
async def slack_bind(
    request: Request,
    code: str = "",
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """One-click Slack identity binding — the magic-link Agnes DMs an unbound
    user. Opening ``/slack/bind?code=<code>`` while signed in to Agnes redeems
    the code server-side and stamps ``users.slack_user_id`` (no copy-paste).

    The code in the URL is inert on its own: this route is auth-gated, so the
    bind only completes for the signed-in Agnes account — proving the same
    person controls both the Slack identity (the code was DM'd to them) and the
    Agnes account (they're logged in). An unauthenticated visitor is bounced to
    sign-in and lands back here afterwards (``next=``).
    """
    if user is None:
        nxt = quote(f"/slack/bind?code={code}", safe="")
        return RedirectResponse(url=f"/login?next={nxt}", status_code=302)

    status = "missing"
    if code:
        from services.slack_bot.binding import (
            BindingThrottled,
            redeem_verification_code,
        )

        try:
            ok = redeem_verification_code(conn, user_email=user["email"], code=code.strip())
            status = "ok" if ok else "invalid"
        except BindingThrottled:
            status = "throttled"
        except Exception:
            logger.exception("slack bind redeem failed")
            status = "error"

    ctx = _build_context(request, user=user, conn=conn, bind_status=status)
    return templates.TemplateResponse(request, "slack_bind.html", ctx)


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
    ctx = _build_context(
        request,
        user=user,
        categories=list(STORE_CATEGORIES),
        guardrail=_guardrail_thresholds(),
        title_acronyms=TITLE_ACRONYMS,
        owner_username=owner_username,
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
    import json as _json
    from src.category_icons import all_paths
    from app.instance_config import get_value

    curators_url = (get_value("marketplace", "curators_url") or "").strip()
    ctx = _build_context(
        request,
        user=user,
        category_icons_json=_json.dumps(all_paths()),
        curators_url=curators_url,
    )
    return templates.TemplateResponse(request, "marketplace.html", ctx)


@router.get("/marketplace/flea/{entity_id}", response_class=HTMLResponse)
async def marketplace_flea_detail(
    request: Request,
    entity_id: str,
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

    common = dict(
        source="flea",
        entity=entity,
        entity_id=entity_id,
        is_owner=is_owner,
        is_admin=is_admin,
        quarantine_sub=quarantine_sub,
        edit_in_flight=edit_in_flight,
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


@router.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from app.instance_config import get_data_source_type

    repo = table_registry_repo()
    tables = repo.list_all()
    # Branch the register-modal layout server-side so the JS doesn't have
    # to round-trip /api/admin/server-config to learn the source type.
    data_source_type = get_data_source_type() or "keboola"
    ctx = _build_context(
        request,
        user=user,
        registered_tables=tables,
        data_source_type=data_source_type,
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
    ctx["groups"] = user_groups_repo().list_all()
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
async def admin_groups_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Group list view — full-width table of user_groups with origin chips,
    member/grant counts, and edit/delete affordances for non-system rows."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_groups.html", ctx)


@router.get("/admin/groups/{group_id}", response_class=HTMLResponse)
async def admin_group_detail_page(
    group_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Single-group detail page — header + members table. Resource grants
    live on /admin/grants (deep-linked from here)."""
    from app.api.access import _is_google_managed, _mapped_email

    g = user_groups_repo().get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    # Project the same flags the API derives so the template avoids env
    # lookups: `is_google_managed` (created_by='system:google-sync' OR
    # system + env mapping) and `mapped_email` (the Workspace group
    # funneling members into the Admin/Everyone system row, when set).
    g_view = dict(g)
    g_view["is_google_managed"] = _is_google_managed(g)
    g_view["mapped_email"] = _mapped_email(g)
    ctx = _build_context(request, user=user, target_group=g_view)
    return templates.TemplateResponse(request, "admin_group_detail.html", ctx)


@router.get("/admin/access", response_class=HTMLResponse)
async def admin_access_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Resource access management — master-detail layout with the group list
    on the left and per-resource-type checkbox tree on the right. Supports
    ``?group=<id>`` deep-link from the group detail page.

    Underlying entity is `resource_grants`; the UI label "Resource access"
    matches what admins think about (who has access) rather than the table
    name (grants)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_access.html", ctx)


@router.get("/admin/grants", response_class=HTMLResponse)
async def admin_grants_redirect(request: Request):
    """Backward-compat redirect for the page's previous URL."""
    qs = request.url.query
    target = "/admin/access" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=308)


@router.get("/admin/marketplaces", response_class=HTMLResponse)
async def admin_marketplaces_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for marketplace git repositories (register / sync / delete)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_marketplaces.html", ctx)


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
            # Direct query — list_for_admin doesn't filter by entity_id
            # and we don't want to add a parameter for this one display
            # need. Order by created_at DESC so newest is first in the
            # switcher.
            ent_sub_rows = [
                dict(zip(["id", "status", "version", "created_at", "reviewed_by_model"], row))
                for row in conn.execute(
                    "SELECT id, status, version, created_at, reviewed_by_model "
                    "FROM store_submissions "
                    "WHERE entity_id = ? "
                    "ORDER BY created_at DESC",
                    [sub["entity_id"]],
                ).fetchall()
            ]
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
    rows = conn.execute(
        """SELECT g.id, g.name, g.description, g.is_system, g.created_by,
                  m.source, m.added_at
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?
           ORDER BY g.is_system DESC, g.name""",
        [user["id"]],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    memberships = [dict(zip(cols, r)) for r in rows]
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

    ctx = _build_context(
        request,
        user=user,
        memberships=memberships,
        is_admin=is_user_admin(user["id"], conn),
        user_record=user_record_safe,
        claims=_decoded_claims(raw_token),
        token_fingerprint=_token_fingerprint(raw_token),
        sync_summary=_last_sync_summary(user["id"], conn),
        # Display-only — keep original case (no .lower()), unlike the
        # refetch-groups handler below which lowercases for set comparison.
        google_group_prefix=os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip(),
    )
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/me/profile/refetch-groups", name="me_profile_refetch_groups")
async def me_profile_refetch_groups(
    _: None = Depends(require_debug_auth_enabled),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-issue ``fetch_user_groups`` for the current user and return a
    dry-run diff against the cached ``user_group_members`` snapshot,
    writing nothing. Gated behind AGNES_DEBUG_AUTH — a dry-run admin
    debug action, not user-facing content."""
    from app.auth.group_sync import fetch_user_groups

    fetched = fetch_user_groups(user["email"])
    soft_failed = fetched is None
    fetched_list = list(fetched) if fetched else []

    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    if prefix:
        relevant = [g.lower() for g in fetched_list if g.lower().startswith(prefix)]
    else:
        relevant = [g.lower() for g in fetched_list]

    has_ext = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'user_groups' AND column_name = 'external_id'"
    ).fetchone()
    select_ext = "g.external_id" if has_ext else "NULL"
    current_rows = conn.execute(
        f"""SELECT g.name, {select_ext} AS external_id
              FROM user_group_members m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = ? AND m.source = 'google_sync'
             ORDER BY g.name""",
        [user["id"]],
    ).fetchall()
    current_external_ids = {r[1].lower() for r in current_rows if r[1]}
    current_names = [r[0] for r in current_rows]

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


@router.get("/help/cowork", response_class=HTMLResponse)
async def cowork_help(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Step-by-step guide for the Connect Claude Code (Agnes Cowork) setup flow."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "cowork_help.html", ctx)


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


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web chat UI — streams Claude Code sessions over WebSocket.

    Goes through ``_build_context`` so the page inherits the standard
    Agnes chrome from ``base_ds.html``: ``_app_header.html`` (nav),
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
    ctx = _build_context(request, user=user, conn=conn, current_user=user)
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
    from src.rbac import can_access_table
    from src.marketplace_filter import resolve_allowed_plugins

    by_source: dict[str, int] = {}
    try:
        all_tables = table_registry_repo().list_all()
        for t in all_tables:
            if not can_access_table(user, t["id"], conn):
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
