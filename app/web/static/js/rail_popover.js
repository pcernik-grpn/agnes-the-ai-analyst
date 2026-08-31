/* One popover interaction, shared by both of the rail's launcher cards.
 *
 * `.rail-getstarted` reveals its panel on CSS :hover, and that alone leaves a
 * click user with a dead button — so the card also opens on CLICK and stays
 * pinned, closes on outside-click and Escape, and suppresses its own hover
 * reveal while it is deliberately closed. That behaviour lived inline in
 * rail_history.js, bound by id to the analyst onboarding card.
 *
 * Which is how the admin setup chain came to have none of it. The chain reuses
 * `.rail-getstarted` wholesale for its LOOK — launcher, ring, panel, icon-mode
 * label hiding — but carries its own ids (`#railSetupChain`,
 * `#rail-setupchain-toggle`) so chat_onboarding.js cannot write analyst numbers
 * into it. Those distinct ids also took it out of the only wiring that made the
 * panel clickable, so the chain could be previewed on hover and never opened,
 * pinned or collapsed. Two cards that are the same object visually behaved
 * differently, and the second one worse.
 *
 * So the behaviour moves here and both cards call it. Not a base class and not
 * a framework — one function taking the two elements and an optional hook for
 * what a card wants to do as it opens (the analyst card re-reads its journey;
 * the chain is server-rendered and needs nothing).
 *
 * Loaded before rail_history.js and rail_setupchain.js, both of which are
 * `defer`red and therefore run in document order.
 */
(function () {
  "use strict";

  window.railPopover = {
    /* wrap: the `.rail-getstarted` element. toggle: its launcher button.
     * onOpen: optional, called with no arguments just before the panel is
     * revealed. Returns false and does nothing when either element is absent,
     * so a caller can wire a card that this page did not render. */
    wire: function (wrap, toggle, onOpen) {
      if (!wrap || !toggle) return false;

      function setOpen(open) {
        if (open && typeof onOpen === "function") onOpen();
        wrap.classList.toggle("is-open", open);
        // `.is-closed` (rail.css) is the ONLY thing that can override the
        // panel's CSS :hover / :focus-within reveal. Without it, closing has
        // no visible effect while the cursor is still over the launcher —
        // exactly when a click-to-close fires — or while the toggle itself
        // holds focus, since it is a descendant of the wrapper and so keeps
        // :focus-within true after Escape moves focus back to it below.
        wrap.classList.toggle("is-closed", !open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      }

      toggle.addEventListener("click", function (e) {
        e.stopPropagation();
        setOpen(!wrap.classList.contains("is-open"));
      });

      document.addEventListener("click", function (e) {
        if (wrap.classList.contains("is-open") && !wrap.contains(e.target)) setOpen(false);
      });

      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && wrap.classList.contains("is-open")) {
          setOpen(false);
          toggle.focus();
        }
      });

      // Once the cursor genuinely leaves the launcher, lift the suppression so
      // a later hover can preview the panel again — `.is-closed` exists to stop
      // the SAME hover session from reopening what was just closed, not to
      // disable hover-to-preview for good.
      //
      // …but only once nothing inside still holds focus. The toggle keeps DOM
      // focus after a click-to-close, and Escape explicitly refocuses it, so
      // lifting `.is-closed` while :focus-within is still true would let the
      // CSS reveal reopen the panel the instant the cursor leaves.
      wrap.addEventListener("mouseleave", function () {
        if (!wrap.contains(document.activeElement)) wrap.classList.remove("is-closed");
      });

      // Parked on the element as well as returned, so the collapse chevrons
      // below can close whichever card they are inside without every caller
      // having to thread its setter through. Same function either way.
      wrap._railPopoverSetOpen = setOpen;

      // Handed back so a caller can pin the panel open later (the profile
      // menu's restore entry does) through the same path a click takes,
      // rather than by setting `.is-open` on its own and stranding
      // `.is-closed` / aria-expanded out of step with it.
      return setOpen;
    },

    /* Close the card containing `node`, if it is inside a wired one. */
    close: function (node) {
      var wrap = node && node.closest ? node.closest(".rail-getstarted") : null;
      if (wrap && typeof wrap._railPopoverSetOpen === "function") {
        wrap._railPopoverSetOpen(false);
        return true;
      }
      return false;
    },
  };

  /* ── The collapse chevron ────────────────────────────────────────────────
   * Both panels carry a `[data-rail-popover-collapse]` button in their top
   * right. The panel already closed four other ways — a second click on the
   * launcher, click-away, Escape, mouse-leave — but every one of them is
   * something you have to already know; none of them is visible. The chevron
   * is the affordance that says the thing you are looking at can be put away,
   * which is the one piece the card was missing.
   *
   * DELEGATED, not bound per button, and that is load-bearing rather than
   * tidiness: chat_onboarding.js rebuilds the analyst panel's innerHTML on
   * every journey update, so a handler attached to the button would be thrown
   * away with the button the first time a step completed — the chevron would
   * work until the moment the user did something, then quietly stop. A
   * listener on the document survives every re-render.
   */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-rail-popover-collapse]");
    if (!btn) return;
    // Stop the outside-click handler above from seeing this too — it would
    // close the same card a second time, which is harmless, and would also
    // fire for a card that is not this one, which is not.
    e.stopPropagation();
    window.railPopover.close(btn);
  });
})();
