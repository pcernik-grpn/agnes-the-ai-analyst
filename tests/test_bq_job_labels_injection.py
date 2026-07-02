from unittest.mock import MagicMock
import pyarrow as pa
from src.remote_query import RemoteQueryEngine
from app.api import v2_scan
from app.api import query as query_module


def _fake_job(value=0):
    job = MagicMock()
    job.to_arrow.return_value = pa.table({"c": [value]})
    return job


def test_register_bq_applies_labels_to_both_jobs():
    client = MagicMock()
    # COUNT(*) job returns 1 row; data job returns a small table
    client.query.side_effect = [_fake_job(1), _fake_job(1)]
    engine = RemoteQueryEngine(MagicMock(), bq_access=MagicMock())
    engine._get_bq_client = lambda: client  # bypass real BQ client resolution

    labels = {"workload_type": "agnes", "agent_name": "hybrid", "user_id": "pcernik"}
    engine.register_bq("bq_x", "SELECT 1", job_labels=labels)

    assert client.query.call_count == 2
    for call in client.query.call_args_list:
        job_config = call.kwargs.get("job_config")
        assert job_config is not None, "client.query called without job_config"
        assert job_config.labels == labels


def test_dry_run_bytes_applies_scan_labels(monkeypatch):
    def fake_get_value(*keys, default=None):
        return {"environment": "dev", "workload_type": "agnes"}.get(keys[-1], default)

    monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
    captured = {}

    class _Client:
        def query(self, sql, job_config=None):
            captured["job_config"] = job_config
            job = MagicMock()
            job.total_bytes_processed = 123
            return job

    bq = MagicMock()
    bq.client.return_value = _Client()
    v2_scan._bq_dry_run_bytes(bq, "SELECT 1", user={"email": "pcernik@example.com"})

    jc = captured["job_config"]
    assert jc.dry_run is True
    assert jc.labels.get("workload_type") == "agnes"
    assert jc.labels.get("agent_name") == "scan"
    assert jc.labels.get("user_id") == "pcernik"


def test_bq_quota_and_cap_guard_applies_query_labels(monkeypatch):
    """The real /api/query dry-run callsite (`_bq_quota_and_cap_guard`) must
    label its BQ job ``agent_name="query"`` — not `_bq_dry_run_bytes`'s
    ``agent_name="scan"`` default. Drives the actual production code path
    (through the real `job_labels_for` call and a captured `job_config`, not
    just an assertion on `job_labels_for`'s output) so a callsite that
    forgets to pass `agent_name="query"` regresses here.
    """
    import app.api.v2_quota as v2_quota

    # Fresh quota state — avoid bleed from other tests' daily-byte usage.
    monkeypatch.setattr(v2_quota, "_quota_singleton", None, raising=False)
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "dev" if keys == ("instance", "environment") else default,
    )

    captured = {}

    class _Client:
        def query(self, sql, job_config=None):
            captured["job_config"] = job_config
            job = MagicMock()
            job.total_bytes_processed = 777
            return job

    class _Projects:
        data = "test-data-project"
        billing = "test-billing-project"

    class _FakeBq:
        projects = _Projects()

        def client(self):
            return _Client()

    monkeypatch.setattr(query_module, "get_bq_access", lambda: _FakeBq())

    with query_module._bq_quota_and_cap_guard(
        user_id="pcernik",
        user={"email": "pcernik@example.com"},
        dry_run_set=[("bucket", "table", 0)],
        name_lookups=[],
        sql="SELECT 1",
    ):
        pass

    jc = captured["job_config"]
    assert jc.dry_run is True
    assert jc.labels["agent_name"] == "query"
    assert jc.labels["user_id"] == "pcernik"
