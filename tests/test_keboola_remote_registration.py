"""B2: Keboola `query_mode='remote'` is engine-supported (the DuckDB Keboola
extension view + `_remote_attach`, `connectors/keboola/extractor.py`) and the
register API already accepts it — the `source_type in ('databricks',
'snowflake')` restriction in `RegisterTableRequest._check_mode_query_coherence`
never touches Keboola. What was missing is a UI path: the `/admin/tables`
Keboola modal offered only whole/direct/custom (all local/materialized), never
a "Live (remote)" option, unlike the BigQuery/Databricks/Snowflake modals in
the same template.

This file pins the end-to-end registration path (server already worked) and
the modal markup (the actual fix)."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def keboola_instance(monkeypatch):
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
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


def test_register_keboola_remote_accepts(seeded_app, keboola_instance):
    """The register endpoint already accepts source_type=keboola +
    query_mode=remote — no validation blocks it. Pin the 201 + the persisted
    row so a future regression (e.g. someone copying the databricks/snowflake
    query_mode allowlist onto keboola) is caught here."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_live",
            "source_type": "keboola",
            "query_mode": "remote",
            "bucket": "in.c-sales",
            "source_table": "orders",
        },
    )
    assert r.status_code == 201, r.text

    rows = c.get("/api/admin/registry", headers=auth).json()
    row = next(t for t in rows["tables"] if t["id"] == "orders_live")
    assert row["source_type"] == "keboola"
    assert row["query_mode"] == "remote"
    assert row["bucket"] == "in.c-sales"
    assert row["source_table"] == "orders"


def test_register_keboola_remote_rejects_server_only(seeded_app, keboola_instance):
    """server_only=true is incoherent with query_mode='remote' (no
    server-stored parquet to suppress) — the existing model validator already
    covers this for every source_type, keboola included."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_live",
            "source_type": "keboola",
            "query_mode": "remote",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "server_only": True,
        },
    )
    assert r.status_code == 422, r.text


def test_keboola_modal_offers_live_remote_option(seeded_app, keboola_instance):
    """D4 — one registration flow: the Keboola register modal this test
    scraped is gone. Its four "What to sync?" modes — including "Live
    (remote)" — now live in `CONNECTORS.keboola.modes`
    (register_table_form.js), rendered through the same generic
    `#rtfModeGroup` every connector shares (BigQuery/Databricks/Snowflake
    included) rather than a per-connector radio-group clone."""
    from pathlib import Path

    js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
    kb_start = js.index("keboola: {")
    kb_end = js.index("\n    bigquery: {", kb_start)
    kb_config = js[kb_start:kb_end]

    assert "value: 'whole'" in kb_config
    assert "value: 'direct'" in kb_config
    assert "value: 'custom'" in kb_config
    # The live option.
    assert "value: 'remote'" in kb_config
    assert "Live" in kb_config


def test_keboola_payload_builder_maps_remote_mode(seeded_app, keboola_instance):
    """`CONNECTORS.keboola.buildPayload`'s remote branch must post
    `query_mode: 'remote'` (not fold into the materialized/local
    branches)."""
    from pathlib import Path

    js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
    kb_start = js.index("keboola: {")
    kb_end = js.index("\n    bigquery: {", kb_start)
    kb_config = js[kb_start:kb_end]

    assert "mode === 'remote'" in kb_config
    assert "query_mode: 'remote'" in kb_config
