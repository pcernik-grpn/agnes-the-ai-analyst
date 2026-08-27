"""use_pg() precedence: instance.yaml::database.backend → env var."""

from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_use_pg_true_when_yaml_says_side_car(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")

    from src.repositories import use_pg

    assert use_pg() is True


def test_use_pg_false_when_yaml_says_duckdb(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.DUCKDB)

    from src.repositories import use_pg

    assert use_pg() is False


def test_use_pg_falls_back_to_env_when_yaml_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")

    from src.repositories import use_pg

    assert use_pg() is True


def test_use_pg_false_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")

    from src.repositories import use_pg

    assert use_pg() is False


def test_use_pg_survives_unrelated_overlay_write(tmp_path, monkeypatch):
    """Exact repro of the PG-backend-revert bug: DATABASE_URL is set (the
    instance is running Postgres purely via the env fallback — no explicit
    ``database:`` declaration was ever written) and an admin saves an
    unrelated /admin/server-config section (e.g. data_source). That save
    creates instance.yaml for the first time but writes only the edited
    section, with NO ``database`` key — it must NOT be interpreted as an
    implicit "backend: duckdb" declaration.

    Pre-fix, ``read_backend_state()`` defaulted the absent ``database`` key
    to duckdb, and ``use_pg()`` then short-circuited on `_OVERLAY_PATH.exists()`
    alone — flipping every repo factory to DuckDB and orphaning the Postgres
    data on the next restart.
    """
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")

    from src.repositories import use_pg

    # Before any overlay write: env fallback alone selects Postgres.
    assert use_pg() is True

    # Admin saves an unrelated server-config section: the overlay editor
    # writes only the touched section, never a `database` key.
    import yaml

    overlay.write_text(yaml.safe_dump({"data_source": {"type": "bigquery"}}))

    from src.db_state_machine import reset_backend_state_cache

    reset_backend_state_cache()  # simulate the "next restart" cold cache

    assert use_pg() is True, "unrelated overlay write must not revert a DATABASE_URL-based PG instance to DuckDB"

    # And again without a reset, to prove it's not merely a caching artifact.
    assert use_pg() is True


def test_use_pg_false_when_yaml_explicitly_declares_duckdb_even_with_database_url(tmp_path, monkeypatch):
    """Explicit ``database: {backend: duckdb}`` still wins over DATABASE_URL —
    an explicit declaration is not the same as an overlay that merely exists
    without a ``database`` key."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.DUCKDB)

    from src.repositories import use_pg

    assert use_pg() is False


def test_use_pg_true_when_yaml_says_side_car_even_with_unrelated_key_missing(tmp_path, monkeypatch):
    """Explicit ``database: {backend: side_car}`` (e.g. from
    scripts/db_state_migrator.py) still selects Postgres, sanity-checking
    that the explicit-declaration path is untouched by the fix."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")

    from src.repositories import use_pg

    assert use_pg() is True


def test_use_pg_parses_overlay_once_across_many_calls(tmp_path, monkeypatch):
    """read_backend_state memoizes the parsed overlay, so a burst
    of repo-factory calls (each of which runs use_pg) parses the YAML ONCE,
    not once per call. Pre-fix this was the ~2 req/s catalog throughput
    ceiling — dozens of yaml.safe_load per request holding the GIL."""
    import src.db_state_machine as sm
    from src.db_state_machine import BackendState, reset_backend_state_cache, write_backend_state

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    reset_backend_state_cache()  # simulate a fresh process (cold cache)

    parses = {"n": 0}
    real_safe_load = sm.yaml.safe_load

    def _counting_safe_load(*args, **kwargs):
        parses["n"] += 1
        return real_safe_load(*args, **kwargs)

    monkeypatch.setattr(sm.yaml, "safe_load", _counting_safe_load)

    from src.repositories import use_pg

    for _ in range(25):
        assert use_pg() is True

    assert parses["n"] == 1, f"overlay parsed {parses['n']}x across 25 use_pg() calls; expected 1"
