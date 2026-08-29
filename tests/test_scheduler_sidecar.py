"""Unit tests for the env-driven JOBS builder in services.scheduler."""

import pytest


def test_build_jobs_uses_documented_defaults(monkeypatch):
    """No env overrides → default cadences."""
    for v in (
        "SCHEDULER_DATA_REFRESH_INTERVAL",
        "SCHEDULER_HEALTH_CHECK_INTERVAL",
        "SCHEDULER_TICK_SECONDS",
        "SCHEDULER_SCRIPT_RUN_INTERVAL",
    ):
        monkeypatch.delenv(v, raising=False)
    from services.scheduler.__main__ import build_jobs, resolved_tick_seconds

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["data-refresh"] == "every 15m"
    assert jobs["health-check"] == "every 5m"
    assert jobs["script-runner"] == "every 1m"
    assert jobs["marketplaces"] == "daily 03:00"
    assert jobs["bq-metadata-refresh"] == "every 4h"
    assert jobs["knowledge-packaging"] == "every 15m"
    assert jobs["knowledge-digests"] == "every 30m"
    # Weekly skill-lint retro-audit (#687) — Monday 05:00 UTC.
    assert jobs["store-lint-audit"] == "cron 0 5 * * 1"
    # It must POST the self-guarded admin audit endpoint.
    lint_job = next(j for j in build_jobs() if j[0] == "store-lint-audit")
    assert lint_job[2] == "/api/admin/store/lint-audit"
    assert lint_job[3] == "POST"
    assert resolved_tick_seconds() == 30


def test_build_jobs_honors_bq_metadata_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_BQ_METADATA_REFRESH_INTERVAL", "7200")  # 2h
    from services.scheduler.__main__ import build_jobs

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["bq-metadata-refresh"] == "every 2h"


def test_build_jobs_honors_knowledge_packaging_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_KNOWLEDGE_PACKAGING_INTERVAL", "1800")  # 30m
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "knowledge-packaging")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 30m"
    assert endpoint == "/api/admin/run-knowledge-packaging"
    assert method == "POST"
    assert timeout == 600


def test_build_jobs_honors_knowledge_digests_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_KNOWLEDGE_DIGESTS_INTERVAL", "3600")  # 1h
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "knowledge-digests")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 1h"
    assert endpoint == "/api/admin/run-knowledge-digests"
    assert method == "POST"
    assert timeout == 900


def test_data_app_reaper_job_present(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_APP_REAP_INTERVAL", "300")
    from services.scheduler.__main__ import build_jobs

    names = [j[0] for j in build_jobs()]
    assert "data-app-idle-reaper" in names


def test_agent_schedules_run_due_job_present_by_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_AGENT_SCHEDULES", raising=False)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "agents:run-due")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 1m"
    assert endpoint == "/api/v1/agents/run-due"
    assert method == "POST"


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE"])
def test_agent_schedules_run_due_job_omitted_when_disabled(monkeypatch, value):
    monkeypatch.setenv("SCHEDULER_AGENT_SCHEDULES", value)
    from services.scheduler.__main__ import build_jobs

    names = [j[0] for j in build_jobs()]
    assert "agents:run-due" not in names


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything-else"])
def test_agent_schedules_run_due_job_present_when_explicitly_enabled(monkeypatch, value):
    monkeypatch.setenv("SCHEDULER_AGENT_SCHEDULES", value)
    from services.scheduler.__main__ import build_jobs

    names = [j[0] for j in build_jobs()]
    assert "agents:run-due" in names


def test_resolved_startup_grace_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_STARTUP_GRACE_SECONDS", raising=False)
    from services.scheduler.__main__ import resolved_startup_grace_seconds

    assert resolved_startup_grace_seconds() == 60


def test_resolved_startup_grace_zero_is_valid(monkeypatch):
    """0 means "disable" — useful for unit tests / fast dev iterations."""
    monkeypatch.setenv("SCHEDULER_STARTUP_GRACE_SECONDS", "0")
    from services.scheduler.__main__ import resolved_startup_grace_seconds

    assert resolved_startup_grace_seconds() == 0


def test_resolved_startup_grace_rejects_negative(monkeypatch):
    monkeypatch.setenv("SCHEDULER_STARTUP_GRACE_SECONDS", "-1")
    from services.scheduler.__main__ import resolved_startup_grace_seconds

    with pytest.raises(ValueError):
        resolved_startup_grace_seconds()


def test_resolved_startup_grace_rejects_empty(monkeypatch):
    """Empty string is operator typo, not 'use default' — fail fast."""
    monkeypatch.setenv("SCHEDULER_STARTUP_GRACE_SECONDS", "")
    from services.scheduler.__main__ import resolved_startup_grace_seconds

    with pytest.raises(ValueError):
        resolved_startup_grace_seconds()


def test_bq_metadata_initial_offset_within_cap(monkeypatch):
    """Default cap is 900s. With a fixed RNG, the offset is deterministic
    and bounded."""
    monkeypatch.delenv("SCHEDULER_BQ_METADATA_INITIAL_OFFSET_MAX_SECONDS", raising=False)
    import random
    from services.scheduler.__main__ import resolved_bq_metadata_initial_offset_seconds

    rng = random.Random(42)  # deterministic
    val = resolved_bq_metadata_initial_offset_seconds(rng=rng)
    assert 0 <= val <= 900


def test_bq_metadata_initial_offset_zero_cap_returns_zero(monkeypatch):
    """Operator opt-out: setting cap to 0 disables the jitter."""
    monkeypatch.setenv("SCHEDULER_BQ_METADATA_INITIAL_OFFSET_MAX_SECONDS", "0")
    from services.scheduler.__main__ import resolved_bq_metadata_initial_offset_seconds

    assert resolved_bq_metadata_initial_offset_seconds() == 0


def test_bq_metadata_initial_offset_honors_custom_cap(monkeypatch):
    monkeypatch.setenv("SCHEDULER_BQ_METADATA_INITIAL_OFFSET_MAX_SECONDS", "60")
    import random
    from services.scheduler.__main__ import resolved_bq_metadata_initial_offset_seconds

    # Loop a few times since RNG could legitimately return 60.
    for seed in range(20):
        val = resolved_bq_metadata_initial_offset_seconds(rng=random.Random(seed))
        assert 0 <= val <= 60


def test_build_jobs_honors_env_overrides(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_REFRESH_INTERVAL", "1800")  # 30m
    monkeypatch.setenv("SCHEDULER_HEALTH_CHECK_INTERVAL", "60")  # 1m
    monkeypatch.setenv("SCHEDULER_SCRIPT_RUN_INTERVAL", "120")  # 2m
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "10")
    from services.scheduler.__main__ import build_jobs, resolved_tick_seconds

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["data-refresh"] == "every 30m"
    assert jobs["health-check"] == "every 1m"
    assert jobs["script-runner"] == "every 2m"
    assert resolved_tick_seconds() == 10


@pytest.mark.parametrize(
    "var",
    [
        "SCHEDULER_DATA_REFRESH_INTERVAL",
        "SCHEDULER_HEALTH_CHECK_INTERVAL",
        "SCHEDULER_TICK_SECONDS",
        "SCHEDULER_SCRIPT_RUN_INTERVAL",
    ],
)
@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_build_jobs_rejects_invalid_env(monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    from services.scheduler.__main__ import build_jobs

    with pytest.raises(ValueError):
        build_jobs()


def test_build_jobs_rejects_tick_larger_than_smallest_interval(monkeypatch):
    """Tick must be <= the smallest job interval, otherwise jobs would
    consistently miss their cadence by up to one tick."""
    monkeypatch.setenv("SCHEDULER_HEALTH_CHECK_INTERVAL", "60")
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "120")
    from services.scheduler.__main__ import build_jobs

    with pytest.raises(ValueError, match="tick"):
        build_jobs()


def test_build_jobs_includes_run_due_endpoint():
    """The script-runner job must POST to /api/scripts/run-due."""
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "script-runner")
    name, schedule, endpoint, method, _timeout = target
    assert endpoint == "/api/scripts/run-due"
    assert method == "POST"


def test_build_jobs_includes_semantic_auto_draft_sweep():
    """semantic-phase5 wave 2: the sweep row must POST every 55 minutes to
    the sweep endpoint, direct-synchronous shape (5-tuple, no enqueue body)."""
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "semantic-auto-draft-sweep")
    assert len(target) == 5
    name, schedule, endpoint, method, timeout_sec = target
    assert schedule == "every 55m"
    assert endpoint == "/api/admin/semantic-auto-draft-sweep"
    assert method == "POST"
    assert timeout_sec == 60


@pytest.mark.parametrize(
    "seconds,expected",
    [
        # Exact multiples of 60 → unchanged.
        (60, "every 1m"),
        (120, "every 2m"),
        (900, "every 15m"),
        # Exact multiples of 3600 → hour form.
        (3600, "every 1h"),
        (7200, "every 2h"),
        # Non-multiples of 60 must round UP (ceiling), so the job never fires
        # MORE often than the operator configured. Devin BUG_0001 on 1af2081.
        (90, "every 2m"),  # 90s asked → 120s scheduled, NOT 60s
        (150, "every 3m"),
        (61, "every 2m"),
        (3601, "every 61m"),
        # Sub-minute clamps to 1m (schedule grammar minute-grained).
        (30, "every 1m"),
        (1, "every 1m"),
    ],
)
def test_seconds_to_schedule_rounds_up_not_down(seconds, expected):
    from services.scheduler.__main__ import _seconds_to_schedule

    assert _seconds_to_schedule(seconds) == expected, (
        f"_seconds_to_schedule({seconds}) must round UP — flooring would "
        f"make jobs fire more often than the operator configured."
    )


# ── Verification-detector off-peak schedule override ──


def _verify_env_clean(monkeypatch):
    monkeypatch.delenv("SCHEDULER_VERIFICATION_SCHEDULE", raising=False)


def test_verification_schedule_defaults_to_interval_derived(monkeypatch):
    _verify_env_clean(monkeypatch)
    from services.scheduler.__main__ import _verification_schedule

    assert _verification_schedule(3600) == "every 1h"
    assert _verification_schedule(900) == "every 15m"


def test_verification_schedule_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_VERIFICATION_SCHEDULE", "daily 04:15")
    from services.scheduler.__main__ import _verification_schedule

    assert _verification_schedule(3600) == "daily 04:15"


def test_verification_schedule_garbage_override_falls_back(monkeypatch):
    """A typo in the override must not crash build_jobs() nor silently
    produce an unparseable schedule — fall back to the interval-derived one."""
    monkeypatch.setenv("SCHEDULER_VERIFICATION_SCHEDULE", "not-a-schedule")
    from services.scheduler.__main__ import _verification_schedule

    assert _verification_schedule(3600) == "every 1h"


def test_build_jobs_verification_uses_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_VERIFICATION_SCHEDULE", "daily 04:15")
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "session-processor:verification")
    assert target[1] == "daily 04:15"
    assert target[2] == "/api/admin/run-session-processor?processor=verification"


def test_build_jobs_verification_default_unaffected_by_unset_override(monkeypatch):
    _verify_env_clean(monkeypatch)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "session-processor:verification")
    # Default SCHEDULER_VERIFICATION_DETECTOR_INTERVAL is 15m.
    assert target[1] == "every 15m"


def test_build_jobs_tick_guard_ignores_daily_verification_schedule(monkeypatch):
    """A `daily …` verification override must not trip the tick<=smallest-
    interval guard — same treatment as the initial-workspace daily job."""
    monkeypatch.setenv("SCHEDULER_VERIFICATION_SCHEDULE", "daily 04:15")
    from services.scheduler.__main__ import build_jobs

    jobs = build_jobs()  # must not raise
    assert any(j[0] == "session-processor:verification" for j in jobs)


# ── Initial Workspace Template nightly auto-sync (#622 Slice 3 PR-B) ──


def _iw_env_clean(monkeypatch):
    monkeypatch.delenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", raising=False)
    # No instance.yaml on the test box → get_value returns default "".


def test_build_jobs_includes_initial_workspace_default(monkeypatch):
    """Default IW job: daily 03:30, sync-if-configured endpoint, POST, 900s."""
    _iw_env_clean(monkeypatch)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "initial-workspace")
    name, schedule, endpoint, method, timeout = target
    assert schedule == "daily 03:30"
    assert endpoint == "/api/admin/initial-workspace/sync-if-configured"
    assert method == "POST"
    assert timeout == 900


def test_initial_workspace_schedule_offsets_from_marketplaces(monkeypatch):
    """The IW default must NOT collide with the marketplaces job (daily 03:00)
    so the two nightly git-clone bursts don't stack."""
    _iw_env_clean(monkeypatch)
    from services.scheduler.__main__ import build_jobs

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["initial-workspace"] != jobs["marketplaces"]
    assert jobs["initial-workspace"] != "daily 03:00"


def test_initial_workspace_schedule_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", "daily 05:15")
    from services.scheduler.__main__ import build_jobs, _iw_sync_schedule

    assert _iw_sync_schedule() == "daily 05:15"
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["initial-workspace"] == "daily 05:15"


def test_initial_workspace_schedule_garbage_falls_back(monkeypatch):
    """A typo in the env override must NOT crash build_jobs nor silently
    produce an unparseable schedule — fall back to the documented default."""
    monkeypatch.setenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", "not-a-schedule")
    from services.scheduler.__main__ import build_jobs, _iw_sync_schedule

    assert _iw_sync_schedule() == "daily 03:30"
    # And build_jobs() must not raise on the (daily) fallback form.
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["initial-workspace"] == "daily 03:30"


def test_build_jobs_tick_guard_ignores_daily_iw_schedule(monkeypatch):
    """`daily …` jobs aren't part of the tick<=smallest-interval guard (which
    only inspects `every Nm/Nh` env-driven intervals). Adding the IW daily job
    must not make build_jobs() raise even with a large tick relative to it."""
    _iw_env_clean(monkeypatch)
    # Keep the interval jobs comfortably above a default tick so the guard
    # itself doesn't trip for unrelated reasons.
    from services.scheduler.__main__ import build_jobs

    jobs = build_jobs()  # must not raise
    assert any(j[0] == "initial-workspace" for j in jobs)


def test_iw_sync_schedule_disabled_when_explicitly_cleared(monkeypatch):
    """Admin cleared the schedule (overlay stores ``sync_schedule: ""``):
    auto-sync is disabled and build_jobs() omits the initial-workspace job
    entirely — the documented "leave empty to disable" contract (#622 Slice 3
    PR-B review). Previously the scheduler always fell back to daily 03:30, so
    the disable path was unreachable."""
    monkeypatch.delenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", raising=False)
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: "" if keys == ("initial_workspace", "sync_schedule") else default,
    )
    from services.scheduler.__main__ import _iw_sync_schedule, build_jobs

    assert _iw_sync_schedule() is None
    assert "initial-workspace" not in {j[0] for j in build_jobs()}


def test_iw_sync_schedule_default_when_absent_or_null(monkeypatch):
    """Key absent or YAML null → "never configured" → daily default, so
    existing instances keep auto-sync on. get_value collapses both an absent
    key and a null value to its sentinel default; only an explicit "" disables."""
    monkeypatch.delenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", raising=False)
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
    from services.scheduler.__main__ import _iw_sync_schedule, build_jobs

    assert _iw_sync_schedule() == "daily 03:30"
    assert "initial-workspace" in {j[0] for j in build_jobs()}


def test_iw_sync_schedule_yaml_value_honored(monkeypatch):
    """A valid YAML schedule (no env override) is used verbatim."""
    monkeypatch.delenv("SCHEDULER_INITIAL_WORKSPACE_SCHEDULE", raising=False)
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: "every 6h" if keys == ("initial_workspace", "sync_schedule") else default,
    )
    from services.scheduler.__main__ import _iw_sync_schedule, build_jobs

    assert _iw_sync_schedule() == "every 6h"
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["initial-workspace"] == "every 6h"


def test_scheduler_audit_actions_include_initial_workspace_sync():
    """The /admin/activity scheduler filter must surface the nightly IW sync
    audit rows (#622 Slice 3 PR-B review — _do_sync writes these actions)."""
    from app.web.router import SCHEDULER_AUDIT_ACTIONS

    assert "initial_workspace.sync" in SCHEDULER_AUDIT_ACTIONS
    assert "initial_workspace.sync_failed" in SCHEDULER_AUDIT_ACTIONS


@pytest.mark.parametrize("good", ["daily 03:30", "every 30m", "every 6h", "daily 03:00,15:00"])
def test_is_valid_schedule_accepts_known_grammar(good):
    from src.scheduler import is_valid_schedule

    assert is_valid_schedule(good) is True


@pytest.mark.parametrize("bad", ["", "garbage", "daily 25:00", "daily 3:30", "weekly"])
def test_is_valid_schedule_rejects_bad(bad):
    from src.scheduler import is_valid_schedule

    assert is_valid_schedule(bad) is False


def test_build_jobs_includes_keboola_semantic_layer_refresh_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL", raising=False)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "keboola-semantic-layer-refresh")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 6h"
    assert endpoint == "/api/admin/run-keboola-semantic-layer-refresh"
    assert method == "POST"
    assert timeout == 900


def test_build_jobs_honors_keboola_semantic_layer_refresh_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL", "3600")  # 1h
    from services.scheduler.__main__ import build_jobs

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["keboola-semantic-layer-refresh"] == "every 1h"


def test_build_jobs_includes_semantic_sources_refresh_default(monkeypatch):
    """Block 3 step 2 of #1707 — the ONE generic scheduled refresh over
    registered `semantic_sources`, distinct from (and running alongside)
    the legacy Keboola/Databricks refreshes above."""
    monkeypatch.delenv("SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL", raising=False)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "semantic-sources-refresh")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 6h"
    assert endpoint == "/api/admin/run-semantic-sources-refresh"
    assert method == "POST"
    assert timeout == 900


def test_build_jobs_honors_semantic_sources_refresh_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL", "3600")  # 1h
    from services.scheduler.__main__ import build_jobs

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["semantic-sources-refresh"] == "every 1h"


# ---------------------------------------------------------------------------
# Wave-2B job-queue migration (Task 6): data-refresh, marketplaces,
# session-collector, corporate-memory now enqueue via POST /api/jobs
# instead of calling their old synchronous endpoint. Every other row must
# be untouched — see docs/jobs-classification.md for the full breakdown.
# ---------------------------------------------------------------------------


class TestEnqueueMigratedJobs:
    """These rows must target /api/jobs with the documented kind + fixed
    idempotency_key body (the original four wave-2B rows, plus wave-2G
    Task 5's ``ducklake-maintenance``)."""

    @pytest.mark.parametrize(
        "name,expected_body",
        [
            ("data-refresh", {"kind": "data-refresh", "idempotency_key": "sync"}),
            (
                "marketplaces",
                {"kind": "marketplaces-sync", "idempotency_key": "marketplaces-sync"},
            ),
            (
                "session-collector",
                {"kind": "session-collector", "idempotency_key": "session-collector"},
            ),
            (
                "corporate-memory",
                {"kind": "corporate-memory", "idempotency_key": "corporate-memory"},
            ),
            (
                "ducklake-maintenance",
                {"kind": "ducklake-maintenance", "idempotency_key": "ducklake-maintenance"},
            ),
        ],
    )
    def test_migrated_row_posts_to_jobs_queue(self, name, expected_body):
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == name)
        _, _schedule, endpoint, method, _timeout, json_body = target
        assert endpoint == "/api/jobs"
        assert method == "POST"
        assert json_body == expected_body

    @pytest.mark.parametrize(
        "name",
        [
            "data-refresh",
            "marketplaces",
            "session-collector",
            "corporate-memory",
            "ducklake-maintenance",
            "jira-org-refresh",
        ],
    )
    def test_migrated_row_uses_short_enqueue_timeout(self, name):
        """Enqueueing just inserts a row and returns 202 — the work no
        longer runs inside this HTTP call, so the old long per-job timeouts
        (120s-900s) are replaced with a short one."""
        from services.scheduler.__main__ import build_jobs

        target = next(j for j in build_jobs() if j[0] == name)
        assert target[4] == 30

    def test_migrated_rows_are_6_tuples_others_stay_5_tuples(self):
        """Only the migrated jobs carry a 6th (json_body) element; every
        other row keeps its original 5-tuple shape."""
        from services.scheduler.__main__ import build_jobs

        migrated = {
            "data-refresh",
            "marketplaces",
            "session-collector",
            "corporate-memory",
            "ducklake-maintenance",
            "jira-org-refresh",
        }
        for j in build_jobs():
            if j[0] in migrated:
                assert len(j) == 6, f"{j[0]} must be a 6-tuple (with json_body)"
            else:
                assert len(j) == 5, f"{j[0]} must stay a 5-tuple (unmigrated)"

    def test_call_api_forwards_json_body_to_httpx_post(self, monkeypatch):
        """_call_api must actually send the enqueue body as the request's
        JSON payload, not just carry it around in the tuple."""
        from services.scheduler import __main__ as sched

        captured = {}

        class _FakeResp:
            status_code = 202
            text = ""

        def _fake_post(url, headers=None, timeout=None, json=None):
            captured["json"] = json
            return _FakeResp()

        monkeypatch.setattr(sched.httpx, "post", _fake_post)
        ok = sched._call_api("/api/jobs", "POST", 30, json_body={"kind": "data-refresh", "idempotency_key": "sync"})
        assert ok is True
        assert captured["json"] == {"kind": "data-refresh", "idempotency_key": "sync"}

    def test_call_api_sends_no_body_when_json_body_omitted(self, monkeypatch):
        """Non-migrated jobs must keep sending no request body — passing
        json=None to httpx.post is a no-op, matching pre-change behavior."""
        from services.scheduler import __main__ as sched

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""

        def _fake_post(url, headers=None, timeout=None, json=None):
            captured["json"] = json
            return _FakeResp()

        monkeypatch.setattr(sched.httpx, "post", _fake_post)
        ok = sched._call_api("/api/admin/run-blocked-purge", "POST", 600)
        assert ok is True
        assert captured["json"] is None

    def test_run_job_forwards_json_body_to_call_api(self, monkeypatch):
        """_run_job (invoked per-tick by the executor) must pass its
        json_body argument through to _call_api unchanged."""
        import threading

        from services.scheduler import __main__ as sched

        captured = {}

        def _fake_call_api(endpoint, method, timeout, json_body=None):
            captured["json_body"] = json_body
            return True

        monkeypatch.setattr(sched, "_call_api", _fake_call_api)
        last_run: dict[str, str | None] = {"data-refresh": None}
        in_flight: set[str] = {"data-refresh"}
        sched._run_job(
            "data-refresh",
            "/api/jobs",
            "POST",
            30,
            "2026-01-01T00:00:00",
            last_run,
            in_flight,
            threading.Lock(),
            json_body={"kind": "data-refresh", "idempotency_key": "sync"},
        )
        assert captured["json_body"] == {"kind": "data-refresh", "idempotency_key": "sync"}


class TestUnmigratedRowsUnchanged:
    """Snapshot-style guard: every scheduler row NOT in the wave-2B migrated
    set must keep its pre-existing (endpoint, method) target. A failure here
    means either an unintended migration or an unrelated endpoint edit —
    either way, review it."""

    _EXPECTED_UNMIGRATED = {
        "health-check": ("/api/health", "GET"),
        "script-runner": ("/api/scripts/run-due", "POST"),
        "session-processor:verification": (
            "/api/admin/run-session-processor?processor=verification",
            "POST",
        ),
        "session-processor:usage": (
            "/api/admin/run-session-processor?processor=usage",
            "POST",
        ),
        "store-blocked-purge": ("/api/admin/run-blocked-purge", "POST"),
        "store-reap-stuck-reviews": ("/api/admin/run-reap-stuck-reviews", "POST"),
        "store-lint-audit": ("/api/admin/store/lint-audit", "POST"),
        "bq-metadata-refresh": ("/api/admin/run-bq-metadata-refresh", "POST"),
        "keboola-semantic-layer-refresh": (
            "/api/admin/run-keboola-semantic-layer-refresh",
            "POST",
        ),
        "usage-prune": ("/api/admin/usage/prune", "POST"),
        "jira-sla-poll": ("/api/admin/run-jira-sla-poll", "POST"),
        "jira-consistency-check": ("/api/admin/run-jira-consistency-check", "POST"),
        "knowledge-packaging": ("/api/admin/run-knowledge-packaging", "POST"),
        "knowledge-digests": ("/api/admin/run-knowledge-digests", "POST"),
        "initial-workspace": ("/api/admin/initial-workspace/sync-if-configured", "POST"),
        "data-app-idle-reaper": ("/api/data-apps/reap-idle", "POST"),
    }

    def test_unmigrated_rows_endpoint_and_method_unchanged(self):
        from services.scheduler.__main__ import build_jobs

        jobs = {j[0]: (j[2], j[3]) for j in build_jobs()}
        for name, expected in self._EXPECTED_UNMIGRATED.items():
            assert jobs[name] == expected, f"{name} endpoint/method drifted: {jobs[name]!r} != {expected!r}"


# ── Document extraction sweep (TCRD-226) ──


def _extraction_env_clean(monkeypatch):
    monkeypatch.delenv("SCHEDULER_EXTRACTION_SCHEDULE", raising=False)
    # No instance.yaml on the test box → get_value returns default "".


def test_extraction_schedule_defaults_to_disabled(monkeypatch):
    """Off by default — unlike initial-workspace, there is no sensible
    fallback cadence, so absent config means no scheduler row at all."""
    _extraction_env_clean(monkeypatch)
    from services.scheduler.__main__ import _extraction_schedule, build_jobs

    assert _extraction_schedule() is None
    assert "extraction-run-due" not in {j[0] for j in build_jobs()}


def test_extraction_schedule_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_EXTRACTION_SCHEDULE", "daily 02:00")
    from services.scheduler.__main__ import _extraction_schedule, build_jobs

    assert _extraction_schedule() == "daily 02:00"
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["extraction-run-due"] == "daily 02:00"


def test_extraction_schedule_garbage_env_falls_back_to_disabled(monkeypatch):
    """A typo in the override must not crash build_jobs() nor silently
    produce an unparseable schedule — falls back to disabled (no sensible
    default cadence to guess, unlike initial-workspace's daily 03:30)."""
    monkeypatch.setenv("SCHEDULER_EXTRACTION_SCHEDULE", "not-a-schedule")
    from services.scheduler.__main__ import _extraction_schedule, build_jobs

    assert _extraction_schedule() is None
    assert "extraction-run-due" not in {j[0] for j in build_jobs()}


def test_extraction_schedule_yaml_value_honored(monkeypatch):
    _extraction_env_clean(monkeypatch)
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: "every 6h" if keys == ("extraction", "schedule") else default,
    )
    from services.scheduler.__main__ import _extraction_schedule, build_jobs

    assert _extraction_schedule() == "every 6h"
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["extraction-run-due"] == "every 6h"


def test_build_jobs_includes_extraction_run_due_endpoint(monkeypatch):
    monkeypatch.setenv("SCHEDULER_EXTRACTION_SCHEDULE", "every 4h")
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "extraction-run-due")
    name, schedule, endpoint, method, _timeout = target
    assert endpoint == "/api/admin/sharepoint/extraction/run-due"
    assert method == "POST"


def test_build_jobs_tick_guard_ignores_extraction_daily_schedule(monkeypatch):
    """A `daily …` extraction schedule isn't part of the tick<=smallest-
    interval guard — same treatment as initial-workspace/jira-org-refresh."""
    monkeypatch.setenv("SCHEDULER_EXTRACTION_SCHEDULE", "daily 02:00")
    from services.scheduler.__main__ import build_jobs

    jobs = build_jobs()  # must not raise
    assert any(j[0] == "extraction-run-due" for j in jobs)
