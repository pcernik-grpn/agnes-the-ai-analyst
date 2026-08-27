"""End-to-end tests for v26 Keboola sync-strategy support.

Coverage matrix:
- HTML: admin_tables.html contains the new Direct-extract radio + v26 panel + JS handlers
- API: POST /api/admin/register-table accepts each v26 strategy + persists v26 fields
- API: PUT /api/admin/registry/{id} updates v26 fields, switches strategies, clears stale values
- API: conflict policy → 422 on incremental+filters and partitioned+remote
- Roundtrip: registered v26 table comes back from GET /api/admin/registry with all fields
- Module: registered v26 table_config flows through extractor.run() dispatcher
  to extract_incremental / extract_partitioned (with mocked SDK)
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


# ───────────────────────────── HTML form structure ────────────────────────────

# D4 — one registration flow: the register-side Direct-extract radio + v26
# panel moved out of admin_tables.html's own markup (kbStrategy, kb-direct-
# only, ...) into the shared drawer partial, rendered generically as
# rtfKbStrategy / rtfKbStrategyPanel. The Edit modal's own copy
# (editKbStrategy, editkb-direct-only, ...) is unchanged.
HTML = Path("app/web/templates/admin_tables.html").read_text()
REGISTER_PARTIAL_HTML = Path("app/web/templates/_register_table_form.html").read_text()
REGISTER_JS = Path("app/web/static/js/register_table_form.js").read_text()


def test_html_register_modal_has_direct_extract_radio():
    assert "value: 'direct'" in REGISTER_JS
    assert "Direct extract (Storage API)" in REGISTER_JS


def test_html_has_kb_strategy_dropdown():
    assert 'id="rtfKbStrategy"' in REGISTER_PARTIAL_HTML
    assert 'id="editKbStrategy"' in HTML
    for option in ("full_refresh", "incremental", "partitioned"):
        assert f'value="{option}"' in REGISTER_PARTIAL_HTML


def test_html_has_v26_inputs():
    """Every v26 field must be wired in both the shared register drawer and
    the edit modal."""
    for kid in [
        "rtfKbIncrementalWindowDays",
        "rtfKbMaxHistoryDays",
        "rtfKbPartitionBy",
        "rtfKbPartitionGranularity",
        "rtfKbInitialLoadChunkDays",
        "rtfKbWhereFilters",
    ]:
        assert f'id="{kid}"' in REGISTER_PARTIAL_HTML, f"v26 input missing: {kid}"
    for kid in [
        "editKbIncrementalWindowDays",
        "editKbMaxHistoryDays",
        "editKbPartitionBy",
        "editKbPartitionGranularity",
        "editKbInitialLoadChunkDays",
        "editKbWhereFilters",
    ]:
        assert f'id="{kid}"' in HTML, f"v26 input missing: {kid}"


def test_html_visibility_classes_match_js_handlers():
    # Register (shared drawer): JS-driven visibility (register_table_form.js
    # toggles `hidden` directly), not CSS class selectors — see
    # onKbStrategyChange / _applyModeVisibility.
    for fn in ("onKbStrategyChange", "_applyModeVisibility"):
        assert f"function {fn}(" in REGISTER_JS, f"visibility handler missing: {fn}"
    # Edit modal: unchanged CSS-class-driven visibility.
    for cls in [
        "editkb-direct-only",
        "editkb-strategy-incremental",
        "editkb-strategy-partitioned",
        "editkb-strategy-not-incremental",
    ]:
        assert cls in HTML, f"visibility class missing: {cls}"


def test_html_js_payload_builders_send_v26_fields():
    """Spot-check: the register-side (register_table_form.js) AND edit-side
    (admin_tables.html) payload builders both emit the v26 field names."""
    for js_field in [
        "sync_strategy",
        "incremental_window_days",
        "max_history_days",
        "partition_by",
        "partition_granularity",
        "initial_load_chunk_days",
        "where_filters",
    ]:
        assert js_field in REGISTER_JS, f"register JS payload missing field: {js_field}"
        assert js_field in HTML, f"edit JS payload missing field: {js_field}"


def test_html_placeholders_documented_in_form_hint():
    """The where_filters help text must mention at least 4 placeholders so
    operators don't have to read the source to know what's supported —
    checked on the shared register drawer, which carries the full list (the
    edit modal's own hint only spot-mentions two, "... etc.")."""
    for token in ("{{today}}", "{{last_3_months}}", "{{last_year}}", "{{start_of_3_months_ago}}"):
        assert token in REGISTER_PARTIAL_HTML, f"placeholder hint missing: {token}"


# ───────────────────────────── API roundtrip ──────────────────────────────────


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_api_register_full_refresh_keboola(seeded_app):
    """Baseline: full_refresh registration with no v26 fields persists clean."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "circle",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "circle",
            "query_mode": "local",
        },
    )
    assert r.status_code == 201, r.text

    g = c.get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    row = next(t for t in g.json()["tables"] if t["id"] == "circle")
    assert row["sync_strategy"] == "full_refresh"
    assert row["incremental_window_days"] is None
    assert row["where_filters"] is None


def test_api_register_incremental_with_full_v26_payload(seeded_app):
    """Mirrors the JS payload from _buildKeboolaPayload(direct + incremental)."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "kpi_snapshot",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "kpi_leadership_snapshot",
            "query_mode": "local",
            "primary_key": ["kpi_id", "snapshot_date"],
            "sync_strategy": "incremental",
            "incremental_window_days": 1,
            "max_history_days": 180,
        },
    )
    assert r.status_code == 201, r.text

    g = c.get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    row = next(t for t in g.json()["tables"] if t["id"] == "kpi_snapshot")
    assert row["sync_strategy"] == "incremental"
    assert row["incremental_window_days"] == 1
    assert row["max_history_days"] == 180
    assert row["primary_key"] == ["kpi_id", "snapshot_date"]


def test_api_register_partitioned_with_full_v26_payload(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
            "primary_key": ["id"],
            "sync_strategy": "partitioned",
            "partition_by": "order_date",
            "partition_granularity": "month",
            "initial_load_chunk_days": 30,
            "max_history_days": 365,
        },
    )
    assert r.status_code == 201, r.text

    g = c.get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    row = next(t for t in g.json()["tables"] if t["id"] == "orders")
    assert row["sync_strategy"] == "partitioned"
    assert row["partition_by"] == "order_date"
    assert row["partition_granularity"] == "month"
    assert row["initial_load_chunk_days"] == 30


def test_api_register_with_where_filters(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "account_balance",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "account_balance",
            "query_mode": "local",
            "sync_strategy": "full_refresh",
            "where_filters": [
                {"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]},
                {"column": "country_code", "operator": "eq", "values": ["CZ", "SK"]},
            ],
        },
    )
    assert r.status_code == 201, r.text

    g = c.get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    row = next(t for t in g.json()["tables"] if t["id"] == "account_balance")
    assert row["where_filters"][0]["column"] == "date"
    # Placeholder must be PRESERVED at register time (resolved at sync time)
    assert row["where_filters"][0]["values"] == ["{{last_3_months}}"]
    assert row["where_filters"][1]["values"] == ["CZ", "SK"]


# ───────────────────────────── conflict policy ────────────────────────────────


def test_api_rejects_incremental_plus_where_filters(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "x",
            "source_type": "keboola",
            "bucket": "in.c-x",
            "source_table": "x",
            "query_mode": "local",
            "sync_strategy": "incremental",
            "where_filters": [{"column": "d", "operator": "ge", "values": ["x"]}],
        },
    )
    assert r.status_code == 422
    assert "incremental" in r.text.lower() or "where_filters" in r.text.lower()


def test_api_rejects_partitioned_remote(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "y",
            "source_type": "keboola",
            "bucket": "in.c-y",
            "source_table": "y",
            "query_mode": "remote",
            "sync_strategy": "partitioned",
            "partition_by": "date",
        },
    )
    assert r.status_code == 422


def test_api_rejects_partitioned_without_partition_by(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "z",
            "source_type": "keboola",
            "bucket": "in.c-z",
            "source_table": "z",
            "query_mode": "local",
            "sync_strategy": "partitioned",
        },
    )
    assert r.status_code == 422
    assert "partition_by" in r.text


def test_api_rejects_invalid_strategy(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/register-table",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "name": "q",
            "source_type": "keboola",
            "bucket": "in.c-q",
            "source_table": "q",
            "query_mode": "local",
            "sync_strategy": "monthly_at_midnight",
        },
    )
    assert r.status_code == 422


# ───────────────────────────── PUT (Edit modal) ───────────────────────────────


def test_api_put_changes_strategy_full_to_incremental(seeded_app):
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "tab1",
            "source_type": "keboola",
            "bucket": "in.c-x",
            "source_table": "tab1",
            "query_mode": "local",
            "sync_strategy": "full_refresh",
        },
    )

    r = c.put(
        "/api/admin/registry/tab1",
        headers=auth,
        json={
            "sync_strategy": "incremental",
            "primary_key": ["id"],
            "incremental_window_days": 7,
        },
    )
    assert r.status_code == 200, r.text

    row = next(t for t in c.get("/api/admin/registry", headers=auth).json()["tables"] if t["id"] == "tab1")
    assert row["sync_strategy"] == "incremental"
    assert row["incremental_window_days"] == 7


def test_api_put_clears_v26_fields_on_strategy_switch(seeded_app):
    """JS sends explicit nulls for v26 fields when switching strategies; the
    PUT path must propagate them so stale values don't survive."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "tab2",
            "source_type": "keboola",
            "bucket": "in.c-x",
            "source_table": "tab2",
            "query_mode": "local",
            "primary_key": ["id"],
            "sync_strategy": "partitioned",
            "partition_by": "date",
            "partition_granularity": "month",
            "max_history_days": 180,
        },
    )

    # Switch back to full_refresh — partition_by/granularity should clear
    r = c.put(
        "/api/admin/registry/tab2",
        headers=auth,
        json={
            "sync_strategy": "full_refresh",
            "partition_by": None,
            "partition_granularity": None,
            "initial_load_chunk_days": None,
            "max_history_days": None,
            "incremental_window_days": None,
            "where_filters": None,
        },
    )
    assert r.status_code == 200, r.text

    row = next(t for t in c.get("/api/admin/registry", headers=auth).json()["tables"] if t["id"] == "tab2")
    assert row["sync_strategy"] == "full_refresh"
    assert row["partition_by"] is None
    assert row["partition_granularity"] is None
    assert row["max_history_days"] is None


def test_api_put_updates_where_filters(seeded_app):
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "tab3",
            "source_type": "keboola",
            "bucket": "in.c-x",
            "source_table": "tab3",
            "query_mode": "local",
            "sync_strategy": "full_refresh",
            "where_filters": [{"column": "d", "operator": "ge", "values": ["{{last_week}}"]}],
        },
    )

    r = c.put(
        "/api/admin/registry/tab3",
        headers=auth,
        json={
            "where_filters": [
                {"column": "d", "operator": "ge", "values": ["{{last_year}}"]},
                {"column": "country", "operator": "eq", "values": ["US"]},
            ],
        },
    )
    assert r.status_code == 200, r.text

    row = next(t for t in c.get("/api/admin/registry", headers=auth).json()["tables"] if t["id"] == "tab3")
    assert len(row["where_filters"]) == 2
    assert row["where_filters"][0]["values"] == ["{{last_year}}"]


# ───────────────────────────── module-level dispatch ──────────────────────────


def test_extractor_dispatches_v26_table_from_registry(tmp_path, seeded_app, monkeypatch):
    """A row registered through the API as 'incremental' is correctly
    routed by extractor.run() to extract_incremental.

    Bridges the API+DB persistence layer to the dispatcher logic — proves
    the registered row's table_config dict shape (as returned by
    list_by_source) matches what the dispatcher reads."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "events_local",
            "source_type": "keboola",
            "bucket": "in.c-evt",
            "source_table": "events",
            "query_mode": "local",
            "primary_key": ["id"],
            "sync_strategy": "incremental",
            "incremental_window_days": 2,
        },
    )

    # Round-trip the registered row through the API to get the table_config
    # shape the dispatcher consumes (extractor.run takes table_configs from
    # repo.list_by_source, which produces the same dict shape as the GET
    # response — verify the new v26 columns survive the read path).
    g = c.get("/api/admin/registry", headers=auth)
    target = next(t for t in g.json()["tables"] if t["id"] == "events_local")
    assert target["sync_strategy"] == "incremental"
    assert target["incremental_window_days"] == 2

    from connectors.keboola import extractor

    called = {"incremental": 0, "extension": 0}

    def fake_incremental(**kw):
        called["incremental"] += 1
        pa_t = pa.table({"id": pa.array([1])})
        pq.write_table(pa_t, kw["parquet_path"])
        return {"rows": 1, "delta_rows": 1, "changed_since_used": None}

    def fake_extension(*a, **kw):
        called["extension"] += 1

    monkeypatch.setattr(extractor, "_extract_via_extension", fake_extension)
    monkeypatch.setattr(extractor, "_try_attach_extension", lambda *a, **kw: True)
    monkeypatch.setattr(extractor, "_read_last_sync", lambda tid: None)
    monkeypatch.setattr("connectors.keboola.incremental.extract_incremental", fake_incremental)

    result = extractor.run(str(tmp_path), [target], "https://kbc.example", "tok")
    assert called == {"incremental": 1, "extension": 0}
    assert result["tables_extracted"] == 1
