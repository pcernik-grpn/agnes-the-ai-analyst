"""Keboola legacy client (`connectors/keboola/client.py`) gs:// sliced-export
URL rewrite.

This file used to pin a divergence rather than a contract: the legacy client
did its own string-replace rewrite while storage_api.py built a JSON-API
media URL, and the assertion below froze that difference in place. The
second scheme chain that difference lived in is also what left the legacy
path without an `azure://` arm long after the other path had one. Both paths
now share `KeboolaStorageClient._prepare_slice_request`, so what this
asserts is the shared rewrite — see
tests/test_keboola_slice_scheme_dispatch.py for the dispatch contract.
"""

from unittest.mock import MagicMock

import pytest

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
# See tests/test_keboola_extractor_typed.py for the same pattern.
pytest.importorskip("kbcstorage")

from connectors.keboola.client import KeboolaClient  # noqa: E402


def test_sliced_gcs_slice_url_rewritten_with_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    client = KeboolaClient()
    client.token = "storage-tok"
    client.url = "https://connection.keboola.com"
    client.client = MagicMock()
    client.client.tables.detail.return_value = {"columns": ["id", "name"]}
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
    file_detail_resp.json.return_value = {
        "url": "https://signed/manifest.json",
        "isSliced": True,
        "gcsCredentials": {"access_token": "gcs-bearer-tok"},
    }

    manifest_resp = MagicMock()
    manifest_resp.raise_for_status = MagicMock()
    manifest_resp.json.return_value = {
        "entries": [{"url": "gs://bkt/exp/slice-0"}],
    }

    slice_resp = MagicMock()
    slice_resp.raise_for_status = MagicMock()
    slice_resp.content = b"1,alice\n"

    monkeypatch.setattr(
        "connectors.keboola.client.requests.post",
        MagicMock(return_value=export_post_resp),
    )
    get_mock = MagicMock(side_effect=[job_poll_resp, file_detail_resp, manifest_resp, slice_resp])
    monkeypatch.setattr("connectors.keboola.client.requests.get", get_mock)

    dest = tmp_path / "out.csv"
    client._export_table_with_filters("in.c-x.t", dest, where_filters=[])

    # Last GET call is the slice download — the gs:// URI must be rewritten
    # through the shared dispatch (JSON-API media URL, object name escaped
    # as one path segment) with the OAuth bearer attached.
    slice_call = get_mock.call_args_list[-1]
    assert slice_call.args[0] == "https://storage.googleapis.com/storage/v1/b/bkt/o/exp%2Fslice-0?alt=media"
    assert slice_call.kwargs["headers"] == {"Authorization": "Bearer gcs-bearer-tok"}

    # Header line synthesized from table metadata (sliced files carry no
    # header per Storage API contract) followed by the slice content.
    assert dest.read_text() == '"id","name"\n1,alice\n'
