"""Tests for instance_config loading."""

import pytest


class TestInstanceConfig:
    def test_missing_config_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        from app.instance_config import get_instance_name

        name = get_instance_name()
        assert isinstance(name, str)

    def test_reads_nested_instance_name(self, tmp_path, monkeypatch):
        """get_instance_name should read instance.name from YAML, not flat instance_name."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme Analytics\n  subtitle: Data Team\n")

        import importlib
        import app.instance_config as mod

        # Reset cached config to force reload
        mod._instance_config = None
        importlib.reload(mod)

        assert mod.get_instance_name() == "Acme Analytics"
        assert mod.get_instance_subtitle() == "Data Team"

        # Cleanup: reset cache after test
        mod._instance_config = None


class TestAllowedDomains:
    """A domain is case-insensitive by definition (DNS), and the providers that
    consume this list compare against a claim they may have lower-cased —
    Microsoft lower-cases the resolved address, Google passes the raw claim
    through. An operator writing ``Acme.com`` would otherwise turn every
    Microsoft sign-in into ``domain_not_allowed`` while Google kept working."""

    def _load(self, tmp_path, monkeypatch, yaml_text):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(yaml_text)

        import importlib

        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_domains_are_lower_cased(self, tmp_path, monkeypatch):
        mod = self._load(tmp_path, monkeypatch, 'auth:\n  allowed_domain: "Acme.com, EXAMPLE.ORG"\n')
        try:
            assert mod.get_allowed_domains() == ["acme.com", "example.org"]
        finally:
            mod._instance_config = None

    def test_unset_is_empty(self, tmp_path, monkeypatch):
        mod = self._load(tmp_path, monkeypatch, "auth:\n  providers: [password]\n")
        try:
            assert mod.get_allowed_domains() == []
        finally:
            mod._instance_config = None


class TestInstanceBrand:
    """Brand and workspace_dir resolution: env > YAML > default,
    workspace_dir derives from brand when not explicitly set."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_brand_defaults_to_agnes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Agnes"
        mod._instance_config = None

    def test_brand_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme\n  brand: Foundry AI\n")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Foundry AI"
        mod._instance_config = None

    def test_brand_env_overrides_yaml(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme\n  brand: FromYaml\n")
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "FromEnv")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "FromEnv"
        mod._instance_config = None

    def test_brand_empty_falls_back_to_default(self, tmp_path, monkeypatch):
        # Empty env should not override the YAML/default to empty.
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "   ")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Agnes"
        mod._instance_config = None

    def test_workspace_dir_derives_from_brand(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "FoundryAI"
        mod._instance_config = None

    def test_workspace_dir_strips_all_non_alphanumeric(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "ACME's Data!")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "ACMEsData"
        mod._instance_config = None

    def test_workspace_dir_default_when_brand_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "Agnes"
        mod._instance_config = None

    def test_workspace_dir_explicit_env_overrides_derivation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        monkeypatch.setenv("AGNES_WORKSPACE_DIR_NAME", "fdry")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "fdry"
        mod._instance_config = None

    def test_workspace_dir_explicit_yaml_overrides_derivation(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  brand: Foundry AI\n  workspace_dir: fdry\n"
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "fdry"
        mod._instance_config = None

    def test_brand_flows_into_resolve_lines(self, tmp_path, monkeypatch):
        """Brand + workspace_dir substitute into the setup script lines."""
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.setup_instructions import resolve_lines

        joined = "\n".join(
            resolve_lines(
                "agnes.whl",
                instance_brand="Foundry AI",
                workspace_dir="FoundryAI",
            )
        )
        assert "Set up the Foundry AI CLI on this machine." in joined
        # Brand + workspace_dir thread through step 2's suggested-folder
        # copy (the directory decision itself lives in `agnes onboard`).
        # The default-path mention renders as `~/Desktop/...` (tilde), not
        # `$HOME/Desktop/...`.
        assert "~/Desktop/FoundryAI" in joined
        assert "2) Set up the Foundry AI workspace in the current directory:" in joined
        assert "Foundry AI workspace is ready" in joined
        # No raw placeholders survive substitution.
        assert "{instance_brand}" not in joined
        assert "{workspace_dir}" not in joined
        mod._instance_config = None

    def test_default_brand_keeps_agnes_branding(self, tmp_path, monkeypatch):
        """Backwards-compat: callers that don't pass brand/workspace_dir
        get the literal 'Agnes' / '~/Desktop/Agnes' rendering."""
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.setup_instructions import resolve_lines

        joined = "\n".join(resolve_lines("agnes.whl"))
        assert "Set up the Agnes CLI on this machine." in joined
        # Default path renders as `~/Desktop/Agnes` (tilde) inside step 2's
        # suggested-folder copy.
        assert "~/Desktop/Agnes" in joined
        assert "2) Set up the Agnes workspace in the current directory:" in joined
        assert "Agnes workspace is ready" in joined
        mod._instance_config = None


class TestInstanceBrandShort:
    """brand_short resolution: env > YAML > full brand. The short form is
    used mid-sentence in /home body copy; unset it mirrors the full brand
    so existing deployments render unchanged."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_defaults_to_full_brand(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme Data Analyst")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "Acme Data Analyst"
        mod._instance_config = None

    def test_defaults_to_agnes_when_brand_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "Agnes"
        mod._instance_config = None

    def test_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  brand: Acme Data Analyst\n  brand_short: Acme\n"
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "Acme"
        mod._instance_config = None

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme\n  brand_short: FromYaml\n")
        monkeypatch.setenv("AGNES_INSTANCE_BRAND_SHORT", "FromEnv")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "FromEnv"
        mod._instance_config = None

    def test_empty_falls_back_to_full_brand(self, tmp_path, monkeypatch):
        # Whitespace-only short must not blank out the brand mid-sentence.
        monkeypatch.setenv("AGNES_INSTANCE_BRAND_SHORT", "   ")
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "Foundry AI"
        mod._instance_config = None

    def test_yaml_whitespace_falls_back_to_full_brand(self, tmp_path, monkeypatch):
        # Same floor for the YAML path: `brand_short: "   "` with no env
        # override must fall back to the full brand, not render blank.
        monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme\n  brand: Foundry AI\n  brand_short: '   '\n")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand_short() == "Foundry AI"
        mod._instance_config = None


class TestDataSourceType:
    """get_data_source_type() — D1 residual precedence flip. Every other
    knob in this module resolves env > instance.yaml > default; this one is
    the deliberate exception: ``data_source.type`` (the overlay) wins over
    ``DATA_SOURCE`` (env), so a UI edit can never be shadowed by a stale
    Terraform-written env line. ``DATA_SOURCE`` remains a fallback for the
    common local-dev case of no overlay at all."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_overlay_wins_over_env(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("data_source:\n  type: bigquery\n")
        monkeypatch.setenv("DATA_SOURCE", "keboola")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_data_source_type() == "bigquery"
        mod._instance_config = None

    def test_env_is_fallback_when_overlay_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_SOURCE", "keboola")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_data_source_type() == "keboola"
        mod._instance_config = None

    def test_default_when_neither_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATA_SOURCE", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_data_source_type() == "local"
        mod._instance_config = None


class TestInstanceFavicon:
    """instance.favicon / AGNES_INSTANCE_FAVICON — favicon href resolution:
    env > YAML > the built-in agnes-orb asset. A data-URI or absolute URL is
    returned verbatim; anything else is resolved through the same
    ``static_url()`` cache-busting helper the templates use for every other
    static asset."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_default_resolves_orb_via_static_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_FAVICON", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.router import _static_url

        assert mod.get_instance_favicon() == _static_url("img/agnes-orb.png")
        mod._instance_config = None

    def test_yaml_static_path_resolved_through_static_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_FAVICON", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  favicon: img/agnes-orb.png\n")
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.router import _static_url

        assert mod.get_instance_favicon() == _static_url("img/agnes-orb.png")
        mod._instance_config = None

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  favicon: img/agnes-orb.png\n")
        monkeypatch.setenv("AGNES_INSTANCE_FAVICON", "https://cdn.example.com/icon.png")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_favicon() == "https://cdn.example.com/icon.png"
        mod._instance_config = None

    def test_data_uri_passthrough(self, tmp_path, monkeypatch):
        data_uri = "data:image/png;base64,iVBORw0KGgo="
        monkeypatch.setenv("AGNES_INSTANCE_FAVICON", data_uri)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_favicon() == data_uri
        mod._instance_config = None

    def test_absolute_url_passthrough(self, tmp_path, monkeypatch):
        url = "https://cdn.example.com/favicon.ico"
        monkeypatch.setenv("AGNES_INSTANCE_FAVICON", url)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_favicon() == url
        mod._instance_config = None

    def test_empty_env_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_FAVICON", "   ")
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.router import _static_url

        assert mod.get_instance_favicon() == _static_url("img/agnes-orb.png")
        mod._instance_config = None


class TestHiddenLoginFeatures:
    """instance.hide_login_features / AGNES_INSTANCE_HIDE_LOGIN_FEATURES —
    stable /login feature-card keys to hide. Resolution: env (comma-string) >
    YAML (list or comma-string) > empty; normalized to a lowercase,
    whitespace-stripped, de-duplicated frozenset."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def _write(self, tmp_path, yaml_body: str):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(yaml_body)

    def test_default_empty(self, tmp_path, monkeypatch):
        """Unset env + unset YAML → hide nothing (OSS vendor-neutral default)."""
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset()
        mod._instance_config = None

    def test_returns_frozenset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp")
        mod = self._reload(tmp_path, monkeypatch)
        assert isinstance(mod.get_hidden_login_features(), frozenset)
        mod._instance_config = None

    def test_env_comma_separated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp,memory")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_env_whitespace_case_and_dedupe(self, tmp_path, monkeypatch):
        """Whitespace trimmed, lowercased, empties dropped, duplicates collapsed."""
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", " MCP , Memory , mcp ,, ")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        self._write(tmp_path, "instance:\n  name: Acme\n  hide_login_features: [data]\n")
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp"})
        mod._instance_config = None

    def test_yaml_list(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        self._write(
            tmp_path,
            "instance:\n  name: Acme\n  hide_login_features:\n    - MCP\n    - memory\n",
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_yaml_comma_string(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        self._write(
            tmp_path,
            'instance:\n  name: Acme\n  hide_login_features: "mcp, memory"\n',
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None


class TestInstanceCustomPreamble:
    """instance.custom_preamble — operator-authored block injected at the
    top of the install prompt. Resolution: env > YAML > "" (mirrors the
    overview/support knobs)."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_defaults_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_CUSTOM_PREAMBLE", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_custom_preamble() == ""
        mod._instance_config = None

    def test_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_CUSTOM_PREAMBLE", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  custom_preamble: |\n    From YAML preamble\n"
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_custom_preamble() == "From YAML preamble"
        mod._instance_config = None

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text("instance:\n  name: Acme\n  custom_preamble: From YAML\n")
        monkeypatch.setenv("AGNES_INSTANCE_CUSTOM_PREAMBLE", "From env")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_custom_preamble() == "From env"
        mod._instance_config = None

    def test_empty_env_falls_back_to_empty_default(self, tmp_path, monkeypatch):
        # Whitespace-only env resolves to "" (stripped), not a stray blank block.
        monkeypatch.setenv("AGNES_INSTANCE_CUSTOM_PREAMBLE", "   ")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_custom_preamble() == ""
        mod._instance_config = None


class TestCustomScripts:
    """instance.custom_scripts — operator-injected HTML/JS blocks rendered
    by base.html. Validates the normalization + filtering done by
    get_custom_scripts() so the template can iterate over a clean list."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod

        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def _write(self, tmp_path, yaml_body: str):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(yaml_body)

    def test_yaml_absent_returns_empty_list(self, tmp_path, monkeypatch):
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_valid_entry_normalized(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - name: marker-io\n"
                "      enabled: true\n"
                "      placement: head_end\n"
                "      html: |\n"
                "        <script>window.markerConfig={project:'abc'};</script>\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert len(scripts) == 1
        s = scripts[0]
        assert s["name"] == "marker-io"
        assert s["enabled"] is True
        assert s["placement"] == "head_end"
        assert "markerConfig" in s["html"]
        mod._instance_config = None

    def test_disabled_entry_dropped(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - name: off\n"
                "      enabled: false\n"
                "      placement: head_end\n"
                "      html: <script>1</script>\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_empty_html_dropped(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - name: noop\n"
                "      enabled: true\n"
                "      placement: head_end\n"
                "      html: '   '\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_bad_placement_dropped_with_warning(self, tmp_path, monkeypatch, caplog):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - name: typo\n"
                "      enabled: true\n"
                "      placement: body_start\n"
                "      html: <script>1</script>\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        import logging

        with caplog.at_level(logging.WARNING, logger="app.instance_config"):
            assert mod.get_custom_scripts() == []
        assert any("unknown placement" in r.message for r in caplog.records)
        mod._instance_config = None

    def test_missing_placement_defaults_to_head_end(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - name: defaulting\n"
                "      enabled: true\n"
                "      html: <script>x</script>\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert len(scripts) == 1
        assert scripts[0]["placement"] == "head_end"
        mod._instance_config = None

    def test_three_placements_all_pass_through(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                "    - {name: a, enabled: true, placement: head_start, html: '<script>1</script>'}\n"
                "    - {name: b, enabled: true, placement: head_end,   html: '<script>2</script>'}\n"
                "    - {name: c, enabled: true, placement: body_end,   html: '<script>3</script>'}\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert [s["placement"] for s in scripts] == ["head_start", "head_end", "body_end"]
        assert [s["name"] for s in scripts] == ["a", "b", "c"]
        mod._instance_config = None

    def test_non_list_value_ignored_with_warning(self, tmp_path, monkeypatch, caplog):
        self._write(tmp_path, ("instance:\n  name: Acme\n  custom_scripts: not-a-list\n"))
        mod = self._reload(tmp_path, monkeypatch)
        import logging

        with caplog.at_level(logging.WARNING, logger="app.instance_config"):
            assert mod.get_custom_scripts() == []
        assert any("must be a list" in r.message for r in caplog.records)
        mod._instance_config = None

    @pytest.mark.parametrize(
        "enabled_yaml,expect_dropped",
        [
            # Boolean false in every YAML truthy-shape the operator might use.
            # All of these must drop the entry so the kill switch behaves the
            # same regardless of whether the operator pasted a quoted block.
            ("false", True),
            ("False", True),
            ('"false"', True),  # quoted string — bool("false") == True in Python
            ('"no"', True),
            ('"NO"', True),
            ('"off"', True),
            ('"0"', True),
            ("0", True),
            # Boolean true / typical live values must keep the entry alive.
            ("true", False),
            ("True", False),
            ('"true"', False),
            ('"yes"', False),
            ("1", False),
        ],
    )
    def test_enabled_coercion(self, tmp_path, monkeypatch, enabled_yaml, expect_dropped):
        """Quoted-string + numeric `enabled` values must be coerced the same
        way the operator expects from a Boolean field — the kill switch is
        the whole point of the field, and `bool("false") == True` would
        silently leave the snippet live (review PR #372)."""
        self._write(
            tmp_path,
            (
                "instance:\n"
                "  name: Acme\n"
                "  custom_scripts:\n"
                f"    - name: probe\n"
                f"      enabled: {enabled_yaml}\n"
                "      placement: head_end\n"
                "      html: <script>1</script>\n"
            ),
        )
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        if expect_dropped:
            assert scripts == [], f"enabled={enabled_yaml!r} should drop the entry but it survived"
        else:
            assert len(scripts) == 1, f"enabled={enabled_yaml!r} should keep the entry but it was dropped"
        mod._instance_config = None


class TestDataAppsConfig:
    """data_apps: — hosted user web apps feature toggle + defaults (v96).
    Resolution: YAML > default (no env override, mirrors corporate_memory)."""

    def _reset_cache(self):
        import app.instance_config as ic

        ic._instance_config = None

    def test_data_apps_config_defaults(self, monkeypatch):
        from app import instance_config

        instance_config._instance_config = None  # reset cache — match how sibling tests do it
        cfg = instance_config.get_data_apps_config()
        assert cfg.get("enabled", False) is False

    def test_data_apps_config_absent_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        self._reset_cache()
        from app.instance_config import get_data_apps_config

        assert get_data_apps_config() == {}

    def test_data_apps_config_reads_yaml_section(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True, "max_apps_per_user": 5}}))
        self._reset_cache()
        from app.instance_config import get_data_apps_config

        cfg = get_data_apps_config()
        assert cfg["enabled"] is True
        assert cfg["max_apps_per_user"] == 5


class TestLintKnobs:
    """Skill-linter config knobs: lint_max_body_chars, lint_duplicate_top_n,
    lint_audit_min_interval_hours. Resolution: YAML > default."""

    def _reset_cache(self):
        import app.instance_config as ic

        ic._instance_config = None

    def test_lint_max_body_chars_default_8000(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        self._reset_cache()
        from app.instance_config import get_lint_max_body_chars

        assert get_lint_max_body_chars() == 8000

    def test_lint_duplicate_top_n_default_5(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        self._reset_cache()
        from app.instance_config import get_lint_duplicate_top_n

        assert get_lint_duplicate_top_n() == 5

    def test_lint_audit_min_interval_hours_default_144(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        self._reset_cache()
        from app.instance_config import get_lint_audit_min_interval_hours

        assert get_lint_audit_min_interval_hours() == 144

    def test_lint_max_body_chars_yaml_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_max_body_chars": 4000}}))
        self._reset_cache()
        from app.instance_config import get_lint_max_body_chars

        assert get_lint_max_body_chars() == 4000

    def test_lint_duplicate_top_n_yaml_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_duplicate_top_n": 10}}))
        self._reset_cache()
        from app.instance_config import get_lint_duplicate_top_n

        assert get_lint_duplicate_top_n() == 10

    def test_lint_audit_min_interval_hours_yaml_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_audit_min_interval_hours": 72}}))
        self._reset_cache()
        from app.instance_config import get_lint_audit_min_interval_hours

        assert get_lint_audit_min_interval_hours() == 72

    def test_lint_string_value_coerced_to_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_max_body_chars": "6000"}}))
        self._reset_cache()
        from app.instance_config import get_lint_max_body_chars

        assert get_lint_max_body_chars() == 6000

    def test_lint_garbage_value_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_duplicate_top_n": "not-a-number"}}))
        self._reset_cache()
        from app.instance_config import get_lint_duplicate_top_n

        assert get_lint_duplicate_top_n() == 5

    def test_lint_zero_or_negative_floored_to_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        import yaml

        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_audit_min_interval_hours": 0}}))
        self._reset_cache()
        from app.instance_config import get_lint_audit_min_interval_hours

        assert get_lint_audit_min_interval_hours() == 1

        # Negative also floors to 1
        (state / "instance.yaml").write_text(yaml.dump({"guardrails": {"lint_audit_min_interval_hours": -50}}))
        self._reset_cache()
        assert get_lint_audit_min_interval_hours() == 1
