"""``AGNES_DATA_APPS_SUBDOMAIN_BASE`` env override + the cookie-domain it drives.

`data_apps.subdomain_base` is the supported fix for the same-origin hazard
(`docs/architecture.md#hosted-data-apps`): apps served from `<slug>.<base>` are
cross-origin with the Agnes `/api`, so CORS blocks the credentialed read that
same-origin serving cannot close. Until this override existed the key was
reachable ONLY by hand-editing `config/instance.yaml` — the `data_apps` section
is locked in the server-config overlay (`app/switches.py`) and the
customer-instance module writes env lines, not yaml — which is exactly the
Terraform-says-one-thing-the-box-says-another drift that already bit
`data_apps.enabled`.

The cookie half matters as much as the serving half: `session_cookie_domain()`
widens the session cookie to the base's PARENT so one login covers the app
subdomains. That widening is the reason the base must be chosen carefully
(`apps.<host>` — not `apps.<registrable-domain>`, which would post the session
cookie to every unrelated host under it), and the reason the override is
ignored while the feature is off.
"""

from __future__ import annotations

import pytest

import app.instance_config as ic


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    monkeypatch.delenv("AGNES_DATA_APPS_RUNTIME_IMAGE", raising=False)
    monkeypatch.delenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", raising=False)


def _yaml(block):
    """Stub `get_value` so instance.yaml carries exactly ``block``."""

    def fake(*keys, default=None):
        if keys == ("data_apps",):
            return block
        return default

    return fake


# --------------------------------------------------------------------------
# get_data_apps_config() — the override
# --------------------------------------------------------------------------


def test_unset_leaves_config_untouched(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps.agnes.example.com"}))
    assert ic.get_data_apps_config()["subdomain_base"] == "apps.agnes.example.com"


def test_env_applies_when_enabled_via_env(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    assert ic.get_data_apps_config()["subdomain_base"] == "apps.agnes.example.com"


def test_env_applies_when_enabled_via_yaml(clean_env, monkeypatch):
    """The sibling `AGNES_DATA_APPS_RUNTIME_IMAGE` is keyed on the env-enable
    path and silently no-ops on a yaml-enabled instance. This key must not be:
    it drives the cookie domain, which cannot depend on HOW the feature was
    switched on."""
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    assert ic.get_data_apps_config()["subdomain_base"] == "apps.agnes.example.com"


def test_env_ignored_while_feature_disabled(clean_env, monkeypatch):
    """A stale env line must never widen the session cookie for a feature that
    is not serving anything."""
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": False}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    cfg = ic.get_data_apps_config()
    assert cfg.get("subdomain_base", "") == ""
    assert ic.session_cookie_domain() is None


def test_yaml_base_dropped_when_env_disables_the_feature(clean_env, monkeypatch):
    """The mirror image of the test above, and the case its first cut missed
    (Devin Review on #1588): the base sits in instance.yaml and the KILL comes
    from env. Gating only the env override left the yaml value intact, and
    neither reader checks `enabled` for itself — so `AGNES_DATA_APPS_ENABLED=
    false` switched the feature off while the session cookie stayed widened to
    the parent domain and `<slug>.<base>` hosts kept being routed.
    """
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps.agnes.example.com"}))
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    cfg = ic.get_data_apps_config()
    assert cfg.get("subdomain_base", "") == ""
    assert ic.session_cookie_domain() is None


def test_yaml_base_dropped_when_yaml_disables_the_feature(clean_env, monkeypatch):
    """Neither half of the config comes from env here — a `data_apps:` block
    someone switched off but left a base in still must not widen the cookie."""
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": False, "subdomain_base": "apps.agnes.example.com"}))
    cfg = ic.get_data_apps_config()
    assert cfg.get("subdomain_base", "") == ""
    assert ic.session_cookie_domain() is None


def test_yaml_base_dropped_when_enabled_key_is_absent(clean_env, monkeypatch):
    """`enabled` omitted means off (the feature ships off), so a base alone
    does not switch on the half of the feature that touches every login."""
    monkeypatch.setattr(ic, "get_value", _yaml({"subdomain_base": "apps.agnes.example.com"}))
    assert ic.get_data_apps_config().get("subdomain_base", "") == ""
    assert ic.session_cookie_domain() is None


def test_subdomain_routing_off_while_the_feature_is(clean_env, monkeypatch):
    """The cookie is one reader; `DataAppSubdomainMiddleware` is the other.
    With the feature off it must be a plain passthrough even for a host that
    matches the configured base — no path rewrite, and (the part that matters
    for #1562's same-origin gate) no `agnes_data_app_subdomain` marker, which
    is precisely what tells the proxy this request may be served.
    """
    import asyncio

    import app.data_apps_subdomain as subdomain_mod

    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": False, "subdomain_base": "apps.example.com"}))

    seen = []

    async def inner_app(scope, receive, send):
        seen.append((scope["path"], scope.get("agnes_data_app_subdomain")))

    middleware = subdomain_mod.DataAppSubdomainMiddleware(inner_app)
    scope = {"type": "http", "path": "/dash", "headers": [(b"host", b"s.apps.example.com")]}
    asyncio.run(middleware(scope, None, None))
    assert seen == [("/dash", None)]


def test_env_ignored_when_no_data_apps_block_at_all(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    assert ic.get_data_apps_config().get("subdomain_base", "") == ""


def test_env_wins_over_yaml(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "old.example.com"}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    assert ic.get_data_apps_config()["subdomain_base"] == "apps.agnes.example.com"


def test_empty_env_forces_path_prefix_mode(clean_env, monkeypatch):
    """Env wins in BOTH directions, per the canonical flag convention (#1022)."""
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps.agnes.example.com"}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "")
    assert ic.get_data_apps_config()["subdomain_base"] == ""


def test_whitespace_stripped(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "  apps.agnes.example.com  ")
    assert ic.get_data_apps_config()["subdomain_base"] == "apps.agnes.example.com"


# --------------------------------------------------------------------------
# session_cookie_domain() — the widening this key drives
# --------------------------------------------------------------------------


def test_cookie_domain_none_without_base(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True}))
    assert ic.session_cookie_domain() is None


def test_cookie_domain_is_the_bases_parent(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps.agnes.example.com"}))
    assert ic.session_cookie_domain() == ".agnes.example.com"


def test_cookie_domain_widens_to_registrable_domain_for_a_shallow_base(clean_env, monkeypatch):
    """Documents the sharp edge: `apps.<registrable-domain>` posts the session
    cookie to EVERY host under that domain, including unrelated services. The
    deployment must pick `apps.<agnes-host>` instead — this test exists so the
    consequence is visible in the suite rather than discovered in production."""
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps.example.com"}))
    assert ic.session_cookie_domain() == ".example.com"


def test_cookie_domain_none_for_single_label_base(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True, "subdomain_base": "apps"}))
    assert ic.session_cookie_domain() is None


def test_cookie_domain_follows_the_env_override(clean_env, monkeypatch):
    monkeypatch.setattr(ic, "get_value", _yaml({"enabled": True}))
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", "apps.agnes.example.com")
    assert ic.session_cookie_domain() == ".agnes.example.com"
