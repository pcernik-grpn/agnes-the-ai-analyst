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

def test_disabled_system_plugin_is_not_visible_on_pg(store_engine):
    """PG-side parity for the invariant the deleted group fan-out used to
    carry: a plugin that is is_system AND admin_disabled reaches nobody.

    A PG-only drop of the ``admin_disabled = FALSE`` clause would otherwise
    serve a disabled plugin to every user, which is exactly the divergence
    the cross-engine contract exists to catch.
    """
    from src.repositories.marketplace_plugins_pg import MarketplacePluginsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(store_engine)
    plugins = MarketplacePluginsPgRepository(store_engine)
    g = groups.create(name="g1")

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
                "INSERT INTO marketplace_plugins (marketplace_id, name, is_system, admin_disabled) "
                "VALUES ('m1', 'p1', TRUE, FALSE), ('m1', 'p2', TRUE, TRUE)"
            )
        )

    served = {(r["marketplace_id"], r["name"])
              for r in plugins.list_granted_for_groups([g["id"]])}
    # p1 is served with NO grant row for this group — that is the read-time
    # resolution the fan-out used to fake by writing one.
    assert ("m1", "p1") in served
    assert ("m1", "p2") not in served
    assert set(plugins.list_system_keys()) == {("m1", "p1")}


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
