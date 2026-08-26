"""Task C1.1 — `/api/v1/agents*` absorbs every operation the `/agents`
builder surface provides, so C1.2 can delete the builder router.

Inventory (paste into the PR body too):

Builder op (`app/api/agents.py`)          v1 equivalent (`app/api/agents_admin.py`)   Delta closed here
--------------------------------------    ------------------------------------------  -----------------
GET /api/agents (owned + shared)          GET /api/v1/agents                          v1 was owned-only; now unions in
                                                                                       ResourceType.AGENT grants, same as
                                                                                       the builder's `_granted_agent_ids`.
GET /api/agents/{id} (owner/admin/grant)  GET /api/v1/agents/{id}                     v1's `_load_agent` denied a grantee
                                                                                       with 404; now allows READ (never
                                                                                       write) for a grantee too.
POST /api/agents (builder shape)          POST /api/v1/agents                         v1 lacked role/instructions/tone/
                                                                                       greeting/knowledge/plugins/surfaces/
                                                                                       status/template_entity_id and
                                                                                       required an explicit slug; all
                                                                                       added as OPTIONAL fields (`slug`
                                                                                       auto-derives when omitted,
                                                                                       `instructions` aliases the existing
                                                                                       `system_prompt`). knowledge/plugins
                                                                                       are pushed through the SAME
                                                                                       `app.api.agents._sync_builder_scope`
                                                                                       the builder uses — one scope-write
                                                                                       path either way.
PATCH /api/agents/{id} (builder shape)    PUT /api/v1/agents/{id}                     Same fields added to
                                                                                       `UpdateAgentRequest`; a
                                                                                       knowledge/plugins PATCH reuses
                                                                                       `_sync_builder_scope` +
                                                                                       `_hydrate_builder_axes` so
                                                                                       governance-owned rows (slack_channel,
                                                                                       table, connection) survive exactly
                                                                                       like the builder's own PATCH. The
                                                                                       draft-slug-follow rule
                                                                                       (`_draft_slug_rename`) is reused
                                                                                       too — inert for every pre-existing
                                                                                       v1 agent (always `status='ready'`).
DELETE /api/agents/{id}                   DELETE /api/v1/agents/{id}                  v1 already cascades
                                                                                       webhooks/artifacts/memories/
                                                                                       schedules + revokes agent PATs
                                                                                       (superset of the builder's own
                                                                                       delete); it was MISSING the
                                                                                       builder's `resource_grants`
                                                                                       cleanup — added, so a v1 delete no
                                                                                       longer orphans a sharing grant.
Share-state read/write                    N/A — generic `/api/sharing/{type}/{id}`    Not an `/api/agents` route; already
(no `/api/agents` route; UI calls         (unchanged)                                  shared by both surfaces, keyed by
`/api/sharing/agent/{id}`)                                                             resource_type/resource_id. No work
                                                                                       needed here.
Template prefill (`template_entity_id`    Same field accepted by                      `_template_prefill` (unchanged,
 on POST, not a separate route)           `CreateAgentRequest`, reuses                 imported) is agent-model
                                           `app.api.agents._template_prefill`          independent of the calling router.

Deliberately NOT done here (noted, not silently dropped):
- Builder allows creating a fully blank (unnamed) draft; v1 keeps `name`
  required (400 `invalid_name`) — a caller wanting that flow supplies a
  placeholder name client-side. This is a C1.2 (UI re-pointing) concern, not
  an API round-trip gap for a NAMED create.
- v1 does not invent a `surfaces` default (`{"web": true}`) the way the
  builder's create does — omitted `surfaces` keeps the existing v1 default
  (empty `{}`), so a pre-existing v1 client's behavior is untouched. C1.2's
  UI sends `surfaces` explicitly.
- No `mine`/`created_by`/`updated` response aliases: v1 already exposes the
  same information under `owner_user_id`/`created_at`/`updated_at`/
  `is_default` (not a delta the scout flagged).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _group_with_members(group_name: str, *user_ids: str) -> str:
    """Create ``group_name`` (if absent) and put every ``user_ids`` in it.

    ``PUT /api/sharing/agent/{id}`` enforces group containment on the
    CALLER (the owner sharing the agent), so the owner must be a member of
    any group they share into — not just the grantee who is meant to gain
    visibility.
    """
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    for user_id in user_ids:
        if not members.has_membership(user_id, grp["id"]):
            members.add_member(user_id, grp["id"], source="admin", added_by="test")
    conn.close()
    return grp["id"]


def _mint_user_pat(user: dict, *, surface: str = "all") -> str:
    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(user_id=user["id"], email=user["email"], token_id=token_id, typ="pat")
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name="test-pat",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        surface=surface,
    )
    return jwt_token


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="grantee1", email="grantee@test.com", name="Grantee")
    conn.close()

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner": {"id": "owner1", "email": "owner@test.com", "token": create_access_token("owner1", "owner@test.com")},
        "grantee": {
            "id": "grantee1",
            "email": "grantee@test.com",
            "token": create_access_token("grantee1", "grantee@test.com"),
        },
    }


def _grant_package(user_id: str, pkg_id: str) -> None:
    """Direct `resource_grants(resource_type='data_package', ...)` row for
    `user_id` — the write-gate (C2.1) requires a non-admin writer to
    currently hold whatever data_package they declare in `knowledge`."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("v1-parity-pkg-grants") or groups.create(
        name="v1-parity-pkg-grants", description="test", created_by="test"
    )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "data_package", pkg_id):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            assigned_by="test",
            requirement="required",
        )
    conn.close()


def _make_pkg(name: str, slug: str) -> str:
    """A data package, granted to `owner1` immediately — every test in this
    file declares it via `owner1`'s `knowledge`, and the write-gate (C2.1)
    requires that writer to already hold what they declare."""
    from src.repositories import data_packages_repo

    pkg_id = data_packages_repo().create(
        name=name, slug=slug, description=None, icon=None, color=None, created_by="test"
    )
    _grant_package("owner1", pkg_id)
    return pkg_id


# ---------------------------------------------------------------------------
# Create: builder-shape payload round-trips through v1
# ---------------------------------------------------------------------------


def test_v1_create_accepts_builder_fields_and_round_trips(env):
    r = env["client"].post(
        "/api/v1/agents",
        json={
            "name": "Revenue Analyst",
            "role": "Finance analyst",
            "instructions": "Be precise.",
            "tone": "warm",
            "greeting": "Hi there",
            "status": "draft",
        },
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "Finance analyst"
    assert body["instructions"] == "Be precise."
    assert body["system_prompt"] == "Be precise."
    assert body["tone"] == "warm"
    assert body["greeting"] == "Hi there"
    assert body["status"] == "draft"
    # Builder auto-derives a slug from the name when the caller omits one.
    assert body["slug"] == "revenue-analyst"

    got = env["client"].get(f"/api/v1/agents/{body['id']}", headers=_auth(env["owner"]["token"]))
    assert got.status_code == 200
    assert got.json()["role"] == "Finance analyst"
    assert got.json()["instructions"] == "Be precise."


def test_v1_create_without_builder_fields_keeps_existing_defaults(env):
    """No behavior change for a plain, pre-existing-shape v1 create."""
    created = (
        env["client"]
        .post("/api/v1/agents", json={"name": "Plain", "slug": "plain-agent"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    assert created["status"] == "ready"
    assert created["surfaces"] == {}
    assert created["knowledge"] == []
    assert created["plugins"] == []
    assert created["role"] == ""
    assert created["tone"] == "concise"
    assert created["instructions"] == ""


def test_v1_create_knowledge_and_plugins_produce_the_expected_scope_rows(env):
    """v1's `knowledge`/`plugins` create fields go through the SAME
    `_sync_builder_scope` mapping the (now-deleted, Task C1.2) `/agents`
    builder router used — a declaration lands in `agent_scope`, and every
    mode axis defaults to `'selected'`, exactly as the builder's own create
    used to produce."""
    from src.repositories import agents_repo

    pkg_id = _make_pkg("Pkg", "v1-parity-pkg")

    via_v1 = env["client"].post(
        "/api/v1/agents",
        json={"name": "V1 Agent", "slug": "v1-agent", "knowledge": [pkg_id], "plugins": ["plug-a"]},
        headers=_auth(env["owner"]["token"]),
    )
    assert via_v1.status_code == 201, via_v1.text

    repo = agents_repo()
    scope_v1 = {(i["item_type"], i["item_id"]) for i in repo.get_scope(via_v1.json()["id"])}
    assert scope_v1 == {("data_package", pkg_id), ("plugin", "plug-a")}

    row_v1 = repo.get_by_id(via_v1.json()["id"])
    for field in ("tables_mode", "plugins_mode", "connections_mode", "memory_mode"):
        assert row_v1[field] == "selected"

    # And the v1 response itself shows the decoded declaration, not the
    # opaque JSON text the underlying column stores it as.
    assert via_v1.json()["knowledge"] == [pkg_id]
    assert via_v1.json()["plugins"] == ["plug-a"]


def test_v1_create_surfaces_round_trips_as_structured_json(env):
    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Surf", "slug": "surf-agent", "surfaces": {"web": True, "slack": False}},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    assert created["surfaces"] == {"web": True, "slack": False}


# ---------------------------------------------------------------------------
# Update: builder-shape PATCH-equivalent (PUT) round-trips + rescopes
# ---------------------------------------------------------------------------


def test_v1_update_builder_fields_round_trip_and_rescope_preserves_governance_rows(env):
    from src.repositories import agents_repo

    pkg1 = _make_pkg("P1", "v1-upd-pkg1")
    pkg2 = _make_pkg("P2", "v1-upd-pkg2")

    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Upd Agent", "slug": "upd-agent", "knowledge": [pkg1]},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )

    # A governance-set slack_channel binding must survive an unrelated
    # builder-shape PUT — mirrors app/api/agents.py's own contract.
    existing = [(i["item_type"], i["item_id"]) for i in agents_repo().get_scope(created["id"])]
    agents_repo().set_scope(created["id"], existing + [("slack_channel", "C1")])

    r = env["client"].put(
        f"/api/v1/agents/{created['id']}",
        json={"knowledge": [pkg2], "tone": "playful", "greeting": "Yo"},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tone"] == "playful"
    assert body["greeting"] == "Yo"
    assert body["knowledge"] == [pkg2]

    scope = {(i["item_type"], i["item_id"]) for i in agents_repo().get_scope(created["id"])}
    assert ("data_package", pkg2) in scope
    assert ("data_package", pkg1) not in scope, "replaced, not merged"
    assert ("slack_channel", "C1") in scope, "governance-owned row must survive"


def test_v1_update_knowledge_forces_all_four_modes_to_selected_from_all(env):
    """Reopened half of #1520: a knowledge/plugins PUT must force every mode
    axis the caller did not explicitly set to 'selected', not merely rewrite
    the declared knowledge/plugins JSON. The seeded default agent is born at
    mode='all' on all four axes (`agents_repo().get_or_create_default`) and
    is reachable through this route — only DELETE is guarded against it
    (`agents_admin.delete_agent`'s `default_agent_undeletable`). Narrowing
    its `knowledge` without also narrowing `tables_mode` etc. would leave the
    declared scope cosmetic: `src.agent_scope_intersection` still passes the
    owner's WHOLE stack through for any axis still sitting at 'all'."""
    from src.repositories import agents_repo

    repo = agents_repo()
    default_agent = repo.get_or_create_default(env["owner"]["id"])
    for field in ("plugins_mode", "connections_mode", "tables_mode", "memory_mode"):
        assert default_agent[field] == "all", "test assumption: seeded default starts at 'all'"

    pkg_id = _make_pkg("Pkg", "v1-default-narrow-pkg")

    r = env["client"].put(
        f"/api/v1/agents/{default_agent['id']}",
        json={"knowledge": [pkg_id]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 200, r.text

    row = repo.get_by_id(default_agent["id"])
    for field in ("plugins_mode", "connections_mode", "tables_mode", "memory_mode"):
        assert row[field] == "selected", (
            f"{field} must be forced to 'selected' by a knowledge/plugins PUT, exactly "
            f"like the /agents builder's own PATCH (app.api.agents.update_agent) — "
            f"got {row[field]!r} instead, the declared scope is cosmetic"
        )


def test_v1_update_forces_modes_from_all_to_selected_on_any_pre_existing_row(env):
    """Same property as the test above, for an agent that reached mode='all'
    on all four axes some OTHER way than being the seeded default — any
    pre-scope-enforcement row has this shape, and a knowledge/plugins PUT
    must narrow it exactly like the (now-deleted, Task C1.2) `/agents`
    builder router's own PATCH used to."""
    from src.repositories import agents_repo

    repo = agents_repo()
    pkg_id = _make_pkg("Pkg", "v1-parity-modes-pkg")
    all_modes = {
        "plugins_mode": "all",
        "connections_mode": "all",
        "tables_mode": "all",
        "memory_mode": "all",
    }

    via_v1 = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "V1 All", "slug": "v1-all-modes-agent"},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    repo.update(via_v1["id"], **all_modes)

    r_v1 = env["client"].put(
        f"/api/v1/agents/{via_v1['id']}",
        json={"knowledge": [pkg_id]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r_v1.status_code == 200, r_v1.text

    row_v1 = repo.get_by_id(via_v1["id"])
    for field in ("plugins_mode", "connections_mode", "tables_mode", "memory_mode"):
        assert row_v1[field] == "selected", (
            f"{field}={row_v1[field]!r} — a knowledge/plugins PUT must force an unset "
            "mode axis to 'selected', or the declared scope is cosmetic"
        )


def test_v1_update_surfaces_round_trips(env):
    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Surf2", "slug": "surf-agent-2", "surfaces": {"web": True}},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    updated = (
        env["client"]
        .put(f"/api/v1/agents/{created['id']}", json={"surfaces": {"web": False}}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    assert updated["surfaces"] == {"web": False}


def test_v1_draft_rename_moves_slug_and_freezes_on_ready(env):
    """Mirrors `app/api/agents.py::_draft_slug_rename` — inert for every
    pre-existing v1 agent (always `status='ready'`), live for one created
    `status='draft'` through the new builder-shape create."""
    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Draft One", "status": "draft"},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    assert created["slug"] == "draft-one"

    renamed = (
        env["client"]
        .put(f"/api/v1/agents/{created['id']}", json={"name": "Draft Two"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    assert renamed["slug"] == "draft-two"

    published = env["client"].put(
        f"/api/v1/agents/{created['id']}", json={"status": "ready"}, headers=_auth(env["owner"]["token"])
    )
    assert published.status_code == 200

    frozen = (
        env["client"]
        .put(f"/api/v1/agents/{created['id']}", json={"name": "Draft Three"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    assert frozen["slug"] == "draft-two", "slug freezes once the agent is no longer a draft"


# ---------------------------------------------------------------------------
# Read visibility: list/get shared-with-me (ResourceType.AGENT grant via a
# group), matching the builder's `_granted_agent_ids` reach.
# ---------------------------------------------------------------------------


def test_v1_get_agent_still_404_for_unrelated_user(env):
    created = (
        env["client"]
        .post("/api/v1/agents", json={"name": "Private", "slug": "private-agent"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    r = env["client"].get(f"/api/v1/agents/{created['id']}", headers=_auth(env["grantee"]["token"]))
    assert r.status_code == 404


def test_v1_get_agent_visible_to_grantee_but_not_writable(env):
    created = (
        env["client"]
        .post("/api/v1/agents", json={"name": "Shared", "slug": "shared-agent"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    gid = _group_with_members("v1-parity-share-grp", env["owner"]["id"], env["grantee"]["id"])
    share = env["client"].put(
        f"/api/sharing/agent/{created['id']}", json={"group_ids": [gid]}, headers=_auth(env["owner"]["token"])
    )
    assert share.status_code == 200, share.text

    got = env["client"].get(f"/api/v1/agents/{created['id']}", headers=_auth(env["grantee"]["token"]))
    assert got.status_code == 200, got.text

    denied = env["client"].put(
        f"/api/v1/agents/{created['id']}", json={"name": "hijack"}, headers=_auth(env["grantee"]["token"])
    )
    assert denied.status_code == 404


def test_v1_list_includes_shared_with_me(env):
    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Shared List", "slug": "shared-list-agent"},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    gid = _group_with_members("v1-parity-list-grp", env["owner"]["id"], env["grantee"]["id"])
    env["client"].put(
        f"/api/sharing/agent/{created['id']}", json={"group_ids": [gid]}, headers=_auth(env["owner"]["token"])
    )

    r = env["client"].get("/api/v1/agents", headers=_auth(env["grantee"]["token"]))
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()["data"]}
    assert created["id"] in ids


def test_v1_list_shared_with_me_visible_via_user_pat(env):
    """Read-only PAT gating (B4) must cover the shared-agent reach too."""
    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "Shared PAT", "slug": "shared-pat-agent"},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    gid = _group_with_members("v1-parity-pat-grp", env["owner"]["id"], env["grantee"]["id"])
    env["client"].put(
        f"/api/sharing/agent/{created['id']}", json={"group_ids": [gid]}, headers=_auth(env["owner"]["token"])
    )

    pat = _mint_user_pat(env["grantee"])
    r = env["client"].get("/api/v1/agents", headers=_auth(pat))
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()["data"]}
    assert created["id"] in ids


# ---------------------------------------------------------------------------
# PAT-vs-session gating matrix on the extended (mutation) fields — a plain
# user PAT must stay denied on every mutation, builder-shaped payload or not.
# ---------------------------------------------------------------------------


def test_v1_create_with_builder_fields_denied_for_pat(env):
    pat = _mint_user_pat(env["owner"])
    r = env["client"].post("/api/v1/agents", json={"name": "X", "role": "R"}, headers=_auth(pat))
    assert r.status_code == 403


def test_v1_update_with_builder_fields_denied_for_pat(env):
    created = (
        env["client"]
        .post("/api/v1/agents", json={"name": "Y", "slug": "pat-update-denied"}, headers=_auth(env["owner"]["token"]))
        .json()
    )
    pat = _mint_user_pat(env["owner"])
    r = env["client"].put(f"/api/v1/agents/{created['id']}", json={"tone": "warm"}, headers=_auth(pat))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Delete: builder's resource_grants cleanup, absorbed into v1's cascade.
# ---------------------------------------------------------------------------


def test_v1_delete_cleans_up_resource_grants(env):
    from src.repositories import resource_grants_repo

    created = (
        env["client"]
        .post(
            "/api/v1/agents",
            json={"name": "To Delete", "slug": "to-delete-grant"},
            headers=_auth(env["owner"]["token"]),
        )
        .json()
    )
    gid = _group_with_members("v1-parity-del-grp", env["owner"]["id"], env["grantee"]["id"])
    env["client"].put(
        f"/api/sharing/agent/{created['id']}", json={"group_ids": [gid]}, headers=_auth(env["owner"]["token"])
    )
    assert resource_grants_repo().list_resource_ids_for_user(env["grantee"]["id"], "agent") == [created["id"]]

    d = env["client"].delete(f"/api/v1/agents/{created['id']}", headers=_auth(env["owner"]["token"]))
    assert d.status_code == 204

    assert resource_grants_repo().list_resource_ids_for_user(env["grantee"]["id"], "agent") == []
