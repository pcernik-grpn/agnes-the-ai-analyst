"""``src.db_transient.is_transient_db_error`` — the shared classifier the
SharePoint crawl's ingest-step retry and the worker runtime's exception
reclassification both use (TCRD-296 C.11)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
import sqlalchemy as sa

from src.db_transient import is_transient_db_error


def _dbapi_error(cls, sqlstate: str | None):
    orig = RuntimeError("boom")
    if sqlstate is not None:
        orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return cls("SELECT 1", {}, orig)


class TestTransient:
    def test_pool_timeout_is_transient(self):
        assert is_transient_db_error(sa.exc.TimeoutError("QueuePool limit of size 5 reached")) is True

    def test_asyncio_timeout_is_transient(self):
        assert is_transient_db_error(asyncio.TimeoutError()) is True

    @pytest.mark.parametrize(
        "sqlstate",
        ["08000", "08001", "08003", "08004", "08006", "40001", "40P01"],
    )
    def test_operational_error_with_transient_sqlstate(self, sqlstate):
        exc = _dbapi_error(sa.exc.OperationalError, sqlstate)
        assert is_transient_db_error(exc) is True

    def test_operational_error_with_no_sqlstate_is_transient(self):
        """A raw connection-level failure (refused/reset/DNS) never reached
        the server to get a sqlstate at all — still transient."""
        exc = _dbapi_error(sa.exc.OperationalError, None)
        assert is_transient_db_error(exc) is True

    def test_bare_psycopg_operational_error_is_transient(self):
        assert is_transient_db_error(psycopg.OperationalError("connection refused")) is True


class TestNotTransient:
    def test_integrity_error_is_not_transient(self):
        exc = _dbapi_error(sa.exc.IntegrityError, "23503")
        assert is_transient_db_error(exc) is False

    def test_data_error_is_not_transient(self):
        exc = _dbapi_error(sa.exc.DataError, "22001")
        assert is_transient_db_error(exc) is False

    def test_programming_error_is_not_transient(self):
        exc = _dbapi_error(sa.exc.ProgrammingError, "42601")
        assert is_transient_db_error(exc) is False

    def test_operational_error_with_a_known_non_transient_sqlstate(self):
        """An OperationalError carrying a REAL, non-connection sqlstate
        (e.g. an admin-initiated shutdown) must not be retried — a retry
        would just re-fail identically."""
        exc = _dbapi_error(sa.exc.OperationalError, "57P03")
        assert is_transient_db_error(exc) is False

    def test_plain_value_error_is_not_transient(self):
        assert is_transient_db_error(ValueError("not a db error")) is False

    def test_runtime_error_is_not_transient(self):
        assert is_transient_db_error(RuntimeError("ingest_file rejected doc.docx: unknown reason")) is False
