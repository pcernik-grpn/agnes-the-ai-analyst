"""Every builder turn labels the LLM call it makes.

Five endpoints run the same shape of call — one structured extraction, from
a request handler, on behalf of a signed-in caller — and until they said so,
a cost report could only see five identical anonymous generations. Each
route now passes the caller (and the subject, where one exists) into
``_llm_turn``, which pushes them onto the LLM call context the extractor's
own ``trace_generation`` reads.

One file rather than five additions, because it is ONE contract: the MCP
builder has no route-level test file of its own, and splitting the same
assertion across four unrelated files would hide the fact that they must
agree. The route is driven end-to-end (not ``_llm_turn`` directly) because
half of what is under test is the wiring at the call site.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from src.observability.llm_context import LlmCallContext, current_llm_context


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _RecordingExtractor:
    """A ``StructuredExtractor`` stand-in that reports the context it was
    called under instead of calling a model."""

    def __init__(self, seen: list[LlmCallContext], reply: dict | None = None) -> None:
        self._seen = seen
        self._reply = reply if reply is not None else {"reply": "Tell me more."}

    def extract_json(self, *_args, **_kwargs) -> dict:
        self._seen.append(current_llm_context())
        return dict(self._reply)


@pytest.fixture
def recording_extractor(monkeypatch):
    """Replace the factory every ``_llm_turn`` resolves at call time, and
    turn the scripted stub off so the model path is the one that runs."""
    seen: list[LlmCallContext] = []
    monkeypatch.setattr(
        "connectors.llm.create_extractor_from_env_or_config",
        lambda *_a, **_k: _RecordingExtractor(seen),
    )
    for module in (
        "app.api.entity_builder",
        "app.api.agent_builder",
        "app.api.mcp_builder",
        "app.api.package_builder",
        "app.api.semantic_model_builder",
    ):
        monkeypatch.setattr(f"{module}.stub_enabled", lambda: False)
    return seen


def _only(seen: list[LlmCallContext]) -> LlmCallContext:
    assert len(seen) == 1, f"expected exactly one labelled generation, got {len(seen)}"
    return seen[0]


def test_the_entity_builder_labels_its_turn(e2e_env, shared_app, recording_extractor):
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="author1", email="author@test.com", name="Author")
    conn.close()
    client = TestClient(shared_app)
    token = create_access_token("author1", "author@test.com")

    r = client.post(
        "/api/store/entities/builder/turn",
        json={"type": "skill", "message": "a recipe for the weekly revenue report"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    ctx = _only(recording_extractor)
    assert (ctx.workload, ctx.purpose) == ("builder", "entity_builder_turn")
    assert ctx.user_id == "author1"


def test_the_agent_builder_labels_its_turn_with_the_agent_as_subject(e2e_env, shared_app, recording_extractor):
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()
    client = TestClient(shared_app)
    token = create_access_token("owner1", "owner@test.com")

    created = client.post("/api/v1/agents", json={"name": "Untitled", "status": "draft"}, headers=_auth(token))
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    r = client.post(
        f"/api/agents/{agent_id}/builder/turn",
        json={"message": "an agent that answers revenue questions"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    ctx = _only(recording_extractor)
    assert (ctx.workload, ctx.purpose) == ("builder", "agent_builder_turn")
    assert ctx.user_id == "owner1"
    assert ctx.subject_id == agent_id


def test_the_mcp_builder_labels_its_turn(seeded_app, recording_extractor):
    client, admin = seeded_app["client"], seeded_app["admin_token"]

    r = client.post(
        "/api/admin/mcp-sources/builder/turn",
        json={"message": "connect our ticketing server"},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text

    ctx = _only(recording_extractor)
    assert (ctx.workload, ctx.purpose) == ("builder", "mcp_builder_turn")
    assert ctx.user_id == "admin1"


def test_the_package_builder_labels_its_turn(seeded_app, recording_extractor):
    client, admin = seeded_app["client"], seeded_app["admin_token"]

    r = client.post(
        "/api/admin/data-packages/builder/turn",
        json={"message": "our sales pipeline tables"},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text

    ctx = _only(recording_extractor)
    assert (ctx.workload, ctx.purpose) == ("builder", "package_builder_turn")
    assert ctx.user_id == "admin1"


def test_the_semantic_model_builder_labels_its_turn(seeded_app, recording_extractor):
    client, admin = seeded_app["client"], seeded_app["admin_token"]

    r = client.post(
        "/api/semantic-models/builder/turn",
        json={"message": "a model over our orders table"},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text

    ctx = _only(recording_extractor)
    assert (ctx.workload, ctx.purpose) == ("builder", "semantic_model_builder_turn")
    assert ctx.user_id == "admin1"


def test_the_label_does_not_outlive_the_turn(seeded_app, recording_extractor):
    """The context is scoped to the call — a request handler that ran a
    builder turn must not leave 'builder' bound for whatever runs next in
    the same task."""
    client, admin = seeded_app["client"], seeded_app["admin_token"]

    r = client.post(
        "/api/admin/data-packages/builder/turn",
        json={"message": "our sales pipeline tables"},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    assert current_llm_context() == LlmCallContext()
