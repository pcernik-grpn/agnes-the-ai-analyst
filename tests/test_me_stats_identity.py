"""/me/activity self-stats resolve rows by ``users.id``, never by the
display ``username``.

Since v60 the session pipeline writes the FULL EMAIL into
``usage_session_summary.username`` (``services/session_pipeline/runner.py``,
``canonical_username = resolved_email or dir_name``) and the account UUID
into the ``user_id`` column added in v45. The self-stats reads used to
filter ``WHERE username = ?`` with the caller's UUID, so every token and
session panel on ``/me/activity`` matched nothing and rendered zero on
every instance.

These tests seed rows exactly as the pipeline writes them today and pin
the identity key from the outside (HTTP, non-admin caller).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


ANALYST_ID = "analyst1"
ANALYST_EMAIL = "analyst@test.com"


@pytest.fixture
def me_stats_client(seeded_app, tmp_path, monkeypatch):
    """Non-admin (analyst) client + an empty session-scan dir.

    The sessions endpoint also walks the filesystem for un-processed
    JSONL; pointing it at an empty directory keeps these tests about the
    database read alone.
    """
    monkeypatch.setenv("AGNES_SESSION_DATA_DIR", str(tmp_path / "no-sessions"))
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "no-sessions"))
    return seeded_app["client"], {"Authorization": f"Bearer {seeded_app['analyst_token']}"}


def _seed_pipeline_row(
    *,
    session_file: str,
    username: str,
    user_id: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    primary_model: str = "claude-opus-4-7",
) -> None:
    """Write a summary row the way ``session_pipeline.runner`` writes it."""
    from src.repositories import usage_repo

    now = datetime.now(timezone.utc)
    usage_repo().upsert_summary(
        {
            "session_file": session_file,
            "session_id": session_file.rsplit("/", 1)[-1],
            "username": username,
            "user_id": user_id,
            "started_at": now,
            "ended_at": now,
            "active_seconds": 30,
            "wall_seconds": 60,
            "user_messages": 2,
            "assistant_messages": 2,
            "tool_calls": 1,
            "tool_errors": 0,
            "primary_model": primary_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
        processor_version=2,
    )


def test_tokens_panel_counts_email_keyed_rows(me_stats_client):
    """The bug: rows keyed ``username=<email>`` + ``user_id=<uuid>`` were
    filtered with the caller's UUID against ``username`` and vanished."""
    client, headers = me_stats_client
    _seed_pipeline_row(
        session_file=f"{ANALYST_ID}/s1.jsonl",
        username=ANALYST_EMAIL,
        user_id=ANALYST_ID,
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=300,
        cache_creation_tokens=40,
    )

    resp = client.get("/api/me/stats/tokens", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["totals"]["input"] == 100
    assert body["totals"]["output"] == 200
    assert body["totals"]["cache_read"] == 300
    assert body["totals"]["cache_creation"] == 40
    assert body["totals"]["total"] == 640
    assert body["totals"]["sessions"] == 1
    assert [m["model"] for m in body["by_model"]] == ["claude-opus-4-7"]
    assert [s["session_file"] for s in body["top_sessions"]] == [f"{ANALYST_ID}/s1.jsonl"]
    assert sum(d["total"] for d in body["daily"]) == 640


def test_sessions_panel_lists_email_keyed_rows(me_stats_client):
    client, headers = me_stats_client
    _seed_pipeline_row(
        session_file=f"{ANALYST_ID}/s2.jsonl",
        username=ANALYST_EMAIL,
        user_id=ANALYST_ID,
        input_tokens=7,
        output_tokens=11,
    )

    resp = client.get("/api/me/stats/sessions", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 1
    row = body["rows"][0]
    assert row["session_file"] == f"{ANALYST_ID}/s2.jsonl"
    assert row["tokens_total"] == 18
    assert row["processed"] is True


def test_another_users_row_never_leaks(me_stats_client):
    """A row whose *username* happens to carry the caller's email but whose
    ``user_id`` is another account must stay invisible — the filter is the
    account id, not the display name (an email can be reassigned after an
    account is deleted and recreated)."""
    client, headers = me_stats_client
    _seed_pipeline_row(
        session_file="other-uid/s3.jsonl",
        username=ANALYST_EMAIL,
        user_id="some-other-account",
        input_tokens=9999,
        output_tokens=9999,
    )

    resp = client.get("/api/me/stats/tokens", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["totals"]["total"] == 0

    resp = client.get("/api/me/stats/sessions", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_legacy_rows_without_user_id_drop_out(me_stats_client):
    """Pre-v45 rows carry no ``user_id``. They are not backfilled and simply
    do not appear in the caller's own view — the totals stay exactly the
    sum of the rows that do carry the id."""
    client, headers = me_stats_client
    _seed_pipeline_row(
        session_file=f"{ANALYST_ID}/current.jsonl",
        username=ANALYST_EMAIL,
        user_id=ANALYST_ID,
        input_tokens=10,
        output_tokens=20,
    )
    _seed_pipeline_row(
        session_file=f"{ANALYST_EMAIL}/legacy.jsonl",
        username=ANALYST_EMAIL,
        user_id=None,
        input_tokens=500,
        output_tokens=500,
    )

    resp = client.get("/api/me/stats/tokens", headers=headers)
    assert resp.status_code == 200
    totals = resp.json()["totals"]
    assert totals["total"] == 30
    assert totals["sessions"] == 1

    resp = client.get("/api/me/stats/sessions", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [r["session_file"] for r in body["rows"]] == [f"{ANALYST_ID}/current.jsonl"]
