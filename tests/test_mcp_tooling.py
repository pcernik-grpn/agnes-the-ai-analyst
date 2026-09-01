"""Unit tests for src/mcp_tooling.py — MCP wire-description summarizer and
output-size guard shared by the HTTP foundation and CLI stdio MCP servers."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.mcp_tooling import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_SEARCH_MAX_CHARS,
    MAX_OUTPUT_CHARS_ENV,
    SEARCH_MAX_CHARS_ENV,
    SEARCH_TEXT_FLOOR,
    MCPOutputTooLarge,
    compact_search_results,
    ensure_output_size,
    ensure_query_output_size,
    max_output_chars,
    progressive_tool,
    search_max_chars,
    summarize_docstring,
    wire_size,
)


class TestSummarizeDocstring:
    def test_single_paragraph_no_more(self):
        assert summarize_docstring("One line only.") == ("One line only.", False)

    def test_multi_paragraph_returns_first_and_flags_more(self):
        doc = "First para line one\ncontinues here.\n\nSecond paragraph."
        summary, has_more = summarize_docstring(doc)
        assert summary == "First para line one continues here."
        assert has_more is True

    def test_indented_docstring_is_cleaned(self):
        doc = """Show schema plus sample rows.

        Args:
            table_id: Table ID.
        """
        summary, has_more = summarize_docstring(doc)
        assert summary == "Show schema plus sample rows."
        assert has_more is True

    def test_none_and_empty(self):
        assert summarize_docstring(None) == ("", False)
        assert summarize_docstring("   ") == ("", False)


class TestEnsureOutputSize:
    def test_under_cap_returns_payload_unchanged(self):
        payload = {"rows": [[1, 2]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload

    def test_over_cap_raises_with_guidance(self):
        payload = {"rows": [["x" * 500]]}
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size(payload, "query", cap=100)
        msg = str(exc.value)
        assert "query response" in msg
        assert "output cap" in msg
        assert "WHERE" in msg  # default hint mentions narrowing options

    def test_custom_hint_lands_in_message(self):
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size({"x": "y" * 200}, "describe", hint="lower `rows`", cap=50)
        assert "lower `rows`" in str(exc.value)

    def test_cap_zero_disables(self):
        payload = {"rows": [["x" * 10_000]]}
        assert ensure_output_size(payload, "query", cap=0) is payload

    def test_non_json_values_measured_via_str(self):
        payload = {"rows": [[date(2026, 1, 1)]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload


class TestProgressiveTool:
    def _mcp(self):
        pytest.importorskip("mcp", reason="mcp package not installed")
        from mcp.server.fastmcp import FastMCP

        return FastMCP("tooling-test")

    def test_wire_description_is_first_paragraph_plus_pointer(self):
        mcp = self._mcp()
        registry: dict[str, str] = {}
        tool = progressive_tool(mcp, registry)

        @tool(read_only=True)
        def sample(x: int = 1) -> dict:
            """Do the thing.

            Args:
                x: A number.
            """
            return {"x": x}

        t = mcp._tool_manager.get_tool("sample")
        assert t.description == "Do the thing. Full contract: tool_docs('sample')."
        assert registry["sample"].startswith("Do the thing.")
        assert "Args:" in registry["sample"]

    def test_single_paragraph_gets_no_pointer(self):
        mcp = self._mcp()
        registry: dict[str, str] = {}
        tool = progressive_tool(mcp, registry)

        @tool(read_only=True)
        def brief() -> dict:
            """Just this."""
            return {}

        assert mcp._tool_manager.get_tool("brief").description == "Just this."

    def test_decorated_function_is_returned_unchanged(self):
        mcp = self._mcp()
        tool = progressive_tool(mcp, {})

        @tool(read_only=True)
        def add_one(x: int) -> dict:
            """Add one.

            More detail.
            """
            return {"x": x + 1}

        # Callable directly (back-compat: mcp_http binds tool fns into globals)
        assert add_one(3) == {"x": 4}
        assert mcp._tool_manager.get_tool("add_one").fn is add_one


class TestMaxOutputChars:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(MAX_OUTPUT_CHARS_ENV, raising=False)
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "5000")
        assert max_output_chars() == 5000

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "banana")
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_zero_disables_guard(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "0")
        big = {"rows": [["x" * (DEFAULT_MAX_OUTPUT_CHARS + 10)]]}
        assert ensure_output_size(big, "query") is big


class TestEnsureQueryOutputSize:
    """`ensure_output_size` RAISES rather than truncating, so an advisory
    bolted onto a borderline result could fail a query that would otherwise
    have returned. The advisory gives way first — never the rows."""

    def _payload(self, *, rows, warnings):
        return {
            "columns": ["id", "blob"],
            "rows": rows,
            "truncated": False,
            "semantic_validation": {
                "valid": False,
                "warnings": [f"w{i} " + "y" * 100 for i in range(warnings)],
                "violations": [{"name": f"c{i}", "reason": "z" * 200} for i in range(warnings)],
                "summary": "s" * 400,
                "detection": "Datasets and metrics were detected by a best-effort text match.",
            },
        }

    def test_under_the_cap_passes_the_payload_through_untouched(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "100000")
        payload = self._payload(rows=[[1, "x"]], warnings=2)
        assert ensure_query_output_size(payload) is payload

    def test_only_the_advisory_is_shortened_when_that_is_enough(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "4000")
        rows = [[i, "x" * 40] for i in range(60)]
        out = ensure_query_output_size(self._payload(rows=rows, warnings=40))
        assert out["rows"] == rows, "the rows the caller asked for are untouched"
        advisory = out["semantic_validation"]
        if advisory is not None:
            assert advisory["truncated"] is True
            assert len(advisory["warnings"]) <= 3
            assert "40 warnings total" in advisory["truncated_note"]

    def test_the_detection_note_survives_the_shrink(self, monkeypatch):
        """The shrunk advisory keeps `detection` — it's one small string, and
        it's the "this is a heuristic, not proof" disclosure. Only dropping
        the whole advisory (rows are the big part) should drop it too."""
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "4000")
        rows = [[i, "x" * 40] for i in range(60)]
        out = ensure_query_output_size(self._payload(rows=rows, warnings=40))
        advisory = out["semantic_validation"]
        assert advisory is not None, "this case should shrink, not drop, the advisory"
        assert advisory["truncated"] is True
        assert advisory["detection"] == "Datasets and metrics were detected by a best-effort text match."

    def test_rows_too_large_on_their_own_still_raise(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "2000")
        rows = [["x" * 100] for _ in range(200)]
        with pytest.raises(MCPOutputTooLarge):
            ensure_query_output_size(self._payload(rows=rows, warnings=1))

    def test_a_payload_without_an_advisory_behaves_exactly_like_before(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "2000")
        payload = {"columns": ["blob"], "rows": [["x" * 100] for _ in range(200)], "semantic_validation": None}
        with pytest.raises(MCPOutputTooLarge):
            ensure_query_output_size(payload)

    def test_a_non_dict_payload_is_not_a_crash(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "100000")
        assert ensure_query_output_size([1, 2, 3]) == [1, 2, 3]


# ── search-result compaction (TCRD-287) ────────────────────────────────────────


def _chunk(i: int, text_len: int = 3_200) -> dict:
    """One ``knowledge_search`` chunk hit the size the ingest chunker emits."""
    return {
        "chunk_id": f"ch_{i:04d}",
        "corpus_id": "col_0123456789abcdef",
        "file_id": f"cf_{i:04d}",
        "filename": f"report-{i}.pdf",
        "ordinal": i,
        "section_path": "1 > 1.2",
        # Non-ASCII on purpose: a Czech document is what the incident carried,
        # and `ensure_ascii=False` vs `True` changes the measured size 6×.
        "text": ("Příliš žluťoučký kůň úpěl ďábelské ódy. " * 200)[:text_len],
        "score": round(1.0 / (1 + i), 4),
        "confidence": "high",
        "matched_on": "body",
        "type": "chunk",
    }


def _incident_payload(k: int = 10) -> dict:
    """Ten 3.2k-char chunks — the ~52k-char response the chat engine refused."""
    return {"query": "kůň", "results": [_chunk(i) for i in range(k)], "retrieval": "hybrid"}


class TestSearchBudget:
    def test_default_is_the_file_read_ceiling(self):
        assert DEFAULT_SEARCH_MAX_CHARS == 20_000
        assert search_max_chars() == DEFAULT_SEARCH_MAX_CHARS

    def test_env_override_and_garbage_fallback(self, monkeypatch):
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "5000")
        assert search_max_chars() == 5000
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "lots")
        assert search_max_chars() == DEFAULT_SEARCH_MAX_CHARS

    def test_wire_size_is_the_text_fastmcp_actually_emits(self):
        """The budget must be measured against the text the client receives —
        FastMCP's own content conversion — not an approximation of it. Pinned
        against FastMCP's converter itself, so a change of serializer there
        fails here rather than silently letting oversized results through."""
        from datetime import date, datetime

        from mcp.server.fastmcp.utilities.func_metadata import _convert_to_content

        payload = _incident_payload(2)
        # Types where json.dumps(default=str) and pydantic_core.to_json disagree.
        payload["results"][0]["score"] = 1.0
        payload["results"][0]["indexed_at"] = datetime(2026, 9, 1, 12, 30)
        payload["results"][1]["document_date"] = date(2026, 9, 1)
        payload["ratio"] = 0.1 + 0.2
        blocks = _convert_to_content(payload)
        assert len(blocks) == 1 and blocks[0].type == "text"
        assert wire_size(payload) == len(blocks[0].text)
        # Unicode is not escaped on the wire — measuring the escaped form would
        # make a Czech chunk look six times bigger than it is.
        assert wire_size(payload) < len(json.dumps(payload, indent=2, default=str))


class TestCompactSearchResults:
    def test_fitting_payload_is_returned_untouched_and_identical(self):
        payload = _incident_payload(1)
        assert compact_search_results(payload, "knowledge_search", budget=20_000) is payload
        assert "truncated" not in payload

    def test_the_incident_payload_fits_the_default_budget(self):
        """The bug: ten chunks → ~52k chars → the engine refused the result.
        After compaction the wire text is inside the budget, every hit is
        still there, and the model is told what happened."""
        payload = _incident_payload()
        # Ten chunker-sized hits are ~35k chars on the wire (the incident's 52k
        # carried fifteen) — well over the budget either way.
        assert wire_size(payload) > 30_000
        out = compact_search_results(payload, "knowledge_search")
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS
        assert out["truncated"] is True
        assert len(out["results"]) == 10  # shortened, not dropped
        assert "10 of 10 results carry shortened text" in out["truncated_note"]
        assert "collection_file_read" in out["truncated_note"]
        assert "knowledge_search" in out["truncated_note"]
        assert "dropped" not in out["truncated_note"]

    def test_shortened_hits_are_marked_and_identifiers_survive(self):
        payload = _incident_payload()
        out = compact_search_results(payload, "knowledge_search", budget=20_000)
        for before, after in zip(payload["results"], out["results"]):
            assert after["truncated"] is True
            assert after["truncated_fields"] == ["text"]
            assert after["text"].endswith("…")
            assert before["text"].startswith(after["text"][:-1])
            assert len(after["text"]) < len(before["text"])
            # Everything a follow-up call needs is byte-identical.
            for key in ("chunk_id", "corpus_id", "file_id", "filename", "ordinal", "score", "matched_on"):
                assert after[key] == before[key]

    def test_input_payload_is_never_mutated(self):
        payload = _incident_payload()
        snapshot = json.dumps(payload, sort_keys=True)
        compact_search_results(payload, "knowledge_search", budget=5_000)
        assert json.dumps(payload, sort_keys=True) == snapshot

    def test_drops_lowest_ranked_hits_only_after_text_is_at_the_floor(self):
        payload = _incident_payload()
        out = compact_search_results(payload, "knowledge_search", budget=3_000)
        kept = out["results"]
        assert 0 < len(kept) < 10
        # Ranked order preserved, cut from the tail.
        assert [h["chunk_id"] for h in kept] == [h["chunk_id"] for h in payload["results"][: len(kept)]]
        assert all(len(h["text"]) <= SEARCH_TEXT_FLOOR + 1 for h in kept)  # floor + the mark
        assert f"{10 - len(kept)} lower-ranked result(s) of 10 were dropped" in out["truncated_note"]
        assert wire_size(out) <= 3_000

    def test_every_prose_field_is_covered_not_just_chunk_text(self):
        """A table card with a long imported description, a metric, a glossary
        term — the other hit types the combined search interleaves."""
        payload = {
            "query": "revenue",
            "results": [
                {
                    "type": "table",
                    "table_id": "t1",
                    "name": "orders",
                    "description": "d" * 5_000,
                    "score": 1.0,
                    "pivot_hint": "structured data — query with SQL via `agnes query`, table id: t1",
                },
                {"type": "metric", "id": "m1", "name": "mrr", "description": "m" * 5_000, "score": 0.9},
                {"type": "glossary", "id": "g1", "term": "MRR", "definition": "g" * 5_000, "score": 0.8},
                {"type": "knowledge", "id": "k1", "title": "note", "snippet": "s" * 5_000, "score": 0.7},
            ],
            "retrieval": "hybrid",
        }
        out = compact_search_results(payload, "knowledge_search", budget=4_000)
        assert wire_size(out) <= 4_000
        fields = {h["type"]: h.get("truncated_fields") for h in out["results"]}
        assert fields == {
            "table": ["description"],
            "metric": ["description"],
            "glossary": ["definition"],
            "knowledge": ["snippet"],
        }
        # The pivot hint is an instruction, not prose — untouched.
        assert out["results"][0]["pivot_hint"].endswith("table id: t1")

    def test_zero_budget_disables(self):
        payload = _incident_payload()
        assert compact_search_results(payload, "knowledge_search", budget=0) is payload

    def test_env_budget_is_read_at_call_time(self, monkeypatch):
        payload = _incident_payload()
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "0")
        assert compact_search_results(payload, "knowledge_search") is payload
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "8000")
        assert wire_size(compact_search_results(payload, "knowledge_search")) <= 8000

    def test_non_search_shapes_pass_through(self):
        assert compact_search_results("plain", "x", budget=10) == "plain"
        big_no_results = {"blob": "x" * 5_000}
        assert compact_search_results(big_no_results, "x", budget=100) is big_no_results
        big_results_not_a_list = {"results": "x" * 5_000}
        assert compact_search_results(big_results_not_a_list, "x", budget=100) is big_results_not_a_list

    def test_empty_result_hint_is_never_cut(self):
        """The empty-result ``hint`` is the other half of the search UX contract
        (an empty result must not read as an access problem); it lives outside
        ``results`` and passes through whole."""
        hint = "Searched 3 collection(s) and 40 table(s) … whole word … no wildcard … " * 3
        payload = {"query": "q", "results": [], "retrieval": "hybrid", "searched_collections": 3, "hint": hint}
        assert compact_search_results(payload, "knowledge_search", budget=20_000) is payload

    def test_deterministic(self):
        payload = _incident_payload()
        a = compact_search_results(payload, "knowledge_search", budget=6_000)
        b = compact_search_results(payload, "knowledge_search", budget=6_000)
        assert a == b

    def test_mixed_lengths_find_the_true_largest_cap(self):
        """Size is not monotonic in the cap across a field-length boundary: at
        cap == len(field) the field comes back whole and sheds its mark and
        its `truncated_fields` entry, so the size can DROP as the cap grows.
        A plain binary search over [floor, longest] can land below the true
        maximum (Copilot review on #2046). Brute-force every cap and require
        the search to use the budget as well as the best cap does — for
        several budgets, so the optimum falls in different intervals."""
        from src.mcp_tooling import _apply_cap, _with_results

        hits = [_chunk(0, 3_000), _chunk(1, 1_200), _chunk(2, 700), _chunk(3, 450)]
        payload = {"query": "kůň", "results": hits, "retrieval": "hybrid"}

        def size_at(cap: int, budget: int) -> int:
            return wire_size(_with_results(payload, "knowledge_search", _apply_cap(hits, cap), total=4, budget=budget))

        floor_size = size_at(SEARCH_TEXT_FLOOR, 9_999)  # everything at the floor fits from here up
        # The budgets with teeth are the step-downs themselves: a budget equal
        # to the size AT a field's length is met by a cap of exactly that
        # length, but the size one below it is ~70 chars larger, so a plain
        # binary search that probes there concludes "does not fit" and ends
        # up short (measured: budget 3724 → cap 430 / size 3722 instead of the
        # optimum 3724). The spread-out budgets then cover the ordinary case.
        boundaries = [size_at(length, 9_999) for length in (450, 700, 1_200)]
        for budget in boundaries + [floor_size + d for d in (300, 900, 1_700, 2_500, 3_100)]:
            out = compact_search_results(payload, "knowledge_search", budget=budget)
            assert wire_size(out) <= budget
            assert len(out["results"]) == 4, budget  # shortened, never dropped, at these budgets
            best_size = max(s for s in (size_at(c, budget) for c in range(SEARCH_TEXT_FLOOR, 3_000)) if s <= budget)
            assert wire_size(out) == best_size, (budget, wire_size(out), best_size)

    def test_cuts_as_little_as_the_budget_requires(self):
        """The cap is the LARGEST that fits, not the first power-of-two below
        it: at k=10 a halving cut left 40% of the budget idle (12.6k of 20k)
        and every passage at 800 chars when ~1.5k would have fit."""
        payload = _incident_payload()
        out = compact_search_results(payload, "knowledge_search")
        used = wire_size(out)
        assert DEFAULT_SEARCH_MAX_CHARS * 0.9 < used <= DEFAULT_SEARCH_MAX_CHARS
        # One more character per field and it would not fit.
        text_len = len(out["results"][0]["text"]) - 1  # minus the mark
        assert text_len > 1_200
        from src.mcp_tooling import _apply_cap, _with_results

        one_more = _with_results(
            payload,
            "knowledge_search",
            _apply_cap(payload["results"], text_len + 1),
            total=10,
            budget=DEFAULT_SEARCH_MAX_CHARS,
        )
        assert wire_size(one_more) > DEFAULT_SEARCH_MAX_CHARS
