"""Legacy Keboola client (`connectors/keboola/client.py`) — the third
sliced-manifest consumer.

`KeboolaStorageClient` has two sliced entry points (`_download_sliced`,
`download_file_slices`); `KeboolaClient._export_table_with_filters` is a
third, with its own HTTP plumbing. It used to write a header-only CSV and
report `exported_rows: 0` for an entries-less manifest *unconditionally* —
clean success for what may have been a lost export.

All three now share one decision,
`connectors.keboola.storage_api.check_empty_sliced_manifest`. These tests
pin it at this site: the shared rule, not a mirrored copy of it.

Note which shape is reachable in production. `export_table` only routes into
`_export_table_with_filters` when `where_filters` is non-empty (client.py's
`if where_filters:`), so every production call here is a FILTERED export —
and on a filtered export the whole-table `rowsCount` cannot arbitrate a
zero-match, so it stays a success. The unfiltered branches below are reached
by direct calls with `where_filters=[]` (which the existing sliced-export
tests in this repo already use) and are what hardens the site against a
future caller that drops the filter.
"""

from unittest.mock import MagicMock

import pytest

from connectors.keboola.storage_api import StorageApiError

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
pytest.importorskip("kbcstorage")

from connectors.keboola.client import KeboolaClient, WhereFilter  # noqa: E402


def _client_with_empty_manifest(tmp_path, monkeypatch, table_detail):
    """A `KeboolaClient` whose async export completes with a sliced file
    whose manifest lists zero entries. `table_detail` is what the Storage
    API's table detail returns (the SDK's `tables.detail`)."""
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    client = KeboolaClient()
    client.token = "storage-tok"
    client.url = "https://kbc"
    client.client = MagicMock()
    client.client.tables.detail.return_value = table_detail
    client.metadata_cache = {}
    client.metadata_cache_path = tmp_path / "meta.json"

    monkeypatch.setattr("connectors.keboola.client.time.sleep", lambda *a, **kw: None)

    export_post_resp = MagicMock()
    export_post_resp.raise_for_status = MagicMock()
    export_post_resp.json.return_value = {"id": 100}

    job_poll_resp = MagicMock()
    job_poll_resp.raise_for_status = MagicMock()
    job_poll_resp.json.return_value = {
        "id": 100,
        "status": "success",
        "results": {"file": {"id": 200}},
    }

    file_detail_resp = MagicMock()
    file_detail_resp.raise_for_status = MagicMock()
    file_detail_resp.json.return_value = {"url": "https://signed/manifest.json", "isSliced": True}

    manifest_resp = MagicMock()
    manifest_resp.raise_for_status = MagicMock()
    manifest_resp.json.return_value = {"entries": []}

    monkeypatch.setattr(
        "connectors.keboola.client.requests.post",
        MagicMock(return_value=export_post_resp),
    )
    monkeypatch.setattr(
        "connectors.keboola.client.requests.get",
        MagicMock(side_effect=[job_poll_resp, file_detail_resp, manifest_resp]),
    )
    return client


_ROW_FILTER = [WhereFilter(column="status", operator="eq", values=["open"])]


# ---- unfiltered: the three branches of the shared decision ------------------


def test_unfiltered_zero_rows_upstream_is_a_clean_zero_row_export(tmp_path, monkeypatch):
    client = _client_with_empty_manifest(tmp_path, monkeypatch, {"rowsCount": 0, "columns": ["id", "name"]})
    dest = tmp_path / "out.csv"

    info = client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    assert info["exported_rows"] == 0
    assert dest.read_text() == '"id","name"\n'


def test_unfiltered_rows_upstream_but_no_slices_is_now_an_error(tmp_path, monkeypatch):
    """Was a silent `exported_rows: 0` success — i.e. data loss reported as
    a clean sync."""
    client = _client_with_empty_manifest(tmp_path, monkeypatch, {"rowsCount": 12, "columns": ["id", "name"]})
    dest = tmp_path / "out.csv"

    with pytest.raises(StorageApiError, match=r"claims 12 rows"):
        client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    assert not dest.exists()


def test_unfiltered_unknown_rows_count_is_now_an_error(tmp_path, monkeypatch):
    """Contract change at this site: unknown must never present itself as
    empty, so a table detail with no readable `rowsCount` fails instead of
    reporting a clean 0-row export."""
    client = _client_with_empty_manifest(tmp_path, monkeypatch, {"columns": ["id", "name"]})
    dest = tmp_path / "out.csv"

    with pytest.raises(StorageApiError, match="sliced manifest had no entries"):
        client._export_table_with_filters("in.c-x.t", dest, where_filters=[])


# ---- filtered: the shape production actually reaches ------------------------


def test_filtered_zero_match_stays_a_clean_zero_row_export(tmp_path, monkeypatch):
    """The only shape `export_table` routes here. A filtered export matching
    nothing on a 4200-row table is a correct empty result, not data loss —
    the whole-table `rowsCount` cannot arbitrate it."""
    client = _client_with_empty_manifest(tmp_path, monkeypatch, {"rowsCount": 4200, "columns": ["id", "name"]})
    dest = tmp_path / "out.csv"

    info = client._export_table_with_filters("in.c-x.t", dest, where_filters=_ROW_FILTER)

    assert info["exported_rows"] == 0
    assert dest.read_text() == '"id","name"\n'


def test_filtered_zero_match_makes_no_rows_count_lookup(tmp_path, monkeypatch):
    """Not merely tolerated — not consulted. Guards against a future edit
    that reintroduces the whole-table count into the filtered path."""
    client = _client_with_empty_manifest(tmp_path, monkeypatch, {"rowsCount": 4200, "columns": ["id"]})
    client.client.tables.detail.reset_mock()

    client._export_table_with_filters("in.c-x.t", tmp_path / "out.csv", where_filters=_ROW_FILTER)

    # One call only: the metadata fetch that supplies the CSV header. No
    # second detail read for a count the filtered branch must not use.
    assert client.client.tables.detail.call_count == 1
