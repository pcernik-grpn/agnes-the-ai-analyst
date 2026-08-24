"""`POST /api/v1/agents/{slug}/responses` + `GET /api/v1/jobs/{id}` (Task 9).

`run_one_shot` is monkeypatched at the router seam (`app.api.agent_runtime
.run_one_shot`) per the task brief — these are API-contract tests, not
chat-sandbox integration tests (that's `app/chat/headless.py`'s own concern).
The background-job path additionally exercises the REAL worker handler
(`app.worker.kinds._run_agent_response`) with `app.chat.headless.run_one_shot`
(a different import site — the handler resolves it via its own deferred
import) monkeypatched instead, so the job/result plumbing gets real coverage
too.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


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
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
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
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=other_agent_id,
        owner_user_id="owner1",
        name="Other Agent",
        slug="other-agent",
    )

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


def _mint_agent_pat(owner_email: str, owner_id: str, agent_id: str, token_id: str) -> str:
    return create_access_token(
        user_id=owner_id,
        email=owner_email,
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": agent_id},
    )


def _register_agent_pat_row(owner_id: str, agent_id: str, token: str, token_id: str) -> None:
    from src.repositories import access_token_repo

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=owner_id,
        name="agent-pat",
        token_hash=token_hash,
        prefix=token_id.replace("-", "")[:8],
        agent_id=agent_id,
    )


def _patch_run_one_shot(monkeypatch, *, chat_id="chat-1", answer="hi there", timed_out=False, calls=None):
    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        if calls is not None:
            calls.append({"user_email": user_email, "agent_id": agent_id, "prompt": prompt, "timeout_s": timeout_s})
        return {"chat_id": chat_id, "answer": answer, "timed_out": timed_out}

    import app.api.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "run_one_shot", _fake_run_one_shot)
    monkeypatch.setattr(agent_runtime, "get_current_chat_manager", lambda: object())
    return _fake_run_one_shot


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_sync_happy_path_returns_answer(env, monkeypatch):
    _patch_run_one_shot(monkeypatch, answer="the answer")
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "what's up"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "the answer"
    assert body["session_id"] == "chat-1"
    assert body["response_id"]
    assert body["agent_config_hash"]
    assert len(body["agent_config_hash"]) == 16
    assert body["usage"] == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    assert body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


# ---------------------------------------------------------------------------
# auth chain
# ---------------------------------------------------------------------------


def test_unknown_slug_returns_404(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/does-not-exist/responses",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "agent_not_found"


def test_agent_pat_for_different_agent_returns_403(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    token_id = "tok-wrong-agent"
    token = _mint_agent_pat("owner@test.com", "owner1", env["other_agent_id"], token_id)
    _register_agent_pat_row("owner1", env["other_agent_id"], token, token_id)

    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi"},
        headers=_auth(token),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "agent_pat_wrong_agent"


def test_agent_pat_for_matching_agent_succeeds(env, monkeypatch):
    _patch_run_one_shot(monkeypatch, answer="pat answer")
    token_id = "tok-right-agent"
    token = _mint_agent_pat("owner@test.com", "owner1", env["agent_id"], token_id)
    _register_agent_pat_row("owner1", env["agent_id"], token, token_id)

    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["answer"] == "pat answer"


def test_missing_chat_grant_returns_403(env, monkeypatch):
    """`other1` has no `user_group_members` row at all — `can_access`
    returns False with zero setup needed (see `app.auth.access._user_group_ids`:
    every membership must be a concrete row)."""
    _patch_run_one_shot(monkeypatch)

    from src.repositories import agents_repo

    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="other1", name="Other's Agent", slug="others-bot")

    r = env["client"].post(
        "/api/v1/agents/others-bot/responses",
        json={"input": "hi"},
        headers=_auth(env["other_token"]),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "chat_access_denied"


@pytest.mark.parametrize("raw_timeout,expected", [(0, 1), (-5, 1), (5000, 600), (30, 30)])
def test_timeout_s_is_clamped(env, monkeypatch, raw_timeout, expected):
    calls = []
    _patch_run_one_shot(monkeypatch, calls=calls)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi", "timeout_s": raw_timeout},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert calls[0]["timeout_s"] == expected


def test_empty_input_returns_422(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "   "},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# sync timeout -> degrades to background job
# ---------------------------------------------------------------------------


def test_sync_timeout_degrades_to_background_job(env, monkeypatch):
    """Genuine timeout — the sink collected NO answer yet (`answer=""`).
    See `test_sync_timeout_with_partial_answer_returns_200_not_job` below
    for the review carry-over fix covering the OTHER case (an answer was
    already collected before the wait timed out)."""
    _patch_run_one_shot(monkeypatch, chat_id="chat-timeout", answer="", timed_out=True)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "slow one"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert job_id

    from src.repositories import jobs_repo

    job = jobs_repo().get(job_id)
    assert job["kind"] == "agent_response"
    assert job["payload_json"]["mode"] == "continue"
    assert job["payload_json"]["chat_id"] == "chat-timeout"
    assert job["payload_json"]["owner_user_id"] == "owner1"


def test_sync_timeout_with_partial_answer_returns_200_not_job(env, monkeypatch):
    """Review carry-over: `timed_out=True` but the sink already collected an
    answer before the wait timed out — serve it now (200) instead of
    degrading to a background job for an answer already in hand."""
    calls = []
    _patch_run_one_shot(monkeypatch, chat_id="chat-partial", answer="partial but usable", timed_out=True, calls=calls)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "slow one"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "partial but usable"
    assert body["session_id"] == "chat-partial"
    assert "job_id" not in body

    from src.repositories import jobs_repo

    assert jobs_repo().list(kind="agent_response") == []  # no job enqueued


# ---------------------------------------------------------------------------
# ConcurrencyCapHit -> 429
# ---------------------------------------------------------------------------


def test_sync_concurrency_cap_hit_returns_429(env, monkeypatch):
    """`app/api/chat.py::create_session` 429s on the same
    `ConcurrencyCapHit` condition — this router must too, instead of
    letting it escape as an unhandled 500."""
    from app.chat.manager import ConcurrencyCapHit

    async def _boom(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        raise ConcurrencyCapHit("user owner@test.com has 3 active sessions; cap = 3")

    import app.api.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "run_one_shot", _boom)
    monkeypatch.setattr(agent_runtime, "get_current_chat_manager", lambda: object())

    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "concurrency_cap"


# ---------------------------------------------------------------------------
# background: true -> immediate 202
# ---------------------------------------------------------------------------


def test_background_true_enqueues_job_without_calling_run_one_shot(env, monkeypatch):
    calls = []
    _patch_run_one_shot(monkeypatch, calls=calls)

    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "do this later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert not calls  # run_one_shot must NOT run synchronously for background:true

    from src.repositories import jobs_repo

    job = jobs_repo().get(job_id)
    assert job["kind"] == "agent_response"
    assert job["payload_json"]["mode"] == "fresh"
    assert job["payload_json"]["prompt"] == "do this later"
    assert job["status"] == "queued"


def test_background_job_completed_via_worker_handler(env, monkeypatch):
    """Drives the REAL `app.worker.kinds._run_agent_response` handler (not
    just the enqueue) end to end, with `app.chat.headless.run_one_shot`
    monkeypatched at ITS OWN import site (the handler resolves it via a
    deferred import inside the function body, so patching
    `app.chat.headless.run_one_shot` — not `app.api.agent_runtime
    .run_one_shot` — is the correct seam here)."""
    import app.chat.headless as headless

    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        return {"chat_id": "chat-bg-1", "answer": "background answer", "timed_out": False}

    monkeypatch.setattr(headless, "run_one_shot", _fake_run_one_shot)

    from app.worker.kinds import _run_agent_response

    job = None

    async def _drive():
        nonlocal job
        from app.chat.manager import set_current_chat_manager
        from src.repositories import jobs_repo

        set_current_chat_manager(object())  # captures asyncio.run's loop
        try:
            job = jobs_repo().enqueue(
                "agent_response",
                {
                    "mode": "fresh",
                    "owner_user_id": "owner1",
                    "owner_email": "owner@test.com",
                    "agent_id": env["agent_id"],
                    "prompt": "background prompt",
                },
            )
            result = await asyncio.to_thread(_run_agent_response, job["payload_json"])
            jobs_repo().complete(job["id"], "w1", "irrelevant-since-not-claimed", result)
        finally:
            set_current_chat_manager(None)

    asyncio.run(_drive())

    from src.repositories import jobs_repo

    # complete() above used a bogus lease_token deliberately — it's a no-op
    # against an un-claimed job (see JobsRepository.complete's guard), so
    # claim it properly first to exercise the real completion path.
    claimed = jobs_repo().claim_next(kinds=["agent_response"], worker_id="w1")
    assert claimed["id"] == job["id"]

    async def _drive2():
        from app.chat.manager import set_current_chat_manager

        set_current_chat_manager(object())
        try:
            result = await asyncio.to_thread(_run_agent_response, claimed["payload_json"])
            jobs_repo().complete(claimed["id"], "w1", claimed["lease_token"], result)
        finally:
            set_current_chat_manager(None)

    asyncio.run(_drive2())

    finished = jobs_repo().get(job["id"])
    assert finished["status"] == "done"
    assert finished["payload_json"]["result"]["answer"] == "background answer"
    assert finished["payload_json"]["result"]["session_id"] == "chat-bg-1"
    assert finished["payload_json"]["result"]["timed_out"] is False


# ---------------------------------------------------------------------------
# worker handler: ConcurrencyCapHit + usage-accumulator flush (review carry-over)
# ---------------------------------------------------------------------------


def test_worker_handler_concurrency_cap_hit_raises_with_recognizable_prefix(env, monkeypatch):
    """`_run_agent_response` must not let a `ConcurrencyCapHit` from
    `run_one_shot`'s `create_session()` call fail the job with a raw
    traceback — it re-raises with `CONCURRENCY_CAP_ERROR_PREFIX` so
    `app/api/agent_runtime.py::_serialize_job` can surface a structured
    `{"code": "concurrency_cap", ...}` for `GET /api/v1/jobs/{id}`."""
    import app.chat.headless as headless
    from app.chat.manager import ConcurrencyCapHit

    async def _boom(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        raise ConcurrencyCapHit("user owner@test.com has 3 active sessions; cap = 3")

    monkeypatch.setattr(headless, "run_one_shot", _boom)

    from app.worker.kinds import CONCURRENCY_CAP_ERROR_PREFIX, _run_agent_response

    async def _drive():
        from app.chat.manager import set_current_chat_manager

        set_current_chat_manager(object())
        try:
            await asyncio.to_thread(
                _run_agent_response,
                {
                    "mode": "fresh",
                    "owner_email": "owner@test.com",
                    "agent_id": env["agent_id"],
                    "prompt": "hi",
                },
            )
        finally:
            set_current_chat_manager(None)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_drive())

    assert str(exc_info.value).startswith(CONCURRENCY_CAP_ERROR_PREFIX)


def test_worker_handler_flushes_usage_accumulator_before_summing(env, monkeypatch):
    """The sync path (`app/api/agent_runtime.py`) flushes the broker's
    batched `llm_usage` ledger before summing a session's usage — the
    worker handler must do the same, or a background/degraded run's
    reported `usage` can undercount whatever is still sitting in the
    in-memory accumulator."""
    import app.chat.headless as headless

    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        return {"chat_id": "chat-flush-1", "answer": "flushed answer", "timed_out": False}

    monkeypatch.setattr(headless, "run_one_shot", _fake_run_one_shot)

    calls = []
    from app.api.broker_agent_policy import usage_accumulator

    monkeypatch.setattr(usage_accumulator, "flush", lambda: calls.append(True))

    from app.worker.kinds import _run_agent_response

    async def _drive():
        from app.chat.manager import set_current_chat_manager

        set_current_chat_manager(object())
        try:
            return await asyncio.to_thread(
                _run_agent_response,
                {
                    "mode": "fresh",
                    "owner_email": "owner@test.com",
                    "agent_id": env["agent_id"],
                    "prompt": "hi",
                },
            )
        finally:
            set_current_chat_manager(None)

    result = asyncio.run(_drive())
    assert calls == [True]
    assert result["answer"] == "flushed answer"


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotency_replay_returns_identical_body_without_recalling(env, monkeypatch):
    calls = []
    _patch_run_one_shot(monkeypatch, answer="cached answer", calls=calls)

    headers = {**_auth(env["owner_token"]), "Idempotency-Key": "replay-key-1"}
    body = {"input": "idempotent call"}

    r1 = env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)
    assert r1.status_code == 200
    assert len(calls) == 1

    r2 = env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)
    assert r2.status_code == 200
    assert len(calls) == 1  # run_one_shot NOT called again
    assert r2.json() == r1.json()


def test_idempotency_key_reuse_with_different_body_returns_409(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    headers = {**_auth(env["owner_token"]), "Idempotency-Key": "replay-key-2"}

    r1 = env["client"].post("/api/v1/agents/support-bot/responses", json={"input": "first body"}, headers=headers)
    assert r1.status_code == 200

    r2 = env["client"].post("/api/v1/agents/support-bot/responses", json={"input": "different body"}, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "idempotency_key_reuse"


def test_idempotency_key_scoped_to_owner_and_agent(env, monkeypatch):
    """Same key, same body, but a DIFFERENT agent -> independent replay
    slot, not a false 409."""
    _patch_run_one_shot(monkeypatch, answer="answer for support-bot")

    headers = {**_auth(env["owner_token"]), "Idempotency-Key": "shared-key"}
    r1 = env["client"].post("/api/v1/agents/support-bot/responses", json={"input": "same body"}, headers=headers)
    assert r1.status_code == 200

    r2 = env["client"].post("/api/v1/agents/other-agent/responses", json={"input": "same body"}, headers=headers)
    assert r2.status_code == 200


def test_idempotency_in_flight_reservation_returns_409(env, monkeypatch):
    """Review carry-over: a request that reserved a key and then crashed
    before fulfilling it (simulated here via `jobs_repo().enqueue` raising
    AFTER the reservation lands) must not let a concurrent/retried call
    under the SAME key double-execute — it 409s in-flight instead, as long
    as the reservation is still fresh."""
    import app.api.agent_runtime as agent_runtime
    from src.repositories import jobs_repo as real_jobs_repo

    class _BoomJobsRepo:
        def enqueue(self, *a, **kw):
            raise RuntimeError("simulated crash after reservation, before fulfillment")

    monkeypatch.setattr(agent_runtime, "jobs_repo", lambda: _BoomJobsRepo())

    headers = {**_auth(env["owner_token"]), "Idempotency-Key": "crash-key"}
    body = {"input": "do this later", "background": True}

    with pytest.raises(RuntimeError):
        env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)

    # Restore the real jobs_repo and retry immediately, same key + same
    # body, while the reservation left behind by the crashed call above is
    # still fresh (well within RESERVATION_TTL_S) -> in-flight 409, not a
    # second execution.
    monkeypatch.setattr(agent_runtime, "jobs_repo", real_jobs_repo)
    r2 = env["client"].post("/api/v1/agents/support-bot/responses", json=body, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "idempotency_key_in_flight"


def test_idempotency_stale_reservation_allows_execution(env, monkeypatch):
    """A reservation whose owning request crashed and never fulfilled it is
    only held for `RESERVATION_TTL_S` — once stale, a fresh call under the
    same key must execute normally rather than 409 forever."""
    import time

    import src.repositories.idempotency as idempotency_module
    from src.repositories import idempotency_repo

    calls = []
    _patch_run_one_shot(monkeypatch, answer="fresh execution", calls=calls)

    monkeypatch.setattr(idempotency_module, "RESERVATION_TTL_S", 0)
    reserved = idempotency_repo().reserve("stale-key", "owner1", env["agent_id"], "some-other-hash")
    assert reserved is True
    time.sleep(0.01)

    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "hi"},
        headers={**_auth(env["owner_token"]), "Idempotency-Key": "stale-key"},
    )
    assert r.status_code == 200
    assert r.json()["answer"] == "fresh execution"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{id}
# ---------------------------------------------------------------------------


def test_get_job_owner_scoped_404_for_non_owner(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    job_id = r.json()["job_id"]

    owner_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert owner_get.status_code == 200
    assert owner_get.json()["status"] == "queued"

    other_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["other_token"]))
    assert other_get.status_code == 404


def test_get_job_agent_pat_wrong_agent_returns_404(env, monkeypatch):
    """Defense-in-depth (review carry-over): an owner's agent-B PAT must
    not read a job created against agent A, even though both belong to the
    SAME owner user — mirrors `require_agent_runtime_principal`'s
    `agent_pat_wrong_agent` binding on the responses endpoint itself."""
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    job_id = r.json()["job_id"]

    wrong_token_id = "tok-other-agent-job-read"
    wrong_token = _mint_agent_pat("owner@test.com", "owner1", env["other_agent_id"], wrong_token_id)
    _register_agent_pat_row("owner1", env["other_agent_id"], wrong_token, wrong_token_id)
    wrong_agent_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(wrong_token))
    assert wrong_agent_get.status_code == 404

    right_token_id = "tok-support-bot-job-read"
    right_token = _mint_agent_pat("owner@test.com", "owner1", env["agent_id"], right_token_id)
    _register_agent_pat_row("owner1", env["agent_id"], right_token, right_token_id)
    right_agent_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(right_token))
    assert right_agent_get.status_code == 200


def test_get_job_unknown_id_returns_404(env):
    r = env["client"].get("/api/v1/jobs/does-not-exist", headers=_auth(env["owner_token"]))
    assert r.status_code == 404


def test_get_job_status_mapping(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    job_id = r.json()["job_id"]

    from src.repositories import jobs_repo

    claimed = jobs_repo().claim_next(kinds=["agent_response"], worker_id="w1")
    assert claimed["id"] == job_id
    running_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert running_get.json()["status"] == "in_progress"

    jobs_repo().complete(job_id, "w1", claimed["lease_token"], {"answer": "done answer", "session_id": "s1"})
    done_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert done_get.json()["status"] == "completed"
    assert done_get.json()["result"]["answer"] == "done answer"


def test_get_job_concurrency_cap_error_is_structured(env, monkeypatch):
    """A job that failed via the `ConcurrencyCapHit` path
    (`app/worker/kinds.py::_run_agent_response`) surfaces a structured
    `{"code": "concurrency_cap", ...}` error, not a raw string — see
    `app/api/agent_runtime.py::_serialize_error`."""
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    job_id = r.json()["job_id"]

    from app.worker.kinds import CONCURRENCY_CAP_ERROR_PREFIX
    from src.repositories import jobs_repo

    claimed = jobs_repo().claim_next(kinds=["agent_response"], worker_id="w1")
    assert claimed["id"] == job_id
    jobs_repo().fail(
        job_id, "w1", claimed["lease_token"], f"{CONCURRENCY_CAP_ERROR_PREFIX}cap hit", retry_in_seconds=None
    )

    failed_get = env["client"].get(f"/api/v1/jobs/{job_id}", headers=_auth(env["owner_token"]))
    assert failed_get.json()["status"] == "failed"
    assert failed_get.json()["error"] == {"code": "concurrency_cap", "message": "cap hit"}
