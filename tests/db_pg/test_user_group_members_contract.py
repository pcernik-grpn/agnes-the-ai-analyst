"""Cross-engine contract tests for the user_group_members repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

This is the test that guards the read path the new ``POST /auth/refresh-groups``
endpoint depends on — Devin Review on PR #520 caught the original drift where
the endpoint diff-computed via a raw DuckDB ``conn.execute`` while
``apply_user_groups`` wrote through the repo factory (PG on use_pg()). The
endpoint now reads via ``user_group_members_repo().list_groups_with_meta_for_user``,
so this contract test pins down the read shape (including the ``source`` field
used for the synced-only filter) across both engines.

Pattern matches test_users_contract.py / test_audit_contract.py.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repos(tmp_path):
    """Returns ``(ug_repo, members_repo, users_repo, conn)``."""
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so
    # the session timezone is pinned to UTC — keeps `tests/db_pg/`'s
    # `test_no_bare_duckdb_connect_in_production_code` regression guard
    # quiet for new test additions.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return (
        UserGroupsRepository(conn),
        UserGroupMembersRepository(conn),
        UserRepository(conn),
        conn,
    )


def _make_pg_repos(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return PG repos."""
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.user_group_members_pg import (
        UserGroupMembersPgRepository,
    )
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    engine = db_pg.get_engine()
    return (
        UserGroupsPgRepository(engine),
        UserGroupMembersPgRepository(engine),
        UsersPgRepository(engine),
        None,
    )


@pytest.fixture(params=["duckdb", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(ug_repo, members_repo, users_repo, raw_conn_or_None, backend)``."""
    backend = request.param
    if backend == "duckdb":
        ug, members, users, conn = _make_duckdb_repos(tmp_path)
        yield ug, members, users, conn, backend
        if conn is not None:
            conn.close()
    else:
        ug, members, users, _ = _make_pg_repos(pg_engine, monkeypatch)
        yield ug, members, users, None, backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_user_and_two_groups(ug_repo, members_repo, users_repo):
    """Common seed: one user in one google_sync group + one admin group."""
    users_repo.create(id="u-1", email="alice@example.com", name="Alice")
    synced = ug_repo.ensure("eng-data@example.com")
    admin = ug_repo.ensure("custom-admin-group")
    members_repo.replace_google_sync_groups("u-1", [synced["id"]])
    # Non-google_sync membership added via the same low-level path the
    # admin UI uses (`add_member` mirrors the OAuth-callback shape
    # but with source='admin'). Both repos expose it.
    members_repo.add_member("u-1", admin["id"], source="admin")
    return synced, admin


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_list_groups_with_meta_for_user_returns_rows_for_both_engines(repos):
    """Empty user → empty list on both backends."""
    ug, members, users, _, _ = repos
    users.create(id="u-empty", email="empty@example.com", name="Empty")
    rows = members.list_groups_with_meta_for_user("u-empty")
    assert rows == []


def test_list_groups_with_meta_for_user_shape(repos):
    """Every row carries the contract fields with stable shapes — that's
    the read shape the refresh-groups endpoint and the /me/profile page
    depend on (see Devin Review on PR #520). The widened shape adds
    ``id`` (alias of ``group_id``), ``description`` and ``added_at`` while
    keeping the original five keys for back-compat."""
    ug, members, users, _, _ = repos
    _seed_user_and_two_groups(ug, members, users)

    rows = members.list_groups_with_meta_for_user("u-1")
    assert len(rows) == 2

    for row in rows:
        assert set(row.keys()) == {
            "group_id",
            "id",
            "name",
            "description",
            "is_system",
            "created_by",
            "source",
            "added_at",
        }, f"row keys drifted: {row.keys()}"
        assert isinstance(row["group_id"], str) and row["group_id"]
        # `id` aliases the group's own id — same value as group_id.
        assert row["id"] == row["group_id"]
        assert isinstance(row["name"], str) and row["name"]
        assert isinstance(row["is_system"], bool)
        # `created_by`, `source`, `description` are nullable text — accept
        # None or str. `added_at` is set on insert (timestamp); just
        # require the key is present (asserted above) and non-None here.
        assert row["added_at"] is not None
        # `ensure` always seeds a description, so it is a non-empty str here.
        assert isinstance(row["description"], str) and row["description"]


def test_list_groups_with_meta_filters_by_source_for_synced_diff(repos):
    """`source='google_sync'` filter on the read shape gives the synced
    subset — what the refresh-groups endpoint's `_synced_names()` does
    to compute the `added` / `removed` diff."""
    ug, members, users, _, _ = repos
    _seed_user_and_two_groups(ug, members, users)

    rows = members.list_groups_with_meta_for_user("u-1")
    synced = {r["name"] for r in rows if r["source"] == "google_sync"}
    everything = {r["name"] for r in rows}

    assert synced == {"eng-data@example.com"}
    assert everything == {"eng-data@example.com", "custom-admin-group"}


def test_list_groups_with_meta_ordering_system_groups_first(repos):
    """Both engines order system groups first, then by name. The
    refresh-groups endpoint sorts `current` itself, so it doesn't depend
    on this — but admin UI list views do, and parity here keeps both
    engines on the same display contract."""
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

    ug, members, users, _, _ = repos
    users.create(id="u-2", email="bob@example.com", name="Bob")

    # DuckDB's `_ensure_schema` seeds system groups; the PG alembic
    # baseline doesn't. Use `ensure_system` (idempotent get-or-create
    # with is_system=True) so the test works on both engines.
    sys_admin = ug.ensure_system(SYSTEM_ADMIN_GROUP, "System admins")
    sys_everyone = ug.ensure_system(SYSTEM_EVERYONE_GROUP, "Everyone")
    assert sys_admin and sys_everyone

    custom_b = ug.ensure("b-custom@example.com")
    custom_a = ug.ensure("a-custom@example.com")

    members.add_member("u-2", sys_admin["id"], source="admin")
    members.add_member("u-2", sys_everyone["id"], source="admin")
    members.add_member("u-2", custom_b["id"], source="admin")
    members.add_member("u-2", custom_a["id"], source="admin")

    rows = members.list_groups_with_meta_for_user("u-2")
    names = [r["name"] for r in rows]
    # System groups come first (admin / everyone, sorted by name within
    # is_system=True). Custom groups follow (a-custom before b-custom).
    is_system_flags = [r["is_system"] for r in rows]
    assert is_system_flags == sorted(is_system_flags, reverse=True), (
        f"system groups must come first on both engines, got: {names}"
    )
    # Within each is_system bucket, names are sorted ascending.
    system_block = [n for n, s in zip(names, is_system_flags) if s]
    custom_block = [n for n, s in zip(names, is_system_flags) if not s]
    assert system_block == sorted(system_block)
    assert custom_block == sorted(custom_block)
    assert custom_block == ["a-custom@example.com", "b-custom@example.com"]


def test_replace_google_sync_groups_is_idempotent(repos):
    """The OAuth callback / refresh-groups write path can run repeatedly —
    second call with the same group set must not duplicate or churn.
    `apply_user_groups` retries on transient Admin SDK hiccups, so this
    is hot path on both engines."""
    ug, members, users, _, _ = repos
    users.create(id="u-3", email="carol@example.com", name="Carol")
    g1 = ug.ensure("g1@example.com")
    g2 = ug.ensure("g2@example.com")

    members.replace_google_sync_groups("u-3", [g1["id"], g2["id"]])
    members.replace_google_sync_groups("u-3", [g1["id"], g2["id"]])  # same set

    rows = members.list_groups_with_meta_for_user("u-3")
    synced = {r["name"] for r in rows if r["source"] == "google_sync"}
    assert synced == {"g1@example.com", "g2@example.com"}


def test_replace_google_sync_groups_diff_membership(repos):
    """Removing a group from the synced set must drop the row on both
    engines. This is the `removed` half of the refresh-groups response."""
    ug, members, users, _, _ = repos
    users.create(id="u-4", email="dan@example.com", name="Dan")
    g_old = ug.ensure("old@example.com")
    g_new = ug.ensure("new@example.com")

    members.replace_google_sync_groups("u-4", [g_old["id"], g_new["id"]])
    members.replace_google_sync_groups("u-4", [g_new["id"]])  # drop g_old

    rows = members.list_groups_with_meta_for_user("u-4")
    synced = {r["name"] for r in rows if r["source"] == "google_sync"}
    assert synced == {"new@example.com"}, f"old@example.com should have been removed, got: {synced}"


def test_replace_microsoft_sync_groups_is_idempotent(repos):
    """The Microsoft OAuth callback write path can run repeatedly — same
    contract as replace_google_sync_groups above, distinct source."""
    ug, members, users, _, _ = repos
    users.create(id="u-3b", email="erin@example.com", name="Erin")
    g1 = ug.ensure("g1-ms@example.com", created_by="system:microsoft-sync")
    g2 = ug.ensure("g2-ms@example.com", created_by="system:microsoft-sync")

    members.replace_microsoft_sync_groups("u-3b", [g1["id"], g2["id"]])
    members.replace_microsoft_sync_groups("u-3b", [g1["id"], g2["id"]])  # same set

    rows = members.list_groups_with_meta_for_user("u-3b")
    synced = {r["name"] for r in rows if r["source"] == "microsoft_sync"}
    assert synced == {"g1-ms@example.com", "g2-ms@example.com"}


def test_replace_microsoft_sync_groups_diff_membership(repos):
    """Removing a group from the synced set drops the row on both engines."""
    ug, members, users, _, _ = repos
    users.create(id="u-4b", email="frank@example.com", name="Frank")
    g_old = ug.ensure("old-ms@example.com")
    g_new = ug.ensure("new-ms@example.com")

    members.replace_microsoft_sync_groups("u-4b", [g_old["id"], g_new["id"]])
    members.replace_microsoft_sync_groups("u-4b", [g_new["id"]])  # drop g_old

    rows = members.list_groups_with_meta_for_user("u-4b")
    synced = {r["name"] for r in rows if r["source"] == "microsoft_sync"}
    assert synced == {"new-ms@example.com"}, f"old-ms@example.com should have been removed, got: {synced}"


def test_google_and_microsoft_sync_sources_are_isolated(repos):
    """`replace_google_sync_groups` and `replace_microsoft_sync_groups` are
    both thin wrappers over the shared `replace_synced_groups` primitive —
    this pins that a refresh scoped to one `source` never touches the
    other's rows, on both engines. A user signed into both providers (or
    matched to the same account by address) keeps both snapshots."""
    ug, members, users, _, _ = repos
    users.create(id="u-dual", email="dual@example.com", name="Dual")
    g_google = ug.ensure("g-google@example.com", created_by="system:google-sync")
    g_ms = ug.ensure("g-microsoft@example.com", created_by="system:microsoft-sync")

    members.replace_google_sync_groups("u-dual", [g_google["id"]])
    members.replace_microsoft_sync_groups("u-dual", [g_ms["id"]])

    rows = members.list_groups_with_meta_for_user("u-dual")
    by_name = {r["name"]: r["source"] for r in rows}
    assert by_name == {
        "g-google@example.com": "google_sync",
        "g-microsoft@example.com": "microsoft_sync",
    }

    # Refreshing the Google side to empty must not touch the Microsoft row.
    members.replace_google_sync_groups("u-dual", [])
    rows = members.list_groups_with_meta_for_user("u-dual")
    assert {r["name"]: r["source"] for r in rows} == {"g-microsoft@example.com": "microsoft_sync"}


def test_list_google_sync_groups_for_user_shape_and_filter(repos):
    """`list_google_sync_groups_for_user` returns only the user's
    google_sync groups, ordered by name, each row ``{name, external_id}``.

    Parity gotcha: ``user_groups`` has no ``external_id`` column on
    Postgres, so the PG impl always returns ``external_id=None``. The
    DuckDB impl probes ``information_schema`` and falls back to NULL when
    the column is absent (it is on the contract schema), so both engines
    return ``external_id=None`` here — the assertion holds for both."""
    ug, members, users, _, backend = repos
    _seed_user_and_two_groups(ug, members, users)
    # u-1 is in `eng-data@example.com` (google_sync) + `custom-admin-group`
    # (admin). Only the synced one must come back.
    rows = members.list_google_sync_groups_for_user("u-1")

    assert [r["name"] for r in rows] == ["eng-data@example.com"]
    for r in rows:
        assert set(r.keys()) == {"name", "external_id"}, f"row keys drifted: {r.keys()}"
        # external_id is None on PG (no column) and on the DuckDB contract
        # schema (column absent → NULL fallback).
        assert r["external_id"] is None


def test_list_google_sync_groups_for_user_empty(repos):
    """A user with no google_sync rows yields an empty list on both
    engines (admin-only membership is excluded)."""
    ug, members, users, _, _ = repos
    users.create(id="u-only-admin", email="adminonly@example.com", name="AO")
    grp = ug.ensure("admin-only-grp")
    members.add_member("u-only-admin", grp["id"], source="admin")

    rows = members.list_google_sync_groups_for_user("u-only-admin")
    assert rows == []


def test_google_sync_summary_counts_and_last_added_at(repos):
    """`google_sync_summary` returns ``{count, last_added_at}`` over the
    user's ``source='google_sync'`` rows only — the admin membership must
    not be counted. Backs ``app.api.me_debug._last_sync_summary``."""
    ug, members, users, _, _ = repos
    synced, admin = _seed_user_and_two_groups(ug, members, users)

    summary = members.google_sync_summary("u-1")
    assert set(summary.keys()) == {"count", "last_added_at"}
    assert summary["count"] == 1
    assert summary["last_added_at"] is not None


def test_google_sync_summary_empty_user_returns_zero(repos):
    """A user with no google_sync rows gets count=0, last_added_at=None on
    both engines."""
    ug, members, users, _, _ = repos
    users.create(id="u-only-admin-2", email="adminonly2@example.com", name="AO2")
    grp = ug.ensure("admin-only-grp-2")
    members.add_member("u-only-admin-2", grp["id"], source="admin")

    summary = members.google_sync_summary("u-only-admin-2")
    assert summary == {"count": 0, "last_added_at": None}


def test_replace_group_members_for_source_segregation(repos):
    """`replace_group_members_for_source` is the group-oriented transpose of
    `replace_synced_groups` — a resource-driven sync (e.g. the SharePoint ACL
    mirror) computes one GROUP's full member set per sweep, rather than one
    user's group set per login. DELETE+INSERT is scoped to (group_id,
    source): an admin-added row on the same group survives, and a second
    call with a different user set replaces only its own source's rows —
    on both engines."""
    ug, members, users, _, _ = repos
    users.create(id="u-sp-1", email="sp1@example.com", name="SP1")
    users.create(id="u-sp-2", email="sp2@example.com", name="SP2")
    users.create(id="u-sp-3", email="sp3@example.com", name="SP3")
    group = ug.ensure("entra:g-123")

    members.add_member("u-sp-1", group["id"], source="admin", added_by="admin-user")
    members.replace_group_members_for_source(
        group["id"],
        ["u-sp-1", "u-sp-2"],
        source="sharepoint_sync",
        added_by="system:sharepoint-acl-sync",
    )
    # u-sp-1's admin row survives (ON CONFLICT DO NOTHING skips the dup
    # insert); u-sp-2 is newly added.
    member_ids = {r["id"] for r in members.list_members_for_group(group["id"])}
    assert member_ids == {"u-sp-1", "u-sp-2"}

    # Replacement removes only its own source's rows: u-sp-2 (sharepoint_sync)
    # is gone, u-sp-1 (admin) is untouched, u-sp-3 is newly added.
    members.replace_group_members_for_source(
        group["id"],
        ["u-sp-3"],
        source="sharepoint_sync",
        added_by="system:sharepoint-acl-sync",
    )
    member_ids = {r["id"] for r in members.list_members_for_group(group["id"])}
    assert member_ids == {"u-sp-1", "u-sp-3"}
