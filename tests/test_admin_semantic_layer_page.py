"""Tests for the /admin/semantic-layer page.

REBUILT in F4.1. The page used to be a Keboola sync-ops view: one row per
master-token Keboola connection, plus hand-computed "connections without a
master token", "orphaned rows" and "legacy / unattributed" sections. All of
that is gone — not moved, gone — and its assertions with it:

* ``connections_without_master`` is now a ROW in the cross-domain report (the
  wrapper in ``src/semantic/coverage.py`` fills the hole K0.5 leaves), not a
  separate list beneath the table;
* the connection-scoped ``orphaned`` count measured stale flat
  ``metric_definitions`` / ``glossary_terms`` rows, which are projections of
  the canonical document rather than the truth; its successor is F4.2's
  source-agnostic "disconnected models" check;
* ``null_absorbed`` / "legacy / unattributed" is the report's synthetic
  ``__local__`` bucket.

What stays identical: the URL and the ``require_admin`` gate.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict shared with the refresh
    endpoint tests — reset it around every test in this file too."""
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    reset = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    endpoint_module._refresh_state.update(reset)
    yield
    endpoint_module._refresh_state.update(reset)


class TestSemanticLayerPageAuth:
    def test_semantic_layer_page_requires_admin(self, seeded_app):
        c = seeded_app["client"]

        anon_resp = c.get("/admin/semantic-layer", follow_redirects=False)
        assert anon_resp.status_code in (302, 303, 307)

        token = seeded_app["analyst_token"]
        non_admin_resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert non_admin_resp.status_code == 403

    def test_admin_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Semantic layer" in resp.text


class TestTheTabStrip:
    def test_all_four_tabs_render(self, seeded_app):
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        for label in ("Coverage", "Health", "Mute", "Feedback"):
            assert f">{label}<" in body, f"{label} tab missing"

    def test_coverage_is_the_default_tab(self, seeded_app):
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="sl-coverage"' in body
        assert "?tab=health" in body

    def test_another_tab_renders_its_own_placeholder_not_coverage(self, seeded_app):
        """Health / Mute / Feedback land in later waves. The tab exists so the
        page's shape is settled, and says plainly that it is empty — an
        invisible tab would be re-designed from scratch by whoever lands it."""
        body = (
            seeded_app["client"].get("/admin/semantic-layer?tab=health", headers=_auth(seeded_app["admin_token"])).text
        )
        assert 'id="sl-coverage"' not in body
        assert "sl-placeholder" in body

    def test_an_unknown_tab_falls_back_to_coverage(self, seeded_app):
        body = (
            seeded_app["client"]
            .get("/admin/semantic-layer?tab=nonsense", headers=_auth(seeded_app["admin_token"]))
            .text
        )
        assert 'id="sl-coverage"' in body


class TestTheCoverageTab:
    def test_it_names_every_domain_as_a_column(self, seeded_app):
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        for label in ("Semantic", "Metrics", "Glossary", "Skill", "Agent", "Knowledge base"):
            assert label in body, f"{label} column missing"

    def test_the_grid_is_filled_from_the_cross_domain_endpoint(self, seeded_app):
        """Fetched after paint, not server-rendered: the Keboola provider
        makes upstream calls, and blocking a page render on every connected
        project's Metastore is what the retired page already refused to do.
        It also keeps the page loading on a DuckDB instance, where the
        Postgres-only report answers 501."""
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/semantic-model/coverage" in body

    def test_a_duckdb_instance_is_told_why_the_grid_is_empty(self, seeded_app):
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "requires_postgres_backend" in body


def _connection(conn_id: str = "conn-a", *, name: str = "Production Project") -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=conn_id,
        name=name,
        source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
        is_default=True,
        created_by="test",
    )


class TestTheTaggingForm:
    def test_it_offers_the_three_taggable_resource_types(self, seeded_app):
        _connection()
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        for value in ("marketplace_plugin", "agent", "memory_domain"):
            assert f'value="{value}"' in body, f"{value} not offered"

    def test_with_no_source_connected_the_form_says_so_instead_of_rendering(self, seeded_app):
        """A picker with an empty source list is a form that cannot be
        submitted — name the missing prerequisite instead."""
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="sl-tag-form"' not in body
        assert "No data sources are connected yet" in body

    def test_it_lists_the_connected_sources(self, seeded_app):
        _connection()
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert 'value="conn-a"' in body
        assert "Production Project" in body

    def test_it_reuses_the_rbac_list_blocks_projection(self, seeded_app):
        """The picker recycles ``app/resource_types.py``'s ``list_blocks``
        delegates — the same projection /admin/access renders — rather than a
        second, drifting query over the same tables."""
        from src.repositories import memory_domains_repo

        _connection()
        memory_domains_repo().create(
            name="Quarterly Reporting",
            slug="quarterly-reporting",
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Quarterly Reporting" in body

    def test_the_action_link_prefills_the_form(self, seeded_app):
        """A `missing` cell's action href is
        ``?tab=coverage&tag_source=…&tag_type=…`` — landing on the page with
        nothing selected would make the link a navigation, not an action."""
        _connection()

        body = (
            seeded_app["client"]
            .get(
                "/admin/semantic-layer?tab=coverage&tag_source=conn-a&tag_type=agent",
                headers=_auth(seeded_app["admin_token"]),
            )
            .text
        )
        assert 'value="conn-a" selected' in body
        assert 'value="agent" selected' in body


class TestTheRetiredKeboolaSpecificSections:
    """These are deletions, so they are asserted as absences. Each has a
    named successor (see the module docstring) — none is a capability lost."""

    def test_the_page_no_longer_computes_connections_without_master(self, seeded_app):
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="conn-tokenless",
            name="Forgotten Project",
            source_type="keboola",
            config={"stack_url": "https://connection.example.com"},
            is_default=True,
            created_by="test",
        )

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Also connected, but not syncing" not in body
        assert "no master (owner) token" not in body

    def test_the_orphaned_rows_section_is_gone(self, seeded_app):
        from src.repositories import metric_repo

        metric_repo().create(
            id="revenue/ghost",
            name="ghost",
            display_name="Ghost",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-deleted",
        )

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Orphaned rows" not in body
        assert "legacy / unattributed" not in body

    def test_the_skipped_metrics_section_is_gone(self, seeded_app):
        """Its content now rides inside the report as the Keboola provider's
        ``domains.semantic.raw.unregistered_tables``, in the one general place
        every source type's detail appears."""
        from app.api import keboola_semantic_layer_refresh as endpoint_module

        endpoint_module._refresh_state["last_result"] = {
            "status": "ok",
            "sources": [
                {
                    "connection_id": "conn-a",
                    "status": "ok",
                    "skipped_unresolved_table": 50,
                    "unresolved_tables": ["in.c-demo.customers"],
                }
            ],
        }

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Skipped metrics" not in body
        assert "in.c-demo.customers" not in body

    def test_the_orphan_reason_helper_is_gone_from_the_router(self):
        """It existed only for the deleted section. Its three-way "why is this
        connection not syncing" answer survives as
        ``src.semantic.coverage._keboola_missing_master_reason``."""
        import app.web.router as router

        assert not hasattr(router, "_orphan_reason")

    def test_the_three_way_missing_token_reason_survived_the_move(self, seeded_app):
        from src.repositories import source_connections_repo
        from src.semantic.coverage import _keboola_missing_master_reason

        source_connections_repo().create(
            id="conn-nourl",
            name="No URL",
            source_type="keboola",
            config={},
            is_default=False,
            created_by="test",
        )
        conn = source_connections_repo().get("conn-nourl")
        assert "owner" in _keboola_missing_master_reason(conn)


class TestTheSyncStrip:
    def test_the_keboola_sync_status_strip_survives(self, seeded_app):
        """The page is no longer Keboola-shaped, but "when did the Keboola
        semantic-layer sync last run, and did it fail" is still the one action
        this page owns — and `Sync now` is still here."""
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Never synced yet" in body
        assert "sl-refresh-btn" in body

    def test_a_failed_sync_is_reported(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("error", {"status": "error", "error": "Metastore fetch failed: boom"})

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "failed" in body.lower()


def test_the_data_sources_page_shows_both_mismatch_codes():
    """Devin Review on #1248: the more serious warning was filtered out.

    A master token contradicting the project the connection is locked to
    stops the sync outright — and the message tells the admin to go to this
    page, which then did not show it.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    assert 'w.code === "master_token_project_mismatch"' in src
    assert 'w.code === "token_project_mismatch"' in src
