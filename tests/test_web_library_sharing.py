"""Library sharing + the agent registry (v103).

Three things ship together here and are locked in one place:

  - ``/api/v1/agents`` — the server-side agent registry that replaced the
    Agent builder's localStorage-only store, so an agent is a real Library
    item. The builder's own adapter router (``/api/agents``) served this
    same registry until the remediation-program's "one agent model" Track
    C1 folded its wire shape into v1 (Task C1.1) and deleted the router
    (Task C1.2) — every call below goes straight to v1.
  - ``/api/sharing`` — OWNER-initiated sharing. Everything in
    ``app/api/access.py`` is ``require_admin``; this is the counterpart that
    lets the creator of an item share it with groups they belong to. The
    security-relevant invariants are ownership and group containment.
  - ``/library`` — the renamed, widened former ``/artefacts``, listing
    artefacts + skills with per-row visibility. Agents are deliberately NOT
    listed there (they have their own home at ``/agents``), but they remain
    real registry rows whose grants ``/api/v1/agents`` honours.

Skills are deliberately NOT grant-shareable: an approved store entity is
already readable by every authenticated user, so a grant row on one would be
read by nothing. ``/api/sharing/skill/...`` must 404 rather than pretend.
"""

from __future__ import annotations
import pytest

import re
from pathlib import Path


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """This file exercises the RAIL redesign's unified /library. Topnav keeps
    the legacy collections page (the /catalog pattern) — guarded by
    tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_collection(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _create_agent(seeded_app, token: str, **fields) -> dict:
    # `surfaces` is sent explicitly, matching the /agents builder's own
    # create payload (`makeAgent()` in agents.html): v1 does not invent the
    # `{"web": true}` default the builder's now-deleted router used to for
    # an omitted one (Task C1.1/C1.2).
    payload = {"name": "Test Agent", "surfaces": {"web": True}}
    payload.update(fields)
    r = seeded_app["client"].post("/api/v1/agents", json=payload, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _create_blank_agent(owner_user_id: str) -> dict:
    """A placeholder-name draft — what "New agent" used to POST through the
    builder's own (now-deleted) `/api/agents` router. `/api/v1/agents`
    requires a non-blank `name` (400 `invalid_name`, Task C1.1/C1.2), so
    exercising this precondition means writing the row directly, in the
    exact shape `create_agent`'s old fallback produced
    (`_unique_slug(_auto_slug("agent"), owner_user_id)`).
    """
    import uuid

    from app.api.agents_builder_shared import _auto_slug, _unique_slug
    from src.repositories import agents_repo

    agent_id = "agt_" + uuid.uuid4().hex
    slug = _unique_slug(_auto_slug("agent"), owner_user_id)
    agents_repo().create(
        id=agent_id,
        owner_user_id=owner_user_id,
        name="",
        slug=slug,
        status="draft",
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    return agents_repo().get_by_id(agent_id)


def _group_with_member(user_id: str, group_name: str) -> str:
    """Create ``group_name`` (if absent) and put ``user_id`` in it."""
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    return grp["id"]


def _bare_group(group_name: str) -> str:
    """A group the caller is NOT a member of."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    groups = UserGroupsRepository(get_system_db())
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    return grp["id"]


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


def test_agent_create_list_patch_delete_roundtrip(seeded_app):
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    a = _create_agent(seeded_app, tok, name="Revenue Analyst", role="Finance", knowledge=["col_x"])
    # Ids are plain UUIDs, not the builder's own `agt_`-prefixed ones — that
    # distinction retired with the router that minted them (Task C1.2); a
    # single v1 create path mints ids the SAME way regardless of caller.
    assert a["id"]
    assert a["slug"] == "revenue-analyst"
    assert a["mine"] is True
    # Web chat is the always-on baseline surface.
    assert a["surfaces"]["web"] is True

    listed = c.get("/api/v1/agents", headers=_auth(tok)).json()["data"]
    assert any(x["id"] == a["id"] for x in listed)

    patched = c.put(f"/api/v1/agents/{a['id']}", json={"name": "Renamed", "plugins": ["p1"]}, headers=_auth(tok))
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["plugins"] == ["p1"]

    assert c.delete(f"/api/v1/agents/{a['id']}", headers=_auth(tok)).status_code == 204
    assert c.get(f"/api/v1/agents/{a['id']}", headers=_auth(tok)).status_code == 404


def test_agent_slug_collision_gets_suffix_not_conflict(seeded_app):
    """Two agents may share a name — duplicates are ordinary for user-named
    things, so the second gets `-2` rather than a 409."""
    tok = seeded_app["admin_token"]
    first = _create_agent(seeded_app, tok, name="Analyst")
    second = _create_agent(seeded_app, tok, name="Analyst")
    assert first["slug"] == "analyst"
    assert second["slug"] == "analyst-2"


def test_agent_slug_freed_name_reuses_suffix_after_delete(seeded_app):
    """Deleting an agent must not poison its slug for the next one.

    ``delete`` is a SOFT delete (``deleted_at``), but the ``slug`` UNIQUE
    constraint spans deleted rows on both backends — so the free-slug search
    has to see them. It previously used the default (live-rows-only) lookup,
    which reported the freed slug as available and drove the INSERT into
    ``ConstraintException`` → 500. Create-delete-create is the ordinary path
    for an untitled draft (the "+ Build an agent" button mints one every
    click), so this fired on the second click of a fresh workspace."""
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    first = _create_agent(seeded_app, tok, name="Recycled")
    assert first["slug"] == "recycled"
    assert c.delete(f"/api/v1/agents/{first['id']}", headers=_auth(tok)).status_code == 204

    # 201, not 500 — and the slug steps around the soft-deleted row.
    second = _create_agent(seeded_app, tok, name="Recycled")
    assert second["slug"] == "recycled-2"

    # The unnamed-draft case the builder actually hits (slug falls back to
    # "agent"), twice over, with a delete in between. `/api/v1/agents`
    # requires a non-blank `name` (Task C1.1/C1.2), so this precondition is
    # written directly through the repo — see `_create_blank_agent`.
    d1 = _create_blank_agent("admin1")
    assert c.delete(f"/api/v1/agents/{d1['id']}", headers=_auth(tok)).status_code == 204
    d2 = _create_blank_agent("admin1")
    assert d1["slug"] != d2["slug"]


def test_agent_default_cannot_be_deleted(seeded_app):
    """The seeded default agent is not deletable through the builder.

    It is listed like any other agent (``list_for_user`` returns it first), so
    the Library's delete control reaches it. Deleting it used to break web chat
    outright: every session create resolves the default first, and that lookup
    re-inserted a row whose ``slug='default'`` still collided with the
    soft-deleted tombstone — a permanent 500 on ``POST /api/chat/sessions``.
    The repository now revives the tombstone instead of raising, but the delete
    still has no business succeeding: the agent would vanish from the Library
    and silently reappear on the owner's next chat.
    """
    from src.repositories import agents_repo

    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    default_id = agents_repo().get_or_create_default("admin1")["id"]
    listed = c.get("/api/v1/agents", headers=_auth(tok)).json()["data"]
    assert any(x["id"] == default_id for x in listed), "default agent is reachable in the Library"

    r = c.delete(f"/api/v1/agents/{default_id}", headers=_auth(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "default_agent_undeletable"

    # Still live, and still the default.
    assert agents_repo().get_by_id(default_id)["deleted_at"] is None
    assert c.get(f"/api/v1/agents/{default_id}", headers=_auth(tok)).status_code == 200


def test_agent_wire_shape_marks_the_default_and_page_hides_its_delete(seeded_app):
    """`is_default` reaches the browser, and the page uses it.

    The API refuses to delete the seeded default (400
    `default_agent_undeletable`). Without the flag on the wire the page renders
    a delete control anyway, and clicking it optimistically removes the row,
    fails, restores it, and toasts a bare "HTTP 400" — so the guard reads as a
    glitch. Assert both halves: the projection, and that the two render sites
    branch on it.
    """
    from pathlib import Path

    from src.repositories import agents_repo

    c = seeded_app["client"]
    tok = seeded_app["admin_token"]
    default_id = agents_repo().get_or_create_default("admin1")["id"]

    listed = c.get("/api/v1/agents", headers=_auth(tok)).json()["data"]
    by_id = {a["id"]: a for a in listed}
    assert by_id[default_id]["is_default"] is True
    # A user-created agent is not the default — the flag has to discriminate.
    mine = _create_agent(seeded_app, tok, name="Ordinary")
    assert c.get(f"/api/v1/agents/{mine['id']}", headers=_auth(tok)).json()["is_default"] is False

    tpl = Path("app/web/templates/agents.html").read_text(encoding="utf-8")
    assert tpl.count("a.is_default") >= 2, "both the list card and the builder header must branch on is_default"


def test_agent_named_default_does_not_claim_the_reserved_slug(seeded_app):
    """`"default"` belongs to the seeded default agent, not to a user-named one.

    The builder derives its slug from a typed name, so "Default" is suffixed
    rather than rejected (the governance router 400s `slug_reserved` instead).
    Left unreserved, an ordinary name could claim the slug before the owner's
    first chat seeded the real default — and
    `POST /api/v1/agents/default/responses` would then address the user's agent.
    """
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Default")
    assert a["slug"] == "default-2"


def test_agent_patch_cannot_reassign_ownership(seeded_app):
    """A hostile payload can't move an agent to another owner or hijack a slug.

    `UpdateAgentRequest` DOES have a `slug` field (unlike the builder's old
    `AgentUpdate`) — but only to refuse it outright (400 `slug_immutable`,
    Task C1.1), a stricter guard than the old router's silent drop. `id`/
    `created_by` aren't real fields on the model either way, so they're
    dropped by Pydantic like any unknown key. The slug CAN still change
    without an explicit `slug` in the payload — renaming a draft re-derives
    it (`app.api.agents_builder_shared._draft_slug_rename`) — but only ever
    to a value derived from the new name, never to the attacker's.
    """
    tok = seeded_app["admin_token"]
    client = seeded_app["client"]
    a = _create_agent(seeded_app, tok, name="Owned", status="draft")

    r = client.put(
        f"/api/v1/agents/{a['id']}",
        json={"slug": "hijacked", "name": "Still Mine"},
        headers=_auth(tok),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "slug_immutable"

    r = client.put(
        f"/api/v1/agents/{a['id']}",
        json={"created_by": "analyst1", "id": "agt_evil", "name": "Still Mine"},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    assert r.json()["owner_user_id"] == a["owner_user_id"]
    assert r.json()["slug"] == "still-mine", "slug must follow the name, not the payload"
    assert r.json()["name"] == "Still Mine"


def test_agent_patch_cannot_hijack_the_slug_of_a_published_agent(seeded_app):
    """The stronger form: once ready, the slug does not move at all — and
    `/api/v1/agents` refuses an explicit `slug` in the payload outright
    (400 `slug_immutable`), so a rename with no `slug` key is the only way
    to prove the address is frozen.
    """
    tok = seeded_app["admin_token"]
    client = seeded_app["client"]
    a = _create_agent(seeded_app, tok, name="Published Bot")  # v1's default status is already 'ready'

    r = client.put(
        f"/api/v1/agents/{a['id']}",
        json={"slug": "hijacked", "name": "Renamed Bot"},
        headers=_auth(tok),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "slug_immutable"

    r = client.put(f"/api/v1/agents/{a['id']}", json={"name": "Renamed Bot"}, headers=_auth(tok))
    assert r.status_code == 200
    assert r.json()["slug"] == "published-bot"


def test_agent_is_private_to_owner_until_shared(seeded_app):
    """Another user can neither list nor read an unshared agent (404, so the
    endpoint never confirms it exists)."""
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Secret Bot")
    other = _auth(seeded_app["analyst_token"])
    assert seeded_app["client"].get(f"/api/v1/agents/{a['id']}", headers=other).status_code == 404
    listed = seeded_app["client"].get("/api/v1/agents", headers=other).json()["data"]
    assert all(x["id"] != a["id"] for x in listed)


def test_shared_agent_becomes_readable_but_not_writable(seeded_app):
    """A grant conveys USE, not authorship: the grantee can read the agent but
    may not edit or delete it. This is also what makes agent grants real rather
    than decorative — the read path honours them."""
    c = seeded_app["client"]
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Team Bot")
    gid = _group_with_member("analyst1", "lib-agent-share-grp")

    r = c.put(f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "shared"

    other = _auth(seeded_app["analyst_token"])
    assert c.get(f"/api/v1/agents/{a['id']}", headers=other).status_code == 200
    listed = c.get("/api/v1/agents", headers=other).json()["data"]
    assert any(x["id"] == a["id"] and x["mine"] is False for x in listed)
    # Read-only for the grantee.
    assert c.put(f"/api/v1/agents/{a['id']}", json={"name": "Hijack"}, headers=other).status_code == 404
    assert c.delete(f"/api/v1/agents/{a['id']}", headers=other).status_code == 404


# ---------------------------------------------------------------------------
# Sharing API
# ---------------------------------------------------------------------------


def test_share_targets_include_everyone(seeded_app):
    r = seeded_app["client"].get("/api/sharing/groups", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    targets = r.json()
    assert targets, "expected at least the Everyone group"
    everyone = [t for t in targets if t["is_everyone"]]
    assert len(everyone) == 1
    assert "workspace" in everyone[0]["name"].lower()


def test_collection_share_cycles_private_shared_workspace(seeded_app):
    """The three visibility states are reachable and reversible through one
    idempotent PUT, for the artefact kind."""
    from src.db import SYSTEM_EVERYONE_GROUP

    c = seeded_app["client"]
    tok = seeded_app["admin_token"]
    col = _create_collection(seeded_app, "Shareable Deck", tok)

    assert c.get(f"/api/sharing/collection/{col['id']}", headers=_auth(tok)).json()["visibility"] == "private"

    gid = _group_with_member("analyst1", "lib-col-share-grp")
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok))
    assert r.json()["visibility"] == "shared"

    everyone = _bare_group(SYSTEM_EVERYONE_GROUP)
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [everyone]}, headers=_auth(tok))
    assert r.json()["visibility"] == "workspace"

    # Empty list = private again.
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=_auth(tok))
    assert r.json()["visibility"] == "private"
    assert r.json()["group_ids"] == []


def test_share_is_idempotent(seeded_app):
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]
    col = _create_collection(seeded_app, "Idem Deck", tok)
    gid = _group_with_member("analyst1", "lib-idem-grp")
    first = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok)).json()
    second = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok)).json()
    assert first["group_ids"] == second["group_ids"] == [gid]


def test_non_owner_cannot_read_or_change_sharing(seeded_app):
    """Sharing state of someone else's item is 404 — not 403 — so ownership
    can't be probed."""
    c = seeded_app["client"]
    col = _create_collection(seeded_app, "Not Yours", seeded_app["admin_token"])
    other = _auth(seeded_app["analyst_token"])
    assert c.get(f"/api/sharing/collection/{col['id']}", headers=other).status_code == 404
    assert c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=other).status_code == 404


def test_cannot_share_into_a_group_you_are_not_in(seeded_app):
    """Group containment: a non-admin owner may only target their own groups,
    so they can't push content at a team they aren't part of."""
    c = seeded_app["client"]
    tok = seeded_app["analyst_token"]
    col = _create_collection(seeded_app, "Analyst Own Deck", tok)
    foreign = _bare_group("lib-foreign-grp")
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [foreign]}, headers=_auth(tok))
    assert r.status_code == 403
    assert r.json()["detail"] == "group_not_shareable"


def test_owner_unshare_preserves_an_admin_grant(seeded_app):
    """An owner clearing their own sharing must not revoke a grant an admin
    made to a group the owner isn't in."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    c = seeded_app["client"]
    tok = seeded_app["analyst_token"]
    col = _create_collection(seeded_app, "Admin Granted Deck", tok)
    admin_grp = _bare_group("lib-admin-only-grp")
    grants = ResourceGrantsRepository(get_system_db())
    grants.create(group_id=admin_grp, resource_type="collection", resource_id=col["id"], assigned_by="admin")

    # The owner sets their own (empty) desired state.
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=_auth(tok))
    assert r.status_code == 200
    # The admin's grant survives, so the item is still shared.
    assert admin_grp in r.json()["group_ids"]
    assert r.json()["visibility"] == "shared"


def test_skill_is_not_grant_shareable(seeded_app):
    """Skills share via the Store (approved => readable by everyone), so the
    grant endpoint must refuse them instead of writing a dead grant row."""
    r = seeded_app["client"].get("/api/sharing/skill/anything", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404
    assert r.json()["detail"] == "resource_not_shareable"


def test_sharing_unknown_resource_is_404(seeded_app):
    r = seeded_app["client"].get("/api/sharing/agent/agt_missing", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Data apps — owner-shareable like collections and agents, keyed on the SLUG
# (grants on this type are slug-keyed everywhere: `data_apps._can_view` reads
# `can_access(uid, 'data_app', row['slug'])`). Rendering the Library row
# share-less had left /admin/access as the only sharing surface for the one
# kind a user builds from chat (Devin Review on #1272).
# ---------------------------------------------------------------------------


def _seed_data_app(monkeypatch, slug: str, name: str, owner: str) -> None:
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    from src.repositories import data_apps_repo

    data_apps_repo().create(slug=slug, name=name, owner_user_id=owner)


def test_data_app_owner_share_roundtrip_writes_the_slug_keyed_grant(seeded_app, monkeypatch):
    """A NON-admin owner shares their own app through the same PUT every
    other kind uses, and the grant lands on the slug — the key the proxy's
    `_can_view` actually reads. An id-keyed grant would be read by nothing."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    c = seeded_app["client"]
    tok = seeded_app["analyst_token"]
    _seed_data_app(monkeypatch, "shared-dash", "Shared Dash", "analyst1")

    assert c.get("/api/sharing/data_app/shared-dash", headers=_auth(tok)).json()["visibility"] == "private"

    gid = _group_with_member("analyst1", "lib-app-share-grp")
    r = c.put("/api/sharing/data_app/shared-dash", json={"group_ids": [gid]}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "shared"

    rows = [
        g for g in ResourceGrantsRepository(get_system_db()).list_all(resource_type="data_app") if g["group_id"] == gid
    ]
    assert [g["resource_id"] for g in rows] == ["shared-dash"], "the grant must be keyed on the slug"

    # The owner's own Library badge reflects the grant instead of claiming
    # "Private" right next to the control that just shared it.
    lib = c.get("/library", headers=_auth(tok)).text
    row_at = lib.index('data-item-id="shared-dash"')
    assert "Specific groups" in lib[row_at : lib.index("</tr>", row_at)]


def test_a_shared_data_app_appears_in_the_grantees_library_read_only(seeded_app, monkeypatch):
    """The grantee sees the app under "Shared with you" with the read-only
    badge — only the owner (or an admin) may change who can see it."""
    c = seeded_app["client"]
    _seed_data_app(monkeypatch, "granted-dash", "Granted Dash", "analyst1")
    gid = _group_with_member("analyst1", "lib-app-grantee-grp")
    _group_with_member("viewer1", "lib-app-grantee-grp")
    r = c.put(
        "/api/sharing/data_app/granted-dash", json={"group_ids": [gid]}, headers=_auth(seeded_app["analyst_token"])
    )
    assert r.status_code == 200, r.text

    lib = c.get("/library", headers=_auth(seeded_app["viewer_token"])).text
    assert "Granted Dash" in lib, "the grantee must see the shared app in their Library"
    row_at = lib.index('data-item-id="granted-dash"')
    row = lib[row_at : lib.index("</tr>", row_at)]
    assert "lib-vis--readonly" in row, "a grantee's row must not offer the Share control"
    assert 'data-share="granted-dash"' not in row


def test_data_app_sharing_is_owner_only(seeded_app, monkeypatch):
    """Someone else's app is 404 — not 403 — matching the collections
    contract, so ownership can't be probed."""
    c = seeded_app["client"]
    _seed_data_app(monkeypatch, "own-dash", "Own Dash", "analyst1")
    other = _auth(seeded_app["viewer_token"])
    assert c.get("/api/sharing/data_app/own-dash", headers=other).status_code == 404
    assert c.put("/api/sharing/data_app/own-dash", json={"group_ids": []}, headers=other).status_code == 404


def test_linked_app_sharing_is_admin_only_and_deliberate(seeded_app, monkeypatch):
    """A LINKED row (synthetic `system` owner) is shareable — by an admin
    only. This is a conscious decision, not a `_reject_linked` gap (Devin
    Review on #1321): sharing is visibility policy, the same `(data_app,
    <slug>)` grants `/admin/access` already lets admins write, not a
    runtime mutation like deploy/secrets/logs. No real user matches the
    `system` owner, so non-admins get the probe-safe 404."""
    from src.db import get_system_db
    from src.repositories import data_apps_repo
    from src.repositories.resource_grants import ResourceGrantsRepository

    c = seeded_app["client"]
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    data_apps_repo().create(slug="linked-dash", name="Linked Dash", owner_user_id="system", repo_mode="linked")

    # Non-admin: the synthetic owner matches nobody -> probe-safe 404.
    other = _auth(seeded_app["viewer_token"])
    assert c.get("/api/sharing/data_app/linked-dash", headers=other).status_code == 404
    assert c.put("/api/sharing/data_app/linked-dash", json={"group_ids": []}, headers=other).status_code == 404

    # Admin: same authority /admin/access grants, friendlier surface.
    gid = _group_with_member("viewer1", "lib-linked-share-grp")
    r = c.put(
        "/api/sharing/data_app/linked-dash",
        json={"group_ids": [gid]},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200, r.text
    rows = [
        g
        for g in ResourceGrantsRepository(get_system_db()).list_all(resource_type="data_app")
        if g["resource_id"] == "linked-dash"
    ]
    assert [g["group_id"] for g in rows] == [gid]


# ---------------------------------------------------------------------------
# The Library page
# ---------------------------------------------------------------------------


def test_library_lists_artefacts_with_visibility_but_not_agents(seeded_app):
    """One table for the caller's things — artefacts (and skills) with a type
    facet and a visibility chip. Agents are excluded: they live on /agents."""
    tok = seeded_app["admin_token"]
    _create_collection(seeded_app, "Quarterly Deck", tok)
    _create_agent(seeded_app, tok, name="Library Bot")

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "Quarterly Deck" in text
    assert 'data-kind="artefact"' in text
    # Visibility chip + the share affordance for the grant-backed artefact kind.
    assert "lib-vis--private" in text
    assert 'data-share-type="collection"' in text
    # The agent exists in the registry but is NOT a Library row.
    assert "Library Bot" not in text
    assert 'data-kind="agent"' not in text


def test_shared_agent_visibility_is_reported_by_the_sharing_api(seeded_app):
    """Agents aren't Library rows, so their shared state is asserted where it
    actually surfaces: the sharing API (and, for use, /api/v1/agents)."""
    tok = seeded_app["admin_token"]
    a = _create_agent(seeded_app, tok, name="Shared Bot")
    gid = _group_with_member("analyst1", "lib-vis-grp")
    seeded_app["client"].put(f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(tok))

    state = seeded_app["client"].get(f"/api/sharing/agent/{a['id']}", headers=_auth(tok)).json()
    assert state["visibility"] == "shared"
    assert gid in state["group_ids"]


def test_agent_shared_with_me_is_not_in_my_library(seeded_app):
    """A shared agent is usable via /api/v1/agents but must not leak into the
    grantee's Library listing."""
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Borrowed Bot")
    gid = _group_with_member("analyst1", "lib-borrow-grp")
    seeded_app["client"].put(
        f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(seeded_app["admin_token"])
    )
    other = _auth(seeded_app["analyst_token"])
    listed = seeded_app["client"].get("/api/v1/agents", headers=other).json()["data"]
    assert any(x["id"] == a["id"] for x in listed)
    assert "Borrowed Bot" not in seeded_app["client"].get("/library", headers=other).text


def test_library_offers_grid_view_toggle(seeded_app):
    """Table is the default view; a grid toggle + its projection target ship
    with the page (the shared .fbar-view control, as on My Stack)."""
    _create_collection(seeded_app, "View Toggle Deck", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'class="fbar-view"' in text
    assert 'data-view="table"' in text and 'data-view="grid"' in text
    assert 'aria-pressed="true"' in text  # table active by default
    # ONE table for the whole list (the groups are its tbodies), and a parallel
    # grid container holding one grid per group — a grid cannot nest in a tbody,
    # so the two views are separate structures wearing the same group headers.

    # One table + one grid per GROUP, both inside the group's own section, so the
    # view toggle and the grouping compose (there is no page-level pair).
    groups = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert groups
    assert text.count('class="lib-tablewrap"') == len(groups)
    assert text.count('class="fbar-grid"') == len(groups)


def test_library_add_actions_live_behind_one_menu(seeded_app):
    """A single "+ Add" chevron button fronts every add path; there is no
    separate per-kind button in the header, and no PERSONAL-agent entry.

    An agent template is a Library kind and does have an entry (renamed from
    "shareable agent" in AGT-4); a personal agent still does not, and lives on
    /agents. The two labels share a prefix, so the negative match is on the
    closing tag.
    """
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="lib-new-btn"' in text
    assert 'id="lib-new-menu"' in text
    # `>label<` rather than `<span>label</span>`: each item carries a `<small>`
    # description after its label, so the label no longer ends the span.
    for label in ("Build a skill", "Build a plugin", "Build an agent template", "Upload a file"):
        assert f">{label}<" in text
    assert ">Build an agent<" not in text
    # Every row goes to the one builder at /skills, so no row is marked WIP.
    assert "lib-wip" not in text
    # The connect banner is a page-level note under the header, never inside
    # the header row itself.
    assert 'class="cbn cbn--bar"' in text
    head = text.split('class="lib-head"', 1)[1].split("</div>", 1)[0]
    assert 'class="cbn cbn--bar"' not in head


def test_search_and_new_ride_the_toolbar(seeded_app):
    """Search and "+ Add" ride the BROWSING BLOCK that sits on the list —
    search first in the controls row, "+ Add" last — not the page header.

    Everything that narrows or adds to the list is one job, so it is one block,
    directly above what it acts on: controls, then the line stating what they
    did (the count and the active-filter chips), then the list. The two were
    split across the page for a while — Filter in the page header, its chips two
    bands lower, the count in a third place — which meant pressing a control and
    reading its effect happened in different parts of the page.
    """
    # One item, so the type sections actually render — they are what bounds the
    # count row below (an empty Library renders no `data-lib-sec`).
    _create_collection(seeded_app, "Row Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    # The page header holds the title, the banner and the tabs. The controls do
    # not live there, and there is no leftover action row for a script to ferry.
    head = text.split('class="lib-head"', 1)[1].split('class="lib-browse"', 1)[0]
    assert 'id="lib-tabs"' in head
    assert 'id="lib-search"' not in head
    assert 'id="lib-new-btn"' not in head
    assert 'class="lib-actions"' not in text

    # The browsing block holds all of it, in order.
    browse = text.split('class="lib-browse"', 1)[1].split('<div class="lib-list">', 1)[0]
    for kept in ('id="lib-search"', 'id="lib-new-btn"', 'id="lib-item-count"', 'id="lib-chips"'):
        assert kept in browse, kept

    # The controls row carries both, at its two ends, with the list controls
    # between them. Anchored on the bar's aria-label rather than its class list:
    # the class carries opt-in modifiers (`fbar--ranked`, the rank treatment
    # shared with /chats) that this test has no view on.
    bar = text.split('aria-label="Search, filter and sort library"', 1)[1].split('id="lib-tabs"', 1)[0]
    for kept in (
        'id="lib-search"',
        'id="lib-filter-btn"',
        'id="lib-sort"',
        'class="fbar-view"',
        'id="lib-new-btn"',
        'id="lib-new-menu"',
    ):
        assert kept in bar
    assert bar.index('id="lib-search"') < bar.index('id="lib-filter-btn"')
    assert bar.index('class="fbar-view"') < bar.index('id="lib-new-btn"')
    # The search box keeps its own landmark now that the bar around it is a
    # `group` rather than a `search`.
    assert 'class="fbar__search" role="search"' in bar
    # The bar takes no `--center`: search is a flex control, so there is no free
    # space for centring to distribute. The chips row does not centre either:
    # `--center` belonged to the dock's card, where the row sat over a centred
    # bar. Above a left-aligned list it left the chips floating mid-page with
    # nothing to align to.
    assert 'class="fbar fbar--center"' not in text
    assert "fbar-chips--center" not in text
    assert 'class="fbar-chips" id="lib-chips"' in text

    # The state line carries the count and the chips — not the controls.
    row = text.split('class="lib-browse__state"', 1)[1].split('<div class="lib-list">', 1)[0]
    assert 'id="lib-item-count"' in row
    assert 'id="lib-chips"' in row
    assert 'id="lib-new-btn"' not in row

    # Page order: title → tabs → controls → count+chips → groups. Everything
    # that acts on the list is contiguous and adjacent to it.
    assert text.index('class="lib-head__bar"') < text.index('id="lib-tabs"')
    assert text.index('id="lib-tabs"') < text.index('class="lib-browse"')
    assert text.index('id="lib-filter-btn"') < text.index('id="lib-chips"')
    assert text.index('id="lib-chips"') < text.index('<div class="lib-list">')
    # The chips now FOLLOW the controls that produced them, because they moved
    # to the list they describe: inside the old dock they had to sit above the
    # bar (a card whose two rows read top-down); beside the list they read as
    # the list's current narrowing, which is what they are.
    assert text.index('id="lib-tabs"') < text.index('aria-label="Search, filter and sort library"')
    assert text.index('class="lib-browse"') < text.index("data-lib-sec=")


FILTER_TOOLBAR_CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "css" / "filter_toolbar.css"


def _css_rule(sheet: str, selector: str, containing: str = "") -> str:
    """The declaration block of the rule matching ``selector``, and ``containing``.

    Splits on braces rather than searching for ``f"{selector} {{"``: the veil's
    three layers share one comma-separated box rule, so a plain string search for
    ``.fbar-dock__veil::after {`` matches that rule's last selector line instead
    of the layer's own block. ``containing`` then picks between the several rules
    a selector legitimately has — the shared box versus that layer's own blur.
    """
    for chunk in sheet.split("}"):
        if "{" not in chunk:
            continue
        head, body = chunk.rsplit("{", 1)
        head = re.sub(r"/\*.*?\*/", "", head, flags=re.S)
        if selector in {s.strip() for s in head.split(",")} and containing in body:
            return body
    raise AssertionError(f"no rule for {selector!r} containing {containing!r}")


def test_page_header_carries_the_controls_and_the_bands_own_the_top(seeded_app):
    """The Library's toolbar pins to the TOP of the list, and is not the shared
    floating dock any more.

    The dock (`.fbar-dock`, still /chats's) solved the right problem — a caller
    at row 200 needs search and "+ Add" as much as one at row 1, and a page
    header scrolls away — but it solved it by floating over the foot of the
    page. That cost the last two rows on every screen (the veil alone reserves
    ~136px) and it put the controls BELOW the list they narrow. Sticky keeps the
    reach and drops both costs.

    Two things are pinned here because each one silently breaks something a
    caller can see:

      * chips row first, controls row second, inside one `.lib-toolbar`;
      * `top: 0`, so it holds the same edge the group bands pin under — a
        toolbar that scrolled away would put the header problem back.
    """
    _create_collection(seeded_app, "Dock Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    # Page furniture, then the browsing block, then the list.
    head = text.split('class="lib-head"', 1)[1].split('<div class="lib-list">', 1)[0]
    assert head.index('class="lib-head__titles"') < head.index('id="lib-tabs"')
    assert head.index('id="lib-tabs"') < head.index('class="lib-browse"')
    assert head.index('class="lib-browse"') < head.index('id="lib-chips"')

    # The HEADER does not pin — the group bands do. A pinned header of title +
    # count + banner + controls + tabs runs ~270px, a third of an 800px
    # viewport, and it leaves a band no room to travel in: a section shorter
    # than the space above it is pushed straight back out by its own bottom, so
    # the list reads as sliding under a wall. The band at the viewport top is
    # what actually keeps a long list readable.
    head_rule = text.split(".library-page .idx-head {", 1)[1].split("}", 1)[0]
    assert "position: sticky" not in head_rule
    band_rule = text.split(".lib-band { position: sticky;", 1)[1].split("}", 1)[0]
    assert "top: 0;" in band_rule

    # The dock is gone from THIS page — markup, veil and the card the retired
    # resize animation observed.
    assert 'class="fbar-dock"' not in text
    assert "fbar-dock__veil" not in text
    assert "fbar-dock__card" not in text

    # The dock is gone from THIS page — markup, veil and the card the retired
    # resize animation observed.
    assert 'class="fbar-dock"' not in text
    assert "fbar-dock__veil" not in text
    assert "fbar-dock__card" not in text

def test_library_title_carries_no_setup_caveat(seeded_app):
    """The caveat never rides the TITLE: no `.pnote` panel under the lede, and no
    status pill beside the `h1`. The page opens on its own name and inventory.

    Narrowed from "this copy appears nowhere on the page". The Library now carries
    a deliberate, TEMPORARY product statement in `.lib-headnotes`
    (``library_prep_warning()`` in ``library.html``) — an amber banner telling
    every reader the inventory is still being filled. That is a product decision,
    made knowingly, and it reverses the earlier one this test used to enforce:
    a standing condition does not earn a marker next to the h1.

    Both halves of that reasoning still hold, and both are still asserted — the
    banner is a sibling of the lede, not an ornament on the title. What is no
    longer asserted is the blanket copy ban, which cannot distinguish "a pill
    stapled to the heading" from "a banner in the notes stack". When the content
    backlog clears, delete ``library_prep_warning()`` and its call site; nothing
    else depends on it.
    """
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    # Neither the panel nor the pill that replaced it — markup, CSS and JS.
    assert 'class="pnote"' not in text
    assert "lib-status" not in text
    # The title stands alone, directly ahead of the lede: nothing is stapled
    # INSIDE the h1. Matched with a pattern rather than the literal
    # `<h1>Library</h1>`, because the heading legitimately carries the shared
    # `page-title` class now (#1915) — what must stay true is that its content
    # is the name and nothing else, which is the property the old literal was
    # really expressing.
    assert re.search(r"<h1[^>]*>Library</h1>", text), (
        "the Library h1 must contain the page name alone — a pill or badge "
        "inside it is what this guards against"
    )
    # The title stands alone. The prose lede that used to follow it is gone —
    # what it became is the count, and the count belongs on the list.
    assert 'class="lede"' not in text
    # Where the note lives now: beside the item count, as list metadata. Not a
    # panel, not a row above the list, and not a pill on the h1 (the two bans
    # above still catch that).
    assert 'class="lib-count-note"' in text
    assert ">More coming soon<" in text
    assert text.index('class="lib-browse__state"') < text.index('class="lib-count-note"')
    # It reads as GROWTH, not as a caveat: no warn vocabulary, no "incomplete",
    # and no cue to explain itself away. The Library is being filled, which is
    # good news — dressing good news in amber is what the old versions got wrong.
    assert "lib-strip" not in text
    assert "lib-soon" not in text
    # Copy checks run against the VISIBLE page: the page-local <style> block
    # documents the wording this replaced, and a whole-document substring search
    # would match that commentary rather than anything a reader sees.
    body = re.sub(r"<(style|script)\b.*?</\1>", "", text, flags=re.S | re.I)
    assert "may be incomplete" not in body
    assert "still being prepared" not in body
    assert "What this means" not in body
    # The elaboration is reachable without hover: fast tooltip + accessible name.
    note = text[text.index('class="lib-count-note"') :]
    note = note[: note.index("</span>")]
    assert "data-tip=" in note
    assert "aria-label=" in note


def test_more_coming_note_is_a_sibling_of_the_count_not_a_child(seeded_app):
    """The note sits BESIDE `#lib-item-count`, never inside it.

    Not a style preference — a correctness constraint. The shared filter toolbar
    rewrites that element's text on every facet change (`count: { el:
    '#lib-item-count', noun: 'item' }`), rendering e.g. "14 of 41 items". Anything
    nested inside the count is destroyed by the first click of a filter, and the
    failure is invisible server-side: the page ships correct HTML and loses the
    note only once the user interacts. So the structure is pinned here.
    """
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    count_at = text.index('id="lib-item-count"')
    note_at = text.index('class="lib-count-note"')
    assert count_at < note_at, "the note follows the count"

    # The count element closes BEFORE the note opens — i.e. they are siblings.
    count_close = text.index("</span>", count_at)
    assert count_close < note_at, (
        "the note is nested inside #lib-item-count; the filter toolbar's count "
        "rewrite will delete it on the first facet click"
    )
    # Both inside the header's count line, under the title.
    head_at = text.index('class="lib-head__titles"')
    assert head_at < count_at


def test_data_apps_schedule_badge_retired_when_the_kind_shipped(seeded_app):
    """The "Data apps coming soon" badge promised the kind would land in the
    Library — its own docstring said it "deletes itself when the kind ships."
    The kind shipped (the Data apps band, tests/test_web_library_data_apps.py),
    so a lingering badge would announce a roadmap the reader is looking at.
    The `fbar-group__soon` seam itself stays (`_SECTION_SOON` in the router)
    for the next kind on the roadmap; only the fulfilled entry is gone."""
    _create_collection(seeded_app, "Soon Badge Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    assert "Soon Badge Anchor" in text  # the Files band really rendered
    assert 'class="fbar-group__soon"' not in text
    assert "Data apps coming soon" not in text
    assert "Nothing to do yet." not in text
    # The old pre-badge panels stay gone too — markup and CSS both.
    assert "lib-soon" not in text
    assert "lib-apps" not in text


# ---------------------------------------------------------------------------
# The Library also lists what has been SHARED WITH the caller
# ---------------------------------------------------------------------------


def _grant(group_id: str, resource_type: str, resource_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    grants = ResourceGrantsRepository(get_system_db())
    if not grants.has_grant([group_id], resource_type, resource_id):
        grants.create(group_id=group_id, resource_type=resource_type, resource_id=resource_id, assigned_by="admin")


def _stock_domain(domain_id: str, item_id: str) -> None:
    """Put one approved item into a memory domain. The Library hides empty
    optional domains (the /corporate-memory _has_content rule moved in with
    the merge), so a domain that should LIST must have content."""
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        KnowledgeRepository(conn).create(
            id=item_id,
            title="Stock item",
            content="body",
            category="workflow",
            status="approved",
            source_user="contrib@example.com",
        )
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) VALUES (?, ?, 'test')",
            [item_id, domain_id],
        )
    finally:
        conn.close()


def test_library_lists_granted_resources_of_every_kind(seeded_app):
    """The Library answers "what do I have?" across kinds: the caller's own
    artefacts PLUS the governed data packages, memory domains and recipes
    granted to one of their groups."""
    from src.repositories import data_packages_repo, memory_domains_repo, recipes_repo

    gid = _group_with_member("analyst1", "lib-access-grp")
    pkg = data_packages_repo().create(
        name="Sales Data",
        slug="lib-sales",
        description="Governed sales tables",
        icon=None,
        color=None,
        created_by="admin",
    )
    dom = memory_domains_repo().create(
        name="Pricing Memory",
        slug="lib-pricing",
        description="How we price",
        icon=None,
        color=None,
        created_by="admin",
    )
    rec = recipes_repo().create(
        slug="lib-churn",
        title="Churn Recipe",
        description="How to compute churn",
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)
    _grant(gid, "memory_domain", dom)
    _grant(gid, "recipe", rec)
    _stock_domain(dom, "lib_pricing_item")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Sales Data" in text
    assert "Pricing Memory" in text
    assert "Churn Recipe" in text
    for type_key in ("data_package", "memory_domain", "recipe"):
        assert f'data-type="{type_key}"' in text
    # They arrived by a grant, so the Source facet says so...
    assert 'data-origin="granted"' in text
    # ...and they are tagged as shared with the caller, not owned by them.
    assert 'data-ownership="shared_with_me"' in text


def test_granted_rows_link_to_the_individual_item(seeded_app):
    """A Library row is ONE item, so it opens that item's page — never the
    generic listing. Memory rows used to hand every domain the same
    /corporate-memory href, which lost which row was clicked; ?source=library
    additionally makes the drill-down's back link return here."""
    from src.repositories import data_packages_repo, memory_domains_repo

    gid = _group_with_member("analyst1", "lib-drilldown-grp")
    pkg = data_packages_repo().create(
        name="Drilldown Data",
        slug="lib-drill-data",
        description=None,
        icon=None,
        color=None,
        created_by="admin",
    )
    dom = memory_domains_repo().create(
        name="Drilldown Memory",
        slug="lib-drill-memory",
        description=None,
        icon=None,
        color=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)
    _grant(gid, "memory_domain", dom)
    _stock_domain(dom, "lib_drill_item")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "/catalog/p/lib-drill-data" in text
    assert "/memory/d/lib-drill-memory?source=library" in text


def test_granted_resources_are_not_reshareable_by_the_caller(seeded_app):
    """A granted resource is an admin's to share, not the recipient's — so its
    row carries no Share action (only the explain-sharing affordance)."""
    from src.repositories import data_packages_repo

    gid = _group_with_member("analyst1", "lib-noreshare-grp")
    pkg = data_packages_repo().create(
        name="Locked Package",
        slug="lib-locked",
        description="d",
        icon=None,
        color=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Locked Package" in text
    # The data-package row is not owner-shareable.
    assert 'data-share-type="data_package"' not in text
    # And the sharing API refuses it outright (not a shareable type).
    r = seeded_app["client"].put(
        f"/api/sharing/data_package/{pkg}", json={"group_ids": []}, headers=_auth(seeded_app["analyst_token"])
    )
    assert r.status_code == 404


def test_library_excludes_resources_not_granted_to_me(seeded_app):
    """No grant → not in my Library (the page is access-scoped, not a catalogue
    of everything in the instance)."""
    from src.repositories import data_packages_repo

    data_packages_repo().create(
        name="Ungranted Package",
        slug="lib-ungranted",
        description="d",
        icon=None,
        color=None,
        created_by="admin",
    )
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Ungranted Package" not in text
