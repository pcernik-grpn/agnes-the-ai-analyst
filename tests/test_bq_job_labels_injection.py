from unittest.mock import MagicMock
import pyarrow as pa
from src.remote_query import RemoteQueryEngine
from app.api import v2_scan


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

    labels = {"workload_type": "foundryai", "agent_name": "hybrid", "user_id": "pcernik"}
    engine.register_bq("bq_x", "SELECT 1", job_labels=labels)

    assert client.query.call_count == 2
    for call in client.query.call_args_list:
        job_config = call.kwargs.get("job_config")
        assert job_config is not None, "client.query called without job_config"
        assert job_config.labels == labels


def test_dry_run_bytes_applies_scan_labels(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "dev")
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
    assert jc.labels.get("workload_type") == "foundryai"
    assert jc.labels.get("agent_name") == "scan"
    assert jc.labels.get("user_id") == "pcernik"
