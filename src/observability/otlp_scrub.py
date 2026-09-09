"""The relay's content scrubber — an OTLP batch under the export policy.

The embedded engine's sandbox exports its own spans (turn → step → tool) and
log records through the broker's relay (``app/api/broker.py::otlp_proxy``).
Those batches are produced outside this codebase and routinely carry the
prompt and the model's answer in span attributes, so forwarding them
byte-for-byte would export content the instance's own spans are not allowed
to export. This module applies the ONE policy
(:mod:`src.observability.content_policy`) to somebody else's protobuf:

- ``off`` — the content attributes (:data:`CONTENT_ATTRIBUTE_KEYS`) are
  removed from every span, span event and log record, and
  ``agnes.content_stripped=true`` is added wherever something was actually
  removed. The structural spans still flow: they are what answers "what did
  this turn do", and refusing the whole batch would lose that to protect
  something a removal already protects.
- ``pseudonymized`` — each content attribute (and each log body) is rewritten
  through :func:`src.observability.content_policy.export_text`, i.e. the
  instance anonymizer with the instance's own pseudonym key.
- ``full`` — the bytes are forwarded untouched (no decode, no re-serialise).

Two rules that are load-bearing rather than cosmetic:

**Fail closed.** A batch that cannot be decoded under ``off`` or
``pseudonymized`` raises :class:`OtlpBatchUndecodable`; the route answers
``400 otlp_batch_undecodable``. A relay that cannot read a batch cannot claim
the batch carries no content, and "forward it and hope" is exactly the
failure mode the policy exists to prevent. The same applies to a gzip payload
that expands past :data:`MAX_DECODED_BYTES` — an unreadable batch, refused,
never buffered.

**A content attribute that is not a plain string is removed**, under
``pseudonymized`` as well as under ``off``, and flagged with
``agnes.content_stripped=true``. The anonymizer works on text; a bytes /
array / kvlist payload under ``gen_ai.prompt`` is content the relay cannot
vouch for, so it does not travel.

Sizes, token counts, timings and every non-content attribute are untouched in
all three modes — the point is to keep the telemetry and drop the text.
"""

from __future__ import annotations

import gzip
import io
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The attributes an OTLP producer puts an LLM exchange's TEXT in — the OTel
#: GenAI conventions' prompt/completion pair plus the newer message-list form
#: the engine's own SDK emits.
CONTENT_ATTRIBUTE_KEYS = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
    }
)

STRIPPED_FLAG = "agnes.content_stripped"

#: Ceiling on a decompressed batch. The route caps the COMPRESSED body at
#: 8 MiB, and gzip's ratio on repetitive text is unbounded, so the decode has
#: its own limit: a batch that expands past this is refused as undecodable
#: rather than buffered.
MAX_DECODED_BYTES = 64 * 1024 * 1024


class OtlpBatchUndecodable(ValueError):
    """The relay could not read the batch (bad gzip, not a protobuf of the
    declared signal, or a decompression past :data:`MAX_DECODED_BYTES`), so
    it cannot say whether it carries content. Refused, never forwarded."""


def decode_body(body: bytes, content_encoding: Optional[str]) -> bytes:
    """The raw protobuf bytes, gunzipped when the request said so."""
    if (content_encoding or "").strip().lower() != "gzip":
        return body
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
            out = fh.read(MAX_DECODED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise OtlpBatchUndecodable("batch is not valid gzip") from exc
    if len(out) > MAX_DECODED_BYTES:
        raise OtlpBatchUndecodable("decompressed batch exceeds the relay's ceiling")
    return out


def _parse(message: Any, body: bytes, what: str) -> Any:
    try:
        message.ParseFromString(body)
    except Exception as exc:  # noqa: BLE001 - protobuf raises its own types
        raise OtlpBatchUndecodable(f"batch is not a valid {what} export request") from exc
    return message


def _scrub_attributes(attributes: Any, mode: str) -> None:
    """Rewrite one repeated ``KeyValue`` field in place.

    Removal is done by rebuilding the list: protobuf's repeated fields have
    no stable "delete by predicate", and a rebuild keeps the surviving
    attributes in their original order.
    """
    from src.observability.content_policy import export_text

    if not any(kv.key in CONTENT_ATTRIBUTE_KEYS for kv in attributes):
        return
    kept: list[Any] = []
    stripped = False
    for kv in attributes:
        if kv.key not in CONTENT_ATTRIBUTE_KEYS:
            kept.append(kv)
            continue
        if mode == "pseudonymized" and kv.value.WhichOneof("value") == "string_value":
            kv.value.string_value = export_text(kv.value.string_value)
            kept.append(kv)
            continue
        # `off`, or a value the anonymizer cannot read: it does not travel.
        stripped = True
    del attributes[:]
    attributes.extend(kept)
    if stripped:
        from opentelemetry.proto.common.v1 import common_pb2

        attributes.append(common_pb2.KeyValue(key=STRIPPED_FLAG, value=common_pb2.AnyValue(bool_value=True)))


def scrub_traces(body: bytes, *, mode: str, content_encoding: Optional[str] = None) -> bytes:
    """One ``ExportTraceServiceRequest``, as the policy allows it to leave.

    Returns the ORIGINAL object under ``full`` (the caller forwards the bytes
    it received, compression and all); otherwise the re-serialised,
    uncompressed batch — the caller must drop the ``content-encoding`` header
    it was going to forward.
    """
    if mode == "full":
        return body
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    request = _parse(trace_service_pb2.ExportTraceServiceRequest(), decode_body(body, content_encoding), "trace")
    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                _scrub_attributes(span.attributes, mode)
                for event in span.events:
                    _scrub_attributes(event.attributes, mode)
    return request.SerializeToString()


def scrub_logs(body: bytes, *, mode: str, content_encoding: Optional[str] = None) -> Optional[bytes]:
    """One ``ExportLogsServiceRequest`` under the policy.

    A log record's body is free text the producer chose — there is no
    structural half to keep — so ``off`` returns ``None``: the caller answers
    the exporter with :func:`empty_logs_response` and forwards nothing.
    """
    if mode == "full":
        return body
    if mode == "off":
        return None
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    request = _parse(logs_service_pb2.ExportLogsServiceRequest(), decode_body(body, content_encoding), "logs")
    from src.observability.content_policy import export_text

    for resource_logs in request.resource_logs:
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                if record.body.WhichOneof("value") == "string_value":
                    record.body.string_value = export_text(record.body.string_value)
                _scrub_attributes(record.attributes, mode)
    return request.SerializeToString()


def empty_logs_response() -> bytes:
    """The serialised ``ExportLogsServiceResponse`` a dropped batch answers
    with: a success with nothing rejected, so the SDK's exporter neither
    retries nor logs a failure for a decision the operator made."""
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    return logs_service_pb2.ExportLogsServiceResponse().SerializeToString()


__all__ = [
    "CONTENT_ATTRIBUTE_KEYS",
    "MAX_DECODED_BYTES",
    "STRIPPED_FLAG",
    "OtlpBatchUndecodable",
    "decode_body",
    "empty_logs_response",
    "scrub_logs",
    "scrub_traces",
]
