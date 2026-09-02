/* =====================================================================
 * chat_row_menu.js — the "⋮" overflow menu on a conversation row.
 *
 * The history list has TWO renderers (chat.js on /chat, rail_history.js
 * everywhere else — see rail_history.js's header for why), and a THIRD surface
 * lists the same conversations as a table (chats_page.js on /chats). Row actions
 * used to be inline icon buttons, which meant every new action cost horizontal
 * space in a ~250px rail and had to be duplicated per renderer. This is the
 * shared owner instead: every caller asks for a trigger button and hands over
 * callbacks. Actions are Pin/Unpin, Rename, Delete — plus, where the caller
 * supplies the handler, Archive or Restore.
 *
 * Exposes ONE global so a classic script and an ES module can both use it:
 *
 *   window.chatRowMenu.trigger({ session, onPin, onRename, onDelete,
 *                                onArchive?, onRestore? })
 *       -> HTMLButtonElement to append to the row
 *   window.chatRowMenu.close()   -> close whatever is open (renderers call
 *                                   this before wiping the list)
 *
 * `onArchive` / `onRestore` are OPTIONAL and mutually exclusive per row: the
 * menu offers Archive on a live conversation and Restore on an archived one,
 * and offers neither where the caller passes neither (the rail and the chat page
 * have no surface that lists archived rows, so archiving there would put a
 * conversation somewhere the caller cannot see it again).
 *
 * Pin follows the same shape: an ARCHIVED row offers NO pin item at all. Pinned
 * and archived are contradictory states — a pin means "keep this at the top of
 * my list", archiving means "this is not in my list" — so the state cannot
 * exist: `ChatRepository.archive_session` clears `pinned_at`, the pin endpoint
 * refuses a pin on an archived row (409), and /chats never presents an archived
 * row as pinned. There is therefore nothing here to offer in either direction,
 * and no Unpin branch to keep for legacy rows: the read side normalises them.
 * `s.archived` is undefined for the rail and the chat page, which list no
 * archived rows, so this is a no-op there.
 *
 * Contract: call it from render/event code, never at top-level parse time —
 * same rule _app_scripts.html documents for confirmModal. Both loaders use
 * `defer`, so by the time a row is built (after an async sessions fetch) this
 * is defined regardless of which script tag came first.
 *
 * Structure follows the house row-menu pattern (library.html's `lib-movemenu`):
 * a single body-appended div[role=menu] reused across rows, positioned under
 * the trigger and clamped into the viewport, arrow-key navigable, Escape and
 * outside-click close and return focus to the trigger. Body-appended rather
 * than nested in the row because the rail's history body is an
 * `overflow: hidden` scroll container — a nested menu would be clipped.
 * ===================================================================== */
(function () {
  "use strict";

  // Idempotent: loaded from both _app_rail.html and chat.html, and on a rail
  // /chat page BOTH tags are present. Second execution must not re-register the
  // document-level listeners below (every keystroke would be handled twice).
  if (window.chatRowMenu) return;

  const KEBAB_SVG =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/>' +
    "</svg>";

  // Leading glyphs, one per action — the house menus (detail-page's overflow
  // menu, the Library move menu) all label their rows with one, and a menu
  // without them reads as a different component. Line icons at the same
  // stroke weight as macros/_detail.html's `ico()` set, which is where the
  // pencil and bin come from; the pushpin matches the row's own pin flag.
  const ITEM_SVG = {
    pin:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 17v5"/>' +
      '<path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8' +
      'a2 2 0 0 0-1.1-1.7l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>' +
      "</svg>",
    rename:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>' +
      "</svg>",
    delete:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12"/>' +
      "</svg>",
    // A box with a lid — the "put it away" glyph, deliberately nothing like the
    // bin above it: Archive is reversible and Delete is not, so the two must not
    // read as variations of the same action.
    archive:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 8h16M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M4 8V5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v3M10 13h4"/>' +
      "</svg>",
    // The same box, with the arrow coming back out of it.
    restore:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 8h16M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M4 8V5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v3"/>' +
      '<path d="M12 17v-5m0 0-2 2m2-2 2 2"/>' +
      "</svg>",
  };

  let menu = null; // the single reused panel
  let openTrigger = null; // the ⋮ button it currently belongs to

  function ensureMenu() {
    if (menu) return menu;
    menu = document.createElement("div");
    menu.className = "chat-rowmenu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    document.body.appendChild(menu);
    return menu;
  }

  function close(restoreFocus) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    menu.innerHTML = "";
    if (openTrigger) {
      openTrigger.setAttribute("aria-expanded", "false");
      // Only pull focus back when the user closed the menu deliberately
      // (Escape / re-click). After an action runs the list re-renders and this
      // trigger is a detached node — focusing it would drop focus to <body>.
      if (restoreFocus && openTrigger.isConnected) openTrigger.focus();
    }
    openTrigger = null;
  }

  function items() {
    return menu ? Array.from(menu.querySelectorAll(".chat-rowmenu__item")) : [];
  }

  function open(trigger, actions) {
    // Re-clicking the open trigger toggles it shut (menu-button convention).
    if (openTrigger === trigger) {
      close(true);
      return;
    }
    close(false);
    const el = ensureMenu();

    for (const action of actions) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chat-rowmenu__item";
      if (action.danger) item.classList.add("is-danger");
      item.setAttribute("role", "menuitem");
      item.dataset.action = action.id;

      // Static markup from ITEM_SVG above — a fixed string, no interpolation,
      // so no untrusted input reaches the HTML parser here.
      const glyph = ITEM_SVG[action.id];
      if (glyph) item.insertAdjacentHTML("afterbegin", glyph);

      const label = document.createElement("span");
      label.className = "chat-rowmenu__label";
      label.textContent = action.label;
      item.appendChild(label);

      item.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        // Close BEFORE running: the action re-renders the list (and may open a
        // modal), and a menu still anchored to a row that no longer exists
        // would hang over the new one.
        close(false);
        Promise.resolve(action.run()).catch(() => {
          /* callers surface their own errors (toast on /chat, silent on the rail) */
        });
      });
      el.appendChild(item);
    }

    // Position under the trigger, clamped into the viewport — and flipped above
    // it when a row near the bottom of a tall rail would push the panel off
    // screen. Fixed positioning (not page coords): the rail is itself a scroll
    // container, so a page-scroll offset would misplace it.
    el.hidden = false;
    const r = trigger.getBoundingClientRect();
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const below = r.bottom + 6;
    const flip = below + h > window.innerHeight - 8 && r.top - h - 6 > 8;
    el.style.top = (flip ? r.top - h - 6 : below) + "px";
    el.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + "px";

    openTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    const first = items()[0];
    if (first) first.focus();
  }

  /** Build the ⋮ button for one row. `session` is the row's session object
   *  (`{id, title, pinned, archived}`); the callbacks do the actual work and own
   *  their own error reporting + re-render. */
  function makeTrigger(opts) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-rowmenu-btn";
    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-expanded", "false");
    btn.title = "More actions";
    btn.innerHTML = KEBAB_SVG;

    // Read `opts.session` at OPEN time, never at build time. The two rail
    // renderers rebuild every row after every action, so a snapshot worked for
    // them; /chats updates a row IN PLACE (a reload there would throw away the
    // search + filters the caller used to find it), so a snapshot would leave
    // the menu offering "Pin" on a row it had just pinned.
    const state = () => opts.session || {};
    const label = () => state().title || "this conversation";
    btn.setAttribute("aria-label", `More actions for ${label()}`);

    function buildActions() {
      const s = state();
      const actions = [];
      // No pin item on an archived row, in either direction — see the note at
      // the top of this file.
      if (!s.archived) {
        actions.push({ id: "pin", label: s.pinned ? "Unpin" : "Pin", run: () => opts.onPin(!s.pinned) });
      }
      actions.push({ id: "rename", label: "Rename", run: () => opts.onRename() });
      // Archive / Restore sits between Rename and Delete — after the harmless
      // actions, before the irreversible one, and next to the action it is most
      // likely to be mistaken for. Which of the two shows follows the row's own
      // state, so one wiring covers a conversation on both sides of archiving.
      if (s.archived && opts.onRestore) {
        actions.push({ id: "restore", label: "Restore", run: () => opts.onRestore() });
      } else if (!s.archived && opts.onArchive) {
        actions.push({ id: "archive", label: "Archive", run: () => opts.onArchive() });
      }
      actions.push({ id: "delete", label: "Delete", danger: true, run: () => opts.onDelete() });
      return actions;
    }

    btn.addEventListener("click", (e) => {
      // The row itself is a button (opens/navigates to the conversation) —
      // without this the menu would open and the row would fire too.
      e.preventDefault();
      e.stopPropagation();
      // Re-stamped from the live row: a renamed conversation must not keep
      // announcing its old name to a screen reader.
      btn.setAttribute("aria-label", `More actions for ${label()}`);
      open(btn, buildActions());
    });
    // Same reason, for the row's own Enter/Space keydown handler.
    btn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") e.stopPropagation();
    });
    return btn;
  }

  // ---- Document-level handlers (registered once) ------------------------
  document.addEventListener("click", (e) => {
    if (menu && !menu.hidden && !menu.contains(e.target) && e.target !== openTrigger) {
      close(false);
    }
  });

  // Any scroll of an ancestor (the rail's history body, or the page) moves the
  // row out from under a fixed-position panel. Cheaper and less surprising to
  // close than to re-anchor mid-scroll. Capture phase: scroll doesn't bubble.
  window.addEventListener("scroll", () => close(false), true);
  window.addEventListener("resize", () => close(false));

  document.addEventListener("keydown", (e) => {
    if (!menu || menu.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const list = items();
      if (!list.length) return;
      e.preventDefault();
      const at = list.indexOf(document.activeElement);
      const step = e.key === "ArrowDown" ? 1 : -1;
      // -1 (focus elsewhere) + a down-step lands on 0, which is what we want.
      list[(at + step + list.length) % list.length].focus();
      return;
    }
    if (e.key === "Home" || e.key === "End") {
      const list = items();
      if (!list.length) return;
      e.preventDefault();
      (e.key === "Home" ? list[0] : list[list.length - 1]).focus();
    }
    // No single-letter accelerators: the P / R / D bindings this menu used to
    // advertise were removed with the hints that advertised them. They keyed off
    // `event.key`, which is the CHARACTER the layout produces — on any
    // non-Latin layout the same physical keys emit different characters and the
    // shortcut silently did nothing, so the panel promised three bindings it
    // could not honour. Arrow / Home / End / Escape above are layout-independent
    // and stay.
  });

  window.chatRowMenu = { trigger: makeTrigger, close: () => close(false) };
})();
