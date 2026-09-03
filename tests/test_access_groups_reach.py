"""``GET /api/admin/groups/reach`` — how many distinct people a set of audiences reaches.

Audit findings E3 and S2. The Access page prints "N groups · M people" at the
moment an admin decides to share, and it used to compute M in the browser by
unioning member ids — with a fallback to a group's own count when its roster
was absent (double-counting anyone in two such groups) and a clamp to the
account total to hide the overshoot. The server answers now, where the
memberships are.

Exercised through the seeded shared app: the ``Admin`` system group holds one
seeded account, ``Everyone`` is the scope sentinel rather than a group, and an
analyst is not an admin.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _overview(client, token):
    r = client.get("/api/admin/access-overview", headers=_auth(token))
    assert r.status_code == 200
    return r.json()


@pytest.fixture
def admin(seeded_app):
    return seeded_app["client"], seeded_app["admin_token"]


def test_ids_is_required(admin):
    """FastAPI answers before any backend code runs, so the status is the
    same on DuckDB and Postgres by construction — which is what the parity
    sweep, calling every parameter-free route, needs it to be."""
    client, token = admin
    r = client.get("/api/admin/groups/reach", headers=_auth(token))
    assert r.status_code == 422


def test_everyone_is_the_account_total(admin):
    """The scope sentinel short-circuits: every account by construction, so
    no union can exceed it and none is needed."""
    client, token = admin
    total = _overview(client, token)["account_total"]
    r = client.get("/api/admin/groups/reach", params={"ids": "everyone"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"count": total, "account_total": total}


def test_everyone_dominates_any_group_beside_it(admin):
    client, token = admin
    ov = _overview(client, token)
    admin_gid = next(g["id"] for g in ov["groups"] if g["name"] == "Admin")
    r = client.get("/api/admin/groups/reach", params={"ids": f"everyone,{admin_gid}"}, headers=_auth(token))
    assert r.json()["count"] == ov["account_total"]


def test_a_group_reaches_its_distinct_members(admin):
    client, token = admin
    ov = _overview(client, token)
    admin_grp = next(g for g in ov["groups"] if g["name"] == "Admin")
    r = client.get("/api/admin/groups/reach", params={"ids": admin_grp["id"]}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == admin_grp["member_count"]
    assert body["count"] <= body["account_total"]


def test_an_unknown_group_reaches_nobody_and_does_not_error(admin):
    """A nonexistent id is not a failure to answer — it is an audience of
    nobody. Raising here would make the picker's footer blank on a stale id."""
    client, token = admin
    r = client.get("/api/admin/groups/reach", params={"ids": "no-such-group"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_the_same_person_in_two_groups_is_counted_once(admin):
    """The whole reason this moved server-side: the browser's fallback
    double-counted. Admin plus a group with no members must equal Admin
    alone, and the union of Admin with itself must not double."""
    client, token = admin
    ov = _overview(client, token)
    admin_gid = next(g["id"] for g in ov["groups"] if g["name"] == "Admin")
    alone = client.get("/api/admin/groups/reach", params={"ids": admin_gid}, headers=_auth(token)).json()["count"]
    twice = client.get(
        "/api/admin/groups/reach", params={"ids": f"{admin_gid},{admin_gid}"}, headers=_auth(token)
    ).json()["count"]
    with_unknown = client.get(
        "/api/admin/groups/reach", params={"ids": f"{admin_gid},no-such-group"}, headers=_auth(token)
    ).json()["count"]
    assert alone == twice == with_unknown


def test_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.get("/api/admin/groups/reach", params={"ids": "everyone"}, headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403
