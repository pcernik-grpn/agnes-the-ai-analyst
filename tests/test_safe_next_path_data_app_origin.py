"""``safe_next_path`` may return to a data-app origin — and nowhere else new.

Signing in from an app subdomain bounces to the MAIN host's login (otherwise
the subdomain rewrite loops — see ``test_data_apps_proxy``). To land the caller
back in the app afterwards, the login flow's ``next`` has to survive a
cross-host target, which the open-redirect guard refuses by design.

So the guard learns exactly ONE new shape: an absolute URL on
``<single-label>.<data_apps.subdomain_base>`` — this deployment's own app
origins, and only while that base is configured and the feature is on. Every
classic open-redirect shape must still be refused, which is what most of this
file is about.

Residual risk, accepted deliberately: someone able to CREATE an app can make
their own app a post-login landing page. That is not a general open redirect —
the target is this deployment's own infrastructure, behind the same RBAC as any
other app — and closing it would mean a DB lookup inside a helper that runs on
every login.
"""

from __future__ import annotations

import pytest

from app.auth._common import safe_next_path

D = "/DEFAULT"
BASE = "apps.example.com"


@pytest.fixture
def apps_on(monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", BASE)


@pytest.fixture
def apps_off(monkeypatch):
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    monkeypatch.delenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", raising=False)


# --- the existing contract, unchanged --------------------------------------


@pytest.mark.parametrize("good", ["/catalog", "/foo?bar=baz", "/a/b/c"])
def test_same_origin_paths_still_pass(apps_on, good):
    assert safe_next_path(good, default=D) == good


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example/",
        "http://evil.example/",
        "https://evil.example/",
        "javascript:alert(1)",
        "dashboard",
        "",
        None,
    ],
)
def test_classic_open_redirects_still_refused(apps_on, hostile):
    assert safe_next_path(hostile, default=D) == D


# --- the one new shape ------------------------------------------------------


@pytest.mark.parametrize(
    "allowed",
    [
        f"https://s.{BASE}/",
        f"https://s.{BASE}/deep/path?q=1",
        f"http://s.{BASE}/",
        f"https://s.{BASE}:8443/",
    ],
)
def test_data_app_origin_allowed_when_configured(apps_on, allowed):
    assert safe_next_path(allowed, default=D) == allowed


def test_data_app_origin_refused_when_no_base_configured(apps_off):
    assert safe_next_path(f"https://s.{BASE}/", default=D) == D


def test_data_app_origin_refused_when_feature_disabled(monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", BASE)
    assert safe_next_path(f"https://s.{BASE}/", default=D) == D


# --- near-misses that must NOT be mistaken for an app origin ----------------


@pytest.mark.parametrize(
    "hostile",
    [
        f"https://{BASE}/",                      # the bare base is not an app
        f"https://a.s.{BASE}/",                  # multi-label: unroutable, no cert
        f"https://s.{BASE}.evil.test/",          # suffix-extension trick
        f"https://evil.test/?x=s.{BASE}",        # base only in the query
        f"https://evil.test/#s.{BASE}",          # base only in the fragment
        f"https://evil.com@s.{BASE}/",           # userinfo: phishing display
        f"https://s.{BASE}@evil.test/",          # real host is evil.test
        f"https:\\\\s.{BASE}/",                  # backslashes: browsers normalize
        f"ftp://s.{BASE}/",                      # non-web scheme
        f"//s.{BASE}/",                          # protocol-relative
        f"s.{BASE}/",                            # schemeless, not a path
    ],
)
def test_lookalikes_refused(apps_on, hostile):
    assert safe_next_path(hostile, default=D) == D
