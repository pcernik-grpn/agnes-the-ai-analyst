/* =====================================================================
 * chats_page.js — the /chats page (templates/chats.html)
 * =====================================================================
 * The list itself is server-rendered. This file owns four things on top of it:
 *
 *   1. the shared FilterToolbar wiring (search · the four views and the
 *      Agent/Source facets, all behind one Filter button · sort · list ⇄ grid),
 *      including the grid card projection;
 *   2. the row menu — the SAME component the rail and the chat page use
 *      (js/components/chat_row_menu.js), with Archive/Restore added, which only
 *      this page can offer because only this page can list an archived row;
 *   3. multi-select + the bulk bar;
 *   4. relative "Modified" labels.
 *
 * Everything mutating goes through the existing per-session endpoints:
 *   PUT    /api/chat/sessions/{id}/pin        {pinned}
 *   PUT    /api/chat/sessions/{id}/title      {title}
 *   PUT    /api/chat/sessions/{id}/archived   {archived}   (archive + restore)
 *   DELETE /api/chat/sessions/{id}/permanent               (hard delete)
 *
 * After a successful action the row is updated IN PLACE and the toolbar is
 * re-applied — never a page reload. A reload would throw away the search term,
 * the segment and the sort the caller set up to find these rows in the first
 * place, which on a tidy-up surface is most of the work they had done.
 *
 * Loaded with `defer` (not a module) from chats.html.
 * ===================================================================== */
(function () {
  "use strict";

  // The rows live in a light flex list, not a table (see the note in
  // chats.html): the filter engine works over any element set, so search,
  // segments, sort and the grid projection are indifferent to the tag.
  var listEl = document.getElementById("ch-list");
  var toolbar = null;

  // ---- Fetch helper -----------------------------------------------------
  // Mirrors the one in rail_history.js / chat.js: same-origin cookies, and an
  // empty 2xx (the 204 the delete returns) resolves to null rather than throwing
  // inside .json().
  function api(path, init) {
    return fetch(
      path,
      Object.assign(
        {
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
        },
        init || {},
      ),
    ).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      if (r.status === 204 || r.headers.get("content-length") === "0") return null;
      return r.json();
    });
  }

  // One place to tell the caller an action failed. There is no toast surface on
  // this page (the chat page's `toast()` is chat.js's own), and silence — which
  // is what the rail does, having no surface either — is the wrong answer for an
  // action the caller explicitly asked for on a row they are looking at.
  function reportFailure(message) {
    if (typeof window.alertModal === "function") {
      window.alertModal({ title: "That didn't work", message: message });
    } else {
      console.warn("chats: " + message);
    }
  }

  var PIN_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 17v5"/>' +
    '<path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8' +
    'a2 2 0 0 0-1.1-1.7l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>' +
    "</svg>";

  function rows() {
    return listEl ? Array.prototype.slice.call(listEl.querySelectorAll(".ch-row")) : [];
  }
  function sessionOf(row) {
    return {
      id: row.dataset.itemId,
      title: row.dataset.title || "",
      pinned: row.dataset.pinned === "1",
      archived: row.dataset.archived === "1",
    };
  }

  // ---- Row state, written in place -------------------------------------
  // The segment set is DERIVED from the three state flags rather than patched, so
  // there is exactly one rule for which buckets a row belongs to and it cannot
  // drift from the server's (see `_chats_rows` in app/web/router.py). Archived is
  // exclusive: a chat that has been put away should not also sit in Pinned.
  function syncBuckets(row) {
    var archived = row.dataset.archived === "1";
    if (archived) {
      row.dataset.buckets = "archived";
      return;
    }
    var buckets = ["all"];
    if (row.dataset.pinned === "1") buckets.push("pinned");
    if (row.dataset.shared === "1") buckets.push("shared");
    row.dataset.buckets = buckets.join("|");
  }

  function setRowPinned(row, pinned) {
    if (pinned) row.dataset.pinned = "1";
    else delete row.dataset.pinned;
    row.classList.toggle("is-pinned", pinned);
    var lead = row.querySelector(".ch-lead");
    if (lead) {
      var flag = lead.querySelector("[data-pin-flag]");
      if (pinned && !flag) {
        flag = document.createElement("span");
        flag.className = "ch-pinflag";
        flag.setAttribute("data-pin-flag", "");
        // Static markup from the constant above — no interpolation, so nothing
        // untrusted reaches the HTML parser.
        flag.innerHTML = PIN_SVG;
        lead.appendChild(flag);
      } else if (!pinned && flag) {
        flag.remove();
      }
    }
    syncBuckets(row);
  }

  function setRowArchived(row, archived) {
    if (archived) row.dataset.archived = "1";
    else delete row.dataset.archived;
    row.classList.toggle("is-archived", archived);
    var pill = row.querySelector(".ch-pill--archived");
    if (archived && !pill) {
      pill = document.createElement("span");
      pill.className = "ch-pill ch-pill--archived";
      pill.textContent = "Archived";
      // Directly after the title, and before any sharing pill — the server's
      // order: what the row IS, then who else can see it.
      var firstPill = row.querySelector(".ch-pill");
      row.insertBefore(pill, firstPill || row.querySelector(".ch-agent"));
    } else if (!archived && pill) {
      pill.remove();
    }
    syncBuckets(row);
  }

  function setRowTitle(row, title) {
    row.dataset.title = title;
    row.dataset.name = title.toLowerCase();
    var label = row.querySelector(".ch-name-title");
    if (label) label.textContent = title;
    // The search index is "title agent surface", lowercased (see the row markup).
    // Rebuild it from the parts rather than string-replacing the old title out.
    var agent = row.dataset.agentLabel || "";
    var surfacePill = row.querySelector(".ch-pill--surface");
    row.dataset.search = [title, agent, surfacePill ? surfacePill.textContent : ""].join(" ").toLowerCase();
    var link = row.querySelector(".ch-name");
    if (link) link.setAttribute("aria-label", title);
    var check = row.querySelector(".ch-check");
    if (check) check.setAttribute("aria-label", "Select " + title);
  }

  // ---- Actions ---------------------------------------------------------
  function setPinned(row, pinned) {
    return api("/api/chat/sessions/" + encodeURIComponent(row.dataset.itemId) + "/pin", {
      method: "PUT",
      body: JSON.stringify({ pinned: pinned }),
    }).then(function () {
      setRowPinned(row, pinned);
    });
  }

  function setArchived(row, archived) {
    return api("/api/chat/sessions/" + encodeURIComponent(row.dataset.itemId) + "/archived", {
      method: "PUT",
      body: JSON.stringify({ archived: archived }),
    }).then(function () {
      setRowArchived(row, archived);
    });
  }

  function destroy(row) {
    return api("/api/chat/sessions/" + encodeURIComponent(row.dataset.itemId) + "/permanent", {
      method: "DELETE",
    }).then(function () {
      row.remove();
    });
  }

  function renameRow(row) {
    if (typeof window.promptModal !== "function") return Promise.resolve();
    var current = row.dataset.title || "";
    return window.promptModal({
      title: "Rename conversation",
      message: "This is the name shown everywhere this chat is listed.",
      defaultValue: current,
      placeholder: "Conversation name",
      confirmText: "Rename",
    }).then(function (next) {
      // null = cancelled/Escape. An unchanged or blank title is a no-op rather
      // than a request the server would just 400.
      if (next === null) return null;
      var title = next.trim();
      if (!title || title === current) return null;
      return api("/api/chat/sessions/" + encodeURIComponent(row.dataset.itemId) + "/title", {
        method: "PUT",
        body: JSON.stringify({ title: title }),
      }).then(function () {
        setRowTitle(row, title);
      });
    });
  }

  function confirmDelete(count, name) {
    if (typeof window.confirmModal !== "function") return Promise.resolve(true);
    return window.confirmModal({
      title: count === 1 ? "Delete this conversation?" : "Delete " + count + " conversations?",
      message:
        count === 1
          ? '"' + name + '" and every message in it will be permanently deleted. Archive it instead to keep it and take it out of your list.'
          : count +
            " conversations and every message in them will be permanently deleted. Archive them instead to keep them and take them out of your list.",
      confirmText: count === 1 ? "Delete" : "Delete " + count,
      danger: true,
    });
  }

  // Every action funnels through here so the page is left consistent whatever
  // ran: the toolbar re-reads its row set (a delete changed it), the segment
  // badges are recomputed, and the selection drops rows that are gone or have
  // moved out of the current view.
  function afterMutation() {
    if (toolbar) {
      // `refresh` re-reads the row set (a delete changed it) and re-applies the
      // filters; the re-sort is separate and also needed, because `pinFirst`
      // means pinning a row changes where it belongs. Reading the order back off
      // the <select> rather than tracking it here keeps ONE source of truth —
      // the engine syncs that control from its own state.
      toolbar.refresh();
      var sortEl = document.getElementById("ch-sort");
      if (sortEl) toolbar.setSort(sortEl.value);
    }
    updateSegmentCounts();
    syncSelection();
  }

  function runAndSettle(promise) {
    return promise.then(afterMutation, function (err) {
      afterMutation();
      reportFailure("The conversation could not be updated. " + (err && err.message ? err.message : ""));
    });
  }

  // ---- The active view, on the Filter button ---------------------------
  // The four views live inside the Filter menu now, so at rest the bar has to
  // say which one is on — that is the one thing the segmented control it replaced
  // did without being opened. "All" is the default and says nothing, so the
  // resting button stays a plain "Filter"; anything else names itself.
  //
  // Read off the engine's own `.is-active` class rather than tracked here: the
  // engine owns that state and pushes it onto the buttons, so this cannot drift.
  function syncFilterView() {
    var label = document.getElementById("ch-filter-view");
    var btn = document.getElementById("ch-filter-btn");
    if (!label) return;
    var active = document.querySelector("#ch-seg .fbar-seg__btn.is-active");
    var view = active && active.getAttribute("data-own");
    var txt = active ? (active.querySelector(".ch-viewsel__txt") || active).textContent.trim() : "";
    var on = !!view && view !== "all";
    label.textContent = on ? txt : "";
    label.hidden = !on;
    // The button's own is-active is the engine's (it counts applied facets); the
    // view is a second reason to light it up, so OR the two rather than
    // overwriting — clearing a facet must not un-light an active view.
    if (btn && on) btn.classList.add("is-active");
    // aria-checked follows the class for the radio group, since the engine only
    // maintains `aria-selected` on segment buttons.
    document.querySelectorAll("#ch-seg .fbar-seg__btn").forEach(function (b) {
      b.setAttribute("aria-checked", b.classList.contains("is-active") ? "true" : "false");
    });
  }

  // ---- Dock geometry ---------------------------------------------------
  // The Filter menu opens UPWARD from a button in a bottom-anchored card, so its
  // offset has to clear the chips row above that button — otherwise it opens
  // straight into the very state it is about to change. The row's height is not
  // knowable in CSS (it wraps), so publish it as a custom property whenever it
  // appears, disappears or rewraps; 0 while there are no chips.
  function syncDockChips() {
    // Published on the FRAME, not the card: the menus inside the card inherit it
    // either way, but the frosted veil is the card's SIBLING and sizes its band
    // off this value, so a property set on the card would never reach it.
    var frame = document.querySelector(".fbar-dock");
    var chips = document.getElementById("ch-chips");
    if (!frame) return;
    var h = chips && !chips.hidden ? chips.getBoundingClientRect().height : 0;
    frame.style.setProperty("--fbar-dock-chips", h ? Math.round(h) + 10 + "px" : "0px");
  }
  if (typeof ResizeObserver === "function") {
    var chipsEl = document.getElementById("ch-chips");
    if (chipsEl) new ResizeObserver(syncDockChips).observe(chipsEl);
  }

  // ---- Segment badge counts -------------------------------------------
  // The UNFILTERED tally per segment, matching how the Filter menu's category
  // options count. Recomputed from the rows' own bucket sets after any action, so
  // archiving four chats moves four out of All and into Archived immediately.
  function updateSegmentCounts() {
    var counts = { all: 0, pinned: 0, shared: 0, archived: 0 };
    rows().forEach(function (row) {
      (row.dataset.buckets || "").split("|").forEach(function (b) {
        if (b in counts) counts[b] += 1;
      });
    });
    Object.keys(counts).forEach(function (key) {
      var el = document.querySelector('[data-seg-count="' + key + '"]');
      if (el) el.textContent = String(counts[key]);
    });
  }

  // ---- Selection + bulk bar -------------------------------------------
  var bulk = document.getElementById("ch-bulk");
  var bulkN = document.getElementById("ch-bulk-n");
  var selectAll = document.getElementById("ch-select-all");
  var section = document.querySelector(".ch-section");

  // A row is selectable only if it is the caller's own (a shared-with-me row has
  // no checkbox at all) and currently VISIBLE: a selection that survives a filter
  // change invisibly is how a bulk delete takes rows the caller cannot see.
  function selectableRows() {
    return rows().filter(function (r) {
      return !r.hidden && r.dataset.owned === "1";
    });
  }
  function selectedRows() {
    return rows().filter(function (r) {
      return r.classList.contains("is-selected") && !r.hidden;
    });
  }

  // One projection, so one place to write the state — the card mirror this used
  // to keep in step went with the grid view.
  function setRowSelected(row, on) {
    row.classList.toggle("is-selected", on);
    var box = row.querySelector(".ch-check");
    if (box) box.checked = on;
  }

  function clearSelection() {
    rows().forEach(function (r) {
      setRowSelected(r, false);
    });
    syncSelection();
  }

  // The one place that reconciles the bar, the select-all box and any row whose
  // selection has become stale (hidden by a filter, or deleted).
  function syncSelection() {
    rows().forEach(function (r) {
      if (r.classList.contains("is-selected") && (r.hidden || r.dataset.owned !== "1")) {
        setRowSelected(r, false);
      }
    });
    var chosen = selectedRows();
    var n = chosen.length;
    if (bulkN) bulkN.textContent = n + " selected";
    if (bulk) bulk.hidden = n === 0;
    if (section) section.classList.toggle("has-selection", n > 0);
    if (selectAll) {
      var pool = selectableRows();
      selectAll.checked = pool.length > 0 && n === pool.length;
      // Some-but-not-all is a third state, and saying so is the difference
      // between "nothing is selected" and "you have a selection you can't see
      // all of".
      selectAll.indeterminate = n > 0 && n < pool.length;
    }
    // Only offer what the selection can actually do — a bar with five buttons of
    // which three are no-ops teaches the caller to distrust all five.
    var some = function (fn) {
      return chosen.some(fn);
    };
    var avail = {
      pin: some(function (r) {
        return r.dataset.pinned !== "1" && r.dataset.archived !== "1";
      }),
      unpin: some(function (r) {
        return r.dataset.pinned === "1";
      }),
      archive: some(function (r) {
        return r.dataset.archived !== "1";
      }),
      restore: some(function (r) {
        return r.dataset.archived === "1";
      }),
      delete: n > 0,
    };
    Object.keys(avail).forEach(function (key) {
      var btn = document.querySelector('[data-bulk="' + key + '"]');
      if (btn) btn.hidden = !avail[key];
    });
  }

  // Bulk = the same per-row endpoints, run together. No bulk endpoint exists and
  // none is needed at this cardinality; what matters is that ONE failure among
  // ten does not lose the other nine, hence allSettled semantics rather than a
  // Promise.all that rejects on the first error.
  function runBulk(label, targets, action) {
    if (!targets.length) return;
    var failures = 0;
    var done = targets.map(function (row) {
      return action(row).catch(function () {
        failures += 1;
      });
    });
    Promise.all(done).then(function () {
      afterMutation();
      if (!failures) return;
      // Two different facts, and saying the wrong one is worse than saying
      // nothing: a partial failure has to name what DID happen, and a total
      // failure must not imply that anything did.
      var noun = targets.length === 1 ? "conversation" : "conversations";
      var message;
      if (failures < targets.length) {
        message = failures + " of " + targets.length + " " + noun + " could not be " + label + ". The rest were.";
      } else if (targets.length === 1) {
        message = "That conversation could not be " + label + ".";
      } else {
        message = "None of the " + targets.length + " conversations could be " + label + ".";
      }
      reportFailure(message);
    });
  }

  if (bulk) {
    bulk.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-bulk]");
      if (!btn) return;
      var kind = btn.getAttribute("data-bulk");
      var chosen = selectedRows();
      if (!chosen.length) return;
      if (kind === "pin") {
        runBulk(
          "pinned",
          chosen.filter(function (r) {
            return r.dataset.pinned !== "1" && r.dataset.archived !== "1";
          }),
          function (r) {
            return setPinned(r, true);
          },
        );
      } else if (kind === "unpin") {
        runBulk(
          "unpinned",
          chosen.filter(function (r) {
            return r.dataset.pinned === "1";
          }),
          function (r) {
            return setPinned(r, false);
          },
        );
      } else if (kind === "archive") {
        runBulk(
          "archived",
          chosen.filter(function (r) {
            return r.dataset.archived !== "1";
          }),
          function (r) {
            return setArchived(r, true);
          },
        );
      } else if (kind === "restore") {
        runBulk(
          "restored",
          chosen.filter(function (r) {
            return r.dataset.archived === "1";
          }),
          function (r) {
            return setArchived(r, false);
          },
        );
      } else if (kind === "delete") {
        confirmDelete(chosen.length, chosen[0].dataset.title || "this conversation").then(function (ok) {
          if (ok) runBulk("deleted", chosen, destroy);
        });
      }
    });
  }

  var bulkClear = document.getElementById("ch-bulk-clear");
  if (bulkClear) bulkClear.addEventListener("click", clearSelection);

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      var on = selectAll.checked;
      selectableRows().forEach(function (r) {
        setRowSelected(r, on);
      });
      syncSelection();
    });
  }

  // ---- The "⋮" menu ----------------------------------------------------
  // One builder for BOTH views — the list row and the card projected from it —
  // so a conversation offers the same actions however it is drawn. The component
  // itself is shared with the rail and the chat page, so this is also the menu a
  // caller already learned there, plus Archive (live rows) / Restore (archived
  // ones), which only this page can offer.
  //
  // Returns null for a row the caller does not own: pin, rename, archive and
  // delete are all owner-only server-side, so offering them would be offering
  // four controls that 404.
  function menuTriggerFor(row) {
    if (!window.chatRowMenu || row.dataset.owned !== "1") return null;
    return window.chatRowMenu.trigger({
      // Read at CLICK time, not at build time: the row's pin/archive state
      // changes under this menu, and a `session` snapshot taken now would have
      // the menu offering "Pin" on an already-pinned row.
      get session() {
        return sessionOf(row);
      },
      onPin: function (pinned) {
        return runAndSettle(setPinned(row, pinned));
      },
      onRename: function () {
        return runAndSettle(renameRow(row));
      },
      // Both handlers, always: the menu picks which one to show from the row's
      // live `archived` state, so archiving a conversation and restoring it are
      // the same wiring.
      onArchive: function () {
        return runAndSettle(setArchived(row, true));
      },
      onRestore: function () {
        return runAndSettle(setArchived(row, false));
      },
      onDelete: function () {
        return confirmDelete(1, row.dataset.title || "this conversation").then(function (ok) {
          if (ok) return runAndSettle(destroy(row));
          return null;
        });
      },
    });
  }

  // ---- Row wiring ------------------------------------------------------
  if (listEl) {
    // The whole row opens its conversation — but not when the click landed on a
    // control (the checkbox, the row menu) or on the name link, which navigates
    // on its own and is what keeps middle-click and the keyboard honest.
    listEl.addEventListener("click", function (ev) {
      if (ev.target.closest(".ch-row__sel, .ch-rowmenu, .chat-rowmenu-btn, a")) return;
      var row = ev.target.closest(".ch-row");
      if (!row || !row.dataset.href) return;
      window.location.href = row.dataset.href;
    });

    listEl.addEventListener("change", function (ev) {
      var box = ev.target.closest(".ch-check");
      if (!box) return;
      var row = box.closest(".ch-row");
      if (row) setRowSelected(row, box.checked);
      syncSelection();
    });

    // Shift-click a checkbox to take the run between it and the last one — the
    // gesture every file list has, and the difference between clearing out
    // twenty abandoned chats in one move and twenty clicks.
    var lastChecked = null;
    listEl.addEventListener("click", function (ev) {
      var box = ev.target.closest(".ch-check");
      if (!box) return;
      var row = box.closest(".ch-row");
      if (!row) return;
      if (ev.shiftKey && lastChecked && lastChecked !== row) {
        var pool = selectableRows();
        var from = pool.indexOf(lastChecked);
        var to = pool.indexOf(row);
        if (from !== -1 && to !== -1) {
          var lo = Math.min(from, to);
          var hi = Math.max(from, to);
          for (var i = lo; i <= hi; i++) setRowSelected(pool[i], box.checked);
          syncSelection();
        }
      }
      lastChecked = row;
    });

    rows().forEach(function (row) {
      var host = row.querySelector(".ch-rowmenu");
      var trigger = menuTriggerFor(row);
      if (host && trigger) host.appendChild(trigger);
    });
  }

  // ---- "Modified" labels ----------------------------------------------
  // Relative ("2d ago"), because on a conversation list recency is the question
  // — with the absolute local time on hover for when it isn't. The server
  // rendered a UTC fallback and stamped `data-hydrated` so the global <time>
  // hydrator (datetime.js) leaves these alone; this is the owner.
  function hydrateWhen() {
    if (!window.AgnesTime) return;
    document.querySelectorAll("time[data-chat-when]").forEach(function (el) {
      var iso = el.getAttribute("datetime");
      var rel = window.AgnesTime.formatRelative(iso);
      if (!rel) return;
      el.textContent = rel;
      if (!el.title) el.title = window.AgnesTime.formatDateTime(iso);
    });
  }
  hydrateWhen();
  // AgnesTime is deferred and may land after this file runs.
  window.addEventListener("load", hydrateWhen);

  // ---- Toolbar ---------------------------------------------------------
  if (listEl && window.FilterToolbar) {
    toolbar = window.FilterToolbar.init({
      rows: "#ch-list .ch-row",
      search: { el: "#ch-search", attr: "data-search" },
      // Non-exclusive segments over a pipe-separated set — a chat can be pinned
      // AND shared, and `all` is a real token so archived rows stay out of every
      // view but their own (see `segments.multi` in filter_toolbar.js).
      segments: { container: "#ch-seg", attr: "data-buckets", multi: true },
      facets: [
        { key: "agent", attr: "data-agent", label: "Agent" },
        { key: "surface", attr: "data-surface", label: "Source" },
      ],
      filterBtn: "#ch-filter-btn",
      menu: "#ch-filter-menu",
      chips: "#ch-chips",
      sort: {
        el: "#ch-sort",
        keys: {
          updated: "data-updated",
          name: "data-name",
          agent: "data-agent",
        },
        // Pinned conversations stay above the rest through every re-sort — the
        // pinned shelf is part of the list's SHAPE, not one order among several,
        // exactly as it is in the rail.
        pinFirst: "data-pinned",
        // No `headers` / `wrap`: the list is not a table, so there are no column
        // headers for the toolbar control to yield to. The <select> is the one
        // sort control, in both views.
      },
      count: { el: "#ch-count", noun: "chat" },
      noResults: "#ch-noresults",
      // No `view` block: this page has ONE view. The list is the only
      // projection, so there is no grid to keep in sync and no card builder —
      // `filter_toolbar.js` treats `view` as optional and skips the whole
      // switch. `#ch-list` needs no hiding when a filter empties it either:
      // `.ch-list` is bare flex rows with no border, header or background, so
      // with every row hidden it collapses to nothing and the no-results panel
      // stands alone on its own.
      onApply: function () {
        // The row menu is a shared component holding a module-level reference to
        // the trigger it was opened from. A re-projection rebuilds every card,
        // including that trigger, so an open menu would stay on screen anchored
        // to a button that is no longer in the document. chat.js closes it around
        // its own re-renders; the grid projection needs the same
        // (Devin Review on #1185).
        if (window.chatRowMenu) window.chatRowMenu.close();
        // A filter change can hide a selected row; the selection must not
        // survive invisibly (see syncSelection).
        syncSelection();
        syncFilterView();
        syncDockChips();
      },
    });
  }

  updateSegmentCounts();
  syncSelection();
  syncFilterView();
  syncDockChips();
})();
