from unittest.mock import MagicMock
import pyarrow as pa
from src.remote_query import RemoteQueryEngine


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
