"""Alembic 0071 (DuckDB v123) — sync_state / sync_history id backfill (B1).

Mirrors ``tests/test_sync_state_key.py``'s DuckDB ``_v123_to_v124`` coverage
for the Postgres ladder: a name-keyed ``sync_state``/``sync_history`` row
with a matching ``table_registry`` row is rewritten to the registry id; an
orphan (no registry match) is left unchanged; a row that would collide with
one already sitting under the target id is left unchanged and logged rather
than raising on the ``sync_state.table_id`` primary-key constraint.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0070_builder_scope_v122"
_THIS = "0072_sync_state_id_v124"


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _insert_registry_row(conn, *, id, name) -> None:
    conn.execute(
        sa.text("INSERT INTO table_registry (id, name, registered_at) VALUES (:id, :name, now())"),
        {"id": id, "name": name},
    )


def _insert_sync_state(conn, *, table_id, status="ok", rows=1, hash_="h") -> None:
    conn.execute(
        sa.text("INSERT INTO sync_state (table_id, rows, hash, status) VALUES (:tid, :rows, :hash, :status)"),
        {"tid": table_id, "rows": rows, "hash": hash_, "status": status},
    )


def _insert_sync_history(conn, *, id, table_id) -> None:
    conn.execute(
        sa.text("INSERT INTO sync_history (id, table_id, synced_at, status) VALUES (:id, :tid, now(), 'ok')"),
        {"id": id, "tid": table_id},
    )


def _sync_state_ids(conn) -> set:
    return {row[0] for row in conn.execute(sa.text("SELECT table_id FROM sync_state")).fetchall()}


def _sync_history_ids(conn) -> set:
    return {row[0] for row in conn.execute(sa.text("SELECT table_id FROM sync_history")).fetchall()}


def test_0071_rewrites_name_keyed_row_to_registry_id(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_registry_row(conn, id="web_sessions", name="Web Sessions")
        _insert_sync_state(conn, table_id="Web Sessions")
        _insert_sync_history(conn, id="hist-1", table_id="Web Sessions")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _sync_state_ids(conn) == {"web_sessions"}
        assert _sync_history_ids(conn) == {"web_sessions"}
        row = conn.execute(sa.text("SELECT rows, hash FROM sync_state WHERE table_id = 'web_sessions'")).one()
        assert row == (1, "h")


def test_0071_leaves_orphan_row_unchanged(pg_engine):
    """A sync_state row with no matching table_registry.name is left
    exactly as-is — never dropped."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_sync_state(conn, table_id="Ghost Table")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _sync_state_ids(conn) == {"Ghost Table"}


def test_0071_skips_and_logs_a_collision_with_an_existing_row(pg_engine, capfd):
    """A pathological pre-existing state: a row already sits under the
    target id. The name-keyed row must be left alone (not raise on the
    sync_state.table_id primary-key collision) and the skip must be
    logged so a PG operator can notice an un-migrated row.

    ``capfd`` (not ``caplog``): Alembic's ``command.upgrade`` re-runs
    ``logging.config.fileConfig(alembic.ini)`` on every invocation, which
    tears out pytest's caplog root handler and installs Alembic's own
    ``StreamHandler`` — this migration's warning reaches the process's
    real stderr, not the logger tree caplog attaches to.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)
    capfd.readouterr()  # discard the setup-phase migration log noise

    with pg_engine.begin() as conn:
        _insert_registry_row(conn, id="orders", name="Orders")
        _insert_sync_state(conn, table_id="Orders", hash_="h1")
        _insert_sync_state(conn, table_id="orders", hash_="h2")

    command.upgrade(cfg, _THIS)  # must not raise

    with pg_engine.connect() as conn:
        assert _sync_state_ids(conn) == {"Orders", "orders"}
    captured = capfd.readouterr()
    log_output = captured.out + captured.err
    assert "leaving" in log_output and "Orders" in log_output and "orders" in log_output


def test_0071_is_idempotent(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_registry_row(conn, id="orders_daily", name="Orders Daily")
        _insert_sync_state(conn, table_id="Orders Daily")

    command.upgrade(cfg, _THIS)
    command.upgrade(cfg, _THIS)  # re-run must not raise or change anything

    with pg_engine.connect() as conn:
        assert _sync_state_ids(conn) == {"orders_daily"}


def test_0071_noops_gracefully_when_sync_state_is_absent(pg_engine):
    """Alembic can run before the app's first boot — a missing table must
    not raise (mirrors 0070's guard)."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS sync_history CASCADE"))
        conn.execute(sa.text("DROP TABLE IF EXISTS sync_state CASCADE"))

    command.upgrade(cfg, _THIS)  # must not raise


def test_0071_downgrade_is_a_documented_noop(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_registry_row(conn, id="orders", name="Orders")
        _insert_sync_state(conn, table_id="Orders")

    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)  # must not raise, and must not touch data

    with pg_engine.connect() as conn:
        assert _sync_state_ids(conn) == {"orders"}
