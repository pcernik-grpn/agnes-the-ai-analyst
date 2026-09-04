/* Extracted from admin_data_sources.html (perf follow-up, 2026-09-03) — was inlined on every page load (uncacheable); now a normal cached static asset, versioned by static_url()'s ?v=<mtime> cache-buster. */
const EXT_POLL_ACTIVE_MS = 3000;
const EXT_POLL_IDLE_MS = 30000;
const EXT_POLL_MAX_BACKOFF_MS = 60000;
const EXT_MAX_FAILURES = 3;

/* Per-connection poll state. `stopped` is terminal (a 501): nothing
   re-arms it short of a page reload, because the answer will not change
   until the instance changes backend. */
const _extState = {};
let _extTimer = null;
let _extInFlight = false;

function _extS(id) {
  if (!_extState[id]) {
    _extState[id] = {
      failures: 0,
      stopped: false,
      data: null,
      lastOk: null,
      error: null,
      stopping: false,
      // One in-flight guard per reprocessing action, so a slow request
      // can't be fired twice from a double-click — mirrors `stopping`
      // above, which does the same job for the Stop button.
      retryingFailed: false,
      retryingEmpty: false,
      rerunning: false,
      cancelling: false,
    };
  }
  return _extState[id];
}

function _extEsc(v) {
  return typeof _esc === "function" ? _esc(v == null ? "" : String(v)) : String(v == null ? "" : v);
}

function _extTime(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "unknown" : d.toLocaleString();
}

function _extDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function _extNum(n) {
  return Number(n || 0).toLocaleString();
}

/* Middle-truncation keeps a long SharePoint path on one line without hiding
   which file it is — the START (library/folder) and the END (filename) are
   the two ends an operator actually reads; the middle is what a `…` can
   safely eat. The untruncated string still travels in a `title` attribute. */
function _extTruncMiddle(path, max) {
  const s = String(path || "");
  if (s.length <= max) return s;
  const keep = Math.max(0, max - 1);
  const head = Math.ceil(keep * 0.6);
  const tail = keep - head;
  return `${s.slice(0, head)}…${tail > 0 ? s.slice(s.length - tail) : ""}`;
}

/* One dot per outcome. `running` is the ONLY animated one — a run past its
   checkpoint window is `stalled` and gets a static warn dot instead. */
const EXT_DOT = {
  running: "ext-dot--live",
  stalled: "ext-dot--warn",
  interrupted: "ext-dot--warn",
  failed: "ext-dot--danger",
  done: "ext-dot--ok",
};

function _extDot(outcome) {
  return `<span class="ext-dot ${EXT_DOT[outcome] || "ext-dot--idle"}" aria-hidden="true"></span>`;
}

/* Whether a live run is currently in the facts (LLM graph-extraction)
   phase rather than the crawl phase — read from `activity.phase`, which
   rides the SAME checkpoint `run.files_done` does (owner-frustration fix,
   2026-09-02: a healthy multi-hour facts pass read "stalled · 24 files"
   for its whole run because nothing distinguished "the crawl is still
   going" from "the crawl finished ages ago and a different, much longer
   stage is"). Older rows (recorded before this field existed) carry no
   `activity` at all and fall back to the crawl wording — never a claim
   this function cannot back up. */
function _extIsFactsPhase(run) {
  return !!(run && run.activity && run.activity.phase === "facts");
}

/* "<done>/<total> documents" for the facts phase, or the crawl's own
   "<files> files" everywhere else. `docs_total` is a GROWING count (see
   `facts_progress`'s doc comment server-side) — shown plainly, without a
   percentage or a bar, the same "no fraction over a moving denominator"
   rule the crawl's own file counts already follow. */
function _extPhaseCountText(run) {
  if (_extIsFactsPhase(run) && run.facts_progress) {
    return `${_extNum(run.facts_progress.docs_done)}/${_extNum(run.facts_progress.docs_total)} documents`;
  }
  return `${_extNum(run.files_done)} files`;
}

/* The pipeline strip's Crawl cell gains a live line ONLY while there is a
   live run to describe; otherwise it stays exactly the document count it
   always was. */
function _extRenderCrawlCell(connId, status) {
  const el = document.getElementById(`ext-crawl-live-${connId}`);
  if (!el) return;
  const run = status && status.running;
  if (!run) { el.hidden = true; el.innerHTML = ""; return; }
  const label = run.outcome === "stalled" ? "stalled" : (_extIsFactsPhase(run) ? "extracting facts" : "crawling");
  el.hidden = false;
  el.innerHTML = `${_extDot(run.outcome)}<span class="ext-sub">${_extEsc(label)} · ${_extPhaseCountText(run)}</span>`;
}

/* The "In-Agnes extraction" fact row's own trigger button is server-
   rendered ONCE from `config.extraction.last_run_at` dispatch bookkeeping —
   that row's job is cadence/last-dispatch, not live run state, so it has no
   reason to grow its own poll. But its one verb, "Run extraction now", must
   never stay offered while a run is already in flight: the server can only
   ever answer that click with `409 extraction_already_running`. Reusing the
   SAME status this script already polls for the Run row above (rather than
   opening a second live-state channel for one button) keeps that promise.
   `data-extraction-ready` remembers the server's own capability gate
   (producer configured, `sharepoint.enabled`) so a run ending restores that
   verdict instead of blindly re-enabling a button that was never allowed to
   begin with. */
function _extRenderInAgnesButton(connId, status) {
  const btn = document.getElementById(`ext-inagnes-btn-${connId}`);
  if (!btn) return;
  const ready = btn.dataset.extractionReady === "1";
  const running = !!(status && status.running);
  btn.disabled = !ready || running;
  btn.title = running
    ? "An extraction is already running for this connection — see the Run row above, or stop it there."
    : "";
}

/* The standalone facts pass is a JOB (`sharepoint-facts-extraction`), not
   a crawl run — it opens no `extraction_runs` row — so the status endpoint
   carries it as `facts_job` off the job queue. One line in the Run row
   while it is queued or running; nothing at all otherwise (`null` means
   "no pass in flight", never a stale or guessed one). */
function _extFactsJobLine(job) {
  if (!job) return "";
  const running = job.status === "running";
  const since = running && job.started_at ? job.started_at : job.created_at;
  const sinceText = since ? ` · since ${_extEsc(_extTime(since))}` : "";
  return `<div class="ext-sub">${_extDot(running ? "running" : "idle")}facts pass <strong>${running ? "running" : "queued"}</strong> · job ${_extEsc(job.id)}${sinceText}</div>`;
}

/* TCRD-296 gap #61: a pass that stopped on its own time budget with
   documents still pending used to leave nothing visible here once the
   crawl that triggered it was long over — an operator had to notice a
   stale corpus and re-trigger by hand every couple hours. `facts_pass_
   running` is server-computed (`facts_job` off the same status payload,
   not re-derived here), so "continuing" here means a job — auto-chained
   or manual — is actually queued/running for it right now, and "not
   running" is the honest gap. Renders only when there is at least one
   pending document (`0`/`null` say nothing, matching every other line's
   "absence is the honest answer" convention here). */
function _extFactsPendingLine(status) {
  // TCRD-296 synthesis F.25 — a fleet-level provider refusal wins over the
  // ordinary "continuing"/"not running" wording: an operator seeing this
  // needs to know WHY nothing is chasing the backlog, not just that
  // nothing currently is (which "not running" alone would also say for an
  // unrelated reason, e.g. the continuation chain hit its cap).
  const limit = status && status.provider_limit;
  if (limit) {
    const provider = _extEsc(limit.provider || "provider");
    return `<div class="ext-sub ext-warn">paused: provider limit (${provider} — ${_extEsc(limit.reason || "")})</div>`;
  }
  const n = status && status.facts_pending_documents;
  if (n === null || n === undefined || n <= 0) return "";
  const continuing = !!status.facts_pass_running;
  const cls = continuing ? "" : " ext-warn";
  const state = continuing ? "continuing" : "not running";
  return `<div class="ext-sub${cls}">${_extNum(n)} document${n === 1 ? "" : "s"} pending facts extraction · ${state}</div>`;
}

function _extRunLine(run) {
  const bits = [];
  if (run.new != null) bits.push(`${_extNum(run.new)} new`);
  if (run.changed != null) bits.push(`${_extNum(run.changed)} changed`);
  if (run.unchanged != null) bits.push(`${_extNum(run.unchanged)} unchanged`);
  if (run.renamed) bits.push(`${_extNum(run.renamed)} renamed`);
  if (run.deleted) bits.push(`${_extNum(run.deleted)} deleted`);
  // `extraction.crawl.min_modified` age filter — so an operator can tell
  // mid-run whether the cutoff is doing anything, not only after the run
  // finishes. Absent/zero (no cutoff configured, or nothing skipped) says
  // nothing rather than a "0 filtered by age" that reads as a claim.
  if (run.filtered_by_age) bits.push(`${_extNum(run.filtered_by_age)} filtered by age`);
  if (run.bytes_downloaded_human) bits.push(`${run.bytes_downloaded_human} downloaded`);
  // NER token accounting (usage or the report's ner_usage): a token COUNT is a
  // fact, and `estimated_cost_usd` (src.llm_pricing.cost_usd, when the stage
  // priced its own spend) is shown next to it. A stage that has not been
  // priced yet says "not priced" rather than inventing a dollar figure.
  // Absence of the whole block means "no tokens spent".
  // `usage` is keyed by stage ({ner: {...}, facts: {...}}); a flat token
  // dict (the pre-stage-keyed shape) and `run.ner_usage` stay readable so
  // rows recorded before the contract change keep rendering their spend.
  const usage = run.usage && Object.keys(run.usage).length ? run.usage : (run.ner_usage ? { ner: run.ner_usage } : null);
  if (usage) {
    const stages = (usage.input_tokens || usage.output_tokens) ? { ner: usage } : usage;
    const stageLabels = { ner: "NER", ocr: "OCR", facts: "facts" };
    for (const [stage, u] of Object.entries(stages)) {
      if (u && (u.input_tokens || u.output_tokens)) {
        const tok = (u.input_tokens || 0) + (u.output_tokens || 0);
        const priceText = typeof u.estimated_cost_usd === "number" ? `$${u.estimated_cost_usd.toFixed(4)}` : "not priced";
        bits.push(`${_extNum(tok)} ${stageLabels[stage] || stage} tokens · ${priceText}`);
      }
    }
  }
  return bits.join(" · ");
}

/* A bare slug ("throttled") tells an operator nothing about what to do
   next — the same lesson `SP_REJECTION_REASON_TEXT` above already encodes
   for rejection reasons. An unknown reason is shown VERBATIM rather than
   collapsed into a generic phrase: a slug we do not recognize is still
   information, and hiding it would be worse than not explaining it. */
const EXT_STOP_REASON_TEXT = {
  timeout: "the run hit its time ceiling (extraction.timeout_s)",
  throttled: "the tenant's throttling budget was exhausted (HTTP 429)",
  stopped: "stopped by an admin",
  cancelled: "force-cancelled by an admin — the run did not respond to a stop request in time",
  abandoned: "the worker running it died (crashed or was killed) and never finished — closed when the next run started",
  error: "an unexpected error",
};

function _extStopReasonText(reason) {
  return EXT_STOP_REASON_TEXT[String(reason || "").trim().toLowerCase()] || reason || "";
}

function _extThrottleLine(run) {
  if (!run.http_429) return "";
  const waited = run.throttle_wait_s ? `, ${_extDuration(run.throttle_wait_s)} waited` : "";
  return `<div class="ext-sub ext-warn">throttled — ${_extNum(run.http_429)}× HTTP 429${_extEsc(waited)}</div>`;
}

/* What the crawl is touching RIGHT NOW (design §4 follow-up: "can't see what
   it's doing"). Older rows (recorded before this field existed, or a DuckDB
   instance that never got it) carry no `activity` at all — that renders
   nothing, never empty scaffolding. Every path is untrusted-ish (it is a
   filename a document owner chose), so it goes through `_extEsc` exactly
   like every other server string on this card — never raw `innerHTML`. */
function _extActivityHtml(activity) {
  if (!activity) return "";
  const bits = [];
  if (activity.phase) bits.push(`<strong>${_extEsc(activity.phase)}</strong>`);
  if (activity.current_path) {
    const shown = _extTruncMiddle(activity.current_path, 64);
    bits.push(
      `<span class="ext-activity__path" title="${_extEsc(activity.current_path)}">${_extEsc(shown)}</span>`,
    );
  }
  let html = bits.length ? `<div class="ext-sub ext-activity">${bits.join(" · ")}</div>` : "";
  const recent = Array.isArray(activity.recent) ? activity.recent.slice(0, 5) : [];
  if (recent.length) {
    const items = recent
      .map((r) => {
        const p = _extTruncMiddle(r && r.path, 40);
        return `<span class="ext-activity__recent-item">${_extDot(r && r.outcome)}<span title="${_extEsc(r && r.path)}">${_extEsc(p)}</span></span>`;
      })
      .join(" ");
    html += `<div class="ext-sub ext-activity__recent">recent: ${items}</div>`;
  }
  return html;
}

/* ── Per-file error detail: a `<details>` an operator can open, never a
   count that only means something in a container log a VM recreate has
   already wiped. The summary line renders for free (the count already
   rides every run payload); the itemized `path` / `reason` / `detail` /
   `status_code` rows are fetched on first expand from A3
   (`GET …/extraction/runs/{run_id}`, `report.errors_detail` — the same
   `{items, total, listed, truncated}` envelope `skips` uses) and cached on
   the element so re-opening never re-fetches. `run.id` is present on every
   projection this renders from (A1's `running`/`last_completed`, A2's list
   rows) — never null when `run.errors` is truthy, since a run cannot both
   exist and have no id. ────────────────────────────────────────────────── */
function _extErrorsSummaryHtml(connId, runId, errorCount) {
  return `
    <details class="ext-errors" data-conn="${_extEsc(connId)}" data-run="${_extEsc(runId)}" ontoggle="_extLoadErrorDetail(this)">
      <summary class="ext-sub ext-danger">${_extNum(errorCount)} error${errorCount === 1 ? "" : "s"} — click to see why</summary>
      <div class="ext-errors__body" data-loaded="0"></div>
    </details>`;
}

async function _extLoadErrorDetail(details) {
  if (!details.open) return;
  const body = details.querySelector(".ext-errors__body");
  if (!body || body.dataset.loaded === "1") return;
  body.dataset.loaded = "1";
  body.innerHTML = `<div class="ds-empty">Loading…</div>`;
  const connId = details.dataset.conn;
  const runId = details.dataset.run;
  try {
    const r = await fetch(
      `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/runs/${encodeURIComponent(runId)}`,
      { credentials: "include" },
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    body.innerHTML = _extErrorItemsHtml(data);
  } catch (e) {
    body.dataset.loaded = "0"; // a failed fetch may be retried on the next open
    body.innerHTML = `<div class="ds-empty">Couldn't load the per-file detail — ${_extEsc(e && e.message)}.</div>`;
  }
}

/* Reason slugs are shown verbatim rather than mapped to prose, unlike
   `EXT_STOP_REASON_TEXT` above: `download_failed` / `convert_failed` /
   `ingest_failed` are already the crawl's own vocabulary and an operator
   reading a status code right next to one needs the exact word, not a
   paraphrase. */
function _extErrorItemsHtml(runDetail) {
  const report = runDetail.report || {};
  const envelope = report.errors_detail || {};
  const items = envelope.items || [];
  let html = "";
  if (items.length) {
    const rows = items.map((item) => {
      const status = item.status_code != null ? ` · HTTP ${_extNum(item.status_code)}` : "";
      const detail = item.detail ? `: ${_extEsc(item.detail)}` : "";
      return `<li><code>${_extEsc(item.path || "(no path)")}</code> — ${_extEsc(item.reason || "error")}${status}${detail}</li>`;
    });
    const total = envelope.total || items.length;
    const note = envelope.truncated
      ? `<div class="ext-sub">${_extNum(total)} total — showing the ${_extNum(envelope.listed)} most recent.</div>`
      : "";
    html += `<ul class="ext-errors__list">${rows.join("")}</ul>${note}`;
  } else {
    html += `<div class="ds-empty">No per-file detail was recorded for this run.</div>`;
  }
  html += _extFailedOrSkippedItemsHtml(
    report.failed_items,
    report.failed_items_truncated,
    "Convert-stage failures — retried by a `retry_failed` run"
  );
  html += _extFailedOrSkippedItemsHtml(
    report.skipped_items,
    report.skipped_items_truncated,
    "Skipped — no conversion backend for this file type (not an error)"
  );
  return html;
}

/* Shared renderer for `report.failed_items` and `report.skipped_items` —
   the SAME flat-list shape (`path`/`item_id`/`reason_type`/`reason`/
   `suffix`), each capped independently of `errors_detail`'s own 200-item
   sample (see `connectors.sharepoint.crawler._FAILED_ITEMS_CAP` = 5000).
   `path` is `null` for an anonymize-marked scope's item — rendered as
   "(redacted)", never blank, so a reader can tell "no path was ever kept"
   from "the path happened to be empty". */
function _extFailedOrSkippedItemsHtml(items, truncated, heading) {
  if (!items || !items.length) return "";
  const rows = items
    .map((item) => {
      const path = item.path != null ? _extEsc(item.path) : `<em>(redacted)</em>`;
      return `<li><code>${path}</code> — ${_extEsc(item.reason || item.reason_type || "")}</li>`;
    })
    .join("");
  const note = truncated ? `<div class="ext-sub">list truncated — not every failure is shown.</div>` : "";
  return `<div class="ext-sub" style="margin-top:8px;"><strong>${_extEsc(heading)}</strong></div><ul class="ext-errors__list">${rows}</ul>${note}`;
}

/* "k/K shards" — the shard count segment of the `Run` row's own head line
   (2026-09-03 auto-parallel-crawl design §4.7), `""` for an ordinary
   (inline) run or one whose `shards_total` is not yet known. */
function _extShardCountText(run) {
  if (!run || run.mode !== "sharded" || run.shards_total == null) return "";
  return ` · ${_extNum(run.shards_done ?? 0)}/${_extNum(run.shards_total)} shards`;
}

/* One line per shard — label, outcome, absolute files done/seen, a live
   (never exact — Graph Search index lag) expected document count, and its
   error when it has one. Reused by the `Run` row itself (a live or last
   run) and the run-history drawer's own per-run entries — one shared
   rendering, never a second copy of it. `[]`/`undefined` (the caller never
   fetched `shards[]`, or this run was never sharded) renders nothing. */
function _extShardsHtml(shards) {
  if (!shards || !shards.length) return "";
  const rows = shards
    .map((s) => {
      const label = s.label != null ? s.label : `shard ${s.index}`;
      const expected = s.expected == null ? "≈ ?" : `≈ ${_extNum(s.expected)}`;
      const stuckFlag = s.stuck ? ` <span class="ext-warn">Stuck?</span>` : "";
      const errorLine = s.error ? ` — <span class="ext-danger">${_extEsc(s.error)}</span>` : "";
      return `<li>${_extDot(s.outcome)}<strong>${_extEsc(label)}</strong> — ${_extEsc(s.outcome)}${stuckFlag} · ${_extNum(s.files_done)}/${_extNum(s.files_seen)} files · ${_extEsc(expected)}${errorLine}</li>`;
    })
    .join("");
  return `<div class="ext-sub">shards:</div><ul class="ext-shards">${rows}</ul>`;
}

/* The `Run` fact row. Counters are absolute; the caption names the moment
   they were last true (the RUN's own checkpoint), which is not the same
   moment this page last asked. */
function _extRunRowHtml(connId, st) {
  const status = st.data;
  const run = status.running;
  // `last_failed` (a run whose OWNING JOB the worker itself marked
  // `failed` — 2026-09 incident) wins over `last_completed` when both are
  // present: the server already gates it to only exist when nothing is
  // currently running and it postdates `last_completed`, so it is always
  // the more recent, more relevant event to show here.
  const last = status.last_failed || status.last_completed;
  let head;
  let sub = "";

  if (run) {
    const verb = run.outcome === "stalled"
      ? "no longer reporting"
      : (_extIsFactsPhase(run) ? "extracting facts" : "running");
    head = `${_extDot(run.outcome)} <strong>${_extEsc(verb)}</strong>${_extShardCountText(run)} · ${_extPhaseCountText(run)} processed`;
    const started = `started ${_extEsc(_extTime(run.started_at))}`;
    const elapsed = run.elapsed_s != null ? ` · ${_extEsc(_extDuration(run.elapsed_s))} elapsed` : "";
    sub += `<div class="ext-sub">${started}${elapsed} · as of ${_extEsc(_extTime(run.checkpoint_at))}</div>`;
    const line = _extRunLine(run);
    if (line) sub += `<div class="ext-sub">${_extEsc(line)}</div>`;
    sub += _extThrottleLine(run);
    if (run.outcome === "stalled") {
      sub += `<div class="ext-sub ext-warn">${_extEsc(run.liveness_note || "no recent checkpoint")}</div>`;
    }
    if (run.activity) sub += _extActivityHtml(run.activity);
    sub += _extShardsHtml(run.shards);
    if (status.can_stop === false) {
      sub += `<div class="ext-sub">A running crawl cannot be stopped from here yet — it finishes, or the worker ends it.</div>`;
    }
  } else if (last) {
    head = `${_extDot(last.outcome)} <strong>${_extEsc(last.outcome)}</strong>${_extShardCountText(last)} · ${_extNum(last.files_done)} files`;
    sub += `<div class="ext-sub">finished ${_extEsc(_extTime(last.finished_at))}`;
    if (last.duration_s != null) sub += ` · took ${_extEsc(_extDuration(last.duration_s))}`;
    sub += `</div>`;
    const line = _extRunLine(last);
    if (line) sub += `<div class="ext-sub">${_extEsc(line)}</div>`;
    // Errors before skips, deliberately: a skip is a policy DECISION (over
    // the size cap, permissions), an error is something that FAILED. A run
    // that erred on nearly everything and ingested nothing must not read as
    // healthy just because its one visible warning happens to be 7 oversize
    // files while 1,263 others failed silently underneath it.
    if (last.errors) sub += _extErrorsSummaryHtml(connId, last.id, last.errors);
    if (last.skipped_unsupported) {
      // Never an error — nothing was attempted, so this reads as a neutral
      // fact, not a warning, distinct from the danger-toned line above.
      sub += `<div class="ext-sub">${_extNum(last.skipped_unsupported)} file${last.skipped_unsupported === 1 ? "" : "s"} skipped — unsupported type, not an error</div>`;
    }
    // The JOB-level error (2026-09 incident: an attempts-exhausted job's
    // own error text, e.g. "lease expired after max attempts") — distinct
    // from `errors` above, which is per-file detail a healthy crawl can
    // also accumulate. A `done`/`interrupted` run normally carries none.
    if (last.error) sub += `<div class="ext-sub ext-danger">${_extEsc(last.error)}</div>`;
    if (last.skips_total) {
      sub += `<div class="ext-sub ext-warn">${_extNum(last.skips_total)} document${last.skips_total === 1 ? "" : "s"} not indexed — see the run</div>`;
    }
    sub += _extShardsHtml(last.shards);
  } else {
    head = `${_extDot("idle")} never run`;
    sub = `<div class="ext-sub">No extraction run has been recorded for this connection yet.</div>`;
  }

  sub += _extFactsJobLine(status.facts_job);
  sub += _extFactsPendingLine(status);

  if (st.error) {
    sub += `<div class="ext-sub ext-danger"><span class="ext-stale">stale</span> couldn't refresh — last read ${_extEsc(_extTime(st.lastOk))}</div>`;
  }

  const total = status.runs_total || 0;
  // Cooperative, not a hard kill — the button says "Stopping…" once clicked
  // and stays disabled until the next poll shows the run actually gone,
  // never a claim it ended the instant the request was sent. Mirrors the
  // `can_stop === false` sentence above: exactly one of the two ever shows.
  const canStopNow = !!run && (run.outcome === "running" || run.outcome === "stalled") && status.can_stop !== false;
  const stopBtn = canStopNow
    ? `<button type="button" class="btn btn-sm btn-danger" onclick="extStopRun('${connId}')"
               ${st.stopping ? "disabled" : ""}>${st.stopping ? "Stopping…" : "Stop run"}</button>`
    : "";

  // Every reprocessing action an operator would otherwise need the shell
  // for (TCRD-296): retry this connection's own failed/empty backlog, or
  // re-run outright after a failed/interrupted attempt. `live` covers BOTH
  // a genuinely running row and a `stalled`/job-`failed` zombie one — its
  // idempotency key may still hold the enqueue dedup lock either way, so
  // every button below stays disabled rather than let the server's own 409
  // be the only thing standing between two overlapping runs.
  const live = !!run;
  const failedCount = status.failed_items_count || 0;
  const emptyCount = status.empty_items_count || 0;
  const retryFailedBtn = `<button type="button" class="btn btn-secondary" onclick="extRetryFailed('${connId}')"
               ${live || !failedCount || st.retryingFailed ? "disabled" : ""}>${
    st.retryingFailed ? "Retrying…" : `Retry failed (${failedCount})`
  }</button>`;
  const retryEmptyBtn = `<button type="button" class="btn btn-secondary" onclick="extRetryEmpty('${connId}')"
               ${live || !emptyCount || st.retryingEmpty ? "disabled" : ""}>${
    st.retryingEmpty ? "Retrying…" : `Retry empty (${emptyCount})`
  }</button>`;
  // Re-run only offers itself once there is a most-recent run to react to
  // AND it did not finish cleanly — a `done` run has nothing to re-run
  // FROM here (its own next scheduled/manual trigger is the ordinary path).
  const latestKnown = run || last;
  const canRerun = !live && !!latestKnown && (latestKnown.outcome === "failed" || latestKnown.outcome === "interrupted");
  const rerunBtn = canRerun
    ? `<button type="button" class="btn btn-secondary" onclick="extRerun('${connId}')"
               ${st.rerunning ? "disabled" : ""}>${st.rerunning ? "Starting…" : "Re-run"}</button>`
    : "";
  // Cancel is the FORCE-close hammer, offered only once a run has already
  // proven Stop alone won't reach it — `outcome === "stalled"` means its
  // checkpoint is stale well past a live crawl's own cadence (see
  // `extraction.stall_after_s` server-side), so a run merely `running`
  // never shows this button; Stop is the right first move there.
  const canCancelNow = !!run && run.outcome === "stalled";
  const cancelBtn = canCancelNow
    ? `<button type="button" class="btn btn-sm btn-danger" onclick="extCancelRun('${connId}', '${run.id}')"
               ${st.cancelling ? "disabled" : ""}>${st.cancelling ? "Cancelling…" : "Cancel run"}</button>`
    : "";

  const actions = `
      <div class="ext-actions">
        ${stopBtn}
        ${cancelBtn}
        ${retryFailedBtn}
        ${retryEmptyBtn}
        ${rerunBtn}
        <button type="button" class="btn btn-secondary" onclick="toggleExtractionDrawer('${connId}', 'runs')"
                ${total ? "" : "disabled"}>Run history (${total})</button>
        <button type="button" class="btn btn-secondary" onclick="toggleExtractionDrawer('${connId}', 'completeness')"
                title="Did we really get everything? Compares Graph Search's own document count against what's indexed.">Completeness</button>
        <a class="btn btn-secondary" href="/admin/extraction"
           title="Every connection's crawl and facts extraction on one screen — phase, pace, spend, and what looks stuck.">All connections</a>
      </div>`;

  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="The built-in extraction pipeline's own run state — read from the recorded run, not from this page's clock.">Run</span>
    <span class="ds-src__fact-v">
      <span class="ext-lines">
        <span>${head}</span>
        ${sub}
      </span>
      ${actions}
    </span>
  </div>
  ${_extConfigRowHtml(connId)}`;
}

/* The `Configuration` fact row (design §6.1): a read-out with origins, not
   an editor. The whole `extraction` block is deploy-time, and inventing a
   second write path for it would be a new settings surface — which is
   exactly what "zero new navigation" forbids. So the row says what it is
   and opens the drawer; it never offers a control. */
function _extConfigRowHtml(connId) {
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k" title="Every effective extraction setting with its origin — instance.yaml, an env var, per-scope, or a built-in constant.">Configuration</span>
    <span class="ds-src__fact-v">
      <span class="ext-sub">Read-only — the extraction block is deploy-time configuration.</span>
      <div class="ext-actions">
        <button type="button" class="btn btn-secondary" onclick="toggleExtractionDrawer('${connId}', 'config')">View configuration</button>
      </div>
    </span>
  </div>`;
}

function _extPanelHtml(tone, title, body, connId, retry) {
  const retryBtn = retry
    ? `<div class="ext-actions"><button type="button" class="btn btn-secondary" onclick="extRetry('${connId}')">Retry</button></div>`
    : "";
  return `
  <div class="ds-src__fact">
    <span class="ds-src__fact-k">Run</span>
    <span class="ds-src__fact-v ${tone}">
      <span class="ext-lines">
        <span><strong>${_extEsc(title)}</strong></span>
        <div class="ext-sub">${_extEsc(body)}</div>
      </span>
      ${retryBtn}
    </span>
  </div>`;
}

/* "Extract facts now" mirrors `_extRenderInAgnesButton` above: never keep
   offering the verb while a facts pass is already queued/running for this
   connection (the server can only answer with `409
   facts_extraction_already_running`), and never re-enable a button the
   server gated off — `data-facts-ready` remembers the switch verdict, and
   its server-rendered `title` (the reason) is left alone in that case. */
function _extRenderFactsButton(connId, status) {
  const btn = document.getElementById(`ext-facts-btn-${connId}`);
  if (!btn) return;
  const ready = btn.dataset.factsReady === "1";
  const inFlight = !!(status && status.facts_job);
  btn.disabled = !ready || inFlight;
  if (inFlight) {
    btn.title = "A facts pass is already queued or running for this connection — see the Run row above.";
  } else if (ready) {
    btn.title = "";
  }
}

function _extRender(connId) {
  const block = document.getElementById(`ext-block-${connId}`);
  if (!block) return;
  const st = _extS(connId);

  if (st.stopped) {
    block.hidden = false;
    block.innerHTML = _extPanelHtml(
      "",
      "Run history needs a Postgres backend",
      "Extraction runs are recorded in a Postgres-only table, so this instance cannot show run state. Everything else on this card is unaffected.",
      connId,
      false,
    );
    _extRenderCrawlCell(connId, null);
    _extRenderInAgnesButton(connId, null);
    _extRenderFactsButton(connId, null);
    _extRenderNextRun(connId, null);
    return;
  }

  if (!st.data && st.failures >= EXT_MAX_FAILURES) {
    block.hidden = false;
    block.innerHTML = _extPanelHtml(
      "is-warn",
      "Couldn't read the extraction run state",
      `${st.failures} attempts failed. Nothing here is being updated.`,
      connId,
      true,
    );
    return;
  }

  if (!st.data) { block.hidden = true; block.innerHTML = ""; return; }

  block.hidden = false;
  block.innerHTML = _extRunRowHtml(connId, st);
  _extRenderCrawlCell(connId, st.data);
  _extRenderInAgnesButton(connId, st.data);
  _extRenderFactsButton(connId, st.data);
  _extRenderNextRun(connId, st.data);
}

/* D.16 — the "Crawl schedule & filter" panel's "next run" line
   (`ds-sp-crawlschedule-nextrun-<connId>`, `data_sources_page.js::
   _extRenderCrawlFilter`), refreshed on the SAME poll that already fetches
   `extraction/status` (`_extFetchOne` below) — no extra request. A no-op
   when the panel isn't in the DOM yet (a card not yet expanded/rendered). */
function _extRenderNextRun(connId, status) {
  const el = document.getElementById(`ds-sp-crawlschedule-nextrun-${connId}`);
  if (!el) return;
  const nextRunAt = status && status.next_run_at;
  el.textContent = nextRunAt ? `Next run: ${new Date(nextRunAt).toLocaleString()}.` : "Next run: not scheduled.";
}

async function _extFetchOne(connId) {
  const st = _extS(connId);
  if (st.stopped) return;
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/status`, {
      credentials: "include",
    });
    if (r.status === 501) {
      // Terminal and self-explaining: the answer cannot change until the
      // instance changes backend, so polling on would be pure noise.
      st.stopped = true;
      _extRender(connId);
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    st.data = await r.json();
    st.lastOk = st.data.as_of;
    st.failures = 0;
    st.error = null;
    // Once the run this admin stopped is no longer the live one — finished,
    // interrupted, or gone — the "Stopping…" lock is stale. Clearing it here
    // (rather than on a timer) is what lets a LATER run on this same
    // connection show a clickable Stop button of its own.
    if (!st.data.running) st.stopping = false;
    // The three reprocessing actions below are single-shot requests, not a
    // wait-for-it state like Stop — by the time ANY subsequent poll lands,
    // the click has already been submitted (202 or a typed refusal), so the
    // button's disabled state can go back to being governed purely by
    // `live`/the backlog counts rather than this flag.
    st.retryingFailed = false;
    st.retryingEmpty = false;
    st.rerunning = false;
  } catch (e) {
    // Keep the last good numbers, VISIBLY marked stale. Blanking them, or
    // redrawing them as current, are the two ways this goes wrong.
    st.failures += 1;
    st.error = String(e && e.message ? e.message : e);
  }
  _extRender(connId);
}

function _extConnectionIds() {
  return Array.from(document.querySelectorAll("[data-ext-conn]")).map((el) => el.dataset.extConn);
}

function _extNextDelay() {
  const states = Object.values(_extState);
  const worstFailures = states.reduce((a, s) => Math.max(a, s.stopped ? 0 : s.failures), 0);
  if (worstFailures) {
    return Math.min(EXT_POLL_ACTIVE_MS * Math.pow(2, worstFailures), EXT_POLL_MAX_BACKOFF_MS);
  }
  const live = states.some((s) => s.data && s.data.running);
  return live ? EXT_POLL_ACTIVE_MS : EXT_POLL_IDLE_MS;
}

async function _extTick() {
  if (_extInFlight) return;
  if (document.visibilityState !== "visible") return;   // a hidden tab watches nothing
  const ids = _extConnectionIds();
  if (!ids.length) return;
  _extInFlight = true;
  try {
    // Sequential, one request in flight at a time — an admin page with
    // several SharePoint connections must not open N sockets every 3s.
    for (const id of ids) await _extFetchOne(id);
  } finally {
    _extInFlight = false;
  }
}

function _extSchedule() {
  if (_extTimer) clearTimeout(_extTimer);
  _extTimer = setTimeout(async () => {
    await _extTick();
    _extSchedule();
  }, _extNextDelay());
}

/* Cards arrive over fetch; this script self-starts at parse time. So the
   first tick saw an empty page, found no connection to ask about, and the
   scheduler — reading a state map that was still empty — armed the IDLE
   interval. The extraction block appeared a full 30s after the card it
   belongs to, and 60s when a mutation's `loadConnections()` repainted over
   it while that tick was in flight. Every paint of the cards now says so.

   Two halves, deliberately: repaint from cache is free and always runs, so
   a rebuilt card gets its run row back in the same frame instead of
   blanking; the request only goes out for a connection this page cannot
   already draw, so a mutation-driven repaint costs nothing. */
function _extAfterCardsPainted() {
  const ids = _extConnectionIds();
  if (!ids.length) return;
  let unknown = false;
  for (const id of ids) {
    const st = _extState[id];
    if (st && (st.data || st.stopped)) _extRender(id);
    else unknown = true;
  }
  if (!unknown) return;
  // Returned, not swallowed: the product callers are fire-and-forget, but a
  // caller that wants to know when the ask landed should not have to guess.
  return _extTick().then(_extSchedule);
}

function extRetry(connId) {
  const st = _extS(connId);
  st.failures = 0;
  st.error = null;
  _extFetchOne(connId);
}

/* Cooperative stop (owner ask: "can't see it, can't stop it"). `confirm()`
   matches this page's other destructive one-click actions (e.g. the
   master-token / project-lock removals above) rather than the async
   `confirmModal` — no extra element to wire up in a script that is
   otherwise dependency-free from the rest of the page. A 202 flips the
   button to a disabled "Stopping…" and lets the existing 3 s poll carry the
   outcome; the button never claims the run ended before that poll confirms it. */
async function extStopRun(connId) {
  if (!confirm("Stop this extraction run? What it has already ingested is kept — the next run can resume.")) return;
  const st = _extS(connId);
  st.stopping = true;
  _extRender(connId);
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/stop`, {
      method: "POST",
      credentials: "include",
    });
    if (r.status === 202) {
      const body = await r.json().catch(() => ({}));
      if (typeof showToast === "function") {
        showToast(body && body.note ? body.note : "Stop requested — the run will end at its next checkpoint.", true);
      }
      await _extFetchOne(connId);
      return;
    }
    const body = await r.json().catch(() => ({}));
    st.stopping = false;
    const msg = typeof detailMessage === "function" ? detailMessage(body, "couldn't stop the run") : "couldn't stop the run";
    if (typeof showToast === "function") showToast(`Stop failed: ${msg}`, false);
    _extRender(connId);
  } catch (e) {
    st.stopping = false;
    if (typeof showToast === "function") showToast("Request failed.", false);
    _extRender(connId);
  }
}

/* Every reprocessing action an operator needed the shell for (TCRD-296):
   retry this connection's own failed/empty backlog, or re-run outright
   after a failed/interrupted run. One shared body — POST, flip an
   in-flight flag so a double-click can't fire the request twice (belt and
   braces: the server's own per-connection idempotency key already refuses
   an overlapping run with 409 `extraction_already_running`, surfaced here
   as a plain sentence via `detailMessage` rather than a raw JSON blob),
   toast the queued count the SERVER computed (never a client-side guess),
   and let the existing status poll (`_extFetchOne`) carry the outcome —
   the row is never redrawn from what this call merely hopes the job will do. */
async function _extTriggerAction(connId, stKey, url, body, successPrefix) {
  const st = _extS(connId);
  st[stKey] = true;
  _extRender(connId);
  try {
    const r = await fetch(url, {
      method: "POST",
      credentials: "include",
      ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    });
    if (r.status === 202) {
      const respBody = await r.json().catch(() => ({}));
      const count = typeof respBody.queued_count === "number" ? ` (${respBody.queued_count} queued)` : "";
      if (typeof showToast === "function") showToast(`${successPrefix}${count}.`, true);
      await _extFetchOne(connId);
      return;
    }
    const respBody = await r.json().catch(() => ({}));
    st[stKey] = false;
    const msg = typeof detailMessage === "function" ? detailMessage(respBody, "request failed") : "request failed";
    if (typeof showToast === "function") showToast(msg, false);
    _extRender(connId);
  } catch (e) {
    st[stKey] = false;
    if (typeof showToast === "function") showToast("Request failed.", false);
    _extRender(connId);
  }
}

function extRetryFailed(connId) {
  return _extTriggerAction(
    connId,
    "retryingFailed",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extract`,
    { retry_failed: true },
    "Retry failed queued",
  );
}

function extRetryEmpty(connId) {
  return _extTriggerAction(
    connId,
    "retryingEmpty",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extraction/retry-empty`,
    null,
    "Retry empty queued",
  );
}

function extRerun(connId) {
  return _extTriggerAction(
    connId,
    "rerunning",
    `/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/extract`,
    null,
    "Extraction queued",
  );
}

/* Force-close a run Stop alone cannot reach — a genuinely stuck crawl loop
   never notices the cooperative flag Stop sets either (the 2026-09-02
   incident: an hour of `running` with no checkpoint, ended by hand via two
   SQL updates). Same `confirm()` posture as `extStopRun` above; the
   phrasing is explicit that this is the "give up on it" button, not a
   gentler variant of Stop. A 200 means the row is closed server-side
   immediately (never waiting on the crawl to notice) — the next poll shows
   it as `interrupted`. */
async function extCancelRun(connId, runId) {
  if (
    !confirm(
      "Cancel this extraction run? This force-closes it even if the worker never reacts — what it " +
        "already ingested is kept, but the run itself will not finish on its own. Use this only after " +
        "Stop hasn't worked."
    )
  ) {
    return;
  }
  const st = _extS(connId);
  st.cancelling = true;
  _extRender(connId);
  try {
    const r = await fetch(`/api/admin/sharepoint/extraction/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      credentials: "include",
    });
    if (r.ok) {
      if (typeof showToast === "function") showToast("Run cancelled.", true);
      await _extFetchOne(connId);
      return;
    }
    const body = await r.json().catch(() => ({}));
    st.cancelling = false;
    const msg = typeof detailMessage === "function" ? detailMessage(body, "couldn't cancel the run") : "couldn't cancel the run";
    if (typeof showToast === "function") showToast(`Cancel failed: ${msg}`, false);
    _extRender(connId);
  } catch (e) {
    st.cancelling = false;
    if (typeof showToast === "function") showToast("Request failed.", false);
    _extRender(connId);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    _extTick();
    _extSchedule();
  }
});

/* ── Drawer: `runs` and `config` segments ──────────────────────────────
   Its own element and its own toggle, deliberately NOT the error-badge
   drawer's: that one renders the LAST INGEST run's rejection categories
   from server-rendered data, this one fetches per segment. Sharing the
   element would have meant one function that means two things. */
async function toggleExtractionDrawer(connId, segment) {
  const el = document.getElementById(`ext-drawer-${connId}`);
  if (!el) return;
  if (el.dataset.segment === segment && !el.hidden) {
    el.hidden = true;
    delete el.dataset.segment;
    return;
  }
  el.dataset.segment = segment;
  el.hidden = false;
  el.innerHTML = `<div class="ds-empty">Loading…</div>`;
  await _extLoadDrawerSegment(connId, segment, el, { refresh: false });
}

/* `segment`'s data-fetch — split out from `toggleExtractionDrawer` so the
   Completeness segment's own "Recount" button can re-fetch (with
   `refresh=true`) and re-render IN PLACE, without going through the
   open/close toggle above (a recount must not look like closing the
   drawer). */
async function _extLoadDrawerSegment(connId, segment, el, { refresh }) {
  const path = segment === "config"
    ? "extraction/config"
    : segment === "completeness"
      ? `extraction/completeness${refresh ? "?refresh=true" : ""}`
      : "extraction/runs?limit=10";
  try {
    const r = await fetch(`/api/admin/sharepoint/connections/${encodeURIComponent(connId)}/${path}`, {
      credentials: "include",
    });
    if (r.status === 501) {
      el.innerHTML = `<div class="ds-empty">This needs a Postgres backend — this instance records no extraction runs.</div>`;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (segment === "config") {
      el.innerHTML = _extConfigHtml(body);
    } else if (segment === "completeness") {
      el._completenessBody = body;
      el.dataset.completenessSort = el.dataset.completenessSort || "desc";
      el.innerHTML = _extCompletenessHtml(connId, el);
    } else {
      el.innerHTML = _extRunsHtml(connId, body);
    }
  } catch (e) {
    el.innerHTML = `<div class="ds-empty">Couldn't load this ${_extEsc(segment)} — ${_extEsc(e && e.message)}.</div>`;
  }
}

function _extRunsHtml(connId, body) {
  const runs = body.runs || [];
  if (!runs.length) return `<div class="ds-empty">No extraction runs recorded yet.</div>`;
  const rows = runs.map((run) => {
    const when = _extTime(run.started_at);
    const dur = run.duration_s != null
      ? _extDuration(run.duration_s)
      : (run.elapsed_s != null ? `${_extDuration(run.elapsed_s)} so far` : "");
    const line = _extRunLine(run);
    let detail = "";
    if (line) detail += `<div class="ext-sub">${_extEsc(line)}</div>`;
    // Same severity-first ordering as the card's own last-run block: a
    // failure reads before a policy skip.
    if (run.errors) detail += _extErrorsSummaryHtml(connId, run.id, run.errors);
    if (run.skipped_unsupported) {
      detail += `<div class="ext-sub">${_extNum(run.skipped_unsupported)} file${run.skipped_unsupported === 1 ? "" : "s"} skipped — unsupported type, not an error</div>`;
    }
    if (run.skips_total) {
      const listed = run.skips_listed != null && run.skips_listed !== run.skips_total
        ? ` (${_extNum(run.skips_listed)} listed by name)`
        : "";
      detail += `<div class="ext-sub ext-warn">${_extNum(run.skips_total)} not indexed${_extEsc(listed)}</div>`;
    }
    if (run.http_429) detail += _extThrottleLine(run);
    // A run that stopped early NAMES its exit, so "ended short" is never
    // something an operator has to infer from a duration. Rendered for a
    // failed run too: "the ceiling did its job", "the tenant rate-limited
    // us" and "something broke" are three different facts with three
    // different next actions, and the reason is what tells them apart.
    if (run.interrupted_reason) {
      detail += `<div class="ext-sub ext-warn">stopped early — ${_extEsc(_extStopReasonText(run.interrupted_reason))}</div>`;
    }
    // Keyed on the SERVER's `resumable` verdict, never on the outcome word.
    // A timeout and a tenant-throttle abort both finalize as `failed` —
    // correctly, the job did not finish its corpus — yet both persisted
    // their state on the way out and cost re-work rather than coverage.
    // Gating this on `outcome === "interrupted"` withheld it from exactly
    // the cases that earned it, which is how an operator re-runs a
    // four-hour crawl out of doubt. A crash still gets no such line.
    if (run.resumable) {
      detail += `<div class="ext-sub">what it ingested is kept; the next run resumes from where it stopped.</div>`;
    }
    if (run.outcome === "stalled" && run.liveness_note) {
      detail += `<div class="ext-sub ext-warn">${_extEsc(run.liveness_note)}</div>`;
    }
    if (run.error) detail += `<div class="ext-sub ext-danger">${_extEsc(run.error)}</div>`;
    detail += _extShardsHtml(run.shards);
    return `
      <li>
        <div class="ext-run__head">
          ${_extDot(run.outcome)}
          <strong>${_extEsc(run.outcome)}</strong>
          <span class="ext-run__when">${_extEsc(when)}</span>
          ${dur ? `<span class="ext-run__when">${_extEsc(dur)}</span>` : ""}
          <span class="ext-run__when">${_extNum(run.files_done)} files${_extShardCountText(run)}</span>
        </div>
        ${detail}
      </li>`;
  });
  const hidden = (body.total || 0) - runs.length;
  const more = hidden > 0
    ? `<div class="ext-sub">${_extNum(hidden)} older run${hidden === 1 ? "" : "s"} not shown.</div>`
    : "";
  return `<ul class="ext-runs">${rows.join("")}</ul>${more}`;
}

/* Read-only, with an origin on every row. `editable: false` renders as bare
   text plus its reason — no chevron, because there is nothing to open. */
const EXT_ORIGIN_LABEL = {
  env: "env",
  yaml: "instance.yaml",
  default: "built-in default",
  builtin: "built in",
};

function _extConfigHtml(body) {
  const rows = (body.effective || []).map((row) => {
    const origin = EXT_ORIGIN_LABEL[row.origin] || row.origin || "";
    const originText = row.origin === "env" && row.env_name ? `env ${row.env_name}` : origin;
    const value = row.value === null || row.value === undefined || row.value === ""
      ? "not set"
      : String(row.value);
    const lock = row.editable
      ? ""
      : `<span class="ext-sub" title="${_extEsc(row.lock_reason || "")}">deploy-time</span>`;
    return `
      <tr>
        <th scope="row">${_extEsc(row.label)}</th>
        <td>
          ${_extEsc(value)}
          ${row.key ? `<code>${_extEsc(row.key)}</code>` : ""}
          ${row.note ? `<span class="ext-cfg__note">${_extEsc(row.note)}</span>` : ""}
        </td>
        <td>${_extEsc(originText)} ${lock}</td>
      </tr>`;
  });

  const scopes = (body.scopes || []).map((s) => {
    const anon = !s.anonymize
      ? "anonymize —"
      : s.anonymization_declared
        ? "anonymize ✓ requested · declared ✓"
        : "anonymize ✓ requested · not declared";
    const audience = (s.audience_classes || []).map((c) => c.name).join(" ▸ ");
    return `
      <tr>
        <th scope="row">${_extEsc(s.display_path || s.source_scope_id || "")}</th>
        <td>
          ${_extEsc(anon)}
          ${audience ? `<span class="ext-cfg__note">audience: ${_extEsc(audience)}</span>` : ""}
          ${s.no_group_warning ? `<span class="ext-cfg__note ext-warn">no group can see this collection</span>` : ""}
        </td>
        <td>per-scope</td>
      </tr>`;
  });

  const scopeBlock = scopes.length
    ? `<table class="ext-cfg"><tbody>${scopes.join("")}</tbody></table>`
    : `<div class="ds-empty">No confirmed scopes yet.</div>`;

  return `
    <table class="ext-cfg"><tbody>${rows.join("")}</tbody></table>
    <div class="ext-sub">${_extEsc(body.section_lock_reason || "")}</div>
    <div class="ext-sub"><strong>Per scope</strong></div>
    ${scopeBlock}`;
}

/* ── Drawer: `completeness` segment (TCRD-296 B.9) — "did we really get
   everything?" One row per confirmed scope, plus (for a single whole-drive
   scope) one indented row per top-level folder under it, plus a totals
   line. Sortable by GAP (click the column header) — the rows worth a look
   float to the top by default. A "Recount" button bypasses the server's
   10-minute cache. */
const EXT_COMPLETENESS_STATUS_CLASS = {
  complete: "badge--success",
  accounted: "badge--warn",
  missing: "badge--danger",
  unknown: "badge--info",
};

/* `expected`/`gap` are `null` when the count itself could not be resolved
   (a "site" scope spanning several drives, or a failed Graph lookup) — that
   MUST render as "—", never as 0, which would read as "zero documents
   expected" instead of "unknown". Every other completeness field is always
   a concrete integer. */
function _extCompletenessNum(n) {
  return n == null ? "—" : Number(n).toLocaleString();
}

function _extCompletenessRowHtml(row) {
  const cls = EXT_COMPLETENESS_STATUS_CLASS[row.status] || "badge--info";
  const label = row.kind === "folder" ? `<span class="ext-sub">↳</span> ${_extEsc(row.label)}` : `<strong>${_extEsc(row.label)}</strong>`;
  return `
    <tr>
      <td>${label}</td>
      <td class="ext-num">${_extCompletenessNum(row.expected)}</td>
      <td class="ext-num">${_extNum(row.indexed)}</td>
      <td class="ext-num">${_extNum(row.rejected)}</td>
      <td class="ext-num">${_extNum(row.failed)}</td>
      <td class="ext-num">${_extNum(row.empty)}</td>
      <td class="ext-num">${_extNum(row.skipped_unsupported)}</td>
      <td class="ext-num">${_extNum(row.oversize)}</td>
      <td class="ext-num">${_extCompletenessNum(row.gap)}</td>
      <td><span class="badge ${cls}">${_extEsc(row.status)}</span></td>
    </tr>`;
}

function _extCompletenessHtml(connId, el) {
  const body = el._completenessBody || {};
  const rows = (body.rows || []).slice();
  const dir = el.dataset.completenessSort === "asc" ? "asc" : "desc";
  // An unresolved gap (a "site" scope, or a failed Graph lookup) always
  // sinks to the bottom, regardless of sort direction — it is neither the
  // biggest nor the smallest gap, it is simply not a number.
  rows.sort((a, b) => {
    if (a.gap == null && b.gap == null) return 0;
    if (a.gap == null) return 1;
    if (b.gap == null) return -1;
    return dir === "asc" ? a.gap - b.gap : b.gap - a.gap;
  });
  const total = body.total || {};
  const provisionalNote = body.provisional
    ? `<div class="ext-sub ext-warn">A crawl is running right now — these numbers are a snapshot mid-crawl.</div>`
    : "";
  const cachedNote = body.cached
    ? `<div class="ext-sub">Cached${body.as_of ? ` as of ${_extEsc(_extTime(body.as_of))}` : ""}.</div>`
    : "";
  const caveats = (body.caveats || [])
    .map((c) => `<div class="ext-sub">note: ${_extEsc(c)}</div>`)
    .join("");
  const emptyRow = !rows.length
    ? `<tr><td colspan="10" class="ds-empty">No confirmed scopes yet.</td></tr>`
    : "";
  return `
    <div class="ext-actions">
      <button type="button" class="btn btn-secondary" onclick="extRecountCompleteness('${connId}')">Recount</button>
      <button type="button" class="btn btn-secondary" onclick="extSortCompleteness('${connId}')">
        Sort by gap: ${dir === "desc" ? "highest first" : "lowest first"}
      </button>
    </div>
    ${provisionalNote}
    <table class="ext-cfg ext-completeness">
      <thead>
        <tr>
          <th>Scope / folder</th><th>Expected</th><th>Indexed</th><th>Rejected</th>
          <th>Failed</th><th>Empty</th><th>Skipped</th><th>Oversize</th><th>Gap</th><th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(_extCompletenessRowHtml).join("")}
        ${emptyRow}
        <tr class="ext-completeness__total">
          <td><strong>Total</strong></td>
          <td class="ext-num">${_extCompletenessNum(total.expected)}</td>
          <td class="ext-num">${_extNum(total.indexed)}</td>
          <td class="ext-num">${_extNum(total.rejected)}</td>
          <td class="ext-num">${_extNum(total.failed)}</td>
          <td class="ext-num">${_extNum(total.empty)}</td>
          <td class="ext-num">${_extNum(total.skipped_unsupported)}</td>
          <td class="ext-num">${_extNum(total.oversize)}</td>
          <td class="ext-num">${_extCompletenessNum(total.gap)}</td>
          <td><span class="badge ${EXT_COMPLETENESS_STATUS_CLASS[total.status] || "badge--info"}">${_extEsc(total.status)}</span></td>
        </tr>
      </tbody>
    </table>
    ${cachedNote}
    ${caveats}`;
}

async function extRecountCompleteness(connId) {
  const el = document.getElementById(`ext-drawer-${connId}`);
  if (!el) return;
  el.innerHTML = `<div class="ds-empty">Recounting…</div>`;
  await _extLoadDrawerSegment(connId, "completeness", el, { refresh: true });
}

function extSortCompleteness(connId) {
  const el = document.getElementById(`ext-drawer-${connId}`);
  if (!el || !el._completenessBody) return;
  el.dataset.completenessSort = el.dataset.completenessSort === "asc" ? "desc" : "asc";
  el.innerHTML = _extCompletenessHtml(connId, el);
}

/* Expanding a card must show its extraction status immediately — not wait
   for the next poll tick, up to `EXT_POLL_IDLE_MS` (30s) away on a fresh
   page (2026-09 live walkthrough, design gap 1: the block stayed empty
   behind "Run extraction now" / "View configuration" until then).
   `setSourceOpen` is the ONE function every expand path already calls — the
   caret, a click on the card head, and every "open this card and show me a
   row" helper elsewhere on the page — so wrapping it here reaches all of
   them with no second listener to keep in sync. Only for a SharePoint card
   (an `ext-block-` anchor exists) with nothing live cached yet: an
   already-fetched card, a 501-stopped one, or a repeat open/close costs
   nothing extra. `_extFetchOne` is the poll's own single fetch-and-render
   for one connection — reused, never reimplemented. */
function _extMaybeFetchOnExpand(connId) {
  if (!document.getElementById(`ext-block-${connId}`)) return;
  const st = _extS(connId);
  if (st.stopped || st.data) return;
  _extFetchOne(connId);
}

if (typeof setSourceOpen === "function") {
  const _extPrevSetSourceOpen = setSourceOpen;
  setSourceOpen = function (id, open) {
    _extPrevSetSourceOpen(id, open);
    if (open) _extMaybeFetchOnExpand(id);
  };
}

_extTick();
_extSchedule();
