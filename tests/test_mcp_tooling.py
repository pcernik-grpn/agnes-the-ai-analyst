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
    compact_graph_result,
    compact_listing,
    compact_search_results,
    ensure_output_size,
    ensure_query_output_size,
    max_output_chars,
    paginate_text,
    paginate_text_response,
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
        # The read tool pages: the note must not promise the document from
        # one call, or a hit past page one sends the model to the wrong text.
        assert "next_offset" in out["truncated_note"]
        assert "in full" not in out["truncated_note"]
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

    def test_a_paragraph_long_query_is_shortened_and_the_hits_survive(self):
        """Devin Review on #2046: both endpoints echo the caller's query with
        no maximum length, so a pasted paragraph could keep the response over
        budget after every hit was dropped. The query takes part in the same
        cap search as the hit text — and the hits, which are the answer, are
        kept."""
        payload = _incident_payload(3)
        payload["query"] = "kůň " * 8_000  # 32k chars, on its own over the budget
        out = compact_search_results(payload, "knowledge_search")
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS
        assert len(out["results"]) == 3
        assert out["truncated_fields"] == ["query"]
        assert out["query"].endswith("…") and len(out["query"]) < 32_000
        assert "response's own query field(s) were shortened" in out["truncated_note"]
        # The hits were cut only as far as the shared cap required, not dropped.
        assert all(h["corpus_id"] == "col_0123456789abcdef" for h in out["results"])

    def test_empty_results_with_an_oversized_query_still_fit(self, monkeypatch):
        """An empty result also echoes the query, plus the long empty-result
        hint — both are envelope text and both come under the cap. Exercised
        through the env-configured budget path."""
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "3000")
        hint = (
            "Searched 3 collection(s) and 40 table(s), plus knowledge notes and the glossary, and found no match. " * 40
        )
        payload = {"query": "x" * 10_000, "results": [], "retrieval": "hybrid", "searched_collections": 3, "hint": hint}
        out = compact_search_results(payload, "knowledge_search")
        assert wire_size(out) <= 3000
        assert out["results"] == []
        assert set(out["truncated_fields"]) == {"query", "hint"}
        assert out["hint"].startswith("Searched 3 collection(s)") and out["hint"].endswith("…")
        assert out["retrieval"] == "hybrid" and out["searched_collections"] == 3

    def test_a_budget_below_the_envelope_returns_a_small_actionable_note(self):
        """Below the minimal envelope nothing can fit — a misconfiguration.
        The answer is a tiny note naming the knob, never an oversized payload
        the client would refuse (the failure mode this helper exists for)."""
        payload = _incident_payload(4)
        # 300 chars is below even "no results, query at the floor, the note".
        out = compact_search_results(payload, "knowledge_search", budget=300)
        assert wire_size(out) <= 300
        assert out == {
            "results": [],
            "truncated": True,
            "truncated_note": out["truncated_note"],
        }
        assert "4 result(s) withheld" in out["truncated_note"]
        assert SEARCH_MAX_CHARS_ENV in out["truncated_note"]

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


# ── generic listing compaction (MCP output-budget sweep, 2026-09) ─────────────
#
# compact_search_results (above) is the search-tool contract, locked to a
# `results` list and the fixed SEARCH_TEXT_FIELDS set. compact_listing is its
# generalization for a tool whose growable field has a different name and
# different prose fields (`skills[].body`, `models[].description`,
# `files[].processing_detail`, `claims[].quote`, ...). Same algorithm, same
# disclosure contract, no default `next_step` — every caller must say what
# to do next.


def _widget(i: int, note_len: int = 3_000) -> dict:
    return {"id": f"w{i:03d}", "name": f"widget-{i}", "note": ("lorem ipsum dolor sit amet. " * 200)[:note_len]}


class TestCompactListing:
    def test_fitting_payload_returned_untouched(self):
        payload = {"widgets": [_widget(0, 50)], "total": 1}
        out = compact_listing(
            payload, "widget_list", list_field="widgets", text_fields=("note",), budget=20_000, next_step="narrow it."
        )
        assert out is payload

    def test_a_non_default_list_field_is_shortened_then_kept(self):
        payload = {"widgets": [_widget(i) for i in range(10)], "total": 10}
        out = compact_listing(
            payload,
            "widget_list",
            list_field="widgets",
            text_fields=("note",),
            budget=6_000,
            shortened_note="{shortened} of {total} widgets carry shortened text",
            next_step="lower `limit` or narrow the filter.",
        )
        assert wire_size(out) <= 6_000
        assert out["truncated"] is True
        assert len(out["widgets"]) == 10  # shortened, not dropped, at this budget
        assert "10 of 10 widgets carry shortened text" in out["truncated_note"]
        assert "lower `limit`" in out["truncated_note"]
        assert out["total"] == 10  # fields outside list_field/envelope_fields pass through

    def test_default_note_wording_says_results_unless_overridden(self):
        """The default templates are the search-tool wording, verbatim —
        reused as-is by a caller with no reason to say it differently. A
        caller with a differently-named list still gets an honest, if
        generic, disclosure without having to write its own templates."""
        payload = {"widgets": [_widget(i) for i in range(10)]}
        out = compact_listing(
            payload, "widget_list", list_field="widgets", text_fields=("note",), budget=6_000, next_step="n/a"
        )
        assert "10 of 10 results carry shortened text" in out["truncated_note"]
        assert out["widgets"] is not payload["widgets"]  # still the widgets list, just under the generic key

    def test_drops_from_the_tail_and_reports_the_limit_that_would_fit(self):
        payload = {"widgets": [_widget(i) for i in range(10)]}
        out = compact_listing(
            payload,
            "widget_list",
            list_field="widgets",
            text_fields=("note",),
            budget=1_200,
            dropped_note="{dropped} of {total} widgets were dropped — pass limit={kept} next time to avoid this cap",
            next_step="lower `limit` or narrow the filter.",
        )
        kept = out["widgets"]
        assert 0 < len(kept) < 10
        assert [w["id"] for w in kept] == [w["id"] for w in payload["widgets"][: len(kept)]]
        assert f"pass limit={len(kept)} next time" in out["truncated_note"]
        assert wire_size(out) <= 1_200

    def test_no_text_fields_still_drops_from_the_tail(self):
        """A list whose items carry no shortenable prose (e.g. small fixed
        fields only) skips straight to count-based dropping."""
        payload = {"rows": [{"id": i, "blob": "x" * 400} for i in range(50)]}
        out = compact_listing(payload, "row_list", list_field="rows", budget=2_000, next_step="lower `limit`.")
        assert wire_size(out) <= 2_000
        assert 0 < len(out["rows"]) < 50
        assert out["truncated"] is True

    def test_zero_budget_disables(self):
        payload = {"widgets": [_widget(i) for i in range(10)]}
        assert compact_listing(payload, "x", list_field="widgets", budget=0, next_step="n/a") is payload

    def test_non_matching_shape_passes_through(self):
        assert compact_listing("plain", "x", list_field="widgets", budget=10, next_step="n/a") == "plain"
        payload = {"other": "x" * 5_000}
        assert compact_listing(payload, "x", list_field="widgets", budget=100, next_step="n/a") is payload

    def test_next_step_is_a_required_keyword(self):
        with pytest.raises(TypeError):
            compact_listing({"widgets": []}, "x", list_field="widgets")  # type: ignore[call-arg]

    def test_envelope_fields_are_capped_alongside_the_list(self):
        payload = {"widgets": [_widget(0, 50)], "query": "q" * 8_000}
        out = compact_listing(
            payload,
            "widget_list",
            list_field="widgets",
            envelope_fields=("query",),
            budget=2_000,
            next_step="narrow `query`.",
        )
        assert wire_size(out) <= 2_000
        assert out["query"].endswith("…") and len(out["query"]) < 8_000

    def test_below_the_envelope_returns_a_small_actionable_note(self):
        """Below the minimal envelope nothing can fit — a misconfiguration.
        The answer is a tiny, FIXED note naming the knob (its own length is
        not itself bounded by the tiny budget that triggered it — same as
        compact_search_results's equivalent fallback), never an oversized
        payload the client would refuse."""
        payload = {"widgets": [_widget(i, 50) for i in range(4)]}
        out = compact_listing(
            payload, "widget_list", list_field="widgets", item_noun="widget", budget=40, next_step="n/a"
        )
        assert out == {
            "widgets": [],
            "truncated": True,
            "truncated_note": out["truncated_note"],
        }
        assert "4 widget(s) withheld" in out["truncated_note"]
        assert SEARCH_MAX_CHARS_ENV in out["truncated_note"]


# ── text pagination (documentation_api, semantic_model_get) ──────────────────


class TestPaginateText:
    def test_short_text_returned_whole_with_no_next_offset(self):
        page = paginate_text("hello world", budget=1_000)
        assert page == {
            "text": "hello world",
            "offset": 0,
            "next_offset": None,
            "total_chars": 11,
            "truncated": False,
        }

    def test_pages_through_with_offset_chaining(self):
        text = "abcdefghij" * 10  # 100 chars
        page1 = paginate_text(text, budget=30)
        assert page1["truncated"] is True
        assert page1["next_offset"] == 30
        assert page1["text"] == text[:30]

        page2 = paginate_text(text, offset=page1["next_offset"], budget=30)
        assert page2["text"] == text[30:60]
        assert page2["next_offset"] == 60

        pages = [page1, page2]
        offset = page2["next_offset"]
        while offset is not None:
            p = paginate_text(text, offset=offset, budget=30)
            pages.append(p)
            offset = p["next_offset"]
        assert "".join(p["text"] for p in pages) == text
        assert pages[-1]["truncated"] is False

    def test_negative_offset_is_clamped(self):
        assert paginate_text("hello", offset=-5, budget=1_000)["offset"] == 0

    def test_budget_zero_returns_everything_from_offset(self):
        text = "x" * 500_000
        page = paginate_text(text, budget=0)
        assert page["text"] == text
        assert page["truncated"] is False

    def test_env_budget_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv(SEARCH_MAX_CHARS_ENV, "10")
        page = paginate_text("0123456789ABCDEF")
        assert page["text"] == "0123456789"
        assert page["next_offset"] == 10


class TestPaginateTextResponse:
    """`paginate_text` slices by raw character count; a caller that wraps the
    page in its own dict (`documentation_api`, `semantic_model_get`) can
    still overshoot the wire budget once JSON-escaping is counted (a
    markdown newline costs two characters once serialized) plus the
    wrapper's own keys — `paginate_text_response` is the fix: it verifies
    the ASSEMBLED response, not just the raw page."""

    def _assemble(self, page: dict) -> dict:
        out = {
            "content": page["text"],
            "offset": page["offset"],
            "next_offset": page["next_offset"],
            "total_chars": page["total_chars"],
            "truncated": page["truncated"],
        }
        if page["truncated"]:
            out["truncated_note"] = f"call again with offset={page['next_offset']}"
        return out

    def test_small_text_passes_through(self):
        out = paginate_text_response("hello world", 0, self._assemble, budget=1_000)
        assert out["content"] == "hello world"
        assert out["truncated"] is False

    def test_newline_heavy_text_still_fits_the_wire_budget(self):
        """The reproduction: raw-length slicing alone (no escaping
        awareness) put a real ~20k-char documentation_api page over a
        20,000-char budget once wrapped and JSON-escaped."""
        text = '# Heading\n\nSome body text with `code` and "quotes".\n' * 2_000
        out = paginate_text_response(text, 0, self._assemble, budget=DEFAULT_SEARCH_MAX_CHARS)
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS

    def test_pages_chain_to_the_exact_original_text(self):
        text = 'line one\nline two with "quotes" and \\backslash\\.\n' * 500
        pages = []
        offset = 0
        while True:
            out = paginate_text_response(text, offset, self._assemble, budget=2_000)
            pages.append(out)
            assert wire_size(out) <= 2_000
            if not out["truncated"]:
                break
            offset = out["next_offset"]
        assert "".join(p["content"] for p in pages) == text

    def test_zero_budget_disables_the_check(self):
        text = "x" * 500_000
        out = paginate_text_response(text, 0, self._assemble, budget=0)
        assert out["content"] == text

    def test_escape_heavy_text_still_fits_within_budget(self):
        """A page of nothing but backslashes: JSON-escaping doubles every
        character's wire cost (each ``\\`` becomes ``\\\\`` on the wire), so
        ``overshoot`` exceeds the raw page length and the naive
        ``shrink_to = len(page) - overshoot - 8`` goes negative. The old
        code treated that as "give up" and returned the still-oversized
        page — exactly the failure this helper exists to prevent. The fix
        must retry from a small positive floor instead."""
        text = "\\" * 50_000
        out = paginate_text_response(text, 0, self._assemble, budget=2_000)
        assert wire_size(out) <= 2_000

    def test_control_character_heavy_text_still_fits_within_budget(self):
        """Worse than a backslash: an unescaped control character costs SIX
        characters on the wire (``\\u00XX``), a ~6x overshoot ratio."""
        text = "\x01\x02\x03\x04\x05\x06\x07" * 10_000
        out = paginate_text_response(text, 0, self._assemble, budget=2_000)
        assert wire_size(out) <= 2_000


# ── fact-graph compaction (fact_edges, fact_neighbors) ────────────────────────


def _edge(i: int, quote_len: int = 0) -> dict:
    edge: dict = {"id": f"e{i:03d}", "src": f"n{i:03d}a", "dst": f"n{i:03d}b", "type": "knows", "attrs": {}}
    if quote_len:
        edge["claims"] = [{"quote": ("evidence text. " * 300)[:quote_len], "id": f"cl{i}"}]
    return edge


def _node(node_id: str) -> dict:
    return {"id": node_id, "type": "person", "aliases": [], "attrs": {}, "claim_count": 1, "quote_count": 1}


def _graph_payload(n: int, *, quote_len: int = 0) -> dict:
    edges = [_edge(i, quote_len) for i in range(n)]
    node_ids = {edges[i]["src"] for i in range(n)} | {edges[i]["dst"] for i in range(n)}
    nodes = [_node(nid) for nid in sorted(node_ids)]
    return {"nodes": nodes, "edges": edges, "truncated": {"depth": False, "fanout": False, "result": False}}


class TestCompactGraphResult:
    def test_small_graph_returned_untouched(self):
        payload = _graph_payload(1)
        out = compact_graph_result(payload, "fact_edges", budget=20_000, next_step="n/a")
        assert out is payload

    def test_shortens_claim_quotes_before_dropping_edges(self):
        payload = _graph_payload(20, quote_len=1_500)
        assert wire_size(payload) > 20_000
        out = compact_graph_result(payload, "fact_edges", budget=20_000, next_step="lower `limit`.")
        assert wire_size(out) <= 20_000
        assert len(out["edges"]) == 20  # kept every edge, only shortened the quotes
        assert any(e["claims"][0]["quote"].endswith("…") for e in out["edges"])
        assert out["truncated"]["output"] is True
        # The pre-existing structured flags survive untouched.
        assert out["truncated"]["depth"] is False

    def test_drops_edges_from_the_tail_and_prunes_unreferenced_nodes(self):
        payload = _graph_payload(200)
        out = compact_graph_result(payload, "fact_edges", budget=3_000, next_step="lower `limit`.")
        kept_edges = out["edges"]
        assert 0 < len(kept_edges) < 200
        # Ranked/discovery order preserved, cut from the tail.
        assert [e["id"] for e in kept_edges] == [e["id"] for e in payload["edges"][: len(kept_edges)]]
        referenced = {e["src"] for e in kept_edges} | {e["dst"] for e in kept_edges}
        assert {n["id"] for n in out["nodes"]} == referenced
        assert wire_size(out) <= 3_000
        assert out["truncated"]["output"] is True
        assert "edges were dropped" in out["truncated_note"]

    def test_never_replaces_the_existing_truncated_dict_with_a_bare_bool(self):
        payload = _graph_payload(200)
        out = compact_graph_result(payload, "fact_neighbors", budget=3_000, next_step="n/a")
        assert isinstance(out["truncated"], dict)
        assert out["truncated"]["result"] is False
        assert out["truncated"]["output"] is True

    def test_zero_budget_disables(self):
        payload = _graph_payload(50)
        assert compact_graph_result(payload, "fact_edges", budget=0, next_step="n/a") is payload

    def test_never_raises_even_when_nothing_fits(self):
        payload = _graph_payload(50)
        out = compact_graph_result(payload, "fact_edges", budget=10, next_step="n/a")
        assert isinstance(out, dict)
        assert out["truncated"]["output"] is True

    def test_input_payload_is_never_mutated(self):
        payload = _graph_payload(20, quote_len=1_500)
        snapshot = json.dumps(payload, sort_keys=True)
        compact_graph_result(payload, "fact_edges", budget=3_000, next_step="n/a")
        assert json.dumps(payload, sort_keys=True) == snapshot

    def test_required_root_with_no_edges_survives_compaction(self):
        """`fact_neighbors` always seeds `nodes` with the queried root, even
        when it has no visible edges. Deriving kept nodes purely from
        surviving edge endpoints drops it — the caller asked about that
        exact fact and gets back an empty graph. The root must be kept
        regardless of edge survival."""
        root = {
            "id": "root1",
            "type": "person",
            "aliases": [],
            "attrs": {"bio": "x" * 20_000},
            "claim_count": 1,
            "quote_count": 1,
        }
        payload = {"nodes": [root], "edges": [], "truncated": {"depth": False, "fanout": False, "result": False}}
        assert wire_size(payload) > 3_000
        out = compact_graph_result(
            payload, "fact_neighbors", budget=3_000, next_step="n/a", required_node_ids={"root1"}
        )
        assert [n["id"] for n in out["nodes"]] == ["root1"]

    def test_required_root_survives_alongside_edge_derived_nodes(self):
        """The root is additive on top of edge-derived nodes, not a
        replacement for them — a fitting graph keeps both."""
        payload = _graph_payload(20, quote_len=1_500)
        root_id = payload["edges"][0]["src"]
        out = compact_graph_result(
            payload, "fact_neighbors", budget=20_000, next_step="n/a", required_node_ids={root_id}
        )
        assert root_id in {n["id"] for n in out["nodes"]}

    def test_required_node_id_absent_from_payload_is_ignored(self):
        """A required id that never appears in `payload["nodes"]` must not
        invent a node out of thin air."""
        payload = _graph_payload(20, quote_len=1_500)
        out = compact_graph_result(
            payload, "fact_neighbors", budget=3_000, next_step="n/a", required_node_ids={"does-not-exist"}
        )
        assert "does-not-exist" not in {n["id"] for n in out["nodes"]}
        assert wire_size(out) <= 3_000
