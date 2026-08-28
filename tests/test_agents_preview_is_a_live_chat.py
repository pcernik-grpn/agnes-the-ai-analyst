"""The agent builder's Preview is a real session, and says so honestly.

This file replaces ``test_agents_preview_is_not_a_chat.py``, whose whole
premise — "the builder page cannot run a turn" — this surface removed. That
file existed because the old Preview rendered a pill styled exactly like the
app's composer with no input behind it: people clicked it, typed, and watched
the text vanish. Its fix was to stop the mock *claiming* to be interactive.

The mock is gone. Preview now opens a chat session bound to the agent's own
slug and streams the answer back, so the honest thing is the opposite of what
that file asserted: it must be a REAL composer, and it must not promise a
turn it cannot run.

What is pinned here:

- the composer is a real control, wired to a send path;
- it is disabled, with an explanatory placeholder, while the agent has no
  name — there is nothing to preview yet, and a live-looking box that
  silently does nothing is the exact regression the old file was written for;
- the draft is flushed to the server before the session spawns, or the agent
  answers as the persona from before the owner's last edit;
- an engine failure is translated, not pasted — ``kai_integration_not_
  configured`` in a chat bubble reads as a broken page rather than as
  "an admin has not set this up";
- model output reaches the DOM as text, never as HTML (this page has no
  sanitizer — see the security playbook on ``innerHTML``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "agents.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def preview_js() -> str:
    """The preview SERVICE. A session, a socket and a stream of tokens is
    genuinely stateful, so it sits beside the pure shell rather than in it —
    but it is one implementation, shared by both builders."""
    return (
        Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "builder_preview.js"
    ).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shell_js() -> str:
    """The builder shell's markup, extracted so a second builder page renders
    the same thing. Assertions about what the SHELL emits read this; the
    page's own script is still `markup`."""
    return (
        Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "builder_shell.js"
    ).read_text(encoding="utf-8")


class TestPreviewRunsARealTurn:
    def test_the_preview_composer_is_a_real_control(self, shell_js):
        """The inverse of the old assertion, and the reason this file exists."""
        assert 'data-ag-comp="' in shell_js
        assert "<textarea" in shell_js
        assert 'data-ag-send="' in shell_js

    def test_it_opens_a_session_bound_to_this_agent(self, markup, preview_js):
        """Bound by slug — a preview against the default agent would answer as
        something other than the thing on screen. The session plumbing moved
        into the shared service; WHICH slug is still this page's decision, and
        that is the half worth pinning here."""
        assert "/api/chat/sessions" in preview_js
        assert "agent_slug: slug" in preview_js
        block = re.search(r"resolveSlug: function \(\) \{(.*?)\n    \},", markup, re.S)
        assert block, "agents.html does not tell the preview which agent to run"
        assert "a.slug" in block.group(1)

    def test_it_streams_over_the_websocket_contract(self, preview_js):
        for frame in ("'token'", "'assistant_message'", "'error'"):
            assert frame in preview_js, f"preview does not handle the {frame} frame"

    def test_the_frame_it_sends_is_the_web_chat_protocols(self, preview_js):
        """Not this module's invention. Getting this wrong sends a message the
        engine silently ignores, which looks exactly like a hung preview."""
        assert "'user_msg'" in preview_js and "text: text" in preview_js

    def test_the_draft_is_saved_before_the_session_spawns(self, markup):
        """The agent runs SERVER-SIDE, so it can only answer as a configuration
        the server has. Previewing therefore commits the working copy — the
        same write Save does — rather than spawning a session against the
        previous edit's persona."""
        assert re.search(r"saveAgent\(\)\.then", markup), "openPreviewSession does not commit the working copy first"


class TestPreviewDoesNotPromiseWhatItCannotDo:
    def test_the_composer_is_disabled_until_the_agent_has_a_name(self, markup):
        """An un-named draft has no slug worth previewing. The composer is
        rendered disabled with the reason as its placeholder rather than
        accepting a message that would go nowhere."""
        block = re.search(r"function previewPaneHtml\(a\) \{(.*?)\n  \}", markup, re.S)
        assert block, "previewPaneHtml not found"
        body = block.group(1)
        assert "Start by defining your agent." in body
        # The empty-state branch passes busy=true, which is what disables both
        # the textarea and the send button in composerHtml.
        assert re.search(r"composerHtml\('preview', '', 'Start by defining your agent\.', true\)", body)

    def test_a_recognised_engine_error_is_translated_not_pasted(self, preview_js):
        """Internal kinds pasted verbatim read as a broken page and send the
        author hunting for a mistake in a configuration that is fine."""
        assert "function errorCopy(" in preview_js
        assert "not_configured" in preview_js
        assert "an admin sets one up" in preview_js
        assert "console.error" in preview_js

    def test_an_unrecognised_engine_error_still_names_itself(self, preview_js):
        """The fallback used to read "the details are in the browser console",
        which is worth nothing to anyone not holding devtools open — a preview
        failing on a deployed instance told its author, and whoever they
        reported it to, precisely nothing. Translating the errors we know is
        not a licence to withhold the ones we don't."""
        # The banned string is the one that reached the SCREEN, so match what
        # a return statement would carry rather than the word anywhere in the
        # file — a comment explaining this history must not fail the test.
        assert "in the browser console'" not in preview_js, (
            "the fallback sends the reader to devtools instead of telling them what happened"
        )
        assert "'The preview could not answer: ' + detail" in preview_js, (
            "the unrecognised case must put the engine's own words on screen"
        )
        assert "errorCopy(frame.message, frame.kind)" in preview_js, (
            "several error frames carry the useful half in `kind` and an empty "
            "`message`, so both have to reach errorCopy"
        )

    def test_a_slow_engine_start_reads_as_retryable(self, preview_js):
        """`runner_not_ready` ("Runner did not become ready within 30 s") is the
        one failure here that is usually nobody's mistake — the first session on
        an instance fetches the sandbox. It said nothing actionable before."""
        assert "runner_not_ready" in preview_js
        assert "did not start in time" in preview_js

    def test_model_output_is_escaped_into_the_dom(self, shell_js):
        """Assistant text goes through `esc()`, never raw innerHTML — neither
        builder page has a sanitizer and the answer is model output.

        Follows the renderer into builder_shell.js. This is the assertion that
        must NOT be allowed to quietly stop testing anything: it is the reason
        a model reply cannot put script in the page."""
        block = re.search(r"function message\(m\) \{(.*?)\n  \}", shell_js, re.S)
        assert block, "BuilderShell.message not found"
        body = block.group(1)
        assert "esc(m.text)" in body
        assert body.count("m.text") == body.count("esc(m.text)"), "an unescaped m.text reaches the DOM"

    def test_the_shell_escaper_is_the_pages_escaper(self, markup):
        """One escaper across both builder pages — a page keeping its own copy
        is how one of them ends up weaker than the other."""
        assert "var esc = BuilderShell.esc;" in markup

    def test_the_shell_escaper_covers_every_dangerous_character(self, shell_js):
        for ch in ("&", "<", ">", '"', "'"):
            assert f"'{ch}':" in shell_js or f'"{ch}":' in shell_js, f"{ch!r} missing from the escape table"

    def test_the_pane_says_the_session_is_real(self, markup):
        """The old card had to disclaim being a chat. This one has the opposite
        duty: say that messages here are a real, billable session."""
        assert "A real session with this agent" in markup
