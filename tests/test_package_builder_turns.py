"""What one data-package builder turn is allowed to do.

The third builder-turn endpoint. Its siblings are
``tests/test_agent_builder_turns.py`` and
``tests/test_entity_builder_turns.py``, and most of the contract is the same:
a message in, a sanitized patch out, model output treated as untrusted input.

What is different is the stake. A data package is the unit governed data
reaches analysts through, and creating one writes GRANTS — "share it with the
sales team" is a sentence that widens who can see tables. Two rules follow,
and both are tested here rather than left to review:

  - the endpoint only ever PROPOSES (there is no `apply` flag, not even one
    defaulting to false), and
  - the candidate lists come from the SERVER, so a caller cannot enlarge the
    set of groups a turn may propose.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.package_builder import PATCHABLE, _sanitize_patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(seeded_app):
    """Admin is Admin-GROUP membership, not a flag — `seeded_app` already
    seeds the four role users and their tokens, so use it rather than
    hand-rolling a half-correct admin."""
    return seeded_app["client"], seeded_app["admin_token"], seeded_app["analyst_token"]


def _turn(client, token=None, **kw):
    c, admin, _ = client
    body = {"message": "our sales pipeline tables"}
    body.update(kw)
    return c.post("/api/admin/data-packages/builder/turn", json=body, headers=_auth(token or admin))


class TestItProposesAndNeverWrites:
    def test_a_turn_answers_with_a_patch(self, client):
        r = _turn(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert isinstance(body["patch"], dict)

    def test_there_is_no_apply_flag_to_find(self, client):
        """Not a default that could be flipped — the parameter does not exist.
        An admin should never learn what a conversation granted by reading it
        back afterwards."""
        from app.api.package_builder import PackageTurnRequest

        assert "apply" not in PackageTurnRequest.model_fields

    def test_it_creates_no_package(self, client):
        c, admin, _ = client
        before = c.get("/api/admin/data-packages", headers=_auth(admin)).json()
        _turn(client)
        after = c.get("/api/admin/data-packages", headers=_auth(admin)).json()
        assert before == after, "a turn changed the package list"

    def test_an_empty_message_is_refused(self, client):
        r = _turn(client, message="   ")
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "empty_message"

    def test_the_scripted_engine_never_proposes_a_group(self, client):
        """The stub must not be the thing that teaches this flow to hand out
        access — it runs on every dev machine and in every test."""
        body = _turn(client).json()
        assert not body["patch"].get("groups")


class TestOnlyAnAdminMayRunOne:
    def test_an_analyst_is_refused(self, client):
        c, _, analyst = client
        r = c.post(
            "/api/admin/data-packages/builder/turn",
            json={"message": "give me everything"},
            headers=_auth(analyst),
        )
        assert r.status_code in (401, 403)

    def test_an_anonymous_caller_is_refused(self, shared_app):
        r = TestClient(shared_app).post(
            "/api/admin/data-packages/builder/turn", json={"message": "hi"}
        )
        assert r.status_code in (401, 403)


class TestSanitizerIsTheTrustBoundary:
    IDS = {"table_ids": {"t1", "t2"}, "group_ids": {"g-sales"}}

    def test_a_fabricated_group_never_survives(self):
        """The one thing here that must never reach a grant."""
        out = _sanitize_patch({"groups": ["g-sales", "g-everyone", "admin"]}, **self.IDS)
        assert out["groups"] == ["g-sales"]

    def test_a_table_the_instance_does_not_have_is_dropped(self):
        out = _sanitize_patch({"tables": ["t1", "t99"]}, **self.IDS)
        assert out["tables"] == ["t1"]

    def test_ids_are_deduped_in_the_order_proposed(self):
        out = _sanitize_patch({"tables": ["t2", "t1", "t2"]}, **self.IDS)
        assert out["tables"] == ["t2", "t1"]

    def test_a_non_list_is_not_coerced(self):
        """A bare string is not a one-element grant list."""
        assert _sanitize_patch({"groups": "g-sales"}, **self.IDS) == {}

    def test_non_string_members_are_dropped(self):
        out = _sanitize_patch({"groups": [{"id": "g-sales"}, None, 7, "g-sales"]}, **self.IDS)
        assert out["groups"] == ["g-sales"]

    def test_unknown_fields_are_dropped(self):
        out = _sanitize_patch(
            {"name": "ok", "visibility": "public", "owner": "someone"}, **self.IDS
        )
        assert out == {"name": "ok"}

    def test_slug_is_not_patchable(self):
        """It is derived from the name and the drawer keeps them in step; a
        model writing one directly could only desynchronise them."""
        assert "slug" not in PATCHABLE
        assert _sanitize_patch({"slug": "hijacked"}, **self.IDS) == {}

    def test_a_non_dict_patch_is_survivable(self):
        for raw in (None, [], "nope", 3):
            assert _sanitize_patch(raw, **self.IDS) == {}


class TestDegradingWithoutAModel:
    def test_no_credential_answers_503_with_an_actionable_hint(self, client, monkeypatch):
        monkeypatch.setattr("app.api.package_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.package_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("no credential")),
        )
        r = _turn(client)
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["kind"] == "builder_llm_unavailable"
        assert "by hand" in detail["hint"]

    def test_a_provider_error_is_not_a_500(self, client, monkeypatch):
        monkeypatch.setattr("app.api.package_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.package_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _turn(client).status_code == 502
