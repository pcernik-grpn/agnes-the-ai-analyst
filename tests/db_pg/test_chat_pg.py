"""Integration tests for the cloud-chat PG repositories.

PG-side smoke covering ChatSessionPgRepository, ChatMessagePgRepository, and
UserWorkdirPgRepository — the CRUD surface plus the two Postgres-only
constraints the DuckDB schema cannot express:

  - chat_messages.session_id FK ON DELETE CASCADE (hard_delete removes
    child rows automatically).
  - per-surface partial unique indexes (slack_dm channel uniqueness,
    slack_thread (channel, ts) uniqueness).

Mirrors the alembic-head fixture idiom from ``test_data_packages_pg.py``.

Also contains the dual-backend contract tests for sandbox-ref repo methods
(Task 3 of the pause/resume plan). Those tests use ``state_backend`` from
conftest.py so the same assertions run against both DuckDB and Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.chat.types import Surface

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return pg_engine


@pytest.fixture
def sessions(engine):
    from src.repositories.chat_sessions_pg import ChatSessionPgRepository

    return ChatSessionPgRepository(engine)


@pytest.fixture
def messages(engine):
    from src.repositories.chat_messages_pg import ChatMessagePgRepository

    return ChatMessagePgRepository(engine)


@pytest.fixture
def workdirs(engine):
    from src.repositories.user_workdirs_pg import UserWorkdirPgRepository

    return UserWorkdirPgRepository(engine)


@pytest.fixture
def participants(engine):
    from src.repositories.chat_session_participants_pg import (
        ChatSessionParticipantPgRepository,
    )

    return ChatSessionParticipantPgRepository(engine)


# --- sessions --------------------------------------------------------------


def test_create_and_get_session(sessions):
    s = sessions.create_session(user_email="a@x.com", surface=Surface.WEB)
    assert s.id.startswith("chat_")
    assert s.message_count == 0
    assert s.archived is False
    fetched = sessions.get_session(s.id)
    assert fetched is not None
    assert fetched.user_email == "a@x.com"
    assert fetched.surface == Surface.WEB


def test_create_session_accepts_a_caller_supplied_id(sessions):
    """An external turn engine can constrain the id's shape — the embedded
    ``kai-agent`` engine stores it in a Postgres ``uuid`` column, so the
    default ``chat_<hex>`` is rejected and the caller must own the key."""
    s = sessions.create_session(
        user_email="a@x.com",
        surface=Surface.WEB,
        session_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    )
    assert s.id == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    fetched = sessions.get_session("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
    assert fetched is not None
    assert fetched.user_email == "a@x.com"


def test_list_sessions_excludes_archived_by_default(sessions):
    a = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.archive_session(a.id)
    visible = sessions.list_sessions("u@x.com")
    assert a.id not in {s.id for s in visible}
    assert len(visible) == 1
    assert len(sessions.list_sessions("u@x.com", include_archived=True)) == 2


def test_set_title(sessions):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.set_title(s.id, "Renamed")
    assert sessions.get_session(s.id).title == "Renamed"


def test_slack_dm_partial_unique_index(sessions, engine):
    sessions.create_session(user_email="u@x.com", surface=Surface.SLACK_DM, slack_channel_id="C1")
    found = sessions.get_slack_dm_session("C1")
    assert found is not None
    # Second slack_dm for the same channel violates the partial unique index.
    with pytest.raises(Exception):
        sessions.create_session(user_email="other@x.com", surface=Surface.SLACK_DM, slack_channel_id="C1")


def test_slack_thread_partial_unique_index(sessions):
    sessions.create_session(
        user_email="u@x.com",
        surface=Surface.SLACK_THREAD,
        slack_channel_id="C1",
        slack_thread_ts="100.1",
    )
    found = sessions.get_slack_thread_session("C1", "100.1")
    assert found is not None
    # Different ts in same channel is allowed.
    sessions.create_session(
        user_email="u@x.com",
        surface=Surface.SLACK_THREAD,
        slack_channel_id="C1",
        slack_thread_ts="200.2",
    )
    with pytest.raises(Exception):
        sessions.create_session(
            user_email="u@x.com",
            surface=Surface.SLACK_THREAD,
            slack_channel_id="C1",
            slack_thread_ts="100.1",
        )


# --- messages --------------------------------------------------------------


def test_append_message_updates_session_rollup(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="hi")
    messages.append_message(
        session_id=s.id,
        role="assistant",
        content="hello",
        tokens_in=10,
        tokens_out=20,
    )
    refreshed = sessions.get_session(s.id)
    # PG keeps the rollup current (no DuckDB FK+index bug).
    assert refreshed.message_count == 2
    assert refreshed.last_message_at is not None


def test_list_messages_and_after_id(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    m1 = messages.append_message(session_id=s.id, role="user", content="one")
    messages.append_message(session_id=s.id, role="assistant", content="two")
    all_msgs = messages.list_messages(s.id)
    assert [m.content for m in all_msgs] == ["one", "two"]
    after = messages.list_messages(s.id, after_id=m1.id)
    assert [m.content for m in after] == ["two"]


def test_list_recent_messages_returns_newest_first(sessions, messages):
    """The counterpart to ``list_messages``'s oldest-first ``LIMIT`` —
    ``_redeliver_pending_question``/``_build_restore_context`` need the
    actual tail of a long conversation, not whatever ``list_messages``'s
    default 500-row ``ORDER BY created_at ASC LIMIT`` happens to return
    (Devin review on #1030)."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="one")
    messages.append_message(session_id=s.id, role="assistant", content="two")
    messages.append_message(session_id=s.id, role="user", content="three")
    recent = messages.list_recent_messages(s.id)
    assert [m.content for m in recent] == ["three", "two", "one"]


def test_list_recent_messages_respects_limit_past_default(sessions, messages):
    """The actual bug this repro guards: with a conversation longer than
    ``list_messages``'s default 500-row window, the true latest message
    must still be reachable via ``list_recent_messages(limit=1)`` — this
    uses a small limit to keep the test fast rather than seeding 500+ rows,
    but pins the same ``ORDER BY ... DESC`` contract that makes it work at
    any conversation length."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    for i in range(5):
        messages.append_message(session_id=s.id, role="user", content=f"msg-{i}")
    newest = messages.list_recent_messages(s.id, limit=1)
    assert len(newest) == 1
    assert newest[0].content == "msg-4"


def test_tool_calls_round_trip(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    payload = [{"name": "query", "args": {"sql": "SELECT 1"}}]
    messages.append_message(session_id=s.id, role="assistant", content="x", tool_calls=payload)
    got = messages.list_messages(s.id)[0]
    assert got.tool_calls == payload


def test_parts_round_trip(sessions, messages):
    """The turn's ordered shape (schema v123) must survive the store on BOTH
    backends — it is what keeps prose and tool cards interleaved after a
    reload, so a backend that silently dropped it would resurrect #1504 on
    that engine only."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    parts = [
        {"type": "text", "text": "Checking."},
        {
            "type": "tool",
            "tool_use_id": "c1",
            "tool": "Bash",
            "args": {"command": "agnes catalog"},
            "state": "output-available",
            "result": {"columns": ["a"], "rows": [[1]]},
            "is_error": False,
        },
        {"type": "text", "text": "Two tables."},
    ]
    messages.append_message(session_id=s.id, role="assistant", content="Checking.\n\nTwo tables.", parts=parts)
    got = messages.list_messages(s.id)[0]
    assert got.parts == parts, "order, nesting and the tool's state must all round-trip"
    assert [p["type"] for p in got.parts] == ["text", "tool", "text"]
    assert got.parts[1]["state"] == "output-available"


def test_parts_default_to_null_not_an_empty_list(sessions, messages):
    """A message written without parts (a user turn, or a pre-v123 writer)
    leaves the column NULL, which is what the client reads as "fall back to
    tool_calls" rather than "this turn had no content"."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="hi")
    got = messages.list_messages(s.id)[0]
    assert got.parts is None


def test_get_first_user_message(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="assistant", content="greeting")
    messages.append_message(session_id=s.id, role="user", content="first ask")
    messages.append_message(session_id=s.id, role="user", content="follow up")
    assert messages.get_first_user_message(s.id) == "first ask"


def test_session_total_and_daily_tokens(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="a", tokens_in=5, tokens_out=7)
    messages.append_message(session_id=s.id, role="assistant", content="b", tokens_in=3, tokens_out=4)
    assert messages.session_total_tokens(s.id) == 19
    tin, tout = messages.daily_anthropic_tokens("u@x.com")
    assert (tin, tout) == (8, 11)


# --- archive / delete ------------------------------------------------------


def test_archive_empty_user_sessions(sessions, messages):
    empty = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    full = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=full.id, role="user", content="hi")
    n = sessions.archive_empty_user_sessions("u@x.com")
    assert n == 1
    assert sessions.get_session(empty.id).archived is True
    assert sessions.get_session(full.id).archived is False


def test_archive_empty_respects_exclude_and_surface(sessions):
    keep = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    n = sessions.archive_empty_user_sessions("u@x.com", surface=Surface.WEB, exclude_id=keep.id)
    assert n == 1
    assert sessions.get_session(keep.id).archived is False


def test_hard_delete_cascades_messages(sessions, messages, engine):
    s = sessions.create_session(user_email="gone@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="bye")
    deleted = sessions.hard_delete_user_sessions("gone@x.com")
    assert deleted == 1
    assert sessions.get_session(s.id) is None
    with engine.connect() as conn:
        remaining = conn.execute(
            sa.text("SELECT COUNT(*) FROM chat_messages WHERE session_id = :sid"),
            {"sid": s.id},
        ).scalar()
    assert remaining == 0  # ON DELETE CASCADE removed children


# --- workdirs --------------------------------------------------------------


def test_workdir_upsert_get_delete(workdirs):
    assert workdirs.get_workdir("u@x.com") is None
    workdirs.upsert_workdir(
        user_email="u@x.com",
        marketplace_sha="abc",
        initial_workspace_sha="def",
        agnes_version="1.0.0",
    )
    w = workdirs.get_workdir("u@x.com")
    assert w is not None
    assert w.marketplace_sha == "abc"
    assert w.agnes_version_at_init == "1.0.0"
    # upsert again updates in place
    workdirs.upsert_workdir(
        user_email="u@x.com",
        marketplace_sha="zzz",
        initial_workspace_sha=None,
        agnes_version="2.0.0",
    )
    w2 = workdirs.get_workdir("u@x.com")
    assert w2.marketplace_sha == "zzz"
    assert w2.initial_workspace_sha is None
    workdirs.delete_workdir_row("u@x.com")
    assert workdirs.get_workdir("u@x.com") is None


# --- v69 co-presence -------------------------------------------------------


def test_session_flags_default_false(sessions):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    assert s.is_co_session is False
    assert s.ephemeral is False
    assert sessions.get_session(s.id).is_co_session is False


def test_agent_id_round_trip(sessions):
    """Task 6: agent_id persists on create and survives a re-fetch; a
    session created without one hydrates to None."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB, agent_id="a1")
    assert s.agent_id == "a1"
    fetched = sessions.get_session(s.id)
    assert fetched.agent_id == "a1"

    s2 = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    assert s2.agent_id is None
    assert sessions.get_session(s2.id).agent_id is None


def test_surface_api_round_trip(sessions):
    """Task 6: Surface.API sessions persist and hydrate correctly."""
    s = sessions.create_session(user_email="u@x.com", surface=Surface.API, agent_id="a2")
    assert s.surface == Surface.API
    fetched = sessions.get_session(s.id)
    assert fetched.surface == Surface.API
    assert fetched.agent_id == "a2"


def test_sender_email_round_trip(sessions, messages):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="hi", sender_email="b@x.com")
    got = messages.list_messages(s.id)[0]
    assert got.sender_email == "b@x.com"


def test_participant_add_list_role_remove(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="o@x.com", user_id="u-o", role="owner")
    participants.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    active = participants.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com", "c@x.com"}
    participants.update_participant_role(s.id, "c@x.com", "owner")
    assert all(p.role == "owner" for p in participants.get_session_participants(s.id) if p.user_email == "c@x.com")
    participants.remove_participant(s.id, "c@x.com")
    assert {p.user_email for p in participants.get_session_participants(s.id)} == {"o@x.com"}


def test_list_sessions_for_participant(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    found = participants.list_sessions_for_participant("c@x.com")
    assert s.id in {x.id for x in found}


def test_participant_session_hydration_matches_main_repo(sessions, participants):
    """Regression: the participants-repo ``_row_to_session`` must hydrate the
    sandbox lifecycle columns (sandbox_id / runner_pid / sandbox_paused_at)
    exactly like ``chat_sessions_pg._row_to_session``.

    A prior gap silently defaulted the three fields to None, so a co-session
    with a live sandbox looked sandbox-less when reached via the participant
    path — latent breakage for pause/resume takeover of co-driven sessions.
    """
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    # Give the session a live sandbox ref plus a paused marker.
    sessions.set_sandbox_ref(s.id, sandbox_id="sbx_co", runner_pid=4242)
    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    sessions.set_sandbox_paused_at(s.id, ts)

    via_main = sessions.get_session(s.id)
    via_participant = next(x for x in participants.list_sessions_for_participant("c@x.com") if x.id == s.id)

    # Field-for-field equality (ChatSession is a dataclass).
    assert via_participant == via_main
    # ...and the sandbox fields are actually populated, not trivially both-None.
    assert via_participant.sandbox_id == "sbx_co"
    assert via_participant.runner_pid == 4242
    assert via_participant.sandbox_paused_at is not None


def test_fork_session_as_co_session_pg(sessions, participants, messages):
    s0 = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
        seed_summary="prior context",
    )
    assert sessions.get_session(s0.id).is_co_session is False  # source untouched
    assert s1.is_co_session is True and s1.ephemeral is True
    parts = participants.get_session_participants(s1.id)
    assert {(p.user_email, p.role) for p in parts} == {("o@x.com", "owner"), ("c@x.com", "collaborator")}
    seeded = messages.list_messages(s1.id)
    assert seeded and seeded[0].content == "prior context"  # summary, not raw clone
    # rollup maintained: the seeded system message bumped message_count.
    assert sessions.get_session(s1.id).message_count == 1


def test_hard_delete_cascades_participants(sessions, participants, engine):
    s = sessions.create_session(user_email="gone@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="gone@x.com", user_id="u-g", role="owner")
    sessions.hard_delete_user_sessions("gone@x.com")
    with engine.connect() as conn:
        remaining = conn.execute(
            sa.text("SELECT COUNT(*) FROM chat_session_participants WHERE session_id = :sid"),
            {"sid": s.id},
        ).scalar()
    assert remaining == 0  # ON DELETE CASCADE


def test_co_session_coexists_with_owner_other_surfaces(sessions, participants):
    """A co-session for an owner does not collide with that owner's existing
    web / slack_dm / slack_thread sessions."""
    web = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    dm = sessions.create_session(user_email="o@x.com", surface=Surface.SLACK_DM, slack_channel_id="D1")
    co = participants.fork_session_as_co_session(
        web.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
    )
    ids = {s.id for s in sessions.list_sessions("o@x.com")}
    assert {web.id, dm.id, co.id} <= ids


# --- Task 9: fork_session_as_co_session contract + fork_co_session_to_private ---


def test_fork_session_as_co_session_no_messages_copied(sessions, participants, messages):
    """SR-8: fork_session_as_co_session must NOT copy transcript messages."""
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    messages.append_message(session_id=s0.id, role="user", content="secret data")
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    assert s1.is_co_session is True and s1.ephemeral is True
    # SR-8: no transcript blind-clone
    assert messages.list_messages(s1.id) == []
    again = sessions.get_session(s0.id)
    assert again.is_co_session is False and again.ephemeral is False
    rows = participants.get_session_participants(s1.id)
    by_role = {r.role: r for r in rows}
    assert by_role["owner"].user_id == "ua"
    assert by_role["collaborator"].user_id == "ub"


def test_fork_co_session_to_private_copies_transcript(sessions, participants, messages):
    """fork_co_session_to_private: fresh private session with co-session transcript."""
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    messages.append_message(session_id=s1.id, role="assistant", content="hi from co")
    priv_id = participants.fork_co_session_to_private(
        source_session_id=s1.id,
        owner_email="b@example.com",
    )
    priv = sessions.get_session(priv_id)
    assert priv.is_co_session is False and priv.ephemeral is False
    assert priv.user_email == "b@example.com"
    assert any(m.content == "hi from co" for m in messages.list_messages(priv_id))


# ---------------------------------------------------------------------------
# Dual-backend contract: sandbox-ref repo methods (Task 3, pause/resume plan)
#
# Uses ``state_backend`` from conftest so each test runs twice: once against
# DuckDB (ChatRepository directly) and once against Postgres (ChatRepository
# delegates to ChatSessionPgRepository when use_pg() is True). The PG leg
# will skip if no PG server is available.
# ---------------------------------------------------------------------------


@pytest.fixture
def _chat_env(state_backend, tmp_path, monkeypatch):
    """Boot the right backend and return a ready ChatRepository."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        conn = get_system_db()
        from app.chat.persistence import ChatRepository

        return ChatRepository(conn)
    else:
        # PG path: ChatRepository's __init__ detects use_pg() and delegates.
        from src.db import _ensure_schema
        from src.duckdb_conn import _open_duckdb

        conn = _open_duckdb(":memory:")
        _ensure_schema(conn)
        from app.chat.persistence import ChatRepository

        return ChatRepository(conn)


def test_sandbox_ref_roundtrip(_chat_env):
    """set_sandbox_ref / get_session / set_sandbox_paused_at / list_paused_sessions
    / clear_sandbox_ref all round-trip correctly on both backends.

    Covers the DuckDB 1.5.3 FK+index guard: the same operations are repeated
    AFTER append_message() has inserted chat_messages rows, which is the
    production order (pause always happens after messages exist) and is exactly
    what the bug would break if the new columns were indexed.
    """
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).sandbox_id is None

    # --- basic set/get roundtrip ---
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_1", runner_pid=413)
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid, got.sandbox_paused_at) == ("sbx_1", 413, None)

    # --- set_sandbox_paused_at marks the session as paused ---
    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    repo.set_sandbox_paused_at(s.id, ts)
    got2 = repo.get_session(s.id)
    assert got2.sandbox_paused_at is not None
    paused = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id in {x.id for x in paused}

    # --- set_sandbox_paused_at(None) clears the marker (resume path) ---
    repo.set_sandbox_paused_at(s.id, None)
    assert repo.get_session(s.id).sandbox_paused_at is None
    paused_after_clear = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id not in {x.id for x in paused_after_clear}

    # --- clear_sandbox_ref wipes all three columns ---
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_2", runner_pid=999)
    repo.clear_sandbox_ref(s.id)
    got3 = repo.get_session(s.id)
    assert (got3.sandbox_id, got3.runner_pid, got3.sandbox_paused_at) == (None, None, None)


def test_sandbox_ref_roundtrip_after_messages(_chat_env):
    """Repeat the full sandbox-ref roundtrip AFTER append_message() has inserted
    chat_messages rows.

    This is the production order — pause always happens after at least one
    message exists. It specifically guards against the DuckDB 1.5.3 FK+index
    bug: if any of the three new columns were indexed, UPDATE on chat_sessions
    after a child chat_messages INSERT would raise a false FK violation.
    """
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)

    # Insert messages first — this is the state that would trigger the FK bug.
    repo.append_message(session_id=s.id, role="user", content="hello")
    repo.append_message(session_id=s.id, role="assistant", content="hi")

    # Now run the full sandbox-ref lifecycle; none of these must raise.
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_post_msg", runner_pid=77)
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid) == ("sbx_post_msg", 77)

    ts = datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc)
    repo.set_sandbox_paused_at(s.id, ts)
    assert repo.get_session(s.id).sandbox_paused_at is not None
    paused = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id in {x.id for x in paused}

    repo.set_sandbox_paused_at(s.id, None)
    repo.clear_sandbox_ref(s.id)
    got2 = repo.get_session(s.id)
    assert (got2.sandbox_id, got2.runner_pid, got2.sandbox_paused_at) == (None, None, None)


def test_relay_protocol_version_roundtrip(_chat_env):
    """Tier 1 restart-invariant sandbox reuse: ``set_sandbox_ref`` stamps
    ``relay_protocol_version`` with ``RELAY_PROTOCOL_VERSION`` on both
    backends, and ``clear_sandbox_ref`` clears it back to NULL alongside
    the other three sandbox columns. A brand-new session starts with NULL
    (unknown/legacy)."""
    from app.chat.types import RELAY_PROTOCOL_VERSION

    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).relay_protocol_version is None

    repo.set_sandbox_ref(s.id, sandbox_id="sbx_relay", runner_pid=42)
    got = repo.get_session(s.id)
    assert got.relay_protocol_version == RELAY_PROTOCOL_VERSION

    repo.clear_sandbox_ref(s.id)
    assert repo.get_session(s.id).relay_protocol_version is None


# ---------------------------------------------------------------------------
# Dual-backend contract: pinned conversations (chat_sessions.pinned_at)
# ---------------------------------------------------------------------------


def test_set_pinned_roundtrip(_chat_env):
    """``set_pinned`` stamps / clears ``pinned_at`` identically on both backends,
    and re-pinning re-stamps it (which is what moves a session to the front of
    the history panel's Pinned group)."""
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).pinned_at is None

    repo.set_pinned(s.id, True)
    first = repo.get_session(s.id).pinned_at
    assert first is not None

    # Re-pinning an already-pinned session is idempotent in state and advances
    # the timestamp — never clears it.
    repo.set_pinned(s.id, True)
    again = repo.get_session(s.id).pinned_at
    assert again is not None and again >= first

    repo.set_pinned(s.id, False)
    assert repo.get_session(s.id).pinned_at is None


def test_set_pinned_after_messages(_chat_env):
    """The production order: a conversation is pinned long after it has
    messages. This is the DuckDB 1.5.3 FK+index guard — if ``pinned_at`` were
    ever indexed, this UPDATE would raise a false FK violation (same failure
    mode the sandbox-ref tests above guard against, but load-bearing here since
    every real pin click lands in this state)."""
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="hello")
    repo.append_message(session_id=s.id, role="assistant", content="hi")

    repo.set_pinned(s.id, True)
    assert repo.get_session(s.id).pinned_at is not None
    repo.set_pinned(s.id, False)
    assert repo.get_session(s.id).pinned_at is None


def test_list_sessions_orders_pinned_first(_chat_env):
    """Pinned sessions lead ``list_sessions`` on both backends, most-recently-
    pinned first, with the unpinned remainder keeping plain recency order.

    Spelled out as a contract test because the two backends need DIFFERENT SQL
    to agree: Postgres defaults ``ORDER BY … DESC`` to NULLS FIRST, so without
    an explicit ``NULLS LAST`` the PG leg would sort every *unpinned* session
    above the pins — the exact inversion of the intended behavior.
    """
    repo = _chat_env
    older = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    newer = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    newest = repo.create_session(user_email="u@example.com", surface=Surface.WEB)

    # Give them real recency, oldest-first, so the unpinned order is defined.
    for s in (older, newer, newest):
        repo.append_message(session_id=s.id, role="user", content="hi")

    unpinned_ids = [s.id for s in repo.list_sessions("u@example.com")]
    assert set(unpinned_ids) == {older.id, newer.id, newest.id}

    repo.set_pinned(older.id, True)
    ids = [s.id for s in repo.list_sessions("u@example.com")]
    assert ids[0] == older.id, "a pinned session leads regardless of its recency"
    assert set(ids[1:]) == {newer.id, newest.id}

    repo.set_pinned(newest.id, True)
    ids2 = [s.id for s in repo.list_sessions("u@example.com")]
    assert ids2[:2] == [newest.id, older.id], "most-recently-pinned leads the pinned block"
    assert ids2[2] == newer.id


# ---------------------------------------------------------------------------
# Dual-backend contract: archive → restore → permanent delete
# ---------------------------------------------------------------------------
# The three states the /chats page exposes. They need a contract test because
# the two backends reach the same endpoint by different means: DuckDB has no
# ON DELETE CASCADE, so `hard_delete_session` deletes the participant and
# message rows itself, while Postgres relies on the FKs from migrations 0015 and
# 0017. A caller must not be able to tell.


def test_archive_and_restore_roundtrip(_chat_env):
    """``archive_session`` / ``restore_session`` move one flag, both ways, and
    both are idempotent. Restore is what makes the page's Archived view an
    actual state rather than a one-way door — before it there was no way back
    from the long-standing soft delete."""
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).archived is False

    repo.archive_session(s.id)
    assert repo.get_session(s.id).archived is True
    # Archived rows leave the default listing but must still be readable, or the
    # Archived view has nothing to show.
    assert s.id not in {x.id for x in repo.list_sessions("u@example.com")}
    assert s.id in {x.id for x in repo.list_sessions("u@example.com", include_archived=True)}

    repo.restore_session(s.id)
    assert repo.get_session(s.id).archived is False
    assert s.id in {x.id for x in repo.list_sessions("u@example.com")}

    # Idempotent in both directions.
    repo.restore_session(s.id)
    assert repo.get_session(s.id).archived is False
    repo.archive_session(s.id)
    repo.archive_session(s.id)
    assert repo.get_session(s.id).archived is True


def test_archive_and_restore_after_messages(_chat_env):
    """The production order — a conversation is archived long after it has
    messages. Same DuckDB 1.5.3 FK+index guard as the pin tests above: were
    ``archived`` ever indexed, this UPDATE would raise a false FK violation."""
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="hello")
    repo.append_message(session_id=s.id, role="assistant", content="hi")

    repo.archive_session(s.id)
    assert repo.get_session(s.id).archived is True
    repo.restore_session(s.id)
    assert repo.get_session(s.id).archived is False
    # The rollup survives the round trip — archiving is not a data change.
    assert repo.get_session(s.id).message_count == 2


def test_hard_delete_session_takes_its_children_and_nothing_else(_chat_env):
    """One session and its messages go; its neighbour is untouched. The return
    value says whether there was a row to delete, so a caller can tell a
    successful delete from a missing id (the API turns the second case into a
    404)."""
    repo = _chat_env
    doomed = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    keeper = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    for sid in (doomed.id, keeper.id):
        repo.append_message(session_id=sid, role="user", content="hi")
        repo.append_message(session_id=sid, role="assistant", content="hello")

    assert repo.hard_delete_session(doomed.id) is True
    assert repo.get_session(doomed.id) is None
    assert repo.list_messages(doomed.id) == []

    assert repo.get_session(keeper.id) is not None
    assert len(repo.list_messages(keeper.id)) == 2

    # Deleting what is already gone is not an error, and says so.
    assert repo.hard_delete_session(doomed.id) is False


def test_hard_delete_session_removes_participants(_chat_env):
    """A co-session's participant rows go with it. On DuckDB they are deleted
    explicitly (the FK would otherwise block the parent delete); on Postgres the
    0017 CASCADE does it. Same observable outcome."""
    repo = _chat_env
    s = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)
    repo.add_session_participant(session_id=s.id, user_email="owner@example.com", user_id="u1", role="owner")
    repo.add_session_participant(session_id=s.id, user_email="mate@example.com", user_id="u2", role="collaborator")
    assert len(repo.get_session_participants(s.id)) == 2

    assert repo.hard_delete_session(s.id) is True
    assert repo.get_session(s.id) is None
    assert repo.get_session_participants(s.id) == []
    assert repo.list_sessions_for_participant("mate@example.com") == []


# ---------------------------------------------------------------------------
# Wire-level parity for the agent_id projection
# ---------------------------------------------------------------------------


def test_session_responses_carry_agent_id_on_postgres(engine, monkeypatch):
    """``agent_id`` reaches the JSON body when the active backend is Postgres.

    The repo-level round-trip above proves the COLUMN survives on both engines,
    which is a different claim from "the two responses that now project it are
    correct on both". ``POST``/``GET /api/chat/sessions`` sit in the
    route-coverage exclusion list for the parameter-free status sweep (they need
    a live ChatManager), so without this the projection was exercised on DuckDB
    only — the dual-backend rule in CONTRIBUTING.md wants the API-level
    behaviour proven on the engine it will actually run on.

    Built like ``tests/test_chat_api.py::_make_app``: real router, real
    repository, no-op sandbox provider, and the access gate delegated to
    ``get_current_user`` so this asserts the projection rather than re-testing
    RBAC (covered in tests/test_chat_session_as_agent.py).
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.api.chat import require_chat_access
    from app.api.chat import router as chat_router
    from app.auth.dependencies import get_current_user
    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.persistence import ChatRepository
    from app.chat.workdir import WorkdirManager
    from src.duckdb_conn import _open_duckdb

    user = {"id": "pguser1", "email": "pg-agent@test.com", "is_admin": False}

    # A PG-backed ChatRepository: the constructor only wires its `_sessions_pg`
    # delegate when `use_pg()` is true, which is exactly the branch under test.
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    # Through the sanctioned opener, not bare `duckdb.connect`: the UTC-pinning
    # guard in tests/test_duckdb_session_tz.py is a ratchet, and an unused
    # throwaway handle is not a reason to widen it. The connection is inert
    # here anyway — the PG delegate below is what serves every call.
    repo = ChatRepository(_open_duckdb(":memory:"))
    assert repo._sessions_pg is not None, (
        "the repository fell back to DuckDB, so this test would prove nothing about Postgres"
    )

    provider = MagicMock()
    provider.spawn = AsyncMock()
    workdirs = MagicMock(spec=WorkdirManager)
    workdirs.ensure_user_workdir = MagicMock()
    workdirs.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    app = FastAPI()
    app.include_router(chat_router)
    app.state.chat_repo = repo
    app.state.chat_manager = ChatManager(
        provider=provider,
        workdir_mgr=workdirs,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=3),
    )

    async def _granted(u: dict = Depends(get_current_user)) -> dict:
        return u

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_chat_access] = _granted
    client = TestClient(app)

    created = client.post("/api/chat/sessions", json={"surface": "web"})
    assert created.status_code == 201, created.text
    agent_id = created.json()["agent_id"]
    assert agent_id, "POST projected a null agent_id — an unnamed web session is attributed to the default agent"

    listed = client.get("/api/chat/sessions")
    assert listed.status_code == 200, listed.text
    row = next(s for s in listed.json() if s["id"] == created.json()["id"])
    assert row["agent_id"] == agent_id


# --- Both forks are session CREATORS: they must honour a caller-owned id ---


def test_both_forks_honour_an_explicit_session_id_pg(sessions, participants):
    """`chat.provider: kai-agent` requires every session id to be a uuid (the
    engine's own chat column is one), so `ChatManager.create_session` and the
    Slack twin thread `engine_session_id(config)` into the repo.

    A fork is a creator too. Both fork methods used to mint `chat_<hex>`
    unconditionally on BOTH backends, so on an engine instance co-drive was
    dead on arrival — the forked row could never spawn, and the error card
    told the user the conversation "predates" a provider it was seconds
    younger than. The id decision has to reach every creator, not the two that
    happened to be in view.
    """
    import uuid

    s0 = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)

    co_id = str(uuid.uuid4())
    co = participants.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
        session_id=co_id,
    )
    assert co.id == co_id
    assert uuid.UUID(co.id)

    priv_id = str(uuid.uuid4())
    got = participants.fork_co_session_to_private(
        source_session_id=co.id,
        owner_email="o@x.com",
        session_id=priv_id,
    )
    assert got == priv_id
    assert uuid.UUID(got)


def test_both_forks_still_mint_the_default_shape_without_one_pg(sessions, participants):
    """Non-vacuity: omitting the id keeps the repo's own `chat_<hex>`, so this
    is a threading change rather than a change of default."""
    s0 = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    co = participants.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
    )
    assert co.id.startswith("chat_")
    priv = participants.fork_co_session_to_private(source_session_id=co.id, owner_email="o@x.com")
    assert priv.startswith("chat_")


# ---------------------------------------------------------------------------
# Dual-backend contract: list_recently_active (F4, audit-full-coverage plan
# Task 8 — the session-pipeline chat-export sweep's discovery query)
# ---------------------------------------------------------------------------


def test_list_recently_active_orders_and_caps(_chat_env):
    """Sessions with a message come back most-recently-active first, and the
    ``limit`` kwarg caps the result on both backends. A session with zero
    messages never appears — nothing to export for it."""
    repo = _chat_env
    empty = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    older = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    repo.append_message(session_id=older.id, role="user", content="hi")
    newer = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    repo.append_message(session_id=newer.id, role="user", content="hi")

    active = repo.list_recently_active(limit=200)
    ids = [s.id for s in active]
    assert empty.id not in ids
    assert ids.index(newer.id) < ids.index(older.id)

    capped = repo.list_recently_active(limit=1)
    assert len(capped) == 1
    assert capped[0].id == newer.id


def test_list_recently_active_is_cross_user(_chat_env):
    """No owner filter — a caller sees sessions across every user, the same
    shape as ``list_paused_sessions``."""
    repo = _chat_env
    a = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    repo.append_message(session_id=a.id, role="user", content="hi")
    b = repo.create_session(user_email="b@example.com", surface=Surface.WEB)
    repo.append_message(session_id=b.id, role="user", content="hi")

    ids = {s.id for s in repo.list_recently_active(limit=200)}
    assert {a.id, b.id} <= ids
