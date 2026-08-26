"""Cross-engine contract for the system_secrets vault (Slack bot tokens).

This repo lives in app/secrets_vault.py (DuckDB) + src/repositories/
secrets_vault_pg.py (PG), so the automatic method-parity sweep
(tests/db_pg/test_repo_method_parity.py, which only scans
src/repositories/*.py) does NOT cover it. This test is the sole mechanical
guard against DuckDB/PG drift — keep it in lockstep with the repo methods.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def test_system_secret_round_trip_both_backends(_env):
    from src.repositories import system_secrets_repo

    repo = system_secrets_repo()
    repo.upsert("SLACK_BOT_TOKEN", "xoxb-original")
    assert repo.has("SLACK_BOT_TOKEN") is True
    assert repo.get("SLACK_BOT_TOKEN") == "xoxb-original"

    # rotate
    repo.upsert("SLACK_BOT_TOKEN", "xoxb-rotated")
    assert repo.get("SLACK_BOT_TOKEN") == "xoxb-rotated"

    repo.delete("SLACK_BOT_TOKEN")
    assert repo.has("SLACK_BOT_TOKEN") is False
    assert repo.get("SLACK_BOT_TOKEN") is None


def test_system_secret_absent_returns_none_both_backends(_env):
    from src.repositories import system_secrets_repo

    assert system_secrets_repo().get("SLACK_APP_TOKEN") is None
    assert system_secrets_repo().has("SLACK_APP_TOKEN") is False


def test_list_names_with_prefix_both_backends(_env):
    """Powers app.secrets.reapply_all_overlay_tokens_from_vault (wave 2C
    task 6) — the ``env_overlay/`` namespace inside this same table."""
    from src.repositories import system_secrets_repo

    repo = system_secrets_repo()
    repo.upsert("SLACK_BOT_TOKEN", "xoxb-unrelated")
    repo.upsert("env_overlay/ANTHROPIC_API_KEY", "sk-anthropic")
    repo.upsert("env_overlay/SENDGRID_API_KEY", "sg-key")

    names = repo.list_names_with_prefix("env_overlay/")
    assert names == ["env_overlay/ANTHROPIC_API_KEY", "env_overlay/SENDGRID_API_KEY"]

    assert repo.list_names_with_prefix("env_overlay/") == sorted(repo.list_names_with_prefix("env_overlay/"))
    assert repo.list_names_with_prefix("does-not-exist/") == []


def test_malformed_key_fails_closed_both_backends(_env, monkeypatch):
    """A set-but-malformed AGNES_VAULT_KEY must make get() return None
    (fail closed), not raise — the (InvalidToken, RuntimeError) catch.
    has() still reports presence (it doesn't decrypt)."""
    from src.repositories import system_secrets_repo

    repo = system_secrets_repo()
    repo.upsert("SLACK_SIGNING_SECRET", "shh-valid")
    assert repo.get("SLACK_SIGNING_SECRET") == "shh-valid"

    # Corrupt the vault key: _get_fernet() now raises RuntimeError.
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")
    assert repo.get("SLACK_SIGNING_SECRET") is None  # swallowed → fail closed
    assert repo.has("SLACK_SIGNING_SECRET") is True  # presence unaffected
