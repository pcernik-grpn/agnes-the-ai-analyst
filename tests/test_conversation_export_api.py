"""``GET /api/admin/conversations/export`` on the DEFAULT (DuckDB-backed) app
fixture (design 2026-09-08 §3.12).

``llm_calls`` and ``chat_message_feedback`` are Postgres-only tables (A3
PG-first ratchet) with no DuckDB sibling, so this route must fail CLEAN on
DuckDB — a typed ``501 requires_postgres_backend``, never a raw crash and
never a 400/422 that happens to also be an error status. The route's real
behaviour (pagination, content policy, audit row) lives in
``tests/db_pg/test_conversation_export_pg.py``, where a Postgres backend
exists to serve it.
"""

from __future__ import annotations

_EXPORT = "/api/admin/conversations/export"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_typed_501(resp) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "llm_calls"


class TestAdminGate:
    def test_requires_authentication(self, seeded_app):
        assert seeded_app["client"].get(_EXPORT).status_code == 401

    def test_refuses_a_non_admin(self, seeded_app):
        """The admin gate must win even when `since` is missing — a non-admin
        caller never learns anything about the route's PG-only/validation
        shape past "you may not call this"."""
        resp = seeded_app["client"].get(_EXPORT, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestTheDuckDbInstanceFailsClean:
    """Not "does not work" — fails in the ONE documented way the parity
    sweeps' ``assert_pg_only_exemptions_fail_clean`` accepts."""

    def test_bare_call_answers_a_typed_501(self, seeded_app, duckdb_backend_pinned):
        """No query params at all — the shape `assert_pg_only_exemptions_
        fail_clean` actually calls. The PG-only repo gate must beat BOTH the
        `since`-required validation and the content-policy check."""
        _assert_typed_501(seeded_app["client"].get(_EXPORT, headers=_auth(seeded_app["admin_token"])))

    def test_answers_a_typed_501_even_with_since_given(self, seeded_app, duckdb_backend_pinned):
        resp = seeded_app["client"].get(
            _EXPORT,
            params={"since": "2026-01-01"},
            headers=_auth(seeded_app["admin_token"]),
        )
        _assert_typed_501(resp)
