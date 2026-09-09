"""A brokered completion belongs to the chat turn that caused it (spec 3.2).

The engine propagates no trace context, so the link is made through the
coordination key ChatManager publishes: the broker reads the session's turn
record and opens its completion span as a child of that (remote) span
context, copying the turn's id and workload onto the ledger row. The two may
be different replicas — the collector stitches on the trace id.

The degradation is as much the contract as the linkage: no record, or a
coordination backend that is down, costs the parent and nothing else.
"""

from __future__ import annotations

import pytest

from app.chat.turn_context import TurnRecord, publish_turn
from app.coordination.factory import reset_coordination_for_tests
from src.repositories import ticket_repo
from tests.test_broker_llm_calls import _json_body, ledger  # noqa: F401
from tests.test_otel_export import _FakeUpstream, _post, otel_broker, otel_exporter  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _completion(app, session_id: str):
    tok = ticket_repo().mint(session_id, "main", ttl_seconds=60)
    return _post(
        app,
        tok,
        "/api/broker/anthropic/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )


def _turn(**kw) -> TurnRecord:
    base = {
        "turn_id": "t-1",
        "trace_id": "c" * 32,
        "span_id": "d" * 16,
        "started_at": "2026-09-08T00:00:00+00:00",
        "user_id": "u-1",
        "agent_id": "ag-1",
        "surface": "api",
        "workload": "agent_api",
    }
    base.update(kw)
    return TurnRecord(**base)  # type: ignore[arg-type]


def test_completion_span_is_a_child_of_the_published_turn(otel_broker, otel_exporter, ledger):  # noqa: F811
    from app.api.broker_agent_policy import usage_accumulator

    publish_turn("chat_link_1", _turn())
    _FakeUpstream.body = _json_body()

    r = _completion(otel_broker, "chat_link_1")
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    assert format(span.context.trace_id, "032x") == "c" * 32
    assert span.parent is not None and format(span.parent.span_id, "016x") == "d" * 16
    attrs = dict(span.attributes)
    assert attrs["agnes.turn_id"] == "t-1"
    assert attrs["agnes.user_id"] == "u-1"
    assert attrs["agnes.agent_id"] == "ag-1"
    assert attrs["agnes.workload"] == "agent_api"

    (row,) = ledger
    assert row["turn_id"] == "t-1"
    assert row["trace_id"] == "c" * 32
    assert row["workload"] == "agent_api"
    assert row["user_id"] == "u-1"
    assert row["agent_id"] == "ag-1"


def test_without_a_turn_record_the_span_is_a_root_and_turn_id_is_null(otel_broker, otel_exporter, ledger):  # noqa: F811
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.body = _json_body()
    r = _completion(otel_broker, "chat_link_none")
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    assert span.parent is None
    (row,) = ledger
    assert row["turn_id"] is None
    # Everything else is recorded all the same — an unattributed call is
    # exactly the one a cost report must not lose.
    assert row["session_id"] == "chat_link_none"
    assert row["workload"] == "chat" and row["status"] == "ok"
    assert row["cost_usd"] > 0


def test_coordination_outage_degrades_to_a_root_span(otel_broker, otel_exporter, ledger, monkeypatch):  # noqa: F811
    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator

    def _boom(session_id):
        raise RuntimeError("coordination down")

    monkeypatch.setattr(broker_mod, "read_turn", _boom)
    _FakeUpstream.body = _json_body()

    r = _completion(otel_broker, "chat_link_down")
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    assert span.parent is None
    (row,) = ledger
    assert row["turn_id"] is None and row["session_id"] == "chat_link_down"


def test_a_turn_record_published_after_the_completion_began_is_not_attributed(
    otel_broker,  # noqa: F811
    otel_exporter,  # noqa: F811
    ledger,  # noqa: F811
):
    """A co-driver's message can land mid-completion and publish turn N+1
    before turn N's completion returns (finding A). The record's own
    ``started_at`` is in the future relative to when this completion began,
    so it must not be attributed — an unattributed row is honest, a
    wrongly-attributed one is not."""
    from app.api.broker_agent_policy import usage_accumulator

    publish_turn("chat_link_future", _turn(turn_id="t-future", started_at="2099-01-01T00:00:00+00:00"))
    _FakeUpstream.body = _json_body()

    r = _completion(otel_broker, "chat_link_future")
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    assert span.parent is None
    (row,) = ledger
    assert row["turn_id"] is None
    assert row["session_id"] == "chat_link_future"


def test_a_turn_without_span_ids_still_labels_the_call(otel_broker, otel_exporter, ledger):  # noqa: F811
    """Export was off when the turn opened, so the record carries no span
    context — the completion is still labelled with the turn it belongs to,
    it just has no parent to hang under."""
    from app.api.broker_agent_policy import usage_accumulator

    publish_turn("chat_link_2", _turn(turn_id="t-2", trace_id=None, span_id=None))
    _FakeUpstream.body = _json_body()

    r = _completion(otel_broker, "chat_link_2")
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    assert span.parent is None
    assert dict(span.attributes)["agnes.turn_id"] == "t-2"
    (row,) = ledger
    assert row["turn_id"] == "t-2" and row["workload"] == "agent_api"
