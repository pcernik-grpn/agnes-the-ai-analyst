"""Tests for the Keboola materialize_query path.

Surface contract: takes ``bucket`` + ``source_table`` (+ optional
``source_query`` JSON filter spec), exports via Storage API, writes a
parquet, returns the same {table_id, path, rows, bytes, md5} shape the
BQ branch returns. We mock `KeboolaStorageClient` so tests don't hit
the network — the real Storage API client is exercised in
tests/test_keboola_storage_api.py.

The default code path is now **parquet** (Storage API serves Snowflake
UNLOAD output directly; the extractor renames into place — no CSV
intermediate, no DuckDB COPY of full file). Tests cover both the
default parquet path and the legacy CSV opt-in (via
``source_query='{"file_type":"csv"}'``).
"""

import hashlib
import importlib.util
import os
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from connectors.keboola import extractor as kbe


def _write_parquet(dest: Path, n_rows: int = 2) -> None:
    """Drop a tiny real parquet at ``dest`` so the materialize path can
    read it back to compute row_count + MD5 — same shape Snowflake
    UNLOAD would produce."""
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


def _seed_csv(dest: Path, header: str, rows: list[str]) -> None:
    """Write a tiny CSV the legacy CSV materialize path will convert to parquet."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def fake_storage_client_parquet():
    """Mock for the **default** parquet path. ``prepare_export`` returns a
    file_info marking a single (non-sliced) file. ``download_file``
    writes a real 2-row parquet at the requested dest."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 2,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        _write_parquet(Path(dest_path), n_rows=2)
        return Path(dest_path)

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download
    return client


@pytest.fixture
def fake_storage_client_csv():
    """Mock for the legacy CSV opt-in path. ``export_table`` writes a
    small CSV at dest. Used for tests that pin
    ``source_query='{"file_type":"csv"}'``."""

    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(Path(dest), "id,name", ["1,alpha", "2,beta"])
        return {"job_id": 100, "file_id": 200, "rows": 2, "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.export_table.side_effect = fake_export
    return client


# ---- source_table normalization (pre-fix wizard rows) ----------------------


def test_normalize_source_table_strips_bucket_prefix():
    from connectors.keboola.storage_api import normalize_source_table

    assert normalize_source_table("in.c-sales", "in.c-sales.orders") == "orders"
    # already bare → unchanged
    assert normalize_source_table("in.c-sales", "orders") == "orders"
    # different bucket prefix is NOT stripped (not ours to touch)
    assert normalize_source_table("in.c-sales", "in.c-other.orders") == "in.c-other.orders"
    # bucket-name-as-substring must not trigger (prefix match includes the dot)
    assert normalize_source_table("in.c-sales", "in.c-sales2.orders") == "in.c-sales2.orders"
    # degenerate inputs pass through
    assert normalize_source_table("", "in.c-sales.orders") == "in.c-sales.orders"
    assert normalize_source_table("in.c-sales", "") == ""


def test_materialize_query_heals_source_table_with_bucket_prefix(tmp_path, fake_storage_client_parquet):
    """Rows registered by the pre-fix Data-sources wizard stored the FULL
    Keboola table id in source_table; composing the export id then doubled
    the bucket (`in.c-sales.in.c-sales.orders`) and every export 404'd.
    The materialize path must strip the prefix at use so those rows heal
    without re-registration."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="orders",
        bucket="in.c-sales",
        source_table="in.c-sales.orders",  # full id, as the pre-fix wizard stored it
        source_query=None,
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    call_args = fake_storage_client_parquet.prepare_export.call_args
    assert call_args.args[0] == "in.c-sales.orders"  # NOT in.c-sales.in.c-sales.orders


# ---- default parquet path --------------------------------------------------


def test_materialize_query_writes_parquet_and_returns_metadata(tmp_path, fake_storage_client_parquet):
    """Default path: no source_query → file_type=parquet, single file."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="example_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    parquet_path = output_dir / "example_subset.parquet"
    assert parquet_path.exists()
    assert result["table_id"] == "example_subset"
    assert result["path"] == str(parquet_path)
    assert result["rows"] == 2
    assert result["bytes"] > 0
    expected_md5 = hashlib.md5(parquet_path.read_bytes()).hexdigest()
    assert result["md5"] == expected_md5

    # Default file_type should be parquet — verify by inspecting the
    # ExportFilter passed to prepare_export.
    call_args = fake_storage_client_parquet.prepare_export.call_args
    assert call_args.args[0] == "in.c-sales.orders"
    assert call_args.kwargs["export_filter"].file_type == "parquet"


def test_materialize_query_resolves_date_placeholder_in_where_filters(tmp_path, fake_storage_client_parquet):
    """Materialized where_filters must resolve {{last_6_months}} to a literal
    date before reaching the Storage API — an unresolved placeholder is
    compared verbatim and silently returns 0 rows. Mirrors the local path's
    resolve_placeholders step, which materialized rows previously skipped."""
    from datetime import datetime, timedelta, timezone

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="kbc_job",
        bucket="in.c-kbc_telemetry",
        source_table="kbc_job",
        source_query=(
            '{"where_filters": [{"column": "job_created_at", "operator": "ge", "values": ["{{last_6_months}}"]}]}'
        ),
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    wf = fake_storage_client_parquet.prepare_export.call_args.kwargs["export_filter"].where_filters
    resolved = wf[0]["values"][0]
    assert "{{" not in resolved, f"placeholder left unresolved: {resolved!r}"
    expected = (datetime.now(timezone.utc).date() - timedelta(days=180)).strftime("%Y-%m-%d")
    assert resolved == expected


def test_materialize_query_rejects_unknown_where_filter_placeholder(tmp_path, fake_storage_client_parquet):
    """An unknown placeholder must fail loudly, not silently pass a literal
    `{{typo}}` to the Storage API (which would return 0 rows)."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ValueError, match="placeholder"):
        kbe.materialize_query(
            table_id="kbc_job",
            bucket="in.c-kbc_telemetry",
            source_table="kbc_job",
            source_query=(
                '{"where_filters": [{"column": "job_created_at", "operator": "ge", "values": ["{{lasst_week}}"]}]}'
            ),
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )


def test_materialize_query_parquet_sliced_merges_via_duckdb(tmp_path):
    """Sliced parquet output: each slice is itself a complete parquet file
    (Snowflake UNLOAD MAX_FILE_SIZE behavior). The extractor must use
    ``download_file_slices`` to keep them as separate files, then
    DuckDB-COPY across ``read_parquet([slice1, slice2])`` to merge —
    naive concat would corrupt the per-slice footer."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 4,
            "file_info": {"id": 200, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def fake_download_slices(file_info, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        s1, s2 = dest_dir / "slice-00000", dest_dir / "slice-00001"
        _write_parquet(s1, n_rows=2)
        _write_parquet(s2, n_rows=2)
        return [s1, s2]

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = fake_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="big_table",
        bucket="in.c-x",
        source_table="t",
        source_query=None,
        storage_client=client,
        output_dir=output_dir,
    )

    # Final parquet contains all 4 rows from both slices.
    final = output_dir / "big_table.parquet"
    assert final.exists()
    n = (
        duckdb.connect()
        .execute(f"SELECT COUNT(*) FROM read_parquet('{str(final).replace(chr(39), chr(39) * 2)}')")
        .fetchone()[0]
    )
    assert n == 4
    assert result["rows"] == 4

    # Slices were not concatenated raw (would leave 2 footers in one file
    # and break DuckDB on read).
    client.download_file_slices.assert_called_once()


def test_materialize_query_parquet_zero_rows_emits_empty_parquet(tmp_path, caplog):
    """Storage API parquet succeeded but the filter matched 0 rows (file
    is empty/missing). We log a warning and emit an empty placeholder."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1,
            "file_id": 2,
            "rows": 0,
            "file_info": {"id": 2, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        # Don't create the file — simulates no-rows result.
        return Path(dest_path)

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with caplog.at_level("WARNING"):
        result = kbe.materialize_query(
            table_id="empty_subset",
            bucket="in.c-test",
            source_table="empty",
            source_query=None,
            storage_client=client,
            output_dir=output_dir,
        )

    assert result["rows"] == 0
    assert (output_dir / "empty_subset.parquet").exists()
    assert "no data" in caplog.text.lower() or "0 rows" in caplog.text


def test_materialize_query_admin_can_pin_file_type_csv(tmp_path, fake_storage_client_csv):
    """Admin can opt out of parquet via ``source_query='{"file_type":"csv"}'``
    — falls back to CSV → DuckDB-COPY → parquet."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="legacy_csv",
        bucket="in.c-x",
        source_table="t",
        source_query='{"file_type": "csv"}',
        storage_client=fake_storage_client_csv,
        output_dir=output_dir,
    )

    assert (output_dir / "legacy_csv.parquet").exists()
    assert result["rows"] == 2

    # Storage client called with file_type=csv on the ExportFilter.
    call = fake_storage_client_csv.export_table.call_args
    assert call.args[0] == "in.c-x.t"
    assert call.kwargs["export_filter"].file_type == "csv"


# ---- CSV dialect (RFC 4180 quote/escape pin) -------------------------------


def test_materialize_query_csv_handles_embedded_quotes_and_commas(tmp_path):
    """Regression: Keboola Storage API CSV exports are RFC-4180 (delimiter
    ',', quote '"', embedded quotes doubled). DuckDB's dialect sniffer can
    misdetect the escape char on cells holding their own quoting (real-world
    failure: `CSV Error on Line: 18985` on a column carrying embedded
    JSON/SQL text) — pinning `quote='"', escape='"'` removes the guesswork.
    Verify row count + value fidelity survive a cell with a doubled
    embedded quote plus a quoted comma."""

    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(
            Path(dest),
            "id,payload,note",
            [
                '1,"a ""quoted"" value","has,comma"',
                '2,plain,"another,one"',
            ],
        )
        return {"job_id": 1, "file_id": 2, "rows": 2, "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.export_table.side_effect = fake_export

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="quoted_csv",
        bucket="in.c-x",
        source_table="t",
        source_query='{"file_type": "csv"}',
        storage_client=client,
        output_dir=output_dir,
    )

    assert result["rows"] == 2
    final = output_dir / "quoted_csv.parquet"
    safe = str(final).replace("'", "''")
    rows = duckdb.connect().execute(f"SELECT id, payload, note FROM read_parquet('{safe}') ORDER BY id").fetchall()
    assert rows == [
        ("1", 'a "quoted" value', "has,comma"),
        ("2", "plain", "another,one"),
    ]


# ---- tempdir cleanup on failure --------------------------------------------


def test_materialize_query_sliced_parquet_tempdir_cleaned_on_exception(tmp_path):
    """When a sliced parquet download raises mid-flight (e.g. OSError 28
    'No space left'), the per-call tempdir at /tmp/kbc-export-<id>-*
    that was already populated with downloaded slices must not survive.

    Regression: an earlier worker death mid-write left a 12 GiB stale
    slice tree on the boot disk because TemporaryDirectory's default
    cleanup path itself raised under disk-full state, masking the
    original exception AND leaving the dir behind. The fix uses
    ``ignore_cleanup_errors=True`` so cleanup is best-effort but always
    fires — the dir is empty (or at least mostly) after the function
    returns."""
    captured_tmpdir: dict[str, Path] = {}

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1,
            "file_id": 2,
            "rows": 1,
            "file_info": {"id": 2, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def boom_download_slices(file_info, dest_dir):
        # Capture the tempdir the extractor created (parent of dest_dir).
        captured_tmpdir["path"] = Path(dest_dir).parent
        # Simulate a real download writing partial state, then disk full.
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        (Path(dest_dir) / "slice-00000").write_bytes(b"PAR1...partial")
        raise OSError(28, "No space left on device")

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = boom_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(OSError, match="No space left"):
        kbe.materialize_query(
            table_id="will_fail_sliced",
            bucket="in.c-test",
            source_table="t",
            source_query=None,
            storage_client=client,
            output_dir=output_dir,
        )

    # The tempdir that held the partial slice must be gone (or at least
    # not the half-populated state that leaked previously).
    assert "path" in captured_tmpdir, "download_file_slices was not invoked"
    leftover = captured_tmpdir["path"]
    assert not leftover.exists(), (
        f"tempdir {leftover} must be cleaned on exception (otherwise leaks under disk-full conditions)"
    )
    # Final parquet must NOT exist.
    assert not (output_dir / "will_fail_sliced.parquet").exists()


def test_materialize_query_warns_on_survived_scratch_when_exception_raised(tmp_path, monkeypatch):
    """Regression (agnes-reviewer-architecture finding on PR #909): the
    scratch-survived warning was originally placed as a plain statement after
    the ``with tempfile.TemporaryDirectory(...)`` block — any exception
    raised inside the block (the exact ENOSPC / mid-write failure this
    warning exists to surface) skips straight past it. Verify the warning
    check actually runs when the export raises, by making the tempdir
    genuinely survive (ignore_cleanup_errors swallowing a forced cleanup
    failure) and asserting the warning fires."""
    import connectors.keboola.storage_api as sapi

    warned: list[str] = []
    monkeypatch.setattr(sapi, "warn_if_scratch_survived", lambda path: warned.append(path))
    monkeypatch.setattr(kbe, "warn_if_scratch_survived", sapi.warn_if_scratch_survived, raising=False)

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1,
            "file_id": 2,
            "rows": 1,
            "file_info": {"id": 2, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def boom_download_slices(file_info, dest_dir):
        raise OSError(28, "No space left on device")

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = boom_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(OSError, match="No space left"):
        kbe.materialize_query(
            table_id="will_fail_sliced_2",
            bucket="in.c-test",
            source_table="t",
            source_query=None,
            storage_client=client,
            output_dir=output_dir,
        )

    assert warned, "warn_if_scratch_survived must be checked even when the export raises"


# ---- AGNES_TEMP_DIR routing -------------------------------------------------


def test_materialize_query_uses_AGNES_TEMP_DIR_when_set(
    monkeypatch,
    tmp_path,
    fake_storage_client_parquet,
):
    """The per-call tempdir lands under ``AGNES_TEMP_DIR`` when set —
    routes Snowflake-UNLOAD slice staging off the container's overlayfs
    /tmp onto the data disk. Capture the dir the storage_client receives
    via download_file's dest_path and assert it's under the configured
    root.

    Regression context: agnes-dev's boot disk filled to 100% during a
    180-day kbc_job sync because slices accumulated in /tmp; the data
    disk had 15 GiB free at the time."""
    custom_root = tmp_path / "agnes-tmp"
    custom_root.mkdir()
    monkeypatch.setenv("AGNES_TEMP_DIR", str(custom_root))

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="anywhere",
        bucket="in.c-x",
        source_table="t",
        source_query=None,
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    # The tempdir created by `materialize_query` is anonymous, but
    # `tempfile.TemporaryDirectory(dir=root, ...)` always places its
    # dir as a direct child of `root`. After materialize_query returns
    # the dir is cleaned, so check the root only contains paths that
    # WOULD have been under it (post-cleanup it's empty — that's still
    # the contract; the assertion is "AGNES_TEMP_DIR was honored as
    # the parent"). We do this indirectly by calling get_temp_root
    # ourselves under the same env and asserting the value flows.
    from connectors.keboola.storage_api import get_temp_root

    assert get_temp_root() == str(custom_root)

    # And the dir is empty post-run (cleanup happened) but still exists
    # — i.e. we didn't accidentally delete the operator's chosen root.
    assert custom_root.is_dir()


def test_materialize_query_falls_back_to_system_tmp_when_unset(
    monkeypatch,
    tmp_path,
    fake_storage_client_parquet,
):
    """No AGNES_TEMP_DIR → no behavioural change vs. pre-fix code.
    The function still returns successfully; we don't peek inside
    /tmp itself (CI-unfriendly), just assert the run completed and
    the parquet exists at output_dir as expected."""
    monkeypatch.delenv("AGNES_TEMP_DIR", raising=False)

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="default_tmp",
        bucket="in.c-x",
        source_table="t",
        source_query=None,
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    assert (output_dir / "default_tmp.parquet").exists()
    assert result["rows"] == 2


# ---- generic guards (file_type-agnostic) -----------------------------------


def test_materialize_query_rejects_unsafe_table_id(tmp_path, fake_storage_client_parquet):
    """Defense: table_id is interpolated into the parquet filename. SQL/
    path-traversal-unsafe values must be rejected up-front."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="table_id"):
        kbe.materialize_query(
            table_id="../../etc/passwd",
            bucket="in.c-test",
            source_table="t",
            source_query=None,
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )


def test_materialize_query_invalid_source_query_json_raises(tmp_path, fake_storage_client_parquet):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="not valid JSON"):
        kbe.materialize_query(
            table_id="bad_filter",
            bucket="in.c-test",
            source_table="t",
            source_query="this is not json",
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )


def test_materialize_query_passes_filter_spec_to_export(tmp_path, fake_storage_client_parquet):
    """source_query JSON is parsed into ExportFilter and forwarded to the
    Storage API client. Verifies the dispatch shape — the actual
    filter→params conversion is covered in test_keboola_storage_api.py."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="filtered",
        bucket="in.c-sales",
        source_table="orders",
        source_query=(
            '{"where_filters": [{"column": "status", "operator": "eq", "values": ["open"]}], "columns": ["id"]}'
        ),
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    f = fake_storage_client_parquet.prepare_export.call_args.kwargs["export_filter"]
    assert f.where_filters == [{"column": "status", "operator": "eq", "values": ["open"]}]
    assert f.columns == ["id"]
    # No explicit file_type → defaults to parquet.
    assert f.file_type == "parquet"


# ---- atomic write contract (#1359: routed through src.parquet_publish) -----
#
# materialize_query's temp+os.replace staging predates #1359 but shared the
# same two defects the extracted helper exists to prevent: a shared
# (non-per-process) `<id>.parquet.tmp` name (#1274) and no chmod (#203).
# Modeled on tests/test_parquet_publish.py / tests/test_jira_atomic_parquet_writes.py:
# the failure stub writes the footerless bytes a killed COPY leaves AT THE
# PATH THE STATEMENT TARGETS, then raises — a stub that raises without
# touching the filesystem first (the ORIGINAL version of this test) would
# pass even against code that isn't atomic at all.

FOOTERLESS = b"PAR1" + b"\x00" * 64


def test_keboola_materialize_atomic_write_on_failure(tmp_path):
    """If the CSV→parquet conversion fails (legacy CSV opt-in), no
    partial file is left at the final .parquet path AND no stray temp is
    left behind."""

    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(Path(dest), "id,name", ["1,alpha"])
        return {"job_id": 1, "file_id": 2, "rows": 1, "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.export_table.side_effect = fake_export

    output_dir = tmp_path / "data"
    output_dir.mkdir()

    real_connect = duckdb.connect

    class FailingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "FORMAT PARQUET" in sql:
                m = re.search(r"TO '([^']+)'", sql)
                assert m, f"could not find COPY target in: {sql}"
                Path(m.group(1)).write_bytes(FOOTERLESS)
                raise RuntimeError("simulated mid-COPY failure")
            return self._inner.execute(sql, *a, **kw)

        def close(self):
            self._inner.close()

    def patched_connect(*args, **kwargs):
        return FailingConn(real_connect(*args, **kwargs))

    with patch("connectors.keboola.extractor.duckdb.connect", side_effect=patched_connect):
        with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
            kbe.materialize_query(
                table_id="atomic_test",
                bucket="in.c-test",
                source_table="t",
                source_query='{"file_type": "csv"}',
                storage_client=client,
                output_dir=output_dir,
            )

    final_path = output_dir / "atomic_test.parquet"
    assert not final_path.exists(), (
        f"Partial parquet left at final path {final_path} — orchestrator "
        f"rebuild would pick this up and serve corrupt data."
    )
    assert list(output_dir.glob("*.tmp")) == [], "stale temp left behind"


def test_keboola_materialize_uses_tmp_path_during_copy(tmp_path, fake_storage_client_parquet):
    """Atomic-write contract: parquet first lands at a per-process temp, then
    is os.replaced into <id>.parquet on success. Verified by patching
    os.replace to capture the (src, dst) pair."""
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    captured = {}
    real_replace = os.replace

    def trace_replace(src, dst):
        captured["src"] = str(src)
        captured["dst"] = str(dst)
        real_replace(src, dst)

    with patch.object(kbe.os, "replace", side_effect=trace_replace):
        result = kbe.materialize_query(
            table_id="tmp_path_test",
            bucket="in.c-test",
            source_table="t",
            source_query=None,
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )

    assert captured["src"].endswith(".tmp"), captured
    assert not captured["src"].endswith(".parquet.tmp"), (
        "temp name must be per-process, not the shared <id>.parquet.tmp"
    )
    assert str(os.getpid()) in Path(captured["src"]).name, "temp name must be per-process (#1274)"
    assert captured["dst"].endswith(".parquet") and not captured["dst"].endswith(".tmp")

    assert (output_dir / "tmp_path_test.parquet").exists()
    assert [p.name for p in output_dir.glob("*.parquet")] == ["tmp_path_test.parquet"]
    assert not list(output_dir.glob("*.tmp"))
    assert result["path"].endswith(".parquet")
    assert not result["path"].endswith(".tmp")


def test_keboola_materialize_published_mode_is_0644_under_restrictive_umask(tmp_path, fake_storage_client_parquet):
    """`pq.write_table`/DuckDB COPY create the temp as `0666 & umask`; a 0077
    umask (seen in container/systemd units) would publish 0600 without the
    explicit chmod #203 documents."""
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    previous = os.umask(0o077)
    try:
        kbe.materialize_query(
            table_id="umask_test",
            bucket="in.c-test",
            source_table="t",
            source_query=None,
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )
    finally:
        os.umask(previous)

    pq_path = output_dir / "umask_test.parquet"
    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


# ---- consolidation-connection resource caps (#431 / #432) ------------------


def test_consolidation_conn_applies_memory_and_thread_caps():
    """``_open_consolidation_conn`` must apply the three resource caps that
    keep the materialize CSV->parquet COPY inside a small cgroup container:
    ``memory_limit`` capped (normalizes to ~1.8 GiB for the '2GB' source
    constant), ``threads=2``, and ``preserve_insertion_order=false``.

    Asserted via observable DuckDB ``current_setting`` state, not by reading
    source strings — a regression that drops or changes any SET fails here.
    """
    # Pin the source-of-truth constants so a silent value change is caught.
    assert kbe._CONSOLIDATION_MEMORY_LIMIT == "2GB"
    assert kbe._CONSOLIDATION_THREADS == 2

    conn = kbe._open_consolidation_conn()
    try:
        threads = conn.execute("SELECT current_setting('threads')").fetchone()[0]
        assert int(threads) == 2

        preserve = conn.execute("SELECT current_setting('preserve_insertion_order')").fetchone()[0]
        # DuckDB returns this as a bool (or a 'false' string on older builds).
        assert preserve in (False, "false")

        # DuckDB normalizes '2GB' to '1.8 GiB' (2e9 bytes). Assert the
        # banded/normalized form — an exact '2GB' string compare would
        # false-fail. The cap must be well below the DuckDB default
        # (80% of host RAM), so a numeric prefix <= 2.0 GiB proves it.
        mem = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        assert "GiB" in mem, mem
        assert float(mem.split()[0]) <= 2.0, mem
    finally:
        conn.close()


# ---- parquet-writer row-group bound -------------------------------------
#
# DuckDB buffers a whole row group before flushing it. The default (122,880
# rows) assumes narrow rows; a document-shaped table (one column holding whole
# conversation transcripts) needs gigabytes for a single group and raises
# OutOfMemoryException mid-COPY no matter how well the scan side streams —
# which is what left six materialized tables permanently unsynced on a live
# instance. Every COPY-to-parquet in this module must therefore carry an
# explicit bound.
#
# These assert the observable *output shape* (row-group count of the written
# parquet) rather than the SQL text, so a regression that keeps the option but
# stops it taking effect still fails. The module's byte target is monkeypatched
# down so the fixtures stay small and the suite stays fast.


def _write_wide_parquet(dest: Path, *, n_rows: int = 400, cell_bytes: int = 4096) -> None:
    """Parquet shaped like a conversation dump: an id plus one very wide,
    high-entropy text column (unique per row so neither snappy nor DuckDB's
    string dictionary can collapse it, which is what makes the writer buffer
    genuinely large)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe = str(dest).replace("'", "''")
    conn = duckdb.connect()
    try:
        conn.execute(
            f"COPY (SELECT i::BIGINT AS id, "
            f"repeat(md5(i::VARCHAR), {max(1, cell_bytes // 32)}) AS body "
            f"FROM range({n_rows}) t(i)) TO '{safe}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
        )
    finally:
        conn.close()


def _row_groups(path: Path) -> int:
    import pyarrow.parquet as pq

    return pq.read_metadata(str(path)).num_row_groups


@pytest.fixture
def small_row_group_target(monkeypatch):
    """Shrink the module's row-group byte target so a MiB-scale fixture
    exercises the same code path a GiB-scale production table does.

    Deliberately leaves ``_ROW_GROUP_MIN_ROWS`` alone: DuckDB flushes on
    2048-row chunk boundaries, so patching the floor below that would make
    these tests assert behaviour the writer cannot actually deliver.
    """
    monkeypatch.setattr(kbe, "_ROW_GROUP_TARGET_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(kbe, "_ROW_GROUP_TARGET_BYTES_SQL", "8MB")


def test_row_group_rows_for_derives_count_from_footer(tmp_path):
    """The derived row count must track the source's real row width, since
    the order-preserving retype COPY cannot use the byte-denominated knob."""
    src = tmp_path / "wide.parquet"
    _write_wide_parquet(src, n_rows=2000, cell_bytes=4096)

    rows = kbe._row_group_rows_for(src)

    assert kbe._ROW_GROUP_MIN_ROWS <= rows <= kbe._ROW_GROUP_MAX_ROWS

    # One group of `rows` rows should land near the byte target. Compare
    # against the source's *measured* average row width (parquet overhead and
    # the id column put it above the nominal cell size), allowing a 10% band.
    import pyarrow.parquet as pq

    md = pq.read_metadata(str(src))
    uncompressed = sum(
        md.row_group(g).column(c).total_uncompressed_size
        for g in range(md.num_row_groups)
        for c in range(md.row_group(g).num_columns)
    )
    avg_row = uncompressed / md.num_rows
    assert 0.9 <= (rows * avg_row) / kbe._ROW_GROUP_TARGET_BYTES <= 1.1, (rows, avg_row)


def test_row_group_rows_for_narrow_table_keeps_duckdb_default(tmp_path):
    """A narrow table must not be penalised — the bound exists for wide rows
    and has to stay neutral everywhere else."""
    src = tmp_path / "narrow.parquet"
    _write_parquet(src, n_rows=50)

    assert kbe._row_group_rows_for(src) == kbe._ROW_GROUP_MAX_ROWS


def test_row_group_rows_for_degrades_to_default_on_unreadable_source(tmp_path):
    """A memory guardrail must never turn into a hard failure of the sync."""
    bogus = tmp_path / "not-a-parquet.parquet"
    bogus.write_bytes(b"definitely not parquet")

    assert kbe._row_group_rows_for(bogus) == kbe._ROW_GROUP_MAX_ROWS


def test_sliced_consolidation_bounds_writer_row_groups(tmp_path, small_row_group_target):
    """The sliced-parquet consolidation COPY must chunk its output. Unbounded,
    this is the exact statement that OOM'd in production."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1,
            "file_id": 2,
            "rows": 6000,
            "file_info": {"id": 2, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def fake_download_slices(file_info, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(2):
            p = dest_dir / f"slice-{i:05d}"
            _write_wide_parquet(p, n_rows=3000, cell_bytes=4096)
            paths.append(p)
        return paths

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = fake_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="wide_table",
        bucket="in.c-x",
        source_table="t",
        source_query=None,
        storage_client=client,
        output_dir=output_dir,
    )

    final = output_dir / "wide_table.parquet"
    assert result["rows"] == 6000
    # ~24 MiB of body text against an 8 MiB target. 6000 rows is far under
    # DuckDB's 122,880-row default, so an unbounded writer emits exactly one
    # group and this assertion is what fails.
    assert _row_groups(final) > 1, "consolidation wrote one unbounded row group"


def test_retype_bounds_writer_row_groups_and_keeps_md5_stable(tmp_path, small_row_group_target):
    """The retype COPY re-enables insertion order for MD5 stability, so it
    needs the row-count bound instead of the byte one — and must not lose the
    stability it re-enabled insertion order to get."""
    import pyarrow as pa

    def build() -> bytes:
        src = tmp_path / "retype-me.parquet"
        if src.exists():
            src.unlink()
        _write_wide_parquet(src, n_rows=6000, cell_bytes=4096)
        # Force a cast so the retype does not short-circuit on "no casts".
        target = pa.schema([pa.field("id", pa.int32()), pa.field("body", pa.string())])
        kbe._retype_parquet_streaming(src, target)
        assert _row_groups(src) > 1, "retype wrote one unbounded row group"
        return hashlib.md5(src.read_bytes()).hexdigest()

    assert build() == build(), "retype output is no longer byte-stable for identical input"


def test_retype_killed_write_leaves_the_pre_retype_parquet_intact(tmp_path, monkeypatch):
    """#1359 bonus site (found via the widened sweep, not in the issue's
    named list): `_retype_parquet_streaming` now routes through
    `atomic_publish` too, with `dest=tmp_parquet` — a temp-to-temp publish,
    not yet the served path (`materialize_query` commits that separately).

    This specific property (a killed retype must not corrupt the pre-retype
    `tmp_parquet`) held even before the move — the original code already
    wrote to its own `.typed` sibling and only replaced `tmp_parquet` on
    success, with `except BaseException: cleanup; raise` on failure, so this
    is a characterization test confirming the refactor preserved that,
    not a regression test for a hole that existed here independently. The
    move's actual value at this site is consistency (one publish mechanism,
    not two) and closing the sweep's allowlist gap — `.typed`'s naming
    inherits per-process-ness transitively from `tmp_parquet`'s own name now,
    where pre-fix both were built from the OUTER function's shared
    `<id>.parquet.tmp`, which the Group B fix on `materialize_query`
    addresses.
    """
    import pyarrow as pa

    src = tmp_path / "retype-me.parquet"
    _write_wide_parquet(src, n_rows=10, cell_bytes=64)
    original = src.read_bytes()
    target = pa.schema([pa.field("id", pa.int32()), pa.field("body", pa.string())])

    real_open = kbe._open_consolidation_conn

    def _boom_on_copy(real_conn, sql, *a, **kw):
        upper = sql.upper()
        if "COPY" in upper and "FORMAT PARQUET" in upper:
            m = re.search(r"TO '([^']+)'", sql)
            assert m, f"could not find COPY target in: {sql}"
            Path(m.group(1)).write_bytes(b"PAR1" + b"\x00" * 64)
            raise RuntimeError("simulated mid-retype-COPY failure")
        return real_conn.execute(sql, *a, **kw)

    class _ExecuteProxy:
        def __init__(self, conn, on_execute):
            self._conn = conn
            self._on_execute = on_execute

        def execute(self, sql, *a, **kw):
            return self._on_execute(self._conn, sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def boom_open(*a, **kw):
        return _ExecuteProxy(real_open(*a, **kw), _boom_on_copy)

    monkeypatch.setattr(kbe, "_open_consolidation_conn", boom_open)

    with pytest.raises(RuntimeError, match="simulated mid-retype-COPY failure"):
        kbe._retype_parquet_streaming(src, target)

    assert src.read_bytes() == original, "killed retype corrupted the pre-retype parquet"
    assert list(tmp_path.glob("*.tmp")) == [], "stale retype temp left behind"


def test_consolidation_conn_spills_under_agnes_temp_dir(tmp_path, monkeypatch):
    """Spill must land on the volume the Keboola scratch root already points
    at, not DuckDB's default `.tmp` relative to cwd — which in the shipped
    image is /app, the container overlay on the boot disk."""
    monkeypatch.setenv("AGNES_TEMP_DIR", str(tmp_path / "scratch"))

    conn = kbe._open_consolidation_conn()
    try:
        temp_dir = conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        assert str(tmp_path / "scratch") in temp_dir, temp_dir

        max_temp = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
        # DuckDB's own default is "90% of available disk space" — anything
        # naming a concrete size proves the cap was applied.
        assert "%" not in max_temp, max_temp
    finally:
        conn.close()


def test_consolidation_spill_dir_is_private_per_connection(tmp_path, monkeypatch):
    """Each consolidation connection gets its OWN spill directory.

    DuckDB names its spill files by size class + index only —
    ``duckdb_temp_storage_DEFAULT-0.tmp``, no pid, no random component — and
    on close it deletes *every* ``duckdb_temp_storage_*`` in its
    ``temp_directory``, including files it did not create. Two DuckDB
    instances sharing one directory therefore write over each other's blocks
    (verified on duckdb 1.5.5: two processes spilling into one shared
    directory ended with one of them killed by SIGSEGV). Consolidation
    connections are opened concurrently — across api/worker/sync-subprocess
    roles on the same data volume, and within one process — so the directory
    must never be shared."""
    monkeypatch.setenv("AGNES_TEMP_DIR", str(tmp_path / "scratch"))

    a = kbe._open_consolidation_conn()
    b = kbe._open_consolidation_conn()
    try:
        da = Path(a.execute("SELECT current_setting('temp_directory')").fetchone()[0])
        db = Path(b.execute("SELECT current_setting('temp_directory')").fetchone()[0])
        assert da != db, f"consolidation connections share a spill directory: {da}"
        assert da.parent == tmp_path / "scratch", da
        assert db.parent == tmp_path / "scratch", db
    finally:
        a.close()
        b.close()


def test_orphaned_consolidation_spill_is_reclaimed_by_sweep(tmp_path, monkeypatch):
    """A consolidation hard-killed mid-spill leaves its spill directory
    behind (DuckDB removes it only on a clean close) — the existing
    orphaned-scratch sweep must reclaim it, or a capped-at-10 GB spill sits
    on the data disk until an operator removes it by hand.

    End-to-end on real DuckDB spill output: force an actual spill, then age
    the directory past the threshold and sweep."""
    from connectors.keboola.storage_api import sweep_orphaned_scratch

    root = tmp_path / "scratch"
    monkeypatch.setenv("AGNES_TEMP_DIR", str(root))
    # Small cap so a tiny fixture really spills to disk.
    monkeypatch.setattr(kbe, "_CONSOLIDATION_MEMORY_LIMIT", "100MB")

    conn = kbe._open_consolidation_conn()
    spill = Path(conn.execute("SELECT current_setting('temp_directory')").fetchone()[0])
    try:
        conn.execute("CREATE OR REPLACE TABLE t AS SELECT i, repeat('y', 300) s FROM range(0, 150000) tbl(i)")
        conn.execute("SELECT count(*) FROM (SELECT s, i, count(*) FROM t GROUP BY s, i)").fetchone()
        spilled = sorted(p.name for p in spill.iterdir())
        assert spilled and all(n.startswith("duckdb_temp_storage_") for n in spilled), spilled

        # Simulate the hard kill: the connection never closes, so the dir and
        # its spill files survive. Age it past the sweep threshold.
        old = time.time() - 7200
        os.utime(spill, (old, old))
        assert sweep_orphaned_scratch(root=str(root), max_age_seconds=3600) == 1
        assert not spill.exists()
    finally:
        conn.close()


# ---- typed-parquet fix for the native-parquet path (2026-07-15) -----------
#
# Verified live: Storage API's Snowflake UNLOAD serves every column as
# VARCHAR regardless of the source's real type. These tests use a real
# KeboolaClient (kbcstorage-backed) with __init__ + get_pyarrow_schema
# monkeypatched. The kbcstorage guard is applied PER-TEST (below), not at
# module level, so the unrelated tests earlier in this file still run on
# installs without the [server] extra.

_needs_kbcstorage = pytest.mark.skipif(
    importlib.util.find_spec("kbcstorage") is None,
    reason="requires the [server] extra (kbcstorage)",
)


def _write_string_typed_parquet(dest: Path) -> None:
    """Write a parquet whose columns are ALL string-typed, even the
    numeric-looking one — same shape Storage API's native parquet export
    produces for the bug this fix addresses."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {"id": pa.array(["1", "2"], type=pa.string()), "amount": pa.array(["100", "250"], type=pa.string())}
    )
    pq.write_table(table, dest)


@pytest.fixture
def fake_storage_client_parquet_untyped():
    """Like fake_storage_client_parquet, but with real string .token/.base
    (so the isinstance guard in materialize_query's typing fix actually
    engages) and an all-string-typed parquet output."""

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 2,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        _write_string_typed_parquet(Path(dest_path))
        return Path(dest_path)

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download
    client.token = "fake-token"
    client.base = "https://connection.keboola.com/v2/storage"
    return client


@_needs_kbcstorage
def test_materialize_query_applies_typed_schema_when_available(
    tmp_path, monkeypatch, fake_storage_client_parquet_untyped
):
    """When KeboolaClient.get_pyarrow_schema returns a schema, the
    materialized parquet's columns must be cast to those types — not left
    as the all-VARCHAR shape Storage API's native parquet export produces."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    fake_schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("amount", pa.int64()),
        ]
    )
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="typed_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet_untyped,
        output_dir=output_dir,
    )

    table = pq.read_table(output_dir / "typed_subset.parquet")
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.int64()
    # Correctness, not just typing: a real aggregate over the now-numeric
    # column must be possible and return the right sum (100 + 250).
    assert table.column("amount").to_pylist() == [100, 250]
    assert result["rows"] == 2


@_needs_kbcstorage
def test_materialize_query_keeps_varchar_when_schema_unavailable(
    tmp_path, monkeypatch, fake_storage_client_parquet_untyped
):
    """Metadata API unreachable → graceful fallback to Storage API's native
    (all-VARCHAR) types, matching the legacy CSV path's existing fallback
    behavior. Must not raise."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    def raise_unreachable(self, tid):
        raise RuntimeError("metadata API unreachable")

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", raise_unreachable)

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="untyped_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet_untyped,
        output_dir=output_dir,
    )

    table = pq.read_table(output_dir / "untyped_subset.parquet")
    assert table.schema.field("amount").type == pa.string()


@_needs_kbcstorage
def test_materialize_query_untyped_storage_client_skips_typing_safely(tmp_path, fake_storage_client_parquet):
    """The pre-existing MagicMock-based fixture (no real .token/.base) must
    keep working exactly as before this fix — the isinstance guard makes
    typing a safe no-op rather than attempting a KeboolaClient call with a
    mock as credentials."""
    import pyarrow.parquet as pq

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="example_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    # fake_storage_client_parquet's _write_parquet writes an int column via
    # DuckDB VALUES — already typed, unaffected either way. The real
    # assertion here is that the call above didn't raise.
    table = pq.read_table(output_dir / "example_subset.parquet")
    assert table.num_rows == 2


# ---- streaming retype (OOM fix for the typed-parquet path) ----------------
#
# The original typed-parquet fix loaded the whole materialized parquet into
# one pyarrow.Table (pq.read_table) before casting — peak memory scaled with
# the materialized result size and OOM-killed syncs of large tables. The
# retype must stream through DuckDB (bounded by the consolidation memory
# cap) while keeping the coerce-to-NULL semantics of the pandas fallback.


def _make_untyped_client(columns):
    """Fake KeboolaStorageClient whose native-parquet export writes the
    given ``{name: [str values]}`` columns ALL string-typed — the shape
    Storage API's Snowflake UNLOAD produces."""
    n_rows = len(next(iter(columns.values())))

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": n_rows,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({k: pa.array(v, type=pa.string()) for k, v in columns.items()}), dest)
        return dest

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download
    client.token = "fake-token"
    client.base = "https://connection.keboola.com/v2/storage"
    return client


@_needs_kbcstorage
def test_materialize_typed_retype_streams_instead_of_full_read(
    tmp_path, monkeypatch, fake_storage_client_parquet_untyped
):
    """The retype must NOT load the whole parquet via pq.read_table —
    with a poisoned read_table the columns must still come out typed
    (streamed through DuckDB, not read into one pyarrow.Table)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    fake_schema = pa.schema([pa.field("id", pa.int64()), pa.field("amount", pa.int64())])
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)

    def boom(*a, **kw):
        raise AssertionError("retype must not pq.read_table the full parquet")

    monkeypatch.setattr("pyarrow.parquet.read_table", boom)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    kbe.materialize_query(
        table_id="streamed_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet_untyped,
        output_dir=output_dir,
    )

    monkeypatch.undo()
    table = pq.read_table(output_dir / "streamed_subset.parquet")
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.int64()
    assert table.column("amount").to_pylist() == [100, 250]


@_needs_kbcstorage
def test_materialize_typed_retype_coerces_lossy_values_to_null(tmp_path, monkeypatch):
    """Uncastable values coerce to NULL (the pandas-fallback semantics the
    in-memory retype had); castable columns and values are unaffected."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    client = _make_untyped_client({"id": ["1", "2"], "amount": ["100", "abc"]})
    fake_schema = pa.schema([pa.field("id", pa.int64()), pa.field("amount", pa.int64())])
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    kbe.materialize_query(
        table_id="lossy_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=client,
        output_dir=output_dir,
    )

    table = pq.read_table(output_dir / "lossy_subset.parquet")
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.int64()
    assert table.column("id").to_pylist() == [1, 2]
    assert table.column("amount").to_pylist() == [100, None]


@_needs_kbcstorage
def test_materialize_typed_retype_handles_dates_timestamps_and_empties(tmp_path, monkeypatch):
    """DATE and TIMESTAMP targets cast from their string forms; empty
    strings coerce to NULL instead of poisoning the whole column."""
    import datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    from connectors.keboola.client import KeboolaClient

    client = _make_untyped_client(
        {
            "ts": ["2026-01-02 03:04:05", ""],
            "d": ["2026-01-02", ""],
        }
    )
    fake_schema = pa.schema([pa.field("ts", pa.timestamp("us")), pa.field("d", pa.date32())])
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    kbe.materialize_query(
        table_id="temporal_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=client,
        output_dir=output_dir,
    )

    table = pq.read_table(output_dir / "temporal_subset.parquet")
    assert table.schema.field("ts").type == pa.timestamp("us")
    assert table.schema.field("d").type == pa.date32()
    assert table.column("ts").to_pylist() == [
        datetime.datetime(2026, 1, 2, 3, 4, 5),
        None,
    ]
    assert table.column("d").to_pylist() == [datetime.date(2026, 1, 2), None]


def test_retype_preserves_row_order_across_row_groups(tmp_path):
    """The materialized parquet's MD5 is the change-detection key for
    `agnes pull` — the retype must preserve input row order (byte-stable
    output for identical input), even across many row groups where
    `preserve_insertion_order=false` could legally reorder."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = tmp_path / "ordered.parquet"
    n = 5000
    pq.write_table(
        pa.table({"id": pa.array([str(i) for i in range(n)], type=pa.string())}),
        src,
        row_group_size=500,
    )

    kbe._retype_parquet_streaming(src, pa.schema([pa.field("id", pa.int64())]))

    table = pq.read_table(src)
    assert table.schema.field("id").type == pa.int64()
    assert table.column("id").to_pylist() == list(range(n))


class TestEveryBucketSourceTableCompositionIsNormalized:
    """Ratchet: the fourth round of one finding, closed structurally.

    `f"{bucket}.{source_table}"` reads like it composes a Keboola tableId, and
    for a row registered by the pre-fix Data-sources wizard it does not — that
    row stores the FULL id in `source_table`, so the composition doubles the
    bucket. Review found this in four separate passes, each time naming another
    call site: the export/view paths, then `semantic_layer` +
    `connectors/keboola/metadata.py`, then `usage.py` + `data_semantics_scaffold`.
    Patching the site that was pointed at is what made a fourth round possible.

    This test finds every such composition instead, so a new one has to either
    route through `normalize_source_table` or be listed below with the reason it
    is a different thing.
    """

    # (path prefix, why this composition needs no Keboola normalization)
    _EXEMPT = {
        "app/api/admin.py": "BigQuery `project.dataset.table` — bucket is a BQ dataset, not a Keboola bucket",
        "app/api/query.py": "BigQuery fqn composition (bq_fqn), same reason",
        "app/api/v2_scan.py": "BigQuery fqn composition (parse_bq_fqn), same reason",
        "connectors/keboola/storage_api.py": "the helper's own docstring, quoting the shape it fixes",
    }

    # Two shapes, because the first version of this ratchet only knew the
    # f-string one and missed `quote_ident(bucket)}.{quote_ident(source_table)`
    # in the /api/query copy-paste hint — a suggestion naming a table that does
    # not exist (Devin Review on #1189).
    _COMPOSITION_RE = (
        r'\{[a-z_.\[\]"\x27]*bucket[a-z_.\[\]"\x27]*\}\.\{[a-z_.\[\]"\x27]*(source_table|table)[a-z_.\[\]"\x27]*\}'
        r"|bucket[^\n]{0,40}\}\.\{[^\n]{0,40}source_table"
    )

    def _hits(self):
        import subprocess

        proc = subprocess.run(
            ["git", "grep", "-nP", self._COMPOSITION_RE, "--", "*.py"],
            capture_output=True,
            text=True,
        )
        return [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("tests/")]

    def test_the_scan_finds_something(self):
        """Guards the guard: a refactor that changes the composition idiom would
        otherwise empty the scan and make the ratchet below pass vacuously."""
        assert len(self._hits()) >= 6

    def test_every_composition_normalizes_or_is_exempt(self):
        """A composition is safe when `normalize_source_table` is called anywhere
        in the ENCLOSING FUNCTION — either right above it, or earlier as the
        reassignment idiom `materialize_query` uses (it normalizes at the top and
        rebinds `source_table`, then composes 89 lines later).

        Scoped by function via AST rather than by a line window, which is the
        point: the first version of this test used a 30-line window and flagged
        that legitimate case. A window is an arbitrary number; the function is the
        actual scope the value flows through.
        """
        import ast
        import pathlib

        def enclosing_span(tree, lineno):
            best = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end = getattr(node, "end_lineno", None)
                    if end and node.lineno <= lineno <= end:
                        if best is None or node.lineno > best[0]:
                            best = (node.lineno, end)
            return best

        unaccounted = []
        for hit in self._hits():
            path, lineno, _ = hit.split(":", 2)
            if any(path.startswith(p) for p in self._EXEMPT):
                continue
            src = pathlib.Path(path).read_text(encoding="utf-8")
            lines = src.split("\n")
            span = enclosing_span(ast.parse(src), int(lineno))
            body = "\n".join(lines[span[0] - 1 : span[1]]) if span else ""
            if "normalize_source_table" not in body:
                unaccounted.append(hit)

        assert not unaccounted, (
            "bucket + source_table composed without normalize_source_table nearby — a "
            "legacy wizard row doubles the bucket prefix here. Route it through the helper, "
            "or add its path to _EXEMPT with the reason it is not a Keboola tableId:\n" + "\n".join(unaccounted)
        )

    def test_no_stale_exemption(self):
        """Shrinks-only: an exempt path that stopped composing must be dropped."""
        hits = self._hits()
        stale = [p for p in self._EXEMPT if not any(h.startswith(p) for h in hits)]
        assert not stale, f"no longer composes — drop from _EXEMPT: {stale}"


# ---- extract.duckdb registration (_meta + inner view) ----------------------


def test_materialize_query_registers_meta_and_inner_view(tmp_path, fake_storage_client_parquet):
    """A materialized Keboola row must land in its source's ``extract.duckdb``
    as a ``_meta`` row + inner view, or ``SyncOrchestrator.rebuild()`` never
    creates the master view: the parquet sits on disk, ``sync_state`` reports
    ``ok`` with a row count, and every read 400s with "registered as
    query_mode='materialized' but is not yet materialized in this instance's
    analytics views".

    BigQuery (``_persist_materialized_inner_view``), Snowflake and Databricks
    all do this; Keboola was the only connector whose materialize path skipped
    it, so a Keboola-materialized-only instance never got an
    ``extract.duckdb`` at all and the orchestrator silently skipped the whole
    source (debug-level "no extract.duckdb").
    """
    output_dir = tmp_path / "extracts" / "keboola" / "data"
    output_dir.mkdir(parents=True)

    kbe.materialize_query(
        table_id="orders",
        bucket="in.c-sales",
        source_table="orders",
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    extract_db = output_dir.parent / "extract.duckdb"
    assert extract_db.is_file(), "materialize must create extract.duckdb for a materialized-only source"

    conn = duckdb.connect(str(extract_db), read_only=True)
    try:
        assert conn.execute("SELECT table_name, rows, query_mode FROM _meta").fetchall() == [
            ("orders", 2, "materialized")
        ]
        # The inner view must resolve through to the published parquet — the
        # orchestrator only creates a master view when an inner object of the
        # same name exists.
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    finally:
        conn.close()


def test_materialize_query_meta_registration_replaces_own_row_only(tmp_path, fake_storage_client_parquet):
    """Re-materializing replaces that table's ``_meta`` row (the table carries
    no UNIQUE on ``table_name``, so a blind INSERT would duplicate it) and
    leaves every other row — e.g. a ``query_mode='remote'`` row written by the
    extractor pass — untouched."""
    output_dir = tmp_path / "extracts" / "keboola" / "data"
    output_dir.mkdir(parents=True)

    for _ in range(2):
        kbe.materialize_query(
            table_id="orders",
            bucket="in.c-sales",
            source_table="orders",
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )

    extract_db = output_dir.parent / "extract.duckdb"
    conn = duckdb.connect(str(extract_db), read_only=False)
    try:
        conn.execute(
            "INSERT INTO _meta VALUES ('sibling', '', 0, 0, current_timestamp, 'remote')",
        )
    finally:
        conn.close()

    kbe.materialize_query(
        table_id="orders",
        bucket="in.c-sales",
        source_table="orders",
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    conn = duckdb.connect(str(extract_db), read_only=True)
    try:
        rows = conn.execute("SELECT table_name, query_mode FROM _meta ORDER BY table_name").fetchall()
    finally:
        conn.close()
    assert rows == [("orders", "materialized"), ("sibling", "remote")]
