"""Postgres-side tests for the marketplace + store + flea cluster:
marketplace_registry, marketplace_plugins, store_entities,
user_store_installs, user_curated_subscriptions, store_submissions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store_engine(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# marketplace_registry
# ---------------------------------------------------------------------------

def test_marketplace_registry_register_upsert(store_engine):
    from src.repositories.marketplace_registry_pg import MarketplaceRegistryPgRepository

    repo = MarketplaceRegistryPgRepository(store_engine)
    repo.register(id="m1", name="My MP", url="https://example.com/repo.git",
                  curator_name="alice")
    row = repo.get("m1")
    assert row["name"] == "My MP"
    assert row["curator_name"] == "alice"

    # Re-register with curator_name=None should NOT clobber alice
    repo.register(id="m1", name="My MP v2", url="https://example.com/repo.git")
    row = repo.get("m1")
    assert row["name"] == "My MP v2"
    assert row["curator_name"] == "alice"


def test_marketplace_registry_update_sync_status(store_engine):
    from src.repositories.marketplace_registry_pg import MarketplaceRegistryPgRepository

    repo = MarketplaceRegistryPgRepository(store_engine)
    repo.register(id="m1", name="MP", url="u")
    repo.update_sync_status("m1", error="boom")
    assert repo.get("m1")["last_error"] == "boom"

    # Success clears error
    repo.update_sync_status(
        "m1",
        commit_sha="abc123",
        synced_at=datetime.now(timezone.utc),
    )
    row = repo.get("m1")
    assert row["last_commit_sha"] == "abc123"
    assert row["last_error"] is None


# ---------------------------------------------------------------------------
# marketplace_plugins
# ---------------------------------------------------------------------------

def test_marketplace_plugins_replace_for_marketplace(store_engine):
    from src.repositories.marketplace_plugins_pg import MarketplacePluginsPgRepository

    repo = MarketplacePluginsPgRepository(store_engine)
    plugins = [
        {"name": "p1", "description": "first", "version": "1.0",
         "author": {"name": "alice"}, "source": "."},
        {"name": "p2", "description": "second", "source": {"source": "github"}},
    ]
    n = repo.replace_for_marketplace("m1", plugins)
    assert n == 2

    listed = repo.list_for_marketplace("m1")
    assert {r["name"] for r in listed} == {"p1", "p2"}
    p1 = next(r for r in listed if r["name"] == "p1")
    assert p1["source_type"] == "path"
    assert p1["author_name"] == "alice"

    # Replace with a shrunken set drops the removed plugin
    repo.replace_for_marketplace("m1", [{"name": "p1", "version": "1.1"}])
    listed = repo.list_for_marketplace("m1")
    assert {r["name"] for r in listed} == {"p1"}


# ---------------------------------------------------------------------------
# resource_grants fanout (now that marketplace_plugins is migrated)
# ---------------------------------------------------------------------------

def test_an_everyone_scoped_grant_is_served_and_a_disabled_plugin_is_not(store_engine):
    """PG-side parity for two invariants that meet on the same query.

    An everyone-scoped grant reaches a caller whose groups were granted
    NOTHING — it is matched by scope, not by the group IN-list, which is the
    one thing this backend can express and the frozen DuckDB ladder cannot.
    And ``admin_disabled`` still wins over any grant: a PG-only drop of that
    clause would serve a hidden plugin to every account, which is exactly the
    divergence the cross-engine contract exists to catch.

    This used to assert the same shape for ``is_system``, whose reach was the
    same and whose spelling was a second one (0098).
    """
    from src.repositories.marketplace_plugins_pg import MarketplacePluginsPgRepository
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(store_engine)
    plugins = MarketplacePluginsPgRepository(store_engine)
    grants = ResourceGrantsPgRepository(store_engine)
    g = groups.create(name="g1")
    carrier = groups.create(name="Everyone")

    import sqlalchemy as sa

    with store_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES ('m1', 'm1', 'https://example.test/m1.git', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_plugins (marketplace_id, name, admin_disabled) "
                "VALUES ('m1', 'p1', FALSE), ('m1', 'p2', TRUE)"
            )
        )
    for name in ("p1", "p2"):
        grants.create(
            group_id=carrier["id"],
            resource_type="marketplace_plugin",
            resource_id=f"m1/{name}",
            requirement="required",
            scope="everyone",
        )

    served = {(r["marketplace_id"], r["name"]) for r in plugins.list_granted_for_groups([g["id"]])}
    # p1 reaches this caller with NO grant on any group they belong to.
    assert ("m1", "p1") in served
    assert ("m1", "p2") not in served, "a disabled plugin was served to an everyone-grantee"

    # And with no groups at all — the case the group model could not express.
    assert ("m1", "p1") in {(r["marketplace_id"], r["name"]) for r in plugins.list_granted_for_groups([])}


# ---------------------------------------------------------------------------
# store_entities
# ---------------------------------------------------------------------------

def test_store_entity_create_and_get(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository

    repo = StoreEntitiesPgRepository(store_engine)
    entity = repo.create(
        id="e1", owner_user_id="u1", owner_username="alice", type="skill",
        name="myskill", description="hi", category="Other", version="abc",
        file_size=100,
    )
    assert entity["id"] == "e1"
    assert entity["version_no"] == 1
    assert entity["version_history"][0]["n"] == 1


def test_store_entity_append_and_promote_version(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository

    repo = StoreEntitiesPgRepository(store_engine)
    repo.create(
        id="e1", owner_user_id="u1", owner_username="alice", type="skill",
        name="myskill", description=None, category=None, version="v1hash",
        file_size=100,
    )
    n = repo.append_version_history(
        "e1", version_hash="v2hash", sha256="abc", size=200,
        submission_id="sub1", created_by="u1",
    )
    assert n == 2

    # version_no not yet promoted
    entity = repo.get("e1")
    assert entity["version_no"] == 1
    assert entity["version"] == "v1hash"

    repo.promote_version("e1", 2)
    entity = repo.get("e1")
    assert entity["version_no"] == 2
    assert entity["version"] == "v2hash"
    assert entity["file_size"] == 200


def test_store_entity_list_filters(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository

    repo = StoreEntitiesPgRepository(store_engine)
    repo.create(id="e1", owner_user_id="u1", owner_username="alice",
                type="skill", name="a", description=None, category="Data",
                version="v", visibility_status="approved")
    repo.create(id="e2", owner_user_id="u2", owner_username="bob",
                type="agent", name="b", description=None, category="Other",
                version="v", visibility_status="approved")
    repo.create(id="e3", owner_user_id="u2", owner_username="bob",
                type="skill", name="c", description=None, category=None,
                version="v", visibility_status="pending")

    items, total = repo.list(visibility_status=["approved"])
    assert total == 2
    items, _ = repo.list(visibility_status=["approved"], type="skill")
    assert len(items) == 1 and items[0]["id"] == "e1"

    # Owner-include: u2 sees their own pending plus approved
    items, _ = repo.list(visibility_status=["approved"], include_owner_id="u2")
    ids = {i["id"] for i in items}
    assert ids == {"e1", "e2", "e3"}


def test_store_entity_archive_and_restore(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository

    repo = StoreEntitiesPgRepository(store_engine)
    repo.create(id="e1", owner_user_id="u1", owner_username="alice",
                type="skill", name="myskill", description=None, category=None,
                version="v", visibility_status="approved")
    info = repo.archive("e1", by_user_id="admin")
    assert info["original_name"] == "myskill"
    assert info["new_name"] != "myskill"
    entity = repo.get("e1")
    assert entity["visibility_status"] == "archived"


def test_store_entity_bump_install_count_floors_at_zero(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository

    repo = StoreEntitiesPgRepository(store_engine)
    repo.create(id="e1", owner_user_id="u1", owner_username="alice",
                type="skill", name="x", description=None, category=None, version="v")
    repo.bump_install_count("e1", 5)
    assert repo.get("e1")["install_count"] == 5
    repo.bump_install_count("e1", -10)
    assert repo.get("e1")["install_count"] == 0


# ---------------------------------------------------------------------------
# user_store_installs
# ---------------------------------------------------------------------------

def test_user_store_install_idempotent(store_engine):
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository

    repo = UserStoreInstallsPgRepository(store_engine)
    assert repo.install("u1", "e1") is True
    assert repo.install("u1", "e1") is False  # idempotent
    assert repo.is_installed("u1", "e1") is True
    assert repo.uninstall("u1", "e1") is True
    assert repo.uninstall("u1", "e1") is False


def test_user_store_install_list_filters_to_approved_and_archived(store_engine):
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository

    entities = StoreEntitiesPgRepository(store_engine)
    installs = UserStoreInstallsPgRepository(store_engine)

    entities.create(id="e_approved", owner_user_id="u_owner",
                    owner_username="o", type="skill", name="a",
                    description=None, category=None, version="v",
                    visibility_status="approved")
    entities.create(id="e_pending", owner_user_id="u_owner",
                    owner_username="o", type="skill", name="b",
                    description=None, category=None, version="v",
                    visibility_status="pending")
    installs.install("u1", "e_approved")
    installs.install("u1", "e_pending")

    rows = installs.list_for_user("u1")
    # Only approved should be returned (pending entries are filtered out)
    assert {r["id"] for r in rows} == {"e_approved"}


# ---------------------------------------------------------------------------
# user_curated_subscriptions
# ---------------------------------------------------------------------------

def test_curated_subscribe_unsubscribe(store_engine):
    from src.repositories.user_curated_subscriptions_pg import (
        UserCuratedSubscriptionsPgRepository,
    )

    repo = UserCuratedSubscriptionsPgRepository(store_engine)
    assert repo.subscribe("u1", "m1", "p1") is True
    assert repo.subscribe("u1", "m1", "p1") is False  # idempotent
    assert repo.is_subscribed("u1", "m1", "p1") is True
    assert repo.subscribed_set("u1") == {("m1", "p1")}
    assert repo.unsubscribe("u1", "m1", "p1") is True




def test_curated_stack_counts_groups_by_plugin(store_engine):
    from src.repositories.user_curated_subscriptions_pg import (
        UserCuratedSubscriptionsPgRepository,
    )

    repo = UserCuratedSubscriptionsPgRepository(store_engine)
    repo.subscribe("u1", "m1", "p1")
    repo.subscribe("u2", "m1", "p1")
    repo.subscribe("u1", "m1", "p2")
    assert repo.stack_counts() == {("m1", "p1"): 2, ("m1", "p2"): 1}


# ---------------------------------------------------------------------------
# store_submissions
# ---------------------------------------------------------------------------

def test_store_submission_create_and_get(store_engine):
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository

    repo = StoreSubmissionsPgRepository(store_engine)
    sub_id = repo.create(
        submitter_id="u1", submitter_email="u@example.com",
        type="skill", name="myskill", version="v1",
        status="pending_llm", entity_id="e1",
        file_size=100, bundle_sha256="abc",
    )
    sub = repo.get(sub_id)
    assert sub["name"] == "myskill"
    assert sub["status"] == "pending_llm"


def test_store_submission_update_status_cas(store_engine):
    """CAS skip on terminal states."""
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository

    repo = StoreSubmissionsPgRepository(store_engine)
    sub_id = repo.create(submitter_id="u1", submitter_email=None,
                         type="skill", name="x", version="v",
                         status="pending_llm")
    # Approved is terminal; subsequent update_status without override flag is a no-op
    assert repo.update_status(sub_id, status="approved") is True
    assert repo.update_status(sub_id, status="blocked_llm") is False
    # With override flag it goes through
    assert repo.update_status(sub_id, status="blocked_llm",
                              allow_terminal_overwrite=True) is True


def test_store_submission_set_override(store_engine):
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository

    repo = StoreSubmissionsPgRepository(store_engine)
    sub_id = repo.create(submitter_id="u1", submitter_email=None,
                         type="skill", name="x", version="v",
                         status="blocked_llm")
    repo.set_override(sub_id, admin_user_id="admin", reason="false positive")
    sub = repo.get(sub_id)
    assert sub["status"] == "overridden"
    assert sub["override_by"] == "admin"
    assert sub["override_reason"] == "false positive"


def test_store_submission_count_blocked_for_submitter_since(store_engine):
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository

    repo = StoreSubmissionsPgRepository(store_engine)
    repo.create(submitter_id="u1", submitter_email=None, type="skill",
                name="a", version="v", status="blocked_llm")
    repo.create(submitter_id="u1", submitter_email=None, type="skill",
                name="b", version="v", status="approved")
    # Only blocked_llm counts
    n = repo.count_blocked_for_submitter_since(
        "u1", datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert n == 1


def test_store_submission_list_for_admin_default_hides_lifecycle_end(store_engine):
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository

    repo = StoreSubmissionsPgRepository(store_engine)
    repo.create(submitter_id="u1", submitter_email=None, type="skill",
                name="alive", version="v", status="pending_llm")
    repo.create(submitter_id="u1", submitter_email=None, type="skill",
                name="dead", version="v", status="deleted")
    items, total = repo.list_for_admin()
    names = {i["name"] for i in items}
    assert "alive" in names
    assert "dead" not in names

    # Explicit deleted chip surfaces the dead row
    items, _ = repo.list_for_admin(lifecycle="deleted")
    assert {i["name"] for i in items} == {"dead"}


def test_browse_listing_and_category_counts_see_an_everyone_scoped_grant(store_engine):
    """The served feed and the browse tab must answer for the same audience.

    `list_granted_for_groups` (the feed) gained the `scope='everyone'` term
    when everyone became a scope. `list_with_filters` (the browse tab) and
    `category_counts` (its category pills) sat directly below it in the same
    class and did NOT — they matched `group_id IN (...)` alone and
    early-returned on an empty group set. So an account an everyone-scoped
    grant reached without a membership was served a plugin the browse tab
    told it did not exist, and the pills counted a different set from the
    list beneath them.

    Postgres-only: the column is (migration 0097).
    """
    from src.repositories.marketplace_plugins_pg import MarketplacePluginsPgRepository
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    import sqlalchemy as sa

    groups = UserGroupsPgRepository(store_engine)
    plugins = MarketplacePluginsPgRepository(store_engine)
    grants = ResourceGrantsPgRepository(store_engine)
    carrier = groups.create(name="Everyone")
    stranger = groups.create(name="g-no-grants")

    with store_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES ('m2', 'm2', 'https://example.test/m2.git', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_plugins (marketplace_id, name, category, admin_disabled) "
                "VALUES ('m2', 'everybody', 'Ops', FALSE), ('m2', 'hidden', 'Ops', TRUE)"
            )
        )
    for name in ("everybody", "hidden"):
        grants.create(
            group_id=carrier["id"],
            resource_type="marketplace_plugin",
            resource_id=f"m2/{name}",
            scope="everyone",
        )

    # A caller whose own group was granted nothing.
    items, total = plugins.list_with_filters(group_ids=[stranger["id"]])
    names = {r["name"] for r in items}
    assert "everybody" in names, "the browse tab hid a plugin the served feed serves"
    assert "hidden" not in names, "a disabled plugin surfaced in the browse tab"
    assert total == 1

    counts = plugins.category_counts(group_ids=[stranger["id"]])
    assert counts.get("Ops") == 1, f"the category pills disagree with the listing: {counts}"

    # And with NO groups at all — the audience the group model could not express.
    items, total = plugins.list_with_filters(group_ids=[])
    assert {r["name"] for r in items} == {"everybody"}
    assert total == 1
    assert plugins.category_counts(group_ids=[]).get("Ops") == 1


def test_an_everyone_scope_is_forced_onto_the_carrier_group(store_engine):
    """The carrier is an invariant, enforced in the repository.

    `create_grant` overrode the caller's `group_id`, but it is not the only
    writer — the built-in marketplace seed, the collections auto-share and
    the chat-grant seed each write `scope='everyone'` and each resolve the
    group by name independently. So the invariant was true by coincidence,
    and two readers depend on it being true by construction:
    `reports._NOT_SYSTEM` (byte-identical on both backends, because the
    frozen DuckDB ladder has no `scope` column) and
    `marketplace_filter.everyone_required_plugin_keys` both identify
    "reaches everyone" BY the carrier group.

    So the repository forces it, and a caller passing some other group with
    a scope gets the carrier anyway.
    """
    from src.grant_scopes import carrier_group_id
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(store_engine)
    grants = ResourceGrantsPgRepository(store_engine)
    carrier = groups.create(name="Everyone")
    wrong = groups.create(name="g-not-the-carrier")

    assert carrier_group_id() == carrier["id"], "fixture sanity: the carrier resolves"

    created = grants.create(
        group_id=wrong["id"],
        resource_type="marketplace_plugin",
        resource_id="m3/everybody",
        scope="everyone",
    )
    row = grants.get(created)
    assert row is not None
    assert row["group_id"] == carrier["id"], (
        "the repository stored an everyone-scoped grant against the caller's group; "
        "the carrier-based readers would never find it"
    )
    assert row["scope"] == "everyone"

    # ensure_grant too — it is the seeders' entry point, and they are the
    # writers that made this true only by coincidence before.
    assert grants.ensure_grant(
        wrong["id"], "marketplace_plugin", "m3/also-everybody", "system", scope="everyone"
    ) is True
    seeded = next(
        r for r in grants.list_everyone_scoped("marketplace_plugin")
        if r["resource_id"] == "m3/also-everybody"
    )
    assert seeded["group_id"] == carrier["id"]

    # A scope-less grant is left exactly where the caller put it.
    plain = grants.create(
        group_id=wrong["id"], resource_type="marketplace_plugin", resource_id="m3/one-team"
    )
    assert grants.get(plain)["group_id"] == wrong["id"]
