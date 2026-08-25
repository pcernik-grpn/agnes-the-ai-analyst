"""Tests for ChatRepository — sessions, messages, and workdir markers.

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents (same pattern as
tests/test_chat_db_migration.py) are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.persistence import ChatRepository
from app.chat.types import Surface


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_create_and_get_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB, title="t")
    assert s.id.startswith("chat_") and len(s.id) == len("chat_") + 12
    fetched = repo.get_session(s.id)
    assert fetched is not None
    assert fetched.user_email == "u@x"
    assert fetched.surface == Surface.WEB
    assert fetched.title == "t"
    assert fetched.archived is False


def test_list_sessions_by_user_recent_first(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.WEB)
    b = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=b.id, role="user", content="hi")
    listing = repo.list_sessions("u@x")
    assert [s.id for s in listing] == [b.id, a.id]


def test_get_slack_dm_session_by_channel(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x",
        surface=Surface.SLACK_DM,
        slack_channel_id="C123",
    )
    again = repo.get_slack_dm_session("C123")
    assert again is not None and again.id == s.id
    assert repo.get_slack_dm_session("C-other") is None


def test_get_slack_thread_session(repo: ChatRepository):
    s = repo.create_session(
        user_email="u@x",
        surface=Surface.SLACK_THREAD,
        slack_channel_id="C1",
        slack_thread_ts="123.456",
    )
    again = repo.get_slack_thread_session("C1", "123.456")
    assert again is not None and again.id == s.id


def test_archive_session(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.archive_session(s.id)
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.archived is True


def test_archive_empty_user_sessions_archives_only_empties(repo: ChatRepository):
    """Soft-archive every empty session for a user, leaving sessions
    with messages and other users' sessions untouched."""
    empty_a = repo.create_session(user_email="u@x", surface=Surface.WEB)
    empty_b = repo.create_session(user_email="u@x", surface=Surface.WEB)
    with_msg = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=with_msg.id, role="user", content="hi")
    other_empty = repo.create_session(user_email="v@x", surface=Surface.WEB)

    n = repo.archive_empty_user_sessions("u@x")
    assert n == 2
    assert repo.get_session(empty_a.id).archived is True
    assert repo.get_session(empty_b.id).archived is True
    assert repo.get_session(with_msg.id).archived is False
    assert repo.get_session(other_empty.id).archived is False


def test_archive_empty_user_sessions_respects_exclude_id(repo: ChatRepository):
    """When called from POST /sessions after a brand-new session is
    created, the new id is passed as ``exclude_id`` so it isn't
    immediately re-archived."""
    just_created = repo.create_session(user_email="u@x", surface=Surface.WEB)
    earlier_empty = repo.create_session(user_email="u@x", surface=Surface.WEB)

    n = repo.archive_empty_user_sessions("u@x", exclude_id=just_created.id)
    assert n == 1
    assert repo.get_session(just_created.id).archived is False
    assert repo.get_session(earlier_empty.id).archived is True


def test_archive_empty_user_sessions_zero_when_nothing_to_do(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="hi")
    assert repo.archive_empty_user_sessions("u@x") == 0


def test_archived_slack_dm_does_not_block_new_one(repo: ChatRepository):
    a = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    repo.archive_session(a.id)
    b = repo.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1")
    assert b.id != a.id


def test_hard_delete_user_sessions(repo: ChatRepository):
    s1 = repo.create_session(user_email="u@x", surface=Surface.WEB)
    s2 = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s1.id, role="user", content="hi")
    repo.append_message(session_id=s2.id, role="user", content="hello")
    # Other user is untouched
    other = repo.create_session(user_email="v@x", surface=Surface.WEB)
    repo.append_message(session_id=other.id, role="user", content="ok")

    n = repo.hard_delete_user_sessions("u@x")
    assert n == 2
    assert repo.get_session(s1.id) is None
    assert repo.get_session(s2.id) is None
    # Messages are gone too — would FK-block if order were reversed
    assert repo.list_messages(s1.id) == []
    assert repo.list_messages(s2.id) == []
    # Other user's data survives
    assert repo.get_session(other.id) is not None
    assert len(repo.list_messages(other.id)) == 1


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_append_and_list_messages(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="hi")
    m2 = repo.append_message(
        session_id=s.id,
        role="assistant",
        content="hello",
        tool_calls=[{"tool": "list_catalog", "args": {}}],
        tokens_in=5,
        tokens_out=3,
        model="claude-haiku-4-5-20251001",
    )
    msgs = repo.list_messages(s.id)
    assert [m.id for m in msgs] == [m1.id, m2.id]
    assert msgs[1].tool_calls == [{"tool": "list_catalog", "args": {}}]
    refreshed = repo.get_session(s.id)
    assert refreshed is not None and refreshed.message_count == 2


def test_list_messages_after_cursor(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    m1 = repo.append_message(session_id=s.id, role="user", content="a")
    m2 = repo.append_message(session_id=s.id, role="user", content="b")
    out = repo.list_messages(s.id, after_id=m1.id)
    assert [m.id for m in out] == [m2.id]


def test_list_recent_messages_returns_newest_first(repo: ChatRepository):
    """The counterpart to ``list_messages``'s oldest-first ``LIMIT`` —
    ``_redeliver_pending_question``/``_build_restore_context`` need the
    actual tail of a long conversation, not whatever ``list_messages``'s
    default 500-row ``ORDER BY created_at ASC LIMIT`` happens to return
    (Devin review on #1030)."""
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="one")
    repo.append_message(session_id=s.id, role="assistant", content="two")
    repo.append_message(session_id=s.id, role="user", content="three")
    recent = repo.list_recent_messages(s.id)
    assert [m.content for m in recent] == ["three", "two", "one"]
    newest_only = repo.list_recent_messages(s.id, limit=1)
    assert [m.content for m in newest_only] == ["three"]


# ---------------------------------------------------------------------------
# Workdirs
# ---------------------------------------------------------------------------


def test_workdir_upsert_and_fetch(repo: ChatRepository):
    repo.upsert_workdir(
        user_email="u@x",
        marketplace_sha="abc",
        initial_workspace_sha="def",
        agnes_version="0.55.0",
    )
    w = repo.get_workdir("u@x")
    assert w is not None
    assert w.marketplace_sha == "abc"
    assert w.agnes_version_at_init == "0.55.0"


def test_workdir_repeated_upsert_same_key_updates_in_place(repo: ChatRepository):
    """Regression 2026-07-17: `INSERT OR REPLACE` deletes-then-inserts the
    conflicting row internally on DuckDB, hitting the same PRIMARY KEY index
    assertion as UsageRepository.upsert_summary (see test_usage_rollups.py).
    Switched to INSERT ... ON CONFLICT DO UPDATE — a second upsert for the
    same user_email must not raise and must leave exactly one, updated row."""
    repo.upsert_workdir(
        user_email="u@x",
        marketplace_sha="abc",
        initial_workspace_sha="def",
        agnes_version="0.55.0",
    )
    repo.upsert_workdir(
        user_email="u@x",
        marketplace_sha="xyz",
        initial_workspace_sha="def",
        agnes_version="0.56.0",
    )
    w = repo.get_workdir("u@x")
    assert w is not None
    assert w.marketplace_sha == "xyz"
    assert w.agnes_version_at_init == "0.56.0"
    n = repo._conn.execute("SELECT COUNT(*) FROM user_workdirs WHERE user_email = 'u@x'").fetchone()[0]
    assert n == 1


def test_daily_anthropic_tokens(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="assistant", content="x", tokens_in=100, tokens_out=50)
    repo.append_message(session_id=s.id, role="assistant", content="y", tokens_in=200, tokens_out=80)
    tin, tout = repo.daily_anthropic_tokens("u@x")
    assert tin == 300 and tout == 130


def test_v69_flags_default_false_and_sender_email_roundtrip(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    assert s.is_co_session is False and s.ephemeral is False
    repo.append_message(session_id=s.id, role="user", content="hi", sender_email="b@x.com")
    msgs = repo.list_messages(s.id)
    assert msgs[0].sender_email == "b@x.com"


def test_participant_crud_and_list_for_participant(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    repo.add_session_participant(session_id=s.id, user_email="o@x.com", user_id="u-o", role="owner")
    repo.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    active = repo.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com", "c@x.com"}
    repo.update_participant_role(s.id, "c@x.com", "owner")
    assert {p.role for p in repo.get_session_participants(s.id)} == {"owner"}
    repo.remove_participant(s.id, "c@x.com")
    active = repo.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com"}
    assert s.id in {x.id for x in repo.list_sessions_for_participant("o@x.com")}


def test_fork_session_as_co_session(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
        seed_summary="prior context",
    )
    # S0 untouched.
    assert repo.get_session(s0.id).is_co_session is False
    # S1 flags set, two participant rows, summary seeded as a system message.
    assert s1.is_co_session is True and s1.ephemeral is True
    parts = repo.get_session_participants(s1.id)
    assert {(p.user_email, p.role) for p in parts} == {("o@x.com", "owner"), ("c@x.com", "collaborator")}
    seeded = repo.list_messages(s1.id)
    assert seeded and seeded[0].content == "prior context"


def test_hard_delete_removes_participants_first(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="gone@x.com", surface=Surface.WEB)
    repo.add_session_participant(session_id=s.id, user_email="gone@x.com", user_id="u-g", role="owner")
    repo.append_message(session_id=s.id, role="user", content="bye", sender_email="gone@x.com")
    n = repo.hard_delete_user_sessions("gone@x.com")
    assert n == 1
    remaining = conn.execute("SELECT COUNT(*) FROM chat_session_participants WHERE session_id = ?", [s.id]).fetchone()[
        0
    ]
    assert remaining == 0


def test_both_forks_honour_an_explicit_session_id(tmp_path):
    """DuckDB twin of `tests/db_pg/test_chat_pg.py::
    test_both_forks_honour_an_explicit_session_id_pg`.

    A fork is a session CREATOR. `chat.provider: kai-agent` needs every id to
    be a uuid, and both forks used to mint `chat_<hex>` unconditionally — so
    on an engine instance a brand-new co-drive session could never spawn, and
    the error card claimed it "predates" a provider it was seconds younger
    than.
    """
    import uuid

    import duckdb

    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="o@x.com", surface=Surface.WEB)

    co_id = str(uuid.uuid4())
    co = repo.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
        session_id=co_id,
    )
    assert co.id == co_id and uuid.UUID(co.id)

    priv_id = str(uuid.uuid4())
    got = repo.fork_co_session_to_private(
        source_session_id=co.id,
        owner_email="o@x.com",
        session_id=priv_id,
    )
    assert got == priv_id and uuid.UUID(got)

    # Non-vacuity: without an explicit id the repo keeps its own shape.
    plain = repo.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c2@x.com",
        invitee_user_id="u-c2",
    )
    assert plain.id.startswith("chat_")
