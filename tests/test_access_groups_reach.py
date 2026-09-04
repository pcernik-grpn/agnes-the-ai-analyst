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


class TestTheHeadcountIsPeople:
    """Issue #2256. The figure is printed as "N groups · M people", and it was
    computed from `count_all()` — every row in `users`, service accounts and
    the identities Agnes seeds for itself included. An everyone-scoped grant
    reaches neither (`src.service_accounts.is_person`), so the number beside
    Apply counted a CI token as a colleague and the clamp stopped being an
    invariant the moment a group held one.

    Driven through the handler with stubbed repos rather than through a
    seeded instance, because a `kind='service'` account is Postgres-only
    (#1534, Alembic 0096) and the DuckDB row shape — no `kind` column at all
    — is half of what needs asserting.
    """

    @staticmethod
    def _call(monkeypatch, members, people_total, ids):
        import asyncio

        from app.api import access as access_mod

        class _Members:
            def list_members_for_group(self, gid):
                return members if gid == "grp-1" else []

        class _Users:
            def count_people(self):
                return people_total

            def count_all(self):  # pragma: no cover - must not be consulted
                raise AssertionError("reach must count people, not every row in `users`")

        monkeypatch.setattr(access_mod, "user_group_members_repo", lambda: _Members())
        monkeypatch.setattr(access_mod, "users_repo", lambda: _Users())
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(access_mod.groups_reach(ids=ids, user={"id": "admin1", "email": "admin@x"}))
        finally:
            loop.close()

    def test_a_service_account_in_a_group_is_not_a_person(self, monkeypatch):
        """A group MAY hold one — that is the only way it gets any authority
        (#1534) — but it is not a colleague the share reaches."""
        members = [
            {"id": "u-human", "email": "ada@example.com", "kind": "human"},
            {"id": "u-svc", "email": "ci@example.com", "kind": "service"},
        ]
        got = self._call(monkeypatch, members, people_total=1, ids="grp-1")
        assert got == {"count": 1, "account_total": 1}

    def test_a_seeded_system_identity_is_not_a_person_either(self, monkeypatch):
        members = [
            {"id": "u-human", "email": "ada@example.com", "kind": "human"},
            {"id": "u-curator", "email": "memory-curator@system.local", "kind": "system"},
        ]
        got = self._call(monkeypatch, members, people_total=1, ids="grp-1")
        assert got == {"count": 1, "account_total": 1}

    def test_a_system_identity_is_excluded_with_no_kind_column_at_all(self, monkeypatch):
        """The DuckDB row shape: `kind` arrived in PG-only Alembic 0096, so
        `list_members_for_group` cannot select it there. `is_person` falls
        back to the address, which is why the exclusion still holds on the
        frozen backend — and a service account, the one case the fallback
        cannot see, cannot exist there to begin with."""
        members = [
            {"id": "u-human", "email": "ada@example.com"},
            {"id": "u-curator", "email": "memory-curator@system.local"},
            {"id": "u-sched", "email": "scheduler@system.local"},
        ]
        got = self._call(monkeypatch, members, people_total=1, ids="grp-1")
        assert got == {"count": 1, "account_total": 1}

    def test_everyone_short_circuits_to_the_people_total(self, monkeypatch):
        """No union for the scope — and the total it returns is the people
        count, so the audience the page calls "everyone" and the number
        beside it describe the same population."""
        got = self._call(monkeypatch, [], people_total=7, ids="everyone")
        assert got == {"count": 7, "account_total": 7}

    def test_the_clamp_is_an_invariant_again(self, monkeypatch):
        """Both sides count people now, so a union can never exceed the
        total — previously a group holding a service account could, and the
        clamp silently hid it."""
        members = [{"id": f"u{i}", "email": f"p{i}@example.com", "kind": "human"} for i in range(3)] + [
            {"id": "u-svc", "email": "ci@example.com", "kind": "service"}
        ]
        got = self._call(monkeypatch, members, people_total=3, ids="grp-1")
        assert got == {"count": 3, "account_total": 3}


def test_the_everyone_block_names_who_is_not_an_account(monkeypatch):
    """Issue #2256's copy half. "Every account" stays — in this product an
    account is one a person signs in as (`HUMAN_KIND`), which is why the
    count is `count_people()` on both this endpoint and the overview. What
    was missing is that the page never said so, and the two identities it
    excludes are ones an admin can see on /admin/users. Named in the
    audience's own explainer rather than in a tooltip."""
    from tests.helpers.access_page import access_js

    js = access_js()
    assert "Not a group — a scope. Anything here reaches every" in js
    assert "Service accounts and Agnes's own" in js
    assert "identities are not accounts in this sense." in js
