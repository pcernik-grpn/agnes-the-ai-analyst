/* issue_pages.js — /me/issues (mode "mine") and /admin/issues (mode
 * "admin"): list, filter, open-detail, comment and (admin only) resolve.
 *
 * Both pages are static shells (app/web/templates/me_issues.html,
 * admin_issues.html + the shared _issue_detail_drawer.html) — every byte of
 * data on screen comes from the EXISTING JSON API, never a new route:
 *
 *   GET  /api/issues/mine?status=&limit=      (mode "mine")
 *   GET  /api/admin/issues?status=&limit=     (mode "admin")
 *   GET  /api/issues/{id}                     (both — owner-or-admin)
 *   POST /api/issues/{id}/comments            (both)
 *   POST /api/admin/issues/{id}/resolve       (mode "admin" only)
 *   GET  /api/issues/{id}/screenshot          (both — plain <img>, cookie
 *                                               auth, when screenshot_path
 *                                               is set)
 *
 * DOM is built with createElement/textContent throughout, never innerHTML,
 * for anything a reporter typed (title, body, comment text, the reporter's
 * own email) — the same rule issue_report.js follows for its fallback
 * textarea. See docs/superpowers/specs/2026-09-09-issue-reporting-step1-
 * design.md.
 */
(function () {
  "use strict";

  var root = document.getElementById("iss-page");
  if (!root) return;

  var MODE = root.getAttribute("data-mode") === "admin" ? "admin" : "mine";
  var IS_ADMIN = MODE === "admin";
  var LIST_URL = IS_ADMIN ? "/api/admin/issues" : "/api/issues/mine";

  var KIND_LABELS = {
    bug: "Bug",
    wrong_answer: "Wrong answer",
    request: "Missing",
    question: "Unclear",
    other: "Other",
  };

  var state = { status: "open" };
  var currentDetailId = null;
  //: Which OPENING of the drawer is current. The id alone is not enough to
  //: identify a response's owner: close a report while its GET is in flight
  //: and reopen the same one, and the stale response matches `currentDetailId`
  //: again — then lands its older snapshot over whatever the second opening
  //: has since fetched or the user has since posted (#2402). Bumped on every
  //: open and on close, so a response is only ever applied to the opening
  //: that asked for it.
  var detailGeneration = 0;
  //: Same idea for the list. Clicking Resolved then Open starts two fetches;
  //: responses need not arrive in request order, so without this the older
  //: one could repaint the table while the newer filter stays highlighted —
  //: a view that silently contradicts the selected status (#2402).
  var listGeneration = 0;
  // The full row last rendered in the drawer, `comments` included — kept so
  // a resolve response (which carries no `comments` key: it's `repo.get()`,
  // not the `GET /api/issues/{id}` shape) can be MERGED onto it rather than
  // replacing it and silently wiping the visible thread.
  var currentDetailRow = null;

  function kindLabel(kind) {
    return KIND_LABELS[kind] || kind || "—";
  }

  function toast(kind, msg) {
    if (window.appToast) window.appToast({ kind: kind, msg: msg });
  }

  // Same reader as data_sources_page.js / admin_extraction.js: a FastAPI
  // `detail` is a string on some paths, a structured `{error, message}` on
  // ours (app/api/issues.py::_err) — read both so a 409's exact wording
  // (which admin resolved it, and when) reaches the toast rather than a
  // generic "something went wrong".
  function detailMessage(body, fallback) {
    var detail = body && body.detail;
    if (detail && typeof detail === "object") return detail.message || detail.error || fallback;
    return detail || fallback;
  }

  function relTime(iso) {
    return window.AgnesTime ? window.AgnesTime.formatRelative(iso) : iso || "";
  }

  function absTime(iso) {
    return window.AgnesTime ? window.AgnesTime.formatDateTime(iso) : iso || "";
  }

  function el(tag, opts) {
    var e = document.createElement(tag);
    opts = opts || {};
    if (opts.className) e.className = opts.className;
    if (opts.text != null) e.textContent = opts.text;
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) {
        e.setAttribute(k, opts.attrs[k]);
      });
    }
    return e;
  }

  function badge(text, accent) {
    return el("span", { className: "badge" + (accent ? " badge--" + accent : ""), text: text });
  }

  function statusBadge(status) {
    return badge(status === "resolved" ? "Resolved" : "Open", status === "resolved" ? "success" : "warn");
  }

  function getJson(url) {
    return fetch(url, { credentials: "include" }).then(function (r) {
      return r.json().then(
        function (body) {
          return { status: r.status, body: body };
        },
        function () {
          return { status: r.status, body: null };
        }
      );
    });
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }).then(function (r) {
      return r.json().then(
        function (body) {
          return { status: r.status, body: body };
        },
        function () {
          return { status: r.status, body: null };
        }
      );
    });
  }

  // ── List ──────────────────────────────────────────────────────────────

  function loadList() {
    var loading = document.getElementById("iss-loading");
    var empty = document.getElementById("iss-empty");
    var wrap = document.getElementById("iss-table-wrap");
    var tbody = document.getElementById("iss-tbody");
    loading.hidden = false;
    empty.hidden = true;
    wrap.hidden = true;
    var url = LIST_URL + "?status=" + encodeURIComponent(state.status) + "&limit=200";
    var generation = ++listGeneration;
    getJson(url).then(function (res) {
      if (generation !== listGeneration) return;
      loading.hidden = true;
      if (res.status !== 200) {
        toast("warn", detailMessage(res.body, "Couldn't load reports."));
        empty.hidden = false;
        return;
      }
      var rows = (res.body && res.body.data) || [];
      tbody.textContent = "";
      if (!rows.length) {
        empty.hidden = false;
        return;
      }
      rows.forEach(function (row) {
        tbody.appendChild(renderRow(row));
      });
      wrap.hidden = false;
    }).catch(function () {
      // `getJson` turns a malformed BODY into {status, body: null} but lets a
      // transport failure reject. Without this the spinner outlived the
      // failure and the page stayed blank until a reload (#2402).
      if (generation !== listGeneration) return;
      loading.hidden = true;
      empty.hidden = false;
      toast("warn", "Couldn't load reports — check your connection.");
    });
  }

  function renderRow(row) {
    var tr = el("tr", { attrs: { tabindex: "0", role: "button", "data-id": row.id } });
    tr.appendChild(el("td", { text: "#" + row.number }));
    if (IS_ADMIN) {
      tr.appendChild(el("td", { className: "iss-reporter-cell", text: row.created_by_email || row.created_by || "—" }));
    }
    tr.appendChild(el("td", { text: kindLabel(row.kind) }));
    var statusTd = el("td");
    statusTd.appendChild(statusBadge(row.status));
    tr.appendChild(statusTd);
    // `data-field` rather than a positional index: "mine" and "admin" don't
    // share a column count (the Reporter column only exists in admin mode),
    // so a fixed cell index is exactly the kind of thing that quietly points
    // at the wrong column the next time either table's shape changes.
    tr.appendChild(el("td", { text: String(row.comment_count || 0), attrs: { "data-field": "replies" } }));
    tr.appendChild(el("td", { text: relTime(row.last_activity_at || row.created_at) }));
    tr.appendChild(el("td", { className: "iss-title-cell", text: row.title || "" }));
    tr.addEventListener("click", function () {
      openDetail(row.id);
    });
    tr.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDetail(row.id);
      }
    });
    return tr;
  }

  // ── Detail drawer ────────────────────────────────────────────────────

  function openDetail(id) {
    currentDetailId = id;
    var generation = ++detailGeneration;
    var dlg = document.getElementById("iss-detail");
    var loading = document.getElementById("iss-detail-loading");
    var content = document.getElementById("iss-detail-content");
    document.getElementById("iss-detail-title").textContent = "Loading…";
    document.getElementById("iss-detail-sub").textContent = "";
    loading.hidden = false;
    content.hidden = true;
    dlg.hidden = false;
    dlg.classList.add("is-open");
    // The panel ships `aria-hidden="true"` so it is out of the accessibility
    // tree while closed; clearing `hidden` and adding the class does not undo
    // that, so a screen-reader user got a drawer they could see nothing of
    // (#2402).
    dlg.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";

    getJson("/api/issues/" + encodeURIComponent(id)).then(function (res) {
      // Ignore a response that does not belong to the CURRENT opening. Two
      // rows clicked in quick succession start two fetches, and the first to
      // ARRIVE is not necessarily the one asked for last — without this the
      // drawer could show issue A's text while every action on it (comment,
      // resolve) still addressed issue B. Keyed on the opening rather than
      // the id, so closing and reopening the same report does not let the
      // abandoned request through either (#2402).
      if (generation !== detailGeneration) return;
      loading.hidden = true;
      if (res.status !== 200) {
        document.getElementById("iss-detail-title").textContent = "Not found";
        document.getElementById("iss-detail-sub").textContent = detailMessage(res.body, "This report could not be loaded.");
        return;
      }
      content.hidden = false;
      currentDetailRow = res.body;
      renderDetail(currentDetailRow);
    }).catch(function () {
      // Transport failure: `getJson` rejects rather than returning a status,
      // so without this the drawer sat on "Loading…" for good (#2402).
      if (generation !== detailGeneration) return;
      loading.hidden = true;
      document.getElementById("iss-detail-title").textContent = "Couldn't load";
      document.getElementById("iss-detail-sub").textContent =
        "The report could not be fetched — check your connection and try again.";
    });
  }

  function closeDetail() {
    var dlg = document.getElementById("iss-detail");
    dlg.classList.remove("is-open");
    dlg.hidden = true;
    dlg.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    detailGeneration++;
    currentDetailId = null;
    currentDetailRow = null;
  }

  function renderDetail(row) {
    document.getElementById("iss-detail-title").textContent = "#" + row.number + " " + (row.title || "");
    document.getElementById("iss-detail-sub").textContent =
      "Reported " + relTime(row.created_at) + " · " + row.source_surface + " · last activity " + relTime(row.last_activity_at);

    var badges = document.getElementById("iss-detail-badges");
    badges.textContent = "";
    badges.appendChild(badge(kindLabel(row.kind)));
    badges.appendChild(statusBadge(row.status));

    if (IS_ADMIN) {
      var reporter = document.getElementById("iss-detail-reporter");
      reporter.textContent = "Reported by " + (row.created_by_email || row.created_by || "—");
    }

    document.getElementById("iss-detail-body").textContent = row.body || "";

    renderContext(row);
    renderScreenshot(row);
    if (IS_ADMIN) renderResolve(row);
    renderComments(row.comments || []);

    var commentBox = document.getElementById("iss-comment-body");
    if (commentBox) commentBox.value = "";
  }

  function renderContext(row) {
    var dl = document.getElementById("iss-detail-context");
    dl.textContent = "";
    var ctx = row.context_json || {};

    function pair(label, valueEl) {
      dl.appendChild(el("dt", { text: label }));
      var dd = el("dd");
      dd.appendChild(valueEl);
      dl.appendChild(dd);
    }

    if (row.page_url) {
      var a = el("a", { text: row.page_url, attrs: { href: row.page_url, target: "_blank", rel: "noopener" } });
      pair("Page", a);
    }
    if (ctx.app_version) {
      pair("Version", document.createTextNode(ctx.app_version + (ctx.app_commit ? " (" + ctx.app_commit + ")" : "")));
    }
    if (ctx.user_agent) {
      pair("Browser", document.createTextNode(ctx.user_agent));
    }
    if (ctx.viewport) {
      pair("Viewport", document.createTextNode(ctx.viewport));
    }
    if (ctx.chat_session_id) {
      var sessLink = el("a", {
        text: ctx.chat_session_id,
        attrs: { href: "/chat?session=" + encodeURIComponent(ctx.chat_session_id) },
      });
      pair("Chat session", sessLink);
    }
    if (row.resolved_by || row.resolved_at) {
      pair(
        "Resolved",
        document.createTextNode((row.resolved_by || "—") + (row.resolved_at ? " · " + absTime(row.resolved_at) : ""))
      );
    }

    var errorsBox = document.getElementById("iss-detail-errors");
    errorsBox.textContent = "";
    var errors = ctx.recent_errors || [];
    if (errors.length) {
      var list = el("div", { className: "iss-errors-list" });
      errors.forEach(function (e) {
        list.appendChild(el("div", { text: "[" + (e.kind || "error") + "] " + (e.message || "") }));
      });
      errorsBox.appendChild(list);
    }
  }

  function renderScreenshot(row) {
    var wrap = document.getElementById("iss-detail-screenshot-wrap");
    var img = document.getElementById("iss-detail-screenshot");
    if (row.screenshot_path) {
      img.src = "/api/issues/" + encodeURIComponent(row.id) + "/screenshot";
      wrap.hidden = false;
    } else {
      img.removeAttribute("src");
      wrap.hidden = true;
    }
  }

  function renderResolve(row) {
    var banner = document.getElementById("iss-resolved-banner");
    var box = document.getElementById("iss-resolve-box");
    if (row.status === "resolved") {
      banner.textContent =
        "Resolved by " + (row.resolved_by || "—") + (row.resolved_at ? " · " + absTime(row.resolved_at) : "") +
        (row.resolution_note ? " — " + row.resolution_note : "");
      banner.hidden = false;
      box.hidden = true;
    } else {
      banner.hidden = true;
      box.hidden = false;
      var note = document.getElementById("iss-resolve-note");
      if (note) note.value = "";
    }
  }

  function renderComments(comments) {
    var box = document.getElementById("iss-comments");
    box.textContent = "";
    if (!comments.length) {
      box.appendChild(el("p", { className: "iss-comments-empty", text: "No comments yet." }));
      return;
    }
    comments.forEach(function (c) {
      box.appendChild(renderComment(c));
    });
  }

  function renderComment(c) {
    var card = el("div", { className: "iss-comment" });
    var meta = el("div", { className: "iss-comment-meta" });
    meta.appendChild(el("span", { className: "iss-comment-author", text: c.author_email || c.author_id || "—" }));
    meta.appendChild(badge(c.author_kind === "admin" ? "Admin" : "Reporter"));
    meta.appendChild(el("span", { text: relTime(c.created_at) }));
    card.appendChild(meta);
    card.appendChild(el("div", { className: "iss-comment-body", text: c.body || "" }));
    return card;
  }

  // ── Actions ───────────────────────────────────────────────────────────

  function submitComment() {
    if (!currentDetailId) return;
    // Pin the report AND the opening: the callback below touches the live
    // drawer, so a response arriving after the user switched reports would
    // otherwise append A's comment into B's thread and bump B's reply count
    // (#2402). The server stored it correctly either way — this is the view.
    var issueId = currentDetailId;
    var generation = detailGeneration;
    var box = document.getElementById("iss-comment-body");
    var body = (box.value || "").trim();
    if (!body) {
      box.focus();
      return;
    }
    var btn = document.getElementById("iss-comment-submit");
    btn.disabled = true;
    postJson("/api/issues/" + encodeURIComponent(issueId) + "/comments", { body: body })
      .then(function (res) {
        // Re-enable FIRST, then decide whether this response still owns the
        // drawer. The button is shared by every report, so returning at the
        // guard without restoring it left the next opening with a dead
        // Comment button until a reload (#2402).
        btn.disabled = false;
        if (generation !== detailGeneration) return;
        if (res.status !== 201) {
          toast("warn", detailMessage(res.body, "Couldn't post the comment."));
          return;
        }
        box.value = "";
        var empty = document.querySelector("#iss-comments .iss-comments-empty");
        if (empty) empty.remove();
        document.getElementById("iss-comments").appendChild(renderComment(res.body));
        if (currentDetailRow) currentDetailRow.comments = (currentDetailRow.comments || []).concat([res.body]);
        bumpReplyCount(issueId);
      })
      .catch(function () {
        btn.disabled = false;
        if (generation !== detailGeneration) return;
        toast("warn", "Couldn't post the comment.");
      });
  }

  function bumpReplyCount(id) {
    // Matched by the `data-id` property (never built into a CSS selector —
    // an id is always server-generated, but there is no reason to trust
    // that at the DOM layer too) and the replies cell's own `data-field`,
    // not a column index that differs between "mine" and "admin".
    var rows = document.querySelectorAll("#iss-tbody tr");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].dataset.id !== id) continue;
      var cell = rows[i].querySelector('[data-field="replies"]');
      if (cell) cell.textContent = String((parseInt(cell.textContent, 10) || 0) + 1);
      return;
    }
  }

  function submitResolve() {
    if (!currentDetailId || !IS_ADMIN) return;
    // Pinned for the same reason as submitComment: the callback re-renders
    // the live drawer, so a response landing after the admin moved on would
    // stamp A's resolved state onto B's panel (#2402).
    var issueId = currentDetailId;
    var generation = detailGeneration;
    var note = document.getElementById("iss-resolve-note");
    var btn = document.getElementById("iss-resolve-btn");
    btn.disabled = true;
    postJson("/api/admin/issues/" + encodeURIComponent(issueId) + "/resolve", {
      resolution_note: (note.value || "").trim() || null,
    })
      .then(function (res) {
        // Shared button: restore it before any early return, or the next
        // report opens with Resolve dead (#2402).
        btn.disabled = false;
        if (generation !== detailGeneration) {
          // The drawer moved on. The resolve DID happen, so refresh the list
          // to reflect it — just do not touch the panel now showing another
          // report.
          loadList();
          return;
        }
        if (res.status !== 200) {
          // The 409 body carries the useful, specific sentence ("#42 was
          // already resolved by X at T") — surface it verbatim rather than
          // a generic failure, per the design spec.
          toast("warn", detailMessage(res.body, "Couldn't resolve this report."));
          return;
        }
        // `res.body` is `repo.get()`'s shape (no `comments` key) — merge onto
        // the row already on screen instead of replacing it, or the thread
        // just rendered from the GET-with-comments fetch would vanish.
        currentDetailRow = Object.assign({}, currentDetailRow, res.body);
        renderDetail(currentDetailRow);
        loadList();
        toast("ok", "Resolved #" + res.body.number + ".");
      })
      .catch(function () {
        btn.disabled = false;
        if (generation !== detailGeneration) return;
        toast("warn", "Couldn't resolve this report.");
      });
  }

  // ── Wiring ────────────────────────────────────────────────────────────

  var seg = document.getElementById("iss-status-filter");
  if (seg) {
    seg.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-status]");
      if (!btn) return;
      seg.querySelectorAll(".fbar-seg__btn").forEach(function (b) {
        b.classList.remove("is-active");
        b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("is-active");
      btn.setAttribute("aria-selected", "true");
      state.status = btn.getAttribute("data-status");
      loadList();
    });
  }

  var newReportBtn = document.getElementById("iss-new-report");
  if (newReportBtn) {
    newReportBtn.addEventListener("click", function () {
      if (window.AgnesIssueReport) window.AgnesIssueReport.open();
    });
  }

  var detailDlg = document.getElementById("iss-detail");
  if (detailDlg) {
    detailDlg.addEventListener("click", function (e) {
      if (e.target.closest("[data-iss-close]")) closeDetail();
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && currentDetailId) {
      e.stopPropagation();
      closeDetail();
    }
  });

  var commentSubmitBtn = document.getElementById("iss-comment-submit");
  if (commentSubmitBtn) commentSubmitBtn.addEventListener("click", submitComment);
  var resolveBtn = document.getElementById("iss-resolve-btn");
  if (resolveBtn) resolveBtn.addEventListener("click", submitResolve);

  loadList();
})();
