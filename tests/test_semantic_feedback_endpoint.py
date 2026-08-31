"""RBAC + PG-gate contract for the semantic-layer feedback channel.

``semantic_feedback`` is a Postgres-only table (A3 PG-first ratchet —
``CLAUDE.md`` -> "Dual-backend discipline"). This file runs against the
DEFAULT (DuckDB-backed) app fixture, so it pins the two things that must hold
there:

* the RBAC shape — **submit is open to any signed-in user** (an analyst who
  saw the bad answer is exactly who should file it), while the queue and the
  resolve action are admin-only;
* past the gate, the failure is the typed ``501 requires_postgres_backend`` —
  never a 500, never a 422 about a field the instance could not have used.

The behaviour itself (a report reaches the queue, resolve stamps who closed it)
lives in ``tests/db_pg/test_semantic_feedback_pg.py``, where a Postgres backend
exists to serve it.
"""

from __future__ import annotations

_SUBMIT = "/api/semantic-feedback"
_QUEUE = "/api/admin/semantic-feedback"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_typed_501(resp) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "semantic_feedback"


class TestWhoMayFileAReport:
    def test_submit_requires_authentication(self, seeded_app):
        resp = seeded_app["client"].post(_SUBMIT, json={"question": "why?"})
        assert resp.status_code == 401

    def test_submit_is_open_to_a_non_admin(self, seeded_app):
        """NOT 403. Restricting the report channel to admins would mean the
        only people who can flag a wrong number are the ones who never see it
        in an analysis — so the analyst's request must get past the gate and
        reach the (PG-only) storage layer."""
        resp = seeded_app["client"].post(
            _SUBMIT,
            json={"question": "What was MRR in June?"},
            headers=_auth(seeded_app["analyst_token"]),
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
            f"{_QUEUE}/sfb_x/resolve",
            json={"resolution_note": "I fixed it myself"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestTheDuckDbInstanceFailsClean:
    """Not "does not work" — fails in the ONE documented way the parity
    sweeps' ``assert_pg_only_exemptions_fail_clean`` accepts."""

    def test_submit_answers_a_typed_501_even_with_an_empty_body(self, seeded_app):
        """The PG gate must beat body validation.

        The mutation parity sweep POSTs ``{}`` to every parameter-free route;
        a 422 there would hide the backend answer behind a complaint about a
        field the instance could never have used. That is why the repository
        resolves as a DEPENDENCY, not inside the handler body.
        """
        _assert_typed_501(seeded_app["client"].post(_SUBMIT, json={}, headers=_auth(seeded_app["analyst_token"])))

    def test_the_queue_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(seeded_app["client"].get(_QUEUE, headers=_auth(seeded_app["admin_token"])))

    def test_resolve_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(
            seeded_app["client"].post(
                f"{_QUEUE}/sfb_x/resolve",
                json={"resolution_note": "n"},
                headers=_auth(seeded_app["admin_token"]),
            )
        )
