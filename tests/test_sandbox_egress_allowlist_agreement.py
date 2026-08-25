"""The bundled in-sandbox egress hook — host derivation and URL parsing.

``ALLOWED_HOSTS`` in the bundled ``.claude/hooks/pre_tool_use.py`` is checked
inside the sandbox, before anything reaches the network. It is the advisory
layer; the enforcing layers live outside the sandbox (the docker provider's
egress modes — ``none``'s internal bridge, ``allowlist``'s egress-proxy
sidecar — and, on kai-agent, the engine's own sandbox policy).

History that shaped these tests: a since-removed sandbox-level allowlist
claimed in a comment to mirror this hook's set, and did not — the sandbox
allowed the Agnes host, the hook did not, and the stricter of two layers wins
silently. Watched live on a running instance: asked to build a data app, the
agent obtained a git credential from the API and then failed to clone the
app's repo by name, by hostname and by IP. The hook's per-deployment host
derivation (``_agnes_host``) is what fixed that, so it is asserted here
rather than claimed in a comment.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")


def _load_hook(agnes_server: str | None):
    """Import the bundled hook with a chosen AGNES_SERVER, as the sandbox does."""
    prev = os.environ.get("AGNES_SERVER")
    if agnes_server is None:
        os.environ.pop("AGNES_SERVER", None)
    else:
        os.environ["AGNES_SERVER"] = agnes_server
    try:
        spec = importlib.util.spec_from_file_location("bundled_hook", HOOK)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("AGNES_SERVER", None)
        else:
            os.environ["AGNES_SERVER"] = prev


def test_the_hook_admits_this_instance_own_host():
    """The case that was broken: the sandbox could not reach Agnes at all."""
    mod = _load_hook("https://agnes.example.com")
    assert "agnes.example.com" in mod.ALLOWED_HOSTS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://agnes.example.com", "agnes.example.com"),
        ("http://app:8000", "app"),
        ("app:8000", "app"),  # bare host:port — urlparse yields no hostname without a scheme
        ("https://agnes.example.com/sub/path", "agnes.example.com"),
    ],
)
def test_the_host_is_derived_from_every_spelling_the_env_uses(raw, expected):
    assert expected in _load_hook(raw).ALLOWED_HOSTS


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_absent_server_url_leaves_the_allowlist_untouched(raw):
    """Fail closed: no host is better than a wrong one, and the four baked
    entries are what a sandbox needs to function at all."""
    assert _load_hook(raw).ALLOWED_HOSTS == {
        "127.0.0.1",
        "localhost",
        "api.anthropic.com",
        "api.github.com",
    }


# ── userinfo in the URL ──────────────────────────────────────────────────────


def _decide(mod, cmd: str):
    d = mod._decide({"tool_name": "Bash", "tool_input": {"command": cmd}})
    out = d.get("hookSpecificOutput", d)
    return out.get("permissionDecision", "allow"), out.get("permissionDecisionReason") or ""


def test_basic_auth_credentials_are_not_read_as_the_hostname():
    """`split(":")[0]` over the whole authority read the basic-auth USERNAME as
    the host, and that was wrong in both directions.

    Deny side, watched live: every data-app clone URL is
    `http://agnes:<jwt>@<host>/data-apps.git/<slug>`, so the hook reported
    "Outbound network to 'agnes' is not in the Agnes egress allowlist" and
    refused the clone no matter what the allowlist contained.
    """
    mod = _load_hook("https://agnes.example.com")
    decision, reason = _decide(mod, "git clone http://agnes:JWT@agnes.example.com/data-apps.git/x")
    assert decision == "allow", reason


def test_an_allowed_name_in_the_userinfo_does_not_smuggle_a_denied_host():
    """Allow side, and the reason this is a security fix rather than a
    convenience one: `http://api.github.com:pw@evil.example/` extracted
    `api.github.com` and was ALLOWED while the request went to `evil.example`.
    Anyone who could get a command run in the sandbox could reach any host by
    putting an allowlisted name in the userinfo."""
    mod = _load_hook("https://agnes.example.com")
    decision, reason = _decide(mod, "curl http://api.github.com:pw@evil.example/x")
    assert decision == "deny"
    assert "evil.example" in reason, f"the real host must be named in the refusal: {reason!r}"


def test_ordinary_urls_still_decide_the_same_way():
    mod = _load_hook("https://agnes.example.com")
    assert _decide(mod, "curl https://api.github.com/repos")[0] == "allow"
    assert _decide(mod, "curl https://evil.example/x")[0] == "deny"


def _load_hook_env(env: dict[str, str | None]):
    """Import the bundled hook with an arbitrary env slice applied."""
    prev = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location("bundled_hook_env", HOOK)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestTheRelayRewriteDoesNotHideTheRealHost:
    """Devin Review on this PR: the allowlist was reading the loopback relay.

    `runner._start_relay()` overwrites `AGNES_SERVER` with
    `http://127.0.0.1:<port>/agnes-api` *before* `claude` — and therefore this
    hook — is spawned, so the real Agnes host was never added and a `git
    clone` of a data-app repo from inside the sandbox stayed refused. The MCP
    tools kept working throughout, because they go through that very relay on
    loopback, which is what made it read as a git problem.
    """

    def test_the_real_host_wins_over_the_relay_rewrite(self):
        mod = _load_hook_env(
            {
                "AGNES_SERVER": "http://127.0.0.1:41234/agnes-api",
                "AGNES_REAL_SERVER": "https://agnes.example.com",
            }
        )
        assert "agnes.example.com" in mod.ALLOWED_HOSTS, "the sandbox still cannot reach its own server"

    def test_agnes_server_remains_the_fallback_without_a_relay(self):
        """Tests and a directly-invoked workspace have no relay to rewrite."""
        mod = _load_hook_env({"AGNES_SERVER": "https://agnes.example.com", "AGNES_REAL_SERVER": None})
        assert "agnes.example.com" in mod.ALLOWED_HOSTS

    def test_the_runner_records_the_real_server_before_overwriting_it(self):
        """Written after the overwrite, it would record the relay's own URL."""
        src = Path("app/chat/runner.py").read_text(encoding="utf-8")
        set_at = src.index('os.environ["AGNES_REAL_SERVER"] = real_server')
        overwrite_at = src.index('os.environ["AGNES_SERVER"] = f"http://127.0.0.1:{port}/agnes-api"')
        assert set_at < overwrite_at


class TestBareHostUserinfoIsNotTheHost:
    """Devin Review on this PR: the two extraction paths disagreed.

    The schemed-URL path strips basic-auth userinfo before reading the host;
    `_bare_hosts` did not. `curl api.github.com:pw@evil.example/x` has the
    authority `api.github.com:pw@evil.example` — splitting on ":" first yields
    the allowed `api.github.com` while the request goes to `evil.example`.
    """

    def _hosts(self, tokens):
        mod = _load_hook_env({"AGNES_SERVER": "https://agnes.example.com", "AGNES_REAL_SERVER": None})
        return mod._bare_hosts(tokens)

    def test_userinfo_does_not_masquerade_as_the_host(self):
        hosts = self._hosts(["curl", "api.github.com:pw@evil.example/x"])
        assert "evil.example" in hosts, "the real destination was never checked"
        assert "api.github.com" not in hosts, "userinfo was read as the host"

    def test_userinfo_containing_an_at_sign_still_resolves_to_the_last_authority(self):
        hosts = self._hosts(["curl", "user@name:pw@evil.example/x"])
        assert hosts == ["evil.example"], hosts

    def test_a_plain_bare_host_is_unchanged(self):
        assert self._hosts(["curl", "api.github.com/repos"]) == ["api.github.com"]
