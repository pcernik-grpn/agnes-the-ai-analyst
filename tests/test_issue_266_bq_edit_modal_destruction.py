"""Regression coverage for #266 — Edit modal on BQ materialized rows
silently nulled bucket / source_table or showed empty inputs.

Three bugs traced from the Save chain (admin_tables.html JS → PUT
/api/admin/registry/{id} → table_registry upsert):

1. `saveBqTabEdit` (synced/custom) sent `bucket: null, source_table:
   null` on every save — not just on a true remote→materialized mode
   flip. An admin editing only the description of an already-
   materialized custom-SQL row wiped persisted bucket/source_table.

2. `_buildBigQueryPayload` (synced/whole) at register time DID NOT
   send bucket/source_table — only source_query. So whole-table
   materialized rows persisted with bucket=NULL from day one. The
   Edit modal then read empty `table.bucket` and rendered empty
   Dataset/Table inputs over the SELECT * SQL.

3. `_openEditBqModal` populated the Dataset/Table inputs from
   `table.bucket || ''` only — for whole-table rows registered pre-#266
   (bucket=NULL), the inputs stayed empty. Saving with the empty
   inputs would synthesize a broken `SELECT * FROM bq."".""` SQL.

These tests pin the server-side contract that the client now relies
on: PUTs that OMIT bucket/source_table keys preserve existing values
(thanks to `exclude_unset=True` in `app/api/admin.update_table`).
The template-grep test pins the JS-side fixes themselves.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_put_omitting_bucket_preserves_existing_value_on_materialized_row(
    seeded_app,
    bq_instance,
    stub_bq_extractor,
):
    """Bug 1 fix contract: when the new Edit-modal save path omits
    bucket/source_table from the JSON body on a no-op-mode save, the
    server preserves the existing values.

    Pre-#266 the JS sent `bucket: null, source_table: null` on every
    save in the synced/custom branch — this test pins that an OMITTED
    key on a custom-SQL materialized row preserves bucket. Same
    invariant the v26 PUT-preservation tests pin for primary_key /
    sync_strategy, but specific to bucket/source_table on a
    materialized row (which the older tests didn't cover)."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    # Register a custom-SQL materialized BQ row that ALSO has bucket+
    # source_table set. Both can coexist — bucket is documentary, the
    # SQL is the source of truth. Pre-#266 a curl PUT could set this
    # state; post-#266 the whole-table register flow also produces it.
    custom_sql = (
        "SELECT order_id, customer_id, total_usd "
        "FROM `myproj.finance.orders` "
        "WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
    )
    resp = client.post(
        "/api/admin/register-table",
        json={
            "name": "preserve_bucket_test",
            "source_type": "bigquery",
            "query_mode": "materialized",
            "bucket": "finance",
            "source_table": "orders",
            "source_query": custom_sql,
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201, 202), resp.text

    # PUT a description-only change — body OMITS bucket/source_table,
    # mirroring the post-#266 JS payload-builder behavior on a no-op
    # mode save.
    resp = client.put(
        "/api/admin/registry/preserve_bucket_test",
        json={"description": "Customer orders, last 30 days"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "preserve_bucket_test")
    assert row["bucket"] == "finance"
    assert row["source_table"] == "orders"
    assert row["description"] == "Customer orders, last 30 days"
    assert row["source_query"] == custom_sql
    assert row["query_mode"] == "materialized"


def test_put_explicit_null_clears_bucket_on_mode_flip(
    seeded_app,
    bq_instance,
    stub_bq_extractor,
):
    """Bug 1 fix contract: a TRUE mode-flip save (remote → materialized
    custom) still wants to clear stale bucket/source_table from the
    old mode. The JS sends explicit null in that case; the server
    persists NULL. This pins the existing exclude_unset semantics
    that distinguish 'omitted' (preserve) from 'explicit null' (clear)
    — see admin.py:2636-2654 inline comment."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    # Register remote BQ row with bucket+source_table.
    resp = client.post(
        "/api/admin/register-table",
        json={
            "name": "flip_remote_to_custom",
            "source_type": "bigquery",
            "bucket": "finance",
            "source_table": "orders",
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201, 202), resp.text

    # Mode flip: remote → materialized custom-SQL. JS sends explicit
    # null in this branch (its `_editOriginalQueryMode !== 'materialized'`
    # condition fires).
    new_sql = "SELECT 1 AS placeholder"
    resp = client.put(
        "/api/admin/registry/flip_remote_to_custom",
        json={
            "query_mode": "materialized",
            "source_query": new_sql,
            "bucket": None,
            "source_table": None,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "flip_remote_to_custom")
    assert row["query_mode"] == "materialized"
    assert row["bucket"] is None
    assert row["source_table"] is None
    assert row["source_query"] == new_sql


def test_register_whole_table_materialized_persists_bucket(
    seeded_app,
    bq_instance,
    stub_bq_extractor,
):
    """Bug 2/3 fix contract: post-#266 the JS whole-table register
    branch sends bucket+source_table alongside source_query so a
    subsequent Edit modal can pre-fill those inputs from the
    persisted values (instead of leaving them empty / re-parsing
    SQL).

    The server already accepts this shape — `_validate_bigquery_register_payload`
    treats bucket/source_table as optional when source_query is
    provided. This test pins that the persisted row carries both."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    # Mirror the post-#266 JS payload for synced/whole register.
    resp = client.post(
        "/api/admin/register-table",
        json={
            "name": "whole_table_with_bucket",
            "source_type": "bigquery",
            "query_mode": "materialized",
            "bucket": "analytics",
            "source_table": "page_views",
            "source_query": 'SELECT * FROM bq."analytics"."page_views"',
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201, 202), resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "whole_table_with_bucket")
    assert row["bucket"] == "analytics"
    assert row["source_table"] == "page_views"
    assert row["query_mode"] == "materialized"
    # source_query persisted verbatim (DuckDB three-part-alias form;
    # the materialize wrapper translates this to BQ-native at exec).
    assert 'bq."analytics"."page_views"' in row["source_query"]


def test_admin_tables_template_has_266_fixes():
    """Pin the three JS-side fixes in the rendered template. These
    are tested by string presence rather than headless browser since
    Agnes has no JS test harness. A future maintainer who reverts
    one of them will trip an obvious failure here."""
    from pathlib import Path

    tpl = Path(__file__).parent.parent / "app" / "web" / "templates" / "admin_tables.html"
    text = tpl.read_text(encoding="utf-8")

    # Bug 1: saveBqTabEdit synced/custom branch must guard the null
    # writes with a mode-flip check, not null unconditionally.
    assert "_editOriginalQueryMode !== 'materialized'" in text, (
        "Bug 1 regression: saveBqTabEdit nulls bucket/source_table unconditionally — must guard on a real mode flip."
    )
    # The unconditional null pattern from pre-#266 must be GONE in
    # the custom branch. We grep for the surrounding comment-tail.
    assert "payload.bucket = null;\n            payload.source_table = null;\n        } else" not in text, (
        "Bug 1 regression: the unconditional `payload.bucket = null` block is back in the synced/custom branch."
    )

    # Bug 2/3: the register-side payload builder's synced/whole branch
    # must include bucket+source_table in the JSON, so Edit can pre-fill.
    # D4 replaced `_buildBigQueryPayload` (admin_tables.html) with
    # `CONNECTORS.bigquery.buildPayload` (register_table_form.js) — same
    # invariant, different file.
    js_path = Path(__file__).parent.parent / "app" / "web" / "static" / "js" / "register_table_form.js"
    js_text = js_path.read_text(encoding="utf-8")
    bq_start = js_text.index("bigquery: {")
    bq_end = js_text.index("\n    databricks: {", bq_start)
    bq_config = js_text[bq_start:bq_end]
    whole_start = bq_config.index("if (mode === 'synced_whole')")
    whole_branch = bq_config[whole_start : whole_start + 400]
    assert "bucket: row.bucket, source_table: row.sourceTable," in whole_branch, (
        "Bug 2/3 regression: CONNECTORS.bigquery.buildPayload's synced_whole "
        "branch must send bucket alongside source_query so Edit can pre-fill."
    )

    # _openEditBqModal must parse dataset+source_table out of the
    # source_query when bucket is empty (back-compat for rows
    # registered pre-#266 with bucket=NULL).
    assert "if (!preDataset && !preSourceTable && isAutoSelectStar)" in text, (
        "Bug 2 regression: _openEditBqModal must fall back to parsing "
        "source_query for pre-#266 whole-table rows with bucket=NULL."
    )
