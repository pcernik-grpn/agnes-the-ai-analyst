"""Every conversational builder takes its turn through the shell.

The shell used to say it "cannot enforce anything — that contract lives in the
pages and in their tests", and four pages proved the point: two capped the
message and trimmed the transcript and two did not; two restored a failed
message and two lost it; two latched a no-model state and two kept inviting
the user to retry something that could not work. Each of those is *policy*,
not markup, and none of it needs the DOM — so `BuilderShell.turn()` owns it.

This is the test that keeps it owned. It is deliberately the same shape as
`test_every_builder_names_its_engine.py`, which is the working precedent: the
engine badge is the one axis on which nothing diverged, because a test pinned
all four builders to one implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "app" / "web" / "static" / "js" / "components" / "builder_shell.js"

#: The four surfaces that hold a builder conversation. Linked apps is absent on
#: purpose — it has no conversation, and its own header comment argues why:
#: every decision there is a pick from a server-supplied list.
BUILDERS = {
    "skills": ROOT / "app" / "web" / "templates" / "skills.html",
    "agents": ROOT / "app" / "web" / "templates" / "agents.html",
    "mcp": ROOT / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js",
    "package": ROOT / "app" / "web" / "static" / "js" / "components" / "package_drawer.js",
}


@pytest.fixture(scope="module")
def shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def test_the_shell_exports_the_turn_helpers(shell):
    for name in ("turn:", "clipMsg:", "trimHistory:", "noModelNotice:", "action:"):
        assert name in shell, f"BuilderShell no longer exports {name.rstrip(':')}"


def test_the_caps_are_declared_once(shell):
    """Four copies of a number is four chances for one to drift."""
    assert "var MAX_MSG_CHARS = 4000;" in shell
    assert "var MAX_HISTORY = 40;" in shell
    assert "var TURN_TIMEOUT_MS = 60000;" in shell


def test_the_shell_caps_match_the_server():
    from app.api.builder_core import MAX_HISTORY, MAX_MESSAGE_CHARS

    text = SHELL.read_text(encoding="utf-8")
    assert f"var MAX_MSG_CHARS = {MAX_MESSAGE_CHARS};" in text, (
        "the shell's message cap no longer matches app/api/builder_core.py"
    )
    assert f"var MAX_HISTORY = {MAX_HISTORY};" in text, (
        "the shell's history cap no longer matches app/api/builder_core.py"
    )


def test_the_turn_refuses_an_over_long_message_before_sending(shell):
    """The author's text stays in their hands. Sending it earned a validation
    error whose `detail` is an ARRAY, which every page then rendered as "the
    assistant could not answer" — blaming the assistant for a paste."""
    body = re.search(r"function turn\(o\) \{(.*?)\n  \}", shell, re.S)
    assert body, "turn() moved — re-point this guard"
    assert "MAX_MSG_CHARS" in body.group(1)
    assert "'too_long'" in body.group(1)
    assert "Array.isArray(d)" in body.group(1), "an array-shaped detail is unhandled again"


def test_the_turn_has_a_deadline(shell):
    body = re.search(r"function turn\(o\) \{(.*?)\n  \}", shell, re.S)
    assert "AbortController" in body.group(1) and "'timeout'" in body.group(1), (
        "a hung turn leaves the composer disabled with no way out but a reload"
    )


def test_a_no_model_instance_is_typed_not_guessed(shell):
    body = re.search(r"function turn\(o\) \{(.*?)\n  \}", shell, re.S)
    assert "builder_llm_unavailable" in body.group(1) and "'llm_unavailable'" in body.group(1), (
        "the page cannot tell 'this instance has no model' from any other failure"
    )


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_conversational_builder_uses_the_shared_turn(name):
    src = BUILDERS[name].read_text(encoding="utf-8")
    assert "BuilderShell.turn(" in src, (
        f"{name} takes its turn by hand again — it will drift on caps, timeout, "
        "failure recovery and the no-model state, which is exactly how the four "
        "diverged the first time"
    )


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_conversational_builder_hands_a_failed_message_back(name):
    """Two of the four cleared the composer and left an orphan user bubble
    above an error, so a long typed message was unrecoverable."""
    src = BUILDERS[name].read_text(encoding="utf-8")
    assert "convDraft = text" in src, f"{name} loses the author's message when a turn fails"


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_conversational_builder_latches_the_no_model_state(name):
    src = BUILDERS[name].read_text(encoding="utf-8")
    assert "llmUnavailable" in src, f"{name} keeps inviting the user to talk on an instance with no model"
