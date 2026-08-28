"""Session files: per-conversation state and what a turn hands the reader.

Three guards, all for review findings on the drawer PR:

1. The freshness baseline is established when a conversation **opens**, not
   lazily on its first turn-end. Seeding it from a listing fetched after the
   turn ran put that turn's own deliverable into the baseline, so the first
   turn of any conversation — the commonest way to get a deliverable — went
   unannounced. What a fresh file now triggers is a CHIP on the answer that
   produced it (the drawer no longer opens itself); the freshness rule these
   scenarios exercise is unchanged, so they still guard the same seam.
2. Switching conversations resets the badge and reloads an open drawer.
   Nothing told the drawer the conversation had changed, so it kept showing
   the previous chat's count and rows (whose links carry a chat id).
3. The workspace prompt actually names ``outputs/``. The listing excludes the
   workspace-template trees (``.claude``, ``scaffolds``, …) at the top level
   and only ``outputs/`` is treated as a deliverable, and both of those rest
   on the agent being told where deliverables go.

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
    """The shipped auto-open block: the two event handlers plus their state,
    with the shipped ``loadSessionFiles`` (the manual open/refresh path, and
    the third writer of the same baseline) appended.

    Sliced from the source so the tests run the real handlers rather than a
    copy that can drift. Declaration order does not matter — the harness only
    calls into them after the whole script body has run.
    """
    js = _read(CHAT_JS)
    start = js.index('const OUTPUTS_PREFIX = "outputs/";')
    block = js[start : js.index("})();", start)]
    loader_start = js.index("async function loadSessionFiles")
    loader = js[loader_start : js.index("// ── drawer open/close", loader_start)]
    return block + "\n" + loader


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
// Enough DOM for `renderFileChips` to actually run: a deliverable is now
// delivered as a chip ON the answer, so stubbing that call out would leave
// these tests asserting nothing about the thing the user receives. `chipped`
// records the paths that reached the transcript.
const chipped = [];
let _chipRow = null;
function _mkEl() {
  const el = {
    className: "", textContent: "", title: "", href: "", type: "", disabled: false,
    dataset: {}, kids: [],
    setAttribute() {}, addEventListener() {}, replaceWith() {},
    appendChild(c) { el.kids.push(c); if (el === _chipRow && c.dataset.path) chipped.push(c.dataset.path); return c; },
    querySelector() { return null; },
  };
  return el;
}
const _bubble = {
  querySelector(sel) { return sel.indexOf("cloud-chat-file-chips") !== -1 ? _chipRow : null; },
  appendChild(c) { _chipRow = c; return c; },
  insertBefore(c) { _chipRow = c; return c; },
};
const _article = { querySelector: () => _bubble };
const document = {
  addEventListener(name, fn) { (_handlers[name] = _handlers[name] || []).push(fn); },
  querySelectorAll(sel) { return sel.indexOf("msg-assistant") !== -1 ? [_article] : []; },
  createElement() { return _mkEl(); },
};
const CSS = { escape: (s) => s };
function fmtSize(n) { return String(n) + " B"; }
function showToast() {}
const filesListEl = { replaceChildren() { listStatus.push("cleared"); } };
const filesErrorEl = {};
const errors = [];
function setFilesStatus(s) { listStatus.push(s); }
function clearDialogError() {}
function showDialogError(el, msg) { errors.push(msg); }
function updateFilesBadge(n) { badge.push(n); }
function drawerOpen() { return _drawerIsOpen; }
function renderFileList(chatId, files) { rendered.push([chatId, files.map((f) => f.path)]); }
function openFilesDrawer() { opened += 1; _drawerIsOpen = true; }
let _failNext = false;
async function fetchSessionFiles(chatId, { quiet = false } = {}) {
  const paths = _nextFiles(chatId);
  const d = _delayNext; _delayNext = 0;
  const fail = _failNext; _failNext = false;
  if (d) await new Promise((r) => setTimeout(r, d));
  if (fail) {
    // Mirrors the shipped failure path: error surfaced unless quiet, empty
    // result, ok:false.
    if (!quiet) showDialogError(filesErrorEl, "Could not load session files");
    return { files: [], truncated: false, supported: true, ok: false };
  }
  return {
    files: paths.map((p) => ({ path: p, name: p.split("/").pop(), size_bytes: 1 })),
    truncated: false,
    supported: true,
    ok: true,
  };
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


def test_first_turn_deliverable_reaches_the_answer_as_a_chip():
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
        process.stdout.write(JSON.stringify({ opened, chipped, badge }));
        """
    )
    assert res["chipped"] == ["outputs/report.docx"], "the first turn's deliverable must reach the answer as a chip"
    assert res["opened"] == 0, "delivery is the chip; the drawer must not throw itself over the reading"
    assert res["badge"][-1] == 1


def test_a_file_present_before_the_turn_is_not_chipped():
    """The other half: the baseline has to actually suppress a repeat. A turn
    that writes nothing new must not re-announce a file the reader already
    had."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["outputs/report.docx"];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        await fire("agnes:turn-end");
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == [], "a file already present when the conversation opened is not fresh"


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
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == [], "c2's pre-existing deliverable is not this turn's output"


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
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == [], "the late seed must not reset the baseline the turn-end already advanced"


def test_a_failed_open_time_seed_does_not_leave_the_drawer_claimed_but_ignorant():
    """Binding the drawer to a conversation and knowing its deliverables are
    two different facts, and the failure path separated them.

    The session claim is made synchronously — it has to be, so the badge and
    rows reset before the listing round-trip — but the baseline is only
    populated once that listing succeeds. A failed seed therefore left the
    drawer *bound* with an *empty* baseline, which made the turn-end handler
    skip its re-seed branch and report every pre-existing `outputs/` file in
    a resumed conversation as freshly produced.
    """
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["outputs/last-week.pptx"];   // a resumed conversation
        _failNext = true;
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        await fire("agnes:turn-end");     // must re-seed, not diff against nothing
        await fire("agnes:turn-end");     // and stay quiet after that
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == [], "a file from last week is not something this turn produced"


def test_a_superseded_open_time_seed_leaves_the_newer_baseline_alone():
    """The other way the seed can end without learning anything: a turn-end
    overtakes it. The claim must survive (the newer writer owns it), but the
    stale seed must neither write nor un-claim it."""
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => [];
        _delayNext = 40;                                  // slow seed
        const seeding = fire("agnes:session-open", { chatId: "c1", switching: true });
        _nextFiles = () => ["outputs/report.docx"];
        await fire("agnes:turn-end");                     // overtakes, opens
        await seeding;
        opened = 0; _drawerIsOpen = false;
        await fire("agnes:turn-end");                     // nothing new
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == [], "the superseded seed must not undo the baseline that overtook it"


def test_a_mid_turn_reconnect_does_not_absorb_the_turns_deliverable():
    """`ensureWsReady` re-enters `openSession` whenever the socket is closed,
    so `agnes:session-open` can fire *during* a turn — after the agent wrote
    `outputs/deck.pptx` but before the `done` frame.

    Folding the listing into the baseline there marks that file as already
    seen, and the turn-end that follows finds nothing fresh. A re-open of the
    same conversation carries `switching: false`, and is left alone.
    """
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => [];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        _nextFiles = () => ["outputs/deck.pptx"];       // written mid-turn
        await fire("agnes:session-open", { chatId: "c1", switching: false });  // socket dropped
        await fire("agnes:turn-end");
        process.stdout.write(JSON.stringify({ opened, chipped }));
        """
    )
    assert res["chipped"] == ["outputs/deck.pptx"], "the reconnect must not consume the turn's own deliverable"


def test_the_turn_end_reseed_branch_refuses_a_failed_listing():
    """The defensive branch has to obey the same rule as the other two
    baseline writers: a failed listing knows nothing.

    Unreachable in normal flow (session-open claims the session first), which
    is exactly why it needs a test — nothing else would notice it drifting.
    Driven directly by dispatching turn-end with no prior session-open.
    """
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["outputs/old.pptx"];
        _failNext = true;
        await fire("agnes:turn-end");        // re-seed attempt, fetch fails
        await fire("agnes:turn-end");        // retry: seeds for real, no open
        await fire("agnes:turn-end");        // nothing new
        process.stdout.write(JSON.stringify({ opened, chipped, badge }));
        """
    )
    assert res["chipped"] == [], "a failed seed must not leave an empty baseline that fakes a fresh file"
    # The failed attempt reports nothing at all; the two that follow report
    # the real count. A `0` here would mean the failure was published.
    assert res["badge"] == [1, 1], res["badge"]


def test_a_failed_refresh_does_not_wipe_the_auto_open_baseline():
    """A listing that failed knows nothing — it must not be adopted as "these
    are the deliverables I have seen".

    `fetchSessionFiles` returns `files: []` with `ok: false`, so writing it
    into the baseline empties it; the next turn then reads every pre-existing
    file as brand new and pops the drawer over the reader, having been told
    nothing new was produced. The two event handlers already checked `ok`;
    `loadSessionFiles` — the manual open/Refresh path — did not.
    """
    res = _run_scenario(
        """
        currentChatId = "c1";
        _nextFiles = () => ["outputs/report.docx"];
        await fire("agnes:session-open", { chatId: "c1", switching: true });
        _drawerIsOpen = true;
        _failNext = true;                 // the user hits Refresh; it fails
        await loadSessionFiles();
        _drawerIsOpen = false; opened = 0;
        await fire("agnes:turn-end");     // nothing new this turn
        process.stdout.write(JSON.stringify({ opened, chipped, errors, badge }));
        """
    )
    assert res["errors"], "the failure itself must still be surfaced to the user"
    assert res["chipped"] == [], "a failed refresh must not turn a known file into a fresh one"
    assert res["badge"][-1] != 0, "and must not report the count as zero either"


def test_no_fetch_handler_paints_rows_for_a_conversation_the_user_has_left():
    """Every listing is a round-trip, and the user can switch during it. A
    late response must not paint its rows into the drawer: the rows carry
    that conversation's chat id in their download and save links, so a stale
    render puts the wrong conversation's files under the reader's cursor.

    The guard has to sit *before* the render, not merely before the baseline
    write — `loadSessionFiles` (the manual open/refresh path) had it after,
    so it repainted while the other two handlers correctly bailed.
    """
    js = _read(CHAT_JS)
    block = js[js.index("async function loadSessionFiles") : js.index("// ── drawer open/close")]
    guard = block.index("if (seq !== _filesSeq || currentChatId !== chatId) return;")
    assert guard < block.index("renderFileList("), "loadSessionFiles must bail before it repaints"

    # And the same ordering in the two event handlers, so this cannot be
    # re-introduced in one of them alone.
    tail = js[js.index('document.addEventListener("agnes:session-open"') :]
    for handler in ("agnes:session-open", "agnes:turn-end"):
        start = tail.index(f'document.addEventListener("{handler}"')
        end = (
            tail.index('document.addEventListener("agnes:turn-end"', start + 1)
            if handler == "agnes:session-open"
            else len(tail)
        )
        body = tail[start:end]
        assert body.index("seq !== _filesSeq || currentChatId !== chatId") < body.index("renderFileList("), (
            f"the {handler} handler renders before checking it still owns the conversation"
        )


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


def _css_block(css: str, selector: str) -> str:
    """The declaration body of one rule, by exact selector."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"expected a rule for {selector}"
    return m.group(1)


def test_the_chip_actions_stay_reachable_from_the_keyboard():
    """The actions are hidden at rest and revealed on ``:hover`` /
    ``:focus-within``. Hiding them with ``display: none`` makes that second
    half a lie — a display:none subtree is removed from the tab order, so its
    Download link and Save button are unfocusable and ``:focus-within`` can
    never fire from a keyboard. The chip would be mouse-only.

    Review finding on this PR. Measured after the fix in headless Chromium:
    Tab reaches ``#dl`` then ``#save``; while focus sits on Download the chip
    matches ``:focus-within`` and the actions measure opacity 1 / width 154px,
    against opacity 0 / width 0 at rest.
    """
    css = _read(Path("app/web/static/css/chat.css"))
    rest = _css_block(css, ".cloud-chat-file-chip-actions")
    assert not re.search(r"display:\s*none", rest), (
        "display: none takes the actions out of the tab order, so the "
        ":focus-within reveal below can never fire from the keyboard"
    )
    # And the reveal must actually key on focus, not hover alone.
    assert ".cloud-chat-file-chip:focus-within .cloud-chat-file-chip-actions" in css


def test_a_long_filename_can_ellipsize_inside_its_chip():
    """The chip is a flex container and its label sets ``text-overflow:
    ellipsis``. A flex child defaults to ``min-width: auto`` — its content
    width — so without an explicit ``min-width: 0`` the label refuses to
    shrink and a long agent-chosen name overflows the chip instead of
    truncating. Review finding on this PR; the chip's own ``max-width: 100%``
    does not help, because the overflow happens inside it.
    """
    css = _read(Path("app/web/static/css/chat.css"))
    chip = re.search(r"\.cloud-chat-file-chip \{([^}]*)\}", css)
    assert chip and "flex" in chip.group(1), "premise: the chip is a flex container"

    block = _css_block(css, ".cloud-chat-file-chip-label")
    assert "text-overflow: ellipsis" in block, "premise: the label truncates rather than wraps"
    assert re.search(r"min-width:\s*0", block), (
        "a flex child needs min-width: 0 or the ellipsis never fires"
    )
    # The chip is itself a flex item of the chips row, with the same
    # content-based min-width floor — which outranks its own max-width: 100%.
    # Without this the LABEL ellipsized and the chip still overflowed the
    # bubble: measured 282px inside a 260px bubble, 260px once set.
    assert re.search(r"min-width:\s*0", chip.group(1)), (
        "the chip needs min-width: 0 too, or max-width: 100% loses to its "
        "content-based minimum and it overflows the message bubble"
    )
