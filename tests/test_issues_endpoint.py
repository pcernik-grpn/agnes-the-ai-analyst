"""RBAC + PG-gate contract for issue reporting (``app/api/issues.py``).

``issue_reports`` is a Postgres-only table (A3 PG-first ratchet — ``CLAUDE.md``
-> "Dual-backend discipline"). This file runs against the DEFAULT
(DuckDB-backed) app fixture, so it pins the two things that must hold there:

* the RBAC shape — **filing and following your own reports is open to any
  signed-in user** (whoever hit the problem is the one who can describe it),
  while the admin queue and resolving a report are admin-only;
* past the gate, the failure is the typed ``501 requires_postgres_backend`` —
  never a 500, never a 422 about a field the instance could not have used.

The behaviour itself (a report reaches the queue, comments thread, resolve
transition, screenshot round-trip) lives in
``tests/db_pg/test_issues_api_pg.py``, where a Postgres backend exists to
serve it.
"""

from __future__ import annotations

_CREATE = "/api/issues"
_MINE = "/api/issues/mine"
_QUEUE = "/api/admin/issues"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_typed_501(resp) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "issue_reports"


class TestWhoMayReport:
    def test_create_requires_authentication(self, seeded_app):
        assert seeded_app["client"].post(_CREATE, json={"title": "x"}).status_code == 401

    def test_create_is_open_to_a_non_admin_and_fails_clean_on_duckdb(self, seeded_app, duckdb_backend_pinned):
        """NOT 403. Restricting "report a problem" to admins would mean the
        only people who can file one are the ones who never hit it."""
        resp = seeded_app["client"].post(
            _CREATE,
            json={"title": "Tables render raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code != 403
        _assert_typed_501(resp)

    def test_create_answers_a_typed_501_even_with_an_empty_body(self, seeded_app, duckdb_backend_pinned):
        """The PG gate must beat body validation.

        The mutation parity sweep POSTs ``{}`` to every parameter-free
        route; a 422 there would hide the backend answer behind a complaint
        about a field the instance could never have used. That is why the
        repository resolves as a DEPENDENCY, not inside the handler body.
        """
        _assert_typed_501(seeded_app["client"].post(_CREATE, json={}, headers=_auth(seeded_app["analyst_token"])))

    def test_mine_is_open_to_a_non_admin(self, seeded_app, duckdb_backend_pinned):
        _assert_typed_501(seeded_app["client"].get(_MINE, headers=_auth(seeded_app["analyst_token"])))

    def test_show_is_open_to_a_non_admin(self, seeded_app, duckdb_backend_pinned):
        _assert_typed_501(seeded_app["client"].get(f"{_CREATE}/iss_x", headers=_auth(seeded_app["analyst_token"])))

    def test_comment_is_open_to_a_non_admin(self, seeded_app, duckdb_backend_pinned):
        resp = seeded_app["client"].post(
            f"{_CREATE}/iss_x/comments",
            json={"body": "more detail"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code != 403
        _assert_typed_501(resp)

    def test_screenshot_put_is_open_to_a_non_admin(self, seeded_app, duckdb_backend_pinned):
        resp = seeded_app["client"].put(
            f"{_CREATE}/iss_x/screenshot",
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
            headers={**_auth(seeded_app["analyst_token"]), "Content-Type": "image/png"},
        )
        assert resp.status_code != 403
        _assert_typed_501(resp)


class TestTheQueueIsAdminOnly:
    def test_queue_requires_authentication(self, seeded_app):
        assert seeded_app["client"].get(_QUEUE).status_code == 401

    def test_queue_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].get(_QUEUE, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_resolve_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].post(
            f"{_QUEUE}/iss_x/resolve",
            json={},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_queue_answers_a_typed_501(self, seeded_app, duckdb_backend_pinned):
        _assert_typed_501(seeded_app["client"].get(_QUEUE, headers=_auth(seeded_app["admin_token"])))

    def test_resolve_answers_a_typed_501(self, seeded_app, duckdb_backend_pinned):
        _assert_typed_501(
            seeded_app["client"].post(
                f"{_QUEUE}/iss_x/resolve",
                json={"resolution_note": "n"},
                headers=_auth(seeded_app["admin_token"]),
            )
        )
