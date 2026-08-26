"""Slack app manifest invariants.

Three copies of the Slack app manifest ship in the repo — the canonical
``services/slack_bot/manifest.yaml`` plus the two paste-able transport
variants in ``docs/``. They have drifted before (dead ``users:*`` scopes,
missing ``reactions:write``, missing slash commands), and a drifted manifest
is exactly how an operator ends up hand-picking scopes and tripping a
workspace-admin security review. These guards pin all three to the same
minimal, bot-token-only surface.
"""

from pathlib import Path
import re

import pytest
import yaml

CANONICAL = Path("services/slack_bot/manifest.yaml")
DOC_HTTP = Path("docs/slack-manifest-http.md")
DOC_SOCKET = Path("docs/slack-manifest-socket.md")

# The minimum the bot code actually calls (services/slack_bot/sender.py,
# events). Growing this set requires a code path that needs the new scope —
# and a matching bullet in every manifest's per-scope rationale.
EXPECTED_BOT_SCOPES = {
    "app_mentions:read",
    "chat:write",
    "im:history",
    "im:write",
    "reactions:write",
}

EXPECTED_BOT_EVENTS = {"app_mention", "message.im"}

_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _load(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".md":
        blocks = _FENCE_RE.findall(text)
        assert len(blocks) == 1, f"{path}: expected exactly one ```yaml block, found {len(blocks)}"
        text = blocks[0]
    return yaml.safe_load(text)


MANIFESTS = {p: _load(p) for p in (CANONICAL, DOC_HTTP, DOC_SOCKET)}


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_bot_scopes_are_the_minimal_set(path: Path):
    scopes = MANIFESTS[path]["oauth_config"]["scopes"]
    assert set(scopes["bot"]) == EXPECTED_BOT_SCOPES, f"{path}: bot scopes drifted from the canonical minimal set"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_no_user_scopes(path: Path):
    """The bot is bot-token only. A user-scope stanza appearing here is a
    red flag for a security review (search:read.im and friends) and is
    never needed — identity binding runs through the /setup code flow."""
    scopes = MANIFESTS[path]["oauth_config"]["scopes"]
    assert "user" not in scopes, f"{path}: user scopes must never appear in the bot manifest"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_bot_events_match(path: Path):
    events = MANIFESTS[path]["settings"]["event_subscriptions"]["bot_events"]
    assert set(events) == EXPECTED_BOT_EVENTS, f"{path}: bot_events drifted"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_slash_commands_match_canonical(path: Path):
    canonical = [c["command"] for c in MANIFESTS[CANONICAL]["slash_commands"]]
    ours = [c["command"] for c in MANIFESTS[path]["slash_commands"]]
    assert ours == canonical, f"{path}: slash commands drifted from canonical"


def test_transport_stanzas():
    """HTTP variants carry request_urls and socket_mode off; the Socket Mode
    variant carries NO request_url anywhere (a stale one is the classic
    foot-gun the two-variant split exists to prevent) and socket_mode on."""
    for path in (CANONICAL, DOC_HTTP):
        m = MANIFESTS[path]
        assert m["settings"]["socket_mode_enabled"] is False, path
        assert "request_url" in m["settings"]["event_subscriptions"], path

    socket = MANIFESTS[DOC_SOCKET]
    assert socket["settings"]["socket_mode_enabled"] is True
    flat = yaml.safe_dump(socket)
    assert "request_url" not in flat, "socket manifest must not carry any request_url"
