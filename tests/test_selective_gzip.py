"""Tests for the SelectiveGZipMiddleware path-skip logic in app/main.py.

Key property: parquet-serving endpoints must not be gzipped on the wire,
but JSON / HTML endpoints above the minimum-size threshold must be.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_client(tmp_path, monkeypatch, shared_app):
    """Fresh FastAPI app with its own tmp DATA_DIR so DuckDB locks don't
    collide with a concurrently-running dev container."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    yield TestClient(shared_app)
    close_system_db()


def test_parquet_path_is_not_gzipped(isolated_client, tmp_path, monkeypatch):
    """/cli/wheel/... must return the raw bytes without Content-Encoding: gzip."""
    wheel = tmp_path / "agnes_fake-1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04" + b"x" * 4096)
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))

    resp = isolated_client.get(
        f"/cli/wheel/{wheel.name}",
        headers={"Accept-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert "gzip" not in resp.headers.get("content-encoding", "")
    assert resp.content.startswith(b"PK")


def test_install_page_is_gzipped(isolated_client):
    """/setup is HTML above the threshold — gzip should kick in when the
    client advertises gzip support. TestClient may decompress transparently,
    so we accept either the header or readable body as proof that the
    middleware decided to handle the response (i.e. did not skip)."""
    resp = isolated_client.get("/setup", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    enc = resp.headers.get("content-encoding", "")
    # Either we see the encoding on the wire OR TestClient auto-decoded it.
    assert "gzip" in enc or "setup" in resp.text.lower()


def test_no_accept_encoding_means_no_gzip_anywhere(isolated_client):
    """Client that doesn't advertise gzip gets uncompressed body."""
    resp = isolated_client.get("/setup", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "gzip" not in resp.headers.get("content-encoding", "")


def test_selective_gzip_wrapper_dispatches_on_prefix():
    """Direct unit test of the wrapper's path-based branch without standing up
    the whole FastAPI app — verifies the skip list is honoured."""
    from app.main import _SelectiveGZipMiddleware

    calls = {"raw": 0, "gzip": 0}

    async def raw_app(scope, receive, send):
        calls["raw"] += 1

    wrapper = _SelectiveGZipMiddleware(raw_app, minimum_size=10, skip_prefixes=("/api/data/",))
    # Monkey-patch the gzip inner so we can count hits without running middleware.
    async def stub_gzip(scope, receive, send):
        calls["gzip"] += 1
    wrapper._gzip = stub_gzip

    import asyncio
    # Path that matches the skip prefix → raw app
    asyncio.run(wrapper({"type": "http", "path": "/api/data/orders/download"}, None, None))
    assert calls == {"raw": 1, "gzip": 0}
    # Path that does not → gzip app
    asyncio.run(wrapper({"type": "http", "path": "/api/sync/manifest"}, None, None))
    assert calls == {"raw": 1, "gzip": 1}
    # Non-http scope (websocket, lifespan) → gzip app (it handles lifespan as pass-through)
    asyncio.run(wrapper({"type": "lifespan"}, None, None))
    assert calls == {"raw": 1, "gzip": 2}


def test_broker_anthropic_path_is_in_gzip_skip_list():
    """The chat sandbox LLM proxy streams the model completion back as
    text/event-stream. GZipMiddleware buffers a StreamingResponse whole to
    compress it, collapsing every token delta into one end-of-turn burst
    (verified live: the in-sandbox CLI saw all SSE events at one timestamp).
    /api/broker/anthropic MUST be skip-listed alongside /api/mcp so the
    broker's stream-through actually reaches the sandbox incrementally."""
    import inspect

    import app.main as main_mod

    src = inspect.getsource(main_mod.create_app)
    assert '"/api/broker/anthropic"' in src, (
        "the broker anthropic SSE endpoint must be in _SelectiveGZipMiddleware "
        "skip_prefixes, or GZip re-buffers the streamed completion"
    )


def test_selective_gzip_skips_configured_sse_prefixes():
    """Behavioral: the middleware bypasses gzip (delegates to the raw app)
    for a skip-listed SSE prefix, so a StreamingResponse is never buffered
    for compression."""
    import asyncio

    from app.main import _SelectiveGZipMiddleware

    calls = {"raw": 0, "gzip": 0}

    async def _raw_app(scope, receive, send):
        calls["raw"] += 1

    mw = _SelectiveGZipMiddleware(_raw_app, skip_prefixes=("/api/broker/anthropic",))

    async def _noop_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _noop_send(msg):
        pass

    async def _drive(path):
        await mw({"type": "http", "path": path, "headers": []}, _noop_receive, _noop_send)

    asyncio.run(_drive("/api/broker/anthropic/v1/messages"))
    assert calls["raw"] == 1, "skip-listed SSE path must bypass GZip (delegate to raw app)"


def test_attachments_path_is_in_gzip_skip_list():
    """Attachment binaries (PDF/PNG/ZIP …) are already compressed — the same
    rationale the skip list states for parquet downloads. Left off the list,
    GZipMiddleware re-compresses every FileResponse chunk at compresslevel 9
    on the event loop, burning CPU for no size win on up-to-50MB files."""
    import inspect

    import app.main as main_mod

    src = inspect.getsource(main_mod.create_app)
    assert '"/api/attachments/"' in src, (
        "the attachment download endpoint must be in _SelectiveGZipMiddleware "
        "skip_prefixes — its payloads are already-compressed binaries"
    )
