"""#2265: the DuckDB-extension full_refresh path (`_extract_via_extension`)
never consulted the table's real schema — it published whatever types the
Keboola extension's QueryService returned, verbatim.

For a natively-typed table that IS the real schema. For a linked/alias
table (``isTyped: false``, no own ``columnMetadata`` — its types live only
on the source table, resolved by ``KeboolaClient.get_pyarrow_schema()``'s
``sourceTable.columnMetadata`` cascade) QueryService serves every column as
VARCHAR, and the extension path shipped that straight to the served
parquet. The CSV/legacy path (``_extract_via_legacy``) and the materialize
path (``materialize_query`` -> ``_retype_best_effort``) already retyped from
that resolved schema; this file locks in that the extension path now does
too, reusing the same ``_retype_best_effort`` helper rather than a third
copy of its degrade logic.
"""

import logging

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# `_retype_best_effort` reaches for `connectors.keboola.client.KeboolaClient`,
# which import-binds the optional `kbcstorage` dependency at module load.
# Skip cleanly (not error) on an install that doesn't ship the connector
# extras, matching `tests/test_keboola_extractor_typed.py`.
pytest.importorskip("kbcstorage")


def _kbc_conn_with_table(bucket: str, table: str, rows_sql: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("ATTACH ':memory:' AS kbc")
    conn.execute(f'CREATE SCHEMA kbc."{bucket}"')
    conn.execute(f'CREATE TABLE kbc."{bucket}"."{table}" AS {rows_sql}')
    return conn


def test_extension_path_retypes_alias_table_from_source_schema(tmp_path, monkeypatch):
    """The regression case: an alias table whose extension COPY comes back
    all-VARCHAR gets retyped from the resolved schema, and an uncastable
    value (empty string in a numeric column) becomes NULL rather than
    failing the sync."""
    from connectors.keboola.client import KeboolaClient
    from connectors.keboola.extractor import _extract_via_extension

    conn = _kbc_conn_with_table(
        "in.c-crm",
        "orders",
        """SELECT * FROM (VALUES
               ('1', '19.99', '2026-01-01 00:00:00', 'alice'),
               ('2', '', '2026-01-02 00:00:00', 'bob')
           ) AS t(id, amount, created_at, name)""",
    )

    typed_schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("amount", pa.float64()),
            pa.field("created_at", pa.timestamp("us")),
            pa.field("name", pa.string()),
        ]
    )
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: typed_schema)

    pq_path = tmp_path / "orders.parquet"
    tc = {"name": "orders", "bucket": "in.c-crm", "source_table": "orders"}
    _extract_via_extension(conn, tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.float64()
    assert table.schema.field("created_at").type == pa.timestamp("us")

    rows = {r["id"]: r for r in table.to_pylist()}
    assert rows[1]["amount"] == pytest.approx(19.99)
    assert rows[2]["amount"] is None  # '' not castable to DOUBLE -> NULL, not a failure


def test_extension_path_schema_fetch_failure_keeps_native_parquet(tmp_path, monkeypatch, caplog):
    """Metadata API unreachable -> keep the extension's native types, log a
    warning, never raise."""
    from connectors.keboola.client import KeboolaClient
    from connectors.keboola.extractor import _extract_via_extension

    conn = _kbc_conn_with_table("in.c-crm", "orders", "SELECT 1 AS id, 'a' AS name")

    def boom_schema(self, table_id):
        raise RuntimeError("Storage API down")

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", boom_schema)

    pq_path = tmp_path / "orders.parquet"
    tc = {"name": "orders", "bucket": "in.c-crm", "source_table": "orders"}

    with caplog.at_level(logging.WARNING):
        _extract_via_extension(conn, tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.num_rows == 1
    assert table.schema.field("id").type == pa.int32()  # native extension type (DuckDB INTEGER), untouched


def test_extension_path_retype_failure_keeps_native_parquet(tmp_path, monkeypatch, caplog):
    """Schema resolves fine but the retype rewrite itself blows up (disk
    full, bad cast, ...) -> keep the native (untyped) parquet, log a
    warning, never raise."""
    from connectors.keboola import extractor
    from connectors.keboola.client import KeboolaClient

    conn = _kbc_conn_with_table("in.c-crm", "orders", "SELECT '1' AS id, 'a' AS name")

    typed_schema = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: typed_schema)

    def boom_retype(tmp_parquet, target_schema):
        raise RuntimeError("disk full")

    monkeypatch.setattr(extractor, "_retype_parquet_streaming", boom_retype)

    pq_path = tmp_path / "orders.parquet"
    tc = {"name": "orders", "bucket": "in.c-crm", "source_table": "orders"}

    with caplog.at_level(logging.WARNING):
        extractor._extract_via_extension(conn, tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.num_rows == 1
    # The retype never landed — native (untyped) VARCHAR column survives.
    assert table.schema.field("id").type == pa.string()


def test_extension_path_no_op_when_types_already_match(tmp_path, monkeypatch):
    """A natively-typed table (already matches the resolved schema) is
    published without a needless rewrite — `_retype_parquet_streaming`
    returns before ever opening a second `atomic_publish` on the temp
    path."""
    from connectors.keboola import extractor
    from connectors.keboola.client import KeboolaClient

    conn = _kbc_conn_with_table(
        "in.c-crm",
        "orders",
        "SELECT 1::BIGINT AS id, 9.5::DOUBLE AS amount",
    )

    matching_schema = pa.schema([pa.field("id", pa.int64()), pa.field("amount", pa.float64())])
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: matching_schema)

    publish_calls = []
    real_atomic_publish = extractor.atomic_publish

    def spy_atomic_publish(dest):
        publish_calls.append(dest)
        return real_atomic_publish(dest)

    monkeypatch.setattr(extractor, "atomic_publish", spy_atomic_publish)

    pq_path = tmp_path / "orders.parquet"
    tc = {"name": "orders", "bucket": "in.c-crm", "source_table": "orders"}
    extractor._extract_via_extension(conn, tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.float64()
    # Exactly one publish — the COPY's own. A second (the retype rewrite's
    # own atomic_publish on the same temp path) would mean the "already
    # matches" case rewrote the file anyway.
    assert len(publish_calls) == 1


def test_extension_path_skips_retype_without_credentials(tmp_path, monkeypatch):
    """No keboola_url/keboola_token -> the retype step is skipped outright
    (`_retype_best_effort` is never even called), which is what keeps the
    atomic-write tests in `test_keboola_extractor_atomic_writes.py` — which
    call `_extract_via_extension` directly with no credentials — free of
    any Keboola-metadata network dependency."""
    from connectors.keboola import extractor

    conn = _kbc_conn_with_table("in.c-crm", "orders", "SELECT '1' AS id")

    called = {"n": 0}
    monkeypatch.setattr(
        extractor,
        "_retype_best_effort",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )

    pq_path = tmp_path / "orders.parquet"
    tc = {"name": "orders", "bucket": "in.c-crm", "source_table": "orders"}
    extractor._extract_via_extension(conn, tc, str(pq_path))  # no url/token

    assert called["n"] == 0
    assert pq.read_table(pq_path).num_rows == 1
