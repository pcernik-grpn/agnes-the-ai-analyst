/* Extracted from admin_data_sources.html (perf follow-up, 2026-09-03) — was inlined on every page load (uncacheable); now a normal cached static asset, versioned by static_url()'s ?v=<mtime> cache-buster. */
const API_CONNECTIONS = "/api/admin/source-connections";
const API_REGISTER_TABLE = "/api/admin/register-table";

// Error text for a failed register-table call. `register-table` answers a
// FAILED remote-extract rebuild with 500 + `{status:"rebuild_failed", detail,
// message}` — the upstream reason (e.g. `Catalog Error: Table with name X does
// not exist! Did you mean "Y"?`) is the single most useful thing on the page,
// and reading only `detail` used to render a bare "✗ failed" for any endpoint
// that put it under `message`. Falls back through both keys, then to a
// non-empty default.
function _registerErrorText(body) {
  const b = body || {};
  const text = typeof b.detail === "string" ? b.detail : typeof b.message === "string" ? b.message : "";
  return text || "failed";
}

function _esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Hand-built `.ds-dropdown` markup (see _components.html's `dropdown` macro)
// for pairing with a <select> built entirely in a JS template literal, where
// the select doesn't exist yet at Jinja-render time. `options` is
// `[{value, label}]`; the caller is responsible for calling
// `window.dsDropdownInit` on the inserted host once, after the surrounding
// innerHTML assignment — matching `dsDropdownMarkup()` in
// admin_server_config.html.
function _dropdownMarkupHtml(id, targetId, ariaLabel, options, selected) {
  const current = options.some((o) => o.value === selected) ? selected : (options[0] ? options[0].value : null);
  const currentLabel = (options.find((o) => o.value === current) || {}).label || "";
  const items = options.map((o) => `<li class="ds-dropdown-menu-item${o.value === current ? " is-selected" : ""}" role="menuitemradio" aria-checked="${o.value === current}" tabindex="0" data-value="${_esc(o.value)}">${_esc(o.label)}</li>`).join("");
  const nameSpan = ariaLabel ? `<span class="ds-dropdown-name" id="${id}-name">${_esc(ariaLabel)}</span>` : "";
  const labelledBy = ariaLabel ? ` aria-labelledby="${id}-name ${id}-btn-label"` : "";
  return `<div class="ds-dropdown" data-ds-dropdown-target="${targetId}">
    <button type="button" class="ds-dropdown-btn" id="${id}-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="${id}-menu"${labelledBy}>
      ${nameSpan}<span class="ds-dropdown-btn-label" id="${id}-btn-label">${_esc(currentLabel)}</span>
      <svg class="ds-dropdown-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
    </button>
    <ul class="ds-dropdown-menu" id="${id}-menu" role="menu" aria-label="${_esc(ariaLabel || "Options")}" hidden>${items}</ul>
  </div>`;
}

// ds_dropdown.js only pushes a selection FROM the custom button+menu ONTO the
// paired native <select> — for a static option list (never rebuilt), setting
// `.value` straight from code needs this explicit re-sync or the paper-theme
// dropdown keeps showing whatever was picked last. Mutates the EXISTING
// items in place rather than rebuilding, since the option list itself never
// changes for these controls.
function _syncDropdownFromSelect(sel) {
  const dd = document.querySelector(`.ds-dropdown[data-ds-dropdown-target="${sel.id}"]`);
  if (!dd) return;
  const label = dd.querySelector(".ds-dropdown-btn-label");
  const opt = sel.options[sel.selectedIndex];
  dd.querySelectorAll('.ds-dropdown-menu [role="menuitemradio"]').forEach((item) => {
    const isSelected = item.dataset.value === sel.value;
    item.classList.toggle("is-selected", isSelected);
    item.setAttribute("aria-checked", isSelected ? "true" : "false");
  });
  if (label && opt) label.textContent = opt.textContent.trim();
}

// Mirrors register_table's identifier derivation in app/api/admin.py BYTE FOR
// BYTE: `request.name.strip().lower().replace(" ", "_")` validated against
// `re.fullmatch(r"[a-z_][a-z0-9_]*", table_id)`. Kept in lockstep so the
// bulk Keboola picker never treats a name as safe that the server then 422s
// on (or the reverse). Do not drift from the server regex without updating
// both.
function _wouldPassRegisterCheck(rawName) {
  const id = (rawName || "").trim().toLowerCase().replace(/ /g, "_");
  return /^[a-z_][a-z0-9_]*$/.test(id);
}

// A suggested identifier for a raw Keboola name the check above rejects —
// pre-filled into an editable input, never submitted silently (the "no
// silent rewrite" policy also documented above the BigQuery register-table
// check in admin.py). Broader than the server's own normalization: every RUN
// of characters outside [a-z0-9] collapses to a single underscore, not just
// spaces, so a Shopify-style `inventory-items` suggests `inventory_items`
// instead of leaving the admin to find the hyphen by trial and error.
function _suggestTableName(rawName) {
  const id = (rawName || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  // Server requires the identifier start with a letter or underscore; a
  // digit-leading name (e.g. `2024-orders`) would otherwise suggest an
  // id the register check still rejects.
  return /^[0-9]/.test(id) ? "_" + id : id;
}

// Server-rendered by the shared icon macro, read once and interpolated into
// the JS-built cards. Trusted markup by construction — it never touches user
// input — and it keeps this page from growing a private copy of two glyphs.
const ICO_CHEVRON = document.getElementById("ds-ico-chevron").innerHTML;
const ICO_CARET = document.getElementById("ds-ico-caret").innerHTML;

function showToast(msg, ok) {
  const el = document.getElementById("ds-toast");
  el.textContent = msg;
  el.className = "ds-toast show " + (ok ? "toast-ok" : "toast-fail");
  clearTimeout(el._t);
  // Only successes auto-dismiss. A failed token save reports the upstream
  // reason, and a 3.5s window meant it was gone before anyone read it — the
  // error got relayed as "I get a 502" with the actual cause lost.
  el.title = ok ? "" : "Click to dismiss";
  if (ok) {
    el._t = setTimeout(() => { el.className = "ds-toast"; }, 3500);
  }
}
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("ds-toast");
  if (el) el.addEventListener("click", () => { el.className = "ds-toast"; });
});

/* ── Connections list ─────────────────────────────────────────────────── */

let _connections = [];

function _secretBadgeHtml(row) {
  const tokenEnv = row.token_env || "";
  let src = "unset";
  if (row.has_secret === true) src = "vault";
  else if (tokenEnv) src = "env";
  const labels = { vault: "vault", env: "env", unset: "unset" };
  const classes = { vault: "badge-vault", env: "badge-env", unset: "badge-unset" };
  return `<span class="ds-badge ${classes[src] || "badge-unset"}">${labels[src] || "unset"}</span>`;
}

function _masterTokenFactHtml(row) {
  // Semantic-layer sync (Metastore) needs a separate Keboola *master* (project
  // owner) token — not the plain storage token above. Only keboola connections
  // support this vault slot.
  //
  // Named "Semantic-layer token" with the system word in the hint, not
  // "Master token (semantic layer)": an admin reading a settings list needs to
  // know WHAT IT IS FOR first — the Keboola noun is the detail, and it is one
  // hover away.
  if (row.source_type !== "keboola") return "";
  const id = row.id;
  const hasMaster = row.has_master_secret === true;
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" role="note" data-tip="Keboola calls this the project's master (owner) token — required only for the semantic layer's metrics and glossary." aria-label="Semantic-layer token. Keboola calls this the project's master (owner) token — required only for the semantic layer's metrics and glossary.">Semantic-layer token</span>
    <span class="ds-src__fact-v"><span class="ds-badge ${hasMaster ? "badge-vault" : "badge-unset"}">${hasMaster ? "set" : "not set"}</span></span>
    <span class="ds-src__fact-a">
      <button type="button" class="btn btn-secondary" onclick="toggleMasterToken('${id}')">${hasMaster ? "Rotate" : "Set token"}</button>
      ${hasMaster ? `<button type="button" class="btn btn-secondary" style="color:var(--ds-accent-danger-ink); border-color:var(--ds-accent-danger-line);" onclick="removeMasterToken('${id}')">Remove</button>` : ""}
    </span>
  </div>
  <div class="ds-rotate-row" id="ds-master-row-${id}">
    <input type="password" id="ds-master-input-${id}" placeholder="paste master (owner) token" autocomplete="off">
    <button type="button" class="btn btn-primary" onclick="saveMasterToken('${id}')">Save token</button>
    <button type="button" class="btn btn-secondary" onclick="toggleMasterToken('${id}')">Cancel</button>
  </div>
  <div class="ds-token-mismatch" id="ds-mismatch-${id}" hidden></div>`;
}


/* The connector's identity: the tile's two letters and its palette class.
   A source whose type this build has never heard of still gets a card — it
   falls back to its own initials rather than being dropped from the list,
   because a table that exists must have a source that is visible. */
/* `wizard` is the picker's own key for the same connector, which is not
   always the registry's `source_type`: files register as `local` but the
   picker calls that pane `csv`. Naming the translation here keeps it out of
   the callers, where a mismatch silently hides every form in step 1. */
const _SVG_LOGO = {
  bigquery: `<svg class="connector-logo" viewBox="0 0 24 24" fill="currentColor" role="img" aria-label="BigQuery"><rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/></svg>`,
  snowflake: `<svg class="connector-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Snowflake"><path d="M12 3v18M4.5 6l15 12M4.5 18l15-12"/></svg>`,
  databricks: `<svg class="connector-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" role="img" aria-label="Databricks"><path d="M12 2l8.7 5v10L12 22l-8.7-5V7L12 2z"/></svg>`,
  local: `<svg class="connector-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Files"><path d="M12 16v-4M8 12l4-4 4 4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>`,
};

const CONNECTOR = {
  keboola:    { abbr: "KB", cls: "keboola",   wizard: "keboola" },
  bigquery:   { abbr: "BQ", cls: "bigquery",  wizard: "bigquery" },
  snowflake:  { abbr: "SF", cls: "snowflake", wizard: "snowflake" },
  databricks: { abbr: "DB", cls: "databricks", wizard: "databricks" },
  jira:       { abbr: "JR", cls: "jira",      wizard: "jira" },
  local:      { abbr: "FS", cls: "local",     wizard: "csv" },
  // The connect wizard for this type ships separately; this entry only
  // gives an EXISTING sharepoint connection row (however it got created) a
  // logo/palette instead of falling back to raw initials. `local`'s palette
  // is reused rather than adding a new one — this is a file source too.
  sharepoint: { abbr: "SP", cls: "local",     wizard: "sharepoint" },
};
function _connector(type) {
  return CONNECTOR[type] || { abbr: (type || "?").slice(0, 2).toUpperCase(), cls: "local", wizard: "keboola" };
}
function _connectorLogo(type) {
  return _SVG_LOGO[type] || "";
}

function _relAge(minutes) {
  if (minutes === null || minutes === undefined) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const h = Math.floor(minutes / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.floor(h / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

/* A file source's pipeline is a DIFFERENT shape entirely — crawl → text
   extraction + scan transcription → facts → graph (spec §13.2 "Source
   card"), not tables/sync/feeds. Cells are plain (not links): there is no
   monitoring page to send an admin to — "no new pages" is the point — so
   the strip stays a verdict, and the itemized detail lives one click below
   in the error-badge drawer (`_sharepointFactsHtml`). */
function _sharepointPipelineStripHtml(row) {
  const fs = (SOURCE_PIPELINES[row.id] || {}).file_source;
  if (!fs) return "";
  const extract = fs.extract || {};
  const extractedTotal = Object.values(extract).reduce((a, b) => a + b, 0);
  const extractTitle = extractedTotal
    ? Object.entries(extract).map(([k, v]) => `${k}: ${v}`).join(" · ")
    : "Nothing extracted yet";
  const queue = fs.queue || { items: 0 };
  const cells = [
    `<div class="ds-pipe__cell">
       <span class="ds-pipe__k">Crawl</span>
       <span class="ds-pipe__v">${fs.crawl.documents} document${fs.crawl.documents === 1 ? "" : "s"}</span>
       <span class="ext-live" id="ext-crawl-live-${row.id}" hidden></span>
     </div>`,
    `<div class="ds-pipe__cell" title="${_esc(extractTitle)}">
       <span class="ds-pipe__k">Extraction</span>
       <span class="ds-pipe__v ${extractedTotal ? "" : "is-warn"}">${extractedTotal ? `${extract.indexed || 0} indexed` : "Nothing yet"}</span>
     </div>`,
    // `fs.graph` is never server-rendered (perf follow-up, 2026-09-03): the
    // fact/edge counts behind it cost 2 SQL statements PER SCOPE, so
    // computing them for every SharePoint connection on the page at once
    // was what dominated the page's own load time on a live instance. This
    // cell starts as a loading placeholder and `_fetchSharepointGraphCounts`
    // (called once cards are painted) fills it in per connection, from
    // `GET .../facts-graph-counts` — a separate, non-blocking request.
    `<div class="ds-pipe__cell" id="ds-sp-graph-${row.id}">
       <span class="ds-pipe__k">Facts → graph</span>
       <span class="ds-pipe__v" style="color:var(--ds-text-muted)">loading…</span>
     </div>`,
    `<div class="ds-pipe__cell" title="Rejected/deferred items from the last ingest run awaiting review — a count, not a price; nothing here invents a dollar figure.">
       <span class="ds-pipe__k">Review queue</span>
       <span class="ds-pipe__v">${(queue.items || 0) ? `${queue.items} item${queue.items === 1 ? "" : "s"}` : "empty"}</span>
     </div>`,
  ];
  return `<div class="ds-pipe ds-pipe--${cells.length}">${cells.join("")}</div>`;
}

function _pipelineStripHtml(row) {
  if (row.source_type === "sharepoint") return _sharepointPipelineStripHtml(row);
  const p = SOURCE_PIPELINES[row.id];
  if (!p) return "";
  const cells = [];

  // Tables. `unlinked` rows carry no connection_id and this source type has
  // several connections, so they cannot be attributed — named rather than
  // reported as "no tables yet", which read as an empty instance.
  const t = p.tables || { count: 0, unlinked: 0 };
  let tblText, tblTone = "", tblTitle = "Registered against this connection";
  if (t.count) {
    tblText = `${t.count} registered`;
    if (t.basis === "source_type") tblTitle = "Counted by source type — these rows predate per-connection tracking, and this is the only connection of this type";
  } else if (t.unlinked) {
    tblText = `${t.unlinked} unlinked`;
    tblTone = "is-warn";
    tblTitle = `${t.unlinked} table(s) of this source type predate per-connection tracking, so they cannot be attributed to one connection. They still sync and are still bundleable.`;
  } else {
    tblText = "No tables yet";
    tblTone = "is-warn";
  }
  // A source with nothing registered gets the VERB here rather than a link to
  // the Tables page, which is the one place that cannot fix it — you add
  // tables from the source, not from the list of tables that already exist.
  // It is also the card's primary action put back within one click, where the
  // reader is already looking at the reason to press it.
  if (!t.count && !t.unlinked) {
    const open = row.derived
      ? `openWizard('${_esc(row.source_type)}')`
      : `toggleBrowse('${row.id}')`;
    cells.push(`
      <button type="button" class="ds-pipe__cell" onclick="${open}">
        <span class="ds-pipe__k">Tables</span>
        <span class="ds-pipe__v is-warn">Add the first tables →</span>
      </button>`);
  } else {
    cells.push(`
      <a class="ds-pipe__cell" href="/admin/tables" title="${_esc(tblTitle)}">
        <span class="ds-pipe__k">Tables</span>
        <span class="ds-pipe__v ${tblTone}">${tblText}</span>
      </a>`);
  }

  // Sync — or, for a connector that never pulls, the thing that CAN go wrong
  // with it instead. A BigQuery source is queried live, so "never synced" is
  // its permanent, correct and useless state; what its admin actually watches
  // is the cost guard. Jira arrives over webhooks and files are replaced by
  // hand, so both keep the freshness reading but say it in their own words.
  if (p.cost) {
    const costTitle = p.cost.title || "Refused above these — a query over the scan cap returns remote_scan_too_large. Editable in server config.";
    cells.push(`
      <a class="ds-pipe__cell" href="/admin/server-config"
         title="${_esc(costTitle)}">
        <span class="ds-pipe__k">Cost guard</span>
        <span class="ds-pipe__v">${_esc(p.cost.scan)} scan · ${_esc(p.cost.materialize)} materialize</span>
      </a>`);
  } else {
    const s = p.sync || {};
    const label = { jira: "Webhook", local: "Freshness" }[row.source_type] || "Sync";
    const never = { jira: "No events yet", local: "Never updated" }[row.source_type] || "Never synced";
    let syncTone = "", syncText;
    if (s.errors) { syncTone = "is-err"; syncText = `${s.errors} failing`; }
    else if (s.last_sync) { syncTone = "is-ok"; syncText = `✓ ${_relAge(s.age_minutes)}`; }
    else { syncTone = "is-warn"; syncText = never; }
    const syncTitle = s.title ? `title="${_esc(s.title)}"` : "";
    cells.push(`
      <a class="ds-pipe__cell" href="/admin/sync" ${syncTitle}>
        <span class="ds-pipe__k">${label}</span>
        <span class="ds-pipe__v ${syncTone}">${syncText}</span>
      </a>`);
  }

  // Semantic layer (Keboola only)
  if (p.semantic) {
    const sem = p.semantic;
    const semText = !sem.token
      ? "Token not set"
      : (sem.metrics || sem.terms)
        ? `✓ ${sem.metrics} metric${sem.metrics === 1 ? "" : "s"} · ${sem.terms} term${sem.terms === 1 ? "" : "s"}`
        : "Nothing imported yet";
    const semTone = !sem.token ? "is-warn" : (sem.metrics || sem.terms) ? "is-ok" : "is-warn";
    // "Token not set" cannot be fixed on /admin/semantic-layer — the page
    // renders fine, it just has no token field; the token is set on this
    // same card (Actions → Semantic-layer token), so a STORED connection's
    // cell calls that action directly instead of navigating to a page that
    // cannot help. A derived card (no stored connection — see `row.derived`
    // above) carries no such widget at all — `_masterTokenFactHtml` is never
    // rendered for it — so it keeps the base link; there is nothing on the
    // card itself to point at instead.
    if (!sem.token && !row.derived) {
      cells.push(`
        <button type="button" class="ds-pipe__cell" onclick="toggleMasterToken('${row.id}')">
          <span class="ds-pipe__k">Semantic layer health</span>
          <span class="ds-pipe__v ${semTone}">${semText}</span>
        </button>`);
    } else {
      cells.push(`
        <a class="ds-pipe__cell" href="/admin/semantic-layer">
          <span class="ds-pipe__k">Semantic layer health</span>
          <span class="ds-pipe__v ${semTone}">${semText}</span>
        </a>`);
    }
  }

  // Feeds — the end of the chain: who actually gets this source's data.
  const f = p.feeds || { packages: 0, groups: 0, people: 0 };
  let feedText;
  if (!f.packages) feedText = "0 packages";
  else if (f.people === -1) feedText = `${f.packages} package${f.packages === 1 ? "" : "s"} → everyone`;
  else feedText = `${f.packages} package${f.packages === 1 ? "" : "s"} → ${f.people} ${f.people === 1 ? "person" : "people"}`;
  cells.push(`
    <a class="ds-pipe__cell" href="/admin/data-packages"
       title="${f.packages ? "Packages holding this source's tables, and who they reach" : "Tables reach analysts only through a data package"}">
      <span class="ds-pipe__k">Feeds</span>
      <span class="ds-pipe__v ${f.packages ? "" : "is-warn"}">${feedText}</span>
    </a>`);

  return `<div class="ds-pipe ds-pipe--${cells.length}">${cells.join("")}</div>`;
}

/* ── The next step ─────────────────────────────────────────────────────────
   The strip above reports; this line acts. It renders ONLY while this
   source's chain is unfinished — the server decides, in `_source_next_step()`
   (app/web/router.py), from the very cells the strip just drew, so a card can
   never show "2 packages → 5 people" over a row telling its admin to bundle
   something. Nothing to say → nothing rendered, so a finished setup is not
   nagged and three healthy cards stay three quiet cards.

   Every state carries the same second link: the Access page's Simulate lens,
   which previews a person's Library from the same projection /library itself
   runs. That is the honest answer to "did that actually work?", and this page
   — where an admin arrives after connecting a source and then finds their
   Library empty — is exactly where it was missing. */
function _nextStepHtml(row) {
  const step = ((SOURCE_PIPELINES[row.id] || {}).feeds || {}).next;
  if (!step) return "";
  return `
<div class="ds-next" data-next-step="${_esc(step.key)}">
  <span class="ds-next__txt">${_esc(step.text)}</span>
  <span class="ds-next__end">
    <a class="btn btn-primary btn-sm" href="${_esc(step.href)}">${_esc(step.cta)} &rarr;</a>
    <a class="ds-next__verify" href="${_esc(step.verify_href)}"
       title="Preview a person's Library exactly as their groups leave it — the same projection /library runs">${_esc(step.verify_cta)} &rarr;</a>
  </span>
</div>`;
}

/* ── The card's one status word ────────────────────────────────────────────
   A fold over the strip below it, in severity order, so the reader gets a
   VERDICT before the evidence and can skip a healthy card without reading
   four cells. The order is the argument: broken beats empty beats
   undelivered.

     err   a sync is failing            — data is stale and nobody was told
     warn  no tables registered         — the source does nothing yet
     warn  tables reach nobody          — the end of the chain is open
     ok    everything above is false

   The semantic-layer token is deliberately NOT in this ladder. It is
   optional — a project with metrics disabled is not unhealthy — so its
   absence stays a warn on its own cell, where it means "this cell is off"
   rather than "this source is broken". Turning an opt-in feature into a
   card-level warning is how a status chip becomes decoration. */
function _sourceHealth(row) {
  const p = SOURCE_PIPELINES[row.id];
  if (!p) return null;
  if (row.source_type === "sharepoint") return _sharepointHealth(p.file_source);
  const s = p.sync || {}, t = p.tables || {}, f = p.feeds || {};
  if (s.errors) return { tone: "is-err", label: `${s.errors} failing sync${s.errors === 1 ? "" : "s"}` };
  if (!t.count && !t.unlinked) return { tone: "is-warn", label: "No tables yet" };
  if (!f.packages) return { tone: "is-warn", label: "Reaches nobody" };
  return { tone: "is-ok", label: "Healthy" };
}

/* The file-source verdict has nothing to do with "tables" — its own ladder:
   an ungranted collection (fail-closed, spec §13.1) is the worst silent
   state, then any error in the last run, else healthy. */
function _sharepointHealth(fs) {
  if (!fs) return null;
  const identity = fs.identity || {};
  if (identity.collections_no_group) {
    return { tone: "is-warn", label: `${identity.collections_no_group} collection${identity.collections_no_group === 1 ? "" : "s"} with no group` };
  }
  const lastRun = fs.last_run;
  const errCount = lastRun ? lastRun.rejected_quotes.length + lastRun.protocol_errors.length : 0;
  if (errCount) return { tone: "is-warn", label: `${errCount} error${errCount === 1 ? "" : "s"} in last run` };
  return { tone: "is-ok", label: "Healthy" };
}

/* One line of identity under the name: where this source is, and which
   project it opens. Three stacked 11px lines (name / project / host) said one
   thing in three places; the middot list says it once, and the part that
   truncates is the tail, which is the least identifying end of a host. */
function _sourceSubtitle(row) {
  const config = row.config || {};
  const bits = [];
  if (row.derived) {
    if (row.subtitle) bits.push(_esc(row.subtitle));
  } else if (row.source_type === "sharepoint") {
    bits.push(_sharepointIdentityLine(config));
  } else {
    const stackUrl = (config.stack_url || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
    bits.push(stackUrl ? `<code>${_esc(stackUrl)}</code>` : "(no connection URL)");
    if (config.project_id != null) {
      bits.push(`${_esc(config.project_name || "unnamed")} · project ${_esc(String(config.project_id))}`);
    }
  }
  return bits.join(" · ");
}

/* SharePoint has no "connection URL" — Graph auth is tenant + app
   registration, not a host — so the generic branch above always fell
   through to "(no connection URL)", which tells the admin nothing true or
   false. Two honest facts instead: the tenant (shortened — it is a GUID,
   the value itself is not the identifying part, just that it's stable) and
   how many scopes are selected, plus their common site when there is one.
   `display_path`'s first " / "-segment is the site name
   (`connectors/sharepoint/graph_client.py`'s BFS prefixes every match with
   `[site["name"], drive["name"]]` before appending folder names). Graph's
   `webUrl` is never fetched or stored on a scope row — verified against
   `app/api/admin_sharepoint.py`, where a scope is exactly
   `{source_scope_id, display_path, anonymize, collection_id}` — so there is
   no real link to prefer over this summary. */
function _sharepointIdentityLine(config) {
  const bits = [];
  const tenantId = String(config.tenant_id || "");
  bits.push(tenantId ? `<code>${_esc(tenantId.slice(0, 8))}…</code>` : "(no tenant configured)");
  const scopes = Array.isArray(config.scopes) ? config.scopes : [];
  if (scopes.length) {
    const sites = new Set(scopes.map((s) => String((s && s.display_path) || "").split(" / ")[0]));
    const siteLabel = sites.size === 1 ? [...sites][0] : `${sites.size} sites`;
    bits.push(`${scopes.length} scope${scopes.length === 1 ? "" : "s"} · ${_esc(siteLabel)}`);
  } else {
    bits.push("no scopes selected");
  }
  return bits.join(" · ");
}

/* Per-connection facts-extraction policy override (retry_mode / transport /
   provider) — `PATCH .../extraction/facts-config`
   (`app/api/admin_extraction.py::patch_extraction_facts_config`). Its own
   render helper, its own save function (`saveSpFactsPolicy` below), so this
   control never entangles with the run-options row or the "Extract facts
   now" button it sits next to. The three selects read the CONNECTION-level
   override straight off `row.config.extraction.facts` — the same rows
   payload `loadConnections()` already fetched, no extra round trip — and an
   empty selection ("Instance default") means "no override", exactly what
   the PATCH body's `null` clears. The resolved value and its source
   ("connection"/"instance") are shown only after Save, from the endpoint's
   own response — there is no resolver on this side to guess it beforehand.
   `provider` exists so a connection whose Anthropic key has hit its
   workspace usage cap can be pinned to `vertex` without an instance-wide
   `ai.provider` change. */
function _extRenderFactsPolicy(row) {
  const facts = ((row.config || {}).extraction || {}).facts || {};
  const retryMode = typeof facts.retry_mode === "string" ? facts.retry_mode : "";
  const transport = typeof facts.transport === "string" ? facts.transport : "";
  const provider = typeof facts.provider === "string" ? facts.provider : "";
  const vertexRegion = typeof facts.vertex_region === "string" ? facts.vertex_region : "";
  const opt = (current, value, label) =>
    `<option value="${_esc(value)}"${current === value ? " selected" : ""}>${_esc(label)}</option>`;
  const label = _esc(row.name || row.id || "");
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Per-connection override for the facts-extraction retry policy, API transport, LLM provider and (provider=vertex only) Vertex region (cost levers). Leave all on 'Instance default' to inherit extraction.facts.* from instance.yaml.">Facts policy</span>
    <span class="ds-src__fact-v">
      <select id="ds-sp-factspolicy-retry-${row.id}" class="ds-dropdown-native" aria-label="Facts retry mode for ${label}">
        ${opt(retryMode, "", "Instance default")}
        ${opt(retryMode, "off", "Off")}
        ${opt(retryMode, "on_gate_fail", "On gate fail")}
        ${opt(retryMode, "always", "Always")}
      </select>
      <select id="ds-sp-factspolicy-transport-${row.id}" class="ds-dropdown-native" aria-label="Facts transport for ${label}">
        ${opt(transport, "", "Instance default")}
        ${opt(transport, "sync", "Sync")}
        ${opt(transport, "batch", "Batch")}
      </select>
      <select id="ds-sp-factspolicy-provider-${row.id}" class="ds-dropdown-native" aria-label="Facts provider for ${label}" onchange="_extToggleVertexRegionInput('${row.id}')">
        ${opt(provider, "", "Instance default")}
        ${opt(provider, "inherit", "Inherit (ai.provider)")}
        ${opt(provider, "anthropic", "Anthropic")}
        ${opt(provider, "vertex", "Vertex")}
      </select>
      <input type="text" id="ds-sp-factspolicy-vertexregion-${row.id}" class="ds-dropdown-native"
             placeholder="Instance default" value="${_esc(vertexRegion)}"
             style="display:${provider === "vertex" ? "" : "none"}"
             title="Vertex region override — only used when Provider above is Vertex. Google enforces Claude-on-Vertex quotas per region, so pinning connections to different regions raises effective throughput."
             aria-label="Vertex region override for ${label}">
      <span class="field-hint" id="ds-sp-factspolicy-status-${row.id}"></span>
    </span>
    <span class="ds-src__fact-a"><button type="button" class="btn btn-secondary" onclick="saveSpFactsPolicy('${row.id}')">Save</button></span>
  </div>`;
}

/* Shows/hides the Vertex region text input next to the "Facts policy"
   provider select — the region only means anything for a pass resolved to
   provider=vertex, so it stays hidden (but its value is preserved, never
   cleared) for every other selection. Fired on the provider select's own
   `onchange` (`_extRenderFactsPolicy` above). */
function _extToggleVertexRegionInput(id) {
  const providerSel = document.getElementById(`ds-sp-factspolicy-provider-${id}`);
  const regionInput = document.getElementById(`ds-sp-factspolicy-vertexregion-${id}`);
  if (!providerSel || !regionInput) return;
  regionInput.style.display = providerSel.value === "vertex" ? "" : "none";
}

/* Crawl filter (backfill lever) — `extraction.crawl.min_modified`,
   `PATCH .../extraction/crawl-config`
   (`app/api/admin_extraction.py::patch_extraction_crawl_config`). Lived only
   inside the "View configuration" drawer until a live walkthrough found
   nothing on the collapsed card hinting it existed (2026-09 gap 2) — moved
   here, next to its sibling "Facts policy" override, rather than
   duplicated: one place to set it from is what keeps the drawer's old
   read-out and the card from ever disagreeing. Pre-filled straight off
   `row.config.extraction.crawl.min_modified` — the SAME string
   `resolve_min_modified` reads server-side, and (per its own docstring)
   there is no instance-level default it could resolve against instead, so
   the raw stored value already IS the resolved one; no extra round trip
   needed just to prefill this input. The PATCH response's `{value,
   source}` — the same shape `GET …/extraction/config` returns — is shown
   after Save/Clear, never guessed beforehand, same discipline
   `_extRenderFactsPolicy` above uses for its own resolved read-out. */
function _extRenderCrawlFilter(row) {
  const crawl = ((row.config || {}).extraction || {}).crawl || {};
  const value = typeof crawl.min_modified === "string" ? crawl.min_modified : "";
  const statusText = value
    ? `Currently: files modified on/after ${value} (connection).`
    : "Currently: no filter — every file is crawled.";
  const label = _esc(row.name || row.id || "");
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Crawl only files modified on or after a date instead of the whole corpus — useful for a backfill. Sets extraction.crawl.min_modified for this connection; clearing it removes the filter, there is no instance-level default to fall back to.">Crawl filter</span>
    <span class="ds-src__fact-v">
      <input type="date" class="ds-dropdown-native" id="ds-sp-crawlfilter-date-${row.id}"
             value="${_esc(value)}" aria-label="Crawl only files modified on/after this date for ${label}">
      <span class="field-hint" id="ds-sp-crawlfilter-status-${row.id}">${_esc(statusText)}</span>
    </span>
    <span class="ds-src__fact-a">
      <button type="button" class="btn btn-secondary" onclick="crawlFilterSave('${row.id}')">Save</button>
      <button type="button" class="btn btn-secondary" onclick="crawlFilterClear('${row.id}')">Clear</button>
    </span>
  </div>`;
}

/* File-source card body (spec §13.2 "Source card"): schedule (static text —
   the crawl runs externally), certificate (origin + set-date, NEVER the
   value), identity matching (fail-closed grant coverage), and error badges
   from the LAST run report — each opens a drawer filtered to that one
   category. `_sharepointPipelineStripHtml` above already covers the
   crawl/extract/facts/graph counts, so this is everything ELSE on the card. */
function _sharepointFactsHtml(row) {
  const fs = (SOURCE_PIPELINES[row.id] || {}).file_source;
  if (!fs) {
    return `
    <div class="ds-src__fact">
      <span class="ds-src__fact-k">File source</span>
      <span class="ds-src__fact-v">Pipeline data needs a Postgres backend (facts graph is PG-only).</span>
    </div>`;
  }

  const cert = fs.certificate || {};
  const isSecretAuth = cert.auth_method === "client_secret";
  const certBadge = cert.error
    ? `<span class="ds-badge badge-unset">not configured</span>`
    : `<span class="ds-badge ${cert.origin === "vault" ? "badge-vault" : "badge-env"}">${_esc(cert.origin || "unset")}</span>`;
  const certDetail = cert.error
    ? _esc(cert.error)
    : cert.origin === "vault"
      ? (cert.set_at ? `set ${_esc(new Date(cert.set_at).toLocaleDateString())}` : "set date unknown")
      : `env <code>${_esc(cert.env_name || "")}</code>`;

  // Certificate METADATA — thumbprint (what the client actually presents,
  // compared against the identity provider), subject, and an expiry badge.
  // Derived server-side from the same stored PEM `certDetail` above already
  // resolved; never the private key. Rendered only once the connection's
  // settings resolved (`cert.error` already covers a settings-resolution
  // failure above — repeating "not configured" here would be noise): a
  // resolvable-but-unusable certificate (no CERTIFICATE block, or one that
  // fails to parse) gets its own single honest line rather than silently
  // vanishing (`cert.metadata_reason`).
  let certMetaHtml = "";
  if (!cert.error && cert.thumbprint_x5t) {
    const tone = cert.status === "expired" ? "badge-danger" : cert.status === "expiring_soon" ? "badge-warn" : "badge-ok";
    const expiresDate = cert.not_after ? new Date(cert.not_after).toLocaleDateString() : "unknown";
    const daysText = cert.expires_in_days < 0 ? `expired ${Math.abs(cert.expires_in_days)}d ago` : `${cert.expires_in_days}d left`;
    certMetaHtml = `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="The value the client actually presents (JWT x5t) — compare against the identity provider's app registration.">Thumbprint</span>
    <span class="ds-src__fact-v">
      <code>${_esc(cert.thumbprint_x5t)}</code>
      <button type="button" class="btn btn-secondary" onclick="_copySharepointThumbprint(this)">Copy</button>
    </span>
  </div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Subject</span>
    <span class="ds-src__fact-v" style="color:var(--ds-text-muted)">${_esc(cert.subject || "unknown")}</span>
  </div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Expires</span>
    <span class="ds-src__fact-v">
      <span class="ds-badge ${tone}">${_esc(cert.status || "")}</span>
      <span style="color:var(--ds-text-muted)">${_esc(expiresDate)} &middot; ${_esc(daysText)}</span>
    </span>
  </div>`;
  } else if (!cert.error && cert.metadata_reason) {
    const reasonText = cert.metadata_reason === "no_certificate_configured" ? "not configured" : "unreadable";
    certMetaHtml = `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Thumbprint</span>
    <span class="ds-src__fact-v" style="color:var(--ds-text-muted)">${_esc(reasonText)}</span>
  </div>`;
  }

  // Sharing state — the row used to say "N groups matched · M collections
  // with no group", which read as internal bookkeeping ("matched" against
  // what?). Owner feedback on live use: say what it actually means — every
  // scope collection either has someone who can see it, or it doesn't.
  const identity = fs.identity || { groups_matched: 0, collections_no_group: 0, collections_total: 0 };
  let identityTone, identityText;
  if (!identity.collections_total) {
    identityTone = "";
    identityText = "no scope collections yet";
  } else if (!identity.collections_no_group) {
    identityTone = "is-ok";
    identityText = "all scope collections have a group";
  } else {
    const n = identity.collections_no_group;
    identityTone = "is-warn";
    identityText = `${n} collection${n === 1 ? "" : "s"} ${n === 1 ? "has" : "have"} no group — only admins see them`;
  }

  // Scope rows (spec follow-up, TCRD-240/241): this connection's OWN
  // confirmed scopes (server shape: `admin_sharepoint._scope_out`, the
  // SAME one the connect wizard's step-3 "Share" preview reads) — clicking
  // one opens that wizard bound to this connection with the row
  // highlighted, rather than the wizard-only path the owner called "a
  // dumb path, make it clickable".
  //
  // NOT server-rendered (perf follow-up, 2026-09-03, second finding on the
  // same live instance): the enriched per-scope rows used to be capped at
  // 50 and STILL inlined ~170 KB of JSON across 8 connections — none of it
  // needed for first paint, since the "Sharing" row above already carries
  // the honest summary. `fs.scopes_total` (a cheap count) draws the
  // collapsed row; expanding it fetches the full, unbounded list from
  // `GET .../scopes` (`toggleSpScopesDrawer`, below) — the exact
  // fetch-on-expand shape the extraction card's drawer already uses.
  const scopesHtml = fs.scopes_total
    ? `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Scope collections</span>
    <span class="ds-src__fact-v">
      ${fs.scopes_total} scope${fs.scopes_total === 1 ? "" : "s"}
    </span>
    <span class="ds-src__fact-a">
      <button type="button" class="btn btn-secondary" onclick="toggleSpScopesDrawer('${row.id}')">View</button>
    </span>
  </div>
  <div class="ds-sp-scopes-drawer" id="ds-sp-scopes-drawer-${row.id}" hidden></div>`
    : "";

  // Anonymization (spec §9.2/§13.2): "requested" is the connect wizard's
  // checkbox (a wish); "declared" is the LAST ingest run's own claim. Never
  // collapse the two into one word — a collection with a checkbox ticked
  // but nothing declared yet reads "requested" (warn), never "anonymized".
  // Row only appears at all when at least one collection requests it — an
  // instance that never anonymizes gets no extra row.
  const anon = fs.anonymization || { requested: [], declared: [], pending: [] };
  const anonRowHtml = anon.requested.length
    ? `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Requested: the connect wizard's anonymize checkbox. Declared: the producer's last ingest run actually reported it anonymized — Agnes cannot verify content itself (see docs/anonymization.md).">Anonymization</span>
    <span class="ds-src__fact-v ${anon.pending.length ? "is-warn" : "is-ok"}">
      ${anon.declared.length ? `<span class="ds-badge badge-env">anonymized ${anon.declared.length}</span>` : ""}
      ${anon.pending.length ? `<span class="ds-badge badge-warn">anonymization requested ${anon.pending.length}</span>` : ""}
    </span>
  </div>`
    : "";

  const lastRun = fs.last_run;
  const badgeDefs = [
    ["rejected_quotes", "Rejected quotes"],
    ["deferred", "Deferred"],
    ["protocol_errors", "Protocol errors"],
    ["source_urls_rejected", "Citation links rejected"],
  ];
  const badgesHtml = lastRun
    ? badgeDefs.map(([key, label]) => {
        const n = (lastRun[key] || []).length;
        return `<button type="button" class="ds-badge ${n ? "badge-env" : "badge-unset"}" ${n ? "" : "disabled"}
                  onclick="toggleFileSourceDrawer('${row.id}', '${key}')" title="${_esc(label)} — click to filter the drawer below">
                  ${_esc(label)} ${n}
                </button>`;
      }).join(" ")
    : `<span style="color:var(--ds-text-muted)">No ingest runs yet</span>`;

  // In-Agnes extraction schedule (TCRD-226) — SEPARATE from the static
  // `Schedule` label above, which describes the EXTERNAL producer's own
  // crawl cadence. `in_agnes` is additive: an older/degraded cell shape
  // with no such key renders every part as its honest "off"/"never
  // run"/"no schedule configured" default rather than throwing.
  const inAgnes = fs.schedule.in_agnes || {};
  const inAgnesParts = [];
  // Honest-UI gate: `extraction_ready`/`extraction_unready_reason` are the
  // SAME two gates the manual trigger itself checks before enqueueing
  // (`app/api/admin_sharepoint.py::_extraction_readiness`) — never offer a
  // button whose click can only 409. Missing on an older/degraded cell
  // shape (no such key at all) reads as "not ready" — fail closed, same
  // posture as every other sub-block on this card.
  const extractionReady = inAgnes.extraction_ready === true;
  if (!extractionReady) {
    inAgnesParts.push(
      `<span class="ds-badge badge-unset">${_esc(_extractionUnreadyReasonText(inAgnes.extraction_unready_reason))}</span>`
    );
  }
  inAgnesParts.push(
    inAgnes.last_run_at ? `last run ${_esc(new Date(inAgnes.last_run_at).toLocaleString())}` : "never run"
  );
  if (inAgnes.next_run_at) {
    inAgnesParts.push(`next run ${_esc(new Date(inAgnes.next_run_at).toLocaleString())}`);
  } else if (!inAgnes.schedule) {
    inAgnesParts.push("no schedule configured");
  }

  // The "re-process everything" run option skips BOTH the delta cursor and
  // the per-document cTag check (`connectors.sharepoint.crawler.
  // run_builtin_crawl`'s `force_reprocess`), so it re-downloads, re-converts
  // and — when fact extraction is on — re-runs the LLM pass over the whole
  // corpus, not just what changed. One sentence naming that cost up front,
  // sized with the actual document count when the card already knows it.
  const forceDocs = fs.crawl && typeof fs.crawl.documents === "number" ? fs.crawl.documents : null;
  const forceReprocessHelp = forceDocs
    ? `Re-downloads, re-converts, and (if fact extraction is on) re-runs the LLM pass over all ${forceDocs} ` +
      `document${forceDocs === 1 ? "" : "s"} — not just what changed.`
    : "Re-downloads, re-converts, and (if fact extraction is on) re-runs the LLM pass over every document — not just what changed.";

  // "Extract facts now" — the standalone `sharepoint-facts-extraction`
  // pass: the fact graph over what this connection has ALREADY indexed,
  // no crawl. Its own honest-UI gate (`facts_extraction_ready` /
  // `facts_extraction_unready_switch`, the SAME two switches the job
  // honours, behind the connector switch) — never a button whose click can
  // only 409. Fail closed on an older/degraded cell shape, like the Run
  // button above.
  const factsReady = inAgnes.facts_extraction_ready === true;
  const factsUnreadyText = factsReady ? "" : _factsExtractionUnreadyText(inAgnes.facts_extraction_unready_switch);
  const factsParts = [];
  if (!factsReady) {
    factsParts.push(`<span class="ds-badge badge-unset">${_esc(factsUnreadyText)}</span>`);
  }
  factsParts.push(`<span style="color:var(--ds-text-muted)">Runs over documents indexed so far; safe to repeat.</span>`);

  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Static label — the built-in crawl's live state is the extraction row below.">Schedule</span>
    <span class="ds-src__fact-v">${_esc(fs.schedule.text)}</span>
  </div>
  <div id="ext-block-${row.id}" data-ext-conn="${row.id}" hidden></div>
  <div class="ext-drawer" id="ext-drawer-${row.id}" hidden></div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Document extraction running inside Agnes (TCRD-226) — the built-in crawl→convert→anonymize→ingest lane.">In-Agnes extraction</span>
    <span class="ds-src__fact-v">${inAgnesParts.join(" · ")}</span>
    <span class="ds-src__fact-a"><button type="button" id="ext-inagnes-btn-${row.id}" class="btn btn-secondary" data-extraction-ready="${extractionReady ? "1" : "0"}" ${extractionReady ? "" : "disabled"} onclick="toggleSpRunOptsRow('${row.id}')">Run extraction now</button></span>
  </div>
  <div class="ds-rotate-row" id="ds-sp-runopts-row-${row.id}">
    <label class="field-hint" for="ds-sp-runopts-conc-${row.id}">Files in parallel (1–16, blank = configured)</label>
    <input type="number" id="ds-sp-runopts-conc-${row.id}" min="1" max="16" step="1" placeholder="configured" style="max-width:9rem;">
    <label class="field-hint" for="ds-sp-runopts-timeout-${row.id}">Time limit in seconds (0 = unbounded, blank = configured)</label>
    <input type="number" id="ds-sp-runopts-timeout-${row.id}" min="0" max="86400" step="60" placeholder="configured" style="max-width:9rem;">
    <label class="field-hint" for="ds-sp-runopts-force-${row.id}" style="flex-basis:100%;display:flex;align-items:center;gap:6px;">
      <input type="checkbox" id="ds-sp-runopts-force-${row.id}">
      Re-process everything (ignore the delta cursor)
    </label>
    <span class="field-hint" style="flex-basis:100%;">${_esc(forceReprocessHelp)}</span>
    <label class="field-hint" for="ds-sp-runopts-resync-${row.id}" style="flex-basis:100%;display:flex;align-items:center;gap:6px;">
      <input type="checkbox" id="ds-sp-runopts-resync-${row.id}">
      Re-enumerate from scratch (drop the change cursor, keep already-ingested files)
    </label>
    <span class="field-hint" style="flex-basis:100%;">Use after widening a crawl filter or when the change cursor ran past files it never ingested: every folder is listed again, but a file whose content is unchanged is not re-downloaded.</span>
    <label class="field-hint" for="ds-sp-runopts-retry-${row.id}" style="flex-basis:100%;display:flex;align-items:center;gap:6px;">
      <input type="checkbox" id="ds-sp-runopts-retry-${row.id}">
      Retry failed items
    </label>
    <span class="field-hint" style="flex-basis:100%;">Gives every document already known to have failed (including ones given up on after repeated failures) one more chance — no full re-enumeration, just this connection's own failure queue.</span>
    <button type="button" class="btn btn-primary" onclick="runSpExtraction('${row.id}')">Start run</button>
    <button type="button" class="btn btn-secondary" onclick="toggleSpRunOptsRow('${row.id}')">Cancel</button>
  </div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="The standalone facts pass (sharepoint-facts-extraction): builds the fact graph over documents this connection has already indexed, without a crawl — its own time budget (extraction.facts.run_timeout_s), and it runs alongside a crawl.">Fact graph</span>
    <span class="ds-src__fact-v">${factsParts.join(" · ")}</span>
    <span class="ds-src__fact-a"><button type="button" id="ext-facts-btn-${row.id}" class="btn btn-secondary" data-facts-ready="${factsReady ? "1" : "0"}" ${factsReady ? "" : "disabled"} title="${_esc(factsUnreadyText)}" onclick="runSpFactsExtraction('${row.id}')">Extract facts now</button></span>
  </div>
  ${_extRenderFactsPolicy(row)}
  ${_extRenderCrawlFilter(row)}
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">${isSecretAuth ? "Client secret" : "Certificate"}</span>
    <span class="ds-src__fact-v">${certBadge} <span style="color:var(--ds-text-muted)">${certDetail}</span></span>
    <span class="ds-src__fact-a"><button type="button" class="btn btn-secondary" onclick="toggleSpCertRow('${row.id}')">Update</button></span>
  </div>${certMetaHtml}
  <div class="ds-rotate-row" id="ds-sp-cert-row-${row.id}">
    <textarea id="ds-sp-cert-input-${row.id}" rows="${isSecretAuth ? 2 : 5}" placeholder="${isSecretAuth ? "new Entra client secret" : "-----BEGIN CERTIFICATE-----...-----END PRIVATE KEY-----"}" autocomplete="off" spellcheck="false"></textarea>
    ${isSecretAuth ? "" : `
    <input type="file" id="ds-sp-cert-file-${row.id}" accept=".pem,.crt,.cer,.key,.txt" multiple style="display:none;" onchange="spCertFilePicked(this, 'ds-sp-cert-input-${row.id}', 'ds-sp-cert-file-status-${row.id}')">
    <div class="field-hint" id="ds-sp-cert-file-status-${row.id}" style="display:none;"></div>
    <button type="button" class="btn btn-secondary" onclick="document.getElementById('ds-sp-cert-file-${row.id}').click()">Upload PEM file&hellip;</button>`}
    <button type="button" class="btn btn-primary" onclick="saveSpCertificate('${row.id}')">${isSecretAuth ? "Save secret" : "Save certificate"}</button>
    <button type="button" class="btn btn-secondary" onclick="toggleSpCertRow('${row.id}')">Cancel</button>
  </div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="For a site too large for one connection to crawl in reasonable time: divide its top-level folders across several sibling connections, each with its own crawl, running in parallel.">Split this site</span>
    <span class="ds-src__fact-v">Divide the drive root's folders into several parallel crawl connections.</span>
    <span class="ds-src__fact-a"><button type="button" class="btn btn-secondary" onclick="toggleSpSplitRow('${row.id}')">Split&hellip;</button></span>
  </div>
  <div class="ds-rotate-row" id="ds-sp-split-row-${row.id}">
    <label class="field-hint" for="ds-sp-split-n-${row.id}">Number of parts</label>
    <input type="number" id="ds-sp-split-n-${row.id}" min="1" max="50" step="1" value="4" style="max-width:7rem;">
    <label class="field-hint" for="ds-sp-split-min-modified-${row.id}">Only count/crawl documents modified on/after (optional)</label>
    <input type="date" id="ds-sp-split-min-modified-${row.id}" style="max-width:11rem;">
    <label class="field-hint" for="ds-sp-split-transport-${row.id}">Facts transport for every part (optional)</label>
    <select id="ds-sp-split-transport-${row.id}" style="max-width:9rem;">
      <option value="">unchanged</option>
      <option value="sync">sync</option>
      <option value="batch">batch</option>
    </select>
    <label class="field-hint" for="ds-sp-split-retry-${row.id}">Retry mode for every part (optional)</label>
    <select id="ds-sp-split-retry-${row.id}" style="max-width:9rem;">
      <option value="">unchanged</option>
      <option value="off">off</option>
      <option value="on_gate_fail">on_gate_fail</option>
      <option value="always">always</option>
    </select>
    <button type="button" class="btn btn-primary" onclick="previewSpSplit('${row.id}')">Preview split</button>
    <button type="button" class="btn btn-secondary" onclick="toggleSpSplitRow('${row.id}')">Cancel</button>
    <div class="ds-sp-split-result" id="ds-sp-split-result-${row.id}"></div>
  </div>
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="An ungranted collection is invisible to everyone — fail closed.">Sharing</span>
    <span class="ds-src__fact-v ${identityTone}">${identityText}</span>
  </div>
  ${scopesHtml}
  ${anonRowHtml}
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Last run</span>
    <span class="ds-src__fact-v">${badgesHtml}</span>
  </div>
  <div class="ds-filesource-drawer" id="ds-fs-drawer-${row.id}" hidden></div>`;
}

/* Copies the certificate thumbprint an admin compares against the identity
   provider's app registration. Reads the rendered <code> text directly
   (already `_esc`-escaped on the way in, and a base64url thumbprint has no
   characters that escaping would have changed) rather than threading the
   raw value through an onclick attribute string. */
function _copySharepointThumbprint(btn) {
  const code = btn.previousElementSibling;
  const text = code && code.textContent;
  if (!text || !navigator.clipboard || typeof navigator.clipboard.writeText !== "function") return;
  navigator.clipboard.writeText(text).then(() => {
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1500);
  });
}

/* Live-use feedback (TCRD-240/241 follow-up): a raw row like
   "6a8e0bc93c07c56a — verbatim_gate_failed" "tells nobody anything". Three
   named reasons get a full human explanation; anything else is shown
   VERBATIM (never hidden — an unknown slug is still information) rather
   than collapsed to a generic message. */
const SP_REJECTION_REASON_TEXT = {
  verbatim_gate_failed:
    "Quote not found in the document's text — the claim was rejected, nothing was stored. Usually the producer cited text outside the uploaded content (e.g. a filename).",
  quote_not_meaningful:
    "Quote was too short or not a real word/phrase (e.g. a bare file extension or punctuation) — rejected as evidence, even though it was found in the document.",
  unresolved_doc_id:
    "The cited document isn't registered in any collection — upload it or fix the producer's corpus map.",
  ambiguous_cross_collection_doc_id:
    "The document exists in multiple collections and the batch didn't say which — rejected rather than guessed.",
  not_https:
    "The producer sent a source_url that isn't https — dropped rather than stored, so the claim wrote but has no citation link. Fix the producer to send an https deep link.",
  no_host:
    "The producer sent a source_url with no host — dropped rather than stored, so the claim wrote but has no citation link.",
  too_long:
    "The producer sent a source_url over the length cap — dropped rather than stored, so the claim wrote but has no citation link.",
  unparseable:
    "The producer sent a source_url Agnes couldn't parse — dropped rather than stored, so the claim wrote but has no citation link.",
};
function _spRejectionReasonText(reason) {
  return SP_REJECTION_REASON_TEXT[reason] || reason || "";
}

/* "Run extraction now" honest-UI gate — `extraction_unready_reason` carries
   the SAME slug `POST .../extract`'s 409 body uses
   (`app/api/admin_sharepoint.py::_extraction_readiness`); this maps it to
   the sentence the card shows instead of letting the click 409.

   The enabled sentence names BOTH configuration sources, because the gate
   honours a deploy-time env override ahead of instance.yaml
   (AGNES_SHAREPOINT_ENABLED). An admin on an env-configured instance told
   to edit instance.yaml would edit a file that is not deciding this — the
   API's own 409 message says both, and this copy is the same sentence one
   layer up. */
const EXTRACTION_UNREADY_REASON_TEXT = {
  extraction_disabled:
    "The SharePoint connector is disabled on this instance — set sharepoint.enabled: true in instance.yaml (or AGNES_SHAREPOINT_ENABLED) first.",
  extraction_dependencies_missing:
    "The extraction dependencies are not installed on this instance — install the extra (pip install 'agnes[extraction]') and restart.",
};
function _extractionUnreadyReasonText(reason) {
  return EXTRACTION_UNREADY_REASON_TEXT[reason] || "Extraction is not ready on this instance.";
}

/* "Extract facts now" honest-UI gate — `facts_extraction_unready_switch`
   names the first config key the standalone facts pass needs and does not
   have (`app/web/router.py`'s pipeline cell, reading the SAME
   `_facts_extraction_readiness` verdict the trigger's own 409 carries as
   `switch`). Rendered as the button's disabled reason, so a click can never
   409 on a switch the page already knew about. */
const FACTS_EXTRACTION_UNREADY_TEXT = {
  "sharepoint.enabled":
    "The SharePoint connector is disabled on this instance — set sharepoint.enabled: true in instance.yaml (or AGNES_SHAREPOINT_ENABLED) first.",
  "extraction.facts.enabled":
    "Fact extraction is off — turn on extraction.facts.enabled in /admin/server-config (the cost switch: this pass spends model tokens per document).",
  "facts.enabled":
    "The fact graph is off — turn on facts.enabled in /admin/server-config first.",
};
function _factsExtractionUnreadyText(switchKey) {
  return FACTS_EXTRACTION_UNREADY_TEXT[switchKey] || "Fact extraction is not ready on this instance.";
}

/* Duplicate (doc_id, reason) pairs collapse into one row with a "2×" count
   badge — a document that fails the same gate ten times in a row is one
   fact ("this document"), not ten identical lines. Rows with different
   reasons for the SAME doc_id stay separate, so a subline is never a blend
   of two different explanations. */
function _spGroupRejectionRows(rows) {
  const groups = [];
  const indexByKey = {};
  rows.forEach((r) => {
    const key = `${r.doc_id || ""}\u0000${r.reason || ""}`;
    if (indexByKey[key] === undefined) {
      indexByKey[key] = groups.length;
      groups.push(Object.assign({}, r, { count: 1 }));
    } else {
      groups[indexByKey[key]].count += 1;
    }
  });
  return groups;
}

/* The row's PRIMARY line: the resolved file name + its collection when the
   server could resolve `doc_id` (`_sharepoint_pipeline_cell`'s `doc` key);
   otherwise the raw sha16 stays visible (never hidden) with an honest
   "not in any collection" fallback. The sha16 is always available as a
   tooltip, resolved or not, for anyone who wants to go find it. */
function _spRejectionRowHtml(r) {
  const docId = r.doc_id || "";
  const doc = r.doc;
  const primary = doc && doc.name
    ? `<span title="${_esc(docId)}">${_esc(doc.name)} · ${_esc(doc.collection || "not in any collection")}</span>`
    : `<code title="Crawler citation key (sha256, first 16 hex chars)">${_esc(docId)}</code> — not in any collection`;
  const countBadge = r.count > 1 ? ` <span class="ds-badge badge-unset">${r.count}×</span>` : "";
  const reasonText = _spRejectionReasonText(r.reason);
  return `<li>${primary}${countBadge}${reasonText ? `<div class="ds-filesource-drawer__reason">${_esc(reasonText)}</div>` : ""}</li>`;
}

/* Segmented switch: click a badge, see that category's itemized rows below
   it; click the same one again to close. One category open at a time. */
function toggleFileSourceDrawer(connId, category) {
  const el = document.getElementById(`ds-fs-drawer-${connId}`);
  if (!el) return;
  const fs = (SOURCE_PIPELINES[connId] || {}).file_source;
  const lastRun = fs && fs.last_run;
  if (!lastRun) { el.hidden = true; return; }
  if (el.dataset.category === category && !el.hidden) {
    el.hidden = true;
    delete el.dataset.category;
    return;
  }
  const rows = lastRun[category] || [];
  el.dataset.category = category;
  el.hidden = false;
  el.innerHTML = rows.length
    ? `<ul class="ds-filesource-drawer__list">${_spGroupRejectionRows(rows).map(_spRejectionRowHtml).join("")}</ul>`
    : `<div class="ds-empty">Nothing in this category in the last run.</div>`;
}

/* One scope row, in the SAME shape the wizard's step-3 "Share" preview
   renders (server shape: `admin_sharepoint._scope_out`) — clicking it opens
   that wizard bound to this connection with the row highlighted. */
function _spScopeRowHtml(connId, s) {
  return `<li>
    <button type="button" class="ds-sp-scope-row" onclick="openSpWizardForConnection('${connId}', { highlightScopeId: '${_esc(s.source_scope_id)}' })">
      <span class="ds-sp-scope-row__path">${_esc(s.display_path || s.source_scope_id || "")}</span>
      ${s.collection ? `<span class="ds-badge badge-env">${_esc(s.collection.name)}</span>` : `<span class="ds-badge badge-unset">no collection yet</span>`}
      ${s.no_group_warning ? `<span class="ds-badge badge-warn">no group</span>` : ""}
    </button>
  </li>`;
}

/* Fetch-on-expand for the "Scope collections" row (perf follow-up,
   2026-09-03) — same shape as `toggleFileSourceDrawer` above and the
   extraction card's `toggleExtractionDrawer`: nothing server-rendered,
   the full unbounded list fetched from `GET .../scopes` only once the
   admin actually asks for it, and a second click closes it again without
   re-fetching. */
async function toggleSpScopesDrawer(connId) {
  const el = document.getElementById(`ds-sp-scopes-drawer-${connId}`);
  if (!el) return;
  if (!el.hidden) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.innerHTML = `<div class="ds-empty">Loading…</div>`;
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/scopes`, {
      credentials: "include",
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    const scopes = body.items || [];
    el.innerHTML = scopes.length
      ? `<ul class="ds-sp-scopes">${scopes.map((s) => _spScopeRowHtml(connId, s)).join("")}</ul>`
      : `<div class="ds-empty">No confirmed scopes yet.</div>`;
  } catch (e) {
    el.innerHTML = `<div class="ds-empty">Couldn't load scopes — ${_esc(e && e.message)}.</div>`;
  }
}

function _connectionCardHtml(row) {
  const id = row.id;
  const name = _esc(row.name || "");
  const config = row.config || {};
  const isDefault = row.is_default === true || row.is_default === 1;
  // Which Keboola project this connection actually opens, recorded from the
  // token's own owner block. Several projects on one stack render as the same
  // host, so the URL alone never told two connections apart — and the "master
  // token: SET" badge said nothing about WHICH project that token unlocks.
  // …and an explicit way OUT of that binding. The connection is locked to the
  // first project it verified against, so a token for a different project on
  // the same stack is refused — correct, and the whole point — but re-pointing
  // an existing connection to another project then had no route at all from
  // this page: an edit no longer clears the binding (it used to, silently, on
  // every save, which is the bug that made the lock necessary). "Unbind" is
  // that route. (Devin Review.)
  const c = _connector(row.source_type);
  const health = _sourceHealth(row);

  // The body: settings, then the inline tasks the Actions menu opens into it.
  // Everything here was previously a permanently-visible band on the card.
  const facts = row.derived
    ? `
    <div class="ds-src__fact">
      <span class="ds-src__fact-k">Managed in</span>
      <span class="ds-src__fact-v">${_esc(row.settings_label || "Instance settings")}</span>
      <span class="ds-src__fact-a">
        ${row.source_type === "keboola" && row.stack_url && row.credentialed && row.token_env_allowlisted ? `<button type="button" class="btn btn-primary" onclick="importKeboolaConnection('${id}')">Import as managed connection</button>` : ""}
        <a class="btn btn-secondary" href="${_esc(row.settings_href || "/admin")}">Open</a>
      </span>
    </div>
    <div class="ds-src__fact">
      <span class="ds-src__fact-k">Tables</span>
      <span class="ds-src__fact-v">Registered against this connector, not against a stored connection.</span>
      <span class="ds-src__fact-a"><a class="btn btn-secondary" href="/admin/tables">View tables</a></span>
    </div>`
    : row.source_type === "sharepoint"
    ? _sharepointFactsHtml(row)
    : `
    <div class="ds-src__fact">
      <span class="ds-src__fact-k" title="Where Agnes reads this project's storage token from.">Storage token</span>
      <span class="ds-src__fact-v">${_secretBadgeHtml(row)}</span>
      <span class="ds-src__fact-a"><button type="button" class="btn btn-secondary" onclick="toggleRotate('${id}')">Rotate</button></span>
    </div>
    <div class="ds-rotate-row" id="ds-rotate-${id}">
      <input type="password" id="ds-rotate-input-${id}" placeholder="paste new token" autocomplete="off">
      <button type="button" class="btn btn-primary" onclick="saveRotatedToken('${id}')">Save token</button>
      <button type="button" class="btn btn-secondary" onclick="toggleRotate('${id}')">Cancel</button>
    </div>
    ${_masterTokenFactHtml(row)}
    ${_chatToolsFactHtml(row)}
    ${config.project_id != null ? `
    <div class="ds-src__fact">
      <span class="ds-src__fact-k" title="The connection is locked to the first project it verified against, so a token for a different project on the same stack is refused.">Bound to project</span>
      <span class="ds-src__fact-v">${_esc(config.project_name || "unnamed")} · ${_esc(String(config.project_id))}</span>
      <span class="ds-src__fact-a"><button type="button" class="btn btn-secondary" onclick="unbindProject('${id}')"
        title="Forget which project this connection is locked to, so a token for another project on the same stack can be stored">Unbind</button></span>
    </div>` : ""}`;

  return `
<article class="ds-src" id="ds-conn-${id}" data-id="${id}">
  <div class="ds-src__head" data-src-head="${id}">
    <span class="ds-src__logo ds-src__logo--${c.cls}" aria-hidden="true">${_connectorLogo(row.source_type) || _esc(c.abbr)}</span>
    <div class="ds-src__id">
      <div class="ds-src__name">
        <span>${name}</span>
        ${isDefault ? `<span class="ds-src__tag" title="The project the CLI and the sync use when nobody names one">default</span>` : ""}
      </div>
      <div class="ds-src__sub">${_sourceSubtitle(row)}</div>
    </div>
    ${health ? `<span class="ds-src__health ${health.tone}">${health.label}</span>` : ""}
    <div class="ds-src__acts">
      ${row.source_type === "sharepoint" ? `
      <button type="button" class="btn btn-primary" onclick="openSpWizardForConnection('${id}')"
              title="Open the connect wizard, bound to this connection, on its scope step">Manage scopes</button>` : ""}
      <button type="button" class="apg-menu__btn" data-srcmenu="${id}"
              aria-haspopup="true" aria-expanded="false">Actions ${ICO_CHEVRON}</button>
      <button type="button" class="ds-src__caret" data-disclose="${id}"
              aria-expanded="false" aria-controls="ds-body-${id}"
              aria-label="Settings for ${name}">${ICO_CARET}</button>
    </div>
  </div>
  ${_pipelineStripHtml(row)}
  ${_nextStepHtml(row)}
  <div class="ds-src__body" id="ds-body-${id}" hidden>
    <span class="ds-conn-test-result" id="ds-test-${id}"></span>
    <div class="ds-src__facts">${facts}</div>
    <div class="ds-browse-panel" id="ds-browse-${id}"></div>
  </div>
</article>`;
}

async function loadConnections() {
  const list = document.getElementById("ds-conn-list");
  try {
    // No `?source_type=` filter. This list is EVERY source — the page used to
    // ask for Keboola alone while its own Add drawer registered BigQuery
    // tables that then appeared nowhere on it.
    const r = await fetch(API_CONNECTIONS, { credentials: "include" });
    if (!r.ok) {
      list.innerHTML = `<div class="ds-empty">Failed to load data sources (HTTP ${r.status}).</div>`;
      return;
    }
    // Stored connections first, then the connectors that keep no row of their
    // own — they are the tail because they are the ones you manage elsewhere.
    _connections = [...(await r.json()), ...DERIVED_SOURCES];
    renderConnList();
  } catch (e) {
    list.innerHTML = `<div class="ds-empty">Failed to load data sources.</div>`;
  }
}

// The derived Keboola card's one-click fix for the old two-hop detour: card
// -> "Open" -> server-config -> back to "+ Add source" before an admin could
// actually browse and register tables. This imports the SAME instance-level
// credential (stack_url + token_env, read server-side in
// `_keboola_instance_config()`) into a real `source_connections` row, so the
// card immediately gets the full inline browse+register experience instead
// of pointing away from it. Additive only — `data_source.keboola` in
// server-config is left untouched, so tables already registered against the
// legacy instance-level connection keep resolving exactly as before.
//
// `seed_from_instance_credentials: true` tells `create_connection` to copy
// the instance-level token into the NEW connection's own vault slot unless
// that token resolves under the EXACT `token_env` name this new row
// inherits (see `_seed_keboola_instance_credential`) — only then does the
// new row's own token resolver already find it unaided. A generic
// `KEBOOLA_STORAGE_TOKEN` fallback behind a DIFFERENT configured
// `token_env`, or a vault-only credential, both still need the copy. The
// button itself only renders when `row.credentialed` AND
// `row.token_env_allowlisted` are both true (server-computed — see
// `_source_inventory()`) — a credentialed-via-generic-env-or-vault instance
// whose configured `token_env` isn't on the remote-attach allowlist would
// otherwise 400 at `create_connection`'s own `_reject_disallowed_token_env`
// check, dead-ending the very button that promises a one-click path. The
// guard below stays as a second line of defense against a stale render.
async function importKeboolaConnection(id) {
  const row = _connections.find((c) => c.id === id);
  if (!row || !row.stack_url || !row.credentialed) {
    showToast("No working Keboola credential yet — set one in Server config first.", false);
    return;
  }
  if (!row.token_env_allowlisted) {
    showToast(
      "This instance's configured token_env isn't on the credential allowlist — use Server config to add it via AGNES_CONFIG_SECRET_ENVS, or connect a new project from “+ Add data source” instead.",
      false,
    );
    return;
  }
  try {
    const r = await fetch(API_CONNECTIONS, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "Keboola",
        source_type: "keboola",
        config: { stack_url: row.stack_url },
        token_env: row.token_env || undefined,
        seed_from_instance_credentials: true,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      if (data.token_seeded === false) {
        // The instance-vault token existed but the server's own preflight
        // rejected it (stale/expired) — the connection was still created,
        // but with no working credential of its own. A plain success toast
        // here would be a lie the admin only discovers on the next sync
        // failure.
        showToast(
          "Connection created, but the stored credential couldn't be verified — use Rotate to add a working token.",
          false,
        );
      } else {
        showToast("Keboola imported as a managed connection.", true);
      }
      // `DERIVED_SOURCES` is the page-load constant `loadConnections()`
      // re-spreads on every refresh; drop the now-superseded entry from it
      // so the reload below doesn't render the derived card ALONGSIDE the
      // real one it just replaced.
      const idx = DERIVED_SOURCES.findIndex((d) => d.source_type === "keboola");
      if (idx !== -1) DERIVED_SOURCES.splice(idx, 1);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      showToast("Import failed: " + (data.detail || `HTTP ${r.status}`), false);
    }
  } catch (e) {
    showToast("Request failed.", false);
  }
}

// The section head's count — the same quiet read-out People puts beside its
// heading and Access beside its group list. Empty (not "0 projects") while the
// list is still loading: a zero that turns into three is worse than a blank.
function setConnCount(n) {
  const c = document.getElementById("ds-conn-count");
  if (c) c.textContent = n === null ? "" : `${n} source${n === 1 ? "" : "s"}`;
}

// The "Facts → graph" strip cell's data, per SharePoint connection, fetched
// AFTER the cards are on screen — never blocking `loadConnections()` itself
// (perf follow-up, 2026-09-03: computing this for every SharePoint
// connection during the page's own server-side render is what dominated a
// live instance's load time — see `app.web.router._sharepoint_pipeline_
// cell`'s docstring). Each connection's request is independent and can
// fail on its own without affecting the others or the rest of the card.
function _fetchSharepointGraphCounts() {
  for (const row of _connections) {
    if (row.source_type !== "sharepoint") continue;
    const cell = document.getElementById(`ds-sp-graph-${row.id}`);
    if (!cell) continue;
    fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(row.id)}/facts-graph-counts`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((counts) => {
        const v = cell.querySelector(".ds-pipe__v");
        if (!v) return;
        const facts = counts.facts || 0;
        const edges = counts.edges || 0;
        v.style.color = "";
        v.textContent = `${facts} fact${facts === 1 ? "" : "s"} · ${edges} edge${edges === 1 ? "" : "s"}`;
      })
      .catch(() => {
        const v = cell.querySelector(".ds-pipe__v");
        if (v) v.textContent = "unavailable";
      });
  }
}

function renderConnList() {
  const list = document.getElementById("ds-conn-list");
  setConnCount(_connections.length);
  if (!_connections.length) {
    list.innerHTML = `<div class="ds-empty">No sources connected yet — <b>+ Add source</b> opens the connector picker.</div>`;
    return;
  }
  list.innerHTML = _connections.map(_connectionCardHtml).join("");
  _fillGrantPickers();
  loadTokenMismatchWarnings();
  // The cards are painted by fetch, so the extraction poll's self-start ran
  // against a page with no cards on it and armed nothing. `typeof` because
  // this function belongs to a LATER script block, and the parser is free to
  // run this fetch's continuation between the two.
  if (typeof _extAfterCardsPainted === "function") _extAfterCardsPainted();
  // Same defensive `typeof` guard, for the same reason it protects tests
  // that extract a curated function subset (e.g. `TestRefreshSourcePipelines
  // Behavior`) rather than the whole script.
  if (typeof _fetchSharepointGraphCounts === "function") _fetchSharepointGraphCounts();
}

/* ── Keeping the strip true after a mutation ───────────────────────────────
   `SOURCE_PIPELINES` is a snapshot of a server-side fold, taken when this
   HTML was built — but everything on this page happens over fetch. The
   wizard registers tables, creates packages and writes grants; the cards
   save tokens and toggle chat tools; none of it touched that snapshot, so a
   card went on saying "Add the first tables → / Never synced / 0 packages"
   after two dozen tables had landed, until somebody hard-reloaded.

   `GET /api/admin/source-pipelines` re-reads the same fold. Every handler
   that mutates anything calls THIS function afterwards — the rule is
   deliberately blunt ("a mutation re-reads the strip") rather than a
   per-handler judgment about which cells that particular write can move,
   because the per-handler judgment is what silently rots.

   Repainted in place, never reloaded: an admin with a card expanded or a
   browse panel open is mid-task, and a reload throws that away along with
   their scroll position. A failed refresh keeps the stale strip and says
   nothing — a card that stops updating is recoverable, one that empties
   itself is not.

   This carries the STRIP, not the connection rows. A handler that also
   changed what the row itself says — a stored secret, a cleared project
   binding, a queued extraction's timestamp, a connection that now exists at
   all — pairs this with `loadConnections()`, in that order, so the redraw
   below paints from the fresh dict. */
let _pipelineRefreshInFlight = null;

async function refreshSourcePipelines() {
  // One read at a time. A wizard exit calls this from several places within a
  // tick, and two overlapping reads would resolve last-response-wins — which,
  // with the older response landing second, is the stale strip this whole
  // function exists to remove. Everyone waiting gets the same promise.
  if (_pipelineRefreshInFlight) return _pipelineRefreshInFlight;
  _pipelineRefreshInFlight = (async () => {
    try {
      const r = await fetch("/api/admin/source-pipelines", { credentials: "include" });
      if (!r.ok) return false;
      SOURCE_PIPELINES = await r.json();
      // Inside the try: a repaint that throws must report itself as a failed
      // refresh, not escape into a caller's `finally` and replace whatever
      // error that block was already unwinding.
      _repaintSourceCards();
      return true;
    } catch (_) {
      return false;
    }
  })().finally(() => { _pipelineRefreshInFlight = null; });
  return _pipelineRefreshInFlight;
}

/* Swap each drawn card's strip and status word for what the fresh dict says,
   and leave the rest of the card — including whether it is open — exactly as
   the admin left it. A source the list has not drawn yet (a connection the
   wizard just created) is skipped here; `loadConnections()` draws it from the
   same fresh dict. */
function _repaintSourceCards() {
  for (const row of _connections) {
    const card = document.getElementById(`ds-conn-${row.id}`);
    if (!card) continue;
    const head = card.querySelector(".ds-src__head");

    const stripHtml = _pipelineStripHtml(row);
    const strip = card.querySelector(":scope > .ds-pipe");
    if (strip && stripHtml) strip.outerHTML = stripHtml;
    else if (strip) strip.remove();
    else if (stripHtml && head) head.insertAdjacentHTML("afterend", stripHtml);

    // The next-step row, same three cases — and the REMOVE case is the one
    // that matters: the mutation that triggered this refresh is usually the
    // very act that finished the chain, so a row still saying "put them in a
    // package" after the admin just did is worse than no row at all.
    const nextHtml = _nextStepHtml(row);
    const next = card.querySelector(":scope > .ds-next");
    if (next && nextHtml) next.outerHTML = nextHtml;
    else if (next) next.remove();
    else if (nextHtml) {
      const anchor = card.querySelector(":scope > .ds-pipe") || head;
      if (anchor) anchor.insertAdjacentHTML("afterend", nextHtml);
    }

    if (!head) continue;
    const health = _sourceHealth(row);
    const chip = head.querySelector(".ds-src__health");
    if (chip && health) {
      chip.className = `ds-src__health ${health.tone}`;
      chip.textContent = health.label;
    } else if (chip) {
      chip.remove();
    } else if (health) {
      const acts = head.querySelector(".ds-src__acts");
      const chipHtml = `<span class="ds-src__health ${health.tone}">${health.label}</span>`;
      if (acts) acts.insertAdjacentHTML("beforebegin", chipHtml);
      else head.insertAdjacentHTML("beforeend", chipHtml);
    }
  }
  // The strip we just replaced carried the live-crawl cell. Repaint it from
  // what the poll already knows, or a mutation blanks a running crawl until
  // the next tick.
  if (typeof _extAfterCardsPainted === "function") _extAfterCardsPainted();
  // Same reason: `_pipelineStripHtml` rebuilt the graph cell back to its
  // "loading…" placeholder (the server-side fold never carries the real
  // numbers — see `_fetchSharepointGraphCounts`), so a mutation-triggered
  // repaint needs to re-fetch it too, or the strip is stuck loading forever.
  if (typeof _fetchSharepointGraphCounts === "function") _fetchSharepointGraphCounts();
}

// A connection whose storage token and master token resolve to DIFFERENT
// projects syncs tables from one project while reading the semantic layer of
// another — so no imported metric can ever bind to a table. It looks like a
// working connection from every other angle, which is exactly why it earns a
// warning here, next to the two tokens that disagree.
//
// Coverage is computed server-side on demand; this runs after the cards paint
// so a slow upstream never delays the list itself.
async function loadTokenMismatchWarnings() {
  let sources;
  try {
    // `warnings_only`: this strip needs the token-identity checks, not every
    // project's semantic model — the full report enumerates each Metastore,
    // which is a heavy upstream sweep to draw one line of text.
    // (Devin Review on this PR.)
    const r = await fetch("/api/admin/semantic-layer/coverage?warnings_only=true", { credentials: "include" });
    if (!r.ok) return;
    sources = (await r.json()).sources || [];
  } catch (e) {
    return; // Additive only — the page is fully usable without it.
  }
  for (const source of sources) {
    const box = document.getElementById(`ds-mismatch-${source.connection_id}`);
    if (!box) continue;
    // BOTH mismatch codes. Matching only the storage-vs-master one filtered
    // out the more serious warning — a master token that contradicts the
    // project the connection is locked to, which stops the sync outright —
    // on the very page its message tells the admin to visit. (Devin Review.)
    const warning = (source.warnings || []).find(
      (w) => w.code === "token_project_mismatch" || w.code === "master_token_project_mismatch",
    );
    box.hidden = !warning;
    box.textContent = "";
    if (!warning) continue;
    // Upstream project names/ids — untrusted, so textContent, never innerHTML.
    const iconTpl = document.getElementById("ds-warn-icon");
    if (iconTpl) box.appendChild(iconTpl.content.cloneNode(true));
    const text = document.createElement("span");
    text.textContent = warning.message || "";
    box.appendChild(text);
  }
}

async function testConn(id) {
  const resultEl = document.getElementById(`ds-test-${id}`);
  // Test lives in the Actions menu now, so there is no button left on the
  // card to grey out — and the verdict lands in the body, which may be shut.
  // Open it and say "testing…" instead: the feedback that a press registered
  // has to exist somewhere, and unexplained silence was the failure the old
  // `querySelector("button")` bug produced. (Devin Review.)
  setSourceOpen(id, true);
  resultEl.className = "ds-conn-test-result show";
  resultEl.textContent = "Testing…";
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/test`, { method: "POST", credentials: "include" });
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.ok) {
      resultEl.className = "ds-conn-test-result show ok";
      resultEl.textContent = "✓ " + (data.project_name ? `Connected — ${data.project_name}` : "Connected");
    } else {
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = "✗ " + (data.error || data.detail || "connection failed");
    }
  } catch (e) {
    resultEl.className = "ds-conn-test-result show fail";
    resultEl.textContent = "✗ Request failed";
  }
}

/* SharePoint's own cheap connectivity check. `POST .../source-connections/
   {id}/test` (the generic `testConn` above) calls Keboola's own
   `/v2/storage/tokens/verify` — meaningless, and 422s, for a SharePoint
   connection (no `stack_url`). No new endpoint: this reuses the two GETs
   the connect wizard/card already have — certificate (proves a usable PEM
   is stored) and an unscoped tree call (proves the Graph token + cert
   actually authenticate, since listing sites is a live upstream call). */
async function testSpConn(id) {
  const resultEl = document.getElementById(`ds-test-${id}`);
  setSourceOpen(id, true);
  resultEl.className = "ds-conn-test-result show";
  resultEl.textContent = "Testing…";
  const base = `/api/admin/sharepoint/connections/${encodeURIComponent(id)}`;
  try {
    const certResp = await fetch(`${base}/certificate`, { credentials: "include" });
    const cert = await certResp.json().catch(() => ({}));
    if (!certResp.ok || !cert.certificate) {
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = "✗ " + (cert.reason || "certificate not configured");
      return;
    }
    const treeResp = await fetch(`${base}/tree`, { credentials: "include" });
    const tree = await treeResp.json().catch(() => ({}));
    if (!treeResp.ok) {
      const detail = tree.detail;
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = "✗ " + ((detail && (detail.message || detail.error)) || "connection failed");
      return;
    }
    const n = (tree.items || []).length;
    resultEl.className = "ds-conn-test-result show ok";
    resultEl.textContent = `✓ Connected — ${n} site${n === 1 ? "" : "s"} reachable`;
  } catch (e) {
    resultEl.className = "ds-conn-test-result show fail";
    resultEl.textContent = "✗ Request failed";
  }
}

/* One-off admin trigger for the in-Agnes extraction job (TCRD-226):
   `POST .../connections/{id}/extract` enqueues the existing
   `corpus-extraction` job kind — see app/api/admin_sharepoint.py. Fire-and-
   forget from the card's point of view (the job runs out-of-band in a
   worker); the visible result here is "queued" or a typed refusal
   (extraction disabled, no producer configured, or already running), never
   the extraction's own outcome. */
function toggleSpRunOptsRow(id) {
  // The "Run extraction now" options row: per-run concurrency and time
  // limit (both optional — blank keeps the configured value). The card's
  // overflow menu still runs immediately with defaults; this row is where
  // an admin steers ONE run without touching /admin/server-config.
  const row = document.getElementById(`ds-sp-runopts-row-${id}`);
  if (!row) { runSpExtraction(id); return; }
  setSourceOpen(id, true);
  row.classList.toggle("show");
}

function _spRunOption(id, kind, min, max) {
  const el = document.getElementById(`ds-sp-runopts-${kind}-${id}`);
  if (!el || el.value.trim() === "") return null;
  const n = Number(el.value);
  if (!Number.isInteger(n) || n < min || n > max) return { invalid: true };
  return n;
}

async function runSpExtraction(id) {
  const resultEl = document.getElementById(`ds-test-${id}`);
  setSourceOpen(id, true);
  const conc = _spRunOption(id, "conc", 1, 16);
  const timeout = _spRunOption(id, "timeout", 0, 86400);
  if ((conc && conc.invalid) || (timeout && timeout.invalid)) {
    showToast("Run options out of range — concurrency 1–16, time limit 0–86400 s.", false);
    return;
  }
  const forceEl = document.getElementById(`ds-sp-runopts-force-${id}`);
  const forceReprocess = !!(forceEl && forceEl.checked);
  const resyncEl = document.getElementById(`ds-sp-runopts-resync-${id}`);
  const resync = !!(resyncEl && resyncEl.checked);
  const retryEl = document.getElementById(`ds-sp-runopts-retry-${id}`);
  const retryFailed = !!(retryEl && retryEl.checked);
  const overrides = {};
  if (conc !== null) overrides.concurrency = conc;
  if (timeout !== null) overrides.timeout_s = timeout;
  if (forceReprocess) overrides.force_reprocess = true;
  if (resync) overrides.resync = true;
  if (retryFailed) overrides.retry_failed = true;
  resultEl.className = "ds-conn-test-result show";
  resultEl.textContent = "Starting extraction…";
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/extract`, {
      method: "POST",
      credentials: "include",
      ...(Object.keys(overrides).length
        ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(overrides) }
        : {}),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      resultEl.className = "ds-conn-test-result show ok";
      resultEl.textContent = `✓ Extraction queued (job ${body.job_id})`;
      showToast("Extraction queued.", true);
      const optsRow = document.getElementById(`ds-sp-runopts-row-${id}`);
      if (optsRow) optsRow.classList.remove("show");
      // A run option, not a saved setting — uncheck it so reopening this
      // row and clicking "Start run" again does not silently re-process
      // everything a second time.
      if (forceEl) forceEl.checked = false;
      if (retryEl) retryEl.checked = false;
      // The dispatch stamps `config.extraction.last_run_at` on the connection
      // row, and the body's "In-Agnes extraction" line reads it — so this
      // needs the rows re-read too, not just the strip.
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const msg = detailMessage(body, "failed to start extraction");
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = "✗ " + msg;
      showToast(msg, false);
    }
  } catch (e) {
    resultEl.className = "ds-conn-test-result show fail";
    resultEl.textContent = "✗ Request failed";
  }
}

/* "Extract facts now" — `POST .../connections/{id}/facts-extract` enqueues
   the `sharepoint-facts-extraction` job (app/api/admin_sharepoint.py): the
   fact graph over whatever this connection has ALREADY indexed, no crawl.
   Its plan is incremental (indexed documents without up-to-date facts), so
   repeating it is safe, and it never dedups against a crawl — the two run
   side by side. Fire-and-forget from the card's point of view, same as
   runSpExtraction: the visible result here is "queued" or a typed refusal
   (a switch off, a pass already running), never the pass's own outcome —
   that arrives through the status poll's `facts_job` line. */
async function runSpFactsExtraction(id) {
  const resultEl = document.getElementById(`ds-test-${id}`);
  setSourceOpen(id, true);
  resultEl.className = "ds-conn-test-result show";
  resultEl.textContent = "Queuing facts pass…";
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/facts-extract`, {
      method: "POST",
      credentials: "include",
    });
    const body = await r.json().catch(() => ({}));
    const detail = body && typeof body.detail === "object" ? body.detail : null;
    if (r.ok) {
      resultEl.className = "ds-conn-test-result show ok";
      resultEl.textContent = `✓ Facts pass queued (job ${body.job_id}) — runs over documents indexed so far.`;
      showToast("Facts pass queued.", true);
    } else if (r.status === 409 && detail && detail.error === "facts_extraction_already_running") {
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = `✗ A facts pass is already running for this connection (job ${detail.job_id}).`;
      showToast("A facts pass is already running.", false);
    } else {
      const msg = detailMessage(body, "failed to queue the facts pass");
      resultEl.className = "ds-conn-test-result show fail";
      resultEl.textContent = "✗ " + msg;
      showToast(msg, false);
    }
    // The status poll owns the live "facts pass queued/running" line and
    // the button's in-flight lock — ask it now rather than waiting for its
    // next tick.
    if (typeof _extTick === "function") _extTick();
  } catch (e) {
    resultEl.className = "ds-conn-test-result show fail";
    resultEl.textContent = "✗ Request failed";
  }
}

/* "Consolidate collections…" — folds this connection's per-scope
   collections (one per bulk-added scope; the common outcome of splitting a
   large site across many scopes before bulk-add grew its own
   shared-collection option) into ONE target. `POST .../collections/
   consolidate` — see app/api/admin_sharepoint.py::consolidate_collections.

   Dry-run-first, always: the FIRST call is always `dry_run: true` (a pure
   preview — the server never mints anything on that call, see the route's
   own docstring), rendering "N collection(s), M file(s) → target" for the
   admin to read BEFORE anything is touched; only an explicit confirm sends
   the second, real (`dry_run: false`) call. A blocked source (still shared
   with another connection) is surfaced and the flow stops — it would only
   be refused with a 409 anyway. */
async function consolidateSpCollections(id) {
  const targetName = window.prompt(
    "Fold this connection's per-scope collections into ONE collection.\n\n" +
      "Name the target collection (a new one is created):",
  );
  if (!targetName || !targetName.trim()) return;
  const name = targetName.trim();
  setSourceOpen(id, true);
  const resultEl = document.getElementById(`ds-test-${id}`);
  if (resultEl) {
    resultEl.className = "ds-conn-test-result show";
    resultEl.textContent = "Checking what would be consolidated…";
  }
  const url = `/api/admin/sharepoint/connections/${encodeURIComponent(id)}/collections/consolidate`;
  try {
    const previewResp = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: { name }, dry_run: true }),
    });
    const preview = await previewResp.json().catch(() => ({}));
    if (!previewResp.ok) {
      const msg = detailMessage(preview, "failed to preview consolidation");
      if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ " + msg; }
      showToast(msg, false);
      return;
    }
    const sources = preview.sources || [];
    const totalFiles = sources.reduce((sum, s) => sum + (s.file_count || 0), 0);
    const blocking = preview.blocking || [];
    if (blocking.length) {
      const msg = `${blocking.length} collection(s) are still shared with another connection — consolidating would be refused.`;
      if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ " + msg; }
      showToast(msg, false);
      return;
    }
    const proceed = window.confirm(
      `This will fold ${sources.length} collection(s) (${totalFiles} file(s) total) into ` +
        `"${name}". This cannot be undone. Continue?`,
    );
    if (!proceed) {
      if (resultEl) { resultEl.className = "ds-conn-test-result show"; resultEl.textContent = ""; }
      return;
    }
    if (resultEl) resultEl.textContent = "Consolidating…";
    const execResp = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: { name }, dry_run: false }),
    });
    const execBody = await execResp.json().catch(() => ({}));
    if (execResp.ok) {
      if (resultEl) {
        resultEl.className = "ds-conn-test-result show ok";
        resultEl.textContent =
          `✓ Folded ${(execBody.sources || []).length} collection(s) into "${execBody.target.name}" ` +
          `(${execBody.files_moved} file(s)).`;
      }
      showToast("Collections consolidated.", true);
      await loadConnections();
    } else {
      const msg = detailMessage(execBody, "failed to consolidate collections");
      if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ " + msg; }
      showToast(msg, false);
    }
  } catch (e) {
    if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ Request failed"; }
    showToast("Request failed.", false);
  }
}

/* "Map site group (ACL)…" — maps ONE SharePoint site group (Owners/
   Members/Visitors, or a custom one) to one or more Agnes user_groups ids
   in this connection's `config.acl_site_group_map`. SharePoint site groups
   are not enumerable through the app-only Graph surface this connector
   uses (`connectors/sharepoint/acl_sync.py::classify_permissions`), so a
   scope granting one classifies `unhonored: site_group` and grants nobody
   until it is mapped here — an admin picks the site group's exact
   displayName and one or more existing Agnes group ids; that group's own
   members are then granted directly, same as any other ordinary grant.

   `PATCH .../acl-site-group-map` replaces the WHOLE map in one call, so
   this reads the connection's CURRENT map first (`GET .../connections`
   already loaded via `loadConnections()` — `row.config` carries it) and
   only changes the one entry being edited, mirroring the CLI's own
   read-modify-write (`agnes admin sharepoint acl map-site-group`). Leaving
   the group-ids prompt blank unmaps that site group entirely. */
async function mapSpSiteGroup(id) {
  const siteGroup = window.prompt(
    "Map a SharePoint site group to Agnes group(s) for ACL mirroring.\n\n" +
      'Site group name (exact, e.g. "Members", "Owners"):',
  );
  if (!siteGroup || !siteGroup.trim()) return;
  const name = siteGroup.trim();

  const row = (_connections || []).find((r) => r.id === id) || {};
  const currentMap = (row.config && row.config.acl_site_group_map) || {};
  const currentIds = (currentMap[name] || []).join(", ");
  const groupsRaw = window.prompt(
    `Agnes group id(s) for site group "${name}" (comma-separated).\n` +
      "Leave blank to remove this mapping:",
    currentIds,
  );
  if (groupsRaw === null) return; // cancelled
  const groupIds = groupsRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const mapping = { ...currentMap };
  if (groupIds.length) {
    mapping[name] = groupIds;
  } else {
    delete mapping[name];
  }

  const resultEl = document.getElementById(`ds-test-${id}`);
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/acl-site-group-map`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mapping }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const msg = groupIds.length
        ? `✓ Mapped "${name}" → ${groupIds.join(", ")}.`
        : `✓ Unmapped "${name}".`;
      if (resultEl) { resultEl.className = "ds-conn-test-result show ok"; resultEl.textContent = msg; }
      showToast("Site group ACL mapping saved.", true);
      await loadConnections();
    } else {
      const msg = detailMessage(body, "failed to save site group mapping");
      if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ " + msg; }
      showToast(msg, false);
    }
  } catch (e) {
    if (resultEl) { resultEl.className = "ds-conn-test-result show fail"; resultEl.textContent = "✗ Request failed"; }
    showToast("Request failed.", false);
  }
}

/* Saves the "Facts policy" control (`_extRenderFactsPolicy` above) via
   `PATCH .../extraction/facts-config`
   (`app/api/admin_extraction.py::patch_extraction_facts_config`). All four
   fields are always sent — the empty "Instance default" option/input maps
   to `null`, which the endpoint reads as "clear the override", the same
   least-surprise reading the endpoint's own docstring describes.
   `vertex_region` is sent regardless of whether it is currently VISIBLE
   (only shown while Provider is Vertex): its typed value survives a
   provider switch on this page, so flipping the select back to Vertex
   later does not silently lose it, and saving is what actually persists
   or clears it either way. The response carries the RESOLVED value and its
   source ("connection" once an override is set, "instance"/"none"
   otherwise) — shown here rather than guessed, since this page has no copy
   of the instance-level default to resolve against itself. `provider` also
   carries an `effective` field (always a concrete "anthropic"/"vertex",
   never "inherit") — shown too, since that is what a pass actually spends
   against. */
async function saveSpFactsPolicy(id) {
  const retrySel = document.getElementById(`ds-sp-factspolicy-retry-${id}`);
  const transportSel = document.getElementById(`ds-sp-factspolicy-transport-${id}`);
  const providerSel = document.getElementById(`ds-sp-factspolicy-provider-${id}`);
  const regionInput = document.getElementById(`ds-sp-factspolicy-vertexregion-${id}`);
  const statusEl = document.getElementById(`ds-sp-factspolicy-status-${id}`);
  if (!retrySel || !transportSel || !providerSel || !regionInput) return;
  const body = {
    retry_mode: retrySel.value || null,
    transport: transportSel.value || null,
    provider: providerSel.value || null,
    vertex_region: regionInput.value.trim() || null,
  };
  if (statusEl) statusEl.textContent = "Saving…";
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/extraction/facts-config`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const respBody = await r.json().catch(() => ({}));
    if (r.ok) {
      const rm = respBody.retry_mode || {};
      const tr = respBody.transport || {};
      const pr = respBody.provider || {};
      const vr = respBody.vertex_region || {};
      if (statusEl) {
        statusEl.textContent =
          `Retry: ${rm.value} (${rm.source}) · Transport: ${tr.value} (${tr.source}) · ` +
          `Provider: ${pr.value} (${pr.source}) — effective: ${pr.effective} · ` +
          `Vertex region: ${vr.value} (${vr.source})`;
      }
      showToast("Facts policy saved.", true);
      // Keep the in-memory row current — a later `renderConnList()` (e.g.
      // after `loadConnections()`) must redraw the just-saved override, not
      // the value the page loaded with.
      const conn = _connections.find((c) => c.id === id);
      if (conn) {
        conn.config = conn.config || {};
        conn.config.extraction = conn.config.extraction || {};
        const facts = { ...(conn.config.extraction.facts || {}) };
        if (body.retry_mode === null) delete facts.retry_mode; else facts.retry_mode = body.retry_mode;
        if (body.transport === null) delete facts.transport; else facts.transport = body.transport;
        if (body.provider === null) delete facts.provider; else facts.provider = body.provider;
        if (body.vertex_region === null) delete facts.vertex_region; else facts.vertex_region = body.vertex_region;
        conn.config.extraction.facts = facts;
      }
    } else {
      if (statusEl) statusEl.textContent = "";
      showToast(detailMessage(respBody, "failed to save the facts policy"), false);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = "";
    showToast("Request failed", false);
  }
}

/* Saves/clears the "Crawl filter" control (`_extRenderCrawlFilter` above)
   via `PATCH .../extraction/crawl-config`
   (`app/api/admin_extraction.py::patch_extraction_crawl_config`). Moved
   here from the "View configuration" drawer, not duplicated — same
   endpoint, same request shape, only the DOM ids changed to match this
   control's own home on the card. An empty Save is refused client-side
   (Clear is the explicit way to remove a filter, never an implicit blank
   Save); the response's resolved `{value, source}` lands in the status
   line, never guessed beforehand, mirroring `saveSpFactsPolicy` above. */
async function _crawlFilterPatch(id, minModified) {
  const statusEl = document.getElementById(`ds-sp-crawlfilter-status-${id}`);
  if (statusEl) statusEl.textContent = "Saving…";
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/extraction/crawl-config`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_modified: minModified }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const mm = body.min_modified || {};
      if (statusEl) {
        statusEl.textContent = mm.value
          ? `Currently: files modified on/after ${mm.value} (${mm.source}).`
          : "Currently: no filter — every file is crawled.";
      }
      showToast(mm.value ? `Crawl filter set to ${mm.value}.` : "Crawl filter cleared.", true);
      // Keep the in-memory row current — a later `renderConnList()` (e.g.
      // after `loadConnections()`) must redraw the just-saved override, not
      // the value the page loaded with.
      const conn = _connections.find((c) => c.id === id);
      if (conn) {
        conn.config = conn.config || {};
        conn.config.extraction = conn.config.extraction || {};
        const crawl = { ...(conn.config.extraction.crawl || {}) };
        if (minModified === null) delete crawl.min_modified; else crawl.min_modified = minModified;
        conn.config.extraction.crawl = crawl;
      }
    } else {
      if (statusEl) statusEl.textContent = "";
      showToast(detailMessage(body, "couldn't save the crawl filter"), false);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = "";
    showToast("Request failed.", false);
  }
}

function crawlFilterSave(id) {
  const input = document.getElementById(`ds-sp-crawlfilter-date-${id}`);
  const value = (input && input.value || "").trim();
  if (!value) {
    showToast("Pick a date first, or use Clear to remove the filter.", false);
    return;
  }
  _crawlFilterPatch(id, value);
}

function crawlFilterClear(id) {
  const input = document.getElementById(`ds-sp-crawlfilter-date-${id}`);
  if (input) input.value = "";
  _crawlFilterPatch(id, null);
}

function toggleSpCertRow(id) {
  const row = document.getElementById(`ds-sp-cert-row-${id}`);
  setSourceOpen(id, true);
  if (row.classList.contains("show")) {
    row.classList.remove("show");
    document.getElementById(`ds-sp-cert-input-${id}`).value = "";
  } else {
    row.classList.add("show");
    document.getElementById(`ds-sp-cert-input-${id}`).focus();
  }
}

async function saveSpCertificate(id) {
  const input = document.getElementById(`ds-sp-cert-input-${id}`);
  const pem = input.value.trim();
  if (!pem) { showToast("Paste a certificate first.", false); return; }
  try {
    // Same generic secret slot Keboola's "Rotate storage token" writes
    // (`kind: "storage"`, the default) — a SharePoint connection has no
    // Storage API to preflight against, so the server stores it unverified,
    // same as the connect wizard's own cert-upload step.
    const r = await fetch(`${API_CONNECTIONS}/${id}/secret`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: pem, kind: "storage" }),
    });
    if (r.ok || r.status === 204) {
      showToast("Certificate updated.", true);
      input.value = "";
      toggleSpCertRow(id);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      const msg = r.status === 409
        ? "Server vault key missing — set AGNES_VAULT_KEY first."
        : "Failed: " + detailMessage(body, "unknown error");
      showToast(msg, false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

/* "Split this site" — the product-shaped front end for
   GET/POST .../split-plan and .../splits (app/api/admin_sharepoint.py):
   preview a greedy-packed folder split, then create the sibling
   connections from it. The panel row's own "show" toggle mirrors
   toggleSpCertRow above; unlike that one, closing it also clears any
   preview result so reopening it never shows a stale plan next to fresh
   inputs. */
function toggleSpSplitRow(id) {
  const row = document.getElementById(`ds-sp-split-row-${id}`);
  setSourceOpen(id, true);
  if (row.classList.contains("show")) {
    row.classList.remove("show");
    const resultEl = document.getElementById(`ds-sp-split-result-${id}`);
    if (resultEl) resultEl.innerHTML = "";
  } else {
    row.classList.add("show");
    document.getElementById(`ds-sp-split-n-${id}`).focus();
  }
}

function _spSplitN(id) {
  const el = document.getElementById(`ds-sp-split-n-${id}`);
  const n = Number(el.value);
  return Number.isInteger(n) && n >= 1 && n <= 50 ? n : null;
}

async function previewSpSplit(id) {
  const resultEl = document.getElementById(`ds-sp-split-result-${id}`);
  const n = _spSplitN(id);
  if (!n) {
    showToast("Number of parts must be a whole number between 1 and 50.", false);
    return;
  }
  const minModified = document.getElementById(`ds-sp-split-min-modified-${id}`).value || "";
  resultEl.innerHTML = `<p class="field-hint">Computing plan — reads the drive root and counts each folder's documents live, this can take a few seconds…</p>`;
  const params = new URLSearchParams({ n: String(n) });
  if (minModified) params.set("min_modified", minModified);
  try {
    const r = await fetch(
      `/api/admin/sharepoint/connections/${encodeURIComponent(id)}/split-plan?${params.toString()}`,
      { credentials: "include" }
    );
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      resultEl.innerHTML = `<p class="ds-conn-test-result show fail">✗ ${_esc(detailMessage(body, "failed to compute the split plan"))}</p>`;
      return;
    }
    _renderSpSplitPlan(id, body);
  } catch (e) {
    resultEl.innerHTML = `<p class="ds-conn-test-result show fail">✗ Request failed</p>`;
  }
}

function _renderSpSplitPlan(id, plan) {
  const resultEl = document.getElementById(`ds-sp-split-result-${id}`);
  const groups = plan.groups || [];
  const rows = groups.map((g) => `
    <tr><td>${_esc(g.name)}</td><td>${(g.folders || []).length}</td><td>${g.documents || 0}</td></tr>
  `).join("");
  const loose = plan.loose_root_files || [];
  const shown = loose.slice(0, 10).map(_esc).join(", ");
  const looseHtml = loose.length
    ? `<p class="field-hint" style="flex-basis:100%;">⚠ ${loose.length} file${loose.length === 1 ? "" : "s"} sit directly at the drive root and will NOT be covered by any part: ${shown}${loose.length > 10 ? ", …" : ""}</p>`
    : "";
  resultEl.innerHTML = `
    <table class="ds-sp-split-table" style="flex-basis:100%;width:100%;">
      <thead><tr><th>Part</th><th>Folders</th><th>Documents</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="3">No folders found at the drive root.</td></tr>`}</tbody>
    </table>
    <p class="field-hint" style="flex-basis:100%;">Total documents across all folders: ${plan.total_documents || 0}</p>
    ${looseHtml}
    <label class="field-hint" style="flex-basis:100%;display:flex;align-items:center;gap:6px;">
      <input type="checkbox" id="ds-sp-split-start-${id}">
      Start each part's crawl immediately after creating it
    </label>
    ${groups.length ? `<button type="button" class="btn btn-primary" onclick="applySpSplit('${id}')">Create ${groups.length} connection${groups.length === 1 ? "" : "s"}</button>` : ""}
  `;
}

async function applySpSplit(id) {
  const resultEl = document.getElementById(`ds-sp-split-result-${id}`);
  const n = _spSplitN(id);
  if (!n) return;
  const minModified = document.getElementById(`ds-sp-split-min-modified-${id}`).value || null;
  const transport = document.getElementById(`ds-sp-split-transport-${id}`).value || null;
  const retryMode = document.getElementById(`ds-sp-split-retry-${id}`).value || null;
  const startEl = document.getElementById(`ds-sp-split-start-${id}`);
  const start = !!(startEl && startEl.checked);
  const payload = { n, start };
  if (minModified) payload.min_modified = minModified;
  if (transport) payload.transport = transport;
  if (retryMode) payload.retry_mode = retryMode;
  resultEl.insertAdjacentHTML("beforeend", `<p class="field-hint" style="flex-basis:100%;">Creating connections…</p>`);
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(id)}/splits`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      showToast(detailMessage(body, "failed to create the split"), false);
      return;
    }
    const created = body.connections || [];
    showToast(`Created ${created.length} connection${created.length === 1 ? "" : "s"}.`, true);
    toggleSpSplitRow(id);
    await refreshSourcePipelines();
    await loadConnections();
  } catch (e) {
    showToast("Request failed.", false);
  }
}

async function deleteConn(id) {
  const conn = _connections.find(c => c.id === id);
  const name = conn ? (conn.name || "") : "";
  if (!await confirmModal({
    title: "Delete data connection?",
    message: `"${name}" will be removed. This cannot be undone.`,
    confirmText: "Delete",
    danger: true,
  })) return;
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}`, { method: "DELETE", credentials: "include" });
    if (r.ok || r.status === 204) {
      showToast(`"${name}" deleted.`, true);
      _connections = _connections.filter(c => c.id !== id);
      // Not only about the card that just went away: with one connection of a
      // type left, that type's unlinked tables become attributable to it.
      await refreshSourcePipelines();
      renderConnList();
    } else {
      const body = await r.json().catch(() => ({}));
      showToast("Delete failed: " + detailMessage(body, "unknown error"), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

async function setDefaultConn(id) {
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_default: true }),
    });
    if (r.ok) {
      showToast("Default project updated.", true);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      showToast("Failed: " + detailMessage(body, "unknown error"), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

function toggleRotate(id) {
  const row = document.getElementById(`ds-rotate-${id}`);
  // These three inline tasks live INSIDE the settings body now, and they are
  // reached from a menu that does not know whether the body is open. Opening
  // it is part of the verb — otherwise the menu item would appear to do
  // nothing at all.
  setSourceOpen(id, true);
  if (row.classList.contains("show")) {
    row.classList.remove("show");
    document.getElementById(`ds-rotate-input-${id}`).value = "";
  } else {
    row.classList.add("show");
    document.getElementById(`ds-rotate-input-${id}`).focus();
  }
}

async function saveRotatedToken(id) {
  const input = document.getElementById(`ds-rotate-input-${id}`);
  const token = input.value.trim();
  if (!token) { showToast("Paste a token first.", false); return; }
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/secret`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: token }),
    });
    if (r.ok || r.status === 204) {
      showToast("Token rotated.", true);
      input.value = "";
      toggleRotate(id);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      const msg = r.status === 409
        ? "Server vault key missing — set AGNES_VAULT_KEY first."
        : "Failed: " + detailMessage(body, "unknown error");
      showToast(msg, false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

/* ── Master (owner) token — separate vault slot for the semantic-layer sync ── */

function toggleMasterToken(id) {
  const row = document.getElementById(`ds-master-row-${id}`);
  setSourceOpen(id, true);
  if (row.classList.contains("show")) {
    row.classList.remove("show");
    document.getElementById(`ds-master-input-${id}`).value = "";
  } else {
    row.classList.add("show");
    document.getElementById(`ds-master-input-${id}`).focus();
  }
}

async function saveMasterToken(id) {
  const input = document.getElementById(`ds-master-input-${id}`);
  const token = input.value.trim();
  if (!token) { showToast("Paste a token first.", false); return; }
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/secret`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: token, kind: "master" }),
    });
    if (r.ok || r.status === 204) {
      showToast("Master token saved.", true);
      input.value = "";
      toggleMasterToken(id);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      const msg = r.status === 409
        ? "Server vault key missing — set AGNES_VAULT_KEY first."
        : "Failed: " + detailMessage(body, "unknown error");
      showToast(msg, false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

async function removeMasterToken(id) {
  if (!confirm("Remove the master token for this connection? Semantic-layer sync will stop working until a new one is set.")) return;
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/secret?kind=master`, { method: "DELETE", credentials: "include" });
    if (r.ok || r.status === 204) {
      showToast("Master token removed.", true);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      showToast("Failed: " + detailMessage(body, "unknown error"), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

/* Clear a connection's recorded project identity.
   Sends the keys explicitly as null rather than omitting them: `PUT /{id}`
   replaces the stored config, and the handler carries `project_id` /
   `project_name` FORWARD when they are absent (an admin form posts neither,
   so an ordinary edit must not drop the binding). An explicit null is how
   the caller says "clear it" — see `update_connection`. */
async function unbindProject(id) {
  const row = _connections.find(c => c.id === id);
  if (!row) return;
  if (!confirm("Forget which Keboola project this connection is locked to?\n\nThe next token you store will re-bind it to whichever project that token opens.")) return;
  try {
    const r = await fetch(`/api/admin/source-connections/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: { ...(row.config || {}), project_id: null, project_name: null } }),
    });
    if (r.ok) {
      showToast("Project binding cleared.", true);
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      showToast("Failed: " + detailMessage(body, `HTTP ${r.status}`), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

/* A FastAPI `detail` is a string on some paths and a structured object on
   others (`{error, message, …}` plus a path-specific list: `tables` for a
   connection still in use, `still_present` for a partial chat-tools
   teardown). Pasting it into a template literal renders "[object Object]"
   and throws away the one thing the admin needs — which is what the chat-
   tools toast did. One reader for every caller. (Devin Review.) */
function detailMessage(body, fallback) {
  const detail = body && body.detail;
  if (detail && typeof detail === "object") {
    let msg = detail.message || detail.error || fallback;
    for (const key of ["tables", "still_present"]) {
      const list = detail[key];
      if (Array.isArray(list) && list.length) msg += " (" + list.join(", ") + ")";
    }
    return msg;
  }
  return detail || fallback;
}

function _chatToolsFactHtml(row) {
  // Exposes this Keboola project's own MCP tools (query_data, buckets, search,
  // semantic layer, …) to the chat agent by deriving an MCP source from this
  // connection. Keboola-only; needs a stored storage token to be useful.
  if (row.source_type !== "keboola") return "";
  const id = row.id;
  const on = row.has_chat_tools === true;
  const badgeClass = on ? "badge-vault" : "badge-unset";
  const badgeLabel = on ? "on" : "off";
  const hint = on
    // Enabling registers the source AND its tools, so there is something to
    // grant — but registered is not reachable, and the grant lives on each
    // tool's own page (/admin/mcp-tools/{id}/grants), not on Access. Saying
    // "done" here, or naming the wrong screen, both leave the admin one step
    // short of the agent seeing anything. The manual Introspect detour this
    // text used to describe is gone; the endpoint does it.
    ? "Tools are registered but not yet reachable — grant them under Admin -> MCP sources, on each tool's Grants page."
    : "Give the chat agent this project's own Keboola tools. The first run downloads the server, so it can take a minute.";
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="${_esc(hint)}">Chat tools (Keboola MCP)</span>
    <span class="ds-src__fact-v"><span class="ds-badge ${badgeClass}">${badgeLabel}</span></span>
    <span class="ds-src__fact-a">
      <button type="button" class="btn btn-secondary" onclick="toggleChatTools('${id}', ${on ? "true" : "false"})">${on ? "Turn off" : "Turn on"}</button>
      ${on ? `<button type="button" class="btn btn-secondary" onclick="toggleChatTools('${id}', false, true)">Re-sync token</button>` : ""}
      ${on && row.chat_tools_source_id ? `
      <select id="ds-grant-group-${id}" class="ds-dropdown-native" aria-label="Group to grant this project's tools to"></select>
      <div class="ds-dropdown" data-ds-dropdown-target="ds-grant-group-${id}">
        <button type="button" class="ds-dropdown-btn" id="ds-grant-group-${id}-dd-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="ds-grant-group-${id}-dd-menu">
          <span class="ds-dropdown-btn-label" id="ds-grant-group-${id}-dd-btn-label"></span>
          <svg class="ds-dropdown-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        <ul class="ds-dropdown-menu" id="ds-grant-group-${id}-dd-menu" role="menu" aria-label="Group to grant this project's tools to" hidden></ul>
      </div>
      <button type="button" class="btn btn-secondary" onclick="grantChatTools('${id}', '${_esc(row.chat_tools_source_id)}')">Grant all to group</button>` : ""}
    </span>
  </div>`;
}

let _groupsCache = null;

async function _loadGroups() {
  if (_groupsCache) return _groupsCache;
  try {
    const r = await fetch(`/api/admin/groups`, { credentials: "include" });
    // Only a successful load is cached. `[]` is truthy, so caching the
    // fallback would make `if (_groupsCache) return _groupsCache` treat one
    // transient failure as a valid hit for the rest of the page visit —
    // leaving the grant picker permanently blank until a full reload.
    if (r.ok) _groupsCache = await r.json();
    else return [];
  } catch (_) {
    return [];
  }
  return _groupsCache;
}

// Rebuilds the paired `.ds-dropdown` (see _components.html's `dropdown`
// macro) menu items + button label from the native select's own freshly-set
// <option>s, then re-runs ds_dropdown.js's init via the exported
// `dsDropdownInit` re-init hook — needed because `_connectionCardHtml`
// re-renders this markup fresh on every list refresh, with an empty menu
// until `_fillGrantPickers` below fills the paired native select.
function _syncGrantPickerDropdown(sel) {
  const host = document.querySelector(`.ds-dropdown[data-ds-dropdown-target="${sel.id}"]`);
  if (!host) return;
  const opts = Array.from(sel.options);
  host.querySelector(".ds-dropdown-btn-label").textContent = opts.length ? opts[0].textContent : "";
  host.querySelector(".ds-dropdown-menu").innerHTML = opts
    .map((o, i) => `<li class="ds-dropdown-menu-item${i === 0 ? " is-selected" : ""}" role="menuitemradio" aria-checked="${i === 0}" tabindex="0" data-value="${_esc(o.value)}">${_esc(o.textContent)}</li>`)
    .join("");
  if (window.dsDropdownInit) window.dsDropdownInit(host);
}

// Rebuilds the paired `.ds-dropdown`'s button AND menu from a select's own
// freshly-set <option>s, then re-runs ds_dropdown.js's init via the exported
// `dsDropdownInit` hook. Unlike `_syncGrantPickerDropdown` above (safe to
// touch only the menu because its host's whole row is discarded and rebuilt
// fresh on every render), this is for statically-rendered `.ds-dropdown`
// markup whose surrounding DOM persists across repeat calls — leaving the
// same <button> node in place across two init() calls double-registers its
// click handler and the menu never appears to open, so the button is
// recreated too.
function _syncDropdownRebuild(sel) {
  const host = document.querySelector(`.ds-dropdown[data-ds-dropdown-target="${sel.id}"]`);
  if (!host) return;
  const ddId = `${sel.id}-dd`;
  const opts = Array.from(sel.options);
  const currentOpt = opts.find((o) => o.value === sel.value) || opts[0];
  const menuEl = host.querySelector(".ds-dropdown-menu");
  const ariaLabel = menuEl ? menuEl.getAttribute("aria-label") || "" : "";
  const nameHtml = ariaLabel ? `<span class="ds-dropdown-name" id="${ddId}-name">${_esc(ariaLabel)}</span>` : "";
  const labelledBy = ariaLabel ? ` aria-labelledby="${ddId}-name ${ddId}-btn-label"` : "";
  const items = opts
    .map((o) => `<li class="ds-dropdown-menu-item${o === currentOpt ? " is-selected" : ""}" role="menuitemradio" aria-checked="${o === currentOpt}" tabindex="0" data-value="${_esc(o.value)}">${_esc(o.textContent)}</li>`)
    .join("");
  host.innerHTML = `<button type="button" class="ds-dropdown-btn" id="${ddId}-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="${ddId}-menu"${labelledBy}>${nameHtml}<span class="ds-dropdown-btn-label" id="${ddId}-btn-label">${currentOpt ? _esc(currentOpt.textContent) : ""}</span><svg class="ds-dropdown-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button><ul class="ds-dropdown-menu" id="${ddId}-menu" role="menu" aria-label="${_esc(ariaLabel || "Options")}" hidden>${items}</ul>`;
  if (window.dsDropdownInit) window.dsDropdownInit(host);
}

async function _fillGrantPickers() {
  const groups = await _loadGroups();
  document.querySelectorAll('select[id^="ds-grant-group-"]').forEach((sel) => {
    if (sel.options.length) return;
    sel.innerHTML = groups
      .map((g) => `<option value="${_esc(g.id)}">${_esc(g.name)}</option>`)
      .join("");
    _syncGrantPickerDropdown(sel);
  });
}

async function grantChatTools(id, sourceId) {
  const sel = document.getElementById(`ds-grant-group-${id}`);
  const groupId = sel && sel.value;
  if (!groupId) { showToast("Pick a group first.", false); return; }
  try {
    const r = await fetch(`/api/admin/mcp-sources/${encodeURIComponent(sourceId)}/grants`, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: groupId }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      // "granted 0 of 37" is not the same news as "granted 37 of 37", and
      // both look like success without the counts.
      const extra = body.already_granted ? ` (${body.already_granted} already granted)` : "";
      // A grant does not reach a mutating tool — the passthrough gate refuses
      // those for every non-admin regardless, so promising reach here would
      // be false for exactly the tools an analyst would notice missing.
      const gated = body.admin_only ? ` ${body.admin_only} are mutating and stay locked until opted in per tool (agnes admin mcp tool grant <tool_id> --group <g> --allow-mutating).` : "";
      const off = body.skipped_disabled ? ` ${body.skipped_disabled} disabled tools were skipped.` : "";
      showToast(`Granted ${body.granted} of ${body.total} tools${extra}.${gated}${off}`, true);
      await refreshSourcePipelines();
    } else {
      showToast("Failed: " + detailMessage(body, `HTTP ${r.status}`), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

async function toggleChatTools(id, isOn, resync) {
  // `resync` re-runs enable on an already-enabled source: the derived source
  // holds its own copy of the token, so a rotated connection token does not
  // reach it until enable runs again.
  const turningOff = isOn && !resync;
  if (turningOff && !confirm("Turn off chat tools for this project? The agent loses its Keboola tools and the stored copy of the token is deleted.")) return;
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/chat-tools`, {
      method: turningOff ? "DELETE" : "POST",
      credentials: "include",
    });
    if (r.ok || r.status === 204) {
      const ok = await r.json().catch(() => ({}));
      const n = ok.tools_registered;
      // Mutating tools are refused for non-admins by the policy gate even
      // when granted, and an upstream with no read-only annotations makes
      // that ALL of them — the "grant them" advice must say so or it is a
      // false promise. (Devin Review on this PR, fifth round.)
      const adminOnly = ok.tools_admin_only;
      const allAdminOnly = n > 0 && adminOnly === n;
      showToast(
        turningOff
          ? "Chat tools turned off."
          : (resync
              ? "Token re-synced."
              : `Chat tools on — ${n === undefined ? "tools" : n + " tools"} registered. Grant them under Admin -> MCP sources to make them reachable.`
                + (allAdminOnly ? " All are recorded as mutating (no read-only annotations upstream); mutating tools stay locked for a group until an admin opts each in per tool (allow_mutating on the tool grant)." : "")),
        true,
      );
      await refreshSourcePipelines();
      await loadConnections();
    } else {
      const body = await r.json().catch(() => ({}));
      showToast("Failed: " + detailMessage(body, `HTTP ${r.status}`), false);
    }
  } catch (_) {
    showToast("Request failed.", false);
  }
}

/* ── Browse & register (inline, per existing connection) ─────────────────────── */

async function toggleBrowse(id) {
  const panel = document.getElementById(`ds-browse-${id}`);
  setSourceOpen(id, true);
  if (panel.classList.contains("show")) {
    panel.classList.remove("show");
    panel.innerHTML = "";
    return;
  }
  panel.classList.add("show");
  panel.innerHTML = `<div class="ds-loading">Loading buckets and tables…</div>`;
  try {
    const r = await fetch(`${API_CONNECTIONS}/${id}/tables`, { credentials: "include" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      panel.innerHTML = `<div class="ds-browse-error">Failed to list tables: ${_esc(data.detail || String(r.status))}</div>`;
      return;
    }
    panel.innerHTML = _scopeNote(data) + _renderBucketPicker(id, data.buckets || []);
    // Once per fresh render only — never inside _syncPicker, which also runs
    // on every checkbox click against this SAME, still-persisting markup;
    // re-running dsDropdownInit there would double-register each row's
    // button click handler.
    if (window.dsDropdownInit) panel.querySelectorAll(".ds-dropdown").forEach(window.dsDropdownInit);
    _syncPicker(panel);
  } catch (e) {
    panel.innerHTML = `<div class="ds-browse-error">Request failed.</div>`;
  }
}

function _scopeNote(data) {
  // Server marks bucket-scoped (custom access) tokens whose listing came
  // from the token's own bucketPermissions rather than the project-wide
  // listing — tell the admin the picker is intentionally partial.
  return data && data.scope === "token_buckets"
    ? `<div class="ds-scope-note">Bucket-scoped token — showing only the buckets this token can read.</div>`
    : "";
}

/* Buckets render CLOSED. On a real project this list is dozens of buckets and
   hundreds of tables; open, it is a wall nobody can navigate, and the one
   thing an admin always knows is which bucket they want. The exception is a
   single-bucket project, where a closed accordion is a click that asks
   nothing. */
function _renderBucketPicker(connId, buckets) {
  if (!buckets.length) {
    return `<div class="ds-empty">No buckets/tables visible to this token.</div>`;
  }
  const openByDefault = buckets.length === 1;
  const totalTables = buckets.reduce((n, b) => n + ((b.tables || []).length), 0);
  const groups = buckets.map((b, bi) => {
    const count = (b.tables || []).length;
    const rows = (b.tables || []).map((t, ti) => {
      // register-table stores bucket and the BARE in-bucket table name in
      // separate columns; the sync path re-composes `bucket.table`. Keboola's
      // /tables returns `t.id` as the FULL id (`bucket.table`), so strip the
      // bucket prefix here — sending the full id doubles the bucket at
      // export time and the materialize 404s on a nonexistent table.
      const bare = (t.id && b.id && t.id.startsWith(b.id + "."))
        ? t.id.slice(b.id.length + 1)
        : (t.name || t.id || "");
      // A raw Keboola name with hyphens (Shopify exports especially) fails
      // register-table's identifier check outright, and the bulk picker gave
      // the operator no way to retype it — a dead end on every row in the
      // same batch. Rows that would fail get an editable, pre-suggested
      // input instead of a plain label; rows that already pass are untouched.
      const needsRename = !_wouldPassRegisterCheck(t.name);
      const nameField = needsRename
        ? `<input type="text" class="ds-table-name-input" value="${_esc(_suggestTableName(t.name))}"
             aria-label="Registered name for ${_esc(t.name)}"
             title="&quot;${_esc(t.name)}&quot; isn't a valid table name — letters, digits, and underscores only. Edit this suggestion before registering.">`
        : `<span class="ds-table-name">${_esc(t.name)}</span>`;
      const modeId = `ds-tmode-${connId}-${bi}-${ti}`;
      return `
      <div class="ds-table-row" data-search="${_esc((t.name || "").toLowerCase())}">
        <label>
          <input type="checkbox" class="ds-table-checkbox" data-conn="${connId}"
                 data-bucket="${_esc(b.id)}" data-table-id="${_esc(t.id)}" data-table-name="${_esc(t.name)}"
                 data-table-bare="${_esc(bare)}">
          ${nameField}
        </label>
        <span class="ds-table-rows">${(t.rows ?? 0).toLocaleString()} rows</span>
        <span class="ds-table-mode-wrap" title="materialized = scheduled copy on the server (DuckDB extension); local = Storage API direct extract, supports incremental sync; live = every query goes to Keboola Storage, nothing syncs">
          <select id="${modeId}" class="ds-table-mode ds-dropdown-native" data-kb-mode aria-label="How ${_esc(t.name)} is served">
            <option value="materialized" selected>materialized</option>
            <option value="local">local</option>
            <option value="remote">live</option>
          </select>
          ${_dropdownMarkupHtml(`${modeId}-dd`, modeId, `How ${t.name} is served`,
            [{ value: "materialized", label: "materialized" }, { value: "local", label: "local" }, { value: "remote", label: "live" }], "materialized")}
        </span>
        <span class="ds-table-status" id="ds-tstatus-${connId}-${bi}-${ti}"></span>
      </div>`;
    }).join("");
    const label = b.name || b.id;
    const panelId = `ds-bkt-${connId}-${bi}`;
    return `
      <div class="ds-bucket-group${openByDefault ? " is-open" : ""}"
           data-bucket-group data-user-open="${openByDefault ? "1" : "0"}"
           data-search="${_esc((label + " " + (b.id || "")).toLowerCase())}">
        <div class="ds-bucket-hd">
          <input type="checkbox" class="ds-bucket-check"
                 aria-label="Select all ${count} table${count === 1 ? "" : "s"} in ${_esc(label)}">
          <button type="button" class="ds-bucket-toggle" aria-expanded="${openByDefault}" aria-controls="${panelId}">
            <span class="ds-bucket-caret" aria-hidden="true">▶</span>
            <span class="ds-bucket-title">${_esc(label)}</span>
            <span class="ds-bucket-stage">${_esc(b.stage || "")}</span>
            <span class="ds-bucket-meta" data-bucket-meta>${count} table${count === 1 ? "" : "s"}</span>
          </button>
        </div>
        <div class="ds-bucket-tables" id="${panelId}"${openByDefault ? "" : " hidden"}>
          ${rows || '<div class="ds-empty">No tables in this bucket.</div>'}
        </div>
      </div>`;
  }).join("");
  return `
    <div class="ds-picker-bar">
      <input type="search" class="ds-picker-search" data-picker-search
             placeholder="Filter buckets and tables…" aria-label="Filter buckets and tables"
             autocomplete="off" spellcheck="false">
      <span class="ds-picker-count" data-picker-count aria-live="polite">0 of ${totalTables} selected</span>
      <button type="button" class="ds-picker-link" data-picker-all>Select all</button>
      <button type="button" class="ds-picker-link" data-picker-none disabled>Clear</button>
      <button type="button" class="ds-picker-link" data-picker-expand>${openByDefault ? "Collapse all" : "Expand all"}</button>
    </div>
    ${groups}
    <div class="ds-browse-actions">
      <button type="button" class="btn btn-primary" onclick="registerSelected('${connId}')">Register selected</button>
      <span class="ds-browse-error" id="ds-browse-error-${connId}"></span>
    </div>`;
}

/* ── Picker interaction ───────────────────────────────────────────────────
   One delegated set of handlers for BOTH places the picker renders: the
   wizard's step 2 and a connection card's inline browse panel. `root` is
   whichever of the two the event landed in, so the two never see each
   other's checkboxes or counts.

   Selection lives in the DOM (the checkboxes themselves), which is what
   makes collapsing a bucket, filtering the list, or stepping back to this
   screen from Bundle non-destructive: nothing is ever re-rendered, only
   hidden. */

const _pickerRoot = (el) =>
  el.closest("#ds-wizard-tables-body") || el.closest(".ds-browse-panel");

const _bucketBoxes = (group) => [...group.querySelectorAll(".ds-table-checkbox")];

function _syncBucket(group) {
  const boxes = _bucketBoxes(group);
  const on = boxes.filter((b) => b.checked).length;
  const head = group.querySelector(".ds-bucket-check");
  if (head) {
    head.checked = boxes.length > 0 && on === boxes.length;
    head.indeterminate = on > 0 && on < boxes.length;
  }
  const meta = group.querySelector("[data-bucket-meta]");
  if (meta) {
    const n = boxes.length;
    meta.innerHTML = on
      ? `<span class="on">${on} selected</span> · ${n} table${n === 1 ? "" : "s"}`
      : `${n} table${n === 1 ? "" : "s"}`;
  }
}

function _syncPicker(root) {
  if (!root) return;
  const groups = [...root.querySelectorAll("[data-bucket-group]")];
  groups.forEach(_syncBucket);
  const boxes = [...root.querySelectorAll(".ds-table-checkbox")];
  // The per-row mode select only means something for a row about to be
  // registered — show it exactly for the checked ones. Bulk paths (bucket
  // check, Select all/none) set .checked programmatically, which fires no
  // change event, so the class has to be reasserted here, the one sync
  // point every path already goes through.
  boxes.forEach((b) => b.closest(".ds-table-row").classList.toggle("is-sel", b.checked));
  const selected = boxes.filter((b) => b.checked);
  // A table selected but filtered out of view is still going to be
  // registered — say so, rather than letting the filter imply otherwise.
  const hidden = selected.filter((b) => b.closest(".ds-table-row").hidden).length;
  const countEl = root.querySelector("[data-picker-count]");
  if (countEl) {
    countEl.innerHTML = `${selected.length} of ${boxes.length} selected` +
      (hidden ? ` <span class="hid">(${hidden} hidden by the filter)</span>` : "");
  }
  const search = root.querySelector("[data-picker-search]");
  const filtering = !!(search && search.value.trim());
  const allBtn = root.querySelector("[data-picker-all]");
  if (allBtn) {
    const visible = boxes.filter((b) => !b.closest(".ds-table-row").hidden);
    allBtn.textContent = filtering ? "Select matching" : "Select all";
    allBtn.disabled = !visible.length || visible.every((b) => b.checked || b.disabled);
  }
  const noneBtn = root.querySelector("[data-picker-none]");
  if (noneBtn) noneBtn.disabled = !selected.length;
  const expandBtn = root.querySelector("[data-picker-expand]");
  if (expandBtn) {
    // Filtering opens buckets on its own — the label has to describe the list
    // as it stands, not as it was left.
    const shown = [...root.querySelectorAll("[data-bucket-group]:not([hidden])")];
    expandBtn.textContent = shown.length && shown.every((g) => g.classList.contains("is-open"))
      ? "Collapse all" : "Expand all";
  }
}

function _setBucketOpen(group, open) {
  group.classList.toggle("is-open", open);
  const panel = group.querySelector(".ds-bucket-tables");
  if (panel) panel.hidden = !open;
  const toggle = group.querySelector(".ds-bucket-toggle");
  if (toggle) toggle.setAttribute("aria-expanded", String(open));
}

/* Filtering matches a bucket by its own name/id and a table by its name; a
   bucket that matches keeps ALL its tables (you asked for the bucket), one
   that only contains matches shows just those. Matching buckets spring open
   so the hit is visible, and clearing the filter puts every bucket back the
   way the admin left it rather than the way the filter left it. */
function _filterPicker(root, raw) {
  const q = (raw || "").trim().toLowerCase();
  for (const group of root.querySelectorAll("[data-bucket-group]")) {
    const rows = [...group.querySelectorAll(".ds-table-row")];
    if (!q) {
      group.hidden = false;
      rows.forEach((r) => { r.hidden = false; });
      _setBucketOpen(group, group.dataset.userOpen === "1");
      continue;
    }
    const bucketHit = (group.dataset.search || "").includes(q);
    let hits = 0;
    for (const row of rows) {
      const hit = bucketHit || (row.dataset.search || "").includes(q);
      row.hidden = !hit;
      if (hit) hits++;
    }
    // An empty bucket has no rows to hit — matching its own name is the only
    // way it can match at all, and hiding it would deny it exists.
    group.hidden = hits === 0 && !bucketHit;
    if (hits) _setBucketOpen(group, true);
  }
  _syncPicker(root);
}

document.addEventListener("click", (e) => {
  const root = _pickerRoot(e.target);
  if (!root) return;

  const toggle = e.target.closest(".ds-bucket-toggle");
  if (toggle) {
    const group = toggle.closest("[data-bucket-group]");
    const open = !group.classList.contains("is-open");
    // Remembered so clearing the filter restores the admin's own layout.
    group.dataset.userOpen = open ? "1" : "0";
    _setBucketOpen(group, open);
    _syncPicker(root);
    return;
  }

  if (e.target.closest("[data-picker-expand]")) {
    const groups = [...root.querySelectorAll("[data-bucket-group]:not([hidden])")];
    const open = !groups.every((g) => g.classList.contains("is-open"));
    groups.forEach((g) => { g.dataset.userOpen = open ? "1" : "0"; _setBucketOpen(g, open); });
    _syncPicker(root);
    return;
  }

  // Bulk gestures act on what is VISIBLE — with a filter on, "select all"
  // meaning "including the 300 you filtered away" is a trap.
  const all = e.target.closest("[data-picker-all]");
  const none = e.target.closest("[data-picker-none]");
  if (all || none) {
    for (const box of root.querySelectorAll(".ds-table-checkbox")) {
      if (box.disabled) continue;
      if (all && box.closest(".ds-table-row").hidden) continue;
      box.checked = !!all;
    }
    _syncPicker(root);
  }
});

document.addEventListener("change", (e) => {
  const root = _pickerRoot(e.target);
  if (!root) return;
  if (e.target.classList.contains("ds-bucket-check")) {
    const group = e.target.closest("[data-bucket-group]");
    const want = e.target.checked;
    for (const box of _bucketBoxes(group)) {
      // Already-registered rows are disabled; a bulk gesture must not appear
      // to change them.
      if (box.disabled || box.closest(".ds-table-row").hidden) continue;
      box.checked = want;
    }
    _syncPicker(root);
    return;
  }
  if (e.target.classList.contains("ds-table-checkbox")) _syncPicker(root);
});

document.addEventListener("input", (e) => {
  if (!e.target.matches("[data-picker-search]")) return;
  const root = _pickerRoot(e.target);
  if (root) _filterPicker(root, e.target.value);
});

async function registerSelected(connId, errElId) {
  // Returns {ok: [{id, name, bucket}], failed: n} — the wizard's Bundle step
  // consumes the list; the standalone Browse&register path ignores it. The
  // id is derived the way the server derives it (see register_table in
  // app/api/admin.py: name → strip/lower/underscores), so the Bundle step can
  // attach tables to packages without a second round-trip.
  const checked = document.querySelectorAll(
    `.ds-table-checkbox[data-conn="${connId}"]:checked`
  );
  // Two surfaces share this function — the wizard's step-2 list and the
  // standalone Browse&register panel — and each has its own error line. It used
  // to always write to the panel's `ds-browse-error-<conn>`, which in the
  // wizard sits on the page BEHIND the open drawer: "select at least one table"
  // was rendered somewhere the reader could not see, so the enabled primary
  // button just did nothing. Only the caller knows which surface it is, so it
  // names its own line; the id is still the panel's by default.
  const errEl = document.getElementById(errElId || `ds-browse-error-${connId}`);
  const showErr = (msg) => {
    if (!errEl) return;
    errEl.textContent = msg;
    // "block", not "": `.ds-wizard-error` is display:none in CSS, so clearing
    // the inline style would hand it straight back to that rule. Flex parents
    // blockify their items, so this is a no-op for the browse panel's span.
    errEl.style.display = msg ? "block" : "none";
  };
  showErr("");
  if (!checked.length) {
    showErr("Select at least one table.");
    return { ok: [], failed: 0 };
  }
  // Per-row "registering…/✗ failed" is the only account of what happened, and
  // buckets are closed by default — open the ones being written to, and drop
  // any filter, so the report is on screen while it is being written.
  const root = _pickerRoot(checked[0]);
  if (root) {
    const search = root.querySelector("[data-picker-search]");
    if (search && search.value) { search.value = ""; _filterPicker(root, ""); }
    for (const cb of checked) {
      const group = cb.closest("[data-bucket-group]");
      if (group && !group.classList.contains("is-open")) _setBucketOpen(group, true);
    }
  }
  const registered = [];
  let okCount = 0, failCount = 0, alreadyCount = 0;
  for (const cb of checked) {
    const rowEl = cb.closest(".ds-table-row");
    const statusId = rowEl.querySelector(".ds-table-status").id;
    const statusEl = document.getElementById(statusId);
    const modeSel = rowEl.querySelector("[data-kb-mode]");
    // A row whose raw Keboola name failed `_wouldPassRegisterCheck` renders an
    // editable input instead of a plain label (see `_renderBucketPicker`) —
    // its current value is the name actually being registered. Untouched rows
    // have no such input, so they keep sending the raw Keboola name.
    const nameInput = rowEl.querySelector(".ds-table-name-input");
    const effectiveName = nameInput ? (nameInput.value.trim() || cb.dataset.tableName) : cb.dataset.tableName;
    statusEl.textContent = "registering…";
    statusEl.className = "ds-table-status";
    try {
      const r = await fetch(API_REGISTER_TABLE, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: effectiveName,
          source_type: "keboola",
          bucket: cb.dataset.bucket,
          // BARE in-bucket name — the full `bucket.table` id here doubled
          // the bucket when the sync path re-composed the export id. Always
          // the REAL Keboola table, regardless of a renamed registry `name`.
          source_table: cb.dataset.tableBare || cb.dataset.tableName,
          query_mode: modeSel ? modeSel.value : "materialized",
          connection_id: connId,
          description: `Added via the Data sources wizard (#755)`,
        }),
      });
      if (r.ok || r.status === 201) {
        statusEl.textContent = "✓ registered";
        statusEl.className = "ds-table-status ok";
        cb.disabled = true;
        if (modeSel) modeSel.disabled = true;
        if (nameInput) nameInput.disabled = true;
        okCount++;
        registered.push({
          id: effectiveName.trim().toLowerCase().replace(/ /g, "_"),
          name: effectiveName,
          bucket: cb.dataset.bucket || "",
        });
      } else if (r.status === 409) {
        statusEl.textContent = "already registered";
        statusEl.className = "ds-table-status ok";
        // Already in the registry — still bundleable, so it rides along. Counted
        // apart from okCount so the summary can say so: folded into neither, the
        // toast read "0 table(s) registered." after a wholly successful step.
        alreadyCount++;
        registered.push({
          id: effectiveName.trim().toLowerCase().replace(/ /g, "_"),
          name: effectiveName,
          bucket: cb.dataset.bucket || "",
        });
      } else {
        const body = await r.json().catch(() => ({}));
        statusEl.textContent = "✗ " + (_registerErrorText(body));
        statusEl.className = "ds-table-status fail";
        failCount++;
      }
    } catch (e) {
      statusEl.textContent = "✗ request failed";
      statusEl.className = "ds-table-status fail";
      failCount++;
    }
  }
  if (root) _syncPicker(root);
  // State each outcome that happened, and only those. A step where every table
  // was already in the registry is a success, not a "0 registered" — the server
  // says 409 and the rows say "already registered", so the summary must agree.
  if (failCount === 0) {
    const parts = [];
    if (okCount) parts.push(`${okCount} table(s) registered`);
    if (alreadyCount) parts.push(`${alreadyCount} already registered`);
    showToast(parts.length ? `${parts.join(" · ")}.` : "Nothing to register.", true);
  } else {
    const done = okCount + alreadyCount;
    showToast(`${done} ready, ${failCount} failed. See per-row status.`, false);
  }
  // The whole point of the strip: "No tables yet" must stop being the
  // answer the moment it stops being true.
  await refreshSourcePipelines();
  return { ok: registered, failed: failCount };
}

/* ── Wizard: Add data (connect → choose → bundle → share) ────────────────── */

let _wizardConnId = null;
let _wizardStep = 1;
let _wizardMaxStep = 1;       // furthest step reached — what the strip lets you back into
let _wizardDone = false;      // the receipt is up; the flow is no longer navigable
let _wizardSource = "keboola"; // 'keboola' | 'bigquery' | 'snowflake' | 'csv' | 'jira'
let _bqCredStatus = null;      // cached /api/admin/datasource-secrets read
let _sfCredStatus = null;      // cached Snowflake credential status {password, key}
let _sfConfigLoaded = false;   // cached Snowflake config read for this wizard open
let _dbxCredStatus = null;     // cached Databricks credential status
let _dbxConfigLoaded = false;  // cached Databricks config read for this wizard open
let _wizardTables = [];       // [{id, name, bucket}] registered in step 2
let _wizardBoard = null;      // {trays: [{id, name, tables: [table], existing?: pkgId}], out: [table]}
let _wizardTrayIdSeq = 0;
let _wizardPackages = [];     // [{id, name, count}] created entering step 4
let _wizardShares = null;     // {trayLocalId: [{group_id, name, tier}]}
let _wizardGroups = null;     // cached /api/admin/groups
let _wizardExistPkgs = null;  // cached /api/admin/data-packages, for attach-only trays
let _wizardExistingAdds = []; // [{name, count}] tables attached to EXISTING packages this run

const _WIZ_STEP_PANES = {
  1: "ds-wizard-step-connect",
  2: "ds-wizard-step-tables",
  3: "ds-wizard-step-bundle",
  4: "ds-wizard-step-share",
};
// Footer buttons visible per step (Cancel is always there).
const _WIZ_STEP_BUTTONS = {
  1: ["ds-wizard-connect-btn"],
  2: ["ds-wizard-finish-early-btn", "ds-wizard-register-btn"],
  3: ["ds-wizard-bundle-btn"],
  4: ["ds-wizard-skip-btn", "ds-wizard-share-btn"],
};
const _WIZ_ALL_BUTTONS = [
  "ds-wizard-connect-btn", "ds-wizard-register-btn", "ds-wizard-bundle-btn",
  "ds-wizard-share-btn", "ds-wizard-skip-btn", "ds-wizard-finish-early-btn",
  "ds-wizard-done-btn",
];

function _setWizStep(step) {
  _wizardStep = step;
  // The furthest the flow has ever reached — NOT the current step. Stepping
  // back to Connect must not un-earn Bundle, or "go back and fix one thing"
  // would cost the admin the three screens they had already filled in.
  _wizardMaxStep = Math.max(_wizardMaxStep, step);
  for (const [n, paneId] of Object.entries(_WIZ_STEP_PANES)) {
    document.getElementById(paneId).classList.toggle("active", Number(n) === step);
  }
  document.querySelectorAll("#ds-wizard-overlay .ds-drawer__step").forEach((el) => {
    const n = Number(el.dataset.wstep);
    el.classList.toggle("is-now", n === step);
    el.classList.toggle("is-done", n <= _wizardMaxStep && n !== step);
    // Reachable = already visited. The last screen is a receipt for work that
    // has landed, so from there the strip stops being a way back.
    el.disabled = _wizardDone || n > _wizardMaxStep;
    el.setAttribute("aria-current", n === step ? "step" : "false");
  });
  for (const id of _WIZ_ALL_BUTTONS) {
    document.getElementById(id).style.display =
      (_WIZ_STEP_BUTTONS[step] || []).includes(id) ? "" : "none";
  }
  // The board is seeded BEFORE the step switches (see the register handler),
  // and the CTA count only recomputes on a live step-3 render — so entering
  // the step is the moment the label must catch up.
  if (step === 3) _updateBundleCta();
  // Each step starts at its own top. The drawer's BODY is the scroll
  // container (its head, steps strip and actions row never scroll), so
  // this is the element to reset — the panel itself does not scroll.
  const body = document.querySelector("#ds-wizard-overlay .ds-drawer__body");
  if (body) body.scrollTop = 0;
}

function _setWizSource(src) {
  _wizardSource = src;
  document.querySelectorAll("[data-wsrc]").forEach((o) => o.classList.toggle("on", o.dataset.wsrc === src));
  document.querySelectorAll("[data-wsrcform]").forEach((f) => { f.hidden = f.dataset.wsrcform !== src; });
  const connectBtn = document.getElementById("ds-wizard-connect-btn");
  if (src === "keboola") {
    connectBtn.style.display = _wizardStep === 1 ? "" : "none";
    connectBtn.textContent = "Connect & validate";
  } else if (src === "bigquery") {
    connectBtn.style.display = _wizardStep === 1 ? "" : "none";
    connectBtn.textContent = "Continue";
    _loadBqCredStatus();
  } else if (src === "snowflake") {
    connectBtn.style.display = _wizardStep === 1 ? "" : "none";
    connectBtn.textContent = "Save & continue";
    _loadSfConfigAndStatus();
    _updateSfAuthUI();
  } else if (src === "databricks") {
    connectBtn.style.display = _wizardStep === 1 ? "" : "none";
    connectBtn.textContent = "Save & continue";
    _loadDbxConfigAndStatus();
  } else {
    // csv / jira are guidance, not forms — nothing to submit.
    connectBtn.style.display = "none";
  }
  document.getElementById("ds-wizard-error").style.display = "none";
  // …and the Snowflake connection-error lock with it. That lock disables the
  // two SHARED step-2 footer buttons ("Continue with selected tables" /
  // "Register only & finish"), which the Keboola, BigQuery and Databricks
  // step-2 panes reuse — but only `_loadSfCatalog` ever cleared it, and only
  // on the Snowflake path. So one failed Snowflake listing left those two
  // buttons dead for EVERY source, across reopens, until a full page reload:
  // registration became impossible. Pointing the wizard at a source is the
  // moment that lock is void, and it is the one funnel every entry goes
  // through — `openWizard()` ends here, the connector picker calls here, and
  // the steps-strip's return to step 1 calls here. Every caller is a step-1
  // context (the picker lives there; "Fix connection" runs `_setWizStep(1)`
  // first), so this can never clear a Snowflake error that is still on
  // screen — and re-entering the Snowflake step re-runs the listing, which
  // re-locks if the connection is still broken.
  _setSfConnErrorState(false);
}

async function _loadBqCredStatus() {
  const host = document.getElementById("ds-bqcred");
  if (_bqCredStatus) return; // already rendered this open
  try {
    const r = await fetch("/api/admin/datasource-secrets", { credentials: "include" });
    const data = r.ok ? await r.json() : { secrets: [] };
    const row = (data.secrets || []).find((s) => s.name === "BIGQUERY_SERVICE_ACCOUNT_JSON");
    _bqCredStatus = row || { source: "unset", has_value: false };
  } catch (e) {
    _bqCredStatus = { source: "unset", has_value: false };
  }
  host.innerHTML = _bqCredStatus.has_value
    ? `<span class="ds-badge ${_bqCredStatus.source === "env" ? "badge-env" : "badge-vault"}">service account · ${_esc(_bqCredStatus.source)}</span>`
    : `<span class="ds-badge badge-unset">not set</span>
       <span style="font-size:12px; color:var(--ds-accent-warn-ink);">Set <code>BIGQUERY_SERVICE_ACCOUNT_JSON</code> in
       <a href="/admin/datasource-credentials">Instance secrets</a> first — registering works, but nothing can query until it's there.</span>`;
}

function _updateSfAuthUI() {
  const sel = document.getElementById("ds-sf-auth-type");
  const authType = sel.value;
  document.getElementById("ds-sf-auth-password").hidden = authType !== "password";
  document.getElementById("ds-sf-auth-key").hidden = authType !== "key_pair";
  _syncDropdownFromSelect(sel);
}

/* D2.3: the Snowflake connection (coordinates + auth_type) lives on this
   row's `config`, not the `data_source.snowflake` server-config yaml overlay
   — a row is read live by every process, so there is no cross-process
   staleness left to warn the operator about, and the restart banner this
   comment used to describe is gone with it. `_sfConnId` is null until the
   wizard has loaded (or created) the instance's one snowflake connection;
   the wizard never creates a second one (multi-connection-per-type is out
   of scope for this slice — see docs/superpowers/plans/
   2026-08-26-derived-connection-model.md). */
let _sfConnId = null;

/* The connection's own vault slot (`kind="storage"`) holds ONE secret at a
   time — whichever auth_type is active. Both the password and key-pair
   badges below read the SAME status: there is nothing to tell apart, unlike
   the old per-env-var-name status the yaml-overlay path used to fetch. */
async function _loadSfConfigAndStatus() {
  const hostPassword = document.getElementById("ds-sfcred-password");
  const hostKey = document.getElementById("ds-sfcred-key");
  if (!_sfConfigLoaded) {
    try {
      const r = await fetch(`${API_CONNECTIONS}?source_type=snowflake`, { credentials: "include" });
      const rows = r.ok ? await r.json() : [];
      const row = rows[0] || null;
      _sfConnId = row ? row.id : null;
      const sf = (row && row.config) || {};
      document.getElementById("ds-sf-account").value = sf.account || "";
      document.getElementById("ds-sf-user").value = sf.user || "";
      document.getElementById("ds-sf-database").value = sf.database || "";
      document.getElementById("ds-sf-warehouse").value = sf.warehouse || "";
      document.getElementById("ds-sf-role").value = sf.role || "";
      document.getElementById("ds-sf-auth-type").value = sf.auth_type || "password";
      _updateSfAuthUI();
      const hasSecret = !!(row && row.has_secret);
      _sfCredStatus = {
        password: { source: hasSecret ? "vault" : "unset", has_value: hasSecret },
        key: { source: hasSecret ? "vault" : "unset", has_value: hasSecret },
      };
      _sfConfigLoaded = true;
    } catch (e) {
      // leave inputs empty and let the admin fill them in
    }
  }
  _renderSfCredStatus(hostPassword, "password");
  _renderSfCredStatus(hostKey, "key");
}

function _renderSfCredStatus(host, kind) {
  if (kind === undefined) kind = "password";
  const inputId = kind === "password" ? "ds-sf-password" : "ds-sf-private-key";
  if (!host) host = document.getElementById(kind === "password" ? "ds-sfcred-password" : "ds-sfcred-key");
  const status = (_sfCredStatus && _sfCredStatus[kind]) || { source: "unset", has_value: false };
  const hasInput = document.getElementById(inputId).value.trim().length > 0;
  const label = kind === "password" ? "password" : "key-pair";
  if (status.has_value || hasInput) {
    const source = hasInput ? "ready to save" : status.source;
    const cls = hasInput ? "badge-vault" : (status.source === "env" ? "badge-env" : "badge-vault");
    host.innerHTML = `<span class="ds-badge ${cls}">${label} · ${_esc(source)}</span>`;
  } else {
    host.innerHTML = `<span class="ds-badge badge-unset">not set</span>
       <span style="font-size:12px; color:var(--ds-accent-warn-ink);">Save a ${label} credential here first — registering works, but nothing can query until it's stored.</span>`;
  }
}

/* D2.3: the passphrase (key-pair auth only) has no slot on the connection
   row's own vault — that slot holds ONE secret (the password OR the private
   key, whichever `auth_type` is active) — so it keeps going through the
   generic named-secret vault under its well-known default name, the same
   fallback `connectors.snowflake.settings._resolve_secret` reads when the
   row's `config` does not carry a custom `private_key_passphrase_env`. */
const _SF_PASSPHRASE_ENV_DEFAULT = "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE";

/* Shared by the Snowflake and Databricks "also sync semantic views" opt-ins
   — the same shape as Keboola's own opt-in above, generalized to any
   connector with a registered `connection`-kind semantic adapter
   (src/semantic/adapters/__init__.py). Reuses the existing
   /api/admin/semantic-sources* API — no new endpoint.

   Idempotent: reuses an existing `connection` source for this adapter
   instead of creating a duplicate one every time the wizard runs (e.g. an
   admin re-opening it to fix a credential). Returns a short status string;
   never throws — this must never block the wizard's own connection flow.

   `connectionId` is the row this opt-in was checked on, written to the
   source's `config.connection_id`. It is NOT a credential — every one of
   these adapters resolves host/token itself from the connection, which is
   why an empty config syncs at all — it is the link the cross-domain
   coverage report scores against: a source recording no connection is
   credited to nobody (src/semantic/coverage.py::_native_semantic_status),
   so without it a working semantic layer still reports as missing. */
async function _connectSemanticSource(adapter, label, connectionId) {
  try {
    const listResp = await fetch("/api/admin/semantic-sources", { credentials: "include" });
    if (!listResp.ok) throw new Error(`HTTP ${listResp.status}`);
    const existing = (await listResp.json()).find((s) => s.kind === "connection" && s.adapter === adapter);

    let sourceId = existing && existing.id;
    if (!sourceId) {
      const config = connectionId ? { connection_id: connectionId } : {};
      const createResp = await fetch("/api/admin/semantic-sources", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "connection", name: label, adapter, config, enabled: true }),
      });
      if (!createResp.ok) {
        const detail = await createResp.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${createResp.status}`);
      }
      sourceId = (await createResp.json()).id;
    } else if (connectionId && (existing.config || {}).connection_id !== connectionId) {
      /* A source registered before the wizard wrote the link — or one left
         pointing at a connection that has since been re-created — repaired
         in place rather than left syncing into a coverage row that reports
         it missing. Safe to re-point: this wizard keeps one connection per
         source_type (see the `_sfConnId` note above), so there is no second
         connection of this type whose link this could be stealing. Scope
         keys already on the config (`catalogs`, `database`, …) are kept. */
      const linkResp = await fetch(`/api/admin/semantic-sources/${encodeURIComponent(sourceId)}`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: { ...(existing.config || {}), connection_id: connectionId } }),
      });
      if (!linkResp.ok) {
        const detail = await linkResp.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${linkResp.status}`);
      }
    }

    const syncResp = await fetch(`/api/admin/semantic-sources/${encodeURIComponent(sourceId)}/sync`, {
      method: "POST", credentials: "include",
    });
    if (!syncResp.ok) {
      const detail = await syncResp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${syncResp.status}`);
    }
    const report = await syncResp.json();
    const invalidCount = (report.invalid || []).length;
    if (invalidCount) {
      return { ok: false, message: `Semantic layer: synced with ${invalidCount} document(s) skipped — see Semantic sources.` };
    }
    return { ok: true, message: `Semantic layer enabled — ${report.models_written ?? 0} model(s) imported.` };
  } catch (e) {
    return { ok: false, message: `Semantic layer sync failed (${e.message || e}) — set it up later on the Semantic sources page.` };
  }
}

async function _saveSnowflakeAndContinue() {
  const err = document.getElementById("ds-wizard-error");
  const account = document.getElementById("ds-sf-account").value.trim();
  const user = document.getElementById("ds-sf-user").value.trim();
  const database = document.getElementById("ds-sf-database").value.trim();
  const warehouse = document.getElementById("ds-sf-warehouse").value.trim();
  const role = document.getElementById("ds-sf-role").value.trim();
  const authType = document.getElementById("ds-sf-auth-type").value;
  const password = document.getElementById("ds-sf-password").value;
  const privateKey = document.getElementById("ds-sf-private-key").value.trim();
  const passphrase = document.getElementById("ds-sf-key-passphrase").value;
  if (!account || !user || !database || !warehouse) {
    err.textContent = "Account, user, database and warehouse are required.";
    err.style.display = "block";
    return;
  }
  if (authType === "password") {
    if ((!_sfCredStatus || !_sfCredStatus.password || !_sfCredStatus.password.has_value) && !password) {
      err.textContent = "Snowflake password is required.";
      err.style.display = "block";
      return;
    }
  } else {
    if ((!_sfCredStatus || !_sfCredStatus.key || !_sfCredStatus.key.has_value) && !privateKey) {
      err.textContent = "Snowflake private key is required.";
      err.style.display = "block";
      return;
    }
  }
  const config = { account, user, database, warehouse, role, auth_type: authType };
  try {
    /* Every leaf this wizard sends is a connection-identity leaf, so on an
       instance whose Snowflake connection already has registrations the
       server refuses with 409 until the operator confirms. saveConnectionConfig
       shows the blast radius and re-sends on confirm — without it this form
       would be a dead end for credential rotation on exactly the instances
       that have data. Update the existing row if the wizard found one (one
       connection per source_type in this slice — see the module-level note
       on `_sfConnId`); otherwise create it. */
    const saved = _sfConnId
      ? await window.saveConnectionConfig(`${API_CONNECTIONS}/${_sfConnId}`, { config }, { method: "PUT" })
      : await window.saveConnectionConfig(API_CONNECTIONS, {
          name: "snowflake", source_type: "snowflake", config, is_default: true,
        });
    if (saved.cancelled) return;
    if (!saved.ok) throw new Error(window.apiDetailText(saved.data.detail, `connection save failed (${saved.status})`));
    _sfConnId = saved.data.id;
    if (!_sfCredStatus) _sfCredStatus = { password: { source: "unset", has_value: false }, key: { source: "unset", has_value: false } };
    if (authType === "password" && password) {
      const rp = await fetch(`${API_CONNECTIONS}/${_sfConnId}/secret`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: password, kind: "storage" }),
      });
      if (!rp.ok) throw new Error("password secret save failed");
      _sfCredStatus.password = { source: "vault", has_value: true };
      // Clear before redrawing: the badge reads "ready to save" off a
      // non-empty input, so rendering first shows a stored credential as
      // still pending.
      document.getElementById("ds-sf-password").value = "";
      _renderSfCredStatus(null, "password");
    }
    if (authType === "key_pair" && privateKey) {
      const rk = await fetch(`${API_CONNECTIONS}/${_sfConnId}/secret`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: privateKey, kind: "storage" }),
      });
      if (!rk.ok) throw new Error("private key secret save failed");
      _sfCredStatus.key = { source: "vault", has_value: true };
      document.getElementById("ds-sf-private-key").value = "";
      _renderSfCredStatus(null, "key");
    }
    if (authType === "key_pair" && passphrase) {
      const rpp = await fetch(`/api/admin/datasource-secrets/${encodeURIComponent(_SF_PASSPHRASE_ENV_DEFAULT)}`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: passphrase }),
      });
      if (!rpp.ok) throw new Error("passphrase secret save failed");
      document.getElementById("ds-sf-key-passphrase").value = "";
    }
    _sfConfigLoaded = true;
    _setWizStep(2);
    _renderSfRowsEditor();
    // Non-fatal, after the step-2 banner has its own text — appended, not
    // awaited-before-navigation, since a stalled/slow sync must never hold
    // up table browsing (Devin Review precedent on the identical Keboola
    // opt-in above).
    if (document.getElementById("ds-new-semantic-snowflake").checked) {
      _connectSemanticSource("snowflake_semantic", "Snowflake semantics", _sfConnId).then((result) => {
        const banner = document.getElementById("ds-wizard-connected-banner");
        if (banner) {
          banner.innerHTML += ` <span style="${result.ok ? "" : "color:var(--ds-accent-warn-ink)"}">${_esc(result.message)}</span>`;
        }
      });
    }
  } catch (e) {
    err.textContent = "Could not save Snowflake settings: " + (e.message || e);
    err.style.display = "block";
  }
}

/* D2.3: the Databricks connection (host/warehouse_id/catalog) lives on this
   row's `config`, not the `data_source.databricks` server-config yaml
   overlay — a row is read live by every process, so a save no longer needs
   the "restart the instance" notice this block used to carry, and the
   wizard is a straight line to /admin/tables again (see `_saveDatabricksAndContinue`).
   `_dbxConnId` is null until the wizard has loaded (or created) the
   instance's one databricks connection — see the identical note on
   `_sfConnId` above. */
let _dbxConnId = null;

async function _loadDbxConfigAndStatus() {
  const hostCred = document.getElementById("ds-dbxcred");
  if (!_dbxConfigLoaded) {
    try {
      const r = await fetch(`${API_CONNECTIONS}?source_type=databricks`, { credentials: "include" });
      const rows = r.ok ? await r.json() : [];
      const row = rows[0] || null;
      _dbxConnId = row ? row.id : null;
      const dbx = (row && row.config) || {};
      document.getElementById("ds-dbx-host").value = dbx.host || "";
      document.getElementById("ds-dbx-warehouse").value = dbx.warehouse_id || "";
      document.getElementById("ds-dbx-catalog").value = dbx.catalog || "";
      _dbxCredStatus = { source: row && row.has_secret ? "vault" : "unset", has_value: !!(row && row.has_secret) };
      _dbxConfigLoaded = true;
    } catch (e) {
      // leave inputs empty and let the admin fill them in
    }
  }
  _renderDbxCredStatus(hostCred);
}

function _renderDbxCredStatus(host) {
  if (!host) host = document.getElementById("ds-dbxcred");
  const status = _dbxCredStatus || { source: "unset", has_value: false };
  const hasInput = document.getElementById("ds-dbx-token").value.trim().length > 0;
  if (status.has_value || hasInput) {
    const source = hasInput ? "ready to save" : status.source;
    host.innerHTML = `<span class="ds-badge badge-vault">token · ${_esc(source)}</span>`;
  } else {
    host.innerHTML = `<span class="ds-badge badge-unset">not set</span>
       <span style="font-size:12px; color:var(--ds-accent-warn-ink);">Save a token here first — registering tables works, but nothing can query until it's stored.</span>`;
  }
}

async function _saveDatabricksAndContinue() {
  const err = document.getElementById("ds-wizard-error");
  err.style.display = "none";
  let host = document.getElementById("ds-dbx-host").value.trim().replace(/\/+$/, "");
  const warehouse = document.getElementById("ds-dbx-warehouse").value.trim();
  const catalog = document.getElementById("ds-dbx-catalog").value.trim();
  const token = document.getElementById("ds-dbx-token").value;
  if (!host || !warehouse || !catalog) {
    err.textContent = "Host, warehouse ID and catalog are required.";
    err.style.display = "block";
    return;
  }
  // Mirror the backend `validate_workspace_host`: https only, no path, no userinfo.
  if (host.toLowerCase().startsWith("http://")) {
    err.textContent = "Databricks host must use https://, not http://.";
    err.style.display = "block";
    return;
  }
  if (!host.toLowerCase().startsWith("https://")) {
    host = `https://${host}`;
  }
  try {
    const u = new URL(host);
    if (!u.hostname) {
      throw new Error("Databricks host is missing a hostname.");
    }
    if (u.username || u.password) {
      throw new Error("Databricks host must not contain userinfo (user:pass@).");
    }
    if (u.pathname && u.pathname !== "/") {
      throw new Error("Databricks host must be a bare workspace URL with no path.");
    }
    if (u.search) {
      throw new Error("Databricks host must be a bare workspace URL. Remove the query string, for example the trailing ?o=... copied from the browser.");
    }
    if (u.hash) {
      throw new Error("Databricks host must be a bare workspace URL with no #fragment.");
    }
    // Reconstruct bare https://hostname[:port]
    host = `https://${u.hostname}${u.port ? ":" + u.port : ""}`;
  } catch (e) {
    err.textContent = "Databricks host is not a valid URL: " + (e.message || e);
    err.style.display = "block";
    return;
  }
  if ((!_dbxCredStatus || !_dbxCredStatus.has_value) && !token) {
    err.textContent = "Databricks token is required.";
    err.style.display = "block";
    return;
  }
  try {
    /* Same as the Snowflake branch above: host/warehouse_id/catalog are all
       connection identity, so a save on an instance whose Databricks
       connection already has registrations needs the confirmation this
       helper carries. Update the existing row if the wizard found one (one
       connection per source_type in this slice); otherwise create it. */
    const config = { host, warehouse_id: warehouse, catalog };
    const saved = _dbxConnId
      ? await window.saveConnectionConfig(`${API_CONNECTIONS}/${_dbxConnId}`, { config }, { method: "PUT" })
      : await window.saveConnectionConfig(API_CONNECTIONS, {
          name: "databricks", source_type: "databricks", config, is_default: true,
        });
    if (saved.cancelled) return;
    if (!saved.ok) {
      throw new Error(window.apiDetailText(saved.data.detail, `connection save failed (${saved.status})`));
    }
    _dbxConnId = saved.data.id;
    if (token) {
      const rt = await fetch(`${API_CONNECTIONS}/${_dbxConnId}/secret`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: token, kind: "storage" }),
      });
      if (!rt.ok) {
        const tBody = await rt.json().catch(() => ({}));
        throw new Error(tBody.detail || tBody.message || `token secret save failed (${rt.status})`);
      }
      document.getElementById("ds-dbx-token").value = "";
      _dbxCredStatus = { source: "vault", has_value: true };
      _renderDbxCredStatus();
    }
    _dbxConfigLoaded = true;
    // Unlike Snowflake/Keboola, this flow closes the wizard and navigates
    // away immediately — there is no banner left to append to, and a
    // fire-and-forget call here would be cancelled by the navigation before
    // it completes. Awaited so it actually runs; still non-fatal for the
    // connection itself (no `throw` — host/warehouse/token are saved either
    // way), but NOT silent: a failure here used to be discarded and the
    // navigation ran regardless, so a checked box, no error and no semantic
    // layer was an indistinguishable outcome from success. This is the only
    // moment the message has somewhere to land, so on failure the wizard
    // stays put and says so, with the onward link the navigation would have
    // taken.
    if (document.getElementById("ds-new-semantic-databricks").checked) {
      const result = await _connectSemanticSource("databricks_metric_views", "Databricks semantics", _dbxConnId);
      if (!result.ok) {
        err.innerHTML = `<span style="color:var(--ds-accent-warn-ink)">Databricks connection saved. ${_esc(result.message)}</span>` +
          ` <a href="/admin/tables">Continue to Tables</a>`;
        err.style.display = "block";
        return;
      }
    }
    closeWizard();
    window.location.href = "/admin/tables";
  } catch (e) {
    err.textContent = "Could not save Databricks settings: " + (e.message || e);
    err.style.display = "block";
  }
}

/* `connector` preselects step 1's picker. A card's "Add tables…" knows which
   connector it is, so landing the admin on Keboola's form when they pressed
   it on the BigQuery card would make them undo a choice they already made. */
function openWizard(connector) {
  _wizardConnId = null;
  _wizardTables = [];
  _wizardBoard = null;
  _wizardPackages = [];
  _wizardShares = null;
  _wizardExistPkgs = null;
  _wizardExistingAdds = [];
  _bqCredStatus = null;
  _sfCredStatus = null;
  _sfConnId = null;
  _sfConfigLoaded = false;
  _dbxCredStatus = null;
  _dbxConnId = null;
  _dbxConfigLoaded = false;
  _wizardMaxStep = 1;
  _wizardDone = false;
  document.getElementById("ds-new-name").value = "";
  document.getElementById("ds-new-stack").value = "";
  document.getElementById("ds-new-token").value = "";
  document.getElementById("ds-new-semantic").checked = false;
  document.getElementById("ds-new-master").value = "";
  document.getElementById("ds-new-semantic-snowflake").checked = false;
  document.getElementById("ds-new-semantic-databricks").checked = false;
  document.getElementById("ds-sf-account").value = "";
  document.getElementById("ds-sf-user").value = "";
  document.getElementById("ds-sf-auth-type").value = "password";
  _updateSfAuthUI();
  document.getElementById("ds-sf-password").value = "";
  document.getElementById("ds-sf-private-key").value = "";
  document.getElementById("ds-sf-key-passphrase").value = "";
  document.getElementById("ds-sf-database").value = "";
  document.getElementById("ds-sf-warehouse").value = "";
  document.getElementById("ds-sf-role").value = "";
  document.querySelector(".ds-wsl-opt__field").hidden = true;
  document.getElementById("ds-dbx-host").value = "";
  document.getElementById("ds-dbx-warehouse").value = "";
  document.getElementById("ds-dbx-catalog").value = "";
  document.getElementById("ds-dbx-token").value = "";
  document.getElementById("ds-dbxcred").innerHTML = "";
  document.getElementById("ds-wizard-error").style.display = "none";
  const share = document.getElementById("ds-wizard-step-share");
  share.querySelector(".ds-wshare").style.display = "";
  share.querySelector(".ds-wdone").style.display = "none";
  // Existing-source shortcut: offer already-connected projects for "add
  // more tables from ..." — the wizard is not only for new connections.
  // STORED connections only: the derived cards (BigQuery, Jira, files) have
  // no connection to browse, and offering them here would open a table
  // picker against an id the API has never heard of.
  const existing = document.getElementById("ds-wexisting");
  const orRule = document.getElementById("ds-wor");
  // A connection can only be BROWSED if it holds a credential. This filter
  // checked `source_type` and nothing else, so a connection whose token
  // failed validation was offered under "Keboola is already connected — no
  // token to paste again", and Choose tables then dead-ended on
  // "no token available (vault empty, token_env unset)" with a green tick on
  // the step behind it. Same predicate the source card and the setup
  // checklist use, so the three cannot disagree about what "connected" means.
  const _hasCredential = (c) => c.has_secret === true || !!(c.token_env || "").trim();
  const keboolaConns = _connections.filter((c) => !c.derived && c.source_type === "keboola");
  const browsable = keboolaConns.filter(_hasCredential);
  const needsToken = keboolaConns.filter((c) => !_hasCredential(c));
  if (browsable.length) {
    // The option carries what the project already holds. "Keboola AI" alone
    // does not say whether picking it is worth anything; "· 3 tables" does,
    // and it is the number the card behind the drawer is showing anyway.
    const wexistingSel = document.getElementById("ds-wexisting-select");
    wexistingSel.innerHTML = browsable
      .map((c) => {
        const n = ((SOURCE_PIPELINES[c.id] || {}).tables || {}).count || 0;
        const held = n ? ` · ${n} table${n === 1 ? "" : "s"}` : " · no tables yet";
        return `<option value="${_esc(c.id)}">${_esc(c.name || c.id)}${held}</option>`;
      })
      .join("");
    _syncDropdownRebuild(wexistingSel);
    const alsoBroken = needsToken.length
      ? ` ${needsToken.length} other project${needsToken.length === 1 ? "" : "s"} still need${needsToken.length === 1 ? "s" : ""} a token.`
      : "";
    document.getElementById("ds-wexisting-copy").textContent = (browsable.length === 1
      ? "Go straight to its tables — no URL, no token to paste again."
      : `Pick one of your ${browsable.length} connected projects and go straight to its tables.`) + alsoBroken;
    existing.classList.remove("is-blocked");
    document.getElementById("ds-wexisting-title").textContent = "Keboola is already connected.";
    document.getElementById("ds-wexisting-btn").hidden = false;
    existing.hidden = false;
    orRule.hidden = false;
  } else if (needsToken.length) {
    // Connections exist but none can be read. Saying nothing here sent the
    // admin to "connect a new project" while the half-finished one sat on
    // the page behind the drawer; claiming they were connected sent them
    // into the dead end. Name the state and where it is fixed.
    const one = needsToken.length === 1;
    document.getElementById("ds-wexisting-copy").textContent = one
      ? `“${needsToken[0].name || needsToken[0].id}” is registered but has no token stored, so its tables cannot be listed. `
        + `Add a token on its card below, or connect a new project.`
      : `${needsToken.length} projects are registered but have no token stored, so their tables cannot be listed. `
        + `Add a token on their cards below, or connect a new project.`;
    document.getElementById("ds-wexisting-title").textContent = one
      ? "A Keboola project is registered, but not usable yet."
      // Deliberately does not name the connector in the plural: that phrasing
      // is banned outright by TestSourcesIsEveryConnector, which reads the
      // whole served body (comments included), because it re-plants the
      // single-connector framing this page was rebuilt to drop.
      : "None of the registered projects are usable yet.";
    document.getElementById("ds-wexisting-select").innerHTML = "";
    _syncDropdownRebuild(document.getElementById("ds-wexisting-select"));
    // No "Choose tables" — that button led straight to
    // "Failed to list tables: no token available".
    document.getElementById("ds-wexisting-btn").hidden = true;
    existing.hidden = false;
    existing.classList.add("is-blocked");
    orRule.hidden = false;
  } else {
    existing.hidden = true;
    orRule.hidden = true;
  }
  _setWizStep(1);
  _setWizSource(connector ? _connector(connector).wizard : "keboola");
  document.getElementById("ds-wizard-overlay").classList.add("show");
  // The page behind a drawer must not scroll under it — a wheel over the
  // backdrop otherwise moves the sources list, which reads as the drawer
  // drifting.
  document.body.style.overflow = "hidden";
  // preventScroll: the drawer body is its own scroll container, and a plain
  // focus() scrolls the name field into view — pushing the steps strip and
  // the connector picker out of the top of the drawer before anyone sees them.
  document.getElementById("ds-new-name").focus({ preventScroll: true });
  document.querySelector("#ds-wizard-overlay .ds-drawer__body").scrollTop = 0;
}

async function closeWizard() {
  document.getElementById("ds-wizard-overlay").classList.remove("show");
  document.body.style.overflow = "";
  // The wizard is where a source's whole pipeline gets built — connection,
  // tables, packages, grants — and every card behind it was drawn before any
  // of that happened. On EVERY exit, not only the Finish button: a connection
  // created in step 1 and then dismissed with X/Escape is still a connection,
  // and it has no card at all until the list is re-read.
  await refreshSourcePipelines();
  await loadConnections();
}

// A connector failure reaches the admin as whatever the upstream API said —
// "storage_api_error: GET https://…/tokens/verify -> HTTP 401: {'error':
// 'Invalid access token', 'exceptionId': 'kbc-us-east-1-…'}". That is the
// single most likely first-run outcome, and it is presented as a stack trace:
// the one actionable fact (the token is wrong) is never stated, and a vendor
// exception hash reads as "something is deeply broken" rather than "retype
// the token". Name the cause; keep the raw text for whoever needs it.
function _connErrorCopy(detail) {
  const d = String(detail == null ? "" : detail);
  let plain;
  if (/tokenInvalid|Invalid access token|401|unauthor/i.test(d)) {
    plain = "That token was rejected. Check you copied a Storage API token for the right project — not an expired one, and not a token for a different stack.";
  } else if (/403|forbidden|permission/i.test(d)) {
    plain = "That token was accepted but is not allowed to read this project. It likely lacks the permissions Agnes needs, or belongs to another project.";
  } else if (/ENOTFOUND|getaddrinfo|Name or service not known/i.test(d)) {
    plain = "That host could not be found. Check the connection URL.";
  } else if (/timeout|timed out|ETIMEDOUT/i.test(d)) {
    plain = "The connection timed out. The host may be unreachable from this instance.";
  } else if (/50\d|internal server error|bad gateway/i.test(d)) {
    plain = "The upstream service returned an error. This is usually temporary — try again shortly.";
  } else {
    plain = "The connection could not be verified.";
  }
  const raw = d.trim()
    ? '<details class="ds-errdetail"><summary>Technical details</summary><pre>' + _esc(d) + '</pre></details>'
    : "";
  return { plain, raw };
}

async function connectAndValidate() {
  const nameInput = document.getElementById("ds-new-name");
  const name = nameInput.value.trim();
  const stack = document.getElementById("ds-new-stack").value.trim().replace(/\/$/, "");
  const token = document.getElementById("ds-new-token").value.trim();
  const errEl = document.getElementById("ds-wizard-error");
  const btn = document.getElementById("ds-wizard-connect-btn");
  errEl.style.display = "none";

  // `https://` specifically — the message already said so, but the check
  // accepted `http://` and the server rejects it, so the admin got a server
  // error for input the form had told them was fine. (Devin Review.)
  if (!stack || !stack.toLowerCase().startsWith("https://")) {
    errEl.textContent = "Enter a valid connection URL (starts with https://).";
    errEl.style.display = "block";
    return;
  }
  if (!token) {
    errEl.textContent = "Paste a storage API token to validate the connection.";
    errEl.style.display = "block";
    return;
  }

  btn.disabled = true;
  try {
    // 1. Create the connection (blank name gets a provisional placeholder;
    //    renamed to the detected project name below on success).
    const provisionalName = name || `Untitled project ${Date.now()}`;
    // A retry reuses the connection this wizard already created. Every step
    // after creation can fail — a mistyped token is the common one — and the
    // wizard returns with the row already saved, so re-running step 1 minted
    // another "Untitled project" per attempt and left the admin to clean them
    // up. Reusing it also means the eventual success lands on the row the
    // admin has been looking at. (Devin Review.)
    let connId = null;
    if (_wizardConnId) {
      const still = await fetch(`${API_CONNECTIONS}/${_wizardConnId}`, { credentials: "include" });
      if (still.ok) {
        connId = _wizardConnId;
        // Apply whatever the admin just corrected. Reuse without this made a
        // mistyped URL unfixable: the wizard told them to correct it and
        // retry, then talked to the original address every time, failing
        // identically with nothing to say the new value was ignored.
        // (Devin Review.)
        const patch = await fetch(`${API_CONNECTIONS}/${connId}`, {
          method: "PUT", credentials: "include",
          headers: { "Content-Type": "application/json" },
          // The NAME too: the admin may have corrected it on the retry, and
          // sending only the URL threw that away while the success banner went
          // on to claim the new name was used. (Devin Review.)
          body: JSON.stringify(name ? { name, config: { stack_url: stack } } : { config: { stack_url: stack } }),
        });
        if (!patch.ok) {
          const pb = await patch.json().catch(() => ({}));
          const pd = pb.detail;
          errEl.textContent = "Failed to update the connection URL: " +
            ((pd && typeof pd === "object" ? (pd.message || pd.error) : pd) || `HTTP ${patch.status}`);
          errEl.style.display = "block";
          return;
        }
      } else {
        _wizardConnId = null; // deleted underneath us — re-create below
      }
    }
    if (!connId) {
    const createResp = await fetch(API_CONNECTIONS, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: provisionalName, source_type: "keboola", config: { stack_url: stack } }),
    });
    const createBody = await createResp.json().catch(() => ({}));
    if (!createResp.ok) {
      errEl.textContent = createResp.status === 409
        ? "A connection with this name already exists — pick a different name."
        : "Failed to create connection: " + (createBody.detail || "unknown error");
      errEl.style.display = "block";
      return;
    }
    connId = createBody.id;
    _wizardConnId = connId;
    }

    // 2. Store the token.
    const secretResp = await fetch(`${API_CONNECTIONS}/${connId}/secret`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: token }),
    });
    if (!secretResp.ok && secretResp.status !== 204) {
      const sb = await secretResp.json().catch(() => ({}));
      const ec = _connErrorCopy(sb.detail || "");
      errEl.innerHTML = ec.plain +
        ` Retry above — this connection is reused, not duplicated — or ` +
        `<a href="#" onclick="deleteConn('${connId}'); closeWizard(); return false;">discard it</a>.` +
        ec.raw;
      errEl.style.display = "block";
      return;
    }

    // 3. Validate connectivity.
    const testResp = await fetch(`${API_CONNECTIONS}/${connId}/test`, { method: "POST", credentials: "include" });
    const testData = await testResp.json().catch(() => ({}));
    if (!testResp.ok || !testData.ok) {
      const tc = _connErrorCopy(testData.error || testData.detail || "");
      errEl.innerHTML = tc.plain +
        ` The connection was saved — fix the URL or token above and retry, or ` +
        `<a href="#" onclick="deleteConn('${connId}'); closeWizard(); return false;">discard it</a>.` +
        tc.raw;
      errEl.style.display = "block";
      return;
    }

    // 4. Adopt the detected project name — automatically if the admin left
    //    the name field blank, or as a one-click suggestion otherwise.
    const projectName = testData.project_name || "";
    const banner = document.getElementById("ds-wizard-connected-banner");
    if (projectName && !name) {
      await fetch(`${API_CONNECTIONS}/${connId}`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName }),
      });
      banner.innerHTML = `✓ Connected — named "${_esc(projectName)}".`;
    } else if (projectName && projectName !== name) {
      banner.innerHTML = `✓ Connected as "${_esc(name)}".` +
        `<span class="rename-hint">Keboola reports this project as "${_esc(projectName)}". ` +
        `<button type="button" onclick="_adoptDetectedName('${connId}', '${_esc(projectName).replace(/'/g, "&#39;")}')">Use this name instead</button></span>`;
    } else {
      banner.innerHTML = `✓ Connected — ${_esc(name || projectName)}.`;
    }

    // 5. Semantic-layer opt-in: store the owner token in its own vault slot.
    //    NON-FATAL — the connection and tables are already good, and the
    //    Data sources page's missing/mismatched-token warnings catch a
    //    failure here later; the banner says what happened either way.
    if (document.getElementById("ds-new-semantic").checked) {
      const masterToken = document.getElementById("ds-new-master").value.trim();
      if (masterToken) {
        const mResp = await fetch(`${API_CONNECTIONS}/${connId}/secret`, {
          method: "PUT", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: masterToken, kind: "master" }),
        });
        if (mResp.ok || mResp.status === 204) {
          banner.innerHTML += ` Semantic layer enabled — metrics & glossary will sync.`;
        } else {
          const mb = await mResp.json().catch(() => ({}));
          banner.innerHTML += ` <span style="color:var(--ds-accent-warn-ink)">Semantic-layer token was refused (` +
            _esc(detailMessage(mb, `HTTP ${mResp.status}`)) +
            `) — tables still work; set it later on this connection's card.</span>`;
        }
      } else {
        banner.innerHTML += ` <span style="color:var(--ds-accent-warn-ink)">No owner token pasted — semantic layer skipped; set it later on this connection's card.</span>`;
      }
    }

    // 6. Move to step 2 and load buckets/tables.
    _setWizStep(2);
    await _loadWizardTables(connId);
  } finally {
    btn.disabled = false;
  }
}

async function _adoptDetectedName(connId, projectName) {
  await fetch(`${API_CONNECTIONS}/${connId}`, {
    method: "PUT", credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: projectName }),
  });
  document.getElementById("ds-wizard-connected-banner").innerHTML = `✓ Connected — renamed to "${_esc(projectName)}".`;
}

async function _loadWizardTables(connId) {
  const body = document.getElementById("ds-wizard-tables-body");
  body.innerHTML = `<div class="ds-loading">Loading buckets and tables…</div>`;
  try {
    const r = await fetch(`${API_CONNECTIONS}/${connId}/tables`, { credentials: "include" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      body.innerHTML = `<div class="ds-browse-error">Failed to list tables: ${_esc(data.detail || String(r.status))}</div>`;
      return;
    }
    body.innerHTML = _scopeNote(data) + _renderBucketPicker(connId, data.buckets || []);
    // Once per fresh render only — see the matching comment on the other
    // _renderBucketPicker call site above.
    if (window.dsDropdownInit) body.querySelectorAll(".ds-dropdown").forEach(window.dsDropdownInit);
    // The picker's own "Register selected" button is redundant with the
    // modal's footer button — hide it here to avoid two triggers.
    const inlineBtn = body.querySelector(".ds-browse-actions .btn-primary");
    if (inlineBtn) inlineBtn.style.display = "none";
    _syncPicker(body);
  } catch (e) {
    body.innerHTML = `<div class="ds-browse-error">Request failed.</div>`;
  }
}

async function finishWizard() {
  await closeWizard();
  if (_wizardSource === "snowflake") {
    window.location.reload();
  }
}

/* ── Step 2 (BigQuery): manual rows editor ────────────────────────────────
   BigQuery has no bucket browser — datasets are named, not listed (the
   service account may see thousands). Each filled row registers as a LIVE
   query (query_mode='remote', cost-guarded); saved queries and partitioning
   keep their full editor on /admin/tables. */

function _renderBqRowsEditor() {
  const body = document.getElementById("ds-wizard-tables-body");
  document.getElementById("ds-wizard-connected-banner").innerHTML =
    "Name the BigQuery tables to register — dataset and table, one per row.";
  body.innerHTML = `
    <div id="ds-bqrows">
      ${_bqRowHtml()}${_bqRowHtml()}${_bqRowHtml()}
    </div>
    <button type="button" class="btn btn-secondary" id="ds-bqrow-add">＋ Add another table</button>
    <div class="ds-browse-error" id="ds-bq-rows-error" style="display:block; margin-top:8px;"></div>`;
  body.querySelector("#ds-bqrow-add").addEventListener("click", () => {
    body.querySelector("#ds-bqrows").insertAdjacentHTML("beforeend", _bqRowHtml());
  });
}

function _bqRowHtml() {
  return `
  <div class="ds-bqrow">
    <input type="text" data-bq-dataset placeholder="dataset (e.g. analytics)" autocomplete="off" spellcheck="false">
    <input type="text" data-bq-table placeholder="table (e.g. events_raw)" autocomplete="off" spellcheck="false">
    <span class="ds-table-status"></span>
  </div>`;
}

async function _registerBqRows() {
  const rows = [...document.querySelectorAll("#ds-bqrows .ds-bqrow")]
    .map((row) => ({
      row,
      dataset: row.querySelector("[data-bq-dataset]").value.trim(),
      table: row.querySelector("[data-bq-table]").value.trim(),
      statusEl: row.querySelector(".ds-table-status"),
    }))
    .filter((r) => r.dataset || r.table);
  const errEl = document.getElementById("ds-bq-rows-error");
  errEl.textContent = "";
  if (!rows.length) {
    errEl.textContent = "Fill in at least one dataset + table row.";
    return { ok: [], failed: 0 };
  }
  const registered = [];
  let failed = 0;
  for (const r of rows) {
    if (!r.dataset || !r.table) {
      r.statusEl.textContent = "✗ needs both dataset and table";
      r.statusEl.className = "ds-table-status fail";
      failed++;
      continue;
    }
    r.statusEl.textContent = "registering…";
    r.statusEl.className = "ds-table-status";
    try {
      const resp = await fetch(API_REGISTER_TABLE, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: r.table,
          source_type: "bigquery",
          bucket: r.dataset,
          source_table: r.table,
          query_mode: "remote",
          description: "Added via the Add-data wizard",
        }),
      });
      if (resp.ok || resp.status === 201 || resp.status === 409) {
        r.statusEl.textContent = resp.status === 409 ? "already registered" : "✓ registered";
        r.statusEl.className = "ds-table-status ok";
        registered.push({
          id: r.table.trim().toLowerCase().replace(/ /g, "_"),
          name: r.table,
          bucket: r.dataset,
        });
      } else {
        const b = await resp.json().catch(() => ({}));
        // No _esc(): the sink is textContent, which escapes on its own.
        // Escaping first would show the operator the literal
        // `Did you mean &quot;Y&quot;?` instead of the quoted table name —
        // mangling the one string this surface exists to relay. Line ~2412
        // (the single-row path) already gets this right.
        r.statusEl.textContent = "✗ " + _registerErrorText(b);
        r.statusEl.className = "ds-table-status fail";
        failed++;
      }
    } catch (e) {
      r.statusEl.textContent = "✗ request failed";
      r.statusEl.className = "ds-table-status fail";
      failed++;
    }
  }
  await refreshSourcePipelines();
  return { ok: registered, failed };
}

/* ── Step 2 (Snowflake): schema/table picker + manual fallback ─────────────
   Was manual-entry only: two free-text inputs, nothing checked against the
   account, and the registry id composed as `schema + "_" + table` — so a name
   pasted with its schema prefix already attached silently produced a doubled
   id pointing at a table that does not exist, which then cannot heal (only a
   re-save re-runs the remote-extract build). The picker below lists what the
   configured user can actually see; the manual rows stay for anything the
   listing cannot reach. Either path registers LIVE (query_mode='remote') or a
   scheduled MATERIALIZED copy; custom SQL and semantic views keep their full
   editor. */

function _renderSfRowsEditor() {
  const body = document.getElementById("ds-wizard-tables-body");
  document.getElementById("ds-wizard-connected-banner").innerHTML =
    "Pick the Snowflake tables to register — or name one by hand if it is not listed.";
  body.innerHTML = `
    <div class="ds-sf-conn-error" id="ds-sf-conn-error" role="alert">
      <span class="ds-sf-conn-error__icon" aria-hidden="true"><svg class="ui-ico " width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span>
      <span class="ds-sf-conn-error__msg"></span>
      <button type="button" class="btn btn-secondary" id="ds-sf-conn-error-fix">Fix connection</button>
    </div>
    <div class="ds-browse-actions" style="margin-bottom:10px; align-items:center;">
      <button type="button" class="btn btn-secondary" id="ds-sf-browse">Reload tables</button>
      <label style="display:inline-flex; align-items:center; gap:6px;">
        <span style="color:var(--ds-text-secondary);">register selected as</span>
        <select id="ds-sf-picker-mode" class="ds-dropdown-native">
          <option value="remote" selected>live</option>
          <option value="materialized">materialized</option>
        </select>
        ${_dropdownMarkupHtml("ds-sf-picker-mode-dd", "ds-sf-picker-mode", "register selected as",
          [{ value: "remote", label: "live" }, { value: "materialized", label: "materialized" }], "remote")}
      </label>
    </div>
    <div id="ds-sf-picker"></div>
    <div style="margin-top:14px;">
      <div style="color:var(--ds-text-secondary); margin-bottom:6px;">
        Not listed — or listing unavailable? Name a table by hand:
      </div>
      <div id="ds-sfrows">
        ${_sfRowHtml()}
      </div>
      <button type="button" class="btn btn-secondary" id="ds-sfrow-add">＋ Add another table</button>
    </div>
    <div class="ds-browse-error" id="ds-sf-rows-error" style="display:block; margin-top:8px;"></div>`;
  if (window.dsDropdownInit) body.querySelectorAll(".ds-dropdown").forEach(window.dsDropdownInit);
  // #1616: "Fix connection" jumps back to step 1 — no new page, the same
  // navigation the steps-strip's own back button uses (see its click
  // handler below) — rather than leaving the operator stranded on a step
  // where nothing past step 1 can succeed until the credential is fixed.
  body.querySelector("#ds-sf-conn-error-fix").addEventListener("click", () => {
    _setWizStep(1);
    _setWizSource(_wizardSource);
  });
  body.querySelector("#ds-sfrow-add").addEventListener("click", () => {
    const rows = body.querySelector("#ds-sfrows");
    rows.insertAdjacentHTML("beforeend", _sfRowHtml());
    if (window.dsDropdownInit) rows.lastElementChild.querySelectorAll(".ds-dropdown").forEach(window.dsDropdownInit);
  });
  body.querySelector("#ds-sf-browse").addEventListener("click", _loadSfCatalog);
  // Auto-load, mirroring the Keboola step (which lists tables as soon as the
  // connection tests OK) — a picker nobody thinks to click does not stop anyone
  // from mistyping a name. Failure degrades to the manual rows below.
  _loadSfCatalog();
}

/* #1616: while the connection itself can't authenticate (or can't be
   reached), Reload tables / the manual table-entry rows / "Continue with
   selected tables" / "Register only & finish" can only ever fail the same
   way the listing just did — leave them live and the operator discovers
   that one dead click at a time. `_setSfConnErrorState(true, …)` shows the
   banner + "Fix connection" and disables all of them in one place;
   `_loadSfCatalog` clears it (`false`) at the top of every fresh attempt.
   Two of the controls it disables — the register / register-only footer
   buttons — are SHARED with the other sources' step 2, so the lock must also
   be released whenever the wizard is pointed at a source at all: see the
   `_setSfConnErrorState(false)` at the end of `_setWizSource`. */
let _sfConnErrorActive = false;

function _setSfConnErrorState(active, message) {
  _sfConnErrorActive = active;
  const banner = document.getElementById("ds-sf-conn-error");
  if (banner) {
    banner.style.display = active ? "flex" : "none";
    const msgEl = banner.querySelector(".ds-sf-conn-error__msg");
    if (msgEl) msgEl.textContent = message || "";
  }
  const browseBtn = document.getElementById("ds-sf-browse");
  if (browseBtn) browseBtn.disabled = active;
  const pickerMode = document.getElementById("ds-sf-picker-mode");
  if (pickerMode) pickerMode.disabled = active;
  // The branded dropdown button paired with the native select above (paper
  // theme hides the real control and intercepts all interaction on its own
  // button) — disabling the native select alone does not stop clicks on it.
  const pickerModeBtn = document.getElementById("ds-sf-picker-mode-dd-btn");
  if (pickerModeBtn) pickerModeBtn.disabled = active;
  const addBtn = document.getElementById("ds-sfrow-add");
  if (addBtn) addBtn.disabled = active;
  const rowsHost = document.getElementById("ds-sfrows");
  if (rowsHost) rowsHost.querySelectorAll("input, select, textarea, button").forEach((el) => { el.disabled = active; });
  // Live outside `#ds-wizard-tables-body`, in the drawer's sticky foot.
  const registerBtn = document.getElementById("ds-wizard-register-btn");
  if (registerBtn) registerBtn.disabled = active;
  const finishBtn = document.getElementById("ds-wizard-finish-early-btn");
  if (finishBtn) finishBtn.disabled = active;
}

/* ── Snowflake table picker ───────────────────────────────────────────────
   Renders with the SAME classes + `data-bucket-group` hooks the Keboola picker
   uses, inside the same `#ds-wizard-tables-body` root, so the delegated
   expand / select-all / filter / count handlers above apply unchanged. One
   schema = one group. */

async function _loadSfCatalog() {
  const host = document.getElementById("ds-sf-picker");
  const btn = document.getElementById("ds-sf-browse");
  if (!host) return;
  _setSfConnErrorState(false);
  host.innerHTML = `<div class="ds-empty">Listing tables in the configured Snowflake account…</div>`;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/admin/data-sources/snowflake/tables", { credentials: "include" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      host.innerHTML = "";
      // Both keys, because the endpoint answers a misconfiguration under
      // `detail` while other admin endpoints use `message`; kept inline rather
      // than shared so this step does not depend on a helper landing first.
      // #1616: the server already classified this into an operator-readable
      // sentence with no driver internals — render it as-is, in the banner,
      // and stop the rest of the step from inviting a retry that can't work.
      const msg = (typeof body.detail === "string" && body.detail)
        || (typeof body.message === "string" && body.message)
        || "listing failed";
      _setSfConnErrorState(true, msg);
      return;
    }
    const schemas = body.schemas || [];
    if (!schemas.length) {
      host.innerHTML = `<div class="ds-empty">No tables visible to the configured Snowflake user.</div>`;
      return;
    }
    host.innerHTML = _sfPickerHtml(schemas, body.database || "");
    _syncPicker(_pickerRoot(host));
  } catch (e) {
    host.innerHTML = "";
    _setSfConnErrorState(true, "Could not reach the server. Check your network connection and try again.");
  } finally {
    // `_setSfConnErrorState` above may have just disabled this same button
    // (the connection-broken case) — defer to whatever it decided rather
    // than unconditionally re-enabling here.
    if (btn) btn.disabled = _sfConnErrorActive;
  }
}

function _sfPickerHtml(schemas, database) {
  const total = schemas.reduce((n, s) => n + (s.tables || []).length, 0);
  const openByDefault = schemas.length === 1;
  const groups = schemas.map((sc, si) => {
    const tables = sc.tables || [];
    const rows = tables.map((t, ti) => `
      <div class="ds-table-row" data-search="${_esc((t.name || "").toLowerCase())}">
        <label>
          <input type="checkbox" class="ds-table-checkbox"
                 data-sf-pick data-sf-pick-schema="${_esc(sc.name)}" data-sf-pick-table="${_esc(t.name)}">
          <span class="ds-table-name">${_esc(t.name)}</span>
        </label>
        <span class="ds-table-rows">${_esc(t.table_type === "VIEW" ? "view" : "")}</span>
        <span class="ds-table-status" id="ds-sfstatus-${si}-${ti}"></span>
      </div>`).join("");
    const panelId = `ds-sfsch-${si}`;
    const count = tables.length;
    return `
      <div class="ds-bucket-group${openByDefault ? " is-open" : ""}"
           data-bucket-group data-user-open="${openByDefault ? "1" : "0"}"
           data-search="${_esc((sc.name || "").toLowerCase())}">
        <div class="ds-bucket-hd">
          <input type="checkbox" class="ds-bucket-check"
                 aria-label="Select all ${count} table${count === 1 ? "" : "s"} in ${_esc(sc.name)}">
          <button type="button" class="ds-bucket-toggle" aria-expanded="${openByDefault}" aria-controls="${panelId}">
            <span class="ds-bucket-caret" aria-hidden="true">▶</span>
            <span class="ds-bucket-title">${_esc(sc.name)}</span>
            <span class="ds-bucket-meta" data-bucket-meta>${count} table${count === 1 ? "" : "s"}</span>
          </button>
        </div>
        <div class="ds-bucket-tables" id="${panelId}"${openByDefault ? "" : " hidden"}>
          ${rows || '<div class="ds-empty">No tables in this schema.</div>'}
        </div>
      </div>`;
  }).join("");
  return `
    <div class="ds-picker-bar">
      <input type="search" class="ds-picker-search" data-picker-search
             placeholder="Filter schemas and tables…" aria-label="Filter schemas and tables"
             autocomplete="off" spellcheck="false">
      <span class="ds-picker-count" data-picker-count aria-live="polite">0 of ${total} selected</span>
      <button type="button" class="ds-picker-link" data-picker-all>Select all</button>
      <button type="button" class="ds-picker-link" data-picker-none disabled>Clear</button>
      <button type="button" class="ds-picker-link" data-picker-expand>${openByDefault ? "Collapse all" : "Expand all"}</button>
    </div>
    ${groups}
    <div style="color:var(--ds-text-secondary); margin-top:6px;">
      Database <code>${_esc(database)}</code>
    </div>`;
}

let _sfRowIdSeq = 0;

function _sfRowHtml() {
  // Each row is inserted once via insertAdjacentHTML and never re-rendered
  // in place, so a per-row id is safe to hand to dsDropdownInit exactly
  // once at insertion (see the "+ Add another table" handler above) — no
  // double-init risk the way a re-rendered-in-place list would have.
  const id = `ds-sfrow-mode-${++_sfRowIdSeq}`;
  return `
  <div class="ds-sfrow">
    <input type="text" data-sf-schema placeholder="schema (e.g. PUBLIC)" autocomplete="off" spellcheck="false">
    <input type="text" data-sf-table placeholder="table (e.g. ORDERS)" autocomplete="off" spellcheck="false">
    <select id="${id}" data-sf-mode class="ds-dropdown-native">
      <option value="remote" selected>live</option>
      <option value="materialized">materialized</option>
    </select>
    ${_dropdownMarkupHtml(`${id}-dd`, id, "How this table is served",
      [{ value: "remote", label: "live" }, { value: "materialized", label: "materialized" }], "remote")}
    <span class="ds-table-status"></span>
  </div>`;
}

async function _registerSfRows() {
  // Two sources of truth, same shape downstream: boxes ticked in the picker
  // (real names off the account, so a mistyped schema or table is out of the
  // question — the derived id is still `schema + "_" + table` below, so a table
  // genuinely called GOLD_BI_X in schema GOLD still reads doubled; the picker
  // removes typos, not that) and any hand-written rows. Picker entries come
  // first so a schema+table named in both is registered once, from the picker.
  const pickerMode = (document.getElementById("ds-sf-picker-mode") || {}).value || "remote";
  // Per-row "registering…/✗ failed" is the only account of what happened, and
  // with more than one schema every group renders collapsed (`openByDefault =
  // schemas.length === 1`). Step 2's handler returns silently on zero
  // successes on the assumption those statuses are visible — so without this,
  // a wholly-failed registration is an enabled button that appears to do
  // nothing. Same preparation `registerSelected` does for the Keboola picker:
  // drop any filter and open the groups being written to.
  const sfChecked = [...document.querySelectorAll("#ds-sf-picker [data-sf-pick]:checked")];
  if (sfChecked.length) {
    // Resolve the root through `_pickerRoot`, not `#ds-sf-picker` directly, so
    // this agrees with what the delegated picker handlers consider the root.
    const sfRoot = _pickerRoot(sfChecked[0]) || document.getElementById("ds-sf-picker");
    if (sfRoot) {
      const search = sfRoot.querySelector("[data-picker-search]");
      if (search && search.value) { search.value = ""; _filterPicker(sfRoot, ""); }
      for (const cb of sfChecked) {
        const group = cb.closest("[data-bucket-group]");
        if (group && !group.classList.contains("is-open")) _setBucketOpen(group, true);
      }
      _syncPicker(sfRoot);
    }
  }
  const picked = sfChecked.map((box) => ({
    row: box.closest(".ds-table-row"),
    schema: box.dataset.sfPickSchema || "",
    table: box.dataset.sfPickTable || "",
    mode: pickerMode,
    statusEl: box.closest(".ds-table-row").querySelector(".ds-table-status"),
  }));
  const manual = [...document.querySelectorAll("#ds-sfrows .ds-sfrow")]
    .map((row) => ({
      row,
      schema: row.querySelector("[data-sf-schema]").value.trim(),
      table: row.querySelector("[data-sf-table]").value.trim(),
      mode: row.querySelector("[data-sf-mode]").value,
      statusEl: row.querySelector(".ds-table-status"),
    }))
    .filter((r) => r.schema || r.table);
  const seen = new Set();
  const rows = [...picked, ...manual].filter((r) => {
    const key = `${r.schema.toUpperCase()}.${r.table.toUpperCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const errEl = document.getElementById("ds-sf-rows-error");
  errEl.textContent = "";
  if (!rows.length) {
    errEl.textContent = "Pick at least one table above, or fill in a schema + table row.";
    return { ok: [], failed: 0 };
  }
  const registered = [];
  let failed = 0;
  for (const r of rows) {
    if (!r.schema || !r.table) {
      r.statusEl.textContent = "✗ needs both schema and table";
      r.statusEl.className = "ds-table-status fail";
      failed++;
      continue;
    }
    r.statusEl.textContent = "registering…";
    r.statusEl.className = "ds-table-status";
    let name = (r.schema + "_" + r.table).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
    if (!name) name = "sf_" + Math.random().toString(36).slice(2, 7);
    if (/^\d/.test(name)) name = "_" + name;
    try {
      const resp = await fetch(API_REGISTER_TABLE, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          source_type: "snowflake",
          bucket: r.schema,
          source_table: r.table,
          query_mode: r.mode,
          description: "Added via the Add-data wizard",
        }),
      });
      if (resp.ok || resp.status === 201 || resp.status === 409) {
        r.statusEl.textContent = resp.status === 409 ? "already registered" : "✓ registered";
        r.statusEl.className = "ds-table-status ok";
        registered.push({ id: name, name: r.table, bucket: r.schema });
      } else {
        const b = await resp.json().catch(() => ({}));
        // No _esc(): the sink is textContent, which escapes on its own.
        // Escaping first would show the operator the literal
        // `Did you mean &quot;Y&quot;?` instead of the quoted table name —
        // mangling the one string this surface exists to relay. Line ~2412
        // (the single-row path) already gets this right.
        r.statusEl.textContent = "✗ " + _registerErrorText(b);
        r.statusEl.className = "ds-table-status fail";
        failed++;
      }
    } catch (e) {
      r.statusEl.textContent = "✗ request failed";
      r.statusEl.className = "ds-table-status fail";
      failed++;
    }
  }
  await refreshSourcePipelines();
  return { ok: registered, failed };
}

// One entry point for step 2's "register whatever this source selected".
async function _registerStepTwo() {
  if (_wizardSource === "bigquery") return _registerBqRows();
  if (_wizardSource === "snowflake") return _registerSfRows();
  if (!_wizardConnId) return { ok: [], failed: 0 };
  // Step 2's own error line — the browse panel's sits on the page behind the
  // drawer, and `#ds-wizard-error` belongs to the connect step's pane.
  return registerSelected(_wizardConnId, "ds-tables-error");
}

/* ── Step 3: the bundle board ─────────────────────────────────────────────
   Arrange the just-registered tables into packages. Seeds (one package /
   split by bucket) are one-click STARTING POINTS, not modes — everything is
   editable after: tick tables → move bar, inline rename, ＋ New package, and
   a warn-styled "Leave out" tray whose tables land in the standing
   unpackaged tray on /admin/data-packages rather than vanishing. */

function _seedBoard(kind) {
  _wizardTrayIdSeq = 0;
  if (kind === "bucket") {
    const byBucket = new Map();
    for (const t of _wizardTables) {
      if (!byBucket.has(t.bucket)) byBucket.set(t.bucket, []);
      byBucket.get(t.bucket).push(t);
    }
    _wizardBoard = {
      trays: [...byBucket.entries()].map(([bucket, tables]) => ({
        id: "t" + (++_wizardTrayIdSeq),
        // `in.c-sales` → "Sales" — a starting name, editable inline.
        name: (bucket.split(".").pop() || bucket || "Package")
          .replace(/^c-/, "").replace(/[-_]+/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase()) || "Package",
        tables: tables.slice(),
      })),
      out: [],
    };
  } else {
    const conn = _connections.find((c) => c.id === _wizardConnId);
    _wizardBoard = {
      trays: [{
        id: "t" + (++_wizardTrayIdSeq),
        name: (conn && conn.name) ? conn.name : "New package",
        tables: _wizardTables.slice(),
      }],
      out: [],
    };
  }
  _renderBoard();
}

/* A tray that has already become a real package is FROZEN here: its name is
   read-only and its tables cannot be ticked or moved. Coming back to Bundle
   after Share must not be able to re-create the same package, and pretending
   a rename or a move on this board still reaches a package that now lives in
   the registry would be a lie. Renames, merges and splits belong to
   /admin/data-packages from that point on — which the lede already says. */
function _renderBoard() {
  const trayHtml = (tray, out) => `
    <div class="ds-btray ${out ? "out" : ""}${tray.committed ? " is-created" : ""}" data-tray="${tray.id}">
      <div class="ds-btray-hd">
        ${out
          ? '<b style="font-size:13px;">Leave out</b>'
          : `<input class="ds-tray-name" value="${_esc(tray.name)}" aria-label="Package name"${tray.committed || tray.existing ? " readonly" : ""}>`}
        <span class="cnt">${tray.tables.length} table${tray.tables.length === 1 ? "" : "s"}</span>
        ${tray.committed
          ? `<span class="ds-tray-created" title="${tray.existing ? "Added — the package already had its own tables and sharing." : "Already created — rename, merge or split it on Data packages."}">✓ ${tray.existing ? "added" : "created"}</span>`
          : tray.existing
            ? '<span class="ds-tray-exist" title="This package already exists — Finish adds these tables to it and changes nothing else about it. Its name is edited on the package’s own page.">→ adds to existing</span>'
            : ""}
        ${out ? '<span class="cnt" style="margin-left:auto; font-weight:500;">they wait in the unpackaged tray — nobody can pull them until packaged</span>' : ""}
      </div>
      ${tray.tables.map((t) => `
        <div class="ds-btray-row">
          <input type="checkbox" class="ds-bt-check" data-table="${_esc(t.id)}" aria-label="Select ${_esc(t.name)}"${tray.committed ? " disabled" : ""}>
          <b>${_esc(t.name)}</b> <span class="bkt">${_esc(t.bucket)}</span>
        </div>`).join("")}
      ${tray.tables.length ? "" : '<div class="ds-btray-empty">Empty — tick tables anywhere and move them here.</div>'}
    </div>`;
  const frozen = _wizardBoard.trays.some((t) => t.committed);
  document.getElementById("ds-bundle-board").innerHTML =
    (frozen
      ? `<div class="ds-scope-note">The ticked packages already exist. Rename, merge or split them on
         <a href="/admin/data-packages">Data packages</a> — here you can still bundle anything new you add.</div>`
      : "") +
    _wizardBoard.trays.map((t) => trayHtml(t, false)).join("") +
    (_wizardBoard.out.length ? trayHtml({ id: "out", tables: _wizardBoard.out }, true) : "");
  // A seed rebuilds the whole board from scratch, which would silently drop
  // trays that are already packages — it is a starting point, and there is no
  // longer a start to go back to.
  document.querySelectorAll("#ds-wizard-step-bundle [data-wseed]").forEach((el) => {
    el.disabled = frozen;
    el.title = frozen ? "Packages have already been created — start over from Data packages." : "";
  });
  _updateMovebar();
  _updateBundleCta();
}

const _selectedBoardTables = () =>
  [...document.querySelectorAll(".ds-bt-check:checked")].map((c) => c.dataset.table);

function _updateMovebar() {
  const sel = _selectedBoardTables();
  const bar = document.getElementById("ds-movebar");
  bar.hidden = sel.length === 0;
  if (!sel.length) return;
  document.getElementById("ds-movebar-label").textContent = sel.length + " selected";
  document.getElementById("ds-movebar-targets").innerHTML =
    // Created packages are not move targets: the package already exists with
    // the tables it was created from, and this board can no longer add to it.
    _wizardBoard.trays.filter((t) => !t.committed)
      .map((t) => `<button type="button" class="btn btn-secondary" data-wmove="${t.id}">${_esc(t.name)}</button>`).join("") +
    '<button type="button" class="btn btn-secondary" data-wmove="out">Leave out</button>' +
    '<button type="button" class="btn btn-secondary" data-wmove="__new">＋ New package</button>';
}

function _moveSelected(target) {
  const sel = new Set(_selectedBoardTables());
  if (!sel.size) return;
  const moved = [];
  for (const tray of _wizardBoard.trays) {
    tray.tables = tray.tables.filter((t) => (sel.has(t.id) ? (moved.push(t), false) : true));
  }
  _wizardBoard.out = _wizardBoard.out.filter((t) => (sel.has(t.id) ? (moved.push(t), false) : true));
  if (target === "__new") {
    _wizardBoard.trays.push({ id: "t" + (++_wizardTrayIdSeq), name: "New package", tables: moved });
  } else if (target === "out") {
    _wizardBoard.out.push(...moved);
  } else {
    const tray = _wizardBoard.trays.find((t) => t.id === target);
    if (tray) tray.tables.push(...moved);
  }
  _renderBoard();
}

function _updateBundleCta() {
  if (_wizardStep !== 3 || !_wizardBoard) return;
  const pending = _wizardBoard.trays.filter((t) => t.tables.length && !t.committed);
  const creates = pending.filter((t) => !t.existing).length;
  const adds = pending.filter((t) => t.existing).length;
  const btn = document.getElementById("ds-wizard-bundle-btn");
  if (!pending.length && _wizardPackages.length) {
    // Stepped back into a Bundle whose packages already exist and were not
    // changed — the button is a way forward, not a second creation.
    btn.textContent = "Back to sharing";
  } else if (!pending.length) {
    btn.textContent = "Continue (no packages)";
  } else {
    // The button says exactly what Finish will do — creating and adding are
    // different verbs with different blast radii, so neither may hide
    // inside the other's count.
    const parts = [];
    if (creates) parts.push(`Create ${creates} package${creates === 1 ? "" : "s"}`);
    if (adds) parts.push(`${creates ? "add" : "Add"} to ${adds} existing`);
    btn.textContent = parts.join(" · ") + " & continue";
  }
}

const _slugify = (name) =>
  name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "package";

async function _createPackagesAndContinue() {
  const btn = document.getElementById("ds-wizard-bundle-btn");
  btn.disabled = true;
  try {
    // Only trays that are not already packages — re-entering this step from
    // the strip must move on, not mint a duplicate of everything on it.
    for (const tray of _wizardBoard.trays.filter((t) => t.tables.length && !t.committed)) {
      // Attach-only tray: the package exists, so Finish only POSTs its
      // tables. It stays OFF the share step — its grants already stand, and
      // a share card here would offer to write a second copy of them.
      if (tray.existing) {
        let added = 0;
        for (const t of tray.tables) {
          const ar = await fetch(`/api/admin/data-packages/${encodeURIComponent(tray.existing)}/tables`, {
            method: "POST", credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ table_id: t.id }),
          });
          if (ar.ok) added++;
          else showToast(`"${t.name}" could not be added to ${tray.name} — attach it later from the package.`, false);
        }
        tray.committed = tray.existing;
        _wizardExistingAdds.push({ name: tray.name, count: added });
        showToast(`${added} table${added === 1 ? "" : "s"} added to "${tray.name}" — its sharing is unchanged.`, true);
        continue;
      }
      let slug = _slugify(tray.name);
      let resp = await fetch("/api/admin/data-packages", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: tray.name.trim() || "Package", slug }),
      });
      if (resp.status === 409) {
        // slug_exists — suffix and retry once rather than failing the flow.
        slug = `${slug}-${Date.now() % 10000}`;
        resp = await fetch("/api/admin/data-packages", {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: tray.name.trim() || "Package", slug }),
        });
      }
      if (!resp.ok) {
        const b = await resp.json().catch(() => ({}));
        showToast(`Could not create "${tray.name}": ${b.detail || resp.status}`, false);
        return; // stop on the failing tray; already-created packages stand
      }
      const pkgId = (await resp.json()).id;
      let attached = 0;
      for (const t of tray.tables) {
        const ar = await fetch(`/api/admin/data-packages/${encodeURIComponent(pkgId)}/tables`, {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ table_id: t.id }),
        });
        if (ar.ok) attached++;
        else showToast(`"${t.name}" could not be added to ${tray.name} — attach it later from the package.`, false);
      }
      tray.committed = pkgId;
      _wizardPackages.push({ id: pkgId, localId: tray.id, name: tray.name, count: attached });
    }
    _renderBoard();
    _setWizStep(4);
    await _renderShareStep();
  } finally {
    btn.disabled = false;
    // In `finally`: a tray that failed halfway still created the packages
    // before it, and the Feeds cell behind the drawer is stale either way.
    await refreshSourcePipelines();
  }
}

/* ── Step 4: share — one card per created package, same grant rows as the
   group-side matrix; entirely skippable because unshared packages warn on
   Overview and on /admin/data-packages. ── */

async function _renderShareStep() {
  const host = document.getElementById("ds-share-cards");
  if (!_wizardGroups) {
    try {
      const r = await fetch("/api/admin/groups", { credentials: "include" });
      _wizardGroups = r.ok ? await r.json() : [];
    } catch (e) { _wizardGroups = []; }
  }
  if (!_wizardShares) {
    _wizardShares = {};
    for (const p of _wizardPackages) _wizardShares[p.localId] = [];
  }
  // Tables attached to EXISTING packages need no card here — their sharing
  // already stands — but they must be SAID, or "no packages were created"
  // reads as "nothing happened" after Finish did exactly what was asked.
  const existingNote = _wizardExistingAdds.length
    ? `<div class="ds-scope-note">${_wizardExistingAdds
        .map((a) => `${a.count} table${a.count === 1 ? "" : "s"} added to <b>${_esc(a.name)}</b>`)
        .join(" · ")} — already shared as those packages are.</div>`
    : "";
  if (!_wizardPackages.length) {
    host.innerHTML = existingNote ||
      '<div class="ds-empty">No packages were created — the tables wait in the unpackaged tray on Data packages.</div>';
    return;
  }
  const card = (p) => {
    const shares = _wizardShares[p.localId] || [];
    const row = (g) => {
      const share = shares.find((s) => s.group_id === g.id);
      const everyone = g.is_system && g.name === "Everyone";
      return `
      <div class="ds-wshare-row" data-pkg="${_esc(p.localId)}" data-group="${_esc(g.id)}">
        <input type="checkbox" ${share ? "checked" : ""} aria-label="Share ${_esc(p.name)} with ${_esc(g.name)}">
        <div>
          <div class="g-name">${AgnesKindGlyph.groupGlyph()}${_esc(g.name)}</div>
          <div class="g-sub ${everyone ? "everyone" : ""}">${everyone
            ? "the whole company — every account, automatically"
            : `${g.member_count ?? 0} member${(g.member_count ?? 0) === 1 ? "" : "s"}`}</div>
        </div>
        <div class="ds-wtier" ${share ? "" : 'aria-disabled="true"'} role="group" aria-label="Access tier">
          <button type="button" data-wtier="available" class="${share && share.tier === "available" ? "on" : ""}" title="available">Optional</button>
          <button type="button" data-wtier="required" class="${share && share.tier === "required" ? "on auto" : ""}" title="required">Automatic</button>
        </div>
      </div>`;
    };
    return `
    <div class="ds-wshare-card">
      <div class="ds-wshare-card__hd"><b>${_esc(p.name)}</b><span class="m">${p.count} table${p.count === 1 ? "" : "s"}</span></div>
      ${_wizardGroups.map(row).join("") || '<div class="ds-btray-empty">No groups on this instance yet.</div>'}
    </div>`;
  };
  // The audience you want may not exist yet. Without this the only way out of
  // that is to abandon the wizard, go to Access, make the group, and start
  // over — so the step that asks "who gets this?" can only be answered with
  // groups someone thought of earlier.
  const newGroup =
    '<button type="button" class="ds-wshare-newgroup" id="ds-wizard-newgroup">' +
    '<span aria-hidden="true">+</span> New group…</button>';
  host.innerHTML = existingNote + _wizardPackages.map(card).join("") + newGroup;
}

async function _shareAndFinish() {
  const btn = document.getElementById("ds-wizard-share-btn");
  const errEl = document.getElementById("ds-share-error");
  errEl.style.display = "none";
  btn.disabled = true;
  try {
    const failures = [];
    for (const p of _wizardPackages) {
      for (const s of _wizardShares[p.localId] || []) {
        const r = await fetch("/api/admin/grants", {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            group_id: s.group_id, resource_type: "data_package",
            resource_id: p.id, requirement: s.tier,
          }),
        });
        if (!r.ok && r.status !== 409) failures.push(`${p.name} → ${s.name}`);
      }
    }
    if (failures.length) {
      errEl.textContent = "Some grants could not be written: " + failures.join(", ") +
        ". Finish anyway — they stay editable from the package's Share editor.";
      errEl.style.display = "block";
    }
    _showDoneSlab();
  } finally {
    btn.disabled = false;
    // Grants are the end of the chain — "0 packages" becomes "1 package →
    // 4 people" here, and the card behind the receipt should say so.
    await refreshSourcePipelines();
  }
}

// Fire-and-forget: a wizard-registered local/materialized row sits unsynced
// in table_registry until the next scheduled tick (which can be hours away
// on a fresh instance's default schedule) or a manual trigger. One
// unscoped POST (covers every source_type the wizard just touched, not just
// Keboola) kicks that off the moment the wizard finishes; the registry rows
// exist either way, so a failed/refused trigger here is not worth surfacing
// as an error — the next scheduled tick still picks them up.
function _triggerWizardSync() {
  fetch("/api/sync/trigger", { method: "POST", credentials: "include" }).catch(() => {});
}

function _showDoneSlab() {
  _triggerWizardSync();
  const lines = _wizardPackages.map((p) => {
    const shares = _wizardShares[p.localId] || [];
    if (!shares.length) {
      return `<li><b>${_esc(p.name)}</b> — created, shared with nobody yet. It waits on the Overview.</li>`;
    }
    const det = shares.map((s) => `${_esc(s.name)} <span style="color:var(--ds-text-muted)">(${s.tier === "required" ? "automatic" : "optional"})</span>`).join(", ");
    return `<li><b>${_esc(p.name)}</b> — shared with ${det}.</li>`;
  });
  if (_wizardBoard && _wizardBoard.out.length) {
    lines.push(`<li><b>${_wizardBoard.out.length} table${_wizardBoard.out.length === 1 ? "" : "s"}</b> left unpackaged — see the tray on Data packages.</li>`);
  }
  const shared = _wizardPackages.filter((p) => (_wizardShares[p.localId] || []).length).length;
  document.getElementById("ds-wdone-slab").innerHTML = `
    <div class="big">✓</div>
    <h3>${_wizardPackages.length} package${_wizardPackages.length === 1 ? "" : "s"} created${shared ? ` · ${shared} shared` : ""}</h3>
    <p class="ds-wdone-sync-note">Sync started — newly registered tables will appear once it completes.</p>
    <ul>${lines.join("")}</ul>
    <a class="btn btn-secondary" href="/admin/data-packages">Open Data packages</a>
    <button type="button" class="btn btn-primary" id="ds-wdone-close">Done</button>`;
  const share = document.getElementById("ds-wizard-step-share");
  share.querySelector(".ds-wshare").style.display = "none";
  share.querySelector(".ds-wdone").style.display = "";
  // The receipt is the end of the flow — the strip stops offering a way back
  // into screens whose work has already landed in the registry.
  _wizardDone = true;
  document.querySelectorAll("#ds-wizard-overlay .ds-drawer__step").forEach((el) => {
    el.disabled = true;
    el.classList.add("is-done");
    el.classList.remove("is-now");
  });
  for (const id of _WIZ_ALL_BUTTONS) document.getElementById(id).style.display = "none";
  document.getElementById("ds-wdone-close").addEventListener("click", finishWizard);
}

/* ── Disclosure: the card's settings body ──────────────────────────────────
   The caret button is the ONE control for assistive tech and the keyboard —
   it carries `aria-expanded` and `aria-controls`. The head also toggles on a
   plain mouse click, because a 60px-tall row that opens something should open
   it wherever you hit it, but that convenience never becomes the only path:
   clicks that land on a real control inside the head (Actions, the caret
   itself) are left alone. */
function setSourceOpen(id, open) {
  const body = document.getElementById(`ds-body-${id}`);
  const caret = document.querySelector(`[data-disclose="${CSS.escape(id)}"]`);
  if (!body || !caret) return;
  body.hidden = !open;
  caret.setAttribute("aria-expanded", open ? "true" : "false");
}
function isSourceOpen(id) {
  const body = document.getElementById(`ds-body-${id}`);
  return !!body && !body.hidden;
}

document.getElementById("ds-conn-list").addEventListener("click", (e) => {
  const caret = e.target.closest("[data-disclose]");
  if (caret) {
    setSourceOpen(caret.dataset.disclose, !isSourceOpen(caret.dataset.disclose));
    return;
  }
  const menuBtn = e.target.closest("[data-srcmenu]");
  if (menuBtn) { e.stopPropagation(); toggleSourceMenu(menuBtn); return; }
  const head = e.target.closest("[data-src-head]");
  // Anything interactive inside the head keeps its own meaning.
  if (head && !e.target.closest("a, button, input, select")) {
    setSourceOpen(head.dataset.srcHead, !isSourceOpen(head.dataset.srcHead));
  }
});

/* ── The Actions menu ──────────────────────────────────────────────────────
   ONE popover node for the whole list, refilled and repositioned per card.
   Its contents are per-connector: a source this page OWNS gets the verbs, a
   source it merely reports on gets the routes to where it is really managed
   plus a line saying so — never a disabled item, which tells the reader what
   they cannot do without telling them what they can.

   The dividers are the ranking. Group one is what an admin does weekly (add
   tables, test); group two is credentials; group three is this-project-wide
   state; `Delete` sits alone at the bottom, out of reach of momentum. */
const _srcMenu = () => document.getElementById("ds-src-menu");

function closeSourceMenu() {
  const menu = _srcMenu();
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  menu.innerHTML = "";
  document.querySelectorAll("[data-srcmenu][aria-expanded='true']")
    .forEach((b) => b.setAttribute("aria-expanded", "false"));
}

function _sourceMenuItems(row) {
  const id = row.id;
  const name = _esc(row.name || "");
  if (row.derived) {
    return `
      <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); openWizard('${_esc(row.source_type)}')">Add tables…</button>
      <a class="apg-menu__item" role="menuitem" href="/admin/tables">Open its tables</a>
      <div class="apg-menu__sep"></div>
      <a class="apg-menu__item" role="menuitem" href="${_esc(row.settings_href || "/admin")}">${_esc(row.settings_label || "Settings")} <span class="note">→</span></a>
      <p class="apg-menu__note">${name} keeps no stored connection — its credentials and settings live where the link above goes.</p>`;
  }
  // SharePoint owns a completely different verb set — the Keboola-only
  // items above (table registration, storage/master token rotation, chat
  // tools, "default project") are meaningless (and in "Test connection"'s
  // case, actively wrong: it calls Keboola's own token-verify endpoint) for
  // a file source. Live-use feedback: those items showing at all read as
  // "dangerous", not just irrelevant.
  if (row.source_type === "sharepoint") {
    return `
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); openSpWizardForConnection('${id}')">Manage scopes…</button>
    <button type="button" class="apg-menu__item" role="menuitem" data-role="test" onclick="closeSourceMenu(); testSpConn('${id}')">Test connection</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); runSpExtraction('${id}')">Run extraction now</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); runSpFactsExtraction('${id}')">Extract facts now</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleSpSplitRow('${id}')">Split this site…</button>
    <div class="apg-menu__sep"></div>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); consolidateSpCollections('${id}')">Consolidate collections…</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); mapSpSiteGroup('${id}')">Map site group (ACL)…</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleSpCertRow('${id}')">Update certificate…</button>
    <div class="apg-menu__sep"></div>
    <button type="button" class="apg-menu__item apg-menu__item--danger" role="menuitem" onclick="closeSourceMenu(); deleteConn('${id}')">Delete source</button>`;
  }
  const isDefault = row.is_default === true || row.is_default === 1;
  const hasMaster = row.has_master_secret === true;
  const chatOn = row.has_chat_tools === true;
  return `
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleBrowse('${id}')">Add tables…</button>
    <button type="button" class="apg-menu__item" role="menuitem" data-role="test" onclick="closeSourceMenu(); testConn('${id}')">Test connection</button>
    <div class="apg-menu__sep"></div>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleRotate('${id}')">Rotate storage token</button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleMasterToken('${id}')">Semantic-layer token <span class="note">${hasMaster ? "set" : "not set"}</span></button>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); toggleChatTools('${id}', ${chatOn ? "true" : "false"})">${chatOn ? "Turn off chat tools" : "Turn on chat tools"} <span class="note">${chatOn ? "on" : "off"}</span></button>
    ${isDefault ? "" : `
    <div class="apg-menu__sep"></div>
    <button type="button" class="apg-menu__item" role="menuitem" onclick="closeSourceMenu(); setDefaultConn('${id}')">Make default project</button>`}
    <div class="apg-menu__sep"></div>
    <button type="button" class="apg-menu__item apg-menu__item--danger" role="menuitem" onclick="closeSourceMenu(); deleteConn('${id}')">Delete source</button>`;
}

function toggleSourceMenu(btn) {
  const wasOpen = btn.getAttribute("aria-expanded") === "true";
  closeSourceMenu();
  if (wasOpen) return;
  const row = _connections.find((c) => c.id === btn.dataset.srcmenu);
  if (!row) return;

  const menu = _srcMenu();
  menu.setAttribute("aria-label", `Actions for ${_esc(row.name || row.id)}`);
  menu.innerHTML = _sourceMenuItems(row);
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");

  // Right-aligned to the trigger, flipping above it when the viewport below
  // is too short — the menu is `position: fixed`, so it is the viewport it
  // has to fit in, not the card.
  const r = btn.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
  menu.style.top = r.bottom + 6 + h > window.innerHeight ? `${Math.max(8, r.top - h - 6)}px` : `${r.bottom + 6}px`;
  menu.querySelector(".apg-menu__item")?.focus({ preventScroll: true });
}

// Broad dismissal — a fixed popover does not travel with the card it belongs
// to, and a menu still pointing at a source that has scrolled away is exactly
// the mistake a Delete item must not make.
document.addEventListener("click", (e) => {
  if (!e.target.closest("#ds-src-menu") && !e.target.closest("[data-srcmenu]")) closeSourceMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSourceMenu(); });
window.addEventListener("scroll", closeSourceMenu, true);
window.addEventListener("resize", closeSourceMenu);

/* ── Event wiring ────────────────────────────────────────────────────────── */

document.getElementById("ds-add-btn").addEventListener("click", () => openWizard());
document.getElementById("ds-wizard-close").addEventListener("click", closeWizard);
document.getElementById("ds-wizard-cancel").addEventListener("click", closeWizard);

// Step 1: the connector picker swaps the connect form. SharePoint runs its
// own drawer (own 3-step flow, own endpoints) rather than a form inside this
// one — picking it closes this wizard and opens that one instead.
document.querySelectorAll("[data-wsrc]").forEach((opt) => {
  opt.addEventListener("click", () => {
    if (opt.dataset.wsrc === "sharepoint") {
      closeWizard();
      openSpWizard();
      return;
    }
    _setWizSource(opt.dataset.wsrc);
  });
});

// Step 1 primary: what "continue" means depends on the connector.
document.getElementById("ds-wizard-connect-btn").addEventListener("click", () => {
  if (_wizardSource === "bigquery") {
    _setWizStep(2);
    _renderBqRowsEditor();
    return;
  }
  if (_wizardSource === "snowflake") {
    _saveSnowflakeAndContinue();
    return;
  }
  if (_wizardSource === "databricks") {
    _saveDatabricksAndContinue();
    return;
  }
  connectAndValidate();
});

// Update the Snowflake credential status badges as the admin types.
document.getElementById("ds-sf-auth-type").addEventListener("change", () => { _updateSfAuthUI(); _renderSfCredStatus(null, "password"); _renderSfCredStatus(null, "key"); });
document.getElementById("ds-sf-password").addEventListener("input", () => _renderSfCredStatus(null, "password"));
document.getElementById("ds-sf-private-key").addEventListener("input", () => _renderSfCredStatus(null, "key"));

// Update the Databricks credential status badge as the admin types.
document.getElementById("ds-dbx-token").addEventListener("input", () => _renderDbxCredStatus(null));

/* The steps strip as a back button. Nothing is re-fetched and nothing is
   re-rendered: every screen's state lives where it was left — the connect
   form's fields, the picker's checkboxes, the board's trays, the share
   grid's rows — so going back to fix one thing costs nothing else. The
   disabled attribute already blocks the steps that have not been earned;
   this only has to honour it. */
document.querySelector("#ds-wizard-overlay .ds-drawer__steps").addEventListener("click", (e) => {
  const btn = e.target.closest(".ds-drawer__step");
  if (!btn || btn.disabled) return;
  const step = Number(btn.dataset.wstep);
  if (!step || step === _wizardStep) return;
  _setWizStep(step);
  // Step 1's primary button is the connector's, not the step's — CSV and
  // Jira have no form to submit, and _setWizStep only knows about steps.
  if (step === 1) _setWizSource(_wizardSource);
});

// Step 1 shortcut: add more tables from an already-connected project.
document.getElementById("ds-wexisting-btn").addEventListener("click", async () => {
  const id = document.getElementById("ds-wexisting-select").value;
  if (!id) return;
  _wizardConnId = id;
  const conn = _connections.find((c) => c.id === id);
  document.getElementById("ds-wizard-connected-banner").innerHTML =
    `Adding tables from "${_esc((conn && conn.name) || id)}".`;
  _setWizStep(2);
  await _loadWizardTables(id);
});

// Step 1: the semantic-layer opt-in reveals its token field.
document.getElementById("ds-new-semantic").addEventListener("change", (e) => {
  document.querySelector(".ds-wsl-opt__field").hidden = !e.target.checked;
  if (e.target.checked) document.getElementById("ds-new-master").focus();
});

// Step 2 → 3: register what this source selected, then bundle it.
document.getElementById("ds-wizard-register-btn").addEventListener("click", async () => {
  const btn = document.getElementById("ds-wizard-register-btn");
  btn.disabled = true;
  try {
    const result = await _registerStepTwo();
    if (!result || !result.ok.length) return; // per-row statuses say why
    if (!_wizardBoard) {
      _wizardTables = result.ok;
      _seedBoard("one");
    } else {
      // Stepped back to add more tables. The board the admin already arranged
      // survives; only what is genuinely new joins it, in a tray of its own so
      // it is obvious what just arrived.
      const known = new Set([..._wizardBoard.trays.flatMap((t) => t.tables), ..._wizardBoard.out].map((t) => t.id));
      const fresh = result.ok.filter((t) => !known.has(t.id));
      _wizardTables = [..._wizardTables, ...fresh];
      if (fresh.length) {
        const open = _wizardBoard.trays.find((t) => !t.committed);
        if (open) open.tables.push(...fresh);
        else _wizardBoard.trays.push({ id: "t" + (++_wizardTrayIdSeq), name: "New package", tables: fresh });
      }
      _renderBoard();
    }
    _setWizStep(3);
    _refreshExistingPkgOptions();
  } finally {
    btn.disabled = false;
  }
});

// Step 2 escape hatch: the pre-redesign behavior — register and stop.
document.getElementById("ds-wizard-finish-early-btn").addEventListener("click", async () => {
  const result = await _registerStepTwo();
  if (result && result.ok.length) {
    _triggerWizardSync();
    showToast("Sync started — pulling the newly registered tables.", true);
    finishWizard();
  }
});

document.getElementById("ds-wizard-bundle-btn").addEventListener("click", _createPackagesAndContinue);
document.getElementById("ds-wizard-share-btn").addEventListener("click", _shareAndFinish);
document.getElementById("ds-wizard-skip-btn").addEventListener("click", _showDoneSlab);
document.getElementById("ds-wizard-done-btn").addEventListener("click", finishWizard);

// Step 3 board: seeds, selection, moves, renames, new tray.
document.getElementById("ds-wizard-step-bundle").addEventListener("click", (e) => {
  const seed = e.target.closest("[data-wseed]");
  if (seed) { _seedBoard(seed.dataset.wseed); return; }
  const mv = e.target.closest("[data-wmove]");
  if (mv) { _moveSelected(mv.dataset.wmove); return; }
  if (e.target.closest("#ds-new-tray-btn")) {
    _wizardBoard.trays.push({ id: "t" + (++_wizardTrayIdSeq), name: "New package", tables: [] });
    _renderBoard();
    const names = document.querySelectorAll(".ds-tray-name");
    const last = names[names.length - 1];
    if (last) { last.focus(); last.select(); }
  }
});
document.getElementById("ds-wizard-step-bundle").addEventListener("change", (e) => {
  if (e.target.classList.contains("ds-bt-check")) _updateMovebar();
  if (e.target.id === "ds-existing-pkg-sel" && e.target.value) {
    const pkg = (_wizardExistPkgs || []).find((p) => p.id === e.target.value);
    if (!pkg) { e.target.value = ""; _syncDropdownRebuild(e.target); return; }
    _wizardBoard.trays.push({
      id: "t" + (++_wizardTrayIdSeq),
      name: pkg.name,
      tables: [],
      existing: pkg.id,
    });
    _renderBoard();
    // Re-filters the onBoard exclusion now that this pick joined the board,
    // and resets the select back to its placeholder.
    _refreshExistingPkgOptions();
  }
});
// The existing-package list loads on first open, not on page load — it is
// another page's data and most wizard runs never open this picker. Packages
// already sitting on the board are omitted, not disabled: offering a second
// tray for the same package would make Finish attach twice.
//
// `_wizardExistPkgs` is fetched once and cached; the onBoard filter is
// recomputed every call since the board changes as packages are added. Also
// called from _setWizStep(3) (paired .ds-dropdown never receives the native
// `focus` below — the branded button intercepts all interaction, and the
// paper theme hides the real select) and after each pick updates the board.
async function _refreshExistingPkgOptions() {
  const sel = document.getElementById("ds-existing-pkg-sel");
  if (_wizardExistPkgs === null) {
    try {
      const r = await fetch("/api/admin/data-packages", { credentials: "include" });
      const body = r.ok ? await r.json() : [];
      _wizardExistPkgs = Array.isArray(body) ? body : (body.items || []);
    } catch (err) { _wizardExistPkgs = []; }
  }
  const onBoard = new Set((_wizardBoard ? _wizardBoard.trays : []).map((t) => t.existing || t.committed).filter(Boolean));
  sel.innerHTML = '<option value="">＋ Into existing package…</option>' +
    _wizardExistPkgs.filter((p) => !onBoard.has(p.id))
      .map((p) => `<option value="${_esc(p.id)}">${_esc(p.name)}</option>`).join("");
  _syncDropdownRebuild(sel);
}
document.getElementById("ds-existing-pkg-sel").addEventListener("focus", _refreshExistingPkgOptions);
document.getElementById("ds-wizard-step-bundle").addEventListener("input", (e) => {
  if (e.target.classList.contains("ds-tray-name")) {
    const tray = _wizardBoard.trays.find((t) => t.id === e.target.closest(".ds-btray").dataset.tray);
    if (tray) tray.name = e.target.value;
  }
});

// Step 4 cards: checkbox = share with default Optional; tier segmented.
document.getElementById("ds-share-cards").addEventListener("click", (e) => {
  // "New group…" — make the audience without leaving the wizard.
  //
  // Reuses AgnesGroupDrawer, the same drawer /admin/access opens, so there is
  // one place a group comes into existence rather than a wizard-shaped copy
  // of it. On save the group list is re-fetched and the step re-rendered; the
  // shares chosen so far live in `_wizardShares`, which this does not touch,
  // so nothing already ticked is lost.
  if (e.target.closest("#ds-wizard-newgroup")) {
    if (!window.AgnesGroupDrawer) return;
    window.AgnesGroupDrawer.open({
      onSaved: async () => {
        try {
          const r = await fetch("/api/admin/groups", { credentials: "include" });
          _wizardGroups = r.ok ? await r.json() : _wizardGroups;
        } catch (err) { /* keep the list we have — the step still works */ }
        _renderShareStep();
      },
    });
    return;
  }
  const row = e.target.closest(".ds-wshare-row");
  if (!row) return;
  const pkgLocal = row.dataset.pkg;
  const groupId = row.dataset.group;
  const group = (_wizardGroups || []).find((g) => g.id === groupId);
  const shares = _wizardShares[pkgLocal] || (_wizardShares[pkgLocal] = []);
  if (e.target.matches('input[type="checkbox"]')) {
    const i = shares.findIndex((s) => s.group_id === groupId);
    if (e.target.checked && i === -1) shares.push({ group_id: groupId, name: group ? group.name : groupId, tier: "available" });
    else if (!e.target.checked && i !== -1) shares.splice(i, 1);
    _renderShareStep();
    return;
  }
  const tierBtn = e.target.closest("[data-wtier]");
  if (tierBtn) {
    const share = shares.find((s) => s.group_id === groupId);
    if (share) { share.tier = tierBtn.dataset.wtier; _renderShareStep(); }
  }
});

// The backdrop is a CHILD of the drawer now (it has to sit under the panel
// rather than around it), so "clicked outside" is a hit on that element —
// `e.target === this` matched the old full-screen overlay and would never
// fire here.
document.getElementById("ds-wizard-overlay").addEventListener("click", function (e) {
  if (e.target.closest("[data-wizard-close]")) closeWizard();
});
// Document-level: focus legitimately leaves the panel (a click on the
// backdrop puts it on <body>), and Escape must still close from there.
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  if (document.getElementById("ds-wizard-overlay").classList.contains("show")) closeWizard();
});

// Deep-link: the Overview's "+ Add data" lands here with ?add=1. The open
// waits for the connections list — openWizard reads it to offer the
// "already connected" shortcut, and racing it hid the strip on exactly the
// entry path that needs it most.
loadConnections().then(() => {
  // Honour the same precondition as `#ds-add-btn`, which is `disabled` when
  // no vault key is configured. This deep link is the Overview checklist's
  // CTA, and it opened the wizard unconditionally — so an admin on an
  // instance that cannot store a secret filled in the whole form behind a
  // banner that had already told them it would fail, and the attempt left a
  // credential-less connection row behind.
  if (new URLSearchParams(window.location.search).has("add")) {
    const addBtn = document.getElementById("ds-add-btn");
    if (addBtn && addBtn.disabled) {
      const strip = document.querySelector(".apg-strip--warn[role='alert']");
      if (strip) strip.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      openWizard();
    }
  }
});
