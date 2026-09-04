"""`GET /api/admin/sessions/{username}/{session_file}/{download,transcript}`
must work with the identity the operator can actually see.

Two names collide on that `{username}` URL segment:

* the **display username**, which `/api/admin/sessions/list` reports (and
  `agnes admin sessions list` prints) as the user's e-mail since v60, and
* the **on-disk directory** under ``SESSION_DATA_DIR``, named after
  ``users.id`` (upload API / chat export) or the e-mail local-part (legacy
  collector).

Only the second one ever resolved, and the allowlist regex rejected `@`
outright — so an admin copying the e-mail out of the CLI listing got
`400 invalid username`, and stripping the domain got `404 session not
found`. Both forms must reach the file now (#2266).

The containment guards must survive that widening: `@` is not a path
separator, and traversal is caught by ``resolve()``/``relative_to(root)``
plus the ``.jsonl`` filename regex — not by the username character class
(`..` already matched the old class). The escape tests below are the proof.
"""

from __future__ import annotations

import uuid

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mkuser(email: str) -> str:
    """Create a user row, returning its (UUID) id."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    uid = str(uuid.uuid4())
    conn = get_system_db()
    try:
        UserRepository(conn).create(id=uid, email=email, name="Test User")
    finally:
        conn.close()
    return uid


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "user_sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SESSION_DATA_DIR", str(root))
    return root


def _write_session(root, dir_name: str, filename: str = "session-001.jsonl") -> str:
    d = root / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(
        '{"type":"user","timestamp":"2026-01-01T00:00:00Z",'
        '"message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n',
        encoding="utf-8",
    )
    return filename


class TestDisplayEmailReachesTheFile:
    def test_download_by_email_resolves_uuid_dir(self, seeded_app, sessions_root):
        """Upload-API / chat-export layout: directory is ``users.id``."""
        email = "first.last@example.com"
        uid = _mkuser(email)
        fname = _write_session(sessions_root, uid)

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{email}/{fname}/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert b'"role":"user"' in resp.content

    def test_download_by_email_resolves_localpart_dir(self, seeded_app, sessions_root):
        """Legacy collector layout: directory is the e-mail local-part."""
        email = "second.user@example.com"
        _mkuser(email)
        fname = _write_session(sessions_root, "second.user")

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{email}/{fname}/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_download_by_plus_addressed_email(self, seeded_app, sessions_root):
        """Plus-addressed e-mails are ordinary user identities; the segment
        must not 400 on `+` either."""
        email = "third.user+agnes@example.com"
        uid = _mkuser(email)
        fname = _write_session(sessions_root, uid)

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{email}/{fname}/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_download_by_on_disk_dir_still_works(self, seeded_app, sessions_root):
        """The web UI sends `session_dir` (the directory name). Unchanged."""
        uid = _mkuser("fourth.user@example.com")
        fname = _write_session(sessions_root, uid)

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{uid}/{fname}/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_transcript_by_email_finds_the_summary_row(self, seeded_app, sessions_root):
        """The summary is keyed on `<on-disk dir>/<file>`, so the endpoint
        must look it up with the RESOLVED directory, not the e-mail the
        caller typed — otherwise the viewer renders "no summary row"."""
        from src.db import get_system_db

        email = "fifth.user@example.com"
        uid = _mkuser(email)
        fname = _write_session(sessions_root, uid)

        conn = get_system_db()
        try:
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
                [f"{uid}/{fname}", fname, email],
            )
        finally:
            conn.close()

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{email}/{fname}/transcript",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["events"], "transcript should render the seeded turn"
        assert body["summary"].get("active_seconds") == 10


class TestCliSurface:
    """`agnes admin sessions download` is the surface the bug was reported
    from: it prints the e-mail in its User column, so the e-mail has to be a
    usable argument. It carries no validation of its own — the only thing it
    owed was a correctly encoded request path."""

    def test_cli_download_reaches_the_endpoint(self, seeded_app, sessions_root, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from cli.commands.admin_sessions import sessions_app

        email = "seventh.user@example.com"
        uid = _mkuser(email)
        fname = _write_session(sessions_root, uid)

        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        captured: dict = {}

        def _fake_get(path, params=None, **kwargs):
            captured["path"] = path
            return client.get(path, headers=headers)

        monkeypatch.setattr("cli.commands.admin_sessions.api_get", _fake_get)
        out = tmp_path / "downloaded.jsonl"
        result = CliRunner().invoke(sessions_app, ["download", email, fname, "-o", str(out)])

        assert result.exit_code == 0, result.output
        # One path segment, percent-encoded — not a re-shaped request path.
        assert captured["path"] == f"/api/admin/sessions/seventh.user%40example.com/{fname}/download"
        assert out.read_text(encoding="utf-8").startswith('{"type":"user"')


class TestContainmentStillHolds:
    """Widening the character class must not widen what the path can reach."""

    def test_separator_in_username_is_refused(self):
        """`/` never enters the allowlist — the regex is still the first gate."""
        from fastapi import HTTPException

        from app.api.admin_sessions import _safe_session_path

        for bad in ("a/b", "..%2f..", "a\\b", "a b", "a\x00b"):
            with pytest.raises(HTTPException) as exc:
                _safe_session_path(bad, "session-001.jsonl")
            assert exc.value.status_code == 400
            assert exc.value.detail == "invalid username"

    def test_dotdot_username_is_refused_by_the_resolve_guard(self, sessions_root):
        """`..` matched the allowlist BEFORE this change too — containment
        comes from ``resolve()``/``relative_to(root)``, and it still fires."""
        from fastapi import HTTPException

        from app.api.admin_sessions import _safe_session_path

        outside = sessions_root.parent / "secret.jsonl"
        outside.write_text('{"secret":true}\n', encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            _safe_session_path("..", "secret.jsonl")
        assert exc.value.status_code == 400
        assert exc.value.detail == "path escape rejected"

    def test_dotdot_over_http_is_refused(self, seeded_app, sessions_root):
        outside = sessions_root.parent / "secret.jsonl"
        outside.write_text('{"secret":true}\n', encoding="utf-8")

        resp = seeded_app["client"].get(
            "/api/admin/sessions/%2E%2E/secret.jsonl/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code in (400, 404), resp.text
        assert b'"secret"' not in resp.content

    def test_symlinked_dir_out_of_root_is_refused(self, seeded_app, sessions_root):
        """A symlink is the escape the regex can never see; ``resolve()``
        is what catches it, and it still does."""
        outside_dir = sessions_root.parent / "elsewhere"
        outside_dir.mkdir()
        (outside_dir / "secret.jsonl").write_text('{"secret":true}\n', encoding="utf-8")
        (sessions_root / "escape").symlink_to(outside_dir, target_is_directory=True)

        resp = seeded_app["client"].get(
            "/api/admin/sessions/escape/secret.jsonl/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "path escape rejected"

    def test_email_localpart_cannot_climb_out(self, seeded_app, sessions_root):
        """The e-mail→directory resolution derives a local-part; that derived
        segment goes through the same containment guard."""
        outside = sessions_root.parent / "secret.jsonl"
        outside.write_text('{"secret":true}\n', encoding="utf-8")

        resp = seeded_app["client"].get(
            "/api/admin/sessions/..@example.com/secret.jsonl/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404, resp.text
        assert b'"secret"' not in resp.content

    def test_unknown_email_is_a_404_not_a_500(self, seeded_app, sessions_root):
        resp = seeded_app["client"].get(
            "/api/admin/sessions/nobody@example.com/session-001.jsonl/download",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404, resp.text

    def test_non_admin_is_still_refused(self, seeded_app, sessions_root):
        email = "sixth.user@example.com"
        uid = _mkuser(email)
        fname = _write_session(sessions_root, uid)

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/{email}/{fname}/download",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403, resp.text
