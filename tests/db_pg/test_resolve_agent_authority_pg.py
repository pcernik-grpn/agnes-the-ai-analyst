"""resolve_agent_authority against a REAL Postgres backend (remediation
Track C, C2.2) — the ONE backend where ``agent_scope.granted_by`` actually
persists (migration 0073, C2.1; DuckDB drops it — see
``src/repositories/agents.py::AgentsRepository.set_scope``). Every scenario
below therefore proves the D-C2 admin/self-granted split with REAL grants,
REAL Admin-group membership, and the production repo/access-control
machinery — not monkeypatched stand-ins (those live in
``tests/test_agent_scope_intersection.py``).

Mirrors the alembic-head + system-group-seed idiom from
``tests/db_pg/_parity_sweep_util.py`` / ``test_agents_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres, wired as the active backend for the repo
    factory + ``app.auth.access``'s group/grant primitives."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


def _admin_group_id(pg_engine) -> str:
    import sqlalchemy as sa

    with pg_engine.connect() as conn:
        return conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()


def test_admin_granted_data_package_reaches_the_agent_even_without_owner_grant(pg_env):
    """REQUIRED (a), real-Postgres proof: an admin-granted ``data_package``
    resolves for the agent even though its OWNER holds no grant on it at
    all. Pre-C2.2 (``compute_agent_intersection``) this intersected the
    package against the OWNER's package reach unconditionally and would
    have returned an EMPTY set here — the exact "package invisible to an
    admin-built agent" bug class C2.2 closes.
    """
    from src.agent_scope_intersection import resolve_agent_authority
    from src.repositories import agents_repo, data_packages_repo, user_group_members_repo, users_repo

    users_repo().create(id="owner1", email="owner1@test.com", name="Owner")
    users_repo().create(id="admin1", email="admin1@test.com", name="Admin")
    user_group_members_repo().add_member("admin1", _admin_group_id(pg_env), source="system_seed")

    pkg_id = data_packages_repo().create(
        name="Admin Pkg", slug="c22-admin-pkg", description=None, icon=None, color=None, created_by="admin1"
    )
    # Owner has ZERO grant on this package — no per-package grant, not even
    # an 'available' one.

    agents_repo().create(
        id="agent1", owner_user_id="owner1", name="A", slug="c22-admin-pkg-agent", tables_mode="selected"
    )
    agents_repo().set_scope("agent1", [("data_package", pkg_id)], granted_by="admin1")

    out = resolve_agent_authority("agent1")
    assert out.get("data_package") == frozenset({pkg_id})


def test_self_granted_data_package_the_owner_lacks_does_not_resolve(pg_env):
    """Control for the test above: the SAME scope row, but ``granted_by`` is
    the (non-admin) owner instead of an admin, resolves to NOTHING — proving
    the divergence is genuinely keyed on the granter's admin-ness, not some
    other PG-specific effect."""
    from src.agent_scope_intersection import resolve_agent_authority
    from src.repositories import agents_repo, data_packages_repo, users_repo

    users_repo().create(id="owner1", email="owner1@test.com", name="Owner")

    pkg_id = data_packages_repo().create(
        name="Self Pkg", slug="c22-self-pkg", description=None, icon=None, color=None, created_by="owner1"
    )

    agents_repo().create(
        id="agent1", owner_user_id="owner1", name="A", slug="c22-self-pkg-agent", tables_mode="selected"
    )
    agents_repo().set_scope("agent1", [("data_package", pkg_id)], granted_by="owner1")

    out = resolve_agent_authority("agent1")
    assert out.get("data_package", frozenset()) == frozenset()


def test_self_granted_item_stops_resolving_when_the_granter_loses_access(pg_env):
    """REQUIRED (b), real-Postgres proof: a self-granted data_package
    resolves while its GRANTER holds the grant, and stops resolving the
    moment the GRANTER's (not the owner's, not any caller's) grant is
    revoked."""
    from src.agent_scope_intersection import resolve_agent_authority
    from src.repositories import (
        agents_repo,
        data_packages_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    users_repo().create(id="owner1", email="owner1@test.com", name="Owner")
    users_repo().create(id="granter1", email="granter1@test.com", name="Granter")

    pkg_id = data_packages_repo().create(
        name="Granter Pkg", slug="c22-granter-pkg", description=None, icon=None, color=None, created_by="granter1"
    )

    groups = user_groups_repo()
    grp = groups.create(name="c22-granter-grp", description="test", created_by="test")
    user_group_members_repo().add_member("granter1", grp["id"], source="admin", added_by="test")
    resource_grants_repo().create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg_id, assigned_by="test", requirement="required"
    )

    agents_repo().create(
        id="agent1", owner_user_id="owner1", name="A", slug="c22-granter-agent", tables_mode="selected"
    )
    agents_repo().set_scope("agent1", [("data_package", pkg_id)], granted_by="granter1")

    out = resolve_agent_authority("agent1")
    assert out.get("data_package") == frozenset({pkg_id})

    # Revoke the GRANTER's own access.
    user_group_members_repo().remove_member("granter1", grp["id"])

    out_after = resolve_agent_authority("agent1")
    assert out_after.get("data_package", frozenset()) == frozenset()


def test_granted_by_preserved_across_a_replace_resolves_unconditionally_after_owner_edit(pg_env):
    """C2.2 must-handle, exercised through ``resolve_agent_authority`` (not
    just the repo layer covered in ``test_agents_contract.py``): an
    admin-granted package stays admin-granted — and therefore keeps
    resolving UNCONDITIONALLY — after the (non-admin) owner does an
    unrelated full-replace ``set_scope`` call, exactly the shape
    ``_sync_builder_scope`` produces on a builder-page save."""
    from src.agent_scope_intersection import resolve_agent_authority
    from src.repositories import agents_repo, data_packages_repo, user_group_members_repo, users_repo

    users_repo().create(id="owner1", email="owner1@test.com", name="Owner")
    users_repo().create(id="admin1", email="admin1@test.com", name="Admin")
    user_group_members_repo().add_member("admin1", _admin_group_id(pg_env), source="system_seed")

    pkg_id = data_packages_repo().create(
        name="Admin Pkg 2", slug="c22-preserve-pkg", description=None, icon=None, color=None, created_by="admin1"
    )

    agents_repo().create(
        id="agent1", owner_user_id="owner1", name="A", slug="c22-preserve-agent", tables_mode="selected"
    )
    agents_repo().set_scope("agent1", [("data_package", pkg_id)], granted_by="admin1")
    assert resolve_agent_authority("agent1").get("data_package") == frozenset({pkg_id})

    # Owner later re-saves the whole scope set (builder shape: preserved +
    # newly declared), re-declaring pkg_id unchanged.
    agents_repo().set_scope("agent1", [("data_package", pkg_id)], granted_by="owner1")

    out = resolve_agent_authority("agent1")
    assert out.get("data_package") == frozenset({pkg_id}), (
        "an admin-granted package must stay admin-granted (and resolve unconditionally) "
        "across an owner's later full-replace save"
    )
