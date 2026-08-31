"""Creating an out-of-domain account warns, and deliberately does not refuse.

`app/api/users.py` never read `get_allowed_domains()`, so on an instance with
`auth.allowed_domain` set an admin could create an account, complete the whole
invite flow and tick the setup checklist for someone who can never sign in.

It warns rather than refuses on purpose: Google, Microsoft and the magic-link
provider all enforce the allowlist, but `keboola`, `password` and `sso` do
not, so an out-of-domain account is legitimate on an instance offering one of
those. Refusing would break that configuration.
"""

import pytest


@pytest.fixture
def client(seeded_app):
    """`POST /api/users` is admin-gated, so this needs a real admin token —
    an unauthenticated client returns 401 and every assertion on the body
    passes vacuously."""
    c = seeded_app["client"]
    c.headers.update({"Authorization": f"Bearer {seeded_app['admin_token']}"})
    return c


def _allow(monkeypatch, domains):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_allowed_domains", lambda: domains)


class TestDomainWarning:
    def test_an_out_of_domain_address_is_still_created(self, client, monkeypatch):
        """Warn, never refuse — some providers do not check the domain."""
        _allow(monkeypatch, ["acme.com"])
        r = client.post("/api/users", json={"email": "x@elsewhere.com", "name": "X"})
        assert r.status_code == 201

    def test_it_carries_a_warning_naming_the_allowed_domains(self, client, monkeypatch):
        _allow(monkeypatch, ["acme.com"])
        body = client.post("/api/users", json={"email": "y@elsewhere.com", "name": "Y"}).json()
        assert body.get("domain_warning")
        assert "acme.com" in body["domain_warning"]

    def test_the_warning_says_what_will_not_work(self, client, monkeypatch):
        """A bare "outside the allowlist" leaves the admin to guess the effect."""
        _allow(monkeypatch, ["acme.com"])
        body = client.post("/api/users", json={"email": "z@elsewhere.com", "name": "Z"}).json()
        assert "sign in" in body["domain_warning"].lower()

    def test_an_in_domain_address_is_not_warned_about(self, client, monkeypatch):
        _allow(monkeypatch, ["acme.com"])
        body = client.post("/api/users", json={"email": "ok@acme.com", "name": "OK"}).json()
        assert body.get("domain_warning") is None

    def test_the_check_is_case_insensitive(self, client, monkeypatch):
        _allow(monkeypatch, ["acme.com"])
        body = client.post("/api/users", json={"email": "Mixed@ACME.com", "name": "M"}).json()
        assert body.get("domain_warning") is None

    def test_no_allowlist_means_no_warning(self, client, monkeypatch):
        """The default instance pins no domain; every address is legitimate."""
        _allow(monkeypatch, [])
        body = client.post("/api/users", json={"email": "any@wherever.io", "name": "A"}).json()
        assert body.get("domain_warning") is None
