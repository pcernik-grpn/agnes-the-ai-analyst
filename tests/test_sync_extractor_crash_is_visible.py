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
        sync_api._record_extractor_crash(table_configs=[{"id": "orders", "name": "orders"}], returncode=1, stderr="x")


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


class TestCrashDetailIsRedacted:
    """The recorded cause must never carry the credential that caused it.

    `_record_extractor_crash` persists the last stderr line into `sync_state`,
    which the admin UI renders. DuckDB echoes the offending statement for a
    whole class of errors — a Catalog error renders `LINE 1: <statement>`
    verbatim — and the Keboola path builds
    `ATTACH '<url>' AS kbc (TYPE keboola, TOKEN '<token>')`. Without redaction
    a credential failure would move the storage token out of the process's
    stdout and into the app-state database.
    """

    @pytest.mark.parametrize(
        "message, secret",
        [
            (
                (
                    "Catalog Error: LINE 1: ATTACH 'https://example.com' AS kbc "
                    "(TYPE keboola, TOKEN 'SUPERSECRET-abc123')"
                ),
                "SUPERSECRET-abc123",
            ),
            ("IO Error: HTTP 401 Unauthorized for token=SUPERSECRET-abc123", "SUPERSECRET-abc123"),
            # The keyword is part of a larger identifier, not a bare word.
            ('CREATE SECRET s (TYPE http, BEARER_TOKEN "SUPERSECRET-abc123")', "SUPERSECRET-abc123"),
            ("KEBOOLA_STORAGE_TOKEN=SUPERSECRET-abc123 rejected", "SUPERSECRET-abc123"),
            ("password: hunter2 was rejected", "hunter2"),
        ],
    )
    def test_credential_literals_are_redacted(self, message, secret):
        from app.api.sync import _redact_secrets

        assert secret not in _redact_secrets(message)
        assert "[REDACTED]" in _redact_secrets(message)

    def test_ordinary_causes_survive_intact(self):
        """Redaction must not eat the diagnostic — that is the whole point."""
        from app.api.sync import _redact_secrets

        assert _redact_secrets("extractor failed: connection refused") == ("extractor failed: connection refused")

    def test_detail_is_capped(self):
        """An error is a UI cell, not a log sink, and it is written once per
        attempted table."""
        from app.api.sync import _redact_secrets

        assert len(_redact_secrets("x" * 5000)) == 500
