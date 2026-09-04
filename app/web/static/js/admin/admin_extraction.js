/* Extracted from admin_extraction.html (perf/externalization follow-up,
   TCRD-296 synthesis, 2026-09-03) — was inlined, is now a normal cached
   static asset, versioned by static_url()'s ?v=<mtime> cache-buster, same
   move data_sources_extraction_observability.js made a day earlier. */

// Scope: "active" (default — only connections with a run CURRENTLY
// running, the "is it on pace right now" view) or "all" (every SharePoint
// connection, idle ones included). Mirrors the API's own `?active=1` /
// `?all=1` query params one-to-one.
let extScope = "active";
let extTimer = null;
let extInFlight = false;

// The last successfully rendered body — kept so a button click can redraw
// the table IMMEDIATELY (its own in-flight/disabled state) without waiting
// for the next scheduled poll, the same way `renderTable` already will once
// that poll lands.
let extLastBody = null;

// Per-connection in-flight guards for the three reprocessing actions below
// (TCRD-296) — belt and braces against a double-click; the server's own
// per-connection idempotency key already refuses an overlapping run.
const extPending = {};

function _extPendingFor(connId) {
  if (!extPending[connId]) extPending[connId] = { failed: false, empty: false, rerun: false, cancel: false };
  return extPending[connId];
}

// A small transient per-row message (queued count, or a typed refusal) —
// this page has no toast component of its own, so the Actions cell carries
// its own one-line result instead. Naturally cleared by the next poll's
// full table rebuild (~5s), same lifetime a toast would have had.
const extActionMsg = {};

/* &, < and > are what a text node needs; the three attribute call sites below
   (`title="${esc(...)}"` twice, `id="ext-drawer-${esc(...)}"`) need both quote
   forms as well, and a `"` one character short of escaped is an attribute
   break-out — `run.error` carries the crawl's own failure text, which quotes
   the Graph path or response it choked on, so `" onmouseover="…` in a stored
   error would run for the next admin who opens this page (the dashboard CSP
   does not block inline handlers). One helper correct in both contexts rather
   than two an author has to choose between: the entities render back as `"`
   and `'` in text position, so the escaping costs the text sites nothing.
   Same call the equivalent helper in `admin_marketplaces.html` already makes
   after the same finding; written as the pure-regex form `builder_shell.js`,
   `chat.js` and `chat_onboarding.js` use, which needs no DOM and so is
   directly node-executable by its test. */
const ESC_ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC_ENTITIES[c]);
}

/* A FastAPI `detail` is a string on some paths and a structured object on
   others (`{error, message, …}`) — see the same reader in
   data_sources_page.js, duplicated here (this page loads no shared JS
   module) rather than reached across scripts. */
function detailMessage(body, fallback) {
  const detail = body && body.detail;
  if (detail && typeof detail === "object") return detail.message || detail.error || fallback;
  return detail || fallback;
}

function fmtAgo(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return Math.round(seconds) + "s ago";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  return Math.floor(seconds / 3600) + "h ago";
}

function fmtRate(rate) {
  return rate == null ? "—" : rate.toFixed(1);
}

// D.16 — `row.next_run_at` (`app/api/admin_extraction.py::
// _crawl_schedule_next_run_at`, best-effort display only): `null` means
// this connection is `off`, OR the instance-wide sweep itself has no
// cadence configured at all (the instance switch is what turns the sweep
// on — see that function's own docstring). Either way "not scheduled" is
// the honest read, not a blank cell.
function fmtNextRun(iso) {
  return iso ? new Date(iso).toLocaleString() : "not scheduled";
}

// Cost-truth fix — `usd` is `null` for BOTH "no_usage" (nothing recorded
// anywhere for this connection) and "unpriced" (tokens known, no model
// this can honestly price) `cost_status` values; `status` distinguishes
// the two in the tooltip (see `costTitle`). A GENUINE `$0.00` (`usd === 0`,
// `cost_status: "priced"`) renders as currency, never collapsed into the
// same em-dash a missing figure gets — that conflation is exactly the bug
// this fix closes (row.estimated_cost_usd used to default to `0` for a
// connection whose crawl run itself had no usage, even when it had real
// spend attributed through `facts_ingest_runs` instead).
function fmtCost(usd, status) {
  if (usd != null) return "$" + usd.toFixed(4);
  return status === "unpriced" ? "— (unpriced)" : "—";
}

// The cost cell's own tooltip — names WHY a figure is missing, or which
// model(s) it was priced at so it can be re-derived (see
// `_fleet_row_cost`'s docstring on the API side).
function costTitle(status, models) {
  const names = (models && models.length) ? models.join(", ") : "";
  if (status === "no_usage") return "No LLM usage recorded for this connection yet.";
  if (status === "unpriced") {
    return names
      ? `Tokens recorded but the model(s) reported (${names}) could not be priced.`
      : "Tokens recorded but no model was reported, so this cannot be priced.";
  }
  return names ? `Priced at ${names} rates.` : "";
}

// Shared-collection cost marker (double-count fix) — `row.cost_shared`
// (`app/api/admin_extraction.py::fleet_extraction_runs`) is true when at
// least one facts_ingest_runs run counted into THIS row's own
// estimated_cost_usd is ALSO attributed to another connection (a bulk-add
// shared collection, or a collection consolidation — see
// `_collection_still_referenced`). The row's own figure stays a full,
// un-split attribution on purpose (see the API docstring for why a
// proportional split was rejected), so the badge is what keeps that
// visible ON SCREEN rather than only in a repository docstring — the page
// TOTAL de-duplicates by run id, but a reader scanning individual rows
// must be able to tell the SAME dollars appear on more than one of them.
function sharedCostBadgeHtml(row) {
  if (!row.cost_shared) return "";
  const withNames = (row.cost_shared_with && row.cost_shared_with.length) ? row.cost_shared_with.join(", ") : "another connection";
  return ` <span class="badge badge--warn ext-cost-shared" title="${esc("Includes cost also attributed to a shared collection with: " + withNames + ". The fleet total below counts it once, not once per connection.")}">shared</span>`;
}

/* The fleet table's ERROR cell (TCRD-296 gap #68): a run-level error always
   wins (the crawl itself broke), otherwise a scan-OCR pause — which is NOT
   a run error, the crawl keeps going and documents land in `convert_empty`
   — reads as "OCR: paused — provider refused (<reason>)" off `run.scan_ocr.
   disabled_reason` (`app/api/admin_extraction.py::_run_out`, mirrored by
   the CLI's own `_fmt_error_cell`). */
function fmtErrorCell(run) {
  if (!run) return "";
  if (run.error) return run.error;
  const disabledReason = run.scan_ocr && run.scan_ocr.disabled_reason;
  return disabledReason ? `OCR: paused — provider refused (${disabledReason})` : "";
}

// TCRD-296 gap #67 — a partitioned facts pass's ETA, "~Xm" / "~Xh Ym".
function fmtEta(seconds) {
  if (seconds == null) return null;
  if (seconds < 60) return "<1m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `~${hours}h ${minutes}m` : `~${minutes}m`;
}

// `row.token_totals` (cost-truth fix, `app/api/admin_extraction.py::
// _fleet_row_cost`) — already the SERVER-COMBINED sum across BOTH cost
// sources: the crawl run's own inline `usage` (stage-keyed — `ner`/`ocr`/
// `facts`, populated only once the run has finished) AND this
// connection's attributable slice of `facts_ingest_runs` (the standalone
// `sharepoint-facts-extraction` job's own ledger). This cell must never
// re-derive that sum client-side from `run.usage` alone — that was the
// bug: a connection whose facts stage ran only through the standalone job
// had an empty `run.usage` and read as "—" here even with real spend
// elsewhere.
function tokenTotals(totals) {
  const inTok = (totals && totals.input_tokens) || 0;
  const outTok = (totals && totals.output_tokens) || 0;
  return inTok || outTok ? `${inTok.toLocaleString()} / ${outTok.toLocaleString()}` : "—";
}

// TCRD-296 gap #67 — "3/4 passes running, 1,400 docs/h, ETA ~40m", appended
// to `factsCell`'s own line below. Empty string when there is nothing to
// add: a single-partition (or never-run) connection reads exactly as it did
// before this feature existed.
function factsThroughputNote(facts) {
  const parts = [];
  if (facts.facts_passes_total != null && facts.facts_passes_total > 1) {
    parts.push(`${facts.facts_passes_running ?? 0}/${facts.facts_passes_total} passes running`);
  }
  if (facts.facts_docs_per_hour != null) {
    parts.push(`${facts.facts_docs_per_hour.toLocaleString()} docs/h`);
  }
  const eta = fmtEta(facts.facts_eta_seconds);
  if (eta != null) parts.push(`ETA ${eta}`);
  return parts.length ? ` <span class="ext-sub">(${parts.join(", ")})</span>` : "";
}

function factsCell(facts) {
  if (!facts) return "—";
  const done = facts.docs_done;
  const throughput = factsThroughputNote(facts);
  if (facts.phase_active) {
    const total = facts.docs_total != null ? facts.docs_total : "?";
    const pending = facts.docs_total != null && done != null ? Math.max(facts.docs_total - done, 0) : "?";
    return `${done ?? 0} / ${total} <span class="ext-sub">(${pending} pending)</span>${throughput}`;
  }
  if (done != null) {
    // A pass that finished walked its whole corpus this run — nothing left
    // pending FROM THIS PASS. Skipped/failed documents are their own
    // counts, not represented as "pending" (they were looked at, not
    // deferred).
    // `orphans_swept` (TCRD-296 C.12) — the pass's own single end-of-pass
    // sweep count. Shown only when non-null and non-zero, so an ordinary
    // finished pass with nothing to sweep reads exactly as it did before
    // this field existed.
    const swept = facts.orphans_swept;
    const sweptNote =
      swept != null && swept > 0
        ? ` <span class="ext-sub">(${swept} orphan${swept === 1 ? "" : "s"} swept)</span>`
        : "";
    return `${done} <span class="ext-sub">(0 pending)</span>${sweptNote}${throughput}`;
  }
  return throughput ? `— ${throughput}` : "—";
}

function phaseCell(run) {
  if (!run) return '<span class="badge">idle</span>';
  const outcome = run.outcome;
  const phase = run.phase || "crawl";
  const cls = outcome === "failed" ? "badge--danger"
    : outcome === "stalled" ? "badge--warn"
    : outcome === "running" ? "badge--info"
    : outcome === "interrupted" ? "badge--warn"
    : "badge--success";
  // 2026-09-04 finding #65 item 3: a large site used to plan for 20+
  // minutes with NOTHING to show here (no run row existed yet). Once the
  // parent row opens as `phase="planning"` before any Graph call, name how
  // far it got instead of a bare "planning" label.
  let sub = phase;
  if (phase === "planning" && run.planning_progress) {
    const done = run.planning_progress.folders_done || 0;
    const total = run.planning_progress.folders_total || 0;
    sub = `planning ${done}/${total} folder${total === 1 ? "" : "s"}`;
  }
  return `<span class="badge ${cls}">${esc(outcome)}</span> <span class="ext-sub">${esc(sub)}</span>`;
}

/* Every reprocessing action an operator would otherwise need the shell for
   (TCRD-296): "Retry failed (N)"/"Retry empty (N)" (this connection's own
   persisted crawl-state backlog) and "Re-run" (for a connection whose most
   recent run did not finish cleanly). Disabled while a run is live — its
   idempotency key may still hold the enqueue dedup lock either way.
   "Cancel run" (stalled-crawl-cancel fix) is the odd one out: it is offered
   for a `running`/`stalled` row (never a finished one — there is nothing to
   cancel), it is a FORCE-close rather than a queue action, and it is never
   disabled by `live` — force-closing a live run is the entire point. */
/* NOTE on the `onclick="fn('${id}')"` handlers below (and in
   `shardBadgeHtml` / `renderRow`): do NOT "fix" these by wrapping the id in
   `esc()`. An attribute value is HTML-decoded BEFORE it is parsed as JS, so
   `&#39;` arrives at the parser as a real `'` and closes the string exactly
   as a bare quote would — escaping there buys nothing and reads as safety.
   What holds is the value: every id here is server-generated (`str(uuid4())`
   for a connection, `"er_" + secrets.token_hex(8)` for a run), so it cannot
   contain a quote. Interpolating anything else — a connection NAME, a label,
   an error — means moving it to a `data-` attribute read by a delegated
   listener. `tests/test_admin_extraction_escaping.py` pins both halves. */
function actionsCell(row) {
  const run = row.run;
  const live = !!(run && run.stored_status === "running");
  const pending = _extPendingFor(row.connection_id);
  const failedCount = row.failed_items_count || 0;
  const emptyCount = row.empty_items_count || 0;

  const retryFailedBtn = `<button type="button" class="btn btn-sm btn-secondary" onclick="extRetryFailed('${row.connection_id}')"
      ${live || !failedCount || pending.failed ? "disabled" : ""}>${
    pending.failed ? "Retrying…" : `Retry failed (${failedCount})`
  }</button>`;
  const retryEmptyBtn = `<button type="button" class="btn btn-sm btn-secondary" onclick="extRetryEmpty('${row.connection_id}')"
      ${live || !emptyCount || pending.empty ? "disabled" : ""}>${
    pending.empty ? "Retrying…" : `Retry empty (${emptyCount})`
  }</button>`;
  // Re-run only offers itself once there is a most-recent run that did NOT
  // finish cleanly — a `done` run has nothing to re-run FROM here.
  const canRerun = !live && !!run && (run.outcome === "failed" || run.outcome === "interrupted");
  const rerunBtn = canRerun
    ? `<button type="button" class="btn btn-sm btn-secondary" onclick="extRerun('${row.connection_id}')"
      ${pending.rerun ? "disabled" : ""}>${pending.rerun ? "Starting…" : "Re-run"}</button>`
    : "";
  const canCancel = !!run && (run.outcome === "running" || run.outcome === "stalled");
  const cancelBtn = canCancel
    ? `<button type="button" class="btn btn-sm btn-danger" onclick="extCancelRun('${row.connection_id}', '${run.id}')"
      ${pending.cancel ? "disabled" : ""}>${pending.cancel ? "Cancelling…" : "Cancel run"}</button>`
    : "";

  const msg = extActionMsg[row.connection_id];
  const msgHtml = msg ? `<div class="ext-sub ${msg.ok ? "" : "ext-danger"}">${esc(msg.text)}</div>` : "";

  return `<div class="ext-row-actions">${cancelBtn}${retryFailedBtn}${retryEmptyBtn}${rerunBtn}</div>${msgHtml}`;
}

/* "k/K shards" badge (2026-09-03 auto-parallel-crawl design §4.7) — a
   sharded site's Phase cell gains this, clickable to reveal/hide the
   per-shard disclosure row `renderShardDisclosureRow` builds. `null` for
   an ordinary (inline) run — nothing sharded, nothing to disclose. */
function shardBadgeHtml(connId, run) {
  if (!run || run.mode !== "sharded" || run.shards_total == null) return "";
  const done = run.shards_done ?? 0;
  const total = run.shards_total;
  return ` <button type="button" class="badge badge--info ext-shard-badge" onclick="toggleShardDisclosure('${connId}')" title="Sharded crawl — click to see per-shard detail">${done}/${total} shards</button>`;
}

/* Age in seconds since a shard's own `checkpoint_at` ISO timestamp — the
   disclosure row's own "Checkpoint" column reuses `fmtAgo` for the exact
   same wording the fleet row's own "Last checkpoint" column already uses. */
function _shardCheckpointAgeS(checkpointAt) {
  if (!checkpointAt) return null;
  const then = new Date(checkpointAt).getTime();
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 1000);
}

function shardRowHtml(shard) {
  const outcome = shard.outcome || "running";
  const cls = outcome === "failed" ? "badge--danger"
    : outcome === "stalled" ? "badge--warn"
    : outcome === "running" ? "badge--info"
    : outcome === "interrupted" ? "badge--warn"
    : "badge--success";
  // Plan counts are approximate (Graph Search index lag) — "≈", never a
  // bare number that would read as exact. `null` (no persisted plan found
  // for this shard, e.g. after a resync re-planned) says "≈ ?" honestly
  // rather than a fabricated 0.
  const expected = shard.expected == null ? "≈ ?" : `≈ ${Number(shard.expected).toLocaleString()}`;
  const filesDone = Number(shard.files_done || 0).toLocaleString();
  const filesSeen = Number(shard.files_seen || 0).toLocaleString();
  const stuckFlag = shard.stuck
    ? ' <span class="badge badge--danger" title="Checkpoint is stale — go look">Stuck?</span>'
    : "";
  return `
    <tr class="${shard.stuck ? "ext-row--stuck" : ""}">
      <td>${esc(shard.label != null ? shard.label : `shard ${shard.index}`)}</td>
      <td><span class="badge ${cls}">${esc(outcome)}</span>${stuckFlag}</td>
      <td class="ext-num">${filesDone} / ${filesSeen}</td>
      <td class="ext-num">${expected}</td>
      <td class="ext-sub">${fmtAgo(_shardCheckpointAgeS(shard.checkpoint_at))}</td>
      <td>${shard.error ? `<span class="ext-error-cell" title="${esc(shard.error)}">${esc(shard.error)}</span>` : ""}</td>
    </tr>`;
}

/* One colspan row per sharded connection, hidden by default, toggled by
   the Phase cell's own "k/K shards" badge — `null` (nothing appended) for
   an inline run or one whose `shards[]` was never fetched (only the fleet
   endpoint's own `children_for` batch populates it — see `_run_out`'s
   `children=` contract server-side). */
function renderShardDisclosureRow(row) {
  const run = row.run;
  if (!run || run.mode !== "sharded" || !run.shards || !run.shards.length) return null;
  const tr = document.createElement("tr");
  tr.id = `ext-shard-disclosure-${row.connection_id}`;
  tr.className = "ext-shard-disclosure";
  tr.hidden = true;
  tr.innerHTML = `
    <td colspan="12">
      <table class="data-table ext-shard-table">
        <thead>
          <tr><th>Shard</th><th>Outcome</th><th>Files done/seen</th><th>Expected</th><th>Checkpoint</th><th>Error</th></tr>
        </thead>
        <tbody>${run.shards.map(shardRowHtml).join("")}</tbody>
      </table>
    </td>`;
  return tr;
}

function toggleShardDisclosure(connId) {
  const tr = document.getElementById(`ext-shard-disclosure-${connId}`);
  if (tr) tr.hidden = !tr.hidden;
}

function renderRow(row) {
  const run = row.run;
  const filesDone = run ? (run.files_done ?? 0) : null;
  const filesSeen = run ? (run.files_seen ?? 0) : null;
  const costTip = costTitle(row.cost_status, row.cost_models);
  // `extraction.crawl.min_modified` age filter — an operator scanning the
  // fleet table must be able to tell a connection's cutoff is doing
  // something without opening its source card. Absent/zero says nothing
  // rather than a "0 filtered by age" that reads as a claim.
  const filteredByAge = run && run.filtered_by_age
    ? ` <span class="ext-sub">(${run.filtered_by_age.toLocaleString()} filtered by age)</span>`
    : "";
  const tr = document.createElement("tr");
  tr.className = row.stuck ? "ext-row--stuck" : "";
  tr.innerHTML = `
    <td>
      <div class="ext-conn">${esc(row.connection_name || row.connection_id)}</div>
      <div class="ext-sub">${esc(row.connection_id)}</div>
    </td>
    <td>${phaseCell(run)}${row.stuck ? ' <span class="badge badge--danger" title="Checkpoint is stale — go look">Stuck?</span>' : ""}${shardBadgeHtml(row.connection_id, run)}</td>
    <td class="ext-num">${filesDone == null ? "—" : `${filesDone.toLocaleString()} / ${filesSeen.toLocaleString()}${filteredByAge}`}</td>
    <td class="ext-num">${fmtRate(row.files_per_min)}</td>
    <td class="ext-num">${factsCell(row.facts)}</td>
    <td class="ext-num">${tokenTotals(row.token_totals)}</td>
    <td class="ext-num"${costTip ? ` title="${esc(costTip)}"` : ""}>${fmtCost(row.estimated_cost_usd, row.cost_status)}${sharedCostBadgeHtml(row)}</td>
    <td class="ext-sub">${fmtAgo(row.checkpoint_age_s)}</td>
    <td class="ext-sub">${fmtNextRun(row.next_run_at)}</td>
    <td>${(() => { const cell = fmtErrorCell(run); return cell ? `<span class="ext-error-cell" title="${esc(cell)}">${esc(cell)}</span>` : ""; })()}</td>
    <td>${actionsCell(row)}</td>
    <td><button type="button" class="btn btn-sm btn-secondary" onclick="openFleetCompleteness('${row.connection_id}')">Completeness</button></td>
  `;
  return tr;
}

// "Did we really get everything?" (TCRD-296 B.9) — re-homes the ONE shared
// drawer shell to this connection's id so
// data_sources_extraction_observability.js's `toggleExtractionDrawer` /
// `_extCompletenessHtml` / `extRecountCompleteness` / `extSortCompleteness`
// (the SAME functions the source card's drawer uses) render it verbatim.
// Switching to a different connection's drawer simply re-homes the shell —
// only one completeness drawer is open on this page at a time.
let extFleetOpenConnId = null;

function openFleetCompleteness(connId) {
  const shell = document.getElementById("ext-fleet-completeness-shell");
  if (extFleetOpenConnId === connId) {
    // Same connection clicked again: let toggleExtractionDrawer's own
    // open/close logic decide (it closes when already open+visible).
    toggleExtractionDrawer(connId, "completeness");
    return;
  }
  extFleetOpenConnId = connId;
  shell.innerHTML = `<div class="ext-drawer" id="ext-drawer-${esc(connId)}" hidden></div>`;
  toggleExtractionDrawer(connId, "completeness");
}

/* The queued-vs-running lane-starvation strip (TCRD-296 synthesis item B.6):
   `jobs` is `{kind: {queued, running}}` for the extraction pipeline's own
   worker lanes — visible without SQL, and independent of the table's own
   scope (a starved job has no `extraction_runs` row yet, so it would never
   show up as a table row at all). A lane with `queued > 0` and `running ===
   0` is flagged — every worker slot busy elsewhere, or none configured for
   this lane. */
function renderJobsStrip(jobs) {
  const el = document.getElementById("ext-jobs-strip");
  if (!el) return;
  const kinds = Object.keys(jobs || {});
  if (!kinds.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = kinds
    .map((kind) => {
      const c = jobs[kind] || { queued: 0, running: 0 };
      const starved = (c.queued || 0) > 0 && (c.running || 0) === 0;
      const cls = starved ? "ext-jobs-strip__item ext-jobs-strip__item--starved" : "ext-jobs-strip__item";
      const flag = starved ? ' <span class="badge badge--warn" title="Queued but nothing running for this lane">starved?</span>' : "";
      return `<span class="${cls}"><strong>${esc(kind)}</strong>: ${c.queued || 0} queued / ${c.running || 0} running${flag}</span>`;
    })
    .join("");
}

/* "Facts extraction paused" vs "OCR extraction paused" (TCRD-296 gap #68) —
   the ONLY thing distinguishing an OCR-authored condition from a
   facts-authored one in that shared, kindless table is the `ocr_` prefix
   scan OCR puts on its own `reason`
   (`src.ingest.scan_ocr._mark_run_disabled`). */
function conditionLabel(c) {
  const reason = String((c && c.reason) || "");
  return reason.startsWith("ocr_") ? "OCR extraction paused" : "Facts extraction paused";
}

/* Fleet-level provider-refusal banner (TCRD-296 synthesis F.25, gaps
   #25/#48/#68) — one strip per active condition, e.g. a workspace usage-limit
   exhaustion or a saturated Vertex region×model quota bucket. Additive:
   `conditions` is `[]` on every instance before this shipped and forever
   on a DuckDB-backed one, so the strip simply stays hidden. This is the
   SAME condition list the crawl's own streamed trigger
   (`crawler._enqueue_streamed_facts_pass`) checks before enqueueing — the
   banner is the operator-facing signal, never the enforcement itself. */
function renderProviderLimitBanner(conditions) {
  const el = document.getElementById("ext-provider-limit-banner");
  if (!el) return;
  const list = conditions || [];
  if (!list.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = list
    .map((c) => {
      const scope = [c.model, c.region].filter(Boolean).join(" in ") || "";
      const retryHint = c.retry_after_s
        ? `retrying in ${Math.max(1, Math.round(c.retry_after_s / 60))}m`
        : "retrying automatically once the condition clears";
      return (
        `<div>${esc(conditionLabel(c))}: ${esc(c.provider || "provider")}` +
        `${scope ? " " + esc(scope) : ""} — ${esc(c.message || c.reason || "provider limit")}; ${esc(retryHint)}.</div>`
      );
    })
    .join("");
}

function renderTable(body) {
  extLastBody = body;
  renderProviderLimitBanner(body.conditions);
  const tbody = document.getElementById("ext-tbody");
  const rows = body.connections || [];
  if (!rows.length) {
    const msg = extScope === "active"
      ? "No SharePoint connection currently has a run in progress."
      : "No SharePoint connections are registered.";
    tbody.innerHTML = `<tr><td colspan="12" class="ext-blank">${msg}</td></tr>`;
  } else {
    tbody.innerHTML = "";
    for (const row of rows) {
      tbody.appendChild(renderRow(row));
      const disclosure = renderShardDisclosureRow(row);
      if (disclosure) tbody.appendChild(disclosure);
    }
  }

  const t = body.totals || {};
  document.getElementById("ext-summary").hidden = false;
  document.getElementById("ext-stat-active").textContent = t.active ?? 0;
  document.getElementById("ext-stat-rate").textContent = fmtRate(t.files_per_min);
  document.getElementById("ext-stat-facts").textContent = (t.facts_docs_done ?? 0).toLocaleString();
  const stuckEl = document.getElementById("ext-stat-stuck");
  stuckEl.textContent = t.stuck ?? 0;
  stuckEl.classList.toggle("danger", (t.stuck || 0) > 0);
  document.getElementById("ext-stat-cost").textContent = fmtCost(t.estimated_cost_usd);
  // Shared-collection double-count fix — `totals.cost_note` states in the
  // response itself what this de-duplicated figure sums (see
  // `app/api/admin_extraction.py::fleet_extraction_runs`'s docstring); the
  // tile surfaces that as its own tooltip rather than leaving a reader to
  // guess why it can differ from a naive sum of the rows' own cost cells.
  const costCard = document.getElementById("ext-card-cost");
  if (costCard) costCard.title = t.cost_note || "";
  // Cost-truth fix — the SAME instance-wide cumulative rollup `GET
  // /api/facts/ingest-runs` already exposes (`llm_usage_totals`), riding
  // on this response too so the summary strip can show it: everything
  // this instance has EVER spent on fact extraction, not just what this
  // page's own (possibly `?active=1`-scoped) rows total.
  const cumulativeEl = document.getElementById("ext-stat-cumulative-cost");
  if (cumulativeEl) cumulativeEl.textContent = fmtCost((body.llm_usage_totals || {}).estimated_cost_usd);

  renderJobsStrip(body.jobs);

  document.getElementById("ext-fresh").textContent = "Updated " + new Date().toLocaleTimeString();
}

function setScope(scope) {
  extScope = scope;
  document.getElementById("ext-scope-active").classList.toggle("active", scope === "active");
  document.getElementById("ext-scope-all").classList.toggle("active", scope === "all");
  return extTick();
}

async function extTick() {
  if (extInFlight) return;
  if (document.visibilityState !== "visible") return;
  extInFlight = true;
  try {
    const qs = extScope === "all" ? "?all=1" : "?active=1";
    const r = await fetch("/api/admin/sharepoint/extraction/runs" + qs, { credentials: "include" });
    if (r.status === 501) {
      document.getElementById("ext-pg-unavailable").hidden = false;
      document.getElementById("ext-toolbar").hidden = true;
      document.getElementById("ext-summary").hidden = true;
      document.getElementById("ext-tbody").innerHTML = "";
      const strip = document.getElementById("ext-jobs-strip");
      if (strip) strip.hidden = true;
      const banner = document.getElementById("ext-provider-limit-banner");
      if (banner) banner.hidden = true;
      return;
    }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    renderTable(body);
  } catch (e) {
    document.getElementById("ext-fresh").textContent = "Refresh failed: " + e.message;
  } finally {
    extInFlight = false;
  }
}

function extSchedule() {
  if (extTimer) clearTimeout(extTimer);
  extTimer = setTimeout(async () => { await extTick(); extSchedule(); }, 5000);
}

/* Shared body for the three reprocessing actions (TCRD-296): POST, flip the
   per-connection in-flight guard, redraw immediately from the cached body
   (so the click's own button locks without waiting on the network), read
   the queued count off the SAME 202 body the server computed it from, and
   let the very next scheduled poll (already running every 5s on this page)
   carry the real outcome. A 409 means the per-connection idempotency key
   already refused an overlapping run — rendered as one readable line, not
   a raw JSON blob. */
async function _extFleetTriggerAction(connId, pendingKey, url, requestBody, successPrefix) {
  const pending = _extPendingFor(connId);
  pending[pendingKey] = true;
  delete extActionMsg[connId];
  if (extLastBody) renderTable(extLastBody);
  try {
    const r = await fetch(url, {
      method: "POST",
      credentials: "include",
      ...(requestBody ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody) } : {}),
    });
    const body = await r.json().catch(() => ({}));
    if (r.status === 202) {
      const count = typeof body.queued_count === "number" ? ` (${body.queued_count} queued)` : "";
      extActionMsg[connId] = { text: `${successPrefix}${count}.`, ok: true };
    } else {
      extActionMsg[connId] = { text: String(detailMessage(body, "request failed")), ok: false };
    }
  } catch (e) {
    extActionMsg[connId] = { text: "Request failed.", ok: false };
  } finally {
    pending[pendingKey] = false;
    if (extLastBody) renderTable(extLastBody);
    await extTick();
  }
}

function extRetryFailed(connId) {
  return _extFleetTriggerAction(
    connId,
    "failed",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extract`,
    { retry_failed: true },
    "Retry failed queued",
  );
}

function extRetryEmpty(connId) {
  return _extFleetTriggerAction(
    connId,
    "empty",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/retry-empty`,
    null,
    "Retry empty queued",
  );
}

function extRerun(connId) {
  return _extFleetTriggerAction(
    connId,
    "rerun",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extract`,
    null,
    "Extraction queued",
  );
}

/* Force-close a run Stop alone cannot reach — a genuinely stuck crawl loop
   never notices the cooperative stop flag either (the 2026-09-02 incident:
   an hour of `running` with no checkpoint, ended by hand via two SQL
   updates and a re-trigger). A confirm dialog (confirmModal/alertModal —
   modal.js, globally loaded — never the native browser dialog, see
   tests/test_design_system_contract.py's native-dialog guard) since this
   force-closes work that may still be in flight. A 200 means the row is
   closed server-side IMMEDIATELY (never waiting on the crawl to notice) —
   redraw from the cached body first (own `pending` lock, same shape the
   three reprocessing actions above use), then let the next poll carry the
   real `interrupted` outcome. */
async function extCancelRun(connId, runId) {
  const ok = await confirmModal(
    "Cancel this extraction run? This force-closes it even if the worker never reacts — what it " +
      "already ingested is kept, but the run itself will not finish on its own."
  );
  if (!ok) return;
  const pending = _extPendingFor(connId);
  pending.cancel = true;
  delete extActionMsg[connId];
  if (extLastBody) renderTable(extLastBody);
  try {
    const r = await fetch(`/api/admin/sharepoint/extraction/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      credentials: "include",
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      extActionMsg[connId] = { text: "Run cancelled.", ok: true };
    } else {
      await alertModal(`Cancel failed: ${detailMessage(body, "couldn't cancel the run")}`);
    }
  } catch (e) {
    await alertModal(`Request failed: ${e.message}`);
  } finally {
    pending.cancel = false;
    if (extLastBody) renderTable(extLastBody);
    await extTick();
  }
}

document.getElementById("ext-scope-active").addEventListener("click", (e) => { e.preventDefault(); setScope("active"); });
document.getElementById("ext-scope-all").addEventListener("click", (e) => { e.preventDefault(); setScope("all"); });

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { extTick(); extSchedule(); }
});

setScope("active");
extSchedule();
