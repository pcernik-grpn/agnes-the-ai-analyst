"""extractor.run() dispatches on sync_strategy."""

import pyarrow as pa
import pyarrow.parquet as pq


def _stub_extension(monkeypatch):
    """Make _try_attach_extension return True with a fake kbc catalog so
    full_refresh path can `CREATE VIEW kbc...` without a real Keboola.
    """
    from connectors.keboola import extractor

    def fake_attach(conn, url, token):
        conn.execute("ATTACH ':memory:' AS kbc")
        return True

    monkeypatch.setattr(extractor, "_try_attach_extension", fake_attach)


def test_full_refresh_uses_extension_path(tmp_path, monkeypatch):
    from connectors.keboola import extractor

    _stub_extension(monkeypatch)

    called = {"extension": 0, "incremental": 0, "partitioned": 0}

    def fake_via_extension(conn, tc, pq_path, *a, **kw):
        called["extension"] += 1
        pa_t = pa.table({"id": pa.array([1, 2])})
        pq.write_table(pa_t, pq_path)

    def fake_incremental(**kw):
        called["incremental"] += 1
        return {"rows": 0, "delta_rows": 0, "changed_since_used": None}

    def fake_partitioned(**kw):
        called["partitioned"] += 1
        return {"rows": 0, "delta_rows": 0, "partitions_touched": 0}

    monkeypatch.setattr(extractor, "_extract_via_extension", fake_via_extension)
    monkeypatch.setattr("connectors.keboola.incremental.extract_incremental", fake_incremental)
    monkeypatch.setattr("connectors.keboola.partitioned.extract_partitioned", fake_partitioned)

    tcs = [
        {
            "id": "in.c-crm.company",
            "name": "company",
            "bucket": "in.c-crm",
            "source_table": "company",
            "sync_strategy": "full_refresh",
            "query_mode": "local",
        }
    ]
    extractor.run(str(tmp_path), tcs, "https://kbc.example", "tok")
    assert called == {"extension": 1, "incremental": 0, "partitioned": 0}


def test_incremental_calls_extract_incremental(tmp_path, monkeypatch):
    from connectors.keboola import extractor

    _stub_extension(monkeypatch)

    called = {"extension": 0, "incremental": 0, "partitioned": 0}

    def fake_via_extension(*a, **kw):
        called["extension"] += 1

    def fake_incremental(**kw):
        called["incremental"] += 1
        # Write a tiny parquet at the requested path so post-extract view + count work
        pa_t = pa.table({"id": pa.array([1])})
        pq.write_table(pa_t, kw["parquet_path"])
        return {"rows": 1, "delta_rows": 1, "changed_since_used": None}

    monkeypatch.setattr(extractor, "_extract_via_extension", fake_via_extension)
    monkeypatch.setattr("connectors.keboola.incremental.extract_incremental", fake_incremental)
    # last_sync read returns None (clean state)
    monkeypatch.setattr(extractor, "_read_last_sync", lambda tid: None)

    tcs = [
        {
            "id": "in.c-crm.activity",
            "name": "activity",
            "bucket": "in.c-crm",
            "source_table": "activity",
            "sync_strategy": "incremental",
            "query_mode": "local",
            "primary_key": ["activity_id"],
        }
    ]
    result = extractor.run(str(tmp_path), tcs, "https://kbc.example", "tok")
    assert called == {"extension": 0, "incremental": 1, "partitioned": 0}
    assert result["tables_extracted"] == 1


def test_partitioned_calls_extract_partitioned(tmp_path, monkeypatch):
    from connectors.keboola import extractor

    _stub_extension(monkeypatch)

    called = {"extension": 0, "incremental": 0, "partitioned": 0}

    def fake_via_extension(*a, **kw):
        called["extension"] += 1

    def fake_partitioned(**kw):
        called["partitioned"] += 1
        # Write a partition file inside the requested output_dir so view + count work
        out = kw["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        pa_t = pa.table({"id": pa.array([1])})
        pq.write_table(pa_t, out / "2026_05.parquet")
        return {
            "rows": 1,
            "delta_rows": 1,
            "partitions_touched": 1,
            "changed_since_used": None,
        }

    monkeypatch.setattr(extractor, "_extract_via_extension", fake_via_extension)
    monkeypatch.setattr("connectors.keboola.partitioned.extract_partitioned", fake_partitioned)
    monkeypatch.setattr(extractor, "_read_last_sync", lambda tid: None)

    tcs = [
        {
            "id": "in.c-sales.orders",
            "name": "orders",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "sync_strategy": "partitioned",
            "query_mode": "local",
            "partition_by": "date",
            "partition_granularity": "month",
            "primary_key": ["id"],
        }
    ]
    result = extractor.run(str(tmp_path), tcs, "https://kbc.example", "tok")
    assert called == {"extension": 0, "incremental": 0, "partitioned": 1}
    assert result["tables_extracted"] == 1


def test_where_filters_force_legacy_path(tmp_path, monkeypatch):
    """Tables with where_filters bypass the extension regardless of availability."""
    from connectors.keboola import extractor

    _stub_extension(monkeypatch)

    called = {"extension": 0, "legacy": 0}

    def fake_via_extension(*a, **kw):
        called["extension"] += 1

    captured_filters = {}

    def fake_legacy(tc, pq_path, url, token, where_filters=None):
        called["legacy"] += 1
        captured_filters["v"] = where_filters
        pa_t = pa.table({"id": pa.array([1])})
        pq.write_table(pa_t, pq_path)

    monkeypatch.setattr(extractor, "_extract_via_extension", fake_via_extension)
    monkeypatch.setattr(extractor, "_extract_via_legacy", fake_legacy)

    tcs = [
        {
            "id": "in.c-crm.opp",
            "name": "opp",
            "bucket": "in.c-crm",
            "source_table": "opp",
            "sync_strategy": "full_refresh",
            "query_mode": "local",
            "where_filters": [
                {"column": "snapshot_date", "operator": "ge", "values": ["{{last_6_months}}"]},
            ],
        }
    ]
    extractor.run(str(tmp_path), tcs, "https://kbc.example", "tok")
    assert called == {"extension": 0, "legacy": 1}
    assert captured_filters["v"] is not None
    assert captured_filters["v"][0]["column"] == "snapshot_date"
    # Placeholder must have been resolved to a YYYY-MM-DD before reaching legacy path
    assert "{{" not in captured_filters["v"][0]["values"][0]
