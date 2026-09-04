"""Tests for admin configure and registry API endpoints."""

import ipaddress
import socket
from unittest.mock import patch

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAdminConfigure:
    def test_configure_local_source(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data_source"] == "local"

    def test_configure_invalid_source_type_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "invalid_source"},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "data_source" in resp.json()["detail"].lower() or "must be" in resp.json()["detail"]

    def test_configure_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_configure_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
        )
        assert resp.status_code == 401

    def test_configure_bigquery_missing_project_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery"},  # missing bigquery_project
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_configure_bigquery_with_project(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery", "bigquery_project": "my-project"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data_source"] == "bigquery"

    def test_configure_missing_data_source_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={},  # missing data_source entirely
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_configure_overlay_does_not_resolve_env_var_placeholders(self, seeded_app, tmp_path, monkeypatch):
        """Regression: pre-fix `/api/admin/configure` seeded `existing` from
        the static config when no overlay existed, then wrote the whole
        thing back. Static `${SMTP_PASSWORD}` placeholders got resolved
        by `config.loader` along the way, so the cleartext secret landed
        in the writable overlay file even though the wizard only sets
        `instance` / `auth` / `data_source`. The narrow-overlay rewrite
        must read the overlay verbatim (or empty) and write only those
        three sections — same contract as `/api/admin/server-config`.
        """
        import yaml as _yaml

        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "instance.yaml").write_text(
            _yaml.dump(
                {
                    "instance": {"name": "Old"},
                    "auth": {"allowed_domain": "example.com", "webapp_secret_key": "x"},
                    "server": {"host": "1.2.3.4", "hostname": "example.com"},
                    "email": {
                        "smtp_host": "smtp.example.com",
                        "smtp_password": "${SMTP_PASSWORD}",
                    },
                }
            )
        )
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CONFIG_DIR", str(static_dir))
        monkeypatch.setenv("SMTP_PASSWORD", "hunter2-cleartext-secret")
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        from pathlib import Path as _Path
        import config.loader as _loader_mod

        monkeypatch.setattr(_loader_mod, "CONFIG_DIR", _Path(static_dir))
        from app.instance_config import reset_cache

        reset_cache()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local", "instance_name": "New"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        overlay_text = (tmp_path / "state" / "instance.yaml").read_text()
        assert "hunter2-cleartext-secret" not in overlay_text, (
            f"env-resolved secret leaked into overlay:\n{overlay_text}"
        )
        overlay = _yaml.safe_load(overlay_text)
        # email/server/auth.webapp_secret_key are static-only here — wizard
        # never touches them, so they must not appear in the overlay.
        assert "email" not in overlay
        assert "server" not in overlay
        # The wizard's three sections DO land:
        assert overlay["instance"]["name"] == "New"
        assert overlay["data_source"]["type"] == "local"

    def test_corrupt_overlay_refused_with_500_not_silently_overwritten(self, seeded_app, tmp_path, monkeypatch):
        """Symmetric to the server-config editor: /configure must refuse to
        overwrite a corrupt overlay so the operator can investigate, instead
        of silently dropping every previously-saved section."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        overlay_path = state / "instance.yaml"
        overlay_path.write_text("instance: {name: 'good'\nauth:\n\tallowed_domain: bad")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local", "instance_name": "New"},
            headers=_auth(token),
        )
        assert resp.status_code == 500, resp.text
        assert "corrupt overlay" in resp.json()["detail"]
        assert overlay_path.read_text().startswith("instance: {name: 'good'")


class TestAdminConfigureSSRF:
    """SSRF protection: keboola_url must not point to private/reserved networks.

    Uses socket.getaddrinfo + ipaddress checks — tests mock DNS resolution
    so they work regardless of the test runner's network/IPv6 config.
    """

    @staticmethod
    def _mock_getaddrinfo(host, port, **kwargs):
        """Predictable DNS resolution for tests — returns the IP literal as-is."""
        try:
            ip = ipaddress.ip_address(host)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(ip), port))]
        except ValueError:
            # Not an IP literal — let real DNS resolve (for public URL test)
            return socket.getaddrinfo(host, port, **kwargs)

    def test_configure_rejects_localhost_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://localhost:8080"},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "private" in resp.json()["detail"].lower() or "reserved" in resp.json()["detail"].lower()

    def test_configure_rejects_127_0_0_1_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://127.0.0.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_10_0_0_1_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://10.0.0.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_192_168_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://192.168.1.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_169_254_metadata_url(self, seeded_app):
        """169.254.x.x (link-local) must be rejected — cloud metadata endpoint."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://169.254.169.254"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_loopback(self, seeded_app):
        """IPv6 loopback ::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_link_local(self, seeded_app):
        """IPv6 link-local fe80::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[fe80::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_unique_local(self, seeded_app):
        """IPv6 unique-local fc00::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[fc00::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_multicast(self, seeded_app):
        """IPv6 multicast ff02::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[ff02::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_accepts_public_url(self, seeded_app):
        """A public URL should pass SSRF validation (connection test may still fail)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://connection.keboola.com"},
            headers=_auth(token),
        )
        # Should NOT be 400 with SSRF message — may be 400 from failed connection test, or 200
        if resp.status_code == 400:
            assert "private" not in resp.json()["detail"].lower()


class TestServerConfigAuthProvidersValidation:
    """`_validate_auth_providers_in_patch` gate on POST /api/admin/server-config.

    Spec (2026-08-12 keboola auth provider): an explicitly empty
    ``auth.providers`` list is a config error — one overlay write must never
    be able to lock every user out — so the admin API rejects it with 422.
    ``null`` is the documented "clear the override" value (validator
    early-returns; reader treats it as unset = all providers), and a
    non-empty list is accepted and persisted.

    ``auth`` is a danger section, so every request here carries
    ``confirm_danger=true`` — without it the danger gate 400s before the
    providers validator runs (asserted explicitly below so nobody mistakes
    that 400 for the 422 contract).
    """

    def test_empty_providers_list_rejected_with_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": []}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "empty" in resp.json()["detail"]

    def test_comma_separated_string_providers_accepted(self, seeded_app):
        """The admin API accepts the comma-separated string form too — it is a
        value the runtime resolver honors from yaml/env, so rejecting it here
        would lock an operator whose config spells providers as text out of
        saving the auth section (Devin review on #1288). `password` is always
        available, so the list has a usable method."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": "password,google"}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_all_unknown_string_providers_rejected_with_422(self, seeded_app):
        """The all-unknown guard applies to the string form as well."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": "gogle,keybola"}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "no known provider" in resp.json()["detail"]

    def test_all_unknown_providers_list_rejected_with_422(self, seeded_app):
        """A list of only misspelled names would fail open to all providers at
        runtime; the admin API surfaces the typo as a 422 instead of silently
        re-enabling every sign-in method."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["gogle", "keybola"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "no known provider" in resp.json()["detail"]

    def test_partially_known_providers_list_accepted(self, seeded_app):
        """A list with at least one known AND available name is accepted (the
        runtime uses the known subset and warns about the rest)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["password", "gogle"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_all_unavailable_providers_rejected_with_422(self, seeded_app):
        """Known but unconfigured providers (no Google OAuth, no Keboola stack
        in the test env) admit nobody as written; the runtime's rescue would
        treat the list as unset — ALL sign-in methods — with a loud error, the
        opposite of the operator's intent. The admin API refuses at save time
        so the operator learns now instead of shipping that."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["google", "keboola"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "no usable sign-in method" in resp.json()["detail"]

    def test_google_only_refusal_explains_the_env_var_probe(self, seeded_app):
        """`providers: [google]` with a yaml-only Google config 422s here
        because google.is_available() reads GOOGLE_CLIENT_ID/SECRET captured
        at import time — the refusal is inexplicable to an operator who just
        filled Google settings into instance.yaml unless the detail says so
        (Devin Review on PR #1288)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["google"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "no usable sign-in method" in detail
        assert "GOOGLE_CLIENT_ID" in detail and "GOOGLE_CLIENT_SECRET" in detail

    def test_microsoft_only_is_accepted_when_the_env_is_configured(self, seeded_app, monkeypatch):
        """`providers: [microsoft]` must be savable once the three env vars are
        set. The write path keeps its own availability probe, separate from the
        runtime registry, and a provider missing from it is known-but-never-
        available — so narrowing an instance to Microsoft was refused as "no
        usable sign-in method" no matter how it was configured."""
        import app.auth.providers.microsoft as ms

        monkeypatch.setattr(ms, "is_available", lambda: True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["microsoft"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_microsoft_only_still_refused_when_the_env_is_absent(self, seeded_app, monkeypatch):
        """The other half of the contract: the branch reports the real probe,
        it does not hardcode availability."""
        import app.auth.providers.microsoft as ms

        monkeypatch.setattr(ms, "is_available", lambda: False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["microsoft"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "no usable sign-in method" in resp.json()["detail"]

    def test_microsoft_refusal_names_microsoft_and_its_env_vars(self, seeded_app, monkeypatch):
        """Mirrors the Google note: availability is read from the process
        environment at start, so a refusal that names neither Microsoft nor
        its three env vars leaves the operator with nothing to act on."""
        import app.auth.providers.microsoft as ms

        monkeypatch.setattr(ms, "is_available", lambda: False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["microsoft"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "MICROSOFT_TENANT_ID" in detail
        assert "MICROSOFT_CLIENT_ID" in detail
        assert "MICROSOFT_CLIENT_SECRET" in detail

    def test_http_auth_keboola_stack_url_is_refused(self, seeded_app):
        """auth.keboola URLs are held to the source-connection bar
        (_validate_stack_url rejects non-https): they carry credentials at
        use time, so a cleartext scheme is refused at store time
        (Devin Review on PR #1288)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"auth": {"keboola": {"stack_url": "http://connection.example.com"}}},
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "must be https" in resp.text

    def test_http_databricks_host_is_refused(self, seeded_app):
        """`data_source.databricks.host` is where the workspace PAT is sent, so a
        cleartext scheme must fail closed at store time (`validate_workspace_host`
        inside `_validate_urls_in_patch`) rather than leak the bearer token over
        http on the first Statement Execution API call."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"data_source": {"databricks": {"host": "http://dbc-a1b2c3d4-e5f6.example.com"}}},
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "data_source.databricks.host" in resp.text
        assert "https" in resp.text

    def test_bare_databricks_host_is_normalized_to_https(self, seeded_app):
        """A bare workspace address (what an admin copies out of the Databricks UI)
        is accepted and upgraded to `https://…` *before* it is persisted and before
        the SSRF check runs, so the stored value is the one the client will dial.

        DNS is stubbed to a public address (same pattern as TestAdminConfigureSSRF)
        so the test does not depend on the runner's resolver.
        """
        import yaml

        def _public_dns(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", _public_dns):
            resp = c.post(
                "/api/admin/server-config",
                json={
                    "sections": {"data_source": {"databricks": {"host": "dbc-a1b2c3d4-e5f6.example.com/"}}},
                    "confirm_danger": True,
                },
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        overlay = yaml.safe_load((seeded_app["env"]["data_dir"] / "state" / "instance.yaml").read_text())
        assert overlay["data_source"]["databricks"]["host"] == "https://dbc-a1b2c3d4-e5f6.example.com"

    def test_keboola_enabled_and_configured_in_same_save_accepted(self, seeded_app):
        """Enabling keboola AND supplying its config in one save must pass —
        availability is evaluated against the current config merged with the
        patch, not the pre-save config alone."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {
                    "auth": {
                        "providers": ["keboola"],
                        "keboola": {
                            "client_id": "cid",
                            "client_secret": "csecret",
                            "project_id": "12345",
                            "stack_url": "https://example.com",
                        },
                    }
                },
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_empty_providers_without_confirm_danger_hits_danger_gate_first(self, seeded_app):
        """auth is a danger section: without confirm_danger the request 400s
        at the danger gate before the providers validator ever runs."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": []}}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "confirm_danger" in resp.json()["detail"]

    def test_nonempty_providers_accepted_and_persisted(self, seeded_app):
        import yaml

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": ["password"]}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        overlay = yaml.safe_load((seeded_app["env"]["data_dir"] / "state" / "instance.yaml").read_text())
        assert overlay["auth"]["providers"] == ["password"]

    def test_masking_sentinel_secret_does_not_fake_keboola_availability(self, seeded_app):
        """The availability check must run on the SCRUBBED patch: a masking
        sentinel (`***`) round-tripped from the GET payload for a secret leaf is
        truthy and, on the raw patch, would falsely report keboola as available
        and let a [keboola]-only lockout through. With no real client_secret
        stored, the sentinel is stripped and the save is correctly rejected
        (Devin review on #1288)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {
                    "auth": {
                        "providers": ["keboola"],
                        "keboola": {
                            "client_id": "cid",
                            "client_secret": "***",  # masking sentinel, not a real value
                            "project_id": "12345",
                            "stack_url": "https://example.com",
                        },
                    }
                },
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "no usable sign-in method" in resp.json()["detail"]

    def test_keboola_login_and_datasource_stack_in_one_save_accepted(self, seeded_app):
        """keboola's stack_url falls back to data_source.keboola.stack_url; an
        admin who configures the login AND the data-source address in one save
        (auth.keboola has no stack_url of its own) must not be refused — the
        fallback is evaluated against this patch's data_source, not only the
        stored config (Devin review on #1288)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {
                    "auth": {
                        "providers": ["keboola"],
                        "keboola": {"client_id": "cid", "client_secret": "csecret", "project_id": "12345"},
                    },
                    "data_source": {"keboola": {"stack_url": "https://example.com"}},
                },
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_non_dict_keboola_block_rejected_with_422(self, seeded_app):
        """A malformed auth.keboola (not an object) must 422 with a clear
        message, not crash the availability merge with a 500."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"keboola": "oops-a-string"}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "auth.keboola must be an object" in resp.json()["detail"]

    def test_clearing_sole_providers_config_without_touching_providers_rejected(self, monkeypatch):
        """The lockout guard fires even when the patch does NOT touch
        auth.providers: an instance already restricted to [keboola] must not be
        able to clear keboola's config in a separate save and self-lock-out."""
        from fastapi import HTTPException
        import app.api.admin as admin

        # Existing effective allowlist = [keboola]; nothing configured for it.
        def fake_get_value(*keys, default=None):
            if keys == ("auth", "providers"):
                return ["keboola"]
            return default

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        # A save that touches auth.keboola but not auth.providers.
        with pytest.raises(HTTPException) as exc:
            admin._validate_auth_providers_in_patch({"auth": {"keboola": {"client_id": ""}}})
        assert exc.value.status_code == 422
        assert "no usable sign-in method" in exc.value.detail

    def test_unrelated_auth_save_not_blocked_when_providers_unset(self, monkeypatch):
        """When no allowlist is configured, an unrelated auth save must pass —
        the runtime offers all providers, so there is no lockout to guard."""
        import app.api.admin as admin

        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        # Must not raise.
        admin._validate_auth_providers_in_patch({"auth": {"allowed_domain": "example.com"}})

    def test_unrelated_auth_save_not_blocked_for_env_provider_allowlist(self, monkeypatch):
        """An env-configured provider's availability (google/email) can't be
        changed by any server-config save, so an unrelated auth save (e.g.
        allowed_domain) against a pre-existing [google] allowlist must NOT be
        re-validated — only a patch that touches auth.keboola re-checks, since
        that is the only availability a config save can break (Devin #1288).
        Otherwise the admin would be dead-ended with no config field to fix it."""
        import app.api.admin as admin

        def fake_get_value(*keys, default=None):
            if keys == ("auth", "providers"):
                return ["google"]  # env-configured, unavailable in the API process
            return default

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        # Patch does NOT touch auth.keboola → must not raise despite google
        # being unavailable.
        admin._validate_auth_providers_in_patch({"auth": {"allowed_domain": "example.com"}})

    def test_null_providers_accepted_as_clear_override(self, seeded_app):
        """``providers: null`` is NOT rejected — the validator early-returns
        on None, the overlay persists the null, and the allowlist reader
        (`configured_allowlist`) treats it as unset = every provider."""
        import yaml

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"providers": None}}, "confirm_danger": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        overlay = yaml.safe_load((seeded_app["env"]["data_dir"] / "state" / "instance.yaml").read_text())
        assert overlay["auth"]["providers"] is None
        # And the reader side: null resolves to "no allowlist" (all providers).
        import app.instance_config as ic

        ic._instance_config = None
        try:
            from app.auth.provider_registry import configured_allowlist

            assert configured_allowlist() is None
        finally:
            ic._instance_config = None


class TestAdminRegistry:
    def test_list_registry_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        assert "count" in data
        assert data["count"] == 0

    def test_list_registry_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 403

    def test_list_registry_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/registry")
        assert resp.status_code == 401

    def test_list_registry_surfaces_sync_state_fields(self, seeded_app):
        """#754: the admin_sync.html dashboard (and `agnes admin
        list-tables`) render `last_sync` / `last_sync_status` / `rows` /
        `file_size_bytes` straight off this response — pre-fix the
        endpoint only ever returned `last_sync_error` / `last_sync_display`,
        so those columns silently rendered as "never synced" / "0 synced"
        even for tables that had synced fine. Three registry rows, one per
        outcome: never-synced, synced ok, and errored."""
        from src.repositories import sync_state_repo, table_registry_repo

        registry = table_registry_repo()
        registry.register(id="never", name="never", source_type="keboola", bucket="in.c-x")
        registry.register(id="synced", name="synced", source_type="keboola", bucket="in.c-x")
        registry.register(id="failed", name="failed", source_type="keboola", bucket="in.c-x")

        state = sync_state_repo()
        state.update_sync(table_id="synced", rows=100, file_size_bytes=2048, hash="abc")
        state.set_error("failed", "connection refused")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 200
        by_name = {t["name"]: t for t in resp.json()["tables"]}

        never = by_name["never"]
        assert never["last_sync"] is None
        assert never["last_sync_status"] == "pending"
        assert never["rows"] is None
        assert never["file_size_bytes"] is None

        synced = by_name["synced"]
        assert synced["last_sync"] is not None
        assert synced["last_sync_status"] == "ok"
        assert synced["rows"] == 100
        assert synced["file_size_bytes"] == 2048

        failed = by_name["failed"]
        assert failed["last_sync_status"] == "error"
        assert failed["last_sync_error"] == "connection refused"


class TestRegisterTable:
    def test_register_table_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "orders",
                "source_type": "keboola",
                "bucket": "in.c-crm",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "orders"
        assert data["name"] == "orders"
        assert data["status"] == "registered"

    def test_register_table_appears_in_registry(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        c.post(
            "/api/admin/register-table",
            json={"name": "customers", "source_type": "keboola"},
            headers=_auth(token),
        )

        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tables"]]
        assert "customers" in names

    def test_register_duplicate_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Register once
        c.post(
            "/api/admin/register-table",
            json={"name": "dup_table"},
            headers=_auth(token),
        )

        # Register again
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "dup_table"},
            headers=_auth(token),
        )
        assert resp.status_code == 409

    def test_register_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "new_table"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_register_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "new_table"},
        )
        assert resp.status_code == 401

    def test_register_table_with_all_fields(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "full_table",
                "source_type": "keboola",
                "bucket": "in.c-crm",
                "source_table": "full_table",
                "query_mode": "local",
                "sync_schedule": "daily 06:00",
                "description": "Full configuration table",
                "profile_after_sync": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201

    def test_register_table_accepts_string_primary_key_for_backcompat(self, seeded_app):
        """primary_key changed from Optional[str] to Optional[List[str]] in
        0.14.0. Pydantic v2 doesn't coerce, so without a backward-compat
        normalizer a CLI script posting `"primary_key": "session_id"` would
        hit a 422. The field validator wraps a bare string in a one-element
        list so old and new callers both work."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "single_pk", "primary_key": "session_id"},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text

        resp = c.post(
            "/api/admin/register-table",
            json={"name": "composite_pk", "primary_key": ["session_id", "event_date"]},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text

    def test_register_table_rejects_hyphen_in_name(self, seeded_app):
        """Table names that produce unsafe DuckDB identifiers (e.g. hyphens) must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "crm-contact", "source_type": "keboola"},
            headers=_auth(token),
        )
        assert resp.status_code == 422
        assert "unsafe identifier" in resp.json()["detail"].lower() or "crm-contact" in resp.json()["detail"]

    def test_register_table_rejects_special_chars(self, seeded_app):
        """Table names with special characters beyond underscores must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        for bad_name in ["my.table", "order$s", "table name!"]:
            resp = c.post(
                "/api/admin/register-table",
                json={"name": bad_name, "source_type": "keboola"},
                headers=_auth(token),
            )
            assert resp.status_code == 422, f"Expected 422 for name={bad_name!r}, got {resp.status_code}"


class TestDeleteRegistryTable:
    def test_delete_registered_table(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Register
        c.post(
            "/api/admin/register-table",
            json={"name": "to_delete"},
            headers=_auth(token),
        )

        # Delete
        resp = c.delete("/api/admin/registry/to_delete", headers=_auth(token))
        assert resp.status_code == 204

        # Verify gone from registry
        list_resp = c.get("/api/admin/registry", headers=_auth(token))
        names = [t["name"] for t in list_resp.json()["tables"]]
        assert "to_delete" not in names

    def test_delete_nonexistent_table_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.delete("/api/admin/registry/nonexistent_table", headers=_auth(token))
        assert resp.status_code == 404

    def test_delete_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.delete("/api/admin/registry/some_table", headers=_auth(token))
        assert resp.status_code == 403

    def test_delete_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete("/api/admin/registry/some_table")
        assert resp.status_code == 401


class TestDiscoverAndRegister:
    def test_discover_and_register_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/discover-and-register", headers=_auth(token))
        assert resp.status_code == 403

    def test_discover_and_register_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/discover-and-register")
        assert resp.status_code == 401

    def test_discover_and_register_non_keboola_returns_zero(self, seeded_app):
        """With no keboola config, discover-and-register returns 0 registered tables."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Configure as local (non-keboola)
        c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )

        resp = c.post("/api/admin/discover-and-register", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] == 0
        assert data["source"] != "keboola"


class TestDocumentedServerConfigKeysAreWritable:
    """`POST /api/admin/server-config` validates the patch against
    `_EDITABLE_SECTIONS` and 400s on anything else, so a settings key the
    deployment guide tells operators to change there must have its section
    listed — otherwise the documented remediation fails and only the env var
    works, which is how `mcp.allow_query_param_token` shipped
    (Devin Review on #1183).
    """

    def _documented_keys(self):
        """`section.key:` pairs the deployment guide writes in backticks.

        Deliberately NARROW, and deliberately one file. Widening this to
        `CONFIGURATION.md` + `feature-flags.md` and to the bare `` `a.b` `` form
        was tried and reverted: it matched ordinary prose (`architecture.md`,
        `redis.…`, `uvicorn.…`) and turned a precise ratchet into a list of
        twelve false positives. Prose is the wrong source of truth for a
        machine-checkable rule — `_registry_sections` is the right one, and it is
        what actually caught the case this scrape missed.
        """
        import re
        from pathlib import Path

        # Anchored on this file, not the cwd: pytest invoked from anywhere else
        # would otherwise make the test vacuous rather than fail.
        doc_path = Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md"
        doc = doc_path.read_text(encoding="utf-8")
        # `section.key: value` inside backticks, as the guide writes them.
        return set(re.findall(r"`([a-z_]+)\.([a-z_]+):", doc))

    def _registry_sections(self):
        """Sections owned by the switch registry."""
        from app.switches import SWITCHES

        return {s.config_keys[0] for s in SWITCHES if s.config_keys}

    #: Sections the deployment guide documents that own no registry switch YET.
    #:
    #: NOT a revival of `_NOT_LIVE_WRITABLE`. That dict answered "why is this
    #: registered flag not writable?", and the registry now answers it itself via
    #: `Switch.lock_reason`. What is left is a different and strictly smaller
    #: question: DEPLOYMENT.md documents these three, but no switch owns them, so
    #: neither derived set can speak for them.
    #:
    #: Temporary by construction. PR2 of this effort registers switches for
    #: `analytics.backend`, `coordination.backend` and `distribution.signed_urls`;
    #: `test_no_switch_backed_section_is_still_listed_as_undeclared` below fails
    #: the moment one of them lands, forcing its removal from here.
    _DOCUMENTED_BUT_NOT_SWITCH_BACKED = {
        "analytics": "backend choice is governed by the state machine + a data migration, not a live patch",
        "coordination": "process topology; takes effect on restart, and the guide pairs it with a compose change",
        "distribution": "documented as an `instance.yaml` + `AGNES_DISTRIBUTION_*` pair, object-store credentials included",
    }

    def _locked_sections(self):
        """Sections whose only switches are locked, each WITH a stated reason.

        Replaces the old `_NOT_LIVE_WRITABLE` dict: the reason now lives on
        the entry, where the product can show it, instead of in this file
        where only a test reader ever saw it. A locked switch with an empty
        `lock_reason` does NOT count as accounted for here — that is what
        makes `test_every_registry_section_is_editable_or_locked_with_a_reason`
        below a real check on the reason rather than just on `editable`.
        """
        from app.switches import SWITCHES

        editable = {s.config_keys[0] for s in SWITCHES if s.editable and s.config_keys}
        locked = {s.config_keys[0] for s in SWITCHES if not s.editable and s.config_keys and s.lock_reason.strip()}
        return locked - editable

    def test_the_documented_key_scrape_finds_something(self):
        """Guards the guard: a doc rewrite that changes the backtick convention
        would silently empty `documented` and make the ratchet below pass on an
        empty set."""
        assert len(self._documented_keys()) >= 5
        assert len(self._registry_sections()) >= 4

    def test_every_registry_section_is_editable_or_locked_with_a_reason(self):
        from app.api.admin import _EDITABLE_SECTIONS

        unaccounted = self._registry_sections() - set(_EDITABLE_SECTIONS) - self._locked_sections()
        assert not unaccounted, (
            "switch section neither writable via /admin/server-config nor backed by a locked "
            f"switch carrying a lock_reason: {sorted(unaccounted)}. The panel displays it, so "
            "a save that 400s is the operator's only signal."
        )

    def test_no_locked_section_is_also_editable(self):
        """Shrinks-only, in its new form: a switch that became editable must
        clear `lock_reason`, and its section must not appear in both sets."""
        from app.api.admin import _EDITABLE_SECTIONS

        stale = self._locked_sections() & set(_EDITABLE_SECTIONS)
        assert not stale, f"now editable — clear lock_reason on: {sorted(stale)}"

    def test_editable_switch_section_derivation_is_pinned(self):
        """NOT a regression guard: `_EDITABLE_SECTIONS` is defined as
        `_STATIC_EDITABLE_SECTIONS | {section of every editable switch}`, so
        this predicate holds by construction for any registry content — it
        cannot fail no matter what `SWITCHES` contains. It exists to pin the
        derivation in prose next to the definition it restates, and to make a
        future rewrite of `_EDITABLE_SECTIONS` that breaks the union show up
        here. The bug shape this derivation actually prevents (an editable
        switch shipping section-less, as `mcp.allow_query_param_token` did) is
        guarded by `test_every_registry_section_is_editable_or_locked_with_a_reason`
        above, which checks the *registry* against `_EDITABLE_SECTIONS` rather
        than checking `_EDITABLE_SECTIONS` against itself."""
        from app.api.admin import _EDITABLE_SECTIONS
        from app.switches import SWITCHES

        for s in SWITCHES:
            if s.editable and s.config_keys:
                assert s.config_keys[0] in _EDITABLE_SECTIONS, (
                    f"{s.name} is editable but section {s.config_keys[0]!r} is not writable"
                )

    def test_every_documented_section_is_editable_or_explicitly_exempt(self):
        from app.api.admin import _EDITABLE_SECTIONS

        documented = {sec for sec, _ in self._documented_keys()}
        unaccounted = (
            documented - set(_EDITABLE_SECTIONS) - self._locked_sections() - set(self._DOCUMENTED_BUT_NOT_SWITCH_BACKED)
        )
        assert not unaccounted, (
            "documented in DEPLOYMENT.md but neither writable via /admin/server-config, "
            "nor backed by a locked switch carrying a lock_reason, nor listed in "
            f"_DOCUMENTED_BUT_NOT_SWITCH_BACKED with a reason: {sorted(unaccounted)}"
        )

    def test_no_switch_backed_section_is_still_listed_as_undeclared(self):
        """Shrinks-only. A section that gains a switch must leave the dict —
        otherwise a stale entry would keep excusing a section the registry can
        now speak for, which is how the exemption this replaced grew stale."""
        from app.switches import SWITCHES

        owned = {s.config_keys[0] for s in SWITCHES if s.config_keys}
        stale = owned & set(self._DOCUMENTED_BUT_NOT_SWITCH_BACKED)
        assert not stale, f"now switch-backed — drop from _DOCUMENTED_BUT_NOT_SWITCH_BACKED: {sorted(stale)}"

    def test_every_editable_section_declares_its_fields(self):
        """Editable ⇒ declared. True for all 19 sections today, so it is a real
        invariant rather than an aspiration.

        Adding a section to `_EDITABLE_SECTIONS` without a `_KNOWN_FIELDS` entry
        is a quiet half-job: the API accepts the patch, but the panel has no
        `kind` to render from, and an undeclared boolean is not covered by
        `_declared_boolean_fields()` — which is what makes `_mask(False)` turn an
        OFF switch into an ON one. Both halves of that bug were live at once for
        `mcp` (#1183); this pins the coupling so the next section cannot repeat
        it.
        """
        from app.api.admin import _EDITABLE_SECTIONS, _KNOWN_FIELDS

        undeclared = [s for s in _EDITABLE_SECTIONS if s not in _KNOWN_FIELDS]
        assert not undeclared, (
            "editable via /admin/server-config but no _KNOWN_FIELDS entry, so the panel "
            f"cannot render it and its booleans escape the mask carve-out: {undeclared}"
        )

    def test_every_editable_bool_switch_is_declared_as_a_bool_field(self):
        """Editable bool switch ⇒ a `kind: "bool"` declaration at its exact
        config path, nested object levels included. The section-level guard
        above cannot see this: `auth` had a `_KNOWN_FIELDS` entry, yet
        `auth.keboola.allow_token_header` (config path three levels deep) had
        no field declaration, so /admin/server-config rendered a free-text box
        instead of a toggle for the switch (Devin Review on PR #1288)."""
        from app.api.admin import _KNOWN_FIELDS
        from app.switches import SWITCHES

        missing = []
        for s in SWITCHES:
            if not (s.editable and s.config_keys and s.kind == "bool"):
                continue
            node = _KNOWN_FIELDS.get(s.config_keys[0], {})
            # Walk intermediate levels through their object declarations
            # (e.g. auth → keboola.fields) down to the leaf's parent.
            for key in s.config_keys[1:-1]:
                node = ((node.get(key) or {}).get("fields")) or {}
            spec = node.get(s.config_keys[-1]) or {}
            if spec.get("kind") != "bool":
                missing.append(".".join(s.config_keys))
        assert not missing, (
            "editable bool switch with no kind='bool' _KNOWN_FIELDS declaration at its "
            f"config path — the panel renders a text box instead of a toggle: {missing}"
        )

    def test_the_chat_and_studio_flags_render_as_booleans(self):
        """The two sections this change made writable, pinned the same way the
        `mcp` switch is below — a regression to free-text or to a masked boolean
        is the failure mode, not a missing section."""
        from app.api.admin import _EDITABLE_SECTIONS, _KNOWN_FIELDS, _is_secret_key

        # Defaults come from FEATURE_FLAGS, so they are asserted against the
        # registry rather than re-typed here — an earlier version of this test
        # hard-coded approvals_enabled=False and thereby pinned the very drift
        # the registry-derived declaration removed (Devin Review on #1190).
        for section, field in (
            ("chat", "enabled"),
            ("chat", "approvals_enabled"),
            ("studio", "enabled"),
        ):
            default = next(
                f.default
                for f in __import__("app.instance_config", fromlist=["x"]).FEATURE_FLAGS
                if f.config_keys and f.config_keys[0] == section and f.config_keys[-1] == field
            )
            assert section in _EDITABLE_SECTIONS, section
            spec = _KNOWN_FIELDS[section][field]
            assert spec["kind"] == "bool", (section, field)
            assert spec["default"] is default, (section, field)
            assert _is_secret_key(field) is False, f"{field} must not be masked"

    def test_the_chat_sender_limits_render_as_numbers_with_runtime_defaults(self):
        """The three per-sender limits `enforce_sender_limits` applies —
        `daily_anthropic_spend_usd`, `max_session_tokens`,
        `rate_messages_per_hour` — were documented as configurable in
        /admin/server-config, but the panel only renders declared fields plus
        whatever the overlay already holds, so an instance that had never
        hand-edited its YAML showed nothing to raise: the daily spend cap could
        only be lifted with a YAML edit on the data disk. Pinned as numeric
        kinds (a free-text box would post a string) with defaults derived from
        `ChatConfig` — the same one-copy rule the flag defaults follow."""
        import dataclasses

        from app.api.admin import _KNOWN_FIELDS, _SECTION_BASELINE_EFFECT, _is_secret_key
        from app.chat.config import ChatConfig

        runtime = {f.name: f.default for f in dataclasses.fields(ChatConfig)}
        for field, kind in (
            ("daily_anthropic_spend_usd", "float"),
            ("max_session_tokens", "int"),
            ("rate_messages_per_hour", "int"),
        ):
            spec = _KNOWN_FIELDS["chat"][field]
            assert spec["kind"] == kind, field
            assert spec["default"] == runtime[field], field
            assert type(spec["default"]) is type(runtime[field]), field
            assert "restart" in spec["hint"], f"{field}: hint must say it applies after a restart"
            assert _is_secret_key(field) is False, f"{field} must not be masked"
        # The hints promise a restart because app.state.chat_config is built
        # once at boot; the save response must say the same.
        assert _SECTION_BASELINE_EFFECT["chat"] == "restart"
        # And `agnes admin config export` keeps the budget: the key-name gate
        # used to omit it as a "secret" literal, so an exported overlay lost it.
        from app.api.admin import _export_scrub

        omitted: list[str] = []
        kept = _export_scrub({"chat": {"max_session_tokens": 123456, "api_token": "x"}}, omitted=omitted)
        assert kept == {"chat": {"max_session_tokens": 123456}}
        assert omitted == ["chat.api_token"]

    def test_declared_defaults_match_the_registry(self):
        """No second copy of a flag's default.

        `chat.approvals_enabled` was hand-declared as off while `FEATURE_FLAGS`
        (and the runtime) had it on, so the panel described the opposite of what
        the system does — and the unset-boolean renderer then wrote that wrong
        default back on the next save. The declarations derive from the registry
        now; this fails if anyone re-introduces a literal (Devin Review on #1190).
        """
        from app.api.admin import _KNOWN_FIELDS
        from app.instance_config import FEATURE_FLAGS

        mismatched = []
        for flag in FEATURE_FLAGS:
            if not flag.config_keys:
                continue
            section, key = flag.config_keys[0], flag.config_keys[-1]
            spec = _KNOWN_FIELDS.get(section, {}).get(key)
            if spec is not None and spec.get("default") != flag.default:
                mismatched.append(f"{section}.{key}: registry={flag.default} declared={spec.get('default')}")
        assert not mismatched, "declared default contradicts FEATURE_FLAGS:\n" + "\n".join(mismatched)

    def test_the_bool_renderer_falls_back_to_the_declared_default(self):
        """An unset boolean must render from its default, not from `!!undefined`.

        The admin panel coerces with `!!value`, so a never-configured switch whose
        real default is ON rendered as OFF and "Save section" wrote that false
        back — enabling chat silently disabled tool-call approvals, and saving the
        Studio section disabled Studio. The text branch already used the registry
        default when unset; the bool branch now does too. Asserted against the
        template source because that is where the coercion lives
        (Devin Review on #1190).
        """
        from pathlib import Path

        tpl = (
            Path(__file__).resolve().parent.parent / "app" / "web" / "templates" / "admin_server_config.html"
        ).read_text(encoding="utf-8")
        assert "const v = isUnset ? dflt : !!value;" in tpl, (
            "the bool branch must fall back to the declared default when unset"
        )
        assert "const v = !!value;" not in tpl, (
            "the bare `!!value` coercion is the bug: `!!undefined` is false, so an "
            "on-by-default switch renders OFF and the next save persists it"
        )

    def test_a_nested_secret_under_chat_is_masked_and_not_written_back(self):
        """Making `chat` editable exposes the whole block, nested keys included.

        A real deployment can carry `chat.slack.*`, so the question is whether the
        existing redaction reaches a nested level or only the top one. It reaches:
        `_redact` recurses into dicts and masks any leaf whose key looks like a
        credential, and the write path drops a value that is still the redaction
        sentinel so a save cannot persist `"***"` over a live secret. Asserted
        rather than manually spot-checked (Devin Review on #1190).
        """
        from app.api.admin import _REDACTED_SENTINELS, _is_secret_key, _redact

        block = {
            "enabled": True,
            "slack": {"transport": "http", "bot_token": "xoxb-REAL", "signing_secret": "sig-REAL"},
        }
        shown = _redact(block, "chat")

        assert shown["slack"]["bot_token"] not in ("xoxb-REAL",)
        assert shown["slack"]["signing_secret"] not in ("sig-REAL",)
        assert shown["slack"]["transport"] == "http", "a non-secret leaf must stay readable"
        assert shown["enabled"] is True

        # ...and what the panel shows back is a sentinel the write path refuses,
        # which is what stops a save from overwriting the real value with stars.
        assert shown["slack"]["bot_token"] in _REDACTED_SENTINELS
        assert _is_secret_key("bot_token") and _is_secret_key("signing_secret")
        assert not _is_secret_key("transport")

    def test_the_mcp_token_flag_renders_as_a_boolean(self):
        from app.api.admin import _EDITABLE_SECTIONS, _KNOWN_FIELDS

        assert "mcp" in _EDITABLE_SECTIONS
        field = _KNOWN_FIELDS["mcp"]["allow_query_param_token"]
        assert field["kind"] == "bool"
        assert field["default"] is False, "off by default since #1656; the switch opts back in"


class TestBooleanConfigFieldsAreNeverMasked:
    """`_mask(False)` returns `"***"`, and the admin UI's bool renderer coerces
    with `!!value` — so masking a boolean makes an OFF switch display as ON,
    and the next "Save section" posts `true` and silently undoes the operator's
    change. `mcp.allow_query_param_token` hit this because its name contains
    the substring "token" (Devin Review on #1183).
    """

    def test_a_declared_boolean_is_not_treated_as_a_secret(self):
        from app.api.admin import _is_secret_key

        assert _is_secret_key("allow_query_param_token") is False
        # ...while an actual credential still is.
        assert _is_secret_key("keboola_token") is True
        assert _is_secret_key("api_token") is True

    def test_a_false_boolean_survives_redaction_verbatim(self):
        from app.api.admin import _redact

        out = _redact({"mcp": {"allow_query_param_token": False}})
        assert out == {"mcp": {"allow_query_param_token": False}}, (
            "masked to a truthy string — the UI would show the switch as ON"
        )
        # The masking that matters is untouched.
        assert _redact({"data_source": {"api_token": "abc123"}}) == {"data_source": {"api_token": "***"}}

    def test_every_registry_boolean_is_covered_not_just_this_one(self):
        """Derived from `_KNOWN_FIELDS`, so a future boolean whose name happens
        to contain a secret-looking substring is covered without anyone
        remembering this failure mode."""
        from app.api.admin import _KNOWN_FIELDS, _is_secret_key

        bools = [
            name for section in _KNOWN_FIELDS.values() for name, spec in section.items() if spec.get("kind") == "bool"
        ]
        assert bools, "no boolean fields in the registry — this guard would be vacuous"
        assert [b for b in bools if _is_secret_key(b)] == []
