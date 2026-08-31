/* Skip / restore for the admin setup chain in the rail.
 *
 * The chain (`#railSetupChain`, `_app_rail.html`) is the admin counterpart of
 * the analyst onboarding card, and this file gives it the pair of controls the
 * analyst card already has: a quiet "hide" in the panel, and a way back in the
 * profile menu once hidden.
 *
 * Why it is not chat_onboarding.js's job. That module owns the analyst journey:
 * six per-user booleans behind /api/chat/journey, where "Skip onboarding" means
 * WRITE ALL SIX TRUE and the card then retires because it is genuinely
 * finished. This chain has no such state to write. Its six steps are readings
 * of the INSTANCE — is a source connected, is every table in a package, has
 * anyone been invited — recomputed server-side on every render
 * (`resolve_setup_rail`), so there is no flag whose flip would make them read
 * done, and inventing one would be a checklist lying about the instance.
 *
 * So "hide" here means exactly that, and the dismissal is a per-BROWSER
 * preference in localStorage — the same shelf the rail's collapse state and
 * chat_onboarding.js's nudge dismissal use. Two consequences worth stating,
 * because both are deliberate:
 *
 *   · It does not follow the admin to another browser. Acceptable: the thing
 *     being remembered is "I have read this list", not an instance fact, and
 *     the instance facts are all still on /admin where the work is.
 *   · Storage being unavailable (private mode, a cleared profile) fails toward
 *     SHOWING the chain, never toward hiding it. A setup checklist that
 *     reappears is a small annoyance; one that vanishes because a write failed
 *     silently is a half-configured instance nobody is being told about.
 *
 * The no-flash half of this lives in `_app_rail.html`: a synchronous inline
 * script reads the same key before the card paints and sets the same attribute
 * this file toggles. This module is `defer`red and only handles the clicks, so
 * a dismissed card is never briefly visible on a page load.
 */
(function () {
  "use strict";

  var KEY = "agnes.setupchain.skipped";
  var ATTR = "data-setupchain-skipped";

  // Every storage touch is wrapped: `localStorage` THROWS on access (not just
  // returns null) in a Safari private window and under a "block all site data"
  // policy, and an exception here would take the rest of the rail's scripts
  // with it.
  function store(skipped) {
    try {
      if (skipped) localStorage.setItem(KEY, "1");
      else localStorage.removeItem(KEY);
    } catch (e) {
      /* Unwritable storage: the attribute below still applies for THIS page, so
         the click is not ignored — it just will not survive a reload, which is
         the honest outcome and the safe direction (the chain comes back). */
    }
  }

  function apply(skipped) {
    if (skipped) document.documentElement.setAttribute(ATTR, "1");
    else document.documentElement.removeAttribute(ATTR);
  }

  function toast(msg) {
    if (window.showToast) window.showToast(msg, { type: "success" });
  }

  // ── The panel's open/close behaviour ────────────────────────────────────
  // The chain reuses `.rail-getstarted` for its look but carries its own ids,
  // which is exactly why it needs this call: the analyst card's click / Escape
  // / outside-click wiring is bound by id in rail_history.js, so the chain had
  // hover-reveal and nothing else — previewable, never openable, never
  // collapsible. Same function, same behaviour, both cards.
  var card = document.getElementById("railSetupChain");
  var toggle = document.getElementById("rail-setupchain-toggle");
  var setChainOpen = window.railPopover
    ? window.railPopover.wire(card, toggle, null)
    : null;

  // ── Hide ────────────────────────────────────────────────────────────────
  // Says where it went, for the same reason the analyst card's skip does: the
  // click removes the card, and the route back is a profile-menu entry the
  // reader has had no reason to notice (it is hidden until this very moment).
  var skipBtn = document.querySelector("[data-setupchain-skip]");
  if (skipBtn) {
    skipBtn.addEventListener("click", function () {
      store(true);
      apply(true);
      toast("Setup checklist hidden — bring it back from your profile menu");
    });
  }

  // ── The other half of the switch ────────────────────────────────────────
  // "Start over onboarding" (chat_onboarding.js) resets the ANALYST journey,
  // and for an admin mid-chain that card is the slot's standby occupant — so
  // the reset landed and nothing appeared. Setting the same flag the hide
  // button sets is what reveals it: chain out, analyst row in, one card in the
  // slot throughout.
  //
  // A second listener on that button rather than a branch inside
  // chat_onboarding.js, because the flag is this module's state and that module
  // does not load on every page this one does. Both handlers fire — the other
  // one's `stopPropagation` stops the event bubbling, not its siblings — and it
  // then pins the analyst panel open, which now finds a card to open.
  var restartOnboarding = document.getElementById("rail-restart-onboarding");
  var standby = document.querySelector(".rail-getstarted[data-chain-alternate]");
  if (restartOnboarding && standby) {
    restartOnboarding.addEventListener("click", function () {
      store(true);
      apply(true);
    });
  }

  // ── Restore ─────────────────────────────────────────────────────────────
  // Mirrors chat_onboarding.js's "Start over onboarding": close the menu the
  // caller is looking at, then pin the card's panel OPEN, so the checklist they
  // were told exists is on screen rather than merely un-hidden somewhere below.
  var restoreBtn = document.getElementById("rail-restart-setupchain");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      store(false);
      apply(false);

      var panel = document.getElementById("userMenuPanel");
      var trigger = document.getElementById("userMenuTrigger");
      if (panel) panel.setAttribute("hidden", "");
      if (trigger) trigger.setAttribute("aria-expanded", "false");

      // Pin the panel open through the popover's OWN setter, not by setting
      // `.is-open` here: that path also clears `.is-closed` (which otherwise
      // beats the CSS reveal and leaves the panel invisible) and moves
      // aria-expanded with it. Setting the class by hand is how the card ended
      // up pinned open with no way to collapse it again.
      if (setChainOpen) setChainOpen(true);
      toast("Setup checklist restored");
    });
  }
})();
