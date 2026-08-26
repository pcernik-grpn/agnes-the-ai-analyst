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
    """The `/admin/tables` Keboola register modal must offer a fourth
    "What to sync?" radio — Live (remote), `value="remote"` — matching the
    interaction pattern already used by BigQuery (`bqAccessMode`),
    Databricks (`dbxAccessMode`) and Snowflake (`sfAccessMode`)."""
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    kb_modal_start = html.index('id="registerKeboolaModal"')
    next_modal_idx = html.find('id="editKeboolaModal"', kb_modal_start)
    kb_tab = html[kb_modal_start:next_modal_idx] if next_modal_idx > 0 else html[kb_modal_start:]

    assert 'name="kbSyncMode"' in kb_tab
    assert 'value="whole"' in kb_tab
    assert 'value="direct"' in kb_tab
    assert 'value="custom"' in kb_tab
    # The new live option.
    assert 'value="remote"' in kb_tab
    assert "Live" in kb_tab


def test_keboola_payload_builder_maps_remote_mode(seeded_app, keboola_instance):
    """`_buildKeboolaPayload`'s remote branch must post `query_mode: 'remote'`
    (not fold into the materialized/local branches)."""
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    start = html.index("function _buildKeboolaPayload(")
    end = html.index("\n    function ", start + len("function _buildKeboolaPayload("))
    body = html[start:end]

    assert "mode === 'remote'" in body
    assert "query_mode: 'remote'" in body
