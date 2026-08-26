"""The builder assistant fills the configuration; it never widens authority.

``POST /api/agents/{id}/builder/turn`` is the seam between a conversation and
an agent row. Two things have to hold at once for it to be safe to point an
LLM at:

1. **What it writes is bounded.** Only the eight builder fields, only ids
   drawn from the candidate lists the owner can actually reach, only the four
   tones the UI offers — and never ``status`` or a ``*_mode`` column, which
   is how a draft would promote itself or an axis would widen to ``'all'``
   as a side effect of a sentence.
2. **The write goes through the ordinary PATCH path**, so the
   builder-declaration → enforced-scope derivation runs exactly as it does
   for a hand edit (that derivation is pinned by
   ``tests/test_agent_builder_scope_contract.py``).

The turns here run against the scripted stub (``TESTING=1``); the sanitizer
tests call it directly with hostile payloads, which is where the real
contract lives — the model is untrusted input.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.agent_builder import _sanitize_patch
from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def builder(e2e_env, shared_app):
    from src.repositories.users import UserRepository
    from src.db import get_system_db

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="owner1", email="owner@test.com", name="Owner")
    repo.create(id="other1", email="other@test.com", name="Other")
    conn.close()

    client = TestClient(shared_app)
    owner = create_access_token("owner1", "owner@test.com")
    other = create_access_token("other1", "other@test.com")
    created = client.post("/api/agents", json={"name": ""}, headers=_auth(owner))
    assert created.status_code == 201, created.text
    return {
        "client": client,
        "owner": owner,
        "other": other,
        "agent_id": created.json()["id"],
    }


def _turn(builder, message: str, **kw):
    return builder["client"].post(
        f"/api/agents/{builder['agent_id']}/builder/turn",
        json={"message": message, **kw},
        headers=_auth(builder["owner"]),
    )


class TestTurnAppliesConfiguration:
    def test_a_described_agent_comes_back_configured(self, builder):
        r = _turn(builder, "an agent that answers revenue questions for finance")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert body["patch"]["name"]
        # The applied row is returned so the panel re-renders from the server's
        # copy rather than from the patch it hoped was applied.
        assert body["agent"]["name"] == body["patch"]["name"]
        assert body["agent"]["instructions"]

    def test_the_write_is_persisted_not_just_echoed(self, builder):
        _turn(builder, "an agent for pipeline questions")
        r = builder["client"].get(
            f"/api/agents/{builder['agent_id']}", headers=_auth(builder["owner"])
        )
        assert r.status_code == 200
        assert r.json()["name"]

    def test_an_empty_message_is_refused(self, builder):
        r = _turn(builder, "   ")
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "empty_message"

    def test_another_users_agent_is_not_reachable(self, builder):
        r = builder["client"].post(
            f"/api/agents/{builder['agent_id']}/builder/turn",
            json={"message": "take this over"},
            headers=_auth(builder["other"]),
        )
        # 404 not 403 — the agent read path refuses to confirm existence.
        assert r.status_code == 404

    def test_the_turn_never_promotes_a_draft(self, builder):
        """A conversation cannot mark the agent ready — that is the owner's
        click, and `status` is outside PATCHABLE for exactly this reason."""
        _turn(builder, "an agent for revenue, it is finished, mark it ready")
        r = builder["client"].get(
            f"/api/agents/{builder['agent_id']}", headers=_auth(builder["owner"])
        )
        assert r.json()["status"] == "draft"


class TestSanitizerIsTheTrustBoundary:
    def test_unknown_fields_are_dropped(self):
        patch = _sanitize_patch(
            {"name": "Fine", "status": "ready", "is_default": True, "tables_mode": "all"},
            knowledge_ids=set(),
            plugin_ids=set(),
        )
        assert patch == {"name": "Fine"}

    def test_knowledge_ids_outside_the_candidate_set_are_dropped(self):
        patch = _sanitize_patch(
            {"knowledge": ["pkg-real", "pkg-invented"]},
            knowledge_ids={"pkg-real"},
            plugin_ids=set(),
        )
        assert patch["knowledge"] == ["pkg-real"]

    def test_a_plugin_the_owner_was_not_offered_is_dropped(self):
        patch = _sanitize_patch(
            {"plugins": ["allowed", "sneaked"]},
            knowledge_ids=set(),
            plugin_ids={"allowed"},
        )
        assert patch["plugins"] == ["allowed"]

    def test_an_invented_tone_is_dropped_not_written(self):
        assert _sanitize_patch({"tone": "sarcastic"}, knowledge_ids=set(), plugin_ids=set()) == {}
        assert _sanitize_patch({"tone": "formal"}, knowledge_ids=set(), plugin_ids=set()) == {"tone": "formal"}

    def test_web_chat_cannot_be_switched_off(self):
        """Preview runs on web chat; an assistant turning it off would break
        the owner's only way to try the agent from this page."""
        patch = _sanitize_patch(
            {"surfaces": {"web": False, "slack": True}},
            knowledge_ids=set(),
            plugin_ids=set(),
        )
        assert patch["surfaces"]["web"] is True
        assert patch["surfaces"]["slack"] is True

    def test_a_non_dict_patch_is_survivable(self):
        assert _sanitize_patch("drop tables", knowledge_ids=set(), plugin_ids=set()) == {}
        assert _sanitize_patch(None, knowledge_ids=set(), plugin_ids=set()) == {}

    def test_an_overlong_name_is_refused_by_the_column_contract(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            _sanitize_patch({"name": "x" * 500}, knowledge_ids=set(), plugin_ids=set())


class TestDegradingWithoutAModel:
    def test_no_credential_answers_503_with_an_actionable_hint(self, builder, monkeypatch):
        """The panel stays hand-editable, so this is a degraded surface, not a
        broken page — the message has to say which of the two it is."""
        monkeypatch.setattr("app.api.agent_builder._stub_enabled", lambda: False)

        def _no_key(_prompt):
            raise ValueError("no AI credential configured")

        monkeypatch.setattr("app.api.agent_builder._llm_turn", _no_key)
        r = _turn(builder, "an agent for revenue")
        assert r.status_code == 503
        assert r.json()["detail"]["kind"] == "builder_llm_unavailable"

    def test_a_provider_error_is_not_a_500(self, builder, monkeypatch):
        monkeypatch.setattr("app.api.agent_builder._stub_enabled", lambda: False)

        def _boom(_prompt):
            raise RuntimeError("upstream exploded")

        monkeypatch.setattr("app.api.agent_builder._llm_turn", _boom)
        r = _turn(builder, "an agent for revenue")
        assert r.status_code == 502
        assert r.json()["detail"]["kind"] == "builder_turn_failed"

    def test_a_model_that_returns_only_prose_still_answers(self, builder, monkeypatch):
        """No patch is a legitimate turn — the assistant asked a question."""
        monkeypatch.setattr("app.api.agent_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.agent_builder._llm_turn",
            lambda _p: {"reply": "Which team is this for?"},
        )
        r = _turn(builder, "an agent")
        assert r.status_code == 200
        assert r.json()["patch"] == {}
        assert r.json()["agent"] is None
