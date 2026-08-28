"""``POST /api/admin/semantic-models/{id}/detach`` and ``.../reattach``
(F3) — Postgres-backend behavior.

Both routes are PG-only (A3 ratchet); the DuckDB clean-501 behavior is
covered by ``tests/test_semantic_models_detach_api.py``. Uses
``seeded_app_both`` (dual-backend endpoint harness), skipping the DuckDB
param — matches ``tests/db_pg/test_semantic_autodraft_sweep_pg.py``'s
pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def _skip_unless_pg(state_backend) -> None:
    if state_backend != "pg":
        pytest.skip("PG-only")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_source_owned_model(*, id="keboola_metastore/proj1/orders", slug="orders", source="keboola_metastore"):
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="doc",
        document_json={"semantic_model": [{"name": slug}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source=source,
        source_ref="proj1",
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(timezone.utc),
    )


class TestDetach:
    def test_400_when_model_has_no_source_to_detach_from(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_source_owned_model(id="manual/_/orders", source="manual")

        c = seeded_app_both["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "not_source_owned"

    def test_400_without_confirm(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_source_owned_model()

        c = seeded_app_both["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "confirm_required"

    def test_detach_sets_sync_mode_and_audit_fields(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_source_owned_model()

        c = seeded_app_both["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sync_mode"] == "detached"
        assert body["detached_by"] == "admin@test.com"
        assert body["detached_at"] is not None

    def test_409_on_a_second_detach(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_source_owned_model()
        c = seeded_app_both["client"]
        c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )

        r = c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "already_detached"

    def test_detach_is_audited(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        row = _seed_source_owned_model()
        c = seeded_app_both["client"]
        c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )

        from src.repositories import audit_repo

        entries, _cursor = audit_repo().query(action="semantic_model.detach", limit=50)
        assert len(entries) == 1
        assert entries[0]["resource"] == row["id"]


class TestCreateAfterDetach:
    """``POST /api/admin/semantic-models`` on a slug that already belongs to
    a DETACHED source-owned row must update that row in place, not create a
    second ``manual/_/<slug>`` row next to it — ``_is_source_owned`` exempts
    a detached row from the 409, but the row still owns the slug, and two
    rows sharing one slug is exactly the nondeterministic-``get_by_slug``
    collision the source-ownership guard exists to prevent."""

    def test_create_updates_the_detached_row_instead_of_shadowing_it(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        seeded = _seed_source_owned_model()
        c = seeded_app_both["client"]
        c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_detach": True},
        )

        doc = (
            "version: '0.2.0.dev0'\n"
            "semantic_model:\n"
            "  - name: orders\n"
            "    description: edited\n"
            "    datasets:\n"
            "      - name: orders\n"
            "        source: db.public.orders\n"
            "        fields: []\n"
        )
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": doc},
            headers=_auth(seeded_app_both["admin_token"]),
        )
        assert r.status_code == 201, r.text
        body = r.json()

        # Same row, same id/provenance — not a new manual/_/orders row.
        assert body["id"] == seeded["id"]
        assert body["source"] == "keboola_metastore"
        assert body["source_ref"] == "proj1"
        assert body["document"] == doc

        listed = c.get("/api/admin/semantic-models", headers=_auth(seeded_app_both["admin_token"])).json()
        matching = [m for m in listed if m["slug"] == "orders"]
        assert len(matching) == 1, "no shadow manual/_/orders row alongside the detached one"


class TestReattach:
    def _detach(self, c, token):
        _seed_source_owned_model()
        c.post(
            "/api/admin/semantic-models/orders/detach",
            headers=_auth(token),
            json={"confirm_detach": True},
        )

    def test_409_when_not_detached(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_source_owned_model()

        c = seeded_app_both["client"]
        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_reattach": True},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "not_detached"

    def test_confirm_required_preview_shows_staleness(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        c = seeded_app_both["client"]
        self._detach(c, seeded_app_both["admin_token"])

        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={},
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "confirm_required"
        assert detail["source_changed_since_detach"] is False
        assert detail["detached_at"] is not None

    def test_reattach_clears_detach_state(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        c = seeded_app_both["client"]
        self._detach(c, seeded_app_both["admin_token"])

        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_reattach": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sync_mode"] == "synced"
        assert body["detached_at"] is None
        assert body["detached_by"] is None
        assert body["detach_base_hash"] is None

    def test_409_source_gone(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        c = seeded_app_both["client"]
        self._detach(c, seeded_app_both["admin_token"])

        from src.repositories import semantic_model_repo

        row = semantic_model_repo().get_by_slug("orders")
        semantic_model_repo().mark_source_missing(row["id"])

        r = c.post(
            "/api/admin/semantic-models/orders/reattach",
            headers=_auth(seeded_app_both["admin_token"]),
            json={"confirm_reattach": True},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "source_gone"
