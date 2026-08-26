"""`agnes schema` must work on a Snowflake `query_mode='remote'` row.

A remote row has no parquet, so `/api/v2/schema/{id}` fell through to the
local-parquet branch, found nothing, and raised `NotFound` — which the CLI
prints as **"Table 'X' not found in the registry."** on a table that is very
much in the registry and answers `agnes query --remote` perfectly well. The
documented agent rails say "run `agnes schema` before writing any query", so
the dead end lands exactly where guessing a column name costs the most.

BigQuery remote and Databricks remote each already read their columns from the
upstream instead. This is the Snowflake sibling.
"""

import pytest

from connectors.snowflake.remote import fetch_schema


class _FakeConn:
    """Stands in for the DuckDB session `fetch_schema` opens and attaches."""

    def __init__(self, rows, *, fail_with=None):
        self._rows = rows
        self._fail_with = fail_with
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._fail_with is not None and "information_schema" in sql:
            raise self._fail_with
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


SETTINGS = {
    "account": "acme-prod123",
    "user": "AGNES_SVC",
    "database": "PROD",
    "warehouse": "ANALYTICS_WH",
    "role": "",
    "auth_type": "password",
    "password": "pw",
}

ROW = {"id": "gold_bi_chargeability", "bucket": "GOLD", "source_table": "BI_CHARGEABILITY"}


class TestFetchSchema:
    def test_maps_information_schema_rows_to_the_shared_column_shape(self):
        conn = _FakeConn(
            [
                ("EMPLOYEE_TYPE", "TEXT", "YES", "Employment category"),
                ("HOURS", "NUMBER", "NO", None),
            ]
        )
        cols = fetch_schema(ROW, settings=SETTINGS, conn=conn)

        assert cols == [
            {"name": "EMPLOYEE_TYPE", "type": "TEXT", "nullable": True, "description": "Employment category"},
            {"name": "HOURS", "type": "NUMBER", "nullable": False, "description": ""},
        ]

    def test_queries_the_rows_own_schema_and_table_as_bound_parameters(self):
        conn = _FakeConn([])
        fetch_schema(ROW, settings=SETTINGS, conn=conn)

        sql, params = next((s, p) for s, p in conn.executed if "information_schema" in s)
        assert "ordinal_position" in sql
        assert params == ["GOLD", "BI_CHARGEABILITY"]

    @pytest.mark.parametrize(
        "bucket,expected_database,expected_schema",
        [("GOLD", "PROD", "GOLD"), ("OTHER.GOLD", "OTHER", "GOLD")],
    )
    def test_bucket_decides_which_database_gets_attached(self, monkeypatch, bucket, expected_database, expected_schema):
        """`split_bucket` semantics: `DB.SCHEMA` is explicit, a plain value is
        the schema inside the configured database. The ATTACH must follow the
        row, or the schema call describes a table in the wrong database."""
        import connectors.snowflake.remote as sf_remote

        conn = _FakeConn([])
        seen: dict = {}

        def _fake_open(settings, database):
            seen["database"] = database
            return conn

        monkeypatch.setattr(sf_remote, "_open_attached_conn", _fake_open)
        sf_remote.fetch_schema({**ROW, "bucket": bucket}, settings=SETTINGS)

        assert seen["database"] == expected_database
        _sql, params = next((s, p) for s, p in conn.executed if "information_schema" in s)
        assert params == [expected_schema, "BI_CHARGEABILITY"]

    def test_unknown_table_raises_rather_than_returning_an_empty_schema(self):
        """An empty column list is indistinguishable from 'table has no columns'
        and would cache as a valid answer."""
        conn = _FakeConn([])
        with pytest.raises(ValueError, match="no columns"):
            fetch_schema(ROW, settings=SETTINGS, conn=conn, allow_empty=False)


class TestSchemaEndpoint:
    def test_snowflake_remote_row_does_not_fall_into_the_parquet_branch(self, seeded_app, monkeypatch):
        """The regression: a registered remote row must not answer 404."""
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        repo.register(
            id="sfschema_chargeability",
            name="sfschema_chargeability",
            source_type="snowflake",
            bucket="GOLD",
            source_table="BI_CHARGEABILITY",
            query_mode="remote",
        )
        try:
            import connectors.snowflake.remote as sf_remote
            import connectors.snowflake.settings as sf_settings

            monkeypatch.setattr(sf_settings, "resolve_snowflake_settings", lambda: SETTINGS)
            monkeypatch.setattr(
                sf_remote,
                "fetch_schema",
                lambda row, *, settings, allow_empty=True: [
                    {"name": "EMPLOYEE_TYPE", "type": "TEXT", "nullable": True, "description": ""}
                ],
            )

            c = seeded_app["client"]
            resp = c.get(
                "/api/v2/schema/sfschema_chargeability",
                headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["source_type"] == "snowflake"
            # The analyst writes DuckDB SQL against the attached `sf` catalog —
            # there is no per-query ship to Snowflake the way Databricks has.
            assert body["sql_flavor"] == "duckdb"
            assert [c["name"] for c in body["columns"]] == ["EMPLOYEE_TYPE"]
        finally:
            try:
                repo.unregister("sfschema_chargeability")
            except Exception:
                pass

    def test_unconfigured_snowflake_still_404s(self, seeded_app, monkeypatch):
        """No connection to ask — 404 is honest there."""
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        repo.register(
            id="sfschema_unconfigured",
            name="sfschema_unconfigured",
            source_type="snowflake",
            bucket="GOLD",
            source_table="T",
            query_mode="remote",
        )
        try:
            import connectors.snowflake.settings as sf_settings

            monkeypatch.setattr(sf_settings, "resolve_snowflake_settings", lambda: None)

            c = seeded_app["client"]
            resp = c.get(
                "/api/v2/schema/sfschema_unconfigured",
                headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            )
            assert resp.status_code == 404, resp.text
        finally:
            try:
                repo.unregister("sfschema_unconfigured")
            except Exception:
                pass
