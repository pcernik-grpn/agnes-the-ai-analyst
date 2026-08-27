"""Tests for ``GET /api/admin/server-config/overlay`` — the export
projection behind ``agnes admin config export`` / ``agnes admin config
apply`` (Track D3).

Deliberately a DIFFERENT view than ``GET /api/admin/server-config``:

- ``GET /server-config`` returns ``load_instance_config()``'s merged
  (static + overlay, env-resolved) view, meant to prefill the settings-form
  UI, and masks every secret-shaped key (including env-var NAME references
  like ``token_env``) for display.
- ``GET /server-config/overlay`` returns exactly the on-disk OVERLAY —
  unresolved, filtered to ``_EDITABLE_SECTIONS`` — so it round-trips
  byte-for-byte through ``POST /server-config``. A bare env-var NAME
  (``token_env``) or an unresolved ``${VAR}`` reference is not itself a
  secret and passes through; a literal cleartext value under a
  secret-shaped key is omitted.
"""

import yaml


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestGetServerConfigOverlayAuth:
    def test_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay")
        assert resp.status_code == 401

    def test_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestGetServerConfigOverlayProjection:
    def test_empty_overlay_returns_empty_sections(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"] == {}
        assert "editable_sections" in data

    def test_only_editable_sections_present_in_overlay_are_returned(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "instance": {"name": "Acme Analyst"},
                    "theme": {"primary_color": "#123456"},
                    "not_an_editable_section": {"foo": "bar"},
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        sections = resp.json()["sections"]
        assert sections["instance"] == {"name": "Acme Analyst"}
        assert sections["theme"] == {"primary_color": "#123456"}
        assert "not_an_editable_section" not in sections

    def test_does_not_resolve_env_var_placeholders(self, seeded_app, tmp_path, monkeypatch):
        """Export must reflect the raw overlay, not the env-resolved merged
        view — a `${VAR}` reference must round-trip as the reference, never
        the cleartext value it resolves to."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SMTP_PASSWORD", "hunter2-cleartext-secret")
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump({"email": {"smtp_host": "smtp.example.com", "smtp_password": "${SMTP_PASSWORD}"}})
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        email = resp.json()["sections"]["email"]
        assert email["smtp_host"] == "smtp.example.com"
        assert email["smtp_password"] == "${SMTP_PASSWORD}"
        assert "hunter2-cleartext-secret" not in resp.text

    def test_literal_secret_is_omitted(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump({"email": {"smtp_host": "smtp.example.com", "smtp_password": "literal-cleartext"}})
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        email = resp.json()["sections"]["email"]
        assert email["smtp_host"] == "smtp.example.com"
        assert "smtp_password" not in email
        assert "literal-cleartext" not in resp.text

    def test_env_name_reference_keys_pass_through(self, seeded_app, tmp_path, monkeypatch):
        """`token_env`/`private_key_env` hold env-var NAMES, not secrets —
        export must NOT drop them (unlike GET /server-config's display-only
        redaction, which masks them for the settings form)."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "data_source": {
                        "snowflake": {
                            "account": "acme-xy12345",
                            "token_env": "SNOWFLAKE_PASSWORD",
                        }
                    }
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        sf = resp.json()["sections"]["data_source"]["snowflake"]
        assert sf["account"] == "acme-xy12345"
        assert sf["token_env"] == "SNOWFLAKE_PASSWORD"


class TestOverlayExportApplyRoundTrip:
    def test_round_trip_is_idempotent(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        original = {
            "instance": {"name": "Acme Analyst", "subtitle": "Data"},
            "theme": {"primary_color": "#112233"},
            "data_source": {
                "type": "keboola",
                "keboola": {"stack_url": "https://connection.keboola.com"},
            },
        }
        (state / "instance.yaml").write_text(yaml.dump(original))
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        exported = c.get("/api/admin/server-config/overlay", headers=_auth(token)).json()["sections"]
        assert exported == original

        # Re-apply the exported sections as a fresh POST — must reproduce
        # the identical overlay on disk (idempotent).
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": exported},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        reapplied = yaml.safe_load((state / "instance.yaml").read_text())
        assert reapplied == original

        # And exporting again gives byte-identical sections.
        exported_again = c.get("/api/admin/server-config/overlay", headers=_auth(token)).json()["sections"]
        assert exported_again == original

    def test_apply_with_redacted_secret_preserves_existing_secret(self, seeded_app, tmp_path, monkeypatch):
        """A round-tripped export omits a literal secret; re-applying it
        must NOT wipe the secret already on disk — the omitted key is simply
        absent from the patch, so deep-merge leaves the existing value."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        original = {"email": {"smtp_host": "smtp.example.com", "smtp_password": "real-secret-value"}}
        (state / "instance.yaml").write_text(yaml.dump(original))
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        exported = c.get("/api/admin/server-config/overlay", headers=_auth(token)).json()["sections"]
        assert "smtp_password" not in exported["email"]

        resp = c.post("/api/admin/server-config", json={"sections": exported}, headers=_auth(token))
        assert resp.status_code == 200, resp.text

        reapplied = yaml.safe_load((state / "instance.yaml").read_text())
        assert reapplied["email"]["smtp_password"] == "real-secret-value"
        assert reapplied["email"]["smtp_host"] == "smtp.example.com"


class TestFreeFormSectionFailClosed:
    """`connectors` is a free-form, admin-typed dict — per-connector slugs
    (e.g. `connector-slack`) come from the runtime seed manifest, not any
    static registry, and the section's own hint text SANCTIONS storing a
    literal credential there (`SLACK_WEBHOOK_URL`, `CONSUMER_KEY`, …). A
    key-name blocklist (`_SECRET_KEY_PATTERNS`) cannot police a key it has
    never seen, so export must fail closed for this whole section: keep
    only `${VAR}` references and `*_env`-named keys, omit every other
    literal — regression coverage for the leak a key-name-only scrub let
    through (BLOCKING finding on PR #1607)."""

    def test_literal_connector_credential_is_omitted(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "connectors": {
                        "connector-slack": {
                            "SLACK_WEBHOOK_URL": (
                                "https://hooks.chat-example.test/services/T00000000/B00000000/7fA9kQ3mZpL1xR2vN4hT6bY8"
                            ),
                            "CONSUMER_KEY": "a-literal-consumer-key-value",
                        }
                    }
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        slack = data["sections"]["connectors"]["connector-slack"]
        assert "SLACK_WEBHOOK_URL" not in slack
        assert "CONSUMER_KEY" not in slack
        assert "hooks.chat-example.test" not in resp.text
        assert "a-literal-consumer-key-value" not in resp.text
        assert "connectors.connector-slack.SLACK_WEBHOOK_URL" in data["omitted_keys"]
        assert "connectors.connector-slack.CONSUMER_KEY" in data["omitted_keys"]

    def test_env_ref_and_env_name_key_survive_inside_free_form_section(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "connectors": {
                        "connector-slack": {
                            "SLACK_WEBHOOK_URL": "${SLACK_WEBHOOK_URL}",
                            "webhook_url_env": "SLACK_WEBHOOK_URL",
                        }
                    }
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        slack = resp.json()["sections"]["connectors"]["connector-slack"]
        assert slack["SLACK_WEBHOOK_URL"] == "${SLACK_WEBHOOK_URL}"
        assert slack["webhook_url_env"] == "SLACK_WEBHOOK_URL"
        assert resp.json()["omitted_keys"] == []


class TestValueShapeBackstop:
    """Defense-in-depth: a literal that is unambiguously credential-shaped
    is omitted regardless of section or key name — catches a secret pasted
    into an innocuously-named field anywhere in the overlay."""

    def test_webhook_url_under_innocuous_key_is_omitted(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "email": {
                        "smtp_host": "smtp.example.com",
                        "notify_url": (
                            "https://hooks.chat-example.test/services/T00000000/B00000000/7fA9kQ3mZpL1xR2vN4hT6bY8"
                        ),
                    }
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        email = data["sections"]["email"]
        assert email["smtp_host"] == "smtp.example.com"
        assert "notify_url" not in email
        assert "hooks.chat-example.test" not in resp.text
        assert "email.notify_url" in data["omitted_keys"]

    def test_jwt_under_innocuous_key_is_omitted(self, seeded_app, tmp_path, monkeypatch):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQmXybGkeE0GtxdxBFn6VOGZKfoGeD7Kbk"
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(yaml.dump({"instance": {"name": "Acme", "auth_ref": jwt}}))
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"]["instance"] == {"name": "Acme"}
        assert jwt not in resp.text
        assert "instance.auth_ref" in data["omitted_keys"]

    def test_pem_block_under_innocuous_key_is_omitted(self, seeded_app, tmp_path, monkeypatch):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQ\n-----END PRIVATE KEY-----"
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(yaml.dump({"instance": {"name": "Acme", "keypair": pem}}))
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"]["instance"] == {"name": "Acme"}
        assert "BEGIN PRIVATE KEY" not in resp.text
        assert "instance.keypair" in data["omitted_keys"]

    def test_ordinary_values_are_not_flagged(self, seeded_app, tmp_path, monkeypatch):
        """Conservatism check: hostnames, emails, short enums, and normal
        URLs must survive — the backstop must not nuke legit config."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "instance": {"name": "Acme Analyst", "subtitle": "Data"},
                    "server": {"hostname": "agnes.example.com"},
                    "auth": {"allowed_domain": "example.com"},
                    "data_source": {
                        "type": "keboola",
                        "keboola": {"stack_url": "https://connection.keboola.com"},
                    },
                }
            )
        )
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"]["instance"] == {"name": "Acme Analyst", "subtitle": "Data"}
        assert data["sections"]["server"] == {"hostname": "agnes.example.com"}
        assert data["sections"]["auth"] == {"allowed_domain": "example.com"}
        assert data["sections"]["data_source"]["keboola"]["stack_url"] == "https://connection.keboola.com"
        assert data["omitted_keys"] == []


class TestFreeFormApplyRoundTrip:
    def test_apply_of_export_with_omitted_connector_secret_preserves_existing_value(
        self, seeded_app, tmp_path, monkeypatch
    ):
        """Mirrors `test_apply_with_redacted_secret_preserves_existing_secret`
        for the free-form-section path: the omitted literal is simply absent
        from the re-applied patch, so deep-merge leaves the existing value
        on disk untouched."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        original = {
            "connectors": {
                "connector-slack": {
                    "SLACK_WEBHOOK_URL": (
                        "https://hooks.chat-example.test/services/T00000000/B00000000/7fA9kQ3mZpL1xR2vN4hT6bY8"
                    )
                }
            }
        }
        (state / "instance.yaml").write_text(yaml.dump(original))
        import app.instance_config as ic

        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        exported = c.get("/api/admin/server-config/overlay", headers=_auth(token)).json()["sections"]
        assert "SLACK_WEBHOOK_URL" not in exported["connectors"]["connector-slack"]

        resp = c.post("/api/admin/server-config", json={"sections": exported}, headers=_auth(token))
        assert resp.status_code == 200, resp.text

        reapplied = yaml.safe_load((state / "instance.yaml").read_text())
        assert (
            reapplied["connectors"]["connector-slack"]["SLACK_WEBHOOK_URL"]
            == (original["connectors"]["connector-slack"]["SLACK_WEBHOOK_URL"])
        )
