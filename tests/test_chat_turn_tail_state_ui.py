"""The tail of a streamed turn is legible, not a silent gap (TCRD-213).

The `next_actions` chips are a fenced trailer inside the SAME streamed
answer, not a second LLM call made after streaming finishes. While that
trailer streams, `_streamingSafeText` withholds it, so tokens keep arriving
and the painted text cannot change — which is the ~10s "is it stuck?" window
the ticket reports. `_inWithheldTrailer` is the predicate that detects it; it
is pure, so these tests run it for real under node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _predicate_src() -> str:
    """The shipped regexes plus the shipped function — assembled rather than
    sliced as one range, because everything between them builds DOM."""
    js = _read(CHAT_JS)
    src = ""
    for name in ("const _SOURCES_OPEN_RE", "const _NEXT_ACTIONS_OPEN_RE"):
        i = js.index(name)
        src += js[i : js.index("\n", i) + 1]
    i = js.index("function _inWithheldTrailer")
    src += js[i : js.index("\n}", i) + 2]
    return src


def _check(cases: list[str]) -> list[bool]:
    script = _predicate_src() + (
        f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(_inWithheldTrailer)));\n"
    )
    return json.loads(_node_run(script))


def test_plain_prose_is_not_a_trailer():
    (r,) = _check(["Here is the answer, and it is complete."])
    assert r is False


def test_an_open_next_actions_fence_is_the_withheld_window():
    """Exactly the reported case: prose looks done, the model is still
    emitting the trailer, nothing on screen can change."""
    (r,) = _check(["The revenue was 4.2M.\n\n```next_actions\n- Break it down by region\n"])
    assert r is True


def test_a_closed_next_actions_fence_is_not():
    (r,) = _check(["The revenue was 4.2M.\n\n```next_actions\n- Break it down by region\n```\n"])
    assert r is False


def test_an_open_sources_fence_also_counts():
    (r,) = _check(["The revenue was 4.2M.\n\n```sources\n- q3.csv\n"])
    assert r is True


def test_a_closed_sources_fence_followed_by_an_open_next_actions_is_judged_on_the_open_one():
    """The common real shape — both trailers, the first already finished."""
    (r,) = _check(["Answer.\n\n```sources\n- q3.csv\n```\n\n```next_actions\n- Compare to Q2\n"])
    assert r is True


def test_both_trailers_closed_is_not_a_withheld_window():
    (r,) = _check(["Answer.\n\n```sources\n- q3.csv\n```\n\n```next_actions\n- Compare to Q2\n```"])
    assert r is False


def test_an_ordinary_code_fence_is_never_mistaken_for_a_trailer():
    """A streaming ```python block is withheld by the painter too, but it is
    NOT this ticket's case and must not claim the turn is 'finishing'."""
    results = _check(["Here:\n\n```python\nprint(1)\n", "Here:\n\n```python\nprint(1)\n```\n"])
    assert results == [False, False]


def test_empty_and_missing_text_are_safe():
    assert _check(["", " "]) == [False, False]


# ── the rendered consequence ───────────────────────────────────────────────


def test_the_streaming_repaint_toggles_the_state():
    js = _read(CHAT_JS)
    start = js.index("function _renderStreamingMarkdown")
    fn = js[start : js.index("\n}", start) + 2]
    assert 'classList.toggle("is-trailing"' in fn
    assert "_inWithheldTrailer(currentAssistantText)" in fn


def test_the_state_is_cleared_wherever_streaming_ends():
    """A stale `is-trailing` on a finished turn would be a lie the compound
    CSS selector happens to hide — clear it at the source instead."""
    js = _read(CHAT_JS)
    assert 'classList.remove("is-streaming")' not in js, "a streaming-end site forgot is-trailing"
    assert js.count('classList.remove("is-streaming", "is-trailing")') == 3


def test_the_tail_replaces_the_caret_rather_than_adding_to_it():
    """Both rules target the same ::after on the same element, so the label
    supersedes the caret instead of appearing beside it — nothing under the
    reader reflows when the turn crosses into its tail."""
    css = _read(CHAT_CSS)
    assert ".msg.is-streaming .msg-body::after {" in css
    assert ".msg.is-streaming.is-trailing .msg-body::after {" in css
    block = css[css.index(".msg.is-streaming.is-trailing .msg-body::after {") :][:400]
    assert "Finishing" in block
    assert "#" not in block


def test_the_tail_animation_respects_reduced_motion():
    css = _read(CHAT_CSS)
    tail = css[css.index("@keyframes cloudchat-tail-pulse") :][:400]
    assert "prefers-reduced-motion" in tail
    assert "animation: none" in tail
