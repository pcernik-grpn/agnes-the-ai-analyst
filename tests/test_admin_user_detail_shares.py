"""/admin/users/{id} says what this person owns and shares, and with whom.

Audit U7. /admin/access shows a shared row from the GROUP's side ("Finance has
Ada's agent") and its owner link landed on a People page that said nothing
about sharing. This is the other side of the same fact, rendered server-side
from the same repositories — no new endpoint. Agents and collections only, for
now: the two owner-shared kinds whose records carry an owner.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _page(seeded_app, user_id: str) -> str:
    r = seeded_app["client"].get(f"/admin/users/{user_id}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.status_code
    return r.text


def test_a_person_who_owns_nothing_says_so(seeded_app):
    from src.repositories import users_repo

    viewer = users_repo().get_by_email("viewer@test.com")
    html = _page(seeded_app, viewer["id"])
    assert 'id="shares"' in html
    assert "Owns nothing that can be shared" in html


def test_owned_items_list_their_groups_and_who_did_the_sharing(seeded_app):
    """The two sentences the Access row uses, from the person's side: the
    owner's own share reads "shared by them"; an admin's grant on the
    owner's item reads "granted by <admin>"."""
    from src.repositories import agents_repo, file_corpora_repo, resource_grants_repo, user_groups_repo, users_repo

    owner = users_repo().get_by_email("analyst@test.com")
    admin_gid = user_groups_repo().get_by_name("Admin")["id"]
    agents_repo().create(
        id="agt_shares_page", owner_user_id=owner["id"], name="Shares Page Agent", slug="shares-page-agent"
    )
    col = file_corpora_repo().create(
        name="Shares Page Collection", slug="shares-page-col", description=None, created_by=owner["id"]
    )
    grants = resource_grants_repo()
    grants.create(
        group_id=admin_gid,
        resource_type="agent",
        resource_id="agt_shares_page",
        assigned_by=owner["id"],
        source="library_share",
    )  # the owner's share
    grants.create(
        group_id=admin_gid, resource_type="collection", resource_id=col, assigned_by="admin@test.com"
    )  # an admin's grant

    html = _page(seeded_app, owner["id"])
    assert 'id="shares"' in html
    assert "Shares Page Agent" in html and "Shares Page Collection" in html
    assert "shared by them" in html
    assert "granted by admin" in html
    # The collection links home — by SLUG, which is what `/library/{slug}`
    # resolves. This asserted `/library/d/<id>` for as long as the page
    # built it: an extra segment no route matches, and the id where a slug
    # goes. The link 404'd here and on /admin/access, and the guard held it
    # in place. Asserting the ROUTE RESOLVES rather than the string is what
    # stops the next wrong path from being frozen the same way.
    assert "/library/shares-page-col" in html
    assert "/library/d/" not in html
    resolved = seeded_app["client"].get("/library/shares-page-col", headers=_auth(seeded_app["admin_token"]))
    assert resolved.status_code == 200, f"the Shares link must lead somewhere: got {resolved.status_code}"
    assert f"/admin/access?group={admin_gid}" in html  # each share can be changed where grants live
    assert "2 of 2 shared" in html


def test_an_unshared_item_is_listed_as_not_shared(seeded_app):
    from src.repositories import agents_repo, users_repo

    owner = users_repo().get_by_email("viewer@test.com")
    agents_repo().create(id="agt_unshared", owner_user_id=owner["id"], name="Unshared Agent", slug="unshared-agent")
    html = _page(seeded_app, owner["id"])
    assert "Unshared Agent" in html
    assert "nobody — not shared" in html
    assert "0 of 1 shared" in html


def test_a_non_admin_cannot_read_anyones_shares(seeded_app):
    from src.repositories import users_repo

    owner = users_repo().get_by_email("analyst@test.com")
    r = seeded_app["client"].get(f"/admin/users/{owner['id']}", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code in (401, 403)
