"""API tests for the privacy-gated memory-mining flow (v78)."""

import pytest


@pytest.fixture(autouse=True)
def studio_on(monkeypatch):
    """Mining writes its output into the Studio's suggestion queue, and this
    module reads it back through `GET /api/admin/authoring-suggestions` — which
    403s while Studio is off, the default since the admin cleanup. The 403 body
    is a dict where a list was expected, so the failure surfaced as a TypeError
    rather than as the gate it is."""
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_consent_defaults_to_opted_out(seeded_app):
    c = seeded_app["client"]
    r = c.get("/api/studio/memory-mining/consent", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    assert r.json()["opted_in"] is False


def test_consent_opt_in_then_out(seeded_app):
    c = seeded_app["client"]
    t = seeded_app["analyst_token"]
    assert c.post("/api/studio/memory-mining/consent", headers=_auth(t), json={"opt_in": True}).status_code == 200
    assert c.get("/api/studio/memory-mining/consent", headers=_auth(t)).json()["opted_in"] is True
    c.post("/api/studio/memory-mining/consent", headers=_auth(t), json={"opt_in": False})
    assert c.get("/api/studio/memory-mining/consent", headers=_auth(t)).json()["opted_in"] is False


def test_run_only_mines_opted_in_users(seeded_app):
    c = seeded_app["client"]
    # analyst opts in; viewer does not
    c.post("/api/studio/memory-mining/consent", headers=_auth(seeded_app["analyst_token"]), json={"opt_in": True})

    r = c.post("/api/admin/memory-mining/run", headers=_auth(seeded_app["admin_token"]), json={})
    assert r.status_code == 200
    body = r.json()
    assert body["authors"] == 1  # only the opted-in analyst
    assert len(body["created"]) == 1

    # the candidate landed as a corporate-memory suggestion with provenance
    q = c.get(
        "/api/admin/authoring-suggestions?domain=corporate-memory",
        headers=_auth(seeded_app["admin_token"]),
    ).json()
    assert any(s["payload"].get("provenance", {}).get("author") == "analyst@test.com" for s in q)


def test_run_is_deduped_on_rerun(seeded_app):
    """Re-running the miner must not spam duplicate pending proposals."""
    c = seeded_app["client"]
    c.post("/api/studio/memory-mining/consent", headers=_auth(seeded_app["analyst_token"]), json={"opt_in": True})

    first = c.post("/api/admin/memory-mining/run", headers=_auth(seeded_app["admin_token"]), json={}).json()
    assert len(first["created"]) == 1

    second = c.post("/api/admin/memory-mining/run", headers=_auth(seeded_app["admin_token"]), json={}).json()
    assert len(second["created"]) == 0
    assert second["skipped_existing"] == 1


def test_run_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/memory-mining/run", headers=_auth(seeded_app["analyst_token"]), json={})
    assert r.status_code in (401, 403)
