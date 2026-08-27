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
        from app.worker.registry import HEAVY_LANE, JOB_KINDS, LIGHT_LANE

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

    def test_idempotent_reregistration(self):
        """Calling register_all_kinds() twice (e.g. test re-imports, or a
        future re-init path) must not raise or duplicate entries."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        register_all_kinds()

        assert len(JOB_KINDS) == len(self._ALWAYS_REGISTERED)


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
            lambda tables=None, source_type_filter=None, result_sink=None: calls.append(
                (tables, source_type_filter)
            ),
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
            lambda tables=None, source_type_filter=None, result_sink=None: calls.append(
                (tables, source_type_filter)
            ),
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
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None, result_sink=None: False,
        )

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
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None, result_sink=None: run_sync_result,
        )

        JOB_KINDS["data-refresh"].handler({})  # must not raise

    def test_returns_result_sink_populated_by_run_sync(self, monkeypatch):
        """#1620: the handler's return value is what `app/worker/runtime.py`
        passes to `JobsRepository.complete(..., result=...)` — it must be
        the exact dict `_run_sync` filled via `result_sink`, not `None`,
        so a job that "succeeded" but silently skipped every table (the
        reported bug) is diagnosable via `GET /api/jobs/{id}`."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        fake_summary = {
            "materialized": {"materialized": [], "skipped": [{"table": "t1", "reason": "due_check"}], "errors": []},
            "errors": [],
            "synced_tables": [],
        }

        def _fake_run_sync(tables=None, source_type_filter=None, result_sink=None):
            if result_sink is not None:
                result_sink.update(fake_summary)
            return True

        monkeypatch.setattr("app.api.sync._run_sync", _fake_run_sync)

        result = JOB_KINDS["data-refresh"].handler({})

        assert result == fake_summary

    def test_returns_none_when_run_sync_is_a_noop(self, monkeypatch):
        """`_run_sync` returning `None` (lock-contention no-op) never
        populates `result_sink` — the handler must return `None`, not an
        empty dict, so `complete()`'s `result is not None` branch (which
        writes `payload_json["result"]`) is skipped for a call that did
        nothing."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None, result_sink=None: None,
        )

        result = JOB_KINDS["data-refresh"].handler({})

        assert result is None


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
