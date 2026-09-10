"""``bq_fqn`` must be load-bearing in the metadata paths too (issue #343
follow-up, part 2).

Part 1 taught the two *execution* path builders to resolve a row's own
``project.dataset.table`` — ``app/api/query.py`` for ``--remote`` and
``app/api/v2_scan.py:_build_bq_sql`` for scan / estimate / snapshot.

Three metadata paths never got the same treatment and still hard-code the
configured ``data_source.bigquery.project``:

* ``connectors/bigquery/access.py:_fetch_bq_columns_full_impl`` builds
  ``\\`{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS\\``` — the column list
  behind ``GET /api/v2/schema/{id}``.
* ``app/api/v2_sample.py:_fetch_bq_sample`` builds
  ``SELECT * FROM \\`{project}.{dataset}.{table}\\```.
* ``connectors/bigquery/metadata.py`` builds four more such paths for the
  catalog's row counts, size, entity type and column cache.

The failure is worse than a missing feature, because what sits at
``<configured-project>.<dataset>`` decides which of three things happens:

1. the dataset does not exist there — a loud upstream error;
2. the dataset exists but the table does not — HTTP 200 with **zero
   columns**;
3. a same-named object exists there — HTTP 200 with **another object's
   schema**, silently.

Shape 3 is the reason this is not merely a discovery gap. ``/api/v2/scan``
validates ``select`` / ``where`` / ``order_by`` against
``v2_schema.build_schema`` before it builds any SQL, so a column that only
exists in the real table is rejected as ``unknown columns: [...]`` even
though part 1 already taught that endpoint's SQL builder to address the
right table.

Every site keeps the legacy configured-project + ``bucket`` +
``source_table`` behaviour when ``bq_fqn`` is absent (pre-v51 rows).
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    """A system DB rooted in ``tmp_path`` — same shape as the one in
    ``tests/test_v2_schema.py``, which these tests sit alongside."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _capture_conn(rows=None):
    """A DuckDB conn double that records every ``bigquery_query`` statement.

    Returns ``(conn, captured)`` where ``captured`` is a list of the
    ``bq_sql`` strings passed as the second positional argument.
    """
    captured: list[str] = []

    def _execute(sql, params=None):
        if params and "bigquery_query" in sql:
            captured.append(params[1])
        result = MagicMock()
        result.fetchall.return_value = list(rows or [])
        result.fetchone.return_value = (rows or [None])[0] if rows else None
        result.fetchdf.return_value = MagicMock(
            to_dict=lambda orient: [],
        )
        return result

    conn = MagicMock()
    conn.execute.side_effect = _execute
    return conn, captured


# ---------------------------------------------------------------------------
# connectors/bigquery/access.py — the shared column fetch
# ---------------------------------------------------------------------------


class TestFetchBqColumnsFullHonorsProjectOverride:
    def test_explicit_project_overrides_the_configured_one(self, bq_access):
        """The INFORMATION_SCHEMA path must use the caller's project when
        one is supplied, not ``bq.projects.data``."""
        from connectors.bigquery.access import _fetch_bq_columns_full_impl

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        _fetch_bq_columns_full_impl(bq, "events_ds", "events", project="data-project")

        assert "`data-project.events_ds.INFORMATION_SCHEMA.COLUMNS`" in captured[0]
        assert "configured-project" not in captured[0]

    def test_no_override_falls_back_to_the_configured_project(self, bq_access):
        """Pre-v51 rows pass no project and must behave exactly as before."""
        from connectors.bigquery.access import _fetch_bq_columns_full_impl

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        _fetch_bq_columns_full_impl(bq, "events_ds", "events")

        assert "`configured-project.events_ds.INFORMATION_SCHEMA.COLUMNS`" in captured[0]

    def test_override_project_is_identifier_validated(self, bq_access):
        """The override is interpolated into a backtick-quoted path, so it
        gets the same validation the configured project already gets."""
        from connectors.bigquery.access import _fetch_bq_columns_full_impl

        conn, _ = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        with pytest.raises(ValueError, match="unsafe BQ identifier"):
            _fetch_bq_columns_full_impl(bq, "events_ds", "events", project="bad`project")

    def test_best_effort_wrapper_forwards_the_override(self, bq_access):
        """``fetch_bq_columns_full`` is the non-raising twin and must carry
        the project through, otherwise the catalog cache still resolves the
        configured project."""
        from connectors.bigquery.access import fetch_bq_columns_full

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        fetch_bq_columns_full(bq, "events_ds", "events", project="data-project")

        assert "`data-project.events_ds.INFORMATION_SCHEMA.COLUMNS`" in captured[0]


# ---------------------------------------------------------------------------
# The shared row resolver
# ---------------------------------------------------------------------------


class TestBqRowTarget:
    def test_bq_fqn_overrides_all_three_legs(self):
        from connectors.bigquery.access import bq_row_target

        row = {
            "bucket": "Marketing",  # friendly label, not a dataset
            "source_table": "ignored_legacy_name",
            "bq_fqn": "data-project.real_dataset.real_table",
        }

        assert bq_row_target(row) == ("real_dataset", "real_table", "data-project")

    def test_legacy_row_keeps_bucket_and_source_table(self):
        from connectors.bigquery.access import bq_row_target

        row = {"bucket": "events_ds", "source_table": "events", "bq_fqn": None}

        assert bq_row_target(row) == ("events_ds", "events", None)

    def test_malformed_bq_fqn_degrades_to_the_legacy_triplet(self):
        """One out-of-band bad row must not break the surfaces that merely
        list it — the same contract the execution paths already keep."""
        from connectors.bigquery.access import bq_row_target

        row = {"bucket": "events_ds", "source_table": "events", "bq_fqn": "not-a-fqn"}

        assert bq_row_target(row) == ("events_ds", "events", None)


# ---------------------------------------------------------------------------
# app/api/v2_schema.py — GET /api/v2/schema/{id}
# ---------------------------------------------------------------------------


class TestSchemaResolvesBqFqn:
    def test_schema_reads_columns_from_the_bq_fqn_project(self, reload_db, bq_access):
        """The reported bug: a cross-project row's schema was read from
        ``<configured-project>.<bucket>``."""
        from app.api import v2_schema
        from src.repositories.table_registry import TableRegistryRepository

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        db = reload_db.get_system_db()
        try:
            TableRegistryRepository(db).register(
                id="events", name="events", source_type="bigquery",
                bucket="events_ds", source_table="events", query_mode="remote",
                bq_fqn="data-project.events_ds.events",
            )
            v2_schema.build_schema_uncached(db, "events", bq=bq)
        finally:
            db.close()

        assert captured, "no BigQuery statement was issued"
        assert all("configured-project" not in sql for sql in captured), captured
        assert any("`data-project.events_ds.INFORMATION_SCHEMA.COLUMNS`" in sql for sql in captured)

    def test_legacy_row_still_reads_the_configured_project(self, reload_db, bq_access):
        from app.api import v2_schema
        from src.repositories.table_registry import TableRegistryRepository

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        db = reload_db.get_system_db()
        try:
            TableRegistryRepository(db).register(
                id="legacy", name="legacy", source_type="bigquery",
                bucket="events_ds", source_table="events", query_mode="remote",
            )
            v2_schema.build_schema_uncached(db, "legacy", bq=bq)
        finally:
            db.close()

        assert any("`configured-project.events_ds.INFORMATION_SCHEMA.COLUMNS`" in sql for sql in captured)


# ---------------------------------------------------------------------------
# app/api/v2_sample.py — the sample / describe path
# ---------------------------------------------------------------------------


class TestSampleResolvesBqFqn:
    def test_sample_selects_from_the_bq_fqn_path(self, bq_access):
        from app.api.v2_sample import _fetch_bq_sample

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        _fetch_bq_sample(bq, "events_ds", "events", 5, project="data-project")

        assert "`data-project.events_ds.events`" in captured[0]
        assert "configured-project" not in captured[0]

    def test_sample_without_override_uses_the_configured_project(self, bq_access):
        from app.api.v2_sample import _fetch_bq_sample

        conn, captured = _capture_conn()
        bq = bq_access(duckdb_conn=conn, billing="billing-proj", data="configured-project")

        _fetch_bq_sample(bq, "events_ds", "events", 5)

        assert "`configured-project.events_ds.events`" in captured[0]


# ---------------------------------------------------------------------------
# connectors/bigquery/metadata.py — the catalog metadata provider
# ---------------------------------------------------------------------------


class TestMetadataRequestCarriesProject:
    def test_request_defaults_to_no_project(self):
        """Adding the field must not break existing construction sites."""
        from app.api._metadata_models import MetadataRequest

        req = MetadataRequest(table_id="t", bucket="ds", source_table="t")

        assert req.project is None

    def test_entity_type_lookup_uses_the_request_project(self, bq_access, monkeypatch):
        """The provider's own lookups run over the SDK (`_run_bq_sql`), not
        the DuckDB extension, so capture at that seam."""
        from app.api._metadata_models import MetadataRequest
        from connectors.bigquery import metadata as bq_metadata

        conn, _ = _capture_conn()
        client = MagicMock()
        client.get_dataset.return_value = MagicMock(location="US")
        bq = bq_access(
            client=client, duckdb_conn=conn,
            billing="billing-proj", data="configured-project",
        )
        monkeypatch.setattr(bq_metadata, "get_bq_access", lambda: bq)

        sdk_sql: list[str] = []

        def _fake_run(bq_, sql, params, **kw):
            sdk_sql.append(sql)
            # The row-count/size lookups want (row_count, size_bytes); the
            # entity-type one wants a single column.
            return (0, 0) if "INFORMATION_SCHEMA.TABLES" not in sql else ("BASE TABLE",)

        monkeypatch.setattr(bq_metadata, "_run_bq_sql", _fake_run)

        req = MetadataRequest(
            table_id="events", bucket="events_ds", source_table="events",
            project="data-project",
        )
        bq_metadata.fetch(req)

        assert sdk_sql, "no BigQuery statement was issued"
        assert all("configured-project" not in sql for sql in sdk_sql), sdk_sql
        assert any("`data-project.events_ds.INFORMATION_SCHEMA.TABLES`" in sql for sql in sdk_sql)


class TestRefreshOneFillsProjectFromRow:
    def test_refresh_one_passes_the_row_bq_fqn_project(self, reload_db, monkeypatch):
        """``refresh_one`` is what populates the catalog's entity/row-count
        cache, so a cross-project row stayed blank there."""
        from app.api import bq_metadata_refresh

        seen = {}

        def _fake_fetch(req):
            seen["project"] = req.project
            seen["bucket"] = req.bucket
            seen["source_table"] = req.source_table

        import connectors.bigquery.metadata as bq_metadata
        monkeypatch.setattr(bq_metadata, "fetch", _fake_fetch)

        bq_metadata_refresh.refresh_one({
            "id": "events",
            "bucket": "events_ds",
            "source_table": "events",
            "bq_fqn": "data-project.events_ds.events",
        })

        assert seen["project"] == "data-project"
        assert seen["bucket"] == "events_ds"
        assert seen["source_table"] == "events"
