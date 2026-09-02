// rail_toggle.js — the ONE control that collapses/expands the global rail
// (rail ui_layout), on every rail page, admin included.
//
// The width itself is a PERSISTED USER PREFERENCE (localStorage
// `agnes.rail.collapsed`), not derived from the request path — see the
// inline bootstrap script at the top of `<nav>` in _app_rail.html, which
// applies a stored value before first paint (this file loads `defer`, too
// late to avoid a flash on its own). This file wires what CSS can't do by
// itself:
//
//   - the `#rail-toggle` click, which PERSISTS whichever state it lands on
//     (collapsed <-> expanded) rather than merely peeking it — hover and
//     keyboard `:focus-within` already peek the collapsed rail open via CSS
//     alone (rail.css);
//   - the "Pin navigation open" label while the collapsed rail is merely
//     peeking (hover/focus, not yet clicked) — the click does the SAME
//     thing either way (persist "expanded"), only the label differs, so a
//     caller reading it while peeking understands what the click commits to;
//   - the Cmd/Ctrl+\ keyboard shortcut (VS Code / Slack convention), guarded
//     against firing while focus is in a text input;
//   - `.rail-strip-only`, reconciled against the DOM on load — the peek is off
//     entirely on a page that carries the admin secondary nav, and only the DOM
//     knows for certain that it does (see the note below).
//
// Self-guards on the rail being present, so loading it on every rail page
// (not just /admin, unlike its retired predecessor rail_icon_mode.js) is a
// belt-and-braces no-op on a page with no session.user (no rail rendered).
(function () {
  "use strict";

  const rail = document.querySelector(".rail");
  if (!rail) return;

  // Does this page carry the ADMIN SECONDARY NAV? If it does, the collapsed
  // rail must not peek open at all: the peek is a 240px overlay and that column
  // is 200px wide starting at the rail's own edge, so opening it erased the
  // navigation in use (#1956 item 6). `.rail-strip-only` turns both triggers
  // off in rail.css — hover AND focus, the latter because the search box is
  // focused by CLICKING it, which put the mouse back on the peek. What replaces
  // it: a hovered or focused row names itself with a label chip, and the
  // onboarding panel and search field fly out beside the strip.
  //
  // _app_rail.html renders the class from the REQUEST PATH so it is right
  // before first paint — a hover must never wait on this file to know whether
  // it may peek. That is a guess, though: an /admin route whose feature flag
  // is off renders the plain error page, and any page may change base without
  // anyone remembering this line. The DOM is the actual answer, and by the
  // time a deferred script runs it exists — so reconcile in BOTH directions
  // rather than only adding, or a stale server guess outlives the page it was
  // made for.
  rail.classList.toggle("rail-strip-only", !!document.querySelector(".admin-nav"));

  // Per-context memory, matching the pre-paint bootstrap in _app_rail.html:
  // `admin` pages remember their own width separately from the rest of the
  // app, so collapsing/expanding on /chat never re-opens the second full-width
  // nav column on /admin (and never forces a re-collapse on arrival there).
  // Keep this key derivation identical to the bootstrap's — they must agree,
  // or a stored preference would be written under one name and read under
  // another.
  const KEY = "agnes.rail.collapsed." + (rail.getAttribute("data-rail-context") || "app");
  const toggle = document.getElementById("rail-toggle");

  function isCollapsed() {
    return rail.classList.contains("rail-icon-mode");
  }

  // Label reflects the PERSISTED state; peeking (hover/focus while
  // collapsed, not yet clicked) borrows the same aria-expanded but swaps
  // the label to name what a click would commit to.
  function syncLabel(peeking) {
    if (!toggle) return;
    if (isCollapsed()) {
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-label", peeking ? "Pin navigation open" : "Expand navigation");
    } else {
      toggle.setAttribute("aria-expanded", "true");
      toggle.setAttribute("aria-label", "Collapse navigation");
    }
  }

  // `.rail-no-peek` suppresses the CSS peek (every peek rule in rail.css is
  // gated on `:not(.rail-no-peek)`) for as long as the pointer or focus that
  // just collapsed the rail is still inside it.
  //
  // Without it a collapse landed in a visibly BROKEN state rather than merely
  // an un-animated one: the class flips, so `body`'s clearance drops to 56px
  // immediately, but `.rail:hover` / `:focus-within` still match — so the rail
  // stayed 240px wide ON TOP of a page that had already reflowed 184px left,
  // and nothing looked collapsed at all until the caller happened to move the
  // pointer off it. Measured on Cmd+\ with the pointer resting on the rail:
  // body padding 56px, rail width 240px.
  //
  // Releasing is what makes this a suppression and not a second state: the
  // moment the pointer is genuinely outside the collapsed strip — or the
  // caller tabs to another row, where `:focus-within` IS how they read the
  // labels — CSS gets the peek back and the next hover opens it as usual.
  function releasePeek() {
    rail.classList.remove("rail-no-peek");
    document.removeEventListener("pointermove", releasePeekIfPointerOutside, true);
  }

  // `mouseleave` is the ordinary way out, but it cannot be the only one: a
  // collapse SHRINKS the rail out from under a stationary pointer, and whether
  // that alone re-fires the mouse events is engine-dependent. This backstop
  // reads the geometry instead, so a suppression can never outlive the pointer
  // that caused it and strand the rail un-peekable.
  function releasePeekIfPointerOutside(e) {
    const box = rail.getBoundingClientRect();
    const inside =
      e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
    if (!inside) releasePeek();
  }

  function suppressPeek() {
    if (rail.classList.contains("rail-no-peek")) return;
    rail.classList.add("rail-no-peek");
    document.addEventListener("pointermove", releasePeekIfPointerOutside, true);
  }

  function setCollapsed(collapsed) {
    rail.classList.toggle("rail-icon-mode", collapsed);
    // Whichever control asked for it — the toggle by pointer, the toggle by
    // Enter/Space, or Cmd+\ — a collapse means "collapse now", so it is
    // suppressed here rather than in any one caller.
    if (collapsed) suppressPeek();
    else releasePeek();
    try {
      localStorage.setItem(KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* private mode / quota — the toggle still works for this page load */
    }
    syncLabel(false);
  }

  // The server-rendered aria attributes reflect the DEFAULT (path-based)
  // class; a stored preference may have overridden that class before this
  // ever painted (see the inline bootstrap script). Re-sync once on load so
  // the button's accessible name always matches reality.
  syncLabel(false);

  if (toggle) {
    toggle.addEventListener("click", (e) => {
      setCollapsed(!isCollapsed());

      // Collapsing with the mouse left the rail visibly EXPANDED until the
      // user clicked somewhere else on the page: the click leaves focus on
      // this button, `.rail:focus-within` is one of the two things that peek
      // the collapsed rail open (rail.css), and it kept matching long after
      // the pointer moved away — so the control appeared not to work.
      //
      // Dropping focus hands the peek back to `:hover` — which `.rail-no-peek`
      // now suppresses in turn (see setCollapsed) for as long as the pointer is
      // still on the rail. Guarded on `detail`: a real pointer click reports
      // >= 1, while a keyboard-activated click (Enter/Space) reports 0, and
      // blurring a keyboard caller would strand them with no focused element.
      if (e.detail !== 0) toggle.blur();
    });
  }

  // Peeking: hovering or focusing into the collapsed rail expands it via
  // CSS alone (rail.css) — this only updates the toggle's label to name the
  // "pin it open" action while that's happening, reverting the instant the
  // pointer/focus leaves.
  rail.addEventListener("mouseenter", () => syncLabel(true));
  rail.addEventListener("mouseleave", () => {
    // The pointer is gone, so there is nothing left to suppress — hand the
    // peek back to CSS so the NEXT hover opens the rail as usual.
    releasePeek();
    syncLabel(false);
  });
  rail.addEventListener("focusin", () => {
    // A caller tabbing into the collapsed rail needs the labels back, whatever
    // the pointer is still resting on — `:focus-within` is the only way they
    // read them. Focus MOVING is the signal, so this can't undo the suppression
    // of the collapse that just happened: a pointer click focuses the toggle
    // before `click` fires, and an Enter/Space collapse leaves focus exactly
    // where it already was.
    releasePeek();
    syncLabel(true);
  });
  rail.addEventListener("focusout", (e) => {
    if (!rail.contains(e.relatedTarget)) syncLabel(false);
  });

  // Cmd (Mac) / Ctrl (everywhere else) + \ — the VS Code / Slack sidebar
  // convention. Never hijacked while the caller is typing.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "\\") return;
    if (!(e.metaKey || e.ctrlKey)) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (t && (t.isContentEditable || tag === "INPUT" || tag === "TEXTAREA")) return;
    e.preventDefault();
    setCollapsed(!isCollapsed());
  });
})();
