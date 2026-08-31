"""Unified switch registry — integrity and resolution.

The registry in `app/switches.py` is the single source of truth for every
operator-facing toggle: its resolution order, its type, whether it can be
edited, and why not when it cannot. These tests guard the registry's own
shape; the derivation guards (that `_EDITABLE_SECTIONS` and the admin field
metadata follow from it) live in `tests/test_admin_configure_api.py`.
"""

from __future__ import annotations

import pytest

from app.switches import CATEGORIES, EFFECTS, KINDS, ON_INVALID, SWITCHES, get_switch


class TestRegistryIntegrity:
    def test_registry_is_not_empty(self):
        assert len(SWITCHES) >= 7

    def test_names_are_unique(self):
        names = [s.name for s in SWITCHES]
        assert len(names) == len(set(names)), "duplicate switch name"

    def test_env_vars_are_unique(self):
        env_vars = [s.env_var for s in SWITCHES if s.env_var]
        assert len(env_vars) == len(set(env_vars)), "duplicate env var"

    def test_config_keys_are_unique(self):
        keys = [s.config_keys for s in SWITCHES if s.config_keys]
        assert len(keys) == len(set(keys)), "duplicate config key path"

    def test_every_entry_carries_a_description(self):
        for s in SWITCHES:
            assert s.description.strip(), f"{s.name} has no description"

    def test_effect_is_a_known_value(self):
        for s in SWITCHES:
            assert s.effect in EFFECTS, f"{s.name}: {s.effect}"

    def test_category_is_a_known_value(self):
        for s in SWITCHES:
            assert s.category in CATEGORIES, f"{s.name}: {s.category}"

    def test_kind_is_a_known_value(self):
        """Unlike `effect` and `category`, `kind` had no validity test: a
        typo'd `kind="boolean"` falls past every branch in `switch_value` and
        returns a lowercased STRING — and `"false"` is truthy at a callsite
        doing `if switch_value(...)`."""
        for s in SWITCHES:
            assert s.kind in KINDS, f"{s.name}: {s.kind}"

    def test_on_invalid_is_a_known_value(self):
        for s in SWITCHES:
            assert s.on_invalid in ON_INVALID, f"{s.name}: {s.on_invalid}"

    def test_select_entries_declare_options_containing_their_default(self):
        for s in SWITCHES:
            if s.kind != "select":
                continue
            assert s.options, f"{s.name} is a select with no options"
            assert s.default in s.options, f"{s.name} default {s.default!r} not in options"

    def test_non_select_entries_declare_no_options(self):
        for s in SWITCHES:
            if s.kind != "select":
                assert s.options == (), f"{s.name} is {s.kind} but declares options"

    def test_locked_entries_state_a_reason(self):
        """A switch the UI refuses to edit must say why, in the product.

        The reason previously lived in a dict inside a test file, where an
        operator hitting the refusal could never see it.
        """
        for s in SWITCHES:
            if not s.editable:
                assert s.lock_reason.strip(), f"{s.name} is locked with no lock_reason"

    def test_no_section_mixes_editable_and_locked_switches(self):
        """`editable` is enforced per SECTION, so a section must not hold both.

        `POST /api/admin/server-config` validates the top-level section name
        against `_EDITABLE_SECTIONS` and then deep-merges the whole patch, so
        one `editable=True` switch opens every key in its section. A locked
        switch sharing that section would render its `lock_reason` in the
        inventory while its key stayed writable through the merge — the lock
        would be a label, not a gate.

        Today both locked switches sit alone in their sections, so the
        existing guards pass vacuously on this case: `_locked_sections()` in
        tests/test_admin_configure_api.py computes `locked - editable`, which
        removes a mixed section before anything is asserted about it. This is
        the assertion that fails instead of letting the next switch author
        discover it in production.
        """
        by_section: dict[str, list] = {}
        for s in SWITCHES:
            if s.config_keys:
                by_section.setdefault(s.config_keys[0], []).append(s)
        for section, group in by_section.items():
            editable = sorted(s.name for s in group if s.editable)
            locked = sorted(s.name for s in group if not s.editable)
            assert not (editable and locked), (
                f"section {section!r} mixes editable {editable} with locked {locked}. "
                "Section-level write gating cannot honour both — either give the locked "
                "switch its own section, or drop its lock and gate the behaviour elsewhere."
            )

    def test_editable_entries_state_no_reason(self):
        """Shrinks-only: a switch that became editable must clear its reason,
        so a stale explanation cannot outlive the restriction it described."""
        for s in SWITCHES:
            if s.editable:
                assert not s.lock_reason, f"{s.name} is editable but still carries a lock_reason"

    def test_deploy_entries_have_no_config_key(self):
        """`deploy` means there is nothing to write: the value is what the
        container was started with, not a section of instance.yaml."""
        for s in SWITCHES:
            if s.effect == "deploy":
                assert s.config_keys == (), f"{s.name} is deploy-class but declares a config key"

    def test_env_var_names_follow_the_convention(self):
        for s in SWITCHES:
            if s.env_var:
                assert s.env_var.isupper(), f"{s.env_var} is not upper-case"

    def test_switches_are_frozen(self):
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            SWITCHES[0].default = "mutated"  # type: ignore[misc]


class TestPortedFlagsAreUnchanged:
    """PR1 must not change what any instance resolves. These pin the seven
    flags' identity fields against the values they shipped with."""

    EXPECTED = {
        # `default` is False since the admin cleanup retired the surface; the
        # config key and env var are what this class pins as unchanged.
        "studio": (("studio", "enabled"), "AGNES_STUDIO_ENABLED", False),
        "guardrails": (("guardrails", "enabled"), "AGNES_GUARDRAILS_ENABLED", True),
        "chat_approvals": (("chat", "approvals_enabled"), "AGNES_CHAT_APPROVALS_ENABLED", True),
        "chat": (("chat", "enabled"), "AGNES_CHAT_ENABLED", False),
        "data_apps": (("data_apps", "enabled"), "AGNES_DATA_APPS_ENABLED", False),
        "library_show_unverified_trust": (
            ("library", "show_unverified_trust"),
            "AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
            True,
        ),
        "mcp_query_param_token": (
            ("mcp", "allow_query_param_token"),
            "AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN",
            True,
        ),
    }

    def test_all_seven_are_present(self):
        assert {s.name for s in SWITCHES} >= set(self.EXPECTED)

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_identity_fields_are_unchanged(self, name):
        s = get_switch(name)
        keys, env_var, default = self.EXPECTED[name]
        assert s.config_keys == keys
        assert s.env_var == env_var
        assert s.default is default
        assert s.kind == "bool"

    def test_chat_flags_declare_their_overlay_only_runtime_view(self):
        """`app/main.py` boots chat from the writable overlay file alone, so
        the panel must read these two from the same place the runtime does."""
        assert get_switch("chat").runtime_view == "enabled"
        assert get_switch("chat_approvals").runtime_view == "approvals_enabled"

    def test_data_apps_is_locked_with_its_sidecar_reason(self):
        s = get_switch("data_apps")
        assert s.editable is False
        assert "apps" in s.lock_reason


class TestDataAppsAllowSameOriginSwitch:
    """`data_apps.allow_same_origin` resolves through the registry (the
    sync-map's "new user-visible switch" row — never a hand-rolled
    os.environ/get_value pair) and stays locked in the panel: flipping it
    hands every hosted app's JS the viewer's session, a deployment decision
    rather than a live toggle (and its `data_apps` section is locked
    regardless — see `test_no_section_mixes_editable_and_locked_switches`)."""

    def test_identity_and_lock(self):
        s = get_switch("data_apps_allow_same_origin")
        assert s.config_keys == ("data_apps", "allow_same_origin")
        assert s.env_var == "AGNES_DATA_APPS_ALLOW_SAME_ORIGIN"
        assert s.kind == "bool"
        assert s.default is False
        assert s.editable is False
        assert s.lock_reason.strip()

    def test_serving_gate_reads_the_registry(self, monkeypatch):
        """`same_origin_serving_allowed()` resolves via `switch_value`, so
        the registry's resolution order (env > merged config > default) IS
        the serving gate's resolution order."""
        import app.switches as sw
        from app.api.data_apps import same_origin_serving_allowed

        monkeypatch.delenv("AGNES_DATA_APPS_ALLOW_SAME_ORIGIN", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert same_origin_serving_allowed() is False
        monkeypatch.setenv("AGNES_DATA_APPS_ALLOW_SAME_ORIGIN", "1")
        assert same_origin_serving_allowed() is True
        assert sw.switch_value("data_apps_allow_same_origin") is True


class TestExtractionSwitch:
    """`extraction.enabled` (spec §7.5 / §16 step 7) gates the
    `corpus-extraction` job kind's handler. Editable from `/admin/server-config`
    (T3: reversing this switch's original deploy-time-only stance) — a
    deploy-time env var still wins per field ahead of a web save (see
    `TestExtractionEnvLock` below), so the panel's write path is never
    silently inert even though the switch itself is no longer locked."""

    def test_identity_and_lock(self):
        s = get_switch("extraction")
        assert s.config_keys == ("extraction", "enabled")
        assert s.env_var == "AGNES_EXTRACTION_ENABLED"
        assert s.kind == "bool"
        assert s.default is False
        assert s.editable is True
        assert not s.lock_reason.strip()

    def test_handler_gate_reads_the_registry(self, monkeypatch):
        """The `corpus-extraction` handler's enabled-check goes through
        `feature_enabled` with this switch's own config keys/env var — not
        a hand-rolled `get_value` pair (sync-map: "new user-visible switch")."""
        from app.instance_config import feature_enabled

        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert feature_enabled("extraction", "enabled", env_var="AGNES_EXTRACTION_ENABLED", default=False) is False
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        assert feature_enabled("extraction", "enabled", env_var="AGNES_EXTRACTION_ENABLED", default=False) is True


class TestGetSwitch:
    def test_returns_the_entry(self):
        assert get_switch("chat").name == "chat"

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            get_switch("no_such_switch")


class TestSwitchValueResolution:
    """The single resolution order: env > overlay/yaml > default.

    Exercised against `data_apps` / `studio` / `guardrails` rather than
    `chat`: `chat` declares `runtime_view` (its runtime reads the writable
    overlay alone via `load_chat_config`, not the merged config `get_value`
    reads), so `switch_value("chat")` raises — see
    `TestSwitchValueRefusesRuntimeViewSwitches` below. None of the three used
    here declare `runtime_view`, so they exercise the generic resolution
    order untouched by that carve-out.
    """

    def test_default_when_nothing_set(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert sw.switch_value("data_apps") is False

    def test_yaml_wins_over_default(self, monkeypatch):
        import app.switches as sw

        monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("data_apps") is True

    def test_env_wins_over_yaml(self, monkeypatch):
        import app.switches as sw

        monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "0")
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: True)
        assert sw.switch_value("data_apps") is False

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", ""])
    def test_falsy_env_spellings(self, monkeypatch, raw):
        """Read through `guardrails` (default True), so a spelling the parser
        failed to recognize as falsy shows up as a failure. On a default-False
        switch this whole class of test passes without parsing anything."""
        import app.switches as sw

        monkeypatch.setenv("AGNES_GUARDRAILS_ENABLED", raw)
        assert sw.switch_value("guardrails") is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", "on", "enabled", "banana"])
    def test_permissive_truthy_env_spellings(self, monkeypatch, raw):
        """Unrecognized values are TRUE — the documented convention. An
        operator's intent to enable must not degrade to disabled over a
        casing mismatch."""
        import app.switches as sw

        monkeypatch.setenv("AGNES_GUARDRAILS_ENABLED", raw)
        assert sw.switch_value("guardrails") is True

    def test_unknown_switch_raises(self):
        import app.switches as sw

        with pytest.raises(KeyError):
            sw.switch_value("no_such_switch")


class TestSwitchValueRefusesRuntimeViewSwitches:
    """`switch_value()` reads the merged config; `chat` and `chat_approvals`
    do not run from there — `app/main.py` boots them via
    `load_chat_config(DATA_DIR/state/instance.yaml)`, the writable overlay
    file alone. Answering from `switch_value()` would be silently wrong
    (True for an instance that only set the flag in the static base, while
    the runtime it gates has it off), so it must raise instead."""

    def test_switch_value_chat_raises(self):
        import app.switches as sw

        with pytest.raises(ValueError, match="chat"):
            sw.switch_value("chat")

    def test_switch_value_chat_approvals_raises(self):
        import app.switches as sw

        with pytest.raises(ValueError, match="chat_approvals"):
            sw.switch_value("chat_approvals")

    def test_non_runtime_view_switch_is_unaffected(self):
        """Sanity check that the guard is scoped to `runtime_view` switches,
        not a blanket regression on `switch_value`.

        Reads `guardrails`, not `studio`: this assertion is only meaningful if
        the value it expects is not also what a swallowed failure would return,
        and `studio` defaults to False since the admin cleanup retired it.
        """
        import app.switches as sw

        assert sw.switch_value("guardrails") is True


class TestBackwardCompatibility:
    def test_feature_flags_still_importable_from_instance_config(self):
        from app.instance_config import FEATURE_FLAGS
        from app.switches import SWITCHES

        assert FEATURE_FLAGS is SWITCHES

    def test_feature_enabled_signature_is_unchanged(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.delenv("AGNES_TEST_FLAG", raising=False)
        monkeypatch.setattr(ic, "get_value", lambda *k, default=None: default)
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=True) is True
        assert ic.feature_enabled("a", "b", env_var="AGNES_TEST_FLAG", default=False) is False

    def test_registry_entries_still_expose_the_old_attribute_names(self):
        """`app/api/admin.py` and `app/web/router.py` read `.name`,
        `.config_keys`, `.env_var`, `.default` and `.description` off registry
        entries. `Switch` is a superset of `FeatureFlag`, so they keep working."""
        from app.instance_config import FEATURE_FLAGS

        for flag in FEATURE_FLAGS:
            assert isinstance(flag.name, str)
            assert isinstance(flag.config_keys, tuple)
            assert isinstance(flag.env_var, str)
            assert isinstance(flag.description, str)
            if flag.kind == "select":
                # First non-bool occupant: the `experience` preset. The panel
                # was taught its type — the inventory renders it through the
                # string-valued leading row (`value_label`, see
                # _feature_flags_inventory + renderFeatureFlags) and the
                # section editor through its `_KNOWN_FIELDS` select entry —
                # so a select's contract here is default ∈ options.
                assert flag.options, f"{flag.name}: a select switch must declare options"
                assert flag.default in flag.options, f"{flag.name}: select default {flag.default!r} not in options"
                continue
            if flag.kind == "int":
                # First `int` occupant: `acl_max_stale_hours`. The generic
                # `_feature_flags_inventory` loop (kind != "bool") already
                # renders it via `value_label`/`str(switch_value(...))` —
                # no bespoke render path needed, unlike `select`. Guard
                # against `bool` sneaking in here: `bool` is an `int`
                # subclass in Python, so `type(...) is int` (not
                # `isinstance`) is the correct check.
                assert type(flag.default) is int, f"{flag.name}: int switch default {flag.default!r} is not an int"
                continue
            assert isinstance(flag.default, bool), (
                f"{flag.name}: the boolean panel path renders this default as a switch; a "
                "non-bool default needs its own render path first (the `experience` select "
                "above shows the pattern)"
            )
