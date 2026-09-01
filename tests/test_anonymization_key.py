"""``src/anonymization_key.py`` — the zero-ops per-instance HMAC key.

Owner decision 2026-09-01: an anonymize-marked scope on an instance with no
operator-minted key must no longer fail closed forever. Agnes generates the
key itself, stores it encrypted through the EXISTING secret vault
(``app/secrets_vault.py`` → ``system_secrets``), and never rotates it.

What these tests actually pin, in the order the module's precedence runs:

1. an operator-minted env key still wins over anything stored;
2. the admin-writable env NAME is still allowlist-gated BEFORE any value is
   read (a security control that must survive the refactor — the same cases
   live in ``tests/test_worker_kinds.py`` against the delegating wrapper);
3. provisioning happens exactly once, writes the cataloged audit event, and
   logs the key's fingerprint and never its value;
4. an existing key is never overwritten — including by a caller that lost a
   race — because a second key orphans every pseudonym written under the
   first (design spec §9.2 / §1);
5. with no env key and no usable vault the failure is still closed, and now
   names BOTH fixes;
6. ``key_status()``'s contract, so wiring it into the admin drawer is one
   line.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest


def _config_get_value(config: dict):
    """A drop-in ``app.instance_config.get_value`` fake driven by a plain
    nested dict, so tests never need a real ``instance.yaml`` on disk.
    Mirrors the helper in ``tests/test_worker_kinds.py``."""

    def _get(*keys, default=None):
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    return _get


@pytest.fixture
def vault_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR + a real, stable vault key.

    A stable ``AGNES_VAULT_KEY`` is what makes the vault "usable" for
    provisioning (the module deliberately refuses the LOCAL_DEV_MODE
    ephemeral key — see ``_vault_usable``).
    """
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    monkeypatch.delenv("AGNES_ANONYMIZATION_HMAC_KEY", raising=False)
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))

    from src.db import close_system_db, get_system_db

    close_system_db()
    get_system_db()  # forces schema creation (system_secrets + audit_log)
    yield tmp_path
    close_system_db()


@pytest.fixture
def no_vault(tmp_path, monkeypatch):
    """Same, but with NO usable vault key — the fail-closed posture."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("AGNES_ANONYMIZATION_HMAC_KEY", raising=False)
    monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))

    from src.db import close_system_db, get_system_db

    close_system_db()
    get_system_db()
    yield tmp_path
    close_system_db()


def _audit_rows(action: str) -> list[dict]:
    from src.db import get_system_db

    rows = get_system_db().execute("SELECT action, params FROM audit_log WHERE action = ?", [action]).fetchall()
    out = []
    for act, params in rows:
        parsed = params
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except ValueError:
                parsed = {"_raw": parsed}
        out.append({"action": act, "params": parsed or {}})
    return out


def _stored_payload() -> dict | None:
    from src.anonymization_key import VAULT_SECRET_NAME
    from src.repositories import system_secrets_repo

    raw = system_secrets_repo().get(VAULT_SECRET_NAME)
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# 1. Precedence
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_env_key_wins_over_a_stored_vault_key(self, vault_db, monkeypatch):
        """An operator who minted their own key must keep using it — a
        provisioned key must never quietly shadow the configured one."""
        from src.anonymization_key import VAULT_SECRET_NAME, resolve_or_provision_key
        from src.repositories import system_secrets_repo

        system_secrets_repo().insert_if_absent(
            VAULT_SECRET_NAME,
            json.dumps({"version": 1, "key": "stored-vault-key", "provisioned_at": "2026-09-01T00:00:00+00:00"}),
        )
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "operator-minted")

        assert resolve_or_provision_key() == b"operator-minted"

    def test_env_key_alone_never_provisions(self, vault_db, monkeypatch):
        """With an env key present, nothing is generated or written — the
        vault stays empty."""
        from src.anonymization_key import resolve_or_provision_key

        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "operator-minted")

        assert resolve_or_provision_key() == b"operator-minted"
        assert _stored_payload() is None
        assert _audit_rows("anonymization.key_provisioned") == []

    def test_stored_vault_key_is_used_when_no_env_key(self, vault_db):
        from src.anonymization_key import VAULT_SECRET_NAME, resolve_or_provision_key
        from src.repositories import system_secrets_repo

        system_secrets_repo().insert_if_absent(
            VAULT_SECRET_NAME,
            json.dumps({"version": 1, "key": "stored-vault-key", "provisioned_at": "2026-09-01T00:00:00+00:00"}),
        )

        assert resolve_or_provision_key() == b"stored-vault-key"
        # Reading a stored key is not a provisioning event.
        assert _audit_rows("anonymization.key_provisioned") == []

    def test_allowlist_gate_fires_before_any_vault_lookup(self, vault_db, monkeypatch):
        """The admin-writable env NAME gate is the security control this
        refactor must not lose: a disallowed name is refused outright, never
        waved through to a generated key."""
        from src.anonymization_key import AnonymizationKeyError, resolve_or_provision_key

        monkeypatch.setenv("SOME_UNRELATED_SECRET", "leaked-if-not-gated")
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"extraction": {"anonymization": {"hmac_key_env": "SOME_UNRELATED_SECRET"}}}),
        )

        with pytest.raises(AnonymizationKeyError, match="not an allowed anonymization key variable"):
            resolve_or_provision_key()

        assert _stored_payload() is None


# ---------------------------------------------------------------------------
# 2. Provisioning
# ---------------------------------------------------------------------------


class TestProvisioning:
    def test_key_is_generated_stored_and_reused(self, vault_db):
        from src.anonymization_key import resolve_or_provision_key

        first = resolve_or_provision_key()
        assert len(first) == 64  # secrets.token_bytes(32) rendered as hex
        bytes.fromhex(first.decode())  # it really is hex

        payload = _stored_payload()
        assert payload is not None
        assert payload["key"] == first.decode()
        assert payload["provisioned_at"]

        # Second resolution returns the SAME key and does not re-provision.
        assert resolve_or_provision_key() == first
        assert len(_audit_rows("anonymization.key_provisioned")) == 1

    def test_provisioning_writes_the_cataloged_audit_event(self, vault_db):
        from src.anonymization_key import fingerprint, resolve_or_provision_key
        from src.audit_events import is_cataloged

        assert is_cataloged("anonymization.key_provisioned")

        key = resolve_or_provision_key()
        rows = _audit_rows("anonymization.key_provisioned")
        assert len(rows) == 1
        params = rows[0]["params"]
        assert params["fingerprint"] == fingerprint(key)
        assert params["provisioned_at"]
        # The key value itself must never reach the audit trail.
        assert key.decode() not in json.dumps(rows[0])

    def test_logs_the_fingerprint_and_never_the_key(self, vault_db, caplog):
        from src.anonymization_key import fingerprint, resolve_or_provision_key

        with caplog.at_level(logging.DEBUG):
            key = resolve_or_provision_key()

        assert fingerprint(key) in caplog.text
        assert key.decode() not in caplog.text

    def test_fingerprint_is_short_and_not_the_key(self):
        from src.anonymization_key import fingerprint

        fp = fingerprint(b"some-key-material")
        assert len(fp) == 12
        assert fp != "some-key-material"
        assert fingerprint(b"some-key-material") == fp  # stable
        assert fingerprint(b"other") != fp


# ---------------------------------------------------------------------------
# 3. Immutability / race convergence
# ---------------------------------------------------------------------------


class TestWriteOnce:
    def test_no_rotate_or_regenerate_surface_exists(self):
        """Rotation orphans every pseudonym (design spec §9.2 / §1), so this
        module deliberately ships no rotate path. Adding one is a designed
        feature with an alias-rewrite story, not a helper dropped in here."""
        import src.anonymization_key as mod

        for banned in ("rotate", "rotate_key", "regenerate", "regenerate_key", "reset_key"):
            assert not hasattr(mod, banned), f"{banned}() must not exist — see the module docstring"

    def test_second_provision_returns_the_stored_key(self, vault_db):
        from src.anonymization_key import _provision

        first = _provision()
        second = _provision()
        assert second["key"] == first["key"]
        assert second["provisioned_at"] == first["provisioned_at"]
        assert len(_audit_rows("anonymization.key_provisioned")) == 1

    def test_losing_the_insert_race_returns_the_winner_key(self, vault_db, monkeypatch):
        """Deterministic stand-in for the cross-process race: another writer
        lands its row between our pre-check and our insert. The loser must
        return the WINNER's key (never its own discarded candidate) and must
        not claim a provisioning event that did not happen."""
        from src.anonymization_key import VAULT_SECRET_NAME, _provision
        from src.repositories import system_secrets_repo

        real_repo = system_secrets_repo()

        class RacingRepo:
            def __init__(self, inner):
                self._inner = inner

            def get(self, name):
                return self._inner.get(name)

            def insert_if_absent(self, name, value):
                # A rival process wins the row first…
                self._inner.insert_if_absent(
                    name,
                    json.dumps({"version": 1, "key": "winner-key", "provisioned_at": "2026-09-01T00:00:00+00:00"}),
                )
                # …so our own atomic insert is the no-op it should be.
                return self._inner.insert_if_absent(name, value)

        monkeypatch.setattr("src.anonymization_key._secrets_repo", lambda: RacingRepo(real_repo))

        result = _provision()

        assert result["key"] == "winner-key"
        assert json.loads(real_repo.get(VAULT_SECRET_NAME))["key"] == "winner-key"
        assert _audit_rows("anonymization.key_provisioned") == []

    def test_concurrent_resolvers_converge_on_one_key(self, vault_db):
        """Four resolvers released together must all end up with the same
        key, and the store must hold exactly one."""
        from src.anonymization_key import resolve_or_provision_key

        barrier = threading.Barrier(4)
        results: list[bytes] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait(timeout=10)
                key = resolve_or_provision_key()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(key)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(results) == 4
        assert len(set(results)) == 1, "concurrent resolvers diverged onto different keys"
        assert _stored_payload()["key"] == results[0].decode()
        assert len(_audit_rows("anonymization.key_provisioned")) == 1

    def test_undecryptable_stored_key_fails_rather_than_being_replaced(self, vault_db, monkeypatch):
        """A key stored under a DIFFERENT AGNES_VAULT_KEY reads as absent —
        but the row is still there, so provisioning must refuse rather than
        mint a replacement that would orphan every existing pseudonym. The
        fix is restoring the vault key, not regenerating."""
        import os

        from cryptography.fernet import Fernet

        from src.anonymization_key import VAULT_SECRET_NAME, AnonymizationKeyError, resolve_or_provision_key
        from src.repositories import system_secrets_repo

        resolve_or_provision_key()  # provision under the fixture's vault key
        original_vault_key = os.environ["AGNES_VAULT_KEY"]
        before = system_secrets_repo().get(VAULT_SECRET_NAME)
        assert before  # readable right now

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))

        with pytest.raises(AnonymizationKeyError, match="Refusing to overwrite"):
            resolve_or_provision_key()

        # Restore the original vault key: the ORIGINAL row is still there, intact.
        monkeypatch.setenv("AGNES_VAULT_KEY", original_vault_key)
        assert system_secrets_repo().get(VAULT_SECRET_NAME) == before

    def test_malformed_stored_payload_is_never_overwritten(self, vault_db):
        """A junk row is a restore problem, not a regenerate problem —
        silently minting a replacement is exactly the accidental key loss
        this module exists to prevent."""
        from src.anonymization_key import VAULT_SECRET_NAME, _read_stored
        from src.repositories import system_secrets_repo

        repo = system_secrets_repo()
        repo.insert_if_absent(VAULT_SECRET_NAME, "not-json-at-all")

        assert _read_stored() is None  # reads as "no usable key"
        assert repo.get(VAULT_SECRET_NAME) == "not-json-at-all"  # left intact


# ---------------------------------------------------------------------------
# 4. Fail-closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_no_env_and_no_vault_names_both_fixes(self, no_vault):
        from src.anonymization_key import AnonymizationKeyError, resolve_or_provision_key

        with pytest.raises(AnonymizationKeyError) as exc:
            resolve_or_provision_key()

        message = str(exc.value)
        assert "AGNES_VAULT_KEY" in message
        assert "AGNES_ANONYMIZATION_HMAC_KEY" in message
        # Kept verbatim so the pre-existing worker-level test (and any
        # operator grep) still matches.
        assert "not set on the server" in message

    def test_invalid_vault_key_is_not_a_usable_vault(self, no_vault, monkeypatch):
        """A set-but-malformed AGNES_VAULT_KEY must not be mistaken for a
        usable vault and silently provision an unrecoverable key."""
        from src.anonymization_key import AnonymizationKeyError, resolve_or_provision_key

        monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")

        with pytest.raises(AnonymizationKeyError, match="not set on the server"):
            resolve_or_provision_key()

    def test_local_dev_ephemeral_key_does_not_count_as_a_vault(self, no_vault, monkeypatch):
        """LOCAL_DEV_MODE's ephemeral vault key would re-generate the
        anonymization key on every restart, orphaning every pseudonym — so
        it is deliberately NOT accepted here."""
        from src.anonymization_key import AnonymizationKeyError, resolve_or_provision_key

        monkeypatch.setenv("LOCAL_DEV_MODE", "1")

        with pytest.raises(AnonymizationKeyError):
            resolve_or_provision_key()


# ---------------------------------------------------------------------------
# 5. key_status() — the UI contract
# ---------------------------------------------------------------------------


class TestKeyStatus:
    _FIELDS = {"configured", "source", "fingerprint", "provisioned_at"}

    def test_unconfigured_shape(self, no_vault):
        from src.anonymization_key import key_status

        status = key_status()
        assert set(status) == self._FIELDS
        assert status == {"configured": False, "source": None, "fingerprint": None, "provisioned_at": None}

    def test_env_source(self, vault_db, monkeypatch):
        from src.anonymization_key import fingerprint, key_status

        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "operator-minted")

        status = key_status()
        assert set(status) == self._FIELDS
        assert status["configured"] is True
        assert status["source"] == "env"
        assert status["fingerprint"] == fingerprint(b"operator-minted")
        assert status["provisioned_at"] is None

    def test_vault_source_reports_fingerprint_and_provisioned_at(self, vault_db):
        from src.anonymization_key import fingerprint, key_status, resolve_or_provision_key

        key = resolve_or_provision_key()
        status = key_status()

        assert set(status) == self._FIELDS
        assert status["configured"] is True
        assert status["source"] == "vault"
        assert status["fingerprint"] == fingerprint(key)
        assert status["provisioned_at"] == _stored_payload()["provisioned_at"]

    def test_never_returns_the_key_value(self, vault_db):
        from src.anonymization_key import key_status, resolve_or_provision_key

        key = resolve_or_provision_key()
        assert key.decode() not in json.dumps(key_status())

    def test_never_provisions(self, vault_db):
        """A status read must not be what creates an instance's key."""
        from src.anonymization_key import key_status

        assert key_status()["configured"] is False
        assert _stored_payload() is None
        assert _audit_rows("anonymization.key_provisioned") == []

    def test_disallowed_env_name_reads_as_unconfigured_not_an_exception(self, vault_db, monkeypatch):
        from src.anonymization_key import key_status

        monkeypatch.setenv("SOME_UNRELATED_SECRET", "leaked-if-not-gated")
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"extraction": {"anonymization": {"hmac_key_env": "SOME_UNRELATED_SECRET"}}}),
        )

        assert key_status() == {
            "configured": False,
            "source": None,
            "fingerprint": None,
            "provisioned_at": None,
        }


# ---------------------------------------------------------------------------
# 6. The worker-side delegate keeps its name/signature
# ---------------------------------------------------------------------------


def test_worker_kinds_delegates_and_re_exports(vault_db):
    """``connectors/sharepoint/crawler.py`` imports
    ``app.worker.kinds._resolve_anonymization_key`` (and the tests import
    ``AnonymizationKeyError``) by name — the move must not break either."""
    from app.worker.kinds import AnonymizationKeyError, _resolve_anonymization_key
    from src.anonymization_key import AnonymizationKeyError as ModuleError
    from src.anonymization_key import resolve_or_provision_key

    assert AnonymizationKeyError is ModuleError

    key = _resolve_anonymization_key()
    assert isinstance(key, str)
    assert key.encode("utf-8") == resolve_or_provision_key()
