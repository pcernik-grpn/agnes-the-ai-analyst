"""The connect-this-tool skill and the default it depends on (TCRD-206).

Connecting an outside AI client moves from a page of per-tool instructions to
a conversation. Two things have to hold for that to be safe: the skill must
ship on an ordinary instance (its flag is default-ON, unlike every gated
skill before it), and it must never invite a token into the transcript.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.workdir import (
    _FEATURE_GATED_SKILLS,
    _prune_disabled_feature_skills,
    skill_disabled_on_this_instance,
)

SKILL_DIR = Path("app/initial_workspace_default/.claude/skills/connect-this-tool")
SKILL = SKILL_DIR / "SKILL.md"


@pytest.fixture
def body() -> str:
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    skills = tmp_path / ".claude" / "skills" / "connect-this-tool"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: connect-this-tool\n---\n", encoding="utf-8")
    return tmp_path


# ── it has to actually ship ────────────────────────────────────────────────


def test_the_skill_is_in_the_bundled_template():
    """`app/initial_workspace_default` is the tree WorkdirManager copies into
    every session — `src/_bundled_seed` is a generated mirror, not the source."""
    assert SKILL.is_file()


def test_a_default_on_flag_means_the_skill_ships_when_nobody_configured_it(workspace, monkeypatch):
    """The regression the per-entry default exists to prevent.

    Every gated skill before this one was default-OFF, so the reader hardcoded
    `default=False`. `mcp.connector_ui_enabled` is default-ON — under the old
    reader this skill would have been pruned from every instance that simply
    never mentioned the flag, which is nearly all of them."""
    monkeypatch.delenv("AGNES_MCP_CONNECTOR_UI_ENABLED", raising=False)
    assert skill_disabled_on_this_instance("connect-this-tool") is False
    _prune_disabled_feature_skills(workspace)
    assert (workspace / ".claude" / "skills" / "connect-this-tool").exists()


def test_the_skill_goes_when_the_connector_ui_is_switched_off(workspace, monkeypatch):
    """With the connector UI off there is no /mcp-connect to send anyone to,
    so the skill could only ever walk someone into a dead end."""
    monkeypatch.setenv("AGNES_MCP_CONNECTOR_UI_ENABLED", "false")
    assert skill_disabled_on_this_instance("connect-this-tool") is True
    _prune_disabled_feature_skills(workspace)
    assert not (workspace / ".claude" / "skills" / "connect-this-tool").exists()


def test_every_gate_entry_carries_its_own_default():
    for name, gate in _FEATURE_GATED_SKILLS.items():
        assert len(gate) == 4, f"{name} predates the per-entry default"
        assert isinstance(gate[3], bool), f"{name}'s default must be a bool"


# ── the rule that keeps this safe ──────────────────────────────────────────


def test_the_skill_refuses_to_take_a_token_through_the_chat(body):
    """A PAT in the transcript is a leaked PAT (DES-80). The skill must say
    so AND tell the user how to invalidate one they already pasted."""
    assert "Never let a token into this conversation" in body
    assert "revokes the one you just pasted" in body


def test_no_snippet_puts_a_token_in_a_url(body):
    """Security audit F13 / CWE-598: a PAT in a query string lands in proxy
    logs and browser history. The header form is the only one offered."""
    assert "?token=" not in body
    assert "Authorization: Bearer" in body


def test_the_skill_never_suggests_disabling_tls_verification(body):
    assert "NODE_TLS_REJECT_UNAUTHORIZED=0" in body, "the refusal must name the thing it refuses"
    assert "Never work around a TLS error" in body


# ── coverage, not handholding ──────────────────────────────────────────────


def test_it_covers_more_than_claude_and_has_a_path_for_unknown_tools(body):
    """'It isn't only Claude, and one page carrying instructions for every
    tool is a matrix' — so named tools are examples, not the whole set."""
    for tool in ("Claude Code", "Cursor", "VS Code"):
        assert tool in body
    assert "Any other MCP client" in body
    assert "Do not assume Claude" in body


def test_the_endpoint_and_transport_match_what_the_page_generates(body):
    """The skill and /mcp-connect must not drift into two different answers."""
    page = Path("app/web/templates/mcp_connect.html").read_text(encoding="utf-8")
    assert "/api/mcp/sse" in page and "/api/mcp/sse" in body
    assert "--transport sse" in page and "--transport sse" in body
    assert "~/.cursor/mcp.json" in page and "~/.cursor/mcp.json" in body
    assert ".vscode/mcp.json" in page and ".vscode/mcp.json" in body


def test_success_is_confirmed_against_last_used_at_not_assumed(body):
    """'A config that was written and a client that connected are different
    facts' — the skill checks the token's own usage stamp."""
    assert "agnes tokens list" in body
    assert "last_used_at" in body
    assert "Never report success off the config alone" in body


def test_the_skill_names_its_own_step_ceiling(body):
    """The ticket's own rule: if the chat needs six steps, the connect path
    is what's wrong. The skill has to say that rather than grow a ladder."""
    assert "never take six steps" in body
    assert body.count("\n## Step ") == 4
