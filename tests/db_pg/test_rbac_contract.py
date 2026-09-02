"""Cross-engine contract tests for the RBAC quartet.

Targets: user_groups_repo, user_group_members_repo, resource_grants_repo.
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the pattern established in test_audit_contract.py.
"""

from __future__ import annotations

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repos(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.memory_domains import MemoryDomainsRepository
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "groups": UserGroupsRepository(conn),
        "members": UserGroupMembersRepository(conn),
        "grants": ResourceGrantsRepository(conn),
        "domains": MemoryDomainsRepository(conn),
        "users": UserRepository(conn),
    }, conn


def _make_pg_repos(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    import sqlalchemy as sa

    # Seed system groups — PG doesn't have DuckDB's _seed_system_groups
    # auto-run; the contract tests need Admin + Everyone to exist so that
    # the DuckDB side (which does auto-seed) and PG side start with the
    # same baseline.
    import uuid as _uuid

    with pg_engine.begin() as conn_:
        for name, description in (
            ("Admin", "System: full access"),
            ("Everyone", "System: default group"),
        ):
            conn_.execute(
                sa.text(
                    "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                    "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                    "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                ),
                {"id": _uuid.uuid4().hex, "name": name, "desc": description},
            )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.memory_domains_pg import MemoryDomainsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    return {
        "groups": UserGroupsPgRepository(engine),
        "members": UserGroupMembersPgRepository(engine),
        "grants": ResourceGrantsPgRepository(engine),
        "domains": MemoryDomainsPgRepository(engine),
        "users": UsersPgRepository(engine),
    }, None


@pytest.fixture(params=["duckdb", "pg"])
def rbac_repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repos_dict, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repos, conn = _make_duckdb_repos(tmp_path)
        yield repos, conn, backend
        if conn is not None:
            conn.close()
    else:
        repos, _ = _make_pg_repos(pg_engine, monkeypatch)
        yield repos, None, backend


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_create_group_then_get_returns_it(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grp = groups.create(name="data-team", description="Data Team", created_by="admin@x.com")
    assert grp is not None
    assert grp["name"] == "data-team"
    fetched = groups.get(grp["id"])
    assert fetched is not None
    assert fetched["name"] == "data-team"
    assert fetched["description"] == "Data Team"


def test_ensure_group_stamps_created_by_and_noops_on_existing(rbac_repos):
    """`ensure(created_by=…)` (Keboola login sync) stamps the creator on
    first create and leaves an existing row — including its created_by —
    untouched, identically on both backends."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grp = groups.ensure("kbc-516-admin", description="Keboola project 516", created_by="system:keboola-sync")
    assert grp["created_by"] == "system:keboola-sync"
    again = groups.ensure("kbc-516-admin", description="changed", created_by="someone:else")
    assert again["id"] == grp["id"]
    assert again["created_by"] == "system:keboola-sync"
    assert again["description"] == "Keboola project 516"
    # Default preserved for the historical caller (Google claim sync).
    google = groups.ensure("acme-group@example.com")
    assert google["created_by"] == "system:google-sync"


def test_add_member_then_list_members_returns_user(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u1", email="alice@example.com", name="Alice")
    grp = groups.create(name="analytics", created_by="admin@x.com")
    members.add_member("u1", grp["id"], source="admin", added_by="admin@x.com")

    listed = members.list_members_for_group(grp["id"])
    assert len(listed) == 1
    assert listed[0]["id"] == "u1"
    assert listed[0]["email"] == "alice@example.com"


def test_has_membership_true_after_add(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u2", email="bob@example.com", name="Bob")
    grp = groups.create(name="eng", created_by="admin@x.com")
    assert not members.has_membership("u2", grp["id"])
    members.add_member("u2", grp["id"], source="admin")
    assert members.has_membership("u2", grp["id"])


def test_remove_member_then_has_membership_false(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u3", email="carol@example.com", name="Carol")
    grp = groups.create(name="ops", created_by="admin@x.com")
    members.add_member("u3", grp["id"], source="admin")
    assert members.has_membership("u3", grp["id"])
    members.remove_member("u3", grp["id"])
    assert not members.has_membership("u3", grp["id"])


def test_resource_grant_create_then_has_grant(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="readers", created_by="admin@x.com")
    # Use 'marketplace_plugin' so no table_registry row is needed — the
    # per-type FK columns are NULL for this type (application-validated).
    grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="my-marketplace/web_sessions",
        assigned_by="admin@x.com",
    )
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/web_sessions") is True


def test_resource_grant_delete_then_has_grant_false(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="writers", created_by="admin@x.com")
    # Use 'marketplace_plugin' so no table_registry row is needed — the
    # per-type FK columns are NULL for this type (application-validated).
    grant_id = grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="my-marketplace/orders",
        assigned_by="admin@x.com",
    )
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/orders") is True
    grants.delete(grant_id)
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/orders") is False


def test_delete_for_marketplace_plugins_drops_only_that_marketplace(rbac_repos):
    """The marketplace-delete cascade (DELETE /api/marketplaces/{id}) must drop
    every grant for the target marketplace's plugins and nothing else —
    including NOT touching a sibling marketplace whose slug differs from the
    target by exactly one character where the target has '_'. That collision is
    the reason the repo matches on split_part(resource_id, '/', 1) and not a
    LIKE '<slug>/%' prefix (LIKE would treat the '_' as a single-char wildcard
    and bleed into the sibling). Same contract on both backends."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="mp-cascade", created_by="admin@x.com")
    # Target marketplace slug contains '_'; sibling differs only by '_'→'-'.
    grants.create(
        group_id=grp["id"], resource_type="marketplace_plugin", resource_id="acme_data/grpn", assigned_by="admin@x.com"
    )
    grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="acme_data/grpn-eng",
        assigned_by="admin@x.com",
    )
    grants.create(
        group_id=grp["id"], resource_type="marketplace_plugin", resource_id="acme-data/other", assigned_by="admin@x.com"
    )

    removed = grants.delete_for_marketplace_plugins("acme_data")
    assert removed == 2
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "acme_data/grpn") is False
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "acme_data/grpn-eng") is False
    # Sibling marketplace's grant survives — proves no LIKE '_' wildcard bleed.
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "acme-data/other") is True


def test_ensure_grant_creates_then_idempotent(rbac_repos):
    """ensure_grant (used by the built-in marketplace seeder on every boot)
    must create the grant on first call and be a no-op on repeat — same
    contract on both backends. Returns True iff the grant exists after the
    call (newly inserted OR already present), never raising on a duplicate."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="seeded", created_by="admin@x.com")
    # First call inserts the grant.
    assert grants.ensure_grant(grp["id"], "marketplace_plugin", "agnes-builtin/agnes-analyst", "system") is True
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "agnes-builtin/agnes-analyst") is True
    # Second call is the INSERT-OR-IGNORE / ON-CONFLICT-DO-NOTHING idempotency
    # path — no error, no duplicate, still reports the grant present.
    assert grants.ensure_grant(grp["id"], "marketplace_plugin", "agnes-builtin/agnes-analyst", "system") is True
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "agnes-builtin/agnes-analyst") is True


def test_grant_source_accepted_by_both_backends_and_surfaced_by_pg(rbac_repos):
    """`source` records WHICH surface wrote a grant, so /admin/access can say
    where a grant is managed instead of re-deriving it (only the Required-plugin
    case was ever derivable).

    The column is Postgres-only — the DuckDB app-state backend is frozen under
    A3 and takes no new schema — so the contract is deliberately asymmetric and
    this test pins BOTH halves of it:

      - every backend ACCEPTS the keyword on `create` and `ensure_grant`
        without raising, so a writer passing `source=` is portable;
      - Postgres READS it back through `list_all`, the projection
        `/api/admin/access-overview` actually consumes;
      - DuckDB drops it and reports no source, which is what makes an
        unlabelled row on a frozen instance correct rather than a bug.

    `list_all` is named explicitly because it has its own inline SELECT (it
    joins the group name) rather than the shared `_SELECT_COLS`, so adding the
    column to the repo is not enough to make the page see it.
    """
    repos, _, backend = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="provenance", created_by="admin@x.com")
    grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="acme/from-wizard",
        assigned_by="admin@x.com",
        source="sharepoint_wizard",
    )
    assert (
        grants.ensure_grant(
            grp["id"],
            "marketplace_plugin",
            "acme/from-sync",
            "system",
            source="marketplace_sync",
        )
        is True
    )

    by_rid = {g["resource_id"]: g for g in grants.list_all(resource_type="marketplace_plugin")}
    assert {"acme/from-wizard", "acme/from-sync"} <= set(by_rid)

    if backend == "duckdb":
        # Frozen backend: the argument is accepted and dropped, never stored.
        assert by_rid["acme/from-wizard"].get("source") is None
        assert by_rid["acme/from-sync"].get("source") is None
    else:
        assert by_rid["acme/from-wizard"]["source"] == "sharepoint_wizard"
        assert by_rid["acme/from-sync"]["source"] == "marketplace_sync"


def test_grant_scope_accepted_by_both_backends_and_surfaced_by_pg(rbac_repos):
    """``scope`` says WHO a grant reaches — the members of its group, or every
    account. The asymmetry is deliberate and both halves are pinned here.

    ``Everyone`` used to be a group that normally held every account but was
    mirrored from a Workspace group when ``AGNES_GROUP_EVERYONE_EMAIL`` was
    set, so the word named two different sets of people on two instances. A
    scope always means every account.

    Postgres-only (migration 0097), like ``source``, because the DuckDB
    app-state ladder is frozen under A3. So:

      - every backend ACCEPTS ``scope=`` on ``create`` and ``ensure_grant``
        without raising, so a writer is portable;
      - Postgres READS it back through ``list_all`` AND ``get`` — two column
        lists, both of which have to carry it;
      - DuckDB drops it, and the grant is an ordinary one on the carrier
        group. That is not a silent loss of reach: every account is
        auto-joined to that group at creation, so the same people are
        reached. The one case the two answer differently is an account an
        admin has REMOVED from it.

    Reads that are ABOUT the column rather than merely carrying it raise
    ``RequiresPostgresBackend`` on DuckDB instead of answering ``0`` — see
    the last assertion.
    """
    from src.repository_errors import RequiresPostgresBackend

    repos, _, backend = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    carrier = groups.get_by_name("Everyone")
    assert carrier, "the seeded carrier group is missing from the fixture baseline"
    narrow = groups.create(name="one-team", created_by="admin@x.com")

    everyone_id = grants.create(
        group_id=carrier["id"],
        resource_type="marketplace_plugin",
        resource_id="acme/for-everyone",
        requirement="required",
        scope="everyone",
    )
    grants.create(
        group_id=narrow["id"],
        resource_type="marketplace_plugin",
        resource_id="acme/for-one-team",
    )
    assert (
        grants.ensure_grant(
            carrier["id"],
            "marketplace_plugin",
            "acme/seeded-for-everyone",
            "system",
            scope="everyone",
        )
        is True
    )

    by_rid = {g["resource_id"]: g for g in grants.list_all(resource_type="marketplace_plugin")}
    assert {"acme/for-everyone", "acme/for-one-team", "acme/seeded-for-everyone"} <= set(by_rid)
    fetched = grants.get(everyone_id)
    assert fetched is not None

    if backend == "duckdb":
        # Frozen backend: accepted and dropped, never stored.
        assert by_rid["acme/for-everyone"].get("scope") is None
        assert by_rid["acme/seeded-for-everyone"].get("scope") is None
        assert fetched.get("scope") is None
        # And a read ABOUT the column refuses rather than answering wrongly.
        with pytest.raises(RequiresPostgresBackend):
            grants.count_everyone_scoped()
        with pytest.raises(RequiresPostgresBackend):
            grants.list_everyone_scoped()
        return

    assert by_rid["acme/for-everyone"]["scope"] == "everyone"
    assert by_rid["acme/seeded-for-everyone"]["scope"] == "everyone"
    assert by_rid["acme/for-one-team"]["scope"] is None
    assert fetched["scope"] == "everyone"

    # An everyone-grant reaches an account in NO group — the case the group
    # model could not express, and the reason this cannot be an extra id in
    # the group IN-list.
    assert grants.has_grant([], "marketplace_plugin", "acme/for-everyone") is True
    assert grants.has_grant([], "marketplace_plugin", "acme/for-one-team") is False
    reached = {r["resource_id"] for r in grants.list_for_groups([])}
    assert {"acme/for-everyone", "acme/seeded-for-everyone"} == reached

    # And a caller CAN ask the narrower question, for the admin surfaces that
    # attribute a row to a group.
    assert grants.has_grant(
        [carrier["id"]],
        "marketplace_plugin",
        "acme/for-everyone",
        include_everyone=False,
    ) is True
    assert grants.has_grant(
        [narrow["id"]],
        "marketplace_plugin",
        "acme/for-everyone",
        include_everyone=False,
    ) is False

    # The carrier does not OWN what it carries: revoking one of these must
    # not read as revoking one of the group's own grants, so they are not
    # counted as such.
    assert grants.count_for_group(carrier["id"]) == 0
    assert grants.count_for_group(narrow["id"]) == 1
    assert grants.count_everyone_scoped() == 2
    assert {r["resource_id"] for r in grants.list_everyone_scoped()} == {
        "acme/for-everyone",
        "acme/seeded-for-everyone",
    }


def test_list_groups_for_user_returns_joined_groups(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u4", email="dave@example.com", name="Dave")
    g1 = groups.create(name="g-alpha", created_by="admin@x.com")
    g2 = groups.create(name="g-beta", created_by="admin@x.com")
    members.add_member("u4", g1["id"], source="admin")
    members.add_member("u4", g2["id"], source="admin")

    group_ids = members.list_groups_for_user("u4")
    assert set(group_ids) == {g1["id"], g2["id"]}


def test_replace_google_sync_groups_sets_final_membership(rbac_repos):
    """google_sync refresh is authoritative for that source on both engines."""
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="gs1", email="erin@example.com", name="Erin")
    g1 = groups.create(name="gs-finance", created_by="admin@x.com")
    g2 = groups.create(name="gs-curator", created_by="admin@x.com")
    g3 = groups.create(name="gs-legal", created_by="admin@x.com")

    members.replace_google_sync_groups("gs1", [g1["id"], g2["id"]])
    assert set(members.list_groups_for_user("gs1")) == {g1["id"], g2["id"]}

    # Re-running is authoritative: drops g1, keeps g2, adds g3 — never an
    # empty intermediate (the rebuild is wrapped in one transaction so a
    # concurrent reader can't observe the post-DELETE / pre-INSERT gap).
    members.replace_google_sync_groups("gs1", [g2["id"], g3["id"]])
    assert set(members.list_groups_for_user("gs1")) == {g2["id"], g3["id"]}


def test_replace_google_sync_preserves_higher_priority_source(rbac_repos):
    """An admin/system_seed membership on the same (user, group) pair must
    survive a google_sync refresh — ON CONFLICT DO NOTHING keeps the existing
    higher-priority row instead of erroring or downgrading it."""
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="gs2", email="frank@example.com", name="Frank")
    admin_grp = groups.create(name="gs-admin-pin", created_by="admin@x.com")
    sync_grp = groups.create(name="gs-sync-only", created_by="admin@x.com")

    members.add_member("gs2", admin_grp["id"], source="admin", added_by="admin@x.com")

    # google_sync also reports the admin-pinned group → must not error or
    # duplicate; the admin row stays. The sync-only group is added.
    members.replace_google_sync_groups("gs2", [admin_grp["id"], sync_grp["id"]])
    assert set(members.list_groups_for_user("gs2")) == {admin_grp["id"], sync_grp["id"]}

    # A later refresh that no longer lists the admin-pinned group must NOT
    # remove it — it's owned by source='admin', not 'google_sync'.
    members.replace_google_sync_groups("gs2", [sync_grp["id"]])
    assert members.has_membership("gs2", admin_grp["id"])
    assert set(members.list_groups_for_user("gs2")) == {admin_grp["id"], sync_grp["id"]}


def test_system_group_rename_raises(rbac_repos):
    """Both engines must protect system groups from rename."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    from src.repositories.user_groups import SystemGroupProtected

    # DuckDB _ensure_schema seeds Admin; PG fixture seeds it above
    admin = groups.get_by_name("Admin")
    assert admin is not None
    with pytest.raises(SystemGroupProtected):
        groups.update(admin["id"], name="Hacked")


def test_list_group_names_for_user_returns_names_not_ids(rbac_repos):
    """``list_group_names_for_user`` — the repo-routed equivalent of the raw
    ``SELECT g.name FROM user_group_members m JOIN user_groups g ...`` query
    that ``app.api.memory._effective_groups`` used to run on the always-DuckDB
    connection. Same names, either engine."""
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u-names", email="names@example.com", name="Names")
    g1 = groups.create(name="grp-alpha", created_by="admin@x.com")
    g2 = groups.create(name="grp-beta", created_by="admin@x.com")
    members.add_member("u-names", g1["id"], source="admin")
    members.add_member("u-names", g2["id"], source="admin")

    names = members.list_group_names_for_user("u-names")
    assert set(names) == {"grp-alpha", "grp-beta"}


def test_list_group_names_for_user_empty_for_unknown_user(rbac_repos):
    repos, _, _ = rbac_repos
    members = repos["members"]
    assert members.list_group_names_for_user("no-such-user") == []


def test_list_resource_ids_for_user_returns_distinct_grants(rbac_repos):
    """``list_resource_ids_for_user`` — the repo-routed equivalent of the raw
    ``SELECT DISTINCT rg.resource_id FROM resource_grants rg JOIN
    user_group_members m ...`` query that
    ``app.api.memory._caller_granted_memory_domains`` used to run on the
    always-DuckDB connection. Same resource_id set, either engine, scoped by
    both user membership and resource_type.

    Uses real ``memory_domains`` rows (rather than made-up ids) because PG
    enforces a per-type FK from ``resource_grants.resource_id_memory_domain``
    to ``memory_domains.id`` (migration 0013) that DuckDB doesn't — a bare
    string id would pass on DuckDB and 500 on PG.
    """
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]
    grants = repos["grants"]
    domains = repos["domains"]

    users.create(id="u-grants", email="grants@example.com", name="Grants")
    g1 = groups.create(name="grants-g1", created_by="admin@x.com")
    g2 = groups.create(name="grants-g2", created_by="admin@x.com")
    members.add_member("u-grants", g1["id"], source="admin")
    members.add_member("u-grants", g2["id"], source="admin")

    finance_id = domains.create(
        name="Finance",
        slug="grants-finance",
        description=None,
        icon=None,
        color=None,
        created_by="admin@x.com",
    )
    legal_id = domains.create(
        name="Legal",
        slug="grants-legal",
        description=None,
        icon=None,
        color=None,
        created_by="admin@x.com",
    )

    grants.create(group_id=g1["id"], resource_type="memory_domain", resource_id=finance_id)
    # Same resource granted via a second group — must not duplicate in the result.
    grants.create(group_id=g2["id"], resource_type="memory_domain", resource_id=finance_id)
    grants.create(group_id=g2["id"], resource_type="memory_domain", resource_id=legal_id)
    # Different resource_type must not leak in.
    grants.create(group_id=g1["id"], resource_type="marketplace_plugin", resource_id="mp/plugin")

    ids = grants.list_resource_ids_for_user("u-grants", "memory_domain")
    assert set(ids) == {finance_id, legal_id}


def test_list_resource_ids_for_user_empty_when_no_membership(rbac_repos):
    repos, _, _ = rbac_repos
    grants = repos["grants"]
    assert grants.list_resource_ids_for_user("no-such-user", "memory_domain") == []


def test_list_for_groups_returns_same_ids_both_backends(rbac_repos):
    """``list_for_groups`` — the primitive ``app.auth.access._allowed_ids_for_user``
    (and therefore ``src.rbac.get_accessible_ids``) builds on. Same
    resource_id set, either engine, scoped by group membership and
    resource_type."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    g1 = groups.create(name="ids-g1", created_by="admin@x.com")
    g2 = groups.create(name="ids-g2", created_by="admin@x.com")

    # marketplace_plugin has no per-type FK column, so a bare string id is
    # valid on both engines without needing a matching parent row.
    grants.create(group_id=g1["id"], resource_type="marketplace_plugin", resource_id="mp/plugin-a")
    grants.create(group_id=g2["id"], resource_type="marketplace_plugin", resource_id="mp/plugin-b")
    # Different resource_type must not leak in.
    grants.create(group_id=g1["id"], resource_type="chat", resource_id="chat")

    rows = grants.list_for_groups([g1["id"], g2["id"]], "marketplace_plugin")
    assert {r["resource_id"] for r in rows} == {"mp/plugin-a", "mp/plugin-b"}

    # Empty group_ids list — no query, empty result, on both engines.
    assert grants.list_for_groups([], "marketplace_plugin") == []


def test_an_everyone_scope_survives_the_authorization_path_for_a_groupless_account(
    pg_engine, monkeypatch
):
    """The scope is only real if the AUTHORIZATION path honours it.

    Not a repository test — the repositories were already correct. Four
    functions in ``app.auth.access`` short-circuited on an empty group set
    (``if not group_ids: return False``) and never reached them, and two more
    in ``src.marketplace_filter`` did the same on the serve path. Every one of
    those was right while "everyone" was a group nobody could be outside of;
    each became a silent denial the moment it became a scope.

    Postgres-only, because the scope is: on DuckDB an everyone-grant is a
    grant on the carrier group and reaching it does require membership, which
    is the documented divergence.

    A groupless account is the whole point of this test. It is rare — no API
    path can remove the auto-membership (``remove_member`` takes
    ``require_source='admin'``) — which is exactly why no existing test
    covered it, and why the bug was invisible until the model changed.
    """
    import uuid as _uuid
    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    carrier_id = _uuid.uuid4().hex
    user_id = "u-" + _uuid.uuid4().hex[:8]
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, 'Everyone', 'System', TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": carrier_id},
        )
        carrier_id = conn.execute(
            sa.text("SELECT id FROM user_groups WHERE name = 'Everyone'")
        ).scalar_one()
        conn.execute(
            sa.text("INSERT INTO users (id, email, name) VALUES (:id, :e, 'Groupless')"),
            {"id": user_id, "e": f"{user_id}@example.com"},
        )
        # No membership row at all — deliberately.
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants "
                "(id, group_id, resource_type, resource_id, requirement, scope) "
                "VALUES (:id, :g, 'chat', 'chat', 'available', 'everyone')"
            ),
            {"id": _uuid.uuid4().hex, "g": carrier_id},
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    import importlib

    import src.repositories as repositories

    importlib.reload(repositories)
    try:
        from app.auth.access import can_access, has_explicit_grant

        assert can_access(user_id, "chat", "chat") is True, (
            "the authorization gate denied access an everyone-scoped grant gives"
        )
        assert has_explicit_grant(user_id, "chat", "chat") is True, (
            "the nav-affordance read missed it, so chat would be granted but hidden"
        )
        assert can_access(user_id, "chat", "some-other-chat") is False
    finally:
        importlib.reload(repositories)
        db_pg.dispose()
