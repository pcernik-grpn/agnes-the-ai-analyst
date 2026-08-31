"""RBAC + PG-gate contract for the cross-domain coverage endpoints.

The three routes under ``/api/admin/semantic-model/coverage`` read
``resource_source_tags``, a Postgres-only table (A3 PG-first ratchet —
``CLAUDE.md`` -> "Dual-backend discipline"). This file runs against the
DEFAULT (DuckDB-backed) app fixture, so it pins the two things that must hold
there: the admin gate fires first, and past it the failure is the typed
``501 requires_postgres_backend`` — never a 500, never a 422 about a field
the instance could not have used anyway.

The per-domain status logic lives in ``tests/db_pg/test_semantic_model_
coverage_pg.py``, where a Postgres backend exists to compute it against.
"""

from __future__ import annotations

_COVERAGE = "/api/admin/semantic-model/coverage"
_TAGS = "/api/admin/semantic-model/coverage/tags"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_typed_501(resp) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "resource_source_tags"


class TestTheAdminGateFiresBeforeAnythingElse:
    def test_coverage_requires_authentication(self, seeded_app):
        resp = seeded_app["client"].get(_COVERAGE)
        assert resp.status_code == 401

    def test_coverage_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].get(_COVERAGE, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_tag_create_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].post(
            _TAGS,
            json={"resource_type": "agent", "resource_id": "ag-1", "source_id": "conn-a"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_tag_delete_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].delete(f"{_TAGS}/rst_x", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestTheDuckDbInstanceFailsClean:
    """Not "does not work" — fails in the ONE documented way the parity
    sweeps' ``assert_pg_only_exemptions_fail_clean`` accepts."""

    def test_coverage_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(seeded_app["client"].get(_COVERAGE, headers=_auth(seeded_app["admin_token"])))

    def test_tag_create_answers_a_typed_501_even_with_an_empty_body(self, seeded_app):
        """The PG gate must beat body validation.

        The mutation parity sweep POSTs ``{}`` to every parameter-free route;
        a 422 there would hide the backend answer behind a complaint about a
        field the instance could never have used. That is why the repository
        resolves as a DEPENDENCY, not inside the handler body.
        """
        _assert_typed_501(seeded_app["client"].post(_TAGS, json={}, headers=_auth(seeded_app["admin_token"])))

    def test_tag_create_answers_a_typed_501_with_a_valid_body(self, seeded_app):
        _assert_typed_501(
            seeded_app["client"].post(
                _TAGS,
                json={"resource_type": "agent", "resource_id": "ag-1", "source_id": "conn-a"},
                headers=_auth(seeded_app["admin_token"]),
            )
        )

    def test_tag_delete_answers_a_typed_501(self, seeded_app):
        _assert_typed_501(seeded_app["client"].delete(f"{_TAGS}/rst_x", headers=_auth(seeded_app["admin_token"])))


class TestTheNewEndpointDoesNotDisplaceTheKeboolaOne:
    def test_the_keboola_coverage_route_still_exists(self, seeded_app):
        """``GET /api/admin/semantic-layer/coverage`` (K0.5) is a PROVIDER
        inside the new report, not a duplicate it retires — and it must keep
        working on a DuckDB instance, since it reads no PG-only table."""
        routes = {getattr(r, "path", "") for r in seeded_app["client"].app.routes}
        assert "/api/admin/semantic-layer/coverage" in routes
        assert _COVERAGE in routes

        resp = seeded_app["client"].get("/api/admin/semantic-layer/coverage", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert resp.json() == {"sources": []}
