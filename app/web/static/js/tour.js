/**
 * tour.js — Config-driven, non-blocking coach-mark engine.
 *
 * Cross-page design: The real app is multi-page (not a SPA). When a step's
 * `page` differs from the current pathname, the engine stashes
 * {id, index} in sessionStorage under 'agnes.tour.pending' and navigates.
 * On load, every page checks for a pending tour whose step[index].page
 * matches window.location.pathname and resumes automatically.
 *
 * State:
 *   localStorage  'agnes.tour.<id>.seen'  — don't auto-launch a seen tour
 *   sessionStorage 'agnes.tour.pending'   — cross-page resume
 *                                           {id, index, ts, skipped?}
 *
 * Usage:
 *   import { launchTour, TOURS } from './tour.js';
 *   launchTour('welcome');        // start from step 0
 *   launchTour('welcome', 2);     // resume at index 2
 *   autoLaunchTour('welcome');    // first visit only — no-op once seen
 *
 * Every step carries a stable `key`, and `tourIndexOf(id, key)` resolves it to
 * an index — that is how the onboarding checklist opens the coach-mark that
 * teaches ITS step instead of restarting the walkthrough from the composer.
 * Addressing steps by name rather than by number is the whole point: a number
 * in chat_onboarding.js would silently point at the wrong card the first time
 * a step is inserted here.
 */

// ── Tour definitions ────────────────────────────────────────────────────────

export const TOURS = {
  // The newcomer walkthrough. It follows the path a first-time user actually
  // takes rather than the old surface-by-surface tour of /stack + /catalog
  // (both retired from the rail): they land on a fresh chat, so the FIRST
  // thing shown is the composer — asking is the product — and only then the
  // Library, what "in stack" means, and how to add and share their own things.
  welcome: [
    // Step 0: /chat — the composer. The scrim is pointer-events:none, so the
    // spotlighted input stays fully typeable while this step is up; that's the
    // point, and chat_onboarding.js ends the tour the moment they send.
    {
      key: 'ask',
      page: '/chat',
      // Rail wraps the textarea in the `.cloud-chat-composer` pill (the whole
      // bar is the affordance); topnav has no wrapper, so fall through to the
      // textarea itself. A comma list is a FALLBACK CHAIN, tried left to right
      // (see _visibleMatch) — not document order.
      selector: '.cloud-chat-composer, #chat-input',
      title: 'Just ask — that\'s the whole thing',
      // Kept deliberately short: this card has to fit ABOVE the composer at a
      // laptop viewport height, or the positioner has to cap and scroll it.
      desc: 'Ask a real work question in plain English — go ahead, type right now, this card won\'t get in the way. I answer from the company knowledge you can see, and always show where the answer came from.',
      points: [
        'Try "what changed in revenue last month?"',
        'Type / to run a skill, or + to attach a file.',
      ],
    },
    // Step 1: /chat — the Library nav item, introduced from the chat page so
    // the concept lands before we navigate anywhere.
    {
      key: 'library',
      page: '/chat',
      // #nav-artefacts is the rail's Library row (the id predates the
      // /artefacts → /library rename); topnav labels its link with data-tour.
      selector: '#nav-artefacts, [data-tour="nav-library"]',
      title: 'Everything I can read lives in your Library',
      desc: 'Knowledge your organization shares with you, plus every file, skill and agent you add yourself — one place, all searchable.',
      points: [
        'Data packages and memory your admin set up appear here on their own.',
        'Anything you add is private to you until you choose to share it.',
      ],
    },
    // Step 2: /library — the "in stack" concept, the one idea that decides
    // whether an answer can use a thing.
    //
    // Anchored on a ROW'S OWN "Add" control, with the "In stack only" filter
    // ringed alongside it (`extraSpotlight`). Getting to that took two fixes:
    //
    //   1. The filter used to lead the selector chain, which made this the one
    //      step pointing at the wrong KIND of control — the toggle FILTERS the
    //      list, so the row whose whole job is "put something in your stack"
    //      spotlighted a filter and left the real action to be found.
    //   2. Leading with `[data-add-to-stack]` alone wasn't enough either: the
    //      Library ships every group COLLAPSED, so on a first visit that control
    //      is in the DOM with a zero-size box and resolution fell straight
    //      through to the filter anyway. `reveal` opens a group that has one
    //      (data packages and memory first — the knowledge a newcomer is meant
    //      to put in their stack — then files and skills).
    //
    // Both controls carry the ring because the step teaches both halves: what
    // "in stack" MEANS (the filter names the set) and how something joins it
    // (the Add button). The popover anchors on the Add control, since that is
    // the one the copy asks you to click.
    {
      key: 'in-stack',
      page: '/library',
      selector: '[data-add-to-stack]',
      // Every Library group ships collapsed, so the Add controls are all in the
      // DOM with a zero-size box; this opens the one group that has one.
      revealHost: { container: '[data-lib-sec]', toggle: '[data-sec-toggle]' },
      // Only if no group turns out to have an Add control at all — a Library
      // where everything is already in-stack has nothing left to add, and the
      // "In stack only" toggle is then the honest thing to point at.
      fallbackSelector: '#lib-stack-toggle, [data-stack-badge]',
      extraSpotlight: ['#lib-stack-toggle'],
      title: 'Put it in your stack and I can use it',
      desc: 'Your Library is everything available to you. Your stack is the smaller set your chat agent actually draws on — that\'s what keeps answers fast and on topic. Click Add on a row and it joins your stack.',
      points: [
        'It counts from your very next question — no reload, no waiting.',
        'The "In stack only" button above filters the list down to what I\'m using right now.',
        'Items your admin marked required are always on and can\'t be removed.',
      ],
    },
    // Step 3: /library — bringing your own knowledge in. Spotlights the
    // "+ Add" button, which is the control the reader has to find and the one
    // the copy names.
    //
    // It briefly opened the menu and ringed THAT instead, so the four routes
    // were on screen while being described. Two things were wrong with it: the
    // ring landed on a popover rather than on the button you press to get it, so
    // the step no longer showed you where to start; and with the card positioned
    // off the button, the open menu and the card compete for the same space
    // below it. The button is the anchor; the menu is what happens when they
    // click it.
    {
      key: 'add',
      page: '/library',
      selector: '#lib-new-btn',
      title: 'Bring your own knowledge in',
      desc: 'Click + Add to upload a file only you have — a pricing deck, a spec, meeting notes — and I\'ll search it and cite it in my answers. No admin needed. The same menu is where you build a skill, a plugin or an agent template.',
      points: [
        'PDF, Markdown, CSV, Excel, and more.',
        'It lands in your Library, and you decide whether it goes in your stack.',
      ],
    },
    // Step 4: /library — sharing. The badge lives on a ROW, and the Library
    // ships every group collapsed on a first visit, so the step opens whichever
    // group holds a sharing badge (`revealHost`). Skipped when no group has one
    // — an empty Library has nothing to share yet, and nothing to point at.
    {
      key: 'share',
      page: '/library',
      selector: '[data-share], [data-share-info]',
      revealHost: { container: '[data-lib-sec]', toggle: '[data-sec-toggle]' },
      title: 'Share what turns out to be useful',
      desc: 'Every row shows who can see it. Click the sharing badge to open something up to a group — or your whole workspace — so colleagues, and their agents, can use it too.',
      points: [
        'Shared items show up in your teammates\' Library, ready for their stack.',
      ],
    },
    // Step 5: final centered step — no anchor
    {
      key: 'done',
      page: '/library',
      centered: true,
      finalChoice: true,
      title: 'That\'s the tour',
      desc: 'Ask questions, add what\'s missing, share what works. Want the same company knowledge in Claude Code, Cursor, and VS Code? Use Connect my AI tools below.',
      points: [],
    },
  ],

  // Agents — a SINGLE card, shown the first time someone lands on /agents,
  // not a step of the walkthrough.
  //
  // The concept had no explanation anywhere: Agents sits directly under Library
  // in the rail, so a first-timer met an unexplained robot icon, and "agent" is
  // used two ways in this product (the chat you are already talking to, and a
  // named assistant you define) — leaving that unsaid isn't neutral, it reads as
  // a duplicate of chat. It was briefly a sixth step of `welcome`, which was the
  // wrong shape: it dragged everyone off /library onto a page they hadn't asked
  // for, mid-walkthrough, to explain a surface most readers don't need on day
  // one. Teaching a destination is better done ON arrival, when the reader chose
  // to be there and the thing being explained is what they are looking at.
  //
  // One step → the popover renders in its solo form (no dots, no "I'll explore
  // on my own", primary reads "Got it"), and autoLaunchTour's localStorage gate
  // means it appears exactly once.
  //
  // `awaitAnchor`: the list view is client-rendered after /api/agents resolves,
  // so `[data-ag-new]` is genuinely absent for the first frames after load.
  // Without the wait the step resolves to nothing and is silently dropped — the
  // failure mode that lets a coach-mark go unnoticed rather than visibly broken.
  // Deliberately NO fallback selector: a fallback present on the first frame
  // (the page head) would always win the race and the wait would never happen.
  agents: [
    {
      key: 'agents',
      page: '/agents',
      selector: '[data-ag-new]',
      awaitAnchor: true,
      // Kept SHORT on purpose. Its anchor sits mid-page, so the positioner has
      // roughly 250px of room above it and caps the card to that — anything
      // longer scrolls its own tail out of sight, which for a card the reader
      // gets exactly once means the explanation simply doesn't arrive.
      title: 'Agents are assistants you define',
      desc: 'The chat in the sidebar reaches everything in your stack. An agent is narrower: one role, only the knowledge it should see, only the skills it may use.',
      points: [
        'Never more than your own permissions.',
      ],
    },
  ],

  // The "use Agnes outside this tab" micro-tour — one card, launched by the
  // checklist step of the same name. It exists because that step used to be a
  // bare link: it dropped the reader at the top of a long orientation page with
  // no indication of which of its eight sections was the one they had just
  // asked about. A single coach-mark on the section, with the two-step shape of
  // the job spelled out, is the difference between "navigated" and "guided".
  //
  // Anchored on the fold's own summary (always visible) rather than the
  // connector-URL box inside it: the page opens the fold itself on a #connect
  // arrival, and a tour that clicked it too would race that script and close
  // what it had just opened.
  connect: [
    {
      key: 'connect',
      page: '/how-it-works#connect',
      selector: '#connect summary, #connect',
      // Same length budget as the agents card: an anchor sitting mid-viewport
      // leaves the positioner roughly 270px, and the body scrolls its tail out
      // of sight beyond that. Title plus about four lines.
      title: 'Take this knowledge into your own tools',
      desc: 'Agnes is not only this tab. Point Claude Code, Cursor or VS Code at your connector URL and they answer from the same governed knowledge, under your own permissions.',
      // No bullets: the section this card rings opens with "Two steps: copy your
      // connector URL, then follow the instructions for your tool" — a bullet
      // repeating it would cost the one line that made the card overflow.
      points: [],
    },
  ],

  // Single-step coach-mark for authors arriving from the Marketplace's
  // "Submit a skill or plugin" CTA (/skills?spotlight=new-skill). One step →
  // the popover renders in its solo form (no dots, no "I'll explore on my
  // own", primary reads "Got it").
  'skill-builder': [
    {
      page: '/skills',
      // Anchors on the TYPE cards, which are the first thing on the page and
      // the first decision to make. It briefly anchored on the name field
      // instead; that broke when type became step 1, because the name field
      // does not exist in the DOM until a type is chosen — the coach-mark had
      // nothing to point at. The cards are always present, including on the
      // ?type= deep link where step 1 lands collapsed.
      selector: '[data-sk-type], [data-sk-change]',
      title: 'Start here',
      desc: 'Pick what you are building — a skill, a plugin, or an agent template. The rest of the form follows from that choice.',
      points: [
        'Choose who can use it: just you, or everyone in the organization.',
        'Save to Library and it appears in your Library, ready to share.',
      ],
    },
  ],
};

// ── Persistence helpers ─────────────────────────────────────────────────────

const PENDING_KEY = 'agnes.tour.pending';
// Only auto-resume a pending record written within this window. A cross-page
// tour hop stashes then navigates and the destination loads within seconds; a
// stale record (the user abandoned the tour via a normal link) must not re-pop
// the tour on later /chat or /library visits.
const RESUME_FRESH_MS = 12000;

function seenKey(id) {
  return `agnes.tour.${id}.seen`;
}

function isSeen(id) {
  try {
    return !!localStorage.getItem(seenKey(id));
  } catch (_) {
    return false;
  }
}

function markSeen(id) {
  try {
    localStorage.setItem(seenKey(id), '1');
  } catch (_) { /* storage unavailable — non-fatal */ }
}

function stashPending(id, index, skipped) {
  try {
    // `skipped` rides along so steps dropped for a missing anchor stay dropped
    // across the tour's cross-page hops — otherwise the progress dots would
    // re-count them after every navigation.
    const rec = { id, index, ts: Date.now() };
    if (skipped && skipped.size) rec.skipped = Array.from(skipped);
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(rec));
  } catch (_) { /* storage unavailable — non-fatal */ }
}

function clearPending() {
  try {
    sessionStorage.removeItem(PENDING_KEY);
  } catch (_) {}
}

function getPending() {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

// ── Journey patch helper (fire-and-forget) ──────────────────────────────────
// Mark ONLY the journey steps the tour genuinely walks the user through.
//
// That is exactly ONE of them: `explored_stack` ("Explore your Library"), which
// the tour completes because walking you through the Library is literally what
// it does. Every other step is something the user has to DO, and ticking a
// to-do because someone read a card about it is how a checklist starts lying:
//
//   • `first_asked` completes when a question is actually asked;
//   • `stack_setup_done` when a package is actually subscribed — and it gates
//     the in-chat gap resolver, so asserting it here would permanently suppress
//     the "your Stack is empty, here's what I'd add" card for anyone who takes
//     the tour before asking their first question;
//   • `catalog_discovered` now reads "Add or share something", an action; the
//     tour only shows you where the controls are. It USED to mean "saw the
//     Catalog", which the old tour did walk — asserting it under the new label
//     left the checklist ticking a step the user had not done, two rows below
//     one they had (see STEP_KEYS in chat_onboarding.js for the ordering half
//     of that same bug);
//   • `use_anywhere`: the final step OFFERS "Connect my AI tools" but finishing
//     the tour is not the same as having connected one. The button itself marks
//     it — see markUseAnywhereDone.
async function markTourStepsDone() {
  try {
    await fetch('/api/chat/journey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ explored_stack: true }),
    });
  } catch (_) { /* fire-and-forget — onboarding is soft state */ }
}

// Mark the "Use Agnes from other AI tools" step — fired when the final step's
// "Connect my AI tools" button navigates to the connect section.
async function markUseAnywhereDone() {
  try {
    // keepalive: the one call site fires this immediately before a full-page
    // navigation (`window.location.href = '/how-it-works#connect'`); without it
    // the browser cancels the in-flight PUT and the journey step stays
    // unmarked (Devin Review on #1092). keepalive lets the write survive the
    // page teardown while keeping the call fire-and-forget.
    await fetch('/api/chat/journey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_anywhere: true }),
      keepalive: true,
    });
  } catch (_) { /* fire-and-forget — onboarding is soft state */ }
}

// ── Engine ──────────────────────────────────────────────────────────────────

let _active = null; // { id, steps, index, overlay, popover, spotlight }

/** Public: launch a named tour at the given step index (default 0). */
export function launchTour(id, index = 0) {
  const steps = TOURS[id];
  if (!steps || !steps.length) return;

  const step = steps[index];
  if (!step) return;

  // If this step lives on a different page, stash + navigate.
  const currentPath = window.location.pathname;
  if (!pathMatches(step.page, currentPath)) {
    stashPending(id, index);
    window.location.href = step.page;
    return;
  }

  // Same page — render.
  _startTour(id, steps, index);
}

/**
 * Public: launch a tour ONCE, unattended — the newcomer path. Every guard here
 * exists because this fires without anyone asking for it:
 *   • already seen → never again (the journey panel's "↻" is the way back);
 *   • a tour already on screen, or a pending cross-page resume about to put one
 *     there → don't stack a second engine over it;
 *   • unknown id → no-op.
 * The caller decides WHETHER this user is a newcomer (chat_onboarding.js reads
 * the journey for that); this only decides whether the tour has run before.
 */
export function autoLaunchTour(id) {
  if (!TOURS[id] || isSeen(id) || _active || _pendingResumesHere()) return false;
  launchTour(id, 0);
  return true;
}

// Is there a cross-page resume about to put a tour on THIS page? That is the
// only pending record an unattended launch has to yield to.
//
// It used to be `getPending()` — any record at all — which quietly suppressed
// unrelated coach-marks: abandoning the welcome tour on /library leaves a record
// naming a /library step, and for the next 12 seconds that record blocked the
// Agents card on /agents, where nothing was going to resume. The card simply
// never appeared, with nothing to see or debug. Same freshness + page test
// resumePendingTour itself applies, so the two can't disagree about what is
// pending.
function _pendingResumesHere() {
  const pending = getPending();
  if (!pending || !pending.ts || Date.now() - pending.ts > RESUME_FRESH_MS) return false;
  const step = (TOURS[pending.id] || [])[pending.index];
  return !!step && pathMatches(step.page, window.location.pathname);
}

/**
 * Public: take the tour off screen without marking it seen. Called when the
 * user does the thing the tour was pointing at — sending their first message —
 * so the coach-mark gets out of the way of the answer instead of narrating
 * over it, and the rest of the walkthrough is still there to replay.
 */
export function dismissActiveTour() {
  if (!_active) return false;
  _endTour(false);
  return true;
}

/** Public: resolve a step's stable `key` to its index in a tour.
 *
 * Callers outside this module address steps by name — `tourIndexOf('welcome',
 * 'in-stack')` — so inserting a step here can't silently repoint them at the
 * wrong card. Unknown id/key falls back to 0: starting the walkthrough from the
 * beginning is a worse experience than the intended step, but a working one.
 */
export function tourIndexOf(id, key) {
  const steps = TOURS[id];
  if (!steps) return 0;
  const i = steps.findIndex((s) => s.key === key);
  return i < 0 ? 0 : i;
}

function pathMatches(page, path) {
  // Normalize both: drop hash + query, strip trailing slash, compare pathname.
  // A step's `page` may carry a hash (`/how-it-works#connect`) because the
  // destination page keys real behaviour off it — the connect fold opens itself
  // on arrival. Navigation uses the value verbatim so that hash survives; only
  // the COMPARISON strips it. Comparing the raw string instead is an infinite
  // navigation loop: we'd arrive at /how-it-works, decide we still weren't
  // there, and navigate again.
  const norm = (p) => String(p).split('#')[0].split('?')[0].replace(/\/$/, '') || '/';
  return norm(page) === norm(path);
}

function _startTour(id, steps, index, skipped) {
  _endTour(false); // clean up any prior run
  // Wire Escape + resize/scroll reflow for EVERY launch path (direct
  // launchTour on the current page as well as resumePendingTour). Guarded by
  // _listenersAttached so it's idempotent.
  _attachListeners();

  const overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  document.body.appendChild(overlay);

  // `skipped` collects indices whose anchor turned out to be absent on their
  // page (permission-gated CTAs, layout-dependent nav items). The progress dots
  // are built from the steps that actually render, so the count stays honest
  // instead of promising a "Step 4 of 5" that never appears. It can only grow
  // as the tour walks forward — a step on a page not yet visited is assumed
  // renderable until proven otherwise. `liftedAncestor` tracks a stacking-
  // context ancestor (e.g. the rail) temporarily promoted above the scrim so
  // a spotlighted descendant is reachable — see _findStackingContextAncestor.
  _active = {
    id, steps, index: null, overlay, popover: null, spotlight: null,
    skipped: new Set(skipped || []),
    liftedAncestor: null,
    // Additional elements ringed for the current step (`extraSpotlight`) —
    // tracked so they are cleaned up on step change and on end, exactly like
    // the anchor's own ring.
    extraSpotlights: [],
  };
  _showStep(index);
}

// Resolve a step's anchor to an element that is actually ON SCREEN.
//
// `document.querySelector` is not enough for either half of that. It returns
// the first MATCH, which with the comma selectors these steps use can easily be
// a hidden one while a perfectly good sibling matches later; and "present in
// the DOM" is not "visible" — the Library renders every group's rows and then
// collapses the groups with `display: none`, so the sharing badge the share
// step points at is a real element with a zero box. Spotlighting that paints a
// ring around nothing and drops the popover in the top-left corner.
//
// A step may also name a `reveal` control (or a list of them, tried in order):
// the thing that would UN-hide its anchor — a collapsed section header, a
// closed menu. It's clicked only when the anchor can't be found on its own, and
// only when it isn't already expanded, so the tour can't collapse something the
// user opened.
function _visibleMatch(selector) {
  // Selector PRIORITY, not document order. A step's comma list is a fallback
  // chain — "the composer pill, else the bare textarea"; "the open menu, else
  // the button that opens it" — and one `querySelectorAll` over the whole list
  // returns whichever comes first in the DOCUMENT, which for a
  // specific-then-generic chain is reliably the wrong one (the generic fallback
  // usually sits higher in the page). Walk the chain in the order it was
  // authored and take the first candidate that is actually laid out.
  for (const part of String(selector).split(',')) {
    const sel = part.trim();
    if (!sel) continue;
    for (const el of document.querySelectorAll(sel)) {
      if (el.offsetWidth || el.offsetHeight || el.getClientRects().length) return el;
    }
  }
  return null;
}

function _applyExtraSpotlights(step, anchor) {
  if (!_active || !step.extraSpotlight) return;
  for (const sel of [].concat(step.extraSpotlight)) {
    const el = _visibleMatch(sel);
    // Never double-ring the anchor (it already has one, and removing the class
    // on step change would then be order-dependent).
    if (!el || el === anchor) continue;
    el.classList.add('tour-spotlight');
    _active.extraSpotlights.push(el);
  }
}

function _clearExtraSpotlights() {
  if (!_active || !_active.extraSpotlights) return;
  for (const el of _active.extraSpotlights) el.classList.remove('tour-spotlight');
  _active.extraSpotlights = [];
}

// Open the collapsed container that HOLDS the anchor.
//
// A step declares `revealHost: { container, toggle }`: the collapsible wrapper
// the anchor may be sitting inside, and the control within that wrapper which
// expands it. For each DOM match of the anchor that has no box, we walk up to
// its own container and open that one — so the section that gets expanded is
// always the section the target is actually in.
//
// This replaces a list of hardcoded openers (`[data-sec-toggle="files"]`,
// `[data-sec-toggle="skill"]`, …), which was guesswork twice over: it opened
// groups that turned out not to contain the target, and it silently failed for
// any group nobody had thought to list — a shareable row in Plugins, an addable
// row in Recipes. Never collapses anything: only a container reporting
// `aria-expanded="false"` is clicked, so a section the user opened themselves is
// left alone.
function _revealAnchorHost(step) {
  const host = step.revealHost;
  if (!host) return null;
  const laidOut = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  // Openers we clicked, so anything that didn't pay off can be put back. Opening
  // a section is how we find out whether the target is in it, and a step that
  // leaves three sections expanded to spotlight a control in the fourth has
  // rearranged the reader's page for no reason.
  const opened = [];
  let found = null;
  for (const el of document.querySelectorAll(step.selector)) {
    if (laidOut(el)) {
      found = el;
      break;
    }
    const container = el.closest(host.container);
    if (!container) continue;
    const opener = container.querySelector(host.toggle);
    if (!opener || opener.getAttribute('aria-expanded') === 'true') continue;
    opener.click();
    opened.push({ opener, container });
    // Reading a layout property forces the reflow, so the freshly un-hidden
    // anchor measures correctly without waiting a frame.
    if (laidOut(el)) {
      found = el;
      break;
    }
  }
  for (const { opener, container } of opened) {
    if (found && container.contains(found)) continue;
    if (opener.getAttribute('aria-expanded') === 'true') opener.click();
  }
  return found;
}

// Resolution order: the step's own `selector`, then `revealHost` (open the thing
// hiding it), then `fallbackSelector` — a lesser anchor accepted only once the
// real one has proven unreachable.
//
// Those last two being SEPARATE fields is load-bearing. The reveal step used to
// be a fallback for the whole comma chain, which quietly defeated itself the
// moment a chain contained its own fallback: the in-stack step listed
// `[data-add-to-stack], #lib-stack-toggle`, every Library group ships COLLAPSED
// (so all twelve Add controls have a zero-size box), and the filter toggle —
// always visible — satisfied the chain before the reveal was ever consulted. The
// step spotlighted a filter and nothing ever opened a group. Keeping the real
// anchor and the consolation prize apart is what lets "try hard, including
// expanding the right section" run before "settle for this instead".
function _resolveAnchor(step) {
  const found = _visibleMatch(step.selector);
  if (found) return found;

  const revealed = _revealAnchorHost(step);
  if (revealed) return revealed;

  return step.fallbackSelector ? _visibleMatch(step.fallbackSelector) : null;
}

// Some anchors are rendered by their own page's JS after a fetch resolves — the
// Agents list is built from /api/agents — so on a cross-page hop they are
// legitimately absent for the first frames after load. Resolving once and
// dropping the step on a miss turns that race into a step nobody ever sees,
// which is invisible: the tour just gets shorter. A step that knows its anchor
// arrives late declares `awaitAnchor` and gets polled for up to
// ANCHOR_WAIT_MS before we accept it isn't coming.
//
// The callback fires synchronously when the anchor is already there, so every
// existing step keeps its current single-pass behaviour.
const ANCHOR_WAIT_MS = 2500;
const ANCHOR_POLL_MS = 100;

function _resolveAnchorEventually(step, cb) {
  const found = _resolveAnchor(step);
  if (found || !step.awaitAnchor) {
    cb(found);
    return;
  }
  const start = performance.now();
  const poll = () => {
    const el = _resolveAnchor(step);
    if (el || performance.now() - start > ANCHOR_WAIT_MS) {
      cb(el);
      return;
    }
    setTimeout(poll, ANCHOR_POLL_MS);
  };
  setTimeout(poll, ANCHOR_POLL_MS);
}

function _showStep(index) {
  if (!_active) return;
  const { steps } = _active;
  const step = steps[index];
  if (!step) { _endTour(true); return; }

  // Clear previous popover + spotlight.
  if (_active.popover) _active.popover.remove();
  if (_active.spotlight) {
    _active.spotlight.classList.remove('tour-spotlight');
    _active.spotlight = null;
  }
  _clearExtraSpotlights();
  if (_active.liftedAncestor) {
    _active.liftedAncestor.classList.remove('tour-lifts-ancestor');
    _active.liftedAncestor = null;
  }

  _active.index = index;

  if (step.centered || !step.selector) {
    _renderStep(index, null);
    return;
  }

  _resolveAnchorEventually(step, (anchor) => {
    // A step can be superseded while we wait (Next/Back/Escape) — `index` is
    // then stale and rendering it would resurrect a card the user dismissed.
    if (!_active || _active.index !== index) return;
    if (!anchor) {
      // Resilient: the anchor is absent here. Record it so the progress dots
      // stop counting a step the user will never see, then route through
      // _gotoStep so a next step on another page cross-navigates instead of
      // recursing on the current page (which would blow through cross-page
      // steps to the final screen).
      _active.skipped.add(index);
      _gotoStep(index + 1);
      return;
    }
    _renderStep(index, anchor);
  });
}

// Paint the step: spotlight the anchor (when there is one), stash the resume
// record, build and place the card. Split out of _showStep because anchor
// resolution can now span frames — everything here has to run AFTER it, and
// only if the step is still the current one.
function _renderStep(index, anchor) {
  if (!_active) return;
  const { steps } = _active;
  const step = steps[index];
  if (!step) { _endTour(true); return; }

  if (anchor) {
    // Scroll target into view — horizontally only "nearest" so a target near
    // the viewport edge (e.g. a nav item in the rail's mobile top-bar layout)
    // never induces horizontal scroll of the page itself (see the
    // rail-overflow note on _positionPopover's horizontal clamp).
    anchor.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    anchor.classList.add('tour-spotlight');
    _active.spotlight = anchor;

    // If the anchor sits inside a positioned ancestor that has its own
    // z-index (a stacking context — e.g. the rail: `position: fixed;
    // z-index: 40`), `.tour-spotlight`'s z-index resolves INSIDE that
    // context and can never outrank the scrim, which paints at the root.
    // Lift the ancestor above the scrim for the step's duration so the
    // spotlighted target is actually reachable through it.
    const stackingAncestor = _findStackingContextAncestor(anchor);
    if (stackingAncestor) {
      stackingAncestor.classList.add('tour-lifts-ancestor');
      _active.liftedAncestor = stackingAncestor;
    }
  }

  // Ring anything else the step is teaching. Some steps explain a CONCEPT that
  // two controls between them express — "in stack" is what the filter names and
  // what the Add button does — and one ring around one of them leaves the reader
  // to guess which half of the sentence they are looking at. These take the same
  // ring but never the popover: a card can only point at one place, and it
  // points at the control the copy asks you to click.
  _applyExtraSpotlights(step, anchor);

  // Keep the resume record in sync with the step actually on screen (not just
  // cross-page hops), so a reload mid-tour resumes here — and re-stamp its
  // freshness. Combined with the RESUME_FRESH_MS window, an abandoned tour
  // stops re-popping once the record goes stale.
  stashPending(_active.id, index, _active.skipped);

  // Build popover.
  const popover = _buildPopover(step, index, steps.length);
  document.body.appendChild(popover);
  _active.popover = popover;

  // Position once the scroll above has actually settled — `scrollIntoView`
  // with `behavior: 'smooth'` takes ~300-500ms, while a single
  // requestAnimationFrame fires in ~16ms. Positioning on the very next frame
  // reads the anchor's PRE-scroll rect, so the popover lands wherever the
  // anchor used to be and is never corrected once the scroll finishes. Wait
  // for the anchor's rect to stop moving instead of guessing a fixed delay.
  _waitForScrollSettle(anchor, () => {
    if (_active && _active.index === index) {
      _ensureAnchorVisible(anchor);
      _positionPopover(popover, anchor, step.centered);
    }
  });
}

// Last line of defence on the one promise a coach-mark makes: that you can see
// what it is pointing at.
//
// The scroll above aims at the anchor's rect AT THAT MOMENT, and the page can
// grow underneath it — the Library builds its grid cards after a group expands,
// so a step that reveals a section and then scrolls to a row inside it lands
// short and leaves the spotlight a screen and a half below the fold. Re-check
// once the scroll has settled and correct instantly (no second smooth scroll to
// wait on), before the popover is placed, so the card is positioned from the
// rect the reader will actually be looking at.
function _ensureAnchorVisible(anchor) {
  if (!anchor) return;
  const rect = anchor.getBoundingClientRect();
  if (rect.top >= 0 && rect.bottom <= window.innerHeight) return;
  anchor.scrollIntoView({ block: 'center', inline: 'nearest' });
}

// Resolve once `anchor`'s bounding rect is unchanged across two consecutive
// animation frames (the scroll driving it has settled), or after
// SCROLL_SETTLE_TIMEOUT_MS elapses — whichever comes first, so a scroll that
// never quite stabilizes (e.g. a still-loading page) can't hang the tour
// forever. No-op (fires on the next frame) when there's no anchor, i.e. a
// centered step.
const SCROLL_SETTLE_TIMEOUT_MS = 600;

function _waitForScrollSettle(anchor, cb) {
  if (!anchor) {
    requestAnimationFrame(cb);
    return;
  }
  const start = performance.now();
  let last = anchor.getBoundingClientRect();
  const check = () => {
    const rect = anchor.getBoundingClientRect();
    const stable = rect.top === last.top && rect.left === last.left;
    if (stable || performance.now() - start > SCROLL_SETTLE_TIMEOUT_MS) {
      cb();
      return;
    }
    last = rect;
    requestAnimationFrame(check);
  };
  requestAnimationFrame(check);
}

// ── Popover builder ─────────────────────────────────────────────────────────

function _buildPopover(step, index, total) {
  // A one-step tour is a coach-mark, not a walkthrough: progress dots and an
  // "I'll explore on my own" escape hatch both imply a sequence that isn't
  // there, so the solo form drops them and reads "Got it" instead of "Next".
  const solo = total === 1;

  const popover = document.createElement('div');
  popover.className = 'tour-popover' + (step.finalChoice ? ' tour-popover--final' : '');
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-modal', 'false');
  popover.setAttribute('aria-live', 'polite');
  popover.setAttribute('aria-label', step.title);

  // Header
  const header = document.createElement('div');
  header.className = 'tour-popover-header';
  header.innerHTML = `
    <span class="tour-popover-header-orb" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="12" r="3.5" fill="currentColor" opacity=".6"/></svg>
    </span>
    <span class="tour-popover-header-label">Agnes is showing you around</span>`;

  // Body
  const body = document.createElement('div');
  body.className = 'tour-popover-body';

  const title = document.createElement('h2');
  title.className = 'tour-popover-title';
  title.textContent = step.title;

  const desc = document.createElement('p');
  desc.className = 'tour-popover-desc';
  desc.textContent = step.desc;

  body.appendChild(title);
  body.appendChild(desc);

  if (step.points && step.points.length) {
    const ul = document.createElement('ul');
    ul.className = 'tour-popover-points';
    step.points.forEach((pt) => {
      const li = document.createElement('li');
      li.className = 'tour-popover-point';
      li.textContent = pt;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }

  // Footer
  const footer = document.createElement('div');
  footer.className = 'tour-popover-footer';

  // Dots
  const dots = document.createElement('div');
  dots.className = 'tour-dots';
  // Count only the steps that will actually render. A step whose anchor was
  // missing is dropped from the progress indicator entirely, so the label never
  // promises a step the user cannot reach.
  const skipped = (_active && _active.skipped) || new Set();
  const shown = [];
  for (let i = 0; i < total; i++) if (!skipped.has(i)) shown.push(i);
  const position = shown.indexOf(index);
  dots.setAttribute('aria-label', `Step ${position + 1} of ${shown.length}`);
  shown.forEach((i) => {
    const dot = document.createElement('span');
    dot.className = 'tour-dot' + (i === index ? ' on' : '');
    dots.appendChild(dot);
  });

  // Actions
  const actions = document.createElement('div');
  actions.className = 'tour-actions';

  if (step.finalChoice) {
    // Final step: Back · Finish onboarding · Connect my AI tools
    const backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'tour-btn tour-btn-back';
    backBtn.textContent = 'Back';
    if (index === 0) backBtn.disabled = true;
    backBtn.addEventListener('click', () => _gotoStep(index - 1));

    const finishBtn = document.createElement('button');
    finishBtn.type = 'button';
    finishBtn.className = 'tour-btn tour-btn-finish';
    finishBtn.textContent = 'Finish onboarding';
    finishBtn.addEventListener('click', async () => {
      markSeen(_active ? _active.id : 'welcome');
      await markTourStepsDone();
      _endTour(true);
      window.location.href = '/chat';
    });

    const connectBtn = document.createElement('button');
    connectBtn.type = 'button';
    connectBtn.className = 'tour-btn tour-btn-connect';
    connectBtn.textContent = 'Connect my AI tools';
    connectBtn.addEventListener('click', () => {
      markSeen(_active ? _active.id : 'welcome');
      // Navigating to the connect section is exactly what the journey panel's
      // "Use Agnes from other AI tools" step does, and that step marks itself
      // done on click — mirror it so the same action doesn't behave two ways.
      // Fire-and-forget: the navigation below must not wait on it.
      markUseAnywhereDone();
      _endTour(true);
      // /how-it-works#connect is the per-tool MCP guide (Claude Code · Cursor ·
      // VS Code · …) that matches this button's intent and the main-page
      // "Connect your tools" CTA. It was the standalone /me/ai-connector page
      // until that was absorbed into the consolidated orientation page, which
      // still 302s here. /setup is the narrower CLI-install page, reached from
      // getting-started, not from here.
      window.location.href = '/how-it-works#connect';
    });

    actions.appendChild(backBtn);
    actions.appendChild(finishBtn);
    actions.appendChild(connectBtn);
  } else {
    // Normal step: "I'll explore on my own" · Back · Next/Done
    const endBtn = document.createElement('button');
    endBtn.type = 'button';
    endBtn.className = 'tour-btn tour-btn-end';
    endBtn.textContent = 'I\'ll explore on my own';
    endBtn.addEventListener('click', () => _endTour(true));

    const backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'tour-btn tour-btn-back';
    backBtn.textContent = 'Back';
    if (index === 0) backBtn.disabled = true;
    backBtn.addEventListener('click', () => _gotoStep(index - 1));

    const isLastNonFinal = index === total - 2; // last before finalChoice
    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'tour-btn tour-btn-next';
    nextBtn.textContent = solo ? 'Got it' : (isLastNonFinal ? 'Done' : 'Next');
    nextBtn.addEventListener('click', () => _advanceStep());

    if (!solo) {
      actions.appendChild(endBtn);
      actions.appendChild(backBtn);
    }
    actions.appendChild(nextBtn);
  }

  if (solo) {
    // Without the dots the lone action row would sit left under
    // `justify-content: space-between` — pin it right.
    footer.classList.add('tour-popover-footer--solo');
  } else {
    footer.appendChild(dots);
  }
  footer.appendChild(actions);

  popover.appendChild(header);
  popover.appendChild(body);
  popover.appendChild(footer);

  return popover;
}

// ── Step advance (handles cross-page navigation) ────────────────────────────

// Go to a specific step index, crossing pages when that step lives on another
// page. Shared by _advanceStep (Next) and the missing-anchor auto-skip so both
// handle cross-page steps identically — otherwise a missing anchor would blindly
// recurse on the current page and blow through every cross-page step to the end.
function _gotoStep(nextIndex) {
  if (!_active) return;
  const { id, steps } = _active;
  if (nextIndex >= steps.length) { _endTour(true); return; }

  const nextStep = steps[nextIndex];
  if (!pathMatches(nextStep.page, window.location.pathname)) {
    // Cross-page: persist progress (as pending, NOT "seen" — the tour isn't
    // done) + navigate. resumePendingTour resumes it on the destination page.
    stashPending(id, nextIndex, _active.skipped);
    window.location.href = nextStep.page;
    return;
  }

  _showStep(nextIndex);
}

function _advanceStep() {
  if (!_active) return;
  _gotoStep(_active.index + 1);
}

// ── End tour ────────────────────────────────────────────────────────────────

function _endTour(markSeenNow) {
  if (!_active) return;
  if (markSeenNow) markSeen(_active.id);
  clearPending();

  if (_active.spotlight) {
    _active.spotlight.classList.remove('tour-spotlight');
  }
  _clearExtraSpotlights();
  if (_active.liftedAncestor) {
    _active.liftedAncestor.classList.remove('tour-lifts-ancestor');
  }
  if (_active.popover) _active.popover.remove();
  if (_active.overlay) _active.overlay.remove();
  _active = null;
  _removeListeners();
}

// Walk up from `el` to find the nearest ancestor that establishes its own
// stacking context via a positioned + z-indexed box (the pattern behind the
// bug this exists to work around: the rail is `position: fixed; z-index:
// 40`). A z-index set on a descendant of such an ancestor is scoped to that
// ancestor's context and can never out-rank an element painted at the root,
// like the tour scrim — regardless of how large the descendant's own
// z-index is. Stops at <body> since promoting the whole document is never
// useful here.
function _findStackingContextAncestor(el) {
  let node = el.parentElement;
  while (node && node !== document.body) {
    const cs = getComputedStyle(node);
    if (cs.position !== 'static' && cs.zIndex !== 'auto') return node;
    node = node.parentElement;
  }
  return null;
}

// ── Positioning ─────────────────────────────────────────────────────────────

const POPOVER_GAP = 14;
const VIEWPORT_PAD = 12;
// Below this, capping the card to the space beside its anchor stops producing
// a readable coach-mark (header + a title + one scrolling line + the actions
// row) and centering is the more honest layout. Roughly the header/footer
// chrome plus two lines of body.
const MIN_POPOVER_HEIGHT = 240;

function _positionPopover(popover, anchor, centered) {
  // Drop any cap a previous placement applied BEFORE measuring — otherwise a
  // reflow into a roomier spot (window resize, the anchor scrolling away from
  // an edge) keeps the old constrained height forever.
  popover.style.maxHeight = '';
  if (centered || !anchor) {
    // Center in the viewport WITHOUT a transform. The popover carries a
    // fill:both entrance animation whose end keyframe sets `transform`
    // (translateY(0) scale(1)); a CSS animation outranks an inline style, so an
    // inline translate(-50%,-50%) would be clobbered and the card would settle
    // off-centre (down/right). Compute absolute top/left from the measured size
    // instead — the animation's identity end-transform is then harmless.
    const popW = popover.offsetWidth || 380;
    const popH = popover.offsetHeight || 260;
    popover.style.position = 'fixed';
    popover.style.transform = '';
    popover.style.left = `${Math.max(VIEWPORT_PAD, (window.innerWidth - popW) / 2)}px`;
    popover.style.top = `${Math.max(VIEWPORT_PAD, (window.innerHeight - popH) / 2)}px`;
    return;
  }

  popover.style.transform = '';
  const rect = anchor.getBoundingClientRect();
  const popW = popover.offsetWidth || 380;
  let popH = popover.offsetHeight || 260;
  const vh = window.innerHeight;
  const vw = window.innerWidth;

  // Room available on each side of the anchor (clamped at 0 — an anchor
  // partly scrolled off-screen must not report negative space).
  const spaceBelow = Math.max(0, vh - rect.bottom - POPOVER_GAP - VIEWPORT_PAD);
  const spaceAbove = Math.max(0, rect.top - POPOVER_GAP - VIEWPORT_PAD);

  if (popH > spaceBelow && popH > spaceAbove) {
    // The card fits on neither side of the anchor at its natural height — a
    // short viewport, or an anchor low on the page like the chat composer.
    //
    // Centering here USED to be the answer, and it was wrong in the one case
    // that matters most: it lays the card straight over the thing the step is
    // pointing at. On the composer step the user is being asked to type into
    // that anchor, so covering it defeats the step. Take the roomier side
    // instead and cap the card to the space that is actually there —
    // `.tour-popover-body` scrolls (tour.css), so nothing becomes unreachable,
    // and the anchor stays visible and usable.
    const roomier = Math.max(spaceBelow, spaceAbove);
    if (roomier < MIN_POPOVER_HEIGHT) {
      // Not even a readable card fits beside the anchor. NOW centering is the
      // honest layout — pinning to `top: VIEWPORT_PAD` reads as broken (a card
      // floating disconnected from what it points at).
      _positionPopover(popover, anchor, true);
      return;
    }
    popover.style.maxHeight = `${roomier}px`;
    popH = roomier;
  }

  // Try below the anchor first, else flip above.
  let top = popH <= spaceBelow ? rect.bottom + POPOVER_GAP : rect.top - POPOVER_GAP - popH;
  top = Math.max(VIEWPORT_PAD, Math.min(top, vh - popH - VIEWPORT_PAD));

  // Horizontal: align to anchor left, clamped into viewport.
  let left = rect.left;
  left = Math.max(VIEWPORT_PAD, Math.min(left, vw - popW - VIEWPORT_PAD));

  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
}

// ── Reflow listeners (resize + scroll) ──────────────────────────────────────

function _onReflow() {
  if (!_active || !_active.popover) return;
  const step = _active.steps[_active.index];
  if (!step) return;
  _positionPopover(_active.popover, _active.spotlight, step.centered);
}

function _onKeyDown(e) {
  if (e.key === 'Escape' && _active) _endTour(true);
}

let _listenersAttached = false;

function _attachListeners() {
  if (_listenersAttached) return;
  window.addEventListener('resize', _onReflow, { passive: true });
  window.addEventListener('scroll', _onReflow, { passive: true });
  document.addEventListener('keydown', _onKeyDown);
  _listenersAttached = true;
}

function _removeListeners() {
  window.removeEventListener('resize', _onReflow);
  window.removeEventListener('scroll', _onReflow);
  document.removeEventListener('keydown', _onKeyDown);
  _listenersAttached = false;
}

// ── Auto-resume on page load ────────────────────────────────────────────────

/**
 * Call once per page on DOMContentLoaded (or directly from a module script).
 * Checks for a pending tour whose next step belongs to the current page
 * and resumes automatically.
 *
 * Returns true if a tour was resumed, false otherwise.
 */
export function resumePendingTour() {
  const pending = getPending();
  if (!pending) return false;

  // Only resume a fresh record — a stale one means the user abandoned the tour
  // via ordinary navigation; don't re-pop it uninvited.
  if (!pending.ts || Date.now() - pending.ts > RESUME_FRESH_MS) {
    clearPending();
    return false;
  }

  const { id, index, skipped } = pending;
  const steps = TOURS[id];
  if (!steps) { clearPending(); return false; }

  const step = steps[index];
  if (!step) { clearPending(); return false; }

  if (!pathMatches(step.page, window.location.pathname)) return false;

  // Resume — don't clearPending yet; _endTour will. _startTour attaches listeners.
  _startTour(id, steps, index, skipped);
  return true;
}
