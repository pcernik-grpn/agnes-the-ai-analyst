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
    """Both refresh modules keep a module-level `_refresh_state` dict shared
    with their own endpoint tests — reset them around every test in this file
    too. The page's strip reads the sweep's; the Keboola one still backs the
    login-triggered sync."""
    from app.api import keboola_semantic_layer_refresh as keboola_module
    from app.api import semantic_sources_refresh as sweep_module

    reset = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    for module in (keboola_module, sweep_module):
        module._refresh_state.update(reset)
    yield
    for module in (keboola_module, sweep_module):
        module._refresh_state.update(reset)


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

    def test_the_health_tab_renders_its_own_section_not_coverage(self, seeded_app):
        """F4.2: Health is no longer a placeholder — it renders its own
        section (fetched after paint, like Feedback and Mute), never the
        coverage grid."""
        body = (
            seeded_app["client"].get("/admin/semantic-layer?tab=health", headers=_auth(seeded_app["admin_token"])).text
        )
        assert 'id="sl-coverage"' not in body
        assert 'id="sl-health"' in body

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


class TestTheHealthTab:
    """F4.2 — is the semantic layer trustworthy right now."""

    def _body(self, seeded_app) -> str:
        return (
            seeded_app["client"].get("/admin/semantic-layer?tab=health", headers=_auth(seeded_app["admin_token"])).text
        )

    def test_it_replaced_its_placeholder(self, seeded_app):
        body = self._body(seeded_app)
        assert 'id="sl-health"' in body
        assert 'class="sl-placeholder"' not in body

    def test_the_report_is_filled_from_the_health_endpoint(self, seeded_app):
        """Fetched after paint, like the coverage grid and the mute list: the
        mute overlay is Postgres-only, so server-rendering it would refuse to
        load the whole page on a DuckDB instance."""
        assert "/api/admin/semantic-layer/health" in self._body(seeded_app)

    def test_a_duckdb_instance_is_told_why_the_report_is_empty(self, seeded_app):
        """ "Nothing wrong" would read as "everything is fine" on an instance
        that cannot even compute the mute half of the report."""
        assert "requires_postgres_backend" in self._body(seeded_app)

    def test_it_links_to_coverage_and_mute(self, seeded_app):
        """The health report and the coverage grid answer different
        questions about the same layer — the tab says so and links between
        them, rather than leaving the reader to notice on their own."""
        body = self._body(seeded_app)
        assert "?tab=coverage" in body
        assert "?tab=mute" in body


class TestTheFeedbackTab:
    """F4.5 — the admin queue of "that answer looked wrong" reports."""

    def _body(self, seeded_app) -> str:
        return (
            seeded_app["client"]
            .get("/admin/semantic-layer?tab=feedback", headers=_auth(seeded_app["admin_token"]))
            .text
        )

    def test_it_replaced_its_placeholder(self, seeded_app):
        body = self._body(seeded_app)
        assert 'id="sl-feedback"' in body
        # The class still EXISTS (Health still uses it, and its rule lives in
        # the shared head block) — what must be gone is any element on this tab
        # wearing it.
        assert 'class="sl-placeholder"' not in body

    def test_the_queue_is_filled_from_the_feedback_endpoint(self, seeded_app):
        """Fetched after paint, like the coverage grid: the queue is
        Postgres-only, so server-rendering it would refuse to load the whole
        page on a DuckDB instance."""
        assert "/api/admin/semantic-feedback" in self._body(seeded_app)

    def test_a_duckdb_instance_is_told_why_the_queue_is_empty(self, seeded_app):
        """ "No reports" would read as "nobody complained" on an instance that
        cannot store a report at all."""
        assert "requires_postgres_backend" in self._body(seeded_app)

    def test_the_status_filter_offers_the_apis_own_vocabulary(self, seeded_app):
        """Read from `FEEDBACK_STATUSES`, not re-typed: a status the select
        offers but the endpoint rejects would 400 on click."""
        from src.models.semantic_feedback import FEEDBACK_STATUSES

        body = self._body(seeded_app)
        for status in FEEDBACK_STATUSES:
            assert f'value="{status}"' in body, f"{status} missing from the filter"

    def test_it_says_where_a_report_comes_from(self, seeded_app):
        """The queue is worked by admins but filed by anyone — the tab names
        the submit surfaces so an admin does not read it as admin-only."""
        body = self._body(seeded_app)
        assert "agnes semantic-model feedback submit" in body


class TestTheMuteTab:
    """F4.3 — silencing a check an admin already knows about, on the record."""

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get("/admin/semantic-layer?tab=mute", headers=_auth(seeded_app["admin_token"])).text

    def test_it_replaced_its_placeholder(self, seeded_app):
        body = self._body(seeded_app)
        assert 'id="sl-mute"' in body
        assert 'class="sl-placeholder"' not in body

    def test_the_list_is_filled_from_the_mutes_endpoint(self, seeded_app):
        """Fetched after paint, like the coverage grid and the feedback queue:
        the table is Postgres-only, so server-rendering it would refuse to load
        the whole page on a DuckDB instance."""
        assert "/api/admin/semantic-layer/mutes" in self._body(seeded_app)

    def test_a_duckdb_instance_is_told_why_the_list_is_empty(self, seeded_app):
        """ "Nothing muted" would read as "nobody silenced anything" on an
        instance that cannot store a mute at all."""
        assert "requires_postgres_backend" in self._body(seeded_app)

    def test_the_form_asks_for_a_reason(self, seeded_app):
        """The whole point of the feature: a mute carries who/when/why. The
        field is optional at the API, but the form must ASK — an empty reason
        should be a decision, not an omission nobody was prompted about."""
        body = self._body(seeded_app)
        assert 'id="sl-mute-reason"' in body

    def test_the_domain_picker_offers_the_reports_own_domains(self, seeded_app):
        """Read from `src.semantic.coverage.DOMAINS`, not re-typed: a domain the
        picker offers but the report never scores would mute nothing."""
        from src.semantic.coverage import DOMAINS

        body = self._body(seeded_app)
        for domain in DOMAINS:
            assert f'value="{domain}"' in body, f"{domain} missing from the mute picker"

    def test_a_source_can_be_picked_even_though_a_domain_alone_is_valid(self, seeded_app):
        """All three scope forms are reachable from one pair of selects —
        source-only, domain-only, and both."""
        _connection()
        body = self._body(seeded_app)
        assert 'value="conn-a"' in body
        assert "Production Project" in body

    def test_it_renders_with_no_source_connected(self, seeded_app):
        """Unlike the tagging form, this one still works: `domain:<domain>`
        mutes a check across every source and needs no connection at all."""
        body = self._body(seeded_app)
        assert 'id="sl-mute-form"' in body

    def test_the_coverage_grid_is_not_rendered_on_this_tab(self, seeded_app):
        assert 'id="sl-coverage"' not in self._body(seeded_app)


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
    def test_the_sync_status_strip_survives(self, seeded_app):
        """The page is no longer Keboola-shaped, but "when did the semantic
        sync last run, and did it fail" is still the one action this page
        owns — and `Sync now` is still here. Since #1707 Block 3 step 4 it
        reports the whole-sweep run, not one connector's."""
        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "Semantic sources sync" in body
        assert "Never synced yet" in body
        assert "sl-refresh-btn" in body
        # And the button posts to the sweep, not to a retired per-connector
        # endpoint — the whole point of the migration.
        assert "/api/admin/run-semantic-sources-refresh" in body
        assert "run-keboola-semantic-layer-refresh" not in body

    def test_a_failed_sync_is_reported(self, seeded_app):
        from app.api.semantic_sources_refresh import _record_completion

        _record_completion("error", "semantic sources sweep exploded: boom")

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        assert "semantic sources sweep exploded: boom" in body


class TestTheStripAfterARestart:
    """A10 (#1707): `_refresh_state` is in-memory BY DESIGN — "since last
    process restart" — so every redeploy empties it. The strip then read
    "Never synced yet." while /admin/semantic-sources listed the very same
    sources synced that morning, two of them with errors.

    The decision not to add a table stands; the SENTENCE was the bug. With no
    sweep in this process the strip falls back to what the source rows already
    carry durably — `max(last_sync_at)` across them — and says what that is:
    the last sync of ANY source, not a sweep. "Never synced yet." survives for
    the one case where it is finally true.
    """

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text

    @staticmethod
    def _synced(*ids: str) -> None:
        from src.repositories import semantic_source_repo

        repo = semantic_source_repo()
        for source_id in ids:
            repo.create(id=source_id, kind="upload", name=source_id, adapter="native", config={})
            repo.record_sync(source_id, status="ok", error=None)

    def test_a_sweep_in_this_process_still_wins(self, seeded_app):
        """The in-memory view is the richer one (counts, per-source results),
        so it is never displaced by the fallback — even with synced rows
        sitting right there to derive one from."""
        from app.api.semantic_sources_refresh import _record_completion

        self._synced("src-a")
        _record_completion("ok", {"synced": 1, "failed": 0})

        body = self._body(seeded_app)
        assert "Last run" in body
        assert "Last source sync" not in body
        assert "Never synced yet" not in body

    def test_no_sweep_but_synced_sources_reports_the_sources_own_last_sync(self, seeded_app):
        from src.repositories import semantic_source_repo

        self._synced("src-a", "src-b")
        # A third row that has never synced is not part of the claim.
        semantic_source_repo().create(id="src-never", kind="upload", name="src-never", adapter="native", config={})

        body = self._body(seeded_app)
        assert "Never synced yet" not in body
        assert "Last source sync" in body
        assert "no sweep since this instance restarted" in body
        assert "across 2 sources" in body

        newest = max(s["last_sync_at"] for s in semantic_source_repo().list_all() if s["last_sync_at"])
        assert newest.isoformat() in body

    def test_the_count_is_singular_for_one_source(self, seeded_app):
        self._synced("src-only")

        body = self._body(seeded_app)
        assert "across 1 source " in body
        assert "across 1 sources" not in body

    def test_sources_that_never_synced_still_read_never_synced_yet(self, seeded_app):
        """The one state the old sentence was always true for: rows exist,
        none of them has ever synced, no sweep has ever run."""
        from src.repositories import semantic_source_repo

        semantic_source_repo().create(id="src-never", kind="upload", name="src-never", adapter="native", config={})

        body = self._body(seeded_app)
        assert "Never synced yet" in body
        assert "Last source sync" not in body


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


class TestTheHealthTabReportsSilentlyEmptySources:
    """#1707: `hlRender` filtered `sources` on `last_sync_status === "error"`
    alone, so a source that synced fine and imported nothing produced no
    section AND still let the headline read "No sync failures, disconnected
    models, or invalid documents."

    The page renders client-side, so what is pinned here is the shell it
    renders FROM — the section, the predicate, and the absence of error
    styling on it.
    """

    def _body(self, seeded_app) -> str:
        return (
            seeded_app["client"].get("/admin/semantic-layer?tab=health", headers=_auth(seeded_app["admin_token"])).text
        )

    def test_the_report_has_its_own_section(self, seeded_app):
        assert "Sources that synced but imported nothing" in self._body(seeded_app)

    def test_the_section_reads_the_owned_model_count_field(self, seeded_app):
        assert "owned_model_count" in self._body(seeded_app)

    def test_the_finding_suppresses_the_nothing_is_wrong_headline(self, seeded_app):
        """`nothingWrong` must account for it, or the page says everything is
        fine while listing a finding right underneath."""
        body = self._body(seeded_app)
        nothing_wrong = body.split("const nothingWrong")[1].split(";")[0]
        assert "emptySources" in nothing_wrong

    def test_only_a_source_that_actually_synced_is_counted(self, seeded_app):
        """A never-synced source has not imported nothing — it has not run."""
        body = self._body(seeded_app)
        predicate = body.split("const emptySources")[1].split(";")[0]
        assert 'last_sync_status === "ok"' in predicate

    def test_it_is_not_rendered_with_error_styling(self, seeded_app):
        """Nothing failed; the fetch worked. It must not borrow the failure
        vocabulary (design-system: no danger accent for an attention state)."""
        body = self._body(seeded_app)
        section = body.split('hlSection(container, "Sources that synced but imported nothing"')[1].split("});")[0]
        assert "danger" not in section
        assert "error" not in section
