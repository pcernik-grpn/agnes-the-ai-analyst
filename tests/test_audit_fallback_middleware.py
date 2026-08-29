"""AuditFallbackMiddleware (F1 — audit-full-coverage plan, Task 2)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient as StarletteTestClient

from app.middleware.audit_fallback import AuditFallbackMiddleware
from src.audit_context import set_audit_identity


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_unannotated_mutation_gets_generic_row(tmp_path, monkeypatch, seeded_app):
    """POST /api/stack/subscribe writes no audit row of its own (only
    usage_events telemetry) — the fallback middleware must cover it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": "posture-test-pkg"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="http.request", limit=10)
    matches = [r for r in rows if r["resource"] == "POST /api/stack/subscribe"]
    assert matches, "fallback middleware did not write a generic http.request row"
    assert matches[0]["user_id"] == "admin1"


def test_audited_route_gets_no_duplicate(tmp_path, monkeypatch, seeded_app):
    """POST /api/sync/trigger already writes its own `sync.trigger` row —
    the fallback middleware must not ALSO write a generic http.request row
    for it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.post("/api/sync/trigger", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code in (200, 409)

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="http.request", limit=10)
    matches = [r for r in rows if r["resource"] == "POST /api/sync/trigger"]
    assert not matches, "fallback middleware duplicated an already-audited route"

    trigger_rows, _ = audit_repo().query(action="sync.trigger", limit=10)
    assert trigger_rows, "the route's own audit row is missing"


def test_unauthenticated_request_writes_nothing(tmp_path, monkeypatch, seeded_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": "posture-test-pkg"},
    )
    assert resp.status_code == 401

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="http.request", limit=10)
    matches = [r for r in rows if r["resource"] == "POST /api/stack/subscribe"]
    assert not matches, "unauthenticated request must not be attributed an audit row"


def test_exempt_route_never_gets_a_generic_row(e2e_env):
    """A route declared `exempt:<reason>` in POSTURE must never get the
    generic row, even when authenticated and even though its handler wrote
    nothing — isolated at the middleware level (no CSRF/debug-flag
    dependencies from the real route) against the exact key POSTURE uses:
    `POST /me/profile/refetch-groups`."""

    async def handler(request):
        set_audit_identity("admin1", "admin@test.com")
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/me/profile/refetch-groups", handler, methods=["POST"])])
    app.add_middleware(AuditFallbackMiddleware)

    client = StarletteTestClient(app)
    resp = client.post("/me/profile/refetch-groups")
    assert resp.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="http.request", limit=10)
    matches = [r for r in rows if r["resource"] == "POST /me/profile/refetch-groups"]
    assert not matches, "exempt route must never get a generic fallback row"
