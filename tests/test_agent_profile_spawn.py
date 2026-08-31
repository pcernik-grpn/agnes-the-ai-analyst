"""Task 7: spawn-time agent persona profile + scope snapshot.

Covers `app/chat/agent_profile.py` (pure unit tests) and the
`ChatManager._spawn_live` seam that consumes it (integration, real DuckDB +
FakeHandle/FakeProvider — same fixture shape as
`tests/test_chat_manager.py::test_spawn_live_happy_path_does_not_kill_sandbox`).

The most important invariant: the seeded default agent (`system_prompt=''`)
must leave web chat's spawn-time behavior bit-for-bit unchanged — no
persona override, generic rails intact.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat import agent_profile
from app.chat.profiles import ChatProfile

# ---------------------------------------------------------------------------
# build_profile
# ---------------------------------------------------------------------------


def _agent_row(**overrides) -> dict:
    row = {
        "id": "agent-1",
        "slug": "sales-helper",
        "name": "Sales Helper",
        "description": "Helps qualify inbound leads.",
        "system_prompt": "",
        "plugins_mode": "all",
        "connections_mode": "all",
        "tables_mode": "all",
        "memory_mode": "all",
    }
    row.update(overrides)
    return row


def test_build_profile_none_on_empty_prompt():
    assert agent_profile.build_profile(_agent_row(system_prompt="")) is None


def test_build_profile_none_on_whitespace_prompt():
    assert agent_profile.build_profile(_agent_row(system_prompt="   \n\t  ")) is None


def test_build_profile_none_on_missing_prompt_key():
    row = _agent_row()
    del row["system_prompt"]
    assert agent_profile.build_profile(row) is None


def test_build_profile_returns_chat_profile_with_matching_claude_md():
    row = _agent_row(system_prompt="You are a sales qualification assistant.\nAlways ask for budget.")
    profile = agent_profile.build_profile(row)
    assert isinstance(profile, ChatProfile)
    # The persona leads — verbatim, and first, so nothing dilutes the
    # authored framing ("You are ...").
    assert profile.claude_md.startswith(row["system_prompt"])
    assert profile.slug == "agent-sales-helper"
    assert profile.skill_name == "agnes-agent-context"


def test_build_profile_appends_data_access_rails():
    """A persona replaces the workspace CLAUDE.md, so without this the
    agent loses every pointer to `agnes catalog` and goes hunting for the
    org's data in whatever other MCP servers its scope exposes."""
    row = _agent_row(system_prompt="You write poems.")
    profile = agent_profile.build_profile(row)
    md = profile.claude_md
    assert agent_profile.DATA_ACCESS_RAILS in md
    # The discovery chain an agent needs to answer any data question.
    for cmd in ("agnes catalog", "agnes schema", "agnes describe", "agnes query"):
        assert cmd in md
    # Metric definitions are canonical, never invented.
    assert "agnes catalog --metrics" in md
    # Depth is one command away rather than inlined.
    assert "agnes skills show agnes-data-querying" in md


def test_build_profile_rails_are_not_added_without_a_persona():
    """No persona -> no materialized CLAUDE.md at all; the workspace rails
    stay symlinked in. The floor must not resurrect a profile here."""
    assert agent_profile.build_profile(_agent_row(system_prompt="")) is None


def _seed_retail_model():
    from src.repositories import semantic_model_repo

    doc = (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        "  - name: retail\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "        fields: []\n"
    )
    semantic_model_repo().upsert(
        id="manual/_/retail",
        slug="retail",
        name="retail",
        description="Retail sales model.",
        document=doc,
        document_json={
            "semantic_model": [
                {
                    "name": "retail",
                    "ai_context": {"instructions": "Always exclude test orders."},
                    "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}],
                }
            ]
        },
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


def test_build_profile_appends_a_readable_semantic_model_when_user_email_is_given(tmp_path, monkeypatch):
    """P1-3: a named agent profile REPLACES the workspace CLAUDE.md, so
    without this it got zero semantic-layer context while the sandbox path
    (`src/claude_md.py`) always has — asserting `DATA_ACCESS_RAILS in md`
    alone would not catch this section being silently empty."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    from src.repositories import user_groups_repo, users_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    users_repo().create(id="u1", email="reader@example.com", name="Reader")
    conn = get_system_db()
    admin_group = user_groups_repo().get_by_name("Admin")
    UserGroupMembersRepository(conn).add_member("u1", admin_group["id"], source="test")
    conn.close()

    _seed_retail_model()

    row = _agent_row(system_prompt="You are a sales assistant.")
    profile = agent_profile.build_profile(row, user_email="reader@example.com")
    md = profile.claude_md
    assert "## Semantic layer" in md
    assert "retail" in md
    assert "Always exclude test orders." in md


def test_build_profile_omits_semantic_model_the_user_cannot_read(tmp_path, monkeypatch):
    """P1-3 RBAC: this section reuses ``_can_read_model`` — admin, a grant on
    the model, or a grant on a linked Data Package — same as
    ``GET /api/semantic-models/search``. A non-admin user with none of those
    must see nothing, or the persona would leak a model's existence and its
    author's ``ai_context.instructions`` to someone `/api/semantic-models`
    would refuse outright."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import users_repo

    users_repo().create(id="u2", email="outsider@example.com", name="Outsider")
    _seed_retail_model()

    row = _agent_row(system_prompt="You are a sales assistant.")
    profile = agent_profile.build_profile(row, user_email="outsider@example.com")
    md = profile.claude_md
    assert "## Semantic layer" not in md
    assert "retail" not in md


def test_build_profile_omits_semantic_layer_section_without_user_email():
    """Default behavior (no ``user_email``) must stay unchanged — no section,
    no lookup attempted."""
    row = _agent_row(system_prompt="You are a sales assistant.")
    profile = agent_profile.build_profile(row)
    assert "## Semantic layer" not in profile.claude_md


def test_build_profile_appends_facts_rails_when_the_switch_is_on(monkeypatch):
    """A persona REPLACES the workspace CLAUDE.md wholesale, so the
    `facts.enabled`-gated "Facts — entity and relationship questions" section
    the default Workspace Prompt carries (`config/claude_md_template.txt`)
    never reached a persona'd agent — it was told its data lives behind
    `agnes catalog` alone and had no pointer to the fact tools, so a
    who/what/relationship question it could have answered via `fact_search`
    failed instead. See docs/superpowers/runs/2026-08-28-run-p-planted.md for
    the same failure mode on the default (non-persona) path."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    row = _agent_row(system_prompt="You write poems.")
    profile = agent_profile.build_profile(row)
    md = profile.claude_md
    assert "fact_search" in md
    assert "fact_neighbors" in md
    assert "fact_claims" in md
    assert "agnes facts search" in md
    # Additive, not a replacement — the agent still needs the table rails.
    assert agent_profile.DATA_ACCESS_RAILS in md
    # The facts guidance follows the catalog guidance, so a who/what/
    # relationship question is steered to the more specific tool.
    assert md.index(agent_profile.DATA_ACCESS_RAILS) < md.index("fact_search")


def test_build_profile_omits_facts_rails_when_the_switch_is_off(monkeypatch):
    """An instance with `facts` off must not steer a persona toward tools
    that would all 404 (`app.auth.access.require_facts_enabled`)."""
    monkeypatch.delenv("AGNES_FACTS_ENABLED", raising=False)
    row = _agent_row(system_prompt="You write poems.")
    profile = agent_profile.build_profile(row)
    md = profile.claude_md
    assert "fact_search" not in md
    assert "Facts —" not in md
    # The catalog guidance is unaffected by the switch.
    assert agent_profile.DATA_ACCESS_RAILS in md


def test_build_profile_skill_body_is_valid_skill_md():
    row = _agent_row(system_prompt="Be helpful.")
    profile = agent_profile.build_profile(row)
    assert profile.skill_body.startswith("---\n")
    assert "name: agnes-agent-context\n" in profile.skill_body
    assert "description:" in profile.skill_body
    # identity content
    assert row["name"] in profile.skill_body
    assert "scoped by" in profile.skill_body.lower()


# ---------------------------------------------------------------------------
# compute_effective_scope
# ---------------------------------------------------------------------------


def test_compute_effective_scope_all_modes():
    row = _agent_row(plugins_mode="all", connections_mode="all", tables_mode="all", memory_mode="all")
    scope = agent_profile.compute_effective_scope(row, scope_items=[])
    assert scope == {
        "plugins": "all",
        "connections": "all",
        "tables": "all",
        "memory_domains": "all",
    }


def test_compute_effective_scope_selected_modes_map_ids():
    row = _agent_row(
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    items = [
        {"item_type": "plugin", "item_id": "p2"},
        {"item_type": "plugin", "item_id": "p1"},
        {"item_type": "connection", "item_id": "c1"},
        {"item_type": "table", "item_id": "t1"},
        {"item_type": "memory_domain", "item_id": "m1"},
    ]
    scope = agent_profile.compute_effective_scope(row, items)
    assert scope == {
        "plugins": ["p1", "p2"],  # sorted
        "connections": ["c1"],
        "tables": ["t1"],
        "memory_domains": ["m1"],
    }


def test_compute_effective_scope_mixed_modes():
    row = _agent_row(plugins_mode="selected", connections_mode="all", tables_mode="selected", memory_mode="all")
    items = [
        {"item_type": "plugin", "item_id": "p1"},
        {"item_type": "table", "item_id": "t1"},
    ]
    scope = agent_profile.compute_effective_scope(row, items)
    assert scope == {
        "plugins": ["p1"],
        "connections": "all",
        "tables": ["t1"],
        "memory_domains": "all",
    }


def test_compute_effective_scope_selected_with_no_items_is_empty_list():
    row = _agent_row(plugins_mode="selected")
    scope = agent_profile.compute_effective_scope(row, scope_items=[])
    assert scope["plugins"] == []


def test_compute_effective_scope_unrecognized_mode_fails_closed_and_warns(caplog):
    """An unrecognized *_mode value must resolve to an empty selection
    (fail CLOSED, V1d) — matching live enforcement
    (src/agent_scope_intersection.py) so the audit view never disagrees
    with what is actually enforced. Must not be silent — a logger.warning
    naming the field and the bad value is the only trace an admin has of
    config drift."""
    row = _agent_row(plugins_mode="bogus-mode")
    with caplog.at_level("WARNING", logger="app.chat.agent_profile"):
        scope = agent_profile.compute_effective_scope(row, scope_items=[])
    assert scope["plugins"] == []
    assert any("plugins_mode" in record.message and "bogus-mode" in record.message for record in caplog.records), (
        caplog.text
    )


# ---------------------------------------------------------------------------
# record_snapshot — real DuckDB (agents_repo() fixture style, per
# tests/test_agents_management_api.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def agents_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import agents_repo

    return agents_repo()


def test_record_snapshot_writes_a_row(agents_env):
    owner_id = "owner-1"
    agent_id = str(uuid.uuid4())
    agents_env.create(
        id=agent_id,
        owner_user_id=owner_id,
        name="Selected Agent",
        slug="selected-agent",
        system_prompt="Be sharp.",
        plugins_mode="selected",
        connections_mode="all",
        tables_mode="selected",
        memory_mode="all",
    )
    agents_env.set_scope(agent_id, [("plugin", "p1"), ("table", "t1")])
    row = agents_env.get_by_id(agent_id)

    session_id = "chat_" + str(uuid.uuid4())
    agent_profile.record_snapshot(session_id, row)

    snaps = agents_env.list_scope_snapshots(session_id)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["agent_id"] == agent_id
    assert snap["session_id"] == session_id
    effective = json.loads(snap["effective_scope"])
    assert effective == {
        "plugins": ["p1"],
        "connections": "all",
        "tables": ["t1"],
        "memory_domains": "all",
    }


def test_record_snapshot_skips_write_for_default_agent_all_modes(agents_env):
    """The default agent's baseline shape (is_default + every *_mode ==
    'all') carries no audit information — its effective scope is fully
    derivable from the row itself — so a snapshot row must not be written.
    Without this guard, every web-chat spawn against the default agent
    would accrue one redundant `agent_scope_snapshots` row, unbounded."""
    agent_id = str(uuid.uuid4())
    agents_env.create(
        id=agent_id,
        owner_user_id="owner-default",
        name="Default",
        slug="default",
        is_default=True,
    )
    row = agents_env.get_by_id(agent_id)
    assert row["is_default"]

    session_id = "chat_" + str(uuid.uuid4())
    agent_profile.record_snapshot(session_id, row)

    assert agents_env.list_scope_snapshots(session_id) == []


def test_record_snapshot_writes_for_default_agent_with_selected_mode(agents_env):
    """Defensive case: even a default-flagged row with a non-'all' mode
    (should not happen in practice, but the row is data, not a guarantee)
    carries real information and must still be recorded."""
    agent_id = str(uuid.uuid4())
    agents_env.create(
        id=agent_id,
        owner_user_id="owner-default-2",
        name="Default",
        slug="default-2",
        is_default=True,
        plugins_mode="selected",
    )
    agents_env.set_scope(agent_id, [("plugin", "p1")])
    row = agents_env.get_by_id(agent_id)
    assert row["is_default"]

    session_id = "chat_" + str(uuid.uuid4())
    agent_profile.record_snapshot(session_id, row)

    assert len(agents_env.list_scope_snapshots(session_id)) == 1


def test_record_snapshot_writes_for_non_default_agent_all_modes(agents_env):
    """A non-default agent with all-'all' modes still gets a row — the
    growth-bound skip only applies to the default agent."""
    agent_id = str(uuid.uuid4())
    agents_env.create(
        id=agent_id,
        owner_user_id="owner-nondefault",
        name="Not Default",
        slug="not-default",
        is_default=False,
    )
    row = agents_env.get_by_id(agent_id)
    assert not row["is_default"]

    session_id = "chat_" + str(uuid.uuid4())
    agent_profile.record_snapshot(session_id, row)

    assert len(agents_env.list_scope_snapshots(session_id)) == 1


def test_record_snapshot_swallows_repo_exceptions(agents_env, monkeypatch):
    owner_id = "owner-2"
    agent_id = str(uuid.uuid4())
    agents_env.create(id=agent_id, owner_user_id=owner_id, name="A", slug="a-agent")
    row = agents_env.get_by_id(agent_id)

    class _BoomRepo:
        def get_scope(self, agent_id):
            return []

        def record_scope_snapshot(self, **kwargs):
            raise RuntimeError("db exploded")

    monkeypatch.setattr("src.repositories.agents_repo", lambda: _BoomRepo())

    # must not raise
    agent_profile.record_snapshot("chat_boom", row)


def test_record_snapshot_swallows_missing_agent_id_key():
    # A malformed row (no "id") must not propagate an exception either.
    agent_profile.record_snapshot("chat_x", {"system_prompt": "hi"})


# ---------------------------------------------------------------------------
# Integration seam — ChatManager._spawn_live (real DuckDB, FakeHandle;
# mirrors tests/test_chat_manager.py's `manager`/FakeHandle fixture shape)
# ---------------------------------------------------------------------------


def _make_workdir_mgr(tmp_path: Path, repo):
    from app.chat.workdir import WorkdirManager

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("generic analyst rails")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    """A ChatManager wired to a real DATA_DIR-backed system DuckDB (so
    ``agents_repo()`` and ``ChatRepository`` share the same underlying
    database, mirroring tests/test_chat_agent_binding.py's `_make_app`
    pattern) plus a real WorkdirManager so `prepare_session_dir` actually
    materializes files on disk."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)

    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.persistence import ChatRepository
    from src.db import get_system_db

    conn = get_system_db()
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )
    return mgr, repo


def _fake_handle():
    from tests.chat_fakes import FakeHandle

    return FakeHandle()


def test_spawn_live_uses_dynamic_profile_when_agent_has_prompt(spawn_env):
    import asyncio

    from src.repositories import agents_repo

    mgr, repo = spawn_env
    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="u1",
        name="Persona Agent",
        slug="persona-agent",
        system_prompt="# Persona\nYou are a specialized persona.",
    )

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u1@x.com", surface=_surface_web(), agent_id=agent_id)
        live = await mgr._spawn_live(s)
        try:
            claude_md = (live.session_dir / "CLAUDE.md").read_text(encoding="utf-8")
            assert claude_md.startswith("# Persona\nYou are a specialized persona.")
            # The persona replaced the workspace rails — the data-access
            # floor must have ridden along into the sandbox with it.
            assert "agnes catalog" in claude_md
            skill_path = live.session_dir / ".claude" / "skills" / "agnes-agent-context" / "SKILL.md"
            assert skill_path.exists()
            assert "Persona Agent" in skill_path.read_text(encoding="utf-8")

            snaps = agents_repo().list_scope_snapshots(s.id)
            assert len(snaps) == 1
            assert snaps[0]["agent_id"] == agent_id
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def test_spawn_live_default_agent_empty_prompt_unchanged(spawn_env):
    """The unchanged-web-chat invariant: an agent_id whose row has an empty
    system_prompt (the seeded default agent's shape) must not touch the
    persona — the workspace's generic CLAUDE.md (symlinked, not
    materialized) stays in effect exactly as it does with no agent_id at
    all."""
    import asyncio

    from src.repositories import agents_repo

    mgr, repo = spawn_env
    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="u2",
        name="Default",
        slug="default",
        system_prompt="",
    )

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u2@x.com", surface=_surface_web(), agent_id=agent_id)
        live = await mgr._spawn_live(s)
        try:
            claude_md_path = live.session_dir / "CLAUDE.md"
            # Not a profile-owned real file — the generic workspace CLAUDE.md
            # is symlinked in exactly as it is for a no-agent_id session.
            assert claude_md_path.is_symlink()
            assert claude_md_path.read_text(encoding="utf-8") == "generic analyst rails"
            assert not (live.session_dir / ".claude" / "skills" / "agnes-agent-context").exists()
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def test_spawn_live_no_agent_id_unchanged_and_no_snapshot(spawn_env):
    """No agent_id at all (pre-Task-6 shape, or a co-session fork) must
    behave identically and must not attempt a scope snapshot."""
    import asyncio

    from src.repositories import agents_repo

    mgr, repo = spawn_env

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u3@x.com", surface=_surface_web())
        assert s.agent_id is None
        live = await mgr._spawn_live(s)
        try:
            claude_md_path = live.session_dir / "CLAUDE.md"
            assert claude_md_path.is_symlink()
            assert agents_repo().list_scope_snapshots(s.id) == []
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def test_spawn_live_survives_malformed_agent_row_at_build_profile_seam(spawn_env):
    """A malformed agent row (e.g. a non-string ``system_prompt`` — bad data
    that slipped past whatever wrote the row) must not blow up the spawn.
    ``ChatManager._spawn_live`` wraps the ``agent_profile.build_profile``
    call in try/except and falls back to the static/default profile,
    matching ``_load_agent_row``'s and ``record_snapshot``'s defensive
    posture."""
    import asyncio

    mgr, repo = spawn_env
    agent_id = str(uuid.uuid4())
    # Bypass the real DB (a TEXT column would coerce an int to a string on
    # insert) so build_profile actually sees a non-string system_prompt and
    # raises — that's the failure mode this guard exists for.
    malformed_row = {
        "id": agent_id,
        "slug": "malformed",
        "name": "Malformed",
        "system_prompt": 123,
        "plugins_mode": "all",
        "connections_mode": "all",
        "tables_mode": "all",
        "memory_mode": "all",
    }
    with pytest.raises(AttributeError):
        agent_profile.build_profile(malformed_row)
    mgr._load_agent_row = MagicMock(return_value=malformed_row)

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u4@x.com", surface=_surface_web(), agent_id=agent_id)
        live = await mgr._spawn_live(s)  # must not raise
        try:
            claude_md_path = live.session_dir / "CLAUDE.md"
            # No dynamic persona materialized — fell back to the static/
            # default profile (the generic symlinked workspace CLAUDE.md).
            assert claude_md_path.is_symlink()
            assert claude_md_path.read_text(encoding="utf-8") == "generic analyst rails"
            assert not (live.session_dir / ".claude" / "skills" / "agnes-agent-context").exists()
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def _surface_web():
    from app.chat.types import Surface

    return Surface.WEB
