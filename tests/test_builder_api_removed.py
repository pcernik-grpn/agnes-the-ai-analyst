"""The /agents builder's own adapter router is gone (Task C1.2).

`app/api/agents.py` — the FastAPI router serving `/api/agents*` — is
deleted outright (LD3: no deprecation window). Task C1.1 folded every
operation the builder used it for into `/api/v1/agents*`
(`app/api/agents_admin.py`); this suite is the negative half (the old
routes genuinely 404) and the positive half (the builder page still works,
wired entirely to v1 now).

Companion coverage: `tests/test_v1_builder_parity.py` (C1.1 — the v1
absorption itself), `tests/test_agents_management_api.py` (v1's own
contract), `tests/test_agent_builder_scope_contract.py` and
`tests/test_agents_placeholder_slug_rename.py` (the builder-shape scope/slug
rules, re-pinned against v1 in this task).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_get_api_agents_is_gone(seeded_app):
    """No route claims `/api/agents` at all — the web router's own catch-all
    (``app/web/router.py::_catch_all_404``, unrelated to this change) is what
    answers every unmatched GET with a formatted 404, so a genuine 404 here
    is the correct "route is gone" signal for GET specifically."""
    resp = seeded_app["client"].get("/api/agents", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 404


def test_post_api_agents_is_gone(seeded_app):
    """The catch-all above is GET-only, so a write verb against a path no
    real router claims surfaces as 405 (Starlette matched the catch-all's
    PATTERN, just not this METHOD) rather than 404 — still an unambiguous
    "no such endpoint" signal, and the one this app actually produces."""
    resp = seeded_app["client"].post(
        "/api/agents", json={"name": "Should Not Exist"}, headers=_auth(seeded_app["admin_token"])
    )
    assert resp.status_code == 405


def test_agent_scoped_paths_are_gone(seeded_app):
    """The `{agent_id}` sub-route is gone too, not just the collection root."""
    tok = seeded_app["admin_token"]
    c = seeded_app["client"]
    assert c.get("/api/agents/some-id", headers=_auth(tok)).status_code == 404
    assert c.patch("/api/agents/some-id", json={"name": "x"}, headers=_auth(tok)).status_code == 405
    assert c.delete("/api/agents/some-id", headers=_auth(tok)).status_code == 405


def test_app_api_agents_module_no_longer_exists():
    """The router module itself is deleted, not merely unregistered — a
    stray import anywhere in the codebase would otherwise still succeed
    against dead code."""
    import importlib

    import pytest as _pytest

    with _pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.api.agents")


def test_the_builder_page_still_renders(seeded_app):
    """`/agents` (the page, not the API) is untouched — it now calls v1
    exclusively client-side."""
    resp = seeded_app["client"].get("/agents", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "ag-list-view" in resp.text or "ag-builder-view" in resp.text


def test_the_builder_page_no_longer_fetches_the_deleted_router():
    """Static check on the page's own JS: no `fetch('/api/agents` call site
    survives the re-point (Task C1.2) — every one of them now targets
    `/api/v1/agents`."""
    from pathlib import Path

    html = Path("app/web/templates/agents.html").read_text(encoding="utf-8")
    assert "fetch('/api/agents" not in html
    assert 'fetch("/api/agents' not in html
    # And the replacement really is there — a passing "absence" check alone
    # would also pass if the fetches were deleted outright rather than
    # re-pointed.
    assert html.count("fetch('/api/v1/agents") >= 3, "expected create/save/delete/list to all hit v1"


def test_builder_operations_work_through_v1_end_to_end(seeded_app):
    """A TestClient walk of the exact v1 endpoints `agents.html` now calls,
    in the order the page calls them: create (with a placeholder name, since
    v1 requires one — the page's own workaround for the builder's blank-draft
    UX), list, rename + re-declare (PUT), read back, delete."""
    tok = seeded_app["admin_token"]
    c = seeded_app["client"]

    # create — mirrors createAgent()'s payload shape, placeholder name and
    # explicit surfaces included (v1 does not invent the {"web": true}
    # default the deleted router used to).
    created = c.post(
        "/api/v1/agents",
        json={
            "name": "Untitled",
            "role": "",
            "instructions": "",
            "tone": "concise",
            "greeting": "Hi! How can I help?",
            "knowledge": [],
            "plugins": [],
            "surfaces": {"web": True, "slack": False, "telegram": False, "cli": False, "mcp": False},
            "status": "draft",
        },
        headers=_auth(tok),
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["slug"] == "untitled"
    assert agent["mine"] is True
    assert agent["surfaces"]["web"] is True

    # list — mirrors the page's boot fetch.
    listed = c.get("/api/v1/agents", headers=_auth(tok)).json()
    assert "data" in listed
    assert any(a["id"] == agent["id"] for a in listed["data"])

    # rename + re-declare — mirrors persist()'s debounced PUT.
    updated = c.put(
        f"/api/v1/agents/{agent['id']}",
        json={
            "name": "Revenue Analyst",
            "role": "Answers revenue questions",
            "instructions": "",
            "tone": "friendly",
            "greeting": "Hi! How can I help?",
            "knowledge": [],
            "plugins": [],
            "surfaces": {"web": True, "slack": False, "telegram": False, "cli": False, "mcp": False},
            "status": "draft",
        },
        headers=_auth(tok),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["name"] == "Revenue Analyst"
    # A draft's slug follows its name (`_draft_slug_rename`) — the same rule
    # the deleted router's own PATCH used.
    assert body["slug"] == "revenue-analyst"

    # read back — mirrors openBuilder's per-agent hydration.
    fetched = c.get(f"/api/v1/agents/{agent['id']}", headers=_auth(tok))
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Revenue Analyst"

    # delete — mirrors the card's Delete control.
    assert c.delete(f"/api/v1/agents/{agent['id']}", headers=_auth(tok)).status_code == 204
    assert c.get(f"/api/v1/agents/{agent['id']}", headers=_auth(tok)).status_code == 404
