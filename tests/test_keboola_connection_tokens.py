"""B2: per-connection credential resolution for the Keboola extractor
subprocess (the pass that handles `local`/`remote` registry rows).

Pre-fix, only `_run_materialized_pass` resolved a per-`connection_id`
Keboola token; the extractor subprocess (`app.api.sync._run_sync`) always
used a single global env pair (`KEBOOLA_STACK_URL`/`KEBOOLA_STORAGE_TOKEN`)
for EVERY `local`/`remote` row regardless of which connection it was
registered against — a UI-created connection's rows never synced (no env
var named after it), and on a multi-connection instance a second project's
rows silently extracted with the FIRST project's token.

Pattern follows `tests/test_run_sync_extractor_stats_persist.py` (fake
`subprocess.Popen`, monkeypatched `TableRegistryRepository.list_local`) and
`tests/test_sync_multi_connection.py` (per-connection resolution against a
real, isolated system.duckdb via `source_connections_repo` /
`connection_secrets_repo`).
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


@pytest.fixture(autouse=True)
def _reset_sync_lock():
    from app.api import sync as sync_mod

    if sync_mod._sync_lock.locked():
        sync_mod._sync_lock.release()
    sync_mod._recent_trigger_at = 0.0
    yield
    if sync_mod._sync_lock.locked():
        sync_mod._sync_lock.release()
    sync_mod._recent_trigger_at = 0.0


class _FakePopen:
    """Captures every invocation's `env` kwarg instead of spawning anything —
    the extractor subprocess is never actually run in these tests."""

    calls: list = []

    def __init__(self, cmd, **kwargs):
        self.pid = 4242
        self.returncode = 0
        type(self).calls.append({"cmd": cmd, "env": kwargs.get("env")})

    def communicate(self, input=None, timeout=None):
        return (json.dumps({"tables_extracted": 0, "tables_failed": 0, "errors": []}), "")


def _patch_common(monkeypatch, tmp_path, table_configs):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()

    from app import instance_config as ic_mod

    monkeypatch.setattr(ic_mod, "get_data_source_type", lambda: "keboola")
    monkeypatch.setattr(ic_mod, "get_value", lambda *a, **kw: "")

    from src.repositories.table_registry import TableRegistryRepository

    monkeypatch.setattr(
        TableRegistryRepository,
        "list_local",
        lambda self, *a, **kw: table_configs,
    )

    from src import orchestrator as orch_mod
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        orch_mod,
        "SyncOrchestrator",
        lambda *a, **kw: MagicMock(rebuild=MagicMock(return_value={})),
        raising=False,
    )

    import subprocess

    _FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)


def test_connection_attributed_row_uses_the_connections_vault_token(tmp_path, monkeypatch):
    """(a) A `local` row attributed to a connection_id whose vault holds a
    token must extract with THAT token/URL — never the global env pair."""
    row = {
        "id": "conn_a_table",
        "name": "conn_a_table",
        "source_type": "keboola",
        "bucket": "in.c-a",
        "source_table": "t",
        "query_mode": "local",
        "connection_id": "conn-a",
    }
    _patch_common(monkeypatch, tmp_path, [row])

    # A global env token/URL that must NEVER be used for this row.
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-should-not-be-used")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://global.example.com")

    from src.repositories import source_connections_repo, connection_secrets_repo

    source_connections_repo().create(
        id="conn-a",
        name="Project A",
        source_type="keboola",
        config={"stack_url": "https://a.keboola.com"},
    )
    connection_secrets_repo().upsert("conn-a", "conn-a-vault-token")

    from app.api import sync as sync_mod

    result = sync_mod._run_sync()

    assert result is True
    assert len(_FakePopen.calls) == 1, f"expected exactly one extractor call, got {_FakePopen.calls}"
    call_env = _FakePopen.calls[0]["env"]
    assert call_env["KEBOOLA_STORAGE_TOKEN"] == "conn-a-vault-token"
    assert call_env["KEBOOLA_STACK_URL"] == "https://a.keboola.com"


def test_connection_with_no_resolvable_token_is_skipped_loudly(tmp_path, monkeypatch):
    """(b) A row attributed to a connection with NO resolvable token must:
    - never reach the extractor subprocess, and
    - record sync_state error == 'missing_connection_token'.

    It must NEVER fall back to the global env token."""
    row = {
        "id": "conn_b_table",
        "name": "conn_b_table",
        "source_type": "keboola",
        "bucket": "in.c-b",
        "source_table": "t",
        "query_mode": "local",
        "connection_id": "conn-b",
    }
    _patch_common(monkeypatch, tmp_path, [row])

    # A global env token that must NOT be used as a fallback for this row.
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-should-not-be-used")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://global.example.com")

    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id="conn-b",
        name="Project B",
        source_type="keboola",
        config={"stack_url": "https://b.keboola.com"},
        # No token_env, and nothing in the vault below — unresolvable.
    )

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    assert _FakePopen.calls == [], (
        f"extractor must not be invoked for an unresolvable connection, got {_FakePopen.calls}"
    )

    from src.repositories import sync_state_repo

    state = sync_state_repo().get_table_state("conn_b_table")
    assert state is not None
    assert state["status"] == "error"
    assert state["error"] == "missing_connection_token"


def test_unattributed_row_still_uses_the_global_env(tmp_path, monkeypatch):
    """(c) A row with no connection_id keeps today's behavior unchanged —
    it extracts with the global env pair."""
    row = {
        "id": "global_table",
        "name": "global_table",
        "source_type": "keboola",
        "bucket": "in.c-global",
        "source_table": "t",
        "query_mode": "local",
        "connection_id": None,
    }
    _patch_common(monkeypatch, tmp_path, [row])

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-token-abc")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://global.example.com")

    from app.api import sync as sync_mod

    result = sync_mod._run_sync()

    assert result is True
    assert len(_FakePopen.calls) == 1
    call_env = _FakePopen.calls[0]["env"]
    assert call_env["KEBOOLA_STORAGE_TOKEN"] == "global-token-abc"
    assert call_env["KEBOOLA_STACK_URL"] == "https://global.example.com"


def test_mixed_rows_are_grouped_into_separate_subprocess_calls(tmp_path, monkeypatch):
    """A run with both an unattributed row and a connection-attributed row
    must fire TWO extractor calls — never merge them into one env."""
    rows = [
        {
            "id": "global_table",
            "name": "global_table",
            "source_type": "keboola",
            "bucket": "in.c-global",
            "source_table": "t",
            "query_mode": "local",
            "connection_id": None,
        },
        {
            "id": "conn_a_table",
            "name": "conn_a_table",
            "source_type": "keboola",
            "bucket": "in.c-a",
            "source_table": "t",
            "query_mode": "local",
            "connection_id": "conn-a",
        },
    ]
    _patch_common(monkeypatch, tmp_path, rows)

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-token-abc")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://global.example.com")

    from src.repositories import source_connections_repo, connection_secrets_repo

    source_connections_repo().create(
        id="conn-a",
        name="Project A",
        source_type="keboola",
        config={"stack_url": "https://a.keboola.com"},
    )
    connection_secrets_repo().upsert("conn-a", "conn-a-vault-token")

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    assert len(_FakePopen.calls) == 2, f"expected two grouped calls, got {_FakePopen.calls}"
    tokens = {c["env"]["KEBOOLA_STORAGE_TOKEN"] for c in _FakePopen.calls}
    assert tokens == {"global-token-abc", "conn-a-vault-token"}


def test_second_group_runs_in_merge_mode_and_global_group_goes_first(tmp_path, monkeypatch):
    """Every extractor group writes the SAME extracts/keboola/extract.duckdb,
    and `run()`'s default mode rebuilds it from scratch — so with 2+ groups
    the parent must pass `--merge` to every invocation AFTER the first, or
    each later group clobbers the previous group's tables (only the last
    connection's `_meta` rows and views would survive the pass). The global
    (connection_id IS NULL) group must also be dispatched FIRST so it runs
    fresh and keeps first-writer ownership of the `kbc` `_remote_attach`
    alias."""
    rows = [
        # Deliberately listed connection-row-first: dispatch order must come
        # from the None-first sort, not from registry order.
        {
            "id": "conn_a_table",
            "name": "conn_a_table",
            "source_type": "keboola",
            "bucket": "in.c-a",
            "source_table": "t",
            "query_mode": "local",
            "connection_id": "conn-a",
        },
        {
            "id": "global_table",
            "name": "global_table",
            "source_type": "keboola",
            "bucket": "in.c-global",
            "source_table": "t",
            "query_mode": "local",
            "connection_id": None,
        },
    ]
    _patch_common(monkeypatch, tmp_path, rows)

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-token-abc")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://global.example.com")

    from src.repositories import source_connections_repo, connection_secrets_repo

    source_connections_repo().create(
        id="conn-a",
        name="Project A",
        source_type="keboola",
        config={"stack_url": "https://a.keboola.com"},
    )
    connection_secrets_repo().upsert("conn-a", "conn-a-vault-token")

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    assert len(_FakePopen.calls) == 2, f"expected two grouped calls, got {_FakePopen.calls}"
    first, second = _FakePopen.calls
    # Global group first, fresh (no --merge): whole-pass prune semantics.
    assert first["env"]["KEBOOLA_STORAGE_TOKEN"] == "global-token-abc"
    assert "--merge" not in first["cmd"]
    # Named-connection group second, merged on top of the global group's file.
    assert second["env"]["KEBOOLA_STORAGE_TOKEN"] == "conn-a-vault-token"
    assert "--merge" in second["cmd"]
