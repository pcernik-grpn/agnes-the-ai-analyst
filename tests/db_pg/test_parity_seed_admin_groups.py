"""Parity test for system-group seeding + seed-admin membership.

A fresh instance must end up with the ``Admin`` and ``Everyone`` system groups
and a seed admin who is actually a *member* of ``Admin`` (that membership is
what grants admin access). On DuckDB ``src.db._seed_system_groups`` handles the
groups on connect, but it never runs on Postgres — nothing seeded the groups
there, and the lifespan seed-admin path then looked the Admin group up off a
raw DuckDB connection, so the membership it wrote referenced a DuckDB-only
group id that does not exist on Postgres → the seed admin had no admin access.

The fix seeds the groups through the factory (``ensure_system``) and looks them
up through the factory (``get_by_name``). These tests exercise that exact
sequence and assert the seed admin resolves as an admin on DuckDB AND Postgres.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def _seed_like_lifespan(seed_admin_id: str, seed_email: str) -> None:
    """Replay the lifespan seed sequence through the factory (the fixed path)."""
    from src.db import _SYSTEM_GROUPS_SEED, SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    for name, desc in _SYSTEM_GROUPS_SEED:
        user_groups_repo().ensure_system(name, desc)

    users_repo().create(id=seed_admin_id, email=seed_email, name="Admin")

    for group_name in (SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP):
        grp = user_groups_repo().get_by_name(group_name)
        assert grp is not None, f"system group {group_name!r} not seeded"
        user_group_members_repo().add_member(
            user_id=seed_admin_id,
            group_id=grp["id"],
            source="system_seed",
            added_by="test",
        )


def test_system_groups_seeded_on_both_backends(_env):
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    _seed_like_lifespan("seed_admin", "seed@example.com")

    names = {g["name"] for g in user_groups_repo().list_all()}
    assert {SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP} <= names, f"[{_env}] system groups missing after seed: {names}"


def test_seed_admin_has_admin_access_on_both_backends(_env):
    from app.auth.access import is_user_admin

    _seed_like_lifespan("seed_admin", "seed@example.com")

    assert is_user_admin("seed_admin") is True, (
        f"[{_env}] seed admin lacks admin access — the Admin-group membership "
        f"did not resolve on this backend (the pre-fix raw-DuckDB group lookup "
        f"wrote a group id absent from Postgres)."
    )


def test_everyone_membership_resolves_on_both_backends(_env):
    from src.db import SYSTEM_EVERYONE_GROUP
    from app.auth.access import _user_group_ids
    from src.repositories import user_groups_repo

    _seed_like_lifespan("seed_admin", "seed@example.com")

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None
    assert everyone["id"] in _user_group_ids("seed_admin"), (
        f"[{_env}] seed admin not resolved into Everyone — Everyone-scoped grants would not surface for them."
    )


def test_ensure_everyone_membership_grants_on_both_backends(_env, monkeypatch):
    """Issue #748: ``app.auth.group_sync.ensure_everyone_membership`` routes
    through the ``src.repositories`` factory pair exclusively, so a
    creation-time grant must resolve identically on DuckDB and Postgres —
    same pattern as the seed-admin bug this file otherwise covers (a raw
    DuckDB group lookup writing an id absent from Postgres)."""
    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo
    from app.auth.group_sync import ensure_everyone_membership

    _seed_like_lifespan("seed_admin", "seed@example.com")
    users_repo().create(id="grant-check", email="grant-check@example.com", name="U")

    result = ensure_everyone_membership("grant-check", added_by="test:parity")
    assert result is True, f"[{_env}] ensure_everyone_membership returned False unexpectedly"

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None, f"[{_env}] Everyone group not resolvable"
    rows = user_group_members_repo().list_groups_with_meta_for_user("grant-check")
    matching = [r for r in rows if r["group_id"] == everyone["id"]]
    assert len(matching) == 1, f"[{_env}] expected exactly one Everyone row, got {matching}"
    assert matching[0]["source"] == "system_seed"


def test_ensure_everyone_membership_ignores_the_legacy_env_on_both_backends(_env, monkeypatch):
    """Single-mode, on both backends. It was dual-mode until 0098: setting
    ``AGNES_GROUP_EVERYONE_EMAIL`` mirrored the seeded row from a Workspace
    group, so writing a local membership would have fought the
    Workspace-authoritative set. That mapping is an ordinary synced group
    now, and the variable is inert — a stale value in an operator's env must
    not change who ends up in ``Everyone``."""
    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")
    from src.repositories import user_group_members_repo, users_repo
    from app.auth.group_sync import ensure_everyone_membership

    _seed_like_lifespan("seed_admin", "seed@example.com")
    users_repo().create(id="grant-check-mapped", email="grant-check-mapped@example.com", name="U")

    result = ensure_everyone_membership("grant-check-mapped", added_by="test:parity")
    assert result is True, f"[{_env}] the legacy env var must no longer suppress the auto-grant"

    rows = user_group_members_repo().list_groups_with_meta_for_user("grant-check-mapped")
    assert [r["name"] for r in rows] == ["Everyone"], f"[{_env}] got {rows}"


def test_per_connect_duckdb_seed_respects_backend_selection(_env):
    """Boot-path leak (found by the post-cutover DuckDB canary): the per-connect
    ``_seed_system_groups`` must never write Admin/Everyone into a local DuckDB
    on a Postgres instance. The invariant hardened: on Postgres the system
    DuckDB must NOT be opened at all, so ``get_system_db()`` raises rather than
    creating an (empty) file. On the DuckDB backend the per-connect seed keeps
    working (recovery contract: a deleted system group reappears on the next
    connect)."""
    from src.db import close_system_db, get_system_db

    close_system_db()

    if _env == "pg":
        with pytest.raises(RuntimeError):
            get_system_db()  # must refuse to open the system DuckDB on Postgres
        return

    conn = get_system_db()  # reopen → _ensure_schema runs under the DuckDB backend
    names = {row[0] for row in conn.execute("SELECT name FROM user_groups WHERE created_by = 'system:seed'").fetchall()}
    assert {"Admin", "Everyone"} <= names, f"[duck] per-connect seed did not run: {names}"


def test_canonical_memory_domains_seed_resolves_on_both_backends(_env):
    """Replay the lifespan's canonical memory-domain seed through the factory.
    Fresh Postgres instances previously had no canonical domains at all — the
    DuckDB ladder seed is (by design) skipped there, and Alembic seeds none.
    On DuckDB the ladder already seeded them, so the replay must no-op and
    resolve the very same deterministic ``md_<slug>`` rows."""
    from src.db import _CANONICAL_MEMORY_DOMAINS_SEED
    from src.repositories import memory_domains_repo

    repo = memory_domains_repo()
    for did, slug, name, icon, color in _CANONICAL_MEMORY_DOMAINS_SEED:
        repo.ensure_seed(domain_id=did, slug=slug, name=name, icon=icon, color=color)

    for did, slug, name, _icon, _color in _CANONICAL_MEMORY_DOMAINS_SEED:
        row = repo.get_by_slug(slug)
        assert row is not None, f"[{_env}] canonical domain {slug!r} missing after lifespan seed"
        assert row["id"] == did, f"[{_env}] canonical domain {slug!r} got non-deterministic id {row['id']}"
        assert row["name"] == name


def test_fresh_boot_opens_no_system_duckdb_on_pg_backend(_env):
    """Canary-style pin for the whole boot-seed class: with the Postgres state
    backend active, the system DuckDB must never be opened — not even the
    ``state/system.duckdb`` file. The invariant hardened from "opens an empty
    DuckDB, writes no rows" to "refuses to open at all": ``get_system_db()``
    raises ``RuntimeError`` on Postgres, and no file is created on disk."""
    if _env != "pg":
        pytest.skip("PG-only — on the DuckDB backend the system DuckDB is the store")
    from pathlib import Path

    from src.db import _get_state_dir, close_system_db, get_system_db

    close_system_db()
    db_file = Path(_get_state_dir()) / "system.duckdb"

    with pytest.raises(RuntimeError):
        get_system_db()

    assert not db_file.exists(), (
        f"system.duckdb was created on a Postgres instance at {db_file}"
    )


def test_ensure_system_creates_absent_group_both_backends(_env):
    """The fresh-PG bug was that nothing *creates* the system groups on Postgres
    (Admin/Everyone are protected from deletion + the fixtures pre-seed them, so
    they can't be removed to simulate empty). Exercise the create path the
    lifespan relies on directly: ``ensure_system`` on a name that doesn't exist
    yet must CREATE it (not just promote) as a system group — on both backends."""
    from src.repositories import user_groups_repo

    repo = user_groups_repo()
    assert repo.get_by_name("ProbeSysGroup") is None, f"[{_env}] probe group pre-exists"

    repo.ensure_system("ProbeSysGroup", "probe system group")

    grp = repo.get_by_name("ProbeSysGroup")
    assert grp is not None, f"[{_env}] ensure_system did not create the absent group"
    assert grp["is_system"] is True, f"[{_env}] created group is not is_system"
