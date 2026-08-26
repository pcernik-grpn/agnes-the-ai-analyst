"""Wave 2C task 6 — ``.env_overlay`` tokens via the control-plane vault +
cross-process reload.

Covers ``app.secrets.persist_overlay_token``'s vault-mode write path (row in
``system_secrets`` under the ``env_overlay/`` namespace + ``os.environ``
update + ``env-overlay-changed`` publish), the cross-process reload handler
(``app.main._on_env_overlay_changed`` / ``app.secrets.
reapply_overlay_token_from_vault``), the keyless/S-tier legacy file
fallback (with its one-time warning), the belt-and-braces periodic sweep
(``reapply_all_overlay_tokens_from_vault``), and vault > file precedence.

Existing coverage for the *file* path itself
(``tests/test_initial_workspace_api.py``,
``tests/test_env_overlay_boot.py``) is untouched — those tests never set
``AGNES_VAULT_KEY``, so ``persist_overlay_token`` keeps routing to the file
branch exactly as before.
"""

from __future__ import annotations

import logging
import os

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _reset_ephemeral():
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated STATE_DIR + a real (fresh) system.duckdb, keyless by default."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)

    import src.db as db

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None
    db.get_system_db()

    # Reset the module-level one-time-warning guard so each test observes
    # its own warning behavior independent of test order.
    import app.secrets as secrets_mod

    secrets_mod._warned_vault_unusable = False

    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()

    yield {"data_dir": data_dir}

    reset_coordination_for_tests()
    db.close_system_db()


def _set_vault_key(monkeypatch) -> None:
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


# ---------------------------------------------------------------------------
# Vault-mode write path
# ---------------------------------------------------------------------------


def test_vault_mode_write_creates_row_and_updates_env(env, monkeypatch):
    _set_vault_key(monkeypatch)
    monkeypatch.delenv("AGNES_TEST_TOKEN", raising=False)

    from app.secrets import persist_overlay_token
    from src.repositories import system_secrets_repo

    persist_overlay_token("AGNES_TEST_TOKEN", "shh-secret")

    assert os.environ["AGNES_TEST_TOKEN"] == "shh-secret"

    repo = system_secrets_repo()
    assert repo.has("env_overlay/AGNES_TEST_TOKEN") is True
    assert repo.get("env_overlay/AGNES_TEST_TOKEN") == "shh-secret"

    # The legacy file must NOT be touched while in vault mode.
    overlay_path = env["data_dir"] / "state" / ".env_overlay"
    assert not overlay_path.exists() or "AGNES_TEST_TOKEN" not in overlay_path.read_text()


def test_vault_mode_publishes_env_overlay_changed(env, monkeypatch):
    _set_vault_key(monkeypatch)

    from app.coordination.factory import coordination
    from app.secrets import persist_overlay_token

    received: list[str] = []
    coordination().subscribe("env-overlay-changed", received.append)

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")

    assert received == ["AGNES_TEST_TOKEN"]


def test_vault_mode_clear_deletes_row_and_env(env, monkeypatch):
    _set_vault_key(monkeypatch)

    from app.secrets import persist_overlay_token
    from src.repositories import system_secrets_repo

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")
    persist_overlay_token("AGNES_TEST_TOKEN", None)

    assert "AGNES_TEST_TOKEN" not in os.environ
    assert system_secrets_repo().has("env_overlay/AGNES_TEST_TOKEN") is False


def test_vault_mode_clear_empty_string_also_deletes(env, monkeypatch):
    _set_vault_key(monkeypatch)

    from app.secrets import persist_overlay_token
    from src.repositories import system_secrets_repo

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")
    persist_overlay_token("AGNES_TEST_TOKEN", "")

    assert "AGNES_TEST_TOKEN" not in os.environ
    assert system_secrets_repo().has("env_overlay/AGNES_TEST_TOKEN") is False


# ---------------------------------------------------------------------------
# Cross-process reload — subscriber handler re-applies from the vault
# ---------------------------------------------------------------------------


def test_handler_reapplies_token_simulating_another_process(env, monkeypatch):
    """Simulate two replicas sharing the vault + coordination backend:
    process A persists (updates its own env + publishes); process B never
    called persist_overlay_token, so its os.environ only picks up the new
    value once its ``env-overlay-changed`` subscriber handler fires — this
    is exactly what ``app.main``'s lifespan subscribe wires up in
    production."""
    _set_vault_key(monkeypatch)
    monkeypatch.delenv("AGNES_TEST_TOKEN", raising=False)

    from app.main import _on_env_overlay_changed
    from app.secrets import persist_overlay_token

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")

    # Simulate process B not yet having seen the write.
    del os.environ["AGNES_TEST_TOKEN"]
    assert "AGNES_TEST_TOKEN" not in os.environ

    _on_env_overlay_changed("AGNES_TEST_TOKEN")

    assert os.environ["AGNES_TEST_TOKEN"] == "v1"


def test_handler_logs_and_continues_on_lookup_failure(env, monkeypatch, caplog):
    """A vault lookup failure inside the handler must not raise — it should
    be logged and swallowed so the coordination backend's dispatch loop
    (and, on the memory backend, every other subscriber on the same
    publish) keeps running."""
    _set_vault_key(monkeypatch)

    from app.main import _on_env_overlay_changed

    monkeypatch.setattr(
        "app.secrets.reapply_overlay_token_from_vault",
        lambda name: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    caplog.set_level(logging.ERROR, logger="app.main")
    _on_env_overlay_changed("AGNES_TEST_TOKEN")  # must not raise

    assert any("env-overlay-changed handler failed" in r.message for r in caplog.records)


def test_reapply_overlay_token_from_vault_removes_stale_env_on_missing_row(env, monkeypatch):
    _set_vault_key(monkeypatch)
    os.environ["AGNES_TEST_TOKEN"] = "stale"

    from app.secrets import reapply_overlay_token_from_vault

    reapply_overlay_token_from_vault("AGNES_TEST_TOKEN")

    assert "AGNES_TEST_TOKEN" not in os.environ


# ---------------------------------------------------------------------------
# Keyless / S-tier fallback
# ---------------------------------------------------------------------------


def test_keyless_fallback_uses_file_and_warns_once(env, caplog):
    from app.secrets import _state_dir, persist_overlay_token

    caplog.set_level(logging.WARNING, logger="app.secrets")

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")
    persist_overlay_token("AGNES_TEST_TOKEN2", "v2")

    overlay_text = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_TEST_TOKEN=v1" in overlay_text
    assert "AGNES_TEST_TOKEN2=v2" in overlay_text
    assert os.environ["AGNES_TEST_TOKEN"] == "v1"
    assert os.environ["AGNES_TEST_TOKEN2"] == "v2"

    warnings = [r for r in caplog.records if "AGNES_VAULT_KEY is not configured" in r.message]
    assert len(warnings) == 1, "warning must fire exactly once per process, not once per call"


def test_keyless_fallback_never_touches_vault(env):
    from app.secrets import persist_overlay_token
    from src.repositories import system_secrets_repo

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")

    assert system_secrets_repo().has("env_overlay/AGNES_TEST_TOKEN") is False


# ---------------------------------------------------------------------------
# Invalid (present-but-malformed) AGNES_VAULT_KEY — must refuse, never
# silently downgrade to the plaintext keyless fallback. This is the fix for
# the "invalid vault downgrade" finding: a misconfigured production key used
# to read as `vault_key_configured() == False`, indistinguishable from the
# deliberate keyless case, so the token got written in plaintext to
# `.env_overlay` under a warning that falsely claimed the key was merely
# "not configured".
# ---------------------------------------------------------------------------


def test_invalid_vault_key_refuses_write(env, monkeypatch):
    from app.secrets import persist_overlay_token
    from app.secrets_vault import VaultKeyInvalidError

    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")

    with pytest.raises(VaultKeyInvalidError):
        persist_overlay_token("AGNES_TEST_TOKEN", "shh-secret")


def test_invalid_vault_key_does_not_write_plaintext_file(env, monkeypatch):
    from app.secrets import _state_dir, persist_overlay_token
    from app.secrets_vault import VaultKeyInvalidError

    monkeypatch.delenv("AGNES_TEST_TOKEN", raising=False)
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")

    with pytest.raises(VaultKeyInvalidError):
        persist_overlay_token("AGNES_TEST_TOKEN", "shh-secret")

    overlay_path = _state_dir() / ".env_overlay"
    assert not overlay_path.exists() or "shh-secret" not in overlay_path.read_text()
    assert "AGNES_TEST_TOKEN" not in os.environ


def test_invalid_vault_key_does_not_write_to_vault_either(env, monkeypatch):
    """An invalid key can't build a Fernet, so the vault path is equally
    unreachable — confirm the refusal happens before any DB write, not just
    before the file write."""
    from app.secrets import persist_overlay_token
    from app.secrets_vault import VaultKeyInvalidError
    from src.repositories import system_secrets_repo

    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")

    with pytest.raises(VaultKeyInvalidError):
        persist_overlay_token("AGNES_TEST_TOKEN", "shh-secret")

    assert system_secrets_repo().has("env_overlay/AGNES_TEST_TOKEN") is False


def test_invalid_vault_key_error_message_distinguishes_from_absent(env, monkeypatch):
    """The error text must name the actual problem (set-but-malformed), never
    the "not configured" phrasing reserved for the deliberate keyless case —
    that phrasing is exactly what made the downgrade silent/confusing."""
    from app.secrets import persist_overlay_token
    from app.secrets_vault import VaultKeyInvalidError

    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")

    with pytest.raises(VaultKeyInvalidError, match="is set but is not a valid"):
        persist_overlay_token("AGNES_TEST_TOKEN", "shh-secret")


def test_absent_vault_key_keyless_fallback_still_works(env, caplog):
    """Backward compatibility: an ABSENT key (the deliberate keyless/S-tier
    mode) must be unaffected by the invalid-key guard — same file fallback,
    same one-time warning, as before this fix."""
    from app.secrets import _state_dir, persist_overlay_token

    caplog.set_level(logging.WARNING, logger="app.secrets")

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")

    overlay_text = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_TEST_TOKEN=v1" in overlay_text
    assert os.environ["AGNES_TEST_TOKEN"] == "v1"

    warnings = [r for r in caplog.records if "AGNES_VAULT_KEY is not configured" in r.message]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Periodic re-read (belt-and-braces)
# ---------------------------------------------------------------------------


def test_periodic_reread_applies_changed_value(env, monkeypatch):
    _set_vault_key(monkeypatch)

    from app.secrets import persist_overlay_token, reapply_all_overlay_tokens_from_vault
    from src.repositories import system_secrets_repo

    persist_overlay_token("AGNES_TEST_TOKEN", "v1")

    # Simulate the value having changed behind this process's back (another
    # replica rotated it) and this process's env-overlay-changed event
    # having been lost — the FLUSHALL story from persist_overlay_token's
    # docstring. This process's os.environ is still stale until the
    # periodic sweep runs.
    system_secrets_repo().upsert("env_overlay/AGNES_TEST_TOKEN", "v2")
    os.environ["AGNES_TEST_TOKEN"] = "v1"

    reapply_all_overlay_tokens_from_vault()

    assert os.environ["AGNES_TEST_TOKEN"] == "v2"


def test_periodic_reread_noop_when_vault_unconfigured(env):
    """Keyless mode: the sweep must not raise or touch os.environ."""
    from app.secrets import reapply_all_overlay_tokens_from_vault

    os.environ.pop("AGNES_TEST_TOKEN", None)
    reapply_all_overlay_tokens_from_vault()
    assert "AGNES_TEST_TOKEN" not in os.environ


def test_periodic_reread_continues_past_one_bad_key(env, monkeypatch):
    """One row that fails to reapply (decrypt error, transient DB hiccup)
    must not stop the sweep from applying the remaining keys."""
    _set_vault_key(monkeypatch)

    from app.secrets import persist_overlay_token, reapply_all_overlay_tokens_from_vault

    persist_overlay_token("AGNES_TEST_TOKEN_A", "a1")
    persist_overlay_token("AGNES_TEST_TOKEN_B", "b1")

    real_reapply = __import__(
        "app.secrets", fromlist=["reapply_overlay_token_from_vault"]
    ).reapply_overlay_token_from_vault

    def _flaky(name):
        if name == "AGNES_TEST_TOKEN_A":
            raise RuntimeError("boom")
        return real_reapply(name)

    monkeypatch.setattr("app.secrets.reapply_overlay_token_from_vault", _flaky)

    os.environ["AGNES_TEST_TOKEN_B"] = "stale"
    reapply_all_overlay_tokens_from_vault()

    assert os.environ["AGNES_TEST_TOKEN_B"] == "b1"


# ---------------------------------------------------------------------------
# Precedence: vault wins over file on conflict
# ---------------------------------------------------------------------------


def test_vault_wins_over_file_on_conflict(env, monkeypatch):
    """Mirrors app.main.create_app's boot sequence: the legacy file load
    runs first, then reapply_all_overlay_tokens_from_vault — a vault row
    for the same env_name must win."""
    overlay_path = env["data_dir"] / "state" / ".env_overlay"
    overlay_path.write_text("AGNES_TEST_TOKEN=from-file\n", encoding="utf-8")
    os.environ["AGNES_TEST_TOKEN"] = "from-file"  # what the boot file-load would set

    _set_vault_key(monkeypatch)
    from app.secrets import reapply_all_overlay_tokens_from_vault
    from src.repositories import system_secrets_repo

    system_secrets_repo().upsert("env_overlay/AGNES_TEST_TOKEN", "from-vault")

    reapply_all_overlay_tokens_from_vault()

    assert os.environ["AGNES_TEST_TOKEN"] == "from-vault"
