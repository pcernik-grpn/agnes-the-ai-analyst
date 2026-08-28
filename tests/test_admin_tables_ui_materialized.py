"""`/admin/tables` register modal exposes the BQ Type selector + Custom SQL.

The backend supports `query_mode='materialized'` since v0.25.0. The Jinja
template at `app/web/templates/admin_tables.html` exposes it via an
operator-facing **Type** selector (Table / View / Custom SQL Query) that
maps to query_mode in the payload (Table+View → remote, Query → materialized).

Structural-only test (no headless browser): loads the template through the
running app and asserts the expected element ids + attributes are present
in the rendered HTML for a `data_source_type='bigquery'` deployment.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    """Force `data_source.type='bigquery'` so /admin/tables renders the BQ
    branch of the register modal."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    yield fake_cfg
    reset_cache()


def test_admin_tables_renders_two_question_radio_form(seeded_app, bq_instance):
    """D4 — one registration flow: the BQ-specific Q1 (live/synced) + Q2
    (whole/custom) radios documented here moved out of the static Jinja
    template entirely — they're now built by register_table_form.js's
    generic `#rtfModeGroup` renderer, driven by a per-connector `modes`
    config (BigQuery: 'live' / 'synced_whole' / 'synced_custom'), the same
    renderer every connector shares. Assert the shared skeleton is present
    and the JS asset carries BigQuery's mode config; per-connector radio
    ids/labels are exercised in test_register_table_form.py against the JS
    directly, not scraped from server-rendered HTML."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # No leftover jargon labels from the prior Type-selector iterations.
    assert "Direct query" not in html
    assert "Sync to parquet" not in html

    # Vendor-agnostic — no internal issue refs in operator-facing UI text.
    assert "Milestone 2" not in html
    assert "issue #108" not in html

    # The shared drawer's generic mode/query/bucket-table skeleton — one
    # instance in the DOM, not a BQ-specific clone.
    assert 'id="registerTableModal"' in html
    assert 'id="rtfModeGroup"' in html
    assert 'id="rtfCustomQuery"' in html
    assert "js/register_table_form.js" in html

    js = (Path("app/web/static/js/register_table_form.js")).read_text(encoding="utf-8")
    assert "bigquery:" in js
    assert "{ value: 'live', title: 'Live from BigQuery'" in js
    assert "{ value: 'synced_whole'" in js
    assert "{ value: 'synced_custom'" in js


def test_edit_modal_has_bq_parity_fields(seeded_app, bq_instance):
    """Edit modal mirrors Register's two-question radio model (Q1 access
    mode: live/synced; Q2 sync mode: whole/custom). Pre-fix Edit had only
    sync_strategy+primary_key+description+folder — missing all BQ-specific
    edit surface. Operator now can flip access mode, change dataset/table,
    rewrite SQL, and tweak the schedule without dropping & re-adding."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Edit Q1 + Q2 radios.
    assert 'name="editBqAccessMode"' in html
    assert 'name="editBqSyncMode"' in html
    assert "onEditBqAccessModeChange" in html
    assert "onEditBqSyncModeChange" in html

    # BQ-specific edit fields.
    assert 'id="editBqDataset"' in html
    assert 'id="editBqSourceTable"' in html
    assert 'id="editBqSourceQuery"' in html
    assert 'id="editBqSyncSchedule"' in html

    # Visibility classes for adaptive show/hide on access/sync mode switch.
    assert "bq-edit-access-synced" in html
    assert "bq-edit-source-table" in html
    assert "bq-edit-source-custom" in html

    # Mode-switch warning surface (filled by JS when operator flips access
    # mode mid-edit).
    assert 'id="editBqModeWarning"' in html

    # Source-type badge so the JS branch knows whether to render BQ vs
    # Keboola fields without a second round-trip.
    assert 'id="editSourceTypeBadge"' in html

    # No leftover Type-selector remnants.
    assert 'id="editBqEntityType"' not in html
    assert "onEditBqTypeChange" not in html

    # Edit modal has the same Discover / List tables / Use-as-base buttons
    # as Register so the operator can re-pick the source from autocomplete
    # without dropping the row.
    assert "discoverBqDatasets(this, 'editBqDatasetList')" in html
    assert "discoverBqTables(this, 'editBqDataset', 'editBqTableList')" in html
    assert "prefillFromTable('editBqSourceQuery')" in html
    assert 'id="editBqDatasetList"' in html
    assert 'id="editBqTableList"' in html
    assert 'list="editBqDatasetList"' in html
    assert 'list="editBqTableList"' in html


def test_keboola_register_form_has_three_question_radio(seeded_app, monkeypatch):
    """D4 — one registration flow: the Keboola register modal this test
    scraped is gone. Its four modes (whole / direct / custom / remote —
    'remote' is new, D4 normalized Keboola onto the same live/synced split
    every other connector offers) now live in register_table_form.js's
    `CONNECTORS.keboola` config, rendered by the same `#rtfModeGroup` +
    `#rtfKbStrategyPanel` the shared drawer uses for every connector.
    """
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
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

        # The shared drawer's Keboola-only strategy panel + filter builder —
        # rendered once, JS-toggled per mode, not cloned per connector.
        assert 'id="rtfKbStrategyPanel"' in html
        assert 'id="rtfKbStrategy"' in html
        assert 'id="rtfKbFilterField"' in html
        assert 'id="rtfPrimaryKey"' in html
        assert "<details" in html
        assert ">Advanced" in html

        js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
        for mode in ("whole", "direct", "custom", "remote"):
            assert f"value: '{mode}'" in js, f"Keboola mode {mode!r} missing from register_table_form.js"
        # Direct-extract keeps its v26 sync_strategy fields, wired through
        # the same shared drawer.
        assert "sync_strategy: s.kbStrategy" in js
        assert "incremental_window_days" in js
        assert "partition_by" in js

        # Discover still routes through the same endpoints (browse step).
        assert "/api/admin/discover-tables" in js
        assert "/tables'" in js  # source-connections/{id}/tables
    finally:
        reset_cache()


def test_keboola_register_payload_maps_to_materialized(seeded_app, monkeypatch):
    """The form's whole-table mode posts query_mode='materialized' — Keboola
    materialized uses bucket/source_table (no SQL source_query)."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
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
        auth = {"Authorization": f"Bearer {token}"}
        r = c.post(
            "/api/admin/register-table",
            headers=auth,
            json={
                "name": "orders",
                "source_type": "keboola",
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "sync_schedule": "every 6h",
            },
        )
        assert r.status_code == 201, r.text
    finally:
        reset_cache()


def test_keboola_edit_modal_parity(seeded_app, monkeypatch):
    """Phase G (v26): Edit modal mirrors Register's three-question structure
    (whole | direct | custom) for Keboola rows.

    Phase F asserted `editKbStrategy` was removed; v26 re-adds it inside
    the Direct-extract panel for the same reason as the Register form."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
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
        # Q2 radio in edit (now three modes).
        assert 'name="editKbSyncMode"' in html
        assert 'id="editKbBucket"' in html
        assert 'id="editKbSourceTable"' in html
        assert 'id="editKbSourceQuery"' in html
        assert 'id="editKbSyncSchedule"' in html
        # Discover/List/Use-as-base buttons mirror Register.
        assert "discoverKeboolaBuckets(this, 'editKbBucketList')" in html
        assert "discoverKeboolaTables(this, 'editKbBucket', 'editKbTableList')" in html
        assert "prefillFromKeboolaTable('editKbSourceQuery')" in html
        # v26: Strategy dropdown re-added inside Direct-extract panel
        assert 'id="editKbStrategy"' in html
        assert "editkb-direct-only" in html
        assert 'id="editKbPrimaryKey"' in html
    finally:
        reset_cache()


def test_bq_edit_modal_renders_as_dom_overlay(seeded_app, bq_instance):
    """Package-centric rewrite: the connector tab that used to wrap
    #editBqModal was dropped, but the modal itself stays in DOM as a
    top-level overlay reachable from the per-row Edit affordance. Old
    shared #editModal still exists but carries no BQ-specific fields."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # BQ edit modal is in the DOM (as a top-level overlay now).
    assert 'id="editBqModal"' in html
    assert 'id="editBqDataset"' in html
    assert 'id="editBqSourceQuery"' in html
    # Old shared #editModal either gone or only carries non-BQ fields.
    if 'id="editModal"' in html:
        edit_modal_start = html.index('id="editModal"')
        # rough lookahead: scan until the next modal-overlay sibling or </body>
        edit_modal_end = (
            html.index('id="toast"', edit_modal_start) if 'id="toast"' in html[edit_modal_start:] else len(html)
        )
        edit_modal = html[edit_modal_start:edit_modal_end]
        assert 'id="editBqDataset"' not in edit_modal  # BQ fields aren't here anymore


def test_keboola_discover_buttons_disabled_on_bigquery_instance(seeded_app, monkeypatch):
    """C1 / #405, migrated for the one-signal fix (see
    test_admin_tables_connectedness.py): Discover/List/Use-as-base buttons
    in the Keboola tab render DISABLED with an explanatory tooltip (rather
    than being hidden) when Keboola is unreachable by BOTH routes — no
    `source_connections` registry row (none in this fixture) AND a
    non-keboola `data_source.type` scalar.

    Pre-fix this asserted on `data_source_type != 'keboola'` ALONE, which
    pinned the reported bug: it fired even on instances with a Keboola
    project connected through the registry, on the adjacent Sources tab.
    The guard now reads `connected_sources` (the sibling-shipped union of
    the registry + the legacy scalar + instance-side credential probes) —
    this test asserts the case where that list genuinely omits 'keboola',
    injected explicitly via `_inject_ctx` so the assertion doesn't
    silently depend on whether app/web/router.py has shipped the real key
    yet (`setdefault` — a real value always wins over the shim)."""
    from tests.test_admin_tables_connectedness import _inject_ctx

    fake_cfg = {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    _inject_ctx(monkeypatch, connected_sources=["bigquery"])
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # D4 removed the register-side Keboola discover buttons this test
        # used to check (register_table_form.js's Browse step tries the
        # discovery call unconditionally and surfaces a failure inline via
        # `#rtfBrowseError` instead of pre-emptively disabling a button —
        # there's no connectedness signal to gate on client-side before the
        # shared drawer even knows which source_type the operator picked).
        # The EDIT Keboola modal's #405 disabled+tooltip guard is unchanged
        # and still scoped here.
        assert 'id="editKbBucket"' in html
        assert 'id="editKbSourceTable"' in html
        assert 'data-tooltip="Keboola not connected' in html
        # #347 follow-up: the tooltip's advice must be FOLLOWABLE — the old
        # copy pointed at a token field /admin/server-config never had.
        assert "connect a project in Data sources" in html
        assert "set token in Instance settings" not in html
        assert "onclick=\"discoverKeboolaBuckets(this, 'editKbBucketList')\"" not in html
        assert "onclick=\"discoverKeboolaTables(this, 'editKbBucket', 'editKbTableList')\"" not in html
        assert "onclick=\"prefillFromKeboolaTable('editKbSourceQuery')\"" not in html
    finally:
        reset_cache()


def test_keboola_discover_buttons_visible_on_keboola_instance(seeded_app, monkeypatch):
    """Inverse — buttons render on a Keboola-typed instance."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
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
        assert "discoverKeboolaBuckets" in html
        assert "discoverKeboolaTables" in html
        assert "prefillFromKeboolaTable" in html
    finally:
        reset_cache()


def test_keboola_test_connection_button_in_register_and_edit_modals(seeded_app):
    """#402: the Keboola EDIT modal exposes a Test-connection button wired
    to the existing /api/admin/keboola/test-connection probe, with an
    inline result element and a self-contained onTestKeboola handler.

    D4 dropped the second (register-modal) copy of this button along with
    the modal it lived in — the shared register drawer doesn't offer a
    Test-connection probe in this first cut (deferred; the connection
    picker itself still surfaces a failed discovery inline via
    `#rtfBrowseError`)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert html.count('onclick="onTestKeboola(this)"') == 1
    assert "Test connection" in html
    assert 'class="kbc-test-result"' in html
    assert "function onTestKeboola(" in html
    assert "/api/admin/keboola/test-connection" in html
    assert "editKbcResult.hidden = true" in html


def test_admin_tables_keboola_branch_unchanged(seeded_app, monkeypatch):
    """D4 — one registration flow: BigQuery and Keboola no longer render
    separate per-connector forms at all (the legacy Type-selector remnant
    and the pre-D4 #registerModal / #registerKeboolaModal are all gone the
    same way) — every source_type opens the ONE shared drawer, so a
    Keboola-typed instance still renders it (and register_table_form.js's
    connector config) regardless of data_source.type."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    try:
        r = c.get("/admin/tables", headers=_auth(token))
        assert r.status_code == 200, r.text
        html = r.text
        # Legacy Type-selector remnant must stay gone.
        assert 'id="bqEntityType"' not in html
        # C3/D4: legacy #registerModal AND the later per-connector modals
        # are both gone; the ONE shared drawer renders regardless of
        # data_source.type.
        assert 'id="registerModal"' not in html
        assert 'id="registerKeboolaModal"' not in html
        assert 'id="registerBqModal"' not in html
        assert 'id="registerTableModal"' in html
        assert "openRegisterModal('bigquery')" in html
        assert "openRegisterModal('keboola')" in html
    finally:
        reset_cache()


def test_register_table_form_surfaces_readable_row_errors():
    """D4 collapsed the old two-step precheck→confirm flow (BQ/Snowflake
    only; Keboola/Databricks already single-POSTed) into ONE path for every
    connector: `RegisterTableForm.submit()` POSTs `/api/admin/register-table`
    directly per checked row and renders each row's own result. Per-field
    red-box pinning (`_applyFieldErrors`/`*_REGISTER_FIELD_MAP`) didn't carry
    forward — deferred; a failed row still gets a readable per-row message
    (never a bare `[object Object]`) because it goes through the same
    `detail`-unwrapping helper (`window.apiDetailText`) the rest of the app
    uses for FastAPI error shapes, not string-concatenated directly."""
    js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
    assert "async function submit()" in js
    assert "'/api/admin/register-table'" in js
    assert "_errText(res.data && res.data.detail" in js
    assert "typeof window.apiDetailText === 'function'" in js
    # Never a bare `+ res.data.detail +`/`String(detail)` shortcut that would
    # print "[object Object]" for the dict-shaped 422/409 detail FastAPI
    # sends — the whole reason `_apiErrorMessage`/`apiDetailText` exist.
    assert "+ res.data.detail" not in js


def test_nested_confirm_prompt_dialog_outranks_the_register_drawer(seeded_app, bq_instance):
    """`Use as base` inside a register drawer awaits an invisible dialog.

    modal.js's confirmModal / promptModal render a `.modal-backdrop` fixed at
    z-index 1000 (style-custom.css), but the register drawers here are
    `.ds-drawer` at 1200 (css/drawer.css) and `prefillFromTable` /
    `prefillFromKeboolaTable` are invoked from buttons *inside* them — so the
    dialog the flow then awaits painted behind the panel that opened it and
    the button looked dead. The page-scoped override must clear the drawer
    and stay under the toast.
    """
    import re
    from pathlib import Path

    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    def _z(css: str, selector: str) -> int:
        parts = css.split(selector + " {", 1)
        assert len(parts) == 2, f"no `{selector} {{` rule found — this guard has nothing to compare"
        m = re.search(r"z-index:\s*(\d+)", parts[1].split("}", 1)[0])
        assert m, f"{selector} lost its z-index — this guard has nothing to compare"
        return int(m.group(1))

    drawer_z = _z(Path("app/web/static/css/drawer.css").read_text(encoding="utf-8"), ".ds-drawer")
    backdrop_z = _z(html, ".modal-backdrop")
    toast_z = _z(html, ".toast")

    assert backdrop_z > drawer_z, (
        f".modal-backdrop ({backdrop_z}) must outrank .ds-drawer ({drawer_z}) — "
        "confirmModal/promptModal are opened from inside the register drawers"
    )
    assert backdrop_z < toast_z, f".modal-backdrop ({backdrop_z}) must stay under .toast ({toast_z})"


def test_keboola_whole_table_payload_is_not_rejected_by_the_register_validator(seeded_app, monkeypatch):
    """The Keboola drawer's DEFAULT mode must post a payload the server accepts.

    `_buildKeboolaPayload` / `_buildKeboolaEditPayload` synthesized
    ``SELECT * FROM kbc."<bucket>"."<table>"`` into ``source_query`` for the
    whole-table branch, but a Keboola *materialized* row exports through the
    Storage API, whose filter is a JSON spec — so
    ``RegisterTableRequest._check_mode_query_coherence`` refuses a
    source_query starting with SELECT/WITH ("must be a JSON filter spec …,
    not SQL"), and `connectors/keboola/extractor.py` raises on the same shape
    at sync time. "Whole table (extension)" is the pre-checked radio, so every
    default Keboola registration 422d; before `_apiErrorMessage` landed the
    operator saw that as the literal string "[object Object]", which is how a
    dead register path stayed invisible.

    Both halves are pinned: the model must still reject the synthesized SQL
    (so this is not silently "fixed" by relaxing the server), and neither
    payload builder may produce it.
    """
    from app.api.admin import RegisterTableRequest

    with pytest.raises(ValidationError) as exc:
        RegisterTableRequest(
            name="orders",
            source_type="keboola",
            query_mode="materialized",
            bucket="in.c-sales",
            source_table="orders",
            source_query='SELECT * FROM kbc."in.c-sales"."orders"',
        )
    assert "JSON filter spec" in str(exc.value)

    # Edit modal — unchanged by D4, still guarded here.
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    start = html.index("function _buildKeboolaEditPayload(")
    end = html.index("\n    function ", start)
    assert "source_query: 'SELECT * FROM kbc." not in html[start:end], (
        "_buildKeboolaEditPayload still synthesizes SQL into source_query for "
        "the whole-table branch — the server rejects exactly that payload"
    )

    # D4's register drawer: the SAME invariant, now in register_table_form.js
    # (CONNECTORS.keboola.buildPayload). The 'whole' branch never sets
    # source_query at all (materialized + bucket/source_table only — a NULL
    # source_query means Storage API full-table export), so there's nothing
    # to synthesize SQL into in the first place.
    js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
    kb_start = js.index("keboola: {")
    kb_end = js.index("\n    bigquery: {", kb_start)
    kb_config = js[kb_start:kb_end]
    assert "source_query: 'SELECT * FROM kbc." not in kb_config
    whole_start = kb_config.rindex("// whole")
    whole_branch = kb_config[whole_start:]
    assert "source_query" not in whole_branch, (
        "Keboola's 'whole' mode payload must not set source_query — a NULL "
        "source_query is what a full-table Storage API export means"
    )


def test_keboola_edit_back_to_whole_table_clears_the_stored_source_query(seeded_app, monkeypatch):
    """Switching a Keboola row back to "Whole table" must CLEAR source_query.

    `update_table` overlays `request.model_dump(exclude_unset=True)` onto the
    stored row, so an *omitted* `source_query` means "keep the existing value",
    not "clear it". A row edited from custom back to whole table would then
    keep exporting the old filter spec while the drawer claimed a full-table
    copy — and a row still carrying the legacy synthesized
    ``SELECT * FROM kbc."b"."t"`` would wedge every PUT on the merged-record
    guard (`app/api/admin.py`, "must be a JSON filter spec"), unfixable from
    this drawer, because neither branch could clear the field. An explicit
    ``null`` is accepted by `UpdateTableRequest` and persists as NULL; an empty
    string is rejected outright.
    """
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        auth = _auth(seeded_app["admin_token"])

        r = c.post(
            "/api/admin/register-table",
            headers=auth,
            json={
                "name": "orders_filtered",
                "source_type": "keboola",
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "source_query": '{"columns": ["id", "created_at"]}',
            },
        )
        assert r.status_code == 201, r.text
        table_id = r.json()["id"]

        # Exactly what _buildKeboolaEditPayload's whole-table branch posts.
        r = c.put(
            f"/api/admin/registry/{table_id}",
            headers=auth,
            json={
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "source_query": None,
            },
        )
        assert r.status_code == 200, r.text

        rows = c.get("/api/admin/registry", headers=auth).json()
        row = next(t for t in (rows if isinstance(rows, list) else rows["tables"]) if t["id"] == table_id)
        assert not (row.get("source_query") or "").strip(), (
            f"switching back to whole-table left the previous filter spec on the row — got {row.get('source_query')!r}"
        )

        html = c.get("/admin/tables", headers=auth).text
        start = html.index("function _buildKeboolaEditPayload(")
        end = html.index("\n    function ", start)
        assert "source_query: null," in html[start:end], (
            "_buildKeboolaEditPayload's whole-table branch must send an EXPLICIT null; "
            "omitting the key makes the PUT keep the stored value"
        )
    finally:
        reset_cache()
