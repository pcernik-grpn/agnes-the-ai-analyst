"""Tests for the LLM entity detector (``src/anonymization_ner.py``).

Every test drives a mocked client — no network, no API key, no SDK
credentials. The fake exposes exactly the surface the detector uses
(``client.messages.create(...) -> response`` with ``.content`` text blocks
and ``.usage``), so a change in what the detector actually sends shows up
here rather than in production.
"""

from __future__ import annotations

import pytest

from src.anonymization_ner import (
    DetectionUnavailable,
    LLMDetector,
    _overlap_tail,
    hybrid_detector,
    parse_entities,
    split_document,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class _Response:
    def __init__(self, text: str, usage: _Usage | None = None) -> None:
        self.content = [_Block(text)]
        self.usage = usage or _Usage(input_tokens=100, output_tokens=20)


class _Messages:
    def __init__(self, outer: "FakeClient") -> None:
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        step = self._outer.script[min(len(self._outer.calls) - 1, len(self._outer.script) - 1)]
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(kwargs)
        return step


class FakeClient:
    """Replays a scripted sequence of replies/exceptions, recording requests."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = _Messages(self)


class _ApiError(Exception):
    """Stands in for an SDK error; classified by its ``status_code``."""

    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


def _detector(script, **kwargs) -> tuple[LLMDetector, FakeClient]:
    client = FakeClient(script)
    detector = LLMDetector(
        model="claude-haiku-4-5",
        client=client,
        sleep=lambda _s: None,
        **kwargs,
    )
    return detector, client


def _pairs(entities):
    return [(e.text, e.kind) for e in entities]


# ---------------------------------------------------------------------------
# Chunk splitting
# ---------------------------------------------------------------------------


def test_short_document_is_a_single_chunk_unchanged():
    doc = "Petr Novák pracuje v Alza.cz a.s."
    assert split_document(doc, max_chars=30_000) == [doc]


def test_empty_document_produces_no_chunks():
    assert split_document("", max_chars=100) == []
    assert split_document("   ", max_chars=100) == ["   "]


def test_split_packs_paragraphs_and_replays_an_overlap():
    paragraphs = [f"Odstavec {i} o firme a jejich lidech." for i in range(40)]
    doc = "\n\n".join(paragraphs)

    chunks = split_document(doc, max_chars=300, overlap=80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 300 for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        tail = _overlap_tail(previous, 80)
        assert tail, "each boundary must replay a non-empty tail"
        assert following.startswith(tail)
    # Nothing is dropped on the floor: every paragraph survives somewhere.
    joined = "\n".join(chunks)
    for paragraph in paragraphs:
        assert paragraph in joined


def test_name_straddling_a_hard_slice_survives_in_the_overlap():
    # One paragraph longer than a call budget, so it is hard-sliced. The name
    # is placed across the slice point: without the overlap replay neither
    # chunk would contain it whole.
    filler = "a b c d e f g h i j " * 40
    name = "Petr Novák"
    doc = filler[:135] + name + filler[135 + len(name) :]
    assert "\n\n" not in doc

    chunks = split_document(doc, max_chars=200, overlap=60)

    assert len(chunks) > 1
    assert name not in chunks[0], "precondition: the name is split by the slice"
    assert any(name in chunk for chunk in chunks), "overlap must make the name whole again"


def test_overlap_tail_snaps_to_a_word_boundary():
    assert _overlap_tail("Petr Novák a Jana Nová", 10) == "Jana Nová"
    # A tail with no whitespace at all would only produce half a token.
    assert _overlap_tail("abcdefghijklmnop", 5) == ""
    assert _overlap_tail("anything", 0) == ""


# ---------------------------------------------------------------------------
# Verbatim filtering — the correctness gate
# ---------------------------------------------------------------------------


def test_hallucinated_spans_are_dropped():
    chunk = "Petr Novák podepsal smlouvu s Alza.cz a.s."
    reply = """[
      {"text": "Petr Novák", "kind": "person"},
      {"text": "Jana Svobodová", "kind": "person"},
      {"text": "Alza.cz a.s.", "kind": "company"},
      {"text": "Seznam.cz", "kind": "company"}
    ]"""

    entities, dropped = parse_entities(reply, chunk)

    assert _pairs(entities) == [("Petr Novák", "person"), ("Alza.cz a.s.", "company")]
    assert dropped == 2


def test_normalized_or_lemmatized_forms_are_dropped_too():
    # The model returned a base form the text never contains — substituting it
    # would be a no-op, so it must not be reported as an entity.
    chunk = "Mluvili jsme s Petrem Novákem o projektu."
    entities, dropped = parse_entities('[{"text": "Petr Novák", "kind": "person"}]', chunk)
    assert entities == []
    assert dropped == 1


def test_malformed_entries_are_dropped_not_fatal():
    chunk = "Petr Novák a Alza"
    reply = """[
      "Petr Novák",
      {"text": "Petr Novák"},
      {"text": "Alza", "kind": "vehicle"},
      {"text": "", "kind": "person"},
      {"text": "Alza", "kind": "COMPANY"},
      {"text": "Petr Novák", "kind": "person"}
    ]"""

    entities, dropped = parse_entities(reply, chunk)

    assert _pairs(entities) == [("Alza", "company"), ("Petr Novák", "person")]
    assert dropped == 4


def test_already_substituted_placeholders_are_not_reported_as_entities():
    # The anonymizer runs its URL/email passes first, so the detector sees
    # placeholders in the text. They are verbatim present — hence a dedicated
    # screen rather than relying on the verbatim check.
    chunk = "Napsal EMAIL_1a2b3c z **URL**, podepsán PERSON_9f0e1d. Petr Novák."
    reply = """[
      {"text": "EMAIL_1a2b3c", "kind": "person"},
      {"text": "PERSON_9f0e1d", "kind": "person"},
      {"text": "URL", "kind": "company"},
      {"text": "Petr Novák", "kind": "person"}
    ]"""

    entities, dropped = parse_entities(reply, chunk)

    assert _pairs(entities) == [("Petr Novák", "person")]
    assert dropped == 3


def test_repeated_surface_forms_are_deduped_within_a_chunk():
    chunk = "Petr Novák a Petr Novák"
    entities, _ = parse_entities(
        '[{"text": "Petr Novák", "kind": "person"}, {"text": "Petr Novák", "kind": "person"}]',
        chunk,
    )
    assert _pairs(entities) == [("Petr Novák", "person")]


def test_distinct_inflected_forms_are_all_kept():
    chunk = "Petr Novák, Petra Nováka, Petrem Novákem"
    entities, dropped = parse_entities(
        '[{"text":"Petr Novák","kind":"person"},'
        '{"text":"Petra Nováka","kind":"person"},'
        '{"text":"Petrem Novákem","kind":"person"}]',
        chunk,
    )
    assert len(entities) == 3
    assert dropped == 0


# ---------------------------------------------------------------------------
# JSON-parse robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '[{"text": "Petr Novák", "kind": "person"}]',
        'Here is what I found:\n\n[{"text": "Petr Novák", "kind": "person"}]\n\nLet me know!',
        '```json\n[{"text": "Petr Novák", "kind": "person"}]\n```',
        '```\n[{"text": "Petr Novák", "kind": "person"}]\n```',
        '{"entities": [{"text": "Petr Novák", "kind": "person"}]}',
        '  \n[{"text": "Petr Novák", "kind": "person"}]  \n',
    ],
)
def test_json_array_is_recovered_from_assorted_reply_shapes(reply):
    entities, _ = parse_entities(reply, "Petr Novák byl tady.")
    assert _pairs(entities) == [("Petr Novák", "person")]


def test_bracket_inside_a_name_does_not_end_the_array_early():
    chunk = "Firma [Alza] s.r.o. a Petr Novák"
    reply = '[{"text": "[Alza]", "kind": "company"}, {"text": "Petr Novák", "kind": "person"}]'
    entities, _ = parse_entities(reply, chunk)
    assert _pairs(entities) == [("[Alza]", "company"), ("Petr Novák", "person")]


def test_clean_empty_array_is_a_valid_answer():
    entities, dropped = parse_entities("[]", "Nikdo tu není.")
    assert entities == []
    assert dropped == 0


def test_unparseable_reply_raises_rather_than_returning_empty():
    from src.anonymization_ner import _ParseError

    with pytest.raises(_ParseError):
        parse_entities("I'm sorry, I can't help with that.", "text")


# ---------------------------------------------------------------------------
# Detector: happy path, resilience, fail-closed
# ---------------------------------------------------------------------------


def test_detects_across_chunks_and_dedupes():
    replies = [
        _Response('[{"text": "Petr Novák", "kind": "person"}]'),
        _Response('[{"text": "Petr Novák", "kind": "person"}, {"text": "Alza", "kind": "company"}]'),
    ]
    detector, client = _detector(replies, max_chars_per_call=200, overlap_chars=60)
    paragraphs = ["Petr Novák " + ("slovo " * 20) for _ in range(6)]
    doc = "\n\n".join(paragraphs) + "\n\nAlza"

    entities = detector(doc)

    assert len(client.calls) >= 2
    assert _pairs(entities) == [("Petr Novák", "person"), ("Alza", "company")]


def test_clean_empty_answer_is_an_empty_list_not_an_error():
    detector, client = _detector([_Response("[]")])

    assert detector("Tady nejsou žádná jména.") == []
    assert len(client.calls) == 1
    assert detector.last_usage["calls"] == 1


def test_empty_document_makes_no_call_at_all():
    detector, client = _detector([_Response("[]")])
    assert detector("") == []
    assert client.calls == []
    assert detector.last_usage["chunks"] == 0


def test_persistent_transient_failure_raises_detection_unavailable():
    detector, client = _detector([_ApiError(429), _ApiError(429), _ApiError(429)])

    with pytest.raises(DetectionUnavailable):
        detector("Petr Novák byl tady.")

    assert len(client.calls) == 3, "retries are bounded by max_attempts"


def test_server_error_is_retried_and_can_succeed():
    detector, client = _detector([_ApiError(503), _Response('[{"text": "Alza", "kind": "company"}]')])

    entities = detector("Alza je firma.")

    assert _pairs(entities) == [("Alza", "company")]
    assert len(client.calls) == 2


def test_permanent_error_fails_fast_without_burning_retries():
    detector, client = _detector([_ApiError(401), _Response("[]")])

    with pytest.raises(DetectionUnavailable):
        detector("Petr Novák byl tady.")

    assert len(client.calls) == 1


def test_unparseable_reply_eventually_raises_detection_unavailable():
    detector, client = _detector([_Response("sorry, no"), _Response("still no"), _Response("nope")])

    with pytest.raises(DetectionUnavailable):
        detector("Petr Novák byl tady.")

    assert len(client.calls) == 3


def test_one_failed_chunk_fails_the_whole_document():
    # The second chunk erroring must not silently yield chunk 1's entities:
    # a partial answer is indistinguishable from a complete one downstream.
    detector, client = _detector(
        [_Response('[{"text": "Petr Novák", "kind": "person"}]')] + [_ApiError(500)] * 3,
        max_chars_per_call=200,
        overlap_chars=40,
    )
    doc = "\n\n".join("Petr Novák " + ("slovo " * 20) for _ in range(6))

    with pytest.raises(DetectionUnavailable):
        detector(doc)


def test_missing_credentials_raise_detection_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("src.anonymization_ner._vertex_config", lambda: None)

    detector = LLMDetector(model="claude-haiku-4-5", sleep=lambda _s: None)
    with pytest.raises(DetectionUnavailable):
        detector("Petr Novák byl tady.")


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_puts_rules_in_a_cached_system_block_and_the_document_in_user_content():
    detector, client = _detector([_Response("[]")])
    detector("Petr Novák byl tady.")

    sent = client.calls[0]
    system = sent["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "VERBATIM" in system[0]["text"]
    assert sent["temperature"] == 0
    assert sent["model"] == "claude-haiku-4-5"
    # The untrusted document rides the user channel, sentinel-wrapped.
    content = sent["messages"][0]["content"]
    assert "<document>" in content and "</document>" in content
    assert "Petr Novák byl tady." in content
    assert "VERBATIM" not in content


def test_temperature_is_dropped_when_the_model_rejects_it():
    rejection = _ApiError(400, "temperature: unsupported parameter for this model")
    detector, client = _detector([rejection, _Response("[]"), _Response("[]")])

    assert detector("Petr Novák byl tady.") == []
    assert "temperature" not in client.calls[1]

    # And it stays dropped for the rest of the run.
    detector("Jana Nová byla tady.")
    assert "temperature" not in client.calls[2]


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


def test_usage_accumulates_per_document_and_in_total():
    replies = [
        _Response("[]", _Usage(input_tokens=1000, output_tokens=10, cache_creation=800)),
        _Response("[]", _Usage(input_tokens=200, output_tokens=12, cache_read=800)),
    ]
    detector, _client = _detector(replies, max_chars_per_call=200, overlap_chars=40)
    doc = "\n\n".join("slovo " * 25 for _ in range(6))

    detector(doc)

    assert detector.last_usage["calls"] >= 2
    assert detector.last_usage["chunks"] == detector.last_usage["calls"]
    assert detector.last_usage["input_tokens"] > 0
    assert detector.last_usage["output_tokens"] > 0
    assert detector.last_usage["model"] == "claude-haiku-4-5"
    first_document = dict(detector.last_usage)

    detector("Petr Novák.")

    # last_usage is per-document; total_usage keeps the running sum.
    assert detector.last_usage["calls"] == 1
    assert detector.total_usage["calls"] == int(first_document["calls"]) + 1
    assert detector.total_usage["input_tokens"] > int(first_document["input_tokens"])


def test_usage_counts_dropped_hallucinations():
    reply = '[{"text": "Petr Novák", "kind": "person"}, {"text": "Kdo Ví", "kind": "person"}]'
    detector, _client = _detector([_Response(reply)])

    detector("Petr Novák byl tady.")

    assert detector.last_usage["dropped_not_verbatim"] == 1
    assert detector.last_usage["entities"] == 1


def test_usage_is_recorded_even_for_a_retried_call():
    detector, _client = _detector(
        [
            _Response("garbage", _Usage(input_tokens=100, output_tokens=5)),
            _Response("[]", _Usage(input_tokens=100, output_tokens=5)),
        ]
    )

    detector("Petr Novák byl tady.")

    assert detector.last_usage["calls"] == 2, "a wasted retry still costs tokens"
    assert detector.last_usage["input_tokens"] == 200


# ---------------------------------------------------------------------------
# Hybrid detector
# ---------------------------------------------------------------------------


def test_hybrid_unions_regex_and_llm_with_dedup(monkeypatch):
    from src.anonymization_ner import _LocalEntity

    def fake_regex(_markdown):
        return [
            _LocalEntity(text="Petr Novák", kind="person"),
            _LocalEntity(text="Alza.cz a.s.", kind="company"),
        ]

    monkeypatch.setattr("src.anonymization_ner._resolve_regex_detector", lambda: fake_regex)

    reply = (
        '[{"text": "Petr Novák", "kind": "person"},'
        ' {"text": "Petrem Novákem", "kind": "person"},'
        ' {"text": "Alza.cz a.s.", "kind": "company"}]'
    )
    llm, _client = _detector([_Response(reply)])

    detect = hybrid_detector(llm)
    entities = detect("Petr Novák a Petrem Novákem v Alza.cz a.s.")

    assert _pairs(entities) == [
        ("Petr Novák", "person"),
        ("Alza.cz a.s.", "company"),
        ("Petrem Novákem", "person"),
    ]


def test_hybrid_same_text_different_kind_is_not_deduped(monkeypatch):
    from src.anonymization_ner import _LocalEntity

    monkeypatch.setattr(
        "src.anonymization_ner._resolve_regex_detector",
        lambda: lambda _m: [_LocalEntity(text="Bata", kind="person")],
    )
    llm, _client = _detector([_Response('[{"text": "Bata", "kind": "company"}]')])

    entities = hybrid_detector(llm)("Bata")

    assert _pairs(entities) == [("Bata", "person"), ("Bata", "company")]


def test_hybrid_propagates_detection_unavailable_instead_of_degrading(monkeypatch):
    from src.anonymization_ner import _LocalEntity

    monkeypatch.setattr(
        "src.anonymization_ner._resolve_regex_detector",
        lambda: lambda _m: [_LocalEntity(text="Petr Novák", kind="person")],
    )
    llm, _client = _detector([_ApiError(500)] * 3)

    with pytest.raises(DetectionUnavailable):
        hybrid_detector(llm)("Petr Novák byl tady.")


def test_hybrid_works_without_the_regex_tier(monkeypatch):
    monkeypatch.setattr("src.anonymization_ner._resolve_regex_detector", lambda: None)
    llm, _client = _detector([_Response('[{"text": "Alza", "kind": "company"}]')])

    assert _pairs(hybrid_detector(llm)("Alza")) == [("Alza", "company")]


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_model_falls_back_to_haiku_without_config(monkeypatch):
    import src.anonymization_ner as ner

    monkeypatch.setattr(ner, "default_model", lambda: ner.FALLBACK_MODEL)
    assert LLMDetector().model == "claude-haiku-4-5"


def test_model_reads_the_extraction_config_knob(monkeypatch):
    import app.instance_config as instance_config

    from src.anonymization_ner import default_model

    def fake_get_value(*path, default=None):
        if path == ("corporate_memory", "extraction", "model"):
            return "claude-haiku-4-5-20251001"
        return default

    monkeypatch.setattr(instance_config, "get_value", fake_get_value)
    assert default_model() == "claude-haiku-4-5-20251001"


def test_model_accepts_a_tier_name_from_config(monkeypatch):
    import app.instance_config as instance_config

    from src.anonymization_ner import default_model

    def fake_get_value(*path, default=None):
        return "haiku" if path == ("extraction", "model") else default

    monkeypatch.setattr(instance_config, "get_value", fake_get_value)
    assert default_model().startswith("claude-haiku")
