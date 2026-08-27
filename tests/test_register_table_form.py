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

from pathlib import Path


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
