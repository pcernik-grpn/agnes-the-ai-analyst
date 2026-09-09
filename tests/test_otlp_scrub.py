"""The relay's protobuf scrubber (``src/observability/otlp_scrub.py``).

The embedded engine's sandbox exports its own spans through the broker's OTLP
relay. Those batches carry prompts and completions in span attributes, so the
relay obeys the same content-export policy the app's own spans do: strip,
pseudonymise, or forward — and refuse what it cannot decode rather than
forward it blind.

Every assertion here runs over a REAL ``opentelemetry-proto`` batch: a
hand-rolled byte fixture would not prove the round-trip a collector sees.
"""

from __future__ import annotations

import gzip

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from src.observability import content_policy as cp
from src.observability import otlp_scrub as sc


def _kv(key, value):
    return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(string_value=value))


def _trace_batch() -> bytes:
    span = trace_pb2.Span(name="turn", trace_id=b"\x01" * 16, span_id=b"\x02" * 8)
    span.attributes.extend([_kv("gen_ai.prompt", "hello jane@example.com"), _kv("agnes.turn_id", "t1")])
    ev = span.events.add(name="gen_ai.content.completion")
    ev.attributes.extend([_kv("gen_ai.completion", "call 777 123 456"), _kv("kept", "yes")])
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.resource_spans.add().scope_spans.add().spans.append(span)
    return req.SerializeToString()


def _span(body: bytes):
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(body)
    return req.resource_spans[0].scope_spans[0].spans[0]


def _attrs(items):
    return {kv.key: kv.value for kv in items}


def _logs_batch() -> bytes:
    req = logs_service_pb2.ExportLogsServiceRequest()
    rec = req.resource_logs.add().scope_logs.add().log_records.add()
    rec.body.string_value = "user jane@example.com asked"
    rec.attributes.extend([_kv("gen_ai.prompt", "mail jane@example.com"), _kv("kept", "yes")])
    return req.SerializeToString()


@pytest.fixture
def pseudonymized(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"k")


def test_off_strips_content_from_spans_and_events_and_keeps_structure():
    out = sc.scrub_traces(_trace_batch(), mode="off")
    span = _span(out)
    a = _attrs(span.attributes)
    assert "gen_ai.prompt" not in a and a["agnes.turn_id"].string_value == "t1"
    assert a["agnes.content_stripped"].bool_value is True
    e = _attrs(span.events[0].attributes)
    assert "gen_ai.completion" not in e and e["kept"].string_value == "yes"
    assert e["agnes.content_stripped"].bool_value is True
    # The structural span itself still reaches the collector — stripping is
    # what keeps the turn/step/tool tree usable at all.
    assert span.name == "turn" and span.trace_id == b"\x01" * 16


def test_a_batch_without_content_is_not_flagged():
    span = trace_pb2.Span(name="tool", trace_id=b"\x03" * 16, span_id=b"\x04" * 8)
    span.attributes.extend([_kv("agnes.turn_id", "t2")])
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.resource_spans.add().scope_spans.add().spans.append(span)
    out = _span(sc.scrub_traces(req.SerializeToString(), mode="off"))
    assert "agnes.content_stripped" not in _attrs(out.attributes)


def test_pseudonymized_rewrites_content(pseudonymized):
    span = _span(sc.scrub_traces(_trace_batch(), mode="pseudonymized"))
    prompt = _attrs(span.attributes)["gen_ai.prompt"].string_value
    assert "jane@example.com" not in prompt and "EMAIL_" in prompt
    completion = _attrs(span.events[0].attributes)["gen_ai.completion"].string_value
    assert "777 123 456" not in completion and "PHONE_" in completion


def test_pseudonymized_removes_a_content_attribute_that_is_not_a_string(pseudonymized):
    """An attribute the anonymizer cannot read is removed, not passed through:
    the relay has no way to vouch for a bytes/array payload's content."""
    span = trace_pb2.Span(name="turn", trace_id=b"\x05" * 16, span_id=b"\x06" * 8)
    span.attributes.append(
        common_pb2.KeyValue(key="gen_ai.prompt", value=common_pb2.AnyValue(bytes_value=b"jane@example.com"))
    )
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.resource_spans.add().scope_spans.add().spans.append(span)
    out = _span(sc.scrub_traces(req.SerializeToString(), mode="pseudonymized"))
    a = _attrs(out.attributes)
    assert "gen_ai.prompt" not in a
    assert a["agnes.content_stripped"].bool_value is True


def test_full_forwards_bytes_unchanged():
    body = _trace_batch()
    assert sc.scrub_traces(body, mode="full") is body


def test_gzip_is_decoded_and_the_result_is_uncompressed():
    body = gzip.compress(_trace_batch())
    out = sc.scrub_traces(body, mode="off", content_encoding="gzip")
    assert _span(out).name == "turn"
    assert "gen_ai.prompt" not in _attrs(_span(out).attributes)


def test_undecodable_batch_is_refused():
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.scrub_traces(b"\xff\xfe not a batch", mode="off")
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.scrub_traces(b"nope", mode="off", content_encoding="gzip")


def test_a_gzip_bomb_is_refused_rather_than_expanded():
    bomb = gzip.compress(b"\x00" * (sc.MAX_DECODED_BYTES + 1024))
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.decode_body(bomb, "gzip")


def test_logs_are_dropped_rewritten_or_forwarded(pseudonymized):
    body = _logs_batch()
    assert sc.scrub_logs(body, mode="off") is None
    out = logs_service_pb2.ExportLogsServiceRequest()
    rewritten = sc.scrub_logs(body, mode="pseudonymized")
    assert rewritten is not None
    out.ParseFromString(rewritten)
    record = out.resource_logs[0].scope_logs[0].log_records[0]
    assert "jane@example.com" not in record.body.string_value
    assert "EMAIL_" in record.body.string_value
    assert "jane@example.com" not in _attrs(record.attributes)["gen_ai.prompt"].string_value
    assert _attrs(record.attributes)["kept"].string_value == "yes"
    assert sc.scrub_logs(body, mode="full") is body
    assert isinstance(sc.empty_logs_response(), bytes)


def test_empty_logs_response_is_the_success_shape_an_exporter_expects():
    resp = logs_service_pb2.ExportLogsServiceResponse()
    resp.ParseFromString(sc.empty_logs_response())
    assert resp.partial_success.rejected_log_records == 0


def test_an_undecodable_logs_batch_is_refused():
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.scrub_logs(b"\xff\xfe junk", mode="pseudonymized")
