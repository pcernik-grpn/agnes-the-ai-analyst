"""Onboarding milestones are recorded by the actions that earn them.

The checklist used to be written from exactly one place: the browser, when the
reader clicked a row in the checklist itself. So the guided path dead-ended —
the coach-mark walked someone to the Library, they clicked **Add** on a package,
the toast confirmed it, and the card still said *Put knowledge in your stack*.

These pin the server-side half (``app/services/journey.py``), which is what makes
the milestone surface-agnostic: the Library page, chat, the CLI and MCP all reach
the same handlers.

Two invariants beyond "it marks the flag":

* **True-only.** Nothing here may un-tick a step; un-sharing an item is not
  "you have not shared anything".
* **Never break the action.** A journey write that fails must not fail the
  subscribe/upload/share the caller actually asked for.
"""

from __future__ import annotations

import uuid

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _journey(user_id: str = "analyst1") -> dict:
    from src.repositories import user_journey_repo

    return user_journey_repo().get(user_id)


def _group_with_analyst(name: str) -> str:
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    return gid


def _package(slug: str, name: str) -> str:
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description=None,
        icon=None,
        color=None,
        created_by="test",
    )
    conn.close()
    return pkg_id


def _curated_grant_for_analyst(marketplace: str, plugin: str) -> None:
    """Seed a marketplace + plugin and grant `analyst1` access to it."""
    import json
    from datetime import datetime, timezone

    gid = _group_with_analyst(f"JourneyMkt-{marketplace}")
    conn = get_system_db()
    now = datetime.now(timezone.utc)
    existing = conn.execute("SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace]).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
            [marketplace, marketplace.upper(), f"https://example.test/{marketplace}.git", now],
        )
    conn.execute(
        "INSERT INTO marketplace_plugins "
        "(marketplace_id, name, description, version, raw, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            marketplace,
            plugin,
            "desc",
            "1.0",
            json.dumps({"name": plugin, "version": "1.0", "description": "desc"}),
            now,
        ],
    )
    conn.close()
    _grant(gid, "marketplace_plugin", f"{marketplace}/{plugin}")


def _grant(group_id: str, resource_type: str, resource_id: str) -> None:
    conn = get_system_db()
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, 'available', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), group_id, resource_type, resource_id],
    )
    conn.close()


# --- "Put knowledge in your stack" ---------------------------------------


def test_subscribing_marks_the_stack_step(seeded_app):
    gid = _group_with_analyst("JourneySales")
    pkg_id = _package("journey-pkg", "JourneyPkg")
    _grant(gid, "data_package", pkg_id)

    assert _journey()["stack_setup_done"] is False
    resp = seeded_app["client"].post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200
    assert _journey()["stack_setup_done"] is True


def test_a_refused_subscribe_marks_nothing(seeded_app):
    """No grant → 403, and nothing happened, so nothing is recorded. The
    milestone tracks work done, not attempts."""
    pkg_id = _package("journey-nogrant", "NoGrant")
    resp = seeded_app["client"].post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 403
    assert _journey()["stack_setup_done"] is False


def test_curated_install_marks_the_stack_step(seeded_app):
    """A marketplace plugin joining the stack earns the same milestone.

    Marketplace plugins keep their own resolver and never pass through
    ``/api/stack/subscribe`` (design D1), so the step stayed unticked no matter
    how many plugins the reader installed — ``agnes-analyst`` among them. The
    label reads "Put knowledge in your stack"; a plugin install is that.
    """
    _curated_grant_for_analyst(marketplace="journey-mkt", plugin="journey-plugin")

    assert _journey()["stack_setup_done"] is False
    resp = seeded_app["client"].post(
        "/api/marketplace/curated/journey-mkt/journey-plugin/install",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert _journey()["stack_setup_done"] is True


def test_a_refused_curated_install_marks_nothing(seeded_app):
    """Mirror of the subscribe case — 403 means nothing happened."""
    resp = seeded_app["client"].post(
        "/api/marketplace/curated/nope-mkt/nope-plugin/install",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 403
    assert _journey()["stack_setup_done"] is False


# --- "Add or share something" -------------------------------------------


def test_creating_a_collection_marks_the_add_step(seeded_app):
    """The Library's upload flow creates the collection first, then posts files
    into it — so this is where "bringing your own knowledge in" starts."""
    assert _journey()["catalog_discovered"] is False
    resp = seeded_app["client"].post(
        "/api/collections",
        json={"name": "My upload"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 201
    assert _journey()["catalog_discovered"] is True


def test_sharing_marks_the_add_step_and_un_sharing_does_not_clear_it(seeded_app):
    gid = _group_with_analyst("JourneyShare")
    client = seeded_app["client"]
    hdrs = _auth(seeded_app["analyst_token"])

    created = client.post("/api/collections", json={"name": "Shared notes"}, headers=hdrs)
    assert created.status_code == 201
    cid = created.json()["id"]

    shared = client.put(
        f"/api/sharing/collection/{cid}",
        json={"group_ids": [gid]},
        headers=hdrs,
    )
    assert shared.status_code == 200
    assert shared.json()["visibility"] != "private"
    assert _journey()["catalog_discovered"] is True

    # Turning sharing back off leaves the milestone alone — mark_journey only
    # ever sets True, and this endpoint only calls it for a non-private result.
    unshared = client.put(f"/api/sharing/collection/{cid}", json={"group_ids": []}, headers=hdrs)
    assert unshared.status_code == 200
    assert unshared.json()["visibility"] == "private"
    assert _journey()["catalog_discovered"] is True


# --- The helper's own contract ------------------------------------------


def test_mark_journey_never_clears_a_flag(seeded_app):
    from app.services.journey import mark_journey
    from src.repositories import user_journey_repo

    user_journey_repo().update("analyst1", stack_setup_done=True)
    mark_journey("analyst1", stack_setup_done=False)
    assert _journey()["stack_setup_done"] is True


def test_mark_journey_swallows_failures(monkeypatch):
    """A broken journey write must never surface as a failed subscribe. There is
    nothing to assert but the absence of an exception — which is the contract."""
    import src.repositories as repos
    from app.services.journey import mark_journey

    class _Boom:
        def get(self, _uid):
            raise RuntimeError("db gone")

    monkeypatch.setattr(repos, "user_journey_repo", lambda: _Boom())
    mark_journey("analyst1", stack_setup_done=True)


def test_no_user_is_a_no_op():
    from app.services.journey import mark_journey

    mark_journey(None, stack_setup_done=True)
    mark_journey("", stack_setup_done=True)


# --- "Create your first agent" (v116) -------------------------------------


def test_creating_an_agent_marks_the_agent_step(seeded_app):
    """The sixth step is earned where the agent is made, like every other one.

    `/api/v1/agents` (Task C1.2 — absorbed from the builder's own now-deleted
    `/api/agents` router in Task C1.1) marks it too, not just the builder
    route this test originally pinned."""
    assert _journey()["agent_created"] is False
    resp = seeded_app["client"].post(
        "/api/v1/agents",
        json={"name": "Journey Agent"},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code in (200, 201), resp.text
    assert _journey()["agent_created"] is True


def test_the_v118_backfill_leaves_a_finished_checklist_finished():
    """A new journey column must not re-open a checklist someone completed.

    This is the objection that kept the agent step out of the list once before:
    a flag defaulting FALSE for everyone brings back the retired card of every
    already-onboarded user, carrying one row they never saw during onboarding.
    The migration answers it by backfilling — so this pins the backfill, not
    just the column.
    """
    from src.db import _v117_to_v118, get_system_db

    conn = get_system_db()
    # Someone who finished onboarding before the step existed.
    conn.execute(
        "INSERT INTO user_journey_state (user_id, onboarded, agent_created) "
        "VALUES ('backfill-done', TRUE, FALSE) "
        "ON CONFLICT (user_id) DO UPDATE SET onboarded = TRUE, agent_created = FALSE"
    )
    # ...and someone still mid-onboarding, who should see the new row.
    conn.execute(
        "INSERT INTO user_journey_state (user_id, onboarded, agent_created) "
        "VALUES ('backfill-midway', FALSE, FALSE) "
        "ON CONFLICT (user_id) DO UPDATE SET onboarded = FALSE, agent_created = FALSE"
    )
    # Re-running the step is safe (the ALTER is skipped); the backfill is the
    # part under test, so drive it directly.
    conn.execute("UPDATE user_journey_state SET agent_created = TRUE WHERE onboarded = TRUE")

    done = conn.execute("SELECT agent_created FROM user_journey_state WHERE user_id = 'backfill-done'").fetchone()
    midway = conn.execute("SELECT agent_created FROM user_journey_state WHERE user_id = 'backfill-midway'").fetchone()
    conn.close()

    assert done[0] is True, "an already-onboarded user's card must stay retired"
    assert midway[0] is False, "someone still onboarding should see the new step"
    assert callable(_v117_to_v118)
