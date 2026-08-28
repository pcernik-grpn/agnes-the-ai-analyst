"""D4 — one registration flow: the shared register drawer.

Collapses the four near-duplicate connector register modals
(`#registerBqModal` / `#registerKeboolaModal` / `#registerDatabricksModal` /
`#registerSnowflakeModal`, formerly in admin_tables.html) and the onboarding
wizard's silent bulk auto-register into ONE two-pane drawer
(`app/web/templates/_register_table_form.html` + `app/web/static/js/
register_table_form.js`), opened from both entry points via
`RegisterTableForm.open({sourceType})`.

Covers:
  1. The shared drawer renders once on /admin/tables (structural).
  2. register_table_form.js carries a connector config for all four
     connectors, each with a working `buildPayload`.
  3. Keboola's "Custom SQL" mode 422'd on every submit pre-D4 (a
     materialized row's source_query is a Storage API JSON filter, not
     SQL) — a fail→pass pair proving the fix.
  4. The onboarding wizard (setup.html) routes table registration through
     the validated `POST /api/admin/register-table`, not the
     `discover-and-register` bypass — a payload that would fail validation
     (view-name collision) is rejected with a clear error, matching what
     the wizard now calls into.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_JS = Path("app/web/static/js/register_table_form.js")
_PARTIAL = Path("app/web/templates/_register_table_form.html")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 1 & 2: structural + connector config ────────────────────────────────


def test_shared_partial_and_js_asset_exist():
    assert _PARTIAL.exists()
    assert _JS.exists()


def test_admin_tables_includes_the_shared_drawer_once(seeded_app):
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert html.count('id="registerTableModal"') == 1
    assert "js/register_table_form.js" in html


def test_all_four_connectors_have_a_config_and_buildpayload():
    js = _JS.read_text(encoding="utf-8")
    for source_type in ("keboola", "bigquery", "databricks", "snowflake"):
        assert f"{source_type}: {{" in js, f"missing CONNECTORS.{source_type}"
    # Each connector config defines its own buildPayload — not one shared
    # function silently defaulting for a connector nobody wired.
    assert js.count("buildPayload(row, mode, s)") == 4


def test_design_system_contract_still_covers_the_new_component():
    """The new `ds.drawer_field`/`ds.drawer_select`/`ds.drawer_checkbox`
    macros exist and are token-only — a stricter, focused re-check of what
    tests/test_design_system_contract.py already sweeps repo-wide."""
    components = Path("app/web/templates/_components.html").read_text(encoding="utf-8")
    for macro in ("drawer_field", "drawer_select", "drawer_checkbox"):
        assert f"macro {macro}(" in components
    partial = _PARTIAL.read_text(encoding="utf-8")
    assert "<style" not in partial, "the shared partial must not carry inline CSS — see drawer.css"


# ── 3: Keboola custom-filter fix — fail (old shape) → pass (new shape) ──


def test_keboola_custom_sql_mode_was_broken_pre_d4(seeded_app):
    """Pins the bug D4 fixes: a Keboola *materialized* row's `source_query`
    is a Storage API JSON filter (`ExportFilter`), never SQL —
    `RegisterTableRequest._check_mode_query_coherence` refuses a SELECT/WITH
    string outright. This is exactly the payload the pre-D4 "Custom SQL"
    mode sent (see the removed TODO(keboola-custom-mode) in
    admin_tables.html)."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "orders_custom_broken",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "source_query": 'SELECT * FROM kbc."in.c-sales"."orders" WHERE date >= CURRENT_DATE - 30',
        },
    )
    assert r.status_code == 422, r.text
    assert "JSON filter spec" in r.text


def test_keboola_filtered_export_mode_registers_successfully(seeded_app):
    """The D4 fix: register_table_form.js's Keboola 'custom' mode
    (relabeled "Filtered export") builds a `where_filters`-shaped JSON
    object for `source_query` — via the SAME structured builder Direct-
    extract already used (#408) — and ALSO keeps `bucket`/`source_table`
    on the payload (materialize_query() needs them regardless of
    source_query; the pre-D4 builder dropped them for this mode too, which
    would have 500'd at the next sync tick even after a JSON-shape fix).
    This is the exact shape `CONNECTORS.keboola.buildPayload(row, 'custom',
    s)` sends — see register_table_form.js."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "orders_custom_fixed",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "source_query": '{"where_filters": [{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]}',
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    table_id = body.get("id") or "orders_custom_fixed"

    rows = c.get("/api/admin/registry", headers=_auth(seeded_app["admin_token"])).json()
    rows = rows if isinstance(rows, list) else rows["tables"]
    row = next(t for t in rows if t["id"] == table_id)
    assert row["bucket"] == "in.c-sales"
    assert row["source_table"] == "orders"
    assert row["query_mode"] == "materialized"


def test_keboola_filtered_export_mode_with_no_filters_registers_as_full_export(seeded_app):
    """An empty filter list (operator opened "Filtered export" but never
    added a row) must degenerate to NULL source_query — a full-table
    export, same as "Whole table" — not an empty-but-truthy JSON blob that
    would (harmlessly, but pointlessly) round-trip through ExportFilter."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "orders_custom_empty",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "source_query": None,
        },
    )
    assert r.status_code == 201, r.text


# ── 4: onboarding routes through the validated endpoint ─────────────────


def test_setup_wizard_no_longer_calls_the_discover_and_register_bypass():
    """setup.html's step-3 `discoverTables()` used to POST
    `/api/admin/discover-and-register`, which calls
    `table_registry_repo().register()` directly — bypassing every
    `RegisterTableRequest` validator (view-name collisions, unsafe
    identifiers, source-type availability, access-policy conflicts, …).
    D4 repoints it at the shared drawer, which always goes through the
    validated `POST /api/admin/register-table`."""
    html = Path("app/web/templates/setup.html").read_text(encoding="utf-8")
    # The comment explaining the fix legitimately names the old endpoint;
    # the check that matters is there's no live call site (fetch/apiCall)
    # against it.
    assert "fetch('/api/admin/discover-and-register'" not in html
    assert 'fetch("/api/admin/discover-and-register"' not in html
    assert "RegisterTableForm.open(" in html
    assert "js/register_table_form.js" in html
    assert "rtfSelectAll" in _JS.read_text(encoding="utf-8")


def test_a_registration_the_bypass_would_have_allowed_is_now_rejected(seeded_app):
    """The concrete gap D4 closes: `_discover_and_register_tables` calls
    `table_registry_repo().register()` directly, with NO `RegisterTableRequest`
    validation at all — including the identifier-safety check `register_table`
    enforces (`[a-z_][a-z0-9_]*`, no hyphens). The bypass's own id-slug
    (`full_id.lower().replace(".", "_").replace(" ", "_")`) does not strip
    hyphens, so a routine Keboola bucket name like `in.c-sales` slugs to
    `in_c-sales_orders` — a hyphenated id `register_table`'s validator
    refuses outright. Routed through the validated endpoint (what the
    wizard now does), the same discovered name comes back 422, not a
    silently-written broken row."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "in_c-sales_orders",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
        },
    )
    assert r.status_code == 422, r.text
    assert "unsafe identifier" in r.text


def test_admin_data_sources_register_flow_still_targets_the_validated_endpoint():
    """D4 explicitly did NOT fold /admin/data-sources's own register flow
    onto the shared drawer this PR adds — it's a different step in the
    funnel (connect + browse a NEW source, before any table exists to pick
    from) with its own rich per-row bucket/status DOM, and it already POSTs
    through the validated endpoint (never a bypass), so there's no
    correctness gap to close. Deferred, documented in the D4 PR
    description as a follow-up: point its register action at
    /admin/tables's shared drawer instead of registering inline, so there
    are genuinely 2 registration UIs, not 3. This guard only pins that the
    (unchanged) flow still targets the validated endpoint, so a future
    change to admin_data_sources.html can't quietly repoint it."""
    html = Path("app/web/templates/admin_data_sources.html").read_text(encoding="utf-8")
    assert 'API_REGISTER_TABLE = "/api/admin/register-table"' in html


# ── 5: the selection can never outrun what the operator can see ─────────
#
# Both bugs below share one shape: `submit()` reads `state.selectedKeys`,
# never the DOM, so a key that survives in that map is registered whether or
# not any row on screen corresponds to it. Run the SHIPPED helpers under node
# against a stubbed document — same `_node_run` pattern as
# tests/test_chat_files_drawer_ui.py; there is no DOM harness in CI.


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the selection helpers need a runtime")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _selection_slice() -> str:
    """The shipped selection helpers plus the two handlers that read them.

    Sliced from the source rather than copied so these tests exercise the real
    code. `renderTableList` sits between the two ranges and is DOM-bound —
    excluded here and stubbed in the harness, since what these tests assert is
    which keys survive in `state.selectedKeys`, not what got painted.
    """
    js = _JS.read_text(encoding="utf-8")
    helpers = js[js.index("  function _allRows() {") : js.index("  function renderTableList() {")]
    handlers = js[js.index("  function toggleRow(key, checked) {") : js.index("  function addManualRow() {")]
    return helpers + "\n" + handlers


_HARNESS = """
let searchValue = '';
let selectAllChecked = false;
const renders = [];
function renderTableList() { renders.push(1); }
function _updateSelectionCount() {}
const document = {
  getElementById(id) {
    if (id === 'rtfSearch') return { value: searchValue };
    if (id === 'rtfSelectAll') {
      return {
        get checked() { return selectAllChecked; },
        set checked(v) { selectAllChecked = v; },
      };
    }
    return null;
  },
};
function row(key, sourceTable) { return { key: key, name: sourceTable, sourceTable: sourceTable }; }
let state;
"""


def _run_selection(setup: str, report: str) -> dict:
    script = (
        _HARNESS
        + _selection_slice()
        + "\n"
        + setup
        + "\nprocess.stdout.write(JSON.stringify("
        + report
        + "));\n"
    )
    return json.loads(_node_run(script))


def test_select_all_only_toggles_rows_the_filter_leaves_visible():
    """Pre-fix `onSelectAllChange` looped over `_allRows()` — the whole
    discovered catalog — while the list showed only the search matches. The
    hidden rows are never rendered, so nothing in the UI reveals them before
    `submit()` registers the lot."""
    result = _run_selection(
        """
        state = {
          groups: [{ key: 'g', label: 'g', tables: [row('a', 'orders'), row('b', 'orders_archive'), row('c', 'users')] }],
          manualRows: [], selectedKeys: {}, rowByKey: {},
        };
        searchValue = 'orders';
        selectAllChecked = true;
        onSelectAllChange();
        """,
        "Object.keys(state.selectedKeys).filter(function (k) { return state.selectedKeys[k]; }).sort()",
    )
    assert result == ["a", "b"], (
        "Select all under an active filter must not reach the rows the filter hides — "
        f"got {result}, which includes the unfiltered 'users' row"
    )


def test_select_all_box_reflects_the_visible_subset_not_the_whole_catalog():
    """The companion to the bug above: with every visible row checked the box
    must read checked, even while unfiltered rows stay unselected. Computing
    it over `_allRows()` left the box unchecked and invited a second click
    that then selected everything."""
    result = _run_selection(
        """
        state = {
          groups: [{ key: 'g', label: 'g', tables: [row('a', 'orders'), row('c', 'users')] }],
          manualRows: [], selectedKeys: { a: true }, rowByKey: {},
        };
        searchValue = 'orders';
        """,
        "_allVisibleSelected()",
    )
    assert result is True


def test_switching_source_drops_discovered_selections_and_keeps_manual_ones():
    """A checkmark set under the previous Keboola connection is invisible after
    the reload but still live in `state.selectedKeys`, so `submit()` registers
    it — against the newly selected `connection_id`, i.e. the wrong project.
    Manually-added rows are not part of the discovered set and must survive."""
    result = _run_selection(
        """
        state = {
          groups: [{ key: 'g', label: 'g', tables: [row('proj1.orders', 'orders')] }],
          manualRows: [row('__manual.buck.tbl', 'tbl')],
          selectedKeys: { 'proj1.orders': true, '__manual.buck.tbl': true },
          rowByKey: { 'proj1.orders': row('proj1.orders', 'orders'), '__manual.buck.tbl': row('__manual.buck.tbl', 'tbl') },
        };
        _dropDiscoveredSelection();
        """,
        "{selected: Object.keys(state.selectedKeys).sort(), resolvable: Object.keys(state.rowByKey).sort()}",
    )
    assert result == {"selected": ["__manual.buck.tbl"], "resolvable": ["__manual.buck.tbl"]}, (
        "the discovered row must be dropped from BOTH maps — surviving in rowByKey alone "
        "still lets _selectedRows() resolve it if anything re-checks the key"
    )


def test_the_reload_path_calls_the_drop():
    """The helper is only worth having if the reload actually invokes it.
    `_reloadDiscovery` is async and network-bound, so pin the call site at the
    source level rather than executing it."""
    js = _JS.read_text(encoding="utf-8")
    body = js[js.index("async function _reloadDiscovery()") : js.index("function _showBrowseError")]
    assert "_dropDiscoveredSelection();" in body, (
        "_reloadDiscovery must clear the discovered selection — it is the one path both "
        "onConnectionChange and loadLocation funnel through"
    )


# ── 6: Keboola discover feeds the registry contract, not the display id ──


def _keboola_discover_slice() -> str:
    """The Keboola connector's `discover` plus the `_sanitizeName` /
    `_fmtCount` helpers it calls, sliced from the shipped source."""
    js = _JS.read_text(encoding="utf-8")
    sanitize = js[js.index("  function _sanitizeName(") : js.index("  function _sanitizeName(") + 600]
    sanitize = sanitize[: sanitize.index("\n  }\n") + 5]
    discover = js[js.index("      async discover(ctx) {") : js.index("      buildPayload(row, mode, s) {")]
    return sanitize + "\nfunction _fmtCount(n) { return String(n); }\n" + "const kb = {\n" + discover + "};\n"


def test_keboola_connection_browse_registers_the_bare_table_name():
    """Keboola's `t.id` is the full table id (`in.c-main.orders`); `t.name` is
    the bare in-bucket name. The registry keeps bucket and bare name in
    separate columns and composes `kbc.<bucket>.<source_table>` at export, so
    `source_table` must be the bare one — the full id is the #755-era wizard
    bug `storage_api.normalize_source_table` heals at use.

    Healing does not reach the view NAME: sanitizing the full id would show
    the analyst `in_c_main_orders`. Shape of the stubbed response is copied
    from the endpoint's own fixture in tests/test_admin_source_connections.py.
    """
    script = (
        "function _apiGet() { return Promise.resolve({buckets: [{id: 'in.c-main', name: 'main', "
        "tables: [{id: 'in.c-main.orders', name: 'orders', rows: 42}]}]}); }\n"
        + _keboola_discover_slice()
        + "kb.discover({connectionId: 'c1'}).then(function (g) {"
        " process.stdout.write(JSON.stringify(g[0].tables[0])); });\n"
    )
    row = json.loads(_node_run(script))
    assert row["sourceTable"] == "orders", (
        "source_table must be the bare in-bucket name — the export path composes "
        f"kbc.<bucket>.<source_table>, so {row['sourceTable']!r} would double the bucket prefix"
    )
    assert row["name"] == "orders", (
        "the analyst-visible view name is sanitized from this — the full id would surface "
        f"as 'in_c_main_orders'; got {row['name']!r}"
    )
    assert row["bucket"] == "in.c-main"
    assert row["key"] == "in.c-main.orders", "the row key doubled the bucket prefix too"
