/* =====================================================================
 * ds_dropdown.js — the `.ds-dropdown` custom select-replacement (#1055).
 *
 * Generalizes the chat composer's "+" upload menu (chat.js `plusBtn` /
 * `plusMenu`: aria-expanded toggling, role="menu" + arrow-key nav, Enter/
 * Space to activate, Esc + outside-click to close) into a reusable
 * component. See `_components.html`'s `dropdown` macro for the markup and
 * `ds_dropdown.css` for the look.
 *
 * A dropdown paired to a native <select> (`data-ds-dropdown-target` on the
 * wrapper) sets `.value` on that select and dispatches a `change` event on
 * it when an item is chosen, so existing listeners on the select keep
 * working unchanged regardless of which UI (native or custom) is visible.
 *
 * Self-bootstraps on every `.ds-dropdown` present at load — no explicit
 * init call needed, same contract as chip-input.js.
 *
 * `window.dsDropdownInit(host)` covers the one case load-time bootstrap
 * can't: markup built entirely client-side from an async fetch (fixed
 * option list, just not present in the DOM yet at DOMContentLoaded — see
 * admin_server_config.html). It's the same internal `init` bootstrapAll()
 * uses, exported rather than duplicated — call it once per host, right
 * after that host is inserted. init() still isn't idempotent for the
 * per-element listeners (each call adds another button/item/menu handler on
 * the SAME element), so a host bootstrapAll() already caught must not be
 * passed here too — but re-initing a REBUILT host is safe and expected, and
 * no longer grows the document-level handler set: Esc and outside-click live
 * once at module scope and resolve the open menu at event time.
 * ===================================================================== */
(function () {
  "use strict";

  function init(host) {
    const btn = host.querySelector(".ds-dropdown-btn");
    const menu = host.querySelector(".ds-dropdown-menu");
    if (!btn || !menu) return;
    const label = btn.querySelector(".ds-dropdown-btn-label");
    const items = Array.from(menu.querySelectorAll('[role="menuitemradio"]'));
    const targetId = host.getAttribute("data-ds-dropdown-target");
    const target = targetId ? document.getElementById(targetId) : null;

    function isOpen() {
      return !menu.hidden;
    }

    function close(restoreFocus) {
      if (!isOpen()) return;
      menu.hidden = true;
      btn.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
      if (restoreFocus) btn.focus();
    }

    function open() {
      if (isOpen()) return;
      menu.hidden = false;
      btn.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      const checked = items.find((i) => i.getAttribute("aria-checked") === "true");
      (checked || items[0] || btn).focus();
    }

    function selectItem(item) {
      const value = item.getAttribute("data-value");
      items.forEach((i) => {
        const selected = i === item;
        i.classList.toggle("is-selected", selected);
        i.setAttribute("aria-checked", selected ? "true" : "false");
      });
      if (label) label.textContent = item.textContent.trim();
      if (target) {
        target.value = value;
        target.dispatchEvent(new Event("change", { bubbles: true }));
      }
      host.dispatchEvent(new CustomEvent("ds-dropdown-change", { detail: { value: value }, bubbles: true }));
      close(true);
    }

    btn.addEventListener("click", () => {
      if (isOpen()) {
        close(false);
      } else {
        open();
      }
    });

    items.forEach((item) => {
      item.addEventListener("click", () => selectItem(item));
    });

    menu.addEventListener("keydown", (e) => {
      const idx = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (idx < items.length - 1) items[idx + 1].focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (idx > 0) items[idx - 1].focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (items[idx]) selectItem(items[idx]);
      } else if (e.key === "Escape") {
        // Stop here so an enclosing dialog's own Escape handler (bubble-
        // phase, listening on document) doesn't ALSO see this keypress and
        // close itself on the same press that was meant to just close this
        // menu — innermost-first dismissal.
        e.stopPropagation();
        close(true);
      }
    });

    // Esc and outside-click are handled ONCE at module scope, not per host —
    // see `_closers` below. `init` is called again for every host a page
    // rebuilds (admin_server_config.html re-renders all its sections on every
    // save), and a per-host `document.addEventListener` pair has no teardown,
    // so each save added two permanent document listeners per dropdown and
    // pinned the detached DOM they closed over. Registering the closer instead
    // makes re-init idempotent for the global handlers: the WeakMap entry is
    // overwritten and the old host becomes collectable.
    _closers.set(host, close);
  }

  /** host element -> its `close(restoreFocus)`. Weak so a host removed from
   *  the DOM does not keep its closure alive. */
  const _closers = new WeakMap();

  function _closeOpenMenusOutside(target, restoreFocus) {
    // Every open menu whose host does not contain the event target, which is
    // exactly what the per-host listeners collectively did. In practice at
    // most one is open, but a page that opens a second without closing the
    // first must not end up with an undismissable menu.
    document.querySelectorAll(".ds-dropdown-menu:not([hidden])").forEach((menu) => {
      const host = menu.closest(".ds-dropdown");
      if (!host || (target && host.contains(target))) return;
      const close = _closers.get(host);
      if (close) close(restoreFocus);
    });
  }

  document.addEventListener("keydown", (e) => {
    // Esc closes the open menu wherever focus is. No `target` filter: the menu
    // owns the key while it is open, and its own keydown handler stops
    // propagation for the in-menu case before this ever runs.
    if (e.key === "Escape") _closeOpenMenusOutside(null, true);
  });
  document.addEventListener("click", (e) => {
    _closeOpenMenusOutside(e.target, false);
  });

  function bootstrapAll() {
    document.querySelectorAll(".ds-dropdown").forEach(init);
  }

  window.dsDropdownInit = init;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapAll);
  } else {
    bootstrapAll();
  }
})();
