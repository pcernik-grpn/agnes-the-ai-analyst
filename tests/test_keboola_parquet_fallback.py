"""Keboola materialized export: capability-aware parquet → CSV fallback.

Background (#1979). A brand-new `query_mode='materialized'` Keboola row
registered through the plain path — `agnes admin register-table ...
--query-mode materialized` / `POST /api/admin/register-table`, i.e. **no**
`source_query` — defaults to `fileType=parquet` on the wire. Some Keboola
stacks/projects reject that outright:

    POST /v2/storage/tables/<bucket>.<table>/export-async
    -> HTTP 400 {'error': 'Invalid request:\\n - fileType: "The value you
                 selected is not a valid choice."'}

The `query_mode='local'` extract path never hit this because it exports CSV
(the `ExportFilter` default), so it puts no `fileType` on the wire at all —
which is exactly the wire-level difference these tests pin down first.

The fix keeps parquet as the default (typed, faster, no CSV intermediate)
and downgrades to CSV only for the projects that refuse it, remembering the
answer per stack so the rejection costs one wasted POST per process rather
than one per row.

Storage API is mocked throughout — the real client is exercised in
tests/test_keboola_storage_api.py.
"""

import importlib.util
import logging
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from connectors.keboola import extractor as kbe
from connectors.keboola import storage_api as kbs

STACK = "https://connection.us-east4.gcp.keboola.com/v2/storage"

# The exact body the live us-east4.gcp stack returned in #1979.
FILETYPE_REJECTION_BODY = {
    "error": 'Invalid request:\n - fileType: "The value you selected is not a valid choice."',
    "code": "validation.failed",
}


@pytest.fixture(autouse=True)
def _reset_capability_memo():
    """The parquet-capability memo is process-wide by design; keep it from
    leaking between tests (and out of this module into the rest of the run)."""
    kbs.reset_parquet_export_capability()
    yield
    kbs.reset_parquet_export_capability()


def _write_parquet(dest: Path, n_rows: int = 2) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe = str(dest).replace("'", "''")
    conn = duckdb.connect()
    try:
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {','.join('(' + str(i) + ')' for i in range(n_rows))}) AS t(id)) "
            f"TO '{safe}' (FORMAT PARQUET)"
        )
    finally:
        conn.close()


def _seed_csv(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")


def _client(*, parquet_supported: bool) -> MagicMock:
    """Mock KeboolaStorageClient. ``parquet_supported=False`` reproduces the
    #1979 stack: export-async 400s the moment ``fileType=parquet`` is sent."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        if not parquet_supported:
            raise kbs.StorageApiError(
                f"POST {STACK}/tables/{table_id}/export-async -> HTTP 400: {FILETYPE_REJECTION_BODY}",
                status=400,
                body=FILETYPE_REJECTION_BODY,
            )
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 2,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path, **kwargs):
        _write_parquet(Path(dest_path), n_rows=2)
        return Path(dest_path)

    def fake_export_table(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(Path(dest))
        return {"job_id": 101, "file_id": 201, "rows": 2, "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.base = STACK
    # Left a non-str on purpose: `materialize_query` only attempts the
    # typed-parquet retype (a real KeboolaClient metadata call) when both
    # `base` and `token` are strings, and this test suite is about the export
    # request, not about typing.
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download
    client.export_table.side_effect = fake_export_table
    return client


def _fallback_warnings(caplog) -> list:
    """Only the parquet→CSV downgrade warning. Narrow on purpose: the
    materialize path logs other warnings that also say "parquet" (e.g. the
    typed-retype fallback), and counting those would make "warned once" a
    lie."""
    return [r for r in caplog.records if r.levelno == logging.WARNING and "refuses parquet export" in r.getMessage()]


def _materialize(client, tmp_path, *, table_id="orders", source_query=None) -> dict:
    return kbe.materialize_query(
        table_id=table_id,
        bucket="in.c-sales",
        source_table="orders",
        source_query=source_query,
        storage_client=client,
        output_dir=tmp_path / "out",
    )


# ---- 1. the wire-level difference the bug report is about ------------------


def test_plain_materialized_row_puts_filetype_parquet_on_the_wire(tmp_path):
    """No source_query (plain register-table) → `fileType=parquet` is sent."""
    client = _client(parquet_supported=True)
    _materialize(client, tmp_path)

    export_filter = client.prepare_export.call_args.kwargs["export_filter"]
    assert export_filter.file_type == kbs.FILE_TYPE_PARQUET
    assert export_filter.to_export_params()["fileType"] == "parquet"


def test_local_extract_path_puts_no_filetype_on_the_wire():
    """The `query_mode='local'` extract path builds a default ExportFilter,
    which is CSV and therefore emits no `fileType` — why those rows never hit
    the 400 that the materialized rows on the same project do."""
    assert "fileType" not in kbs.ExportFilter().to_export_params()
    assert "fileType" not in kbs.ExportFilter(where_filters=[]).to_export_params()


# ---- 2. rejection classifier ----------------------------------------------


def test_classifier_matches_the_filetype_rejection():
    exc = kbs.StorageApiError("boom", status=400, body=FILETYPE_REJECTION_BODY)
    assert kbs.is_parquet_file_type_rejected(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        kbs.StorageApiError("boom", status=400, body={"error": "Table not found"}),
        kbs.StorageApiError("boom", status=400, body={"error": 'whereFilters: "not a valid choice."'}),
        kbs.StorageApiError("boom", status=403, body=FILETYPE_REJECTION_BODY),
        kbs.StorageApiError("boom", status=500, body=FILETYPE_REJECTION_BODY),
        RuntimeError("unrelated"),
    ],
    ids=["other-400", "other-field-400", "403", "500", "not-a-storage-error"],
)
def test_classifier_rejects_everything_else(exc):
    assert kbs.is_parquet_file_type_rejected(exc) is False


# ---- 3. fallback behaviour -------------------------------------------------


def test_parquet_rejection_falls_back_to_csv(tmp_path, caplog):
    client = _client(parquet_supported=False)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        result = _materialize(client, tmp_path)

    # Parquet was attempted, refused, and the CSV export carried the load.
    assert client.prepare_export.call_count == 1
    assert client.export_table.call_count == 1
    assert client.export_table.call_args.kwargs["export_filter"].file_type == kbs.FILE_TYPE_CSV

    # And it still produced the normal materialize result.
    parquet_path = tmp_path / "out" / "orders.parquet"
    assert parquet_path.exists()
    assert result["rows"] == 2
    assert result["path"] == str(parquet_path)

    warnings = _fallback_warnings(caplog)
    assert len(warnings) == 1
    assert "csv" in warnings[0].getMessage().lower()


def test_fallback_warns_once_and_skips_the_doomed_probe_for_later_rows(tmp_path, caplog):
    """One WARNING per stack, not one per row — and once the stack is known
    to refuse parquet, later rows go straight to CSV without re-probing."""
    client = _client(parquet_supported=False)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        _materialize(client, tmp_path, table_id="orders")
        _materialize(client, tmp_path, table_id="customers")
        _materialize(client, tmp_path, table_id="invoices")

    assert client.prepare_export.call_count == 1  # probed once, remembered
    assert client.export_table.call_count == 3
    assert len(_fallback_warnings(caplog)) == 1

    for name in ("orders", "customers", "invoices"):
        assert (tmp_path / "out" / f"{name}.parquet").exists()


def test_capability_is_remembered_per_stack(tmp_path):
    """A project that refuses parquet must not poison a different stack."""
    bad = _client(parquet_supported=False)
    _materialize(bad, tmp_path, table_id="orders")

    good = _client(parquet_supported=True)
    good.base = "https://connection.north-europe.azure.keboola.com/v2/storage"
    _materialize(good, tmp_path, table_id="customers")

    assert good.prepare_export.call_count == 1
    assert good.export_table.call_count == 0


def test_stack_that_accepts_parquet_never_retries(tmp_path, caplog):
    client = _client(parquet_supported=True)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        _materialize(client, tmp_path)

    assert client.prepare_export.call_count == 1
    assert client.export_table.call_count == 0
    assert _fallback_warnings(caplog) == []


def test_explicit_csv_spec_goes_straight_to_csv(tmp_path):
    client = _client(parquet_supported=True)
    _materialize(client, tmp_path, source_query='{"file_type":"csv"}')

    assert client.prepare_export.call_count == 0
    assert client.export_table.call_count == 1
    assert client.export_table.call_args.kwargs["export_filter"].file_type == kbs.FILE_TYPE_CSV


def test_unrelated_400_propagates_without_retry(tmp_path):
    client = _client(parquet_supported=True)
    client.prepare_export.side_effect = kbs.StorageApiError(
        "POST .../export-async -> HTTP 400: {'error': 'Table in.c-sales.orders not found'}",
        status=400,
        body={"error": "Table in.c-sales.orders not found"},
    )

    with pytest.raises(kbs.StorageApiError):
        _materialize(client, tmp_path)

    assert client.export_table.call_count == 0
    # A failed materialize leaves no half-written parquet behind.
    assert not (tmp_path / "out" / "orders.parquet").exists()
    # And it does not mark the stack as parquet-incapable.
    assert kbs.parquet_export_supported(STACK) is True


def test_explicitly_pinned_parquet_also_falls_back(tmp_path, caplog):
    """An admin who pinned parquet gets the fallback too.

    The pin exists to force a format, not to force a failure: the CSV export
    carries the same rows, so refusing to sync the table would cost the admin
    their data for no gain. It stays visible — the same WARNING fires.
    """
    client = _client(parquet_supported=False)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        _materialize(client, tmp_path, source_query='{"file_type":"parquet"}')

    assert client.export_table.call_count == 1
    assert (tmp_path / "out" / "orders.parquet").exists()
    assert len(_fallback_warnings(caplog)) == 1


# ---- 4. the CSV path retypes columns too (#1979 follow-up) -----------------
#
# The initial fallback fix (b16f52ffa) left the CSV branch reading
# `all_varchar=true` and writing straight to parquet with no retype step —
# a project on the fallback got every materialized column as VARCHAR, silently,
# while the WARNING claimed "the data is identical". These tests pin the fix:
# the CSV branch must apply the exact same best-effort retype the native
# parquet branch does.

_needs_kbcstorage = pytest.mark.skipif(
    importlib.util.find_spec("kbcstorage") is None,
    reason="requires the [server] extra (kbcstorage)",
)


def _client_with_real_credentials(*, parquet_supported: bool, csv_text: str) -> MagicMock:
    """Like `_client`, but with real string `.token`/`.base` so the
    `isinstance` guard in `_retype_best_effort` actually engages, and a CSV
    export body the caller controls (to exercise typed-looking values)."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        if not parquet_supported:
            raise kbs.StorageApiError(
                f"POST {STACK}/tables/{table_id}/export-async -> HTTP 400: {FILETYPE_REJECTION_BODY}",
                status=400,
                body=FILETYPE_REJECTION_BODY,
            )
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 2,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_export_table(table_id, dest, *, export_filter=None, export_timeout=None):
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(csv_text, encoding="utf-8")
        return {"job_id": 101, "file_id": 201, "rows": 2, "bytes": dest_path.stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.base = STACK
    client.token = "fake-token"
    client.prepare_export.side_effect = fake_prepare
    client.export_table.side_effect = fake_export_table
    return client


_TYPED_CSV = "id,amount,signup_date\n1,100.5,2024-01-01\n2,250.0,2024-02-02\n"


def _typed_schema():
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("amount", pa.float64()),
            pa.field("signup_date", pa.date32()),
        ]
    )


@_needs_kbcstorage
def test_csv_fallback_retypes_columns_same_as_parquet_path(tmp_path, monkeypatch, caplog):
    """When the project refuses parquet and falls back to CSV, the
    materialized parquet must come out typed — not all-VARCHAR."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: _typed_schema())

    client = _client_with_real_credentials(parquet_supported=False, csv_text=_TYPED_CSV)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        result = _materialize(client, tmp_path)

    parquet_path = tmp_path / "out" / "orders.parquet"
    table = pq.read_table(parquet_path)
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.float64()
    assert table.schema.field("signup_date").type == pa.date32()
    assert table.column("amount").to_pylist() == [100.5, 250.0]
    assert result["rows"] == 2

    # The downgrade WARNING still fires exactly once, but no longer claims
    # the data is untouched between the two export formats.
    warnings = _fallback_warnings(caplog)
    assert len(warnings) == 1
    assert "identical" not in warnings[0].getMessage().lower()


@_needs_kbcstorage
def test_csv_fallback_keeps_varchar_when_schema_unavailable(tmp_path, monkeypatch, caplog):
    """Metadata API unreachable → the CSV fallback still succeeds, keeping
    Storage API's native (all-VARCHAR) types, exactly like the parquet path's
    existing graceful degradation. Must not raise."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    def raise_unreachable(self, tid):
        raise RuntimeError("metadata API unreachable")

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", raise_unreachable)

    client = _client_with_real_credentials(parquet_supported=False, csv_text=_TYPED_CSV)
    with caplog.at_level(logging.WARNING, logger="connectors.keboola.extractor"):
        _materialize(client, tmp_path)

    table = pq.read_table(tmp_path / "out" / "orders.parquet")
    assert table.schema.field("amount").type == pa.string()

    schema_warnings = [r for r in caplog.records if "schema unavailable" in r.getMessage()]
    assert len(schema_warnings) == 1


@_needs_kbcstorage
def test_explicit_csv_spec_also_retypes_columns(tmp_path, monkeypatch):
    """The explicit `{"file_type":"csv"}` pin reaches the same CSV branch as
    the automatic downgrade, so it gets the same retype — pinning to CSV
    trades speed for nothing else."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: _typed_schema())

    # Parquet is supported here — the pin, not a rejection, drives the CSV
    # branch, and retyping must engage all the same.
    client = _client_with_real_credentials(parquet_supported=True, csv_text=_TYPED_CSV)
    _materialize(client, tmp_path, source_query='{"file_type":"csv"}')

    assert client.prepare_export.call_count == 0
    table = pq.read_table(tmp_path / "out" / "orders.parquet")
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.float64()
