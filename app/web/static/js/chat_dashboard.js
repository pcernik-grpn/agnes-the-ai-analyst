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
// The server-rendered capability snapshot (chat.js passes it in). Null means
// "unknown", never "empty" — see buildSuggestedActions().
let _capabilities = null;

// ---- Greeting -------------------------------------------------------------
// The server renders the salutation from ITS clock; re-derive from the
// browser clock so users in another timezone see the right time of day.
function fixGreeting() {
  const el = $("rdb-greeting-tod");
  if (!el) return;
  const h = new Date().getHours();
  const evening = h < 5 || h >= 18;
  el.textContent = h >= 5 && h < 12 ? "Good morning" : h >= 12 && h < 18 ? "Good afternoon" : "Good evening";
  // The sun/moon glyph rides the same correction — both are in the DOM and
  // `data-tod` shows one (see `.cld-greet` in style-custom.css), so the words
  // and the picture can never disagree about what time it is.
  const greet = el.closest("[data-tod]");
  if (greet) greet.setAttribute("data-tod", evening ? "night" : "day");
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

// ---- Zero-data starters ---------------------------------------------------
//
// Every entry in TASKS above needs company data to succeed: "Compare revenue
// trends" against an instance with no reachable tables cannot do anything but
// apologise. Offering all four anyway is the page's last dishonest offer, so
// when the caller can reach nothing these take their place.
//
// Split by what the caller can actually DO about it. An admin can connect a
// source; a member cannot, and telling them to would be a dead end — their
// route is asking whoever can. Both sets stay answerable with no data at all:
// they ask Agnes about itself and about this workspace, not about numbers.

const ADMIN_ZERO_TASKS = [
  {
    id: "zero-how-to-connect",
    title: "How do I connect our data?",
    description: "What Agnes needs from your warehouse, and in what order",
    icon: ICONS.doc,
    available: true,
    opener:
      "I am an admin setting this instance up and nothing is connected yet. Walk me through "
      + "connecting our data: ask which system it lives in, then tell me what you need from me, "
      + "what happens after the connection exists, and what I still have to do before anyone on "
      + "my team can ask a question. Be concrete about the order.",
  },
  {
    id: "zero-what-possible",
    title: "What will people be able to ask?",
    description: "What Agnes can answer once data is connected",
    icon: ICONS.chat,
    available: true,
    opener:
      "Nothing is connected here yet. Explain what my team will be able to ask you once our data "
      + "is connected and shared — the kinds of questions you can answer well, and the kinds you "
      + "cannot. Be honest about the limits.",
  },
];

const MEMBER_ZERO_TASKS = [
  {
    id: "zero-what-is-here",
    title: "What is in this workspace?",
    description: "What exists here, and what you can reach",
    icon: ICONS.search,
    available: true,
    opener:
      "I cannot reach any company data here yet. Tell me what this workspace contains, what I "
      + "personally have access to, and what I would need access to before I can ask real "
      + "questions about our numbers.",
  },
  {
    id: "zero-get-access",
    title: "How do I get access?",
    description: "Who grants it, and what to ask them for",
    icon: ICONS.person,
    available: true,
    opener:
      "I do not have access to any data here. Explain how access works in Agnes — who grants it, "
      + "what the unit of access is called, and exactly what I should ask my admin for. Do not "
      + "guess at a person's name unless you can find one.",
  },
];

// ---- Admin starters, on an instance that HAS data -------------------------
//
// The four member starters are about the numbers; an admin's own questions are
// about who can reach them. These are the governance gaps that actually bite —
// a table in no package cannot be shared or pulled, and a package granted to
// nobody is invisible to every analyst — both of which instances discover late.
//
// Each opener tells Agnes to CHECK rather than assert, and to say so when it
// cannot: an admin acting on an invented access answer is worse served than one
// told the question needs the admin pages.

const ADMIN_TASKS = [
  {
    id: "admin-who-sees-what",
    title: "Who can see what?",
    description: "Which groups reach which packages, and who is in them",
    icon: ICONS.person,
    available: true,
    opener:
      "Give me an access picture of this instance: which data packages exist, which groups are "
      + "granted each one, and roughly how many people are in those groups. Check the real state "
      + "rather than guessing, and tell me plainly if you cannot read some of it.",
  },
  {
    id: "admin-unreachable",
    title: "What can nobody reach?",
    description: "Tables in no package, and packages granted to no group",
    icon: ICONS.search,
    available: true,
    opener:
      "Find the things nobody can reach in this instance: registered tables that are in no data "
      + "package, and packages that are granted to no group. Both are invisible to analysts. List "
      + "what you find and say which fix each one needs — and if you cannot check, say so.",
  },
  {
    id: "admin-definitions",
    title: "What are we missing definitions for?",
    description: "Metrics people will ask about that have no canonical answer",
    icon: ICONS.chart,
    available: true,
    opener:
      "Look at what data is registered here and tell me which important metrics have no canonical "
      + "definition in the catalog yet — the ones where two people would compute a different "
      + "number. Do not invent definitions; just tell me where the gaps are.",
  },
  {
    id: "admin-try-as-analyst",
    title: "What would an analyst see?",
    description: "The same workspace from a member's side",
    icon: ICONS.compare,
    available: true,
    opener:
      "Describe what someone on my team with ordinary (non-admin) access would find in this "
      + "workspace right now — what they could ask about, and what they would hit a wall on. Base "
      + "it on the real grants, not on what is theoretically installed.",
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
  // With no reachable tables the data starters cannot succeed, so the honest
  // set depends on who is looking. `_capabilities` is null when the snapshot
  // was absent or unparseable — that is not evidence of an empty instance, so
  // it falls through to the normal set rather than accusing a working
  // workspace of being empty.
  const total = _capabilities ? (_capabilities.tables_total || 0) : null;
  const isAdmin = !!(_capabilities && _capabilities.is_admin);
  let tasks;
  if (total === 0) {
    // Nothing reachable: the data starters can only apologise. Split by what
    // the caller can act on — an admin can connect a source, a member cannot.
    tasks = isAdmin ? ADMIN_ZERO_TASKS : MEMBER_ZERO_TASKS;
  } else if (isAdmin) {
    // Data exists, and an admin's own questions are about who reaches it.
    tasks = ADMIN_TASKS;
  } else {
    tasks = TASKS;
  }
  return tasks.map((task, i) => ({
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
export function initChatDashboard({ submitPrompt, capabilities }) {
  if (!$("rdb-actions")) return;
  _submitPrompt = submitPrompt;
  _capabilities = capabilities || null;
  fixGreeting();
  updateDashboardSuggestions(null);
}
