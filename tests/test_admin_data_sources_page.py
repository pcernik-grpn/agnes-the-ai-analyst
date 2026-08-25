"""Tests for the /admin/data-sources "Add Keboola project" wizard page (#755).

Covers:
- Auth gate (admin loads, non-admin 403, unauthenticated redirect).
- Page-shell markers the JS hangs off.
- Vault-key-not-configured blocking banner + disabled affordance.
- Vault-key-configured: no banner, affordance enabled.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token):
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


class TestDataSourcesPageAuth:
    def test_admin_can_load_page(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Hero + nav-distinguishing copy (#755 acceptance: data vs MCP sources
        # legible from the page itself).
        assert "Data sources" in body
        assert "/admin/mcp-sources" in body

        # Page-shell markers the JS targets.
        assert 'id="ds-add-btn"' in body
        assert 'id="ds-conn-list"' in body
        assert 'id="ds-wizard-overlay"' in body
        assert 'id="ds-new-stack"' in body
        assert 'id="ds-new-token"' in body

        # Endpoint constants — guards against URL drift between UI and API.
        assert "/api/admin/source-connections" in body
        assert "/api/admin/register-table" in body

        # Per-card default + token-rotation controls, ported from the
        # now-retired Keboola section of /admin/datasource-credentials. They
        # live in the card's Actions menu and settings body now rather than in
        # a standing button strip, but every one of them is still reachable —
        # the redesign moved them, it did not drop them.
        assert "setDefaultConn" in body
        assert "Make default project" in body
        assert "toggleRotate" in body
        assert "Rotate storage token" in body
        assert 'class="ds-rotate-row"' in body

        # Master (owner) token controls — separate vault slot consumed by the
        # semantic-layer sync (task 8, #contract in Task 3). Labelled by what
        # it is FOR; the Keboola noun stays in the hint.
        assert "saveMasterToken" in body
        assert "removeMasterToken" in body
        assert "Semantic-layer token" in body
        assert "MASTER (owner) token" in body
        assert 'kind: "master"' in body

        # Reciprocal link to the vault-secrets page.
        assert "/admin/datasource-credentials" in body

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-sources", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)


class TestDataSourcesPageVaultBanner:
    def test_banner_shown_when_vault_key_unset(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" in body
        assert "AGNES_VAULT_KEY" in body
        # The "add" flow is disabled without a vault key.
        assert 'id="ds-add-btn" disabled' in body

    def test_no_banner_when_vault_key_set(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" not in body
        assert 'id="ds-add-btn" disabled' not in body


class TestDataSourcesPageCarriesNoSemanticStatusStrip:
    """No page-wide semantic-layer status band above the sources list.

    It used to render one — 'Semantic layer: Never synced yet.' / '… — OK.' /
    '… failed: <result>' at full width, in all three states (task 7 slim-down,
    #953 status visibility). The Data section now carries **Semantic layer as
    its own tab** in the same tab row, and every source card already carries a
    semantic cell in its pipeline strip, so the band was a third copy of the
    same fact — occupying the position directly above the list, which is the
    one an admin's eye lands on first.

    These invert the three cases the band was pinned for: whatever the recorded
    sync state, none of its sentences may reach this page. The Semantic layer
    DESTINATION must survive — removing the band must not remove the way there.
    """

    #: The band's own copy, per state. Absence of these is the contract; the
    #: bare words "Semantic layer" and "Never synced" stay legal, since the tab
    #: row and the per-card pipeline cells both use them.
    _BAND_COPY = ("Never synced yet.", "Last sync attempt", "— OK.")

    def _body(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-sources", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        return resp.text

    def test_no_band_when_nothing_synced_yet(self, seeded_app):
        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
        assert "/admin/semantic-layer" in body

    def test_no_band_after_failed_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("error", "needs a master token")

        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
        # The failure detail belonged to the band — it must not leak onto the
        # page some other way now that the band is gone.
        assert "needs a master token" not in body
        assert "/admin/semantic-layer" in body

    def test_no_band_after_successful_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("ok", {"status": "ok", "created_or_updated": 0, "pruned": 0})

        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
        assert "/admin/semantic-layer" in body


class TestWizardRegisterPayloadContract:
    """The wizard's register payload must send the BARE in-bucket name.

    The pre-fix wizard sent the full Keboola table id (`bucket.table`) as
    `source_table`; combined with the separate `bucket` field, the sync path
    re-composed `bucket.bucket.table` — every wizard-registered table then
    failed to materialize (doubled prefix → nonexistent table id upstream)
    and the catalog preview showed "not found". Text-assertion contract on
    the template so a future edit can't silently regress the payload.
    """

    @staticmethod
    def _template_text():
        from pathlib import Path

        tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
        return tpl.read_text(encoding="utf-8")

    def test_register_payload_uses_bare_table_name(self):
        tpl = self._template_text()
        assert 'data-table-bare="${_esc(bare)}"' in tpl
        assert "cb.dataset.tableBare || cb.dataset.tableName" in tpl
        # The regression: full table id sent as source_table.
        assert "source_table: cb.dataset.tableId" not in tpl

    def test_keboola_rows_carry_a_per_row_mode_select(self):
        """The Keboola picker must offer live vs materialized per row.

        The pre-fix wizard hardcoded `query_mode: "materialized"` for every
        Keboola row, so live (`remote`) Keboola tables were API-only even
        though the backend accepts them. The payload must read the row's
        select, defaulting to materialized so an untouched row behaves
        exactly as before.
        """
        tpl = self._template_text()
        # The per-row control, rendered inside the bucket-browser row…
        picker_row = tpl.split('data-table-bare="${_esc(bare)}"', 1)[1].split("ds-bucket-group", 1)[0]
        assert "data-kb-mode" in picker_row
        assert '<option value="materialized" selected>' in picker_row
        assert '<option value="remote">' in picker_row
        # …and the payload reads it, keeping the old behavior as fallback.
        assert 'query_mode: modeSel ? modeSel.value : "materialized"' in tpl
        # The regression: the mode hardcoded in the register payload.
        assert 'query_mode: "materialized",' not in tpl

    def test_scoped_token_note_wired(self):
        """Bucket-scoped tokens get a partial listing — the picker must say so."""
        tpl = self._template_text()
        assert 'data.scope === "token_buckets"' in tpl
        assert "ds-scope-note" in tpl

    def test_group_cache_holds_only_a_successful_load(self):
        """A failed group fetch must not be remembered as the group list.

        `_loadGroups()` short-circuits on `if (_groupsCache) return _groupsCache`
        and `[]` is truthy, so caching the empty fallback turned one transient
        failure into a permanently blank "Grant all to group" picker — every
        later grant answered "Pick a group first" until a full page reload.
        The failure paths must return `[]` without writing the cache.
        """
        tpl = self._template_text()
        body = tpl.split("async function _loadGroups()", 1)[1].split("async function _fillGrantPickers", 1)[0]
        # The regression: the fallback was cached on a non-OK response / throw.
        assert "_groupsCache = []" not in body
        # Exactly one write, and only on the success branch.
        assert body.count("_groupsCache =") == 1
        assert "if (r.ok) _groupsCache" in body
        # Both failure paths hand back an uncached empty list.
        assert body.count("return [];") == 2


class TestAddDataWizard:
    """The 4-step Add-data flow (connect → choose tables → bundle → share) —
    the redesign's one path from "nothing connected" to "data in someone's
    hands" (spec §3.6–3.8), grown out of the two-step Add-Keboola-project
    modal without dropping any of its behavior.

    The flow itself is client JS over endpoints that carry their own suites
    (source-connections, register-table, data-packages, grants); what this
    class pins is the SERVED SCAFFOLDING — the pieces whose silent absence
    would degrade the wizard back to the old two steps without failing
    anything else."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text

    def test_the_four_steps_are_declared(self, seeded_app):
        body = self._page(seeded_app)
        for pane in (
            "ds-wizard-step-connect",
            "ds-wizard-step-tables",
            "ds-wizard-step-bundle",
            "ds-wizard-step-share",
        ):
            assert pane in body
        # The steps strip names them in flow order. It is the SHARED drawer's
        # strip now (`css/drawer.css`) rather than a page-local `.ds-wsteps` —
        # same four steps, same order, one sheet with the group drawer.
        strip = body.split('class="ds-drawer__steps"', 1)[1].split("</div>", 1)[0]
        for label in ("Connect", "Choose tables", "Bundle", "Share"):
            assert label in strip

    def test_the_steps_are_buttons_and_only_connect_starts_reachable(self, seeded_app):
        """The strip is the way BACK into a step you have already been
        through, so a step has to be a real control — and a step you have not
        earned has to say so in its own state rather than by a click that
        does nothing. Served state: step 1 live, 2-4 disabled."""
        body = self._page(seeded_app)
        strip = body.split('class="ds-drawer__steps"', 1)[1].split("</div>", 1)[0]
        assert strip.count("<button") == 4, "every step must be a control"
        # aria-hidden would take the whole strip out of the a11y tree — it is
        # a navigation control now, not decoration.
        assert "aria-hidden" not in strip
        assert strip.count("disabled") == 3, "only Connect is reachable at open"
        assert 'data-wstep="1"' in strip and "is-now" in strip

    def test_the_table_picker_collapses_and_selects_by_bucket(self, seeded_app):
        """A real project lists dozens of buckets and hundreds of tables. The
        picker's unit of navigation is the bucket — closed by default, with
        its own tri-state checkbox as the bulk gesture — and the per-table
        checkboxes stay for the granular case. Pinning the renderer's
        contract, since the picker itself is built client-side."""
        body = self._page(seeded_app)
        # Bucket chrome: a toggle that reports its own expanded state, a
        # bucket-level checkbox, and the per-bucket selected/total read-out.
        for marker in (
            "ds-bucket-toggle",
            "ds-bucket-check",
            "data-bucket-group",
            "data-bucket-meta",
            'aria-expanded="${openByDefault}"',
        ):
            assert marker in body, marker
        # Closed unless the project has exactly one bucket, where there is
        # nothing to choose between.
        assert "const openByDefault = buckets.length === 1;" in body
        # Granular selection is untouched.
        assert "ds-table-checkbox" in body
        # Scale controls: filter, bulk select/clear, expand-all, and a count
        # that is over the WHOLE picker rather than over what the filter shows.
        for marker in (
            "data-picker-search",
            "data-picker-all",
            "data-picker-none",
            "data-picker-expand",
            "data-picker-count",
            "hidden by the filter",
        ):
            assert marker in body, marker

    def test_semantic_layer_opt_in_lives_on_connect(self, seeded_app):
        """The master-token requirement used to be discoverable only on the
        semantic-layer page's empty state; the wizard offers it at the moment
        of connecting, skippably."""
        body = self._page(seeded_app)
        assert 'id="ds-new-semantic"' in body
        assert 'id="ds-new-master"' in body
        assert "owner" in body  # the copy says WHICH token this is

    def test_bundle_and_share_write_through_the_canonical_apis(self, seeded_app):
        """The wizard must create real packages and real grants — the same
        rows /admin/data-packages and a group's Access tab edit — never a
        parallel store."""
        body = self._page(seeded_app)
        assert "/api/admin/data-packages" in body
        assert "/api/admin/grants" in body
        assert "/api/admin/groups" in body

    def test_bundle_step_can_add_to_existing_packages(self, seeded_app):
        """New tables don't always deserve a NEW package — a fresh source's
        churn table usually belongs in the Customer 360 that already exists.
        The board offers an existing-package picker; picking one adds an
        attach-only tray whose Finish POSTs tables to the package it names,
        creating nothing and leaving its sharing untouched (so it must stay
        off the share step — a card there would offer to write a second copy
        of grants that already stand)."""
        body = self._page(seeded_app)
        assert 'id="ds-existing-pkg-sel"' in body
        # The tray renders as a different authority than a to-be-created one…
        assert "ds-tray-exist" in body
        # …its name belongs to the package (read-only here)…
        assert "tray.committed || tray.existing" in body
        # …Finish attaches instead of creating…
        assert "tray.existing" in body and "/tables" in body
        # …and the outcome is SAID on the share step even when no new
        # package earned a card.
        assert "_wizardExistingAdds" in body

    def test_old_register_only_exit_is_preserved(self, seeded_app):
        """The pre-redesign behavior — register the selection and stop — is
        an explicit escape hatch, not removed."""
        body = self._page(seeded_app)
        assert 'id="ds-wizard-finish-early-btn"' in body

    def test_share_step_is_skippable(self, seeded_app):
        body = self._page(seeded_app)
        assert 'id="ds-wizard-skip-btn"' in body
        # The lede says WHY skipping is safe (the Overview catches it).
        assert "waits for you on the Overview" in body

    def test_add_deep_link_auto_opens(self, seeded_app):
        """The Overview's '+ Add data' lands on ?add=1 — the page script must
        read it, or the button silently becomes a plain nav link."""
        body = self._page(seeded_app)
        assert 'has("add")' in body

    def test_overview_carries_the_add_data_action(self, seeded_app):
        c = seeded_app["client"]
        hub = c.get(
            "/admin",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "/admin/data-sources?add=1" in hub


class TestAddDataConnectorPicker:
    """Step 1 opens on a connector picker (spec §3.7): Keboola and BigQuery
    are real flows; CSV and Jira are HONEST guidance — no CSV connector
    exists (files enter through Library Collections) and Jira is configured
    instance-side + webhook. Only step 1 varies by source; Bundle and Share
    are shared."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text

    def test_all_four_connectors_are_offered(self, seeded_app):
        body = self._page(seeded_app)
        for src in ("keboola", "bigquery", "csv", "jira"):
            assert f'data-wsrc="{src}"' in body
            assert f'data-wsrcform="{src}"' in body

    def test_bigquery_reads_the_real_credential_status(self, seeded_app):
        """BQ credentials are the instance service account — the wizard must
        consult /api/admin/datasource-secrets and route to Instance secrets,
        never grow its own credential field."""
        body = self._page(seeded_app)
        assert "/api/admin/datasource-secrets" in body
        assert "BIGQUERY_SERVICE_ACCOUNT_JSON" in body
        assert "/admin/datasource-credentials" in body

    def test_bigquery_registers_live_queries_and_defers_depth(self, seeded_app):
        """The wizard registers `remote` rows; saved queries / partitioning
        keep their full editor on /admin/tables — stated, not dropped."""
        body = self._page(seeded_app)
        assert '"remote"' in body.split("_registerBqRows", 1)[1].split("async function", 1)[0]
        assert "/admin/tables" in body

    def test_csv_guidance_is_honest_about_the_missing_connector(self, seeded_app):
        """`csv` is an alias with no connector (docs/DATA_SOURCES.md); the
        card must send people to Library Collections, not fake a form."""
        body = self._page(seeded_app)
        assert "collections-vs-data-packages" in body

    def test_existing_connection_shortcut_is_offered(self, seeded_app):
        """The wizard is also how MORE tables are added from a project that
        is already connected — without this, 'Add data' reads as 'new
        connection only'."""
        body = self._page(seeded_app)
        assert 'id="ds-wexisting-select"' in body
        assert 'id="ds-wexisting-btn"' in body


class TestSourcePipelineStrip:
    """The per-source pipeline strip — connected → synced → semantic →
    feeding whom, on one card (spec §3.7).

    Its whole value is that each cell is TRUE and actionable, so that is
    what these pin: the shape is per-connector (no semantic cell where there
    is no Metastore), a cell degrades rather than lying, and unattributable
    legacy rows are named as unlinked instead of reported as "no tables yet"
    — the misreading that made an eleven-table instance look empty.
    """

    def test_strip_is_computed_per_connection(self, seeded_app):
        import uuid

        from src.repositories import source_connections_repo
        from app.web.router import _source_pipelines

        conn_id = f"probe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Pipeline Probe {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
        )
        try:
            strips = _source_pipelines()
            assert conn_id in strips
            cells = strips[conn_id]
            assert set(cells) == {"tables", "sync", "semantic", "feeds"}
            # Nothing registered or granted yet — every cell says so rather
            # than guessing.
            assert cells["tables"]["count"] == 0
            assert cells["sync"]["last_sync"] is None
            assert cells["semantic"]["token"] is False
            assert cells["feeds"]["packages"] == 0
        finally:
            source_connections_repo().delete(conn_id)

    def test_bigquery_source_has_no_semantic_cell(self, seeded_app):
        """The strip is per-connector: the Metastore is a Keboola API, so a
        BigQuery source must not render a semantic cell at all (rather than
        an empty or misleading one)."""
        from src.repositories import source_connections_repo
        from app.web.router import _source_pipelines

        import uuid

        conn_id = f"bqprobe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"BQ Probe {conn_id[-4:]}",
            source_type="bigquery",
            config={"project_id": "acme-warehouse"},
        )
        try:
            cells = _source_pipelines()[conn_id]
            assert "semantic" not in cells
            assert {"tables", "sync", "feeds"}.issubset(cells)
        finally:
            source_connections_repo().delete(conn_id)

    def test_a_registered_table_is_attributed_to_its_connection(self, seeded_app):
        import uuid

        from src.repositories import source_connections_repo, table_registry_repo
        from app.web.router import _source_pipelines

        conn_id = f"attrprobe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Attributed Probe {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
        )
        tid = f"strip-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"strip_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-test",
            source_table="strip",
            query_mode="local",
            connection_id=conn_id,
        )
        try:
            cells = _source_pipelines()[conn_id]
            assert cells["tables"]["count"] == 1
            assert cells["tables"]["basis"] == "connection"
            # Registered but in no package — the end of the chain says so.
            assert cells["feeds"]["packages"] == 0
        finally:
            table_registry_repo().unregister(tid)
            source_connections_repo().delete(conn_id)

    def test_semantic_cell_counts_the_post_cutover_source_too(self, seeded_app):
        """The flat-table cutover changed the Keboola sync's written `source`
        from `keboola_semantic_layer` to `keboola_metastore`
        (`src/semantic/keboola_sources.py`). The strip's `semantic` cell must
        count a `keboola_metastore` row exactly like a legacy one — matching
        only the retired literal would show 0 on an upgraded instance that
        has already synced."""
        import uuid

        from src.repositories import glossary_repo, metric_repo, source_connections_repo
        from app.web.router import _source_pipelines

        conn_id = f"semprobe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Semantic Probe {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
        )
        metric_repo().create(
            id=f"keboola_metastore/{conn_id}/core/mrr",
            name="mrr",
            display_name="MRR",
            category="core",
            sql="SELECT 1",
            source="keboola_metastore",
            source_ref=conn_id,
        )
        glossary_repo().create(
            id=f"keboola_metastore/{conn_id}/core/mrr",
            term="MRR",
            definition="…",
            source="keboola_metastore",
            source_ref=conn_id,
        )
        try:
            cells = _source_pipelines()[conn_id]
            assert cells["semantic"]["metrics"] == 1
            assert cells["semantic"]["terms"] == 1
        finally:
            source_connections_repo().delete(conn_id)

    def test_the_page_serves_the_strip_data(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "SOURCE_PIPELINES" in body
        assert "_pipelineStripHtml" in body
        # Each cell routes to the page owning that stage.
        for href in ("/admin/tables", "/admin/sync", "/admin/semantic-layer", "/admin/data-packages"):
            assert href in body


class TestSourcesIsEveryConnector:
    """Sources means every source.

    The page used to say "Keboola projects", offer "+ Add Keboola project",
    and fetch `?source_type=keboola` — while the drawer behind that button
    registered BigQuery, CSV and Jira tables that then appeared nowhere on
    it. Three surfaces disagreeing about what a source is, on the page whose
    whole job is to teach that model. These pin the fix from both ends: the
    server synthesizes a card for a connector that keeps no connection row,
    and the client asks for every type rather than one.
    """

    def test_a_connector_with_no_connection_row_still_gets_a_card(self, seeded_app):
        """A registered BigQuery table means a BigQuery source exists, whether
        or not anything wrote a `source_connections` row for it."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import table_registry_repo

        tid = f"bqderived-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"bq_derived_{tid[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="derived",
            query_mode="remote",
        )
        try:
            inv = _source_inventory()
            card = next((d for d in inv["derived"] if d["source_type"] == "bigquery"), None)
            assert card is not None, "a registered BigQuery table must surface a BigQuery card"
            assert card["derived"] is True
            # It says where it is REALLY managed rather than offering controls
            # this page does not own.
            assert card["settings_href"] == "/admin/datasource-credentials"
            # And its tables are attributed to it, not reported as orphans.
            assert inv["pipelines"][card["id"]]["tables"]["count"] >= 1
        finally:
            table_registry_repo().unregister(tid)

    def test_a_derived_cards_strip_is_its_own_connectors(self, seeded_app):
        """Per-connector cells, not a fixed four: BigQuery is queried live, so
        "never synced" is its permanent and useless state — it carries the cost
        guard instead, and never a semantic cell."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import table_registry_repo

        tid = f"bqcost-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"bq_cost_{tid[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="cost",
            query_mode="remote",
        )
        try:
            inv = _source_inventory()
            card = next(d for d in inv["derived"] if d["source_type"] == "bigquery")
            cells = inv["pipelines"][card["id"]]
            assert "cost" in cells and "semantic" not in cells
            # The caps are read from live config and rendered as the operator
            # wrote them, so a raised cap shows the raised number.
            assert cells["cost"]["scan"].endswith("GiB")
            assert cells["cost"]["materialize"].endswith("GiB")
        finally:
            table_registry_repo().unregister(tid)

    def test_a_real_connection_of_that_type_owns_the_card_instead(self, seeded_app):
        """A derived card is a fallback, not a duplicate — a stored BigQuery
        connection means the page must show that one and not both."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"bqreal-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"BQ Real {conn_id[-4:]}",
            source_type="bigquery",
            config={"project_id": "acme-warehouse"},
        )
        try:
            inv = _source_inventory()
            assert conn_id in inv["pipelines"]
            assert not [d for d in inv["derived"] if d["source_type"] == "bigquery"]
        finally:
            source_connections_repo().delete(conn_id)

    def test_the_page_asks_for_every_source_type(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        # The Keboola-only filter is gone, and the derived cards ride along.
        assert "source_type=keboola" not in body
        assert "DERIVED_SOURCES" in body
        # One heading, one CTA, both connector-agnostic.
        assert "Connected sources" in body
        assert "+ Add source" in body
        assert "Keboola projects" not in body


class TestKeboolaImportAsManagedConnection:
    """The derived Keboola card's one-click fix for the two-hop detour: card
    -> "Open" -> server-config -> back to "+ Add source" before an admin
    could actually browse and register tables. "Import as managed
    connection" posts the instance-level credential straight to
    `POST /api/admin/source-connections`, so the card flips to a real,
    fully-interactive connection in place.

    A prior adversarial review flagged the string-substring tests below as
    unable to catch a regression that keeps the literal text but breaks the
    actual behavior — the node-executed `TestImportKeboolaConnectionBehavior`
    class exercises the real function against a mocked `fetch` instead.
    """

    @staticmethod
    def _template_text() -> str:
        from pathlib import Path

        tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
        return tpl.read_text(encoding="utf-8")

    def test_button_only_renders_for_the_keboola_derived_card(self):
        tpl = self._template_text()
        # Gated on a working credential, not merely a configured stack_url —
        # a stack_url with no token anywhere has nothing to import.
        assert 'row.source_type === "keboola" && row.stack_url && row.credentialed && row.token_env_allowlisted' in tpl
        assert "importKeboolaConnection('${id}')" in tpl
        # Scoped to Keboola only — the other derived connectors keep their
        # plain "Open" action untouched.
        facts_block = tpl.split("const facts = row.derived", 1)[1].split(": `", 1)[0]
        assert facts_block.count("importKeboolaConnection") == 1

    def test_import_posts_the_instance_credential_without_a_secret_paste(self):
        """`create_connection` accepts `token_env` alone — no secret paste
        required — so the button must send `name`, `source_type`,
        `config.stack_url`, `token_env`, and the vault-seeding opt-in."""
        tpl = self._template_text()
        fn = tpl.split("async function importKeboolaConnection(id) {", 1)[1].split("\nasync function ", 1)[0]
        assert 'source_type: "keboola"' in fn
        assert "config: { stack_url: row.stack_url }" in fn
        assert "token_env: row.token_env" in fn
        assert "seed_from_instance_credentials: true" in fn
        assert "fetch(API_CONNECTIONS, {" in fn

    def test_success_drops_the_stale_derived_entry_and_reloads_the_list(self):
        """No full page reload: the stale `DERIVED_SOURCES` entry (a
        page-load constant `loadConnections()` re-spreads on every refresh)
        must be dropped client-side so the derived card doesn't render
        alongside the real one it was just replaced by."""
        tpl = self._template_text()
        fn = tpl.split("async function importKeboolaConnection(id) {", 1)[1].split("\nasync function ", 1)[0]
        assert "DERIVED_SOURCES.findIndex" in fn
        assert "DERIVED_SOURCES.splice(idx, 1)" in fn
        assert "await loadConnections();" in fn


class TestImportKeboolaConnectionBehavior:
    """`importKeboolaConnection()` executed for real via `node` against a
    mocked `fetch`/`showToast`/`loadConnections` — not just string-matched
    against the template source. A future edit could keep every literal
    string above intact while breaking the guard, the request body, or the
    response handling, and none of those tests would notice; these would.
    """

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
        """The function's exact source, found by brace-matching from
        `signature` rather than by looking for the NEXT declaration (which
        broke once a plain, non-`async` function could follow)."""
        start = tpl.index(signature)
        depth = 0
        started = False
        for i in range(start, len(tpl)):
            ch = tpl[i]
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    return tpl[start : i + 1]
        raise AssertionError(f"unbalanced braces extracting {signature!r}")

    def _run(self, *, row: dict, fetch_ok: bool, fetch_status: int, response_json: dict) -> dict:
        """Runs `importKeboolaConnection('derived:keboola')` under node with
        a stubbed `fetch`, `showToast`, and `loadConnections`, and returns
        what happened: the request actually sent (or `None`), every toast
        call, how many times the list was reloaded, and the resulting
        `DERIVED_SOURCES` length."""
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fn = self._extract_function(tpl, "async function importKeboolaConnection(id) {")

        script = f"""
{fn}

const API_CONNECTIONS = "/api/admin/source-connections";
let _connections = [{json.dumps(row)}];
let DERIVED_SOURCES = [{json.dumps(row)}];
const toasts = [];
function showToast(msg, ok) {{ toasts.push([msg, ok]); }}
let loadCalls = 0;
async function loadConnections() {{ loadCalls++; }}
let sentRequest = null;
global.fetch = async (url, opts) => {{
  sentRequest = {{ url, opts }};
  return {{
    ok: {str(fetch_ok).lower()},
    status: {fetch_status},
    json: async () => ({json.dumps(response_json)}),
  }};
}};

(async () => {{
  await importKeboolaConnection("derived:keboola");
  console.log(JSON.stringify({{
    body: sentRequest ? JSON.parse(sentRequest.opts.body) : null,
    url: sentRequest ? sentRequest.url : null,
    toasts,
    loadCalls,
    derivedSourcesLength: DERIVED_SOURCES.length,
  }}));
}})();
"""
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            proc = subprocess.run(["node", path], capture_output=True, text=True)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode == 127:
            pytest.skip("node unavailable")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    def test_the_guard_refuses_to_request_when_not_credentialed(self):
        """A stale render (or a race with a background refresh) must not let
        the click through to a POST that would create a connection with
        nothing to actually reach Keboola with."""
        result = self._run(
            row={
                "id": "derived:keboola",
                "source_type": "keboola",
                "stack_url": "https://connection.keboola.com",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
                "credentialed": False,
            },
            fetch_ok=True,
            fetch_status=201,
            response_json={"id": "new-conn"},
        )
        assert result["body"] is None, "no request should have been sent"
        assert result["loadCalls"] == 0
        assert result["toasts"], "the admin must be told why nothing happened"
        assert result["toasts"][0][1] is False

    def test_the_guard_refuses_to_request_when_token_env_is_not_allowlisted(self):
        """Devin Review: `create_connection` rejects an unallowlisted
        `token_env` before anything else — a credentialed card whose
        configured `token_env` isn't on the remote-attach allowlist would
        otherwise dead-end at a 400 despite looking ready to import."""
        result = self._run(
            row={
                "id": "derived:keboola",
                "source_type": "keboola",
                "stack_url": "https://connection.keboola.com",
                "token_env": "SOME_UNALLOWLISTED_NAME",
                "credentialed": True,
                "token_env_allowlisted": False,
            },
            fetch_ok=True,
            fetch_status=201,
            response_json={"id": "new-conn"},
        )
        assert result["body"] is None, "no request should have been sent"
        assert result["loadCalls"] == 0
        assert result["toasts"], "the admin must be told why nothing happened"
        assert result["toasts"][0][1] is False

    def test_a_successful_import_posts_the_seeding_flag_and_toasts_ok(self):
        result = self._run(
            row={
                "id": "derived:keboola",
                "source_type": "keboola",
                "stack_url": "https://connection.keboola.com",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
                "credentialed": True,
                "token_env_allowlisted": True,
            },
            fetch_ok=True,
            fetch_status=201,
            response_json={"id": "new-conn", "has_secret": True, "token_seeded": True},
        )
        assert result["url"] == "/api/admin/source-connections"
        assert result["body"] == {
            "name": "Keboola",
            "source_type": "keboola",
            "config": {"stack_url": "https://connection.keboola.com"},
            "token_env": "KEBOOLA_STORAGE_TOKEN",
            "seed_from_instance_credentials": True,
        }
        assert result["toasts"] == [["Keboola imported as a managed connection.", True]]
        assert result["loadCalls"] == 1
        # The stale derived entry is dropped before the reload.
        assert result["derivedSourcesLength"] == 0

    def test_a_seed_failure_still_creates_the_connection_but_toasts_honestly(self):
        """The server created the row but could not verify the vault-sourced
        token (e.g. it was stale). Success-looking silence here is exactly
        what the review flagged: the admin would only find out on the next
        failed sync."""
        result = self._run(
            row={
                "id": "derived:keboola",
                "source_type": "keboola",
                "stack_url": "https://connection.keboola.com",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
                "credentialed": True,
                "token_env_allowlisted": True,
            },
            fetch_ok=True,
            fetch_status=201,
            response_json={
                "id": "new-conn",
                "has_secret": False,
                "token_seeded": False,
                "token_seed_error": "storage_api_error: token invalid",
            },
        )
        assert result["toasts"] == [
            [
                "Connection created, but the stored credential couldn't be verified — "
                "use Rotate to add a working token.",
                False,
            ]
        ]
        # The connection still exists server-side, so the list still refreshes.
        assert result["loadCalls"] == 1

    def test_a_non_ok_response_reports_the_failure_and_never_refreshes(self):
        result = self._run(
            row={
                "id": "derived:keboola",
                "source_type": "keboola",
                "stack_url": "https://connection.keboola.com",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
                "credentialed": True,
                "token_env_allowlisted": True,
            },
            fetch_ok=False,
            fetch_status=409,
            response_json={"detail": "connection_name_exists"},
        )
        assert result["toasts"] == [["Import failed: connection_name_exists", False]]
        assert result["loadCalls"] == 0


class TestKeboolaBulkPickerRenameSuggestion:
    """Bug: bulk Keboola registration 422s on hyphenated names (Shopify
    exports especially) with no way to fix them in the picker — the
    rejection itself is correct, tested, intentional server policy
    (test_register_table_rejects_hyphen_in_name); the gap is the UI giving no
    way to retype before submitting."""

    @staticmethod
    def _template_text() -> str:
        from pathlib import Path

        tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
        return tpl.read_text(encoding="utf-8")

    def test_picker_renders_an_editable_input_only_for_names_that_would_fail(self):
        tpl = self._template_text()
        assert "const needsRename = !_wouldPassRegisterCheck(t.name);" in tpl
        assert "ds-table-name-input" in tpl
        assert "_suggestTableName(t.name)" in tpl

    def test_register_payload_and_bookkeeping_use_the_effective_name(self):
        tpl = self._template_text()
        fn = tpl.split("async function registerSelected(connId, errElId) {", 1)[1].split("\n/* ── Wizard:", 1)[0]
        assert 'const nameInput = rowEl.querySelector(".ds-table-name-input");' in fn
        assert (
            "const effectiveName = nameInput ? (nameInput.value.trim() || cb.dataset.tableName) : cb.dataset.tableName;"
            in fn
        )
        assert "name: effectiveName," in fn
        # Both bookkeeping call sites (success + already-registered 409) key
        # off the effective name, not the raw Keboola name.
        assert fn.count('id: effectiveName.trim().toLowerCase().replace(/ /g, "_"),') == 2
        assert fn.count("name: effectiveName,\n") >= 2
        # The regression: the raw name sent straight through.
        assert "name: cb.dataset.tableName," not in fn

    def test_source_table_still_targets_the_real_keboola_table(self):
        """A renamed registry `name` must never change what the sync path
        reads FROM Keboola — `bucket` / `source_table` stay pinned to the
        checkbox's own `data-bucket` / `data-table-bare`."""
        tpl = self._template_text()
        fn = tpl.split("async function registerSelected(connId, errElId) {", 1)[1].split("\n/* ── Wizard:", 1)[0]
        assert "bucket: cb.dataset.bucket," in fn
        assert "source_table: cb.dataset.tableBare || cb.dataset.tableName," in fn

    def test_sanitizer_matches_the_server_and_suggests_a_valid_identifier(self):
        """Executed for real via node (not just string-matched against the
        template), so a future edit that silently changes the mirror's
        behavior fails here rather than only in production."""
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = self._template_text()

        def _extract(fn_name: str) -> str:
            start = tpl.index(f"function {fn_name}(")
            end = tpl.index("\n}\n", start) + len("\n}\n")
            return tpl[start:end]

        script = (
            _extract("_wouldPassRegisterCheck")
            + "\n"
            + _extract("_suggestTableName")
            + """
const assert = require("assert");
// Mirrors register_table's own accepted/rejected cases.
assert.strictEqual(_wouldPassRegisterCheck("inventory-items"), false);
assert.strictEqual(_wouldPassRegisterCheck("crm-contact"), false);
assert.strictEqual(_wouldPassRegisterCheck("orders"), true);
assert.strictEqual(_wouldPassRegisterCheck("Order Line"), true);
// The picker's suggestion for the exact names from the bug report.
assert.strictEqual(_suggestTableName("inventory-items"), "inventory_items");
assert.strictEqual(_suggestTableName("inventory-levels"), "inventory_levels");
assert.strictEqual(_suggestTableName("line-item"), "line_item");
assert.strictEqual(_suggestTableName("product-images"), "product_images");
// A digit-leading name must not suggest an identifier that itself starts
// with a digit — the server's check requires a leading letter/underscore.
assert.strictEqual(_suggestTableName("2024-orders"), "_2024_orders");
assert.strictEqual(_wouldPassRegisterCheck("2024-orders"), false);
assert.strictEqual(_wouldPassRegisterCheck(_suggestTableName("2024-orders")), true);
console.log("OK");
"""
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            proc = subprocess.run(["node", path], capture_output=True, text=True)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode == 127:
            import pytest

            pytest.skip("node unavailable")
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestSourceCardHierarchy:
    """The card ranks its contents instead of stacking five equal bands.

    Head (identity + one status word + the verbs) → strip → a settings body
    that is CLOSED. These pin the two properties that make that safe: the
    status word is a fold over the strip rather than a sixth fact, and every
    control the old action strip carried is still reachable.
    """

    def test_the_status_word_folds_the_strip(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "_sourceHealth" in body
        for state in ("Healthy", "No tables yet", "Reaches nobody", "failing sync"):
            assert state in body
        # An absent semantic-layer token must NOT reach the card-level verdict:
        # it is opt-in, and an off feature is not a broken source.
        health = body[body.index("function _sourceHealth") : body.index("function _sourceSubtitle")]
        assert "semantic" not in health

    def test_the_body_is_closed_and_the_caret_says_so(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert 'class="ds-src__body" id="ds-body-${id}" hidden' in body
        assert 'aria-expanded="false" aria-controls="ds-body-${id}"' in body
        # A verb reached from the menu opens the body it writes into —
        # otherwise the menu item appears to do nothing.
        assert body.count("setSourceOpen(id, true)") >= 3

    def test_an_empty_source_offers_the_verb_not_a_dead_link(self, seeded_app):
        """A source with nothing registered must not point at /admin/tables —
        the list of tables that already exist is the one page that cannot fix
        an empty source. The cell becomes the action instead, which is also
        where the card's primary verb stays one click away."""
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "Add the first tables" in body
        strip = body[body.index("function _pipelineStripHtml") : body.index("function _sourceHealth")]
        # A stored connection browses its own tables; a derived card has none
        # to browse, so it opens the wizard already on its connector.
        assert "toggleBrowse(" in strip
        assert "openWizard(" in strip

    def test_no_control_was_dropped_with_the_action_strip(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        for fn in (
            "testConn",
            "toggleBrowse",
            "toggleRotate",
            "toggleMasterToken",
            "toggleChatTools",
            "setDefaultConn",
            "unbindProject",
            "deleteConn",
        ):
            assert fn in body, f"{fn} lost its route into the UI"
        # Delete is separated and marked, never reachable by momentum from the
        # item above it.
        assert "apg-menu__item--danger" in body
        assert "apg-menu__sep" in body


class TestSnowflakeWizardCredentialNames:
    """The wizard's Snowflake pane stores credentials under an env-var name it
    reads back from `GET /api/admin/server-config` — which redacts exactly
    those keys (see
    `test_admin_server_config.py::test_get_redacts_the_snowflake_credential_env_NAMES`).
    Trusting that read put the `***` sentinel in the PUT path, so once
    Snowflake had been configured through the wizard no credential could be
    stored or rotated through it again."""

    def _template(self):
        from pathlib import Path

        import app.web.router as web_router

        return (Path(web_router.__file__).parent / "templates" / "admin_data_sources.html").read_text()

    def test_env_names_from_the_config_read_are_shape_checked(self):
        src = self._template()
        assert "_sfEnvNameOr(sf.token_env," in src
        assert "_sfEnvNameOr(sf.private_key_env," in src
        assert "_sfEnvNameOr(sf.private_key_passphrase_env," in src
        # The unguarded form is what shipped the bug — it must not come back.
        assert "sf.token_env || " not in src
        assert "sf.private_key_env || " not in src
        assert "sf.private_key_passphrase_env || " not in src

    def test_the_shape_check_rejects_the_redaction_sentinel(self):
        """`***` and `<empty>` are the two sentinels `_mask` produces; neither
        is a legal env-var name, so the guard's regex must reject both."""
        import re

        src = self._template()
        m = re.search(r"const _ENV_NAME_RE = /(.+?)/;", src)
        assert m, "the env-name shape guard is gone"
        pattern = re.compile(m.group(1))
        for sentinel in ("***", "<empty>", ""):
            assert not pattern.match(sentinel), f"{sentinel!r} must not pass as an env-var name"
        for legal in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY", "_x9"):
            assert pattern.match(legal), f"{legal!r} must pass as an env-var name"

    def test_the_save_never_writes_a_credential_env_name_back(self):
        """`token_env` / `private_key_env` / `private_key_passphrase_env` come
        back from the config read redacted, so the wizard cannot know which name
        is configured. Writing its fallback back would REPLACE an operator's
        custom name with the default and break every Snowflake query and sync.

        (Before the shape guard the wizard POSTed the `***` sentinel, which
        `_strip_redacted_sentinels` dropped server-side — a harmless no-op. A
        plausible-looking default is not, which is what makes this a write the
        wizard must not perform at all.)

        Nothing is lost: `resolve_snowflake_settings` defaults each name to
        exactly what the wizard stores the credential under."""
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "sf.token_env" not in save
        assert "sf.private_key_env" not in save
        assert "sf.private_key_passphrase_env" not in save

    def test_the_save_omits_a_blank_role_rather_than_clearing_it(self):
        """`POST /api/admin/server-config` deep-merges per leaf, so a
        present-but-empty `role` overwrites a stored one — and the prefill is
        empty whenever the config read failed."""
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "if (role) sf.role = role;" in save
        assert "warehouse, role," not in save

    def test_the_stored_under_note_fires_on_a_save_not_on_every_page_open(self):
        """`token_env` is redacted on every instance that has ever configured
        Snowflake, so a note keyed on "the name was unreadable" alone would fire
        for the majority that use the default names — noise that trains
        operators to ignore it. It is keyed on a credential having actually been
        stored, and says which name it went under."""
        src = self._template()
        render = src[src.index("function _renderSfCredStatus") : src.index("async function _saveSnowflakeAndContinue")]
        assert "_storedUnderNote" not in render, "the note is back on the badge, where it fires unconditionally"
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert save.count("storedUnder.push(") == 3, "a stored credential is not recorded for every kind"
        banner = src[src.index("function _renderSfRowsEditor") :]
        # Renamed from `_sfStoredUnderNote` when the Databricks pane started
        # sharing it — the text was never Snowflake-specific.
        assert "_storedUnderNote(_sfStoredUnder)" in banner
        assert "_sfStoredUnder = [];" in src[src.index("function openWizard") :]

    def test_a_server_config_save_surfaces_restart_required(self):
        """`POST /api/admin/server-config` answers `restart_required: true` and
        resets only the in-process config cache. This wizard is the first
        server-config writer outside /admin/server-config, so dropping that
        field left a role-split deployment registering tables against a config
        the scheduler had not re-read, with nothing saying so."""
        src = self._template()
        save = src.index("_saveSnowflakeAndContinue")
        assert "_sfRestartRequired = !!savedCfg.restart_required;" in src[save:]
        # ...and it has to reach the operator, not just a variable.
        assert "_sfRestartRequired" in src[src.index("_renderSfRowsEditor") :]
        # Reset per wizard open, so a later source cannot inherit the note.
        assert "_sfRestartRequired = false;" in src[src.index("function openWizard") :]

    def test_a_saved_credential_box_is_cleared_before_the_badge_is_redrawn(self):
        """`_renderSfCredStatus` reads "ready to save" off a non-empty input,
        so redrawing before clearing reports a stored credential as pending."""
        src = self._template()
        for input_id, kind in (("ds-sf-password", "password"), ("ds-sf-private-key", "key")):
            clear = src.index(
                f'document.getElementById("{input_id}").value = "";', src.index("_saveSnowflakeAndContinue")
            )
            render = src.index(f'_renderSfCredStatus(null, "{kind}");', src.index("_saveSnowflakeAndContinue"))
            assert clear < render, f"{input_id} is cleared after the badge is redrawn"


class TestDatabricksWizardCredentialAndRestartNotice:
    """The Databricks pane is the newest arrival in the Add-data wizard and
    repeated three defects the Snowflake pane had already been fixed for: an
    unstyled credential-status row, a `restart_required` flag thrown away, and
    a credential written under a hardcoded name the backend may not read.

    Source-level assertions, like the Snowflake class above: this is inline
    template JS with no module boundary to import."""

    def _template(self):
        from pathlib import Path

        import app.web.router as web_router

        return (Path(web_router.__file__).parent / "templates" / "admin_data_sources.html").read_text()

    def test_the_credential_badge_row_is_styled_like_its_siblings(self):
        """`.ds-dbxcred` was on the div and in no stylesheet rule, so the badge
        and its warning text stacked instead of aligning and the row collapsed
        to zero height while the async status load was in flight."""
        src = self._template()
        rule = next((ln for ln in src.splitlines() if ".ds-bqcred" in ln and "display: flex" in ln), "")
        assert rule, "the credential-status row rule is gone"
        assert ".ds-dbxcred" in rule, "the Databricks badge row is not styled like the BigQuery/Snowflake rows"

    def test_the_credential_is_read_and_written_under_the_configured_name(self):
        """`resolve_databricks_settings` reads the env var named by
        `data_source.databricks.token_env`, so a wizard that always writes
        `DATABRICKS_TOKEN` can report a stored credential the backend never
        looks at. Both the status lookup and the PUT go through the resolved
        name now, and the name is shape-guarded because the config read redacts
        it."""
        src = self._template()
        assert "_envNameOr(dbx.token_env, _DBX_TOKEN_ENV_DEFAULT)" in src
        assert "s.name === _dbxTokenEnv" in src
        assert "datasource-secrets/${encodeURIComponent(_dbxTokenEnv)}" in src
        # The hardcoded constant must not be the thing either path reaches for.
        assert "s.name === _DBX_TOKEN_ENV_DEFAULT" not in src
        assert "encodeURIComponent(_DBX_TOKEN_ENV_DEFAULT)" not in src

    def test_the_env_name_guard_is_one_helper_for_both_connectors(self):
        """The redaction is a property of `_is_secret_key`, not of a connector —
        two copies of the same shape check would drift."""
        src = self._template()
        assert "function _envNameOr(" in src
        assert src.count("const _ENV_NAME_RE") == 1
        # The Snowflake wrapper keeps its own redaction flag but delegates.
        sf = src[src.index("function _sfEnvNameOr(") : src.index("async function _loadSfConfigAndStatus")]
        assert "_envNameOr(fromConfig, fallback)" in sf

    def test_a_saved_connection_change_reports_that_a_restart_is_needed(self):
        """`POST /api/admin/server-config` always answers `restart_required`
        and only resets the calling process's config cache. The Snowflake path
        carries that onto its step-2 banner; the Databricks path has no step 2 —
        it closes the wizard and navigates — so an ignored flag means the
        operator is never told, and on a role-split deployment the scheduler
        keeps the old warehouse while freshly registered tables fail."""
        src = self._template()
        save = src[src.index("async function _saveDatabricksAndContinue") : src.index("function openWizard")]
        assert "savedCfg.restart_required" in save, "the restart signal is discarded again"
        # The unconditional navigate is what swallowed it.
        assert save.count('window.location.href = "/admin/tables"') == 1, (
            "the save must not navigate away when it has something to report"
        )
        assert "_dbxSaveDone = true" in save

    def test_the_held_open_save_can_still_reach_the_tables_page(self):
        """Holding the wizard open must not strand the operator: the same
        primary button navigates on the next click."""
        src = self._template()
        handler = src[src.index('if (_wizardSource === "databricks") {') :]
        handler = handler[: handler.index("connectAndValidate();")]
        assert "if (_dbxSaveDone) {" in handler
        assert 'window.location.href = "/admin/tables"' in handler


def test_register_error_text_is_not_html_escaped_before_textcontent(seeded_app):
    """`_registerErrorText` output goes to `textContent`, so it must not be
    `_esc`'d first.

    `textContent` escapes by assignment; running the string through `_esc`
    beforehand double-escapes it, so the upstream reason this surface exists to
    relay — `Catalog Error: Table with name X does not exist! Did you mean "Y"?`
    — reaches the operator as `Did you mean &quot;Y&quot;?`. Not an XSS risk
    (every one of the three sinks is `textContent`, never `innerHTML`), just a
    mangled message on the one line that matters. The single-row path already
    omits `_esc`; the two bulk-register paths did not.
    """
    c = seeded_app["client"]
    html = c.get("/admin/data-sources", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}).text
    assert "_esc(_registerErrorText(" not in html, (
        "a register-error string is HTML-escaped before being assigned to "
        "textContent — the operator sees &quot; entities instead of the quoted "
        "identifier the server suggested"
    )
    assert "_registerErrorText(" in html, "guard has nothing to check — helper is gone"
