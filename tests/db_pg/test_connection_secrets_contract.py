"""Cross-engine contract tests for connection_secrets (vault scope)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from app.secrets_vault import ConnectionSecretsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return ConnectionSecretsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.secrets_vault_pg import ConnectionSecretsPgRepository

    return ConnectionSecretsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_upsert_get_has_delete_roundtrip(repo):
    assert repo.has("c1") is False
    assert repo.get("c1") is None
    repo.upsert("c1", "tok-secret-1")
    assert repo.has("c1") is True
    assert repo.get("c1") == "tok-secret-1"
    repo.upsert("c1", "tok-secret-2")  # rotate
    assert repo.get("c1") == "tok-secret-2"
    repo.delete("c1")
    assert repo.has("c1") is False


def test_get_returns_none_on_decrypt_failure(repo, monkeypatch, caplog):
    """Vault key rotated → ``get()`` reads as absent (never raises) on either
    backend, and the WARNING names the column. Both repos read through
    ``app.secrets_vault.decrypt_optional``; the ``ciphertext`` column is TEXT,
    so the repos encode before handing the token over — a regression there
    would silently turn every readable secret into ``None``, which this test
    catches via the positive round-trip above plus the log assertion here."""
    import logging

    from cryptography.fernet import Fernet

    repo.upsert("c-rot", "encrypted-under-key-a")
    assert repo.get("c-rot") == "encrypted-under-key-a"

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.secrets_vault"):
        assert repo.get("c-rot") is None
    # Row survives — only unreadable.
    assert repo.has("c-rot") is True
    assert "connection_secrets.ciphertext[c-rot]" in caplog.text, (
        f"decrypt warning does not identify the column: {caplog.text!r}"
    )


def test_updated_at_is_none_when_absent(repo):
    """The source card's certificate row (spec §13.2) shows a vault-provided
    credential's set-date without ever reading the value — ``updated_at()``
    exists for exactly that (see ``connectors/sharepoint/settings.py``'s
    ``credential_set_at``). No row yet -> ``None``, never an exception."""
    assert repo.updated_at("c-never-set") is None


def test_updated_at_round_trips_and_advances_on_rotate(repo):
    repo.upsert("c1", "tok-secret-1")
    first = repo.updated_at("c1")
    assert first is not None

    repo.upsert("c1", "tok-secret-2")
    second = repo.updated_at("c1")
    assert second is not None
    assert second >= first
