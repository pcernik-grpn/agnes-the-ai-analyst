"""KeboolaStorageClient — direct Storage API export-async path.

Replaces the previous DuckDB-extension materialize path (extension scan
broken on linked-bucket projects, see keboola/duckdb-extension#17). Tests
mock the requests.Session at the adapter level so we exercise the real
HTTP shapes (status codes, JSON bodies) without touching the network.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
import requests

import connectors.keboola.storage_api as sapi
from connectors.keboola.storage_api import (
    FILE_TYPE_CSV,
    FILE_TYPE_PARQUET,
    ExportFilter,
    KeboolaStorageClient,
    StorageApiError,
    _slice_sort_key,
    get_temp_root,
    sweep_orphaned_scratch,
    warn_if_scratch_survived,
)


# ---- ExportFilter ----------------------------------------------------------


class TestExportFilter:
    def test_empty_dict_means_full_table(self):
        f = ExportFilter.from_dict({})
        assert f.to_export_params() == {}

    def test_none_means_full_table(self):
        f = ExportFilter.from_dict(None)
        assert f.to_export_params() == {}

    def test_where_filters_columns_changed_since(self):
        f = ExportFilter.from_dict(
            {
                "where_filters": [
                    {"column": "status", "operator": "eq", "values": ["open"]},
                ],
                "columns": ["id", "status"],
                "changed_since": "2026-04-01",
            }
        )
        params = f.to_export_params()
        # whereFilters must be emitted as Keboola's indexed form fields, not
        # a nested list — `requests` form-encodes the latter into a single
        # stringified-dict scalar that Keboola rejects ("should be an array").
        assert params["whereFilters[0][column]"] == "status"
        assert params["whereFilters[0][operator]"] == "eq"
        assert params["whereFilters[0][values][0]"] == "open"
        assert "whereFilters" not in params  # never the raw nested form
        # Storage API takes columns as comma-joined string, not array — the
        # `kbcstorage` SDK does the same join, so match its wire format.
        assert params["columns"] == "id,status"
        assert params["changedSince"] == "2026-04-01"

    def test_where_filters_multiple_filters_and_values_indexed(self):
        # Multi-filter, multi-value spec must index each filter and each value
        # so Keboola's PHP-array form parser reconstructs the full structure.
        f = ExportFilter.from_dict(
            {
                "where_filters": [
                    {"column": "job_created_at", "operator": "ge", "values": ["2025-12-18"]},
                    {"column": "status", "operator": "in", "values": ["open", "done"]},
                ],
            }
        )
        params = f.to_export_params()
        assert params["whereFilters[0][column]"] == "job_created_at"
        assert params["whereFilters[0][operator]"] == "ge"
        assert params["whereFilters[0][values][0]"] == "2025-12-18"
        assert params["whereFilters[1][column]"] == "status"
        assert params["whereFilters[1][operator]"] == "in"
        assert params["whereFilters[1][values][0]"] == "open"
        assert params["whereFilters[1][values][1]"] == "done"

    def test_where_filters_form_encode_roundtrip(self):
        # The actual wire body must carry the indexed keys, not a stringified
        # Python dict — this is the regression that made job_created_at
        # filtering silently return 0 rows / 400 on the materialized path.
        f = ExportFilter.from_dict(
            {
                "where_filters": [
                    {"column": "job_created_at", "operator": "ge", "values": ["2025-12-18"]},
                ],
            }
        )
        body = requests.models.RequestEncodingMixin._encode_params(f.to_export_params())
        assert "whereFilters%5B0%5D%5Bcolumn%5D=job_created_at" in body
        assert "whereFilters%5B0%5D%5Boperator%5D=ge" in body
        assert "whereFilters%5B0%5D%5Bvalues%5D%5B0%5D=2025-12-18" in body
        # The broken form stringified the dict — make sure that never recurs.
        assert "%27column%27" not in body  # no url-encoded "'column'"

    def test_where_filter_missing_keys_raises_with_context(self):
        f = ExportFilter.from_dict(
            {
                "where_filters": [{"column": "x", "operator": "eq"}],  # no values
            }
        )
        with pytest.raises(ValueError, match=r"missing fields.*\['values'\]"):
            f.to_export_params()

    def test_where_filter_values_must_be_list(self):
        f = ExportFilter.from_dict(
            {
                "where_filters": [{"column": "x", "operator": "eq", "values": "open"}],
            }
        )
        with pytest.raises(ValueError, match="values must be a list"):
            f.to_export_params()

    def test_default_file_type_is_csv_and_omits_param(self):
        # Wire-side default is csv — preserve old behavior for callers
        # that never set file_type.
        assert ExportFilter().file_type == FILE_TYPE_CSV
        assert "fileType" not in ExportFilter().to_export_params()

    def test_file_type_parquet_emits_fileType_param(self):
        f = ExportFilter(file_type=FILE_TYPE_PARQUET)
        assert f.to_export_params()["fileType"] == "parquet"

    def test_from_dict_reads_file_type_snake_case(self):
        f = ExportFilter.from_dict({"file_type": "parquet"})
        assert f.file_type == "parquet"
        assert f.to_export_params()["fileType"] == "parquet"

    def test_from_dict_reads_fileType_camel_case_alias(self):
        # Operators copying examples from Apiary docs ship the wire name.
        f = ExportFilter.from_dict({"fileType": "parquet"})
        assert f.file_type == "parquet"

    def test_from_dict_invalid_file_type_raises(self):
        with pytest.raises(ValueError, match="file_type"):
            ExportFilter.from_dict({"file_type": "orc"})


# ---- HTTP client low-level -------------------------------------------------


def _mock_response(status, body):
    """Build a fake `requests.Response`-like object."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestStorageClient:
    def test_init_normalises_trailing_slash(self):
        c = KeboolaStorageClient(url="https://kbc/", token="t")
        assert c.base.endswith("/v2/storage")
        assert "/" * 2 not in c.base.replace("https://", "")

    def test_init_rejects_missing_url_or_token(self):
        with pytest.raises(ValueError):
            KeboolaStorageClient(url="", token="t")
        with pytest.raises(ValueError):
            KeboolaStorageClient(url="https://kbc", token="")

    def test_post_sends_storage_api_token_header(self):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 42})
        c = KeboolaStorageClient(url="https://kbc", token="abc", session=sess)

        c.export_table_async("in.c-x.t", {"columns": "a"})

        sess.post.assert_called_once()
        kwargs = sess.post.call_args.kwargs
        assert kwargs["headers"]["X-StorageApi-Token"] == "abc"

    def test_post_4xx_redacts_token_in_error_message(self):
        # If the API echoes the token (or a proxy injects it), we must not
        # leak it into raised exceptions.
        sess = MagicMock()
        sess.post.return_value = _mock_response(403, {"detail": "rejected token=secrettoken123"})
        c = KeboolaStorageClient(url="https://kbc", token="secrettoken123", session=sess)

        with pytest.raises(StorageApiError) as e:
            c.export_table_async("in.c-x.t", {})

        assert "secrettoken123" not in str(e.value)
        assert "<redacted-storage-token>" in str(e.value)

    def test_verify_token_calls_tokens_verify(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"id": "123", "isMasterToken": True})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        info = c.verify_token()

        assert info["isMasterToken"] is True
        url = sess.get.call_args.args[0]
        assert url == "https://kbc/v2/storage/tokens/verify"


# ---- discovery: list_buckets / list_tables ---------------------------------


class TestListBucketsAndTables:
    def test_list_buckets_returns_raw_list(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            [
                {"id": "in.c-main", "name": "main", "stage": "in"},
                {"id": "out.c-reports", "name": "reports", "stage": "out"},
            ],
        )
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        buckets = c.list_buckets()

        assert [b["id"] for b in buckets] == ["in.c-main", "out.c-reports"]
        url = sess.get.call_args.args[0]
        assert url == "https://kbc/v2/storage/buckets"
        assert sess.get.call_args.kwargs["headers"]["X-StorageApi-Token"] == "t"

    def test_list_tables_all_returns_raw_list(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            [
                {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}, "rowsCount": 10},
            ],
        )
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        tables = c.list_tables()

        assert tables[0]["name"] == "orders"
        url = sess.get.call_args.args[0]
        assert url == "https://kbc/v2/storage/tables"

    def test_list_tables_scoped_to_bucket(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, [])
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        c.list_tables(bucket_id="in.c-main")

        url = sess.get.call_args.args[0]
        assert url == "https://kbc/v2/storage/buckets/in.c-main/tables"

    def test_list_buckets_4xx_raises_storage_api_error(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(403, {"error": "invalid token"})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="HTTP 403"):
            c.list_buckets()

    def test_list_tables_non_list_response_raises_typed_error(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"unexpected": "object"})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="non-list"):
            c.list_tables()


# ---- wait_for_job ----------------------------------------------------------


class TestWaitForJob:
    def test_returns_on_success(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {
                "id": 1,
                "status": "success",
                "results": {"file": {"id": 99}},
            },
        )
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        job = c.wait_for_job(1, timeout=5, poll_interval=0.01)
        assert job["status"] == "success"

    def test_raises_on_error_status(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {
                "id": 1,
                "status": "error",
                "error": {"message": "bad table"},
            },
        )
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="reported error"):
            c.wait_for_job(1, timeout=5, poll_interval=0.01)

    def test_polls_until_terminal(self):
        # First two responses 'waiting', third 'success'. The client must
        # keep polling instead of giving up.
        sess = MagicMock()
        sess.get.side_effect = [
            _mock_response(200, {"id": 1, "status": "waiting"}),
            _mock_response(200, {"id": 1, "status": "processing"}),
            _mock_response(200, {"id": 1, "status": "success", "results": {"file": {"id": 7}}}),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        job = c.wait_for_job(1, timeout=5, poll_interval=0.01)
        assert job["status"] == "success"
        assert sess.get.call_count == 3

    def test_timeout_raises_with_job_id(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"id": 1, "status": "waiting"})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="did not finish"):
            c.wait_for_job(1, timeout=0.1, poll_interval=0.05)


# ---- download_file ---------------------------------------------------------


class TestDownloadFile:
    def test_single_file_csv_passthrough(self, tmp_path):
        sess = MagicMock()
        # File detail returns a signed URL for a non-sliced .csv; download
        # streams it directly.
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        single_resp.iter_content.return_value = [b"col1,col2\n", b"a,1\n", b"b,2\n"]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file(
            {
                "url": "https://signed/single.csv",
                "name": "single.csv",
                "isSliced": False,
            },
            dest,
        )

        assert dest.exists()
        assert dest.read_bytes() == b"col1,col2\na,1\nb,2\n"

    def test_single_file_gz_is_gunzipped(self, tmp_path):
        gzipped = BytesIO()
        with gzip.GzipFile(fileobj=gzipped, mode="wb") as gz:
            gz.write(b"col1,col2\nx,42\n")
        payload = gzipped.getvalue()

        sess = MagicMock()
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        single_resp.iter_content.return_value = [payload]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file(
            {
                "url": "https://signed/single.csv.gz",
                "name": "single.csv.gz",
                "isSliced": False,
            },
            dest,
        )

        assert dest.read_bytes() == b"col1,col2\nx,42\n"

    def test_sliced_concat_in_order(self, tmp_path):
        # isSliced=True: detail.url points at a JSON manifest of slice URLs.
        # Simulate two slices: slice 0 (header + rows), slice 1 (more rows,
        # NO header per Storage API contract). We just concatenate bytes —
        # the contract test is "every slice's bytes appear in dest, in order".
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "https://signed/slice-0"},
                {"url": "https://signed/slice-1"},
            ]
        }
        manifest_resp.raise_for_status = MagicMock()

        slice0 = MagicMock()
        slice0.__enter__ = MagicMock(return_value=slice0)
        slice0.__exit__ = MagicMock(return_value=False)
        slice0.iter_content.return_value = [b"col\n", b"a\n"]
        slice0.raise_for_status = MagicMock()

        slice1 = MagicMock()
        slice1.__enter__ = MagicMock(return_value=slice1)
        slice1.__exit__ = MagicMock(return_value=False)
        slice1.iter_content.return_value = [b"b\n"]
        slice1.raise_for_status = MagicMock()

        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file(
            {
                "url": "https://signed/manifest.json",
                "name": "sliced",
                "isSliced": True,
            },
            dest,
        )

        assert dest.read_bytes() == b"col\na\nb\n"

    def test_sliced_concat_reorders_out_of_order_manifest(self, tmp_path):
        # Regression test (2026-07-15): a real garbled-header parquet was
        # traced to _download_sliced trusting the manifest's raw `entries`
        # array order. This manifest lists slice 1 BEFORE slice 0 (Keboola's
        # real numbered-filename convention, e.g. `export_0_0_0.csv`) — the
        # client must still download and concatenate the header slice
        # (index 0) first.
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "https://signed/export_0_0_1.csv"},
                {"url": "https://signed/export_0_0_0.csv"},
            ]
        }
        manifest_resp.raise_for_status = MagicMock()

        slice0 = MagicMock()
        slice0.__enter__ = MagicMock(return_value=slice0)
        slice0.__exit__ = MagicMock(return_value=False)
        slice0.iter_content.return_value = [b"col\n", b"a\n"]
        slice0.raise_for_status = MagicMock()

        slice1 = MagicMock()
        slice1.__enter__ = MagicMock(return_value=slice1)
        slice1.__exit__ = MagicMock(return_value=False)
        slice1.iter_content.return_value = [b"b\n"]
        slice1.raise_for_status = MagicMock()

        # side_effect is the actual call order the client makes: manifest
        # first, then whichever slice URL it requests first. After the fix
        # that must be export_0_0_0.csv (the header slice) despite it being
        # SECOND in the manifest's entries array.
        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file(
            {
                "url": "https://signed/manifest.json",
                "name": "sliced",
                "isSliced": True,
            },
            dest,
        )

        assert dest.read_bytes() == b"col\na\nb\n"
        requested_urls = [call.args[0] for call in sess.get.call_args_list]
        assert requested_urls == [
            "https://signed/manifest.json",
            "https://signed/export_0_0_0.csv",
            "https://signed/export_0_0_1.csv",
        ]


class TestSliceSortKey:
    def test_single_digit_slices_sort_numerically(self):
        urls = ["https://x/export_0_0_1.csv", "https://x/export_0_0_0.csv"]
        assert sorted(urls, key=_slice_sort_key) == [
            "https://x/export_0_0_0.csv",
            "https://x/export_0_0_1.csv",
        ]

    def test_multi_digit_slices_sort_numerically_not_lexicographically(self):
        # Plain string sort would put "_0_0_12" before "_0_0_2" (lexical
        # "1" < "2"); natural sort must order them numerically.
        urls = ["https://x/export_0_0_12.csv", "https://x/export_0_0_2.csv"]
        assert sorted(urls, key=_slice_sort_key) == [
            "https://x/export_0_0_2.csv",
            "https://x/export_0_0_12.csv",
        ]

    def test_already_sorted_input_is_a_no_op(self):
        urls = ["https://x/export_0_0_0.csv", "https://x/export_0_0_1.csv"]
        assert sorted(urls, key=_slice_sort_key) == urls

    def test_presigned_query_string_digits_do_not_perturb_order(self):
        # Real S3/Azure signed URLs carry digit-heavy query strings
        # (signatures, expiry epochs) that vary per slice. Only the path's
        # slice index must drive the sort — the header slice (index 0) must
        # come first even when the query string of a later slice sorts lower.
        urls = [
            "https://s3.example.com/b/export_0_0_1.csv?X-Amz-Expires=3600&X-Amz-Signature=000aaa",
            "https://s3.example.com/b/export_0_0_0.csv?X-Amz-Expires=3600&X-Amz-Signature=999zzz",
        ]
        assert sorted(urls, key=_slice_sort_key) == [
            "https://s3.example.com/b/export_0_0_0.csv?X-Amz-Expires=3600&X-Amz-Signature=999zzz",
            "https://s3.example.com/b/export_0_0_1.csv?X-Amz-Expires=3600&X-Amz-Signature=000aaa",
        ]


# ---- _gs_to_https / _azure_to_https URL rewriting --------------------------


class TestGsToHttps:
    def test_encodes_bucket_and_key_for_json_api_media_url(self):
        # Matches google-cloud-storage SDK's `bucket.blob(key).download_as_bytes()`
        # wire shape: slashes and spaces inside the key are percent-encoded
        # into a single path segment, `alt=media` switches JSON-metadata to
        # raw bytes.
        url = KeboolaStorageClient._gs_to_https("gs://bkt/a/b c.csv")
        assert url == "https://storage.googleapis.com/storage/v1/b/bkt/o/a%2Fb%20c.csv?alt=media"

    def test_rejects_non_gs_scheme(self):
        with pytest.raises(ValueError, match="expects gs://"):
            KeboolaStorageClient._gs_to_https("https://not-gs/x")

    def test_rejects_malformed_url_missing_key(self):
        with pytest.raises(ValueError, match="malformed"):
            KeboolaStorageClient._gs_to_https("gs://bucket-only-no-key")


class TestAzureToHttps:
    def test_appends_sas_token_from_connection_string(self):
        url = KeboolaStorageClient._azure_to_https(
            "azure://acct.blob.core.windows.net/container/blob.csv",
            {
                "SASConnectionString": (
                    "BlobEndpoint=https://acct.blob.core.windows.net;SharedAccessSignature=sv=2020-01-01&sig=abc"
                ),
            },
        )
        assert url == "https://acct.blob.core.windows.net/container/blob.csv?sv=2020-01-01&sig=abc"

    def test_missing_credentials_returns_url_without_token(self):
        url = KeboolaStorageClient._azure_to_https(
            "azure://acct.blob.core.windows.net/container/blob.csv",
            {},
        )
        assert url == "https://acct.blob.core.windows.net/container/blob.csv"

    def test_rejects_non_azure_scheme(self):
        with pytest.raises(ValueError, match="expects azure://"):
            KeboolaStorageClient._azure_to_https("https://not-azure/x", {})


# ---- sliced GCP download (download_file) ------------------------------------


class TestSlicedGcpDownload:
    """`download_file` sliced branch when the manifest carries `gs://`
    entries — previously ZERO coverage, every other sliced test in this
    module uses https:// entries which skip the GCS rewrite path entirely."""

    def test_rewrites_gs_urls_and_sends_bearer_token(self, tmp_path):
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "gs://bkt/exp/slice-0"},
                {"url": "gs://bkt/exp/slice-1"},
            ]
        }
        manifest_resp.raise_for_status = MagicMock()

        slice0 = MagicMock()
        slice0.__enter__ = MagicMock(return_value=slice0)
        slice0.__exit__ = MagicMock(return_value=False)
        slice0.iter_content.return_value = [b"col\n", b"a\n"]
        slice0.raise_for_status = MagicMock()

        slice1 = MagicMock()
        slice1.__enter__ = MagicMock(return_value=slice1)
        slice1.__exit__ = MagicMock(return_value=False)
        slice1.iter_content.return_value = [b"b\n"]
        slice1.raise_for_status = MagicMock()

        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file(
            {
                "url": "https://signed/manifest.json",
                "name": "sliced",
                "isSliced": True,
                "gcsCredentials": {"access_token": "gcs-bearer-tok"},
            },
            dest,
        )

        # Header kept from slice 0 only, concat order preserved.
        assert dest.read_bytes() == b"col\na\nb\n"

        assert sess.get.call_count == 3
        slice0_call = sess.get.call_args_list[1]
        slice1_call = sess.get.call_args_list[2]
        assert slice0_call.args[0] == "https://storage.googleapis.com/storage/v1/b/bkt/o/exp%2Fslice-0?alt=media"
        assert slice0_call.kwargs["headers"] == {"Authorization": "Bearer gcs-bearer-tok"}
        assert slice1_call.args[0] == "https://storage.googleapis.com/storage/v1/b/bkt/o/exp%2Fslice-1?alt=media"
        assert slice1_call.kwargs["headers"] == {"Authorization": "Bearer gcs-bearer-tok"}

    def test_missing_gcs_credentials_raises_storage_api_error(self, tmp_path):
        sess = MagicMock()
        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {"entries": [{"url": "gs://bkt/slice-0"}]}
        manifest_resp.raise_for_status = MagicMock()
        sess.get.return_value = manifest_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"

        with pytest.raises(StorageApiError, match="no gcs_token"):
            c.download_file(
                {
                    "url": "https://signed/manifest.json",
                    "name": "sliced",
                    "isSliced": True,
                    # no gcsCredentials at all
                },
                dest,
            )


# ---- end-to-end export_table_to_csv ---------------------------------------


class TestExportTableToCsv:
    def test_full_pipeline_calls_post_poll_detail_download(self, tmp_path):
        """Smoke: export-async → wait_for_job → file_detail → download.
        Mock the session at the boundary; assert the URL composition and
        order of operations match the contract. The actual bytes-written
        path is covered by TestDownloadFile."""
        sess = MagicMock()

        # 1) POST /tables/X/export-async → {id: 100}
        export_resp = _mock_response(200, {"id": 100})

        # 2) GET /jobs/100 → success with file id 200
        job_resp = _mock_response(
            200,
            {
                "id": 100,
                "status": "success",
                "results": {"file": {"id": 200}, "totalRowsCount": 5},
            },
        )

        # 3) GET /files/200?federationToken=1 → single non-sliced URL
        file_resp = _mock_response(
            200,
            {
                "url": "https://signed/file.csv",
                "name": "file.csv",
                "isSliced": False,
            },
        )

        # 4) GET https://signed/file.csv (download)
        download_resp = MagicMock()
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)
        download_resp.iter_content.return_value = [b"col\n1\n"]
        download_resp.raise_for_status = MagicMock()

        # session.get is called for: jobs (poll), file detail, download.
        # session.post for the export-async kickoff.
        sess.post.return_value = export_resp
        sess.get.side_effect = [job_resp, file_resp, download_resp]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        stats = c.export_table_to_csv(
            "in.c-x.t",
            dest,
            export_filter=ExportFilter(columns=["col"]),
        )

        assert dest.read_bytes() == b"col\n1\n"
        assert stats["job_id"] == 100
        assert stats["file_id"] == 200
        assert stats["rows"] == 5
        assert stats["bytes"] == len(b"col\n1\n")

        # Assert export-async POST URL composition + body shape
        post_url = sess.post.call_args.args[0]
        assert post_url == "https://kbc/v2/storage/tables/in.c-x.t/export-async"
        post_body = sess.post.call_args.kwargs["data"]
        assert post_body["columns"] == "col"

    def test_missing_job_id_in_response_is_typed_error(self):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {})  # no `id`
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="missing job id"):
            c.export_table_to_csv("in.c-x.t", Path("/tmp/x"))

    def test_missing_file_in_job_results_is_typed_error(self, tmp_path):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 1})
        sess.get.return_value = _mock_response(
            200,
            {
                "id": 1,
                "status": "success",
                "results": {},  # no `file`
            },
        )
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="no result file"):
            c.export_table_to_csv("in.c-x.t", tmp_path / "x")


# ---- prepare_export + download_file_slices (parquet path) ------------------


class TestParquetPath:
    def test_parquet_request_emits_fileType_in_post_body(self, tmp_path):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 100})
        sess.get.side_effect = [
            _mock_response(
                200,
                {
                    "id": 100,
                    "status": "success",
                    "results": {"file": {"id": 200}, "totalRowsCount": 3},
                },
            ),
            _mock_response(
                200,
                {
                    "id": 200,
                    "url": "https://signed/x.parquet",
                    "name": "x.parquet",
                    "isSliced": False,
                },
            ),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        prep = c.prepare_export(
            "in.c-x.t",
            export_filter=ExportFilter(file_type=FILE_TYPE_PARQUET),
        )

        assert prep["file_type"] == "parquet"
        assert prep["file_info"]["isSliced"] is False
        assert sess.post.call_args.kwargs["data"]["fileType"] == "parquet"

    def test_export_table_rejects_sliced_parquet(self, tmp_path):
        """Concatenating sliced parquet would corrupt per-slice footers.
        ``export_table`` must fail loud and direct callers at
        ``download_file_slices``."""
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 1})
        sess.get.side_effect = [
            _mock_response(
                200,
                {
                    "id": 1,
                    "status": "success",
                    "results": {"file": {"id": 2}},
                },
            ),
            _mock_response(
                200,
                {
                    "id": 2,
                    "url": "https://signed/manifest.json",
                    "name": "x.parquet",
                    "isSliced": True,
                },
            ),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced parquet"):
            c.export_table(
                "in.c-x.t",
                tmp_path / "x.parquet",
                export_filter=ExportFilter(file_type=FILE_TYPE_PARQUET),
            )

    def test_download_file_slices_returns_per_slice_paths(self, tmp_path):
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "https://signed/slice-0"},
                {"url": "https://signed/slice-1"},
            ],
        }
        manifest_resp.raise_for_status = MagicMock()

        def mk_chunk_resp(payload: bytes):
            r = MagicMock()
            r.__enter__ = MagicMock(return_value=r)
            r.__exit__ = MagicMock(return_value=False)
            r.iter_content.return_value = [payload]
            r.raise_for_status = MagicMock()
            return r

        slice0 = mk_chunk_resp(b"PAR1...slice0...")
        slice1 = mk_chunk_resp(b"PAR1...slice1...")
        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        paths = c.download_file_slices(
            {"url": "https://signed/manifest.json", "isSliced": True, "name": "x.parquet"},
            tmp_path / "slices",
        )

        assert len(paths) == 2
        assert paths[0].read_bytes() == b"PAR1...slice0..."
        assert paths[1].read_bytes() == b"PAR1...slice1..."
        # Naming preserves manifest order — required for deterministic
        # downstream merge.
        assert paths[0].name < paths[1].name

    def test_download_file_slices_gcp_rewrites_url_and_sends_bearer(self, tmp_path):
        """Parquet path, GCP manifest — same `gs://` rewrite + bearer-token
        contract as the CSV `_download_sliced` branch, previously untested."""
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [{"url": "gs://bkt/exp/x.parquet"}],
        }
        manifest_resp.raise_for_status = MagicMock()

        def mk_chunk_resp(payload: bytes):
            r = MagicMock()
            r.__enter__ = MagicMock(return_value=r)
            r.__exit__ = MagicMock(return_value=False)
            r.iter_content.return_value = [payload]
            r.raise_for_status = MagicMock()
            return r

        slice0 = mk_chunk_resp(b"PAR1...slice0...")
        sess.get.side_effect = [manifest_resp, slice0]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        paths = c.download_file_slices(
            {
                "url": "https://signed/manifest.json",
                "isSliced": True,
                "name": "x.parquet",
                "gcsCredentials": {"access_token": "gcs-bearer-tok"},
            },
            tmp_path / "slices",
        )

        assert len(paths) == 1
        assert paths[0].read_bytes() == b"PAR1...slice0..."
        slice_call = sess.get.call_args_list[1]
        assert slice_call.args[0] == "https://storage.googleapis.com/storage/v1/b/bkt/o/exp%2Fx.parquet?alt=media"
        assert slice_call.kwargs["headers"] == {"Authorization": "Bearer gcs-bearer-tok"}

    def test_download_file_slices_refuses_non_sliced(self):
        c = KeboolaStorageClient(url="https://kbc", token="t", session=MagicMock())
        with pytest.raises(StorageApiError, match="non-sliced"):
            c.download_file_slices(
                {"url": "https://x", "isSliced": False},
                Path("/tmp/x"),
            )

    def test_get_temp_root_unset_returns_none(self, monkeypatch):
        """No env var → None → tempfile falls back to system default
        (typically /tmp). Preserves OSS-pre-fix behaviour for users
        who haven't set AGNES_TEMP_DIR."""
        monkeypatch.delenv("AGNES_TEMP_DIR", raising=False)
        assert get_temp_root() is None

    def test_get_temp_root_creates_dir_when_missing(self, monkeypatch, tmp_path):
        """First-time use: target dir doesn't yet exist; helper mkdirs
        it (non-recursive parents handled by exist_ok). Returns the
        absolute path so tempfile uses it as the parent for staging."""
        target = tmp_path / "agnes-tmp-fresh"
        assert not target.exists()
        monkeypatch.setenv("AGNES_TEMP_DIR", str(target))
        assert get_temp_root() == str(target)
        assert target.is_dir()

    def test_get_temp_root_existing_dir_reused(self, monkeypatch, tmp_path):
        target = tmp_path / "agnes-tmp-existing"
        target.mkdir()
        monkeypatch.setenv("AGNES_TEMP_DIR", str(target))
        assert get_temp_root() == str(target)

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason=(
            "root can create /nonexistent/, so the helper succeeds and there is "
            "no unwritable path to test. Worse, the failing run LEAVES the "
            "directory behind — and tests/test_chat_config.py uses /nonexistent "
            "as its stand-in for a path that does not exist, so three unrelated "
            "tests start failing too. Skipped rather than rewritten: the "
            "behaviour under test is real and holds for the non-root user CI "
            "runs as."
        ),
    )
    def test_get_temp_root_unwritable_falls_back(self, monkeypatch, tmp_path, caplog):
        """Sandboxes / read-only mounts make the target uncreatable; the
        helper logs a warning and returns None so tempfile falls back
        to the system default rather than blowing up the sync run."""
        # Point at a path under a read-only parent that doesn't exist.
        unwritable = "/nonexistent/forbidden/agnes-tmp"
        monkeypatch.setenv("AGNES_TEMP_DIR", unwritable)
        with caplog.at_level("WARNING"):
            assert get_temp_root() is None
        assert any("AGNES_TEMP_DIR" in r.message for r in caplog.records)

    def test_get_temp_root_empty_string_treated_as_unset(self, monkeypatch):
        # Operator who left ``AGNES_TEMP_DIR=`` (empty) in .env doesn't
        # get an mkdir of "" — same as unset.
        monkeypatch.setenv("AGNES_TEMP_DIR", "")
        assert get_temp_root() is None

    def test_parquet_download_does_not_gunzip_plain_parquet(self, tmp_path):
        """Regression: previous heuristic flagged any unencrypted file as
        gzipped, which would corrupt parquet downloads at gunzip time.
        Verify a `.parquet` file is written through unmodified."""
        sess = MagicMock()
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        # Real parquet magic bytes — not valid gzip, would crash gunzip.
        single_resp.iter_content.return_value = [b"PAR1\x00\x00\x00binary"]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"
        c.download_file(
            {
                "url": "https://signed/x.parquet",
                "name": "x.parquet",
                "isSliced": False,
                "isEncrypted": False,
            },
            dest,
        )

        assert dest.read_bytes() == b"PAR1\x00\x00\x00binary"


# ---- sweep_orphaned_scratch ------------------------------------------------


class TestSweepOrphanedScratch:
    """Orphaned ``kbc-export-*`` staging dirs are left behind only when a
    sync worker is hard-killed (SIGKILL/OOM/auto-upgrade container recreate)
    mid-export, so ``TemporaryDirectory.__exit__`` never ran. The sweep
    reclaims them on the next sync; age-gating protects an in-flight export.
    """

    def _mk_dir(self, parent: Path, name: str, age_seconds: float) -> Path:
        d = parent / name
        d.mkdir()
        (d / "slice0.parquet").write_bytes(b"PAR1junk")
        old = time.time() - age_seconds
        import os as _os

        _os.utime(d, (old, old))
        return d

    def test_removes_old_scratch_dirs(self, tmp_path):
        old = self._mk_dir(tmp_path, "kbc-export-foo-abc123", age_seconds=7200)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 1
        assert not old.exists()

    def test_removes_old_slice_dirs(self, tmp_path):
        """`kbc-slice-*` dirs (the sliced-CSV download path in
        `_download_sliced`) orphan on the same hard-kill and are swept too."""
        old = self._mk_dir(tmp_path, "kbc-slice-xyz789", age_seconds=7200)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 1
        assert not old.exists()

    def test_removes_old_duckdb_spill_dirs(self, tmp_path):
        """The per-connection DuckDB spill dirs the consolidation connections
        point ``temp_directory`` at (``kbc-spill-*``, see
        ``connectors/keboola/extractor.py:_open_consolidation_conn``) orphan on
        the same hard kill — DuckDB removes the directory itself only on a
        clean close — and are swept by the same pass. Without this the spill
        (capped at 10 GB per connection) sits on the data disk forever."""
        d = tmp_path / f"{sapi.SPILL_DIR_PREFIX}4242-deadbeef"
        d.mkdir()
        (d / "duckdb_temp_storage_DEFAULT-0.tmp").write_bytes(b"\0" * 1024)
        old = time.time() - 7200
        os.utime(d, (old, old))

        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 1
        assert not d.exists()

    def test_keeps_fresh_duckdb_spill_dir(self, tmp_path):
        """A spill dir younger than the threshold belongs to a live
        consolidation — DuckDB is still reading blocks back out of it."""
        d = tmp_path / f"{sapi.SPILL_DIR_PREFIX}4242-c0ffee"
        d.mkdir()
        (d / "duckdb_temp_storage_DEFAULT-0.tmp").write_bytes(b"\0" * 1024)

        assert sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600) == 0
        assert d.exists()

    def test_keeps_fresh_scratch_dir(self, tmp_path):
        """A dir younger than the threshold may belong to a concurrent
        in-flight export — never sweep it."""
        fresh = self._mk_dir(tmp_path, "kbc-export-bar-def456", age_seconds=10)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 0
        assert fresh.exists()

    def test_ignores_non_scratch_entries(self, tmp_path):
        """Only ``kbc-export-*`` dirs are swept; unrelated files/dirs in the
        temp root (the data disk also holds extracts/, state/, etc.) are
        never touched even when old."""
        keep_dir = self._mk_dir(tmp_path, "extracts", age_seconds=7200)
        keep_file = tmp_path / "kbc-export-not-a-dir.txt"
        keep_file.write_text("x")
        old_file = time.time() - 7200
        import os as _os

        _os.utime(keep_file, (old_file, old_file))

        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 0
        assert keep_dir.exists()
        assert keep_file.exists()

    def test_none_root_is_noop(self):
        """No temp root configured (AGNES_TEMP_DIR unset) → nothing to sweep."""
        assert sweep_orphaned_scratch(root=None, max_age_seconds=3600) == 0

    def test_missing_root_is_noop(self, tmp_path):
        assert sweep_orphaned_scratch(root=str(tmp_path / "does-not-exist"), max_age_seconds=3600) == 0

    def test_max_age_from_env_default(self, tmp_path, monkeypatch):
        """Threshold falls back to AGNES_SCRATCH_MAX_AGE_SEC when not passed."""
        monkeypatch.setenv("AGNES_SCRATCH_MAX_AGE_SEC", "100")
        old = self._mk_dir(tmp_path, "kbc-export-baz-ghi789", age_seconds=200)
        fresh = self._mk_dir(tmp_path, "kbc-export-qux-jkl012", age_seconds=10)
        removed = sweep_orphaned_scratch(root=str(tmp_path))
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()


# ---- warn_if_scratch_survived ----------------------------------------------


class TestWarnIfScratchSurvived:
    """`ignore_cleanup_errors=True` on the owning TemporaryDirectory silently
    swallows a cleanup failure (PROD incident 2026-07-16: two 12 GiB dirs
    survived with nothing in the logs explaining why). This is the visibility
    backstop — it does not delete anything, only logs."""

    def test_logs_warning_when_dir_survives(self, tmp_path, caplog):
        survivor = tmp_path / "kbc-export-foo-abc123"
        survivor.mkdir()
        with caplog.at_level("WARNING", logger="connectors.keboola.storage_api"):
            warn_if_scratch_survived(str(survivor))
        assert any("still present after TemporaryDirectory cleanup" in r.message for r in caplog.records)

    def test_no_warning_when_dir_is_gone(self, tmp_path, caplog):
        cleaned = tmp_path / "kbc-export-bar-def456"
        with caplog.at_level("WARNING", logger="connectors.keboola.storage_api"):
            warn_if_scratch_survived(str(cleaned))
        assert caplog.records == []


# ---- get_table_info --------------------------------------------------------


class TestGetTableInfo:
    """`get_table_info` is a thin wrapper around the existing _get path
    so the metadata provider doesn't have to bleed `_get` out of the
    module (#155)."""

    def test_calls_storage_api_with_table_id(self, monkeypatch):
        from connectors.keboola.storage_api import KeboolaStorageClient

        captured = {}

        def fake_get(self, path, **kwargs):
            captured["path"] = path
            return {"rowsCount": 100, "dataSizeBytes": 4096}

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(url="https://connection.keboola.com", token="tok")
        info = client.get_table_info("in.c-orders.events")
        assert captured["path"] == "/tables/in.c-orders.events"
        assert info["rowsCount"] == 100
        assert info["dataSizeBytes"] == 4096

    def test_propagates_storage_api_error(self, monkeypatch):
        from connectors.keboola.storage_api import (
            KeboolaStorageClient,
            StorageApiError,
        )

        def fake_get(self, path, **kwargs):
            raise StorageApiError("404 not found", status=404, body={})

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(url="https://x", token="tok")
        import pytest

        with pytest.raises(StorageApiError):
            client.get_table_info("missing.table")


# ---- _download_single disk-space pre-flight (#431 / #432) ------------------


def _streaming_resp(*, headers, chunks):
    """Build a MagicMock that behaves like a streaming ``requests`` response
    used as a context manager: ``with session.get(...) as r``."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.raise_for_status = MagicMock()
    # Real dict so ``r.headers.get('Content-Length')`` returns the literal
    # value (or None) we control — a bare MagicMock would return a MagicMock
    # and silently fall through the pre-flight via the int() TypeError path.
    resp.headers = dict(headers)
    resp.iter_content = MagicMock(return_value=list(chunks))
    return resp


class TestDownloadDiskPreflight:
    @pytest.mark.parametrize(
        "gunzip_on_read, free, should_raise",
        [
            # expected_bytes = 1e9. non-gunzip needs 1.25x = 1.25e9;
            # gunzip needs 5x = 5e9. free=2e9 clears the 1.25x bar but
            # not the 5x bar -> pins the multiplier branch.
            (False, 2_000_000_000, False),
            (True, 2_000_000_000, True),
            # tiny free always fails, both branches.
            (False, 100, True),
            (True, 100, True),
        ],
    )
    def test_multiplier_branch(self, tmp_path, gunzip_on_read, free, should_raise):
        sess = MagicMock()
        resp = _streaming_resp(
            headers={"Content-Length": "1000000000"},
            chunks=[b"PAR1payload"],
        )
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        fake_usage = MagicMock(return_value=MagicMock(free=free))
        with patch.object(sapi.shutil, "disk_usage", fake_usage):
            if should_raise:
                with pytest.raises(StorageApiError, match="insufficient disk space"):
                    c._download_single("https://signed/x", dest, gunzip_on_read=gunzip_on_read)
                # The raise must fire BEFORE the write loop.
                resp.iter_content.assert_not_called()
                assert not dest.exists()
            else:
                c._download_single("https://signed/x", dest, gunzip_on_read=gunzip_on_read)
                resp.iter_content.assert_called()
                assert dest.exists()

    def test_raises_storage_api_error_when_free_below_needed(self, tmp_path):
        """Insufficient free space -> StorageApiError raised BEFORE the write
        loop (iter_content never touched)."""
        sess = MagicMock()
        resp = _streaming_resp(
            headers={"Content-Length": "1000000000"},
            chunks=[b"PAR1payload"],
        )
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        with patch.object(sapi.shutil, "disk_usage", return_value=MagicMock(free=100)):
            with pytest.raises(StorageApiError, match="insufficient disk space"):
                c._download_single("https://signed/x", dest, gunzip_on_read=False)
        resp.iter_content.assert_not_called()
        assert not dest.exists()

    def test_absent_content_length_falls_through(self, tmp_path):
        """No Content-Length header -> the whole pre-flight block is skipped:
        no exception, the file is written, and shutil.disk_usage is never
        called."""
        sess = MagicMock()
        resp = _streaming_resp(headers={}, chunks=[b"PAR1data"])
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        fake_usage = MagicMock()
        with patch.object(sapi.shutil, "disk_usage", fake_usage):
            c._download_single("https://signed/x", dest, gunzip_on_read=False)

        assert dest.exists()
        assert dest.read_bytes() == b"PAR1data"
        fake_usage.assert_not_called()


# ---- empty sliced exports --------------------------------------------------
#
# A sliced-export manifest with zero entries used to be an unconditional
# StorageApiError, which made a table that is legitimately empty upstream
# (a form-fields table with no attachments, say) permanently red and
# indistinguishable from a broken extraction. The table detail's `rowsCount`
# now separates the two states. Both sliced entry points — the CSV
# `_download_sliced` and the parquet `download_file_slices` — must route
# through the same decision, so the error branches are parametrized over
# both call sites.


def _empty_manifest_session(table_detail=None, *, detail_status=200):
    """Session whose first GET returns an entries-less sliced manifest and
    whose second (when ``table_detail`` is given) answers the
    ``get_table_info`` lookup the empty-manifest branch makes."""
    sess = MagicMock()
    manifest_resp = MagicMock()
    manifest_resp.json.return_value = {"entries": []}
    manifest_resp.raise_for_status = MagicMock()
    responses = [manifest_resp]
    if table_detail is not None:
        responses.append(_mock_response(detail_status, table_detail))
    sess.get.side_effect = responses
    return sess


_SLICED_CSV_FILE_INFO = {
    "url": "https://signed/manifest.json",
    "name": "export.csv",
    "isSliced": True,
}
_SLICED_PARQUET_FILE_INFO = {
    "url": "https://signed/manifest.json",
    "name": "export.parquet",
    "isSliced": True,
}


def _run_csv_site(client, tmp_path, table_id):
    """`download_file` → `_download_sliced` (the CSV entry point)."""
    dest = tmp_path / "out.csv"
    client.download_file(_SLICED_CSV_FILE_INFO, dest, table_id=table_id)
    return dest


def _run_parquet_site(client, tmp_path, table_id):
    """`download_file_slices` (the parquet entry point)."""
    paths = client.download_file_slices(
        _SLICED_PARQUET_FILE_INFO,
        tmp_path / "slices",
        table_id=table_id,
    )
    assert len(paths) == 1, "empty export must still yield exactly one schema-bearing artifact"
    return paths[0]


_BOTH_SITES = pytest.mark.parametrize(
    "run_site",
    [_run_csv_site, _run_parquet_site],
    ids=["csv_download_file", "parquet_download_file_slices"],
)


class TestEmptySlicedExport:
    # -- branch 1: rowsCount == 0 → success, empty artifact with schema ------

    def test_csv_site_zero_rows_writes_header_only_export(self, tmp_path):
        """Storage API puts the CSV header in slice 0 and data in slices
        0..n, so "header, no data rows" IS the empty table's export. DuckDB
        must read it back as the declared columns with zero rows — that is
        what makes the downstream master view resolve."""
        sess = _empty_manifest_session({"rowsCount": 0, "columns": ["id", "answer_text"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        dest = _run_csv_site(c, tmp_path, "in.c-forms.answers")

        assert dest.read_text(encoding="utf-8") == "id,answer_text\n"
        safe = str(dest).replace("'", "''")
        res = duckdb.connect().execute(
            f"SELECT * FROM read_csv('{safe}', all_varchar=true, max_line_size=67108864, quote='\"', escape='\"')"
        )
        assert [d[0] for d in res.description] == ["id", "answer_text"]
        assert res.fetchall() == []

    def test_parquet_site_zero_rows_writes_empty_parquet_with_declared_schema(self, tmp_path):
        """The synthetic slice must be a real parquet carrying the declared
        columns — the extractor merges it through
        `read_parquet([...])`, and a view over a column-less file would not
        resolve."""
        sess = _empty_manifest_session({"rowsCount": 0, "columns": ["id", "answer_text", "created_at"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        slice_path = _run_parquet_site(c, tmp_path, "in.c-forms.answers")

        assert slice_path.exists()
        safe = str(slice_path).replace("'", "''")
        res = duckdb.connect().execute(f"SELECT * FROM read_parquet('{safe}')")
        assert [d[0] for d in res.description] == ["id", "answer_text", "created_at"]
        assert res.fetchall() == []

    def test_parquet_site_zero_rows_survives_the_extractor_merge_copy(self, tmp_path):
        """End of the chain the extractor actually walks: COPY over
        `read_parquet([slice])` into the published parquet, then COUNT(*).
        Columns survive, the count is 0 — so `_meta` records 0 rows rather
        than the sync failing."""
        sess = _empty_manifest_session({"rowsCount": 0, "columns": ["id", "answer_text"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        slice_path = _run_parquet_site(c, tmp_path, "in.c-forms.answers")

        merged = tmp_path / "merged.parquet"
        conn = duckdb.connect()
        conn.execute(f"COPY (SELECT * FROM read_parquet(['{slice_path}'])) TO '{merged}' (FORMAT PARQUET)")
        res = conn.execute(f"SELECT * FROM read_parquet('{merged}')")
        assert [d[0] for d in res.description] == ["id", "answer_text"]
        assert conn.execute(f"SELECT COUNT(*) FROM read_parquet('{merged}')").fetchone()[0] == 0

    @_BOTH_SITES
    def test_zero_rows_upstream_is_not_an_error(self, tmp_path, run_site):
        sess = _empty_manifest_session({"rowsCount": 0, "columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        artifact = run_site(c, tmp_path, "in.c-forms.answers")

        assert artifact.exists()

    @_BOTH_SITES
    def test_zero_rows_string_rows_count_is_still_empty_not_unknown(self, tmp_path, run_site):
        """Some stacks serialize `rowsCount` as a numeric string. `"0"` is
        still zero — not "unavailable"."""
        sess = _empty_manifest_session({"rowsCount": "0", "columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        assert run_site(c, tmp_path, "in.c-forms.answers").exists()

    # -- branch 2: rowsCount > 0 → still an error, said plainly --------------

    @_BOTH_SITES
    def test_rows_upstream_but_no_slices_is_still_an_error(self, tmp_path, run_site):
        sess = _empty_manifest_session({"rowsCount": 4200, "columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match=r"claims 4200 rows"):
            run_site(c, tmp_path, "in.c-forms.answers")

    @_BOTH_SITES
    def test_rows_upstream_error_names_the_table_and_the_lost_slices(self, tmp_path, run_site):
        sess = _empty_manifest_session({"rowsCount": 7, "columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError) as exc:
            run_site(c, tmp_path, "in.c-forms.answers")

        msg = str(exc.value)
        assert "in.c-forms.answers" in msg
        assert "no slices" in msg

    # -- branch 3: rowsCount unavailable → error, exactly as before ----------

    @_BOTH_SITES
    def test_missing_rows_count_field_raises_as_before(self, tmp_path, run_site):
        """Unknown must never present itself as empty."""
        sess = _empty_manifest_session({"columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced manifest had no entries"):
            run_site(c, tmp_path, "in.c-forms.answers")

    @_BOTH_SITES
    def test_no_table_id_to_ask_about_raises_as_before(self, tmp_path, run_site):
        """Callers that cannot name the table (no `table_id` threaded
        through) keep the pre-fix behaviour — and make no extra API call."""
        sess = _empty_manifest_session()
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced manifest had no entries"):
            run_site(c, tmp_path, None)

        assert sess.get.call_count == 1, "no table detail lookup without a table_id"

    @_BOTH_SITES
    def test_table_detail_lookup_failure_raises_as_before(self, tmp_path, run_site):
        """A 403 on the table detail leaves `rowsCount` unknown; the export
        must not be reported as an empty success."""
        sess = _empty_manifest_session({"error": "forbidden"}, detail_status=403)
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced manifest had no entries"):
            run_site(c, tmp_path, "in.c-forms.answers")

    @_BOTH_SITES
    def test_non_numeric_rows_count_raises_as_before(self, tmp_path, run_site):
        sess = _empty_manifest_session({"rowsCount": "unknown", "columns": ["id"]})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced manifest had no entries"):
            run_site(c, tmp_path, "in.c-forms.answers")

    # -- unchanged: a manifest WITH entries never asks for the table detail --

    def test_non_empty_manifest_makes_no_table_detail_call(self, tmp_path):
        sess = MagicMock()
        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {"entries": [{"url": "https://signed/slice-0"}]}
        manifest_resp.raise_for_status = MagicMock()
        slice0 = MagicMock()
        slice0.__enter__ = MagicMock(return_value=slice0)
        slice0.__exit__ = MagicMock(return_value=False)
        slice0.iter_content.return_value = [b"PAR1...slice0..."]
        slice0.raise_for_status = MagicMock()
        sess.get.side_effect = [manifest_resp, slice0]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        paths = c.download_file_slices(
            _SLICED_PARQUET_FILE_INFO,
            tmp_path / "slices",
            table_id="in.c-forms.answers",
        )

        assert len(paths) == 1
        assert sess.get.call_count == 2  # manifest + the slice, no table detail
