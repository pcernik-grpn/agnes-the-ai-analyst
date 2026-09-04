"""``GET /api/admin/groups/member-search`` — which groups hold a matching person.

Audit finding S2. The Access page's search box has always promised people,
and answered by matching the typed text against every group's roster in the
browser — so every group's full ``member_ids`` travelled in the overview
payload, growing with headcount, capped at 500, and quietly wrong past that.
The server answers now, where the memberships are, and the roster no longer
ships at all.

Exercised through the seeded shared app: ``admin@test.com`` is in the
``Admin`` system group; an analyst is not an admin.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(seeded_app):
    return seeded_app["client"], seeded_app["admin_token"]


def _search(client, token, q):
    return client.get("/api/admin/groups/member-search", params={"q": q}, headers=_auth(token))


def test_q_is_required(admin):
    client, token = admin
    r = client.get("/api/admin/groups/member-search", headers=_auth(token))
    assert r.status_code == 422


def test_one_letter_is_refused(admin):
    """The same two-character floor the page always applied: one letter
    matches half the instance and says nothing."""
    client, token = admin
    assert _search(client, token, "a").status_code == 422


def test_a_seeded_admin_is_found_by_email_fragment_with_their_group(admin):
    client, token = admin
    ov = client.get("/api/admin/access-overview", headers=_auth(token)).json()
    admin_gid = next(g["id"] for g in ov["groups"] if g["name"] == "Admin")

    r = _search(client, token, "admin@test")
    assert r.status_code == 200
    body = r.json()
    assert body["matched_people"] >= 1
    by_group = {m["group_id"]: m["people"] for m in body["matches"]}
    assert admin_gid in by_group
    assert any(p["email"] == "admin@test.com" for p in by_group[admin_gid])


def test_the_match_is_case_insensitive(admin):
    client, token = admin
    lower = _search(client, token, "admin@test").json()
    upper = _search(client, token, "ADMIN@TEST").json()
    assert lower["matched_people"] == upper["matched_people"] >= 1


def test_a_miss_is_empty_not_an_error(admin):
    client, token = admin
    r = _search(client, token, "nobody-with-this-name-zz")
    assert r.status_code == 200
    assert r.json() == {"matches": [], "matched_people": 0}


def test_at_most_three_names_per_group(admin):
    """The row says WHO matched — up to three — not the whole roster."""
    client, token = admin
    body = _search(client, token, "test.com").json()  # every seeded account
    assert body["matched_people"] >= 3
    for m in body["matches"]:
        assert 1 <= len(m["people"]) <= 3
        for p in m["people"]:
            assert set(p) == {"name", "email"}


def test_the_overview_no_longer_ships_rosters(admin):
    """The whole point: nothing on the wire grows with headcount."""
    client, token = admin
    ov = client.get("/api/admin/access-overview", headers=_auth(token)).json()
    assert ov["groups"], "seeded groups expected"
    assert all("member_ids" not in g for g in ov["groups"])
    # The O(1) facts the page reads instead are still there.
    assert "account_total" in ov
    assert all("member_count" in g and "is_everyone" in g for g in ov["groups"])


def test_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.get("/api/admin/groups/member-search", params={"q": "admin"}, headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403
