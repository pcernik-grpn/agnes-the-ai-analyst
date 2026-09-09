"""Render the agent-setup-prompt for the /setup page.

The prompt is admin-editable at /admin/agent-prompt.  When no override is
set, the default content is the live output of
``app.web.setup_instructions.resolve_lines()`` — the thin bootstrap prompt
(optional TLS trust block, CLI install, ``agnes onboard``, restart,
confirm).  When an override is saved it replaces the default everywhere:
both the /setup page display and the dashboard clipboard CTA.

Override content is a Jinja2 template (autoescape=False, StrictUndefined).
Available placeholders: instance.{name,subtitle}, server.{url,hostname},
user (may be None for anonymous visitors), now, today.

The bash default is **not** HTML-sanitized (it is bash, not HTML).  Override
content IS HTML-sanitized after render: script/iframe/event-handler strip.

See also: surfaced as the "Agent Setup Prompt" admin editor at /admin/agent-prompt.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

import duckdb
from jinja2 import TemplateError

from app.instance_config import (
    get_instance_name,
    get_instance_subtitle,
)
from src.prompt_render import make_prompt_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

_RE_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
_RE_IFRAME = re.compile(r"<iframe[\s\S]*?(?:</iframe>|/>)", re.IGNORECASE)
_RE_ON_EVENT = re.compile(r"\s+on\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_RE_JS_URI = re.compile(r"""(?:href|src|action)\s*=\s*(?:"|')(javascript:|data:)""", re.IGNORECASE)


def _sanitize_banner_html(html: str) -> str:
    """Strip dangerous constructs from admin-authored HTML.

    Defense-in-depth only — admins are trusted, but this prevents accidental
    XSS from copy-pasted snippets reaching the public /setup page.

    Strips:
    - <script>…</script> blocks (any content)
    - <iframe>…</iframe> tags
    - on*= event handler attributes (onclick=, onload=, etc.)
    - javascript: / data: URI schemes in href/src/action attributes
    """
    html = _RE_SCRIPT.sub("", html)
    html = _RE_IFRAME.sub("", html)
    html = _RE_ON_EVENT.sub("", html)
    html = _RE_JS_URI.sub(lambda m: m.group(0).replace(m.group(1), "#"), html)
    return html


# ---------------------------------------------------------------------------
# Render context
# ---------------------------------------------------------------------------


def build_context(
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> dict[str, Any]:
    """Compose the Jinja2 render context for the banner.

    Intentionally small: instance identity, server URL, and the requesting
    user (may be None for anonymous /setup visitors). No tables, metrics, or
    marketplaces — the banner is for org-operational notes, not data-catalog
    content.

    Note: ``now`` is tz-aware UTC.
    """
    now = datetime.now(UTC)
    parsed = urlparse(server_url)
    user_ctx: dict[str, Any] | None = None
    if user:
        user_ctx = {
            "id": user.get("id", ""),
            "email": user.get("email", ""),
            "name": user.get("name") or "",
            "is_admin": bool(user.get("is_admin")),
            "groups": user.get("groups") or [],
        }
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "user": user_ctx,
        "now": now,
        "today": date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# Default content — live setup script
# ---------------------------------------------------------------------------


def compute_default_agent_prompt(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> str:
    """Return the live default setup script from setup_instructions.resolve_lines().

    This is the thin bootstrap prompt that /setup shows when no admin
    override is set. The returned string is bash + prose (not HTML) —
    callers must NOT pass it through _sanitize_banner_html.

    ``conn`` and ``user`` are accepted (and kept in the signature) because
    every caller passes them and the override-resolution seam above needs
    them; the default prompt itself is caller-independent now. The
    per-caller plugin-grant resolution that used to feed the marketplace
    block is gone: ``agnes onboard`` installs plugins off the LIVE
    marketplace manifest, which is strictly fresher than a render-time
    snapshot, and the connector tiles moved into a post-install
    conversation.

    ``server_url`` is used to derive the server host and, together with the
    served TLS cert, decides whether the step-0 trust block renders.
    """
    try:
        from urllib.parse import urlparse as _urlparse

        from app.web.setup_instructions import resolve_lines

        parsed = _urlparse(server_url)
        server_host = parsed.netloc or parsed.hostname or ""

        ca_pem: str | None = None
        try:
            from app.web.router import _read_agnes_ca_pem

            ca_pem = _read_agnes_ca_pem()
        except Exception:
            pass

        from app.instance_config import (
            get_instance_brand,
            get_instance_custom_preamble,
            get_workspace_dir_name,
        )

        lines = resolve_lines(
            "agnes.whl",
            server_host=server_host,
            ca_pem=ca_pem,
            instance_brand=get_instance_brand(),
            workspace_dir=get_workspace_dir_name(),
            custom_preamble=get_instance_custom_preamble(),
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("compute_default_agent_prompt: unexpected error; returning empty")
        return ""


# ---------------------------------------------------------------------------
# Prompt renderer (override or default)
# ---------------------------------------------------------------------------


def render_agent_prompt_banner(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any] | None,
    server_url: str,
) -> str:
    """Render the agent setup prompt for the /setup page.

    When an admin override is set:
      - Renders via Jinja2 (autoescape=True, StrictUndefined).
      - HTML-sanitizes the output.
      - Returns the sanitized HTML string.

    When no override is set:
      - Returns the live default from compute_default_agent_prompt() — the
        thin bootstrap prompt.  This is bash + prose, not HTML, so no
        sanitization is applied.

    Render failures on the override path are swallowed (logged) and fall back
    to the live default so a broken template never blocks /setup.

    #622: resolution honors the install prompt's ``source_mode`` toggle —
    ``'editor'`` returns the DB override (today's behavior); ``'git'`` binds to
    the IWT clone file at the bound ``git_path``. A None result falls through
    to the live default exactly as an unset override does.
    """
    from src.initial_workspace import resolve_prompt

    content, _mode = resolve_prompt("install", conn)

    # An override written BEFORE the PAT handoff moved to `--token-file` still
    # carries the retired `{token}` placeholder, and Jinja2 leaves a single-brace
    # token untouched — so it would render literally and the user would save the
    # string `{token}` as their credential, failing every setup attempt with an
    # authentication error. The save-time guards only see NEW writes; stored
    # content is never re-validated, which is why this belongs at the render
    # seam. A template that cannot produce a working script is broken, so it
    # takes the same documented path a TemplateError does: warn, fall back to
    # the live default (Devin Review on #1139).
    if content and "{token}" in content:
        logger.warning(
            "Install-prompt override references the retired `{token}` placeholder — "
            "ignoring it and serving the built-in default. Re-save the prompt in "
            "/admin/prompts (the PAT is delivered via --token-file now)."
        )
        content = None

    if content:
        # Admin-authored override — render as Jinja2, sanitize.
        # autoescape=False to match /setup rendering — the outer Jinja2 template
        # applies escaping where needed.
        try:
            # F4: SandboxedEnvironment — this renders the same admin-authored
            # install-prompt override as /setup and must not be an SSTI sink.
            env = make_prompt_env()
            template = env.from_string(content)
            ctx = build_context(user=user, server_url=server_url)
            rendered = template.render(**ctx)
            # An override saved before the bare-placeholder save-time guard
            # shipped (or authored by hand, bypassing the editor) can still
            # carry the single-brace `{server_url}` the live default
            # substitutes outside Jinja — left alone, Jinja renders it
            # through unchanged and the reader gets the literal placeholder
            # instead of a URL. Same fix as the live default's own bind-time
            # substitution, applied here so a stored override can't regress
            # behind it.
            rendered = rendered.replace("{server_url}", server_url)
            return _sanitize_banner_html(rendered)
        except TemplateError as exc:
            logger.warning("Agent-prompt banner render failed (template error): %s", exc)
            # Fall through to default
        except Exception:
            logger.exception("Agent-prompt banner render failed (unexpected)")
            # Fall through to default

    # No override (or broken override) — return the live default prompt.
    # Same flow for everyone: the thin prompt has no per-caller branches
    # left (plugin grants are resolved by `agnes onboard` off the live
    # marketplace manifest, not baked in at render time).
    return compute_default_agent_prompt(
        conn,
        user=user,
        server_url=server_url,
    )
