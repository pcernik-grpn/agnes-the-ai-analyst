"""``POST /api/admin/sharepoint/anonymization/preview`` — the redaction
dry-run behind the extraction config drawer's "Preview redaction" panel.

What these tests are actually protecting is not "does it return 200". It is
the four promises the endpoint makes, each of which is a way it could
quietly become harmful:

* it is **admin-only** (it runs the instance's real pseudonym key);
* it **persists nothing** — an admin pasting a real document into a preview
  box must not thereby file that document into the system;
* it **never previews under a fake key** — a pseudonym an admin cannot
  reproduce in production is worse than no preview at all, so a missing key
  is a 409 that names the knob;
* its response **cannot echo an original back** — the redacted text plus
  tokens, never a mapping from token to what it replaced.
"""

from __future__ import annotations

import pytest

URL = "/api/admin/sharepoint/anonymization/preview"

SAMPLE = (
    "Kontakt: jan.novak@example.com, https://example.com/a\n"
    "Tel: +420 776 041 900, účet CZ65 0800 0000 1920 0014 5399\n"
    "Rodné číslo 760419/0341.\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def keyed(monkeypatch):
    """A resolvable per-instance HMAC key, the way a real deployment has one."""
    monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "preview-test-instance-key")


class TestGating:
    def test_unauthenticated_is_401(self, seeded_app):
        assert seeded_app["client"].post(URL, json={"text": "x"}).status_code == 401

    def test_non_admin_is_403(self, seeded_app):
        r = seeded_app["client"].post(URL, json={"text": "x"}, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_gating_runs_before_the_key_is_touched(self, seeded_app, monkeypatch):
        """An analyst must not be able to probe whether the instance has a
        key configured by reading which error they get."""
        monkeypatch.delenv("AGNES_ANONYMIZATION_HMAC_KEY", raising=False)
        r = seeded_app["client"].post(URL, json={"text": "x"}, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestKeyResolution:
    def test_no_configured_key_is_409_naming_the_config_key(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_ANONYMIZATION_HMAC_KEY", raising=False)
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "anonymization_key_unavailable"
        assert detail["config_key"] == "extraction.anonymization.hmac_key_env"
        assert "AGNES_ANONYMIZATION_HMAC_KEY" in detail["message"]

    def test_the_key_value_never_appears_in_any_response(self, seeded_app, keyed):
        secret = "preview-test-instance-key"
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert secret not in r.text

    def test_pseudonyms_match_what_the_anonymizer_itself_produces(self, seeded_app, keyed):
        """The whole point of using the real key: the preview's token is the
        token a crawl will write. Compared against the library directly, so
        a future endpoint that quietly used its own key would fail here."""
        from src.anonymization import anonymize_markdown

        expected = anonymize_markdown(SAMPLE, key=b"preview-test-instance-key")
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert r.json()["redacted_text"] == expected.text


class TestRedaction:
    def test_it_redacts_and_counts_each_kind(self, seeded_app, keyed):
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.json()
        counts = body["counts_by_kind"]
        assert counts["email"] == 1
        assert counts["url"] == 1
        assert counts["phone"] == 1
        assert counts["iban"] == 1
        assert counts["national_id"] == 1
        assert body["replaced"] == sum(counts.values())
        assert body["detector"] == "regex"

    def test_the_response_carries_tokens_never_originals(self, seeded_app, keyed):
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        body = r.json()
        # None of the redacted values survives anywhere in the payload —
        # not in the text, not in `entities`, not in a stray debug field.
        for original in ("jan.novak@example.com", "776 041 900", "CZ65 0800", "760419/0341"):
            assert original not in r.text
        kinds = {e["kind"] for e in body["entities"]}
        assert {"email", "phone", "iban", "national_id"} <= kinds
        for entity in body["entities"]:
            assert set(entity) == {"kind", "pseudonym"}
            assert entity["pseudonym"] in body["redacted_text"]

    def test_regex_detector_reports_no_token_usage(self, seeded_app, keyed):
        """`{}` means NO tokens were spent — a different claim from $0.00."""
        r = seeded_app["client"].post(URL, json={"text": SAMPLE}, headers=_auth(seeded_app["admin_token"]))
        assert r.json()["usage"] == {}


class TestInputBounds:
    def test_an_oversized_sample_is_413_naming_the_limit(self, seeded_app, keyed):
        from app.api.admin_extraction import _PREVIEW_MAX_CHARS

        r = seeded_app["client"].post(
            URL,
            json={"text": "a" * (_PREVIEW_MAX_CHARS + 1)},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 413
        detail = r.json()["detail"]
        assert detail["error"] == "preview_text_too_long"
        assert detail["limit"] == _PREVIEW_MAX_CHARS

    def test_a_sample_at_the_limit_is_accepted(self, seeded_app, keyed):
        from app.api.admin_extraction import _PREVIEW_MAX_CHARS

        r = seeded_app["client"].post(
            URL,
            json={"text": "a" * _PREVIEW_MAX_CHARS},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200

    def test_an_empty_sample_is_422(self, seeded_app, keyed):
        r = seeded_app["client"].post(URL, json={"text": "   "}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 422

    def test_an_unknown_detector_is_422(self, seeded_app, keyed):
        r = seeded_app["client"].post(
            URL, json={"text": SAMPLE, "detector": "magic"}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 422
        assert "unknown_detector" in str(r.json()["detail"])


class TestCustomTerms:
    def test_a_configured_term_is_redacted(self, seeded_app, keyed, monkeypatch):
        import app.instance_config as instance_config

        monkeypatch.setattr(
            instance_config,
            "get_value",
            _config_stub({("extraction", "anonymization", "custom_terms"): ["Projekt Fénix"]}),
        )
        r = seeded_app["client"].post(
            URL, json={"text": "Projekt Fénix je tajný."}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        assert "Fénix" not in r.text
        assert r.json()["counts_by_kind"]["term"] == 1

    def test_an_unusable_term_is_400_naming_the_config_key(self, seeded_app, keyed, monkeypatch):
        import app.instance_config as instance_config

        monkeypatch.setattr(
            instance_config,
            "get_value",
            _config_stub({("extraction", "anonymization", "custom_terms"): [r"\d{3}"]}),
        )
        r = seeded_app["client"].post(URL, json={"text": "123"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["error"] == "custom_terms_invalid"
        assert detail["config_key"] == "extraction.anonymization.custom_terms"


def _config_stub(overrides: dict):
    """A ``get_value`` that answers ``overrides`` and defaults otherwise.

    Patches the module attribute rather than writing an ``instance.yaml``:
    the anonymizer imports ``get_value`` lazily from
    ``app.instance_config`` at call time, so this reaches the real
    resolution path without leaving a file behind for the next test.
    """

    def get_value(*keys, default=None):
        return overrides.get(tuple(keys), default)

    return get_value


class TestLlmDetector:
    """The `llm` choice runs the REAL hybrid detector. These drive it through
    a stub client so no network call happens — what is under test is that the
    endpoint uses the hybrid tier at all and hands its token usage back."""

    def test_usage_from_the_llm_pass_is_returned(self, seeded_app, keyed, monkeypatch):
        import src.anonymization_ner as ner

        class _StubLLM:
            def __init__(self, *_a, **_kw):
                self.last_usage = {"calls": 1, "input_tokens": 1234, "output_tokens": 56, "model": "stub-model"}

            def __call__(self, _markdown):
                return []

        monkeypatch.setattr(ner, "LLMDetector", _StubLLM)
        r = seeded_app["client"].post(
            URL, json={"text": SAMPLE, "detector": "llm"}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        body = r.json()
        assert body["detector"] == "llm"
        assert body["usage"]["input_tokens"] == 1234
        assert body["usage"]["model"] == "stub-model"
        # The deterministic tier still ran — `llm` ADDS to regex, never
        # replaces it, and the drawer's copy says so.
        assert body["counts_by_kind"]["email"] == 1

    def test_an_unavailable_detector_is_502_and_never_a_silent_downgrade(self, seeded_app, keyed, monkeypatch):
        import src.anonymization_ner as ner

        class _DeadLLM:
            def __init__(self, *_a, **_kw):
                self.last_usage = {}

            def __call__(self, _markdown):
                raise ner.DetectionUnavailable("no credentials configured")

        monkeypatch.setattr(ner, "LLMDetector", _DeadLLM)
        r = seeded_app["client"].post(
            URL, json={"text": SAMPLE, "detector": "llm"}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail["error"] == "detector_unavailable"
        assert "no credentials configured" in detail["message"]


class TestNothingIsPersisted:
    def test_no_collection_document_or_extraction_run_is_created(self, seeded_app, keyed):
        """A preview is a pure function of its input. The only write it may
        make is the audit row — anything else would turn a sanity check into
        an ingest."""
        client, token = seeded_app["client"], seeded_app["admin_token"]

        def snapshot() -> dict:
            return {
                "collections": client.get("/api/facts/collections", headers=_auth(token)).text,
                "connections": client.get("/api/admin/source-connections", headers=_auth(token)).text,
            }

        before = snapshot()
        assert client.post(URL, json={"text": SAMPLE}, headers=_auth(token)).status_code == 200
        assert snapshot() == before

    def test_the_audit_row_records_counts_but_never_the_text(self, seeded_app, keyed):
        """The audit trail is the one place an admin's pasted document must
        not land — the row carries the sample's LENGTH and what was redacted,
        and nothing that could reconstruct it."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        secret_line = "Zcela unikátní tajná věta k nalezení."
        assert client.post(URL, json={"text": secret_line}, headers=_auth(token)).status_code == 200

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="anonymization.preview", limit=10)
        assert rows, "the preview must be audited"
        blob = str(rows[0])
        assert "Zcela unikátní" not in blob
        assert "tajná věta" not in blob
        assert str(len(secret_line)) in blob


class TestConfigDrawerRows:
    """The drawer answers "which shapes are redacted"; the panel above
    answers "what will that do to my documents". Neither is much use
    without the other, so the rows are pinned here beside the preview."""

    def _rows(self, client, token) -> dict:
        created = client.post(
            "/api/admin/source-connections",
            json={
                "name": "sp-anonprev",
                "source_type": "sharepoint",
                "config": {"tenant_id": "t", "client_id": "c"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conn_id = created.json()["id"]
        r = client.get(
            f"/api/admin/sharepoint/connections/{conn_id}/extraction/config",
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return {row["label"]: row for row in r.json()["effective"]}

    def test_each_tier_and_the_custom_term_count_are_shown(self, seeded_app):
        rows = self._rows(seeded_app["client"], seeded_app["admin_token"])
        for label in ("Detect phone numbers", "Detect IBANs", "Detect national ids"):
            assert rows[label]["value"] is True
            assert rows[label]["origin"] == "default"
        assert rows["Custom terms"]["value"] == "0 term(s)"

    def test_an_unusable_custom_term_is_reported_in_the_drawer(self, seeded_app, monkeypatch):
        """An operator whose terms cannot compile has every anonymized
        document dropped by every crawl. The drawer says so rather than
        leaving the run report to be the first hint."""
        import app.instance_config as instance_config

        monkeypatch.setattr(
            instance_config,
            "get_value",
            _config_stub({("extraction", "anonymization", "custom_terms"): ["(oops|"]}),
        )
        rows = self._rows(seeded_app["client"], seeded_app["admin_token"])
        assert rows["Custom terms"]["value"] == "invalid"
        assert "unusable" in rows["Custom terms"]["note"]
