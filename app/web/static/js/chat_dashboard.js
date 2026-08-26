// chat_dashboard.js — the rail pre-conversation Dashboard behavior.
//
// The rail layout's /chat empty state IS the Dashboard: one hero (greeting,
// Knowledge Layer banner, the real composer, Stack context line) and one
// "Suggested next actions" list below it — markup in chat.html's rail
// blocks, styles in css/chat_dashboard.css. This module owns the
// Dashboard-specific behavior and is driven by chat.js, which passes in
// the pieces of the one true chat lifecycle:
//
//   initChatDashboard({ submitPrompt, openSession })
//       — greeting fix-up + handler wiring. submitPrompt is chat.js's
//         submitUserMessage; openSession its in-place session opener.
//         Guided actions submit through them directly, so every suggestion
//         starts (or resumes) a conversation through the exact same flow
//         as a typed message. No navigation, no handoff, no second
//         composer.
//
//   updateDashboardSuggestions(sessions)
//       — builds + renders the Suggested-next-actions list once chat.js
//         has the caller's session list (no second request). Pass null
//         when that fetch failed: the personalized resume row is skipped
//         and the static suggestions still render (partial data beats a
//         broken section).
//
// Every function no-ops when the Dashboard markup is absent (topnav chat,
// or an active conversation restored from a deep link) — chat.js calls
// unconditionally.

const $ = (id) => document.getElementById(id);

// Handler handed over by chat.js in initChatDashboard.
let _submitPrompt = null;

// ---- Greeting -------------------------------------------------------------
// The server renders the salutation from ITS clock; re-derive from the
// browser clock so users in another timezone see the right time of day.
function fixGreeting() {
  const el = $("rdb-greeting-tod");
  if (!el) return;
  const h = new Date().getHours();
  el.textContent = h >= 5 && h < 12 ? "Good morning" : h >= 12 && h < 18 ? "Good afternoon" : "Good evening";
}

// ---- Guided task definitions ------------------------------------------------
//
// Conversation starters. Clicking a suggested-action item sends the task's
// `opener` as the first user message, which starts a chat where Agnes asks what
// it needs and guides the user to the goal. Suggested-next-actions rows are
// derived from these via buildSuggestedActions() below.
//
// Task shape:
//   id           stable slug
//   title        row title
//   description  relevance line (kept for the button's aria-label)
//   icon         inline SVG string
//   available    render enabled; false → disabled with the unavailable hint
//   opener       first user message that kicks off the guided conversation
//
// Openers ground Agnes in COMPANY knowledge first (catalog / metric definitions
// / memory over generic model knowledge) and ask it to say so honestly when
// something can't be found instead of inventing an answer.

const ICONS = {
  doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 3h8l4 4v14H6V3Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M14 3v4h4M9 12h6M9 16h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  person:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.7"/><path d="M5 20c1.2-3.2 3.8-4.8 7-4.8s5.8 1.6 7 4.8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  chart:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 19h16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="m5 14 4-4 3.5 3L18 7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  compare:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="5" width="7.5" height="14" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="5" width="7.5" height="14" rx="1.5" stroke="currentColor" stroke-width="1.7"/></svg>',
  calendar:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M4 9.5h16M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H4l1.6-3.2A8 8 0 1 1 21 12Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  bars: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 20h16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><rect x="5.5" y="11" width="3.4" height="6" rx="1" stroke="currentColor" stroke-width="1.6"/><rect x="10.8" y="7" width="3.4" height="10" rx="1" stroke="currentColor" stroke-width="1.6"/><rect x="16.1" y="13" width="3.4" height="4" rx="1" stroke="currentColor" stroke-width="1.6"/></svg>',
  search:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="1.7"/><path d="m20 20-3.4-3.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
};

// Each task's `opener` is the first user message sent when the card is
// clicked. It states the goal and asks Agnes to guide the user — so the click
// STARTS a conversation and Agnes replies with what it needs / what to do next
// to reach the goal, rather than opening a form up front.
const TASKS = [
  {
    id: "summarize",
    title: "Summarize a document",
    description: "Key points from any document in your company knowledge",
    icon: ICONS.doc,
    available: true,
    opener:
      "Help me summarize a document from our company knowledge. Ask me which document and how I'd like it summarized (executive, key decisions, action items, or detailed), then find it and summarize it — and tell me if you can't find it.",
  },
  {
    id: "find-owner",
    title: "Find an owner or expert",
    description: "Who owns — or knows the most about — a topic, dataset, or area",
    icon: ICONS.person,
    available: true,
    opener:
      "Help me find who owns or knows the most about something. Ask me the topic, dataset, project, or area, then search our company knowledge and tell me who to talk to — separating confirmed owners from likely experts.",
  },
  {
    id: "explain-metric",
    title: "Explain a metric or term",
    description: "Grounded in your company's canonical definitions",
    icon: ICONS.chart,
    available: true,
    opener:
      "Help me understand a metric or term we use. Ask me which one, then explain it using our canonical definition (from the catalog), how it's calculated, and who owns it — and say if we don't have a company definition.",
  },
  {
    id: "compare-revenue",
    title: "Compare revenue trends",
    description: "Across products, regions, or time periods",
    icon: ICONS.bars,
    available: true,
    opener:
      "Help me compare revenue trends. Ask me the metric, whether to break it down by product, region, or time, and the time range — then run the numbers from our data and show the breakdown with sources.",
  },
];

// ---- Suggested next actions — the personalization boundary -----------------
//
// TODO(personalization): replace buildSuggestedActions() with a backend
// recommendation source (e.g. GET /api/me/suggested-actions) that ranks
// actions from the caller's Stack (knowledge sources, skills) and accessible
// company context. The renderer only consumes the typed shape — swapping the
// source requires no UI change.
//
// Today it is the fixed set of guided-task defaults (honest capability
// statements, NOT presented as AI-generated recommendations).
//
// Suggested-action shape (what the renderer reads):
//   id           stable slug
//   kind         "task" (dialog-backed prompt)
//   title        card title
//   reason       short description (muted, under the title)
//   icon         inline SVG string
//   priority     ascending sort rank
//   available    false → disabled card with an unavailable hint
//   task         the TASKS entry to run

/** Build the caller's suggested-action list — the four guided
 *  conversation-starter cards. `sessions` is accepted for signature
 *  compatibility with chat.js but no longer consumed: the dashboard row is a
 *  fixed set of guided tasks, and resuming a past conversation lives in the
 *  rail's Chats history, so there is no per-session "resume" card here. */
function buildSuggestedActions(_sessions) {
  return TASKS.map((task, i) => ({
    id: task.id,
    kind: "task",
    task,
    title: task.title,
    reason: task.description,
    icon: task.icon,
    priority: 10 + i,
    available: task.available,
  }));
}

// ---- Suggested next actions — renderer --------------------------------------

function _runAction(action) {
  if (!action.available) return;
  if (action.kind === "task" && _submitPrompt && action.task?.opener) {
    // Start the conversation: send the opener as the first user message, and
    // Agnes replies asking what it needs / what to do next to reach the goal.
    _submitPrompt(action.task.opener);
  }
}

/** One guided-task row: pale-mint icon tile + bold title, centred under the
 *  composer. The whole row is the button.
 *
 *  It used to carry a trailing "Start →" as well, which was the row saying
 *  twice what it does: four rows under an input, each already a button, do not
 *  need a per-row verb — and the four repeated CTAs pulled the eye down the
 *  right margin, away from the titles that actually differ. */
function _renderActionCard(action) {
  const li = document.createElement("li");
  li.className = "rdb-action-card";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rdb-action";
  btn.dataset.action = action.id;
  btn.disabled = !action.available;
  btn.setAttribute(
    "aria-label",
    `${action.title} — ${action.available ? action.reason : "not available on this instance"}`,
  );

  const icon = document.createElement("span");
  icon.className = "rdb-action-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = action.icon;

  // Title + description stacked; the description is DOM-only (hidden in CSS,
  // read by the button's aria-label).
  const txt = document.createElement("span");
  txt.className = "rdb-action-txt";
  const title = document.createElement("span");
  title.className = "rdb-action-title";
  title.textContent = action.title;
  const reason = document.createElement("span");
  reason.className = "rdb-action-reason";
  reason.textContent = action.available ? action.reason : "Not available on this instance";
  txt.append(title, reason);

  btn.append(icon, txt);

  if (action.available) {
    btn.addEventListener("click", () => _runAction(action));
  }
  li.appendChild(btn);
  return li;
}

/** Render the Suggested-next-actions card row. Idempotent — safe to call on
 *  init and again from chat.js. `sessions` is unused (kept for the chat.js
 *  call signature); the row is a fixed set of guided-task cards. */
export function updateDashboardSuggestions(sessions) {
  const section = $("rdb-actions");
  const list = $("rdb-actions-list");
  if (!section || !list) return; // topnav, or dashboard markup not on this page
  const loading = $("rdb-actions-loading");
  const empty = $("rdb-actions-empty");
  if (loading) loading.hidden = true;

  const actions = buildSuggestedActions(sessions);

  list.innerHTML = "";
  if (empty) empty.hidden = actions.length > 0;
  for (const action of actions) list.appendChild(_renderActionCard(action));
}

// ---- init ---------------------------------------------------------------------

/** Wire the Dashboard empty state. No-ops when its markup is absent
 *  (topnav chat). ``submitPrompt`` is chat.js's submitUserMessage. Renders the
 *  guided-action cards immediately so they paint with the page (no loading
 *  flash); chat.js may call updateDashboardSuggestions() again — it's
 *  idempotent. `openSession` is accepted for call-site compatibility. */
export function initChatDashboard({ submitPrompt }) {
  if (!$("rdb-actions")) return;
  _submitPrompt = submitPrompt;
  fixGreeting();
  updateDashboardSuggestions(null);
}
