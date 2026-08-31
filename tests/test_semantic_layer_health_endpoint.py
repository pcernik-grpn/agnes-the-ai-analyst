"""RBAC + PG-gate contract for ``GET /api/admin/semantic-layer/health``.

Same shape as ``tests/test_semantic_model_coverage_endpoint.py``: this file
runs against the DEFAULT (DuckDB-backed) app fixture, so it pins the two
things that must hold there — the admin gate fires first, and past it the
failure is the typed ``501 requires_postgres_backend`` (the mutes half of the
roll-up is Postgres-only), never a crash.

The per-check logic (sync failures, disconnected models, invalid documents,
the static document-quality checks, the coverage summary, the mute overlay)
lives in ``tests/db_pg/test_semantic_layer_health_pg.py``, where a Postgres
backend exists to compute it against.
"""

from __future__ import annotations

_HEALTH = "/api/admin/semantic-layer/health"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTheAdminGateFiresBeforeAnythingElse:
    def test_health_requires_authentication(self, seeded_app):
        resp = seeded_app["client"].get(_HEALTH)
        assert resp.status_code == 401

    def test_health_refuses_a_non_admin(self, seeded_app):
        resp = seeded_app["client"].get(_HEALTH, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestTheDuckDbInstanceFailsClean:
    """Not "does not work" — fails in the ONE documented way the parity
    sweeps' ``assert_pg_only_exemptions_fail_clean`` accepts.

    The mutes overlay is Postgres-only (F4.3); everything else the health
    roll-up reads (``semantic_sources``, ``semantic_models``,
    ``compute_cross_domain_coverage``) has no such restriction on its own.
    Since ONE ingredient is PG-only, the whole endpoint answers the typed 501
    rather than a partial report that silently drops the mute overlay — a
    health check that hides which of its own findings are silenced is the
    exact failure mode F4.3 exists to prevent.
    """

    def test_health_answers_a_typed_501(self, seeded_app):
        resp = seeded_app["client"].get(_HEALTH, headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 501, resp.text
        body = resp.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "semantic_health_mutes"
