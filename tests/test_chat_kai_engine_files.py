"""Tests for the kai-agent engine file proxy (#1611, engine side of the
chat session-files routes).

Drives the real routes in ``app.api.chat_session_files`` with the engine
transport swapped for ``httpx.MockTransport`` — no sockets, no real engine.
The end-to-end pairing against the stub engine lives in
``tests/test_kai_engine_stub_files.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_USER = {"id": "user_engine_files", "email": "engine-files@test.com", "is_admin": False}
CHAT_ID = "0b8a1c9e-7d2f-4e5a-9c3b-1f2e3d4c5b6a"

KAI_CONFIG = SimpleNamespace(provider="kai-agent", kai_agent_url="http://engine.test")


class _FakeChatRepo:
    def __init__(self, sessions: dict[str, str]):
        self._sessions = sessions

    def get_session(self, chat_id: str):
        email = self._sessions.get(chat_id)
        if email is None:
            return None
        return SimpleNamespace(id=chat_id, user_email=email)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the engine JWT mint (a sync repo write) with a recorder."""
    calls: dict = {"args": []}

    def _fake_mint(user_email: str, session_id: str) -> tuple[str, int]:
        calls["args"].append((user_email, session_id))
        return "test-engine-jwt", 2_000_000_000

    from app.api import kai

    monkeypatch.setattr(kai, "mint_engine_session_token", _fake_mint)
    return calls


def _make_client(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> TestClient:
    os.environ["DATA_DIR"] = str(data_dir)

    import app.api.chat_session_files as mod

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(handler))

    app = FastAPI()
    app.include_router(mod.router)
    app.state.chat_repo = _FakeChatRepo({CHAT_ID: TEST_USER["email"]})
    app.state.chat_config = KAI_CONFIG
    app.dependency_overrides[mod.require_chat_access] = lambda: TEST_USER
    return TestClient(app)


def _entries(*items: dict) -> httpx.Response:
    return httpx.Response(200, json={"entries": list(items)})


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_listing_proxied_from_engine(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == f"/api/chat/{CHAT_ID}/sandbox/files"
        assert request.headers["authorization"] == "Bearer test-engine-jwt"
        if request.url.params.get("path") == "outputs":
            return _entries({"name": "report.docx", "path": "outputs/report.docx", "type": "file", "size": 10})
        return _entries(
            {"name": "outputs", "path": "outputs", "type": "dir"},
            {"name": "chart.png", "path": "chart.png", "type": "file", "size": 3},
        )

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "engine"
    assert body["supported"] is True
    assert {(f["path"], f["size_bytes"]) for f in body["files"]} == {
        ("chart.png", 3),
        ("outputs/report.docx", 10),
    }
    assert all(f["modified_at"] is None for f in body["files"])
    assert minted["args"] == [(TEST_USER["email"], CHAT_ID)]
    assert len(seen) == 2  # root + one subdirectory


def test_listing_skips_the_workspace_template(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    """The engine serves this instance's own workspace into its sandbox, and
    its browser filters only DOT-directories — so `.claude` is hidden there
    but `scaffolds/` and `CLAUDE.md` are not. Observed live: the drawer listed
    `scaffolds/nodejs-dashboard/{package.json,index.html,…}` and the user's
    actual document was nowhere in it.

    The host walk already excluded these; the engine path did not, which is
    why the fix for the host surface did not change what that instance showed.
    A scaffolds/ subdirectory must not even be REQUESTED — walking it is what
    produced the rows."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params.get("path") or "")
        if request.url.params.get("path") == "outputs":
            return _entries({"name": "report.docx", "path": "outputs/report.docx", "type": "file", "size": 10})
        if request.url.params.get("path") == "scaffolds":
            raise AssertionError("walked into the template scaffold tree")
        return _entries(
            {"name": "outputs", "path": "outputs", "type": "dir"},
            {"name": "scaffolds", "path": "scaffolds", "type": "dir"},
            {"name": "CLAUDE.md", "path": "CLAUDE.md", "type": "file", "size": 25000},
            {"name": "snapshots", "path": "snapshots", "type": "dir"},
        )

    client = _make_client(data_dir, monkeypatch, handler)
    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert [f["path"] for f in body["files"]] == ["outputs/report.docx"]
    assert "scaffolds" not in requested and "snapshots" not in requested


def test_listing_engine_404_degrades_to_unsupported(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    """Unknown chat OR an engine predating the sandbox-files routes: both
    404 the root listing, both must read as supported=false, never a 5xx."""
    client = _make_client(data_dir, monkeypatch, lambda request: httpx.Response(404, json={"error": {}}))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"files": [], "truncated": False, "source": "engine", "supported": False}


@pytest.mark.parametrize("body", [b"", b"not json", b"<html>gateway</html>"])
def test_listing_engine_malformed_body_maps_to_502(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict, body: bytes
) -> None:
    """A 200 whose body is empty or not JSON is an engine fault, not a
    caller error — 502, never an unhandled 500 (Devin review on #1628)."""
    client = _make_client(data_dir, monkeypatch, lambda request: httpx.Response(200, content=body))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 502
    assert resp.json()["detail"]["kind"] == "engine_files_unavailable"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_listing_engine_5xx_maps_to_502(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict, status: int
) -> None:
    client = _make_client(data_dir, monkeypatch, lambda request: httpx.Response(status))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 502
    assert resp.json()["detail"]["kind"] == "engine_files_unavailable"


def test_listing_engine_unreachable_maps_to_502(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 502


@pytest.mark.parametrize("status", [401, 403])
def test_listing_engine_auth_rejection_is_502_not_empty(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    minted: dict,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A JWT-contract misconfiguration must surface as an error, not as
    "this conversation has no files"."""
    client = _make_client(data_dir, monkeypatch, lambda request: httpx.Response(status))
    with caplog.at_level("ERROR"):
        resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 502
    assert any("rejected the host session JWT" in r.message for r in caplog.records)


def test_engine_listing_paths_revalidated(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    """Entries whose paths fail the same validation the download route
    enforces are dropped, not served."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _entries(
            {"name": "passwd", "path": "../../etc/passwd", "type": "file", "size": 1},
            {"name": "ok.txt", "path": "ok.txt", "type": "file", "size": 2},
            {"name": "abs", "path": "/etc/shadow", "type": "file", "size": 3},
        )

    client = _make_client(data_dir, monkeypatch, handler)
    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert [f["path"] for f in body["files"]] == ["ok.txt"]


def test_engine_listing_bounded_and_flagged_truncated(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    """A pathological (cyclic/enormous) engine tree stops at the request cap
    and reports truncated instead of walking forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        d = request.url.params.get("path", "")
        nxt = f"{d}/deeper" if d else "deeper"
        return _entries(
            {"name": "deeper", "path": nxt, "type": "dir"},
            {"name": "f.txt", "path": f"{nxt}.f.txt", "type": "file", "size": 1},
        )

    client = _make_client(data_dir, monkeypatch, handler)
    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert body["truncated"] is True
    assert body["supported"] is True


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_handler(files: dict[str, bytes]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/chat/{CHAT_ID}/sandbox/file/download"
        blob = files.get(request.url.params.get("path", ""))
        if blob is None:
            return httpx.Response(404, json={"error": {}})
        # The engine's own headers are advisory; the proxy must ignore them.
        return httpx.Response(200, content=blob, headers={"content-type": "text/html"})

    return handler


def test_download_streams_with_host_identical_headers(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    client = _make_client(data_dir, monkeypatch, _download_handler({"outputs/deck.pptx": b"pptx-bytes"}))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/deck.pptx"})
    assert resp.status_code == 200
    assert resp.content == b"pptx-bytes"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"] == 'attachment; filename="deck.pptx"'
    assert resp.headers["cache-control"] == "private, no-store"


def test_download_active_content_pinned_to_octet_stream(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    """page.html must come back non-renderable even though the engine said
    text/html — the media type is derived from the filename and pinned."""
    client = _make_client(data_dir, monkeypatch, _download_handler({"page.html": b"<script>alert(1)</script>"}))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "page.html"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")


def test_download_unknown_path_404(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    client = _make_client(data_dir, monkeypatch, _download_handler({}))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "nope.txt"})
    assert resp.status_code == 404


def test_download_traversal_rejected_before_engine_call(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("engine must not be called for an invalid path")

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "../secret"})
    assert resp.status_code == 400
    assert minted["args"] == []


def test_download_content_length_over_cap_413(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    import app.api.chat_session_files as mod

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-length": str(mod._MAX_PROXY_DOWNLOAD_BYTES + 1)})

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "huge.bin"})
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Save to Library
# ---------------------------------------------------------------------------


def test_save_artefact_proxies_engine_bytes(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    import app.api.chat_session_files as mod  # noqa: F401 — patched below via corpus_ingest

    captured: dict = {}

    def _fake_create(*, owner_id: str, filename: str, data: bytes):
        captured.update(owner_id=owner_id, filename=filename, data=data)
        return {"file_id": "f1", "collection": {"slug": "sess-notes"}}

    from app import corpus_ingest

    monkeypatch.setattr(corpus_ingest, "create_single_file_artefact", _fake_create)
    monkeypatch.setattr("src.ingest.runner.ingest_file", lambda file_id: None)

    client = _make_client(data_dir, monkeypatch, _download_handler({"outputs/notes.md": b"# engine notes"}))
    resp = client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "outputs/notes.md"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"artefact_slug": "sess-notes", "library_url": "/library/sess-notes"}
    assert captured == {"owner_id": TEST_USER["id"], "filename": "notes.md", "data": b"# engine notes"}


def test_save_artefact_unknown_engine_path_404(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    client = _make_client(data_dir, monkeypatch, _download_handler({}))
    resp = client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "missing.md"})
    assert resp.status_code == 404


def test_save_artefact_bad_extension_415_without_engine_call(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("engine must not be called for an unsupported type")

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "blob.xyz"})
    assert resp.status_code == 415
    assert minted["args"] == []


def test_save_artefact_over_cap_413(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    from src.corpus_allowlist import MAX_UPLOAD_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_UPLOAD_BYTES + 1))

    client = _make_client(data_dir, monkeypatch, handler)
    resp = client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "big.md"})
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Misconfiguration
# ---------------------------------------------------------------------------


def test_kai_provider_without_engine_url_is_502(data_dir: Path, monkeypatch: pytest.MonkeyPatch, minted: dict) -> None:
    os.environ["DATA_DIR"] = str(data_dir)
    import app.api.chat_session_files as mod

    app = FastAPI()
    app.include_router(mod.router)
    app.state.chat_repo = _FakeChatRepo({CHAT_ID: TEST_USER["email"]})
    app.state.chat_config = SimpleNamespace(provider="kai-agent", kai_agent_url="")
    app.dependency_overrides[mod.require_chat_access] = lambda: TEST_USER
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 502


def test_source_guard_engine_calls_only_after_validation() -> None:
    """Source-text guard, engine edition: the download/save bodies validate
    the path before any engine helper call, and the mint only ever runs
    off-loop (asyncio.to_thread)."""
    src = (Path(__file__).parent.parent / "app" / "api" / "chat_session_files.py").read_text()
    assert "asyncio.to_thread(mint_engine_session_token" in src
    for fn in ("async def download_session_file", "async def save_session_file_as_artefact"):
        body = src[src.index(fn) :]
        nxt = body.find("\nasync def")
        body = body[: nxt if nxt != -1 else len(body)]
        validate_at = body.index("_validate_rel_path")
        engine_at = body.index("_files_source")
        assert validate_at < engine_at, f"{fn}: path validation must precede the engine branch"


@pytest.mark.anyio
async def test_mid_stream_cap_abort_closes_the_engine_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap hit mid-stream raises while Starlette's cleanup BackgroundTask
    never runs (it only fires after a normal finish) — the iterator itself
    must close the upstream response + client, or the httpx connection leaks
    (Devin review on #1628)."""
    from app.chat.kai_engine_files import EngineFileTooLarge, open_engine_download

    async def _body():
        yield b"x" * 70_000
        yield b"x" * 70_000

    # Streamed body => no Content-Length, so the preflight cannot refuse it.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=_body()))
    opened = await open_engine_download(
        base_url="http://engine.test",
        chat_id=CHAT_ID,
        path="huge.bin",
        token="jwt",
        max_bytes=100_000,
        transport=transport,
    )
    assert opened is not None
    iterator, handle = opened
    with pytest.raises(EngineFileTooLarge):
        async for _ in iterator:
            pass
    assert handle._client.is_closed
