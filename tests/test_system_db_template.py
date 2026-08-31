"""Guards for the seeded-schema `system.duckdb` template in `tests/conftest.py`.

Building the system database from nothing runs `_SYSTEM_SCHEMA` plus the whole
v1..vN ladder — ~150 ms warm locally, ~280 ms on a CI runner — and nearly every
test in the suite pays it, because `e2e_env` and its callers point `DATA_DIR` at
a fresh `tmp_path`. The harness builds that database once per pytest process and
byte-copies it thereafter.

The optimisation is only safe because of where it is hooked. An earlier version
sat on the `duckdb.connect` wrapper — the broader choke point — and seeded a
CURRENT-schema database underneath the tests that hand-build an OLD-schema one
to watch the ladder run. 36 tests failed with `Table with name "schema_version"
already exists`. The tests below pin the narrower contract that replaced it.
"""

from __future__ import annotations

import duckdb
import pytest

from tests import conftest as harness


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestTheTemplateOnlySeedsTheCreatePath:
    """The property whose absence broke 36 migration tests."""

    def test_an_existing_database_is_opened_untouched(self, data_dir):
        db_path = data_dir / "state" / "system.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.close()
        before = db_path.read_bytes()

        seeded = harness._system_db_template() is not None
        if not seeded:
            pytest.skip("template could not be built in this environment")

        # The hook must decline: the file exists, so nothing is copied over it.
        # Under the earlier connect-wrapper version, the `duckdb.connect` above
        # would already have seeded a current-schema database and the
        # `CREATE TABLE schema_version` would have raised.
        assert db_path.exists()
        opened = duckdb.connect(str(db_path))
        try:
            assert opened.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        finally:
            opened.close()
        assert db_path.read_bytes()[:16] == before[:16]

    def test_a_hand_built_v1_database_still_migrates(self, data_dir):
        """The exact shape tests/test_db.py uses — it must survive the harness."""
        db_path = data_dir / "state" / "system.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP)")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.close()

        from src.db import SCHEMA_VERSION, get_system_db

        migrated = get_system_db()
        try:
            version = migrated.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        finally:
            migrated.close()
        assert version == SCHEMA_VERSION


class TestASeededDatabaseIsIndistinguishable:
    def test_a_fresh_data_dir_lands_at_the_current_schema_version(self, data_dir):
        from src.db import SCHEMA_VERSION, SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP, get_system_db

        conn = get_system_db()
        try:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
            names = {row[0] for row in conn.execute("SELECT name FROM user_groups WHERE is_system").fetchall()}
        finally:
            conn.close()
        assert {SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP} <= names

    def test_two_fresh_data_dirs_do_not_share_state(self, tmp_path, monkeypatch):
        """The template carries schema, never a previous test's rows."""
        from src.db import get_system_db

        for i, marker in enumerate(("first", "second")):
            root = tmp_path / f"run{i}"
            (root / "state").mkdir(parents=True)
            monkeypatch.setenv("DATA_DIR", str(root))
            conn = get_system_db()
            try:
                seen = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                assert seen == 0, f"{marker} run started with {seen} leaked users"
                conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", [marker, f"{marker}@x.test"])
            finally:
                conn.close()


class TestTheTemplateIsOptional:
    def test_the_escape_hatch_exists(self):
        """`AGNES_TEST_DB_TEMPLATE=0` has to keep working — it is the bisect
        handle for a suspected staleness bug, and CI's fallback if this ever
        goes wrong."""
        assert isinstance(harness._TEMPLATE_ENABLED, bool)

    def test_a_failed_build_degrades_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(harness, "_template_state", "failed")
        monkeypatch.setattr(harness, "_template_path", None)
        assert harness._system_db_template() is None
