"""The workspace prompt must tell the sandbox agent the truth about its runtime.

Before this guard existed, ``config/claude_md_template.txt`` gave the chat
sandbox laptop-workspace advice — "Sync data regularly with `agnes pull`",
"run `agnes pull` first", a Directory Structure of paths that don't exist
there — and said nothing about how the sandbox authenticates. Observed on a
live instance: the in-chat agent concluded auth was broken (`agnes auth
whoami` → "Not logged in"), reported `Tables: 0` as a problem to fix, and
recommended `agnes pull`. These tests pin the split: the ``is_sandbox=True``
render explains the brokered-identity model and forbids the laptop-only
commands; the laptop render keeps them.

Render helper cloned from tests/test_chat_answer_provenance_and_charts.py
(same production path: ``compute_default_claude_md``), parametrized by
``is_admin`` because the sandbox admin guidance branches on ``user.is_admin``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

BUNDLED_FALLBACK = Path("app/initial_workspace_default/CLAUDE.md")


def _render(*, is_sandbox: bool, is_admin: bool = False) -> str:
    import duckdb

    from src.claude_md import compute_default_claude_md
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    try:
        _ensure_schema(conn)
        with patch("src.repositories.get_system_db", lambda: conn):
            user = {
                "id": "u1",
                "email": "alice@example.com",
                "name": "Alice",
                "is_admin": is_admin,
                "groups": ["Everyone"] + (["Admin"] if is_admin else []),
            }
            return compute_default_claude_md(conn, user=user, server_url="https://example.com", is_sandbox=is_sandbox)
    finally:
        conn.close()


class TestSandboxRender:
    def test_explains_brokered_auth(self):
        md = _render(is_sandbox=True)
        assert "This sandbox — how you run and authenticate" in md
        assert "secret broker" in md
        assert "alice@example.com" in md
        assert "brokered identity" in md or "brokered" in md

    def test_normalizes_the_empty_local_state(self):
        """`Tables: 0` / server-side fallback must be framed as the NORMAL
        state, and the laptop-only commands must be explicitly off the table."""
        md = _render(is_sandbox=True)
        assert "Tables: 0" in md
        assert "running server-side" in md
        assert "Do NOT run `agnes pull`" in md

    def test_drops_laptop_only_advice(self):
        md = _render(is_sandbox=True)
        assert "Sync data regularly with `agnes pull`" not in md
        assert "## Private sessions" not in md
        assert "## Corporate Memory" not in md
        assert "server/parquet/*.parquet" not in md
        assert "(if missing locally, run `agnes pull` first)" not in md

    def test_admin_variant_names_the_read_only_boundary(self):
        md = _render(is_sandbox=True, is_admin=True)
        assert "read-only admin commands work" in md
        assert "admin_mutations_require_interactive_auth" in md
        assert "https://example.com/admin" in md

    def test_non_admin_variant_says_so(self):
        md = _render(is_sandbox=True, is_admin=False)
        assert "not in the Admin group" in md
        assert "read-only admin commands work" not in md


class TestLaptopRender:
    def test_keeps_the_sync_workflow(self):
        md = _render(is_sandbox=False)
        assert "Sync data regularly with `agnes pull`" in md
        assert "## Private sessions" in md
        assert "## Corporate Memory" in md
        assert "server/parquet/*.parquet" in md

    def test_has_no_sandbox_framing(self):
        md = _render(is_sandbox=False)
        assert "This sandbox — how you run and authenticate" not in md
        assert "brokered" not in md


class TestBundledFallback:
    """`app/initial_workspace_default/CLAUDE.md` is the static fallback the
    sandbox gets when the server render fails — it must carry the same truths
    (it cannot branch, so it states them unconditionally)."""

    def test_explains_auth_and_forbids_laptop_commands(self):
        md = BUNDLED_FALLBACK.read_text(encoding="utf-8")
        assert "## How you are authenticated" in md
        assert "no local token" in md.lower() or "holds no credential" in md
        assert "admin_mutations_require_interactive_auth" in md

    def test_no_stale_local_synced_copy_claim(self):
        md = BUNDLED_FALLBACK.read_text(encoding="utf-8")
        assert "runs against your local synced copy" not in md
        assert "`agnes pull` — download the newly-subscribed tables" not in md
