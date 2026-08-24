"""`response_format: json_schema` — server-side structured output (V1b Task 7).

`app.chat.structured_output.validate`/`schema_directive` are pure-function
unit tests; the API section drives `POST /api/v1/agents/{slug}/responses`
with a fake `run_one_shot` (same seam `test_agent_responses_api.py` uses)
returning a canned answer, to exercise the 200-with-`parsed` and
422-`schema_validation_failed` wiring end to end — including that the 422 is
stored under an `Idempotency-Key` and replayed verbatim (no re-run) like any
other terminal response (C13)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.chat.structured_output import schema_directive, validate

# ---------------------------------------------------------------------------
# structured_output.validate / schema_directive — pure unit tests
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}
_RESPONSE_FORMAT = {"type": "json_schema", "schema": _SCHEMA}


def test_validate_valid_json_matching_schema():
    ok, parsed, err = validate('{"name": "Ada", "age": 30}', _RESPONSE_FORMAT)
    assert ok is True
    assert parsed == {"name": "Ada", "age": 30}
    assert err is None


def test_validate_tolerates_fenced_json_block():
    answer = '```json\n{"name": "Ada", "age": 30}\n```'
    ok, parsed, err = validate(answer, _RESPONSE_FORMAT)
    assert ok is True
    assert parsed == {"name": "Ada", "age": 30}
    assert err is None


def test_validate_tolerates_fenced_block_without_json_tag():
    answer = '```\n{"name": "Ada", "age": 30}\n```'
    ok, parsed, err = validate(answer, _RESPONSE_FORMAT)
    assert ok is True
    assert parsed == {"name": "Ada", "age": 30}


def test_validate_unfenced_json_with_embedded_fence_in_string_value():
    """A correct, unfenced top-level JSON answer whose string value happens
    to contain an embedded ```-fenced snippet must parse as-is — not get
    mangled by `_strip_fence` scanning for ANY triple-backtick pair in the
    whole answer and returning only the substring between them."""
    schema = {"type": "object"}
    answer = '{"snippet": "here is code: ```python\\nprint(1)\\n``` end"}'
    ok, parsed, err = validate(answer, {"type": "json_schema", "schema": schema})
    assert ok is True
    assert parsed == {"snippet": "here is code: ```python\nprint(1)\n``` end"}
    assert err is None


def test_validate_malformed_json_returns_error():
    ok, parsed, err = validate("not json at all {", _RESPONSE_FORMAT)
    assert ok is False
    assert parsed is None
    assert err
    assert "json" in err.lower()


def test_validate_schema_violation_returns_error():
    ok, parsed, err = validate('{"name": "Ada"}', _RESPONSE_FORMAT)  # missing "age"
    assert ok is False
    assert parsed is None
    assert err


def test_validate_non_json_schema_response_format_is_ignored():
    ok, parsed, err = validate("literally anything, not even json", {"type": "text"})
    assert ok is True
    assert parsed is None
    assert err is None


def test_validate_none_response_format_is_ignored():
    ok, parsed, err = validate("anything", None)
    assert ok is True
    assert parsed is None
    assert err is None


def test_schema_directive_mentions_json_and_embeds_schema():
    directive = schema_directive(_RESPONSE_FORMAT)
    assert "JSON" in directive
    assert "name" in directive
    assert "age" in directive


# ---------------------------------------------------------------------------
# API wiring: POST /api/v1/agents/{slug}/responses
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import agents_repo, resource_grants_repo, user_group_members_repo, user_groups_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="owner1",
        name="Support Bot",
        slug="support-bot",
    )

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "agent_id": agent_id,
    }


def _patch_run_one_shot(monkeypatch, *, chat_id="chat-1", answer="hi there", timed_out=False, calls=None):
    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        if calls is not None:
            calls.append({"user_email": user_email, "agent_id": agent_id, "prompt": prompt, "timeout_s": timeout_s})
        return {"chat_id": chat_id, "answer": answer, "timed_out": timed_out}

    import app.api.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "run_one_shot", _fake_run_one_shot)
    monkeypatch.setattr(agent_runtime, "get_current_chat_manager", lambda: object())
    return _fake_run_one_shot


def test_responses_without_response_format_unchanged(env, monkeypatch):
    calls = []
    _patch_run_one_shot(monkeypatch, answer="plain text answer", calls=calls)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "plain text answer"
    assert "parsed" not in body
    # No directive appended when response_format is absent.
    assert calls[0]["prompt"] == "hi"


def test_responses_matching_schema_returns_200_with_parsed(env, monkeypatch):
    calls = []
    _patch_run_one_shot(monkeypatch, answer='{"name": "Ada", "age": 30}', calls=calls)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "give me a person", "response_format": _RESPONSE_FORMAT},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == '{"name": "Ada", "age": 30}'
    assert body["parsed"] == {"name": "Ada", "age": 30}
    # The schema directive was appended to the prompt sent to run_one_shot.
    assert "give me a person" in calls[0]["prompt"]
    assert "JSON" in calls[0]["prompt"]


def test_responses_violating_schema_returns_422_structured(env, monkeypatch):
    _patch_run_one_shot(monkeypatch, chat_id="chat-bad", answer='{"name": "Ada"}')
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "give me a person", "response_format": _RESPONSE_FORMAT},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "schema_validation_failed"
    assert body["message"]
    assert body["session_id"] == "chat-bad"
    assert body["raw_answer"] == '{"name": "Ada"}'
    assert body["usage"] == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}


def test_responses_malformed_json_returns_422_structured(env, monkeypatch):
    _patch_run_one_shot(monkeypatch, chat_id="chat-malformed", answer="not json")
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "give me a person", "response_format": _RESPONSE_FORMAT},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "schema_validation_failed"
    assert body["raw_answer"] == "not json"


def test_422_schema_validation_failure_is_idempotency_replayed_without_rerun(env, monkeypatch):
    """C13: a 422 schema_validation_failed must not orphan a paid run — it is
    stored under the Idempotency-Key like any other terminal response, so a
    retry replays the SAME 422 instead of re-running `run_one_shot`."""
    calls = []
    _patch_run_one_shot(monkeypatch, chat_id="chat-idem", answer='{"name": "Ada"}', calls=calls)

    headers = {**_auth(env["owner_token"]), "Idempotency-Key": "schema-fail-key"}
    body = {"input": "give me a person", "response_format": _RESPONSE_FORMAT}

    r1 = env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)
    assert r1.status_code == 422
    assert len(calls) == 1

    r2 = env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)
    assert r2.status_code == 422
    assert len(calls) == 1  # run_one_shot NOT called again — no re-run of the paid work
    assert r2.json() == r1.json()


# ---------------------------------------------------------------------------
# background job path: schema validation happens in the worker handler
# ---------------------------------------------------------------------------


def test_background_job_schema_violation_fails_job_with_structured_error(env, monkeypatch):
    import asyncio

    import app.chat.headless as headless

    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        return {"chat_id": "chat-bg-bad", "answer": '{"name": "Ada"}', "timed_out": False}

    monkeypatch.setattr(headless, "run_one_shot", _fake_run_one_shot)

    from app.worker.kinds import _run_agent_response

    async def _drive():
        from app.chat.manager import set_current_chat_manager
        from src.repositories import jobs_repo

        set_current_chat_manager(object())
        try:
            job = jobs_repo().enqueue(
                "agent_response",
                {
                    "mode": "fresh",
                    "owner_user_id": "owner1",
                    "owner_email": "owner@test.com",
                    "agent_id": env["agent_id"],
                    "prompt": "give me a person",
                    "response_format": _RESPONSE_FORMAT,
                },
            )
            claimed = jobs_repo().claim_next(kinds=["agent_response"], worker_id="w1")
            assert claimed["id"] == job["id"]
            try:
                await asyncio.to_thread(_run_agent_response, claimed["payload_json"])
            except RuntimeError as exc:
                jobs_repo().fail(job["id"], "w1", claimed["lease_token"], str(exc), retry_in_seconds=None)
            return job["id"]
        finally:
            set_current_chat_manager(None)

    job_id = asyncio.run(_drive())

    # Read back through the public API — `_serialize_error`
    # (`app/api/agent_runtime.py`) is what turns the raw persisted error
    # string back into the structured `{"code": ..., ...}` shape.
    r = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert r.status_code == 200
    finished = r.json()
    assert finished["status"] == "failed"
    assert finished["error"]["code"] == "schema_validation_failed"
    assert finished["error"]["session_id"] == "chat-bg-bad"
    assert finished["error"]["raw_answer"] == '{"name": "Ada"}'


def test_background_job_timed_out_answer_skips_schema_validation(env, monkeypatch):
    """Bounded-wait-leg contract: a timed-out leg's answer is empty/partial
    by definition — validating it would fail the job as
    schema_validation_failed and misreport a healthy long-running turn.
    The leg must COMPLETE with ``timed_out: true`` and no ``parsed``; only
    the final (non-timed-out) leg is schema-validated."""
    import asyncio

    import app.chat.headless as headless

    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        return {"chat_id": "chat-bg-slow", "answer": "", "timed_out": True}

    monkeypatch.setattr(headless, "run_one_shot", _fake_run_one_shot)

    from app.worker.kinds import _run_agent_response

    async def _drive():
        from app.chat.manager import set_current_chat_manager
        from src.repositories import jobs_repo

        set_current_chat_manager(object())
        try:
            job = jobs_repo().enqueue(
                "agent_response",
                {
                    "mode": "fresh",
                    "owner_user_id": "owner1",
                    "owner_email": "owner@test.com",
                    "agent_id": env["agent_id"],
                    "prompt": "give me a person",
                    "response_format": _RESPONSE_FORMAT,
                },
            )
            claimed = jobs_repo().claim_next(kinds=["agent_response"], worker_id="w1")
            assert claimed["id"] == job["id"]
            result = await asyncio.to_thread(_run_agent_response, claimed["payload_json"])
            jobs_repo().complete(job["id"], "w1", claimed["lease_token"], result=result)
            return job["id"]
        finally:
            set_current_chat_manager(None)

    job_id = asyncio.run(_drive())

    r = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert r.status_code == 200
    finished = r.json()
    assert finished["status"] == "completed"
    assert finished["result"]["timed_out"] is True
    assert finished["result"]["answer"] == ""
    assert "parsed" not in finished["result"]
