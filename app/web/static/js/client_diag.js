/* client_diag.js — client-side diagnostics ring buffer.
 *
 * Loaded FIRST and non-deferred by _app_scripts.html (before every other
 * script in the file, including modal.js/app.js) so its window.onerror /
 * unhandledrejection listeners and fetch() wrapper are installed before
 * anything later on the page can throw or fetch. Keeps the last 20 entries
 * IN MEMORY ONLY — nothing is sent anywhere until the "Report a problem"
 * dialog (issue_report.js) reads a snapshot at submit time.
 *
 * Exposes window.AgnesDiag.snapshot() -> {recent_errors: [...]}.
 * See docs/issue-reporting.md.
 */
(function () {
  "use strict";

  var MAX = 20;
  var LIMIT = 300;
  var buf = [];

  function push(entry) {
    buf.push(entry);
    if (buf.length > MAX) buf.shift();
  }

  function trunc(s) {
    s = String(s == null ? "" : s);
    return s.length > LIMIT ? s.slice(0, LIMIT) : s;
  }

  function stripQuery(u) {
    try {
      var x = new URL(u, location.href);
      return x.origin + x.pathname;
    } catch (e) {
      return trunc(u);
    }
  }

  window.addEventListener("error", function (e) {
    push({
      ts: new Date().toISOString(),
      kind: "error",
      message: trunc(e.message),
      source: trunc((e.filename || "") + ":" + (e.lineno || 0)),
    });
  });

  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    push({
      ts: new Date().toISOString(),
      kind: "rejection",
      message: trunc(r && (r.message || r)),
    });
  });

  if (window.fetch) {
    var orig = window.fetch;
    window.fetch = function (input, init) {
      var method = (init && init.method) || (input && input.method) || "GET";
      var url = typeof input === "string" ? input : (input && input.url) || "";
      return orig.apply(this, arguments).then(
        function (res) {
          if (!res.ok) {
            push({
              ts: new Date().toISOString(),
              kind: "fetch",
              message: method.toUpperCase() + " " + stripQuery(url),
              status: res.status,
            });
          }
          return res;
        },
        function (err) {
          push({
            ts: new Date().toISOString(),
            kind: "fetch",
            message: method.toUpperCase() + " " + stripQuery(url) + " failed: " + trunc(err && err.message),
          });
          throw err;
        }
      );
    };
  }

  window.AgnesDiag = {
    snapshot: function () {
      return { recent_errors: buf.slice() };
    },
  };
})();
