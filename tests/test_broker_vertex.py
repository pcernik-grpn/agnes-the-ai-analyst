"""Unit tests for ``app/api/broker_vertex.py`` — pure path/rewrite helpers for
the chat broker's Vertex AI mode. No HTTP fixtures.
"""

from __future__ import annotations

import json

import pytest

from app.api import broker_vertex as bv

_NATIVE = (
    "/v1/projects/proj-1/locations/europe-west1/publishers/anthropic/models/claude-sonnet-4-5@20250929:streamRawPredict"
)


# ---------------------------------------------------------------------------
# parse_vertex_path
# ---------------------------------------------------------------------------


def test_parse_native_path():
    t = bv.parse_vertex_path(_NATIVE)
    assert t is not None
    assert t.project == "proj-1"
    assert t.location == "europe-west1"
    assert t.model == "claude-sonnet-4-5@20250929"
    assert t.verb == "streamRawPredict"
    assert t.upstream_path == _NATIVE


def test_parse_accepts_missing_v1_prefix_and_canonicalizes():
    """The Anthropic SDK's Vertex client emits /projects/... against a /v1
    base; the rebuilt outbound path always carries the /v1 form."""
    t = bv.parse_vertex_path(_NATIVE.removeprefix("/v1"))
    assert t is not None
    assert t.upstream_path == _NATIVE


def test_parse_rawpredict_verb():
    t = bv.parse_vertex_path(_NATIVE.replace(":streamRawPredict", ":rawPredict"))
    assert t is not None
    assert t.verb == "rawPredict"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages",
        "/v1/projects/p/locations/l/publishers/google/models/gemini:streamRawPredict",  # wrong publisher
        "/v1/projects/p/locations/l/publishers/anthropic/models/m:predict",  # unknown verb
        "/v1/projects/p/locations/l/publishers/anthropic/models/m",  # no verb
        "/v1/projects/p/publishers/anthropic/models/m:rawPredict",  # missing locations
        _NATIVE.replace(":", "%3A"),  # percent-encoded verb separator — fail closed
        _NATIVE + "/extra",  # trailing garbage
        "/v1/projects/UPPER!/locations/l/publishers/anthropic/models/m:rawPredict",  # bad project chars
        "/v1/projects/" + "p" * 100 + "/locations/l/publishers/anthropic/models/m:rawPredict",  # over-length
    ],
)
def test_parse_refuses_non_vertex_paths(path):
    assert bv.parse_vertex_path(path) is None


# ---------------------------------------------------------------------------
# vertex_upstream_base / validate_vertex_target
# ---------------------------------------------------------------------------


def test_upstream_base_global_vs_regional():
    assert bv.vertex_upstream_base("global") == "https://aiplatform.googleapis.com"
    assert bv.vertex_upstream_base("Global ") == "https://aiplatform.googleapis.com"
    assert bv.vertex_upstream_base("europe-west1") == "https://europe-west1-aiplatform.googleapis.com"


def test_validate_target_pins_project_and_location():
    t = bv.parse_vertex_path(_NATIVE)
    assert bv.validate_vertex_target(t, "proj-1", "europe-west1") is None
    assert bv.validate_vertex_target(t, "other-proj", "europe-west1") == "vertex_target_not_allowed"
    assert bv.validate_vertex_target(t, "proj-1", "us-east5") == "vertex_target_not_allowed"
    # Config region is compared case/whitespace-insensitively.
    assert bv.validate_vertex_target(t, "proj-1", " Europe-West1 ") is None


# ---------------------------------------------------------------------------
# messages_to_vertex (the kai-agent / Messages-format compat shim)
# ---------------------------------------------------------------------------


def test_messages_to_vertex_streaming():
    raw = json.dumps({"model": "claude-sonnet-4-5-20250929", "stream": True, "max_tokens": 5}).encode()
    path, body, model = bv.messages_to_vertex(raw, "proj-1", "europe-west1")
    assert path == _NATIVE  # model translated to the @-form, verb from stream flag
    assert model == "claude-sonnet-4-5-20250929"  # original spelling for the policy gate
    parsed = json.loads(body)
    assert "model" not in parsed  # moved into the URL
    assert parsed["anthropic_version"] == bv.VERTEX_ANTHROPIC_VERSION
    assert parsed["max_tokens"] == 5


def test_messages_to_vertex_non_streaming_uses_rawpredict():
    raw = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 5}).encode()
    path, _body, _model = bv.messages_to_vertex(raw, "p", "global")
    assert path.endswith("/models/claude-sonnet-4-6:rawPredict")


def test_messages_to_vertex_keeps_existing_anthropic_version():
    raw = json.dumps({"model": "m-1", "anthropic_version": "custom"}).encode()
    _path, body, _model = bv.messages_to_vertex(raw, "p", "global")
    assert json.loads(body)["anthropic_version"] == "custom"


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]", json.dumps({"stream": True}).encode(), b""])
def test_messages_to_vertex_invalid_body_raises(raw):
    with pytest.raises(ValueError):
        bv.messages_to_vertex(raw, "p", "global")


@pytest.mark.parametrize(
    "model",
    [
        # httpx collapses dot-segments in the outbound URL — a model carrying
        # '/' would walk the signed request out of publishers/anthropic.
        "claude-x/../../../../publishers/google/models/gemini-pro",
        "m/other",
        "m?x=1",  # query smuggling
        "m#frag",  # fragment smuggling
        "m:rawPredict",  # verb injection
        "m im",  # whitespace
        "m\nx",  # embedded newline (trailing whitespace is stripped harmlessly)
        "..",
        "-leading-dash",  # must start alphanumeric
        "m" * 200,  # over-length
    ],
)
def test_messages_to_vertex_rejects_url_unsafe_model(model):
    """The body's model is rebuilt into the outbound URL — it must satisfy the
    same anchored character class the native-path parser enforces (fail
    closed, same as parse_vertex_path)."""
    raw = json.dumps({"model": model, "stream": True}).encode()
    with pytest.raises(ValueError, match="model"):
        bv.messages_to_vertex(raw, "proj-1", "europe-west1")


# ---------------------------------------------------------------------------
# count_tokens_to_vertex
# ---------------------------------------------------------------------------


def test_count_tokens_keeps_model_in_body():
    raw = json.dumps({"model": "claude-haiku-4-5-20251001", "messages": []}).encode()
    path, body = bv.count_tokens_to_vertex(raw, "proj-1", "europe-west1")
    assert path == ("/v1/projects/proj-1/locations/europe-west1/publishers/anthropic/models/count-tokens:rawPredict")
    parsed = json.loads(body)
    assert parsed["model"] == "claude-haiku-4-5@20251001"  # translated, still in the body
    assert parsed["anthropic_version"] == bv.VERTEX_ANTHROPIC_VERSION


def test_count_tokens_invalid_body_raises():
    with pytest.raises(ValueError):
        bv.count_tokens_to_vertex(json.dumps({"messages": []}).encode(), "p", "global")
