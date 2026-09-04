/* Extraction breakdown (2026-09-04) — "how many documents did we get, how
   many did we not, broken down by file type and by reason, and how much of
   the corpus is silently empty" for one SharePoint connection.

   A SEPARATE panel from the fleet table above (admin_extraction.js): that
   table is a live poll of run COUNTERS; this fetches
   `GET /api/admin/sharepoint/connections/{id}/extraction/breakdown` once
   per "Apply" and renders two tables driven by the product's shared
   client-side filter engine (`filter_toolbar.js`, already loaded by every
   page that needs a search/sort table) rather than a private copy of one —
   same discipline the Tables lens and the Data page already follow.

   `_extEsc` (HTML-escaping) is reused from data_sources_extraction_
   observability.js, loaded earlier in the same page — a plain global
   function, not an ES module export, so any script tag after it can call
   it directly.
*/

let extbdExtToolbar = null;
let extbdReasonToolbar = null;
let extbdConnLoaded = false;

function extbdHumanBytes(n) {
  n = Number(n) || 0;
  if (n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? String(v) : v.toFixed(1)) + " " + units[i];
}

function extbdCountBytes(cell) {
  const count = (cell && cell.count) || 0;
  const bytes = (cell && cell.bytes) || 0;
  if (!count) return "—";
  return bytes > 0 ? `${count} <span class="extbd-sub-count">(${extbdHumanBytes(bytes)})</span>` : String(count);
}

async function extbdLoadConnections() {
  const sel = document.getElementById("extbd-conn");
  if (!sel || extbdConnLoaded) return;
  try {
    const r = await fetch("/api/admin/sharepoint/extraction/runs?all=1", { credentials: "include" });
    if (!r.ok) return;
    const body = await r.json();
    const rows = (body.connections || [])
      .slice()
      .sort((a, b) => (a.connection_name || "").localeCompare(b.connection_name || ""));
    for (const row of rows) {
      const opt = document.createElement("option");
      opt.value = row.connection_id;
      opt.textContent = row.connection_name || row.connection_id;
      sel.appendChild(opt);
    }
    extbdConnLoaded = true;
  } catch (e) {
    /* Best-effort: the picker just stays at "Choose a connection…" — the
       fleet table above already shows the same fetch failing louder. */
  }
}

function extbdSetMessage(text) {
  const empty = document.getElementById("extbd-empty");
  const body = document.getElementById("extbd-body");
  empty.textContent = text;
  empty.hidden = false;
  body.hidden = true;
}

function extbdRenderSummary(data) {
  const el = document.getElementById("extbd-summary");
  const rec = data.reconciliation;
  const unexplainedClass = rec.unexplained !== 0 ? " warn" : "";
  el.innerHTML = `
    <div class="extbd-card"><div class="label">Seen</div><div class="value">${rec.seen}</div></div>
    <div class="extbd-card"><div class="label">Indexed</div><div class="value">${rec.indexed}</div></div>
    <div class="extbd-card"><div class="label">Accounted for</div><div class="value">${rec.accounted_for}</div></div>
    <div class="extbd-card"><div class="label">Unexplained</div><div class="value${unexplainedClass}">${rec.unexplained}</div></div>
  `;
  document.getElementById("extbd-runs-note").textContent =
    `${data.runs.considered} run(s) considered — ${data.runs.with_report} finished with a full report, ` +
    `${data.runs.progress_only} interrupted (progress-only; a few counters undercount for those).`;

  const banner = document.getElementById("extbd-empty-text-banner");
  const emptyText = data.failures.empty_text;
  if (emptyText && emptyText.count > 0) {
    banner.hidden = false;
    banner.className = "extbd-empty-text-banner";
    banner.innerHTML =
      `<strong>${emptyText.count}</strong> <span>converted fine but produced no extractable text — ` +
      `usually needs OCR, not a broken pipeline.</span>`;
  } else {
    banner.hidden = true;
    banner.innerHTML = "";
  }

  const truncated = document.getElementById("extbd-failures-truncated");
  if (data.failures.truncated) {
    truncated.hidden = false;
    truncated.innerHTML =
      `<div class="apg-strip apg-strip--warn" role="status">Showing at least ${data.failures.listed} ` +
      `failure(s) — a contributing run's own failure list hit its cap, so the true count is higher.</div>`;
  } else {
    truncated.hidden = true;
    truncated.innerHTML = "";
  }
}

function extbdRenderExtensionTable(rows) {
  const tbody = document.getElementById("extbd-ext-tbody");
  tbody.innerHTML = rows
    .map((row) => {
      const search = _extEsc(row.extension).toLowerCase();
      return `<tr data-search="${search}">
        <td>${_extEsc(row.extension)}</td>
        <td class="extbd-num" data-indexed="${row.indexed.count}">${extbdCountBytes(row.indexed)}</td>
        <td class="extbd-num" data-rejected="${row.rejected.count}">${extbdCountBytes(row.rejected)}</td>
        <td class="extbd-num" data-processing="${row.processing.count}">${extbdCountBytes(row.processing)}</td>
        <td class="extbd-num" data-pending="${row.pending.count}">${extbdCountBytes(row.pending)}</td>
        <td class="extbd-num" data-needs-review="${row.needs_review.count}">${extbdCountBytes(row.needs_review)}</td>
        <td class="extbd-num" data-failed="${row.failed}">${row.failed || "—"}</td>
        <td class="extbd-num" data-empty-text="${row.empty_text}">${row.empty_text || "—"}</td>
      </tr>`;
    })
    .join("");

  if (extbdExtToolbar) extbdExtToolbar.destroy();
  extbdExtToolbar = window.FilterToolbar
    ? window.FilterToolbar.init({
        rows: "#extbd-ext-tbody tr",
        search: { el: "#extbd-ext-search", attr: "data-search" },
        count: { el: "#extbd-ext-count", noun: "file type" },
        noResults: "#extbd-ext-noresults",
      })
    : null;
}

function extbdRenderReasonTable(rows) {
  const tbody = document.getElementById("extbd-reason-tbody");
  tbody.innerHTML = rows
    .map((row) => {
      const search = (row.reason || "").toLowerCase();
      const exts = Object.entries(row.by_extension || {})
        .sort((a, b) => b[1] - a[1])
        .map(([ext, n]) => `${_extEsc(ext || "(none)")}: ${n}`)
        .join(", ");
      return `<tr data-search="${_extEsc(search)}">
        <td>${_extEsc(row.reason)}</td>
        <td class="extbd-num">${row.count}</td>
        <td>${exts}</td>
      </tr>`;
    })
    .join("");

  if (extbdReasonToolbar) extbdReasonToolbar.destroy();
  extbdReasonToolbar = window.FilterToolbar
    ? window.FilterToolbar.init({
        rows: "#extbd-reason-tbody tr",
        search: { el: "#extbd-reason-search", attr: "data-search" },
        count: { el: "#extbd-reason-count", noun: "reason" },
        noResults: "#extbd-reason-noresults",
      })
    : null;
}

async function extbdFetch() {
  const connId = document.getElementById("extbd-conn").value;
  if (!connId) {
    extbdSetMessage("Choose a connection above to see its breakdown.");
    return;
  }
  const since = document.getElementById("extbd-since").value;
  const until = document.getElementById("extbd-until").value;
  const qs = new URLSearchParams();
  if (since) qs.set("since", since);
  if (until) qs.set("until", until);

  extbdSetMessage("Loading…");
  try {
    const r = await fetch(
      `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/breakdown?${qs}`,
      { credentials: "include" }
    );
    if (r.status === 501) {
      extbdSetMessage("This breakdown needs the Postgres app-state backend — this instance still runs the DuckDB one.");
      return;
    }
    if (r.status === 404) {
      extbdSetMessage("That connection could not be found.");
      return;
    }
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      const detail = body && body.detail;
      const msg = (detail && (detail.error || detail.message)) || `HTTP ${r.status}`;
      extbdSetMessage(`Could not load the breakdown: ${msg}`);
      return;
    }
    const data = await r.json();
    document.getElementById("extbd-empty").hidden = true;
    document.getElementById("extbd-body").hidden = false;
    extbdRenderSummary(data);
    extbdRenderExtensionTable(data.by_extension || []);
    extbdRenderReasonTable((data.failures && data.failures.by_reason) || []);
  } catch (e) {
    extbdSetMessage(`Request failed: ${e.message}`);
  }
}

document.getElementById("extbd-conn").addEventListener("change", extbdFetch);
document.getElementById("extbd-apply").addEventListener("click", extbdFetch);
document.getElementById("extbd-since").addEventListener("change", extbdFetch);
document.getElementById("extbd-until").addEventListener("change", extbdFetch);

extbdLoadConnections();
