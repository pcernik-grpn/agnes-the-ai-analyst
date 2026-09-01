"""Adoption dashboard endpoints — behaviour + dual-backend parity.

Every assertion runs twice via ``seeded_app_both`` (DuckDB and Postgres),
so this doubles as the cross-engine contract test for the new
``adoption_*`` repository methods: the HTTP layer reads them through
``usage_repo()`` (the backend factory), and identical assertions must
hold on both engines.

Seeds go through ``usage_repo()`` so the write lands on whichever backend
the parametrized fixture selected. Data is timestamped a few days back so
it falls inside the 7d/30d windows (and the 30-day trend series) but
outside the 24h window — which lets us assert the window toggle actually
re-scopes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _when() -> datetime:
    # 3 days ago: inside 7d/30d + the 30-entry trend series, outside 24h.
    return datetime.now(timezone.utc) - timedelta(days=3)


def _event(*, username, user_id=None, tool_name=None, skill_name=None, is_error=False, occurred_at=None) -> dict:
    eid = uuid.uuid4().hex
    return {
        "id": eid,
        "session_id": f"sess-{eid[:8]}",
        "session_file": f"{username}/{eid[:8]}.jsonl",
        "username": username,
        "user_id": user_id,
        "event_uuid": None,
        "parent_uuid": None,
        "event_type": "tool_use",
        "tool_name": tool_name,
        "skill_name": skill_name,
        "subagent_type": None,
        "command_name": None,
        "is_error": is_error,
        "source": "claude_code",
        "ref_id": None,
        "model": "claude-x",
        "cwd": None,
        "occurred_at": occurred_at or _when(),
    }


def _summary(*, session_file, username, user_id=None, **over) -> dict:
    when = over.get("started_at", _when())
    return {
        "session_file": session_file,
        "session_id": session_file.split("/", 1)[-1],
        "username": username,
        "user_id": user_id,
        "started_at": when,
        "ended_at": when,
        "active_seconds": over.get("active_seconds", 0),
        "wall_seconds": over.get("wall_seconds", 0),
        "user_messages": over.get("user_messages", 0),
        "assistant_messages": 0,
        "tool_calls": over.get("tool_calls", 0),
        "tool_errors": over.get("tool_errors", 0),
        "mcp_calls": over.get("mcp_calls", 0),
        "subagent_dispatches": over.get("subagent_dispatches", 0),
        "skill_invocations": over.get("skill_invocations", 0),
        "input_tokens": over.get("input_tokens", 0),
        "output_tokens": over.get("output_tokens", 0),
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "primary_model": over.get("primary_model", "claude-x"),
    }


def _seed_overall(repo) -> None:
    """Two users. alice: 1h active, 5 skills, 10 prompts, 1500 tokens;
    bob: 0.5h active, 2 skills, 4 prompts, 300 tokens."""
    repo.upsert_summary(
        _summary(
            session_file="alice@test.com/s1.jsonl",
            username="alice@test.com",
            active_seconds=3600,
            wall_seconds=7200,
            user_messages=10,
            skill_invocations=5,
            tool_calls=10,
            tool_errors=1,
            mcp_calls=5,
            subagent_dispatches=1,
            input_tokens=1000,
            output_tokens=500,
            primary_model="claude-opus",
        ),
        processor_version=1,
    )
    repo.upsert_summary(
        _summary(
            session_file="bob@test.com/s2.jsonl",
            username="bob@test.com",
            active_seconds=1800,
            wall_seconds=3600,
            user_messages=4,
            skill_invocations=2,
            tool_calls=4,
            tool_errors=0,
            input_tokens=200,
            output_tokens=100,
            primary_model="claude-haiku",
        ),
        processor_version=1,
    )
    repo.upsert_events(
        [
            _event(username="alice@test.com", tool_name="Bash", skill_name="grpn:foo"),
            _event(username="alice@test.com", tool_name="Read", skill_name="grpn:foo"),
            _event(username="bob@test.com", tool_name="Read", skill_name="bar"),
        ],
        processor_version=1,
    )


# ---------------------------------------------------------------------------
# /api/admin/adoption/kpis
# ---------------------------------------------------------------------------


def test_adoption_kpis(seeded_app_both):
    from src.repositories import usage_repo

    _seed_overall(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/kpis?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["window"] == "7d"
    assert b["active_users"] == 2
    assert b["active_seconds"] == 5400
    assert b["active_hours"] == 1.5  # 5400 / 3600
    assert b["wall_hours"] == 3.0  # 10800 / 3600
    assert b["sessions"] == 2
    assert b["prompts"] == 14
    assert b["skill_invocations"] == 7
    assert b["distinct_skills"] == 2  # grpn:foo + bar (from events)
    assert b["tokens"] == 1800
    # ALL calls: alice 10 native + 5 MCP + 1 subagent, bob 4 native.
    assert b["tool_calls"] == 20
    assert b["tool_errors"] == 1


def test_adoption_kpis_excludes_synthetic_sessions(seeded_app_both):
    """Adoption KPIs and the digest's session KPI must agree.

    The digest renders BOTH in one report. If only one filters the `<synthetic>`
    processing artifact, the report shows two different numbers under two tiles
    both labelled "Sessions". Regression guard for that divergence.
    """
    from src.repositories import usage_repo

    repo = usage_repo()
    _seed_overall(repo)
    # the artifact: synthetic modal model, no tool calls, no active time
    repo.upsert_summary(
        _summary(
            session_file="ghost@test.com/s9.jsonl",
            username="ghost@test.com",
            active_seconds=0,
            wall_seconds=0,
            user_messages=1,
            skill_invocations=0,
            tool_calls=0,
            tool_errors=0,
            input_tokens=0,
            output_tokens=0,
            primary_model="<synthetic>",
        ),
        processor_version=1,
    )
    # synthetic-DOMINATED but real: must still count (primary_model is only the
    # MOST FREQUENT model, not a session type)
    repo.upsert_summary(
        _summary(
            session_file="carol@test.com/s10.jsonl",
            username="carol@test.com",
            active_seconds=600,
            wall_seconds=900,
            user_messages=3,
            skill_invocations=1,
            tool_calls=9,
            tool_errors=0,
            input_tokens=50,
            output_tokens=25,
            primary_model="<synthetic>",
        ),
        processor_version=1,
    )

    b = (
        seeded_app_both["client"]
        .get(
            "/api/admin/adoption/kpis?window=7d",
            headers=_h(seeded_app_both["admin_token"]),
        )
        .json()
    )
    # alice + bob + carol. ghost is the artifact and is excluded.
    assert b["sessions"] == 3
    assert b["active_users"] == 3
    # ghost's single synthetic user_message must not inflate prompts either
    assert b["prompts"] == 17  # 10 + 4 + 3, not 18


def test_adoption_kpis_window_rescopes(seeded_app_both):
    """24h window excludes data seeded 3 days ago."""
    from src.repositories import usage_repo

    _seed_overall(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/kpis?window=24h",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["active_users"] == 0
    assert b["sessions"] == 0
    assert b["active_hours"] == 0.0


def test_adoption_kpis_empty_window_is_zeroed(seeded_app_both):
    """No data at all → all-zero payload, never a 404/500."""
    r = seeded_app_both["client"].get(
        "/api/admin/adoption/kpis?window=30d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["active_users"] == 0 and b["sessions"] == 0 and b["tokens"] == 0


def test_adoption_kpis_forbids_non_admin(seeded_app_both):
    r = seeded_app_both["client"].get(
        "/api/admin/adoption/kpis",
        headers=_h(seeded_app_both["analyst_token"]),
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# /api/admin/adoption/series
# ---------------------------------------------------------------------------


def test_adoption_series(seeded_app_both):
    from src.repositories import usage_repo

    _seed_overall(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/series",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["days"]) == 30
    # Every day carries the full metric set (zero-filled).
    for d in b["days"]:
        assert set(d) >= {
            "day",
            "active_users",
            "active_hours",
            "wall_hours",
            "sessions",
            "prompts",
            "tokens",
            "skill_invocations",
        }
    # The seeded day (3 days ago) shows both users active + 2 sessions.
    assert any(d["active_users"] == 2 for d in b["days"])
    assert sum(d["sessions"] for d in b["days"]) == 2


# ---------------------------------------------------------------------------
# /api/admin/adoption/top-users  +  /top-skills
# ---------------------------------------------------------------------------


def test_adoption_top_users(seeded_app_both):
    from src.repositories import usage_repo

    _seed_overall(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/top-users?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["username"] for x in rows] == ["alice@test.com", "bob@test.com"]
    assert rows[0]["active_hours"] == 1.0  # 3600s
    assert rows[1]["active_hours"] == 0.5  # 1800s
    # Rows carry the keys the UI links/renders on.
    for x in rows:
        assert "user_id" in x and "username" in x and "last_active" in x


def test_adoption_top_users_enriched_from_users_table(seeded_app_both):
    """Rows carry the real users-table identity (name/email/registered) so
    the UI renders the same person as /admin/users. Resolution is by
    user_id; an unmatched session identity is registered=False."""
    from src.repositories import usage_repo

    repo = usage_repo()
    # Matches the seeded analyst1 / analyst@test.com / "Analyst" user by id.
    repo.upsert_summary(
        _summary(
            session_file="analyst/s1.jsonl",
            username="analyst",
            user_id="analyst1",
            active_seconds=3600,
        ),
        processor_version=1,
    )
    # No matching user — username is not a local-part of any registered email.
    repo.upsert_summary(
        _summary(
            session_file="ghost/s2.jsonl",
            username="ghost",
            user_id=None,
            active_seconds=60,
        ),
        processor_version=1,
    )

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/top-users?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    rows = {x["username"]: x for x in r.json()["rows"]}
    assert rows["analyst"]["registered"] is True
    assert rows["analyst"]["name"] == "Analyst"
    assert rows["analyst"]["email"] == "analyst@test.com"
    assert rows["ghost"]["registered"] is False
    assert rows["ghost"]["name"] is None and rows["ghost"]["email"] is None


def test_adoption_top_users_local_part_fallback(seeded_app_both):
    """Legacy rows without a user_id resolve by matching the username
    against the email local-part — but only when it is unambiguous."""
    from src.repositories import usage_repo, users_repo

    # Two users sharing the local-part "dup" → ambiguous, must NOT resolve.
    users_repo().create(id="d1", email="dup@a.com", name="Dup A")
    users_repo().create(id="d2", email="dup@b.com", name="Dup B")
    repo = usage_repo()
    repo.upsert_summary(
        _summary(
            session_file="analyst/s1.jsonl",
            username="analyst",
            user_id=None,
            active_seconds=3600,
        ),
        processor_version=1,
    )
    repo.upsert_summary(
        _summary(
            session_file="dup/s2.jsonl",
            username="dup",
            user_id=None,
            active_seconds=60,
        ),
        processor_version=1,
    )

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/top-users?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    rows = {x["username"]: x for x in r.json()["rows"]}
    # Unique local-part "analyst" → resolves to analyst@test.com.
    assert rows["analyst"]["registered"] is True
    assert rows["analyst"]["email"] == "analyst@test.com"
    # Ambiguous local-part "dup" → left unresolved.
    assert rows["dup"]["registered"] is False
    assert rows["dup"]["email"] is None


def test_adoption_top_skills(seeded_app_both):
    from src.repositories import usage_repo

    _seed_overall(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/top-skills?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    by = {x["skill_name"]: x for x in r.json()["rows"]}
    assert by["grpn:foo"]["invocations"] == 2
    assert by["grpn:foo"]["distinct_users"] == 1
    assert by["bar"]["invocations"] == 1


# ---------------------------------------------------------------------------
# Per-user drill-down — /users/{id}/...
# ---------------------------------------------------------------------------


def _seed_for_analyst(repo) -> None:
    """Seed under both the user_id (analyst1) and derived username
    (analyst, the email local-part) the per-user query matches on."""
    repo.upsert_summary(
        _summary(
            session_file="analyst/s9.jsonl",
            username="analyst",
            user_id="analyst1",
            active_seconds=7200,
            wall_seconds=9000,
            user_messages=20,
            tool_calls=50,
            tool_errors=3,
            mcp_calls=8,
            subagent_dispatches=2,
            primary_model="claude-opus",
        ),
        processor_version=1,
    )
    repo.upsert_events(
        [
            _event(username="analyst", user_id="analyst1", tool_name="Bash", skill_name="grpn:foo"),
            _event(username="analyst", user_id="analyst1", tool_name="Bash"),
            _event(username="analyst", user_id="analyst1", tool_name="Edit"),
        ],
        processor_version=1,
    )


def test_adoption_user_kpis(seeded_app_both):
    from src.repositories import usage_repo

    _seed_for_analyst(usage_repo())

    r = seeded_app_both["client"].get(
        "/api/admin/adoption/users/analyst1/kpis?window=7d",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["user_id"] == "analyst1"
    assert b["email"] == "analyst@test.com"
    assert b["active_hours"] == 2.0  # 7200s
    assert b["sessions"] == 1
    assert b["prompts"] == 20
    # ALL calls: 50 native + 8 MCP + 2 subagent.
    assert b["tool_calls"] == 60
    assert b["tool_errors"] == 3
    assert b["distinct_tools"] == 2  # Bash + Edit
    assert b["distinct_skills"] == 1  # grpn:foo
    assert b["active_days"] == 1
    assert b["models"][0]["model"] == "claude-opus"


def test_adoption_user_series_and_top(seeded_app_both):
    from src.repositories import usage_repo

    _seed_for_analyst(usage_repo())
    client, hdr = seeded_app_both["client"], _h(seeded_app_both["admin_token"])

    s = client.get("/api/admin/adoption/users/analyst1/series", headers=hdr)
    assert s.status_code == 200, s.text
    days = s.json()["days"]
    assert len(days) == 30
    for d in days:
        assert "tool_calls" in d and "skill_invocations" in d

    tools = client.get("/api/admin/adoption/users/analyst1/top-tools?window=7d", headers=hdr)
    assert tools.status_code == 200, tools.text
    by = {x["tool_name"]: x["invocations"] for x in tools.json()["rows"]}
    assert by["Bash"] == 2 and by["Edit"] == 1


def test_adoption_user_unknown_id_404(seeded_app_both):
    r = seeded_app_both["client"].get(
        "/api/admin/adoption/users/nope/kpis",
        headers=_h(seeded_app_both["admin_token"]),
    )
    assert r.status_code == 404, r.text


def test_adoption_user_forbids_non_admin(seeded_app_both):
    r = seeded_app_both["client"].get(
        "/api/admin/adoption/users/analyst1/kpis",
        headers=_h(seeded_app_both["analyst_token"]),
    )
    assert r.status_code == 403, r.text
