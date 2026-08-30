"""Detach/re-attach toolbar on the semantic-layer detail page (issue #1707,
findings A1/A2/A9).

PG-only by necessity: ``sync_mode='detached'`` is an F3 (Postgres-only, A3
ratchet) column — the DuckDB sibling has nothing to back it, so a row there
always reads back as ``sync_mode='synced'`` and the toolbar's detached
branch never renders (see ``app/web/router.py::semantic_layer_detail``'s
``.get()`` comment). Follows the ``state_backend``/``seeded_app_both`` +
``if state_backend != "pg": pytest.skip(...)`` shape used by
``tests/db_pg/test_semantic_health_mutes_pg.py`` and
``tests/db_pg/test_semantic_models_detach_api_pg.py`` (the latter is also
where ``semantic_model_repo().detach(...)`` — the way to put a model into
the detached state without going through the confirm/CSRF-guarded HTTP
endpoint — comes from).
"""

from __future__ import annotations

from datetime import datetime, timezone


def _skip_unless_pg(state_backend) -> None:
    import pytest

    if state_backend != "pg":
        pytest.skip("PG-only (sync_mode/detach columns do not exist on DuckDB)")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_detached_imported_model(*, id="keboola_metastore/proj1/orders", slug="orders") -> dict:
    """A model imported from a source, then detached — the only state that
    renders the "Detached" badge + Re-attach button branch of the toolbar."""
    from src.repositories import semantic_model_repo

    repo = semantic_model_repo()
    row = repo.upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="# fixture, not schema-authored",
        document_json={"semantic_model": [{"name": slug, "datasets": [], "metrics": []}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source="keboola_metastore",
        source_ref="proj1",
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(timezone.utc),
    )
    return repo.detach(row["id"], by="admin@test.com", base_hash="h1")


class TestDetachToolbarPage:
    """A1 (export href), A2 (button classes) and A9 (this file itself)."""

    def test_detached_imported_model_shows_badge_and_working_buttons(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _seed_detached_imported_model()

        s = seeded_app_both
        r = s["client"].get("/semantic-layer/orders", headers=_auth(s["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.text

        assert "Detached" in body

        # A2: the two occurrences of the (now-nonexistent) `.btn--sm` class
        # rendered borderless text — the real button styles live two lines
        # below in the same template, `btn-secondary`/`btn-primary` + `btn-sm`.
        assert 'id="slb-reattach-btn"' in body
        assert (
            '<button type="button" class="btn btn-secondary btn-sm" id="slb-reattach-btn">' in body
        ), "Re-attach button must use the real btn-secondary/btn-sm classes, not the dead btn--sm"
        assert "btn--sm" not in body

        # A1: the export link must point at the route that actually serves
        # the document (`GET /api/semantic-models/{slug}.yaml`), not the
        # HTML detail route the slug-with-suffix 404s against. Resolved from
        # the app's own route table, not a copy of the template literal, so
        # a future rename of the export route breaks this test instead of
        # silently drifting back out of sync with the template.
        expected_href = s["client"].app.url_path_for("export_semantic_model", slug="orders")
        assert f'href="{expected_href}"' in body
        assert 'href="/semantic-layer/orders.yaml"' not in body

        export = s["client"].get(expected_href, headers=_auth(s["admin_token"]))
        assert export.status_code == 200, export.text

    def test_detach_to_edit_button_on_a_still_attached_imported_model(self, state_backend, seeded_app_both):
        """The other branch of the toolbar (F3's "not yet detached" state)
        gets the same A2 class fix — `btn-primary btn-sm`, not `btn--sm`."""
        _skip_unless_pg(state_backend)
        from src.repositories import semantic_model_repo

        semantic_model_repo().upsert(
            id="keboola_metastore/proj1/customers",
            slug="customers",
            name="customers",
            description=None,
            document="# fixture, not schema-authored",
            document_json={"semantic_model": [{"name": "customers", "datasets": [], "metrics": []}]},
            spec_version="0.2.0.dev0",
            content_hash="h1",
            source="keboola_metastore",
            source_ref="proj1",
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(timezone.utc),
        )

        s = seeded_app_both
        r = s["client"].get("/semantic-layer/customers", headers=_auth(s["admin_token"]))
        assert r.status_code == 200, r.text
        assert (
            '<button type="button" class="btn btn-primary btn-sm" id="slb-detach-btn">' in r.text
        ), "Detach button must use the real btn-primary/btn-sm classes, not the dead btn--sm"
        assert "btn--sm" not in r.text

    def test_non_admin_with_read_access_does_not_see_the_detach_toolbar(self, state_backend, seeded_app_both):
        """A grant is enough to read the page (RBAC tier for `_can_read_model`
        is any-authenticated-user-with-a-grant, not admin-only) but the
        detach/re-attach buttons are an admin-only affordance — a non-admin
        reader must see the "Detached" badge without either button."""
        _skip_unless_pg(state_backend)
        row = _seed_detached_imported_model(id="keboola_metastore/proj1/invoices", slug="invoices")

        from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

        group = user_groups_repo().create(name="Semantic Model Readers", description="", created_by="admin1")
        group_id = group["id"] if isinstance(group, dict) else group
        user_group_members_repo().add_member("analyst1", group_id, source="test")
        resource_grants_repo().create(group_id, "semantic_model", row["id"], "admin1")

        s = seeded_app_both
        r = s["client"].get("/semantic-layer/invoices", headers=_auth(s["analyst_token"]))
        assert r.status_code == 200, r.text
        assert "Detached" in r.text
        # The bottom `<script>` block always references these element ids
        # (it wires click handlers unconditionally); the toolbar's actual
        # `<button id="...">` tags are what must be admin-gated.
        assert 'id="slb-reattach-btn"' not in r.text
        assert 'id="slb-detach-btn"' not in r.text
        # A1's failure mode was "the link 403s for exactly the users who can
        # see the page" — so the export route must answer the SAME non-admin
        # principal the page just answered, not only the admin.
        export = s["client"].get(
            "/api/semantic-models/invoices.yaml", headers=_auth(s["analyst_token"])
        )
        assert export.status_code == 200, export.text
