"""GET /api/admin/discover-tables — BigQuery branch.

Two-step shape: dataset list (no `dataset` query param) → table list (with
`dataset=name`). The UI populates the dataset autocomplete first, then
fetches tables only after the operator picks a dataset, avoiding the
per-dataset `list_tables()` cost on projects with hundreds of datasets.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from connectors.bigquery.access import BqAccess, BqProjects


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    """Force `data_source.type='bigquery'` so the endpoint routes to the
    BQ branch."""
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


def _stub_bq_with_client(client_mock):
    """Build a BqAccess wired to return `client_mock` from .client(). The
    duckdb_session_factory is unused by the discover endpoint — supply a
    no-op."""
    from contextlib import contextmanager

    @contextmanager
    def _noop(_p):
        yield None

    return BqAccess(
        BqProjects(billing="my-test-project", data="my-test-project"),
        client_factory=lambda _p: client_mock,
        duckdb_session_factory=_noop,
    )


def test_discover_returns_dataset_list(seeded_app, bq_instance, monkeypatch):
    """Without `dataset` param: list datasets in the configured project."""
    client = MagicMock()
    ds_a = MagicMock()
    ds_a.dataset_id = "analytics"
    ds_a.project = "my-test-project"
    ds_b = MagicMock()
    ds_b.dataset_id = "raw"
    ds_b.project = "my-test-project"
    client.list_datasets.return_value = [ds_a, ds_b]

    monkeypatch.setattr(
        "connectors.bigquery.access.get_bq_access",
        lambda: _stub_bq_with_client(client),
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/discover-tables", headers=_auth(token))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["source"] == "bigquery"
    assert body["count"] == 2
    # Sorted alphabetically by dataset_id.
    assert [d["dataset_id"] for d in body["datasets"]] == ["analytics", "raw"]
    assert body["datasets"][0]["full_id"] == "my-test-project.analytics"


def test_discover_returns_table_list_for_dataset(seeded_app, bq_instance, monkeypatch):
    """With `?dataset=analytics`: list tables + views in that dataset."""
    client = MagicMock()
    t_orders = MagicMock()
    t_orders.table_id = "orders"
    t_orders.table_type = "TABLE"
    t_orders.project = "my-test-project"
    t_orders.dataset_id = "analytics"
    t_view = MagicMock()
    t_view.table_id = "orders_active"
    t_view.table_type = "VIEW"
    t_view.project = "my-test-project"
    t_view.dataset_id = "analytics"
    client.list_tables.return_value = [t_view, t_orders]  # unsorted

    monkeypatch.setattr(
        "connectors.bigquery.access.get_bq_access",
        lambda: _stub_bq_with_client(client),
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/admin/discover-tables?dataset=analytics",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["source"] == "bigquery"
    assert body["dataset"] == "analytics"
    assert body["count"] == 2
    # Sorted by table_id.
    assert [t["table_id"] for t in body["tables"]] == ["orders", "orders_active"]
    by_id = {t["table_id"]: t for t in body["tables"]}
    assert by_id["orders"]["table_type"] == "TABLE"
    assert by_id["orders_active"]["table_type"] == "VIEW"
    # Verify dataset filter was passed through.
    client.list_tables.assert_called_once_with("analytics")


def test_discover_keboola_branch_unchanged(seeded_app, monkeypatch):
    """Negative — when source_type is keboola, BQ logic isn't reached.

    Skipped when the Keboola SDK (`kbcstorage`) is not installed: CI
    runners don't ship it because the dev container only needs it for
    instances that actually configure source_type=keboola, and the
    route's lazy import would fail before the test stub gets a chance
    to fire. The branch-unchanged contract is tested separately by the
    Keboola integration suite when the package is present.
    """
    pytest.importorskip("kbcstorage")
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()

    # Stub the Keboola client so the test doesn't reach the network.
    fake_client = MagicMock()
    fake_client.discover_all_tables.return_value = [{"id": "in.c-foo.bar"}]
    monkeypatch.setattr(
        "connectors.keboola.client.KeboolaClient",
        lambda *a, **kw: fake_client,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    try:
        r = c.get("/api/admin/discover-tables", headers=_auth(token))
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["source"] == "keboola"
        assert body["count"] == 1
    finally:
        reset_cache()


def test_discover_bq_not_configured_returns_500(seeded_app, monkeypatch):
    """When data_source.bigquery.project is missing, BqAccess returns its
    not_configured sentinel — endpoint surfaces the structured error."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {},  # no project
        },
    }
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
        r = c.get("/api/admin/discover-tables", headers=_auth(token))
        # not_configured is mapped to 500 in BqAccessError.HTTP_STATUS.
        assert r.status_code == 500, r.json()
        detail = r.json().get("detail", {})
        assert detail.get("kind") == "not_configured"
    finally:
        reset_cache()


def test_admin_tables_html_wires_discover_buttons(seeded_app, bq_instance):
    """Structural — D4 moved BQ discovery for REGISTRATION into the shared
    drawer's Browse step (register_table_form.js's `listDatasets`/
    `discover`, GET /api/admin/discover-tables — same endpoint, a
    checkbox list instead of a datalist-backed text input). The Discover /
    List-tables buttons + datalists this test originally checked survive
    on the EDIT modal only, which D4 left untouched."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text
    assert "discoverBqDatasets" in html
    assert "discoverBqTables" in html
    assert 'id="editBqDatasetList"' in html
    assert 'id="editBqTableList"' in html
    assert 'list="editBqDatasetList"' in html
    assert 'list="editBqTableList"' in html

    js = (Path("app/web/static/js/register_table_form.js")).read_text(encoding="utf-8")
    assert "/api/admin/discover-tables" in js
