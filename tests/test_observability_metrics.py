"""Tests for the Prometheus `/metrics` endpoint (three-plane wave 2D, task 1).

Named distinctly from `tests/test_metrics.py` — that pre-existing file covers
the unrelated business `MetricRepository` (`metric_definitions` table), not
this Prometheus scrape endpoint.

`app.observability.metrics` holds process-wide singletons (a dedicated
`CollectorRegistry` + the `Counter`/`Histogram` registered on it) — like the
module import itself, they persist for the life of the test process across
every `create_app()` call. Tests therefore assert *deltas* around a request
(or "this label never appears"/"at most one new label appears"), never
absolute counter values, since other tests in the same session may already
have recorded requests against the same series.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")

    app = shared_app
    return TestClient(app)


def _samples(metric_name):
    """Yield every prometheus_client Sample for `metric_name` off the dedicated REGISTRY."""
    from app.observability.metrics import REGISTRY

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == metric_name:
                yield sample


def _counter_value(metric_name, **labels):
    for sample in _samples(metric_name):
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return None


def _path_templates(metric_name):
    return {sample.labels.get("path_template") for sample in _samples(metric_name)}


class TestMetricsEndpoint:
    def test_metrics_returns_200_text_plain_with_http_requests_total(self, app_client):
        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "agnes_http_requests_total" in r.text

    def test_metrics_endpoint_is_unauthenticated(self, app_client):
        # No Authorization header at all — /metrics must not 401/403 like the
        # /api/* surface does (it's a scrape endpoint, like /healthz).
        r = app_client.get("/metrics")
        assert r.status_code == 200

    def test_request_increments_counter(self, app_client):
        from app.observability.metrics import replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/healthz",
                status="200",
                replica=replica_id(),
            )
            or 0.0
        )

        r = app_client.get("/healthz")
        assert r.status_code == 200

        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/healthz",
            status="200",
            replica=replica_id(),
        )
        assert after == before + 1.0

    def test_role_and_replica_labels_present(self, app_client):
        from app.observability.metrics import replica_id

        app_client.get("/healthz")
        sample = next(
            s
            for s in _samples("agnes_http_requests_total")
            if s.labels.get("path_template") == "/healthz" and s.labels.get("method") == "GET"
        )
        assert sample.labels.get("replica") == replica_id()
        assert sample.labels.get("role")  # non-empty

    def test_scraping_metrics_itself_is_not_recorded(self, app_client):
        # Skip-recording rule: /metrics must not show up as a path_template
        # label on its own series (would grow unboundedly under scraping).
        app_client.get("/metrics")
        app_client.get("/metrics")
        assert "/metrics" not in _path_templates("agnes_http_requests_total")

    def test_path_template_used_not_raw_path(self, app_client):
        # /cli/wheel/{wheel_name} always 404s in a fresh test DATA_DIR (no
        # wheel on disk) regardless of the filename requested — hitting it
        # with two very different filenames must collapse onto ONE
        # path_template label ("/cli/wheel/{wheel_name}"), not two distinct
        # raw-path labels. That's the cardinality guard the brief calls out.
        #
        # Like test_job_duration_histogram_has_hours_scale_bucket, this test
        # does not depend on cross-test ordering: it records the counter value
        # before and after, then asserts on the delta. This ensures the test
        # is self-contained under pytest -n auto (pytest-xdist), where other
        # tests in the same worker may already have recorded requests to
        # /cli/wheel/{wheel_name}, polluting the REGISTRY state.
        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/cli/wheel/{wheel_name}",
                status="404",
            )
            or 0.0
        )

        r1 = app_client.get("/cli/wheel/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.whl")
        r2 = app_client.get("/cli/wheel/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.whl")
        assert r1.status_code == 404
        assert r2.status_code == 404

        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/cli/wheel/{wheel_name}",
            status="404",
        )
        assert after == before + 2.0

        # Cardinality guard: verify no raw /cli/wheel/* paths appear as labels.
        # If raw paths leaked, we'd see path_template values like
        # "/cli/wheel/aaaaa...whl" instead of just "/cli/wheel/{wheel_name}".
        for sample in _samples("agnes_http_requests_total"):
            path = sample.labels.get("path_template")
            # A raw path would be /cli/wheel/ followed by a filename (no {})
            if path and path.startswith("/cli/wheel/") and "{" not in path:
                assert False, f"raw path must not appear as label, got: {path}"

    def test_unmatched_paths_collapse_not_leak_raw_path(self, app_client):
        # Whatever the app does with a totally bogus, high-entropy path (route
        # it to the catch-all template or the UNMATCHED_PATH constant), the
        # raw path itself must never become a label value, and two unrelated
        # bogus paths must not create two new distinct labels.
        before = _path_templates("agnes_http_requests_total")

        bogus_1 = "/totally-bogus-path-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        bogus_2 = "/totally-bogus-path-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        app_client.get(bogus_1)
        app_client.get(bogus_2)

        after = _path_templates("agnes_http_requests_total")
        assert bogus_1 not in after
        assert bogus_2 not in after
        assert len(after - before) <= 1

    def test_observe_http_helper(self):
        from app.observability.metrics import observe_http, replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/unit-test-path",
                status="200",
                replica=replica_id(),
            )
            or 0.0
        )
        observe_http("GET", "/unit-test-path", 200, 0.01)
        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/unit-test-path",
            status="200",
            replica=replica_id(),
        )
        assert after == before + 1.0

        duration_count = None
        for sample in _samples("agnes_http_request_duration_seconds_count"):
            if sample.labels.get("path_template") == "/unit-test-path":
                duration_count = sample.value
        assert duration_count is not None and duration_count >= 1.0

    def test_replica_id_is_hostname_colon_pid(self):
        import os
        import socket

        from app.observability.metrics import replica_id

        assert replica_id() == f"{socket.gethostname()}:{os.getpid()}"
        # Stable across repeated calls within the same process.
        assert replica_id() == replica_id()

    def test_registry_is_dedicated_not_the_global_default(self):
        from prometheus_client import REGISTRY as _GLOBAL_REGISTRY

        from app.observability.metrics import REGISTRY

        assert REGISTRY is not _GLOBAL_REGISTRY

    def test_500_error_recorded_when_exception_propagates(self, tmp_path, monkeypatch):
        """Verify that HTTP 500 metric is recorded even when an exception propagates
        through the middleware (rather than returning a response object).

        The outermost _observe_http_metrics middleware must record the 500 metric
        before re-raising, so that exceptions in inner middleware/handlers don't
        skip observability.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")

        # Create a minimal app with just the middleware we care about, plus a
        # test route that raises.
        app = FastAPI()

        # Import and attach the same middleware from create_app.
        import time as _time

        from app.observability.metrics import METRICS_PATH, UNMATCHED_PATH, observe_http

        @app.middleware("http")
        async def _observe_http_metrics(request, call_next):
            if request.url.path == METRICS_PATH:
                return await call_next(request)
            start = _time.monotonic()
            try:
                response = await call_next(request)
                duration = _time.monotonic() - start
                route = request.scope.get("route")
                path_template = route.path if route is not None else UNMATCHED_PATH
                observe_http(request.method, path_template, response.status_code, duration)
                return response
            except Exception:
                duration = _time.monotonic() - start
                route = request.scope.get("route")
                path_template = route.path if route is not None else UNMATCHED_PATH
                observe_http(request.method, path_template, 500, duration)
                raise

        @app.get("/test-error")
        def test_error_route():
            raise RuntimeError("Intentional test error")

        client = TestClient(app, raise_server_exceptions=False)

        from app.observability.metrics import replica_id

        before = (
            _counter_value(
                "agnes_http_requests_total",
                method="GET",
                path_template="/test-error",
                status="500",
                replica=replica_id(),
            )
            or 0.0
        )

        # Make request to the error route — should get a 500 response.
        response = client.get("/test-error")
        assert response.status_code == 500

        # Verify the 500 metric was recorded.
        after = _counter_value(
            "agnes_http_requests_total",
            method="GET",
            path_template="/test-error",
            status="500",
            replica=replica_id(),
        )
        assert after == before + 1.0


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR — jobs_repo() resolves to the
    DuckDB backend here (mirrors the ``worker_db`` fixture in
    ``tests/test_worker_runtime.py``)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


@pytest.fixture(autouse=True)
def _clean_job_kinds_registry_for_metrics_tests():
    """Isolate ``JOB_KINDS`` (a process-wide dict) across tests in this
    module too — ``_bounded_kind`` reads it to decide the ``other`` bucket,
    so a kind registered by an earlier test must not leak into this one's
    assertions."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


class TestJobQueueMetrics:
    """Task 2: job-queue + worker runtime metrics on the same dedicated
    REGISTRY (queue-depth via a scrape-time Collector, everything else via
    plain Gauge/Histogram/Counter mutated by app/worker/runtime.py)."""

    def test_queued_gauge_reflects_enqueued_jobs_by_kind(self, jobs_db):
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from src.repositories import jobs_repo

        register_kind(JobKind(name="metrics_test_kind_a", handler=lambda p: None, lane=LIGHT_LANE))
        register_kind(JobKind(name="metrics_test_kind_b", handler=lambda p: None, lane=LIGHT_LANE))

        repo = jobs_repo()
        for _ in range(3):
            repo.enqueue("metrics_test_kind_a", {})
        for _ in range(2):
            repo.enqueue("metrics_test_kind_b", {})

        assert _counter_value("agnes_jobs_queued", kind="metrics_test_kind_a") == 3.0
        assert _counter_value("agnes_jobs_queued", kind="metrics_test_kind_b") == 2.0

    def test_queued_gauge_buckets_unregistered_kind_as_other(self, jobs_db):
        from src.repositories import jobs_repo

        # No register_kind() call for this kind — JOB_KINDS is empty
        # (cleared by the autouse fixture), so it must collapse to "other"
        # rather than becoming its own unbounded label value.
        jobs_repo().enqueue("totally_unregistered_kind", {})

        assert _counter_value("agnes_jobs_queued", kind="other") == 1.0
        assert _counter_value("agnes_jobs_queued", kind="totally_unregistered_kind") is None

    def test_queued_collector_survives_jobs_repo_error(self, monkeypatch):
        """A broken jobs_repo() (e.g. DB not initialized) must not 500 the
        whole /metrics endpoint — collect() catches it, bumps
        agnes_metrics_collector_errors_total, and yields nothing for this
        metric on this scrape."""
        import src.repositories as repos_module

        from app.observability.metrics import REGISTRY

        before = 0.0
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                if (
                    sample.name == "agnes_metrics_collector_errors_total"
                    and sample.labels.get("collector") == "jobs_queued"
                ):
                    before = sample.value

        def boom():
            raise RuntimeError("db is not available")

        monkeypatch.setattr(repos_module, "jobs_repo", boom)

        # generate_latest() must not raise — this is the actual /metrics
        # code path (REGISTRY.collect() drives every registered collector,
        # including the custom queued-jobs one).
        from prometheus_client import generate_latest

        text = generate_latest(REGISTRY).decode()
        assert "agnes_http_requests_total" in text  # other metrics still present

        after = _counter_value("agnes_metrics_collector_errors_total", collector="jobs_queued")
        assert after == before + 1.0

    def test_queued_collector_via_metrics_endpoint_survives_error(self, app_client, monkeypatch):
        """Same as above, but through the real /metrics HTTP endpoint."""
        import src.repositories as repos_module

        def boom():
            raise RuntimeError("db is not available")

        monkeypatch.setattr(repos_module, "jobs_repo", boom)

        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert "agnes_http_requests_total" in r.text

    def test_queued_capped_flag_reflects_scan_cap(self, jobs_db, monkeypatch):
        from app.observability import metrics as obs_metrics
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from src.repositories import jobs_repo

        monkeypatch.setattr(obs_metrics, "_QUEUED_SCAN_LIMIT", 5)
        register_kind(JobKind(name="metrics_capped_kind", handler=lambda p: None, lane=LIGHT_LANE))
        repo = jobs_repo()
        for _ in range(8):
            repo.enqueue("metrics_capped_kind", {})

        assert _counter_value("agnes_jobs_queued_capped") == 1.0

    def test_queued_not_capped_when_under_limit(self, jobs_db, monkeypatch):
        from app.observability import metrics as obs_metrics
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from src.repositories import jobs_repo

        monkeypatch.setattr(obs_metrics, "_QUEUED_SCAN_LIMIT", 500)
        register_kind(JobKind(name="metrics_uncapped_kind", handler=lambda p: None, lane=LIGHT_LANE))
        jobs_repo().enqueue("metrics_uncapped_kind", {})

        assert _counter_value("agnes_jobs_queued_capped") == 0.0

    def test_oldest_queued_age_reflects_backdated_created_at(self, jobs_db):
        """`agnes_jobs_oldest_queued_age_seconds` = now - min(created_at) per
        kind, reusing the same bounded scan as `agnes_jobs_queued` (no
        second DB hit). Backdate one job's `created_at` directly (jobs_repo
        has no public API to control it at enqueue time) and assert the
        reported age reflects the older row, not the newer one."""
        from datetime import datetime, timedelta, timezone

        from app.worker.registry import LIGHT_LANE, JobKind, register_kind
        from src.repositories import jobs_repo

        register_kind(JobKind(name="metrics_age_kind", handler=lambda p: None, lane=LIGHT_LANE))

        repo = jobs_repo()
        older = repo.enqueue("metrics_age_kind", {})
        repo.enqueue("metrics_age_kind", {})

        backdated = datetime.now(timezone.utc) - timedelta(seconds=120)
        repo.conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", [backdated, older["id"]])

        age = _counter_value("agnes_jobs_oldest_queued_age_seconds", kind="metrics_age_kind")
        assert age is not None
        # Backdated by 120s — allow generous slack for test-runtime overhead,
        # but it must reflect the OLDER row, not the freshly-enqueued one
        # (which would read close to 0s).
        assert 120.0 <= age < 130.0

    def test_oldest_queued_age_absent_for_empty_queue(self, jobs_db):
        """No queued jobs at all -> the series carries no sample for a kind
        that was never enqueued, mirroring agnes_jobs_queued's own
        empty-queue handling (collect() adds no metric for kinds with zero
        rows rather than emitting a 0-valued series)."""
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        register_kind(JobKind(name="metrics_age_empty_kind", handler=lambda p: None, lane=LIGHT_LANE))

        assert _counter_value("agnes_jobs_queued", kind="metrics_age_empty_kind") is None
        assert _counter_value("agnes_jobs_oldest_queued_age_seconds", kind="metrics_age_empty_kind") is None

    def test_oldest_queued_age_skips_rows_with_missing_created_at(self, monkeypatch):
        """A row missing `created_at` (defensive guard — the `jobs` table
        column is NOT NULL in practice, but the collector must not crash or
        report a bogus age if a row ever comes back without one) is still
        counted toward `agnes_jobs_queued` but excluded from the age
        computation entirely."""
        import src.repositories as repos_module
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        register_kind(JobKind(name="metrics_missing_created_at_kind", handler=lambda p: None, lane=LIGHT_LANE))

        class _FakeJobsRepo:
            def list(self, *, status=None, kind=None, limit=50):
                return [
                    {"kind": "metrics_missing_created_at_kind", "created_at": None},
                    {"kind": "metrics_missing_created_at_kind", "created_at": None},
                ]

        monkeypatch.setattr(repos_module, "jobs_repo", lambda: _FakeJobsRepo())

        from prometheus_client import generate_latest

        from app.observability.metrics import REGISTRY

        text = generate_latest(REGISTRY).decode()
        assert "agnes_http_requests_total" in text  # collect() didn't raise

        assert _counter_value("agnes_jobs_queued", kind="metrics_missing_created_at_kind") == 2.0
        assert _counter_value("agnes_jobs_oldest_queued_age_seconds", kind="metrics_missing_created_at_kind") is None

    def test_record_job_claim_increments_counter(self):
        from app.observability.metrics import record_job_claim
        from app.worker.registry import LIGHT_LANE, JobKind, register_kind

        register_kind(JobKind(name="metrics_claim_kind", handler=lambda p: None, lane=LIGHT_LANE))
        before = _counter_value("agnes_job_claims_total", kind="metrics_claim_kind") or 0.0
        record_job_claim("metrics_claim_kind")
        after = _counter_value("agnes_job_claims_total", kind="metrics_claim_kind")
        assert after == before + 1.0

    def test_record_job_failure_bounded_kind_and_reason(self):
        from app.observability.metrics import record_job_failure

        before = _counter_value("agnes_job_failures_total", kind="other", reason="RuntimeError") or 0.0
        # Kind not registered anywhere -> bucketed to "other".
        record_job_failure("some_unregistered_kind_xyz", "RuntimeError")
        after = _counter_value("agnes_job_failures_total", kind="other", reason="RuntimeError")
        assert after == before + 1.0

    def test_record_job_failure_reason_falls_back_to_other(self):
        from app.observability.metrics import record_job_failure

        before = _counter_value("agnes_job_failures_total", kind="other", reason="other") or 0.0
        record_job_failure("some_unregistered_kind_xyz", "")
        after = _counter_value("agnes_job_failures_total", kind="other", reason="other")
        assert after == before + 1.0

    def test_record_job_duration_observes_histogram(self):
        from app.observability.metrics import record_job_duration

        record_job_duration("metrics_duration_kind", "done", 0.05)
        count = None
        for sample in _samples("agnes_job_duration_seconds_count"):
            if sample.labels.get("outcome") == "done":
                count = sample.value
        assert count is not None and count >= 1.0

    def test_job_duration_histogram_has_hours_scale_bucket(self):
        # Agnes jobs run seconds-to-hours (BQ materialize, heavy sync) — the
        # default prometheus_client buckets top out around 10s, which would
        # dump everything into +Inf. Assert an explicit bucket boundary at
        # or beyond one hour (3600s) exists, distinct from the +Inf bucket.
        #
        # A `Histogram` with label dimensions emits zero samples until
        # `.labels(...)` is first called for some concrete label
        # combination — record one observation ourselves so this test does
        # not depend on `test_record_job_duration_observes_histogram` (or
        # any other test) having already done so first. Under `pytest -n
        # auto` (pytest-xdist's default `load` distribution), individual
        # tests — even from the same class — are not guaranteed to land on
        # the same worker process, and `REGISTRY` is a per-process
        # singleton, so relying on cross-test ordering here was flaky.
        from app.observability.metrics import record_job_duration

        record_job_duration("hours_scale_bucket_kind", "done", 0.05)
        bucket_bounds = set()
        for sample in _samples("agnes_job_duration_seconds_bucket"):
            le = sample.labels.get("le")
            if le is not None and le != "+Inf":
                bucket_bounds.add(float(le))
        assert any(b >= 3600 for b in bucket_bounds), (
            f"expected a bucket boundary >= 3600s, got {sorted(bucket_bounds)}"
        )

    def test_begin_end_job_running_increments_and_decrements(self):
        from app.observability.metrics import begin_job_running, end_job_running
        from app.worker.registry import HEAVY_LANE, JobKind, register_kind

        register_kind(JobKind(name="metrics_running_kind", handler=lambda p: None, lane=HEAVY_LANE))

        before_running = _counter_value("agnes_jobs_running", kind="metrics_running_kind", lane=HEAVY_LANE) or 0.0
        before_lane = _counter_value("agnes_worker_lane_active", lane=HEAVY_LANE) or 0.0

        begin_job_running("metrics_running_kind", HEAVY_LANE)
        assert (
            _counter_value("agnes_jobs_running", kind="metrics_running_kind", lane=HEAVY_LANE) == before_running + 1.0
        )
        assert _counter_value("agnes_worker_lane_active", lane=HEAVY_LANE) == before_lane + 1.0

        end_job_running("metrics_running_kind", HEAVY_LANE)
        assert _counter_value("agnes_jobs_running", kind="metrics_running_kind", lane=HEAVY_LANE) == before_running
        assert _counter_value("agnes_worker_lane_active", lane=HEAVY_LANE) == before_lane

    def test_metrics_helpers_never_raise_on_broken_role_label(self, monkeypatch):
        """Every record_*/begin/end helper must swallow its own exceptions
        — a metrics bug must never propagate into the worker loop and fail
        a job."""
        import app.observability.metrics as obs_metrics

        def boom():
            raise RuntimeError("role_label exploded")

        monkeypatch.setattr(obs_metrics, "role_label", boom)

        # None of these may raise.
        obs_metrics.record_job_claim("whatever")
        obs_metrics.record_job_failure("whatever", "SomeError")
        obs_metrics.record_job_duration("whatever", "done", 0.1)
        obs_metrics.begin_job_running("whatever", "heavy")
        obs_metrics.end_job_running("whatever", "heavy")


@pytest.fixture(autouse=True)
def _reset_coordination_for_metrics_tests(monkeypatch):
    """`coordination()` is a process-wide singleton (app/coordination/factory.py)
    — isolate it across tests in this module the same way
    tests/test_coordination_factory.py does, so an earlier test's redis
    override (or a monkeypatched `ping`) never leaks into a later one."""
    from app.coordination.factory import reset_coordination_for_tests

    for var in ("AGNES_COORDINATION_BACKEND", "AGNES_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def _restore_readiness():
    """`app.api.health_probes.readiness` is a process-wide singleton also
    mutated by tests/test_health_probes.py — restore it to a fresh ready
    state afterward so this module's readiness-tripping tests don't leak
    into unrelated tests running later in the same session."""
    yield
    from app.api.health_probes import readiness

    for _ in range(2):
        readiness.record_canary(True)


class TestCoordinationReadinessMetrics:
    """Task 3: `agnes_coordination_up` / `agnes_coordination_backend_info` /
    `agnes_readiness`, all populated at scrape time by
    `_CoordinationReadinessCollector` (app/observability/metrics.py)."""

    def test_coordination_up_is_1_for_memory_backend(self, app_client, monkeypatch):
        monkeypatch.delenv("AGNES_COORDINATION_BACKEND", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)

        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert _counter_value("agnes_coordination_up") == 1.0

    def test_coordination_backend_info_shows_memory(self, app_client, monkeypatch):
        monkeypatch.delenv("AGNES_COORDINATION_BACKEND", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)

        app_client.get("/metrics")
        sample = next(iter(_samples("agnes_coordination_backend_info")), None)
        assert sample is not None
        assert sample.labels.get("backend") == "memory"

    def test_readiness_gauge_reflects_tripped_state(self, app_client, _restore_readiness):
        from app.api.health_probes import readiness

        for _ in range(3):
            readiness.record_canary(False)
        assert not readiness.is_ready()

        app_client.get("/metrics")
        assert _counter_value("agnes_readiness") == 0.0

    def test_readiness_gauge_reflects_ready_state(self, app_client, _restore_readiness):
        from app.api.health_probes import readiness

        for _ in range(2):
            readiness.record_canary(True)
        assert readiness.is_ready()

        app_client.get("/metrics")
        assert _counter_value("agnes_readiness") == 1.0

    def test_coordination_ping_raising_yields_up_0_and_bumps_error_counter(self, app_client, monkeypatch):
        # Mirrors test_queued_collector_survives_jobs_repo_error's shape: capture
        # `before` while the backend still works (no error bump yet), trigger
        # exactly one broken scrape, then read `after` via exactly one more
        # REGISTRY.collect()-triggering call. Every call to `_counter_value`/
        # `_samples` itself runs REGISTRY.collect() — since our collector bumps
        # the SAME error counter it's about to be asked to report, an extra
        # intermediate check in between would itself trigger another bump
        # (the ping stays broken) and throw off the before/after delta. So the
        # `up` value is read straight out of the one response's own exposition
        # text (already-collected data) instead of a fresh `_counter_value` call.
        from prometheus_client.parser import text_string_to_metric_families

        from app.coordination.factory import coordination

        backend = coordination()

        before = _counter_value("agnes_metrics_collector_errors_total", collector="coordination") or 0.0

        def boom():
            raise RuntimeError("ping exploded")

        monkeypatch.setattr(backend, "ping", boom)

        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert "agnes_http_requests_total" in r.text  # other metrics still present

        up_value = None
        for family in text_string_to_metric_families(r.text):
            for sample in family.samples:
                if sample.name == "agnes_coordination_up":
                    up_value = sample.value
        assert up_value == 0.0

        after = _counter_value("agnes_metrics_collector_errors_total", collector="coordination")
        assert after == before + 1.0
