"""API tests for the authoring_suggestions queue (v77)."""

import pytest


@pytest.fixture(autouse=True)
def studio_on(monkeypatch):
    """The suggestion API is part of the Studio surface, which is OFF by
    default since the admin cleanup retired it (a disabled instance 403s the
    submit endpoint). These tests are about the queue's behavior when the
    surface is exposed, so they turn it on; the one test that asserts the
    disabled 403 patches `get_studio_enabled` directly and still wins.
    """
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _submit(client, token, domain="data-package", payload=None):
    return client.post(
        "/api/studio/suggestions",
        headers=_auth(token),
        json={"domain": domain, "payload": payload or {"name": "X", "slug": "x"}},
    )


def test_non_admin_can_submit_suggestion(seeded_app):
    c = seeded_app["client"]
    r = _submit(c, seeded_app["analyst_token"])
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"


def test_submit_rejects_unknown_domain(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        json={"domain": "nope", "payload": {"name": "x"}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "unknown_domain"


def test_caller_sees_only_their_own(seeded_app):
    c = seeded_app["client"]
    _submit(c, seeded_app["analyst_token"], payload={"name": "mine", "slug": "mine"})
    r = c.get("/api/studio/suggestions/mine", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    assert all(s["created_by"] == "analyst@test.com" for s in r.json())


def test_admin_queue_and_approve_flow(seeded_app):
    c = seeded_app["client"]
    sid = _submit(c, seeded_app["analyst_token"]).json()["id"]

    # admin lists pending
    q = c.get(
        "/api/admin/authoring-suggestions?status=pending",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert q.status_code == 200
    assert any(s["id"] == sid for s in q.json())

    # admin approves
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={"note": "lgtm"},
    )
    assert a.status_code == 200
    assert a.json()["status"] == "approved"

    # re-approving a resolved row is a 409 guard miss
    a2 = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a2.status_code == 409


def test_reject_flow_for_a_domain_without_a_side_effect(seeded_app):
    """`_SIDE_EFFECTS` is domain-keyed and most domains have no entry — a
    reject for one of those must resolve normally rather than error."""
    c = seeded_app["client"]
    sid = _submit(c, seeded_app["analyst_token"]).json()["id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/reject",
        headers=_auth(seeded_app["admin_token"]),
        json={"note": "no thanks"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_admin_endpoints_require_admin(seeded_app):
    c = seeded_app["client"]
    r = c.get("/api/admin/authoring-suggestions", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code in (401, 403)


def test_approve_data_package_auto_creates_resource(seeded_app):
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="data-package",
        payload={"name": "Auto", "slug": "auto-dp", "description": "x"},
    ).json()["id"]
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 200
    rid = a.json()["created_resource_id"]
    assert rid and rid.startswith("pkg_")
    pkgs = c.get("/api/admin/data-packages", headers=_auth(seeded_app["admin_token"])).json()
    assert any(p.get("slug") == "auto-dp" for p in pkgs)


def test_approve_mcp_auto_creates_via_revalidation(seeded_app):
    """mcp approval replays through CreateMCPSourceRequest re-validation; the
    admin saw the full payload (command/url) in the queue before approving."""
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="mcp",
        payload={"name": "auto_mcp", "transport": "http", "url": "https://x"},
    ).json()["id"]
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 200, a.text
    assert a.json()["created_resource_id"]  # an mcp source id


def test_approve_mcp_unsafe_name_is_rejected(seeded_app):
    """Re-validation still enforces the safe-identifier rule on approve."""
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="mcp",
        payload={"name": "bad-name", "transport": "http", "url": "https://x"},
    ).json()["id"]
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 409  # create_failed — unsafe SQL identifier


def test_submit_rejects_direct_domain(seeded_app):
    """Domains with their own moderation (the store) must not enter the
    suggestions queue — there is no _SAFE_REPLAY for them, so an approve
    would silently create nothing."""
    c = seeded_app["client"]
    r = _submit(
        c,
        seeded_app["analyst_token"],
        domain="skill",
        payload={"name": "x", "skill_md": "y"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "domain_submits_directly"


def test_suggestion_api_403_when_studio_disabled(seeded_app, monkeypatch):
    """The instance-level Studio toggle closes the WHOLE suggestion API —
    public submit/read-own and the admin moderation endpoints — mirroring the
    web routes (owner decision on PR #973). Pending rows persist and reappear
    when Studio is re-enabled."""
    monkeypatch.setattr("app.api.authoring_suggestions.get_studio_enabled", lambda: False)
    c = seeded_app["client"]
    r = _submit(c, seeded_app["analyst_token"])
    assert r.status_code == 403
    assert r.json()["detail"]["kind"] == "studio_disabled"
    r = c.get("/api/studio/suggestions/mine", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403
    q = c.get("/api/admin/authoring-suggestions", headers=_auth(seeded_app["admin_token"]))
    assert q.status_code == 403
    a = c.post(
        "/api/admin/authoring-suggestions/nope/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 403


def test_an_approved_corporate_memory_submission_keeps_the_knowledge(seeded_app):
    """Devin Review on #1263: the replay dropped the whole submission.

    A non-admin fills in the knowledge on the Corporate Memory builder, sees
    "Submitted for approval", and the admin approves — and the replay copied
    only name/slug/description, so what appeared was an empty domain and the
    text was gone. Nothing warned anybody, on either side.
    """
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    c = seeded_app["client"]
    content = "Never report the latest month before the FX rate lands."
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="corporate-memory",
        payload={"name": "Month-end close", "slug": "month-end-close", "content": content},
    ).json()["id"]

    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={"note": "lgtm"},
    )
    assert a.status_code == 200, a.text

    conn = get_system_db()
    items = [it for it in KnowledgeRepository(conn).list_items(limit=200) if it.get("domain") == "month-end-close"]
    conn.close()
    assert items, "the knowledge the author wrote did not survive approval"
    assert items[0]["content"] == content
    # The submitter's knowledge, not the approver's.
    assert items[0]["source_user"] == "analyst@test.com", items[0]


def test_a_corporate_memory_submission_without_content_still_creates_the_domain(seeded_app):
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="corporate-memory",
        payload={"name": "Empty on purpose", "slug": "empty-on-purpose"},
    ).json()["id"]

    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 200, a.text
    assert a.json()["created_resource_id"]


def test_a_failed_seeding_leaves_no_domain_behind(seeded_app):
    """Devin Review on #1263: a half-done approval must not become permanent.

    The domain is created first, so a failure while saving the knowledge
    reopened the suggestion with the domain already there — and every retry
    then died on the duplicate slug, leaving the submission impossible to
    approve and an empty domain nobody asked for.
    """
    from unittest.mock import patch

    from src.repositories import memory_domains_repo

    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="corporate-memory",
        payload={"name": "Rollback", "slug": "rollback-me", "content": "some knowledge"},
    ).json()["id"]

    with patch("app.api.memory_domains.seed_domain_item", side_effect=RuntimeError("boom")):
        r = c.post(
            f"/api/admin/authoring-suggestions/{sid}/approve",
            headers=_auth(seeded_app["admin_token"]),
            json={},
        )
    assert r.status_code == 409, r.text

    assert not any(d["slug"] == "rollback-me" for d in memory_domains_repo().list()), (
        "the domain survived a failed approval, so no retry can ever succeed"
    )

    # …and the retry works once the failure is gone.
    r2 = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert r2.status_code == 200, r2.text


def test_a_slug_with_stray_whitespace_still_approves(seeded_app):
    """Devin Review on #1263: the domain took the raw slug, the item a trimmed
    one — so the seeding could not resolve it and every approval rolled back."""
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="corporate-memory",
        payload={"name": "  Spaced  ", "slug": "  spaced-slug  ", "content": "knowledge here"},
    ).json()["id"]

    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert r.status_code == 200, r.text

    conn = get_system_db()
    items = [it for it in KnowledgeRepository(conn).list_items(limit=200) if it.get("domain") == "spaced-slug"]
    conn.close()
    assert items and items[0]["content"] == "knowledge here"


def test_a_blank_name_or_slug_is_refused_rather_than_created(seeded_app):
    """Devin Review on #1263: present-but-empty is as unusable as absent."""
    c = seeded_app["client"]
    for payload in (
        {"name": "  ", "slug": "blank-name", "content": "x"},
        {"name": "Blank slug", "slug": "   ", "content": "x"},
    ):
        sid = _submit(c, seeded_app["analyst_token"], domain="corporate-memory", payload=payload).json()["id"]
        r = c.post(
            f"/api/admin/authoring-suggestions/{sid}/approve",
            headers=_auth(seeded_app["admin_token"]),
            json={},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["kind"] == "invalid_payload"
