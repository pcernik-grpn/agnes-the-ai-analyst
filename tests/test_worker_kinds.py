"""Tests for ``app/worker/kinds.py`` (wave-2B Task 4; ``ducklake-maintenance``
added in wave-2G Task 5 — see ``tests/test_ducklake_maintenance.py`` for its
dedicated coverage; ``analytics-migrate`` added in wave-2G Task 6;
``distribution-mirror`` added in wave-2H Task WF-3 — see
``tests/test_distribution_mirror.py`` for its dedicated coverage).

Verifies:

- ``register_all_kinds()`` registers all five wave-2B job kinds with the
  correct lane (the sixth, ``ducklake-maintenance``, the seventh,
  ``analytics-migrate``, and the eighth, ``distribution-mirror``, are
  asserted alongside them here too — just their presence/lane;
  ``ducklake-maintenance``'s handler behavior lives in
  ``tests/test_ducklake_maintenance.py``, ``analytics-migrate``'s dispatch
  behavior in ``TestAnalyticsMigrateHandler`` below, and
  ``distribution-mirror``'s handler behavior in
  ``tests/test_distribution_mirror.py``).
- Each kind's handler is a thin adapter that DELEGATES to the existing
  function it wraps — no logic is reimplemented here. Verified by
  monkeypatching the wrapped target and asserting it was called (with
  the expected arguments where relevant), not by re-checking the
  wrapped function's own behavior.
- The Jira webhook's incremental-transform path now enqueues a
  ``jira-refresh`` job instead of calling ``SyncOrchestrator().rebuild_source``
  inline.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test.

    Also resets the process-wide chat-manager singleton
    (``app.chat.manager``'s ``_current_manager``/``_current_loop``, read by
    ``register_all_kinds()`` to decide whether ``agent_response`` is
    claimable on this process — see its docstring) to ``None`` around every
    test in this module, so a real ``ChatManager`` left behind by some OTHER
    test's full app-lifespan run (e.g. a ``with TestClient(create_app())``
    elsewhere in the suite) can never leak in and flip which kinds a bare
    ``register_all_kinds()`` call here registers."""
    from app.chat.manager import set_current_chat_manager
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    set_current_chat_manager(None)
    yield
    JOB_KINDS.clear()
    set_current_chat_manager(None)


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR (has the ``jobs`` table),
    closed after the test. Mirrors ``tests/test_worker_runtime.py``'s
    ``worker_db`` fixture."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. the jobs table)
    yield
    close_system_db()


class TestRegisterAllKinds:
    #: The kinds that register UNCONDITIONALLY, regardless of whether
    #: this process has a live chat manager — everything except
    #: ``agent_response`` (role-split review carry-over; see
    #: ``TestAgentResponseRoleSplitRegistration`` below for that one).
    #: ``webhook-deliver`` (V1b Task 6) joined this set because it's a plain
    #: outbound HTTP POST with no dependency on the chat event loop.
    _ALWAYS_REGISTERED = {
        "data-refresh",
        "marketplaces-sync",
        "session-collector",
        "corporate-memory",
        "jira-refresh",
        "jira-org-refresh",
        "ducklake-maintenance",
        "analytics-migrate",
        "distribution-mirror",
        "analytics-rebuild",
        "collections-purge",
        "webhook-deliver",
        "corpus-extraction",
        "sharepoint-acl-sync",
        "sharepoint-subtree-sweep",
    }

    def test_registers_unconditional_kinds_without_chat_manager(self):
        """No live chat manager (the `clean_job_kinds_registry` fixture
        already reset the singleton to `None`) — a worker-only/non-gateway
        process. `agent_response` must NOT be in the registry."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        assert set(JOB_KINDS) == self._ALWAYS_REGISTERED
        assert "agent_response" not in JOB_KINDS

    def test_lanes_are_correct(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import EXTRACTION_LANE, HEAVY_LANE, JOB_KINDS, LIGHT_LANE

        register_all_kinds()

        assert JOB_KINDS["data-refresh"].lane == HEAVY_LANE
        assert JOB_KINDS["jira-refresh"].lane == HEAVY_LANE
        assert JOB_KINDS["marketplaces-sync"].lane == LIGHT_LANE
        assert JOB_KINDS["session-collector"].lane == LIGHT_LANE
        assert JOB_KINDS["corporate-memory"].lane == LIGHT_LANE
        assert JOB_KINDS["ducklake-maintenance"].lane == LIGHT_LANE
        assert JOB_KINDS["analytics-migrate"].lane == HEAVY_LANE
        assert JOB_KINDS["distribution-mirror"].lane == LIGHT_LANE
        assert JOB_KINDS["webhook-deliver"].lane == LIGHT_LANE
        assert JOB_KINDS["analytics-rebuild"].lane == HEAVY_LANE
        assert JOB_KINDS["collections-purge"].lane == HEAVY_LANE
        assert JOB_KINDS["corpus-extraction"].lane == EXTRACTION_LANE
        assert JOB_KINDS["sharepoint-acl-sync"].lane == LIGHT_LANE
        assert JOB_KINDS["sharepoint-subtree-sweep"].lane == LIGHT_LANE

    def test_idempotent_reregistration(self):
        """Calling register_all_kinds() twice (e.g. test re-imports, or a
        future re-init path) must not raise or duplicate entries."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        register_all_kinds()

        assert len(JOB_KINDS) == len(self._ALWAYS_REGISTERED)

    def test_sharepoint_subtree_sweep_no_automatic_retry(self):
        """Same rationale as corpus-extraction — a failed multi-hour sweep
        needs an operator to look at it, not an unattended re-run."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        assert JOB_KINDS["sharepoint-subtree-sweep"].retry_in_seconds is None

    def test_sharepoint_subtree_sweep_lease_env_override(self, monkeypatch):
        """``_sp_sweep_lease_seconds()`` reads the env fresh on every
        ``register_all_kinds()`` call — no reload needed, same as every
        other lease knob in this module."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        monkeypatch.setenv("AGNES_SP_SWEEP_LEASE_S", "600")
        register_all_kinds()

        assert JOB_KINDS["sharepoint-subtree-sweep"].lease_seconds == 600

    def test_sharepoint_subtree_sweep_lease_default(self):
        """Default 4h (14400s) — a full probe pass over a large library is
        multi-hour (spec §6.2)."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        assert JOB_KINDS["sharepoint-subtree-sweep"].lease_seconds == 14400


class TestAgentResponseRoleSplitRegistration:
    """``agent_response`` is registered CONDITIONALLY on
    ``get_current_chat_manager() is not None`` at call time — the fix for
    the role-split bug where a worker-only process (no live `ChatManager`)
    would claim, and permanently fail, a background agent-response job it
    had no way to run. See `register_all_kinds()`'s docstring."""

    def test_no_chat_manager_leaves_agent_response_unregistered(self):
        """A non-gateway process — built the same way `register_all_kinds()`
        is called from `app/main.py`'s lifespan on a `Role.WORKER`-only
        replica, i.e. AFTER CHAT-INIT settled to `None` (worker-only
        topology has no chat manager at all)."""
        from app.chat.manager import get_current_chat_manager, set_current_chat_manager
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        set_current_chat_manager(None)
        assert get_current_chat_manager() is None  # sanity: the case under test

        register_all_kinds()

        assert "agent_response" not in JOB_KINDS

    def test_chat_manager_present_registers_agent_response(self):
        """A gateway-colocated (or all-in-one, single-container) process —
        built the same way `app/main.py`'s lifespan calls
        `set_current_chat_manager(app.state.chat_manager)` BEFORE
        `register_all_kinds()` once CHAT-INIT settles to a real manager."""
        from app.chat.manager import set_current_chat_manager
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import LIGHT_LANE, JOB_KINDS

        set_current_chat_manager(object())  # any non-None sentinel — only identity matters here
        register_all_kinds()

        assert "agent_response" in JOB_KINDS
        assert JOB_KINDS["agent_response"].lane == LIGHT_LANE
        assert JOB_KINDS["agent_response"].retry_in_seconds is None

    def test_registering_then_losing_the_manager_does_not_retroactively_unregister(self):
        """`register_all_kinds()` only ever ADDS/replaces entries — it never
        removes one that's no longer applicable. Documents the actual
        (idempotent, additive) behavior rather than asserting a stronger
        "dynamic deregistration" guarantee this fix does not provide (chat
        state is boot-time-settled in practice; this only matters for a
        hypothetical re-init path)."""
        from app.chat.manager import set_current_chat_manager
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        set_current_chat_manager(object())
        register_all_kinds()
        assert "agent_response" in JOB_KINDS

        set_current_chat_manager(None)
        register_all_kinds()
        assert "agent_response" in JOB_KINDS  # still there — not retroactively removed


class TestDataRefreshHandler:
    def test_delegates_to_run_sync_with_defaults(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None: calls.append((tables, source_type_filter)),
        )

        JOB_KINDS["data-refresh"].handler({})

        assert calls == [(None, None)]

    def test_delegates_to_run_sync_with_payload_overrides(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None: calls.append((tables, source_type_filter)),
        )

        JOB_KINDS["data-refresh"].handler({"tables": ["orders"], "source": "keboola"})

        assert calls == [(["orders"], "keboola")]

    def test_raises_when_run_sync_reports_failure(self, monkeypatch):
        """Job-outcome honesty (wave-2B review carry-over, W2B-4/7):
        `_run_sync` used to swallow every failure internally and return
        nothing, so a `data-refresh` job always finalized 'done' even when
        the sync itself failed. `_run_sync` now returns `False` on a fatal
        or per-table failure; the handler must turn that into a raised
        exception so the worker's lane-slot records the job `failed`
        (with `retry_in_seconds` from the kind's registration) instead of
        `done`."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: False)

        with pytest.raises(RuntimeError):
            JOB_KINDS["data-refresh"].handler({})

    @pytest.mark.parametrize("run_sync_result", [True, None])
    def test_does_not_raise_when_run_sync_succeeds_or_noops(self, monkeypatch, run_sync_result):
        """`True` (clean run) and `None` (this call was a no-op — another
        same-process `_run_sync` already held `_sync_lock`) must both be
        treated as "not a failure of THIS job" — only `False` raises."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: run_sync_result)

        JOB_KINDS["data-refresh"].handler({})  # must not raise


class TestMarketplacesSyncHandler:
    def test_delegates_to_sync_marketplaces(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr("src.marketplace.sync_marketplaces", lambda: calls.append(True) or {"synced": []})

        JOB_KINDS["marketplaces-sync"].handler({})

        assert calls == [True]


class TestSessionCollectorHandler:
    def test_delegates_to_collector_run(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        def fake_run(dry_run=False, verbose=False):
            calls.append((dry_run, verbose))
            return (0, {})

        monkeypatch.setattr("services.session_collector.collector.run", fake_run)

        JOB_KINDS["session-collector"].handler({})

        assert calls == [(False, False)]


class TestCorporateMemoryHandler:
    def test_delegates_to_collect_all(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "services.corporate_memory.collector.collect_all",
            lambda dry_run=False: calls.append(dry_run) or {},
        )

        JOB_KINDS["corporate-memory"].handler({})

        assert calls == [False]


class TestJiraRefreshHandler:
    def test_delegates_to_orchestrator_rebuild_source(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        class FakeOrchestrator:
            def rebuild_source(self, name):
                calls.append(name)
                return {}

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", FakeOrchestrator)

        JOB_KINDS["jira-refresh"].handler({})

        assert calls == ["jira"]


class TestAnalyticsMigrateHandler:
    """``analytics-migrate`` (wave-2G Task 6) — a thin adapter over
    ``SyncOrchestrator().migrate_to_backend(to)``, dispatch-only (the
    method's own behavior is covered in
    ``tests/test_orchestrator.py::TestMigrateToBackend`` and
    ``tests/test_orchestrator_ducklake.py::TestMigrateToBackendDucklakeDirection``)."""

    def test_delegates_to_migrate_to_backend_with_payload_target(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        class FakeOrchestrator:
            def migrate_to_backend(self, to):
                calls.append(to)
                return {}

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", FakeOrchestrator)

        JOB_KINDS["analytics-migrate"].handler({"to": "ducklake"})

        assert calls == ["ducklake"]

    def test_propagates_invalid_target_error(self, monkeypatch):
        """An unknown ``to`` value re-raises ``migrate_to_backend``'s own
        ``ValueError`` — the worker's lane-slot handler turns that into a
        failed job the same way any other handler exception does."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        with pytest.raises(ValueError):
            JOB_KINDS["analytics-migrate"].handler({"to": "bogus"})


class TestWebhookDeliverHandler:
    """``webhook-deliver`` (V1b Task 6) — a thin adapter over
    ``app.chat.webhook_delivery.deliver``; the SSRF guard / HMAC signing /
    failure-tracking behavior of ``deliver`` itself is covered in
    ``tests/test_webhook_delivery.py``. These tests cover only the handler's
    own responsibilities: resolving the webhook row, skipping a
    deleted/disabled one, and turning a failed delivery into a raised
    exception so the worker's standard retry path engages."""

    def test_missing_webhook_id_raises(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        with pytest.raises(RuntimeError, match="webhook_id"):
            JOB_KINDS["webhook-deliver"].handler({})

    def test_deleted_webhook_is_a_clean_noop(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr("src.repositories.agent_webhooks_repo", lambda: _FakeAgentWebhooksRepo(row=None))

        # Must not raise — the webhook was deleted between enqueue and claim.
        JOB_KINDS["webhook-deliver"].handler({"webhook_id": "gone", "notification": {}})

    def test_disabled_webhook_is_a_clean_noop(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr(
            "src.repositories.agent_webhooks_repo",
            lambda: _FakeAgentWebhooksRepo(row={"id": "w1", "active": False}),
        )

        JOB_KINDS["webhook-deliver"].handler({"webhook_id": "w1", "notification": {}})

    def test_failed_delivery_raises_for_job_retry(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr(
            "src.repositories.agent_webhooks_repo",
            lambda: _FakeAgentWebhooksRepo(row={"id": "w1", "active": True, "url": "https://h/x", "secret": "s"}),
        )
        monkeypatch.setattr("app.chat.webhook_delivery.deliver", lambda webhook, payload: False)

        with pytest.raises(RuntimeError, match="w1"):
            JOB_KINDS["webhook-deliver"].handler({"webhook_id": "w1", "notification": {"event": "job.completed"}})

    def test_successful_delivery_does_not_raise(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr(
            "src.repositories.agent_webhooks_repo",
            lambda: _FakeAgentWebhooksRepo(row={"id": "w1", "active": True, "url": "https://h/x", "secret": "s"}),
        )
        delivered = []
        monkeypatch.setattr(
            "app.chat.webhook_delivery.deliver",
            lambda webhook, payload: (delivered.append((webhook, payload)), True)[1],
        )

        JOB_KINDS["webhook-deliver"].handler({"webhook_id": "w1", "notification": {"event": "job.completed"}})

        assert delivered == [
            ({"id": "w1", "active": True, "url": "https://h/x", "secret": "s"}, {"event": "job.completed"})
        ]


class _FakeAgentWebhooksRepo:
    def __init__(self, row):
        self._row = row

    def get(self, webhook_id):
        return self._row


class _FakeSourceConnectionsRepo:
    def __init__(self, row):
        self._row = row

    def get(self, connection_id):
        return self._row


def _config_get_value(config: dict):
    """A drop-in ``app.instance_config.get_value`` fake driven by a plain
    nested dict, so tests never need a real ``instance.yaml`` on disk."""

    def _get(*keys, default=None):
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    return _get


class TestCorpusExtractionHandler:
    """``corpus-extraction`` (spec §7.5 / §16 step 7) — the producer-
    invocation seam. No test here launches a real subprocess
    (``subprocess.run`` is monkeypatched) or touches a real vault — these
    cover only the handler's OWN responsibilities: the ``extraction.enabled``
    / producer-config gate, credential resolution through the EXISTING
    SharePoint settings resolver, secrets landing in the child env (never
    argv), and timeout/failure handling."""

    _ENABLED_CONFIG = {
        "extraction": {
            "enabled": True,
            "producer": {"command": "python -m fake_producer"},
            "timeout_s": 60,
        }
    }

    @pytest.fixture(autouse=True)
    def _clear_extraction_env_var(self, monkeypatch):
        """The `extraction` switch's env var (`AGNES_EXTRACTION_ENABLED`)
        wins over the mocked `get_value` config in every test here — clear
        it so each test's `_config_get_value` fake is what actually decides
        the gate, not whatever happens to be in the runner's shell env."""
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)

    def _register(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        return JOB_KINDS["corpus-extraction"].handler

    def test_disabled_by_default_refuses_to_run(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        handler = self._register()

        with pytest.raises(RuntimeError, match="extraction.enabled"):
            handler({"connection_id": "conn1"})

    def test_missing_producer_config_raises(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"extraction": {"enabled": True}}))
        handler = self._register()

        with pytest.raises(RuntimeError, match="no producer configured"):
            handler({"connection_id": "conn1"})

    def test_missing_connection_id_raises(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        handler = self._register()

        with pytest.raises(RuntimeError, match="connection_id"):
            handler({})

    def test_unknown_connection_raises(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: _FakeSourceConnectionsRepo(row=None))
        handler = self._register()

        with pytest.raises(RuntimeError, match="conn1"):
            handler({"connection_id": "conn1"})

    def test_non_sharepoint_connection_raises(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: _FakeSourceConnectionsRepo(row={"id": "conn1", "source_type": "keboola"}),
        )
        handler = self._register()

        with pytest.raises(RuntimeError, match="conn1"):
            handler({"connection_id": "conn1"})

    def test_settings_resolution_failure_is_wrapped(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: _FakeSourceConnectionsRepo(row={"id": "conn1", "source_type": "sharepoint", "config": {}}),
        )
        from connectors.sharepoint.settings import SharePointSettingsError

        def _boom(connection):
            raise SharePointSettingsError("certificate not configured")

        monkeypatch.setattr("connectors.sharepoint.settings.resolve_sharepoint_settings", _boom)
        handler = self._register()

        with pytest.raises(RuntimeError, match="certificate not configured"):
            handler({"connection_id": "conn1"})

    def _stub_connection_and_settings(self, monkeypatch, *, private_key="super-secret-pem-material", config=None):
        # Default config carries ONE confirmed scope: since the corpus-map
        # handoff, a scope-less connection with no payload corpus_id refuses
        # to run (see test_no_scopes_and_no_corpus_id_refuses) — tests that
        # exercise that exact refusal pass config={} explicitly.
        default_config = {
            "scopes": [
                {
                    "source_scope_id": "scope-default-1",
                    "display_path": "Default site",
                    "anonymize": False,
                    "collection_id": "col_default",
                }
            ]
        }
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: _FakeSourceConnectionsRepo(
                row={
                    "id": "conn1",
                    "source_type": "sharepoint",
                    "config": config if config is not None else default_config,
                }
            ),
        )
        from connectors.sharepoint.settings import SharePointSettings

        fake_settings = SharePointSettings(
            tenant_id="tenant-1",
            client_id="client-1",
            private_key=private_key,
            credential_source="vault",
        )
        monkeypatch.setattr("connectors.sharepoint.settings.resolve_sharepoint_settings", lambda conn: fake_settings)
        return fake_settings

    def test_resolves_credentials_and_builds_argv_with_no_secret_on_argv(self, monkeypatch):
        """The core security assertion (playbook F7): the resolved
        certificate never appears on the subprocess argv — only in the
        child process's environment."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, private_key="super-secret-pem-material")

        calls = []

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, env=None, timeout=None, **kwargs):
            calls.append({"argv": list(argv), "env": dict(env or {}), "timeout": timeout, "kwargs": kwargs})
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        result = handler({"connection_id": "conn1", "corpus_id": "corpus-9"})

        assert result == {"connection_id": "conn1", "corpus_id": "corpus-9", "returncode": 0}
        assert len(calls) == 1
        call = calls[0]
        assert call["argv"] == ["python", "-m", "fake_producer"]
        assert call["timeout"] == 60
        # The secret must never be on argv.
        assert "super-secret-pem-material" not in " ".join(call["argv"])
        # It must be reachable via the child env instead.
        assert call["env"]["AGNES_SHAREPOINT_TENANT_ID"] == "tenant-1"
        assert call["env"]["AGNES_SHAREPOINT_CLIENT_ID"] == "client-1"
        assert call["env"]["AGNES_SHAREPOINT_PRIVATE_KEY"] == "super-secret-pem-material"
        assert call["env"]["AGNES_EXTRACTION_CORPUS_ID"] == "corpus-9"

    def test_child_env_does_not_forward_instance_secrets(self, monkeypatch):
        """The producer is an EXTERNAL, admin-configurable binary — it must
        get a curated non-secret allowlist (+ any explicit
        `env_passthrough`), never the full parent environment. This is the
        actual security boundary: forwarding `{**os.environ}` would leak
        every instance secret (vault key, LLM API key, DB DSN, ...) to
        whatever command an admin points `extraction.producer` at."""
        monkeypatch.setenv("AGNES_VAULT_KEY", "vault-secret-value")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, private_key="cert-secret-material")

        calls = []

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, env=None, **kwargs):
            calls.append(dict(env or {}))
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert len(calls) == 1
        env = calls[0]
        # Instance secrets sitting in the PARENT process env must be absent.
        assert "AGNES_VAULT_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        # The allowlisted operational vars and the named credentials must
        # still be present.
        assert env.get("PATH") == os.environ.get("PATH")
        assert env["AGNES_SHAREPOINT_TENANT_ID"] == "tenant-1"
        assert env["AGNES_SHAREPOINT_CLIENT_ID"] == "client-1"
        assert env["AGNES_SHAREPOINT_PRIVATE_KEY"] == "cert-secret-material"

    def test_env_passthrough_forwards_only_named_vars(self, monkeypatch):
        """`extraction.producer.env_passthrough` is an explicit, per-name
        operator opt-in — it must forward ONLY the vars it names, not open
        the floodgates back to the full environment."""
        monkeypatch.setenv("AGNES_VAULT_KEY", "vault-secret-value")
        monkeypatch.setenv("MY_CUSTOM_PRODUCER_VAR", "custom-value")
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value(
                {
                    "extraction": {
                        "enabled": True,
                        "producer": {
                            "command": "python -m fake_producer",
                            "env_passthrough": ["MY_CUSTOM_PRODUCER_VAR"],
                        },
                        "timeout_s": 60,
                    }
                }
            ),
        )
        self._stub_connection_and_settings(monkeypatch)

        calls = []

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, env=None, **kwargs):
            calls.append(dict(env or {}))
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        handler({"connection_id": "conn1"})

        env = calls[0]
        assert env["MY_CUSTOM_PRODUCER_VAR"] == "custom-value"
        assert "AGNES_VAULT_KEY" not in env

    def test_producer_module_config_builds_python_dash_m_argv(self, monkeypatch):
        import sys

        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value(
                {"extraction": {"enabled": True, "producer": {"module": "fake_producer.run"}, "timeout_s": 30}}
            ),
        )
        self._stub_connection_and_settings(monkeypatch)

        calls = []

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, **kwargs):
            calls.append(list(argv))
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert calls == [[sys.executable, "-m", "fake_producer.run"]]

    def test_producer_timeout_raises(self, monkeypatch):
        import subprocess

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch)

        def _fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        with pytest.raises(RuntimeError, match="timed out"):
            handler({"connection_id": "conn1"})

    def test_producer_nonzero_exit_raises(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch)

        class _FakeCompleted:
            returncode = 3
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr("app.worker.kinds.subprocess.run", lambda argv, **kwargs: _FakeCompleted())
        handler = self._register()

        with pytest.raises(RuntimeError, match="exited 3"):
            handler({"connection_id": "conn1"})

    # -- anonymize-in-front handoff (spec §9/§9.2) --------------------------

    _ANON_CONFIG = {
        "scopes": [
            {
                "source_scope_id": "scope-anon-1",
                "display_path": "Contracts",
                "anonymize": True,
                "collection_id": "col_anon_1",
            },
            {
                "source_scope_id": "scope-plain-1",
                "display_path": "Public docs",
                "anonymize": False,
                "collection_id": "col_plain_1",
            },
        ]
    }

    def _fake_run_capturing(self, monkeypatch, calls):
        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, env=None, **kwargs):
            calls.append({"argv": list(argv), "env": dict(env or {})})
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)

    def test_anonymize_marked_scopes_land_in_child_env_as_json(self, monkeypatch):
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "instance-hmac-secret")
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config=self._ANON_CONFIG)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert len(calls) == 1
        env = calls[0]["env"]
        assert json.loads(env["AGNES_EXTRACTION_ANONYMIZE_SCOPES"]) == {"scope-anon-1": "col_anon_1"}
        assert env["AGNES_ANONYMIZATION_HMAC_KEY"] == "instance-hmac-secret"
        # Never on argv.
        argv_joined = " ".join(calls[0]["argv"])
        assert "instance-hmac-secret" not in argv_joined
        assert "col_anon_1" not in argv_joined

    def test_no_anonymize_scopes_omits_both_vars(self, monkeypatch):
        """No scope marked anonymize -> neither the scopes map NOR the HMAC
        key is resolved or forwarded, even if a key happens to be set in the
        environment — an instance that never anonymizes should never touch
        the key."""
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "unused-secret")
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        plain_config = {
            "scopes": [
                {
                    "source_scope_id": "scope-plain-1",
                    "display_path": "Public docs",
                    "anonymize": False,
                    "collection_id": "col_plain_1",
                }
            ]
        }
        self._stub_connection_and_settings(monkeypatch, config=plain_config)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        handler({"connection_id": "conn1"})

        env = calls[0]["env"]
        assert "AGNES_EXTRACTION_ANONYMIZE_SCOPES" not in env
        assert "AGNES_ANONYMIZATION_HMAC_KEY" not in env

    def test_no_scopes_at_all_omits_both_vars(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config={})

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        # corpus_id because a scope-less connection now refuses without one
        # (see test_no_scopes_and_no_corpus_id_refuses) — this test is about
        # the anonymize vars staying absent, not about scope routing.
        handler({"connection_id": "conn1", "corpus_id": "corpus-9"})

        env = calls[0]["env"]
        assert "AGNES_EXTRACTION_ANONYMIZE_SCOPES" not in env
        assert "AGNES_ANONYMIZATION_HMAC_KEY" not in env

    def test_anonymize_scope_without_a_resolvable_key_raises(self, monkeypatch):
        """An anonymize-marked scope with no HMAC key configured must fail
        the job clean rather than silently run the producer without a
        per-instance key."""
        monkeypatch.delenv("AGNES_ANONYMIZATION_HMAC_KEY", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config=self._ANON_CONFIG)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        with pytest.raises(RuntimeError, match="AGNES_ANONYMIZATION_HMAC_KEY"):
            handler({"connection_id": "conn1"})
        # Never invoked the producer at all — the key resolution failure
        # happens before subprocess.run.
        assert calls == []

    def test_anonymize_key_env_name_must_be_allowlisted(self, monkeypatch):
        monkeypatch.setenv("SOME_UNRELATED_SECRET", "leaked-if-not-gated")
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value(
                {
                    "extraction": {
                        "enabled": True,
                        "producer": {"command": "python -m fake_producer"},
                        "timeout_s": 60,
                        "anonymization": {"hmac_key_env": "SOME_UNRELATED_SECRET"},
                    }
                }
            ),
        )
        self._stub_connection_and_settings(monkeypatch, config=self._ANON_CONFIG)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        with pytest.raises(RuntimeError, match="not an allowed anonymization key variable"):
            handler({"connection_id": "conn1"})
        assert calls == []

    def test_anonymize_key_env_cannot_reuse_the_attach_token_allowlist(self, monkeypatch):
        """RBAC review, 2026-08-28: a name that IS on the connector-ATTACH
        token-env allowlist (e.g. the SharePoint certificate's own name)
        must NOT thereby be usable as the anonymization key env — the two
        allowlists are deliberately disjoint (see
        src/orchestrator_security.py's ``_PRODUCER_KEY_ENVS`` docstring)."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "leaked-if-not-gated")
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value(
                {
                    "extraction": {
                        "enabled": True,
                        "producer": {"command": "python -m fake_producer"},
                        "timeout_s": 60,
                        "anonymization": {"hmac_key_env": "SHAREPOINT_CERT_PRIVATE_KEY"},
                    }
                }
            ),
        )
        self._stub_connection_and_settings(monkeypatch, config=self._ANON_CONFIG)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        with pytest.raises(RuntimeError, match="not an allowed anonymization key variable"):
            handler({"connection_id": "conn1"})
        assert calls == []

    # ---------------------------------------------------------------- corpus map
    # The standard enqueue path (POST /connections/{id}/extract and the
    # scheduled sweep) sends `{"connection_id": ...}` with no corpus_id, so
    # the ONLY way the producer can learn where documents go is
    # AGNES_EXTRACTION_CORPUS_MAP built from the connection's own confirmed
    # scopes. Keys must be in the producer's corpusmap.corpus_for() shape —
    # matched against crawler rows whose `site` is the site display name and
    # whose `path` is DRIVE-RELATIVE — not the wizard's display_path verbatim
    # (which includes the document-library segment for folder scopes).

    _SCOPED_CONFIG = {
        "scopes": [
            {
                # Folder scope: an item id (neither a composite site id nor a
                # "b!" drive id). The wizard breadcrumb always includes the
                # library ("Documents") — the map key must NOT.
                "source_scope_id": "01SO3DIHVJLOMDRMYCA5B37XU577X4KL57",
                "display_path": "Communication site/Documents/Project Kemp",
                "anonymize": False,
                "collection_id": "col_kemp",
            },
            {
                # Site scope: Graph composite id "<host>,<siteGuid>,<webGuid>".
                "source_scope_id": "host.sharepoint.com,f0259dd4-6abe-451b-9b7c-6760d1cdcb5f,aaac162f-d03a-4137-9b64-b56386e26ff0",
                "display_path": "Northwind Star Test",
                "anonymize": False,
                "collection_id": "col_site",
            },
        ]
    }

    def test_corpus_map_built_from_confirmed_scopes(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config=self._SCOPED_CONFIG)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert len(calls) == 1
        env = calls[0]["env"]
        assert json.loads(env["AGNES_EXTRACTION_CORPUS_MAP"]) == {
            "Communication site/Project Kemp": "col_kemp",
            "Northwind Star Test": "col_site",
        }
        # Collection routing is data, not command line — never on argv.
        argv_joined = " ".join(calls[0]["argv"])
        assert "col_kemp" not in argv_joined
        assert "col_site" not in argv_joined

    def test_drive_scope_key_degrades_to_site_segment(self, monkeypatch):
        """A whole-library scope (Graph drive ids start "b!") keys on the
        site segment alone — the producer's map format has no drive
        dimension. Segments are stripped so a UI-authored
        "Site / Documents" display path cannot poison component matching."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        config = {
            "scopes": [
                {
                    "source_scope_id": "b!1J0l8L5qG0WbfGdg0c3LXy8WrKo60DdB",
                    "display_path": "Northwind Star Test / Documents",
                    "anonymize": False,
                    "collection_id": "col_drive",
                }
            ]
        }
        self._stub_connection_and_settings(monkeypatch, config=config)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        handler({"connection_id": "conn1"})

        env = calls[0]["env"]
        assert json.loads(env["AGNES_EXTRACTION_CORPUS_MAP"]) == {"Northwind Star Test": "col_drive"}

    def test_colliding_corpus_map_keys_refuse_loudly(self, monkeypatch):
        """A site scope and a drive scope of the SAME site resolve to the
        same key but different collections — silently routing every row to
        whichever scope happened to win the dict insert is exactly the
        silent-loss class this feature must never have. Refuse, naming both
        scopes, before any subprocess runs."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        config = {
            "scopes": [
                {
                    "source_scope_id": "host.sharepoint.com,f0259dd4,aaac162f",
                    "display_path": "Northwind Star Test",
                    "anonymize": False,
                    "collection_id": "col_site",
                },
                {
                    "source_scope_id": "b!1J0l8L5qG0WbfGdg0c3LXy8WrKo60DdB",
                    "display_path": "Northwind Star Test / Documents",
                    "anonymize": False,
                    "collection_id": "col_drive",
                },
            ]
        }
        self._stub_connection_and_settings(monkeypatch, config=config)

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        with pytest.raises(RuntimeError, match="corpus map"):
            handler({"connection_id": "conn1"})
        assert calls == []

    def test_no_scopes_and_no_corpus_id_refuses(self, monkeypatch):
        """The producer would only preflight-fail later with its own error;
        failing HERE names the connection and costs nothing."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config={})

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        with pytest.raises(RuntimeError, match="no confirmed scopes"):
            handler({"connection_id": "conn1"})
        assert calls == []

    def test_explicit_corpus_id_without_scopes_still_runs(self, monkeypatch):
        """A manual payload with corpus_id keeps working on a connection
        with no confirmed scopes — corpus_id wins outright on the producer
        side, so no map is required."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config={})

        calls: list = []
        self._fake_run_capturing(monkeypatch, calls)
        handler = self._register()

        handler({"connection_id": "conn1", "corpus_id": "corpus-9"})

        env = calls[0]["env"]
        assert env["AGNES_EXTRACTION_CORPUS_ID"] == "corpus-9"
        assert "AGNES_EXTRACTION_CORPUS_MAP" not in env

    def test_producer_output_is_not_buffered_in_this_process(self, monkeypatch):
        """The producer may run for `extraction.timeout_s` (an hour by
        default) and is an external binary nobody here controls the verbosity
        of. `capture_output=True` would hold every byte of that in the
        worker's own memory for the whole run, to serve one DEBUG line on
        failure — enough to OOM a worker whose container limit is 4g by
        default (Devin Review on this PR). stdout is discarded outright (this
        handler never reads it: the producer reports through the ingest API),
        and stderr streams to a file object, not a pipe."""
        import subprocess as _sp

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch)

        seen = {}

        class _FakeCompleted:
            returncode = 0

        def _fake_run(argv, **kwargs):
            seen.update(kwargs)
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert not seen.get("capture_output"), "capture_output buffers the whole run in this process"
        assert seen.get("stdout") is _sp.DEVNULL
        stderr = seen.get("stderr")
        assert stderr is not _sp.PIPE and hasattr(stderr, "write"), stderr

    def test_failed_producer_logs_only_the_tail_of_its_stderr(self, monkeypatch, caplog):
        """A bounded tail is the point of the temp file — a producer that
        wrote a gigabyte before dying must cost a bounded amount of memory to
        report on."""
        import logging

        from app.worker import kinds as _kinds

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch)
        monkeypatch.setattr(_kinds, "_PRODUCER_STDERR_TAIL_BYTES", 32)

        class _FakeCompleted:
            returncode = 3

        def _fake_run(argv, **kwargs):
            kwargs["stderr"].write(b"A" * 5000 + b"THE-ONLY-PART-THAT-MATTERS")
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        handler = self._register()

        with caplog.at_level(logging.DEBUG, logger="app.worker.kinds"):
            with pytest.raises(RuntimeError, match="exited 3"):
                handler({"connection_id": "conn1"})

        tails = [r.getMessage() for r in caplog.records if "producer stderr tail" in r.getMessage()]
        assert tails, caplog.text
        assert "THE-ONLY-PART-THAT-MATTERS" in tails[0]
        assert "A" * 100 not in tails[0], "the whole 5 KB was logged, not a 32-byte tail"

    # -- producer callback credential ----------------------------------------
    # The producer calls back into Agnes's own REST API (corpus-map, scopes,
    # the collections upload, POST /api/facts/ingest, GET
    # /api/facts/corrections) to do its actual work. AGNES_API_TOKEN is a
    # short-lived, PRODUCER-SCOPED JWT minted fresh for this run (see
    # app.auth.producer_token) — this REPLACES the earlier design that
    # forwarded the scheduler shared secret here (a genuine over-grant: that
    # secret resolves to a synthetic Admin-group user).

    def _run_capturing_env(self, monkeypatch):
        calls = []

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(argv, env=None, **kwargs):
            calls.append({"argv": list(argv), "env": dict(env or {})})
            return _FakeCompleted()

        monkeypatch.setattr("app.worker.kinds.subprocess.run", _fake_run)
        return calls

    def test_agnes_api_url_always_forwarded(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.delenv("SCHEDULER_API_TOKEN", raising=False)
        monkeypatch.setenv("SERVER_URL", "https://agnes.example.com")
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert calls[0]["env"]["AGNES_API_URL"] == "https://agnes.example.com"

    def test_agnes_api_token_is_always_a_producer_jwt_never_the_scheduler_secret(self, monkeypatch):
        """AGNES_API_TOKEN must be present and be a producer-typed JWT
        regardless of whether SCHEDULER_API_TOKEN happens to be configured
        — the two credentials are unrelated now. Regression guard for the
        exact bug this change fixes: the child env used to carry the raw
        scheduler secret verbatim."""
        from app.auth.jwt import verify_token

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        secret = "s" * 40
        monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        token = calls[0]["env"]["AGNES_API_TOKEN"]
        assert token != secret
        payload = verify_token(token)
        assert payload is not None
        assert payload["typ"] == "producer"
        assert payload["connection_id"] == "conn1"
        # Never on argv — same F7 rule as every other secret this handler
        # resolves.
        assert token not in " ".join(calls[0]["argv"])

    def test_agnes_api_token_present_even_with_no_scheduler_secret_configured(self, monkeypatch):
        """No scheduler shared secret configured (e.g. LOCAL_DEV_MODE) used
        to mean no callback token at all — now it makes no difference,
        since the producer JWT is minted independently."""
        from app.auth.jwt import verify_token

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.delenv("SCHEDULER_API_TOKEN", raising=False)
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        payload = verify_token(calls[0]["env"]["AGNES_API_TOKEN"])
        assert payload is not None
        assert payload["typ"] == "producer"

    def test_agnes_api_token_collection_ids_match_the_confirmed_scopes(self, monkeypatch):
        """`collection_ids` claim is the SAME source `GET .../corpus-map`
        reads — every confirmed scope's `collection_id`, sorted."""
        from app.auth.jwt import verify_token

        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "instance-hmac-secret")
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config=self._ANON_CONFIG)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        payload = verify_token(calls[0]["env"]["AGNES_API_TOKEN"])
        assert payload["collection_ids"] == ["col_anon_1", "col_plain_1"]

    def test_agnes_api_token_collection_ids_empty_when_no_scopes_confirmed(self, monkeypatch):
        """A connection with no confirmed scopes mints a token naming NO
        collections — so the producer can reach `.../corpus-map` and
        `.../scopes` for its own connection but cannot upload anywhere.

        Reached via an explicit `corpus_id` payload: a run with neither
        confirmed scopes NOR a `corpus_id` is refused outright before the
        mint (nothing would say where documents go), so that is the only
        remaining path on which a scopeless connection spawns a producer.
        """
        from app.auth.jwt import verify_token

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config={})
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1", "corpus_id": "col_explicit"})

        payload = verify_token(calls[0]["env"]["AGNES_API_TOKEN"])
        assert payload["collection_ids"] == []

    def test_agnes_api_token_expiry_tracks_timeout_plus_grace(self, monkeypatch):
        from app.auth.jwt import verify_token
        from app.worker.kinds import _PRODUCER_TOKEN_GRACE_SECONDS

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        payload = verify_token(calls[0]["env"]["AGNES_API_TOKEN"])
        # self._ENABLED_CONFIG sets timeout_s=60. `iat`/`exp` are minted from
        # two separate `datetime.now()` calls a few microseconds apart, so
        # assert within a tight tolerance rather than exact equality — a
        # sub-second-boundary flake is possible but not the thing this test
        # means to pin.
        assert abs((payload["exp"] - payload["iat"]) - (60 + _PRODUCER_TOKEN_GRACE_SECONDS)) <= 1

    def test_agnes_api_token_never_logged(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.setenv("SCHEDULER_API_TOKEN", "t" * 40)
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        with caplog.at_level(logging.DEBUG):
            handler({"connection_id": "conn1"})

        token = calls[0]["env"]["AGNES_API_TOKEN"]
        assert token not in caplog.text
        assert "t" * 40 not in caplog.text

    # -- loopback callback URL guard (role-split worker, no SERVER_URL) -----
    # `agnes_server_url()`'s own loopback fallback is harmless for the
    # default all-in-one process — the producer's parent process IS the
    # app, so 127.0.0.1:8000 is genuinely reachable. It is NOT harmless for
    # a role-split `extraction-worker` container (docker-compose.yml): that
    # fallback would hand the producer THAT WORKER's own loopback address,
    # not the instance's real callback surface — a misconfiguration that
    # reads like a producer bug (crawl succeeds, the final callback just
    # times out or connection-refuses) rather than a clear error naming the
    # missing config key.

    def _set_role(self, monkeypatch, role):
        from app.roles import reset_roles_cache

        if role is None:
            monkeypatch.delenv("AGNES_ROLE", raising=False)
        else:
            monkeypatch.setenv("AGNES_ROLE", role)
        reset_roles_cache()

    @pytest.fixture(autouse=True)
    def _reset_roles_cache_after(self):
        from app.roles import reset_roles_cache

        yield
        reset_roles_cache()

    def test_unconfigured_url_on_role_split_worker_refuses(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
        self._set_role(monkeypatch, "worker")
        self._stub_connection_and_settings(monkeypatch)
        handler = self._register()

        with pytest.raises(RuntimeError, match="SERVER_URL"):
            handler({"connection_id": "conn1"})

    def test_configured_url_on_role_split_worker_is_unaffected(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.setenv("SERVER_URL", "https://agnes.example.com")
        self._set_role(monkeypatch, "worker")
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert calls[0]["env"]["AGNES_API_URL"] == "https://agnes.example.com"

    def test_agnes_internal_url_on_role_split_worker_also_satisfies_the_guard(self, monkeypatch):
        """AGNES_INTERNAL_URL is the documented data-rails-only escape hatch
        for a deployment that cannot set SERVER_URL — the guard must accept
        either, exactly like `agnes_server_url()`'s own resolution order."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.setenv("AGNES_INTERNAL_URL", "http://extraction-worker-internal:8000")
        self._set_role(monkeypatch, "worker")
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert calls[0]["env"]["AGNES_API_URL"] == "http://extraction-worker-internal:8000"

    def test_unconfigured_url_on_all_in_one_process_is_unaffected(self, monkeypatch):
        """Single-container deployment (AGNES_ROLE unset, or `all`): the
        producer's parent process IS the app, so the loopback fallback
        legitimately reaches it — the guard must not fire here."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
        self._set_role(monkeypatch, None)
        self._stub_connection_and_settings(monkeypatch)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert calls[0]["env"]["AGNES_API_URL"] == "http://127.0.0.1:8000"

    # -- broken-inheritance subtree exclusion handoff (2026-08-30 plan,
    # Task 7) --------------------------------------------------------------

    _EXCLUDED_SUBTREE_CONFIG = {
        "scopes": [
            {
                "source_scope_id": "scope-excl-1",
                "display_path": "Contracts",
                "collection_id": "col_excl_1",
                "access_mode": "mirrored",
                "excluded_subtrees": [
                    {"item_id": "item-A", "path": "Contracts/A", "detected_at": "2026-08-30T00:00:00+00:00"},
                    {"item_id": "item-B", "path": "Contracts/B/C", "detected_at": "2026-08-30T00:00:00+00:00"},
                ],
            },
            {
                "source_scope_id": "scope-plain-1",
                "display_path": "Public docs",
                "collection_id": "col_plain_1",
                "access_mode": "mirrored",
            },
        ]
    }

    def test_excluded_subtrees_land_in_child_env_as_json(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        self._stub_connection_and_settings(monkeypatch, config=self._EXCLUDED_SUBTREE_CONFIG)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        env = calls[0]["env"]
        assert json.loads(env["AGNES_SP_EXCLUDED_SUBTREE_IDS"]) == {"scope-excl-1": ["item-A", "item-B"]}
        # The unaffected scope contributes nothing to the map.
        assert "scope-plain-1" not in json.loads(env["AGNES_SP_EXCLUDED_SUBTREE_IDS"])

    def test_no_excluded_subtrees_omits_the_var(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        plain_config = {
            "scopes": [
                {
                    "source_scope_id": "scope-plain-1",
                    "display_path": "Public docs",
                    "collection_id": "col_plain_1",
                    "access_mode": "mirrored",
                }
            ]
        }
        self._stub_connection_and_settings(monkeypatch, config=plain_config)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert "AGNES_SP_EXCLUDED_SUBTREE_IDS" not in calls[0]["env"]

    def test_include_excluded_subtrees_override_omits_that_scope_from_the_map(self, monkeypatch):
        """`should_not`'s per-subtree "include anyway" override
        (``app/api/admin_sharepoint.py``'s ``include_excluded_subtrees``) —
        the crawler must NOT be told to skip a subtree the admin explicitly
        chose to include."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(self._ENABLED_CONFIG))
        overridden_config = {
            "scopes": [
                {
                    **self._EXCLUDED_SUBTREE_CONFIG["scopes"][0],
                    "include_excluded_subtrees": True,
                },
            ]
        }
        self._stub_connection_and_settings(monkeypatch, config=overridden_config)
        calls = self._run_capturing_env(monkeypatch)
        handler = self._register()

        handler({"connection_id": "conn1"})

        assert "AGNES_SP_EXCLUDED_SUBTREE_IDS" not in calls[0]["env"]


class TestJiraWebhookEnqueues:
    """The Jira incremental-transform path must enqueue a ``jira-refresh``
    job instead of calling ``SyncOrchestrator().rebuild_source`` inline.
    """

    def test_trigger_incremental_transform_enqueues_not_inline(self, jobs_db, monkeypatch):
        from connectors.jira.service import trigger_incremental_transform

        # Fail loudly if anything still calls the orchestrator inline.
        class ExplodingOrchestrator:
            def rebuild_source(self, name):  # pragma: no cover - must not be hit
                raise AssertionError("SyncOrchestrator().rebuild_source called inline; expected enqueue instead")

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", ExplodingOrchestrator)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            # **kwargs absorbs the raw_dir/output_dir the caller now passes. These
            # cases are about the enqueue behaviour, not the paths — those are pinned
            # in connectors/jira/tests/test_webhook_transform_paths.py, and a stub
            # narrower than the real signature turns any signature change into four
            # failures here that say nothing about enqueuing.
            lambda issue_key, deleted=False, **kwargs: True,
        )

        result = trigger_incremental_transform("KSP-1", deleted=False)

        assert result is True

        from src.repositories import jobs_repo

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == "jira-refresh"

    def test_second_webhook_dedups_via_idempotency_key(self, jobs_db, monkeypatch):
        """Two webhook events before the job runs must not queue two
        rebuilds — enqueue()'s idempotency dedup collapses them."""
        from connectors.jira.service import trigger_incremental_transform

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            # **kwargs absorbs the raw_dir/output_dir the caller now passes. These
            # cases are about the enqueue behaviour, not the paths — those are pinned
            # in connectors/jira/tests/test_webhook_transform_paths.py, and a stub
            # narrower than the real signature turns any signature change into four
            # failures here that say nothing about enqueuing.
            lambda issue_key, deleted=False, **kwargs: True,
        )

        trigger_incremental_transform("KSP-1", deleted=False)
        trigger_incremental_transform("KSP-2", deleted=False)

        from src.repositories import jobs_repo

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 1

    def test_webhook_during_running_refresh_enqueues_coalescing_followup(self, jobs_db, monkeypatch):
        """A webhook whose parquet write lands while a jira-refresh job is
        already RUNNING must not be dropped: that running job may have
        started (and read parquet) before this write, so the dedup above
        (which matches 'queued' or 'running') would otherwise silently
        swallow it. A follow-up job (distinct idempotency key) must be
        queued to guarantee a rebuild strictly after this write."""
        from connectors.jira.service import trigger_incremental_transform
        from src.repositories import jobs_repo

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            # **kwargs absorbs the raw_dir/output_dir the caller now passes. These
            # cases are about the enqueue behaviour, not the paths — those are pinned
            # in connectors/jira/tests/test_webhook_transform_paths.py, and a stub
            # narrower than the real signature turns any signature change into four
            # failures here that say nothing about enqueuing.
            lambda issue_key, deleted=False, **kwargs: True,
        )

        # Simulate a jira-refresh job already RUNNING (e.g. claimed by the
        # worker before this webhook's parquet write landed).
        jobs_repo().enqueue("jira-refresh", {}, idempotency_key="jira-refresh")
        claimed = jobs_repo().claim_next(kinds=["jira-refresh"], worker_id="test-worker")
        assert claimed is not None and claimed["status"] == "running"

        trigger_incremental_transform("KSP-1", deleted=False)

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 2
        by_key = {r["idempotency_key"]: r for r in rows}
        assert by_key["jira-refresh"]["status"] == "running"
        assert by_key["jira-refresh-followup"]["status"] == "queued"

    def test_second_webhook_mid_run_dedups_onto_followup(self, jobs_db, monkeypatch):
        """A second webhook while the primary is still running must dedup
        onto the same follow-up row, not create a third."""
        from connectors.jira.service import trigger_incremental_transform
        from src.repositories import jobs_repo

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            # **kwargs absorbs the raw_dir/output_dir the caller now passes. These
            # cases are about the enqueue behaviour, not the paths — those are pinned
            # in connectors/jira/tests/test_webhook_transform_paths.py, and a stub
            # narrower than the real signature turns any signature change into four
            # failures here that say nothing about enqueuing.
            lambda issue_key, deleted=False, **kwargs: True,
        )

        jobs_repo().enqueue("jira-refresh", {}, idempotency_key="jira-refresh")
        jobs_repo().claim_next(kinds=["jira-refresh"], worker_id="test-worker")

        trigger_incremental_transform("KSP-1", deleted=False)
        trigger_incremental_transform("KSP-2", deleted=False)

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 2  # still exactly 1 running + 1 queued follow-up
