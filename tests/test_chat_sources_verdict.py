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
    v = verdict(_answer("table: mrr\nassumption: x"), TOOL_CALLS)
    assert v.to_dict() == {
        "declared": True,
        "claims": [
            {"kind": "table", "ref": "mrr", "verified": True},
            {"kind": "assumption", "ref": "x", "verified": None},
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
        return src[i : i + 4000]

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
