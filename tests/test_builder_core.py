"""The shared half of a builder turn.

Two of these are regressions with a screenshot behind them:

  - a suggestion chip that was the BUILDER's question ("Who should this package
    go to — just Sales, or Sales + Finance?"), clicked by an author who then
    had their own question asked back at them;
  - a reply rendered with literal ``**asterisks**``, because the prompt says
    plain text, the model wrote markdown anyway, and the transcript inserts the
    reply as TEXT (model output is never trusted as HTML).
"""

from __future__ import annotations


from app.api.builder_core import (
    BuilderMessage,
    Slot,
    is_opening_turn,
    merged_draft,
    open_slots,
    plain_reply,
    slots_payload,
    slots_prompt_section,
    stub_enabled,
    turn_response,
    usable_suggestions,
)

_SLOTS = (
    Slot(key="what", label="what it does", known=lambda d: bool(d.get("body")), ask="the job."),
    Slot(key="name", label="a name", known=lambda d: bool(d.get("name")), ask="a handle."),
)


class TestSuggestionsAreWhatTheAuthorSays:
    def test_the_builders_own_question_is_dropped(self):
        kept = usable_suggestions(
            ["Who should this package go to—just Sales, or Sales + Finance?", "Just the sales team"],
            reply="Before I name it: who is it for?",
        )
        assert kept == ["Just the sales team"]

    def test_enumerated_options_survive_even_though_the_reply_contains_them(self):
        """The trap in the obvious fix. When the reply lists the options, the
        chips repeating them are exactly the right answers — a filter that
        dropped anything appearing in the reply would delete them."""
        reply = "Who is it for? Just the Sales team, or Sales + Finance together?"
        assert usable_suggestions(["Just the Sales team", "Sales and Finance"], reply=reply) == [
            "Just the Sales team",
            "Sales and Finance",
        ]

    def test_instructions_survive(self):
        assert usable_suggestions(["Make it stricter about numbers"], reply="Drafted it.") == [
            "Make it stricter about numbers"
        ]

    def test_capped_at_three(self):
        assert len(usable_suggestions([f"do thing {i}" for i in range(9)], reply="ok")) == 3

    def test_junk_is_survivable(self):
        for raw in (None, "nope", 7, [None, 3, {}], {}):
            assert usable_suggestions(raw, reply="ok") == []

    def test_markdown_in_a_chip_is_stripped_not_shown(self):
        assert usable_suggestions(["**Just** sales"], reply="ok") == ["Just sales"]


class TestReplyIsProse:
    def test_emphasis_markers_are_removed(self):
        assert plain_reply("I need to know: **who is it for?** Then I can name it.") == (
            "I need to know: who is it for? Then I can name it."
        )

    def test_underscore_emphasis_too(self):
        assert plain_reply("__really__ important") == "really important"

    def test_headings_and_bullets_are_flattened(self):
        assert plain_reply("## Steps\n- one\n- two") == "Steps\none\ntwo"

    def test_it_is_not_a_sanitizer(self):
        """Deliberately unchanged: the transcript inserts this as text, so
        markup is shown literally rather than parsed. Stripping the two markers
        the contract forbids must not turn into HTML handling here."""
        assert "<b>" in plain_reply("a <b>tag</b> stays a tag")

    def test_non_strings_and_empties(self):
        for raw in (None, 7, [], ""):
            assert plain_reply(raw) == ""


class TestTheInterview:
    def test_open_slots_are_in_declared_order(self):
        assert [s.key for s in open_slots(_SLOTS, {})] == ["what", "name"]
        assert [s.key for s in open_slots(_SLOTS, {"body": "x"})] == ["name"]
        assert open_slots(_SLOTS, {"body": "x", "name": "y"}) == []

    def test_a_raising_predicate_counts_as_open_rather_than_failing_the_turn(self):
        boom = (Slot(key="b", label="b", known=lambda d: 1 / 0, ask="."),)
        assert [s.key for s in open_slots(boom, {})] == ["b"]

    def test_payload_reports_every_slot_with_its_state(self):
        assert slots_payload(_SLOTS, {"name": "n"}) == [
            {"key": "what", "label": "what it does", "known": False},
            {"key": "name", "label": "a name", "known": True},
        ]

    def test_the_prompt_names_the_first_open_slot_as_the_job(self):
        lines = "\n".join(slots_prompt_section(_SLOTS, {}))
        assert "YOUR JOB THIS TURN" in lines
        assert "what it does" in lines.split("YOUR JOB THIS TURN")[1]

    def test_a_finished_draft_is_told_not_to_open_a_new_question(self):
        lines = "\n".join(slots_prompt_section(_SLOTS, {"body": "x", "name": "y"}))
        assert "Nothing is missing" in lines
        assert "YOUR JOB THIS TURN" not in lines

    def test_no_slots_declared_is_not_an_error(self):
        assert slots_prompt_section((), {}) == []


class TestTheOpeningTurn:
    def test_empty_with_no_history_is_the_builder_speaking_first(self):
        assert is_opening_turn("", [])
        assert is_opening_turn("   ", [])

    def test_empty_later_is_not(self):
        assert not is_opening_turn("", [BuilderMessage(role="user", text="hi")])

    def test_a_real_message_is_never_an_opening(self):
        assert not is_opening_turn("hello", [])


class TestTheEnvelope:
    def test_progress_is_reported_after_the_patch_is_merged(self):
        body = turn_response(
            {"reply": "Named it.", "suggestions": []},
            patch={"name": "n"},
            engine="model",
            slots=_SLOTS,
            draft=merged_draft({}, {"name": "n"}),
            fallback_reply="Updated.",
        )
        assert body["engine"] == "model"
        assert [s["known"] for s in body["slots"]] == [False, True]

    def test_a_missing_reply_falls_back(self):
        body = turn_response({}, patch={}, engine="stub", slots=(), draft={}, fallback_reply="Updated the draft.")
        assert body["reply"] == "Updated the draft."

    def test_extra_fields_ride_along(self):
        body = turn_response(
            {"reply": "ok"},
            patch={},
            engine="stub",
            slots=(),
            draft={},
            fallback_reply="x",
            extra={"agent": {"id": "a"}},
        )
        assert body["agent"] == {"id": "a"}


class TestTheStubGate:
    def test_testing_forces_it(self, monkeypatch):
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.delenv("AGNES_BUILDER_STUB", raising=False)
        assert stub_enabled()

    def test_local_dev_mode_alone_no_longer_implies_it(self, monkeypatch):
        """The whole point of the change: LOCAL_DEV_MODE is also what provides
        local auto-auth, so implying the stub from it left no way to run a
        local instance with auth AND a real turn."""
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.delenv("AGNES_BUILDER_STUB", raising=False)
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        assert not stub_enabled()

    def test_its_own_flag_enables_it(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("AGNES_BUILDER_STUB", "1")
        assert stub_enabled()
