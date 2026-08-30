"""A dead extractor must leave a trace in `sync_state`, not only in stdout.

`set_error` was written only when the subprocess printed a parseable per-table
stats line. A credential failure kills the extractor at startup, so nothing was
parsed, and the fallback appended to `collected_errors` — which is consumed
only by `notify_sync_failure`, itself a no-op without an alert webhook. The
result: table status stayed `pending`, the registry's error column stayed
blank, the source card said "Never synced" rather than failed, and `/admin`'s
Needs-fixing zone stayed empty, because `_resolve_sync_failures` reads
`sync_state` and `sync_state` was never written. The one copy of
"Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN" lived in the server
process's stdout.

These tests pin the fallback: when the run dies with no recoverable per-table
stats, every table it attempted gets an error state carrying the stderr tail.
"""

import pytest

from app.api import sync as sync_api


class _FakeState:
    def __init__(self):
        self.errors: dict[str, str] = {}

    def set_error(self, key, err):
        self.errors[key] = err

    def get_table_state(self, _key):
        return None


@pytest.fixture
def captured(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr(sync_api, "sync_state_repo", lambda: state)
    monkeypatch.setattr(sync_api, "table_registry_repo", lambda: _FakeRegistry())
    return state


class _FakeRegistry:
    def list_all(self):
        return [{"name": "orders", "id": "orders"}, {"name": "customers", "id": "customers"}]


class TestExtractorCrashFallback:
    def test_every_attempted_table_gets_an_error_state(self, captured):
        sync_api._record_extractor_crash(
            table_configs=[{"id": "orders", "name": "orders"}, {"id": "customers", "name": "customers"}],
            returncode=1,
            stderr="ERROR: Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN\n",
        )
        assert set(captured.errors) == {"orders", "customers"}

    def test_the_error_carries_the_real_cause(self, captured):
        sync_api._record_extractor_crash(
            table_configs=[{"id": "orders", "name": "orders"}],
            returncode=1,
            stderr="ERROR: Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN\n",
        )
        assert "KEBOOLA_STACK_URL" in captured.errors["orders"]

    def test_the_exit_code_is_named(self, captured):
        sync_api._record_extractor_crash(
            table_configs=[{"id": "orders", "name": "orders"}],
            returncode=127,
            stderr="",
        )
        assert "127" in captured.errors["orders"]

    def test_an_empty_stderr_still_produces_an_actionable_message(self, captured):
        sync_api._record_extractor_crash(
            table_configs=[{"id": "orders", "name": "orders"}],
            returncode=1,
            stderr="",
        )
        msg = captured.errors["orders"]
        assert msg.strip()
        assert "server log" in msg.lower()

    def test_it_never_raises_and_never_blocks_the_run(self, captured, monkeypatch):
        """This runs on the failure path — it must not turn a failed sync into
        a crashed request."""
        class _Boom:
            def set_error(self, *a, **k):
                raise RuntimeError("db gone")

        monkeypatch.setattr(sync_api, "sync_state_repo", lambda: _Boom())
        sync_api._record_extractor_crash(
            table_configs=[{"id": "orders", "name": "orders"}], returncode=1, stderr="x"
        )


class TestRealExtractorPathReachesIt:
    """The unit tests above prove the helper writes. This proves the crash
    path actually CALLS it — the wiring is the part that was missing, not the
    writing, so a test that only exercised the helper would pass on the bug.
    """

    def test_a_credential_less_run_lands_in_sync_state(self, tmp_path, monkeypatch):
        state = _FakeState()
        monkeypatch.setattr(sync_api, "sync_state_repo", lambda: state)
        monkeypatch.setattr(sync_api, "table_registry_repo", lambda: _FakeRegistry())
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        collected: list[dict] = []
        # No KEBOOLA_STACK_URL / KEBOOLA_STORAGE_TOKEN in this env, which is
        # exactly the failure an admin hits after a token validation error:
        # the extractor dies at startup, before printing any stats line.
        sync_api._invoke_keboola_extractor_subprocess(
            table_configs=[{"id": "orders", "name": "orders", "bucket": "in.c-main", "source_table": "orders"}],
            env={"PATH": "/usr/bin:/bin"},
            collected_errors=collected,
            synced_table_names=set(),
        )

        assert "orders" in state.errors, (
            "a dead extractor left sync_state empty — every admin surface that "
            "reports sync health reads it, so the failure is invisible"
        )
        assert "exit" in state.errors["orders"]
