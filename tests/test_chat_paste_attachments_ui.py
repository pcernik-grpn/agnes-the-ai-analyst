"""Pasting a screenshot into the chat composer.

The gesture people actually use for a screenshot is Cmd-V. Before this, the
only route into a chat was the "+" menu's file dialog, which means saving the
image to disk first — so the guards here are about the paste path existing and
about the two things that silently break it:

1. **The filename.** ``app/api/chat_uploads.py`` REJECTS a name it dislikes
   (``_SAFE_FILENAME_RE``) rather than sanitizing it, and a macOS screenshot is
   called "Snímek obrazovky …" on a Czech machine. The client rebuilds the name
   before uploading, or every such paste 400s.
2. **The path in the message.** An upload the agent is never told about is an
   upload that did nothing. Each ready attachment appends one line naming
   ``uploads/<name>``, which resolves because ``WorkdirManager`` symlinks the
   workspace ``uploads`` directory into every session dir
   (``tests/test_chat_uploads.py`` guards the server half).

The behavioural tests run the SHIPPED module under node against stubs (there is
no DOM harness in CI), same ``_node_run`` pattern as
tests/test_chat_files_drawer_ui.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_HTML = Path("app/web/templates/chat.html")
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


def _module_slice() -> str:
    """The shipped ``ChatAttachments`` IIFE, sliced from the source so these
    tests run the real thing rather than a copy that can drift."""
    js = _read(CHAT_JS)
    start = js.index("const ChatAttachments = (() => {")
    end = js.index("})();", start)
    return js[start : end + len("})();")]


#: Just enough browser for the module: an id lookup, element factories that
#: record what was built, a clipboard-event dispatcher, and a fetch that
#: answers like the upload endpoint.
_HARNESS_PREAMBLE = """
const calls = [];        // every /api/chat/uploads request
const toasts = [];
const handlers = {};
let _uploadStatus = 200;
let _overlayOpen = false;

function _mkEl(tag) {
  const el = {
    tag, className: "", textContent: "", type: "", src: "", alt: "",
    hidden: false, dataset: {}, kids: [], attrs: {}, listeners: {},
    setAttribute(k, v) { el.attrs[k] = v; },
    addEventListener(name, fn) { el.listeners[name] = fn; },
    appendChild(c) { el.kids.push(c); return c; },
    replaceChildren(...c) { el.kids = c; },
    closest(sel) { return null; },
  };
  return el;
}
const strip = _mkEl("div");
strip.hidden = true;
const input = _mkEl("textarea");
input.id = "chat-input";
let focused = 0;
input.focus = () => { focused += 1; };
function $(id) {
  if (id === "chat-attachments") return strip;
  if (id === "chat-input") return input;
  return null;
}
const document = {
  activeElement: null,
  body: { nodeType: 1 },
  addEventListener(name, fn) { handlers[name] = fn; },
  createElement(tag) { return _mkEl(tag); },
  querySelector(sel) {
    return _overlayOpen && sel.indexOf("cloud-chat-upload-overlay") !== -1 ? _mkEl("div") : null;
  },
};
function showToast(text, kind) { toasts.push([text, kind]); }
const URL = { createObjectURL: () => "blob:preview", revokeObjectURL() {} };
class FormData {
  constructor() { this.fields = {}; }
  append(k, v, name) { this.fields[k] = name !== undefined ? name : v; }
}
async function fetch(url, opts) {
  calls.push({ url, kind: opts.body.fields.kind, filename: opts.body.fields.file });
  const name = opts.body.fields.file;
  return {
    ok: _uploadStatus === 200,
    status: _uploadStatus,
    json: async () => (_uploadStatus === 200
      ? { workspace_path: "uploads/" + name, filename: name }
      : { detail: "nope" }),
  };
}
/** A clipboard payload: files, plus optional plain text. */
function paste(files, text = "", target = document.body) {
  let prevented = false;
  handlers.paste({
    target,
    preventDefault() { prevented = true; },
    clipboardData: {
      items: files.map((f) => ({ kind: "file", getAsFile: () => f })),
      files: [],
      getData: () => text,
    },
  });
  return prevented;
}
const png = { name: "image.png", type: "image/png", size: 1024 };
"""


def _run(body: str) -> dict:
    return json.loads(_node_run(_HARNESS_PREAMBLE + _module_slice() + "\n" + body))


# ---------------------------------------------------------------------------
# Filename derivation — the thing that 400s if it drifts
# ---------------------------------------------------------------------------


def test_localized_screenshot_name_survives_the_server_filename_rule():
    """A Czech macOS screenshot ("Snímek obrazovky 2026-09-01 v 10.15.30.png")
    fails ``_SAFE_FILENAME_RE`` outright — non-ASCII and spaces. The client
    must rebuild it, not forward it."""
    out = _run("""
      const { safeUploadName } = ChatAttachments._pure;
      const d = new Date(2026, 8, 1, 10, 15, 30);
      console.log(JSON.stringify({
        czech: safeUploadName("Snímek obrazovky 2026-09-01 v 10.15.30.png", "image/png", d, 1),
        generic: safeUploadName("image.png", "image/png", d, 2),
        nameless: safeUploadName("", "image/png", d, 3),
        no_ext: safeUploadName("scan", "application/pdf", d, 4),
      }));
    """)
    server_re = r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,199}$"
    import re

    for name in out.values():
        assert re.match(server_re, name), f"{name!r} would be rejected by the upload endpoint"
    # The stamp + sequence are what keep two pastes of the browser's generic
    # "image.png" from overwriting each other in the workspace.
    assert out["generic"] == "pasted-20260901-101530-2.png"
    assert out["nameless"].endswith(".png")
    assert out["czech"].startswith("Sn_mek_obrazovky")
    # No extension in the name → taken from the mime type.
    assert out["no_ext"] == "scan-20260901-101530-4.pdf"


def test_two_pastes_in_the_same_second_get_different_names():
    out = _run("""
      const { safeUploadName } = ChatAttachments._pure;
      const d = new Date(2026, 8, 1, 10, 15, 30);
      console.log(JSON.stringify([
        safeUploadName("image.png", "image/png", d, 1),
        safeUploadName("image.png", "image/png", d, 2),
      ]));
    """)
    assert out[0] != out[1]


def test_kind_routes_data_files_away_from_document():
    """A pasted CSV sent as kind=document is a 415 — ``text/csv`` is not in the
    document allowlist. Route by extension first."""
    out = _run("""
      const { kindFor } = ChatAttachments._pure;
      console.log(JSON.stringify({
        png: kindFor("shot.png", "image/png"),
        no_mime: kindFor("shot.PNG", ""),
        pdf: kindFor("spec.pdf", "application/pdf"),
        csv: kindFor("rows.csv", "text/csv"),
        xlsx: kindFor("book.xlsx", "application/vnd.ms-excel"),
      }));
    """)
    assert out == {"png": "image", "no_mime": "image", "pdf": "document", "csv": "data", "xlsx": "data"}


# ---------------------------------------------------------------------------
# What the turn actually sends
# ---------------------------------------------------------------------------


def test_message_names_the_path_and_keeps_the_user_text():
    out = _run("""
      const { composeText } = ChatAttachments._pure;
      console.log(JSON.stringify({
        with_text: composeText("what is this?", [{ kind: "image", path: "uploads/a.png" }]),
        no_text: composeText("", [{ kind: "image", path: "uploads/a.png" }]),
        two: composeText("hi", [
          { kind: "image", path: "uploads/a.png" },
          { kind: "document", path: "uploads/b.pdf" },
        ]),
      }));
    """)
    assert out["with_text"].startswith("what is this?")
    assert "uploads/a.png" in out["with_text"]
    # A screenshot with no words is a complete message.
    assert out["no_text"].strip().startswith("[Attached image:")
    assert out["two"].count("[Attached") == 2
    assert "uploads/b.pdf" in out["two"]


# ---------------------------------------------------------------------------
# The paste gesture end to end
# ---------------------------------------------------------------------------


def test_pasted_image_uploads_and_becomes_a_ready_attachment():
    out = _run("""
      (async () => {
        const prevented = paste([png]);
        const taken = ChatAttachments.take();
        const { ready, failed } = await ChatAttachments.settle(taken);
        console.log(JSON.stringify({
          prevented,
          calls,
          chips: strip.kids.length,
          stripHiddenAfterTake: strip.hidden,
          ready: ready.map((r) => r.path),
          failed: failed.length,
          text: ChatAttachments.composeText("look", ready),
        }));
      })();
    """)
    assert out["prevented"] is True, "a file-only clipboard must not also paste"
    assert len(out["calls"]) == 1
    assert out["calls"][0]["url"] == "/api/chat/uploads"
    assert out["calls"][0]["kind"] == "image"
    assert out["calls"][0]["filename"].startswith("pasted-")
    assert out["failed"] == 0
    assert out["ready"][0].startswith("uploads/pasted-")
    assert out["ready"][0] in out["text"]
    # take() empties the strip in the same tick the composer clears.
    assert out["stripHiddenAfterTake"] is True


def test_clipboard_carrying_text_too_keeps_the_text():
    """Copying an image WITH its caption must not eat the caption — the files
    attach and the default paste still runs."""
    out = _run("""
      const prevented = paste([png], "the caption");
      console.log(JSON.stringify({ prevented, calls: calls.length }));
    """)
    assert out["prevented"] is False
    assert out["calls"] == 1


def test_paste_into_another_field_or_an_open_dialog_is_left_alone():
    out = _run("""
      const other = _mkEl("input");
      other.tagName = "INPUT";
      const a = paste([png], "", other);
      const inDialog = calls.length;
      _overlayOpen = true;
      const b = paste([png], "", document.body);
      console.log(JSON.stringify({ a, b, afterField: inDialog, afterOverlay: calls.length }));
    """)
    assert out["afterField"] == 0, "a paste into another input is a paste into that input"
    assert out["afterOverlay"] == 0, "the upload dialog has its own drop zone"


def test_failed_upload_is_reported_and_never_silently_named_in_the_message():
    out = _run("""
      (async () => {
        _uploadStatus = 413;
        paste([png]);
        const { ready, failed } = await ChatAttachments.settle(ChatAttachments.take());
        console.log(JSON.stringify({
          ready: ready.length,
          failed: failed.length,
          toasts: toasts.map((t) => t[1]),
          text: ChatAttachments.composeText("look", ready),
        }));
      })();
    """)
    assert out["ready"] == 0
    assert out["failed"] == 1
    assert "error" in out["toasts"]
    # The agent is never pointed at a file that is not there.
    assert out["text"] == "look"


def test_restore_puts_the_attachments_back_when_the_send_never_started():
    """The composer hands the text back when the chat backend is down; what was
    attached belongs to the same retry."""
    out = _run("""
      paste([png]);
      const taken = ChatAttachments.take();
      const empty = ChatAttachments.count();
      ChatAttachments.restore(taken);
      console.log(JSON.stringify({ empty, back: ChatAttachments.count(), chips: strip.kids.length }));
    """)
    assert out["empty"] == 0
    assert out["back"] == 1
    assert out["chips"] == 1


def test_oversized_paste_never_reaches_the_wire():
    """The substantive claim is `calls == 0` — nothing is uploaded.

    The toast is a `warn`, not an `error`: a file over the limit is a stated
    cap with an obvious next step, not a fault. Every such message used to be
    red, which is what taught readers to distrust the product over things it
    handled correctly (see tests/test_notice_component_contract.py)."""
    out = _run("""
      paste([{ name: "huge.png", type: "image/png", size: 21 * 1024 * 1024 }]);
      console.log(JSON.stringify({ calls: calls.length, toasts: toasts.map((t) => t[1]) }));
    """)
    assert out["calls"] == 0
    assert out["toasts"] == ["warn"], "the reader must still be told, just not alarmed"


def test_a_co_drive_session_refuses_the_paste_instead_of_half_doing_it():
    """A co-session runs in an ephemeral sandbox with NO link to any personal
    workspace (SR-6). The upload would succeed and the path would then be
    unopenable — so the paste is refused with a reason, not accepted."""
    out = _run("""
      ChatAttachments.setCoDrive(true);
      paste([png]);
      const during = calls.length;
      ChatAttachments.setCoDrive(false);
      paste([png]);
      console.log(JSON.stringify({
        during,
        after: calls.length,
        said: toasts.map((t) => t[0]).join(" "),
      }));
    """)
    assert out["during"] == 0
    assert out["after"] == 1, "leaving co-drive must restore the paste path"
    assert "co-drive" in out["said"]


def test_the_participants_frame_is_what_sets_co_drive():
    """``participant_emails`` is empty for a solo session, so a non-empty
    roster IS the co-drive signal — the same test renderCoPresence makes."""
    js = _read(CHAT_JS)
    block = js[js.index('case "session_participants":') : js.index('case "full_refresh":')]
    assert "ChatAttachments.setCoDrive((frame.participants || []).length > 0)" in block


# ---------------------------------------------------------------------------
# Wiring that has no runtime test (the page has to carry the strip)
# ---------------------------------------------------------------------------


def test_composer_carries_the_attachment_strip_and_its_styles():
    html = _read(CHAT_HTML)
    assert 'id="chat-attachments"' in html
    # ABOVE the pill: .cloud-chat-composer is a fixed-height flex row whose
    # geometry the send button is aligned against.
    assert html.index('id="chat-attachments"') < html.index('<div class="cloud-chat-composer">')
    css = _read(CHAT_CSS)
    assert ".cloud-chat-attachments {" in css
    assert ".cloud-chat-attachment-remove:focus-visible {" in css, "chips must keep a visible focus ring"


def test_submit_takes_settles_and_restores_attachments():
    """The three seams in ``submitUserMessage``: taken with the text, settled
    before the bubble renders, handed back when the turn never starts."""
    js = _read(CHAT_JS)
    body = js[js.index("async function submitUserMessage(text)") : js.index("/** Resize the composer textarea")]
    assert "ChatAttachments.take()" in body
    assert "ChatAttachments.settle(pendingAttachments)" in body
    assert "ChatAttachments.restore(pendingAttachments)" in body
    # The composed text is what renders and what is sent — not a hidden
    # instruction the reader cannot see.
    assert body.index("ChatAttachments.settle") < body.index('renderMessage({ role: "user"')
