"""TableRegistryRepository.register() persists v27 sync-strategy columns.

where_filters is JSON-encoded on write, decoded on read (matching the
pattern used for primary_key). Other fields are scalar pass-through.
"""

import duckdb
import pytest

from src.db import _V26_TO_V27_MIGRATIONS, _V50_TO_V51_MIGRATIONS
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def repo(tmp_path):
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
    # Match the post-v26 shape that v27 migrations alter (main's v26 is a
    # data migration with no schema change so the column shape is unchanged
    # from v25; we only need to seed the canonical post-v25 column set).
    conn.execute(
        "CREATE TABLE table_registry ("
        "id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, folder VARCHAR, "
        "sync_strategy VARCHAR DEFAULT 'full_refresh', primary_key VARCHAR, "
        "description TEXT, registered_by VARCHAR, "
        "registered_at TIMESTAMP DEFAULT current_timestamp, "
        "source_type VARCHAR, bucket VARCHAR, source_table VARCHAR, "
        "source_query TEXT, query_mode VARCHAR DEFAULT 'local', "
        "sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true)"
    )
    for sql in _V26_TO_V27_MIGRATIONS:
        conn.execute(sql)
    for sql in _V50_TO_V51_MIGRATIONS:
        conn.execute(sql)
    # v74 (#607): server_only distribution flag — register() now writes it.
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS server_only BOOLEAN DEFAULT false")
    # v79: named source connections — register() now writes connection_id.
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS connection_id VARCHAR")
    # v116: table access policies — register() now READS access_policy_sql to
    # refuse an upsert that would leave a policied row distributable (#2147).
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS access_policy_sql VARCHAR")
    return TableRegistryRepository(conn)


def test_register_with_incremental_fields(repo):
    repo.register(
        id="in.c-crm.activity",
        name="activity",
        source_type="keboola",
        bucket="in.c-crm",
        source_table="activity",
        sync_strategy="incremental",
        primary_key=["activity_id"],
        incremental_window_days=1,
        max_history_days=180,
    )
    got = repo.get("in.c-crm.activity")
    assert got["sync_strategy"] == "incremental"
    assert got["incremental_window_days"] == 1
    assert got["max_history_days"] == 180
    assert got["incremental_column"] is None
    assert got["where_filters"] is None


def test_register_with_partitioned_fields(repo):
    repo.register(
        id="in.c-sales.orders",
        name="orders",
        source_type="keboola",
        bucket="in.c-sales",
        source_table="orders",
        sync_strategy="partitioned",
        primary_key=["id"],
        partition_by="date",
        partition_granularity="month",
        initial_load_chunk_days=30,
    )
    got = repo.get("in.c-sales.orders")
    assert got["partition_by"] == "date"
    assert got["partition_granularity"] == "month"
    assert got["initial_load_chunk_days"] == 30


def test_register_with_where_filters_encodes_json(repo):
    filters = [
        {"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]},
        {"column": "country_code", "operator": "eq", "values": ["CZ", "SK"]},
    ]
    repo.register(
        id="in.c-x.y",
        name="y",
        source_type="keboola",
        bucket="in.c-x",
        source_table="y",
        where_filters=filters,
    )
    got = repo.get("in.c-x.y")
    assert got["where_filters"] == filters


def test_register_no_optional_fields_leaves_them_null(repo):
    repo.register(
        id="in.c-crm.company",
        name="company",
        source_type="keboola",
        bucket="in.c-crm",
        source_table="company",
    )
    got = repo.get("in.c-crm.company")
    assert got["sync_strategy"] == "full_refresh"
    assert got["incremental_window_days"] is None
    assert got["where_filters"] is None
    assert got["partition_by"] is None


def test_register_upsert_overwrites_v26_fields(repo):
    repo.register(
        id="in.c-crm.activity",
        name="activity",
        source_type="keboola",
        bucket="in.c-crm",
        source_table="activity",
        sync_strategy="incremental",
        incremental_window_days=1,
    )
    repo.register(
        id="in.c-crm.activity",
        name="activity",
        source_type="keboola",
        bucket="in.c-crm",
        source_table="activity",
        sync_strategy="incremental",
        incremental_window_days=7,
        max_history_days=90,
    )
    got = repo.get("in.c-crm.activity")
    assert got["incremental_window_days"] == 7
    assert got["max_history_days"] == 90
