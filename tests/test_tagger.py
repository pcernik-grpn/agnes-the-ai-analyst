"""Tests for services/corporate_memory/tagger.py — auto topic tagging."""

from services.corporate_memory.tagger import (
    TOPIC_VOCABULARY,
    auto_tag_items,
    _build_prompt,
)
from connectors.llm.exceptions import LLMError


class _FakeExtractor:
    """Controllable fake extractor for testing."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def extract_json(self, prompt, max_tokens, json_schema, schema_name):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if self._raises:
            raise self._raises
        return self._response or {}


class TestTopicVocabulary:
    def test_vocabulary_is_non_empty(self):
        assert len(TOPIC_VOCABULARY) >= 5

    def test_vocabulary_contains_expected_topics(self):
        for topic in ("data", "automation", "reports", "metrics"):
            assert topic in TOPIC_VOCABULARY, f"'{topic}' should be in TOPIC_VOCABULARY"

    def test_vocabulary_has_no_duplicates(self):
        assert len(TOPIC_VOCABULARY) == len(set(TOPIC_VOCABULARY))


class TestBuildPrompt:
    def test_includes_all_vocab_terms(self):
        items = [{"id": "a", "title": "T", "content": "C"}]
        prompt = _build_prompt(items, TOPIC_VOCABULARY)
        for term in TOPIC_VOCABULARY:
            assert term in prompt

    def test_includes_item_id(self):
        items = [{"id": "km_abc123", "title": "My title", "content": "My content"}]
        prompt = _build_prompt(items, TOPIC_VOCABULARY)
        assert "km_abc123" in prompt

    def test_truncates_long_content(self):
        long_content = "x" * 500
        items = [{"id": "a", "title": "T", "content": long_content}]
        prompt = _build_prompt(items, TOPIC_VOCABULARY)
        # Only first 200 chars of content should appear
        assert "x" * 200 in prompt
        assert "x" * 300 not in prompt


class TestAutoTagItems:
    def test_returns_empty_for_empty_input(self):
        extractor = _FakeExtractor()
        result = auto_tag_items([], extractor)
        assert result == {}
        assert extractor.calls == []

    def test_parses_valid_response(self):
        extractor = _FakeExtractor(
            response={
                "assignments": [
                    {"id": "item1", "topics": ["data", "queries"]},
                    {"id": "item2", "topics": ["automation"]},
                ]
            }
        )
        items = [
            {"id": "item1", "title": "SQL tips", "content": "use indexes"},
            {"id": "item2", "title": "CI setup", "content": "automate builds"},
        ]
        result = auto_tag_items(items, extractor)
        assert result["item1"] == ["data", "queries"]
        assert result["item2"] == ["automation"]

    def test_filters_out_vocabulary_hallucinations(self):
        """Topics not in TOPIC_VOCABULARY must be dropped."""
        extractor = _FakeExtractor(
            response={
                "assignments": [
                    {"id": "x", "topics": ["data", "MADE_UP_TOPIC", "queries"]},
                ]
            }
        )
        result = auto_tag_items([{"id": "x", "title": "T", "content": "C"}], extractor)
        assert "MADE_UP_TOPIC" not in result.get("x", [])
        assert "data" in result.get("x", [])

    def test_skips_entries_without_id(self):
        extractor = _FakeExtractor(
            response={
                "assignments": [
                    {"id": "", "topics": ["data"]},
                    {"id": "good", "topics": ["reports"]},
                ]
            }
        )
        result = auto_tag_items([{"id": "good", "title": "T", "content": "C"}], extractor)
        assert "" not in result
        assert result.get("good") == ["reports"]

    def test_returns_empty_on_llm_error(self):
        extractor = _FakeExtractor(raises=LLMError("boom"))
        result = auto_tag_items([{"id": "a", "title": "T", "content": "C"}], extractor)
        assert result == {}

    def test_returns_empty_on_unexpected_exception(self):
        extractor = _FakeExtractor(raises=RuntimeError("network down"))
        result = auto_tag_items([{"id": "a", "title": "T", "content": "C"}], extractor)
        assert result == {}

    def test_returns_empty_when_assignments_missing(self):
        extractor = _FakeExtractor(response={})
        result = auto_tag_items([{"id": "a", "title": "T", "content": "C"}], extractor)
        assert result == {}

    def test_handles_empty_topics_list(self):
        extractor = _FakeExtractor(response={"assignments": [{"id": "a", "topics": []}]})
        result = auto_tag_items([{"id": "a", "title": "T", "content": "C"}], extractor)
        assert result.get("a") == []


class TestTheCallIsLabelled:
    def test_tagging_says_what_workload_it_belongs_to(self):
        """Best-effort tagging still costs tokens — it is recorded under the
        corporate-memory workload with its own purpose."""
        from src.observability.llm_context import current_llm_context

        seen = {}

        class _RecordingExtractor(_FakeExtractor):
            def extract_json(self, *args, **kwargs):
                seen["context"] = current_llm_context()
                return super().extract_json(*args, **kwargs)

        extractor = _RecordingExtractor(response={"assignments": []})
        auto_tag_items([{"id": "a", "title": "T", "content": "C"}], extractor)

        ctx = seen["context"]
        assert (ctx.workload, ctx.purpose) == ("corporate_memory", "tagger")
