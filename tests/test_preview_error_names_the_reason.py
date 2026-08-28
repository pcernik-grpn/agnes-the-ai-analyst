"""What the Preview pane tells you when the engine refuses the turn.

Written from a real failure: the agent builder's Preview answered "The preview
could not answer. The details are in the browser console." on a deployed
instance. Nobody could act on that — not the author, not the person they
reported it to, and not the engineer they reported it to either.

Reproducing it locally against the scripted engine showed what the copy was
throwing away. The frame was:

    kind    = "engine_error"
    message = 'engine refused the turn (500): {"detail":"stub misconfigured:
               KAI_HOST_JWT_SECRET is unset, so the host\\'s session token
               cannot be verified. Set the SAME value here and on the Agnes
               process, or set KAI_STUB_REQUIRE_AUTH=0."}'

A complete diagnosis, discarded by a `return` that named the console instead.
`engine_error` matched none of the recognised patterns, and the fallback said
nothing — so the copy was at its least useful exactly where the failure was
least familiar.

These run the shipped `errorCopy` under node rather than restating it here: a
Python transcription of the rules would pass whatever the rules happen to be,
which is the failure mode this file exists to catch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PREVIEW_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "builder_preview.js"

#: The frame captured from the local repro, verbatim.
ENGINE_ERROR_MESSAGE = (
    'engine refused the turn (500): {"detail":"stub misconfigured: '
    "KAI_HOST_JWT_SECRET is unset, so the host's session token cannot be "
    "verified. Set the SAME value here and on the Agnes process, or set "
    'KAI_STUB_REQUIRE_AUTH=0."}'
)


def _copy(message: str, kind: str = "") -> str:
    """`errorCopy(message, kind)` as the browser would compute it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        "global.window = {};\n"
        + PREVIEW_JS.read_text(encoding="utf-8")
        + "\nprocess.stdout.write(JSON.stringify(window.BuilderPreview.errorCopy("
        + json.dumps(message)
        + ", "
        + json.dumps(kind)
        + ")));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class TestTheFailureThatStartedThis:
    def test_an_engine_error_says_what_the_engine_said(self):
        copy = _copy(ENGINE_ERROR_MESSAGE, "engine_error")
        assert "console" not in copy.lower(), "still sending the reader to devtools"
        assert "KAI_HOST_JWT_SECRET" in copy, "the one word that identifies this failure did not survive the copy"

    def test_the_json_wrapper_is_unwrapped(self):
        """The sentence is the answer; the braces around it are noise. A reader
        parsing JSON out of an error banner is barely better off than one
        sent to the console."""
        copy = _copy(ENGINE_ERROR_MESSAGE, "engine_error")
        assert '{"detail"' not in copy
        assert "Set the SAME value here and on the Agnes process" in copy
        assert "engine refused the turn (500):" in copy, "the prefix says WHICH step failed and is worth keeping"


class TestTheRecognisedCasesStillReadAsProse:
    """Naming unknown errors must not turn the known ones back into jargon."""

    def test_a_missing_engine_blames_the_instance_not_the_author(self):
        copy = _copy("kai_integration_not_configured", "engine_session_unusable")
        assert "an admin sets one up" in copy
        assert "Your work is saved either way." in copy

    def test_a_slow_start_reads_as_retryable(self):
        copy = _copy("Runner did not become ready within 30 s.", "runner_not_ready")
        assert "did not start in time" in copy
        assert "trying again usually works" in copy

    def test_a_spent_budget_names_the_remedy(self):
        assert "An admin can raise it." in _copy("budget_exhausted", "")

    def test_a_busy_instance_says_to_wait(self):
        assert "in a moment" in _copy("concurrency_cap", "")


class TestTheEdges:
    def test_a_kind_only_frame_still_says_something(self):
        """Several frames carry the useful half in `kind` and an empty
        `message` — taking only `message` is how those became silent."""
        copy = _copy("", "engine_session_unusable")
        assert "engine_session_unusable" in copy or "an admin sets one up" in copy

    def test_a_frame_with_nothing_in_it_admits_that(self):
        copy = _copy("", "")
        assert "gave no reason" in copy
        assert "console" not in copy.lower()

    def test_a_kind_that_merely_repeats_the_message_is_not_printed_twice(self):
        copy = _copy("engine_error", "engine_error")
        assert copy.count("engine_error") == 1

    def test_malformed_json_is_left_alone_rather_than_swallowed(self):
        """Unwrapping is a convenience. A tail that does not parse must still
        reach the reader whole — dropping it would recreate the original bug
        for a narrower input."""
        raw = 'engine refused the turn (500): {"detail": not json'
        # Containment, not endswith: the kind is appended after the detail when
        # the two differ, which is deliberate and not what this test is about.
        assert raw in _copy(raw, "engine_error")
