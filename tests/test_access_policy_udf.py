"""The DuckDB-only keyed-pseudonym UDF behind the `pseudonymize_keyed` mask.

`agnes_hmac(col)` is the one scalar function Agnes itself registers on the
analytics connection for access-policy bodies to call. What this file pins:

* it computes the same value Python's ``hmac.new(key, v, sha256).hexdigest()``
  does, so a pseudonym is reproducible outside the database by whoever holds
  the key -- and by nobody else;
* NULL in, NULL out (a pseudonym must never invent a real-looking value where
  there is none, the same edge the partial masks handle);
* the instance key is resolved ONCE per process and lives only in a Python
  closure -- never in SQL text, never in a table, never in a DuckDB setting,
  so it cannot be read back through the very connection the function runs on;
* registration itself never resolves (and therefore never auto-provisions) the
  key -- opening a connection must not be what mints an instance's key;
* an unresolvable key fails the query rather than returning the plaintext.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from src.access_policy_udf import (
    POLICY_HMAC_FUNCTION,
    POLICY_UDF_NAMES,
    references_policy_udf,
    register_policy_udfs,
    reset_key_cache,
)

KEY = "0123456789abcdef0123456789abcdef"


def _expected(value: str, key: str = KEY) -> str:
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _clean_key_cache(monkeypatch):
    """The key cache is process-wide by design (see the module docstring);
    reset it around every test so one test's key never answers another's."""
    reset_key_cache()
    monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", KEY)
    yield
    reset_key_cache()


@pytest.fixture
def conn():
    from src.duckdb_conn import _open_duckdb

    c = _open_duckdb(":memory:")
    register_policy_udfs(c)
    try:
        yield c
    finally:
        c.close()


class TestValue:
    def test_matches_python_hmac_sha256(self, conn):
        row = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('alice@example.com')").fetchone()
        assert row[0] == _expected("alice@example.com")

    def test_null_in_null_out(self, conn):
        row = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}(NULL)").fetchone()
        assert row[0] is None

    def test_empty_string_is_hashed_not_nulled(self, conn):
        row = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('')").fetchone()
        assert row[0] == _expected("")

    def test_same_input_same_pseudonym_so_it_joins(self, conn):
        rows = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}(v) FROM (VALUES ('a'), ('a'), ('b')) AS t(v)").fetchall()
        assert rows[0][0] == rows[1][0]
        assert rows[0][0] != rows[2][0]

    def test_a_different_key_gives_a_different_pseudonym(self, conn, monkeypatch):
        """Cross-instance correlation is exactly what the key buys over md5."""
        first = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('alice@example.com')").fetchone()[0]
        reset_key_cache()
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "ffffffffffffffffffffffffffffffff")
        second = conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('alice@example.com')").fetchone()[0]
        assert first != second
        assert first == _expected("alice@example.com")


class TestRegistration:
    def test_registering_twice_on_one_connection_is_a_no_op(self, conn):
        register_policy_udfs(conn)
        register_policy_udfs(conn)
        assert conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")

    def test_a_cursor_of_a_registered_connection_sees_the_function(self, conn):
        cur = conn.cursor()
        try:
            assert cur.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")
            # A cursor is where re-registration would otherwise explode.
            register_policy_udfs(cur)
            assert cur.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")
        finally:
            cur.close()

    def test_registration_never_resolves_the_key(self, monkeypatch):
        """Opening a connection must not be what provisions an instance's
        key -- only a query that actually calls the function may."""
        from src.duckdb_conn import _open_duckdb

        calls: list[int] = []

        def _boom():
            calls.append(1)
            raise AssertionError("the key must not be resolved at registration time")

        monkeypatch.setattr("src.anonymization_key.resolve_or_provision_key", _boom)
        c = _open_duckdb(":memory:")
        try:
            register_policy_udfs(c)
            assert calls == []
        finally:
            c.close()

    def test_the_key_is_resolved_once_per_process_not_per_row(self, monkeypatch):
        from src.duckdb_conn import _open_duckdb

        calls: list[int] = []

        def _counting():
            calls.append(1)
            return KEY.encode("utf-8")

        monkeypatch.setattr("src.anonymization_key.resolve_or_provision_key", _counting)
        c = _open_duckdb(":memory:")
        try:
            register_policy_udfs(c)
            c.execute(f"SELECT {POLICY_HMAC_FUNCTION}(v) FROM (SELECT 'a' AS v FROM range(500))").fetchall()
            c.execute(f"SELECT {POLICY_HMAC_FUNCTION}('b')").fetchone()
            assert calls == [1]
        finally:
            c.close()


class TestKeySecrecy:
    def test_the_key_is_not_readable_through_the_connection(self, conn):
        """A closure is not a catalog object: nothing the connection can be
        asked about carries the key material."""
        for query in (
            f"SELECT * FROM duckdb_functions() WHERE function_name = '{POLICY_HMAC_FUNCTION}'",
            "SELECT * FROM duckdb_settings()",
        ):
            rendered = str(conn.execute(query).fetchall())
            assert KEY not in rendered

    def test_an_unresolvable_key_fails_the_query_and_never_returns_plaintext(self, monkeypatch):
        from src.anonymization_key import AnonymizationKeyError
        from src.duckdb_conn import _open_duckdb

        def _fail():
            raise AnonymizationKeyError("no key here")

        monkeypatch.setattr("src.anonymization_key.resolve_or_provision_key", _fail)
        c = _open_duckdb(":memory:")
        try:
            register_policy_udfs(c)
            with pytest.raises(Exception) as exc:
                c.execute(f"SELECT {POLICY_HMAC_FUNCTION}('alice@example.com')").fetchall()
            assert "alice@example.com" not in str(exc.value)
        finally:
            c.close()

    def test_a_resolution_failure_is_not_cached_as_a_key(self, monkeypatch):
        """A transient vault outage must not poison the process: once the key
        resolves again the function works, and it never falls back to a
        default/empty key in between."""
        from src.anonymization_key import AnonymizationKeyError
        from src.duckdb_conn import _open_duckdb

        state = {"fail": True}

        def _flaky():
            if state["fail"]:
                raise AnonymizationKeyError("vault down")
            return KEY.encode("utf-8")

        monkeypatch.setattr("src.anonymization_key.resolve_or_provision_key", _flaky)
        c = _open_duckdb(":memory:")
        try:
            register_policy_udfs(c)
            with pytest.raises(Exception):
                c.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchall()
            state["fail"] = False
            assert c.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")
        finally:
            c.close()


class TestReferenceDetection:
    """`references_policy_udf` is what the remote-transpile paths and the
    save-time validator use to refuse a body they must never ship."""

    def test_detects_a_call(self):
        assert references_policy_udf('SELECT agnes_hmac("email") AS "email" FROM t') == POLICY_HMAC_FUNCTION

    def test_detects_a_quoted_call(self):
        assert references_policy_udf('SELECT "agnes_hmac"(email) FROM t') == POLICY_HMAC_FUNCTION

    def test_ignores_an_ordinary_body(self):
        assert references_policy_udf("SELECT md5(email) AS email FROM t") is None

    def test_ignores_a_column_named_like_the_function(self):
        assert references_policy_udf("SELECT agnes_hmac_note FROM t") is None

    def test_unparseable_sql_fails_closed(self):
        assert references_policy_udf("SELECT agnes_hmac(((") == POLICY_HMAC_FUNCTION

    def test_the_name_set_is_what_callers_reserve(self):
        assert POLICY_HMAC_FUNCTION in POLICY_UDF_NAMES


class TestAnalyticsConnections:
    """Every connection that can execute a policy body carries the function.

    `get_analytics_db_readonly()` is the choke point for /api/query,
    /api/v2/sample's registered-view path, /api/mcp/query-table, the
    effective-access counts and the save-time `probe_policy`;
    `get_analytics_db()` is the read-write singleton src/db.py keeps for
    itself. Both are covered so a policy body never fails with "function
    does not exist" on a surface that should serve it.
    """

    def test_readonly_connection_has_the_function(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.db import get_analytics_db_readonly

        conn = get_analytics_db_readonly()
        try:
            assert conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")
        finally:
            conn.close()

    def test_readwrite_singleton_has_the_function(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.db import close_analytics_db, get_analytics_db

        conn = get_analytics_db()
        try:
            assert conn.execute(f"SELECT {POLICY_HMAC_FUNCTION}('x')").fetchone()[0] == _expected("x")
        finally:
            conn.close()
            close_analytics_db()
