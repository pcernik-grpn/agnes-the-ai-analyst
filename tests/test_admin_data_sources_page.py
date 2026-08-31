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
        assert "master (owner) token" in body
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


class TestMasterTokenCardTooltip:
    """A12 (#1707): the card's "Semantic-layer token" fact used a native
    `title=` — a 600ms+ OS-controlled show delay, invisible on touch. It must
    use the shared `[data-tip]` fast-tooltip mechanism instead: `data-tip` and
    `aria-label` carrying the same text, never `title` alongside it."""

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
        """Brace-matched extraction — a naive index-slice to the next known
        function name would false-fire on an unrelated `title=` introduced
        anywhere in between by a later, unrelated edit."""
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

    def _fact_fn(self, seeded_app) -> str:
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        return self._extract_function(body, "function _masterTokenFactHtml(row) {")

    def test_uses_data_tip_and_aria_label_not_title(self, seeded_app):
        fn = self._fact_fn(seeded_app)
        assert "data-tip=" in fn
        assert "aria-label=" in fn
        assert "title=" not in fn
        tip = fn.split('data-tip="', 1)[1].split('"', 1)[0]
        label = fn.split('aria-label="', 1)[1].split('"', 1)[0]
        # The accessible name must PREFIX the visible label with the tip,
        # never replace it outright (WCAG 2.5.3 Label in Name) — the
        # library.html:83 precedent this was modeled on does the same.
        assert label.startswith("Semantic-layer token")
        assert label.endswith(tip)
        assert "master" in tip.lower()
        assert len(tip) < 160

    def test_a_derived_card_has_no_master_token_widget(self, seeded_app):
        """`_masterTokenFactHtml` is never called for a derived row (no
        stored connection to hold the secret in) — proving this here is what
        makes the pipeline-strip fallback to the plain link, rather than
        `toggleMasterToken`, for a derived card the only safe choice."""
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        card = self._extract_function(body, "function _connectionCardHtml(row) {")
        derived_branch = card[: card.index("const c = _connector(row.source_type);")]
        assert "_masterTokenFactHtml" not in derived_branch


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

    def test_wizard_master_token_field_names_the_keboola_noun(self, seeded_app):
        """A12 (#1707): "project owner" is Agnes's own phrasing, not
        Keboola's — an admin searching their Keboola project for "project
        owner token" finds nothing, because Keboola's own UI calls it the
        project MASTER token. The wizard field must say "master" at the
        point of entry, not only on the connection card two steps later."""
        body = self._page(seeded_app)
        field = body[body.index('id="ds-new-master"') : body.index('id="ds-new-master"') + 600]
        assert "master" in field.lower()

    def test_the_semantic_opt_ins_link_the_source_to_the_connection(self, seeded_app):
        """`config: {}` is what the created semantic source carries — the
        adapters resolve their own credentials — but it must carry the
        `connection_id` link, or the cross-domain coverage report credits the
        source to nobody and scores a working semantic layer as missing
        (src/semantic/coverage.py::_native_semantic_status)."""
        body = self._page(seeded_app)
        assert "async function _connectSemanticSource(adapter, label, connectionId)" in body
        assert "connection_id: connectionId" in body
        # Both opt-ins pass the row the wizard just saved.
        assert '_connectSemanticSource("snowflake_semantic", "Snowflake semantics", _sfConnId)' in body
        assert '_connectSemanticSource("databricks_metric_views", "Databricks semantics", _dbxConnId)' in body

    def test_a_failed_databricks_semantic_optin_is_reported_not_swallowed(self, seeded_app):
        """The Databricks branch closes the wizard and navigates away, so a
        discarded failure left a checked box, no error and no semantic layer
        indistinguishable from success. On failure it stays put and says so."""
        body = self._page(seeded_app)
        assert "Databricks connection saved." in body
        assert "Continue to Tables" in body

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


class TestSemanticLayerCellNoTokenAction:
    """A13 (#1707): "Token not set" used to link to /admin/semantic-layer —
    the page renders fine, but has no token field; the token is set on the
    card itself. The actual fix — Actions → Semantic-layer token — lives on
    the same card, so a STORED connection's not-set cell must call
    `toggleMasterToken` directly instead of navigating to a page that cannot
    help. Once a token IS set, the cell keeps linking to the health page —
    and so does a DERIVED card's cell regardless of token state, since a
    derived row (no stored connection) carries no `toggleMasterToken` widget
    at all (`_masterTokenFactHtml` is never rendered for it — see
    `TestMasterTokenCardTooltip.test_a_derived_card_has_no_master_token_widget`);
    calling it there would dereference a DOM element that does not exist.
    Executed for real via `node` against a seeded `SOURCE_PIPELINES` fixture
    (same pattern as `TestSharePointSourceCardRendering`)."""

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
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

    def _run(self, *, token_set: bool, derived: bool = False) -> str:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fn = self._extract_function(tpl, "function _pipelineStripHtml(row) {")
        pipeline = {
            "tables": {"count": 5},
            "sync": {},
            "semantic": {"token": token_set, "metrics": 2 if token_set else 0, "terms": 3 if token_set else 0},
            "feeds": {"packages": 1, "people": 3},
        }
        row = {"id": "kbc-conn-1", "source_type": "keboola", "derived": derived}
        script = f"""
function _esc(s) {{ return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
const SOURCE_PIPELINES = {{ "kbc-conn-1": {json.dumps(pipeline)} }};

{fn}

console.log(_pipelineStripHtml({json.dumps(row)}));
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
        return proc.stdout

    def test_no_token_cell_calls_toggle_master_token_not_the_health_page(self):
        html = self._run(token_set=False)
        assert "Token not set" in html
        assert "toggleMasterToken('kbc-conn-1')" in html
        assert 'href="/admin/semantic-layer"' not in html

    def test_token_set_cell_keeps_the_health_page_link(self):
        html = self._run(token_set=True)
        assert 'href="/admin/semantic-layer"' in html
        assert "toggleMasterToken" not in html

    def test_derived_no_token_cell_keeps_the_base_link_not_toggle_master_token(self):
        """The blocking regression this guards: a derived card has no
        `ds-master-row-*` element for `toggleMasterToken` to find, so wiring
        it there would `TypeError` on click (`row.classList` on `null`) the
        moment the pipeline strip renders for the common case of an
        instance-credentialed Keboola source with no managed connection yet."""
        html = self._run(token_set=False, derived=True)
        assert "Token not set" in html
        assert "toggleMasterToken" not in html
        assert 'href="/admin/semantic-layer"' in html


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
let refreshCalls = 0;
async function refreshSourcePipelines() {{ refreshCalls++; return true; }}
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
    refreshCalls,
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
        # The new connection has no entry in the strip snapshot this page was
        # rendered from, so its card would draw with no pipeline strip at all
        # until a reload.
        assert result["refreshCalls"] == 1
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
    """D2.3: the wizard's Snowflake pane saves the connection (account/user/
    database/warehouse/role/auth_type) onto the SF `source_connections` row
    (`PUT`/`POST /api/admin/source-connections*`), not the
    `data_source.snowflake` server-config yaml overlay — a row is read live
    by every process, so there is no cross-process staleness left to warn
    about, and the credential is stored in the row's own vault slot by
    connection id rather than under a redaction-prone env-var name read back
    from `GET /api/admin/server-config`."""

    def _template(self):
        from pathlib import Path

        import app.web.router as web_router

        return (Path(web_router.__file__).parent / "templates" / "admin_data_sources.html").read_text()

    def test_the_save_writes_the_connection_row_not_the_yaml_overlay(self):
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "API_CONNECTIONS" in save
        assert "sections: { data_source:" not in save, "must not write the yaml overlay any more"
        assert "API_SERVER_CONFIG" not in save

    def test_the_save_updates_the_existing_row_when_the_wizard_found_one(self):
        """One connection per source_type in this slice — the wizard must not
        create a second snowflake row if it already loaded one."""
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "_sfConnId" in save
        assert 'method: "PUT"' in save or "method: 'PUT'" in save

    def test_the_credential_is_stored_on_the_connection_not_a_named_env_var(self):
        """The password / private key both go through the connection's own
        vault slot by id. The key-pair passphrase is the one exception —
        the row has no second vault slot for it, so it keeps going through
        the generic named-secret vault under its well-known default name
        (`_SF_PASSPHRASE_ENV_DEFAULT`), the same fallback
        `connectors.snowflake.settings._resolve_secret` reads."""
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "/secret" in save
        assert 'kind: "storage"' in save or "kind: 'storage'" in save
        assert save.count("datasource-secrets") == 1, "only the passphrase should use the named-secret vault"
        assert "_SF_PASSPHRASE_ENV_DEFAULT" in save

    def test_role_is_always_sent_since_put_replaces_the_whole_config(self):
        """Unlike the old yaml-overlay POST (which deep-merged per leaf, so a
        present-but-empty `role` would clobber a stored one), `PUT .../
        {id}` replaces `config` wholesale — an empty role is simply the
        unset value, so it can always be included."""
        src = self._template()
        save = src[src.index("async function _saveSnowflakeAndContinue") : src.index("function openWizard")]
        assert "auth_type: authType" in save
        assert "role" in save

    def test_no_restart_warning_copy_remains(self):
        """D2.3's headline: a row is read live by every process, so there is
        nothing left to restart for."""
        src = self._template()
        assert "_sfRestartRequired" not in src
        assert "Restart the instance" not in src[src.index("_saveSnowflakeAndContinue") :][:4000]

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
    """D2.3: like the Snowflake pane above, the Databricks pane saves the
    connection (host/warehouse_id/catalog) onto the DBX `source_connections`
    row, not the `data_source.databricks` server-config yaml overlay, and
    stores the token in the row's own vault slot by connection id — a row is
    read live by every process, so the old `restart_required` notice (and
    the two-click "hold the wizard open" flow it justified) is gone: the
    wizard is a straight line to /admin/tables again.

    Source-level assertions: this is inline template JS with no module
    boundary to import."""

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

    def test_the_save_writes_the_connection_row_not_the_yaml_overlay(self):
        src = self._template()
        save = src[src.index("async function _saveDatabricksAndContinue") : src.index("function openWizard")]
        assert "API_CONNECTIONS" in save
        assert "sections: { data_source:" not in save, "must not write the yaml overlay any more"
        assert "API_SERVER_CONFIG" not in save

    def test_the_save_updates_the_existing_row_when_the_wizard_found_one(self):
        src = self._template()
        save = src[src.index("async function _saveDatabricksAndContinue") : src.index("function openWizard")]
        assert "_dbxConnId" in save
        assert 'method: "PUT"' in save or "method: 'PUT'" in save

    def test_the_credential_is_stored_on_the_connection_not_a_named_env_var(self):
        src = self._template()
        save = src[src.index("async function _saveDatabricksAndContinue") : src.index("function openWizard")]
        assert "/secret" in save
        assert 'kind: "storage"' in save or "kind: 'storage'" in save
        assert "datasource-secrets" not in save

    def test_no_restart_notice_and_the_wizard_is_a_straight_line(self):
        """D2.3's headline: a row is read live by every process, so the save
        navigates straight to /admin/tables — no held-open second click."""
        src = self._template()
        assert "_dbxSaveDone" not in src
        assert "restart_required" not in src
        save = src[src.index("async function _saveDatabricksAndContinue") : src.index("function openWizard")]
        assert save.count('window.location.href = "/admin/tables"') == 1

    def test_the_databricks_branch_of_the_connect_button_just_saves(self):
        src = self._template()
        handler = src[src.index('if (_wizardSource === "databricks") {') :]
        handler = handler[: handler.index("connectAndValidate();")]
        assert "_saveDatabricksAndContinue();" in handler
        assert "_dbxSaveDone" not in handler


class TestSharePointSourceCard:
    """The file-source source card (`_sharepoint_pipeline_cell`, spec §13.2
    "Source card") — DuckDB-side coverage: graceful degrade when
    `facts_ingest_runs_repo()`/`facts_repo()` raise `RequiresPostgresBackend`
    (A3 ratchet — every fact-graph repo is PG-only), the certificate row
    (which needs no Postgres at all), and the "no connection yet" state.
    Counts/badges that genuinely need a live Postgres backend are covered in
    `tests/db_pg/test_facts_source_card_pg.py`.
    """

    def test_no_sharepoint_connection_means_no_file_source_cell(self, seeded_app):
        """Zero new navigation, zero placeholder card — a source type with
        no connection row simply contributes nothing extra, same as every
        other connector that needs credentials it does not have."""
        from app.web.router import _source_inventory

        inv = _source_inventory()
        for cells in inv["pipelines"].values():
            assert "file_source" not in cells

    def test_sharepoint_connection_degrades_gracefully_without_postgres(self, seeded_app):
        """No Postgres backend active -> every fact-graph repo raises
        `RequiresPostgresBackend` -> the cell still renders, with the
        PG-dependent numbers at their honest zero/empty rather than a 500
        for the whole page."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={"tenant_id": "t1", "client_id": "c1"},
        )
        try:
            inv = _source_inventory(user={"id": "admin1", "email": "admin@test.com"})
            fs = inv["pipelines"][conn_id]["file_source"]
            assert fs["crawl"]["documents"] == 0
            assert fs["extract"] == {}
            assert fs["graph"] == {"facts": 0, "edges": 0}
            assert fs["last_run"] is None
            assert fs["cost_estimate"] == {"amount_usd": 0.0, "placeholder": True}
            assert fs["identity"] == {"groups_matched": 0, "collections_no_group": 0, "collections_total": 0}
            # `scopes` needs no Postgres at all (config.scopes + the dual-
            # backend file_corpora/resource_grants repos) — an empty
            # connection still yields an empty (not missing) list.
            assert fs["scopes"] == []
        finally:
            source_connections_repo().delete(conn_id)

    def test_scopes_resolve_collection_name_and_no_group_warning_without_postgres(self, seeded_app):
        """`scopes` reuses `admin_sharepoint._scope_out`, which only touches
        the dual-backend `file_corpora`/`resource_grants` repos — no
        Postgres required, unlike the rest of this cell."""
        import uuid

        from src.repositories import file_corpora_repo, source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        slug = f"col-{uuid.uuid4().hex[:8]}"
        collection_id = file_corpora_repo().create(name="Contracts", slug=slug, description=None, created_by="admin1")
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={
                "tenant_id": "t1",
                "client_id": "c1",
                "scopes": [
                    {
                        "source_scope_id": "site1-drive1",
                        "display_path": "Site / Contracts",
                        "anonymize": False,
                        "collection_id": collection_id,
                    }
                ],
            },
        )
        try:
            from app.web.router import _source_inventory

            fs = _source_inventory()["pipelines"][conn_id]["file_source"]
            assert len(fs["scopes"]) == 1
            row = fs["scopes"][0]
            assert row["source_scope_id"] == "site1-drive1"
            assert row["display_path"] == "Site / Contracts"
            assert row["collection"]["name"] == "Contracts"
            # No group ever granted on this collection -> the warning fires.
            assert row["no_group_warning"] is True
        finally:
            source_connections_repo().delete(conn_id)
            file_corpora_repo().soft_delete(collection_id)

    def test_schedule_row_is_static_and_honest(self, seeded_app):
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={"tenant_id": "t1", "client_id": "c1"},
        )
        try:
            inv = _source_inventory()
            schedule = inv["pipelines"][conn_id]["file_source"]["schedule"]
            assert schedule["text"] == "external producer · hourly delta"
            # TCRD-226's in-Agnes schedule state is a SEPARATE, additive
            # sub-object — a fresh connection with no scheduled runs reads
            # honestly off (never a stale/guessed default).
            assert schedule["in_agnes"] == {
                "enabled": False,
                "schedule": None,
                "last_run_at": None,
                "next_run_at": None,
            }
        finally:
            source_connections_repo().delete(conn_id)

    def test_in_agnes_schedule_reflects_live_config_and_dispatch_state(self, seeded_app, monkeypatch):
        """TCRD-226: enabling extraction + configuring a cadence + a
        recorded dispatch on the connection's own config all show up in the
        card's `schedule.in_agnes` sub-object — the same state the sweep
        (`POST .../extraction/run-due`) and the manual trigger
        (`POST .../extract`) read and write."""
        import uuid
        from datetime import datetime, timezone

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "true")

        def _fake_get_value(*keys, default=None):
            if keys == ("extraction", "schedule"):
                return "every 4h"
            return default

        monkeypatch.setattr("app.instance_config.get_value", _fake_get_value)

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        last_run_at = datetime(2026, 8, 29, 8, 0, 0, tzinfo=timezone.utc).isoformat()
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={
                "tenant_id": "t1",
                "client_id": "c1",
                "extraction": {"last_run_at": last_run_at, "last_job_id": "job-1"},
            },
        )
        try:
            inv = _source_inventory()
            in_agnes = inv["pipelines"][conn_id]["file_source"]["schedule"]["in_agnes"]
            assert in_agnes["enabled"] is True
            assert in_agnes["schedule"] == "every 4h"
            assert in_agnes["last_run_at"] == last_run_at
            # next_due_at(every 4h, last_run_at) == last_run_at + 4h.
            assert in_agnes["next_run_at"] == "2026-08-29T12:00:00+00:00"
        finally:
            source_connections_repo().delete(conn_id)

    def test_certificate_row_reports_a_resolution_error_without_crashing(self, seeded_app):
        """A misconfigured connection (no tenant_id/client_id) must not take
        the whole card down — the certificate row carries the error text
        instead, same "status row, not a gate" posture every other cell on
        this page follows."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={},
        )
        try:
            inv = _source_inventory()
            cert = inv["pipelines"][conn_id]["file_source"]["certificate"]
            assert cert["origin"] is None
            assert cert["error"] is not None
            assert "tenant_id" in cert["error"]
        finally:
            source_connections_repo().delete(conn_id)

    def test_a_vault_certificate_reports_its_set_date_not_an_error(self, seeded_app, monkeypatch):
        """The vault leg had no end-to-end cover here, and that is where it
        broke: the repos hand back ``str(row[0])`` while this cell calls
        ``.isoformat()``, so the AttributeError landed in the block's own
        ``except`` and a working certificate rendered as unconfigured with a
        stray Python error in its place.
        """
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Vault SharePoint",
            source_type="sharepoint",
            config={"tenant_id": "t1", "client_id": "c1"},
        )

        class _VaultRepo:
            def get(self, connection_id):
                return "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----"

            def updated_at(self, connection_id):
                return "2026-08-20 12:00:00"

        monkeypatch.setattr("src.repositories.connection_secrets_repo", lambda: _VaultRepo())
        try:
            cert = _source_inventory()["pipelines"][conn_id]["file_source"]["certificate"]
            assert cert["error"] is None, f"a resolvable vault credential must not report an error: {cert}"
            assert cert["origin"] == "vault"
            assert cert["set_at"] and cert["set_at"].startswith("2026-08-20T12:00:00")
            assert "-----BEGIN PRIVATE KEY-----" not in str(cert)
        finally:
            source_connections_repo().delete(conn_id)

    def test_certificate_row_never_carries_the_value(self, seeded_app, monkeypatch):
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={"tenant_id": "t1", "client_id": "c1", "cert_private_key_env": "SHAREPOINT_CERT_PRIVATE_KEY"},
        )
        monkeypatch.setenv(
            "SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----"
        )
        try:
            inv = _source_inventory()
            cert = inv["pipelines"][conn_id]["file_source"]["certificate"]
            assert cert["origin"] == "env"
            assert cert["env_name"] == "SHAREPOINT_CERT_PRIVATE_KEY"
            assert "-----BEGIN PRIVATE KEY-----" not in str(cert)
            # No CERTIFICATE PEM block in this stored value -> a clean typed
            # absence, not a missing key / a crash.
            assert cert["metadata_reason"] == "no_certificate_configured"
            assert "thumbprint_x5t" not in cert
        finally:
            source_connections_repo().delete(conn_id)

    def test_certificate_metadata_flows_through_from_a_real_certificate(self, seeded_app, monkeypatch):
        """The thumbprint/subject/expiry `certificate_metadata` derives are
        merged into the same cell the settings-resolution fields already
        populate — one certificate row, not two competing sources of truth."""
        import datetime
        import uuid

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agnes-test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=90))
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        pem = cert_pem + key_pem

        conn_id = f"sp-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Corp SharePoint",
            source_type="sharepoint",
            config={"tenant_id": "t1", "client_id": "c1", "cert_private_key_env": "SHAREPOINT_CERT_PRIVATE_KEY"},
        )
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", pem)
        try:
            inv = _source_inventory()
            cert_cell = inv["pipelines"][conn_id]["file_source"]["certificate"]
            assert cert_cell["origin"] == "env"  # settings-resolution field, unaffected
            assert cert_cell["subject"] == "CN=agnes-test"
            assert cert_cell["issuer"] == "CN=agnes-test"
            assert cert_cell["status"] == "ok"
            assert cert_cell["thumbprint_x5t"]
            assert "-----BEGIN PRIVATE KEY-----" not in str(cert_cell)
        finally:
            source_connections_repo().delete(conn_id)


class TestSharePointSourceCardRendering:
    """`_sharepointPipelineStripHtml` / `_sharepointFactsHtml` /
    `toggleFileSourceDrawer` executed for real via `node` against a seeded
    `SOURCE_PIPELINES` fixture — proves the RENDERED markup (counts, badge
    counts, drawer content), not just the server-side data shape
    `TestSharePointSourceCard` above pins. Same pattern as
    `TestImportKeboolaConnectionBehavior`.
    """

    _FILE_SOURCE = {
        "crawl": {"documents": 12},
        "extract": {"indexed": 9, "processing": 2, "needs_review": 1},
        "graph": {"facts": 7, "edges": 3},
        "cost_estimate": {"amount_usd": 0.06, "placeholder": True},
        "schedule": {"text": "external producer · hourly delta"},
        "certificate": {"origin": "vault", "env_name": None, "set_at": "2026-08-20T12:00:00+00:00", "error": None},
        "identity": {"groups_matched": 2, "collections_no_group": 1, "collections_total": 3},
        "last_run": {
            "id": "ir_abc123",
            "created_at": "2026-08-27T10:00:00+00:00",
            "rejected_quotes": [{"row": 0, "reason": "verbatim_gate_failed", "doc_id": "doc-a"}],
            "deferred": [{"row": 1, "doc_id": "doc-b"}],
            "protocol_errors": [
                {"row": 2, "reason": "unresolved_doc_id", "doc_id": "doc-c"},
                {"row": 3, "reason": "malformed_edge"},
            ],
            # O7 follow-up: a dropped documents[].source_url — the claim
            # itself still wrote, only its citation link is missing.
            "source_urls_rejected": [{"doc_id": "doc-d", "reason": "not_https"}],
        },
    }

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
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

    def _run(self, body: str, *, file_source=None) -> dict:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig)
            for sig in (
                "function _esc(s) {",
                "function _sharepointPipelineStripHtml(row) {",
                "function _sharepointFactsHtml(row) {",
                "const SP_REJECTION_REASON_TEXT = {",
                "function _spRejectionReasonText(reason) {",
                "function _spGroupRejectionRows(rows) {",
                "function _spRejectionRowHtml(r) {",
                "function toggleFileSourceDrawer(connId, category) {",
            )
        )
        fs = file_source if file_source is not None else self._FILE_SOURCE
        script = f"""
{fns}

const SOURCE_PIPELINES = {{ "sp-conn-1": {{ file_source: {json.dumps(fs)} }} }};
const _elements = {{ "ds-fs-drawer-sp-conn-1": {{ dataset: {{}}, hidden: true, innerHTML: "" }} }};
const document = {{ getElementById: (id) => _elements[id] }};
const row = {{ id: "sp-conn-1", source_type: "sharepoint" }};

{body}
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

    def test_pipeline_strip_renders_the_four_stage_counts(self):
        result = self._run("console.log(JSON.stringify({ html: _sharepointPipelineStripHtml(row) }));")
        html = result["html"]
        assert "12 documents" in html
        assert "9 indexed" in html
        assert "7 facts · 3 edges" in html
        assert "~$0.06" in html

    def test_facts_html_renders_certificate_identity_and_badge_counts(self):
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));")
        html = result["html"]
        assert "external producer · hourly delta" in html
        assert "vault" in html
        # Never the certificate value, only origin/set-date.
        assert "BEGIN PRIVATE KEY" not in html
        # "Sharing" row — rephrased from the cryptic "2 groups matched · 1
        # collection with no group" to a plain sentence about what it means.
        assert "1 collection has no group — only admins see them" in html
        assert "Rejected quotes 1" in html
        assert "Deferred 1" in html
        assert "Protocol errors 2" in html
        assert "Citation links rejected 1" in html

    # -- in-Agnes extraction scheduling + manual trigger (TCRD-226) --------

    def test_run_extraction_now_button_always_renders_and_is_wired(self):
        """The action is available regardless of whether in-Agnes
        scheduling is configured — the fixture above carries no
        `schedule.in_agnes` at all, and the button must still render and
        call the SAME endpoint."""
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));")
        html = result["html"]
        assert "Run extraction now" in html
        assert "runSpExtraction('sp-conn-1')" in html

    def test_in_agnes_schedule_renders_last_and_next_run(self):
        fs = dict(self._FILE_SOURCE)
        fs["schedule"] = {
            **fs["schedule"],
            "in_agnes": {
                "enabled": True,
                "schedule": "every 4h",
                "last_run_at": "2026-08-29T08:00:00+00:00",
                "next_run_at": "2026-08-29T12:00:00+00:00",
            },
        }
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        # Never a bare "off" badge when enabled.
        assert "extraction.enabled is off" not in html
        assert "8/29/2026" in html or "2026" in html  # locale-rendered date, just prove SOME date landed

    def test_in_agnes_schedule_never_run_reads_honestly(self):
        fs = dict(self._FILE_SOURCE)
        fs["schedule"] = {
            **fs["schedule"],
            "in_agnes": {"enabled": True, "schedule": "every 4h", "last_run_at": None, "next_run_at": None},
        }
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        assert "never run" in result["html"].lower()

    def test_in_agnes_schedule_disabled_shows_an_off_badge(self):
        fs = dict(self._FILE_SOURCE)
        fs["schedule"] = {
            **fs["schedule"],
            "in_agnes": {"enabled": False, "schedule": None, "last_run_at": None, "next_run_at": None},
        }
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "off" in html.lower()
        assert "no schedule configured" in html.lower()

    def test_drawer_filters_to_the_clicked_category_and_toggles_closed(self):
        result = self._run(
            """
            toggleFileSourceDrawer("sp-conn-1", "rejected_quotes");
            const afterOpen = { ..._elements["ds-fs-drawer-sp-conn-1"] };
            toggleFileSourceDrawer("sp-conn-1", "rejected_quotes");
            const afterToggleClose = { ..._elements["ds-fs-drawer-sp-conn-1"] };
            toggleFileSourceDrawer("sp-conn-1", "protocol_errors");
            const afterSwitch = { ..._elements["ds-fs-drawer-sp-conn-1"] };
            console.log(JSON.stringify({ afterOpen, afterToggleClose, afterSwitch }));
            """
        )
        after_open = result["afterOpen"]
        assert after_open["hidden"] is False
        # No `doc` on this row (never resolved) -> raw sha16 fallback, and
        # the KNOWN reason slug earns its full human sentence, not the slug.
        assert "doc-a" in after_open["innerHTML"]
        assert "Quote not found in the document's text" in after_open["innerHTML"]
        assert "verbatim_gate_failed" not in after_open["innerHTML"]
        assert "doc-c" not in after_open["innerHTML"]

        assert result["afterToggleClose"]["hidden"] is True

        after_switch = result["afterSwitch"]
        assert after_switch["hidden"] is False
        assert "doc-c" in after_switch["innerHTML"]
        assert "isn't registered in any collection" in after_switch["innerHTML"]
        assert "unresolved_doc_id" not in after_switch["innerHTML"]
        # An UNKNOWN reason slug ("malformed_edge", row 3) is shown
        # verbatim — never hidden, per spec.
        assert "malformed_edge" in after_switch["innerHTML"]
        assert "doc-a" not in after_switch["innerHTML"]

    def test_source_urls_rejected_drawer_shows_the_reason_not_the_slug(self):
        """O7 follow-up: the new badge category drives the SAME generic
        drawer/reason machinery as every other category — proven here by a
        reason (`not_https`) none of the pre-existing categories use."""
        result = self._run(
            """
            toggleFileSourceDrawer("sp-conn-1", "source_urls_rejected");
            console.log(JSON.stringify({ ..._elements["ds-fs-drawer-sp-conn-1"] }));
            """
        )
        assert result["hidden"] is False
        assert "doc-d" in result["innerHTML"]
        assert "isn't https" in result["innerHTML"]
        assert "not_https" not in result["innerHTML"]

    def test_drawer_reports_empty_category_honestly(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["deferred"] = []
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "deferred"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        assert "Nothing in this category" in result["innerHTML"]

    # -- humanized rejection rows (TCRD-240/241 live-use follow-up) --------

    def test_drawer_renders_resolved_name_and_collection_with_sha16_as_tooltip(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["rejected_quotes"] = [
            {
                "row": 0,
                "reason": "verbatim_gate_failed",
                "doc_id": "6a8e0bc93c07c56a",
                "doc": {"name": "Q3 Contract.pdf", "collection": "Contracts"},
            }
        ]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "rejected_quotes"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        html = result["innerHTML"]
        assert "Q3 Contract.pdf" in html
        assert "Contracts" in html
        # The raw sha16 is demoted to a tooltip, not shown as the primary text.
        assert 'title="6a8e0bc93c07c56a"' in html
        assert ">6a8e0bc93c07c56a<" not in html

    def test_drawer_falls_back_to_sha16_and_not_in_any_collection_when_unresolved(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["protocol_errors"] = [
            {"row": 0, "reason": "unresolved_doc_id", "doc_id": "deadbeefcafebabe", "doc": None}
        ]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "protocol_errors"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        html = result["innerHTML"]
        assert ">deadbeefcafebabe<" in html
        assert "not in any collection" in html

    def test_drawer_groups_duplicate_doc_id_reason_pairs_with_a_count_badge(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["rejected_quotes"] = [
            {"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1", "doc": {"name": "a.pdf", "collection": "C"}},
            {"row": 1, "reason": "verbatim_gate_failed", "doc_id": "d1", "doc": {"name": "a.pdf", "collection": "C"}},
            # Different reason, same doc_id -> stays a SEPARATE row (never
            # a blended subline).
            {"row": 2, "reason": "unresolved_doc_id", "doc_id": "d1", "doc": None},
        ]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "rejected_quotes"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        html = result["innerHTML"]
        assert "2×" in html
        assert html.count("<li>") == 2  # the (d1, verbatim) pair collapsed; (d1, unresolved) stayed its own row

    def test_unknown_reason_slug_is_shown_verbatim_never_hidden(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["protocol_errors"] = [{"row": 0, "reason": "some_future_reason", "doc_id": "d9", "doc": None}]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "protocol_errors"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        assert "some_future_reason" in result["innerHTML"]

    def test_ambiguous_cross_collection_reason_gets_its_own_human_subline(self):
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["protocol_errors"] = [
            {"row": 0, "reason": "ambiguous_cross_collection_doc_id", "doc_id": "d9", "doc": None}
        ]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "protocol_errors"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        html = result["innerHTML"]
        assert "ambiguous_cross_collection_doc_id" not in html
        assert "didn't say which" in html

    def test_quote_not_meaningful_reason_gets_its_own_human_subline(self):
        """spec §8.4: a distinct reason from `verbatim_gate_failed` (the
        quote WAS found, it just isn't evidence) — surfaced in the SAME
        `rejected_quotes` drawer, with its own explanatory sentence rather
        than the raw slug."""
        fs = dict(self._FILE_SOURCE)
        fs["last_run"] = dict(fs["last_run"])
        fs["last_run"]["rejected_quotes"] = [{"row": 0, "reason": "quote_not_meaningful", "doc_id": "d9", "doc": None}]
        result = self._run(
            'toggleFileSourceDrawer("sp-conn-1", "rejected_quotes"); '
            'console.log(JSON.stringify(_elements["ds-fs-drawer-sp-conn-1"]));',
            file_source=fs,
        )
        html = result["innerHTML"]
        assert "quote_not_meaningful" not in html
        assert "too short or not a real word/phrase" in html

    # -- sharing-state row (rephrased from "Identity matching") ------------

    def test_sharing_row_ok_when_every_scope_collection_has_a_group(self):
        fs = dict(self._FILE_SOURCE)
        fs["identity"] = {"groups_matched": 2, "collections_no_group": 0, "collections_total": 2}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "all scope collections have a group" in html
        assert "is-ok" in html

    def test_sharing_row_warn_singular_and_plural_phrasing(self):
        fs = dict(self._FILE_SOURCE)
        fs["identity"] = {"groups_matched": 0, "collections_no_group": 1, "collections_total": 1}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        assert "1 collection has no group — only admins see them" in result["html"]

        fs["identity"] = {"groups_matched": 0, "collections_no_group": 3, "collections_total": 3}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        assert "3 collections have no group — only admins see them" in result["html"]

    def test_sharing_row_empty_state_when_no_scope_collections_exist_yet(self):
        fs = dict(self._FILE_SOURCE)
        fs["identity"] = {"groups_matched": 0, "collections_no_group": 0, "collections_total": 0}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        assert "no scope collections yet" in result["html"]

    # -- scope rows (spec follow-up: clickable into the wizard) ------------

    def test_scope_rows_render_clickable_and_wire_the_connection_id(self):
        fs = dict(self._FILE_SOURCE)
        fs["scopes"] = [
            {
                "source_scope_id": "site1-drive1",
                "display_path": "Site / Contracts",
                "collection": {"id": "col_a", "slug": "contracts", "name": "Contracts"},
                "no_group_warning": False,
            }
        ]
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "Scope collections" in html
        assert "Site / Contracts" in html
        assert "Contracts" in html
        assert "openSpWizardForConnection('sp-conn-1', { highlightScopeId: 'site1-drive1' })" in html

    def test_scope_row_warns_when_ungranted(self):
        fs = dict(self._FILE_SOURCE)
        fs["scopes"] = [
            {
                "source_scope_id": "site1-drive2",
                "display_path": "Site / Reports",
                "collection": {"id": "col_b", "slug": "reports", "name": "Reports"},
                "no_group_warning": True,
            }
        ]
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "no group" in html
        assert "badge-warn" in html

    def test_no_scope_rows_section_when_scopes_is_empty(self):
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));")
        assert "Scope collections" not in result["html"]

    # -- anonymization row (spec §9.2/§13.2): requested vs declared --------

    def test_facts_html_has_no_anonymization_row_when_nothing_requested(self):
        """No scope has ever been marked anonymize — the row must not
        appear at all, not render as empty/zero."""
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));")
        assert "Anonymization" not in result["html"]

    def test_facts_html_renders_requested_but_not_declared_as_warn(self):
        fs = dict(self._FILE_SOURCE)
        fs["anonymization"] = {"requested": ["col_a"], "declared": [], "pending": ["col_a"]}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "anonymization requested 1" in html
        # Never claims "anonymized" for a scope nothing has declared yet.
        assert "anonymized 1" not in html
        assert "badge-warn" in html

    def test_facts_html_renders_declared_as_ok(self):
        fs = dict(self._FILE_SOURCE)
        fs["anonymization"] = {"requested": ["col_a"], "declared": ["col_a"], "pending": []}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "anonymized 1" in html
        assert "anonymization requested" not in html
        assert "badge-env" in html

    def test_facts_html_renders_both_declared_and_pending_together(self):
        fs = dict(self._FILE_SOURCE)
        fs["anonymization"] = {"requested": ["col_a", "col_b"], "declared": ["col_a"], "pending": ["col_b"]}
        result = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)
        html = result["html"]
        assert "anonymized 1" in html
        assert "anonymization requested 1" in html


class TestSourceTypeAwareActionsMenu:
    """`_sourceMenuItems(row)` — a SharePoint connection gets its OWN verb
    set (spec follow-up, TCRD-240/241 live-use feedback: the Keboola-only
    items "meaningless/dangerous" on a file source, and no direct scope-
    management entry at all). Executed for real via `node`, not just
    string-matched, so a future edit that keeps the SharePoint branch's
    literal strings but breaks the guard (e.g. falls through to the
    Keboola branch) would fail this."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)

    def _run(self, row: dict) -> str:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig) for sig in ("function _esc(s) {", "function _sourceMenuItems(row) {")
        )
        script = f"""
{fns}

console.log(_sourceMenuItems({json.dumps(row)}));
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
        return proc.stdout

    _SP_ONLY_ITEMS = [
        "Manage scopes…",
        "Test connection",
        "Run extraction now",
        "Update certificate…",
        "Delete source",
    ]
    _KEBOOLA_ONLY_ITEMS = [
        "Add tables…",
        "Rotate storage token",
        "Semantic-layer token",
        "chat tools",
        "Make default project",
    ]

    def test_sharepoint_menu_has_exactly_the_sharepoint_item_set(self):
        html = self._run({"id": "sp-conn-1", "source_type": "sharepoint", "name": "Corp SharePoint"})
        for item in self._SP_ONLY_ITEMS:
            assert item in html, f"missing {item!r} from the SharePoint menu"
        for item in self._KEBOOLA_ONLY_ITEMS:
            assert item not in html, f"Keboola-only item {item!r} leaked into the SharePoint menu"
        assert "runSpExtraction('sp-conn-1')" in html
        # The wrong "Test connection" (Keboola's storage-token verify) must
        # not be wired — the SharePoint-specific `testSpConn` is.
        assert "testSpConn('sp-conn-1')" in html
        assert "testConn('sp-conn-1')" not in html
        assert "openSpWizardForConnection('sp-conn-1')" in html
        assert "toggleSpCertRow('sp-conn-1')" in html

    def test_keboola_menu_is_unchanged_by_the_sharepoint_branch(self):
        html = self._run(
            {
                "id": "kbc-conn-1",
                "source_type": "keboola",
                "name": "Corp Keboola",
                "is_default": False,
                "has_master_secret": False,
                "has_chat_tools": False,
            }
        )
        for item in self._KEBOOLA_ONLY_ITEMS:
            # "chat tools" itself is a substring of both on/off labels.
            assert item.replace("chat tools", "chat tools") in html
        assert "Manage scopes…" not in html
        assert "Update certificate…" not in html
        assert "testConn('kbc-conn-1')" in html


class TestManageScopesButtonOnTheCard:
    """The collapsed card's primary "Manage scopes" button — the owner's
    explicit demand ("a dumb path, make it clickable") for a direct, one-
    click entry into the wizard bound to THIS connection, sitting next to
    the Actions dropdown rather than buried inside it."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)

    def _run(self, row: dict) -> str:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fn = self._extract_function(tpl, "function _connectionCardHtml(row) {")
        script = f"""
function _esc(s) {{ return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
function _connector(t) {{ return {{ abbr: "?", cls: "local" }}; }}
function _connectorLogo(t) {{ return ""; }}
function _sourceHealth(row) {{ return null; }}
function _sourceSubtitle(row) {{ return ""; }}
function _pipelineStripHtml(row) {{ return ""; }}
function _sharepointFactsHtml(row) {{ return ""; }}
function _secretBadgeHtml(row) {{ return ""; }}
function _masterTokenFactHtml(row) {{ return ""; }}
function _chatToolsFactHtml(row) {{ return ""; }}
const ICO_CHEVRON = "";
const ICO_CARET = "";

{fn}

console.log(_connectionCardHtml({json.dumps(row)}));
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
        return proc.stdout

    def test_present_and_wired_on_a_sharepoint_card(self):
        html = self._run({"id": "sp-conn-1", "source_type": "sharepoint", "name": "Corp SharePoint"})
        assert "Manage scopes" in html
        assert "openSpWizardForConnection('sp-conn-1')" in html

    def test_absent_on_a_keboola_card(self):
        html = self._run({"id": "kbc-conn-1", "source_type": "keboola", "name": "Corp Keboola"})
        assert "Manage scopes" not in html


class TestOpenSpWizardForConnection:
    """`openSpWizardForConnection` — the card's "Manage scopes" button and
    each scope row's click target. Unit-tested against STUBBED
    `openSpWizard`/`spGoStep`/`spEnableStep`/`spLoadScopesThenTree`/
    `spLoadShare` (each is either exercised live elsewhere or is the
    wizard's own long-standing DOM machinery) so this test is about the ONE
    thing this function actually adds: does it bind the right connection
    id, land on the right step, and — when asked — highlight the right
    scope row once step 3 has actually rendered."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)

    def _run(self, call: str, *, highlight_target_found: bool = True) -> dict:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig)
            for sig in (
                "function openSpWizardForConnection(connId, opts) {",
                "function _spHighlightShareRow(scopeId) {",
            )
        )
        found = "true" if highlight_target_found else "false"
        script = f"""
{fns}

let spConnId = null;
let spLevel = null;
let spCrumbs = null;
const calls = [];
function openSpWizard() {{ calls.push(["openSpWizard"]); }}
function spEnableStep(n) {{ calls.push(["spEnableStep", n]); }}
function spGoStep(n) {{ calls.push(["spGoStep", n]); }}
function spLoadScopesThenTree() {{ calls.push(["spLoadScopesThenTree"]); }}
function spLoadShare() {{ calls.push(["spLoadShare"]); return Promise.resolve(); }}

global.CSS = {{ escape: (s) => s }};
const highlighted = [];
const _row = {{
  scrollIntoView: () => highlighted.push("scrolled"),
  classList: {{ add: () => highlighted.push("added"), remove: () => highlighted.push("removed") }},
}};
const document = {{ querySelector: (sel) => ({found} ? _row : null) }};
global.setTimeout = (fn) => fn();  // run the un-highlight synchronously, deterministically

{call}.then(() => {{
  console.log(JSON.stringify({{ calls, spConnId, highlighted }}));
}});
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

    def test_plain_call_binds_the_id_and_lands_on_the_scope_step(self):
        result = self._run('Promise.resolve(openSpWizardForConnection("sp-conn-1"))')
        assert result["spConnId"] == "sp-conn-1"
        assert result["calls"] == [
            ["openSpWizard"],
            ["spEnableStep", 2],
            ["spEnableStep", 3],
            ["spGoStep", 2],
            ["spLoadScopesThenTree"],
        ]
        assert result["highlighted"] == []

    def test_highlight_call_lands_on_share_step_and_highlights_the_row(self):
        result = self._run(
            'Promise.resolve(openSpWizardForConnection("sp-conn-1", { highlightScopeId: "site1-drive1" }))'
        )
        assert result["spConnId"] == "sp-conn-1"
        assert result["calls"] == [
            ["openSpWizard"],
            ["spEnableStep", 2],
            ["spEnableStep", 3],
            ["spGoStep", 3],
            ["spLoadShare"],
        ]
        assert result["highlighted"] == ["scrolled", "added", "removed"]

    def test_highlight_is_a_no_op_when_the_row_is_not_on_screen(self):
        result = self._run(
            'Promise.resolve(openSpWizardForConnection("sp-conn-1", { highlightScopeId: "missing" }))',
            highlight_target_found=False,
        )
        assert result["calls"][-2] == ["spGoStep", 3]
        assert result["highlighted"] == []


class TestOpenSpWizardPreselectsSingleExistingConnection:
    """`openSpWizard()`'s step-1 "Continue an existing connection" picker
    pre-selects the only option when exactly one SharePoint connection
    exists — one fewer click on the connect-wizard entry path too."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)

    def _run(self, connections: list) -> dict:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig)
            for sig in ("function spEsc(s) {", "function spApi(url, opts) {", "function openSpWizard() {")
        )
        script = f"""
{fns}

const SP_CONN_API = "/api/admin/source-connections";
let spConnId, spCertChoice, spLevel, spCrumbs, spItems, spScopes, spGroups, spPendingGroups, spTreeFilterQuery, spLastSearchMatches, spUniquePerms;
function spSetCertChoice(c) {{}}
function spGoStep(n) {{}}
function _syncDropdownRebuild(sel) {{}}

function mockEl() {{
  return {{
    value: "", style: {{}}, innerHTML: "", hidden: false,
    classList: {{ add() {{}}, remove() {{}}, contains() {{ return false; }} }},
    focus() {{}},
  }};
}}
const _elements = {{}};
const document = {{
  getElementById: (id) => (_elements[id] = _elements[id] || mockEl()),
  body: {{ style: {{}} }},
}};
global.fetch = async (url, opts) => ({{
  ok: true, status: 200,
  json: async () => ({json.dumps(connections)}),
}});

openSpWizard();
await new Promise((r) => setTimeout(r, 80));
console.log(JSON.stringify({{
  selectValue: _elements["spw-existing-select"].value,
  selectHtml: _elements["spw-existing-select"].innerHTML,
}}));
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

    def test_single_connection_is_preselected(self):
        result = self._run([{"id": "sp-conn-1", "name": "Corp SharePoint"}])
        assert result["selectValue"] == "sp-conn-1"

    def test_multiple_connections_leave_the_native_default_selection(self):
        result = self._run(
            [{"id": "sp-conn-1", "name": "Corp SharePoint"}, {"id": "sp-conn-2", "name": "Marketing SharePoint"}]
        )
        # Both options are rendered; no assertion on which one is
        # "selected" — a real <select> defaults to its first option, which
        # this mock element doesn't model, so this only pins that the code
        # does not special-case multiple rows the way it does exactly one.
        assert "sp-conn-1" in result["selectHtml"]
        assert "sp-conn-2" in result["selectHtml"]


class TestSharePointWizardShareBadgeRendering:
    """`spRenderShare` (the connect wizard's step-3 share preview, spec
    §13.2) executed for real via `node` — the badge this task exists to
    fix: `anonymize=true` alone must never render "anonymized"; that word
    is earned only once the scope row's server-computed
    `anonymization_declared` is also true. Same node-harness pattern as
    `TestSharePointSourceCardRendering`."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)

    def _run(self, items: list) -> str:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig) for sig in ("function spEsc(s) {", "function spRenderShare(items) {")
        )
        script = f"""
{fns}

const _host = {{ innerHTML: "", querySelectorAll: () => [] }};
const document = {{ getElementById: (id) => (id === "spw-share-rows" ? _host : null) }};
let spPendingGroups = {{}};
let spGroups = [];
// `spRenderShare` reads `spUniquePerms[source_scope_id]` for its advisory
// summary line — an empty map here means "nothing flagged", which is
// exactly right for this class: it is not exercising that summary, only
// the anonymize badge ladder.
let spUniquePerms = {{}};
const items = {json.dumps(items)};

spRenderShare(items);
console.log(_host.innerHTML);
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
        return proc.stdout

    def _row(self, **overrides) -> dict:
        row = {
            "source_scope_id": "scope-1",
            "display_path": "Contracts",
            "anonymize": False,
            "anonymization_declared": False,
            "collection_id": "col_a",
            "collection": {"id": "col_a", "slug": "contracts", "name": "Contracts"},
            "group_ids": ["g1"],
        }
        row.update(overrides)
        return row

    def test_not_anonymize_marked_shows_no_badge(self):
        html = self._run([self._row(anonymize=False)])
        assert "sp-badge--anon" not in html

    def test_requested_but_not_declared_shows_requested_warn_badge(self):
        html = self._run([self._row(anonymize=True, anonymization_declared=False)])
        assert "anonymization requested" in html
        assert ">anonymized<" not in html
        assert 'sp-badge--anon"' in html
        assert "sp-badge--anon-declared" not in html

    def test_declared_shows_anonymized_ok_badge(self):
        html = self._run([self._row(anonymize=True, anonymization_declared=True)])
        assert ">anonymized<" in html
        assert "anonymization requested" not in html
        assert "sp-badge--anon-declared" in html

    def test_declared_true_but_anonymize_false_shows_no_badge(self):
        """A defensive edge case: the server never produces this
        combination (declared implies anonymize was true when the run
        landed), but the client must not invent a claim from a stale
        `anonymization_declared` alone."""
        html = self._run([self._row(anonymize=False, anonymization_declared=True)])
        assert "sp-badge--anon" not in html


class TestSharePointCertificateMetadataRendering:
    """Certificate metadata rows (thumbprint, subject/issuer, expiry status)
    on the file-source card — introduced alongside
    `GET .../connections/{id}/certificate` (integration, #1704). Same
    node-harness pattern; kept as its own class rather than folded back
    into `TestSharePointSourceCardRendering` so a merge conflict here next
    time is a smaller diff."""

    _extract_function = staticmethod(TestSharePointSourceCardRendering._extract_function)
    _FILE_SOURCE = TestSharePointSourceCardRendering._FILE_SOURCE
    _run = TestSharePointSourceCardRendering._run

    def test_certificate_metadata_renders_thumbprint_subject_and_ok_badge(self):
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": "vault",
            "env_name": None,
            "set_at": "2026-08-20T12:00:00+00:00",
            "error": None,
            "thumbprint_x5t": "abcXYZ123-_",
            "thumbprint_sha1_hex": "AB" * 20,
            "subject": "CN=agnes-test",
            "issuer": "CN=agnes-test",
            "not_before": "2026-08-01T00:00:00+00:00",
            "not_after": "2027-08-01T00:00:00+00:00",
            "expires_in_days": 300,
            "status": "ok",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert "abcXYZ123-_" in html
        assert "CN=agnes-test" in html
        assert "badge-ok" in html
        assert "300d left" in html

    def test_certificate_metadata_expiring_soon_gets_the_warn_badge(self):
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": "env",
            "env_name": "SHAREPOINT_CERT_PRIVATE_KEY",
            "set_at": None,
            "error": None,
            "thumbprint_x5t": "thumb-soon",
            "thumbprint_sha1_hex": "CD" * 20,
            "subject": "CN=agnes-test",
            "issuer": "CN=agnes-test",
            "not_before": "2026-01-01T00:00:00+00:00",
            "not_after": "2026-09-05T00:00:00+00:00",
            "expires_in_days": 8,
            "status": "expiring_soon",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert "badge-warn" in html
        assert "8d left" in html

    def test_certificate_metadata_expired_gets_the_danger_badge(self):
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": "env",
            "env_name": "SHAREPOINT_CERT_PRIVATE_KEY",
            "set_at": None,
            "error": None,
            "thumbprint_x5t": "thumb-expired",
            "thumbprint_sha1_hex": "EF" * 20,
            "subject": "CN=agnes-test",
            "issuer": "CN=agnes-test",
            "not_before": "2025-01-01T00:00:00+00:00",
            "not_after": "2025-06-01T00:00:00+00:00",
            "expires_in_days": -80,
            "status": "expired",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert "badge-danger" in html
        assert "expired 80d ago" in html

    def test_no_certificate_configured_renders_a_clean_absence_not_a_blank_card(self):
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": "env",
            "env_name": "SHAREPOINT_CERT_PRIVATE_KEY",
            "set_at": None,
            "error": None,
            "metadata_reason": "no_certificate_configured",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert "not configured" in html
        assert "badge-ok" not in html and "badge-warn" not in html and "badge-danger" not in html

    def test_unparseable_certificate_renders_unreadable_not_a_blank_card(self):
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": "vault",
            "env_name": None,
            "set_at": "2026-08-20T12:00:00+00:00",
            "error": None,
            "metadata_reason": "certificate_unparseable: bad PEM",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert "unreadable" in html

    def test_settings_resolution_error_does_not_duplicate_the_not_configured_row(self):
        """`cert.error` (a settings-RESOLUTION failure) already renders "not
        configured" via the existing Certificate row — the metadata rows must
        not repeat that verdict a second time."""
        fs = dict(self._FILE_SOURCE)
        fs["certificate"] = {
            "origin": None,
            "env_name": None,
            "set_at": None,
            "error": "SharePoint connection is missing required field(s): tenant_id, client_id",
        }
        html = self._run("console.log(JSON.stringify({ html: _sharepointFactsHtml(row) }));", file_source=fs)["html"]
        assert html.count("Thumbprint") == 0


class TestSourceCardSubtitleIdentity:
    """`_sourceSubtitle` executed for real via `node`. A SharePoint connection
    has no `config.stack_url` — the generic branch always fell through to
    the literal `"(no connection URL)"`, which is not an honest fact about a
    SharePoint connection (there is no connection URL to report). Pins the
    SharePoint-specific identity line (tenant + scope summary) and that
    every non-SharePoint card is byte-for-byte unchanged.
    """

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
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

    def _run(self, row: dict) -> str:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig)
            for sig in (
                "function _esc(s) {",
                "function _sourceSubtitle(row) {",
                "function _sharepointIdentityLine(config) {",
            )
        )
        script = f"""
{fns}

const row = {json.dumps(row)};
console.log(_sourceSubtitle(row));
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
        return proc.stdout.strip()

    def test_sharepoint_card_shows_tenant_and_single_shared_site(self):
        row = {
            "derived": False,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "scopes": [
                    {
                        "source_scope_id": "s1",
                        "display_path": "Communication site / Shared Documents",
                        "anonymize": False,
                        "collection_id": "c1",
                    },
                    {
                        "source_scope_id": "s2",
                        "display_path": "Communication site / Reports",
                        "anonymize": False,
                        "collection_id": "c2",
                    },
                ],
            },
        }
        out = self._run(row)
        assert "(no connection URL)" not in out
        assert "<code>a1b2c3d4…</code>" in out
        assert "2 scopes · Communication site" in out

    def test_sharepoint_card_shows_n_sites_when_scopes_span_multiple_sites(self):
        row = {
            "derived": False,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "scopes": [
                    {
                        "source_scope_id": "s1",
                        "display_path": "Marketing site / Docs",
                        "anonymize": False,
                        "collection_id": "c1",
                    },
                    {
                        "source_scope_id": "s2",
                        "display_path": "Finance site / Docs",
                        "anonymize": False,
                        "collection_id": "c2",
                    },
                ],
            },
        }
        out = self._run(row)
        assert "(no connection URL)" not in out
        assert "2 scopes · 2 sites" in out

    def test_sharepoint_card_with_no_scopes_is_honest_not_a_url_placeholder(self):
        row = {
            "derived": False,
            "source_type": "sharepoint",
            "config": {"tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "scopes": []},
        }
        out = self._run(row)
        assert "(no connection URL)" not in out
        assert "no scopes selected" in out

    def test_sharepoint_card_with_no_tenant_is_honest(self):
        row = {"derived": False, "source_type": "sharepoint", "config": {"scopes": []}}
        out = self._run(row)
        assert "(no connection URL)" not in out
        assert "(no tenant configured)" in out

    def test_sharepoint_identity_line_escapes_the_site_name(self):
        """`display_path` comes from a SharePoint site name — untrusted text
        rendered into innerHTML — so the site label must go through `_esc`
        like everything else on the card."""
        row = {
            "derived": False,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "scopes": [
                    {
                        "source_scope_id": "s1",
                        "display_path": "<img src=x onerror=alert(1)> / Docs",
                        "anonymize": False,
                        "collection_id": "c1",
                    },
                ],
            },
        }
        out = self._run(row)
        assert "<img" not in out
        assert "&lt;img" in out

    def test_non_sharepoint_card_with_no_stack_url_is_unchanged(self):
        row = {"derived": False, "source_type": "keboola", "config": {}}
        out = self._run(row)
        assert out == "(no connection URL)"

    def test_non_sharepoint_card_with_stack_url_is_unchanged(self):
        row = {
            "derived": False,
            "source_type": "keboola",
            "config": {"stack_url": "https://connection.keboola.com", "project_id": 42, "project_name": "My Project"},
        }
        out = self._run(row)
        assert out == "<code>connection.keboola.com</code> · My Project · project 42"


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


class TestSourcePipelinesEndpoint:
    """`GET /api/admin/source-pipelines` — the pipeline strip as data.

    The page inlines the same dict at render time (`SOURCE_PIPELINES`), so
    every card froze at whatever was true when the HTML was built: an admin
    who registered tables through the wizard kept reading "Add the first
    tables →" until they hard-reloaded. This endpoint is what the page
    re-reads after each mutation — read-only, admin-gated exactly like the
    page it serves.
    """

    def _get(self, seeded_app, token):
        return seeded_app["client"].get(
            "/api/admin/source-pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_admin_gets_the_strip_dict(self, seeded_app):
        import uuid

        from src.repositories import source_connections_repo

        conn_id = f"pipeapi-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Pipe API {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.example.com"},
        )
        try:
            resp = self._get(seeded_app, seeded_app["admin_token"])
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert conn_id in body
            assert set(body[conn_id]) == {"tables", "sync", "semantic", "feeds"}
            assert body[conn_id]["tables"]["count"] == 0
        finally:
            source_connections_repo().delete(conn_id)

    def test_it_serves_the_same_dict_the_template_inlines(self, seeded_app):
        """No new data — it is `_source_pipelines()`, the page's own context."""
        from app.web.router import _source_pipelines

        resp = self._get(seeded_app, seeded_app["admin_token"])
        assert resp.status_code == 200
        assert set(resp.json()) == set(_source_pipelines())

    def test_a_registration_after_page_render_is_visible_without_a_reload(self, seeded_app):
        """The A14 regression, end to end: register a table AFTER the page
        HTML was built, and the endpoint must already know about it."""
        import uuid

        from src.repositories import source_connections_repo, table_registry_repo

        conn_id = f"pipefresh-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Pipe Fresh {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.example.com"},
        )
        c = seeded_app["client"]
        auth = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        page = c.get("/admin/data-sources", headers=auth).text
        assert f'"{conn_id}"' in page  # the stale snapshot the page froze

        tid = f"pipefresh-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"pipe_fresh_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-test",
            source_table="pipe_fresh",
            query_mode="local",
            connection_id=conn_id,
        )
        try:
            body = self._get(seeded_app, seeded_app["admin_token"]).json()
            assert body[conn_id]["tables"]["count"] == 1
        finally:
            table_registry_repo().unregister(tid)
            source_connections_repo().delete(conn_id)

    def test_non_admin_is_refused(self, seeded_app):
        assert self._get(seeded_app, seeded_app["analyst_token"]).status_code == 403

    def test_unauthenticated_is_refused(self, seeded_app):
        resp = seeded_app["client"].get("/api/admin/source-pipelines")
        assert resp.status_code in (401, 403)


class TestSourceCardRefreshWiring:
    """Every mutating action on /admin/data-sources re-reads the strip.

    `SOURCE_PIPELINES` is baked into the page at render time, so a card kept
    reporting "Add the first tables → / Never synced / 0 packages" after the
    wizard had registered two dozen tables (A14). The fix is one function —
    `refreshSourcePipelines()` — called from every handler that changes what
    a strip says; a full page reload is the fallback, never the mechanism,
    because it throws away expanded cards and scroll position.
    """

    # Handler → what it changes about a strip.
    HANDLERS = {
        "registerSelected": "registers tables (inline browse + Keboola wizard step 2)",
        "_registerBqRows": "registers BigQuery rows from wizard step 2",
        "_registerSfRows": "registers Snowflake rows from wizard step 2",
        "closeWizard": "the wizard registered/bundled/shared, and is now closing",
        "_createPackagesAndContinue": "creates data packages / attaches tables",
        "_shareAndFinish": "grants packages to groups (the feeds cell)",
        "saveRotatedToken": "stores a storage token",
        "saveMasterToken": "stores the semantic-layer master token",
        "removeMasterToken": "clears the semantic-layer master token",
        "saveSpCertificate": "stores a SharePoint certificate",
        "toggleChatTools": "enables/disables the connection's chat tools",
        "grantChatTools": "grants the derived MCP tools to a group",
        "unbindProject": "clears the connection's project binding",
        "deleteConn": "removes a source (and re-attributes unlinked tables)",
        "setDefaultConn": "moves the default flag between connections",
        "runSpExtraction": "queues a SharePoint extraction run",
        "importKeboolaConnection": "turns the derived card into a real connection",
        "closeSpWizard": "the SharePoint wizard connected/scoped/shared",
    }

    @staticmethod
    def _js_function_body(page: str, name: str) -> str:
        """The source of one top-level JS function, up to the next one."""
        import re

        start = re.search(rf"^(?:async )?function {re.escape(name)}\(", page, re.MULTILINE)
        assert start, f"{name}() is gone from the template — update this guard"
        rest = page[start.end() :]
        nxt = re.search(r"^(?:async )?function ", rest, re.MULTILINE)
        return rest[: nxt.start()] if nxt else rest

    def _page(self, seeded_app) -> str:
        return (
            seeded_app["client"]
            .get(
                "/admin/data-sources",
                headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            )
            .text
        )

    def test_the_refresh_function_reads_the_endpoint(self, seeded_app):
        body = self._js_function_body(self._page(seeded_app), "refreshSourcePipelines")
        assert "/api/admin/source-pipelines" in body

    def test_the_refresh_repaints_instead_of_reloading(self, seeded_app):
        """A reload loses expanded cards and scroll position — the strip is
        repainted in place from the fresh dict instead."""
        page = self._page(seeded_app)
        body = self._js_function_body(page, "refreshSourcePipelines")
        assert "window.location.reload" not in body
        assert "_repaintSourceCards" in body
        repaint = self._js_function_body(page, "_repaintSourceCards")
        assert "_pipelineStripHtml" in repaint
        assert "_sourceHealth" in repaint

    def test_every_mutation_handler_calls_the_refresh(self, seeded_app):
        page = self._page(seeded_app)
        missing = [
            f"{name} ({why})"
            for name, why in self.HANDLERS.items()
            if "refreshSourcePipelines(" not in self._js_function_body(page, name)
        ]
        assert not missing, (
            "these handlers mutate what a source card reports but never re-read "
            "the strip, so the card keeps showing the pre-mutation state until a "
            "hard reload:\n" + "\n".join(f"  {m}" for m in missing)
        )

    def test_the_refresh_is_one_function_not_copy_paste(self, seeded_app):
        """One reader, many callers — nobody re-implements the fetch."""
        page = self._page(seeded_app)
        assert page.count('fetch("/api/admin/source-pipelines"') == 1

    # The strip is not the whole card. These handlers also change something
    # the card BODY or head draws from the connection ROW (`config`, secret
    # presence, chat-tools state, or the row's very existence), and the strip
    # endpoint does not carry rows — so they re-read the list too.
    REDRAWERS = {
        "importKeboolaConnection": "the derived card becomes a real connection row",
        "saveSpCertificate": "secret presence + certificate metadata on the row",
        "setDefaultConn": "the `default` tag in the card head",
        "saveRotatedToken": "secret presence badge",
        "saveMasterToken": "secret presence badge",
        "removeMasterToken": "secret presence badge",
        "unbindProject": "`config.project_id` — the subtitle and the Unbind row",
        "toggleChatTools": "`has_chat_tools` + `chat_tools_source_id`",
        "runSpExtraction": "the dispatch stamps `config.extraction.last_run_at`",
        "closeSpWizard": "`config.scopes` — the identity line and scope list",
        "closeWizard": "a connection created in step 1 has no card at all yet",
    }

    def test_handlers_that_change_the_row_also_redraw_the_card_list(self, seeded_app):
        page = self._page(seeded_app)
        missing = [
            f"{name} ({why})"
            for name, why in self.REDRAWERS.items()
            if "loadConnections(" not in self._js_function_body(page, name)
        ]
        assert not missing, (
            "these handlers change what the card's ROW says, which the strip "
            "endpoint does not carry — refreshing the strip alone leaves the "
            "card body stale until a hard reload:\n" + "\n".join(f"  {m}" for m in missing)
        )

    def test_delete_redraws_from_the_cached_list_on_purpose(self, seeded_app):
        """The one deliberate exception to the rule above: the deleted row is
        dropped from `_connections` locally, so re-fetching the list to learn
        the same thing would be a round-trip for nothing."""
        body = self._js_function_body(self._page(seeded_app), "deleteConn")
        assert "renderConnList()" in body
        assert "loadConnections(" not in body


class TestRefreshSourcePipelinesBehavior:
    """`refreshSourcePipelines()` executed for real via `node` — the strip
    HTML it writes back into an already-drawn card, from the endpoint's
    payload, without a reload.

    String-matching the wiring (above) proves every handler CALLS it; this
    proves what it does when it runs: the fresh dict wins, the new strip is
    written into the card that is already on screen, and a failed read
    leaves the stale one alone rather than blanking the card.
    """

    _extract_function = staticmethod(TestImportKeboolaConnectionBehavior._extract_function)

    # Minimal DOM shim — this repo carries no jsdom, and what matters here is
    # WHAT gets written WHERE, not HTML parsing.
    _DOM_SHIM = """
class El {
  constructor(cls) {
    this.className = cls; this.textContent = ""; this.removed = false;
    this.inserted = []; this.written = null; this.kids = {};
  }
  set outerHTML(v) { this.written = v; }
  get outerHTML() { return this.written; }
  querySelector(sel) { return this.kids[sel] || null; }
  insertAdjacentHTML(pos, html) { this.inserted.push([pos, html]); }
  remove() { this.removed = true; }
}
"""

    def _run(self, *, fresh: dict, ok: bool = True, break_repaint: bool = False, concurrent: int = 1) -> dict:
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html").read_text(
            encoding="utf-8"
        )
        fns = "\n".join(
            self._extract_function(tpl, sig)
            for sig in (
                "function _esc(s) {",
                "function _relAge(minutes) {",
                "function _sharepointPipelineStripHtml(row) {",
                "function _pipelineStripHtml(row) {",
                "function _sharepointHealth(fs) {",
                "function _sourceHealth(row) {",
                "async function refreshSourcePipelines() {",
                "function _repaintSourceCards() {",
            )
        )
        break_repaint_js = "true" if break_repaint else "false"
        script = f"""
{self._DOM_SHIM}
{fns}

// The page as the admin left it: one card, drawn from a snapshot in which
// nothing was registered and nothing was shared.
let SOURCE_PIPELINES = {{
  "c1": {{"tables": {{"count": 0, "unlinked": 0}}, "sync": {{}},
          "semantic": {{"token": false, "metrics": 0, "terms": 0}},
          "feeds": {{"packages": 0, "groups": 0, "people": 0}}}}
}};
let _connections = [{{"id": "c1", "source_type": "keboola", "config": {{}}}}];
// Declared beside the function in the template, so it is not part of what
// `_extract_function` lifts out.
let _pipelineRefreshInFlight = null;

const card = new El("ds-src");
const head = new El("ds-src__head");
const strip = new El("ds-pipe");
const chip = new El("ds-src__health is-warn");
chip.textContent = "No tables yet";
const acts = new El("ds-src__acts");
const bodyEl = new El("ds-src__body");
bodyEl.hidden = false;  // the admin has this card expanded
card.kids = {{".ds-src__head": head, ":scope > .ds-pipe": strip}};
head.kids = {{".ds-src__health": chip, ".ds-src__acts": acts}};
const breakRepaint = {break_repaint_js};
global.document = {{
  getElementById: (id) => {{
    if (breakRepaint) throw new Error("DOM is gone");
    return id === "ds-conn-c1" ? card : null;
  }},
}};

let fetched = null;
let fetchCount = 0;
global.fetch = async (url, opts) => {{
  fetched = {{ url, opts }};
  fetchCount++;
  await new Promise((res) => setTimeout(res, 5));
  return {{ ok: {str(ok).lower()}, status: {200 if ok else 500},
            json: async () => ({json.dumps(fresh)}) }};
}};

(async () => {{
  const results = await Promise.all(
    Array.from({{ length: {concurrent} }}, () => refreshSourcePipelines()),
  );
  const returned = results[0];
  console.log(JSON.stringify({{
    returned,
    results,
    fetchCount,
    inFlightCleared: _pipelineRefreshInFlight === null,
    fetchedUrl: fetched && fetched.url,
    credentials: fetched && fetched.opts && fetched.opts.credentials,
    stripWritten: strip.written,
    stripRemoved: strip.removed,
    stripsInserted: head.inserted,
    chipText: chip.textContent,
    chipClass: chip.className,
    chipRemoved: chip.removed,
    bodyHidden: bodyEl.hidden,
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

    _FRESH = {
        "c1": {
            "tables": {"count": 3, "unlinked": 0, "basis": "connection"},
            "sync": {"last_sync": "2026-08-30T10:00:00+00:00", "age_minutes": 5, "errors": 0},
            "semantic": {"token": True, "metrics": 2, "terms": 1},
            "feeds": {"packages": 1, "groups": 1, "people": 4},
        }
    }

    def test_it_reads_the_endpoint_as_the_signed_in_admin(self):
        out = self._run(fresh=self._FRESH)
        assert out["fetchedUrl"] == "/api/admin/source-pipelines"
        assert out["credentials"] == "include"
        assert out["returned"] is True

    def test_the_card_on_screen_gets_the_fresh_strip(self):
        out = self._run(fresh=self._FRESH)
        written = out["stripWritten"]
        assert written, "the drawn card's strip was never rewritten"
        assert "3 registered" in written
        assert "Add the first tables" not in written
        assert "1 package → 4 people" in written
        assert "Never synced" not in written
        # Rewritten in place — not inserted a second time, not removed.
        assert out["stripsInserted"] == []
        assert out["stripRemoved"] is False

    def test_the_status_word_follows_the_strip(self):
        out = self._run(fresh=self._FRESH)
        assert out["chipText"] == "Healthy"
        assert "is-ok" in out["chipClass"]
        assert out["chipRemoved"] is False

    def test_an_expanded_card_stays_expanded(self):
        """The reason this repaints instead of reloading: an admin mid-task
        keeps their open card (and their scroll position)."""
        assert self._run(fresh=self._FRESH)["bodyHidden"] is False

    def test_a_failed_read_keeps_the_stale_strip_rather_than_blanking_it(self):
        out = self._run(fresh=self._FRESH, ok=False)
        assert out["returned"] is False
        assert out["stripWritten"] is None
        assert out["stripRemoved"] is False
        assert out["chipText"] == "No tables yet"

    def test_a_throwing_repaint_is_a_failed_refresh_not_an_escaping_error(self):
        """Two callers await this from inside a `finally` block. An exception
        escaping the repaint would replace whatever error that block was
        already unwinding — so the repaint lives inside the same `try` as the
        read, and a broken DOM is simply `false`."""
        out = self._run(fresh=self._FRESH, break_repaint=True)
        assert out["returned"] is False
        assert out["inFlightCleared"] is True

    def test_concurrent_callers_share_one_read(self):
        """A wizard exit reaches this from several places within a tick. Two
        overlapping reads resolve last-response-wins, and the loser can be the
        older snapshot — the exact staleness this function removes."""
        out = self._run(fresh=self._FRESH, concurrent=3)
        assert out["fetchCount"] == 1
        assert out["results"] == [True, True, True]
        # And the marker is cleared, so the NEXT mutation still gets a fresh read.
        assert out["inFlightCleared"] is True
