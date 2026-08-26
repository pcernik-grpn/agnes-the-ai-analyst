"""What one /skills builder turn is allowed to do.

The server-side half of the entity builder. Its sibling is
``tests/test_agent_builder_turns.py``; the two endpoints are deliberately
close, and the differences tested here are the ones the entities force:

  - it is STATELESS — a Library entity has no row until the author saves it,
    so the draft travels in the request and nothing is read or written;
  - it NEVER applies — there is nothing to apply to, and *Save to Library* is
    the only thing that creates;
  - the patch is PER TYPE — a plugin's contents are an uploaded archive, so
    no conversation may produce a body for one.

The turns run against the scripted stub (``TESTING=1``); the sanitizer tests
call it directly with hostile payloads, which is where the real contract
lives — model output is untrusted input.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.entity_builder import PATCHABLE, _sanitize_patch
from app.auth.jwt import create_access_token

CATS = ["Data", "Reporting"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(e2e_env, shared_app):
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="author1", email="author@test.com", name="Author")
    conn.close()
    return TestClient(shared_app), create_access_token("author1", "author@test.com")


def _turn(client, **kw):
    c, token = client
    body = {"type": "skill", "message": "a recipe for the weekly revenue report"}
    body.update(kw)
    return c.post("/api/store/entities/builder/turn", json=body, headers=_auth(token))


class TestATurnDrafts:
    def test_a_described_skill_comes_back_drafted(self, client):
        r = _turn(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert body["patch"]["name"]
        assert body["patch"]["body"], "a skill's body is writable"

    def test_it_writes_nothing(self, client):
        """There is no row to write to, and Save to Library is the only thing
        that creates. A turn that persisted would be a back door around it."""
        c, token = client
        before = c.get("/api/store/entities", headers=_auth(token))
        _turn(client)
        after = c.get("/api/store/entities", headers=_auth(token))
        assert before.status_code == after.status_code
        assert before.json() == after.json(), "a turn changed the store"

    def test_an_empty_message_is_refused(self, client):
        r = _turn(client, message="   ")
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "empty_message"

    def test_an_unknown_type_is_refused(self, client):
        r = _turn(client, type="database")
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "unknown_type"

    def test_it_needs_a_caller(self, shared_app):
        assert TestClient(shared_app).post(
            "/api/store/entities/builder/turn", json={"type": "skill", "message": "hi"}
        ).status_code in (401, 403)


class TestAPluginsContentsAreNotWritable:
    """A plugin is a .zip the author uploads. A patch that claimed to write
    its contents would silently discard what they attached."""

    def test_body_is_not_patchable_for_a_plugin(self):
        assert "body" not in PATCHABLE["plugin"]
        assert "body" in PATCHABLE["skill"] and "body" in PATCHABLE["agent"]

    def test_a_body_from_the_model_is_dropped(self):
        out = _sanitize_patch(
            {"name": "jira-tools", "body": "## not yours to write"},
            entity_type="plugin",
            categories=CATS,
        )
        assert out == {"name": "jira-tools"}

    def test_the_stub_says_so_rather_than_pretending(self, client):
        r = _turn(client, type="plugin", message="write me a plugin that adds jira commands")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "body" not in body["patch"]
        assert "upload" in body["reply"].lower()


class TestSanitizerIsTheTrustBoundary:
    def test_unknown_fields_are_dropped(self):
        out = _sanitize_patch(
            {"name": "ok", "visibility_status": "approved", "owner_id": "someone-else"},
            entity_type="skill",
            categories=CATS,
        )
        assert out == {"name": "ok"}

    def test_an_invented_category_is_dropped(self):
        """It would be rejected by the store on save, and would meanwhile show
        the author a choice that does not exist."""
        assert _sanitize_patch({"category": "Nonsense"}, entity_type="skill", categories=CATS) == {}
        assert _sanitize_patch({"category": "Data"}, entity_type="skill", categories=CATS) == {"category": "Data"}

    def test_a_non_string_is_dropped(self):
        for value in ({"a": 1}, ["x"], 7, None, True):
            assert _sanitize_patch({"name": value}, entity_type="skill", categories=CATS) == {}

    def test_a_non_dict_patch_is_survivable(self):
        for raw in (None, [], "nope", 3):
            assert _sanitize_patch(raw, entity_type="skill", categories=CATS) == {}

    def test_overlong_values_are_capped_not_refused(self):
        """A too-long field should be trimmed to what the column takes, not
        thrown away — the author still gets the work."""
        out = _sanitize_patch({"name": "x" * 500}, entity_type="skill", categories=CATS)
        assert 0 < len(out["name"]) <= 64

    def test_a_body_is_capped_but_generous(self):
        out = _sanitize_patch({"body": "y" * 100000}, entity_type="skill", categories=CATS)
        assert len(out["body"]) == 40000

    def test_whitespace_only_values_do_not_blank_a_field_silently(self):
        """Trimming is fine; the point is that what comes back is what the
        panel will show."""
        out = _sanitize_patch({"name": "  spaced  "}, entity_type="skill", categories=CATS)
        assert out == {"name": "spaced"}


class TestDegradingWithoutAModel:
    def test_no_credential_answers_503_with_an_actionable_hint(self, client, monkeypatch):
        monkeypatch.setattr("app.api.entity_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.entity_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("no credential")),
        )
        r = _turn(client)
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["kind"] == "builder_llm_unavailable"
        assert "by hand" in detail["hint"], "the hint must name the working path"

    def test_a_provider_error_is_not_a_500(self, client, monkeypatch):
        monkeypatch.setattr("app.api.entity_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.entity_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream exploded")),
        )
        r = _turn(client)
        assert r.status_code == 502
        assert r.json()["detail"]["kind"] == "builder_turn_failed"

    def test_a_model_that_returns_only_prose_still_answers(self, client, monkeypatch):
        monkeypatch.setattr("app.api.entity_builder._stub_enabled", lambda: False)
        monkeypatch.setattr("app.api.entity_builder._llm_turn", lambda *a, **k: {"reply": "Tell me more."})
        r = _turn(client)
        assert r.status_code == 200
        assert r.json() == {"reply": "Tell me more.", "patch": {}, "suggestions": []}
