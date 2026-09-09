"""Tests for GET /api/admin/config-surface.

Covers:
- Auth gate (admin only).
- Payload shape: knobs list, initial_workspace, marketplaces, infra_repo_url.
- current_value/source correctly reflect env override vs yaml value vs default.
- infra_repo_url round-trips through the get_infra_repo_url() resolver.
"""

import os
from unittest.mock import patch


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestConfigSurfaceAuth:
    def test_admin_can_access(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 200, resp.text

    def test_non_admin_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/config-surface")
        assert resp.status_code == 401


class TestConfigSurfaceShape:
    def test_top_level_keys_present(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "knobs" in data
        assert "initial_workspace" in data
        assert "marketplaces" in data
        assert "infra_repo_url" in data

    def test_knobs_is_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["knobs"], list)

    def test_knob_entry_shape(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knobs = data["knobs"]
        assert knobs, "knobs list must not be empty"
        # Every knob must have the documented fields.
        required_fields = {"key", "resolver", "env_var", "yaml_path", "default", "current_value", "source"}
        for knob in knobs:
            missing = required_fields - knob.keys()
            assert not missing, f"knob {knob.get('resolver', '?')} missing fields: {missing}"

    def test_source_values_constrained(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        valid_sources = {"env", "yaml", "default"}
        for knob in data["knobs"]:
            assert knob["source"] in valid_sources, f"knob {knob['resolver']} has invalid source={knob['source']!r}"

    def test_marketplaces_is_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["marketplaces"], list)

    def test_initial_workspace_is_none_or_dict(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        iw = data["initial_workspace"]
        assert iw is None or isinstance(iw, dict), f"initial_workspace must be null or dict, got {type(iw)}"
        if isinstance(iw, dict):
            assert "url" in iw
            assert "branch" in iw
            assert "last_sync_sha" in iw

    def test_infra_repo_url_is_string(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["infra_repo_url"], str)

    def test_known_knob_present(self, seeded_app):
        """get_home_route must appear in the knobs list."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        resolvers = {k["resolver"] for k in data["knobs"]}
        assert "get_home_route" in resolvers

    def test_infra_repo_url_knob_present(self, seeded_app):
        """get_infra_repo_url must appear in the knobs list."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        resolvers = {k["resolver"] for k in data["knobs"]}
        assert "get_infra_repo_url" in resolvers


class TestConfigSurfaceSourceResolution:
    def test_source_is_default_when_nothing_set(self, seeded_app):
        """get_home_route resolves from default when no env/yaml is set."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Remove env override to ensure default path.
        env_without_override = {k: v for k, v in os.environ.items() if k != "AGNES_HOME_ROUTE"}
        with patch.dict("os.environ", env_without_override, clear=True):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_home_route"), None)
        assert knob is not None
        # In the test environment with no yaml and no env override, source=default.
        assert knob["source"] in ("default", "yaml")

    def test_theme_on_a_clean_instance_reports_source_default(self, seeded_app):
        """The catalogued `default` must be what the resolver actually returns
        when nothing is configured.

        `_source_for` infers `yaml` from `current_value != default`, so a stale
        default doesn't just mislabel one field — it makes a clean instance
        claim someone deliberately configured the theme. This endpoint is what
        the operator tooling reads, so that claim gets repeated as fact.

        Pinned strictly (`== "default"`), unlike the home_route test above
        which tolerates either: the whole point here is that the two agree.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        env_clean = {k: v for k, v in os.environ.items() if k != "AGNES_INSTANCE_THEME"}
        with patch.dict("os.environ", env_clean, clear=True):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_instance_theme"), None)
        assert knob is not None
        assert knob["current_value"] == knob["default"], (
            f"catalogued default {knob['default']!r} is not what get_instance_theme() "
            f"returns on a clean instance ({knob['current_value']!r})"
        )
        assert knob["source"] == "default"

    def test_source_is_env_when_env_var_set(self, seeded_app):
        """When AGNES_HOME_ROUTE is set, source=env and current_value matches."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch.dict("os.environ", {"AGNES_HOME_ROUTE": "/test-route"}):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_home_route"), None)
        assert knob is not None
        assert knob["source"] == "env"
        assert knob["current_value"] == "/test-route"

    def test_infra_repo_url_default_is_empty(self, seeded_app):
        """infra_repo_url knob default and current_value are both empty string."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        env_without_infra = {k: v for k, v in os.environ.items() if k != "AGNES_INFRA_REPO_URL"}
        with patch.dict("os.environ", env_without_infra, clear=True):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_infra_repo_url"), None)
        assert knob is not None
        assert knob["default"] == ""
        assert knob["source"] in ("default", "yaml")

    def test_infra_repo_url_round_trips_via_env(self, seeded_app):
        """AGNES_INFRA_REPO_URL env var reaches the endpoint as source=env."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        test_url = "https://github.example.com/org/infra-repo"
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": test_url}):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert data["infra_repo_url"] == test_url
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_infra_repo_url"), None)
        assert knob is not None
        assert knob["source"] == "env"
        assert knob["current_value"] == test_url


class TestGetInfraRepoUrl:
    """Unit-level test of the get_infra_repo_url() resolver in isolation."""

    def test_returns_empty_string_by_default(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        env = {k: v for k, v in os.environ.items() if k != "AGNES_INFRA_REPO_URL"}
        with patch.dict("os.environ", env, clear=True):
            result = get_infra_repo_url()
        assert result == ""

    def test_env_var_takes_priority(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": "https://git.example.com/infra"}):
            result = get_infra_repo_url()
        assert result == "https://git.example.com/infra"

    def test_strips_whitespace(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": "  https://git.example.com/infra  "}):
            result = get_infra_repo_url()
        assert result == "https://git.example.com/infra"


class TestFaviconKnob:
    """The favicon is an operator-facing branding knob like brand/logo_svg, so
    it has to appear in the introspection catalogue — and it is the first knob
    whose resolver runs a static asset through `static_url()`, which adds a
    `?v=<mtime>` cache-buster no declared default can match."""

    def test_favicon_knob_present(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        data = c.get("/api/admin/config-surface", headers=_auth(token)).json()
        resolvers = {k["resolver"] for k in data["knobs"]}
        assert "get_instance_favicon" in resolvers, (
            "favicon is documented in CONFIGURATION.md but missing from the catalogue"
        )

    def test_unconfigured_favicon_does_not_report_as_yaml(self, seeded_app):
        """`_source_for` infers `yaml` from `current != default`, and the
        resolved favicon always carries `?v=<mtime>`. Without stripping that,
        an instance which configured nothing would be told its favicon was set
        deliberately — the same trap `instance_theme` documents."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        data = c.get("/api/admin/config-surface", headers=_auth(token)).json()
        knob = next(k for k in data["knobs"] if k["resolver"] == "get_instance_favicon")
        assert knob["source"] == "default", f"clean instance reports source={knob['source']!r}"
        assert knob["current_value"].split("?v=")[0] == "/static/img/agnes-orb.png"
        assert knob["default"] == "/static/img/agnes-orb.png", (
            "the declared default must be the SERVED url the resolver returns, not the bare asset path"
        )

    def test_cache_buster_stripping_is_opt_in(self):
        """Only knobs that declare `cache_busted` get the looser comparison, so
        a genuine `?v=` in some other knob's value still reads as configured."""
        from app.api.config_surface import _source_for

        assert _source_for(None, "r", "img/x.png?v=9", "img/x.png") == "yaml"
        assert _source_for(None, "r", "img/x.png?v=9", "img/x.png", cache_busted=True) == "default"
        assert _source_for(None, "r", "img/other.png?v=9", "img/x.png", cache_busted=True) == "yaml"


class TestEmptyEnvProvenance:
    """`_source_for` decides `env` from the env var's CONTENT, which is right
    for the knobs that read `os.environ.get(X) or get_value(...)` and wrong for
    the ones that go through `feature_enabled` — that takes any PRESENT value
    and coerces `""` to False. A rendered `.env` line with nothing after the
    `=` therefore turns such a feature off while the inventory blamed `yaml`,
    sending an operator to look for a config line that does not exist (Devin
    Review on #2419).

    The per-row `env_empty_overrides` flag is the fix. This sweep is the guard
    that the next row to need it gets it, since nothing else couples the two.
    """

    def _resolves_empty_as_override(self, resolver_name, env_var):
        import app.instance_config as ic

        fn = getattr(ic, resolver_name, None)
        if fn is None:
            return False
        had = os.environ.get(env_var)
        try:
            os.environ.pop(env_var, None)
            ic.reset_cache()
            unset = fn()
            os.environ[env_var] = ""
            ic.reset_cache()
            empty = fn()
        except Exception:  # noqa: BLE001 — a resolver that raises is not comparable, so skip it
            return False
        finally:
            os.environ.pop(env_var, None)
            if had is not None:
                os.environ[env_var] = had
            ic.reset_cache()
        return empty != unset

    def test_every_row_honouring_an_empty_override_declares_it(self):
        from app.api.config_surface import _KNOB_CATALOGUE

        missing = [
            e["key"]
            for e in _KNOB_CATALOGUE
            if e.get("env_var")
            and not e.get("env_empty_overrides")
            and self._resolves_empty_as_override(e["resolver"], e["env_var"])
        ]
        assert not missing, (
            "these knobs treat a present-but-empty env var as an override, so their "
            f'source must be reported as `env` — add "env_empty_overrides": True to '
            f"their _KNOB_CATALOGUE rows: {missing}"
        )

    def test_the_flag_is_not_set_where_an_empty_override_is_ignored(self):
        """The other direction, so the flag cannot be sprinkled on by habit: a
        knob that ignores an empty value must NOT claim `env` for one."""
        from app.api.config_surface import _KNOB_CATALOGUE

        wrong = [
            e["key"]
            for e in _KNOB_CATALOGUE
            if e.get("env_empty_overrides")
            and e.get("env_var")
            and not self._resolves_empty_as_override(e["resolver"], e["env_var"])
        ]
        assert not wrong, (
            "these knobs ignore a present-but-empty env var, so declaring "
            f"env_empty_overrides would mislabel their source as `env`: {wrong}"
        )
