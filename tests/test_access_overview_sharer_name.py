"""The overview names who shared a row, not their id.

`library_sharing` records a share's `assigned_by` as the sharer's user id;
the admin API records an email. The page resolved ids only against a user
list the group list never loads, so an owner-shared row read
"shared by <uuid>". `/api/admin/access-overview` now carries
`assigned_by_name` on every grant row — resolved once per page through the
dual-backend `get_info_by_ids`, the reader the collection projection already
uses for owners. Emails pass through; an id that no longer resolves stays as
it is rather than vanishing.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _overview(seeded_app):
    r = seeded_app["client"].get("/api/admin/access-overview", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    return r.json()


def test_every_grant_row_carries_the_key(seeded_app):
    ov = _overview(seeded_app)
    assert all("assigned_by_name" in g for g in ov["grants"])


def test_a_user_id_resolves_to_the_persons_name(seeded_app):
    """Exactly what a Library share writes: assigned_by = the sharer's id."""
    from src.repositories import resource_grants_repo, users_repo

    ov = _overview(seeded_app)
    admin_gid = next(g["id"] for g in ov["groups"] if g["name"] == "Admin")
    sharer = users_repo().get_by_email("analyst@test.com")
    assert sharer, "seeded analyst expected"
    gid = resource_grants_repo().create(
        group_id=admin_gid, resource_type="collection", resource_id="col_sharer_test",
        assigned_by=sharer["id"], source="library_share",
    )
    row = next(g for g in _overview(seeded_app)["grants"] if g["id"] == gid)
    assert row["assigned_by"] == sharer["id"]
    assert row["assigned_by_name"] == (sharer.get("name") or sharer["email"])
    assert row["assigned_by_name"] != sharer["id"]


def test_an_email_passes_through_and_a_stale_id_stays_raw(seeded_app):
    from src.repositories import resource_grants_repo

    ov = _overview(seeded_app)
    admin_gid = next(g["id"] for g in ov["groups"] if g["name"] == "Admin")
    by_email = resource_grants_repo().create(
        group_id=admin_gid, resource_type="collection", resource_id="col_email_test", assigned_by="someone@test.com",
    )
    by_ghost = resource_grants_repo().create(
        group_id=admin_gid, resource_type="collection", resource_id="col_ghost_test", assigned_by="no-such-user-id",
    )
    rows = {g["id"]: g for g in _overview(seeded_app)["grants"]}
    assert rows[by_email]["assigned_by_name"] == "someone@test.com"      # untouched
    assert rows[by_ghost]["assigned_by_name"] == "no-such-user-id"       # raw, not dropped


def test_agent_and_collection_items_name_their_owner(seeded_app):
    """Without these two fields a row someone shared had no ownership on it and
    nowhere an admin could go — the builder page shows only the caller's own
    agents. Both projections now carry the owner's id and email."""
    from src.repositories import agents_repo, file_corpora_repo, users_repo

    owner = users_repo().get_by_email("analyst@test.com")
    agents_repo().create(id="agt_owner_test", owner_user_id=owner["id"], name="Owner test", slug="owner-test")
    col = file_corpora_repo().create(name="Owner test collection", slug="owner-test-col", description=None, created_by=owner["id"])

    ov = _overview(seeded_app)
    items = {(r["type_key"], it["resource_id"]): it for r in ov["resources"] for b in r["blocks"] for it in b["items"]}
    agent = items[("agent", "agt_owner_test")]
    assert agent["owner_user_id"] == owner["id"]
    assert agent["owner_email"] == owner["email"]
    coll = items[("collection", col)]
    assert coll["owner_user_id"] == owner["id"]
    assert coll["owner_email"] == owner["email"]
