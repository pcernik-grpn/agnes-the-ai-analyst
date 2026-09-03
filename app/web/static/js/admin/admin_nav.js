// admin_nav.js — per-section disclosure for the /admin sidebar
// (`_admin_nav.html`), persisted per browser in localStorage.
//
// Server-side rendering already gets FIRST PAINT right — see the comment
// block at the top of `_admin_nav.html`: the active section's body has no
// `hidden` attribute and its header's `aria-expanded` is already "true";
// every other section is already collapsed.
//
// This file's job is the one thing that HTML response could not have known: a
// caller's own manually-opened/closed sections, which may disagree with the
// server's "just the active one" default.
//
// The whole-sidebar collapse this file used to own (a 56px icon strip with
// hover flyouts, the `agnes.adminNav.collapsed` key, and an inline
// first-paint script inside the aside) is gone — the column does not collapse
// any more. The primary rail beside it owns that behaviour for the window.
(function () {
  "use strict";

  const nav = document.querySelector("[data-admin-nav]");
  if (!nav) return;

  const ACTIVE_KEY = nav.dataset.activeSection || "";
  const SEC_STORAGE_KEY = "agnes.adminNav.sections";
  const SCROLL_STORAGE_KEY = "agnes.adminNav.scrollTop";

  function readSectionState() {
    try {
      const raw = localStorage.getItem(SEC_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeSectionState(state) {
    try {
      localStorage.setItem(SEC_STORAGE_KEY, JSON.stringify(state));
    } catch (_) {
      /* private mode / quota — toggling still works for this page load */
    }
  }

  function setGroupOpen(group, open) {
    const toggle = group.querySelector("[data-admin-nav-toggle]");
    const body = group.querySelector(".admin-nav__group-body");
    if (!toggle || !body) return;
    body.hidden = !open;
    group.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  const groups = nav.querySelectorAll("[data-admin-nav-group]");
  const storedSections = readSectionState();

  groups.forEach((group) => {
    const key = group.dataset.adminNavGroup;

    // A stored preference wins over the server default — EXCEPT the active
    // section, which must never end up hidden regardless of what a previous
    // visit stored (the caller is standing in it right now).
    if (
      key !== ACTIVE_KEY &&
      Object.prototype.hasOwnProperty.call(storedSections, key)
    ) {
      setGroupOpen(group, !!storedSections[key]);
    }

    const toggle = group.querySelector("[data-admin-nav-toggle]");
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const body = group.querySelector(".admin-nav__group-body");
      const willOpen = !!(body && body.hidden);
      setGroupOpen(group, willOpen);
      const next = readSectionState();
      next[key] = willOpen;
      writeSectionState(next);
    });
  });

  // ── The column keeps its scroll position across a navigation ──────────
  // `.admin-nav__sections` is its own scroll container (admin-nav.css: it
  // takes the column's free space and scrolls inside it), and every click in
  // it is a full page load — so the new document's fresh element started at
  // scrollTop 0. Clicking Adoption, at the bottom of the last section, landed
  // you on the page with the column scrolled back to the top and the row you
  // just used off-screen: the sidebar appeared to jump away from where you
  // were working, on every single click.
  //
  // sessionStorage, not localStorage: this is where you are in a column right
  // now, not a preference. It should not survive a new tab or come back a week
  // later, and it is per-tab, so two admin tabs scrolled differently do not
  // fight over one number (the section disclosures above are genuine
  // preferences and stay in localStorage).
  //
  // Restored on `DOMContentLoaded` at the latest — this script is `defer`red,
  // so it runs after the document is parsed and the assignment lands in the
  // same frame the column first paints in, before the browser has shown it.
  const scroller = nav.querySelector(".admin-nav__sections");
  if (scroller) {
    try {
      const saved = parseInt(sessionStorage.getItem(SCROLL_STORAGE_KEY) || "0", 10);
      // Only when it is a real offset AND the column can actually scroll that
      // far: a short viewport, a different set of open sections, or a page
      // whose column is shorter would otherwise clamp silently and store the
      // clamped value back, walking the position toward 0 over a few clicks.
      if (saved > 0 && scroller.scrollHeight > scroller.clientHeight) {
        scroller.scrollTop = saved;
      }
    } catch (_) {
      /* private mode — the column just starts at the top, as it always did */
    }

    // Written on `pagehide`, which unlike `beforeunload` also fires when the
    // page goes into the back/forward cache, so a Back into this page restores
    // the position it left with rather than the one before that.
    const persist = () => {
      try {
        sessionStorage.setItem(SCROLL_STORAGE_KEY, String(scroller.scrollTop));
      } catch (_) {
        /* quota / private mode — nothing to do, the position is cosmetic */
      }
    };
    window.addEventListener("pagehide", persist);
    // `pagehide` is not guaranteed on every platform (notably older mobile
    // Safari kills a backgrounded tab outright), so a click inside the column
    // — the navigation this exists for — persists eagerly too.
    scroller.addEventListener("click", persist);
  }
})();
