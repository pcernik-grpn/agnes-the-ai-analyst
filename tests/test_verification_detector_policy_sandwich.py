"""Tests for the transcript-detector prompt safety sandwich (issue #1971
Part 2): non-editable trust-boundary preamble + editable policy (plain
text, never templated) + non-editable output-schema contract.
"""

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# render_verification_prompt — pure assembly, no LLM involved
# ---------------------------------------------------------------------------


def test_sandwich_order_preamble_then_policy_then_schema():
    from services.verification_detector.prompts import (
        OUTPUT_INSTRUCTIONS,
        render_verification_prompt,
    )

    policy_marker = "MARKER-POLICY-TEXT-12345"
    prompt = render_verification_prompt("alice", "sess-1", "<turn>hi</turn>", policy_marker)

    preamble_idx = prompt.index("Trust boundary")
    policy_idx = prompt.index(policy_marker)
    schema_idx = prompt.index(OUTPUT_INSTRUCTIONS.splitlines()[0])
    assert preamble_idx < policy_idx < schema_idx
    assert "alice" in prompt
    assert "sess-1" in prompt
    assert "<turn>hi</turn>" in prompt


def test_policy_text_is_inserted_as_plain_text_never_templated():
    """A policy string containing literal `{...}` placeholders (e.g. an
    admin pasting a JSON example) must survive verbatim — no `.format()`
    or Jinja rendering is ever applied to it, and it must never raise."""
    from services.verification_detector.prompts import render_verification_prompt

    hostile_policy = 'Treat {username} and {undefined_placeholder} and {{"json": "example"}} literally.'
    prompt = render_verification_prompt("alice", "sess-1", "conversation text", hostile_policy)

    assert hostile_policy in prompt


def test_policy_text_curly_braces_do_not_raise_keyerror():
    from services.verification_detector.prompts import render_verification_prompt

    # A bare `{nonexistent}` would raise KeyError if this were ever run
    # through str.format() alongside the caller-supplied kwargs.
    policy = "{nonexistent} should not blow anything up"
    # Must not raise.
    render_verification_prompt("bob", "sess-2", "conv", policy)


def test_missing_policy_falls_back_to_default_never_empty():
    from services.verification_detector.prompts import (
        DEFAULT_DETECTION_POLICY,
        render_verification_prompt,
    )

    # detector.py resolves the fallback before calling render — but the
    # DEFAULT constant itself must be non-empty, which is what makes that
    # fallback meaningful.
    assert DEFAULT_DETECTION_POLICY.strip()
    prompt = render_verification_prompt("bob", "sess-2", "conv", DEFAULT_DETECTION_POLICY)
    assert "engagement" in prompt.lower()


# ---------------------------------------------------------------------------
# extract_verifications — the live policy lookup + fallback contract
# ---------------------------------------------------------------------------


def _mock_extractor(response: dict) -> MagicMock:
    mock = MagicMock()
    mock.extract_json.return_value = response
    return mock


def test_extract_verifications_sends_the_live_edited_policy(monkeypatch):
    import services.verification_detector.detector as detector_module

    monkeypatch.setattr(
        detector_module,
        "_load_active_policy",
        lambda: "MARKER-EDITED-POLICY",
    )
    extractor = _mock_extractor({"verifications": []})

    detector_module.extract_verifications(extractor, "alice", "sess-1", [{"role": "user", "content": "hi"}])

    sent_prompt = extractor.extract_json.call_args.kwargs.get("prompt") or extractor.extract_json.call_args.args[0]
    assert "MARKER-EDITED-POLICY" in sent_prompt


def test_extract_verifications_falls_back_to_default_when_policy_lookup_raises(monkeypatch):
    """Never a crash, never an empty policy — a repo/backend hiccup in the
    memory-curator profile lookup must not take detection down with it."""
    from app.services.memory_curator_profile import DEFAULT_DETECTION_POLICY
    import services.verification_detector.detector as detector_module

    def _raising_get_policy():
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(
        "app.services.memory_curator_profile.get_memory_curator_policy_text",
        _raising_get_policy,
    )
    extractor = _mock_extractor({"verifications": []})

    detector_module.extract_verifications(extractor, "alice", "sess-1", [{"role": "user", "content": "hi"}])

    sent_prompt = extractor.extract_json.call_args.kwargs.get("prompt") or extractor.extract_json.call_args.args[0]
    assert "engagement" in sent_prompt.lower()  # DEFAULT_DETECTION_POLICY content
    assert DEFAULT_DETECTION_POLICY.strip() in sent_prompt


def test_extract_verifications_no_turns_never_calls_llm():
    import services.verification_detector.detector as detector_module

    extractor = _mock_extractor({"verifications": []})
    result = detector_module.extract_verifications(extractor, "alice", "sess-1", [])
    assert result == []
    extractor.extract_json.assert_not_called()
