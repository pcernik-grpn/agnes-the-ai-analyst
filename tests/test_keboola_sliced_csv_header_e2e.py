"""End-to-end regression for #1916.

Some Keboola tables ingested with the FIRST DATA ROW as the parquet header
(column names looking like `2026-09-14`, `Manager - Data`, `19`). Root
cause: Storage API never puts a header in any slice of a sliced CSV
export, but `_download_sliced` assumed slice 0 did and just concatenated —
so the downstream `pd.read_csv(..., dtype=str)` (default `header=0`) read
the first *data* row as the column names.

This test exercises the REAL (non-monkeypatched)
`KeboolaStorageClient.export_table_to_csv` pipeline — export-async, job
poll, file detail, manifest, the table-detail lookup that now supplies the
synthesized header, and two genuinely headerless slices — against a mocked
`requests.Session`, then feeds the resulting CSV through
`connectors.keboola.parquet_io.csv_to_parquet`: the exact call the issue
traced the silent corruption to. Unit coverage for the header-synthesis
mechanism itself lives in `tests/test_keboola_storage_api.py`
(`TestDownloadFile.test_sliced_concat_*`, `TestSlicedCsvHeaderSynthesis`);
this is the one test that walks the whole pipe.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pyarrow.parquet as pq
import requests

from connectors.keboola.parquet_io import csv_to_parquet
from connectors.keboola.storage_api import ExportFilter, KeboolaStorageClient


def _mock_response(status: int, body) -> MagicMock:
    """Build a fake `requests.Response`-like object (mirrors the helper of
    the same name in tests/test_keboola_storage_api.py)."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


def _streaming_chunk_response(chunks) -> MagicMock:
    """A `with session.get(...) as r:` streaming response mock."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.iter_content = MagicMock(return_value=list(chunks))
    resp.raise_for_status = MagicMock()
    return resp


def test_headerless_sliced_csv_export_produces_parquet_with_declared_column_names(tmp_path):
    """The exact bug: a sliced export whose slices carry only data (no
    slice, ever, has a header) must not let the first data row become the
    parquet's column names. Real-world-shaped values like `2026-09-14` or
    `Manager - Data` must land in DATA cells, under the real declared
    columns -- not as column names."""
    sess = MagicMock()

    # 1) POST /tables/X/export-async
    sess.post.return_value = _mock_response(200, {"id": 100})

    # Call order for a sliced CSV export via export_table_to_csv:
    #   2) GET /jobs/100                     -> success, file id 200
    #   3) GET /files/200?federationToken=1  -> sliced manifest URL
    #   4) GET <manifest>                    -> two headerless slices
    #   5) GET /tables/X                     -> declared columns (header synthesis)
    #   6) GET <slice-0>, GET <slice-1>      -> pure data, no header anywhere
    job_resp = _mock_response(
        200,
        {
            "id": 100,
            "status": "success",
            "results": {"file": {"id": 200}, "totalRowsCount": 2},
        },
    )
    file_resp = _mock_response(
        200,
        {
            "url": "https://signed/manifest.json",
            "name": "export.csv",
            "isSliced": True,
        },
    )
    manifest_resp = MagicMock()
    manifest_resp.json.return_value = {
        "entries": [
            {"url": "https://signed/export_0_0_0.csv"},
            {"url": "https://signed/export_0_0_1.csv"},
        ]
    }
    manifest_resp.raise_for_status = MagicMock()
    detail_resp = _mock_response(200, {"columns": ["report_date", "manager", "count"]})
    slice0 = _streaming_chunk_response([b"2026-09-14,Manager - Data,19\n"])
    slice1 = _streaming_chunk_response([b"2026-09-15,Manager - Data,21\n"])

    sess.get.side_effect = [job_resp, file_resp, manifest_resp, detail_resp, slice0, slice1]

    client = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
    csv_path = tmp_path / "export.csv"
    client.export_table_to_csv("in.c-main.reports", csv_path, export_filter=ExportFilter())

    # The CSV itself must carry the real header -- not "2026-09-14,..." as
    # column names.
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "report_date,manager,count"

    parquet_path = tmp_path / "export.parquet"
    csv_to_parquet(csv_path, parquet_path)

    table = pq.read_table(parquet_path)
    assert table.schema.names == ["report_date", "manager", "count"]
    assert table.num_rows == 2, "both data rows must survive -- none swallowed as a header"
    rows = table.to_pylist()
    assert rows[0] == {"report_date": "2026-09-14", "manager": "Manager - Data", "count": "19"}
    assert rows[1] == {"report_date": "2026-09-15", "manager": "Manager - Data", "count": "21"}
