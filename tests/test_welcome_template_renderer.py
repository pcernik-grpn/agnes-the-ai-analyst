"""Unit tests for the agent-setup-prompt renderer."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import (
    _sanitize_banner_html,
    build_context,
    compute_default_agent_prompt,
    render_agent_prompt_banner,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def _user(email="alice@example.com"):
    return {
        "id": "u1",
        "email": email,
        "name": "Alice",
        "is_admin": False,
        "groups": ["Everyone"],
    }


# ---------------------------------------------------------------------------
# Default (no override) → live setup script, not empty string
# ---------------------------------------------------------------------------


def test_returns_default_script_when_no_override(conn):
    """When no override is set, render_agent_prompt_banner returns the live
    thin setup prompt (not an empty string). Every caller — admin or
    non-admin — sees the same four steps: the prompt has no per-caller
    branch left, because `agnes onboard` resolves plugins and connectors at
    run time off the live manifest.
    """
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    # Must be non-empty — the default IS the setup prompt
    assert out != ""
    # Thin layout: `agnes onboard` is the one orchestration call.
    assert "agnes onboard" in out
    # Superseded verbs are gone — `agnes onboard` subsumes them.
    assert "agnes init" not in out
    assert "agnes auth import-token" not in out
    assert "agnes auth whoami" not in out
    # No legacy verb anywhere in the rendered default
    assert "da analyst setup" not in out
    assert "da sync" not in out


def test_compute_default_returns_setup_script(conn):
    """compute_default_agent_prompt returns a non-empty string with the
    thin setup-prompt markers, including the {server_url} placeholder and
    the `agnes onboard` line.
    """
    out = compute_default_agent_prompt(conn, user=_user(), server_url="https://example.com")
    assert out != ""
    # {server_url} placeholder must survive (not replaced by Jinja2)
    assert "{server_url}" in out
    # Thin layout: install + onboard are always present.
    assert "agnes onboard" in out
    assert "uv tool install" in out
    # Superseded verbs replaced by `agnes onboard`.
    assert "agnes init" not in out
    assert "agnes auth import-token" not in out
    assert "agnes auth whoami" not in out
    # No legacy verb anywhere in the rendered default
    assert "da analyst setup" not in out


def test_compute_default_server_url_placeholder_survives(conn):
    """{server_url} is a single-brace JS placeholder. compute_default_agent_prompt
    must NOT replace it — it stays literal. The access token is delivered
    out-of-band (never a placeholder in the prompt body — see
    app/web/setup_instructions.py's module docstring), so `{token}` must
    never appear."""
    out = compute_default_agent_prompt(conn, user=_user(), server_url="https://example.com")
    assert "{server_url}" in out
    assert "{token}" not in out


def test_returns_empty_for_none_user_with_no_override(conn):
    """Anonymous visitor with no override → still returns the default script."""
    out = render_agent_prompt_banner(conn, user=None, server_url="https://example.com")
    # No override → default (non-empty bash script)
    assert out != ""


# ---------------------------------------------------------------------------
# Override renders correctly
# ---------------------------------------------------------------------------


def test_renders_override(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Welcome to {{ instance.name }}!</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    assert "<p>Welcome to" in out
    # instance.name comes from instance_config — any non-empty string is fine
    assert "!" in out


def test_renders_user_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Hello {{ user.email }}</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user("bob@example.com"), server_url="https://example.com")
    assert "bob@example.com" in out


def test_renders_server_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "<p>Server: {{ server.url }}</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://myserver.example.com")
    assert "https://myserver.example.com" in out


# ---------------------------------------------------------------------------
# Anonymous user (user=None)
# ---------------------------------------------------------------------------


def test_renders_with_anonymous_user(conn):
    WelcomeTemplateRepository(conn).set(
        "{% if user %}<p>Hi {{ user.email }}</p>{% else %}<p>Please sign in.</p>{% endif %}",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=None, server_url="https://example.com")
    assert "Please sign in." in out
    assert "Hi" not in out


# ---------------------------------------------------------------------------
# Build context shape
# ---------------------------------------------------------------------------


def test_context_exposes_documented_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    for key in ("instance", "server", "user", "now", "today"):
        assert key in ctx, f"missing context key: {key}"
    assert "tables" not in ctx
    assert "metrics" not in ctx
    assert "marketplaces" not in ctx
    assert "sync_interval" not in ctx
    assert "data_source" not in ctx


def test_context_user_none():
    ctx = build_context(user=None, server_url="https://example.com")
    assert ctx["user"] is None


def test_context_instance_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    assert "name" in ctx["instance"]
    assert "subtitle" in ctx["instance"]


def test_context_server_keys():
    ctx = build_context(user=_user(), server_url="https://example.com")
    assert ctx["server"]["url"] == "https://example.com"
    assert ctx["server"]["hostname"] == "example.com"


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------


def test_sanitize_strips_script_tag():
    html = '<p>Hello</p><script>alert("xss")</script>'
    result = _sanitize_banner_html(html)
    assert "<script>" not in result
    assert "alert" not in result
    assert "<p>Hello</p>" in result


def test_sanitize_strips_script_with_attributes():
    html = '<script type="text/javascript">evil()</script><p>ok</p>'
    result = _sanitize_banner_html(html)
    assert "evil" not in result
    assert "<p>ok</p>" in result


def test_sanitize_strips_iframe():
    html = '<p>text</p><iframe src="https://evil.example.com"></iframe>'
    result = _sanitize_banner_html(html)
    assert "<iframe" not in result
    assert "<p>text</p>" in result


def test_sanitize_strips_event_handlers():
    html = '<button onclick="evil()">Click me</button>'
    result = _sanitize_banner_html(html)
    assert "onclick" not in result
    assert "evil" not in result
    assert "Click me" in result


def test_sanitize_strips_onload_on_img():
    html = '<img src="x" onload="steal()" alt="test">'
    result = _sanitize_banner_html(html)
    assert "onload" not in result
    assert "steal" not in result


def test_sanitize_strips_javascript_uri():
    html = '<a href="javascript:alert(1)">click</a>'
    result = _sanitize_banner_html(html)
    assert "javascript:" not in result


def test_sanitize_allows_safe_html():
    html = "<p>VPN required. Contact <a href='https://support.example.com'>support</a>.</p>"
    result = _sanitize_banner_html(html)
    assert "<p>" in result
    assert "<a href" in result
    assert "support" in result


# ---------------------------------------------------------------------------
# Render failure → empty string (not exception)
# ---------------------------------------------------------------------------


def test_render_failure_falls_back_to_default_not_exception(conn):
    # StrictUndefined: referencing an unknown variable raises at render time.
    WelcomeTemplateRepository(conn).set("{{ does_not_exist }}", updated_by="admin@example.com")
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    # Must not raise — falls back to the live default script (non-empty)
    assert out != ""
    # Thin layout: `agnes onboard` is the bootstrap step regardless of role.
    assert "agnes onboard" in out
    assert "uv tool install" in out


def test_sanitize_applied_after_render(conn):
    """A template that produces <script> output is sanitized before return."""
    WelcomeTemplateRepository(conn).set(
        "<script>evil()</script><p>safe content</p>",
        updated_by="admin@example.com",
    )
    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")
    assert "<script>" not in out
    assert "evil" not in out
    assert "<p>safe content</p>" in out


def test_a_stored_override_with_the_retired_token_placeholder_is_ignored(conn, caplog):
    """The save-time guards only inspect NEW writes. An override saved before
    the PAT handoff moved to `--token-file` still carries `{token}`, and Jinja2
    leaves a single-brace token alone — so it would render literally and the
    user would save the string `{token}` as their credential. Stored content is
    never re-validated, so the render seam is where this has to be caught
    (Devin Review on #1139).
    """
    WelcomeTemplateRepository(conn).set(
        "mkdir -p ~/.agnes && cat > ~/.agnes/token <<'AGNES_PAT'\n{token}\nAGNES_PAT\n",
        updated_by="admin@example.com",
    )

    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")

    assert "{token}" not in out, "served an override that writes a literal placeholder as the credential"
    # Fell back to the live default, which is the bash bootstrap script.
    assert "agnes" in out
    assert any("retired" in r.message.lower() or "retired" in str(r.msg).lower() for r in caplog.records), (
        "the operator gets no log line explaining why their override was ignored"
    )


def test_a_stored_override_with_a_bare_server_url_placeholder_still_resolves(conn):
    """The save-time guard (`_reject_bare_server_url_placeholder`) only
    inspects NEW writes — an override saved before that guard shipped, or
    authored by hand, can still carry the single-brace `{server_url}` the
    live default substitutes outside Jinja. Jinja2 only processes `{{ }}`,
    so left alone the reader would get the literal placeholder instead of a
    URL. Unlike the retired `{token}` placeholder this isn't unsafe to serve
    — it's just wrong — so the fix is to substitute it, not fall back to the
    default and discard the admin's content."""
    WelcomeTemplateRepository(conn).set(
        "Server: {server_url}\n",
        updated_by="admin@example.com",
    )

    out = render_agent_prompt_banner(conn, user=_user(), server_url="https://example.com")

    assert "{server_url}" not in out
    assert "Server: https://example.com" in out
