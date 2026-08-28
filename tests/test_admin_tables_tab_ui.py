"""UI tests for the /admin/tables lens.

The page has been through three shapes, and each guard below pins the
reason the current one exists:

  1. per-connector tab nav (BigQuery / Keboola / Jira / Agnes internal),
     which let the connector drive the page layout;
  2. a package-centric layout — one `<details>` per Data Package with its
     member tables inside, plus an "Unpackaged tables" callout. That made
     this page a SECOND list of packages beside /admin/data-packages, with
     a package's composition here and its sharing there;
  3. today: the flat cross-source lookup. Composition moved to the
     package's own page (/admin/data-packages/<id>); what is left is the
     question only a flat list answers — where is this table, and does it
     reach anyone — in the shared `.data-table` under one `.fbar` toolbar,
     with bulk selection for acting on many rows at once.

The file name keeps `_tab_ui` for git history continuity.
"""

import pathlib


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_one_toolbar(seeded_app):
    """ONE toolbar — the Library's `.fbar`, the component People and Groups
    adopted — carrying search, narrowing, order and the page's actions.

    They used to be scattered: a card of buttons above the list
    (`#adminTablesActionBar`), a search box inside the list panel, a checkbox
    beside it and cache freshness floating at the card's right edge.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    html = r.text
    assert 'id="adminTablesActionBar"' not in html
    assert 'class="fbar apg-bar"' in html
    # Search, narrow, order, act — in that order, all in the one bar.
    assert 'id="adminTablesSearch"' in html
    assert 'id="tbl-filter-btn"' in html
    assert 'id="adminTablesSort"' in html
    assert 'id="cacheWarmupCard"' in html
    assert 'id="registerNewTableBtn"' in html
    assert 'id="registerNewTableMenu"' in html
    # The count rides IN the bar. It had a row of its own between the toolbar
    # and the table — ~40px of page spent on three words.
    assert 'class="tbl-count" id="adminTablesCount"' in html
    assert 'apg-sechead__count" id="adminTablesCount"' not in html
    # Connector-typed Register entry points exist as dropdown items —
    # the page is package-centric but the operator still needs to pick
    # which register modal to open.
    assert 'data-register-source="bigquery"' in html
    assert 'data-register-source="keboola"' in html
    # Jira is webhook-driven — no Register button at all (the dropdown
    # surfaces a 'see docs' link instead).
    assert "onclick=\"closeRegisterNewTableMenu(); openRegisterModal('jira')\"" not in html


def test_admin_tables_active_register_modal_matches_instance_type(seeded_app, monkeypatch):
    """The instance's data_source.type still drives the body
    `data-source-type` marker, which the JS uses as a default when
    `openRegisterModal()` is called without an explicit source."""
    fake_cfg = {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # body carries the data-source-type marker → DATA_SOURCE_TYPE
        # picks it up so openRegisterModal() (no arg) routes to BQ.
        assert 'data-source-type="bigquery"' in html
    finally:
        reset_cache()


def test_admin_tables_register_dropdown_lists_connectors(seeded_app):
    """The `+ Register new table ▾` dropdown lists the connectors that
    have a register flow (BigQuery + Keboola). Jira is webhook-only —
    appears as a docs link, not a register trigger."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # The two register entry points are present (in the dropdown).
    assert "openRegisterModal('bigquery')" in html
    assert "openRegisterModal('keboola')" in html
    # Jira's read-only nature is communicated via a docs link.
    assert "docs/connectors/jira.md" in html


def test_admin_tables_layout_renders_one_flat_row_host(seeded_app):
    """ONE host, hydrated by loadAdminTablesLayout() from
    /api/admin/registry.

    This lens used to render a host per PACKAGE (`adminTablesLayoutPackages`)
    plus an `adminTablesLayoutUnpackaged` bucket — which made /admin/tables a
    second list of packages beside /admin/data-packages, with a package's
    composition here and its sharing there. Composition moved to the
    package's own page; this is the flat cross-source lookup that is left.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="adminTablesLayout"' in html
    assert 'id="adminTablesLayoutRows"' in html
    # The package-grouped hosts are GONE — their return would be the
    # duplication coming back.
    assert 'id="adminTablesLayoutPackages"' not in html
    assert 'id="adminTablesLayoutUnpackaged"' not in html
    assert "function loadAdminTablesLayout" in html
    assert "function _renderFlatTableRows" in html


def test_admin_tables_rows_carry_package_and_reach(seeded_app):
    """Each row runs to the END of the chain: which package carries the
    table, and how many people that reaches. "A table reaches nobody until
    it is in a package" was a rule stated once in the page subtitle while
    every row stayed silent about whether it obeyed."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    # Rows are hydrated client-side, so the column and cell vocabulary lives
    # in the page's own renderer rather than in server-rendered <td>s.
    assert "<th>Package</th>" in html
    assert "<th>Reaches</th>" in html
    # Reach is a grants x group-membership join no browser endpoint exposes,
    # so it is server-rendered into the page.
    assert "var TABLE_DELIVERY" in html
    # The two states that matter for an unpackaged row.
    assert "In no package" in html
    assert "Nobody" in html


def test_admin_tables_uses_the_shared_table_not_a_private_one(seeded_app):
    """`.data-table` is the product's ONE table — the one People, Groups and
    the Library render. This page carried its own `.registry-table`, a second
    table in a product that had agreed on one."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert '<table class="data-table" id="adminTablesFlat">' in html
    # The name may still appear in a comment recording why the private table
    # was retired (history, not a live rule) — what must be gone is every
    # SELECTOR and every element that would use one.
    for selector in (".registry-table {", ".registry-table ", 'class="registry-table'):
        assert selector not in html, selector


def test_admin_tables_note_explains_access_without_a_second_exit(seeded_app):
    """Access is written against the PACKAGE, not the table. The page made
    that point twice — a subtitle sentence and a button sending you to
    Packages — and the button was the wrong answer: a reader here is looking
    at tables, and a CTA out is not context."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert "Sharing is managed per" in html
    assert '<a class="btn btn-secondary" href="/admin/data-packages">Packages →</a>' not in html


def test_admin_tables_toolbar_holds_no_package_verbs(seeded_app):
    """ "+ New Data Package", "Group tables by bucket" and "Bulk assign
    tables" moved to the Packages lens — three PACKAGE verbs on a toolbar
    over a list of TABLES was the same category slip as the package-grouped
    layout underneath them."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'onclick="openCreateDataPackageModal' not in html
    assert 'onclick="groupTablesByBucket()"' not in html
    assert "onclick=\"openBulkAssignModal('')\"" not in html
    # …but the flows themselves stay reachable — the Packages lens links in
    # with the flow named in the query.
    assert "group_by_bucket" in html
    assert "new_package" in html


def test_admin_tables_no_connector_tab_nav(seeded_app):
    """Connector tab nav was dropped — every table now appears under a
    Data Package or in 'Unpackaged tables'. Verify the prior tab markers
    are gone."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # No tab nav structural markers. Scoped to the CONNECTOR strip: the page
    # carries the shared section strip (Sources · Tables · Packages ·
    # Semantic), which is a `role="tablist"` of its own and is not what this
    # guard is about.
    assert 'class="tab-nav"' not in html
    assert 'id="tab-content-bigquery"' not in html
    assert 'id="tab-content-keboola"' not in html
    assert 'id="tab-content-jira"' not in html
    assert 'id="tab-content-internal"' not in html
    # No per-tab listing divs either (rolled into loadAdminTablesLayout).
    assert 'id="bqTableListing"' not in html
    assert 'id="kbTableListing"' not in html
    assert 'id="jiraTableListing"' not in html
    assert 'id="internalTableListing"' not in html


def test_admin_tables_renders_register_modals_in_dom(seeded_app):
    """D4 — one registration flow: the four connector-specific register
    modals collapsed into ONE shared drawer (`#registerTableModal`,
    `_register_table_form.html`), opened via `RegisterTableForm.open(...)`
    from the `+ Register new table ▾` dropdown items. Edit modals are out
    of scope for D4 and stay in DOM unchanged. Tests for the shared form's
    fields live in test_admin_tables_ui_materialized /
    test_register_table_form.py."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="registerBqModal"' not in html
    assert 'id="registerKeboolaModal"' not in html
    assert 'id="registerDatabricksModal"' not in html
    assert 'id="registerSnowflakeModal"' not in html
    assert 'id="editBqModal"' in html
    assert 'id="editKeboolaModal"' in html
    assert 'id="registerTableModal"' in html
    assert "openRegisterModal('bigquery')" in html
    assert "openRegisterModal('keboola')" in html
    assert "openRegisterModal('databricks')" in html
    assert "openRegisterModal('snowflake')" in html
    assert "RegisterTableForm.open({" in html
    assert "js/register_table_form.js" in html


def test_databricks_only_instance_keeps_the_full_register_menu_reachable(seeded_app, monkeypatch):
    """The Databricks shortcut must not swallow the only path to the menu.

    On an instance whose ``data_source.type`` is ``databricks`` and whose
    connection registry holds nothing else, the primary button opens the
    Databricks drawer straight away — one click saved on the source an admin
    registers every time. That shortcut used to be the button's ONLY
    behaviour, which made the dropdown unreachable and with it the Jira docs
    link and the BigQuery / Keboola register items: registering any of them
    meant dropping to the API or the CLI.

    So the primary is a split button — a second segment that ALWAYS opens the
    full list, whatever the first one shortcuts to.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    class _OnlyDatabricks:
        def list(self):
            return [{"id": "dbx1", "name": "Warehouse", "source_type": "databricks"}]

    monkeypatch.setattr(
        "src.repositories.source_connections_repo",
        lambda: _OnlyDatabricks(),
        raising=False,
    )
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: {"data_source": {"type": "databricks", "databricks": {}}},
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        r = c.get("/admin/tables", headers=_auth(token))
        assert r.status_code == 200
        html = r.text
        # Precondition: this IS the configuration that arms the shortcut.
        assert 'data-source-type="databricks"' in html
        # Two body attrs, two questions. The connectedness rewrite added the
        # `connected_sources` union (registry ∪ real scalar ∪ credential
        # probes) for "is X reachable"; the registry-only
        # `connected_source_types` stayed, because it is what arms this
        # shortcut — a stray BigQuery credential must not disarm it.
        assert 'data-connected-sources="databricks"' in html
        assert 'data-connected-source-types="databricks"' in html

        # The second segment of the split button, and the handler it calls.
        assert 'id="registerNewTableMoreBtn"' in html
        assert 'onclick="openRegisterNewTableMenu(event)"' in html
        assert "function openRegisterNewTableMenu(" in html

        # That handler opens the menu unconditionally — no source sniffing,
        # no drawer shortcut inside it.
        opener = html.split("function openRegisterNewTableMenu(", 1)[1]
        opener = opener.split("\n    function ", 1)[0]
        assert "DATA_SOURCE_TYPE" not in opener
        assert "_dbxIsOnlyConnectedSource" not in opener
        assert "openRegisterModal" not in opener
        assert "registerNewTableMenu" in opener

        # …and the items it reveals are still all there.
        assert "openRegisterModal('bigquery')" in html
        assert "openRegisterModal('keboola')" in html
        assert "docs/connectors/jira.md" in html
    finally:
        reset_cache()


def test_registry_listing_renders_manage_access_button(seeded_app):
    """Each row in the package-centric listing has an Edit affordance.
    The Manage-access deep-link helper survives in the JS (used by other
    surfaces — kept callable). It now targets /admin/groups: grants are
    group-scoped, so the operator picks a group and the hash rides along
    to that group's Access tab."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register a table so the API will surface at least one row.
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "test_orders",
            "source_type": "keboola",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
    )

    r = c.get("/admin/tables", headers=auth)
    body = r.text
    # The manageAccess() helper still exists in the JS (deep-links to the
    # group list scoped to a table_id).
    assert "function manageAccess(" in body or "manageAccess =" in body
    # It targets the group list, which is where a grant target is chosen.
    assert "/admin/groups#table:" in body


def test_group_list_forwards_the_table_deep_link(seeded_app):
    """The workspace reads the deep link on load: it shows the "pick a
    group" banner and pre-filters the grant tree to that table.

    The link's shape changed with the surface. `/admin/tables` sent
    `#table:<id>` to a group LIST, which forwarded the hash onto whichever
    group row you clicked; there is no second page now, so the workspace
    takes `?resource=<type>:<id>` directly — and still rewrites the old
    fragment, so an existing bookmark lands filtered rather than ignored."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/access", headers={"Authorization": f"Bearer {token}"})
    body = r.text
    assert '"resource"' in body, "the workspace must read ?resource= on load"
    assert "#table:" in body, "the retired hash must still be rewritten"
    assert 'id="ax-pick"' in body, "the pick-a-group banner must be present"


def test_group_detail_applies_the_table_deep_link(seeded_app):
    """The far end of the deep link: a group is selected AND the grant tree
    is pinned to the table, on one page.

    This used to be two hops — the list, then that group's detail page —
    which is why both ends needed their own test. `?group=` and `?resource=`
    are read by the same pane now."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    groups = c.get("/api/admin/groups", headers=auth).json()
    assert groups, "seeded instance must have at least the system groups"
    gid = groups[0]["id"]

    # The retired detail URL keeps working, carrying the selection through.
    hop = c.get(f"/admin/groups/{gid}", headers=auth, follow_redirects=False)
    assert hop.status_code == 308
    assert hop.headers["location"] == f"/admin/access?group={gid}"

    r = c.get(f"/admin/access?group={gid}&resource=table:t1", headers=auth)
    body = r.text
    assert r.status_code == 200
    # Both deep-link params are read, and the grant matrix lives here.
    assert '"group"' in body and '"resource"' in body
    assert "/api/admin/access-overview" in body
    assert 'id="ax-resources"' in body


def test_admin_tables_shares_the_data_page_head(seeded_app):
    """Tables is a LENS of the Data section, not a page of its own. It
    rendered a gradient hero card titled "Tables" under a "DATA" eyebrow
    while Sources, Packages and Semantic layer all render the plain "Data"
    head — so one click changed the heading, its size and its treatment."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert "page-header--plain" in html
    assert "page-header--hero" not in html
    assert '<h1 class="page-header__title">Data</h1>' in html


def test_register_opens_in_the_shared_drawer(seeded_app):
    """Registering a table is a SETUP flow, and setup flows in this product
    open from the right — the same `.ds-drawer` chrome Connect-source, the
    Add-data wizard and the group editor use. D4 collapsed the two
    connector-specific register drawers this test used to check into ONE
    (`#registerTableModal`), generic across all four connectors."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'class="ds-drawer ds-drawer--wide" id="registerTableModal"' in html
    assert '<div class="modal-overlay" id="registerBqModal">' not in html
    assert '<div class="modal-overlay" id="registerKeboolaModal">' not in html
    assert "css/drawer.css" in html
    # The single form's fields — generic ids, not per-connector clones.
    for field in ('id="rtfViewName"', 'id="rtfCustomQuery"', 'id="rtfModeGroup"'):
        assert field in html, field


def test_admin_tables_supports_bulk_selection(seeded_app):
    """Assigning twelve tables to a package meant twelve trips through the
    row menu. Ticking rows reveals a contextual bar carrying the verbs that
    make sense for a SET — and only while a set exists."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'id="adminTablesBulk"' in html
    assert 'id="adminTablesCheckAll"' in html
    assert "tbl-rowcheck" in html
    for action in ('id="bulkAssign"', 'id="bulkSync"', 'id="bulkDelete"', 'id="bulkClear"'):
        assert action in html, action


def test_bulk_actions_float_and_leave_the_toolbar_alone(seeded_app):
    """The bulk bar is `.fbar-bulk` — the floating bar /chats runs, promoted
    into the shared component — pinned to the foot of the viewport.

    It used to be an in-flow tinted strip that REPLACED the toolbar in place
    (`syncBulkBar` set `fbar.hidden = true`), so ticking one row took search,
    Filter and Sort off the page — exactly when an operator is most likely to
    want to widen or re-narrow the set they are building.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'class="fbar-bulk" id="adminTablesBulk"' in html
    assert 'class="fbar-bulk__card"' in html
    # The page-local in-flow strip and its select-all are gone with it: the
    # table's own header checkbox is the one select-all there is.
    assert 'class="tbl-bulk"' not in html
    assert 'id="adminTablesBulkAll"' not in html
    # Nothing may hide the toolbar on selection again.
    assert "fbar.hidden" not in html
    # The list reserves the bar's clearance unconditionally — adding it when a
    # row is ticked would shift the list under the pointer that ticked it.
    assert "tbl-listwrap" in html

    css = (pathlib.Path(__file__).resolve().parents[1] / "app/web/static/css/filter_toolbar.css").read_text(
        encoding="utf-8"
    )
    for rule in (".fbar-bulk {", ".fbar-bulk__card {", ".fbar-bulk__btn {", ".fbar-bulk__clear {"):
        assert rule in css, rule


def test_admin_tables_filter_menu_is_two_level_with_chips(seeded_app):
    """Five families running to dozens of values (one option per Keboola
    bucket) was a single flat popover you scrolled. The menu lists FAMILIES
    and each opens its values beside it — `.fbar-menu--cats` + `.fbar-cat`,
    the Library's own two-level menu — and what is applied shows as removable
    chips on a row under the bar rather than as one number on the button.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert 'class="fbar-menu fbar-menu--cats" id="tbl-filter-menu"' in html
    assert 'class="fbar-chips" id="tbl-chips"' in html
    # The category rows and their per-family count badge are built from the
    # rows, so the vocabulary lives in the page's own renderer.
    assert 'class="fbar-cat" data-cat=' in html
    assert 'class="fbar-cat__pop"' in html
    assert "data-cat-count=" in html


def test_admin_tables_runs_the_shared_toolbar_engine(seeded_app):
    """`filter_toolbar.js` — the engine /library, /chats and the Add-tables
    drawer share. This page had a private copy of that job (its own facet
    matcher, comparator and badge bookkeeping), which is how one lens ends up
    filtering by rules nothing else in the product uses."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert "js/filter_toolbar.js" in html
    assert "FilterToolbar.init(" in html
    # The private engine's entry points are gone; what is left delegates.
    assert "function _tblActiveFacets" not in html
    assert "r.style.display = ok" not in html

    js = (pathlib.Path(__file__).resolve().parents[1] / "app/web/static/js/filter_toolbar.js").read_text(
        encoding="utf-8"
    )
    # A page whose rows AND menu come from a fetch has to be able to hand the
    # engine back before it mounts a new one, or every re-render leaves another
    # engine listening on `document`.
    assert "destroy: destroy," in js
    assert "function syncCatCounts" in js
