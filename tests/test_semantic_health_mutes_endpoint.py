"""RBAC + PG-gate contract for muting a semantic-layer health check.

``semantic_health_mutes`` is a Postgres-only table (A3 PG-first ratchet —
``CLAUDE.md`` -> "Dual-backend discipline"). This file runs against the DEFAULT
(DuckDB-backed) app fixture, so it pins the two things that must hold there:

* the RBAC shape — **every route is admin-only**. Muting is the one action on
  this surface that makes a warning stop shouting, so the authority to do it is
  the same authority that reads the report; and
* past the gate, the failure is the typed ``501 requires_postgres_backend`` —
  never a 500, never a 422 about a field the instance could not have used.

The behaviour itself (a mute is stored with its author, expiry hides it, unmute
removes it) lives in ``tests/db_pg/test_semantic_health_mutes_pg.py``, where a
Postgres backend exists to serve it.
"""

from __future__ import annotations

_MUTES = "/api/admin/semantic-layer/mutes"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_typed_501(resp) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "semantic_health_mutes"


class TestMutingIsAdminOnly:
    def test_listing_requires_authentication(self, seeded_app):
        assert seeded_app["client"].get(_MUTES).status_code == 401

    def test_listing_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].get(_MUTES, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_muting_refuses_a_non_admin(self, seeded_app):
        """An analyst who could silence the check that names their own gap is
        the one caller this feature cannot allow."""
        resp = seeded_app["client"].post(
            _MUTES,
            json={"scope": "domain:metrics", "reason": "not my problem"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unmuting_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].delete(f"{_MUTES}/shm_x", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestTheDuckDbInstanceFailsClean:
    """Not "does not work" — fails in the ONE documented way the parity
    sweeps' ``assert_pg_only_exemptions_fail_clean`` accepts."""

    def test_muting_answers_a_typed_501_even_with_an_empty_body(self, seeded_app):
        """The PG gate must beat body validation.

        The mutation parity sweep POSTs ``{}`` to every parameter-free route; a
        422 there would hide the backend answer behind a complaint about a
        field the instance could never have used. That is why the repository
        resolves as a DEPENDENCY, not inside the handler body.
        """
        _assert_typed_501(seeded_app["client"].post(_MUTES, json={}, headers=_auth(seeded_app["admin_token"])))

    def test_listing_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(seeded_app["client"].get(_MUTES, headers=_auth(seeded_app["admin_token"])))

    def test_unmuting_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(seeded_app["client"].delete(f"{_MUTES}/shm_x", headers=_auth(seeded_app["admin_token"])))
