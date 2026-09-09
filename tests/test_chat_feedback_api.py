"""``POST /api/chat/sessions/{chat_id}/feedback`` — the DuckDB-backed side
(LLM observability design §3.5).

``chat_message_feedback`` is a PG-only table (A3 ratchet): the feedback repo
is resolved as a FastAPI dependency (``app.api.chat._feedback_repo``), so a
DuckDB-backed instance answers the typed ``501 requires_postgres_backend``
before the request body is even validated. This file therefore proves the
gate (owner/live-participant, 404 for a stranger, never 403), the 501
fail-clean shape, body validation, and — with the repo dependency overridden
by a fake — the upsert call shape, the audit row (verdict only, never the
comment), the structured log record, and the span event when export is on.
The real upsert semantics on Postgres live in
``tests/db_pg/test_chat_feedback_pg.py``.

Built exactly like ``tests/test_chat_api.py::_make_app`` (imported, not
duplicated) — a minimal FastAPI app with only the chat router, so this suite
runs without a running DuckDB system.db.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import Depends
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.chat.turn_context import TurnRecord, publish_turn
from src.repositories import RequiresPostgresBackend
from tests.test_chat_api import TEST_USER, _make_app
from tests.test_otel_export import otel_exporter  # noqa: F401 - the in-memory-exporter fixture

BOB = {"id": "user2", "email": "bob@test.com", "is_admin": False}


def _feedback_client(**kwargs) -> TestClient:
    """``_make_app`` plus the ``RequiresPostgresBackend`` -> 501 translation
    ``app/main.py`` normally registers globally — the minimal test app never
    goes through ``create_app()``, so this suite registers it itself, same
    shape as the real handler (see CLAUDE.md -> "Dual-backend discipline")."""
    app = _make_app(**kwargs)

    @app.exception_handler(RequiresPostgresBackend)
    async def _pg_required(request, exc):
        return JSONResponse(
            status_code=501,
            content={"detail": str(exc), "error": "requires_postgres_backend", "feature": exc.feature},
        )

    return TestClient(app)


class _RecordingRepo:
    """A fake ``chat_message_feedback_repo()`` — records every ``upsert``
    call and answers with a plausible row, so the endpoint's call shape and
    response projection are provable without a real Postgres engine."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": "fb_1",
            "turn_id": kwargs["turn_id"],
            "verdict": kwargs["verdict"],
            "comment": kwargs["comment"],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }


@pytest.fixture
def client() -> TestClient:
    return _feedback_client(chat_enabled=True)


def _create_session(client: TestClient) -> str:
    return client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]


@pytest.fixture
def chat_id_with_a_published_turn(client: TestClient) -> str:
    """A session whose ``chat:turn:{session_id}`` record is already
    published — what a real turn leaves behind (§3.2) for the feedback
    endpoint to parent its span under."""
    chat_id = _create_session(client)
    publish_turn(
        chat_id,
        TurnRecord(
            turn_id="t1",
            trace_id="c" * 32,
            span_id="d" * 16,
            started_at="2026-01-01T00:00:00+00:00",
            user_id=TEST_USER["id"],
            agent_id=None,
            surface="web",
            workload="chat",
        ),
    )
    return chat_id


# ---------------------------------------------------------------------------
# Fail-clean on the frozen DuckDB backend
# ---------------------------------------------------------------------------


def test_feedback_is_501_on_duckdb(client: TestClient):
    chat_id = _create_session(client)
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_feedback_501_precedes_body_validation(client: TestClient):
    """The repo dependency resolves before the body validator runs — a bad
    verdict on a DuckDB-backed instance still answers 501, not 422, so the
    caller learns the REAL reason it cannot rate rather than a red herring."""
    chat_id = _create_session(client)
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "not-a-verdict"})
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# With the repo overridden: upsert shape, audit, validation, gate
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(client: TestClient) -> _RecordingRepo:
    from app.api.chat import _feedback_repo

    fake = _RecordingRepo()
    client.app.dependency_overrides[_feedback_repo] = lambda: fake
    return fake


def test_feedback_upserts_and_audits(client: TestClient, fake_repo: _RecordingRepo, monkeypatch):
    audits: list[dict] = []
    monkeypatch.setattr("app.api.chat.write_audit", lambda **kw: audits.append(kw))
    chat_id = _create_session(client)

    r = client.post(
        f"/api/chat/sessions/{chat_id}/feedback",
        json={"turn_id": "t1", "verdict": "down", "comment": "wrong number"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "down"
    assert r.json()["comment"] == "wrong number"

    assert fake_repo.calls == [
        {
            "session_id": chat_id,
            "turn_id": "t1",
            "user_id": TEST_USER["id"],
            "verdict": "down",
            "comment": "wrong number",
            "message_id": None,
        }
    ]

    (row,) = audits
    assert row["action"] == "chat.feedback"
    assert row["details"] == {"session_id": chat_id, "turn_id": "t1", "verdict": "down"}
    assert "wrong number" not in json.dumps(row)


def test_feedback_upsert_without_a_comment(client: TestClient, fake_repo: _RecordingRepo):
    chat_id = _create_session(client)
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 200, r.text
    assert r.json()["comment"] is None
    assert fake_repo.calls[0]["comment"] is None


def test_feedback_strips_a_whitespace_only_comment_to_none(client: TestClient, fake_repo: _RecordingRepo):
    chat_id = _create_session(client)
    r = client.post(
        f"/api/chat/sessions/{chat_id}/feedback",
        json={"turn_id": "t1", "verdict": "up", "comment": "   "},
    )
    assert r.status_code == 200, r.text
    assert r.json()["comment"] is None
    assert fake_repo.calls[0]["comment"] is None


def test_feedback_validates_the_body(client: TestClient, fake_repo: _RecordingRepo):
    chat_id = _create_session(client)
    assert (
        client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "meh"}).status_code
        == 422
    )
    assert (
        client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "", "verdict": "up"}).status_code == 422
    )
    assert (
        client.post(
            f"/api/chat/sessions/{chat_id}/feedback",
            json={"turn_id": "t1", "verdict": "up", "comment": "x" * 2001},
        ).status_code
        == 422
    )
    assert fake_repo.calls == []


def test_feedback_404_for_an_unknown_session(client: TestClient, fake_repo: _RecordingRepo):
    r = client.post("/api/chat/sessions/chat_nonexistent/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 404


def test_feedback_404_for_a_stranger_never_403(client: TestClient, fake_repo: _RecordingRepo):
    """Auth-scoped like the sibling session routes — 404, never 403, so the
    endpoint cannot be used to probe for other users' session ids."""
    chat_id = _create_session(client)
    app = client.app
    app.dependency_overrides[get_current_user] = lambda: BOB
    try:
        r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
    assert fake_repo.calls == []


def test_feedback_ok_for_a_live_participant(client: TestClient, fake_repo: _RecordingRepo):
    """A co-drive peer who joined the session — not its owner — can still
    rate the turn they watched happen."""
    chat_id = _create_session(client)
    repo = client.app.state.chat_repo
    repo.add_session_participant(session_id=chat_id, user_email=BOB["email"], user_id=BOB["id"], role="collaborator")

    app = client.app
    app.dependency_overrides[get_current_user] = lambda: BOB
    try:
        r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides[get_current_user] = lambda: TEST_USER
    assert fake_repo.calls[0]["user_id"] == BOB["id"]


def test_feedback_rejects_a_restricted_principal(client: TestClient, fake_repo: _RecordingRepo):
    """A co-session/agent-session caller has no single identity to rate an
    answer under — 403, matching the sibling routes' restricted-principal
    guard, before the handler ever subscripts `user`. The client fixture
    builds a fresh app per test, so there is nothing to restore afterward."""
    from app.api.chat import require_chat_access
    from app.auth.session_principal import SessionPrincipal

    chat_id = _create_session(client)

    async def _restricted(_user: dict = Depends(get_current_user)):
        return SessionPrincipal(
            session_id="co_1",
            participant_user_ids=[TEST_USER["id"]],
            participant_emails=[TEST_USER["email"]],
            intersection={},
        )

    client.app.dependency_overrides[require_chat_access] = _restricted
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 403
    assert fake_repo.calls == []


# ---------------------------------------------------------------------------
# Log record + span event
# ---------------------------------------------------------------------------


def test_feedback_emits_a_log_record(client: TestClient, fake_repo: _RecordingRepo, caplog):
    chat_id = _create_session(client)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 200, r.text
    rec = next(rec for rec in caplog.records if getattr(rec, "event", None) == "chat_feedback")
    assert rec.session_id == chat_id
    assert rec.turn_id == "t1"
    assert rec.verdict == "up"
    assert rec.has_comment is False


def test_feedback_span_is_parented_on_the_turn(
    client: TestClient,
    fake_repo: _RecordingRepo,
    otel_exporter,  # noqa: F811
    chat_id_with_a_published_turn,
):
    r = client.post(
        f"/api/chat/sessions/{chat_id_with_a_published_turn}/feedback",
        json={"turn_id": "t1", "verdict": "up"},
    )
    assert r.status_code == 200, r.text

    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "agnes.chat.feedback"]
    assert format(span.context.trace_id, "032x") == "c" * 32
    assert format(span.parent.span_id, "016x") == "d" * 16
    (event,) = span.events
    assert event.name == "agnes.feedback"
    assert dict(event.attributes)["agnes.verdict"] == "up"


def test_feedback_span_degrades_to_a_root_span_without_a_published_turn(
    client: TestClient,
    fake_repo: _RecordingRepo,
    otel_exporter,  # noqa: F811
):
    """No ``chat:turn:{session_id}`` record (coordination unavailable, or the
    turn simply never published one) — the span still records, just without
    a parent, exactly the degrade-not-fail contract §3.2 promises."""
    chat_id = _create_session(client)
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 200, r.text

    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "agnes.chat.feedback"]
    assert span.parent is None
