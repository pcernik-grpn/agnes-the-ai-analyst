"""Cross-engine contract tests for the agents repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agents import AgentsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.agents_pg import AgentsPgRepository

    return AgentsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def _make_duckdb_stack(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agents import AgentsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return (
        AgentsRepository(conn),
        UserGroupsRepository(conn),
        UserGroupMembersRepository(conn),
        ResourceGrantsRepository(conn),
        conn,
    )


def _make_pg_stack(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.agents_pg import AgentsPgRepository
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    return (
        AgentsPgRepository(engine),
        UserGroupsPgRepository(engine),
        UserGroupMembersPgRepository(engine),
        ResourceGrantsPgRepository(engine),
        None,
    )


@pytest.fixture(params=["duckdb", "pg"])
def stack(request, tmp_path, pg_engine, monkeypatch):
    """``(agents_repo, user_groups_repo, user_group_members_repo,
    resource_grants_repo)`` sharing one connection/engine — for tests that
    need to seed groups + grants alongside agent rows (C2.3
    ``get_runnable_by_slug``)."""
    if request.param == "duckdb":
        agents, groups, members, grants, conn = _make_duckdb_stack(tmp_path)
        yield agents, groups, members, grants
        conn.close()
    else:
        agents, groups, members, grants, _ = _make_pg_stack(pg_engine, monkeypatch)
        yield agents, groups, members, grants


def test_get_runnable_by_slug_owned(stack):
    agents, _groups, _members, _grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    row = agents.get_runnable_by_slug("u1", "finance")
    assert row is not None and row["id"] == "a1"


def test_get_runnable_by_slug_owner_may_also_address_by_id(stack):
    """ "Runtime paths accept slug or id" holds for the OWNER too, not only
    a grantee — addressing by id must not require a grant just because it
    took the id-shaped branch."""
    agents, _groups, _members, _grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    row = agents.get_runnable_by_slug("u1", "a1")
    assert row is not None and row["id"] == "a1"


def test_get_runnable_by_slug_denies_stranger(stack):
    """Neither the owner's slug nor the agent's id resolve for a user with
    no ownership and no grant — this is the pre-C2.3 behavior (404
    everywhere) and must stay true absent a grant."""
    agents, _groups, _members, _grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    assert agents.get_runnable_by_slug("u2", "finance") is None
    assert agents.get_runnable_by_slug("u2", "a1") is None


def test_get_runnable_by_slug_unknown_returns_none(stack):
    """A slug/id that matches no agent at all resolves to None on both
    backends — the outright miss branch, distinct from an agent that exists
    but the caller may not reach."""
    agents, _groups, _members, _grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    assert agents.get_runnable_by_slug("u1", "does-not-exist") is None
    assert agents.get_runnable_by_slug("u1", "a9999") is None


def test_get_runnable_by_slug_via_group_grant_resolves_by_id(stack):
    """A shared agent is addressed by its id, not the owner's slug — slug
    is only unique per-owner, so it is meaningless in a grantee's
    namespace."""
    agents, groups, members, grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    group = groups.create(name="finance-team", created_by="admin")
    members.add_member("u2", group["id"], source="admin")
    grants.create(group["id"], "agent", "a1", assigned_by="admin")

    row = agents.get_runnable_by_slug("u2", "a1")
    assert row is not None and row["id"] == "a1"
    # borrowing the OWNER's slug string must not resolve for the grantee
    assert agents.get_runnable_by_slug("u2", "finance") is None


def test_get_runnable_by_slug_excludes_soft_deleted(stack):
    agents, groups, members, grants = stack
    agents.create(id="a1", owner_user_id="u1", name="A", slug="finance")
    group = groups.create(name="finance-team", created_by="admin")
    members.add_member("u2", group["id"], source="admin")
    grants.create(group["id"], "agent", "a1", assigned_by="admin")
    agents.soft_delete("a1")

    assert agents.get_runnable_by_slug("u2", "a1") is None
    assert agents.get_runnable_by_slug("u1", "finance") is None


def test_create_get_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="Sales reporter", slug="sales-reporter")
    row = repo.get_by_slug("u1", "sales-reporter")
    assert row["id"] == "a1" and row["plugins_mode"] == "all"
    assert row["memory_write_mode"] == "propose"


def test_slug_unique_per_owner(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    with pytest.raises(Exception):
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")
    repo.create(id="a3", owner_user_id="u2", name="C", slug="x")  # other owner OK


def test_soft_delete_tombstones_slug(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.soft_delete("a1")
    assert repo.get_by_slug("u1", "x") is None  # invisible to runtime
    assert repo.get_by_id("a1")["deleted_at"] is not None
    with pytest.raises(Exception):  # slug never reused
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")


def test_get_or_create_default_idempotent(repo):
    d1 = repo.get_or_create_default("u1")
    d2 = repo.get_or_create_default("u1")
    assert d1["id"] == d2["id"] and d1["is_default"] is True
    assert len(repo.list_for_user("u1")) == 1


def test_get_or_create_default_revives_soft_deleted(repo):
    """A soft-deleted default agent is revived, not re-inserted.

    The `(owner_user_id, slug)` UNIQUE spans soft-deleted rows, so the
    seeded `slug='default'` row survives deletion. Re-inserting it would
    raise a ConstraintException on EVERY subsequent call — permanently
    breaking web chat, whose session create resolves the default agent
    first (`app/api/chat.py::_default_agent_id`).
    """
    d1 = repo.get_or_create_default("u1")
    repo.soft_delete(d1["id"])
    assert repo.get_by_id(d1["id"])["deleted_at"] is not None

    revived = repo.get_or_create_default("u1")
    assert revived["id"] == d1["id"]  # same row, history preserved
    assert revived["is_default"] is True
    assert revived["deleted_at"] is None
    assert len(repo.list_for_user("u1")) == 1


def test_get_or_create_default_revives_a_default_seeded_under_a_suffixed_slug(repo):
    """The revive keys on `is_default`, not on the literal slug.

    When a live non-default agent already holds `slug='default'`, the seeder
    lands the default on `default-2`. A slug-keyed revive would miss that
    tombstone, strand the id `chat_sessions.agent_id` points at, and seed a
    fresh duplicate on every delete cycle.
    """
    repo.create(id="squatter", owner_user_id="u1", name="Default", slug="default")
    seeded = repo.get_or_create_default("u1")
    assert seeded["slug"] == "default-2"

    repo.soft_delete(seeded["id"])
    revived = repo.get_or_create_default("u1")

    assert revived["id"] == seeded["id"]  # same row, not a duplicate
    assert revived["deleted_at"] is None
    # No third agent was invented.
    assert len(repo.list_for_user("u1")) == 2


def test_get_or_create_default_sidesteps_live_default_slug(repo):
    """A live non-default agent already holding `slug='default'` is left alone.

    Reviving it would silently promote one of the owner's own agents to be
    their default. Seed the new default under the next free slug instead.
    """
    repo.create(id="a1", owner_user_id="u1", name="Default", slug="default")

    seeded = repo.get_or_create_default("u1")
    assert seeded["id"] != "a1"
    assert seeded["is_default"] is True
    assert seeded["slug"] != "default"
    assert repo.get_by_id("a1")["is_default"] is False


def test_scope_replace_all(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.set_scope("a1", [("plugin", "p1"), ("table", "t1")])
    repo.set_scope("a1", [("plugin", "p2")])
    # `granted_by` defaults to None on both backends when the caller doesn't
    # pass one (C2.1) — see test_granted_by_* below for the PG-persists /
    # DuckDB-drops split.
    assert repo.get_scope("a1") == [{"item_type": "plugin", "item_id": "p2", "granted_by": None}]


def test_get_scope_for_agents_batches_the_same_rows(repo):
    """The list endpoint projects every visible agent's declaration, hydrating
    an empty axis from `agent_scope`. Per-agent reads made that an N+1, so the
    batched read has to agree with `get_scope` row for row — on both backends,
    since a divergence here would show as a wrong declaration on one engine
    only."""
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.create(id="a2", owner_user_id="u1", name="B", slug="y")
    repo.create(id="a3", owner_user_id="u1", name="C", slug="z")  # no scope rows
    repo.set_scope("a1", [("plugin", "p1"), ("table", "t1")])
    repo.set_scope("a2", [("data_package", "pkg1")])

    batched = repo.get_scope_for_agents(["a1", "a2", "a3"])

    assert batched["a1"] == repo.get_scope("a1")
    assert batched["a2"] == repo.get_scope("a2")
    # An agent with no rows is simply absent — callers use .get(id, []).
    assert "a3" not in batched
    assert repo.get_scope("a3") == []
    # Unknown ids and an empty request are both benign.
    assert repo.get_scope_for_agents(["nope"]) == {}
    assert repo.get_scope_for_agents([]) == {}


def test_update_whitelist(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.update("a1", name="B", model="claude-sonnet-5", plugins_mode="selected")
    row = repo.get_by_id("a1")
    assert row["name"] == "B" and row["plugins_mode"] == "selected"
    with pytest.raises(ValueError):
        repo.update("a1", owner_user_id="u2")  # not whitelisted


def test_update_whitelists_are_identical_across_backends():
    """The two ``_UPDATABLE`` sets are hand-maintained copies.

    Nothing else compares them, so widening one (as the placeholder-slug
    rename had to) silently leaves the other backend raising ValueError on
    the very write the feature depends on — a one-backend outage the
    parametrized tests below cannot see, because each backend only ever
    exercises its own copy.
    """
    from src.repositories.agents import _UPDATABLE as duck
    from src.repositories.agents_pg import _UPDATABLE as pg

    assert duck == pg, f"only in DuckDB: {sorted(duck - pg)}; only in Postgres: {sorted(pg - duck)}"


def test_slug_is_updatable_on_both_backends(repo):
    """Renaming a draft re-derives its slug (app/api/agents_admin.py).

    That write goes through ``update``, so ``slug`` must be whitelisted —
    and the row must remain addressable under the NEW slug and gone from
    the old one, since the slug is the public address.
    """
    repo.create(id="a1", owner_user_id="u1", name="", slug="agent")
    repo.update("a1", name="Revenue Analyst", slug="revenue-analyst")
    assert repo.get_by_slug("u1", "revenue-analyst")["id"] == "a1"
    assert repo.get_by_slug("u1", "agent") is None


def test_builder_superset_roundtrip(repo):
    """v111 paper-theme builder superset — create + update + read the authored
    fields on the same canonical row that holds main's agent-as-API columns."""
    repo.create(
        id="a1",
        owner_user_id="u1",
        name="Analyst",
        slug="analyst",
        system_prompt="be precise",
        role="data analyst",
        tone="warm",
        greeting="hi there",
        knowledge='["k1", "k2"]',
        plugins='["p1"]',
        surfaces='{"chat": true}',
        status="ready",
    )
    row = repo.get_by_id("a1")
    assert row["role"] == "data analyst"
    assert row["tone"] == "warm"
    assert row["greeting"] == "hi there"
    assert row["knowledge"] == '["k1", "k2"]'
    assert row["plugins"] == '["p1"]'
    assert row["surfaces"] == '{"chat": true}'
    assert row["status"] == "ready"
    # The builder maps instructions -> system_prompt on the same table.
    assert row["system_prompt"] == "be precise"

    # Superset columns are whitelisted for update; the JSON payloads are opaque.
    repo.update("a1", role="senior analyst", knowledge='["k3"]', status="draft")
    row = repo.get_by_id("a1")
    assert row["role"] == "senior analyst"
    assert row["knowledge"] == '["k3"]'
    assert row["status"] == "draft"


def test_builder_defaults_and_slug_picker(repo):
    """A create with no builder fields lands the column DEFAULTs, and the slug
    picker sees tombstones via include_deleted."""
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    row = repo.get_by_id("a1")
    assert row["tone"] == "concise"
    assert row["knowledge"] == "[]"
    assert row["surfaces"] == "{}"
    assert row["status"] == "draft"
    repo.soft_delete("a1")
    assert repo.get_by_slug("u1", "x") is None
    assert repo.get_by_slug("u1", "x", include_deleted=True) is not None


def test_scope_snapshot_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.record_scope_snapshot(id="s1", session_id="c1", agent_id="a1", effective_scope='{"tables": ["t1"]}')
    snaps = repo.list_scope_snapshots("c1")
    assert len(snaps) == 1 and snaps[0]["effective_scope"] == '{"tables": ["t1"]}'


def test_agent_for_scope_item_finds_the_binding_holder(repo):
    repo.create(id="a-route", owner_user_id="u1", name="Router", slug="router")
    repo.set_scope("a-route", [("slack_channel", "C123"), ("plugin", "p1")])
    hit = repo.agent_for_scope_item("slack_channel", "C123")
    assert hit is not None and hit["id"] == "a-route"
    # different item id / type miss
    assert repo.agent_for_scope_item("slack_channel", "C999") is None
    assert repo.agent_for_scope_item("plugin", "C123") is None


def test_agent_for_scope_item_skips_deleted_agents(repo):
    repo.create(id="a-gone", owner_user_id="u1", name="Gone", slug="gone")
    repo.set_scope("a-gone", [("slack_channel", "C777")])
    repo.soft_delete("a-gone")
    assert repo.agent_for_scope_item("slack_channel", "C777") is None


# ---------------------------------------------------------------------------
# C2.1 — agent_scope.granted_by. A genuine schema change on an existing pair
# is PG-only under the A3 ratchet
# (`.claude/skills/agnes-conventions/references/migration.md` -> "Adding a
# PG-only feature"; `docs/migrations.md` -> "Extending an EXISTING (frozen
# pre-A3) pair"): the column lives on Postgres alone, so these two tests are
# deliberately NOT parametrized over the shared `repo` fixture — each drives
# its own backend directly to pin the intentional asymmetry (persists on PG,
# silently dropped on DuckDB, same call shape either way).
# ---------------------------------------------------------------------------


def test_granted_by_persists_on_postgres(pg_engine, monkeypatch):
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.set_scope("a1", [("table", "t1"), ("plugin", "p1")], granted_by="admin1")
    assert repo.get_scope("a1") == [
        {"item_type": "plugin", "item_id": "p1", "granted_by": "admin1"},
        {"item_type": "table", "item_id": "t1", "granted_by": "admin1"},
    ]
    # A later replace with a different writer re-attributes every row this
    # call writes that is NEW — one `set_scope` call has exactly one writer
    # for the rows it actually introduces. "p2" was never declared before,
    # so it is attributed to the new writer.
    repo.set_scope("a1", [("plugin", "p2")], granted_by="owner1")
    assert repo.get_scope("a1") == [{"item_type": "plugin", "item_id": "p2", "granted_by": "owner1"}]


def test_granted_by_is_preserved_across_a_replace_for_unchanged_rows(pg_engine, monkeypatch):
    """C2.2 must-handle: a full-replace `set_scope` call that re-declares a
    row UNCHANGED must not re-attribute it to the new writer.

    `_sync_builder_scope` (`app/api/agents_builder_shared.py`) reads back
    every governance-owned row it does not itself own (`preserved`) and
    passes it straight through `set_scope` alongside the builder's own
    `declared` items — one call, one `granted_by` kwarg. Without this
    preservation, an admin-granted `data_package` row would silently
    downgrade to owner-granted (and lose D-C2's unconditioned resolution)
    the next time the (non-admin) owner saves the builder page for an
    unrelated reason (adding a plugin, say) — see
    `docs/superpowers/plans/2026-08-26-one-agent-model.md` C2.2's
    must-handle note.
    """
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    repo.create(id="a1", owner_user_id="owner1", name="A", slug="x")

    # Admin grants a data_package.
    repo.set_scope("a1", [("data_package", "pkg1")], granted_by="admin1")
    assert repo.get_scope("a1") == [{"item_type": "data_package", "item_id": "pkg1", "granted_by": "admin1"}]

    # Owner later does a full-replace save touching an unrelated axis
    # (adding a plugin), re-declaring pkg1 unchanged alongside it — the
    # exact shape `_sync_builder_scope` produces (preserved + declared).
    repo.set_scope("a1", [("data_package", "pkg1"), ("plugin", "p1")], granted_by="owner1")

    scope = {(r["item_type"], r["item_id"]): r["granted_by"] for r in repo.get_scope("a1")}
    assert scope[("data_package", "pkg1")] == "admin1"  # UNCHANGED — still admin-granted
    assert scope[("plugin", "p1")] == "owner1"  # genuinely new -> attributed to this writer


def test_granted_by_is_dropped_on_duckdb(tmp_path):
    """DuckDB has no `granted_by` column: `set_scope` accepts the kwarg so
    both call sites (`app/api/agents_admin.py`,
    `app/api/agents_builder_shared.py`) work unmodified regardless of the
    active backend, but the write is a documented no-op here — the DuckDB
    side of this pair gains no capability that depends on the column."""
    repo, conn = _make_duckdb_repo(tmp_path)
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.set_scope("a1", [("table", "t1")], granted_by="admin1")
    assert repo.get_scope("a1") == [{"item_type": "table", "item_id": "t1", "granted_by": None}]
    conn.close()
