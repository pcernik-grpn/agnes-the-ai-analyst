"""Read and WebSocket routes join the declared-action audit ratchet (Wave 2 —
audit-coverage plan, Task 2).

``READ_POSTURE`` and ``WS_POSTURE`` extend the Task 1 "posture entry IS the
route's declared action" contract to GET and WebSocket routes: every route
must name a cataloged action or ``"exempt:<reason>"``, drawn from the closed
``EXEMPT_REASONS`` vocabulary.
"""

from __future__ import annotations

from src.audit_events import is_cataloged
from src.audit_posture import (
    EXEMPT_REASONS,
    READ_POSTURE,
    READ_SELF_AUDITING,
    WS_POSTURE,
    declared_read_action,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _routes(app, methods):
    out = set()
    for r in app.routes:
        ms = getattr(r, "methods", None)
        if ms is None:  # WebSocket route (or a Mount, same ASGI shape)
            if "WS" in methods:
                out.add(f"WS {r.path}")
            continue
        for m in ms & methods:
            out.add(f"{m} {r.path}")
    return out


def test_every_read_route_declares_posture(shared_app):
    routes = _routes(shared_app, {"GET"})
    undeclared = sorted(routes - READ_POSTURE.keys())
    stale = sorted(READ_POSTURE.keys() - routes)
    assert not undeclared, (
        "New read routes must declare audit posture in src/audit_posture.py "
        f"(a cataloged action, or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune READ_POSTURE, routes gone: {stale}"


def test_every_ws_route_declares_posture(shared_app):
    routes = _routes(shared_app, {"WS"})
    undeclared = sorted(routes - WS_POSTURE.keys())
    stale = sorted(WS_POSTURE.keys() - routes)
    assert not undeclared, f"New WS/mount routes must declare posture: {undeclared}"
    assert not stale, f"Prune WS_POSTURE, routes gone: {stale}"


def test_exempt_reasons_come_from_the_closed_vocabulary():
    for source in (READ_POSTURE, WS_POSTURE):
        for key, v in source.items():
            if v.startswith("exempt:"):
                assert v.split(":", 1)[1] in EXEMPT_REASONS, (key, v)
            else:
                assert is_cataloged(v), (key, v)


def test_sensitive_reads_are_not_exempt():
    """The categories the policy says must always be audited.

    ``/cli/download`` is the one deliberate exception: it serves the CLI's own
    installer binary (a build artifact, same non-sensitive bucket as
    ``/cli/install.sh``/``/cli/wheel/{wheel_name}``), never a customer's data
    — so it is excluded from the keyword sweep rather than forced into a
    real action that would carry no security meaning.
    """
    must_audit = [
        k
        for k in READ_POSTURE
        if any(s in k for s in ("/download", "/export", "bundle", "secret", "/sample")) and k != "GET /cli/download"
    ]
    assert must_audit, "sanity: the fixture list should not be empty"
    wrongly_exempt = sorted(k for k in must_audit if READ_POSTURE[k].startswith("exempt:"))
    assert not wrongly_exempt, wrongly_exempt


def test_read_self_auditing_routes_are_all_real_declared_actions():
    """Every READ_SELF_AUDITING entry is a route that (a) is declared in
    READ_POSTURE and (b) names a real action, never an exempt reason — the
    whole point of the set is "the handler already writes THIS action, skip
    the middleware entirely for it"."""
    for key in READ_SELF_AUDITING:
        assert key in READ_POSTURE, f"{key} is in READ_SELF_AUDITING but undeclared in READ_POSTURE"
        assert not READ_POSTURE[key].startswith("exempt:"), f"{key} is exempt, it should not be self-auditing"


def test_declared_read_action_resolves_and_skips_exempt():
    key = next(k for k, v in READ_POSTURE.items() if not v.startswith("exempt:"))
    method, template = key.split(" ", 1)
    assert declared_read_action(method, template) == READ_POSTURE[key]

    ex = next(k for k, v in READ_POSTURE.items() if v.startswith("exempt:"))
    m, t = ex.split(" ", 1)
    assert declared_read_action(m, t) is None

    assert declared_read_action("GET", "/no/such/route") is None


def test_unaudited_read_route_gets_its_declared_action(tmp_path, monkeypatch, seeded_app):
    """GET /api/collections/search writes no audit row of its own — the
    middleware must emit its declared action (collection.search)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.get(
        "/api/collections/search",
        params={"q": "posture-test"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="collection.search", limit=10)
    matches = [r for r in rows if r["resource"] == "/api/collections/search"]
    assert matches, "middleware must emit the route's DECLARED read action"
    assert matches[0]["user_id"] == "analyst1"


def test_self_auditing_sync_read_route_is_not_duplicated(tmp_path, monkeypatch, seeded_app):
    """GET /api/v2/catalog is a plain ``def`` (thread-offloaded) handler that
    writes its own ``catalog.list`` row. The contextvar counter can't see
    that write from the async middleware — READ_SELF_AUDITING must make the
    middleware skip this route outright rather than double-write it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert "GET /api/v2/catalog" in READ_SELF_AUDITING
    client = seeded_app["client"]
    resp = client.get("/api/v2/catalog", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="catalog.list", limit=10)
    assert rows, "the route's own audit row is missing"
    assert len(rows) == 1, f"exactly one row expected, got {len(rows)} — middleware duplicated a self-audited read"


def test_read_path_adds_no_correlation_id_query(tmp_path, monkeypatch, seeded_app):
    """The read branch must never run the correlation-id tie-break query —
    that is a DB round-trip per request and the read path is far hotter than
    the mutating one (only the mutating path may pay it, on its slow/rare
    ambiguous-zero-count branch)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    calls = []
    from src.repositories import audit as audit_mod

    orig = audit_mod.AuditRepository.query

    def _spy(self, **kwargs):
        calls.append(kwargs)
        return orig(self, **kwargs)

    monkeypatch.setattr(audit_mod.AuditRepository, "query", _spy)

    client = seeded_app["client"]
    resp = client.get("/api/v2/catalog", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    resp2 = client.get(
        "/api/collections/search",
        params={"q": "posture-test"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp2.status_code == 200

    assert not [c for c in calls if "correlation_id" in c], (
        f"the read path issued a correlation-id tie-break query: {calls}"
    )


def test_exempt_read_route_writes_nothing(tmp_path, monkeypatch, seeded_app):
    """A GET route declared 'exempt:<reason>' must never get a middleware-
    written row, no matter how the handler behaves."""
    key = next(k for k, v in READ_POSTURE.items() if v == "exempt:health")
    _, path = key.split(" ", 1)
    assert "{" not in path, "pick a health route with no path params for a simple direct hit"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    client.get(path)

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(limit=200)
    assert not any(r["resource"] == path for r in rows)
