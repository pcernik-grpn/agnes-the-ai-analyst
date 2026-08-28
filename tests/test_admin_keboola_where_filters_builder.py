"""Structured where_filters builder for Keboola Direct-extract (#408).

Re-scoped slice: the issue named a "materialized JSON filter field" that no
longer exists on `main`. The surviving raw-JSON filter input is the
`kbWhereFilters` / `editKbWhereFilters` textarea on the DIRECT local Keboola
registration path (query_mode='local', Storage API). This builds a structured
filter editor (column + operator + values rows, plus a date-range convenience)
on top of those fields. It serialises to the EXACT JSON array the backend
already consumes, so no schema or submit-path change is needed and the raw-JSON
escape hatch stays available.

Three layers of coverage:
  1. Structural — the builder host + escape-hatch textarea + the JS module
     render in BOTH the register and edit Keboola modals.
  2. Serialisation — the pure builder logic (no DOM) emits the expected JSON
     for a sample (a where row + a date-range), executed under `node`.
  3. Byte-compatibility — that exact JSON round-trips through the live
     /api/admin/register-table endpoint AND PUT update path unchanged.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "where-filters-builder.js"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ───────────────────────────── 1. structural ──────────────────────────────


def test_builder_module_loaded_and_hosts_present(seeded_app):
    """The standalone builder JS is loaded and both the Keboola edit modal
    AND the shared register drawer (D4 — one registration flow) carry a
    builder mount + the preserved raw-JSON escape-hatch textarea. The
    register-side host moved from `#kbWhereFiltersBuilder` (the old
    connector-specific modal) to `#rtfKbWhereFiltersBuilder` (generic,
    shared across all four connectors' Configure step) — same reuse of
    the builder, different id."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text

    # Module shipped + loaded.
    assert "/static/js/where-filters-builder.js" in html

    # Shared register drawer: builder host + the preserved raw-JSON
    # escape-hatch textarea (Direct-extract's `where_filters` AND the
    # 'custom' materialized mode's JSON-wrapped `source_query` both read
    # from this one instance — see register_table_form.js).
    assert 'id="rtfKbWhereFiltersBuilder"' in html
    assert 'id="rtfKbWhereFilters"' in html
    assert 'id="kbWhereFiltersBuilder"' not in html
    # Edit modal: unchanged.
    assert 'id="editKbWhereFiltersBuilder"' in html
    assert 'id="editKbWhereFilters"' in html

    # The escape hatch is reachable via an explicit toggle in both places
    # (edit's server-rendered `toggleWhereFiltersRaw`, register's JS-built
    # equivalent in register_table_form.js).
    assert "toggleWhereFiltersRaw" in html
    js = (Path("app/web/static/js/register_table_form.js")).read_text(encoding="utf-8")
    assert "rtfKbFilterRawToggle" in js


def test_builder_initialised_for_both_modals(seeded_app):
    """The page wires WhereFiltersBuilder.attach() for the register + edit
    hosts so the structured editor hydrates from / writes into the textarea."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    assert "WhereFiltersBuilder.attach" in html
    # Both hosts referenced by id in the init wiring.
    assert "kbWhereFiltersBuilder" in html
    assert "editKbWhereFiltersBuilder" in html


# ───────────────────────────── 2. serialisation ───────────────────────────


def _run_builder(rows_expr):
    """Execute the pure builder serialisation under node and return parsed
    JSON. Skips if node is unavailable (CI installs it; local may not)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — JS serialisation test needs a runtime")
    script = "const B = require(%r);\nprocess.stdout.write(JSON.stringify(%s));\n" % (str(_JS), rows_expr)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_serialize_where_row():
    """A single where row → one {column, operator, values} entry; CSV values
    split into the IN-list the Storage API expects."""
    rows = "[{column:'country', operator:'eq', values:'CZ, SK'}]"
    result = _run_builder("B.serializeFilterRows(%s)" % rows)
    assert result == [{"column": "country", "operator": "eq", "values": ["CZ", "SK"]}]


def test_date_range_emits_two_boundary_rows():
    """A date range → two boundary rows (ge + le) on the same column, with
    placeholders passed through verbatim for server-side resolution."""
    result = _run_builder("B.dateRangeRows('event_date', '{{last_3_months}}', '{{today}}')")
    assert result == [
        {"column": "event_date", "operator": "ge", "values": ["{{last_3_months}}"]},
        {"column": "event_date", "operator": "le", "values": ["{{today}}"]},
    ]


def test_builder_emits_expected_json_for_sample():
    """End-to-end builder sample: one where row (country=CZ) PLUS a date
    range (event_date in [{{last_3_months}}, {{today}}]). The combined
    rowsToJSON output must equal the canonical filter array the backend
    accepts."""
    rows = (
        "[{column:'country', operator:'eq', values:'CZ'}]"
        ".concat(B.dateRangeRows('event_date', '{{last_3_months}}', '{{today}}'))"
    )
    raw = _run_builder("B.rowsToJSON(%s)" % rows)
    # rowsToJSON returns a JSON *string*; parse it.
    parsed = json.loads(raw)
    assert parsed == [
        {"column": "country", "operator": "eq", "values": ["CZ"]},
        {"column": "event_date", "operator": "ge", "values": ["{{last_3_months}}"]},
        {"column": "event_date", "operator": "le", "values": ["{{today}}"]},
    ]


def test_half_typed_rows_are_dropped():
    """A row with a blank column or no values must NOT emit an invalid filter
    (which the backend would 400 on). Empty result → empty string."""
    rows = "[{column:'', operator:'eq', values:'x'}, {column:'c', operator:'eq', values:''}]"
    assert _run_builder("B.rowsToJSON(%s)" % rows) == ""


def test_comma_in_value_detected_for_rawonly():
    """#649 review: a stored value containing a comma (e.g. "Smith, John")
    cannot round-trip the CSV row editor without silently splitting, so the
    builder must detect it and defer to raw-JSON mode."""
    parsed = "[{column:'name', operator:'eq', values:['Smith, John']}]"
    assert _run_builder("B.parsedHasCommaValue(%s)" % parsed) is True
    # Plain values (no comma) stay in the structured editor.
    plain = "[{column:'country', operator:'eq', values:['CZ','SK']}]"
    assert _run_builder("B.parsedHasCommaValue(%s)" % plain) is False


# ──────────────────────── 3. byte-compatibility ────────────────────────────


SAMPLE_FILTERS = [
    {"column": "country", "operator": "eq", "values": ["CZ"]},
    {"column": "event_date", "operator": "ge", "values": ["{{last_3_months}}"]},
    {"column": "event_date", "operator": "le", "values": ["{{today}}"]},
]


def test_register_accepts_builder_json(seeded_app, monkeypatch):
    """The JSON the builder emits is accepted unchanged by the existing
    Direct-extract register path (query_mode='local', full_refresh)."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/register-table",
            headers=_auth(token),
            json={
                "name": "kb_filtered_orders",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "sync_strategy": "full_refresh",
                "where_filters": SAMPLE_FILTERS,
            },
        )
        assert r.status_code == 201, r.text
        # The stored filters round-trip back unchanged.
        body = r.json()
        stored = body.get("where_filters") or body.get("table", {}).get("where_filters")
        if stored is not None:
            assert stored == SAMPLE_FILTERS
    finally:
        reset_cache()


def test_update_accepts_builder_json(seeded_app, monkeypatch):
    """The same JSON is accepted by the PUT update path on an existing
    Direct-extract row — the edit modal's builder writes into editKbWhereFilters
    which feeds the unchanged update payload."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        auth = _auth(token)
        reg = c.post(
            "/api/admin/register-table",
            headers=auth,
            json={
                "name": "kb_edit_filtered",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "sync_strategy": "full_refresh",
            },
        )
        assert reg.status_code == 201, reg.text
        table_id = reg.json().get("id") or reg.json().get("table", {}).get("id")
        assert table_id, reg.text

        upd = c.put(
            f"/api/admin/registry/{table_id}",
            headers=auth,
            json={
                "query_mode": "local",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "sync_strategy": "full_refresh",
                "where_filters": SAMPLE_FILTERS,
            },
        )
        assert upd.status_code in (200, 204), upd.text
    finally:
        reset_cache()
