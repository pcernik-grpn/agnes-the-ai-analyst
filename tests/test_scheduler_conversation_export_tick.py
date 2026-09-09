"""``conversation-export`` scheduler row -- Task 11 push sink (design
2026-09-08 §3.12). Registered ONLY when
``observability.conversation_export.endpoint`` is configured -- same
off-by-default registration discipline as ``extraction-run-due``
(``tests/test_scheduler_sidecar.py``).
"""

from __future__ import annotations


def _clean_env(monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)


def test_conversation_export_schedule_defaults_to_disabled(monkeypatch):
    """Off by default -- an operator must set `endpoint` to turn this on,
    so absent config means no scheduler row at all, not a guessed cadence."""
    _clean_env(monkeypatch)
    from services.scheduler.__main__ import _conversation_export_schedule, build_jobs

    assert _conversation_export_schedule() is None
    assert "conversation-export" not in {j[0] for j in build_jobs()}


def test_conversation_export_schedule_enqueues_when_endpoint_set(monkeypatch):
    import app.instance_config as ic

    values = {
        ("observability", "conversation_export", "endpoint"): "https://collector.example.com/ingest",
        ("observability", "conversation_export", "interval_minutes"): 60,
    }
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: values.get(keys, default))
    from services.scheduler.__main__ import _ENQUEUE_BODIES, _conversation_export_schedule, build_jobs

    assert _conversation_export_schedule() == "every 1h"

    target = next(j for j in build_jobs() if j[0] == "conversation-export")
    _name, schedule, endpoint, method, _timeout_sec, body = target
    assert schedule == "every 1h"
    assert endpoint == "/api/jobs"
    assert method == "POST"
    assert body == _ENQUEUE_BODIES["conversation-export"]
    assert body["kind"] == "conversation-export"
    assert body["idempotency_key"] == "conversation-export"


def test_conversation_export_schedule_custom_interval_in_minutes(monkeypatch):
    import app.instance_config as ic

    values = {
        ("observability", "conversation_export", "endpoint"): "https://collector.example.com/ingest",
        ("observability", "conversation_export", "interval_minutes"): 15,
    }
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: values.get(keys, default))
    from services.scheduler.__main__ import _conversation_export_schedule

    assert _conversation_export_schedule() == "every 15m"


def test_conversation_export_schedule_survives_a_config_read_failure(monkeypatch):
    """A broken/unreadable instance.yaml must not take build_jobs() down --
    same defensive posture as `_extraction_schedule`/`_acl_sync_schedule`."""
    import app.instance_config as ic

    def _boom(*keys, default=None):
        if keys and keys[0] == "observability":
            raise RuntimeError("config unreadable")
        return default

    monkeypatch.setattr(ic, "get_value", _boom)
    from services.scheduler.__main__ import _conversation_export_schedule, build_jobs

    assert _conversation_export_schedule() is None
    jobs = build_jobs()  # must not raise
    assert "conversation-export" not in {j[0] for j in jobs}
