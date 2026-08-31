"""``POST /api/admin/semantic-models/{id}/detach`` and ``.../reattach``
(F3): the two backend-agnostic edges.

Both routes are PG-only (A3 ratchet — ``sync_mode`` and friends don't exist
on DuckDB), gated by an ``is not use_pg()`` check before touching anything
else, so on the DuckDB-backed ``seeded_app`` they must fail clean with a
typed 501 rather than crash or silently no-op. The admin gate runs first
(FastAPI dependency), so RBAC is identical on either backend too.

Happy-path behavior (detach/reattach state transitions, guards, audit) can
only run against Postgres — see
``tests/db_pg/test_semantic_models_detach_api_pg.py``.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_source_owned_model(conn, *, id="keboola_metastore/proj1/orders", slug="orders"):
    from src.repositories.semantic_models import SemanticModelsRepository

    SemanticModelsRepository(conn).upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="doc",
        document_json={"semantic_model": [{"name": slug}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source="keboola_metastore",
        source_ref="proj1",
        status="valid",
        validation_errors=None,
        validated_at=None,
    )
    return id


class TestRequiresAdmin:
    def test_detach_denied_for_non_admin(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app["analyst_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 403

    def test_reattach_denied_for_non_admin(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app["analyst_token"]),
            json={"confirm_reattach": True},
        )
        assert r.status_code == 403


class TestRequiresPostgresBackend:
    def test_detach_fails_clean_with_a_typed_501_on_duckdb(self, seeded_app):
        from src.db import get_system_db

        conn = get_system_db()
        model_id = _seed_source_owned_model(conn)

        c = seeded_app["client"]
        r = c.post(
            f"/api/admin/semantic-models/{model_id}/detach",
            headers=_auth(seeded_app["admin_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 501, r.text
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "semantic_model_detach"

    def test_reattach_fails_clean_with_a_typed_501_on_duckdb(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app["admin_token"]),
            json={"confirm_reattach": True},
        )
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"

    def test_backend_guard_runs_before_the_model_lookup(self, seeded_app):
        """A nonexistent model id must still 501, not 404 — the backend
        guard is the first thing either route does."""
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models/does-not-exist/detach",
            headers=_auth(seeded_app["admin_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 501, r.text
