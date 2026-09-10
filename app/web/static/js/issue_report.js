/* issue_report.js — "Report a problem" dialog (#issue-dialog).
 *
 * Deferred, loaded by _app_scripts.html after modal.js. A no-op on any page
 * where the dialog partial did not render (can_report_issue is False — a
 * DuckDB-backed instance, or a base.html page that never includes it).
 *
 * Submit flow: POST /api/issues with the auto-captured context envelope;
 * on 201, if the screenshot checkbox is on, lazy-load the vendored
 * html-to-image (window._agHtmlToImageUrl, stamped by _app_scripts.html),
 * capture the page with the dialog itself hidden, and PUT the PNG to
 * /api/issues/{id}/screenshot. A screenshot failure is reported as a toast
 * but never fails the already-filed report.
 *
 * Exposes window.AgnesIssueReport.open() and window.AgnesDiag (from
 * client_diag.js) as the envelope's error source. See docs/issue-reporting.md.
 */
(function () {
  "use strict";

  var dlg = document.getElementById("issue-dialog");
  if (!dlg) return;

  var SENSITIVE_PARAMS = ["token", "access_token", "code", "state"];

  function pageUrl() {
    var u = new URL(location.href);
    SENSITIVE_PARAMS.forEach(function (k) {
      u.searchParams.delete(k);
    });
    return u.toString().slice(0, 2000);
  }

  function chatSessionId() {
    return new URL(location.href).searchParams.get("session") || null;
  }

  var versionCache = null;
  function version() {
    if (versionCache) return Promise.resolve(versionCache);
    return fetch("/api/version", { credentials: "include" })
      .then(function (r) {
        return r.ok ? r.json() : {};
      })
      .then(function (v) {
        versionCache = v;
        return v;
      })
      .catch(function () {
        return {};
      });
  }

  function envelope(v) {
    var diag = window.AgnesDiag ? window.AgnesDiag.snapshot() : { recent_errors: [] };
    return {
      app_version: v.version,
      user_agent: navigator.userAgent,
      viewport: window.innerWidth + "x" + window.innerHeight,
      language: navigator.language,
      chat_session_id: chatSessionId(),
      recent_errors: diag.recent_errors,
      captured_at: new Date().toISOString(),
    };
  }

  function chip(text) {
    var s = document.createElement("span");
    s.className = "issue-chip";
    s.textContent = text;
    return s;
  }

  function renderChips(env) {
    var box = document.getElementById("issue-context-chips");
    if (!box) return;
    box.textContent = "";
    try {
      box.appendChild(chip(new URL(pageUrl()).pathname));
    } catch (e) {
      /* malformed location — skip the path chip */
    }
    if (env.app_version) box.appendChild(chip("v" + env.app_version));
    box.appendChild(chip(_browserLabel()));
    if (env.chat_session_id) box.appendChild(chip("chat " + env.chat_session_id.slice(0, 8)));
    if (env.recent_errors.length) box.appendChild(chip(env.recent_errors.length + " recent errors"));
  }

  function _browserLabel() {
    var brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (brands && brands.length) return brands[brands.length - 1].brand;
    return "browser";
  }

  function _closeUserMenu() {
    var panel = document.getElementById("userMenuPanel");
    var trigger = document.getElementById("userMenuTrigger");
    if (panel) panel.setAttribute("hidden", "");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  function open() {
    _closeUserMenu();
    version().then(function (v) {
      renderChips(envelope(v));
      dlg.style.removeProperty("display"); // clear a leftover Esc-close inline style
      // Reset what only `close()` used to clear. The GLOBAL Escape handler in
      // _app_scripts.html hides this dialog by inline display + removing
      // `is-open`; it cannot call this module's private close(), so a failed
      // submission's fallback text survived and reappeared under the fresh
      // form on the next open (#2402). Clearing it here covers every way the
      // dialog can have been dismissed.
      var fallback = document.getElementById("issue-fallback");
      if (fallback) fallback.hidden = true;
      dlg.style.display = "";
      dlg.classList.add("is-open");
      var title = document.getElementById("issue-title");
      if (title) title.focus();
    });
  }

  function close() {
    dlg.classList.remove("is-open");
    var fallback = document.getElementById("issue-fallback");
    if (fallback) fallback.hidden = true;
  }

  function loadHtmlToImage() {
    if (window.htmlToImage) return Promise.resolve(window.htmlToImage);
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = window._agHtmlToImageUrl || "/static/vendor/html-to-image.min.js";
      s.onload = function () {
        resolve(window.htmlToImage);
      };
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  // html-to-image paints through an SVG <foreignObject>, so the browser
  // renders the CSS itself — html2canvas 1.4.1 could not parse the paper
  // skin's color-mix() colors (reported as `color(srgb …)`) and failed on
  // every page. The filter drops what never belongs in a screenshot and
  // what makes the capture slow: the inlined icon sprite (hundreds of
  // <symbol>s) and any display:none subtree.
  var SCREENSHOT_TIMEOUT_MS = 15000;

  function _skipForScreenshot(node) {
    if (!node || node.nodeType !== 1) return false;
    // The dialog is excluded from the capture rather than hidden during it.
    // Hiding made the window blink twice on every save — once when the
    // capture started, once when it was restored just before closing — and
    // it also hid the "Capturing…" label, which lives on the dialog's own
    // button, exactly while there was something to report. Dropping the
    // node here keeps the form on screen and out of the picture.
    if (node === dlg) return true;
    var tag = (node.tagName || "").toUpperCase();
    if (tag === "SCRIPT" || tag === "NOSCRIPT") return true;
    if (tag === "SVG" && node.querySelectorAll("symbol").length > 20) return true;
    if (node !== document.body && getComputedStyle(node).display === "none") return true;
    return false;
  }

  function screenshotBlob() {
    // The dialog stays on screen throughout — `_skipForScreenshot` drops it
    // from the capture, so the reporter never gets a picture of the form
    // asking for a picture, and never sees the window flicker either.
    var capture = loadHtmlToImage().then(function (lib) {
      return lib.toBlob(document.body, {
        pixelRatio: 1,
        cacheBust: false,
        skipFonts: true,
        filter: function (node) {
          return !_skipForScreenshot(node);
        },
      });
    });
    var timeout = new Promise(function (_resolve, reject) {
      setTimeout(function () {
        reject(new Error("screenshot timed out"));
      }, SCREENSHOT_TIMEOUT_MS);
    });
    return Promise.race([capture, timeout])
      .then(function (blob) {
        if (!blob) throw new Error("empty screenshot");
        return blob;
      });
  }

  function _toast(kind, msg) {
    if (window.appToast) window.appToast({ kind: kind, msg: msg });
  }

  function submit() {
    var titleEl = document.getElementById("issue-title");
    var title = (titleEl.value || "").trim();
    if (!title) {
      titleEl.focus();
      return;
    }
    var bodyEl = document.getElementById("issue-body");
    var body = (bodyEl.value || "").trim();
    var kindEl = dlg.querySelector('input[name="issue-kind"]:checked');
    var kind = kindEl ? kindEl.value : "bug";
    var wantShot = document.getElementById("issue-screenshot").checked;
    var btn = document.getElementById("issue-submit");
    btn.disabled = true;

    version()
      .then(function (v) {
        var payload = {
        title: title,
        body: body || null,
        kind: kind,
        page_url: pageUrl(),
        context: envelope(v),
        // Tells the mirror a screenshot is on its way, so the operator
        // message waits for it instead of promising a link the row has not
        // got yet — the upload is a separate PUT that cannot start until
        // this POST has answered.
        expect_screenshot: wantShot,
      };
        return fetch("/api/issues", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }).then(function (r) {
          return r.json().then(function (j) {
            return { status: r.status, body: j };
          });
        });
      })
      .then(function (res) {
        if (res.status !== 201) {
          var msg =
            (res.body && (res.body.message || (res.body.detail && res.body.detail.message))) ||
            "Couldn't send the report.";
          if (res.status === 501) msg = "Reporting needs the Postgres app-state backend on this instance.";
          _toast("warn", msg);
          btn.disabled = false;
          return;
        }
        var row = res.body;
        if (wantShot) btn.textContent = "Capturing\u2026";
        var done = wantShot
          ? screenshotBlob()
              .then(function (blob) {
                return fetch("/api/issues/" + row.id + "/screenshot", {
                  method: "PUT",
                  credentials: "include",
                  headers: { "Content-Type": "image/png" },
                  body: blob,
                });
              })
              .then(function (r) {
                if (!r.ok) _toast("warn", "Reported, but the screenshot was not attached.");
              })
              .catch(function () {
                _toast("warn", "Reported, but the screenshot could not be captured.");
              })
          : Promise.resolve();
        return done.then(function () {
          close();
          btn.disabled = false;
          btn.textContent = "Report";
          titleEl.value = "";
          bodyEl.value = "";
          _toast("ok", "Reported as #" + row.number + " — you'll hear back.");
        });
      })
      .catch(function () {
        var fb = document.getElementById("issue-fallback");
        var fbText = document.getElementById("issue-fallback-text");
        if (fb && fbText) {
          fb.hidden = false;
          fbText.value = "[" + kind + "] " + title + "\n\n" + body + "\n\nPage: " + pageUrl();
        }
        btn.disabled = false;
      });
  }

  ["rail-report-issue", "rail-report-issue-menu"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", open);
  });
  var cancelBtn = document.getElementById("issue-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", close);
  var submitBtn = document.getElementById("issue-submit");
  if (submitBtn) submitBtn.addEventListener("click", submit);

  window.AgnesIssueReport = { open: open };
})();
