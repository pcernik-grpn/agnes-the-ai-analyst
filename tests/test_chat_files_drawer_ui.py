"""Session-files drawer: per-conversation state and the auto-open trigger.

Three guards, all for review findings on the drawer PR:

1. The auto-open baseline is established when a conversation **opens**, not
   lazily on its first turn-end. Seeding it from a listing fetched after the
   turn ran put that turn's own deliverable into the baseline, so the first
   turn of any conversation — the commonest way to get a deliverable — could
   never open the drawer.
2. Switching conversations resets the badge and reloads an open drawer.
   Nothing told the drawer the conversation had changed, so it kept showing
   the previous chat's count and rows (whose links carry a chat id).
3. The workspace prompt actually names ``outputs/``. The listing excludes the
   workspace-template trees (``.claude``, ``scaffolds``, …) at the top level
   and the drawer only auto-opens for ``outputs/``, and both of those rest on
   the agent being told where deliverables go.

The first two run the **shipped** drawer source under node against stubbed
collaborators (same ``_node_run`` pattern as tests/test_chat_tool_rendering_ui.py)
— there is no DOM harness in CI, so the stubs stand in for the document,
the fetch and the drawer chrome, and the assertions are about the handlers'
observable calls rather than rendered pixels.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
TEMPLATE = Path("config/claude_md_template.txt")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _drawer_slice() -> str:
    """The shipped auto-open block: the two event handlers plus their state.

    Sliced from the constant that opens the block to the end of the IIFE, so
    the test runs the real handlers rather than a copy that can drift.
    """
    js = _read(CHAT_JS)
    start = js.index('const OUTPUTS_PREFIX = "outputs/";')
    end = js.index("})();", start)
    return js[start:end]


#: Stubs for everything the sliced block reaches out to. ``fetchSessionFiles``
#: captures its listing at call time and only then honours a delay, so a test
#: can put two fetches in flight and land them out of order.
_HARNESS_PREAMBLE = """
let currentChatId = null;
let _drawerIsOpen = false;
let _nextFiles = () => [];
let _delayNext = 0;
const badge = [];
const rendered = [];
const listStatus = [];
let opened = 0;
const _handlers = {};
const document = {
  addEventListener(name, fn) { (_handlers[name] = _handlers[name] || []).push(fn); },
};
const filesListEl = { replaceChildren() { listStatus.push("cleared"); } };
function setFilesStatus(s) { listStatus.push(s); }
function updateFilesBadge(n) { badge.push(n); }
function drawerOpen() { return _drawerIsOpen; }
function renderFileList(chatId, files) { rendered.push([chatId, files.map((f) => f.path)]); }
function openFilesDrawer() { opened += 1; _drawerIsOpen = true; }
async function fetchSessionFiles(chatId) {
  const paths = _nextFiles(chatId);
  const d = _delayNext; _delayNext = 0;
  if (d) await new Promise((r) => setTimeout(r, d));
  return { files: paths.map((p) => ({ path: p })), truncated: false, ok: true };
}
"""

_HARNESS_FIRE = """
async function fire(name, detail) {
  for (const h of (_handlers[name] || [])) await h({ detail });
}
"""


def _run_scenario(body: str) -> dict:
    script = _HARNESS_PREAMBLE + _drawer_slice() + _HARNESS_FIRE + "\n(async () => {\n" + body + "\n})();\n"
    return json.loads(_node_run(script))


def test_first_turn_deliverable_opens_the_drawer():
    """The headline case: a brand-new conversation whose FIRST turn writes a
    file under ``outputs/``.

    Before the fix the turn-end handler took its baseline-reset branch —
    ``_filesSessionId`` started null and was assigned nowhere else — seeded
    the baseline from a listing that already contained the new file, and
    returned. The drawer stayed shut on exactly the turn it exists for.
    """
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => [];                       // nothing in the session yet
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        _nextFiles = () => ["outputs/report.docx"];  // the first turn writes it
        await fire("agnes:turn-end");
        process.stdout.write(JSON.stringify({ opened, badge }));
        """
    )
    assert res["opened"] == 1, "the first turn's deliverable must open the drawer"
    assert res["badge"][-1] == 1


def test_second_turn_with_no_new_deliverable_leaves_the_drawer_alone():
    """The other half: the baseline has to actually suppress a repeat. A turn
    that writes nothing new must not re-open the panel over the reader."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["outputs/report.docx"];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        await fire("agnes:turn-end");
        process.stdout.write(JSON.stringify({ opened }));
        """
    )
    assert res["opened"] == 0, "a file already present when the conversation opened is not fresh"


def test_switching_conversations_resets_badge_and_reloads_an_open_drawer():
    """Switching used to leave the previous conversation's count on the badge
    and its rows in the drawer, with download links bound to the old chat id,
    until a turn happened to complete in the new conversation."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["a.txt", "b.txt", "outputs/x.md"];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        _drawerIsOpen = true;
        currentChatId = "c2";
        _nextFiles = () => ["only.txt"];
        await fire("agnes:session-open", { chatId: "c2", switching: true });
        process.stdout.write(JSON.stringify({ badge, rendered }));
        """
    )
    assert res["badge"][-1] == 1, "badge must show the NEW conversation's count"
    assert 0 in res["badge"], "and must be cleared synchronously, before the listing round-trip lands"
    assert res["rendered"][-1] == ["c2", ["only.txt"]], "an open drawer reloads for the conversation switched to"


def test_switching_conversations_reseeds_the_auto_open_baseline():
    """A conversation that already has deliverables must not auto-open on its
    first turn just because the drawer was looking at a different chat."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => [];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        currentChatId = "c2";
        _nextFiles = () => ["outputs/old-deck.pptx"];   // queued long before now
        await fire("agnes:session-open", { chatId: "c2", switching: true });
        await fire("agnes:turn-end");
        process.stdout.write(JSON.stringify({ opened }));
        """
    )
    assert res["opened"] == 0, "c2's pre-existing deliverable is not this turn's output"


def test_a_slow_open_seed_cannot_clobber_a_newer_turn_end_baseline():
    """Two listings for the same conversation can be in flight at once — the
    open-time seed and a turn-end poll — and they can land out of order. If
    the stale seed wins, its (older, emptier) baseline makes the NEXT turn
    re-report the same file as fresh and pop the drawer a second time."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => [];
        _delayNext = 40;                                  // the seed is slow
        const seeding = fire("agnes:session-open", { chatId: "c1", switching: true });
        _nextFiles = () => ["outputs/report.docx"];
        await fire("agnes:turn-end");                     // lands first, opens
        await seeding;                                    // stale seed resolves late
        opened = 0; _drawerIsOpen = false;
        await fire("agnes:turn-end");                     // same file, nothing new
        process.stdout.write(JSON.stringify({ opened }));
        """
    )
    assert res["opened"] == 0, "the late seed must not reset the baseline the turn-end already advanced"


def test_an_engine_without_a_files_channel_still_says_so_in_the_drawer():
    """``supported: false`` (an engine-backed session whose engine exposes no
    files channel) must reach the reader as the honest notice, not as an empty
    list reading "your agent produced nothing".

    Guarded here because splitting fetch from render — the drawer refactor —
    moved this branch across a seam: it used to live inside the one function
    that both fetched and painted, and the unattended turn-end poll now shares
    that fetch and must NOT paint.
    """
    js = _read(CHAT_JS)
    fetch_fn = js[js.index("async function fetchSessionFiles") : js.index("function updateFilesBadge")]
    assert "supported: data.supported !== false" in fetch_fn, (
        "the flag must be carried out of the fetch, not rendered inside it — the turn-end poll shares this function"
    )
    assert "setFilesStatus(" not in fetch_fn.split("function renderFileList")[0], (
        "fetchSessionFiles runs unattended on every turn-end; it must not write into the drawer"
    )
    render_fn = js[js.index("function renderFileList") : js.index("function updateFilesBadge")]
    assert "if (!supported)" in render_fn
    assert "doesn't expose them yet" in render_fn
    # Every render call site passes the flag through, or the notice is dead code.
    assert js.count("renderFileList(chatId, files, truncated, supported)") == 3


def test_open_session_is_the_one_place_that_announces_a_conversation_change():
    """Structural guard on the seam: ``openSession`` is the only assigner of a
    non-null ``currentChatId``, so it is the only honest place to raise the
    event. Kept as a source assertion because node cannot run ``openSession``
    (it reaches the network, the WS and the DOM)."""
    js = _read(CHAT_JS)
    body = js[js.index("async function openSession") : js.index("function handleFrame(frame)")]
    assert 'new CustomEvent("agnes:session-open"' in body, "openSession must announce the switch"
    assert re.search(r"currentChatId = chatId;[\s\S]{0,1200}agnes:session-open", body), (
        "the event must follow the currentChatId assignment it describes"
    )
    # Nowhere else may assign a non-null currentChatId, or the seam has a hole.
    assert len(re.findall(r"^\s*currentChatId = (?!null)", js, re.M)) == 1


def test_sandbox_prompt_tells_the_agent_where_deliverables_go():
    """The premise under two separate behaviours: the listing drops the
    workspace-template trees (``.claude`` included) at the top level, and the
    drawer only auto-opens for ``outputs/``. Both are only correct if the
    agent has been told to write deliverables there — and nothing said so.
    """
    from tests.test_chat_answer_provenance_and_charts import (  # local import: heavy fixture
        _rendered_server_default_claude_md,
    )

    rendered = re.sub(r"\s+", " ", _rendered_server_default_claude_md(is_sandbox=True))
    assert "outputs/" in rendered, "the sandbox prompt must name the directory the user can reach"
    assert "Files you produce" in rendered
    # And it must say what does NOT reach the user, since that is the half the
    # listing exclusion depends on.
    assert ".claude/" in rendered

    laptop = _rendered_server_default_claude_md(is_sandbox=False)
    assert "Files you produce" not in laptop, (
        "a laptop workspace IS the user's computer — the outputs/ hand-off is sandbox-only"
    )


def test_sandbox_prompt_no_longer_claims_files_cannot_reach_the_user():
    """The Charts section asserted 'you do not have any way to hand the user a
    file'. True when it was written; false since the Files panel shipped, and
    directly contradicted by the section above — the model would have had to
    pick one."""
    template = _read(TEMPLATE)
    assert "any way to hand the user a file" not in template
