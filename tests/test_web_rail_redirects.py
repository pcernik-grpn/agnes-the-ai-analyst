"""tests/test_web_rail_redirects.py — standalone browse pages fold into the
Library under the rail chrome; topnav serves them unchanged.

302 (not 308) so a later layout flip is not cached permanently — the same
reasoning as the /dashboard → /chat rail redirect.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_app_for(owner_id: str, slug: str = "rt-app"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug=slug, name=slug, owner_user_id=owner_id, description="")
    finally:
        conn.close()


def _grant_required_domain_to(user_id: str, domain_id: str = "md_data"):
    """Make the Library's Memory band show ``user_id`` a row: grant a
    canonical seeded domain to Everyone as ``required`` (a required mandate
    renders even at zero items) and join the user explicitly — seeded_app
    creates users via the repo, which does NOT auto-join Everyone (that
    happens at login)."""
    import uuid

    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        group_id = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
        UserGroupMembersRepository(conn).add_member(user_id, group_id, source="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', ?, 'required', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, domain_id],
        )
    finally:
        conn.close()


def test_corporate_memory_redirects_to_library_under_rail(seeded_app, monkeypatch):
    """The redirect fires only when the Library's Memory band will actually
    show the caller a row — seed a required grant so the claim is testable
    (same shape as the /apps twin below)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    _grant_required_domain_to("analyst1")
    c = seeded_app["client"]
    resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=memory_domain"


def test_corporate_memory_keeps_empty_state_under_rail_when_caller_sees_no_band(seeded_app, monkeypatch):
    """A caller the Library would show NO memory row must keep this page's
    explicit empty state — redirecting them lands on a Library whose Memory
    band never rendered, with nothing explaining where the page went (the
    same finding the /apps twin fixed on this PR)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_corporate_memory_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    c = seeded_app["client"]
    resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_redirects_to_library_under_rail_when_caller_sees_an_app(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app_for("analyst1", slug="rt-own-app")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=files"


def test_apps_redirects_under_rail_when_caller_is_granted_not_owner(seeded_app, monkeypatch):
    """The redirect's predicate is owner OR granted — the granted half rides
    the same fetched-once grant set the Library band uses (one lookup per
    request, not one per registered app; Devin review on PR #1278)."""
    import uuid

    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app_for("admin1", slug="rt-granted-app")

    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        group_id = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
        UserGroupMembersRepository(conn).add_member("analyst1", group_id, source="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_app', 'rt-granted-app', 'available', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id],
        )
    finally:
        conn.close()

    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=files"


def test_apps_keeps_empty_state_under_rail_when_caller_sees_none(seeded_app, monkeypatch):
    """A caller the Library would show NO app row must keep this page's
    explicit empty state — redirecting them lands on a Library whose
    Artifacts band never rendered, with nothing explaining where the apps
    inventory went (Devin review on PR #1278)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_keeps_empty_state_under_rail_when_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200
