// rail_history.js — the rail's recent-conversations list: populated here on
// every page EXCEPT /chat.
//
// The rail (html[data-ui-layout="rail"]) renders the chat list
// (_app_rail.html → .rail-history) on every rail page, /admin included, directly
// under the New chat + Chats rows. On /chat, chat.js owns that same
// <ul id="chat-list"> — it renders live, highlights the active row, and handles
// open/delete in place — so this script MUST stay out of its way there. On every
// other page chat.js isn't loaded, so we fetch the caller's sessions and render
// the same rows, wiring each to a /chat?session=<id> navigation.
//
// The recent feed is CAPPED (RAIL_RECENT_LIMIT); the Chats row ABOVE the lists
// is the way to the rest of them (/chats). Pins are never capped. See the note
// further down for why this is not the five-row cap that was deliberately
// removed.
//
// Pinning is server-side state (`chat_sessions.pinned_at`, PUT
// /api/chat/sessions/{id}/pin). Pinned rows go into their OWN section — the
// <ul id="pinned-chat-list"> above the feed — on BOTH paths, which is why
// chat.js routes its rows the same way and stamps `data-pinned="1"` on them too.
//
// The two sections (Pinned · Chats) are collapsible, and THIS file owns that on
// every rail page including /chat: the open/closed state (localStorage), the
// aria wiring, and which of the two sections renders at all. chat.js
// only has to call window.railChatSections.sync() after it re-renders. See the
// section block below and the markup notes in _app_rail.html.
//
// Row actions (Pin/Unpin · Rename · Delete) live behind one "⋮" menu owned by
// js/components/chat_row_menu.js, shared with chat.js so both renderers offer
// the identical set. Rename goes through PUT /api/chat/sessions/{id}/title.
//
// Loaded with `defer` (not a module) from the rail partial, gated on can_chat.
(function () {
  "use strict";

  // ---- The recent feed is a working set, not the history ----------------
  // This IS a cap, and the rail had one removed on purpose, so the difference
  // matters: that one had nowhere to go. It showed five rows on a laptop with
  // room for nine and then offered a "Show more" button whose only job was to
  // undo a limit we had imposed ourselves — plus a truncation pass, a
  // MutationObserver, a persisted expanded-state, date-header hiding, a "never
  // truncate the active row" case and a pins-are-exempt budget to maintain it.
  // None of that comes back.
  //
  // What justifies a cap now is that there is a DESTINATION: /chats
  // (templates/chats.html) lists every conversation with search, the four
  // views, sorting, per-row actions and bulk actions. With that page in place
  // the rail's job narrows to "put me back into what I was doing", which is a
  // handful of rows and the pins — and an uncapped scroll box full of a hundred
  // titles actively works against it, because it reads as the only list there
  // is. So: this many recent rows, under a Chats row that goes to all of them.
  // (That row is why the cap needs no "View all chats" link at the foot of the
  // list any more — see railSectionsSync below.)
  //
  // Pins are NOT capped. The shelf is hand-curated and small by construction,
  // and hiding a pin would break the one promise pinning makes.
  //
  // Date group headers are gone with the long feed — "Older" over a five-row
  // list bounded at the top by "Recent" was labelling a boundary the list is too
  // short to have. chat.js keeps the full five-bucket set for the TOPNAV
  // sidebar, which is a full-height column that genuinely needs them.
  const RAIL_RECENT_LIMIT = 5;

  // ---- Mobile/tablet nav collapse (row layout, ≤1024px) --------------
  // Below 1024px the rail becomes a wrapping top bar with nowhere to collapse
  // to — this toggle shows/hides both nav zones + recents + Admin
  // (.rail-collapsible); the foot (the profile row) stays reachable
  // regardless of its state. Inert above the breakpoint (the
  // button is display:none there, rail.css) — no harm wiring it always.
  const navToggle = document.getElementById("rail-collapse-toggle");
  const railEl = document.querySelector("html[data-ui-layout='rail'] .rail");
  if (navToggle && railEl) {
    navToggle.addEventListener("click", () => {
      const open = !railEl.classList.contains("is-nav-open");
      railEl.classList.toggle("is-nav-open", open);
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // ---- Onboarding card popover ("Set up Agnes") -----------------------
  // Reveals on hover via CSS, but ALSO opens on click and stays pinned, with
  // outside-click / Escape to close. Wired before the /chat bail so it works
  // there too. chat_onboarding.js fills #chat-journey inside the panel — and
  // hides the whole row once every step is done.
  //
  // The behaviour itself lives in rail_popover.js because the rail has TWO of
  // these cards: this one and the admin setup chain, which reuses
  // `.rail-getstarted` for its look but carries its own ids and so was getting
  // none of this. See the note there.
  const gsToggle = document.getElementById("rail-getstarted-toggle");
  const gsWrap = document.getElementById("railGetStarted");
  if (window.railPopover) {
    window.railPopover.wire(gsWrap, gsToggle, () => {
      // Ask the checklist to re-read /api/chat/journey before it comes into
      // view. Steps complete from real activity now (adding to your stack from
      // the Library marks its milestone server-side), so the copy this panel
      // loaded at page load can already be out of date by the time it is
      // opened — and a checklist showing a step you just finished as pending is
      // the single fastest way to make onboarding feel broken.
      document.dispatchEvent(new CustomEvent("agnes:journey-refresh"));
    });
  }

  // ---- Scroll fade on the recents list --------------------------------
  // Marks WHICH edge of .rail-history-body still has rows behind it; rail.css
  // turns each flag into a gradient mask so the list dissolves into the rail
  // instead of ending in a hard cut through a title. Only the state lives here
  // — the pixels are the stylesheet's.
  //
  // Wired BEFORE the /chat bail, like the nav collapse and the onboarding
  // popover above: chat.js owns the ROWS on /chat, but the scroll container is
  // the same element on every rail page and no other script touches it.
  // Assigned inside the block below and called from the section-collapse
  // handler too: collapsing or opening a section changes what is scrollable, so
  // the fade flags have to be recomputed immediately rather than waiting for the
  // ResizeObserver to notice a box that may not have changed size.
  let syncHistoryFade = () => {};
  const histBody = document.getElementById("rail-history-body");
  if (histBody) {
    // A pixel of slack at both ends. Fractional scroll offsets (browser zoom,
    // HiDPI, momentum scrolling) mean scrollTop rarely lands exactly on 0 or
    // exactly on the maximum, so an exact comparison leaves the bottom fade
    // permanently on at the true end of the list — which is precisely the
    // "there is a clipped row down there" impression the fade exists to avoid.
    const EPS = 1;
    const syncFade = () => {
      const max = histBody.scrollHeight - histBody.clientHeight;
      histBody.classList.toggle("is-fade-top", histBody.scrollTop > EPS);
      histBody.classList.toggle("is-fade-bottom", max > EPS && histBody.scrollTop < max - EPS);
    };
    syncHistoryFade = syncFade;
    histBody.addEventListener("scroll", syncFade, { passive: true });
    // Observe the boxes rather than syncing once at load: the rows arrive from
    // an async fetch (on BOTH renderers), rename/delete/pin change the list's
    // height under us, and the free space itself moves whenever the viewport
    // or the zones around it resize. Watching the list's own box covers the
    // content changes without a MutationObserver.
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(syncFade);
      ro.observe(histBody);
      for (const id of ["chat-list", "pinned-chat-list"]) {
        const histList = document.getElementById(id);
        if (histList) ro.observe(histList);
      }
    }
    syncFade();
  }

  // ---- Chat sections (Pinned · Recent) --------------------------------
  // Wired BEFORE the /chat bail, like the nav collapse, the onboarding popover
  // and the scroll fade: chat.js owns the ROWS on /chat, but the sections are
  // rail chrome and have exactly one owner on every rail page. chat.js reaches
  // this through `window.railChatSections.sync()` after it re-renders.
  //
  // Neither section collapses any more, so there is no per-section open/closed
  // state and nothing in localStorage: the labels are inert <h2>s (see
  // _app_rail.html), and the ONLY thing decided here is which of the two
  // sections renders at all, from how many rows each holds. `body` is still
  // addressed because the fade observer above watches it.
  const SECTIONS = [
    { key: "pinned", sec: "rail-pinned", body: "rail-pinned-body", list: "pinned-chat-list" },
    { key: "chats", sec: "rail-chats", body: "rail-chats-body", list: "chat-list" },
  ];

  function railSectionsSync() {
    // Row counts first: whether a section renders at all depends on the OTHER
    // one. A "Pinned" header over nothing is dead chrome, so Pinned appears only
    // once it has rows; and Chats hides when its feed is empty but pins exist,
    // because its header would then stand over nothing while its empty state
    // ("No conversations yet.") would be plainly false. With nothing pinned and
    // nothing in the feed, Chats stays and shows that empty state — the one
    // thing a first-run rail should say.
    const rowsIn = (id) => {
      const el = document.getElementById(id);
      return el ? el.querySelectorAll("li[data-id]").length : 0;
    };
    const pinnedRows = rowsIn("pinned-chat-list");
    // The "View all chats" link at the foot of the region is NOT touched here.
    // It used to be revealed only once the caller had a conversation to view —
    // sound for a footer link, fatal for a way to a page: it meant /chats had no
    // entry point at all on a first run. It is static markup now, so emptiness
    // is the only thing this function still decides.
    for (const s of SECTIONS) {
      const sec = document.getElementById(s.sec);
      if (!sec) continue;
      const rows = rowsIn(s.list);
      sec.hidden = s.key === "pinned" ? rows === 0 : rows === 0 && pinnedRows > 0;
    }
    syncHistoryFade();
  }

  // The seam chat.js binds to on /chat (it owns the rows there, this file owns
  // the chrome). Published unconditionally so load order between the two scripts
  // cannot matter: chat.js guards the call.
  window.railChatSections = { sync: railSectionsSync };
  // Apply the persisted state before any rows arrive, so a collapsed section
  // never flashes open on load.
  railSectionsSync();

  // /chat is chat.js's turf for the LIST — bail so we never double-render or
  // fight it (the nav collapse + onboarding popover above are wired for both).
  if (document.body.classList.contains("chat-page-body")) return;

  // Looked up here, NOT hoisted to the top of the IIFE. It used to be a
  // module-level `listEl` declared beside the truncation state; when that block
  // was deleted this line still read `listEl`, so the whole script threw a
  // ReferenceError and the rail rendered NO conversations on any page except
  // /chat. `node --check` cannot catch that — it is valid syntax.
  const list = document.getElementById("chat-list");
  if (!list) return; // no chat grant / history section not rendered

  // The Pinned section's own list. Absent only if this partial ever renders
  // without it — the renderer then falls back to putting pinned rows at the head
  // of the feed, which is where they used to live.
  const pinnedList = document.getElementById("pinned-chat-list");

  const emptyEl = document.getElementById("cloud-chat-empty-state");
  // TCRD-207 (DES-153): the FAILED sibling of emptyEl — a request that never
  // completed must never render as "no conversations" (see load() below).
  const failedEl = document.getElementById("cloud-chat-failed-state");

  // ---- Fetch helper ---------------------------------------------------
  async function api(path, init) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      ...(init || {}),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    if (r.status === 204 || r.headers.get("content-length") === "0") return null;
    return r.json();
  }

  // ---- Row rendering --------------------------------------------------
  // Same markup/classes as chat.js's _makeSidebarItem so rail.css styles both
  // identically — but the row NAVIGATES (this page has no in-place session
  // machinery) instead of calling openSession.
  // Pushpin glyph, shared with chat.js's copy. Outline by default; rail.css
  // fills it via `fill: currentColor` on the pressed state so a pinned row
  // reads as pinned at a glance and not only by position.
  const PIN_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 17v5"/>' +
    '<path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8' +
    'a2 2 0 0 0-1.1-1.7l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>' +
    "</svg>";

  function makeRow(s) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    // Read by rail.css for the always-visible pin button on a pinned row, and
    // it is the contract chat.js mirrors (see the header comment).
    if (s.pinned) {
      li.dataset.pinned = "1";
      li.classList.add("is-pinned");
    }
    li.title = s.title || `Untitled · ${s.id}`;
    li.setAttribute("role", "button");
    li.tabIndex = 0;
    li.setAttribute("aria-label", `Open ${s.title || "untitled conversation"}`);
    const go = () => {
      window.location.href = `/chat?session=${encodeURIComponent(s.id)}`;
    };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        go();
      }
    });

    const label = document.createElement("span");
    label.className = "cloud-chat-list-label";
    label.textContent = s.title || "Untitled chat";
    li.appendChild(label);

    if (s.surface === "slack_dm" || s.surface === "slack_thread") {
      const badge = document.createElement("span");
      badge.className = "cloud-chat-surface-badge";
      badge.textContent = "Slack";
      badge.setAttribute("aria-hidden", "true");
      li.appendChild(badge);
    }

    // Pinned-state indicator. NOT a control — the pin ACTION lives in the row
    // menu now, so without this a pinned row would look identical to any other
    // once the "Pinned" header scrolled out of view. Filled (rail.css) and
    // aria-hidden: the row's group header already names the state for AT.
    if (s.pinned) {
      const flag = document.createElement("span");
      flag.className = "cloud-chat-pin-flag";
      flag.setAttribute("aria-hidden", "true");
      flag.innerHTML = PIN_SVG;
      li.appendChild(flag);
    }

    // One "⋮" for all three row actions (Pin/Unpin · Rename · Delete). Inline
    // icon buttons don't scale in a ~250px rail, and the shared component keeps
    // the menu identical to chat.js's copy. Absent if the component failed to
    // load — the row still opens the conversation, which is its main job.
    if (window.chatRowMenu) {
      li.appendChild(
        window.chatRowMenu.trigger({
          session: s,
          onPin: (pinned) => setPinned(s.id, pinned),
          onRename: () => renameSession(s),
          onDelete: () => deleteSession(s, { confirm: true }),
        }),
      );
    }
    return li;
  }

  function render(sessions) {
    // Every action re-renders, and the menu is body-appended (not a child of the
    // row) — so without this an open panel would survive the wipe and hover over
    // a row it no longer belongs to.
    if (window.chatRowMenu) window.chatRowMenu.close();
    list.innerHTML = "";
    // Pinned rows go to the Pinned section; everything else to the dated feed.
    // Without that section's list they fall back to the head of the feed, which
    // is the pre-sections behavior (the server already sorts pinned-first).
    const pinned = pinnedList ? sessions.filter((s) => s.pinned) : [];
    const dated = pinnedList ? sessions.filter((s) => !s.pinned) : sessions;
    if (pinnedList) {
      pinnedList.innerHTML = "";
      for (const s of pinned) pinnedList.appendChild(makeRow(s));
    }
    // The server already sorts pinned-first then most-recent-first, so the head
    // of this list IS the most recent work — no grouping, no headers, and the
    // rest is one click away in /chats (see RAIL_RECENT_LIMIT above).
    for (const s of dated.slice(0, RAIL_RECENT_LIMIT)) list.appendChild(makeRow(s));
    if (emptyEl) emptyEl.hidden = sessions.length > 0;
    // Reveal/hide each section for what it now holds, and re-apply its state.
    railSectionsSync();
  }

  async function setPinned(id, pinned) {
    try {
      await api(`/api/chat/sessions/${id}/pin`, {
        method: "PUT",
        body: JSON.stringify({ pinned }),
      });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load(); // re-fetch so the row moves into (or out of) the Pinned group
  }

  async function renameSession(s) {
    if (typeof window.promptModal !== "function") return;
    const next = await window.promptModal({
      title: "Rename conversation",
      message: "This is the name shown in the history panel.",
      defaultValue: s.title || "",
      placeholder: "Conversation name",
      confirmText: "Rename",
    });
    // null = cancelled/Escape. An unchanged or blank-only title is a no-op
    // rather than a request the server would just 400.
    if (next === null) return;
    const title = next.trim();
    if (!title || title === (s.title || "")) return;
    try {
      await api(`/api/chat/sessions/${s.id}/title`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load();
  }

  async function deleteSession(s, opts) {
    // `s` may be a bare id (internal callers) or a session object (the menu,
    // which needs the title for the confirmation copy).
    const id = typeof s === "string" ? s : s.id;
    const name = typeof s === "string" ? "this conversation" : s.title || "this conversation";
    if (opts && opts.confirm && typeof window.confirmModal === "function") {
      const ok = await window.confirmModal({
        title: "Delete conversation?",
        message: `"${name}" will be permanently deleted.`,
        confirmText: "Delete",
        danger: true,
      });
      if (!ok) return;
    }
    try {
      await api(`/api/chat/sessions/${id}`, { method: "DELETE" });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load();
  }

  async function load() {
    try {
      const sessions = await api("/api/chat/sessions");
      // A retry that succeeds must clear a failed state left over from an
      // earlier attempt — otherwise a transient blip stays on screen forever.
      if (failedEl) failedEl.hidden = true;
      render(Array.isArray(sessions) ? sessions : []);
    } catch (_) {
      // TCRD-207 (DES-153): the fetch did NOT complete — a distinct FAILED
      // state, never the EMPTY one. The conversations may well still be
      // there; this page just couldn't confirm it, which used to read as
      // "you have no conversations" (indistinguishable from an account that
      // genuinely has none, or one that lost access to the list).
      if (emptyEl) emptyEl.hidden = true;
      if (failedEl) failedEl.hidden = false;
    }
  }

  if (failedEl) {
    const retryBtn = failedEl.querySelector("[data-state-retry]");
    if (retryBtn) retryBtn.addEventListener("click", load);
  }

  load();
})();
