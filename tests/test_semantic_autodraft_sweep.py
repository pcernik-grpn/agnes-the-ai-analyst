"""``POST /api/admin/semantic-auto-draft-sweep`` (semantic-phase5, wave 2):
finds tables with zero semantic-layer coverage and drafts a model for a
bounded batch of them via a headless ``semantic-model-builder`` chat
session, landing each result in the ``authoring_suggestions`` moderation
queue like any human-submitted proposal.

A3 PG-first ratchet: the sweep's dedup flag
(``table_registry.mark_semantic_draft_pending`` / ``clear_semantic_draft_
pending``) is a Postgres-only column, so the endpoint itself is gated
PG-only (``if not use_pg(): raise RequiresPostgresBackend(...)`` — see
``app/api/semantic_models.py``). This file covers the two backend-agnostic
edges: the admin-only gate (``require_admin`` runs before the backend
check, so it 403s identically on either backend) and the clean-501 fail
mode on a DuckDB-backend instance. Every other behavior (dedup, batch
limiting, concurrency-cap degradation, applied/no_apply_call detection,
system-identity/surface wiring) can only run against Postgres and lives in
``tests/db_pg/test_semantic_autodraft_sweep_pg.py``.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestRequiresAdmin:
    def test_non_admin_is_denied(self, seeded_app):
        c = seeded_app["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestRequiresPostgresBackend:
    def test_duckdb_backend_fails_clean_with_a_typed_501(self, seeded_app):
        """A3 PG-first ratchet: on a DuckDB-backend instance the dedup
        column this sweep depends on doesn't exist, so the endpoint must
        never attempt any real work — it raises ``RequiresPostgresBackend``
        before touching the coverage read or the chat manager, and
        ``app/main.py``'s app-wide handler translates that into a clean
        501 naming the feature."""
        c = seeded_app["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 501, r.text
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "semantic-auto-draft-sweep"
