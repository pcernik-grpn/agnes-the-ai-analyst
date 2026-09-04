"""The `sources` block, and the claim it makes being checkable.

The point of the block is not that an answer *says* where a number came from —
a prose sentence did that, unreliably. It is that the saying is parseable, so
absence is visible and a wrong claim can be contradicted by the turn's own tool
calls.

These tests are therefore mostly about the unflattering paths: no block, a
block naming a table nothing queried, a block with only assumptions. The happy
path is one case; the ways it can be wrong are the rest.
"""

from __future__ import annotations

import pytest

from app.chat.sources import (
    SourceClaim,
    extract_block,
    parse_claims,
    verdict,
)

TOOL_CALLS = [
    {"tool": "Bash", "args": {"command": 'agnes query --sql "SELECT month, mrr FROM mrr ORDER BY 1"'}},
    {"tool": "Bash", "args": {"command": "agnes catalog --metrics --show sales_revenue/mrr"}},
]


def _answer(block: str) -> str:
    return f"MRR is $1,126,632.\n\n```sources\n{block}\n```\n"


# ── parsing ─────────────────────────────────────────────────────────────────


def test_block_is_found_and_claims_keep_their_order():
    body = extract_block(_answer("table: mrr\nmetric: sales_revenue/mrr"))
    assert body is not None
    claims = parse_claims(body)
    assert [(c.kind, c.ref) for c in claims] == [("table", "mrr"), ("metric", "sales_revenue/mrr")]


def test_backticks_and_stray_lines_do_not_cost_the_reader_the_block():
    """A model reaching for markdown habits inside the block, plus a blank line
    and a line that is not a claim at all. None of it is an error worth
    discarding real provenance over."""
    claims = parse_claims("table: `mrr`\n\nsomething the model wrote\nmetric: sales_revenue/mrr\n")
    assert [(c.kind, c.ref) for c in claims] == [("table", "mrr"), ("metric", "sales_revenue/mrr")]


def test_a_repeated_claim_is_shown_once():
    claims = parse_claims("table: mrr\ntable: mrr\n")
    assert len(claims) == 1


def test_no_block_is_not_an_empty_block():
    """The distinction the whole feature rests on: an answer that declared
    nothing must not look like one that declared and had nothing to say."""
    v = verdict("MRR is $1,126,632.", TOOL_CALLS)
    assert v.declared is False
    assert v.claims == []


# ── verification ────────────────────────────────────────────────────────────


def test_a_claim_the_tool_calls_support_is_verified():
    v = verdict(_answer("table: mrr\nmetric: sales_revenue/mrr"), TOOL_CALLS)
    assert v.declared is True
    assert all(c.verified for c in v.claims)
    assert v.unverified == []


def test_a_table_nothing_queried_is_reported_unverified():
    """The case that separates this from a prompt rule. The answer claims a
    table; the record of what ran does not contain it; the reader is told."""
    v = verdict(_answer("table: mrr\ntable: hr_headcount"), TOOL_CALLS)
    assert [c.ref for c in v.unverified] == ["hr_headcount"]


def test_an_assumption_is_never_marked_unverified():
    """There is nothing to check a stated assumption against. Marking honest
    assumptions as unverified would train the reader to ignore the badge."""
    v = verdict(_answer("table: mrr\nassumption: excludes contractors"), TOOL_CALLS)
    by_kind = {c.kind: c for c in v.claims}
    assert by_kind["assumption"].verified is None
    assert by_kind["table"].verified is True


def test_a_metric_cited_by_bare_name_still_verifies():
    """`sales_revenue/mrr` and `mrr` are the same metric; a correct citation
    must not read as unverified over punctuation."""
    v = verdict(_answer("metric: sales_revenue/mrr"), [{"args": {"command": "agnes catalog --metrics --show mrr"}}])
    assert v.claims[0].verified is True


def test_no_tool_calls_means_nothing_is_verified():
    """An answer that ran nothing cannot have a supported claim — and this is
    the shape a fabricated citation takes."""
    v = verdict(_answer("table: mrr"), None)
    assert v.claims[0].verified is False


@pytest.mark.parametrize(
    "calls",
    [
        ["agnes query --sql 'SELECT * FROM mrr'"],  # plain strings
        [{"tool": "x", "args": {"nested": {"sql": "FROM mrr"}}}],  # nested dicts
        [{"unserializable": object()}, {"args": {"command": "FROM mrr"}}],  # one bad entry
    ],
    ids=["strings", "nested", "unserializable-entry"],
)
def test_tool_call_shapes_do_not_break_verification(calls):
    """tool_calls is untyped JSON off the wire. A shape the serializer chokes
    on must degrade to 'unverified', never to a 500 on the messages endpoint."""
    v = verdict(_answer("table: mrr"), calls)
    assert v.claims[0].verified is True


def test_verdict_serializes_for_the_wire():
    """A table/metric claim keeps the three-key shape it always had; the two
    TCRD-289 keys ride on assumptions only, present even when unset so the
    client reads one shape."""
    v = verdict(_answer("table: mrr\nassumption: x"), TOOL_CALLS)
    assert v.to_dict() == {
        "declared": True,
        "claims": [
            {"kind": "table", "ref": "mrr", "verified": True},
            {"kind": "assumption", "ref": "x", "verified": None, "origin": None, "why": None},
        ],
    }


def test_claim_is_immutable():
    """Verdicts are recomputed per read; a mutable claim would invite a caller
    to 'fix' one in place and diverge from what the tool calls say."""
    with pytest.raises(Exception):
        SourceClaim(kind="table", ref="mrr").ref = "other"  # type: ignore[misc]


class TestTheManagerFeedsTheVerdictRealToolCalls:
    """Devin Review on this PR: the pair was always half-empty.

    `ChatManager` stamped `frame["sources"]` from `frame.get("tool_calls")`,
    but the runner emits each call as its own `tool_call` frame and its final
    `assistant_message` carries only content/tokens/model — so the haystack
    was empty and every verifiable claim came back `verified=False`. An amber
    UNVERIFIED badge on correct answers is worse than no badge: it teaches
    the reader to ignore it.

    The reload path (`GET /sessions/{id}/messages`) recomputed the same
    verdict from the persisted `tool_calls`, which was being stored as `None`
    for the same reason — so both surfaces agreed, and both were wrong.
    Source-level, because standing up a live manager turn to assert one
    stamped field is far more machinery than the invariant is worth.
    """

    import pathlib

    SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "chat" / "manager.py"

    def _stamp_block(self) -> str:
        src = self.SOURCE.read_text(encoding="utf-8")
        i = src.index('if frame.get("type") == "assistant_message":')
        # A READING window, not an assertion: it has to reach past the stamp
        # into the buffer accumulation below it (`"tool_call"`), and the
        # comments in between are load-bearing prose that grows. Sized with
        # room rather than to the byte, so a paragraph added next to the
        # stamp fails the invariant it breaks and not this slice.
        return src[i : i + 6000]

    def test_the_verdict_is_fed_from_the_turn_buffer(self):
        block = self._stamp_block()
        assert "live.turn_buffer" in block and '"tool_call"' in block, (
            "the verdict is computed against a field the runner never sets"
        )

    def test_only_the_tool_and_args_are_persisted(self):
        """This list rides on the message row forever — the frame envelope
        (`type`, `frame_seq`, ids) would be dead weight on every message, and
        `chat.js::formatToolCall` reads `{tool, args}` anyway.

        The projection moved into ``app/chat/message_parts.py`` when the row
        gained the ordered ``parts`` array: `tool_calls` is now derived from
        `parts` rather than re-walking the buffer, so the two cannot disagree
        about which calls a turn made. The invariant is unchanged, so it is
        asserted where it now lives."""
        from app.chat.message_parts import parts_to_tool_calls

        block = self._stamp_block()
        assert "parts_to_tool_calls(" in block, "tool_calls must be the projection of parts, not a second walk"

        # Behavioural, not textual: a tool part carrying result/state/envelope
        # noise must project down to exactly {tool, args}.
        calls = parts_to_tool_calls(
            [
                {"type": "text", "text": "prose"},
                {
                    "type": "tool",
                    "tool_use_id": "c1",
                    "tool": "Bash",
                    "args": {"command": "agnes catalog"},
                    "state": "output-available",
                    "result": "35 tables",
                    "is_error": False,
                },
                # No tool name — would render as `tool: undefined`.
                {"type": "tool", "tool_use_id": "c2", "tool": None, "args": {}},
            ]
        )
        assert calls == [{"tool": "Bash", "args": {"command": "agnes catalog"}}], (
            "only tool and args survive, and a nameless call is dropped"
        )

    def test_the_calls_are_attached_before_the_verdict_is_computed(self):
        """Order is the whole fix — a stamp computed first sees nothing."""
        block = self._stamp_block()
        assert block.index('frame["tool_calls"] =') < block.index('frame["sources"] ='), (
            "tool_calls must be attached before sources_verdict reads them"
        )

    def test_the_same_calls_reach_the_persisted_message(self):
        """Otherwise a reload disagrees with the live turn."""
        src = self.SOURCE.read_text(encoding="utf-8")
        assert 'tool_calls=frame.get("tool_calls")' in src
        assert src.index('frame["tool_calls"] =') < src.index('tool_calls=frame.get("tool_calls")')


class TestPushSinksDoNotShowTheRawFence:
    """Devin Review on this PR: only the web client stripped it.

    The web draws chips from the server's verdict and removes the fence
    (`stripSourcesFence`). A push sink has neither, so where an answer used
    to end in a readable `Sources:` line it would now end in a code block of
    machinery — on every answer, for every Slack and Telegram user.
    """

    def test_strip_block_removes_the_fence_and_keeps_the_prose(self):
        from app.chat.sources import strip_block

        content = "Revenue was 4.2M.\n\n```sources\ntable: orders\n```"
        assert strip_block(content) == "Revenue was 4.2M."

    def test_strip_block_is_a_no_op_without_a_fence(self):
        from app.chat.sources import strip_block

        assert strip_block("just prose") == "just prose"
        assert strip_block("") == ""

    def test_the_verdict_still_parses_out_of_the_unstripped_content(self):
        """The strip must NOT reach persistence — the verdict is derived from
        the saved content on every reload, so a stripped save loses the chips."""
        from app.chat.sources import verdict

        content = "Revenue was 4.2M.\n\n```sources\ntable: orders\n```"
        assert verdict(content, [{"tool": "query", "args": {"sql": "select * from orders"}}]).declared

    def test_the_slack_sink_strips_on_both_of_its_post_paths(self):
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "slack_bot" / "sink.py").read_text(
            encoding="utf-8"
        )
        assert src.count('strip_block(data.get("content", ""))') == 2, (
            "both the streaming reply and the ephemeral responder post content"
        )
        assert 'content = data.get("content", "")' not in src, "a post path still sends the raw fence"


class TestTheBlockLocatorIsLinear:
    """Devin Review on #1239: `verdict()` now runs on every assistant message
    of every history read, over model output.

    The old body pattern was non-greedy with DOTALL, so every UNTERMINATED
    opening fence made the engine rescan to end-of-string — O(occurrences x
    length). The repo's rule is that regexes over untrusted text stay linear.
    """

    def test_the_body_is_not_matched_with_a_regex(self):
        import inspect

        from app.chat import sources

        # Compare the COMPILED patterns, not the source text — the module
        # comment quotes the old pattern to explain why it went.
        assert not any("(.*?)" in getattr(v, "pattern", "") for v in vars(sources).values() if hasattr(v, "pattern")), (
            "a non-greedy body pattern is back"
        )
        src = inspect.getsource(sources)
        assert "_OPEN_RE" in src and "content.find(_CLOSE" in src

    def test_many_fences_are_handled_in_linear_time(self):
        """500 opening fences over 100 KB. The point is that this returns —
        the old pattern rescanned to end-of-string per occurrence."""
        import time

        from app.chat.sources import strip_block

        hostile = ("```sources\n" + "x" * 200) * 500
        started = time.monotonic()
        out = strip_block(hostile)
        assert time.monotonic() - started < 1.0, "the locator is not linear"
        assert "```sources" not in out

    def test_a_lone_unterminated_fence_leaves_the_text_alone(self):
        """With nothing after it to act as a closing fence, it is not a block."""
        from app.chat.sources import extract_block, strip_block

        lone = "answer\n\n```sources\ntable: orders"
        assert extract_block(lone) is None
        assert strip_block(lone) == lone

    def test_an_unterminated_fence_is_not_a_block(self):
        """Treating it as one would let a truncated answer swallow the rest."""
        from app.chat.sources import extract_block, strip_block

        text = "Revenue was 4.2M.\n\n```sources\ntable: orders"
        assert extract_block(text) is None
        assert strip_block(text) == text

    def test_every_complete_block_is_still_stripped(self):
        from app.chat.sources import strip_block

        two = "a\n\n```sources\nt: x\n```\n\nb\n\n```sources\nt: y\n```"
        out = strip_block(two)
        assert "```sources" not in out
        assert "a" in out and "b" in out


class TestAnAssumptionSaysWhereItCameFromAndWhy:
    """TCRD-289: six `assumes …` chips under an answer, and the reader could
    not tell where any of them originated or why it was made.

    The statement alone cannot carry that — "signed date proxied by close
    date" reads the same whether the user asked for it, a definition says so,
    the CRM has no better column, or the model guessed. So the line grew two
    keyed segments, `| origin: … | why: …`, and this is the parse: the origin
    is a CLOSED vocabulary the badge copy is keyed on, the why is one
    sentence, and a line with neither is still an assumption — shown as
    "origin not stated", the block's own visible-absence rule applied to
    itself.
    """

    def test_a_structured_line_splits_into_statement_origin_and_why(self):
        (c,) = parse_claims('assumption: active employees only | origin: user | why: you asked about "the team"')
        assert (c.kind, c.ref) == ("assumption", "active employees only")
        assert c.origin == "user"
        assert c.why == 'you asked about "the team"'

    def test_a_legacy_line_is_still_an_assumption_with_nothing_stated(self):
        """History written before this change, and a model that ignores the
        two segments, must not lose the assumption — only its badge."""
        (c,) = parse_claims("assumption: excludes contractors")
        assert c.ref == "excludes contractors"
        assert c.origin is None and c.why is None

    def test_the_segments_may_come_in_either_order(self):
        (c,) = parse_claims("assumption: proxied by close date | why: no SOW date in the CRM | origin: data")
        assert (c.origin, c.why) == ("data", "no SOW date in the CRM")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("user", "user"),
            ("User", "user"),
            ("question", "user"),
            ("definition", "definition"),
            ("semantic model", "definition"),
            ("glossary", "definition"),
            ("data", "data"),
            ("missing data", "data"),
            ("judgment", "judgment"),
            ("judgement", "judgment"),
            ("my own", "judgment"),
            ("`data`", "data"),
        ],
    )
    def test_the_origin_is_normalized_to_the_closed_vocabulary(self, raw, expected):
        from app.chat.sources import ASSUMPTION_ORIGINS

        (c,) = parse_claims(f"assumption: s | origin: {raw}")
        assert c.origin == expected
        assert c.origin in ASSUMPTION_ORIGINS

    @pytest.mark.parametrize("raw", ["Salesforce", "the CRM", "", "n/a"])
    def test_an_origin_outside_the_vocabulary_is_not_stated_rather_than_guessed(self, raw):
        """The badge is a category with fixed copy. A model's free-text origin
        goes nowhere near it — the reader is told the origin was not stated,
        which is true of that line in the only sense the badge can express."""
        (c,) = parse_claims(f"assumption: s | origin: {raw} | why: w")
        assert c.origin is None
        assert c.why == "w", "an off-vocabulary origin must not cost the reader the rationale"

    def test_a_pipe_inside_the_statement_stays_in_the_statement(self):
        """`|` is the segment separator, but only in front of a recognized key.
        A pipe in prose — `A | B` naming two things — continues whatever it
        was inside, never becomes a phantom segment and is never dropped."""
        line = "assumption: shipping | billing country both count | origin: judgment | why: either | both mark CZ"
        (c,) = parse_claims(line)
        assert c.ref == "shipping | billing country both count"
        assert c.origin == "judgment"
        assert c.why == "either | both mark CZ"

    def test_an_empty_why_is_no_rationale(self):
        (c,) = parse_claims("assumption: s | origin: data | why:")
        assert c.why is None

    def test_duplicates_are_judged_on_the_statement(self):
        """The same assumption written twice with two rationales is one
        assumption; the reader gets the first."""
        claims = parse_claims("assumption: s | origin: user | why: a\nassumption: s | origin: data | why: b")
        assert len(claims) == 1
        assert (claims[0].origin, claims[0].why) == ("user", "a")

    def test_origin_and_why_survive_verification_and_reach_the_wire(self):
        v = verdict(_answer("table: mrr\nassumption: s | origin: judgment | why: w"), TOOL_CALLS)
        d = v.to_dict()
        assert d["claims"][1] == {"kind": "assumption", "ref": "s", "verified": None, "origin": "judgment", "why": "w"}
        assert set(d["claims"][0]) == {"kind", "ref", "verified"}, "table claims keep their wire shape"

    def test_table_and_metric_lines_are_not_split(self):
        """The segment grammar is the assumption line's alone — a `|` on a
        table line is part of the (odd) ref and is compared as written."""
        (c,) = parse_claims("table: a | origin: user")
        assert c.ref == "a | origin: user"
        assert c.origin is None

    def test_the_prompt_teaches_exactly_the_vocabulary_the_parser_accepts(self):
        """The parser's closed vocabulary and the prompt's list of allowed
        words are the same contract written twice. Pinned so one cannot gain
        a value the other never heard of."""
        import pathlib
        import re

        from app.chat.sources import ASSUMPTION_ORIGINS

        root = pathlib.Path(__file__).resolve().parents[1]
        md = (root / "app" / "initial_workspace_default" / "CLAUDE.md").read_text(encoding="utf-8")
        section = md[md.index("## Say where every number came from") :]
        section = section[: section.index("\n## ", 1)]
        assert "`origin:`" in section and "`why:`" in section
        for word in ASSUMPTION_ORIGINS:
            assert f"`{word}`" in section, f"the prompt does not offer {word!r}"
        # The example the model imitates most is the fenced one — it must show
        # the segments, not the bare legacy line.
        fence = re.search(r"```sources\n(.*?)```", section, re.DOTALL)
        assert fence and "| origin:" in fence.group(1) and "| why:" in fence.group(1)
        assert "origin not stated" in section, "the model is told what an omitted origin looks like to the reader"

    def test_the_persona_rail_teaches_the_same_vocabulary(self):
        """A persona-backed named agent replaces the workspace template with
        `build_profile`'s rails (Devin Review on #2047), so the contract has a
        third carrier. Same pin: every origin the parser accepts is offered
        there, and the example shows the segments."""
        import re

        from app.chat.agent_profile import PROVENANCE_RAILS
        from app.chat.sources import ASSUMPTION_ORIGINS

        for word in ASSUMPTION_ORIGINS:
            assert f"`{word}`" in PROVENANCE_RAILS, f"the persona rail does not offer {word!r}"
        fence = re.search(r"```sources\n(.*?)```", PROVENANCE_RAILS, re.DOTALL)
        assert fence and "| origin:" in fence.group(1) and "| why:" in fence.group(1)
        assert "origin not stated" in PROVENANCE_RAILS


class TestADocumentIsProvenanceNotAnAssumption:
    """A `document:` claim, and the misfiling it exists to stop.

    The block's vocabulary was SQL-shaped — `table:` and `metric:` — while a
    fact-graph answer rests on neither. Asked for the closest precedent to a
    prospect, an agent reading the document graph had exactly one legal slot
    left for "this came from `Acme_Week_2_Deliverable.pdf`", and used it: five
    `assumption:` lines carrying filenames and engagement ids, every one of
    them badged "origin not stated" because no origin in the vocabulary means
    "a source document". The last line said so outright — `assumption: No SQL
    tables queried, this answer is sourced entirely from the document fact
    graph` — which is a model reporting a schema mismatch, rendered as a
    caveat about method.

    Meanwhile the row above it read "Sources — none declared", truthfully by
    its own definition (provenance is judged on tables and metrics) and
    absurdly to anyone looking at the five named PDFs underneath.

    So `document:` is a first-class claim: it lands in the provenance row, it
    is checked against the turn's tool calls like the other two, and it takes
    the citations back out of the assumptions row — which shrinks to the
    method caveats TCRD-289 built it for.
    """

    # Arguments and results, kept apart the way the projections keep them
    # (`parts_to_tool_calls` / `parts_to_tool_results`). This fixture used to
    # fold the filename into an `args["output"]` key — a shape no projection
    # produces — so the happy path below passed on a haystack the live path
    # could not build, and the feature shipped unable to verify a document
    # cited by name. See `TestADocumentIsNamedByTheToolOutputNotTheCall`.
    FACT_CALLS = [
        {"tool": "Bash", "args": {"command": "agnes facts claims engagement:acme-rapid-roadmap"}},
        {"tool": "Bash", "args": {"command": "agnes facts search engagement acme"}},
    ]
    FACT_RESULTS = ["1. Acme_Rapid_Roadmap_Week_2_Deliverable.pdf — engagement:acme-rapid-roadmap"]

    def test_a_document_is_parsed_as_its_own_kind(self):
        (c,) = parse_claims("document: Acme_Discovery_Synthesis.docx")
        assert (c.kind, c.ref) == ("document", "Acme_Discovery_Synthesis.docx")

    def test_a_document_the_turn_read_is_verified(self):
        """Same contract as `table:`: the turn's tool calls are the record of
        what was actually read, and a fact tool naming the file is that."""
        v = verdict(
            _answer("document: Acme_Rapid_Roadmap_Week_2_Deliverable.pdf"),
            self.FACT_CALLS,
            self.FACT_RESULTS,
        )
        assert v.claims[0].verified is True

    def test_a_document_nothing_opened_is_reported_unverified(self):
        """The point of the whole feature, on the new kind: a plausible
        filename nothing ran on is exactly what a fabricated citation looks
        like, and the reader is told."""
        v = verdict(_answer("document: Never_Read_This.pdf"), self.FACT_CALLS, self.FACT_RESULTS)
        assert [c.ref for c in v.unverified] == ["Never_Read_This.pdf"]

    def test_a_fact_graph_subject_id_verifies_as_a_document(self):
        """`agnes facts claims` is addressed by subject id, so that id — not a
        filename — is often the most precise thing the answer can cite."""
        v = verdict(_answer("document: engagement:acme-rapid-roadmap"), self.FACT_CALLS)
        assert v.claims[0].verified is True

    def test_a_path_or_extension_does_not_cost_a_correct_citation(self):
        """The crude matcher errs toward accepting on purpose (see
        `_tool_call_haystack`). A citation that names the same document with a
        directory in front of it, or with the extension the tool output
        omitted, must not read as unverified over punctuation — the same
        latitude `metric:` already gets for `family/name`."""
        calls = [{"args": {"command": "read Acme_Discovery_Synthesis"}}]
        for ref in (
            "collections/acme/Acme_Discovery_Synthesis.docx",
            "Acme_Discovery_Synthesis.docx",
            "Acme_Discovery_Synthesis",
        ):
            v = verdict(_answer(f"document: {ref}"), calls)
            assert v.claims[0].verified is True, ref

    def test_a_document_reaches_the_wire_in_the_reference_shape(self):
        """Three keys, like a table or a metric — `origin`/`why` are the
        assumption line's alone, and a document is something the answer READ,
        never something it decided."""
        v = verdict(_answer("document: a.pdf"), self.FACT_CALLS)
        assert set(v.to_dict()["claims"][0]) == {"kind", "ref", "verified"}

    def test_a_document_line_is_not_split_on_pipes(self):
        (c,) = parse_claims("document: Q3 | Q4 review.pdf")
        assert c.ref == "Q3 | Q4 review.pdf"
        assert c.origin is None

    def test_an_answer_citing_documents_has_declared_a_source(self):
        """The bug as the reader met it: five cited PDFs above the words
        "none declared". A document is provenance, so it counts."""
        from app.chat.sources import VERIFIABLE_KINDS

        assert "document" in VERIFIABLE_KINDS
        v = verdict(_answer("document: a.pdf\ndocument: b.docx"), self.FACT_CALLS)
        assert [c.kind for c in v.claims] == ["document", "document"]
        assert not any(c.kind == "assumption" for c in v.claims)

    def test_every_prompt_carrier_offers_the_document_kind(self):
        """The parser's vocabulary and the text that teaches it are one
        contract with three carriers (the bundled workspace CLAUDE.md, the
        server-rendered template, and the persona rail). A kind the model is
        never told about is a kind it will keep smuggling into `assumption:`,
        which is the defect this fixes — so pin all three."""
        import pathlib

        from app.chat.agent_profile import PROVENANCE_RAILS

        root = pathlib.Path(__file__).resolve().parents[1]
        carriers = {
            "workspace CLAUDE.md": (root / "app" / "initial_workspace_default" / "CLAUDE.md").read_text(
                encoding="utf-8"
            ),
            "server template": (root / "config" / "claude_md_template.txt").read_text(encoding="utf-8"),
            "persona rail": PROVENANCE_RAILS,
        }
        for name, text in carriers.items():
            section = text[text.index("Say where every number came from") :]
            assert "`document:`" in section, f"{name} does not offer the document kind"
            fence = section[section.index("```sources") :]
            fence = fence[: fence.index("```", 3)]
            assert "document:" in fence, f"{name}'s example does not show a document line"
            assert "assumption" in section.lower()

    def test_a_derived_needle_can_never_be_short_enough_to_match_anything(self):
        """The peeling above is latitude, not a rubber stamp.

        Two degenerate refs turned the check into one, found by probing rather
        than by review: `document: docs/` peels to a basename of `""`, and
        `"" in haystack` is true of every haystack ever built — so a
        trailing slash verified against tool calls that touched nothing.
        `document: a.pdf` peels to the stem `"a"`, which appears in
        essentially any serialized tool call, so it verified the same way.

        Both are the one direction this module must not err in. A false
        "unverified" leaves a careful answer looking sloppy; a false
        "verified" is the badge lying about the only thing it exists to
        assert. So a DERIVED needle below `_MIN_DERIVED_NEEDLE` is dropped —
        the ref itself is always still matched in full, at any length.
        """
        calls = [{"args": {"command": "agnes catalog --json"}}]
        for ref in ("docs/", "a.pdf", "a/", "x.y", "/"):
            v = verdict(_answer(f"document: {ref}"), calls)
            assert v.claims[0].verified is False, f"{ref!r} matched a tool call that never touched it"

    def test_the_full_ref_is_matched_at_any_length(self):
        """The floor applies only to what peeling INVENTS. A short document
        genuinely named in a tool call still verifies on its own name."""
        v = verdict(_answer("document: a.pdf"), [{"args": {"command": "read a.pdf"}}])
        assert v.claims[0].verified is True

    def test_peeling_still_works_for_a_real_filename(self):
        """The regression guard for the fix: the latitude the peel exists for
        must survive the floor that stops it lying."""
        calls = [{"args": {"command": "read Acme_Discovery_Synthesis"}}]
        for ref in ("collections/acme/Acme_Discovery_Synthesis.docx", "Acme_Discovery_Synthesis.docx"):
            assert verdict(_answer(f"document: {ref}"), calls).claims[0].verified is True, ref


class TestADocumentIsNamedByTheToolOutputNotTheCall:
    """The half of `document:` that shipped broken: what the check can see.

    Reported from a live instance — six document chips, six amber
    UNVERIFIED badges, under a correct answer. The badge was not disagreeing
    with the model; it was reading a record the filenames could not be in.

    `verify()`'s haystack is built from the turn's tool CALLS, and the
    positionless projection those come from keeps `{tool, args}` and drops
    the result (`app/chat/message_parts.py::parts_to_tool_calls`). That is
    the right record for a `table:` — a table name is an INPUT: it is in the
    SQL the agent wrote. A document's name is an OUTPUT: no fact tool takes a
    filename argument (`fact_search` takes a query, `fact_claims` a subject
    id), so the file is named for the first time in the tool's own result.
    Checked against arguments alone, a document cited by filename could not
    verify on any turn, at any peeling.

    So the results reach the check too — for `document:` claims only. A
    result is a much weaker record of what a turn *did* than its arguments
    (`agnes catalog` returns every table name the caller can see, which
    would verify any `table:` the answer named), and the asymmetry is the
    point: it is granted exactly where the name cannot be anywhere else.
    """

    # The production shape: what the model passed IN, with no output folded
    # into it. The class above kept an `output` key inside `args` — where a
    # projection never puts one — so its happy path passed on a haystack the
    # live path could not build.
    CALLS = [
        {"tool": "Bash", "args": {"command": "agnes facts search engagement acme"}},
        {"tool": "Bash", "args": {"command": "agnes facts claims engagement:acme-rapid-roadmap"}},
    ]
    RESULTS = [
        "1. Acme_Rapid_Roadmap_Week_2_Deliverable.pdf — engagement:acme-rapid-roadmap",
        "claim: 4-week roadmap delivered (source: Acme_Discovery_Synthesis.docx)",
    ]

    def test_a_document_named_only_in_a_result_verifies(self):
        v = verdict(
            _answer("document: Acme_Rapid_Roadmap_Week_2_Deliverable.pdf"),
            self.CALLS,
            self.RESULTS,
        )
        assert v.claims[0].verified is True

    def test_without_the_results_the_same_citation_cannot_verify(self):
        """The bug, pinned: this is what the reader saw six times."""
        v = verdict(_answer("document: Acme_Rapid_Roadmap_Week_2_Deliverable.pdf"), self.CALLS)
        assert v.claims[0].verified is False

    def test_a_document_no_result_names_is_still_unverified(self):
        """Widening the record must not turn the check into a rubber stamp."""
        v = verdict(_answer("document: Never_Read_This.pdf"), self.CALLS, self.RESULTS)
        assert [c.ref for c in v.unverified] == ["Never_Read_This.pdf"]

    def test_a_table_named_only_in_a_result_does_not_verify(self):
        """The asymmetry is deliberate, not an oversight: a table name is an
        input, and a listing tool's OUTPUT names every table there is."""
        v = verdict(
            _answer("table: hr_headcount"),
            [{"tool": "Bash", "args": {"command": "agnes catalog --json"}}],
            ['[{"id": "hr_headcount"}, {"id": "mrr"}]'],
        )
        assert v.claims[0].verified is False

    def test_a_citation_carrying_its_own_description_still_verifies(self):
        """Observed in the same screenshot: the model writes the filename and
        then says what it is. The whole line is the ref, so nothing matched —
        a citation was penalised for being helpful."""
        v = verdict(
            _answer("document: Acme_Discovery_Synthesis.docx — 4-week AI Opportunity Assessment, week 1"),
            self.CALLS,
            self.RESULTS,
        )
        assert v.claims[0].verified is True

    def test_an_uploaded_file_verifies_under_the_name_the_user_saw(self):
        """`safeUploadName` in chat.js rewrites a dropped file's name before
        it is stored — spaces to underscores, a stamp before the extension —
        while the chat bubble, and therefore the model, keeps saying the name
        the user dropped. Matching those two spellings is not latitude; the
        rewrite is ours."""
        stored = [{"tool": "Read", "args": {"file_path": "uploads/AI_Opportunity_Assessment-20260904T151500-1.pptx"}}]
        v = verdict(_answer("document: AI Opportunity Assessment.pptx"), stored)
        assert v.claims[0].verified is True

    def test_a_degenerate_ref_does_not_survive_normalization(self):
        """The floor of `_MIN_DERIVED_NEEDLE` holds on the normalized
        spellings too — otherwise punctuation-insensitivity would hand back
        exactly the rubber stamp the floor exists to stop."""
        calls = [{"args": {"command": "agnes catalog --json"}}]
        for ref in ("docs/", "a.pdf", "a/", "x.y", "/"):
            v = verdict(_answer(f"document: {ref}"), calls, ['{"data": ["a", "b"]}'])
            assert v.claims[0].verified is False, f"{ref!r} matched a turn that never touched it"


class TestTheVerdictReadsTheSameTurnTheReaderSees:
    """The projection that carries results to the check, and both call sites.

    `parts` is the turn's ordered shape and the only place a result survives;
    `tool_calls` is its positionless projection. A second projection keeps
    the pair honest — the verdict must never be computed from a record the
    rendered transcript does not have.
    """

    def test_results_project_out_of_parts_in_call_order(self):
        from app.chat.message_parts import parts_to_tool_results

        parts = [
            {"type": "text", "text": "looking"},
            {"type": "tool", "tool": "Bash", "args": {"command": "a"}, "result": "first"},
            {"type": "tool", "tool": "Bash", "args": {"command": "b"}, "result": "second"},
        ]
        assert parts_to_tool_results(parts) == ["first", "second"]

    def test_a_turn_with_no_results_projects_to_none(self):
        from app.chat.message_parts import parts_to_tool_results

        assert parts_to_tool_results(None) is None
        assert parts_to_tool_results([{"type": "text", "text": "hi"}]) is None
        assert parts_to_tool_results([{"type": "tool", "tool": "Bash", "args": {}}]) is None

    def test_a_whole_turn_verifies_the_document_it_actually_read(self):
        """End to end over the real frame shapes: the runner's `tool_call` and
        `tool_result` frames, folded by `build_message_parts` exactly as the
        manager folds them, then judged. The unit tests above feed `verify()`
        the two lists directly; this is the seam where the filename either
        survives the projection or does not."""
        from app.chat.message_parts import build_message_parts, parts_to_tool_calls, parts_to_tool_results

        frames = [
            {"type": "token", "text": "Looking at the engagement documents.", "frame_seq": 1},
            {
                "type": "tool_call",
                "tool_use_id": "c1",
                "tool": "Bash",
                "args": {"command": "agnes facts search engagement acme"},
                "frame_seq": 2,
            },
            {
                "type": "tool_result",
                "tool_use_id": "c1",
                "result": "1. Acme_Rapid_Roadmap_Week_2_Deliverable.pdf (engagement:acme-rapid-roadmap)",
                "is_error": False,
                "frame_seq": 3,
            },
        ]
        parts = build_message_parts(frames)
        v = verdict(
            _answer("document: Acme_Rapid_Roadmap_Week_2_Deliverable.pdf\ndocument: Not_In_This_Turn.pdf"),
            parts_to_tool_calls(parts),
            parts_to_tool_results(parts),
        )
        assert [c.verified for c in v.claims] == [True, False]

    def test_both_call_sites_pass_the_results(self):
        """The live path (`assistant_message` in the manager) and the reload
        path (`GET /sessions/{id}/messages`) must agree — a badge that
        changes on refresh is worse than either verdict alone."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        for rel in ("app/chat/manager.py", "app/api/chat.py"):
            text = (root / rel).read_text(encoding="utf-8")
            i = text.index("sources_verdict(", text.index("import") + 1)
            while text[i - 1 : i] not in ("", "\n"):  # back up to the start of the statement
                i -= 1
            call = text[i : i + 400]
            assert "parts_to_tool_results" in call, f"{rel} computes the verdict without the results:\n{call}"
class TestAGlossaryTermIsEvidenceNotAnAssumption:
    """A `glossary:` claim, and the misfiling it exists to stop (#2258).

    The vocabulary offered `table:`, `metric:` and `document:`, so a governed
    business term the answer leaned on had exactly one legal slot left:
    `assumption: … | origin: definition`. That files a citation as a caveat
    about method — the same category error the prompt already warns about for
    a file ("the reader sees your evidence listed among your guesses"), and
    the reader meets a definition they could have opened among the answer's
    guesses instead of among its sources.

    A term is evidence. So `glossary:` is a first-class claim: it lands in the
    provenance row and is checked against the turn's tool calls like the other
    three.

    Its READ rule is its own, and deliberately not the metric's. `GET
    /api/glossary*` is any authenticated caller with no per-resource grant
    (business vocabulary, not data — see `app/api/glossary.py` and the same
    note on the semantic layer's glossary tab), while `GET /api/metrics` drops
    every metric bound to a table outside the caller's stack. Nothing in this
    module reads either registry, which is what keeps the verdict out of that
    split entirely; the chip's destination is where the client honours it (see
    `tests/test_chat_sources_ui.py`).
    """

    GLOSSARY_CALLS = [
        {"tool": "Bash", "args": {"command": 'agnes glossary search "full-time equivalent"'}},
        {
            "tool": "Bash",
            "args": {
                "command": "agnes glossary show full_time_equivalent",
                "output": "Term: Full-time equivalent\nDefinition: headcount normalised to a 40h week",
            },
        },
    ]

    def test_a_glossary_term_is_parsed_as_its_own_kind(self):
        (c,) = parse_claims("glossary: Full-time equivalent")
        assert (c.kind, c.ref) == ("glossary", "Full-time equivalent")

    def test_a_term_the_turn_looked_up_is_verified(self):
        """Same contract as the other three kinds: the turn's tool calls are
        the record of what was actually read, and `agnes glossary` naming the
        term is that."""
        v = verdict(_answer("glossary: Full-time equivalent"), self.GLOSSARY_CALLS)
        assert v.claims[0].verified is True

    def test_a_term_nothing_looked_up_is_reported_unverified(self):
        """A plausible-sounding term nothing ran on is what an invented
        definition looks like, and the reader is told."""
        v = verdict(_answer("glossary: Fully burdened cost"), self.GLOSSARY_CALLS)
        assert [c.ref for c in v.unverified] == ["Fully burdened cost"]

    def test_the_ref_is_matched_case_insensitively(self):
        """The term is prose the model retypes; the catalog's capitalisation is
        not a claim about provenance."""
        v = verdict(_answer("glossary: FULL-TIME EQUIVALENT"), self.GLOSSARY_CALLS)
        assert v.claims[0].verified is True

    def test_a_term_is_never_split_the_way_a_metric_id_is(self):
        """The one place the metric path must NOT be copied.

        A metric id is a namespaced `family/name`, so its tail is the same
        identifier written shorter, and accepting it costs nothing. A glossary
        term is prose, where a slash is punctuation INSIDE the term — so
        matching the tail would verify "bookings/billings" against a turn that
        looked up something else entirely, and the badge would be asserting a
        check that never happened.
        """
        calls = [{"args": {"command": 'agnes glossary search "billings"'}}]
        v = verdict(_answer("glossary: bookings/billings"), calls)
        assert v.claims[0].verified is False
        # And the metric it is NOT: the same haystack, the same slash, verified.
        assert verdict(_answer("metric: bookings/billings"), calls).claims[0].verified is True

    def test_a_glossary_line_is_not_split_on_pipes(self):
        (c,) = parse_claims("glossary: Bookings | net of cancellations")
        assert c.ref == "Bookings | net of cancellations"
        assert c.origin is None

    def test_a_glossary_term_reaches_the_wire_in_the_reference_shape(self):
        """Three keys, like a table, a metric or a document — `origin`/`why`
        belong to the assumption line alone."""
        v = verdict(_answer("glossary: Full-time equivalent"), self.GLOSSARY_CALLS)
        assert set(v.to_dict()["claims"][0]) == {"kind", "ref", "verified"}

    def test_an_answer_citing_only_terms_has_declared_a_source(self):
        """The defect as the reader met it: a governed definition could only
        arrive as a caveat. A term is provenance, so it counts."""
        from app.chat.sources import VERIFIABLE_KINDS

        assert "glossary" in VERIFIABLE_KINDS
        v = verdict(_answer("glossary: Full-time equivalent\nglossary: Attrition"), self.GLOSSARY_CALLS)
        assert [c.kind for c in v.claims] == ["glossary", "glossary"]
        assert not any(c.kind == "assumption" for c in v.claims)

    def test_the_verdict_reads_no_registry(self):
        """The glossary's read rule is honoured by not importing it.

        A later "improvement" that resolved the ref against `glossary_repo()`
        would put a registry read behind every rendered message and tie the
        badge to instance state — and, worse, invite the metric-shaped read
        next to it, which IS per-caller filtered. The check is text over the
        turn's own tool calls and nothing else, so blowing the repository up
        changes no verdict.
        """
        import src.repositories as repos

        def _boom(*a, **k):  # pragma: no cover - only runs if the rule breaks
            raise AssertionError("the sources verdict must not read the glossary registry")

        original = repos.glossary_repo
        repos.glossary_repo = _boom
        try:
            v = verdict(_answer("glossary: Full-time equivalent"), self.GLOSSARY_CALLS)
        finally:
            repos.glossary_repo = original
        assert v.claims[0].verified is True

    def test_every_prompt_carrier_offers_the_glossary_kind(self):
        """Same three carriers as `document:`, same reason: a kind the model is
        never told about is a kind it will keep smuggling into `assumption:`."""
        import pathlib

        from app.chat.agent_profile import PROVENANCE_RAILS

        root = pathlib.Path(__file__).resolve().parents[1]
        carriers = {
            "workspace CLAUDE.md": (root / "app" / "initial_workspace_default" / "CLAUDE.md").read_text(
                encoding="utf-8"
            ),
            "server template": (root / "config" / "claude_md_template.txt").read_text(encoding="utf-8"),
            "persona rail": PROVENANCE_RAILS,
        }
        for name, text in carriers.items():
            section = text[text.index("Say where every number came from") :]
            assert "`glossary:`" in section, f"{name} does not offer the glossary kind"
            fence = section[section.index("```sources") :]
            fence = fence[: fence.index("```", 3)]
            assert "glossary:" in fence, f"{name}'s example does not show a glossary line"
