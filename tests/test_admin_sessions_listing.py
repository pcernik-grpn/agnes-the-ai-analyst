"""Contract test for /api/admin/sessions/list — the listing response
must surface a `session_dir` field derived from `session_file.split('/')[0]`
so the UI can build transcript / download URLs that target the on-disk
directory, NOT the display `username` (which v60 collapses to email).

Regression guard for the cross-file integration break that landed with
v60 (PR #458 admin telemetry rewrite): every test below seeds rows with
the post-v60 shape (username = email; session_file = `<dir>/<file>`) and
asserts the listing API exposes `session_dir` so the UI URL builder has
the directory to point at.

`_safe_session_path` also accepts the display email now and resolves it
(#2266, tests/test_admin_sessions_download_username.py) — but that costs a
user lookup per request and cannot disambiguate a deleted user's orphaned
directory, so `session_dir` stays the field the UI builds URLs from.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_session(conn, session_file: str, username: str) -> None:
    conn.execute(
        """
        INSERT INTO usage_session_summary
          (session_file, session_id, username, started_at, ended_at,
           active_seconds, wall_seconds, user_messages, assistant_messages,
           tool_calls, tool_errors, skill_invocations, subagent_dispatches,
           mcp_calls, slash_commands, distinct_tools, distinct_skills,
           primary_model, input_tokens, output_tokens, cache_read_tokens,
           cache_creation_tokens, processor_version)
        VALUES (?, ?, ?, current_timestamp, current_timestamp,
                10, 30, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 'sonnet', 0, 0, 0, 0, 2)
        """,
        [session_file, session_file, username],
    )


class TestSessionDirInListing:
    """The listing API must surface `session_dir` so the UI URL builder
    points at the on-disk directory, not the email-shaped `username`."""

    def test_uuid_dir_emits_uuid_as_session_dir(self, seeded_app):
        """Upload-API path: session_file = `<uuid>/<filename>`,
        username = email (post-v60). session_dir == the UUID."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn = get_system_db()
        try:
            _seed_session(
                conn,
                "b00f5e3e-aaaa-bbbb-cccc-dddddddddddd/session-001.jsonl",
                "alice@example.com",
            )
        finally:
            conn.close()

        resp = c.get(
            "/api/admin/sessions/list",
            headers=_auth(token),
            params={"since_minutes": 60},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["rows"]
        match = next((r for r in rows if r["session_file"].startswith("b00f5e3e-")), None)
        assert match is not None, "seeded row missing from listing"
        assert match["session_dir"] == "b00f5e3e-aaaa-bbbb-cccc-dddddddddddd"
        # Display column carries the email; URL builder must NOT use it.
        assert match["username"] == "alice@example.com"

    def test_localpart_dir_emits_localpart_as_session_dir(self, seeded_app):
        """Legacy collector path: session_file = `<local-part>/<filename>`,
        username = email. session_dir == the local-part."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn = get_system_db()
        try:
            _seed_session(
                conn,
                "bob/session-legacy.jsonl",
                "bob@example.com",
            )
        finally:
            conn.close()

        resp = c.get(
            "/api/admin/sessions/list",
            headers=_auth(token),
            params={"since_minutes": 60},
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        match = next((r for r in rows if r["session_file"] == "bob/session-legacy.jsonl"), None)
        assert match is not None
        assert match["session_dir"] == "bob"
        assert match["username"] == "bob@example.com"

    def test_orphan_row_with_dir_only_username_still_exposes_session_dir(self, seeded_app):
        """v60 leaves orphan rows (no user_id resolvable) with the old
        directory-name username; session_dir is still derived from
        session_file, not from username — so the UI URL builder is
        consistent across resolved and orphan rows."""
        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn = get_system_db()
        try:
            _seed_session(
                conn,
                "orphandir/session-orph.jsonl",
                "orphandir",  # username still the dir-name (orphan, untouched by v60)
            )
        finally:
            conn.close()

        resp = c.get(
            "/api/admin/sessions/list",
            headers=_auth(token),
            params={"since_minutes": 60},
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        match = next((r for r in rows if r["session_file"] == "orphandir/session-orph.jsonl"), None)
        assert match is not None
        assert match["session_dir"] == "orphandir"
        # Orphan row's username is still the dir-name (legacy fallback).
        assert match["username"] == "orphandir"
