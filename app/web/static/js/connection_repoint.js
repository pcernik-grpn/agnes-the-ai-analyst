/* Shared handling for the server's 409 `connection_change_affects_registrations`.
 *
 * FOUR surfaces write a `data_source.<source>` connection: the instance-settings
 * editor, the Snowflake and Databricks branches of the Add-data wizard, and the
 * first-boot setup flow. The guard that raises the 409 lives on the two
 * endpoints all four post to, so a confirm flow taught to only one of them
 * turns the other three into dead ends — an operator rotating a credential from
 * the wizard would be stopped by a refusal it cannot answer. This is that flow,
 * in one place, for all of them.
 *
 * Also exports the detail-to-text helper: a FastAPI `detail` is a string on most
 * errors but an object on this 409 (and an array on a 422), and three of those
 * four surfaces used to interpolate it straight into an Error — printing the
 * literal "[object Object]" at the exact moment the operator needed to read why.
 */
(function () {
  "use strict";

  var ERROR_CODE = "connection_change_affects_registrations";

  window.apiDetailText = function (detail, fallback) {
    if (detail == null || detail === "") return fallback || "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc, msg, type}, …]
      return detail
        .map(function (d) {
          if (!d || !d.msg) return String(d);
          var loc = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null;
          return loc ? loc + ": " + d.msg : d.msg;
        })
        .join("\n");
    }
    return detail.hint || detail.message || detail.error || JSON.stringify(detail);
  };

  function isRepointRefusal(status, data) {
    return status === 409 && data && data.detail && data.detail.error === ERROR_CODE;
  }

  /* Plain text, not HTML: `confirmModal` renders through textContent with
     `white-space: pre-wrap`, so newlines survive and nothing needs escaping. */
  function repointMessage(detail) {
    var n = detail.affected_tables;
    var lines = [
      n + " registered table" + (n === 1 ? "" : "s") +
        " resolve against the current " + detail.source + " connection.",
      "",
    ];
    (detail.changes || []).forEach(function (c) {
      lines.push("    " + c.field + ":  " + JSON.stringify(c.before) + "  →  " + JSON.stringify(c.after));
    });
    var sample = detail.sample_tables || [];
    if (sample.length) {
      lines.push("");
      lines.push("For example: " + sample.join(", ") + (sample.length < n ? ", …" : ""));
    }
    lines.push("");
    lines.push(
      "They keep naming the old database/schema and will stop resolving after " +
        "this change — re-register them, or make sure the new upstream carries " +
        "the same names."
    );
    lines.push(
      "A failing table reports its last SUCCESSFUL sync until the next run, so " +
        "the instance can look healthy while its data goes stale."
    );
    return lines.join("\n");
  }

  function askToProceed(detail) {
    var opts = {
      title: "This repoints a live data source",
      message: repointMessage(detail),
      confirmText: "Yes, repoint",
      cancelText: "Cancel",
      danger: true,
    };
    if (typeof window.confirmModal === "function") return window.confirmModal(opts);
    // Degrade rather than trap the operator on a page without modal.js.
    return Promise.resolve(window.confirm(opts.title + "\n\n" + opts.message));
  }

  /* POST a connection-config payload, answering the repoint refusal if it comes.
   *
   * Resolves {ok, status, data, cancelled} — `cancelled: true` means the
   * operator declined the confirmation and NOTHING was written; callers should
   * treat it as a no-op, not as an error.
   */
  window.saveConnectionConfig = async function (url, payload, options) {
    var opts = options || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});

    async function post(body) {
      var r = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: headers,
        body: JSON.stringify(body),
      });
      var data = await r.json().catch(function () { return {}; });
      return { ok: r.ok, status: r.status, data: data };
    }

    var res = await post(payload);
    if (!isRepointRefusal(res.status, res.data)) return res;

    var proceed = await askToProceed(res.data.detail);
    if (!proceed) return { ok: false, status: res.status, data: res.data, cancelled: true };

    return post(Object.assign({}, payload, { confirm_connection_change: true }));
  };
})();
