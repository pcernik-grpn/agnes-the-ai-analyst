"""Canonical feature-flag convention (#1022).

Covers:
- `feature_enabled()` resolution order: env > get_value() (instance.yaml +
  admin server-config overlay) > default.
- Truthy-string parsing shared with the rest of the codebase's boolean
  config readers ("0"/"false"/"no"/"off"/"" are false; everything else,
  including unrecognized strings, is true).
- `FEATURE_FLAGS` registry completeness — every entry resolves cleanly.
- Behavior preservation for `get_studio_enabled` / `get_guardrails_enabled`
  after their refactor to delegate to `feature_enabled`.
- The `feature_flags` inventory block in GET /api/admin/server-config.
"""

from __future__ import annotations

import pytest

import app.instance_config as ic

# Captured at collection time — tests/conftest.py's autouse
# `_flea_guardrails_disabled_by_default` fixture monkeypatches
# `app.instance_config.get_guardrails_enabled` to a `lambda: False` stub for
# every test (so legacy flea-market tests don't need live LLM credentials).
# That per-test patch runs *after* this module is imported, so this
# reference still points at the real implementation regardless of the stub.
_real_get_guardrails_enabled = ic.get_guardrails_enabled


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- feature_enabled() resolution order --------------------------------------


class TestFeatureEnabledResolutionOrder:
    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("AGNES_TEST_FLAG", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is False
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=True) is True

    def test_yaml_wins_over_default(self, monkeypatch):
        monkeypatch.delenv("AGNES_TEST_FLAG", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is True

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("AGNES_TEST_FLAG", "0")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=True) is False

    def test_env_wins_over_yaml_the_other_direction(self, monkeypatch):
        monkeypatch.setenv("AGNES_TEST_FLAG", "1")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is True

    def test_no_env_var_configured_falls_through_to_yaml(self, monkeypatch):
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert ic.feature_enabled("a", "b", default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", ""])
    def test_env_falsy_strings(self, monkeypatch, raw):
        monkeypatch.setenv("AGNES_TEST_FLAG", raw)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=True) is False

    @pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on", "anything"])
    def test_env_truthy_strings(self, monkeypatch, raw):
        monkeypatch.setenv("AGNES_TEST_FLAG", raw)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is True

    def test_unset_env_var_name_is_ignored(self, monkeypatch):
        # env_var=None (or omitted) never consults os.environ.
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.feature_enabled("a", "b", default=True) is False


# --- FEATURE_FLAGS registry ---------------------------------------------------


class TestFeatureFlagsRegistry:
    def test_registry_covers_expected_flags(self):
        names = {f.name for f in ic.FEATURE_FLAGS}
        assert names == {
            "studio",
            "news",
            "knowledge_digests",
            "contribute_skill",
            "guardrails",
            "chat",
            "chat_provider",
            "chat_approvals",
            "chat_bootstrap_marketplace",
            "chat_broker_admin_reads",
            "data_apps",
            "data_apps_allow_same_origin",
            "library_show_unverified_trust",
            "library_auto_share_admin_uploads",
            "experience",
            "stack_auto_membership",
            "mcp_query_param_token",
            "mcp_session_pool",
            "mcp_source_url_strict",
            "mcp_connector_ui",
            "mcp_source_url_runtime_enforce",
            "agent_profiles",
            "access_policies",
            "keboola_token_header",
            "keboola_multi_project_mode",
            "microsoft_group_sync",
            "kai_broker_mcp_enabled",
            "facts",
            "facts_visibility_mode",
            "extraction",
        }

    def test_every_entry_resolves(self, monkeypatch):
        from app import switches as sw

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        for flag in ic.FEATURE_FLAGS:
            monkeypatch.delenv(flag.env_var, raising=False)
            if flag.kind != "bool":
                # Non-boolean switches (the `experience` select) resolve
                # through switch_value, not the boolean feature_enabled.
                if flag.runtime_view:
                    continue
                assert sw.switch_value(flag.name) == flag.default
                continue
            result = ic.feature_enabled(*flag.config_keys, env_var=flag.env_var, default=flag.default)
            assert isinstance(result, bool)
            assert result == flag.default

    def test_grandfathered_flags_default_on(self):
        by_name = {f.name: f for f in ic.FEATURE_FLAGS}
        assert by_name["guardrails"].default is True
        assert by_name["agent_profiles"].default is True

    def test_retired_surfaces_default_off(self):
        """The four surfaces the admin cleanup hid: Studio (with its
        suggestions queue), news, the digests admin page, contribute-a-skill.

        `studio` used to be asserted alongside `guardrails` above as
        grandfathered-on. It moved here deliberately, not by accident: the
        Library builders do its authoring jobs now, so an upgrade turning it
        off is the intended behavior change. The pages still exist — every one
        of these is one flag away from coming back.
        """
        by_name = {f.name: f for f in ic.FEATURE_FLAGS}
        assert by_name["studio"].default is False
        assert by_name["news"].default is False
        assert by_name["knowledge_digests"].default is False
        assert by_name["contribute_skill"].default is False

    def test_new_flags_default_off(self):
        by_name = {f.name: f for f in ic.FEATURE_FLAGS}
        assert by_name["chat"].default is False
        assert by_name["data_apps"].default is False

    def test_positive_trust_vocabulary_is_on_by_default(self):
        """The Library states all three provenance levels, not two and a silence.

        This flag was off, on an upgrade-parity rationale that the paper gate
        already delivers: every `mark()` callsite passes `paper=is_paper()` and
        the macro renders nothing without it, so a default blue instance grows no
        markers whatever this flag says (pinned by
        tests/test_ui_layout_theme.py::test_default_instance_renders_no_ds_trust_marker
        and ::test_default_theme_renders_no_trust_markers_on_populated_rows).
        Off therefore bought no parity — it only withheld the third level from
        the one look built to state all three, so Organization and Verified rows
        wore a marker and every unverified row was left bare, which reads as
        markers being broken rather than as a provenance level.
        """
        by_name = {f.name: f for f in ic.FEATURE_FLAGS}
        assert by_name["library_show_unverified_trust"].default is True

    def test_entries_carry_a_description(self):
        for flag in ic.FEATURE_FLAGS:
            assert flag.description

    def test_facts_operator_text_does_not_deny_shipped_surfaces(self):
        """The `facts` flag ships the read surface across REST/CLI/MCP and the
        REST write surface, but the operator-facing copy is written in three
        places — the `/admin/server-config` field hint, the switch registry
        description, and the docs/feature-flags.md row — and two of them still
        said ingest and the CLI/MCP wrappers were "a separate follow-up". An
        operator reading that is told a shipped capability does not exist
        (Devin Review on #1652).

        Asserted as a coherence check across all three, not a spelling check
        on one: each must name the write surface, and none may describe part
        of the feature as not-yet-available.
        """
        from pathlib import Path

        from app.api.admin import _KNOWN_FIELDS
        from app.switches import SWITCHES

        hint = _KNOWN_FIELDS["facts"]["enabled"]["hint"]
        switch_desc = next(s for s in SWITCHES if s.name == "facts").description
        row = next(
            ln
            for ln in (Path(__file__).resolve().parents[1] / "docs/feature-flags.md").read_text().splitlines()
            if ln.startswith("| `facts` |")
        )

        for label, text in (("admin hint", hint), ("switch description", switch_desc), ("docs row", row)):
            assert "ingest" in text, f"{label} never mentions the shipped write surface"
            assert "follow-up" not in text, f"{label} still calls a shipped surface a follow-up: {text!r}"


# --- Behavior preservation: get_studio_enabled / get_guardrails_enabled ------


class TestStudioEnabledBehaviorPreserved:
    def test_default_false(self, monkeypatch):
        """Off by default since the admin cleanup — was True before it."""
        monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.get_studio_enabled() is False

    def test_yaml_false(self, monkeypatch):
        monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.get_studio_enabled() is False

    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", "0")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert ic.get_studio_enabled() is False

    def test_env_true_string(self, monkeypatch):
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.get_studio_enabled() is True


class TestAgentProfilesEnabledBehaviorPreserved:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("AGNES_AGENT_PROFILES_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert ic.get_agent_profiles_enabled() is True

    def test_yaml_false(self, monkeypatch):
        monkeypatch.delenv("AGNES_AGENT_PROFILES_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.get_agent_profiles_enabled() is False

    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert ic.get_agent_profiles_enabled() is False

    def test_env_true_string(self, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "1")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert ic.get_agent_profiles_enabled() is True


class TestGuardrailsEnabledBehaviorPreserved:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("AGNES_GUARDRAILS_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        assert _real_get_guardrails_enabled() is True

    def test_yaml_false(self, monkeypatch):
        monkeypatch.delenv("AGNES_GUARDRAILS_ENABLED", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: False)
        assert _real_get_guardrails_enabled() is False

    def test_env_override_is_new_and_additive(self, monkeypatch):
        monkeypatch.setenv("AGNES_GUARDRAILS_ENABLED", "0")
        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: True)
        assert _real_get_guardrails_enabled() is False


# --- GET /api/admin/server-config feature_flags block ------------------------


class TestServerConfigFeatureFlagsInventory:
    def test_get_includes_feature_flags_block(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "feature_flags" in data
        flags = data["feature_flags"]
        assert isinstance(flags, list)
        names = {f["name"] for f in flags}
        assert names == {
            "instance.experience",
            "studio",
            "news",
            "knowledge_digests",
            "contribute_skill",
            "guardrails",
            "chat",
            "chat_provider",
            "chat_approvals",
            "chat_bootstrap_marketplace",
            "chat_broker_admin_reads",
            "data_apps",
            "data_apps_allow_same_origin",
            "library_show_unverified_trust",
            "library_auto_share_admin_uploads",
            "stack_auto_membership",
            "mcp_query_param_token",
            "mcp_session_pool",
            "mcp_source_url_strict",
            "mcp_connector_ui",
            "mcp_source_url_runtime_enforce",
            "agent_profiles",
            "access_policies",
            "keboola_token_header",
            "keboola_multi_project_mode",
            "microsoft_group_sync",
            "kai_broker_mcp_enabled",
            "facts",
            "facts_visibility_mode",
            "extraction",
        }
        # The experience preset leads as a string-valued informational row.
        assert flags[0]["name"] == "instance.experience"
        assert flags[0]["value_label"] in ("classic", "redesign")
        for f in flags:
            if f["name"] == "instance.experience":
                assert set(f.keys()) >= {"name", "value_label", "source", "env_var", "description"}
                assert f["source"] in ("env", "config", "default")
                continue
            assert set(f.keys()) >= {"name", "effective", "source", "default", "env_var", "description"}
            assert f["source"] in ("env", "config", "default", "preset")
            assert isinstance(f["effective"], bool)

    def test_select_switch_reports_its_resolved_mode(self, seeded_app, monkeypatch):
        """A select-kind switch must surface its resolved STRING — the boolean
        path coerced every option (its 'disabled' default included) to
        effective=True, so the panel said multi-project was on while it was
        off (Devin Review on PR #1328)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        monkeypatch.delenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", raising=False)
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        row = flags["keboola_multi_project_mode"]
        assert row["value_label"] == "disabled"
        assert row["effective"] is False
        assert row["source"] == "default"
        monkeypatch.setenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", "auto")
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        row = flags["keboola_multi_project_mode"]
        assert row["value_label"] == "auto"
        assert row["effective"] is True
        assert row["source"] == "env"

    def test_chat_provider_row_resolves_through_the_runtime_view(self, seeded_app, monkeypatch):
        """chat_provider is a select AND chat-runtime-resolved: its string
        must come from `_chat_flag_runtime_view` (switch_value raises for
        runtime_view switches by design), and a BLANK env must not label the
        source "env" — `_resolve_chat_provider` treats blank as unset, so the
        panel would claim a pin that does not exist (Devin Review on this PR)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        monkeypatch.delenv("AGNES_CHAT_PROVIDER", raising=False)
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        row = flags["chat_provider"]
        assert row["value_label"] == "kai-agent"
        assert row["effective"] is False
        assert row["source"] == "default"
        monkeypatch.setenv("AGNES_CHAT_PROVIDER", "docker")
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        row = flags["chat_provider"]
        assert row["value_label"] == "docker"
        assert row["effective"] is True
        assert row["source"] == "env"
        # Blank env = unset for the select resolver — the label must agree.
        monkeypatch.setenv("AGNES_CHAT_PROVIDER", "  ")
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        row = flags["chat_provider"]
        assert row["value_label"] == "kai-agent"
        assert row["source"] == "default"

    def test_preset_coupled_flag_resolves_and_labels_preset_source(self, seeded_app, monkeypatch):
        """Under ``experience: redesign`` with no per-knob setting, the
        coupled flags must report their RUNTIME value (on) with source
        ``preset`` — never "off/default" while the running instance has them
        on (spec 2026-08-07-default-chrome-ux-parity)."""
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        monkeypatch.delenv("AGNES_STACK_AUTO_MEMBERSHIP", raising=False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        flags = {f["name"]: f for f in resp.json()["feature_flags"]}
        assert flags["instance.experience"]["value_label"] == "redesign"
        assert flags["instance.experience"]["source"] == "env"
        assert flags["stack_auto_membership"]["effective"] is True
        assert flags["stack_auto_membership"]["source"] == "preset"
        # Per-knob env still wins over the preset — and labels as env.
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
        flags = {f["name"]: f for f in c.get("/api/admin/server-config", headers=_auth(token)).json()["feature_flags"]}
        assert flags["stack_auto_membership"]["effective"] is False
        assert flags["stack_auto_membership"]["source"] == "env"

    def test_known_fields_multi_project_mode_renders_resolved_value(self, seeded_app, monkeypatch):
        """The editable panel must render the RESOLVED mode for the unset
        key: an env-only `auto` instance rendered the static `disabled`, so
        a routine auth-section save wrote `disabled` into the overlay — a
        silent revert of the feature the day the env var is dropped (Devin
        Review on PR #1328; same failure as instance.experience on #1199)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        monkeypatch.delenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", raising=False)
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["auth"]["keboola"]["fields"]["multi_project_mode"]["default"] == "disabled"
        monkeypatch.setenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", "auto")
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["auth"]["keboola"]["fields"]["multi_project_mode"]["default"] == "auto"

    def test_known_fields_project_id_required_follows_the_mode(self, seeded_app, monkeypatch):
        """`project_id` is required exactly when the single-project gate is
        in force: under an active discovery mode unset/`'*'` IS the wildcard,
        and a static required marker beside the leave-it-blank hint nudged
        operators to pin a project and silently lose the wildcard (Devin
        Review on PR #1328, sixteenth round)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        monkeypatch.delenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", raising=False)
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["auth"]["keboola"]["fields"]["project_id"]["required"] is True
        monkeypatch.setenv("AGNES_KEBOOLA_MULTI_PROJECT_MODE", "auto")
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["auth"]["keboola"]["fields"]["project_id"]["required"] is False

    def test_known_fields_defaults_follow_the_preset(self, seeded_app, monkeypatch):
        """The EDITABLE registry must render the preset-implied default for
        unset preset-coupled fields — a static literal there means an
        instance sees the stack switch OFF / theme `blue` and a routine
        "Save section" silently persists a value the runtime doesn't
        actually use (Devin Review on #1199). `redesign` is the default now
        (classic retired), so this holds whether the preset is left unset or
        set explicitly — neither should ever surface the registry's raw
        pre-redesign literals (`stack_auto_membership: False`, `theme: blue`)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["features"]["stack_auto_membership"]["default"] is True
        assert kf["instance"]["theme"]["default"] == "paper"

        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        kf = c.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]
        assert kf["features"]["stack_auto_membership"]["default"] is True
        assert kf["instance"]["theme"]["default"] == "paper"
        # The select must offer every value the resolver accepts — a lagging
        # option list can only write values that erase a working choice.
        assert set(kf["instance"]["theme"]["options"]) == {"blue", "navy", "dark", "auto", "paper"}

    def test_env_source_reflected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", "0")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        flags = {f["name"]: f for f in resp.json()["feature_flags"]}
        assert flags["studio"]["effective"] is False
        assert flags["studio"]["source"] == "env"

    def test_default_source_when_unset(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        flags = {f["name"]: f for f in resp.json()["feature_flags"]}
        assert flags["chat"]["effective"] is False
        assert flags["chat"]["source"] == "default"

    def test_chat_ignores_merged_static_config(self, seeded_app, monkeypatch):
        # chat's runtime reads ONLY the overlay file (app/main.py loads
        # load_chat_config(DATA_DIR/state/instance.yaml), never the static
        # config/instance.yaml base) — so a chat.enabled visible only through
        # the merged get_value() view must NOT surface as enabled here.
        from app.api import admin as admin_mod

        monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: True if keys == ("chat", "enabled") else default,
        )
        inv = {f["name"]: f for f in admin_mod._feature_flags_inventory()}
        assert inv["chat"]["effective"] is False
        assert inv["chat"]["source"] == "default"

    def test_chat_reflects_runtime_overlay_file(self, seeded_app, monkeypatch):
        from app.secrets import _state_dir

        monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
        overlay = _state_dir() / "instance.yaml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("chat:\n  enabled: true\n")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        flags = {f["name"]: f for f in resp.json()["feature_flags"]}
        assert flags["chat"]["effective"] is True
        assert flags["chat"]["source"] == "config"

    def test_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 403


def test_chat_approvals_reports_the_value_the_gate_actually_uses(tmp_path, monkeypatch):
    """`load_chat_config` parses the writable overlay only, `feature_enabled`
    the merged static+overlay — so resolving this flag the ordinary way let the
    panel report a value the running chat gate does not use (Devin Review on
    #1157). It goes through the same runtime view `chat` already needed."""
    import app.api.admin as admin_mod
    from app.instance_config import FEATURE_FLAGS

    assert "chat_approvals" in admin_mod._CHAT_RUNTIME_FLAGS, "would resolve from the wrong source"

    overlay = tmp_path / "instance.yaml"
    overlay.write_text("chat:\n  enabled: true\n  approvals_enabled: false\n")
    monkeypatch.setattr(admin_mod, "_state_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr("app.secrets._state_dir", lambda: tmp_path)
    monkeypatch.delenv("AGNES_CHAT_APPROVALS_ENABLED", raising=False)

    flag = next(f for f in FEATURE_FLAGS if f.name == "chat_approvals")
    effective, source = admin_mod._chat_flag_runtime_view(flag)
    assert effective is False, "the panel must show what the gate reads"
    assert source == "config"

    monkeypatch.setenv("AGNES_CHAT_APPROVALS_ENABLED", "1")
    effective, source = admin_mod._chat_flag_runtime_view(flag)
    assert effective is True and source == "env"


def test_no_read_site_restates_the_library_trust_default_as_a_literal():
    """The registry entry must BE the default, not a fourth opinion about it.

    Three call sites resolve `library.show_unverified_trust` — the Jinja global
    `_show_unverified_trust`, the /library route, and the store-item detail route
    — and each once carried its own `default=False`. The docstring asked them not
    to drift, which is documentation, not a mechanism: flipping the registry entry
    changed nothing at all, because all three overrode it. They now read
    `_LIBRARY_TRUST_DEFAULT`, which is derived from the registry.
    """
    import re
    from pathlib import Path

    text = Path("app/web/router.py").read_text(encoding="utf-8")
    # Every resolution of this flag, with whatever it passes for `default=`.
    blocks = re.findall(
        r'env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",\s*\n\s*default=([^,\n]+),',
        text,
    )
    assert len(blocks) == 3, f"expected 3 read sites, found {len(blocks)}: {blocks}"
    offenders = [b for b in blocks if b.strip() != "_LIBRARY_TRUST_DEFAULT"]
    assert not offenders, (
        f"these read sites restate the default as a literal instead of using _LIBRARY_TRUST_DEFAULT: {offenders}"
    )


class TestInventoryExposesSwitchMetadata:
    """The panel cannot explain a refusal it is not told about."""

    def test_every_row_carries_the_new_fields(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200
        rows = resp.json()["feature_flags"]
        assert rows, "inventory is empty"
        for row in rows:
            assert row["effect"] in ("live", "restart", "deploy")
            assert isinstance(row["editable"], bool)
            assert isinstance(row["lock_reason"], str)

    def test_locked_row_carries_its_reason(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = next(r for r in resp.json()["feature_flags"] if r["name"] == "data_apps")
        assert row["editable"] is False
        assert row["lock_reason"], "a locked switch must explain itself to the operator"

    def test_editable_row_carries_no_reason(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = next(r for r in resp.json()["feature_flags"] if r["name"] == "studio")
        assert row["editable"] is True
        assert row["lock_reason"] == ""

    def test_existing_fields_are_untouched(self, seeded_app):
        """PR3 rewrites the panel; until then the current renderer must keep
        working against the same keys."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        row = resp.json()["feature_flags"][0]
        for key in ("name", "effective", "source", "default", "env_var", "description"):
            assert key in row
