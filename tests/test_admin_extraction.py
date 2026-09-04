"""Extraction observability API (2026-08-31 design §9, A1/A2/A3/A5).

Two layers, both DuckDB-backed (the default test backend):

* the route contract — admin gating, 404-before-any-repo-work, and the typed
  ``501 requires_postgres_backend`` the three run routes owe a DuckDB
  instance (``extraction_runs`` is a post-A3 PG-only table);
* the pure read-side rules — outcome precedence and derived liveness — as
  unit tests over ``_derived_outcome``, because those are the rules that keep
  a card from rendering a crashed or abandoned run as healthy, and they must
  be checkable without a database.

The PG happy path (a recorded run actually surfacing through the API) lives
in ``tests/db_pg/test_extraction_api_pg.py``.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE = "/api/admin/sharepoint/connections"


def _self_signed_pem() -> str:
    """A throwaway self-signed certificate + its private key, concatenated —
    same idiom as ``tests/test_admin_sharepoint.py``'s helper of the same
    name (kept local rather than shared: each test module in this codebase
    owns its own fixtures)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agnes-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem + key_pem


PEM = _self_signed_pem()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


_ROUTES = (
    "extraction/status",
    "extraction/runs",
    "extraction/runs/er_whatever",
    "extraction/config",
    "extraction/completeness",
    "extraction/breakdown",
)


class TestAuthGating:
    def test_every_route_requires_auth(self, seeded_app):
        for suffix in _ROUTES:
            r = seeded_app["client"].get(f"{BASE}/nope/{suffix}")
            assert r.status_code == 401, suffix

    def test_every_route_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        for suffix in _ROUTES:
            r = seeded_app["client"].get(f"{BASE}/nope/{suffix}", headers=_auth(token))
            assert r.status_code == 403, suffix


class TestUnknownConnection:
    def test_unknown_connection_is_404_not_501(self, seeded_app):
        """The connection lookup runs BEFORE any PG-only repo, so a typo'd
        id is a 404 on every backend — a 501 would tell an admin to migrate
        their database over a misspelling."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        for suffix in _ROUTES:
            r = client.get(f"{BASE}/does-not-exist/{suffix}", headers=_auth(token))
            assert r.status_code == 404, (suffix, r.status_code)
            assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={"name": "kbc", "source_type": "keboola", "config": {"stack_url": "https://connection.example.com"}},
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.get(f"{BASE}/{created.json()['id']}/extraction/status", headers=_auth(token))
        assert r.status_code == 404


class TestDuckDbDegradesCleanly:
    """A3 ratchet: `extraction_runs` is PG-only, so a DuckDB instance gets a
    TYPED 501 the card can recognize and stop polling on — never a raw 500,
    and never an empty-but-healthy-looking answer."""

    def test_status_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)
        r = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token))
        assert r.status_code == 501
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "extraction_runs"

    def test_runs_list_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-runs")
        r = client.get(f"{BASE}/{conn_id}/extraction/runs", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_run_detail_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-run-detail")
        r = client.get(f"{BASE}/{conn_id}/extraction/runs/er_x", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_breakdown_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-breakdown")
        r = client.get(f"{BASE}/{conn_id}/extraction/breakdown", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


class TestExtractionConfig:
    """The config read-out touches no run rows, so it answers on BOTH
    backends: an admin locked out of reading their own configuration because
    the instance is on DuckDB would be a degradation with no cause."""

    def test_answers_200_on_duckdb(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config")
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        # Editability is read from the registry at render time (UX review
        # M5): with the producer command line gone, the section is
        # admin-editable and the drawer must not claim otherwise.
        assert body["section_editable"] is True
        assert body["section_lock_reason"] is None
        assert body["as_of"]

    def test_every_row_names_its_origin_and_lock_state(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-rows")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        assert rows
        for row in rows:
            assert row["origin"] in ("env", "yaml", "default", "builtin"), row
            assert isinstance(row["editable"], bool)
            if not row["editable"]:
                assert row["lock_reason"], row

    def test_unset_value_reads_as_default_not_as_yaml(self, seeded_app):
        """ "50 MB because that is the default" and "50 MB because someone
        chose it" are different facts, and the drawer must not blur them."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-default")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        cap = by_key["extraction.crawler.max_file_mb"]
        assert cap["origin"] == "default"
        assert cap["value"] == 50

    def test_min_modified_reads_unset_by_default(self, seeded_app):
        """The config drawer's Crawl filter panel needs the CURRENT
        per-connection ``extraction.crawl.min_modified`` override to
        pre-fill its date input — the same resolved shape the crawl-config
        PATCH endpoint itself returns."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-min-modified")
        body = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()
        assert body["min_modified"] == {"value": None, "source": "none"}

    def test_min_modified_reflects_a_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-min-modified-set")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        body = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()
        assert body["min_modified"] == {"value": "2023-12-31", "source": "connection"}

    def test_vertex_region_row_falls_back_to_ai_vertex_region_when_no_override(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "connectors.llm.factory.vertex_config_or_none", lambda *a, **k: ("my-project", "us-central1")
        )
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-vertex-region")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        region = by_key["extraction.facts.vertex_region"]
        assert region["value"] == "us-central1"
        assert "instance:ai.vertex" in (region["note"] or "")

    def test_vertex_region_row_reflects_a_connection_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-vertex-region-override")
        # A documented bucket for the instance's default (Haiku) model —
        # see VERTEX_REGION_MODEL_MATRIX (TCRD-296 synthesis F.25).
        patch_resp = client.patch(
            f"{BASE}/{conn_id}/extraction/facts-config", json={"vertex_region": "europe-west1"}, headers=_auth(token)
        )
        assert patch_resp.status_code == 200, patch_resp.text
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        region = by_key["extraction.facts.vertex_region"]
        assert region["value"] == "europe-west1"

    def test_detector_defaults_to_regex_and_says_no_tokens_are_spent(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-detector")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        detector = by_key["extraction.anonymization.detector"]
        assert detector["value"] == "regex"
        assert detector["origin"] == "default"
        assert "No LLM call" in (detector["note"] or "")

    def test_the_detector_note_describes_the_value_actually_in_force(self, monkeypatch):
        """A drawer that explains `regex` while the instance is set to `llm`
        describes a pipeline nobody is running. The note follows the value."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            monkeypatch.setattr(mod, "_config_row", lambda *a, **k: {"value": value, "note": None})
            return mod._detector_row()

        assert "No LLM call" in _row_for("regex")["note"]

        llm = _row_for("llm")["note"]
        # The llm setting ADDS the LLM tier to the regex one; saying it
        # replaces regex would understate what still runs deterministically.
        assert "not swapped out" in llm
        assert "Spends tokens" in llm

        # An unrecognized value is NOT an error at runtime: anything but
        # `llm` runs the regex tier. The note has to say both — the value is
        # wrong, AND here is what actually executes — or an operator is left
        # guessing whether anything ran at all.
        unknown = _row_for("magic")["note"]
        assert "unrecognized value" in unknown
        assert "runs the regex tier" in unknown
        assert "No LLM call" in unknown

    def test_the_detector_note_matches_the_way_the_runtime_normalizes(self):
        """`crawler._entity_detector` lowercases and strips before comparing
        against "llm". A drawer that read "LLM" as unrecognized would
        disagree with the engine it is describing."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            import unittest.mock as m

            with m.patch.object(mod, "_config_row", lambda *a, **k: {"value": value, "note": None}):
                return mod._detector_row()

        for spelling in ("llm", "LLM", " llm ", "Llm"):
            assert "not swapped out" in _row_for(spelling)["note"], spelling

    def test_an_unset_detector_reads_as_the_deterministic_tier(self):
        """An empty value resolves to the regex tier at runtime, so it must
        not be reported as an unrecognized one."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            import unittest.mock as m

            with m.patch.object(mod, "_config_row", lambda *a, **k: {"value": value, "note": None}):
                return mod._detector_row()

        for empty in ("", "   ", None):
            note = _row_for(empty)["note"]
            assert "No LLM call" in note, empty
            assert "unrecognized" not in note, empty

    def test_the_timeout_row_describes_the_in_process_ceiling(self, seeded_app):
        """It used to be a subprocess kill that did not apply to the built-in
        crawl; it now genuinely bounds the run. A note still saying the old
        thing would tell an operator a cap they set does nothing."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-timeout")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        note = by_key["extraction.timeout_s"]["note"]
        assert "not a subprocess" not in note
        assert "resumes" in note
        assert "0 = unbounded" in note

    def test_a_code_constant_and_an_unset_env_knob_are_told_apart(self):
        """ "built in" means there is nothing to set; "default" means nobody
        has set it. Collapsing the two would send an admin hunting for a knob
        that does not exist, or stop them setting one that does."""
        from app.api.admin_extraction import _config_row

        constant = _config_row("Checkpoint granularity", (), default="every 200 delta rows")
        assert constant["origin"] == "builtin"
        assert "no setting to change" in constant["lock_reason"]

        env_only = _config_row("Scan transcription model", (), env_var="AGNES_VISION_MODEL_UNSET_IN_TESTS")
        assert env_only["origin"] == "default"
        assert "AGNES_VISION_MODEL_UNSET_IN_TESTS" in env_only["lock_reason"]

    def test_env_set_value_is_reported_as_env_and_locked(self, seeded_app, monkeypatch):
        """An admin edit writes YAML, which the environment overrides —
        offering the edit would be offering a change that does nothing."""
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-env")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        enabled = by_key["sharepoint.enabled"]
        assert enabled["origin"] == "env"
        assert enabled["env_name"] == "AGNES_SHAREPOINT_ENABLED"
        assert enabled["editable"] is False
        assert "AGNES_SHAREPOINT_ENABLED" in enabled["lock_reason"]

    def test_no_producer_command_row_is_rendered(self, seeded_app):
        """The built-in pipeline has no producer command; rendering one an
        admin cannot change would be a pointer at an executable for nothing."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-noproducer")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        keys = [r["key"] for r in rows if r["key"]]
        assert not any(k.startswith("extraction.producer") for k in keys)

    def test_credential_env_vars_are_named_never_valued(self, seeded_app, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-leak-me")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-secret")
        raw = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).text
        assert "sk-do-not-leak-me" not in raw


def _confirm_drive_scope(client, token, conn_id, *, source_scope_id="b!drive1", drive_id="drv1", display_path="Docs"):
    r = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": source_scope_id, "display_path": display_path, "drive_id": drive_id},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _install_completeness_mock(monkeypatch, *, drive_id="drv1", web_url, count):
    """Graph token exchange + drive-root webUrl lookup + an empty
    root/children listing (a whole-drive scope also fetches a per-folder
    breakdown) + a single Search count — enough for one confirmed drive
    scope with no sub-folders."""
    from connectors.sharepoint import graph_client as gc

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-completeness"})
        if path == f"/v1.0/drives/{drive_id}/root":
            return httpx.Response(200, json={"webUrl": web_url})
        if path == f"/v1.0/drives/{drive_id}/root/children":
            return httpx.Response(200, json={"value": []})
        if path == "/v1.0/search/query":
            return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": count}]}]})
        raise AssertionError(f"unexpected sharepoint completeness mock path {path}")

    monkeypatch.setattr(
        gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )


class TestCompleteness:
    """``GET …/extraction/completeness`` — A6, "did we really get
    everything?" (TCRD-296 B.9). Answers on BOTH backends (no
    ``extraction_runs`` read), unlike A1/A2/A3."""

    def test_no_scopes_never_needs_a_cert(self, seeded_app):
        """No confirmed scope means nothing to count against — the endpoint
        must answer 200, never a 409 about a certificate that would only
        matter once there's a scope to count."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-nocert-noscope")
        r = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert r.status_code == 200, r.text

    def test_no_cert_with_a_confirmed_scope_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-nocert")
        _confirm_drive_scope(client, token, conn_id)
        r = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "sharepoint_cert_unresolved"

    def test_invalid_min_modified_is_400(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-baddate")
        r = client.get(f"{BASE}/{conn_id}/extraction/completeness?min_modified=not-a-date", headers=_auth(token))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_min_modified"

    def test_no_scopes_answers_200_with_an_unknown_total(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-noscopes")
        r = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"] == []
        assert body["total"]["status"] == "unknown"
        assert body["provisional"] is False

    def test_single_drive_scope_answers_200_on_duckdb(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-happy")
        _confirm_drive_scope(client, token, conn_id)
        _install_completeness_mock(monkeypatch, web_url="https://example.sharepoint.com/sites/s/Docs", count=3)

        r = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        scope_row = next(row for row in body["rows"] if row["kind"] == "scope")
        assert scope_row["expected"] == 3
        assert scope_row["indexed"] == 0
        assert scope_row["gap"] == 3
        assert scope_row["status"] == "missing"
        assert body["total"]["expected"] == 3
        assert body["cached"] is False
        assert body["provisional"] is False
        assert body["min_modified"] == {"value": None, "source": "none"}

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_connection.completeness_read", limit=10)
        assert len(rows) == 1

    def test_repeat_call_is_cached_and_refresh_bypasses_it(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-cache")
        _confirm_drive_scope(client, token, conn_id)

        calls = {"n": 0}
        from connectors.sharepoint import graph_client as gc

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s/Docs"})
            if path == "/v1.0/drives/drv1/root/children":
                return httpx.Response(200, json={"value": []})
            if path == "/v1.0/search/query":
                calls["n"] += 1
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": 1}]}]})
            raise AssertionError(path)

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

        first = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert first.status_code == 200
        assert first.json()["cached"] is False
        first_calls = calls["n"]

        second = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert second.status_code == 200
        assert second.json()["cached"] is True
        assert calls["n"] == first_calls  # no new Graph Search calls — served from cache

        refreshed = client.get(f"{BASE}/{conn_id}/extraction/completeness?refresh=true", headers=_auth(token))
        assert refreshed.status_code == 200
        assert refreshed.json()["cached"] is False
        assert calls["n"] > first_calls  # refresh recomputed for real

    def test_provisional_true_while_a_crawl_job_is_in_flight(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-completeness-provisional")
        _confirm_drive_scope(client, token, conn_id)
        _install_completeness_mock(monkeypatch, web_url="https://example.sharepoint.com/sites/s/Docs", count=1)

        from app.api.admin_sharepoint import _extraction_idempotency_key
        from src.repositories import jobs_repo

        jobs_repo().enqueue(
            kind="corpus-extraction",
            payload={"connection_id": conn_id},
            idempotency_key=_extraction_idempotency_key(conn_id),
        )

        r = client.get(f"{BASE}/{conn_id}/extraction/completeness", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["provisional"] is True


class TestDerivedOutcome:
    """Liveness is DERIVED, never trusted, and outcome precedence is
    severity-first — the two rules that stop a card from rendering a crashed
    or abandoned run as healthy."""

    def _run(self, **over):
        row = {
            "id": "er_1",
            "status": "running",
            "job_id": None,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(over)
        return row

    def test_a_fresh_running_run_is_running(self):
        from app.api.admin_extraction import _derived_outcome

        out = _derived_outcome(self._run())
        assert out["outcome"] == "running"
        assert out["evidence"] is None

    def test_a_long_silent_running_run_is_stalled_with_its_age(self):
        from app.api.admin_extraction import _STALL_AFTER_S, _derived_outcome

        stale = datetime.now(timezone.utc) - timedelta(seconds=_STALL_AFTER_S + 600)
        out = _derived_outcome(self._run(checkpoint_at=stale.isoformat()))
        assert out["outcome"] == "stalled"
        assert out["stale_s"] > _STALL_AFTER_S
        # The card must be able to say WHY, not merely assert.
        assert "checkpoint" in out["evidence"]
        # The stored value is reported separately — the two are never merged.
        assert out["stored_status"] == "running"

    def test_a_run_whose_job_failed_renders_failed_not_running(self, monkeypatch):
        """A worker killed outright finalizes nothing. If the job it belonged
        to is already failed, the run is failed — severity wins."""
        import app.api.admin_extraction as mod

        monkeypatch.setattr(mod, "_job_status", lambda job_id: "failed")
        out = mod._derived_outcome(self._run(job_id="job_1"))
        assert out["outcome"] == "failed"
        assert "failed" in out["evidence"]

    def test_a_finalized_run_is_never_second_guessed(self, monkeypatch):
        import app.api.admin_extraction as mod

        monkeypatch.setattr(mod, "_job_status", lambda job_id: "failed")
        for stored in ("done", "interrupted", "failed"):
            out = mod._derived_outcome(self._run(status=stored, job_id="job_1"))
            assert out["outcome"] == stored

    def test_precedence_order_puts_failure_first(self):
        from app.api.admin_extraction import OUTCOME_PRECEDENCE

        assert OUTCOME_PRECEDENCE.index("failed") < OUTCOME_PRECEDENCE.index("interrupted")
        assert OUTCOME_PRECEDENCE.index("stalled") < OUTCOME_PRECEDENCE.index("done")


class TestRunProjection:
    def test_run_out_reports_absolute_counters_and_no_progress_fraction(self):
        """No percentage, no bar, no ETA: the crawl enumerates and processes
        in lockstep, and files_per_s counts only new+changed."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "running",
                "checkpoint_at": datetime.now(timezone.utc).isoformat(),
                "files_done": 812,
                "files_seen": 812,
                "progress": {"new": 800, "unchanged": 12, "elapsed_s": 391.0, "http_429": 4},
            }
        )
        assert out["files_done"] == 812
        assert out["new"] == 800
        assert out["elapsed_s"] == 391.0
        assert out["http_429"] == 4
        for forbidden in ("percent", "progress_pct", "eta_s", "eta"):
            assert forbidden not in out

    def test_run_out_surfaces_the_age_filter_counters_from_a_running_checkpoint(self):
        """An operator watching a LIVE run must be able to tell whether
        `min_modified` is doing anything mid-run, not only after `report()`
        becomes readable."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "running",
                "progress": {"filtered_by_age": 40, "age_unknown": 3},
            }
        )
        assert out["filtered_by_age"] == 40
        assert out["age_unknown"] == 3

    def test_run_out_surfaces_the_age_filter_counters_from_a_finished_report(self):
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "done",
                "report": {"filtered_by_age": 12, "age_unknown": 0},
            }
        )
        assert out["filtered_by_age"] == 12
        assert out["age_unknown"] == 0

    def test_run_out_never_restamps_freshness(self):
        """`checkpoint_at` is when the numbers were last TRUE — a read must
        not quietly refresh it to now."""
        from app.api.admin_extraction import _run_out

        stamp = "2026-08-31T14:08:41+00:00"
        out = _run_out({"id": "er_1", "status": "done", "checkpoint_at": stamp, "report": {}})
        assert out["checkpoint_at"] == stamp

    def test_run_out_surfaces_skipped_unsupported_never_folded_into_errors(self):
        """A file no conversion backend even attempts is a separate counter
        from `errors` — the fleet view and the source card must be able to
        show BOTH without one masking the other."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "done",
                "report": {"errors": 3, "skipped_unsupported": 7},
            }
        )
        assert out["errors"] == 3
        assert out["skipped_unsupported"] == 7

    def test_usage_empty_dict_survives_the_projection(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {}, "usage": {}})
        assert out["usage"] == {}

    def test_a_recorded_stop_reason_is_surfaced(self):
        """A run that hit the timeout ceiling names its exit; an operator
        should never have to infer "it ended short" from a duration."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "failed",
                "report": {"interrupted": True, "interrupted_reason": "timeout", "duration_s": 3600.0},
            }
        )
        assert out["interrupted_reason"] == "timeout"

    def test_a_timeout_is_resumable_even_though_it_finalized_as_failed(self):
        """The crawl persists deltaLinks/cTags on the way out of a timeout,
        so the next run costs re-work, not coverage. Keying the reassurance
        on the outcome WORD withheld it from exactly the case that earned
        it — an operator then re-runs a four-hour crawl out of doubt."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "timeout"}})
        assert out["outcome"] == "failed"
        assert out["resumable"] is True

    def test_a_crash_is_never_claimed_resumable(self):
        """Nothing is known about how far the crawl state got before it
        died, and "your work is safe" must never be guessed."""
        from app.api.admin_extraction import _run_out

        for report in ({"interrupted_reason": "error"}, {}, {"interrupted_reason": None}):
            out = _run_out({"id": "er_1", "status": "failed", "report": report})
            assert out["resumable"] is False, report

    def test_a_cancelled_run_stays_resumable(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "interrupted", "report": {}})
        assert out["resumable"] is True

    def test_a_throttle_abort_is_resumable(self):
        """A 429-budget abort stops at the same consistent point a timeout
        does — `_process_item` re-raises it rather than absorbing it as a
        per-file fault — so the persisted cTags describe exactly what was
        ingested and the next run picks up from there."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS, _run_out

        assert "throttled" in RESUMABLE_STOP_REASONS
        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "throttled"}})
        assert out["resumable"] is True

    def test_resumability_matches_the_crawls_own_normalization(self):
        from app.api.admin_extraction import _run_out

        for spelling in ("timeout", "TIMEOUT", " Timeout "):
            out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": spelling}})
            assert out["resumable"] is True, spelling

    def test_a_run_that_ended_normally_has_no_stop_reason(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"duration_s": 12.0}})
        assert out["interrupted_reason"] is None

    def test_a_cooperative_stop_is_resumable(self):
        """An admin-requested stop (`POST …/extraction/stop`) aborts at the
        same consistent point a timeout does — see `_STOP_REASONS` on the
        crawl side — so it earns the same "next run resumes" promise."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS, _run_out

        assert "stopped" in RESUMABLE_STOP_REASONS
        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "stopped"}})
        assert out["interrupted_reason"] == "stopped"
        assert out["resumable"] is True

    def test_activity_rides_the_same_checkpoint_projection(self):
        """`activity` (owner-frustration fix, 2026-09-01) is read from
        whichever of `report`/`progress` `live` resolves to — no separate
        lookup, so it can never disagree with the counters next to it."""
        from app.api.admin_extraction import _run_out

        activity = {"phase": "crawl", "current_path": "Reports/q3.docx", "recent": []}
        out = _run_out({"id": "er_1", "status": "running", "progress": {"activity": activity}})
        assert out["activity"] == activity

    def test_a_finished_run_has_no_live_activity(self):
        """`report` (the FINAL shape `finish()` stores) never grows an
        `activity` key — a completed run honestly has nothing in flight."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"duration_s": 12.0}})
        assert out["activity"] is None

    def test_the_row_s_own_phase_column_is_surfaced(self):
        """Owner-frustration fix, 2026-09-02: the row stayed `phase="crawl"`
        for the whole facts pass, which is its own small lie. Surfaced
        straight from the column so a caller does not have to unpack
        `activity` to know it."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "running", "phase": "facts", "progress": {}})
        assert out["phase"] == "facts"

    def test_facts_progress_is_absent_during_the_crawl_phase(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "running", "phase": "crawl", "progress": {"new": 5}})
        assert out["facts_progress"] is None

    def test_facts_progress_rides_the_same_checkpoint_projection_as_activity(self):
        """`docs_done`/`docs_total` come from the SAME checkpoint write as
        `activity` (`_RunRecorder.checkpoint_facts`) — never a separate
        lookup, so the two can never disagree."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "running",
                "phase": "facts",
                "progress": {
                    "activity": {"phase": "facts", "current_path": "a.docx", "recent": []},
                    "facts": {"docs_done": 340, "docs_total": 1200},
                    # The crawl's own last numbers stay in the SAME blob —
                    # a facts checkpoint layers on top of them, never wipes
                    # them.
                    "new": 812,
                },
            }
        )
        assert out["facts_progress"] == {"docs_done": 340, "docs_total": 1200}
        assert out["activity"]["phase"] == "facts"
        assert out["new"] == 812

    def test_a_finished_run_has_no_facts_progress(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"duration_s": 12.0}})
        assert out["facts_progress"] is None

    def test_scan_ocr_is_absent_when_the_crawl_never_reported_one(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "running", "phase": "crawl", "progress": {"new": 5}})
        assert out["scan_ocr"] is None

    def test_scan_ocr_carries_the_disabled_reason_once_the_crawl_reports_it(self):
        """`src.ingest.scan_ocr.triage_run_usage` — the crawl's
        own `report["scan_ocr"]` block — rides straight through, same
        "layered onto report/progress" contract as `facts_progress`."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "done",
                "report": {
                    "scan_ocr": {
                        "disabled_reason": "http_400",
                        "provider_error": "Your workspace has hit the API usage limits ...",
                    }
                },
            }
        )
        assert out["scan_ocr"] == {
            "disabled_reason": "http_400",
            "provider_error": "Your workspace has hit the API usage limits ...",
        }


# ---------------------------------------------------------------------------
# Shard roll-up (2026-09-03 auto-parallel-crawl design §4.7, plan Task 8) —
# pure unit tests over `_run_out`'s additive keys and `_rollup_children`; the
# PG happy path (real child rows joined through the API) lives in
# `tests/db_pg/test_extraction_api_pg.py`.
# ---------------------------------------------------------------------------


class TestShardModeProjection:
    def test_a_plain_run_reports_inline_mode_and_no_shard_counters(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"new": 3}})
        assert out["mode"] == "inline"
        assert out["shards_total"] is None
        assert out["shards_done"] is None
        assert out["expected_documents"] is None
        assert out["seen_documents"] is None
        assert out["shards"] is None

    def test_a_parent_row_reports_sharded_mode_from_its_own_columns_alone(self):
        """`shards_total`/`shards_done` come straight off the row — no
        children needed for the mode/counter fields, only for `shards[]`."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_parent", "status": "running", "shards_total": 3, "shards_done": 1})
        assert out["mode"] == "sharded"
        assert out["shards_total"] == 3
        assert out["shards_done"] == 1
        # No `children=` given — the per-shard rollup is simply not computed.
        assert out["shards"] is None

    def test_children_none_vs_empty_list_both_render_shards_as_a_list(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_parent", "status": "running", "shards_total": 2}, children=[])
        assert out["shards"] == []


class TestRollupChildren:
    def _child(self, **overrides):
        base = {
            "id": "er_child",
            "connection_id": "conn-1",
            "status": "done",
            "shard_key": "drive-1:item-1",
            "shard_label": "part 1/2",
            "checkpoint_at": "2026-09-03T00:00:00+00:00",
            "files_done": 40,
            "files_seen": 40,
            "report": {"new": 10, "changed": 5, "unchanged": 25, "filtered_by_age": 2},
            "error": None,
        }
        base.update(overrides)
        return base

    def test_sums_counters_and_carries_one_row_per_child(self):
        from app.api.admin_extraction import _rollup_children

        children = [
            self._child(shard_key="k1", shard_label="part 1/2", report={"new": 10, "changed": 0, "unchanged": 10}),
            self._child(shard_key="k2", shard_label="part 2/2", report={"new": 3, "changed": 1, "unchanged": 1}),
        ]
        out = _rollup_children({"connection_id": "conn-1"}, children, expected_by_key={"k1": 20, "k2": 5})
        assert len(out["shards"]) == 2
        assert out["shards"][0]["label"] == "part 1/2"
        assert out["shards"][0]["expected"] == 20
        assert out["shards"][1]["expected"] == 5
        # new+changed+unchanged summed across both children.
        assert out["seen_documents"] == (10 + 0 + 10) + (3 + 1 + 1)
        assert out["expected_documents"] == 25

    def test_expected_documents_is_none_when_any_shard_has_no_known_plan(self):
        """A partial sum would silently understate the site's real target —
        worse than admitting the total is unknown."""
        from app.api.admin_extraction import _rollup_children

        children = [self._child(shard_key="k1"), self._child(shard_key="k2")]
        out = _rollup_children({"connection_id": "conn-1"}, children, expected_by_key={"k1": 20})
        assert out["expected_documents"] is None
        assert out["shards"][0]["expected"] == 20
        assert out["shards"][1]["expected"] is None

    def test_no_children_reports_none_not_zero(self):
        from app.api.admin_extraction import _rollup_children

        out = _rollup_children({"connection_id": "conn-1"}, [], expected_by_key={})
        assert out["shards"] == []
        assert out["expected_documents"] is None
        assert out["seen_documents"] is None

    def test_a_stalled_child_is_flagged_stuck_on_its_own_row(self):
        from datetime import datetime, timedelta, timezone

        from app.api.admin_extraction import _STALL_AFTER_S, _rollup_children

        now = datetime.now(timezone.utc)
        stale = (now - timedelta(seconds=_STALL_AFTER_S + 600)).isoformat()
        children = [
            self._child(shard_key="k1", status="running", checkpoint_at=stale),
            self._child(shard_key="k2", status="running", checkpoint_at=now.isoformat()),
        ]
        out = _rollup_children({"connection_id": "conn-1"}, children, expected_by_key={}, now=now)
        assert out["shards"][0]["outcome"] == "stalled"
        assert out["shards"][0]["stuck"] is True
        assert out["shards"][1]["outcome"] == "running"
        assert out["shards"][1]["stuck"] is False

    def test_a_failed_childs_error_rides_its_own_shard_row(self):
        from app.api.admin_extraction import _rollup_children

        children = [self._child(shard_key="k1", status="failed", error="CrawlError: boom")]
        out = _rollup_children({"connection_id": "conn-1"}, children, expected_by_key={})
        assert out["shards"][0]["outcome"] == "failed"
        assert out["shards"][0]["error"] == "CrawlError: boom"


# ---------------------------------------------------------------------------
# Fleet view (`GET /extraction/runs`, `/admin/extraction`, 2026-09-02) — the
# pure helper functions first, no database required; the PG happy path (a
# fleet row actually surfacing through the API) lives in
# `tests/db_pg/test_extraction_api_pg.py`.
# ---------------------------------------------------------------------------


class TestFilesPerMin:
    def test_falls_back_to_the_since_started_average_on_first_observation(self):
        from app.api.admin_extraction import _files_per_min

        started = datetime.now(timezone.utc) - timedelta(minutes=10)
        checkpoint = started + timedelta(minutes=5)
        run = {
            "id": "er_rate_first_observation",
            "started_at": started.isoformat(),
            "checkpoint_at": checkpoint.isoformat(),
            "files_done": 50,
        }
        assert _files_per_min(run) == pytest.approx(10.0, rel=0.05)

    def test_derives_the_windowed_rate_from_two_consecutive_polls(self):
        """Two calls with the SAME run id, spaced 5 minutes apart in
        `checkpoint_at` — the windowed rate, not the since-start average
        (which would read ~2.9/min over the same 21-minute span)."""
        from app.api.admin_extraction import _files_per_min

        started = datetime.now(timezone.utc) - timedelta(minutes=20)
        first_checkpoint = started + timedelta(minutes=1)
        _files_per_min(
            {
                "id": "er_rate_two_polls",
                "started_at": started.isoformat(),
                "checkpoint_at": first_checkpoint.isoformat(),
                "files_done": 10,
            }
        )
        second_checkpoint = first_checkpoint + timedelta(minutes=5)
        rate = _files_per_min(
            {
                "id": "er_rate_two_polls",
                "started_at": started.isoformat(),
                "checkpoint_at": second_checkpoint.isoformat(),
                "files_done": 60,
            }
        )
        assert rate == pytest.approx(10.0, rel=0.05)

    def test_none_with_no_files_done_and_no_history(self):
        from app.api.admin_extraction import _files_per_min

        now = datetime.now(timezone.utc)
        run = {
            "id": "er_rate_no_files",
            "started_at": now.isoformat(),
            "checkpoint_at": now.isoformat(),
            "files_done": 0,
        }
        assert _files_per_min(run) is None

    def test_none_without_a_checkpoint(self):
        from app.api.admin_extraction import _files_per_min

        assert _files_per_min({"id": "er_rate_no_checkpoint", "files_done": 5}) is None

    def test_none_without_a_run_id(self):
        from app.api.admin_extraction import _files_per_min

        now = datetime.now(timezone.utc)
        assert _files_per_min({"checkpoint_at": now.isoformat(), "files_done": 5}) is None


class TestRunTotalCostUsd:
    def test_sums_estimated_cost_across_every_stage(self):
        from app.api.admin_extraction import _run_total_cost_usd

        run = {
            "usage": {
                "ner": {"estimated_cost_usd": 0.5},
                "ocr": {"estimated_cost_usd": 0.25},
                "facts": {"estimated_cost_usd": 1.25},
            }
        }
        assert _run_total_cost_usd(run) == pytest.approx(2.0)

    def test_a_stage_with_no_priced_cost_contributes_nothing(self):
        """`{}` (no tokens spent) and a stage that never priced itself both
        contribute 0 — never an invented estimate."""
        from app.api.admin_extraction import _run_total_cost_usd

        assert _run_total_cost_usd({"usage": {"ner": {}}}) == 0.0

    def test_missing_run_or_empty_usage_is_zero(self):
        from app.api.admin_extraction import _run_total_cost_usd

        assert _run_total_cost_usd(None) == 0.0
        assert _run_total_cost_usd({}) == 0.0
        assert _run_total_cost_usd({"usage": {}}) == 0.0


class TestFleetFacts:
    """`_fleet_facts(run, connection_id)` — `connection_id` drives
    `facts_pending_documents`/`facts_pass_running` (TCRD-296 gap #61),
    connection-level facts independent of `run`; every test here needs a
    working repo context (``seeded_app``) for those two lookups even
    though the connection itself is never seeded — an unknown connection
    reads as "0 pending, nothing running", never an error."""

    def test_no_run_is_the_empty_shape_with_every_count_none(self, seeded_app):
        from app.api.admin_extraction import _EMPTY_FLEET_FACTS, _fleet_facts

        out = _fleet_facts(None, "conn-none")
        assert out["docs_done"] is None
        assert out["usage"] == {}
        assert out["facts_pending_documents"] == 0
        assert out["facts_pass_running"] is False
        # Every OTHER field still matches the run-keyed empty shape.
        for key, value in _EMPTY_FLEET_FACTS.items():
            if key in ("facts_pending_documents", "facts_pass_running"):
                continue
            assert out[key] == value

    def test_mutating_the_result_never_corrupts_the_shared_empty_constant(self, seeded_app):
        from app.api.admin_extraction import _EMPTY_FLEET_FACTS, _fleet_facts

        out = _fleet_facts(None, "conn-none")
        out["docs_done"] = 999
        assert _EMPTY_FLEET_FACTS["docs_done"] is None

    def test_live_progress_while_the_facts_phase_is_running(self, seeded_app):
        from app.api.admin_extraction import _fleet_facts

        run = {
            "status": "running",
            "phase": "facts",
            "progress": {"facts": {"docs_done": 12, "docs_total": 340}},
        }
        out = _fleet_facts(run, "conn-live")
        assert out["phase_active"] is True
        assert out["docs_done"] == 12
        assert out["docs_total"] == 340
        # Not known until `finish()` — a live pass has no outcome breakdown yet.
        assert out["docs_extracted"] is None

    def test_final_report_once_the_pass_has_finished(self, seeded_app):
        from app.api.admin_extraction import _fleet_facts

        run = {
            "status": "done",
            "phase": "facts",
            "report": {
                "facts": {
                    "docs_extracted": 300,
                    "docs_unchanged": 20,
                    "docs_skipped_tabular": 5,
                    "docs_skipped_no_text": 1,
                    "docs_skipped_not_indexed": 2,
                    "facts_failed": 3,
                }
            },
            "usage": {"facts": {"estimated_cost_usd": 4.5, "input_tokens": 1000}},
        }
        out = _fleet_facts(run, "conn-final")
        assert out["phase_active"] is False  # the row is `done`, not `running`
        assert out["docs_done"] == 300  # falls back to docs_extracted
        assert out["docs_extracted"] == 300
        assert out["docs_skipped_tabular"] == 5
        assert out["docs_skipped_no_text"] == 1
        assert out["docs_skipped_not_indexed"] == 2
        assert out["facts_failed"] == 3
        assert out["usage"]["estimated_cost_usd"] == 4.5

    def test_phase_active_is_false_outside_the_facts_phase(self, seeded_app):
        from app.api.admin_extraction import _fleet_facts

        run = {"status": "running", "phase": "crawl", "progress": {}}
        assert _fleet_facts(run, "conn-crawl")["phase_active"] is False

    def test_pending_documents_and_pass_running_reflect_the_connection(self, seeded_app, monkeypatch):
        """Connection-level, not read off `run` — proven by mocking the two
        underlying lookups (already covered elsewhere: `count_pending_
        documents` in `tests/db_pg/test_facts_extraction_pg.py`,
        `_facts_job_in_flight` in `TestFactsJobInFlight` below) and
        checking `_fleet_facts` threads `connection_id` through to both,
        regardless of `run`."""
        from app.api import admin_extraction as mod

        monkeypatch.setattr(mod, "_facts_pending_documents", lambda cid: 7 if cid == "conn-x" else 0)
        monkeypatch.setattr(
            mod, "_facts_job_in_flight", lambda cid: {"id": "job-1", "status": "running"} if cid == "conn-x" else None
        )

        out = mod._fleet_facts(None, "conn-x")
        assert out["facts_pending_documents"] == 7
        assert out["facts_pass_running"] is True

        out_other = mod._fleet_facts(None, "conn-y")
        assert out_other["facts_pending_documents"] == 0
        assert out_other["facts_pass_running"] is False


FLEET_URL = "/api/admin/sharepoint/extraction/runs"


class TestFleetRoute:
    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(FLEET_URL)
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].get(FLEET_URL, headers=_auth(token))
        assert r.status_code == 403

    def test_typed_501_on_duckdb(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        _create_connection(client, token, name="sp-fleet-501")
        r = client.get(FLEET_URL, headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_typed_501_even_with_no_sharepoint_connections_at_all(self, seeded_app):
        """The repo factory raises before any connection list is even
        walked — a DuckDB instance with zero SharePoint connections still
        owes the typed 501, not a hollow 200 with an empty list."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.get(FLEET_URL, headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


CANCEL_URL = f"{FLEET_URL}/er_whatever/cancel"


class TestCancelRoute:
    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(CANCEL_URL)
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].post(CANCEL_URL, headers=_auth(token))
        assert r.status_code == 403

    def test_typed_501_on_duckdb(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.post(CANCEL_URL, headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


class TestFactsPendingDocumentsCache:
    """``_facts_pending_documents``'s TTL cache (TCRD-296 gap #72) — the
    source card polls ``…/extraction/status`` every few seconds and the
    fleet view polls it once per row, so this collapses repeat polls onto
    one real ``count_pending_documents`` call within the TTL window."""

    CONN_ID = "sp-pending-cache-ttl"

    def setup_method(self):
        from app.api import admin_extraction as mod

        mod._facts_pending_cache.pop(self.CONN_ID, None)

    teardown_method = setup_method

    def test_a_repeat_call_within_the_ttl_reuses_the_cached_value(self, monkeypatch):
        from app.api import admin_extraction as mod

        calls = {"n": 0}

        def fake_count(connection_id: str) -> int:
            calls["n"] += 1
            return calls["n"]

        monkeypatch.setattr("connectors.sharepoint.facts_extraction.count_pending_documents", fake_count)

        first = mod._facts_pending_documents(self.CONN_ID)
        second = mod._facts_pending_documents(self.CONN_ID)
        assert (first, second) == (1, 1)
        assert calls["n"] == 1, "a call within the TTL must not recompute"

    def test_a_call_past_the_ttl_recomputes(self, monkeypatch):
        from app.api import admin_extraction as mod

        calls = {"n": 0}

        def fake_count(connection_id: str) -> int:
            calls["n"] += 1
            return calls["n"]

        monkeypatch.setattr("connectors.sharepoint.facts_extraction.count_pending_documents", fake_count)

        assert mod._facts_pending_documents(self.CONN_ID) == 1
        # Backdate the cache entry past the TTL rather than sleeping or
        # monkeypatching the global `time` module — same value, an earlier
        # timestamp, exactly what "the TTL elapsed" looks like to the cache.
        computed_at, value = mod._facts_pending_cache[self.CONN_ID]
        mod._facts_pending_cache[self.CONN_ID] = (computed_at - mod._FACTS_PENDING_CACHE_TTL_S - 1, value)

        assert mod._facts_pending_documents(self.CONN_ID) == 2
        assert calls["n"] == 2, "a call past the TTL must recompute"

    def test_different_connections_do_not_share_a_cache_entry(self, monkeypatch):
        from app.api import admin_extraction as mod

        monkeypatch.setattr(
            "connectors.sharepoint.facts_extraction.count_pending_documents",
            lambda cid: 5 if cid == self.CONN_ID else 9,
        )
        try:
            assert mod._facts_pending_documents(self.CONN_ID) == 5
            assert mod._facts_pending_documents("sp-pending-cache-other") == 9
        finally:
            mod._facts_pending_cache.pop("sp-pending-cache-other", None)


class TestFactsJobInFlight:
    """The standalone facts pass (``sharepoint-facts-extraction``) writes no
    ``extraction_runs`` row — it is a JOB, not a crawl run — so the card's
    status poll reads it off the job queue instead: ``_facts_job_in_flight``
    is the one lookup ``GET …/extraction/status`` uses to say "a facts pass
    is queued/running for this connection". Backend-agnostic (the jobs
    table exists on both), so it is pinned here on DuckDB even though the
    status route itself is Postgres-only."""

    KIND = "sharepoint-facts-extraction"

    @staticmethod
    def _enqueue(connection_id: str):
        from src.repositories import jobs_repo

        return jobs_repo().enqueue(
            "sharepoint-facts-extraction",
            {"connection_id": connection_id},
            idempotency_key=f"sharepoint-facts-extraction:{connection_id}",
        )

    def test_nothing_in_flight_is_none(self, seeded_app):
        from app.api.admin_extraction import _facts_job_in_flight

        assert _facts_job_in_flight("sp-none") is None

    def test_a_queued_pass_is_reported_with_its_id_and_status(self, seeded_app):
        from app.api.admin_extraction import _facts_job_in_flight

        job = self._enqueue("sp-queued")
        found = _facts_job_in_flight("sp-queued")
        assert found is not None
        assert found["id"] == job["id"]
        assert found["status"] == "queued"
        assert found["created_at"]

    def test_another_connections_pass_is_not_this_ones(self, seeded_app):
        from app.api.admin_extraction import _facts_job_in_flight

        self._enqueue("sp-other")
        assert _facts_job_in_flight("sp-mine") is None

    def test_a_running_pass_is_reported_running_and_a_finished_one_is_gone(self, seeded_app):
        from app.api.admin_extraction import _facts_job_in_flight
        from src.repositories import jobs_repo

        job = self._enqueue("sp-running")
        claimed = jobs_repo().claim_next(kinds=[self.KIND], worker_id="w1", lease_seconds=60)
        assert claimed and claimed["id"] == job["id"]
        found = _facts_job_in_flight("sp-running")
        assert found is not None and found["status"] == "running"

        assert jobs_repo().complete(job["id"], "w1", claimed["lease_token"], result={"docs_extracted": 0})
        assert _facts_job_in_flight("sp-running") is None

    def test_the_lookup_follows_the_triggers_own_key_and_kind(self, seeded_app):
        """The reader must find a job enqueued the way the TRIGGER enqueues it.

        `_facts_job_in_flight` is a consumer of a key and kind that
        `POST …/facts-extract` is the sole producer of
        (`app/api/admin_sharepoint.py::_facts_extraction_idempotency_key`).
        Every other test in this class enqueues with a literal, so all of
        them stay green if the producer's shape ever changes — while the
        card goes permanently blind and the button never locks, with no
        symptom. This one enqueues through the producer's OWN key builder
        and kind constant, so a change on that side fails here instead.
        """
        from app.api.admin_extraction import _FACTS_JOB_KIND, _facts_job_in_flight
        from app.api.admin_sharepoint import _facts_extraction_idempotency_key
        from src.repositories import jobs_repo

        # The `list(kind=…)` filter must name the same job kind the key is
        # prefixed with; the trigger builds both from that one string, so a
        # rename on its side that left this constant behind would filter
        # every real job out before the key is even compared.
        assert _facts_extraction_idempotency_key("sp-contract") == f"{_FACTS_JOB_KIND}:sp-contract"

        job = jobs_repo().enqueue(
            _FACTS_JOB_KIND,
            {"connection_id": "sp-contract"},
            idempotency_key=_facts_extraction_idempotency_key("sp-contract"),
        )
        found = _facts_job_in_flight("sp-contract")
        assert found is not None, (
            "the status reader did not find a job enqueued with the trigger's own "
            "idempotency key + kind — the two surfaces have drifted apart"
        )
        assert found["id"] == job["id"]


class TestFactsJobsInFlight:
    """``_facts_jobs_in_flight`` (TCRD-296 gap #67) — the PLURAL,
    payload-scanning sibling of ``_facts_job_in_flight`` that finds every
    partition of a fanned-out pass, not just the one matching a single
    idempotency key."""

    KIND = "sharepoint-facts-extraction"

    @staticmethod
    def _enqueue(connection_id: str, *, index: int, count: int):
        from src.repositories import jobs_repo

        return jobs_repo().enqueue(
            "sharepoint-facts-extraction",
            {"connection_id": connection_id, "partition": {"index": index, "count": count}},
            idempotency_key=f"sharepoint-facts-extraction:{connection_id}:{index}/{count}",
        )

    def test_no_jobs_is_an_empty_list(self, seeded_app):
        from app.api.admin_extraction import _facts_jobs_in_flight

        assert _facts_jobs_in_flight("sp-empty") == []

    def test_every_partition_is_found_and_sorted_by_index(self, seeded_app):
        from app.api.admin_extraction import _facts_jobs_in_flight

        self._enqueue("sp-fanout", index=2, count=3)
        self._enqueue("sp-fanout", index=0, count=3)
        self._enqueue("sp-fanout", index=1, count=3)

        found = _facts_jobs_in_flight("sp-fanout")
        assert [j["partition_index"] for j in found] == [0, 1, 2]
        assert all(j["partition_count"] == 3 for j in found)
        assert all(j["status"] == "queued" for j in found)

    def test_a_legacy_un_partitioned_job_has_null_partition_fields(self, seeded_app):
        from app.api.admin_extraction import _facts_jobs_in_flight
        from src.repositories import jobs_repo

        jobs_repo().enqueue(
            "sharepoint-facts-extraction",
            {"connection_id": "sp-legacy"},
            idempotency_key="sharepoint-facts-extraction:sp-legacy",
        )
        found = _facts_jobs_in_flight("sp-legacy")
        assert len(found) == 1
        assert found[0]["partition_index"] is None
        assert found[0]["partition_count"] is None

    def test_another_connections_partitions_are_not_this_ones(self, seeded_app):
        from app.api.admin_extraction import _facts_jobs_in_flight

        self._enqueue("sp-other", index=0, count=2)
        self._enqueue("sp-other", index=1, count=2)
        assert _facts_jobs_in_flight("sp-mine") == []
