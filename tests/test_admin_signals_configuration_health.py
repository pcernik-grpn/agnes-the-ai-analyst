"""The dashboard must have a signal for "you configured something and it is broken".

`admin_signals.py` carried eight signals — five content-moderation, three
operational — and none about configuration. `_resolve_sync_failures` counts
sync RUNS with a non-ok status, so a source that has never run produces none,
which is how "Nothing needs your attention" came to sit above a
credential-less connection on an instance where no table could be read.

These pin the two signals that close that gap.
"""

import pytest

from app.web import admin_signals as sig


class TestVaultKeySignal:
    def test_it_fires_when_secrets_cannot_be_stored(self, monkeypatch):
        monkeypatch.setattr("app.secrets_vault.can_store_secrets", lambda: False)
        s = sig._resolve_vault_key_missing()
        assert s is not None
        assert "AGNES_VAULT_KEY" in s.blurb
        assert "restart" in s.blurb.lower()

    def test_it_names_where_the_work_is_done(self, monkeypatch):
        """Signal rule 3: a row must land where the action can be taken."""
        monkeypatch.setattr("app.secrets_vault.can_store_secrets", lambda: False)
        assert sig._resolve_vault_key_missing().href == "/admin/data-sources"

    def test_it_is_silent_when_a_write_would_succeed(self, monkeypatch):
        """Signal rule 1: zero renders nothing. Guards on `can_store_secrets`,
        not `vault_key_configured` — the latter answers False in local dev
        where the write in fact succeeds, and an error on a working instance
        is exactly the noise that trains admins to stop reading."""
        monkeypatch.setattr("app.secrets_vault.can_store_secrets", lambda: True)
        assert sig._resolve_vault_key_missing() is None


class _Conns:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return self._rows


class _Secrets:
    def __init__(self, have):
        self._have = set(have)

    def has(self, cid):
        return cid in self._have


@pytest.fixture
def _repos(monkeypatch):
    def install(rows, with_secret):
        import src.repositories as repos

        monkeypatch.setattr(repos, "source_connections_repo", lambda: _Conns(rows))
        monkeypatch.setattr(repos, "connection_secrets_repo", lambda: _Secrets(with_secret))

    return install


class TestSourcesWithoutCredential:
    def test_a_credential_less_connection_is_reported(self, _repos):
        _repos([{"id": "c1", "token_env": None}], with_secret=set())
        s = sig._resolve_sources_without_credential()
        assert s is not None and s.count == 1
        assert "cannot read any table" in s.blurb

    def test_a_connection_with_a_stored_secret_is_not(self, _repos):
        _repos([{"id": "c1", "token_env": None}], with_secret={"c1"})
        assert sig._resolve_sources_without_credential() is None

    def test_token_env_counts_as_a_credential(self, _repos):
        """An env-var-backed connection stores nothing in the vault and works."""
        _repos([{"id": "c1", "token_env": "KBC_STORAGE_TOKEN"}], with_secret=set())
        assert sig._resolve_sources_without_credential() is None

    def test_only_the_broken_ones_are_counted(self, _repos):
        _repos(
            [{"id": "a", "token_env": None}, {"id": "b", "token_env": None}, {"id": "c", "token_env": None}],
            with_secret={"a"},
        )
        assert sig._resolve_sources_without_credential().count == 2

    def test_no_connections_at_all_is_silent(self, _repos):
        """'Nothing yet' is not a fault to report."""
        _repos([], with_secret=set())
        assert sig._resolve_sources_without_credential() is None


class TestBothAreRegistered:
    def test_they_sit_in_the_fixing_zone(self):
        keys = {s.key: s for s in sig.signals_for_zone(sig.ZONE_NEEDS_FIXING)}
        assert "vault_key_missing" in keys
        assert "sources_without_credential" in keys

    def test_configuration_health_outranks_failed_runs(self):
        """A broken precondition is why the run failed, so it reads first."""
        order = [s.key for s in sig.signals_for_zone(sig.ZONE_NEEDS_FIXING)]
        assert order.index("vault_key_missing") < order.index("sync_failures")
        assert order.index("sources_without_credential") < order.index("sync_failures")
