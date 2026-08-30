// chat_onboarding.js — chat-driven onboarding layer.
//
// The onboarding is driven BY the chat itself (not a separate wizard): Agnes
// greets once, and when a first question lands against an empty Stack she
// recommends the packages that would answer it, subscribes them on the user's
// say-so, then resumes the original question — all inside the conversation.
// A brand-new user additionally gets the coach-mark tour launched over the
// composer on their first /chat visit (see maybeAutoLaunchTour) — the landing
// page is otherwise a blank input that explains nothing — and sending their
// first message takes it back down.
//
// A small "Set up Agnes" panel tracks the five progress steps
// and persists to `/api/chat/journey` (per-user, both backends). Under the rail
// layout it lives in a popover behind the onboarding card above the bottom nav;
// on topnav it stays inline in the chat sidebar.
//
// This module owns its own DOM (greeting bubbles, gap-resolver card, journey
// panel) so it needs only a few well-defined hooks from chat.js:
//   • renderAssistant(markdown)  — append an Agnes bubble to the thread
//   • appendNode(el)             — append a raw node to #chat-messages
//   • resubmit(text)             — re-enter submitUserMessage with `text`
//   • scrollToBottom()           — keep the newest node in view
//   • scrollLastIntoView()       — force the newest node into view (fallback)

// The checklist renders in THIS order, and it is the order the coach-mark tour
// walks (TOURS.welcome in tour.js): ask → see your Library → put something in
// your stack → add or share your own → take Agnes to your tools. It used to run
// stack-before-Library, which contradicted the tour and, worse, put the one
// step you cannot complete by looking at anything (`stack_setup_done` lands
// when a package is actually subscribed) ahead of the one the tour completes
// for you — so finishing the tour ticked step 3 and left step 2 pending.

const STEP_KEYS = [
  "first_asked",
  "explored_stack",
  "stack_setup_done",
  "catalog_discovered",
  "use_anywhere",
  "agent_created",
];

const DEFAULT_JOURNEY = {
  first_asked: false,
  stack_setup_done: false,
  explored_stack: false,
  catalog_discovered: false,
  use_anywhere: false,
  agent_created: false,
  onboarded: false,
  successful_answers: 0,
};

// The newcomer walkthrough in tour.js (TOURS.welcome): composer → Library →
// "in stack" → add → share. Named once here because four things reach for it —
// the checklist steps that open it AT THEIR OWN CARD, the "↻" replay, and the
// unattended first-visit launch below. TOURS.connect is the one-card micro-tour
// for the last step, which lives on another page entirely. (Agents has a card
// too, but it isn't reached from here: TOURS.agents fires on a first visit to
// /agents, which is where explaining that surface belongs.)
const WELCOME_TOUR = "welcome";
const CONNECT_TOUR = "connect";

// Every step declares `tour: { id, step }` — the coach-mark that SHOWS the
// user how to do it, addressed by the step's stable key (tour.js owns the
// numbering; see tourIndexOf).
//
// This is the difference between a checklist and onboarding. Two of these rows
// used to launch the welcome tour at index 0, so clicking "Put knowledge in your
// stack" replayed the composer card and left the reader to walk four cards back
// to their own step; the other three were bare links that dropped them on a page
// with no indication of which control was the one they had just asked about. A
// row now opens the walkthrough AT the card for that row, on the right page,
// pointing at the control that completes it — and the walkthrough continues
// forward from there, so "show me one thing" naturally becomes "show me the
// rest".
const STEP_META = {
  first_asked: {
    label: "Ask your first question",
    why: "Start from your real goal so Agnes can shape onboarding around what you need.",
    // Reachable from every rail page, not just /chat: the checklist is mounted
    // on all of them (mountJourneyPanel), and a step with no destination was a
    // dead row anywhere else — the one row a newcomer is most likely to click.
    href: "/chat",
    tour: { id: WELCOME_TOUR, step: "ask" },
  },
  // The three middle steps keep their original column names — they are journey
  // columns on both backends, and renaming them would cost a migration to say
  // the same thing — but their LABELS follow the current IA, where the Library
  // is the one place knowledge lives and "in stack" is the subset the chat
  // agent draws on. `explored_stack` = the Library has been looked at (the one
  // step the tour can complete on your behalf, because walking you through the
  // Library is literally what it does); `stack_setup_done` = something is
  // actually in the stack; `catalog_discovered` = you have added or shared
  // something of your own. Rendered in STEP_KEYS order, not this one.
  explored_stack: {
    label: "Explore your Library",
    why: "Your Library holds everything Agnes can read: what your organization shares with you, plus what you add.",
    href: "/library",
    // Deliberately NO tour hand-off. The welcome tour's `library` card lives on
    // /chat and rings the rail's Library NAV ITEM — correct inside a
    // walkthrough that is teaching the chrome, and wrong for this row, which
    // promises to show you your Library. Clicking it on /chat therefore drew a
    // ring around a nav link and went nowhere (and if that anchor didn't
    // resolve, the tour silently rendered nothing at all) — so the row read as
    // "tick a box", which is exactly what a checklist must never do. Opening
    // the Library IS this step; the visit marks it server-side (see
    // app/web/router.py::library_page).
  },
  stack_setup_done: {
    label: "Put knowledge in your stack",
    why: "Your stack is what Agnes actually draws on — it needs the right company knowledge in it to answer usefully.",
    href: "/library",
    tour: { id: WELCOME_TOUR, step: "in-stack" },
  },
  catalog_discovered: {
    label: "Add or share something",
    why: "Upload a file only you have, or share what you built so your team — and their agents — can use it too.",
    href: "/library",
    tour: { id: WELCOME_TOUR, step: "add" },
    // No loose "Or build an agent of your own" link here. That link used to
    // hang off this row and rendered full-width BETWEEN two steps, so the
    // reader had to work out that the thing mid-list was not itself a step.
    // Agents is a step now (`agent_created`, last) — a row, keyed and tracked
    // like every other, which is the shape that was missing.
  },
  // Deliberately NOT a sixth step. This one covers "Agnes beyond this browser
  // tab" in both directions — where you ask her from (the MCP connector) and
  // where she reaches you (notifications) — so the notification channel rides
  // along as a `sub` action instead of its own checkbox. A sixth journey column
  // would default FALSE for every already-onboarded user, un-retiring the rail's
  // completed "Set up Agnes" card and re-nagging exactly the people who
  // finished; and the checklist's completion rate drops with every step added.
  // The canonical home for the setting stays /me/profile#notifications — this is
  // a signpost to it, not a second implementation.
  use_anywhere: {
    label: "Take Agnes to your tools",
    why: "Reuse the same trusted company context in Claude Code, Cursor, and VS Code — and pick where Agnes reaches you when a long run finishes.",
    href: "/how-it-works#connect",
    tour: { id: CONNECT_TOUR, step: "connect" },
    sub: {
      href: "/me/profile#notifications",
      label: "Choose where Agnes reaches you",
      doneLabel: "Telegram connected ✓",
      // Only rendered where the operator actually configured the channel —
      // see subActionHtml. Every other sub-action renders unconditionally.
      gate: "telegram",
    },
  },
  // Last, because it is the one step that presumes the others: an agent is
  // built OUT OF the stack, so it only means anything once there is something
  // in it. TOURS.agents still runs as a coach-mark on /agents — that one shows
  // WHERE, this shows what is done.
  agent_created: {
    label: "Create your first agent",
    why: "An agent is a named specialist scoped to part of your stack — it answers with only what you point it at, and you can hand it to your team or call it as an API.",
    href: "/agents",
  },
};

let journey = { ...DEFAULT_JOURNEY };
let hooks = {};
let ready = false;
// Soft, in-memory "not now" for the inline journey panel — set by the head's
// "×" and honored by renderJourneyPanel so a later patch can't re-pop it.
// Resets on reload (the panel is meant to come back next visit).
let dismissed = false;
// chatMode: on /chat the module has the full chat hooks (greeting, gap
// resolver, in-thread replay). On other rail pages it's mounted standalone
// (mountJourneyPanel) as a navigable tracker only — the chat-only affordances
// (the "?" replay) are omitted there because they'd have nothing to talk to.
let chatMode = false;

async function apiJson(path, init) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

// ── Notification channel ────────────────────────────────────────────────────
// The one channel with a self-serve link flow today. `window._agTelegramBot`
// (stamped by base_ds.html) is BOTH the handle the user is told to message and
// the gate: empty means the operator configured no bot, so every notification
// affordance here stays hidden rather than sending someone to "@your-bot".
// The macOS app is invite-only with no link flow, so it isn't offered.
function telegramBot() {
  return String(window._agTelegramBot || "").trim();
}

// null = unknown/not checked (renders nothing rather than a wrong state).
let notifyLinked = null;

async function loadNotifyState() {
  if (!telegramBot()) return;
  try {
    const data = await apiJson("/api/telegram/status");
    notifyLinked = !!data.linked;
  } catch (_) {
    notifyLinked = null;
  }
}

async function loadJourney() {
  try {
    const data = await apiJson("/api/chat/journey");
    journey = { ...DEFAULT_JOURNEY, ...data };
  } catch (_) {
    journey = { ...DEFAULT_JOURNEY };
  }
}

// Persist a partial update. Optimistically merge + re-render first so the UI
// feels instant; the PUT is fire-and-forget (onboarding progress is soft
// state — a dropped write just re-nudges next time).
async function patchJourney(fields) {
  journey = { ...journey, ...fields };
  renderJourneyPanel();
  try {
    await apiJson("/api/chat/journey", {
      method: "PUT",
      body: JSON.stringify(fields),
    });
  } catch (_) {
    /* soft state — ignore */
  }
}

// ── Stack helpers ─────────────────────────────────────────────────────────
// Only DATA_PACKAGE + MEMORY_DOMAIN are subscribable (see app/api/stack.py).
const STACK_TYPES = [
  { type: "data_package", label: "data" },
  { type: "memory_domain", label: "memory" },
];

async function browseStack() {
  const out = [];
  for (const { type } of STACK_TYPES) {
    try {
      const data = await apiJson(`/api/stack/browse?type=${encodeURIComponent(type)}`);
      for (const item of data.items || []) out.push({ ...item, resource_type: type });
    } catch (_) {
      /* type may be unavailable for this instance — skip */
    }
  }
  return out;
}

async function subscribe(resourceType, resourceId) {
  await apiJson("/api/stack/subscribe", {
    method: "POST",
    body: JSON.stringify({ resource_type: resourceType, resource_id: resourceId }),
  });
}

function inStackCount(items) {
  return items.filter((i) => i.in_stack).length;
}

// ── Journey panel ───────────────────────────────────────────────────────────
// A step's secondary destination, rendered as a real <a> SIBLING of the step
// button — it cannot go inside it (a button may not contain a link), and as an
// anchor it navigates natively so no click handler is needed. Rendered on every
// step: nothing in the checklist is gated on reaching it first.
//
// `gate: "telegram"` is the notification channel's own special case — that one
// sub-action reflects link STATE and must vanish entirely on an instance with no
// bot configured, or it would send someone to message "@your-bot". The gate is
// per-sub rather than global rather than being the function's second line, so a
// sub-action added later can't inherit a Telegram dependency that has nothing to
// do with it.
//
// No `title` on these: the label is the whole affordance, and a native tooltip
// inside a popover this narrow opens an OS box over the rows below it.
function subActionHtml(step) {
  const sub = step.sub;
  if (!sub) return "";

  if (sub.gate === "telegram") {
    if (!telegramBot()) return "";
    const linked = notifyLinked === true;
    const label = linked ? sub.doneLabel : sub.label;
    return `<a class="cloud-chat-journey-sub-action${linked ? " is-linked" : ""}"
      href="${escapeAttr(sub.href)}">${escapeHtml(label)}</a>`;
  }

  return `<a class="cloud-chat-journey-sub-action"
    href="${escapeAttr(sub.href)}">${escapeHtml(sub.label)}</a>`;
}

// GO THERE. A row means "take me to where I do this" — it takes the reader to
// the page the work happens on and, where there is one, opens the coach-mark at
// the control that completes it. tour.js cross-navigates by itself when the card
// belongs to another page, so there is no separate "navigate then hope" branch.
//
// It does NOT tick the step. Clicking a row used to mark three of the five
// milestones done on the spot, which is how the card ended up feeling like a
// to-do list you check off yourself: the reader clicked "Explore your Library",
// watched the row go ✓ struck-through, and was not taken to the Library. Every
// milestone is now earned where the work happens, server-side, so the tick means
// something:
//   • `first_asked`        — a question was actually asked (chat API)
//   • `explored_stack`     — the Library page was actually opened (web route)
//   • `stack_setup_done`   — something was actually put in the stack (stack API);
//                            it also gates the in-chat gap resolver, so ticking
//                            it early would suppress that help
//   • `catalog_discovered` — something was actually added or shared (sharing /
//                            collections API)
//   • `use_anywhere`       — the connect page was actually opened (web route)
// See app/services/journey.py for the rule those call sites follow.
//
// Keyed off the journey key rather than the clicked element, so every row shares
// one implementation.
function runStep(key) {
  const step = STEP_META[key];
  if (!step) return;

  if (step.tour) {
    // Resolve the card's index from its stable key at click time (tour.js owns
    // the numbering). Uses window._agTourUrl (stamped by the tour boot script in
    // base_ds.html) with a bare-path fallback; if the module can't load at all,
    // fall back to plain navigation — worse, but never a dead click.
    tourModule()
      .then((mod) => {
        mod.launchTour(step.tour.id, mod.tourIndexOf(step.tour.id, step.tour.step));
      })
      .catch(() => {
        if (step.href) window.location.href = step.href;
      });
    return;
  }
  if (step.href) window.location.href = step.href;
}

function renderJourneyPanel() {
  const el = document.getElementById("chat-journey");
  if (!el) return;
  if (!ready || dismissed) {
    el.hidden = true;
    return;
  }
  el.hidden = false;

  const steps = STEP_KEYS.map((k) => ({ k, done: !!journey[k], ...STEP_META[k] }));
  // The step the card points at with "→". It is a suggestion of where to
  // resume, not a cursor that blocks the rows after it — every row is clickable
  // in any order (see the row loop below).
  const nextIdx = steps.findIndex((s) => !s.done);
  const complete = nextIdx === -1;
  const doneCount = steps.filter((s) => s.done).length;
  // Keep the rail's onboarding card in sync with progress (no-op when those
  // elements aren't present, e.g. topnav where the journey stays inline).
  updateGetStartedIndicator(doneCount, steps.length, complete);

  const rows = steps
    .map((s, i) => {
      // EVERY row is clickable — the checklist is a menu, not a rail. Progress
      // was never strictly sequential (the tour completes "Explore your Library"
      // for you, a step can be clicked out of turn, and the middle steps land
      // from real activity server-side), so positional locking only ever
      // punished the reader: a step ahead of the cursor read as disabled even
      // though nothing stopped it from being done, and there was no way back to
      // a finished step to re-read what it taught. `active` is now purely the
      // "you are here" marker on the next unfinished step, not a gate.
      const cls = s.done ? "done" : i === nextIdx ? "active" : "todo";
      const mark = s.done ? "✓" : i === nextIdx ? "→" : "•";
      // Which coach-mark this row opens, by tour id + the step's own stable key.
      // The index is resolved at click time via tour.js's tourIndexOf, never
      // written down here — a number in this file silently rots the first time
      // a card is inserted into the walkthrough.
      const tourAttr = s.tour
        ? ` data-journey-tour="${escapeAttr(s.tour.id)}" data-journey-tour-step="${escapeAttr(s.tour.step)}"`
        : "";
      // Carry the step key so the click handler marks the step the user actually
      // clicked — not one inferred from the (shared) href. "Explore your Library"
      // and "Put knowledge in your stack" both point at /library, so href alone
      // is ambiguous.
      const keyAttr = ` data-journey-key="${escapeAttr(s.k)}"`;
      const attrs = s.href ? ` data-journey-go="${s.href}"${tourAttr}${keyAttr}` : "";
      // The "why" is rendered as a real line under the step the card suggests,
      // NOT as a `title` on every row. A native tooltip is the wrong carrier for
      // the one sentence that explains the step: it needs a hover the reader
      // doesn't know to try, never appears on touch, and the OS box it opens is
      // wide enough to cover the rows underneath the one being explained. One
      // visible line on one row also gives a very plain list a focal point.
      const whyHtml =
        cls === "active"
          ? `<span class="cloud-chat-journey-why">${escapeHtml(s.why)}</span>`
          : "";
      // No trailing chevron. It was here to say "this row goes somewhere", but
      // five of them down the right edge added a second column of glyphs to a
      // list that already has one on the left, and at this width they crowded
      // labels that wrap. The hover wash carries the same signal.
      return `<button type="button" class="cloud-chat-journey-step ${cls}"${attrs}>
        <span class="cloud-chat-journey-mark" aria-hidden="true">${mark}</span>
        <span class="cloud-chat-journey-body">
          <span class="cloud-chat-journey-label">${escapeHtml(s.label)}</span>
          ${whyHtml}
        </span>
      </button>${subActionHtml(s)}`;
    })
    .join("");

  // The popover heads with the same words as the card that opened it — being
  // sent from "Set up Agnes" to a panel called something else reads as two
  // different features.
  const brand = escapeHtml(brandShort());
  const heading = complete
    ? `Set up ${brand}`
    : steps.some((s) => s.done)
      ? "Continue setup"
      : `Set up ${brand}`;
  // No "×" when the checklist is the rail's "Set up Agnes" popover: that panel
  // is transient chrome hanging off a launcher and already closes four other
  // ways (mouse-leave, Escape, click-away, a second click on the card — all in
  // rail_history.js), so a dismiss button inside it was a fifth route to the
  // same outcome, spending one of the two slots in a very compact header. The
  // INLINE panel keeps it: in the topnav /chat sidebar the checklist is part of
  // the page, and "×" is the only way to put it away for the page load
  // (dismissJourney handles both mount contexts).
  const inRailPopover = !!el.closest(".rail-getstarted-panel");
  el.innerHTML = `
    <div class="cloud-chat-journey-head">
      <h3>${heading}</h3>
      ${complete ? '<span class="cloud-chat-journey-badge">Complete ✓</span>' : ""}
      <div class="cloud-chat-journey-actions">
        <button type="button" class="cloud-chat-journey-iconbtn" data-journey-replay
          title="Replay the ${brand} tour" aria-label="Replay the tour">↻</button>
        ${
          inRailPopover
            ? ""
            : `<button type="button" class="cloud-chat-journey-iconbtn" data-journey-close
          title="Close" aria-label="Close onboarding">×</button>`
        }
      </div>
    </div>
    ${
      // Progress, not a slogan. "Learn what Agnes is and make it yours" spent
      // this line saying nothing the heading above it hadn't already said, in a
      // popover barely wider than one row of it. Where you ARE is the fact worth
      // the line — and it has to be stated here, because the INLINE mount (the
      // topnav /chat sidebar) has no launcher card to read it off.
      //
      // Text only, no progress bar: under the rail the launcher sits directly
      // below this popover and keeps its own bar on screen the whole time it is
      // open, so a second one is the same fraction drawn twice.
      complete
        ? `<p class="cloud-chat-journey-sub">All ${steps.length} steps done — you can start over any time.</p>`
        : `<p class="cloud-chat-journey-sub">${doneCount} of ${steps.length} done</p>`
    }
    <div class="cloud-chat-journey-list">${rows}</div>
    <button type="button" class="cloud-chat-journey-finish" ${complete ? "data-journey-restart" : "data-journey-finish-all"}>
      ${complete ? "Start over" : "Skip onboarding"}
    </button>`;

  // No guard on the click: the rows ARE the panel's actions now. The separate
  // primary CTA that used to name the next step is gone — it ran this same
  // runStep() path on the row directly above it, and being the only
  // button-shaped thing in the panel it made the rows themselves read as inert
  // text.
  el.querySelectorAll("[data-journey-go]").forEach((btn) => {
    btn.addEventListener("click", () => runStep(btn.getAttribute("data-journey-key")));
  });

  const finishAllBtn = el.querySelector("[data-journey-finish-all]");
  if (finishAllBtn) {
    finishAllBtn.addEventListener("click", async () => {
      await patchJourney({
        first_asked: true,
        stack_setup_done: true,
        explored_stack: true,
        catalog_discovered: true,
        use_anywhere: true,
        agent_created: true,
      });
      // Say where it went, not just that it happened: this click retires the
      // whole card, and the only route back is a profile-menu entry the reader
      // has no reason to have noticed yet.
      if (window.showToast) {
        window.showToast("Onboarding skipped — restart it any time from your profile menu", {
          type: "success",
        });
      } else {
        // Soft fallback — non-blocking status message in the panel.
        finishAllBtn.textContent = "Skipped ✓";
        finishAllBtn.disabled = true;
      }
    });
  }

  // "Start over" — only rendered once every step is done ("Skip onboarding"
  // has nothing left to skip in that state, and "↻" only relaunches the
  // coach-mark tour, not the journey checklist itself, so there was no way back
  // to the start). patchJourney's optimistic merge + synchronous re-render is
  // the feedback: the checklist unchecks, the "Complete ✓" badge drops, and this
  // button reverts to "Skip onboarding" — no separate toast needed.
  const restartBtn = el.querySelector("[data-journey-restart]");
  if (restartBtn) restartBtn.addEventListener("click", restartJourney);

  const replayBtn = el.querySelector("[data-journey-replay]");
  if (replayBtn) replayBtn.addEventListener("click", replayTour);
  const closeBtn = el.querySelector("[data-journey-close]");
  if (closeBtn) closeBtn.addEventListener("click", dismissJourney);
}

// Back to step one. patchJourney's optimistic merge re-renders synchronously,
// so updateGetStartedIndicator drops `.is-complete` (the ring reads empty and
// its glyph swaps back to the checklist icon) before the PUT has even landed.
// `dismissed` is cleared too — a soft "not now" from earlier in this page
// load must not swallow a checklist the caller just asked to restart.
//
// Shared by the checklist's own "Start over" button and the profile menu's
// "Start over onboarding" (the rail's only route back once the row is gone).
function restartJourney() {
  dismissed = false;
  patchJourney({
    onboarded: false, // re-arm the "Hi, I'm Agnes 👋" greeting too
    first_asked: false,
    stack_setup_done: false,
    explored_stack: false,
    catalog_discovered: false,
    use_anywhere: false,
    agent_created: false,
  });
}

// Wire the profile menu's "Start over onboarding" entry (rail chrome only; the
// topnav journey card keeps its own inline button). Called from both boot paths
// so the entry works on /chat and on every other rail page.
//
// Restarting from a menu needs feedback the checklist's own button doesn't: the
// caller is looking at the profile menu, not at the checklist. So close the menu
// and pin the onboarding popover open — the card they were told exists is then
// on screen, at 0/5, with the first step ready.
function wireRestartOnboardingMenuItem() {
  const btn = document.getElementById("rail-restart-onboarding");
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    restartJourney();

    const panel = document.getElementById("userMenuPanel");
    const trigger = document.getElementById("userMenuTrigger");
    if (panel) panel.setAttribute("hidden", "");
    if (trigger) trigger.setAttribute("aria-expanded", "false");

    const wrap = document.getElementById("railGetStarted");
    const gsToggle = document.getElementById("rail-getstarted-toggle");
    if (wrap) {
      // `.is-closed` is the suppression rail.css uses to beat its own
      // :hover/:focus-within reveal — it has to come off, or the panel we just
      // pinned stays invisible (see rail_history.js).
      wrap.classList.remove("is-closed");
      wrap.classList.add("is-open");
    }
    if (gsToggle) gsToggle.setAttribute("aria-expanded", "true");
    if (window.showToast) window.showToast("Onboarding restarted", { type: "success" });
  });
}

// Reflect journey progress on the rail's onboarding row (the "Set up Agnes"
// destination above the bottom nav that opens this checklist as a popover) —
// specifically its leading icon, a circular progress ring (`.rail-getstarted-
// ring`) that IS the row's icon on every rail page, not an admin-only stand-
// in. Every write is a no-op when its element is absent — on topnav the
// journey stays inline in the chat sidebar and none of these exist.
//
// The title names the job differently depending on where you are: "Set up
// Agnes" for someone who hasn't started (it has to say what the row is FOR),
// "Continue setup" once any step has landed (they know; the accurate word is
// "continue"). At 5/5 the title stops mattering: `.is-complete` retires the
// whole row (rail.css), which is the outcome "Skip onboarding" — it lands all
// five steps — promises in its toast. restartJourney() clears the steps and
// the row comes back.
const RING_R = 15;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_R;

// ── The product name is an operator setting ─────────────────────────────────
// Every string this module renders that NAMES the product has to come from
// `instance.brand_short`, not a literal: the rail renders its onboarding card
// branded server-side and this module then rewrites the same element, so a
// literal here would show the operator's brand for one frame and replace it
// with "Agnes" the moment /api/chat/journey resolves.
//
// The seam is a data attribute on `#railGetStarted` — the element this module
// already writes into, and the ancestor of the popover mount (`#chat-journey`),
// so one attribute covers every branded string below with nothing to keep in
// sync. Falls back to "Agnes", the same default the template declares, for a
// DOM without the rail.
//
// NOT yet branded, and out of this seam's reach: the step labels/`why` copy,
// the first-visit greeting and the sub-action labels in this file, plus the
// prose in chat.js / tour.js / chat_dashboard.js / identity.js / chats_page.js.
// Those are pre-existing literals rather than a server-rendered value being
// overwritten, and reaching them needs a window-level stamp (like
// `window._agTelegramBot` in base_ds.html) rather than this element.
function brandShort() {
  const el = document.getElementById("railGetStarted");
  return (el && el.dataset.brandShort) || "Agnes";
}

function updateGetStartedIndicator(done, total, complete) {
  const titleText = done > 0 ? "Continue setup" : `Set up ${brandShort()}`;
  const title = document.getElementById("rail-getstarted-title");
  if (title) title.textContent = titleText;

  // Written whole rather than as a bare "3/5": the row has room for the
  // sentence, and the element renders empty server-side so nothing states a
  // wrong number before the journey resolves.
  const countText = `${done} of ${total} steps complete`;
  const count = document.getElementById("rail-getstarted-count");
  if (count) count.textContent = countText;

  // The ring's arc, drawn as stroke-dasharray "filled remainder" so the
  // unfilled portion of the circle stays the track colour underneath.
  const pct = total ? Math.round((done / total) * 100) : 0;
  const ringFill = document.getElementById("rail-getstarted-ring-fill");
  if (ringFill) {
    const filled = (pct / 100) * RING_CIRCUMFERENCE;
    ringFill.style.strokeDasharray = `${filled} ${RING_CIRCUMFERENCE}`;
  }

  // The row's accessible name is this one sentence — a screen reader gets it
  // from `aria-label` regardless of whether the label beside the ring is
  // showing, and `title` gives a mouse user the same text as a tooltip
  // (useful in icon mode, where the label is hidden).
  const toggle = document.getElementById("rail-getstarted-toggle");
  if (toggle) {
    const sentence = `${titleText} — ${countText}`;
    toggle.setAttribute("aria-label", sentence);
    toggle.title = sentence;
  }

  const wrap = document.getElementById("railGetStarted");
  if (wrap) wrap.classList.toggle("is-complete", !!complete);
}

// ── Re-read progress on demand ──────────────────────────────────────────────
// Steps now complete from real activity, server-side (app/services/journey.py):
// put something in your stack on the Library page and the milestone lands
// without the checklist being touched. But this panel loaded its copy of the
// journey at page load, so opening it afterwards would show state from BEFORE
// the thing the user just did — a checklist that watches your work and then
// reports it one page-load late is barely better than one that doesn't watch.
//
// A plain DOM event rather than an export, so the rail chrome can ask for a
// refresh (it fires this when the popover opens) without importing this module
// — rail_history.js is a classic script and this is an ES module.
const REFRESH_EVENT = "agnes:journey-refresh";

async function refreshJourney() {
  if (!ready) return;
  await loadJourney();
  renderJourneyPanel();
}

function wireRefreshListener() {
  if (document.body.dataset.journeyRefreshWired) return;
  document.body.dataset.journeyRefreshWired = "1";
  document.addEventListener(REFRESH_EVENT, () => {
    refreshJourney();
  });
}

// ── Greeting ────────────────────────────────────────────────────────────────
function greetOnce(synced) {
  if (journey.onboarded) return;
  hooks.renderAssistant(
    "Hi, I'm **Agnes** `icon:hand` I'll answer using the company knowledge in your Stack, and I'll always say where an answer came from.",
  );
  if (synced === false) {
    hooks.renderAssistant(
      "Heads up — I can't reach Claude Code or Cursor until you connect a machine, but I can still answer you right here.",
    );
  }
  patchJourney({ onboarded: true });
}

// ── Newcomer spotlight ──────────────────────────────────────────────────────
// A first-timer lands on an empty chat, and an empty chat explains nothing: the
// composer is the entire product and reads like a search box. So the coach-mark
// tour fires by itself, exactly once, on that first visit — it spotlights the
// composer (the scrim is `pointer-events: none`, so they can type straight
// through the card), then Library, what "in stack" means, uploading, sharing.
//
// "Newcomer" is the whole journey being untouched, not just `first_asked`:
// someone who worked through the checklist by hand, or restarted onboarding
// after finishing it, has already seen these surfaces and must not be walked
// through them unasked. tour.js owns the other half of the gate (the
// localStorage `seen` flag), so taking the tour and later resetting the journey
// doesn't re-launch it either.
function isNewcomer() {
  return STEP_KEYS.every((k) => !journey[k]);
}

function tourModule() {
  return import(window._agTourUrl || "/static/js/tour.js");
}

function maybeAutoLaunchTour() {
  if (!chatMode) return; // the first step anchors on /chat's composer
  if (!isNewcomer()) return;
  // An admin whose instance is not set up yet is not the audience for this.
  // The welcome tour teaches the ANALYST product — "ask a question", "your
  // Library", "data packages your admin set up appear here on their own" —
  // to the person who has to be that admin, before any of it is true. It ran
  // over the top of a first-run instance and left them 2/6 through a
  // "setup" that never mentions connecting a source or granting anything.
  //
  // Deliberately narrow: this suppresses the tour for an admin with an
  // INCOMPLETE chain only. A non-admin still gets it on their first visit —
  // that is who it was written for — and so does an admin once the instance
  // is actually set up, because by then they are also just a user.
  if (window._agAdminSetupPending === true) return;
  // A deep link into an existing conversation is not a first look at the
  // product — and the empty-state composer the first step points at isn't even
  // the thing on screen.
  if (new URLSearchParams(window.location.search).get("session")) return;
  tourModule()
    .then((mod) => mod.autoLaunchTour(WELCOME_TOUR))
    .catch(() => {
      /* the tour is additive — never block the chat */
    });
}

// Take the coach-mark down the moment the user does what it asked for. It is
// NOT marked seen: the walkthrough stays available behind "↻" and the checklist
// steps, it just stops narrating over the answer they're now waiting for.
// Guarded on the scrim actually being in the DOM so the common case (no tour
// running) costs no module fetch.
function dismissTourIfShowing() {
  if (!document.querySelector(".tour-overlay")) return;
  tourModule()
    .then((mod) => mod.dismissActiveTour())
    .catch(() => {});
}

// Replay the guided tour on demand — the "↻" in the journey head relaunches
// Agnes's spotlight tour from step 0. The tour is cross-page (it stashes +
// navigates to /chat, then /library, itself), so this works from any page the
// panel is mounted on.
function replayTour() {
  tourModule()
    .then((mod) => mod.launchTour(WELCOME_TOUR, 0))
    .catch(() => {
      // Module load failed — fall back to just landing on the Library.
      window.location.href = "/library";
    });
}

// Dismiss the Journey panel — the "×" in the journey head. When the panel is
// rendered inside the rail's onboarding popover we simply close the popover
// (it reopens from the rail launcher). When it's inline (the /chat sidebar on
// topnav) we hide it for this page load; `dismissed` guards re-renders so a
// later patchJourney() doesn't pop it back. It's in-memory, so a reload brings
// the panel back — a soft "not now", not a permanent opt-out.
function dismissJourney() {
  const el = document.getElementById("chat-journey");
  const popover = el && el.closest(".rail-getstarted-panel");
  if (popover) {
    // Deliberately NOT `popover.hidden = true`: the panel's visibility is
    // fully CSS-driven (revealed by :hover / :focus-within / `.is-open`,
    // suppressed by `.is-closed`) and it carries no `hidden` attribute in the
    // template. Sticking `hidden` here was one-way — no reopen path clears
    // it, and pages that ship a `[hidden] { display: none !important; }`
    // reset (stack_card.css, marketplace.css) could never show the panel
    // again until a full reload (Devin Review on #1092).
    const wrap = document.getElementById("railGetStarted");
    if (wrap) {
      wrap.classList.remove("is-open");
      // `[hidden]` and removing `.is-open` both lose to rail.css's CSS-only
      // :hover / :focus-within reveal — the cursor is, by definition, over
      // the launcher at the exact moment "×" is clicked. `.is-closed`
      // (rail.css) is the only rule that overrides those with !important;
      // rail_history.js clears it again once the cursor actually leaves.
      wrap.classList.add("is-closed");
    }
    const toggle = document.getElementById("rail-getstarted-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    return;
  }
  dismissed = true;
  if (el) el.hidden = true;
}

// ── Long-run notification nudge ─────────────────────────────────────────────
// Notifications used to be reachable ONLY from /me/profile — which is where a
// user goes to look the setting up *after* they already know it exists. Nobody
// thinks "I should configure notifications" before a run has finished while they
// were in another tab, so discovery was effectively zero.
//
// So Agnes offers it herself, at the only moment its value is legible: a turn
// that's still running after LONG_RUN_MS. The offer is in-thread (same shape as
// the gap-resolver card above), links the channel inline, and never repeats —
// once per page load, and "Not now" is remembered.
//
// Deliberately NOT a checklist step; see the note on STEP_META.use_anywhere.
const LONG_RUN_MS = 45000;
const NUDGE_DISMISSED_KEY = "agnes.notify.nudge.dismissed";

let turnTimer = null;
// One offer per page load, whatever happens to it afterwards — a user who is
// mid-thought about their data should not be asked twice about Telegram.
let nudgeOffered = false;
// True between noteTurnStarted and the turn's terminal frame. Read only to pick
// the offer's wording when it was deferred past the end of the run.
let turnLive = false;
// A deferred offer is already waiting on the tab to come back — don't stack a
// second visibilitychange listener behind a later turn.
let nudgePending = false;

function nudgeDismissed() {
  try {
    return localStorage.getItem(NUDGE_DISMISSED_KEY) === "1";
  } catch (_) {
    // Private mode / storage disabled — fall back to the per-page-load guard
    // above. Being asked again next visit beats throwing here.
    return false;
  }
}

function rememberNudgeDismissed() {
  try {
    localStorage.setItem(NUDGE_DISMISSED_KEY, "1");
  } catch (_) {
    /* soft state — the in-memory guard still holds for this page load */
  }
}

// Fire the offer, but only while the user is actually looking. A card rendered
// into a hidden tab would be scrolled past unread when they return — and the
// whole point is the moment of recognition ("right, I did walk away"). So when
// the timer comes due on a backgrounded tab we wait for the tab to come back,
// which is also exactly when the offer is most persuasive.
//
// The wait deliberately OUTLIVES the turn: someone who walked away and came back
// to a finished run is the single best audience for this offer. It just needs
// the right tense, which is what `turnLive` at render time decides.
function scheduleNudge() {
  if (document.hidden) {
    if (nudgePending) return;
    nudgePending = true;
    const onVisible = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", onVisible);
      nudgePending = false;
      showNotifyNudge();
    };
    document.addEventListener("visibilitychange", onVisible);
    return;
  }
  showNotifyNudge();
}

async function showNotifyNudge() {
  if (nudgeOffered) return;
  // Read the tense BEFORE the await — loadNotifyState can span the very frame
  // that ends the turn, and "this is taking a while" about a finished run is
  // worse than either correct wording.
  const stillRunning = turnLive;
  // Re-read the link state at offer time rather than trusting the page-load
  // snapshot: the user may have linked in another tab since boot.
  await loadNotifyState();
  if (notifyLinked === true) return;
  if (!hooks.renderAssistant || !hooks.appendNode) return;
  nudgeOffered = true;

  const bot = telegramBot();
  hooks.renderAssistant(
    stillRunning
      ? "This one's taking a while. I can ping you on **Telegram** the moment it's done — then you're free to close the tab."
      : "That finished while you were away. I can ping you on **Telegram** next time, so you don't have to keep the tab open.",
  );

  const card = document.createElement("div");
  card.className = "msg msg-assistant cloud-chat-notifycard-wrap";
  card.innerHTML = `
    <div class="msg-avatar" aria-hidden="true">A</div>
    <div class="msg-bubble">
      <div class="cloud-chat-notifycard">
        <div class="cloud-chat-notifycard-label">Get notified</div>
        <div class="cloud-chat-notifycard-foot">
          <button type="button" class="btn btn-primary btn-sm" data-notify-link>Ping me on Telegram</button>
          <button type="button" class="btn btn-secondary btn-sm" data-notify-dismiss>Not now</button>
        </div>
        <div class="cloud-chat-notifycard-steps" hidden>
          <ol>
            <li>Message <code>/start</code> to <strong>@${escapeHtml(bot)}</strong> on Telegram</li>
            <li>Enter the 6-digit code it replies with</li>
          </ol>
          <div class="cloud-chat-notifycard-verify">
            <input type="text" data-notify-code placeholder="6-digit code" maxlength="6"
              inputmode="numeric" autocomplete="one-time-code" aria-label="Telegram verification code">
            <button type="button" class="btn btn-primary btn-sm" data-notify-verify>Verify</button>
          </div>
          <div class="cloud-chat-notifycard-err" role="alert" hidden></div>
        </div>
        <div class="cloud-chat-notifycard-hint">Change this any time in <a href="/me/profile#notifications">your profile</a>.</div>
      </div>
    </div>`;
  hooks.appendNode(card);
  if (hooks.scrollToBottom) hooks.scrollToBottom();

  const steps = card.querySelector(".cloud-chat-notifycard-steps");
  const errEl = card.querySelector(".cloud-chat-notifycard-err");
  const foot = card.querySelector(".cloud-chat-notifycard-foot");

  const settle = (message) => {
    foot.remove();
    if (steps) steps.remove();
    const done = document.createElement("p");
    done.className = "cloud-chat-notifycard-settled";
    done.textContent = message;
    card.querySelector(".cloud-chat-notifycard-label").after(done);
  };

  card.querySelector("[data-notify-link]").addEventListener("click", () => {
    steps.hidden = false;
    card.querySelector("[data-notify-code]").focus();
    if (hooks.scrollToBottom) hooks.scrollToBottom();
  });

  card.querySelector("[data-notify-dismiss]").addEventListener("click", () => {
    rememberNudgeDismissed();
    // Leave a trace instead of vanishing: the user answered a question Agnes
    // asked, and the thread should still read like a conversation later.
    settle("No problem — you can switch this on later in your profile.");
  });

  const verifyBtn = card.querySelector("[data-notify-verify]");
  // Enter submits — the field is a one-time-code input, and it sits in a chat
  // thread whose own composer submits on Enter, so doing nothing here reads as
  // broken. Not a <form> (this card lives inside the message list).
  card.querySelector("[data-notify-code]").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (!verifyBtn.disabled) verifyBtn.click();
  });
  verifyBtn.addEventListener("click", async () => {
    const input = card.querySelector("[data-notify-code]");
    const code = (input.value || "").trim();
    errEl.hidden = true;
    if (!code) {
      errEl.textContent = "Enter the 6-digit code Telegram sent you.";
      errEl.hidden = false;
      return;
    }
    verifyBtn.disabled = true;
    verifyBtn.textContent = "Verifying…";
    try {
      await apiJson("/api/telegram/verify", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
    } catch (_) {
      // The endpoint's only failure for a well-formed request is an invalid or
      // expired code (400) — say that rather than surfacing a status number.
      verifyBtn.disabled = false;
      verifyBtn.textContent = "Verify";
      errEl.textContent = "That code didn't work — it may have expired. Send /start again for a fresh one.";
      errEl.hidden = false;
      return;
    }
    notifyLinked = true;
    // The checklist's sub-action reads the same flag — repaint it so both
    // surfaces agree immediately.
    renderJourneyPanel();
    settle("Telegram connected ✓ — I'll message you when this finishes.");
  });
}

// ── Gap resolver ─────────────────────────────────────────────────────────────
// Lightweight intent reasons, phrased against common question shapes. Real
// package ids vary per instance, so we attach a reason by resource TYPE rather
// than by a hard-coded id (the prototype's fake ids don't exist here).
function reasonFor(item) {
  if (item.resource_type === "memory_domain")
    return "domain rules and definitions to ground my answers";
  return "the data I'd query to answer questions like this";
}

// Show the in-chat gap-resolver card. Returns true if it took over the turn
// (caller must NOT send the message to the model yet — the card's "Add &
// continue" button resubmits once the Stack is ready).
async function maybeShowGapResolver(text) {
  if (journey.stack_setup_done) return false;
  let items;
  try {
    items = await browseStack();
  } catch (_) {
    return false;
  }
  // Only intercept when the Stack is genuinely empty AND there is something to
  // add. A user who already has knowledge in their Stack goes straight to the
  // model.
  if (inStackCount(items) > 0) return false;
  const candidates = items.filter((i) => !i.in_stack);
  if (!candidates.length) return false;

  const rec = candidates.slice(0, 4);
  hooks.renderAssistant(
    "I can't answer that yet — your Stack is empty. But I know what I'd need. Here's what I'd add so I can help with this and questions like it:",
  );

  const card = document.createElement("div");
  card.className = "msg msg-assistant cloud-chat-gapcard-wrap";
  card.innerHTML = `
    <div class="msg-avatar" aria-hidden="true">A</div>
    <div class="msg-bubble">
      <div class="cloud-chat-gapcard">
        <div class="cloud-chat-gapcard-label">Recommended for your question</div>
        <div class="cloud-chat-gapcard-list">
          ${rec
            .map(
              (c) => `<label class="cloud-chat-gapcard-opt">
            <input type="checkbox" checked data-gap-id="${escapeAttr(c.id)}" data-gap-type="${escapeAttr(c.resource_type)}">
            <span><b>${escapeHtml(c.name || c.id)}</b> <span class="cloud-chat-gapcard-why">— ${escapeHtml(reasonFor(c))}</span>
            ${c.description ? `<br><span class="cloud-chat-gapcard-desc">${escapeHtml(c.description)}</span>` : ""}</span>
          </label>`,
            )
            .join("")}
        </div>
        <div class="cloud-chat-gapcard-foot">
          <button type="button" class="btn btn-primary btn-sm cloud-chat-gapcard-cta">Add &amp; answer my question</button>
        </div>
        <div class="cloud-chat-gapcard-hint">Prefer to type? Just tell me — e.g. <code>add ${escapeHtml(rec[0].name || rec[0].id)}</code>.</div>
      </div>
    </div>`;
  hooks.appendNode(card);
  hooks.scrollToBottom();

  const cta = card.querySelector(".cloud-chat-gapcard-cta");
  cta.addEventListener("click", async () => {
    const picks = [...card.querySelectorAll("input[data-gap-id]:checked")].map((cb) => ({
      id: cb.getAttribute("data-gap-id"),
      type: cb.getAttribute("data-gap-type"),
    }));
    if (!picks.length) return;
    cta.disabled = true;
    cta.textContent = "Adding…";
    let added = 0;
    for (const p of picks) {
      try {
        await subscribe(p.type, p.id);
        added += 1;
      } catch (_) {
        /* skip failures (e.g. lost grant) */
      }
    }
    await patchJourney({ stack_setup_done: true });
    hooks.renderAssistant(
      `Done — added ${added} package${added === 1 ? "" : "s"} to your Stack. Now let me answer what you actually asked:`,
    );
    hooks.resubmit(text);
  });

  return true;
}

// ── "add <thing>" parity ─────────────────────────────────────────────────────
// Typing "add X" in chat does what clicking Add in the Catalog does. Returns
// true if it handled the message.
//
// This runs BEFORE the model sees the message, so anything it claims is
// swallowed whole. Two guards keep that power proportionate:
//
//   * only a short, single-clause imperative counts as a command. A message
//     that merely opens with the verb is prose — "Install instructions for the
//     CLI, please", "Add the sales package, then explain churn" — and taking
//     it here silently discarded everything after the first clause.
//   * the match must be confident before we mutate the Stack. `matchScore`
//     awards 12 points per incidental word found anywhere in an item's name,
//     id or *description*, so a long sentence could clear `> 0` on one shared
//     word and subscribe the user to something they never named.
//
// Anything that fails either guard falls through to the model, which can ask
// what was meant. Bailing out is always safe here; guessing is not.
const ADD_COMMAND_MAX_WORDS = 8;
const ADD_COMMAND_MIN_SCORE = 60; // exact name (100) or substring hit (60)

async function maybeHandleAddCommand(text) {
  const m = text.trim().match(/^(?:add|enable|install)\s+(.+)$/i);
  if (!m) return false;
  // A trailing sentence terminator ("add sales-package.") is punctuation,
  // not part of what to add — strip it once so every downstream
  // normalization agrees on the same subject. Previously only the
  // word-count check was normalized this way; the search query was still
  // built from the raw capture, so the dot rode along into `q` and broke
  // the exact-name match, the substring match, and the per-token fallback
  // alike — the identical message without the trailing "." resolved fine.
  const subject = m[1].trim().replace(/[.!?]+$/, "");
  // Sentence break or newline => prose, not a command.
  if (/[.!?;:]\s|\n/.test(subject)) return false;
  const words = subject.split(/\s+/).filter(Boolean);
  if (words.length > ADD_COMMAND_MAX_WORDS) return false;
  const query = subject
    .replace(/\b(the|a|an|package|data|memory|to|my|stack|please)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  let items;
  try {
    items = await browseStack();
  } catch (_) {
    return false;
  }
  const q = (query || subject).toLowerCase();
  const scored = items
    .filter((i) => !i.in_stack)
    .map((i) => ({ i, s: matchScore(q, i) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s);
  // No match, or only a weak bag-of-words match, or several equally weak
  // candidates: hand the turn to the model rather than dead-ending on a
  // "couldn't find X" that quotes the user's own sentence back at them.
  // The bar applies uniformly regardless of how many candidates matched —
  // a lone addable item on the instance is not itself a reason to trust a
  // weak match; it just means there's nothing else it *could* be.
  if (!scored.length) return false;
  const confident = scored[0].s >= ADD_COMMAND_MIN_SCORE;
  if (!confident) return false;
  const target = scored[0].i;
  try {
    await subscribe(target.resource_type, target.id);
  } catch (_) {
    hooks.renderAssistant(
      `I couldn't add **${escapeHtml(target.name || target.id)}** — you may not have access. Ask your admin, or pick another from the [Catalog](/catalog).`,
    );
    return true;
  }
  await patchJourney({ stack_setup_done: true });
  hooks.renderAssistant(
    // /library?stack=in_stack, not /stack — the Stack page is retired
    // (#1088); the Library's "In stack only" toggle arrives pressed on
    // that deep link and reads the same StackResolver.browse() rows.
    `Added **${escapeHtml(target.name || target.id)}** to your Stack ✓ — it's live now, and visible under [My Stack](/library?stack=in_stack).`,
  );
  return true;
}

function matchScore(needle, item) {
  const hay = `${item.name || ""} ${item.id || ""} ${item.description || ""}`.toLowerCase();
  const n = needle.toLowerCase().trim();
  if (!n) return 0;
  if ((item.name || "").toLowerCase() === n) return 100;
  // The substring tier carries the same "too short to mean anything" bar the
  // per-token tier below already applies. Without it, "install it" reduces to
  // the needle "it", which occurs inside almost any name/id/description, scores
  // the full 60, and clears the confidence bar — so an item nobody named gets
  // added with no confirmation. Exact-name equality above is left unguarded on
  // purpose: an item genuinely called "ai" is a real match, and equality cannot
  // happen by accident the way containment can.
  if (n.length > 2 && hay.includes(n)) return 60;
  let s = 0;
  n.split(/\s+/)
    .filter((t) => t.length > 2)
    .forEach((t) => {
      if (hay.includes(t)) s += 12;
    });
  // Bag-of-words overlap ranks candidates but must never *be* confidence:
  // five matching tokens reach 60, which is exactly ADD_COMMAND_MIN_SCORE, so
  // an ordinary five-word sentence whose words happen to appear in a
  // description would have counted as naming that item. Capped below the bar
  // so only the exact-name (100) and substring (60) tiers can clear it.
  return Math.min(s, ADD_COMMAND_MIN_SCORE - 1);
}

// ── escaping ─────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}
function escapeAttr(s) {
  return escapeHtml(s);
}

// ── public API ───────────────────────────────────────────────────────────────
export async function initChatOnboarding(h) {
  hooks = h;
  chatMode = true;
  // Before the await: the profile menu's "Start over onboarding" must work from
  // the moment the page is interactive, not only once /api/chat/journey answers.
  wireRestartOnboardingMenuItem();
  wireRefreshListener();
  await loadJourney();
  await loadNotifyState();
  ready = true;
  renderJourneyPanel();
  // Last, and only for someone who has never done any of this: spotlight the
  // composer they just landed on and walk them through the rest.
  maybeAutoLaunchTour();
}

// Standalone panel for rail pages OTHER than /chat: renders the navigable step
// tracker into #chat-journey (inside the rail's onboarding popover) and
// keeps the launcher's progress count in sync, without the chat hooks. The
// href steps (Explore My Stack / Discover more / Use anywhere) navigate as
// usual; the chat-only steps show their state but aren't actionable here, and
// the "?" replay is omitted (chatMode stays false). Safe to call when there is
// no #chat-journey (no-op via renderJourneyPanel's guard).
export async function mountJourneyPanel() {
  wireRestartOnboardingMenuItem();
  wireRefreshListener();
  if (ready) {
    // Already initialized (e.g. chat.js ran first) — just repaint.
    renderJourneyPanel();
    return;
  }
  hooks = {};
  chatMode = false;
  await loadJourney();
  await loadNotifyState();
  ready = true;
  renderJourneyPanel();
}

// Called from submitUserMessage right after the user bubble is rendered and
// before the message is sent to the model. Returns true when onboarding has
// taken over the turn (the model send must be skipped).
export async function onUserMessage(text, { synced } = {}) {
  if (!ready) return false;
  greetOnce(synced);
  if (!journey.first_asked) await patchJourney({ first_asked: true });

  // Parity command takes precedence — "add X" is an action, not a question.
  if (await maybeHandleAddCommand(text)) return true;

  return maybeShowGapResolver(text);
}

// Called by chat.js the moment a submit is accepted — before the socket, the
// runner, or anything that can fail or take five seconds. The user did the
// thing the coach-mark asked for; it has to be gone by the time they look back
// at the composer, whether or not the turn ends up going anywhere.
export function noteComposerSubmitted() {
  dismissTourIfShowing();
}

// Let chat.js report a successful answer so we can advance the counter.
export function noteAnswered() {
  if (!ready) return;
  patchJourney({ successful_answers: (journey.successful_answers || 0) + 1 });
}

// ── Turn lifecycle (drives the long-run notification nudge) ──────────────────
// chat.js calls these around a model turn: `started` right before the message
// goes to the runner, `ended` on every terminal frame (done / error / cancelled).
// The only thing riding on them is the LONG_RUN_MS timer, so both are cheap
// no-ops whenever there's nothing to offer.
export function noteTurnStarted() {
  noteTurnEnded(); // a resubmit mid-turn must not leave two timers armed
  if (!chatMode) return; // no thread to render an offer into
  turnLive = true;
  if (nudgeOffered || nudgeDismissed()) return;
  if (!telegramBot()) return; // channel not configured on this instance
  if (notifyLinked === true) return; // already reachable — nothing to ask for
  turnTimer = setTimeout(scheduleNudge, LONG_RUN_MS);
}

export function noteTurnEnded() {
  turnLive = false;
  // Only the un-fired timer is cancelled. An offer already deferred behind a
  // hidden tab (nudgePending) survives on purpose — see scheduleNudge.
  if (turnTimer === null) return;
  clearTimeout(turnTimer);
  turnTimer = null;
}
