"""Tests for src/db_pg.py — engine/session factory + DeclarativeBase.

Mirrors the shape of src/db.py::get_system_db (lines 937-959): a process-
wide singleton engine guarded by a lock, lazy-initialized, reads the URL
from AGNES_DB_URL / DATABASE_URL. Disposing the engine is supported for
test isolation.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session


def test_module_exports_base_and_factories():
    """Public surface contract."""
    import src.db_pg as db_pg

    assert hasattr(db_pg, "Base"), "src.db_pg must expose `Base`"
    assert issubclass(db_pg.Base, DeclarativeBase), "Base must subclass DeclarativeBase"
    assert hasattr(db_pg, "get_engine"), "src.db_pg must expose `get_engine`"
    assert hasattr(db_pg, "get_session"), "src.db_pg must expose `get_session`"
    assert hasattr(db_pg, "dispose"), "src.db_pg must expose `dispose`"


def test_get_engine_returns_singleton(_pg_url, monkeypatch):
    """get_engine() returns the same Engine across calls.

    Matches the DuckDB singleton pattern in src/db.py (one process owns
    one connection pool, all repos share it).
    """
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    e1 = db_pg.get_engine()
    e2 = db_pg.get_engine()
    assert e1 is e2


def test_get_engine_reads_url_from_env(_pg_url, monkeypatch, tmp_path):
    """No URL → RuntimeError. With AGNES_DB_URL set → connects."""
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Also ensure no instance.yaml overlay leaks in from another test's
    # DATA_DIR — point at a guaranteed-missing path.
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    with pytest.raises(RuntimeError, match="Postgres URL is unset"):
        db_pg.get_engine()

    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    db_pg.dispose()
    eng = db_pg.get_engine()
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT 42")).scalar() == 42


def test_get_session_yields_session(_pg_url, monkeypatch):
    """get_session() is a context-manager that produces a Session."""
    import src.db_pg as db_pg

    db_pg.dispose()
    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    with db_pg.get_session() as session:
        assert isinstance(session, Session)
        assert session.execute(sa.text("SELECT 7")).scalar() == 7


def test_dispose_clears_singleton(_pg_url, monkeypatch):
    """Calling dispose() drops the engine; next get_engine() builds a fresh one."""
    import src.db_pg as db_pg

    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    db_pg.dispose()
    e1 = db_pg.get_engine()
    db_pg.dispose()
    e2 = db_pg.get_engine()
    assert e1 is not e2


def test_database_url_is_primary_agnes_db_url_aliased_with_warning(monkeypatch, caplog):
    """DATABASE_URL is the primary; AGNES_DB_URL still works but logs a deprecation warning."""
    import logging
    from src import db_pg

    db_pg.dispose()  # clear singleton

    # 1. DATABASE_URL alone: no warning.
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    with caplog.at_level(logging.WARNING, logger="src.db_pg"):
        assert db_pg._resolve_url() == "postgresql+psycopg://x:y@localhost/z"
    assert "AGNES_DB_URL" not in caplog.text

    # 2. AGNES_DB_URL alone: works, logs deprecation.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AGNES_DB_URL", "postgresql+psycopg://a:b@localhost/c")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.db_pg"):
        assert db_pg._resolve_url() == "postgresql+psycopg://a:b@localhost/c"
    assert "AGNES_DB_URL is deprecated" in caplog.text

    # 3. Both set: DATABASE_URL wins, AGNES_DB_URL ignored, no warning.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("AGNES_DB_URL", "postgresql+psycopg://a:b@localhost/c")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.db_pg"):
        assert db_pg._resolve_url() == "postgresql+psycopg://x:y@localhost/z"
    assert "AGNES_DB_URL" not in caplog.text


# ---------------------------------------------------------------------------
# Connection-pool sizing (AGNES_PG_POOL_SIZE / AGNES_PG_MAX_OVERFLOW /
# AGNES_PG_POOL_TIMEOUT_S) + the extraction-worker-role pool_size default.
# Live finding (2026-09): a worker replica running 2 facts-extraction passes
# (32 threads each) alongside 4 SharePoint crawls in ONE process, sharing
# ONE engine, exhausted the hardcoded 5+10 pool 30x/10min.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    for name in (
        "AGNES_PG_POOL_SIZE",
        "AGNES_PG_MAX_OVERFLOW",
        "AGNES_PG_POOL_TIMEOUT_S",
        "AGNES_WORKER_LANES",
        "AGNES_EXTRACTION_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestPoolSettings:
    def test_defaults_unchanged_when_nothing_set(self):
        """Byte-for-byte the pre-existing hardcoded values (5 / 10 / 30s) —
        an instance that never touches any of these knobs is unaffected."""
        from src.db_pg import _resolve_pool_settings

        assert _resolve_pool_settings() == (5, 10, 30)

    def test_pool_size_env_override(self, monkeypatch):
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_PG_POOL_SIZE", "20")
        pool_size, max_overflow, pool_timeout = _resolve_pool_settings()
        assert pool_size == 20
        assert (max_overflow, pool_timeout) == (10, 30)

    def test_max_overflow_env_override(self, monkeypatch):
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_PG_MAX_OVERFLOW", "40")
        assert _resolve_pool_settings() == (5, 40, 30)

    def test_pool_timeout_env_override(self, monkeypatch):
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_PG_POOL_TIMEOUT_S", "5")
        assert _resolve_pool_settings() == (5, 10, 5)

    def test_invalid_env_values_fall_back_to_defaults(self, monkeypatch):
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_PG_POOL_SIZE", "not-a-number")
        monkeypatch.setenv("AGNES_PG_MAX_OVERFLOW", "also-bad")
        monkeypatch.setenv("AGNES_PG_POOL_TIMEOUT_S", "nope")
        assert _resolve_pool_settings() == (5, 10, 30)

    def test_non_extraction_process_keeps_plain_default_pool_size(self, monkeypatch):
        """A process not running the extraction lane (api, gateway, or a
        single-process all-in-one deployment) is unaffected by the
        worker-role default — matches every deployment before this knob
        existed."""
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_WORKER_LANES", "heavy,light")
        assert _resolve_pool_settings()[0] == 5

    def test_extraction_lane_pool_size_defaults_to_concurrency_sum(self, monkeypatch):
        """AGNES_WORKER_LANES includes `extraction` and AGNES_PG_POOL_SIZE is
        unset: pool_size defaults to extraction.concurrency +
        extraction.facts.concurrency, reading the SAME knobs the worker
        itself reads."""
        import app.instance_config as instance_config
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")
        monkeypatch.setenv("AGNES_EXTRACTION_CONCURRENCY", "5")
        monkeypatch.setattr(
            instance_config,
            "get_value",
            lambda *keys, default=None: 10 if keys == ("extraction", "facts", "concurrency") else default,
        )
        pool_size, max_overflow, pool_timeout = _resolve_pool_settings()
        assert pool_size == 15
        assert (max_overflow, pool_timeout) == (10, 30)

    def test_extraction_lane_pool_size_reads_yaml_when_env_concurrency_unset(self, monkeypatch):
        import app.instance_config as instance_config
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")

        def _get_value(*keys, default=None):
            if keys == ("extraction", "concurrency"):
                return 4
            if keys == ("extraction", "facts", "concurrency"):
                return 3
            return default

        monkeypatch.setattr(instance_config, "get_value", _get_value)
        assert _resolve_pool_settings()[0] == 7

    def test_extraction_lane_pool_size_capped_at_64(self, monkeypatch):
        import app.instance_config as instance_config
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")
        monkeypatch.setenv("AGNES_EXTRACTION_CONCURRENCY", "24")
        monkeypatch.setattr(
            instance_config,
            "get_value",
            lambda *keys, default=None: 64 if keys == ("extraction", "facts", "concurrency") else default,
        )
        # 24 + 64 = 88, capped to 64.
        assert _resolve_pool_settings()[0] == 64

    def test_explicit_pool_size_env_wins_over_extraction_lane_default(self, monkeypatch):
        """An operator's explicit AGNES_PG_POOL_SIZE always wins, even on
        an extraction-lane process — the auto-sizing is only a DEFAULT."""
        import app.instance_config as instance_config
        from src.db_pg import _resolve_pool_settings

        monkeypatch.setenv("AGNES_WORKER_LANES", "extraction")
        monkeypatch.setenv("AGNES_PG_POOL_SIZE", "9")
        monkeypatch.setattr(
            instance_config,
            "get_value",
            lambda *keys, default=None: 64 if keys == ("extraction", "facts", "concurrency") else default,
        )
        assert _resolve_pool_settings()[0] == 9

    def test_get_engine_logs_resolved_pool_settings_once(self, _pg_url, monkeypatch, caplog):
        import logging

        import src.db_pg as db_pg

        db_pg.dispose()
        monkeypatch.setenv("AGNES_DB_URL", _pg_url)
        monkeypatch.setenv("AGNES_PG_POOL_SIZE", "17")
        with caplog.at_level(logging.INFO, logger="src.db_pg"):
            db_pg.get_engine()
        assert "pool_size=17" in caplog.text
        db_pg.dispose()
