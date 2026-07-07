import re
from connectors.bigquery.labels import build_bq_job_labels, job_labels_for
from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

_LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")


def test_workload_type_defaults_to_agnes():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", "dev")
    assert labels["workload_type"] == "agnes"


def test_workload_type_passed_through_and_sanitized():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", "dev", workload_type="MixedCase!")
    assert labels["workload_type"] == "mixedcase"


def test_user_id_is_email_local_part():
    labels = build_bq_job_labels({"email": "pcernik@example.com"}, "query", "dev")
    assert labels["user_id"] == "pcernik"


def test_agent_and_environment_passed_through():
    labels = build_bq_job_labels({"email": "a@b.com"}, "scan", "production")
    assert labels["agent_name"] == "scan"
    assert labels["environment"] == "production"


def test_uppercase_and_dots_are_sanitized():
    labels = build_bq_job_labels({"email": "First.Last@Example.COM"}, "query", "dev")
    assert labels["user_id"] == "first_last"
    assert _LABEL_VALUE_RE.match(labels["user_id"])


def test_long_value_truncated_to_63():
    labels = build_bq_job_labels({"email": "x" * 100 + "@example.com"}, "query", "dev")
    assert len(labels["user_id"]) == 63


def test_no_user_omits_user_id():
    labels = build_bq_job_labels(None, "scan", "dev")
    assert "user_id" not in labels
    assert labels["workload_type"] == "agnes"


def test_scheduler_user_omits_user_id():
    labels = build_bq_job_labels({"email": SCHEDULER_USER_EMAIL}, "sync", "production")
    assert "user_id" not in labels
    assert labels["agent_name"] == "sync"


def test_empty_environment_omitted():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", "")
    assert "environment" not in labels


def test_all_values_match_bq_grammar():
    labels = build_bq_job_labels({"email": "weird+user.name@x.com"}, "hy brid!", "Prod/Env")
    for k, v in labels.items():
        assert _LABEL_VALUE_RE.match(v), f"{k}={v!r} not BQ-valid"
    assert len(labels) <= 64


def test_never_raises_on_bad_user():
    labels = build_bq_job_labels({"id": None}, "query", "dev")
    assert labels["workload_type"] == "agnes"


def test_returns_empty_dict_when_user_is_not_a_dict():
    # A non-dict user makes client_kind_from_user / .get() raise internally;
    # the totality guard must swallow it and return {}.
    assert build_bq_job_labels(["not", "a", "dict"], "query", "dev") == {}


def test_environment_none_is_omitted():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", None)
    assert "environment" not in labels
    assert labels["workload_type"] == "agnes"


def test_job_labels_for_reads_environment(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "production")
    labels = job_labels_for({"email": "pcernik@example.com"}, "query")
    assert labels["environment"] == "production"
    assert labels["user_id"] == "pcernik"
    assert labels["agent_name"] == "query"


def test_job_labels_for_defensive_on_config_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config down")

    monkeypatch.setattr("app.instance_config.get_value", boom)
    labels = job_labels_for({"email": "a@b.com"}, "scan")
    assert labels["workload_type"] == "agnes"
    assert "environment" not in labels
