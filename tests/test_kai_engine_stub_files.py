"""End-to-end pairing of the chat session-files proxy with the engine stub.

The unit tests (tests/test_chat_kai_engine_files.py) fake the engine with
MockTransport; here the real Agnes routes talk to the real stub app
(services/kai_engine_stub) over httpx.ASGITransport — the stub is the
executable form of the engine wire contract, so a drift on either side
fails here first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_USER = {"id": "user_stub_files", "email": "stub-files@test.com", "is_admin": False}
CHAT_ID = "7f0e6d5c-4b3a-2918-8765-43210fedcba9"
SECRET = "stub-e2e-secret-at-least-32-chars-long"


class _FakeChatRepo:
    def __init__(self, sessions: dict[str, str]):
        self._sessions = sessions

    def get_session(self, chat_id: str):
        email = self._sessions.get(chat_id)
        if email is None:
            return None
        return SimpleNamespace(id=chat_id, user_email=email)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign_jwt(claims: dict) -> str:
    """HS256 signer mirroring app.api.kai._sign_session_jwt's output shape."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    sig = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def _claims(scope_id: str) -> dict:
    now = int(time.time())
    return {
        "sub": TEST_USER["email"],
        "tenant": "default",
        "scope_id": scope_id,
        "downstream_credential": "cred",
        "read_only": False,
        "iss": "agnes",
        "aud": "kai-agent",
        "iat": now,
        "exp": now + 3600,
    }


@pytest.fixture
def stub_env(monkeypatch: pytest.MonkeyPatch):
    """Auth ON with a known secret, clean per-test sandbox file store."""
    from services.kai_engine_stub import api as stub

    monkeypatch.setenv("KAI_STUB_REQUIRE_AUTH", "1")
    monkeypatch.setenv("KAI_HOST_JWT_SECRET", SECRET)
    monkeypatch.delenv("KAI_STUB_FILES_ROUTES", raising=False)
    stub._session_files.clear()
    yield stub
    stub._session_files.clear()


def _agnes_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub) -> TestClient:
    """The real Agnes routes, engine transport bound to the real stub app,
    mint stubbed to a locally signed (but contract-true) session JWT."""
    os.environ["DATA_DIR"] = str(tmp_path / "data")

    import app.api.chat_session_files as mod
    from app.api import kai

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.ASGITransport(app=stub.app))
    monkeypatch.setattr(
        kai,
        "mint_engine_session_token",
        lambda email, chat_id: (_sign_jwt(_claims(chat_id)), int(time.time()) + 3600),
    )

    agnes = FastAPI()
    agnes.include_router(mod.router)
    agnes.state.chat_repo = _FakeChatRepo({CHAT_ID: TEST_USER["email"]})
    agnes.state.chat_config = SimpleNamespace(provider="kai-agent", kai_agent_url="http://kai-agent:3000")
    agnes.dependency_overrides[mod.require_chat_access] = lambda: TEST_USER
    return TestClient(agnes)


def test_listing_and_download_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_env) -> None:
    stub_env._session_files[CHAT_ID] = {
        "outputs/report.docx": b"docx bytes",
        "outputs/deck.pptx": b"pptx bytes",
        "chart.png": b"png bytes",
    }
    client = _agnes_client(tmp_path, monkeypatch, stub_env)

    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert body["source"] == "engine"
    assert body["supported"] is True
    assert {f["path"] for f in body["files"]} == {"outputs/report.docx", "outputs/deck.pptx", "chart.png"}

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/deck.pptx"})
    assert resp.status_code == 200
    assert resp.content == b"pptx bytes"
    assert resp.headers["content-disposition"] == 'attachment; filename="deck.pptx"'
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_stub_enforces_scope_id_binding(stub_env) -> None:
    """A session JWT minted for chat A must not browse chat B's sandbox —
    the stub pins the ownership half of the wire contract the way the
    engine's host-JWT scope binding does."""
    stub_env._session_files[CHAT_ID] = {"a.txt": b"a"}
    stub_client = TestClient(stub_env.app)

    ok = stub_client.get(
        f"/api/chat/{CHAT_ID}/sandbox/files",
        headers={"Authorization": f"Bearer {_sign_jwt(_claims(CHAT_ID))}"},
    )
    assert ok.status_code == 200
    assert [e["name"] for e in ok.json()["entries"]] == ["a.txt"]

    mismatched = stub_client.get(
        f"/api/chat/{CHAT_ID}/sandbox/files",
        headers={"Authorization": f"Bearer {_sign_jwt(_claims('some-other-chat'))}"},
    )
    assert mismatched.status_code == 403


def test_unknown_chat_degrades_to_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_env) -> None:
    """The stub has never seen this chat (no turn ran) → engine 404 → the
    Agnes listing reports supported=false instead of erroring."""
    client = _agnes_client(tmp_path, monkeypatch, stub_env)
    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert body == {"files": [], "truncated": False, "source": "engine", "supported": False}


def test_files_routes_knob_simulates_old_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_env) -> None:
    stub_env._session_files[CHAT_ID] = {"a.txt": b"a"}
    monkeypatch.setenv("KAI_STUB_FILES_ROUTES", "0")
    client = _agnes_client(tmp_path, monkeypatch, stub_env)
    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert body["supported"] is False
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "a.txt"})
    assert resp.status_code == 404


def test_save_artefact_end_to_end_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_env) -> None:
    stub_env._session_files[CHAT_ID] = {"outputs/notes.md": b"# from the engine sandbox"}
    captured: dict = {}

    from app import corpus_ingest

    monkeypatch.setattr(
        corpus_ingest,
        "create_single_file_artefact",
        lambda *, owner_id, filename, data: (
            captured.update(owner_id=owner_id, filename=filename, data=data)
            or {"file_id": "f1", "collection": {"slug": "engine-notes"}}
        ),
    )
    monkeypatch.setattr("src.ingest.runner.ingest_file", lambda file_id: None)

    client = _agnes_client(tmp_path, monkeypatch, stub_env)
    resp = client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "outputs/notes.md"})
    assert resp.status_code == 200, resp.text
    assert captured["data"] == b"# from the engine sandbox"
    assert captured["filename"] == "notes.md"


def test_deliverable_scenario_registers_files(stub_env) -> None:
    """The `deliverable` scenario seeds the chat's sandbox store, so the
    manual two-terminal flow (docs/kai-agent-local-dev.md) has real rows to
    show in the Files overlay."""
    stub_client = TestClient(stub_env.app)
    token = _sign_jwt(_claims(CHAT_ID))
    resp = stub_client.post(
        "/api/chat",
        json={"id": CHAT_ID, "message": {"id": "m1", "role": "user", "parts": [{"text": "deliverable please"}]}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert set(stub_env._session_files[CHAT_ID]) == {"outputs/report.docx", "outputs/deck.pptx"}
    listing = stub_client.get(
        f"/api/chat/{CHAT_ID}/sandbox/files",
        params={"path": "outputs"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert {e["name"] for e in listing.json()["entries"]} == {"report.docx", "deck.pptx"}
