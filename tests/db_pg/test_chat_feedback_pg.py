"""``POST /api/chat/sessions/{chat_id}/feedback`` on Postgres (LLM
observability design §3.5).

``chat_message_feedback`` is a PG-only table (A3 ratchet) — there is no
DuckDB sibling to parametrize against, so unlike a frozen dual-backend pair
this suite runs against Postgres only; the DuckDB side's contract (the typed
501, the gate, body validation) is pinned in ``tests/test_chat_feedback_api.py``.

Built like ``tests/db_pg/test_chat_pg.py::test_session_responses_carry_agent_id_on_postgres``
(itself modeled on ``tests/test_chat_api.py::_make_app``): a real router, a
PG-backed ``ChatRepository`` (wired by setting ``DATABASE_URL`` BEFORE
construction — that is the branch its constructor checks), a no-op sandbox
provider, and the access gate delegated to ``get_current_user`` so this
proves the endpoint's Postgres behaviour rather than re-testing RBAC.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.chat import require_chat_access
from app.api.chat import router as chat_router
from app.auth.dependencies import get_current_user
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.turn_context import TurnRecord, publish_turn
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from src.duckdb_conn import _open_duckdb
from tests.test_otel_export import otel_exporter  # noqa: F401 - the in-memory-exporter fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

USER = {"id": "pgfeedback1", "email": "pg-feedback@test.com", "is_admin": False}


@pytest.fixture
def engine(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return pg_engine


@pytest.fixture
def client(engine, monkeypatch) -> TestClient:
    # `ChatRepository` only wires its `_sessions_pg`/`_participants_pg`
    # delegates when `use_pg()` is true AT CONSTRUCTION — set the env first.
    monkeypatch.setenv("DATABASE_URL", str(engine.url))

    repo = ChatRepository(_open_duckdb(":memory:"))
    assert repo._sessions_pg is not None, "the repository fell back to DuckDB — this test proves nothing about it"

    provider = MagicMock()
    provider.spawn = AsyncMock()
    workdirs = MagicMock(spec=WorkdirManager)
    workdirs.ensure_user_workdir = MagicMock()
    workdirs.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    app = FastAPI()
    app.include_router(chat_router)
    app.state.chat_repo = repo
    app.state.chat_manager = ChatManager(
        provider=provider,
        workdir_mgr=workdirs,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=3),
    )

    async def _granted(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[require_chat_access] = _granted
    return TestClient(app)


def _create_session(client: TestClient) -> str:
    r = client.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_two_posts_for_one_turn_upsert_a_single_row_and_the_second_verdict_wins(client: TestClient):
    chat_id = _create_session(client)

    first = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = client.post(
        f"/api/chat/sessions/{chat_id}/feedback",
        json={"turn_id": "t1", "verdict": "down", "comment": "actually wrong"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_id
    assert second.json()["verdict"] == "down"
    assert second.json()["comment"] == "actually wrong"

    from src.repositories import chat_message_feedback_repo

    rows = chat_message_feedback_repo().list_feedback()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "down"
    assert rows[0]["comment"] == "actually wrong"


def test_two_different_users_on_the_same_turn_get_two_rows(client: TestClient):
    """The unique key is `(turn_id, user_id)`, not `turn_id` alone — two
    people rating the same answer must not clobber each other."""
    chat_id = _create_session(client)

    from src.repositories import chat_message_feedback_repo, users_repo

    users_repo().create(id="pgfeedback2", email="pg-feedback-2@test.com", name="Second")
    # Not the session's owner, so it must be a live participant to rate at all.
    client.app.state.chat_repo.add_session_participant(
        session_id=chat_id, user_email="pg-feedback-2@test.com", user_id="pgfeedback2", role="collaborator"
    )

    first = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert first.status_code == 200, first.text
    client.app.dependency_overrides[get_current_user] = lambda: {
        "id": "pgfeedback2",
        "email": "pg-feedback-2@test.com",
        "is_admin": False,
    }
    second = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "down"})
    assert second.status_code == 200, second.text

    rows = chat_message_feedback_repo().list_feedback()
    assert {r["user_id"] for r in rows} == {USER["id"], "pgfeedback2"}
    assert {r["verdict"] for r in rows} == {"up", "down"}


def test_feedback_writes_an_audit_row_without_the_comment(client: TestClient):
    chat_id = _create_session(client)

    r = client.post(
        f"/api/chat/sessions/{chat_id}/feedback",
        json={"turn_id": "t1", "verdict": "down", "comment": "the number is wrong"},
    )
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _cursor = audit_repo().query(action="chat.feedback")
    rows = list(rows)
    assert rows, "the feedback submission left no audit trail"

    raw_params = rows[0]["params"]
    params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    assert params == {"session_id": chat_id, "turn_id": "t1", "verdict": "down"}
    assert "the number is wrong" not in json.dumps(rows[0], default=str)


def test_feedback_emits_a_log_record(client: TestClient, caplog):
    chat_id = _create_session(client)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 200, r.text

    rec = next(rec for rec in caplog.records if getattr(rec, "event", None) == "chat_feedback")
    assert rec.session_id == chat_id
    assert rec.turn_id == "t1"
    assert rec.verdict == "up"
    assert rec.has_comment is False


def test_feedback_emits_a_log_record_with_has_comment_true(client: TestClient, caplog):
    chat_id = _create_session(client)
    with caplog.at_level(logging.INFO):
        r = client.post(
            f"/api/chat/sessions/{chat_id}/feedback",
            json={"turn_id": "t1", "verdict": "down", "comment": "nope"},
        )
    assert r.status_code == 200, r.text
    rec = next(rec for rec in caplog.records if getattr(rec, "event", None) == "chat_feedback")
    assert rec.has_comment is True


def test_feedback_emits_a_span_event_parented_on_the_turn(client: TestClient, otel_exporter):  # noqa: F811
    chat_id = _create_session(client)
    publish_turn(
        chat_id,
        TurnRecord(
            turn_id="t1",
            trace_id="a" * 32,
            span_id="b" * 16,
            started_at="2026-01-01T00:00:00+00:00",
            user_id=USER["id"],
            agent_id=None,
            surface="web",
            workload="chat",
        ),
    )

    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 200, r.text

    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "agnes.chat.feedback"]
    assert format(span.context.trace_id, "032x") == "a" * 32
    assert format(span.parent.span_id, "016x") == "b" * 16
    (event,) = span.events
    assert event.name == "agnes.feedback"
    assert dict(event.attributes)["agnes.verdict"] == "up"
    assert dict(event.attributes)["agnes.has_comment"] is False


def test_feedback_404_for_a_foreign_session(client: TestClient):
    from src.repositories import chat_session_repo

    other = chat_session_repo().create_session(user_email="someone-else@test.com", surface=Surface.WEB)
    r = client.post(f"/api/chat/sessions/{other.id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 404
