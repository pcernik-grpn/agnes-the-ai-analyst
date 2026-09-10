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


class TestARestrictedPrincipalDoesNotCrash:
    """`get_current_user` can return a frozen principal, not a dict.

    Indexing it (`user["id"]`) raised a TypeError -> 500 on every reporter
    route, including the in-sandbox `report_issue` tool this channel was
    built for (#2402). These pin the decision each principal
    kind now gets, at the unit level: the routes themselves are covered on
    Postgres, and what matters here is that no shape reaches an index.
    """

    def _principals(self):
        from app.auth.session_principal import AgentPrincipal, DataAppViewerPrincipal, SessionPrincipal

        return AgentPrincipal, DataAppViewerPrincipal, SessionPrincipal

    def test_agent_files_as_its_caller(self):
        from app.api.issues import _reporter

        AgentPrincipal, _, _ = self._principals()
        p = AgentPrincipal(
            session_id="s1",
            agent_id="a1",
            owner_user_id="owner-1",
            owner_email="owner@example.com",
            intersection={},
            caller_user_id="caller-1",
            caller_email="caller@example.com",
        )
        assert _reporter(p) == ("caller-1", "caller@example.com")

    def test_agent_without_a_caller_files_as_its_owner(self):
        from app.api.issues import _reporter

        AgentPrincipal, _, _ = self._principals()
        p = AgentPrincipal(
            session_id="s1",
            agent_id="a1",
            owner_user_id="owner-1",
            owner_email="owner@example.com",
            intersection={},
        )
        assert _reporter(p) == ("owner-1", "owner@example.com")

    def test_single_participant_session_files_as_that_person(self):
        from app.api.issues import _reporter

        _, _, SessionPrincipal = self._principals()
        p = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["only-1"],
            participant_emails=["only@example.com"],
            intersection={},
        )
        assert _reporter(p) == ("only-1", "only@example.com")

    def test_a_shared_co_session_is_refused_not_guessed(self):
        import pytest as _pytest
        from fastapi import HTTPException

        from app.api.issues import _reporter

        _, _, SessionPrincipal = self._principals()
        p = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["a", "b"],
            participant_emails=["a@example.com", "b@example.com"],
            intersection={},
        )
        with _pytest.raises(HTTPException) as exc:
            _reporter(p)
        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "reporter_unidentified"

    def test_a_data_app_viewer_is_refused(self):
        """The narrowest principal stays narrow: `agnes_issues` is an internal
        table and #2383 deliberately denied this principal that surface."""
        import pytest as _pytest
        from fastapi import HTTPException

        from app.api.issues import _reporter

        _, DataAppViewerPrincipal, _ = self._principals()
        p = DataAppViewerPrincipal(
            slug="app",
            app_id="app-1",
            owner_user_id="owner-1",
            owner_email="owner@example.com",
            viewer_user_id="viewer-1",
            viewer_email="viewer@example.com",
            intersection={},
        )
        with _pytest.raises(HTTPException) as exc:
            _reporter(p)
        assert exc.value.status_code == 403

    def test_a_restricted_principal_is_never_admin(self):
        from app.api.issues import _is_admin

        AgentPrincipal, _, _ = self._principals()
        p = AgentPrincipal(
            session_id="s1",
            agent_id="a1",
            owner_user_id="owner-1",
            owner_email="owner@example.com",
            intersection={},
        )
        assert _is_admin(p) is False


class TestTheContextCapCountsBytes:
    """`_MAX_CONTEXT_BYTES` is named in bytes; Python string length is not.

    One emoji is four UTF-8 bytes, so counting code points accepted a context
    several times over the promised limit (#2402).
    """

    def test_multibyte_context_over_the_cap_is_refused(self):
        import pytest as _pytest
        from fastapi import HTTPException

        from app.api.issues import _MAX_CONTEXT_BYTES, _cap_context

        # Comfortably under the cap in characters, far over it in bytes.
        payload = {"notes": ["🙂" * 300 for _ in range(50)]}
        as_chars = len(str(payload))
        assert as_chars < _MAX_CONTEXT_BYTES, "fixture must be under the cap by character count"

        with _pytest.raises(HTTPException) as exc:
            _cap_context(payload)
        assert exc.value.detail["error"] == "context_too_large"

    def test_a_normal_context_still_passes(self):
        from app.api.issues import _cap_context

        assert _cap_context({"app_version": "0.101.0", "recent_errors": []}) is not None
