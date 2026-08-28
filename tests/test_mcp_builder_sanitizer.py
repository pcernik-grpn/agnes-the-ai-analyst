"""The MCP-source builder's trust boundary.

`app/api/mcp_builder.py::_sanitize_patch` is the only thing standing between a
model's JSON and a row that decides what this instance DIALS or RUNS. Its three
siblings (agent, entity, package) each have their own sanitizer tests; this one
shipped without any, which is how `command` and `args` came to be patchable
with no guard while `url` had one.

Why that gap mattered: on `stdio` transport, `command` and `args` are what reach
`StdioServerParameters` and are launched as a subprocess on the server
(`connectors/mcp/client.py`). A patched `command` chooses the binary, and `args`
alone is enough — a benign `npx` or `node` the admin typed runs whatever it is
handed. The `url` rule was written on the reasoning that a model may not choose
which host the instance dials; the same reasoning applies harder here.

These are unit tests on the sanitizer itself, not on the route: the route's job
is to call it, and what needs pinning is the refusals.
"""

from __future__ import annotations

import pytest

from app.api.mcp_builder import PATCHABLE, _sanitize_patch


def _sanitize(raw, draft=None):
    return _sanitize_patch(raw, draft=draft or {})


# ── The connection target: url, command, args ────────────────────────────────


class TestTheModelCannotChooseWhatIsDialledOrRun:
    def test_a_url_the_admin_never_typed_is_dropped(self):
        assert _sanitize({"url": "https://evil.example/mcp"}) == {}

    def test_a_url_echoing_the_draft_is_kept(self):
        """The model repeating the panel back is the normal case and harmless —
        the point is that it cannot introduce a different one."""
        draft = {"url": "https://mcp.example.com/sse"}
        assert _sanitize({"url": "https://mcp.example.com/sse"}, draft) == draft

    def test_a_corrected_url_is_still_a_chosen_url(self):
        """A plausible 'fix' — adding a path, switching the scheme — is the same
        act as picking a host, so it is refused like any other."""
        draft = {"url": "https://mcp.example.com"}
        assert _sanitize({"url": "https://mcp.example.com/sse"}, draft) == {}

    def test_a_command_the_admin_never_typed_is_dropped(self):
        """The sharpest one: this is a subprocess on the server."""
        assert _sanitize({"command": "/bin/sh"}) == {}

    def test_a_command_echoing_the_draft_is_kept(self):
        draft = {"command": "npx"}
        assert _sanitize({"command": "npx"}, draft) == draft

    def test_a_swapped_command_is_dropped(self):
        assert _sanitize({"command": "bash"}, {"command": "npx"}) == {}

    def test_invented_args_are_dropped(self):
        """`args` alone is enough to change what a command does: the admin's own
        `npx` will run whatever package it is handed."""
        draft = {"command": "npx", "args": ["-y", "@acme/crm-mcp"]}
        out = _sanitize({"args": ["-y", "@attacker/pkg"]}, draft)
        assert "args" not in out

    def test_args_echoing_the_draft_are_kept(self):
        draft = {"command": "npx", "args": ["-y", "@acme/crm-mcp"]}
        assert _sanitize({"args": ["-y", "@acme/crm-mcp"]}, draft)["args"] == ["-y", "@acme/crm-mcp"]

    def test_an_appended_arg_is_dropped(self):
        """Not "mostly the same" — appending one flag is how `node` becomes
        `node -e`, so the comparison is the whole list or nothing."""
        draft = {"args": ["-y", "@acme/crm-mcp"]}
        assert "args" not in _sanitize({"args": ["-y", "@acme/crm-mcp", "--eval"]}, draft)

    def test_the_empty_draft_case_accepts_nothing(self):
        """A first turn has no typed target at all, so every one of the three is
        refused rather than seeded by the model."""
        out = _sanitize({"url": "https://x.example", "command": "sh", "args": ["-c", "id"]})
        assert out == {}


# ── The remaining fields ─────────────────────────────────────────────────────


class TestEnumsAndShapes:
    @pytest.mark.parametrize(
        "field,good,bad",
        [
            ("transport", "http", "telnet"),
            ("auth_method", "bearer", "basic"),
            ("scope", "per_user", "global"),
        ],
    )
    def test_only_known_values_pass(self, field, good, bad):
        assert _sanitize({field: good}) == {field: good}
        assert _sanitize({field: bad}) == {}

    def test_a_pasted_token_is_not_written_into_the_env_name_field(self):
        """`auth_secret_env` NAMES a variable; it never holds the secret. The
        shape check is what stops a model that misread the question from
        storing a credential in a field that is displayed."""
        assert _sanitize({"auth_secret_env": "sk-live-abc123"}) == {}
        assert _sanitize({"auth_secret_env": "ACME_MCP_TOKEN"}) == {"auth_secret_env": "ACME_MCP_TOKEN"}

    def test_lowercase_and_leading_digits_are_not_env_names(self):
        assert _sanitize({"auth_secret_env": "acme_token"}) == {}
        assert _sanitize({"auth_secret_env": "1TOKEN"}) == {}

    def test_a_field_outside_patchable_cannot_be_set(self):
        for key in ("id", "enabled", "secret_value", "url_policy_verdict"):
            assert key not in PATCHABLE
            assert _sanitize({key: "x"}) == {}

    def test_non_dict_and_non_string_values_are_refused_not_coerced(self):
        assert _sanitize(None) == {}
        assert _sanitize("name=x") == {}
        assert _sanitize([{"name": "x"}]) == {}
        assert _sanitize({"name": 7}) == {}
        assert _sanitize({"args": "not-a-list"}) == {}

    def test_a_long_value_is_capped_rather_than_refused(self):
        out = _sanitize({"name": "n" * 5000})
        assert 0 < len(out["name"]) <= 2048
