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
  // The lifecycle-state set is DERIVED from the row's `archived` flag rather
  // than patched, so there is exactly one rule for it and it cannot drift from
  // the server's (see `_chats_rows` in app/web/router.py). `all` is on every row
  // so the option of that name can mean what it says.
  //
  // Pinned and Shared are NOT in here — they are their own toggle facets over
  // `data-pinned` / `data-shared`, which setRowPinned() already maintains, so
  // archiving a pinned chat leaves it pinned (it is, on the server too) and
  // "Pinned only" still reaches it.
  function syncBuckets(row) {
    row.dataset.status = "all|" + (row.dataset.archived === "1" ? "archived" : "active");
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
    // Archiving UNPINS (ChatRepository.archive_session), so the row has to lose
    // its pin here as well — otherwise it keeps a glyph, a `data-pinned` the
    // `Pinned only` filter would match, and a place at the top of the sort that
    // a reload would not reproduce.
    if (archived && row.dataset.pinned === "1") setRowPinned(row, false);
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
    // The rail is on screen BESIDE this page and holds the same conversations,
    // so anything that moves here has to move there: an archived chat went on
    // sitting in its Pinned shelf until the next full page load, and a rename
    // or a delete was just as stale. This page updates its own rows in place (a
    // reload would throw away the search and filters the caller used to find
    // the row); the rail has no such constraint, so it simply re-fetches.
    if (window.railChatHistory && window.railChatHistory.reload) {
      window.railChatHistory.reload();
    }
  }

  // ---- Feedback for one row's action ----------------------------------
  // A row menu action was silent: the row left the view and nothing said what
  // had happened or offered a way back. Archiving especially — it is reversible,
  // but reversing it meant opening Filter, choosing Archived, finding the row
  // again and hitting Restore, which is a lot of work to undo a click.
  //
  // Rides the shared `showUndoToast` every admin delete already uses, so this is
  // the app's one undo affordance rather than a second one for this page.
  // Silent when it is absent: feedback must never be the thing that breaks an
  // action that already succeeded.
  function announceUndo(message, undo) {
    if (typeof window.showUndoToast !== "function") return;
    window.showUndoToast(message, undo, afterMutation);
  }

  function runAndSettle(promise) {
    return promise.then(afterMutation, function (err) {
      afterMutation();
      reportFailure("The conversation could not be updated. " + (err && err.message ? err.message : ""));
    });
  }

  // ---- What is applied -------------------------------------------------
  // Read off the inputs themselves — the engine owns their state and pushes it
  // back onto them on every path (a choice, a tick, a chip's ×, Clear), so this
  // cannot drift from what is filtering the list.
  function statusValue() {
    var on = document.querySelector('#ch-filter-menu input[data-facet="status"]:checked');
    return (on && on.value) || "active";
  }
  function anyFacetApplied() {
    return !!document.querySelector('#ch-filter-menu input[type="checkbox"][data-facet]:checked');
  }

  // ---- "N archived" — the control behind the count ---------------------
  // The default view excludes archived conversations, so the count reads
  // "7 of 27" with nothing next to it to act on: twenty chats that are put
  // away look exactly like twenty chats that are lost, and the only way
  // through was a segment button inside the Filter menu (#1974). This puts
  // the way in beside the number that raises the question.
  //
  // Shown only while NOTHING is applied — the resting state, which is the one
  // with no control on screen saying the archive exists. The moment anything is
  // applied the chips say so and the Filter menu is one click away, so a second
  // control would be noise; it is also the only state in which this control's
  // own number ("8 archived") is the number you would actually get, since it
  // counts every archived row rather than the ones the other filters would
  // leave. Not during a search either: a search already looks everywhere
  // (`spansSearch`), so there would be nothing left to offer.
  function syncHiddenNote() {
    var btn = document.getElementById("ch-show-archived");
    if (!btn) return;
    var search = document.getElementById("ch-search");
    var searching = !!(search && (search.value || "").trim());
    var archived = rows().filter(function (r) {
      return (r.dataset.status || "").split("|").indexOf("archived") !== -1;
    }).length;
    var show = statusValue() === "active" && !anyFacetApplied() && !searching && archived > 0;
    btn.hidden = !show;
    if (show) btn.textContent = "Show " + archived + " archived";
  }

  var showArchivedBtn = document.getElementById("ch-show-archived");
  if (showArchivedBtn) {
    showArchivedBtn.addEventListener("click", function () {
      // Goes through the engine, so this and choosing Archived in the menu are
      // the same act — including growing the chip that takes it back off.
      if (toolbar && toolbar.setFacet) toolbar.setFacet("status", "archived", true);
    });
  }

  // ---- Show-option counts ---------------------------------------------
  // The UNFILTERED tally per option, matching how every other Filter menu in the
  // app counts. Recomputed after any action, so archiving four chats moves four
  // out of Active and into Archived immediately.
  //
  // `pinned` can only ever be a live row (archiving unpins), so its tally is
  // the same whether counted over the whole list or the live half; `shared` is
  // genuinely orthogonal to the state — an archived co-session is coherent — so
  // it does span the archive. An option the server did not render (a caller with
  // nothing shared) simply has no element to write to.
  function updateSegmentCounts() {
    var counts = { all: 0, active: 0, archived: 0, pinned: 0, shared: 0 };
    rows().forEach(function (row) {
      (row.dataset.status || "").split("|").forEach(function (b) {
        if (b in counts) counts[b] += 1;
      });
      if (row.dataset.pinned) counts.pinned += 1;
      if (row.dataset.shared) counts.shared += 1;
    });
    Object.keys(counts).forEach(function (key) {
      var el = document.querySelector('[data-opt-count="' + key + '"]');
      if (el) el.textContent = String(counts[key]);
    });
    // Same tally, other readout: archiving the last unarchived chat has to move
    // the "Show N archived" control too, or it reports a number that has moved
    // on without it.
    syncHiddenNote();
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
      // Neither direction on an archived row: pinned and archived contradict
      // each other, so the state does not exist (archiving clears the pin, the
      // endpoint refuses a pin on an archived row, and the server never renders
      // one as pinned). `unpin` needs no such clause for the same reason — an
      // archived row never carries `data-pinned`.
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
  function runBulk(label, targets, action, onAllDone) {
    if (!targets.length) return;
    var failures = 0;
    var done = targets.map(function (row) {
      return action(row).catch(function () {
        failures += 1;
      });
    });
    Promise.all(done).then(function () {
      afterMutation();
      if (!failures) {
        // Only on a CLEAN run: offering "Undo" over a batch that half failed
        // would promise to restore rows that never moved.
        if (onAllDone) onAllDone();
        return;
      }
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
        var going = chosen.filter(function (r) {
          return r.dataset.archived !== "1";
        });
        runBulk("archived", going, function (r) {
          return setArchived(r, true);
        }, function () {
          // ONE toast for the batch, not one per row. Undo puts back exactly
          // the rows this action moved — captured here rather than re-derived
          // from the archive, which by then also holds everything the caller
          // archived earlier and meant to keep archived.
          announceUndo(
            going.length === 1
              ? "Archived “" + (going[0].dataset.title || "that conversation") + "”"
              : "Archived " + going.length + " conversations",
            function () {
              return Promise.all(going.map(function (r) { return setArchived(r, false); }));
            },
          );
        });
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
      // Archive is the one row action whose result LEAVES THE VIEW while
      // being reversible, so it is the one that earns a toast: without it the
      // row simply vanished, and undoing a click meant opening Filter, choosing
      // Archived, finding the row and hitting Restore.
      //
      // Restore gets none. It is the same shape of change, but it happens while
      // the caller is deliberately looking AT the archive, having gone there to
      // do exactly this — so the row leaving is the confirmation, and an Undo
      // for it would be a control for re-archiving something the caller just
      // chose to take out.
      onArchive: function () {
        var title = row.dataset.title || "That conversation";
        return runAndSettle(setArchived(row, true)).then(function () {
          announceUndo("Archived “" + title + "”", function () {
            return setArchived(row, false);
          });
        });
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
      facets: [
        // ── The lifecycle state ────────────────────────────────────────────
        // Active / Archived / All: alternatives, so `exclusive` — one chosen,
        // selecting replaces. This and the two toggles below were ONE list of
        // four (All · Pinned · Shared · Archived), first as the engine's
        // single-select `segments` control and then as one OR-group of
        // checkboxes. Both were wrong the same way: a state and two attributes
        // are not four of a kind. As segments, "my pinned ones in the archive"
        // was unaskable; as one OR-group, `All` was a superset of `Shared` so
        // ticking both said nothing extra, and `All` only existed because a
        // checkbox group already spends "nothing ticked" on "no filter".
        //
        // `whenEmpty` is the resting condition a plain facet cannot express:
        // before anything is chosen the list shows the LIVE conversations, so
        // the archive stays out of it, and a choice REPLACES that rather than
        // narrowing within it. A facet resting on that value is not an applied
        // filter — no chip, no badge count — however it got there, which is
        // what keeps "Active" from chipping as a filter over an unfiltered
        // list.
        //
        // `spansSearch`: a search looks EVERYWHERE, not just in the chosen
        // state. Without it an archived conversation was unreachable twice
        // over — absent from the default list AND invisible to a search for
        // its own title, which is how a chat from the same morning became
        // impossible to reopen (#1974).
        { key: "status", attr: "data-status", label: "Show",
          multi: true, exclusive: true,
          whenEmpty: ["active"], spansSearch: true },
        // ── The attributes ────────────────────────────────────────────────
        // Independent flags, ANDed with the state and with each other, so
        // "All + Pinned only" is every pinned conversation either side of the
        // archive and "Archived + Pinned only" is the pinned ones inside it.
        // `toggle`: one condition rather than a category of values, so the chip
        // states the condition ("Pinned only") instead of naming a category.
        // The row already carries both attributes for the pin indicator and the
        // Shared pill, so there is nothing new to keep in step.
        { key: "pinned", attr: "data-pinned", label: "Pinned only", toggle: true },
        { key: "shared", attr: "data-shared", label: "Shared only", toggle: true },
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
        syncHiddenNote();
      },
    });
  }

  updateSegmentCounts();
  syncSelection();
  syncHiddenNote();
})();
