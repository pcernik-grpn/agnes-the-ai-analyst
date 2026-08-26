"""Tests for the Snowflake connector: attach, extract, settings, admin registration, sync dispatch, and query guardrail."""

import base64
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from connectors.bigquery.extractor import MaterializeBudgetError
from connectors.snowflake.attach import (
    SF_ALIAS,
    SF_EXTENSION,
    SF_TOKEN_ENV,
    attach_snowflake,
    build_remote_attach_url,
    parse_remote_attach_url,
)
from connectors.snowflake.extract_init import init_extract
from connectors.snowflake.extractor import (
    full_table_sql,
    materialize_query,
    split_bucket,
)
from connectors.snowflake.settings import resolve_snowflake_settings

SF_SETTINGS = {
    "account": "xy12345",
    "user": "alice",
    "password": "secret",
    "database": "analytics",
    "warehouse": "compute_wh",
    "role": "analyst",
    "auth_type": "password",
    "token_env": "SNOWFLAKE_PASSWORD",
}

SF_HOST = "xy12345.snowflakecomputing.com"


@pytest.fixture
def snowflake_instance(monkeypatch):
    """Patch instance.yaml so Snowflake settings resolve from env."""
    cfg = {
        "data_source": {
            "type": "snowflake",
            "snowflake": {
                "account": SF_SETTINGS["account"],
                "user": SF_SETTINGS["user"],
                "database": SF_SETTINGS["database"],
                "warehouse": SF_SETTINGS["warehouse"],
                "role": SF_SETTINGS["role"],
                "token_env": SF_SETTINGS["token_env"],
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.setenv(SF_SETTINGS["token_env"], SF_SETTINGS["password"])
    from app.instance_config import reset_cache

    reset_cache()
    yield cfg
    reset_cache()


@pytest.fixture
def snowflake_settings():
    return SF_SETTINGS.copy()


@pytest.fixture
def sample_rsa_key():
    """Return a generated RSA private key object."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def pkcs8_pem(sample_rsa_key):
    return sample_rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def pkcs1_pem(sample_rsa_key):
    return sample_rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def encrypted_pkcs8_pem(sample_rsa_key):
    return sample_rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(b"it's secret"),
    ).decode()


# --- attach.py -----------------------------------------------------------------


def test_build_and_parse_remote_attach_url():
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
    )
    assert url.startswith(f"https://{SF_HOST}?")
    assert parse_remote_attach_url(url) == {
        "account": "xy12345",
        "database": "analytics",
        "warehouse": "compute_wh",
        "user": "alice",
        "role": "analyst",
    }


def test_build_remote_attach_url_allows_full_host():
    url = build_remote_attach_url(
        "xy12345.snowflakecomputing.com",
        "analytics",
        "compute_wh",
        "alice",
    )
    assert url.startswith("https://xy12345.snowflakecomputing.com?")


def test_build_remote_attach_url_rejects_unsafe_account():
    with pytest.raises(ValueError):
        build_remote_attach_url("evil;drop", "analytics", "compute_wh", "alice")


def test_parse_remote_attach_url_requires_https():
    with pytest.raises(ValueError):
        parse_remote_attach_url("http://xy12345.snowflakecomputing.com?database=analytics")


def test_attach_snowflake_issues_secret_and_attach(monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
    )
    attach_snowflake(conn, alias=SF_ALIAS, url=url, token="secret")
    calls = [c[0][0] for c in conn.execute.call_args_list]
    assert any("CREATE OR REPLACE SECRET" in c for c in calls)
    assert any("ATTACH '' AS sf" in c for c in calls)
    secret_call = next(c for c in calls if "CREATE OR REPLACE SECRET" in c)
    assert "ACCOUNT 'xy12345'" in secret_call
    assert "USER 'alice'" in secret_call
    assert "PASSWORD 'secret'" in secret_call
    assert "DATABASE 'analytics'" in secret_call
    assert "WAREHOUSE 'compute_wh'" in secret_call
    assert "ROLE 'analyst'" in secret_call


def test_attach_snowflake_respects_host_allowlist(monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "other.example.com")
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
    )
    with pytest.raises(ValueError, match="AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"):
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token="secret")


# --- extractor.py ----------------------------------------------------------------


def test_split_bucket_default_and_dotted():
    assert split_bucket("public", "analytics") == ("analytics", "public")
    assert split_bucket("analytics.public", "analytics") == ("analytics", "public")


def test_split_bucket_rejects_traversal():
    with pytest.raises(ValueError):
        split_bucket("../public", "analytics")
    with pytest.raises(ValueError):
        split_bucket("analytics/../public", "analytics")


def test_full_table_sql():
    assert full_table_sql("public", "orders") == 'SELECT * FROM "sf"."public"."orders"'


def test_full_table_sql_rejects_unsafe():
    with pytest.raises(ValueError):
        full_table_sql('public"', "orders")
    with pytest.raises(ValueError):
        full_table_sql("public", "orders;drop")


def _make_stub_duckdb_conn():
    """Return a stub connection that no-ops extension/secret/attach but runs real DuckDB for COPY."""
    real = duckdb.connect(":memory:")
    real.execute("ATTACH ':memory:' AS sf")
    real.execute("CREATE SCHEMA sf.public")
    real.execute("CREATE TABLE sf.public.orders AS SELECT 'EU' AS region, 100 AS revenue")

    class Stub:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args):
            upper = sql.strip().upper()
            if upper.startswith(
                (
                    "INSTALL",
                    "LOAD",
                    "CREATE SECRET",
                    "CREATE OR REPLACE SECRET",
                    "ATTACH",
                )
            ):
                return MagicMock()
            return self._real.execute(sql, *args)

        def close(self):
            self._real.close()

    return Stub(real)


def test_materialize_query_writes_parquet(tmp_path, monkeypatch, snowflake_settings):
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )
    stats = materialize_query(
        "orders_summary",
        output_dir=str(out),
        source_query='SELECT * FROM "sf"."public"."orders"',
        settings=snowflake_settings,
        max_bytes=None,
    )
    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()
    assert stats["query_mode"] == "materialized"
    assert stats["rows"] == 1
    assert stats["size_bytes"] > 0
    assert stats["hash"]


def test_materialize_query_enforces_max_bytes(tmp_path, monkeypatch, snowflake_settings):
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )
    with pytest.raises(MaterializeBudgetError):
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=1,
        )


def test_materialize_query_host_allowlist_blocks(tmp_path, monkeypatch, snowflake_settings):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "other.example.com")
    out = tmp_path / "extracts" / "snowflake"
    with pytest.raises(ValueError, match="AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"):
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=None,
        )


# --- extract_init.py --------------------------------------------------------------


def test_init_extract_creates_meta_and_views(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    out = tmp_path / "extracts" / "snowflake"
    attach_calls = []

    def fake_attach(conn, *, url, token, passphrase=None):
        attach_calls.append((url, token))
        conn.execute("ATTACH ':memory:' AS sf")
        conn.execute("CREATE SCHEMA sf.public")
        conn.execute("CREATE TABLE sf.public.orders (id INTEGER)")

    stats = init_extract(
        str(out),
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
        [{"name": "orders", "bucket": "public", "source_table": "orders", "description": ""}],
        token="secret",
        attach_fn=fake_attach,
    )
    assert stats["tables_registered"] == 1
    assert stats["errors"] == []
    assert len(attach_calls) == 1

    extract_db = duckdb.connect(str(out / "extract.duckdb"))
    try:
        meta = extract_db.execute("SELECT table_name, query_mode FROM _meta").fetchall()
        assert meta == [("orders", "remote")]
        attach_rows = extract_db.execute("SELECT alias, extension, token_env FROM _remote_attach").fetchall()
        assert attach_rows == [("sf", "snowflake", "SNOWFLAKE_PASSWORD")]
        views = extract_db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'orders'"
        ).fetchall()
        assert views == [("orders",)]
    finally:
        extract_db.close()


# --- settings.py ------------------------------------------------------------------


def test_resolve_snowflake_settings_from_env(snowflake_instance):
    settings = resolve_snowflake_settings()
    assert settings == SF_SETTINGS


def test_resolve_snowflake_settings_missing(monkeypatch):
    cfg = {
        "data_source": {
            "type": "snowflake",
            "snowflake": {
                "account": "xy",
                "user": "u",
                "database": "db",
                "warehouse": "wh",
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        assert resolve_snowflake_settings() is None
    finally:
        reset_cache()


# --- admin API --------------------------------------------------------------------


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _sf_payload(**overrides):
    p = {
        "name": "orders",
        "source_type": "snowflake",
        "bucket": "public",
        "source_table": "orders",
        "query_mode": "remote",
    }
    p.update(overrides)
    return p


@pytest.fixture
def stub_snowflake_extract(monkeypatch):
    rebuild = MagicMock(return_value={"tables_registered": 1, "errors": [], "skipped": False})
    monkeypatch.setattr("connectors.snowflake.extract_init.rebuild_from_registry", rebuild)
    return rebuild


def test_admin_precheck_snowflake_valid(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(),
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["table"]["source_type"] == "snowflake"


def test_admin_precheck_missing_bucket(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(bucket=""),
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert "bucket" in resp.json()["detail"].lower()


def test_admin_precheck_unsafe_view_name(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(name="orders-2026"),
        headers=_auth(token),
    )
    assert resp.status_code in (400, 422)


def test_admin_precheck_unconfigured_instance(seeded_app, monkeypatch):
    """When Snowflake settings are absent, precheck must fail fast."""
    cfg = {"data_source": {"type": "snowflake", "snowflake": {}}}
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table/precheck",
            json=_sf_payload(),
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"].lower()
    finally:
        reset_cache()


def test_admin_register_snowflake_remote(seeded_app, snowflake_instance, stub_snowflake_extract):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(),
        headers=_auth(token),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "registered"
    stub_snowflake_extract.assert_called_once()


def test_admin_register_snowflake_materialized(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(
            query_mode="materialized",
            source_query='SELECT * FROM "sf"."public"."orders"',
        ),
        headers=_auth(token),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "registered"


# --- sync dispatch ----------------------------------------------------------------


def test_run_materialized_pass_dispatches_snowflake(monkeypatch, tmp_path, snowflake_settings):
    from app.api.sync import _run_materialized_pass

    monkeypatch.setattr("app.api.sync._get_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("app.api.sync.is_table_due", lambda schedule, last: True)
    monkeypatch.setattr("connectors.snowflake.settings.resolve_snowflake_settings", lambda: snowflake_settings)

    sf_materialize = MagicMock(return_value={"rows": 5, "size_bytes": 100, "hash": "abc", "query_mode": "materialized"})
    monkeypatch.setattr("connectors.snowflake.extractor.materialize_query", sf_materialize)

    registry = MagicMock()
    registry.list_all.return_value = [
        {
            "name": "orders_summary",
            "id": "orders_summary",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": "SELECT region, revenue FROM sf.public.orders",
            "bucket": "public",
            "source_table": "orders",
            "sync_schedule": None,
        }
    ]
    state = MagicMock()
    state.get_last_sync.return_value = None
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: registry)
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)

    summary = _run_materialized_pass(None, None, source_type="snowflake")
    assert summary["materialized"] == ["orders_summary"]
    assert summary["errors"] == []
    assert summary["skipped"] == []
    sf_materialize.assert_called_once()
    call_kwargs = sf_materialize.call_args.kwargs
    assert call_kwargs["table_id"] == "orders_summary"
    assert call_kwargs["settings"] == snowflake_settings


# --- query guardrail --------------------------------------------------------------


def _patch_guardrail(monkeypatch, rows, admin=False):
    repo = MagicMock()
    repo.list_by_source.return_value = rows
    monkeypatch.setattr("src.repositories.table_registry_repo", lambda: repo)
    monkeypatch.setattr("app.api.query._caller_is_unrestricted_admin", lambda *a, **kw: admin)


def test_sf_guardrail_registered_and_accessible(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "public", "source_table": "orders", "name": "orders"}],
    )
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."orders"',
        'select * from sf."public"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is None


def test_sf_guardrail_unregistered_path(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(monkeypatch, [])
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."missing"',
        'select * from sf."public"."missing"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "sf_path_not_registered"


def test_sf_guardrail_access_denied(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "public", "source_table": "orders", "name": "orders"}],
    )
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."orders"',
        'select * from sf."public"."orders"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "sf_path_access_denied"


# --- PR #1389 review-finding regressions -------------------------------------------


def test_admin_register_snowflake_custom_sql_without_bucket(seeded_app, snowflake_instance, stub_snowflake_extract):
    """Finding #1: a materialized row carrying only custom SQL must register.

    The UI's synced+custom payload deliberately omits bucket/source_table. If the
    validator demands them before it reaches the custom-SQL early return, that
    payload can never be registered (nor edited afterwards).
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "orders_eu",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": 'SELECT region, revenue FROM sf."public"."orders"',
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "registered"


def test_admin_precheck_snowflake_custom_sql_without_bucket(seeded_app, snowflake_instance):
    """Finding #1: the same payload must survive the precheck route."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json={
            "name": "orders_eu",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": 'SELECT region, revenue FROM sf."public"."orders"',
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_materialize_query_swap_is_atomic(tmp_path, monkeypatch, snowflake_settings):
    """Finding #2: the previous parquet must survive a failed swap.

    Pre-fix the destination was ``unlink()``ed before the fresh file was moved
    into place, so a crash (or a concurrent reader) at that instant saw no file
    at all. With a single ``os.replace`` the old copy is still readable when the
    swap itself fails.

    The swap now lives in `src.parquet_publish.atomic_publish_finalize` (the
    shared publish protocol every connector answers to), so the break is
    injected there rather than at a connector-local ``os.replace``.
    """
    out = tmp_path / "extracts" / "snowflake"
    (out / "data").mkdir(parents=True)
    parquet_path = out / "data" / "orders_summary.parquet"
    parquet_path.write_bytes(b"PREVIOUS-GENERATION")

    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )

    def _boom(src, dst):
        raise OSError("swap interrupted")

    monkeypatch.setattr("src.parquet_publish.os.replace", _boom)

    with pytest.raises(OSError, match="swap interrupted"):
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=None,
        )

    assert parquet_path.exists()
    assert parquet_path.read_bytes() == b"PREVIOUS-GENERATION"
    # And the failed publish takes its own temp with it — `atomic_publish_
    # finalize` cleans up the commit half, so nothing is stranded beside the
    # destination for a later run (or an operator) to collect.
    assert list((out / "data").glob("*.tmp")) == []


def test_materialize_query_publishes_world_readable(tmp_path, monkeypatch, snowflake_settings):
    """The published parquet is 0644 whatever the ambient umask.

    A bare ``os.replace`` preserves the temp's mode, so under a restrictive
    umask (0077, seen in some container/systemd units) the served file lands
    0600 and `agnes pull` can no longer read it — incident #203, the reason
    `src.parquet_publish` chmods before it replaces.
    """
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )

    old_umask = os.umask(0o077)
    try:
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=None,
        )
    finally:
        os.umask(old_umask)

    pq_path = out / "data" / "orders_summary.parquet"
    assert pq_path.exists()
    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)
    assert list((out / "data").glob("*.tmp")) == []


def test_materialize_query_scratch_db_is_per_table(tmp_path, monkeypatch, snowflake_settings):
    """Finding #3: two tables must not share one scratch DuckDB file.

    ``_get_table_lock`` only serializes the SAME table, so a scheduler tick that
    overlaps an operator-triggered sync of a different table would open (and then
    delete) the same ``.tmp_materialize`` file underneath the other run.
    """
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    seen: list[str] = []

    def _open(path, **kw):
        seen.append(str(path))
        return _make_stub_duckdb_conn()

    monkeypatch.setattr("connectors.snowflake.extractor._open_duckdb", _open)

    for table_id in ("orders_a", "orders_b"):
        materialize_query(
            table_id,
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=None,
        )

    scratch = [p for p in seen if ".tmp_materialize" in p]
    assert len(scratch) == 2
    assert scratch[0] != scratch[1], f"scratch DuckDB path is shared across tables: {scratch}"


def test_run_materialized_pass_hash_fallback_uses_snowflake_dir(monkeypatch, tmp_path, snowflake_settings):
    """Finding #4: a Snowflake row that falls back to hashing must read the
    Snowflake extract dir, not the Keboola one."""
    from app.api.sync import _run_materialized_pass

    monkeypatch.setattr("app.api.sync._get_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("app.api.sync.is_table_due", lambda schedule, last: True)
    monkeypatch.setattr("connectors.snowflake.settings.resolve_snowflake_settings", lambda: snowflake_settings)
    # No `hash` in the stats dict → the fallback branch runs.
    monkeypatch.setattr(
        "connectors.snowflake.extractor.materialize_query",
        MagicMock(return_value={"rows": 5, "size_bytes": 100, "query_mode": "materialized"}),
    )

    hashed: list[str] = []

    def _fake_hash(path):
        hashed.append(str(path))
        return "deadbeef"

    monkeypatch.setattr("app.api.sync._file_hash", _fake_hash)

    registry = MagicMock()
    registry.list_all.return_value = [
        {
            "name": "orders_summary",
            "id": "orders_summary",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": 'SELECT * FROM sf."public"."orders"',
            "bucket": "public",
            "source_table": "orders",
            "sync_schedule": None,
        }
    ]
    state = MagicMock()
    state.get_last_sync.return_value = None
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: registry)
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)

    _run_materialized_pass(None, None, source_type="snowflake")

    assert hashed, "fallback hash was never taken"
    assert "extracts/snowflake/data/orders_summary.parquet" in hashed[0].replace("\\", "/")


def test_init_extract_persists_custom_token_env(tmp_path, monkeypatch):
    """Finding #5: an operator-chosen ``token_env`` must reach ``_remote_attach``.

    Both replay paths (``src/orchestrator.py`` and ``src/db.py``) read the env var
    NAMED IN THAT COLUMN, so hardcoding ``SNOWFLAKE_PASSWORD`` silently breaks
    every remote row on an instance that configured a different name.
    """
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "SF_SECRET_PASSWORD")
    out = tmp_path / "extracts" / "snowflake"

    init_extract(
        str(out),
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
        [{"name": "orders", "bucket": "public", "source_table": "orders"}],
        token="secret",
        token_env="SF_SECRET_PASSWORD",
        attach_fn=lambda conn, *, url, token, passphrase=None: None,
    )

    conn = duckdb.connect(str(out / "extract.duckdb"))
    try:
        row = conn.execute("SELECT alias, extension, token_env FROM _remote_attach").fetchone()
    finally:
        conn.close()
    assert row[0] == SF_ALIAS
    assert row[1] == SF_EXTENSION
    assert row[2] == "SF_SECRET_PASSWORD"


def test_init_extract_default_token_env_is_the_module_default(tmp_path, monkeypatch):
    """Finding #5 corollary: with no override the default name is still written."""
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    out = tmp_path / "extracts" / "snowflake"
    init_extract(
        str(out),
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
        [{"name": "orders", "bucket": "public", "source_table": "orders"}],
        token="secret",
        attach_fn=lambda conn, *, url, token, passphrase=None: None,
    )
    conn = duckdb.connect(str(out / "extract.duckdb"))
    try:
        token_env = conn.execute("SELECT token_env FROM _remote_attach").fetchone()[0]
    finally:
        conn.close()
    assert token_env == SF_TOKEN_ENV


def test_init_extract_warns_when_token_env_not_allowlisted(tmp_path, monkeypatch, caplog):
    """Finding #5: a non-allowlisted ``token_env`` still builds, but the operator
    must be told — otherwise the ATTACH is silently skipped at replay time and the
    only symptom is a missing view. The allowlist gate itself is NOT weakened."""
    import logging

    from src.orchestrator_security import is_token_env_allowed

    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
    assert not is_token_env_allowed("SF_SECRET_PASSWORD")

    out = tmp_path / "extracts" / "snowflake"
    with caplog.at_level(logging.WARNING, logger="connectors.snowflake.extract_init"):
        init_extract(
            str(out),
            SF_SETTINGS["account"],
            SF_SETTINGS["database"],
            SF_SETTINGS["warehouse"],
            SF_SETTINGS["user"],
            SF_SETTINGS["role"],
            [{"name": "orders", "bucket": "public", "source_table": "orders"}],
            token="secret",
            token_env="SF_SECRET_PASSWORD",
            attach_fn=lambda conn, *, url, token, passphrase=None: None,
        )
    assert "AGNES_REMOTE_ATTACH_TOKEN_ENVS" in caplog.text


def test_sf_guardrail_tolerates_null_bucket_rows(monkeypatch):
    """Finding #7: a custom-SQL row stores NULL bucket/source_table (finding #1
    relaxes the validator that used to force them), so the guard must not
    ``.lower()`` a ``None`` and turn every sf.* query into a 500."""
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [
            {"id": "custom", "bucket": None, "source_table": None, "name": "orders_eu"},
            {"id": "t1", "bucket": "public", "source_table": "orders", "name": "orders"},
        ],
    )
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."orders"',
        'select * from sf."public"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is None


def test_snapshot_from_query_refuses_unregistered_sf_path(seeded_app):
    """Finding #6: ``run_remote_select_to_arrow`` (``/api/v2/scan --from-query``,
    ``agnes query --remote --auto-snapshot``) re-ATTACHes the ``sf`` catalog on the
    read-only analytics connection, so it needs the SAME registry gate as
    ``/api/query``. Without it an ``sf.*`` path reaches Snowflake ungated."""
    from fastapi import HTTPException

    from app.api.query import run_remote_select_to_arrow
    from src.db import get_system_db

    conn = get_system_db()
    try:
        with pytest.raises(HTTPException) as exc:
            run_remote_select_to_arrow(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                'SELECT * FROM sf."public"."unregistered"',
                None,
                None,
            )
    finally:
        conn.close()
    assert exc.value.status_code == 403
    assert exc.value.detail["reason"] == "sf_path_not_registered"


def test_admin_register_snowflake_remote_not_configured_is_not_a_500(seeded_app, snowflake_instance, monkeypatch):
    """Finding #8: a *skipped* rebuild is a message, not a failed registration.

    ``rebuild_from_registry`` returns ``skipped/not_configured`` when the password
    resolves at validation time but not at rebuild time. Mapping that to 500
    ``rebuild_failed`` leaves the registry row in place while telling the operator
    the registration failed. Databricks' sibling never 500s.
    """
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": True,
                "reason": "not_configured",
                "tables_registered": 0,
                "errors": [],
            }
        ),
    )
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_skip"),
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "registered"
    assert "not configured" in (body.get("message") or "").lower()


def test_admin_register_snowflake_rebuild_error_is_visible_twice(seeded_app, snowflake_instance, monkeypatch):
    """A per-table rebuild error — in practice a schema/table name that does not
    exist in the account — must reach the operator through BOTH channels.

    Pre-fix it reached neither. The 500 body carried the real upstream reason
    under ``message`` only, and every admin-UI error path reads ``detail``
    (``typeof b.detail === "string" ? b.detail : "failed"``), so the operator
    saw a bare "✗ failed". Meanwhile the registry row — inserted before the
    rebuild is attempted, and deliberately kept so the name can be corrected by
    editing rather than re-typing — reported ``pending``, i.e. "never synced",
    indistinguishable from a row simply waiting for its first tick. Live
    incident 2026-08-19: four rows sat pending for days pointing at tables that
    never existed.
    """
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 0,
                "errors": [
                    {
                        "table": "orders_typo",
                        "error": "Catalog Error: Table with name BI_TYPO does not exist!",
                    }
                ],
            }
        ),
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_typo"),
        headers=_auth(token),
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "rebuild_failed"
    # `detail` is what every client renders; it must carry the upstream reason.
    assert "BI_TYPO" in body["detail"]
    # `message` stays for existing consumers, same content.
    assert body["detail"] == body["message"]

    # ...and the row must read as failed, not as "never synced".
    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == "orders_typo")
    assert row["last_sync_status"] == "error"
    assert "BI_TYPO" in (row["last_sync_error"] or "")


def test_fixing_a_bad_snowflake_name_clears_the_recorded_failure(seeded_app, snowflake_instance, monkeypatch):
    """Correcting the typo must clear the row's failed state, not leave a stale one.

    Registration marks a row failed when its remote-extract rebuild errors (so
    it stops reading "never synced"). The obvious next move is to edit the name
    — and `PUT /registry/{id}` re-runs the rebuild. If that success does not
    clear `sync_state.error`, `GET /api/admin/registry` and `/admin/sync` keep
    showing the OLD failure until the next full orchestrator sweep re-derives
    state from `_meta`, i.e. the fix looks like it did not take. A status that
    lies about a corrected row is the same defect class this whole change set
    exists to remove.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # 1. a registration whose rebuild fails → row is marked failed
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 0,
                "errors": [{"table": "orders_fix", "error": "Catalog Error: Table with name NOPE does not exist!"}],
            }
        ),
    )
    resp = c.post("/api/admin/register-table", json=_sf_payload(name="orders_fix"), headers=_auth(token))
    assert resp.status_code == 500, resp.text
    row = next(
        t for t in c.get("/api/admin/registry", headers=_auth(token)).json()["tables"] if t["id"] == "orders_fix"
    )
    assert row["last_sync_status"] == "error"

    # 2. the admin corrects the name; this rebuild succeeds
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(return_value={"skipped": False, "tables_registered": 1, "errors": []}),
    )
    resp = c.put(
        "/api/admin/registry/orders_fix",
        json={"source_table": "orders"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    row = next(
        t for t in c.get("/api/admin/registry", headers=_auth(token)).json()["tables"] if t["id"] == "orders_fix"
    )
    assert row["last_sync_status"] != "error", row
    assert not (row["last_sync_error"] or ""), row


def test_admin_register_snowflake_custom_sql_foreign_catalog_refused(seeded_app, snowflake_instance):
    """Finding #9: the materialize session ATTACHes only ``sf``. Custom SQL naming
    another catalog registers happily today and then fails at COPY time on the
    scheduler tick — exactly the 'registered but never materializes' state the
    other validators exist to prevent."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "orders_bad",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": 'SELECT * FROM other."public"."orders"',
        },
        headers=_auth(token),
    )
    assert resp.status_code == 400, resp.text
    assert "sf" in resp.json()["detail"].lower()


def test_admin_register_snowflake_custom_sql_unqualified_refused(seeded_app, snowflake_instance):
    """Finding #9: an unqualified reference resolves against nothing in the
    materialize session either."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "orders_bare",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": "SELECT * FROM orders",
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 400, resp.text


def test_admin_register_snowflake_custom_sql_allows_ctes(seeded_app, snowflake_instance, stub_snowflake_extract):
    """Finding #9: a CTE alias is a local name, not a foreign catalog."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "orders_cte",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": (
                'WITH raw AS (SELECT * FROM sf."public"."orders") '
                "SELECT region, SUM(revenue) AS revenue FROM raw GROUP BY 1"
            ),
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 201, resp.text


def test_attach_snowflake_refuses_a_key_carrying_the_dollar_quote_tag(monkeypatch):
    """The PEM is written into CREATE SECRET inside `$PK$ … $PK$` dollar-quoting,
    because a PEM carries newlines. The key is normalized before SQL is built,
    but the tag guard runs on the raw input first: a key whose text contains
    `$PK$` would close the literal early and inject the remainder as SQL.
    """
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
    )
    hostile = "-----BEGIN PRIVATE KEY-----\nQUJD$PK$, WAREHOUSE 'x'); DROP TABLE t; --\n-----END PRIVATE KEY-----"
    with pytest.raises(ValueError, match="dollar-quote tag"):
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token=hostile)
    assert not any("DROP TABLE" in str(c) for c in conn.execute.call_args_list)


def test_attach_snowflake_key_pair_decrypts_and_normalizes_to_pkcs8(monkeypatch, encrypted_pkcs8_pem):
    """An encrypted PEM plus passphrase is decrypted and written as an
    unencrypted PKCS#8 PEM file; the passphrase is consumed and not forwarded."""
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
    )
    attach_snowflake(conn, alias=SF_ALIAS, url=url, token=encrypted_pkcs8_pem, passphrase="it's secret")
    secret_call = next(c[0][0] for c in conn.execute.call_args_list if "CREATE OR REPLACE SECRET" in str(c[0][0]))
    assert "AUTH_TYPE 'key_pair'" in secret_call
    assert "PRIVATE_KEY_FILE '" in secret_call
    assert "PRIVATE_KEY_PASSPHRASE" not in secret_call
    assert encrypted_pkcs8_pem not in secret_call

    m = re.search(r"PRIVATE_KEY_FILE '([^']+)'", secret_call)
    assert m, "PRIVATE_KEY_FILE path missing from CREATE SECRET"
    key_path = Path(m.group(1))
    assert key_path.suffix == ".pem"
    assert key_path.is_file()
    content = key_path.read_text()
    assert "-----BEGIN PRIVATE KEY-----" in content
    assert "-----END PRIVATE KEY-----" in content
    key_path.unlink()


def test_attach_snowflake_key_pair_normalizes_pasted_keys(monkeypatch, pkcs8_pem, pkcs1_pem, sample_rsa_key):
    """PEMs with escaped newlines, Windows line endings, PKCS#1 form, and
    base64-only DER all resolve to a PKCS#8 unencrypted PEM."""
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)

    der_b64 = base64.b64encode(
        sample_rsa_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()

    for label, token in (
        ("pkcs8", pkcs8_pem),
        ("escaped_newlines", pkcs8_pem.replace("\n", "\\n")),
        ("windows_line_endings", pkcs8_pem.replace("\n", "\r\n")),
        ("pkcs1", pkcs1_pem),
        ("base64_only", der_b64),
    ):
        conn = MagicMock()
        url = build_remote_attach_url(
            SF_SETTINGS["account"],
            SF_SETTINGS["database"],
            SF_SETTINGS["warehouse"],
            SF_SETTINGS["user"],
        )
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token=token)
        secret_call = next(c[0][0] for c in conn.execute.call_args_list if "CREATE OR REPLACE SECRET" in str(c[0][0]))
        assert "AUTH_TYPE 'key_pair'" in secret_call, label
        assert "PRIVATE_KEY_FILE '" in secret_call, label
        assert "PRIVATE_KEY_PASSPHRASE" not in secret_call, label

        m = re.search(r"PRIVATE_KEY_FILE '([^']+)'", secret_call)
        assert m, f"PRIVATE_KEY_FILE path missing from CREATE SECRET: {label}"
        key_path = Path(m.group(1))
        assert key_path.suffix == ".pem", label
        assert key_path.is_file(), label
        content = key_path.read_text()
        assert "-----BEGIN PRIVATE KEY-----" in content, label
        assert "-----END PRIVATE KEY-----" in content, label
        key_path.unlink()


def test_attach_snowflake_password_starting_with_tilde_is_not_a_key_path(monkeypatch):
    """A plain password is fed to `_looks_like_key_pair`, which probes whether the
    value is a filesystem path by calling `Path(token).expanduser()`. On Python
    3.11+ that raises **RuntimeError** ("Could not determine home directory.")
    for a `~user` prefix naming a user that cannot be resolved — and RuntimeError
    is not an OSError/ValueError, so it escaped the probe's except tuple and
    aborted the whole attach. Any Snowflake password shaped like `~<not-a-user>…`
    made the connector unusable.
    """
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
    )
    # '~' + a username that cannot exist -> expanduser() raises RuntimeError.
    for password in ("~nonuser", "~nonuser-P@ssw0rd!", "~no_such_user_42/hunter2"):
        conn = MagicMock()
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token=password)
        secret_call = next(c[0][0] for c in conn.execute.call_args_list if "CREATE OR REPLACE SECRET" in str(c[0][0]))
        # Treated as a password, not a key path: no key-pair auth, no PEM file.
        assert "AUTH_TYPE 'key_pair'" not in secret_call, password
        assert "PRIVATE_KEY" not in secret_call, password
        assert "PASSWORD '" in secret_call, password


def test_unnameable_single_line_value_is_treated_as_an_inline_key_not_a_path(pkcs8_pem):
    """A value the OS cannot even name is not a path, so the path probe must
    fall through to the inline-key branch instead of failing the call.

    This is the deterministic form of a ~5%-per-run flake in
    `test_attach_snowflake_key_pair_normalizes_pasted_keys`. That test feeds a
    pasted base64 DER key, and base64's alphabet includes `/` — so the value
    splits into path components whose lengths depend on where those `/` land,
    i.e. on the freshly generated key's random bytes. When one component
    exceeds NAME_MAX, `Path(...).is_file()` raises `OSError(ENAMETOOLONG)`
    (which `is_file()` does not ignore) and the loader used to convert that
    into `ValueError: … could not be read`. Measured over 40 generated keys:
    2 raised. Here the over-long component is constructed rather than hoped
    for, so the case is pinned.

    A real path that cannot be read, and an unresolvable `~user`, must still
    raise — see the test below.
    """
    from connectors.snowflake.attach import _private_key_pem_and_passphrase

    # No newline, no PEM header, under the 4096 length gate, and one component
    # far beyond NAME_MAX (255) — exactly the shape base64 sometimes produces.
    unnameable = "A" * 300 + "/" + "B" * 300
    with pytest.raises(ValueError) as exc_info:
        _private_key_pem_and_passphrase(unnameable)
    # It must fail as "not a key", never as "the file could not be read".
    assert "could not be read" not in str(exc_info.value)

    # And the same shape carrying a real key body resolves normally.
    pem, passphrase = _private_key_pem_and_passphrase(pkcs8_pem)
    assert "BEGIN PRIVATE KEY" in pem
    assert passphrase is None


def test_unreadable_private_key_path_is_not_echoed_into_the_error(tmp_path):
    """The failure message must not carry the credential value.

    The path probe fires on any single-line, non-PEM value under 4 KiB — which a
    pasted base64 DER key is. If such a value happens to name a real file that
    cannot be decoded (or read at all), the raised `ValueError` used to
    interpolate `{raw!r}`, i.e. the key material itself, and `{exc}` too — an
    `OSError`'s own `str()` ends with the offending filename, which here IS the
    credential. That string then travels: `attach_snowflake` →
    `extract_init` → `register-table`'s 500 body → and, since the row is now
    marked failed, into `sync_state.error`, where `GET /api/admin/registry`,
    `/admin/sync` and `agnes admin list-tables` all read it with no redaction
    and no TTL. Admin-only, but a Snowflake service-account key's
    confidentiality boundary is not Agnes's admin boundary.
    """
    from connectors.snowflake.attach import _private_key_pem_and_passphrase

    # A real file whose bytes are not valid UTF-8 → read_text(errors="strict")
    # raises UnicodeDecodeError, the branch that used to echo the value.
    secret_looking_path = tmp_path / "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj"
    secret_looking_path.write_bytes(b"\xff\xfe\x00binary-not-utf8")

    with pytest.raises(ValueError) as exc_info:
        _private_key_pem_and_passphrase(str(secret_looking_path))

    msg = str(exc_info.value)
    assert secret_looking_path.name not in msg, msg
    assert str(secret_looking_path) not in msg, msg
    # Still has to be diagnosable: name the setting and the failure class.
    assert "private key" in msg.lower(), msg
    assert "UnicodeDecodeError" in msg or "unicode" in msg.lower(), msg


def test_private_key_path_that_cannot_be_expanded_raises_valueerror(monkeypatch):
    """When the credential really is meant to be a key *path* but '~user' cannot
    be resolved, the loader must surface the typed `ValueError` its own contract
    promises rather than letting `expanduser()`'s RuntimeError escape."""
    from connectors.snowflake.attach import _private_key_pem_and_passphrase

    with pytest.raises(ValueError, match="could not be read"):
        _private_key_pem_and_passphrase("~no_such_user_42/snowflake_key.pem")


def test_another_rows_rebuild_error_does_not_mark_the_new_row_failed(seeded_app, snowflake_instance, monkeypatch):
    """A healthy new row must not inherit somebody else's rebuild failure.

    `rebuild_from_registry` walks EVERY `query_mode='remote'` Snowflake row, not
    just the one being registered, and `_rebuild_snowflake_remote_extract`
    collapses all per-table errors into one aggregate string. Stamping that
    string on `request.name` therefore marked the row the operator just added
    as `error` — quoting a completely different table's name — whenever any
    pre-existing row was broken. On an instance that already has a few phantom
    rows (the live case this change set came from) every subsequent valid
    registration read as failed until the next orchestrator sweep re-derived
    state from `_meta`.

    The 500 itself is deliberately left alone here: the extract rebuild really
    did fail, so telling the caller "something went wrong" is not the lie — the
    lie was pinning it on their row.
    """
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 1,
                "errors": [
                    {
                        "table": "other_broken_row",
                        "error": "Catalog Error: Table with name BI_GONE does not exist!",
                    }
                ],
            }
        ),
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_good"),
        headers=_auth(token),
    )
    assert resp.status_code == 500, resp.text

    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == "orders_good")
    assert row["last_sync_status"] != "error", (
        "the newly registered row was marked failed by another row's error; "
        f"last_sync_error={row.get('last_sync_error')!r}"
    )
    assert "BI_GONE" not in (row.get("last_sync_error") or "")
    assert "other_broken_row" not in (row.get("last_sync_error") or "")


def test_a_skipped_rebuild_does_not_clear_a_recorded_failure(seeded_app, snowflake_instance, monkeypatch):
    """A rebuild that never ran must not wipe the row's real failure.

    `_rebuild_snowflake_remote_extract` reports ``ok=True`` for a benign SKIP
    (`not_configured` / `no_remote_rows`) on purpose, so a skip cannot turn a
    successful registration into a 500. The edit path's background wrapper
    branched on `ok` alone and called `clear_error`, which sets
    ``status='ok'`` and blanks the message — so editing a row on an instance
    whose Snowflake password no longer resolves at rebuild time made a table
    the operator still cannot query read as green in `/admin/sync` and
    `GET /api/admin/registry`, with nothing to correct it until the next full
    orchestrator sweep. Nothing was verified; the recorded failure was still
    true.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Register with a per-table error so the row is recorded as failed.
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 0,
                "errors": [{"table": "orders_broken", "error": "Catalog Error: nope"}],
            }
        ),
    )
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_broken"),
        headers=_auth(token),
    )
    assert resp.status_code == 500, resp.text
    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    assert next(t for t in reg["tables"] if t["id"] == "orders_broken")["last_sync_status"] == "error"

    # Now the rebuild is SKIPPED rather than run — nothing is verified.
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": True,
                "reason": "not_configured",
                "tables_registered": 0,
                "errors": [],
            }
        ),
    )
    from app.api.admin import _rebuild_snowflake_remote_extract_bg

    _rebuild_snowflake_remote_extract_bg("orders_broken")

    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == "orders_broken")
    assert row["last_sync_status"] == "error", (
        "a skipped rebuild cleared the row's recorded failure — the table is still unusable but now reads as healthy"
    )
    assert "Catalog Error" in (row["last_sync_error"] or "")


def test_fixing_one_row_clears_its_error_even_while_another_row_is_broken(seeded_app, snowflake_instance, monkeypatch):
    """A corrected row must clear, whoever else on the instance is broken.

    `_rebuild_snowflake_remote_extract` walks EVERY registered remote row and
    reports ``ok=False`` as soon as any one of them errors. Gating the clear on
    that aggregate means a single pre-existing phantom row — the live scenario
    this change set came from — keeps `ok` permanently False, so the row the
    operator just fixed never loses its recorded failure and `/admin/sync` and
    `GET /api/admin/registry` keep serving the old error. The fix then reads as
    if it did not take, which is the exact symptom being removed.

    The recording side already attributes per row (`failed_tables`); the
    clearing side has to use the same attribution.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 0,
                "errors": [{"table": "orders_typo", "error": "Catalog Error: BI_TYPO"}],
            }
        ),
    )
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_typo"),
        headers=_auth(token),
    )
    assert resp.status_code == 500, resp.text
    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    assert next(t for t in reg["tables"] if t["id"] == "orders_typo")["last_sync_status"] == "error"

    # The operator corrects that row. The rebuild RUNS and this row now builds
    # — but a different, pre-existing row is still broken, so the aggregate
    # outcome stays ok=False.
    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(
            return_value={
                "skipped": False,
                "tables_registered": 1,
                "errors": [{"table": "some_other_phantom_row", "error": "Catalog Error: GONE"}],
            }
        ),
    )
    from app.api.admin import _rebuild_snowflake_remote_extract_bg

    _rebuild_snowflake_remote_extract_bg("orders_typo")

    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == "orders_typo")
    assert row["last_sync_status"] != "error", (
        "the corrected row kept its old failure because an unrelated row is broken — "
        f"last_sync_error={row.get('last_sync_error')!r}"
    )


def test_rename_edit_attributes_the_rebuild_to_the_new_name(seeded_app, snowflake_instance, monkeypatch):
    """A PUT that renames the row must hand the rebuild the row's NEW name.

    `sync_state.table_id == table_registry.name` by convention, and
    `_rebuild_snowflake_remote_extract` attributes its per-table errors from
    the CURRENT registry rows. Passing `existing["name"]` — the pre-update
    name — means the old name is absent from `failed_tables` no matter what
    the rebuild found, so the "not in failed_tables" guard always passes and a
    rename that left the table broken would clear the only record of the
    failure. Renames are an anticipated case on this path (the surrounding
    comment says the rebuild exists so the view picks them up).
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    monkeypatch.setattr(
        "connectors.snowflake.extract_init.rebuild_from_registry",
        MagicMock(return_value={"skipped": False, "tables_registered": 1, "errors": []}),
    )
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(name="orders_before"),
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    table_id = resp.json()["id"]

    seen: list = []
    monkeypatch.setattr(
        "app.api.admin._rebuild_snowflake_remote_extract_bg",
        lambda table_name=None: seen.append(table_name),
    )
    resp = c.put(
        f"/api/admin/registry/{table_id}",
        json={"name": "orders_after"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert seen == ["orders_after"], (
        "the rebuild was attributed to the pre-update name; a rename that leaves the "
        f"table broken would then clear its recorded failure — got {seen!r}"
    )
