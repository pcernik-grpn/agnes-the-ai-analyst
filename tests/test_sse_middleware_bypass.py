"""Every BaseHTTPMiddleware subclass in the response chain must bypass SSE
paths via the shared ``SSE_BYPASS_PREFIXES`` tuple — BaseHTTPMiddleware
buffers the whole body, which re-collapses a token stream into one burst
(and can crash on Python 3.13). The broker LLM proxy regression: GZip was
skip-listed but the rate-limit middleware still buffered the stream.
"""

from __future__ import annotations

import asyncio

from app.middleware import SSE_BYPASS_PREFIXES


def test_prefixes_cover_mcp_and_broker():
    assert "/api/mcp" in SSE_BYPASS_PREFIXES
    assert "/api/broker/anthropic" in SSE_BYPASS_PREFIXES


def _passes_through_untouched(mw_cls, path: str) -> bool:
    """True if the middleware forwards the request to the bare inner ASGI app
    (bypassing BaseHTTPMiddleware's buffering dispatch)."""
    hit = {}

    async def inner(scope, receive, send):
        hit["inner"] = True

    mw = mw_cls(inner)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": path, "headers": []}
    asyncio.run(mw(scope, receive, send))
    return hit.get("inner", False)


def test_rate_limit_middleware_bypasses_broker_sse():
    from app.auth.rate_limit import SlowAPIMiddleware

    assert _passes_through_untouched(SlowAPIMiddleware, "/api/broker/anthropic")


def test_gzip_skip_list_stays_in_sync():
    """The gzip skip-list in app/main.py mirrors the SSE prefixes — a new SSE
    endpoint added to SSE_BYPASS_PREFIXES must be gzip-skipped too."""
    import inspect

    import app.main as main_mod

    src = inspect.getsource(main_mod)
    for prefix in SSE_BYPASS_PREFIXES:
        assert f'"{prefix}"' in src, f"{prefix} missing from app/main.py gzip skip_prefixes"
