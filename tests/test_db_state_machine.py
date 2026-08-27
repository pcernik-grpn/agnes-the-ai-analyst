"""Unit tests for DB backend state machine."""

from __future__ import annotations
import pytest
from src.db_state_machine import (
    BackendNotYetSupportedError,
    BackendState,
    InvalidTransitionError,
    MigrationInProgressError,
    MigrationLock,
    allowed_transitions,
    read_backend_state,
    validate_transition,
    write_backend_state,
)


def test_backend_state_values():
    """Four stable backends + three in-progress variants."""
    assert BackendState.DUCKDB.value == "duckdb"
    assert BackendState.SIDE_CAR.value == "side_car"
    assert BackendState.CLOUD.value == "cloud"
    assert BackendState.DUCKDB_QUACK.value == "duckdb_quack"
    assert BackendState.SIDE_CAR_IN_PROGRESS.value == "side_car_in_progress"
    assert BackendState.CLOUD_IN_PROGRESS.value == "cloud_in_progress"
    assert BackendState.DUCKDB_QUACK_IN_PROGRESS.value == "duckdb_quack_in_progress"


def test_allowed_transitions_matrix_is_multi_destination():
    """Every stable backend can migrate to every other stable backend.

    Multi-destination shape (not forward-only): operators move between
    backends as cost / HA / compliance needs shift. DuckDB is NOT
    "start-only" — reverse migrations from PG back to DuckDB are
    supported by ``copy_pg_to_duckdb`` (UPSERT semantics).
    """
    # DUCKDB can target all three other stable backends.
    assert set(allowed_transitions(BackendState.DUCKDB)) == {
        BackendState.SIDE_CAR,
        BackendState.CLOUD,
        BackendState.DUCKDB_QUACK,
    }
    # SIDE_CAR can target all three others — including reverse to DUCKDB.
    assert set(allowed_transitions(BackendState.SIDE_CAR)) == {
        BackendState.DUCKDB,
        BackendState.CLOUD,
        BackendState.DUCKDB_QUACK,
    }
    # CLOUD can target all three others — including reverse to DUCKDB.
    assert set(allowed_transitions(BackendState.CLOUD)) == {
        BackendState.DUCKDB,
        BackendState.SIDE_CAR,
        BackendState.DUCKDB_QUACK,
    }
    # DUCKDB_QUACK can target all three others (when runtime support lands).
    assert set(allowed_transitions(BackendState.DUCKDB_QUACK)) == {
        BackendState.DUCKDB,
        BackendState.SIDE_CAR,
        BackendState.CLOUD,
    }


def test_allowed_transitions_from_transient():
    """In-progress states allow ONLY the next stable state (retry)."""
    assert allowed_transitions(BackendState.SIDE_CAR_IN_PROGRESS) == [BackendState.SIDE_CAR]
    assert allowed_transitions(BackendState.CLOUD_IN_PROGRESS) == [BackendState.CLOUD]
    assert allowed_transitions(BackendState.DUCKDB_QUACK_IN_PROGRESS) == [BackendState.DUCKDB_QUACK]


def test_validate_transition_ok():
    """Valid forward transitions return None."""
    validate_transition(BackendState.DUCKDB, BackendState.SIDE_CAR)
    validate_transition(BackendState.DUCKDB, BackendState.CLOUD)
    validate_transition(BackendState.SIDE_CAR, BackendState.CLOUD)
    validate_transition(BackendState.CLOUD, BackendState.SIDE_CAR)


def test_validate_transition_allows_reverse_to_duckdb():
    """Reverse migrations from PG back to DuckDB are now supported.

    The migrator dispatches to ``copy_pg_to_duckdb`` for these paths
    (DuckDB UPSERT for idempotent replay). Use cases: cost reduction
    (drop the side-car after pilot), compliance re-evaluation,
    development snapshot of a production PG.
    """
    # Both SIDE_CAR → DUCKDB and CLOUD → DUCKDB now legal.
    validate_transition(BackendState.SIDE_CAR, BackendState.DUCKDB)
    validate_transition(BackendState.CLOUD, BackendState.DUCKDB)


def test_validate_transition_rejects_duckdb_quack_until_runtime_supported():
    """DuckDB Quack is reserved in the API but not yet runtime-
    implemented. Transitions TO Quack raise BackendNotYetSupportedError
    (a NotImplementedError subclass) until DuckDB 2.0 lands."""
    with pytest.raises(BackendNotYetSupportedError, match="DUCKDB_QUACK|duckdb_quack"):
        validate_transition(BackendState.DUCKDB, BackendState.DUCKDB_QUACK)
    with pytest.raises(BackendNotYetSupportedError):
        validate_transition(BackendState.SIDE_CAR, BackendState.DUCKDB_QUACK)
    with pytest.raises(BackendNotYetSupportedError):
        validate_transition(BackendState.CLOUD, BackendState.DUCKDB_QUACK)


def test_backend_not_yet_supported_is_notimplementederror():
    """BackendNotYetSupportedError is a NotImplementedError subclass so
    callers catching NotImplementedError can route placeholder targets
    cleanly."""
    assert issubclass(BackendNotYetSupportedError, NotImplementedError)


def test_validate_transition_rejects_self():
    """No-op transition (state → same state) raises."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.SIDE_CAR, BackendState.SIDE_CAR)
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.DUCKDB, BackendState.DUCKDB)
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.CLOUD, BackendState.CLOUD)


def test_write_then_read_backend_state(tmp_path, monkeypatch):
    """Round-trip: write side_car + URL, read same values."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(
        BackendState.SIDE_CAR,
        url="postgresql+psycopg://agnes:pw@postgres:5432/agnes",
    )
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == "postgresql+psycopg://agnes:pw@postgres:5432/agnes"


def test_read_returns_duckdb_when_overlay_absent(tmp_path, monkeypatch):
    """Fresh install defaults to duckdb."""
    overlay = tmp_path / "nonexistent.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    state, url = read_backend_state()
    assert state == BackendState.DUCKDB
    assert url is None


def test_is_backend_explicitly_declared_false_when_overlay_absent(tmp_path, monkeypatch):
    from src.db_state_machine import is_backend_explicitly_declared

    overlay = tmp_path / "nonexistent.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    assert is_backend_explicitly_declared() is False


def test_is_backend_explicitly_declared_false_when_overlay_exists_without_database_key(tmp_path, monkeypatch):
    """The PG-backend-revert conflation: an overlay that exists (e.g.
    written by an unrelated /admin/server-config save) but never touches
    the `database` section must NOT be reported as an explicit declaration
    — even though ``read_backend_state()`` still (correctly, for its own
    documented zero-config-fallback contract) resolves to DUCKDB."""
    import yaml as _yaml

    from src.db_state_machine import is_backend_explicitly_declared

    overlay = tmp_path / "instance.yaml"
    overlay.write_text(_yaml.safe_dump({"data_source": {"type": "bigquery"}}))
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    state, _url = read_backend_state()
    assert state == BackendState.DUCKDB  # unchanged zero-config-fallback contract
    assert is_backend_explicitly_declared() is False  # but NOT a declaration


def test_is_backend_explicitly_declared_true_for_explicit_duckdb(tmp_path, monkeypatch):
    from src.db_state_machine import is_backend_explicitly_declared, write_backend_state

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(BackendState.DUCKDB)
    assert is_backend_explicitly_declared() is True


def test_is_backend_explicitly_declared_true_for_side_car(tmp_path, monkeypatch):
    from src.db_state_machine import is_backend_explicitly_declared, write_backend_state

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    assert is_backend_explicitly_declared() is True


def test_write_is_atomic(tmp_path, monkeypatch):
    """Writes go through .tmp + os.replace; no .tmp left behind on success."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    assert overlay.exists()
    assert not (tmp_path / "instance.yaml.tmp").exists()


def test_lock_acquire_release(tmp_path, monkeypatch):
    """flock acquired and released cleanly."""
    lock_path = tmp_path / "db-migration.lock"
    monkeypatch.setattr("src.db_state_machine._LOCK_PATH", lock_path)

    with MigrationLock() as lock:
        assert lock.held
    assert lock_path.exists()  # file remains; lock released


def test_second_acquire_raises(tmp_path, monkeypatch):
    """Concurrent acquisition raises MigrationInProgressError."""
    lock_path = tmp_path / "db-migration.lock"
    monkeypatch.setattr("src.db_state_machine._LOCK_PATH", lock_path)

    with MigrationLock():
        with pytest.raises(MigrationInProgressError):
            with MigrationLock():
                pass


def test_get_database_config_reads_from_state_module(tmp_path, monkeypatch):
    """get_database_config delegates to state machine read."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import write_backend_state

    write_backend_state(BackendState.CLOUD, url="postgresql://cloud/agnes")

    from app.instance_config import get_database_config, reset_database_cache

    reset_database_cache()
    config = get_database_config()
    assert config["backend"] == "cloud"
    assert config["url"] == "postgresql://cloud/agnes"


def test_write_backend_state_preserves_url_when_url_kw_absent(tmp_path, monkeypatch):
    """When ``write_backend_state`` is called with no ``url`` argument
    it must KEEP the existing url key in instance.yaml. Previously it
    omitted url from the output → yaml.safe_dump erased the key →
    repository routing saw backend=*_in_progress (treated as PG) with
    no URL → 30s+ window where every authenticated request crashed
    with ``Postgres URL is unset``."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)  # no url= kwarg

    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR_IN_PROGRESS
    assert url == "postgresql+psycopg://x:y@h/d"


def test_write_backend_state_clears_url_when_url_none_explicit(tmp_path, monkeypatch):
    """Explicit url=None means CLEAR. Used for transitioning to a
    stateless backend like DuckDB."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.DUCKDB, url=None)
    state, url = read_backend_state()
    assert state == BackendState.DUCKDB
    assert url is None


def test_write_backend_state_preserves_other_top_level_keys(tmp_path, monkeypatch):
    """Non-database keys (logging, auth, feature flags) the operator
    may have set via /admin/server-config must NOT be lost when
    write_backend_state mutates the database subkey."""
    import yaml

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    # Pre-seed the overlay with operator-set keys.
    overlay.write_text(
        yaml.safe_dump(
            {
                "logging": {"level": "debug"},
                "auth": {"providers": ["google", "magic_link"]},
                "database": {"backend": "duckdb"},
            }
        )
    )
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    data = yaml.safe_load(overlay.read_text())
    assert data["logging"]["level"] == "debug"
    assert data["auth"]["providers"] == ["google", "magic_link"]
    assert data["database"]["backend"] == "side_car"
    assert data["database"]["url"] == "postgresql+psycopg://x:y@h/d"


def test_read_backend_state_is_cached_until_reset(tmp_path, monkeypatch):
    """read_backend_state is memoized for the process lifetime.

    An out-of-process overlay rewrite (e.g. the migrator subprocess) is NOT
    observed until the cache is reset — which in production happens because
    a backend flip restarts the app. reset_backend_state_cache() is the
    explicit hook that models that boundary.
    """
    import yaml as _yaml

    from src.db_state_machine import (
        BackendState,
        read_backend_state,
        reset_backend_state_cache,
    )

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    overlay.write_text(_yaml.safe_dump({"database": {"backend": "duckdb"}}))
    reset_backend_state_cache()
    assert read_backend_state()[0] == BackendState.DUCKDB  # parses + caches

    # Rewrite the file directly (bypassing write_backend_state so no
    # auto-invalidation) — the cached value must still be served.
    overlay.write_text(_yaml.safe_dump({"database": {"backend": "side_car", "url": "postgresql://x"}}))
    assert read_backend_state()[0] == BackendState.DUCKDB  # stale, still cached

    reset_backend_state_cache()  # simulate the app-restart boundary
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == "postgresql://x"


def test_write_backend_state_invalidates_cache(tmp_path, monkeypatch):
    """A same-process write must be visible to the next read without an
    explicit reset — write_backend_state invalidates the parse-once cache."""
    from src.db_state_machine import (
        BackendState,
        read_backend_state,
        write_backend_state,
    )

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(BackendState.DUCKDB)
    assert read_backend_state()[0] == BackendState.DUCKDB  # caches duckdb

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    assert read_backend_state()[0] == BackendState.SIDE_CAR  # not the stale cache


def test_reset_database_cache_clears_stale_backend_state_cache(tmp_path, monkeypatch):
    """The public app.instance_config.reset_database_cache() wrapper must
    actually drop the backend-state cache — tests the wrapper's observable
    behavior directly (rewriting the overlay on disk, not via
    write_backend_state which already invalidates), so the suite would catch
    the wrapper regressing back to a no-op."""
    import yaml as _yaml

    from app.instance_config import get_database_config, reset_database_cache

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    overlay.write_text(_yaml.safe_dump({"database": {"backend": "duckdb"}}))
    assert get_database_config()["backend"] == "duckdb"  # caches duckdb

    overlay.write_text(_yaml.safe_dump({"database": {"backend": "side_car", "url": "postgresql://x"}}))
    reset_database_cache()
    assert get_database_config() == {"backend": "side_car", "url": "postgresql://x"}


def test_malformed_overlay_fallback_is_not_cached(tmp_path, monkeypatch, caplog):
    """The yaml.YAMLError branch deliberately does NOT cache its safe
    fallback, so an operator who repairs a malformed overlay while the
    process is still alive is observed on the very next read (no restart /
    explicit reset needed)."""
    import logging

    import yaml as _yaml

    from src.db_state_machine import BackendState, read_backend_state, reset_backend_state_cache

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    overlay.write_text("database: [")  # malformed
    reset_backend_state_cache()

    with caplog.at_level(logging.WARNING, logger="src.db_state_machine"):
        assert read_backend_state() == (BackendState.DUCKDB, None)
    assert "YAML parse failed" in caplog.text

    # Repair the overlay directly (no reset) — the uncached fallback means
    # the next read picks up the repaired backend.
    overlay.write_text(_yaml.safe_dump({"database": {"backend": "side_car", "url": "postgresql://x"}}))
    assert read_backend_state() == (BackendState.SIDE_CAR, "postgresql://x")


def test_write_backend_state_sets_0600(tmp_path, monkeypatch):
    """H2 — instance.yaml carries plaintext PG credentials and must
    be owner-readable only. Catches the case where a non-app user on
    the same host could ``cat`` the overlay and exfiltrate the URL."""
    import os
    import stat

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://agnes:pw@host/agnes")
    mode = stat.S_IMODE(os.stat(overlay).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
