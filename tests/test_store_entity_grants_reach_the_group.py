"""A `store_entity` grant reaches the group it was given to.

It used to be accepted and mean nothing. `POST /api/admin/grants` with
`resource_type=store_entity` returned 201, the row landed in
`resource_grants`, and for the group it changed exactly nothing: 404 on the
item, absent from every listing, and `409 entity_not_approved` on install. The
Required tier built on top of it was worse than inert — it installed, for
every member of the group, something none of them could open.

So the grant now says what it always looked like it said. Private stops
meaning "nobody but me" and starts meaning "not everyone": a granted group can
find the item, open it, and add it. What a grant does NOT do is override
review — a bundle the guardrails blocked stays unreachable, which is the one
property that must survive this change.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_DESC = "Use when checking whether a store entity grant reaches the group it was given to"
_BODY = "# Shared skill\n\n" + ("This body is long enough to clear the content guardrail's floor. " * 6)


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    for d in ("state", "analytics", "extracts"):
        (tmp_path / d).mkdir()
    from src.db import close_system_db

    close_system_db()
    yield TestClient(shared_app)
    close_system_db()


def _user(client, email, *, admin=False):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    uid = email.split("@")[0]
    UserRepository(conn).create(id=uid, email=email, name=uid, password_hash=PasswordHasher().hash("UserPass1!"))
    if admin:
        g = UserGroupsRepository(conn).get_by_name("Admin")
        UserGroupMembersRepository(conn).add_member(uid, g["id"], source="test")
    conn.close()
    t = client.post("/auth/token", json={"email": email, "password": "UserPass1!"})
    assert t.status_code == 200, t.text
    return uid, {"access_token": t.json()["access_token"]}


def _private_entity(client, cookies, name="shared-skill", publisher="user"):
    r = client.post(
        "/api/store/entities/from-markdown",
        cookies=cookies,
        json={
            "name": name,
            "description": _DESC,
            "skill_md": _BODY,
            "access": "private",
            "publisher_kind": publisher,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _group_with(client, admin_cookies, member_email, name="Finance"):
    g = client.post("/api/admin/groups", cookies=admin_cookies, json={"name": name})
    assert g.status_code in (200, 201), g.text
    gid = g.json()["id"]
    m = client.post(f"/api/admin/groups/{gid}/members", cookies=admin_cookies, json={"email": member_email})
    assert m.status_code in (200, 201, 204), m.text
    return gid


def _grant(client, admin_cookies, gid, eid, requirement=None):
    body = {"group_id": gid, "resource_type": "store_entity", "resource_id": eid}
    if requirement:
        body["requirement"] = requirement
    return client.post("/api/admin/grants", cookies=admin_cookies, json=body)


class TestAGrantIsAccess:
    def test_an_ungranted_member_still_sees_nothing(self, client):
        """The control. Without this the rest could pass on a broken gate."""
        _, admin = _user(client, "root@x.com", admin=True)
        _, member = _user(client, "stranger@x.com")
        eid = _private_entity(client, admin)
        assert client.get(f"/api/store/entities/{eid}", cookies=member).status_code == 404

    def test_a_granted_member_can_open_it(self, client):
        aid, admin = _user(client, "root2@x.com", admin=True)
        mid, member = _user(client, "member2@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        assert _grant(client, admin, gid, eid).status_code == 201
        r = client.get(f"/api/store/entities/{eid}", cookies=member)
        assert r.status_code == 200, r.text

    def test_a_granted_member_finds_it_in_the_listing(self, client):
        aid, admin = _user(client, "root3@x.com", admin=True)
        mid, member = _user(client, "member3@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        _grant(client, admin, gid, eid)
        body = client.get("/api/store/entities", cookies=member).json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert eid in [e["id"] for e in items], "granted, and still not browsable"

    def test_a_granted_member_can_install_it(self, client):
        aid, admin = _user(client, "root4@x.com", admin=True)
        mid, member = _user(client, "member4@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        _grant(client, admin, gid, eid)
        r = client.post(f"/api/store/entities/{eid}/install", cookies=member)
        assert r.status_code in (200, 201), r.text

    def test_it_is_then_actually_served(self, client):
        """Install is not serve: the serve chokepoint re-evaluates visibility
        on every read, so a row in `user_store_installs` for an entity that
        path still filters out would be a silent no-op."""
        aid, admin = _user(client, "root5@x.com", admin=True)
        mid, member = _user(client, "member5@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        _grant(client, admin, gid, eid)
        client.post(f"/api/store/entities/{eid}/install", cookies=member)
        from src.repositories import user_store_installs_repo

        served = user_store_installs_repo().list_for_user(mid, [eid])
        assert eid in [row["id"] for row in served], "installed, and served to nobody"
        # ...and the same call without the grant set serves nothing, which is
        # what proves the set is what did it.
        assert user_store_installs_repo().list_for_user(mid) == []


class TestAGrantIsNotAnOverride:
    def test_a_blocked_bundle_stays_unreachable(self, client):
        """The property that must survive: a grant widens WHO may install,
        never what a bundle the guardrails blocked is allowed to do."""
        aid, admin = _user(client, "root6@x.com", admin=True)
        mid, member = _user(client, "member6@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        _grant(client, admin, gid, eid)

        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO store_submissions (id, entity_id, submitter_id, type, name, version, status, created_at) "
            "VALUES ('sub-blocked', ?, ?, 'skill', 'shared-skill', 'v1', 'blocked_llm', CURRENT_TIMESTAMP)",
            [eid, aid],
        )
        conn.close()

        r = client.post(f"/api/store/entities/{eid}/install", cookies=member)
        assert r.status_code == 409, r.text
        from src.repositories import user_store_installs_repo

        assert user_store_installs_repo().list_for_user(mid, [eid]) == [], (
            "a blocked bundle is being served to a granted group"
        )

    def test_a_stranger_is_unaffected_by_someone_elses_grant(self, client):
        aid, admin = _user(client, "root7@x.com", admin=True)
        mid, member = _user(client, "member7@x.com")
        _, outsider = _user(client, "outsider7@x.com")
        eid = _private_entity(client, admin)
        gid = _group_with(client, admin, f"{mid}@x.com")
        _grant(client, admin, gid, eid)
        assert client.get(f"/api/store/entities/{eid}", cookies=outsider).status_code == 404


# ── The builder's side of it ────────────────────────────────────────────────


def test_the_builder_offers_three_tiers():
    """Two tiers were the whole choice — only me, or the entire instance — and
    the middle one is the common case."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "skills.html").read_text()
    for tier in ("private", "groups", "everyone"):
        assert f"accessOpt('{tier}'" in page, f"the {tier} tier is missing"
    # "These groups" is a private row plus grants — not a third `access` value
    # the API has never accepted.
    assert "draft.access = tier === 'everyone' ? 'everyone' : 'private';" in page, (
        "the builder is inventing an access value the store API does not take"
    )


def test_the_builder_writes_the_grants_after_the_row_exists():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "skills.html").read_text()
    assert "'/api/sharing/store_entity/'" in page, "the chosen groups are never granted anything"
    assert "function shareAfterSave" in page
    # A grant that did not land is the difference between "shared with Finance"
    # and "shared with nobody", and it is invisible on the next page.
    assert "Saved, but not shared" in page, "a sharing failure is swallowed"


def test_sharing_a_store_entity_is_owner_scoped_not_admin_only():
    """The author shares their own item with groups they are in; an admin can
    reach any group. That is `app/services/library_sharing.py`'s existing
    containment model — the type was simply excluded from it."""
    from app.services.library_sharing import SHAREABLE_TYPES, _OWNER_RESOLVERS
    from app.resource_types import ResourceType

    assert ResourceType.STORE_ENTITY.value in SHAREABLE_TYPES
    assert ResourceType.STORE_ENTITY.value in _OWNER_RESOLVERS
