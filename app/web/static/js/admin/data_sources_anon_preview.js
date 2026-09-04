/* Extracted from admin_data_sources.html (perf follow-up, 2026-09-03) — was inlined on every page load (uncacheable); now a normal cached static asset, versioned by static_url()'s ?v=<mtime> cache-buster. */
/* One counter, so two connections with their config drawers open at once
   never share an element id. */
let _anonPrevSeq = 0;

function _anonPrevPanel() {
  const id = `anonprev-${++_anonPrevSeq}`;
  return `
    <div class="anonprev" id="${id}">
      <div class="anonprev__head">
        <span class="anonprev__title">Preview redaction</span>
        <span class="ext-sub">Paste a sample and see exactly what a crawl would redact — before it runs over thousands of documents. Nothing is stored.</span>
      </div>
      <textarea class="anonprev__ta" id="${id}-text"
        placeholder="Paste a representative page here…"
        aria-label="Sample text to preview redaction for"></textarea>
      <div class="anonprev__row">
        <button type="button" class="btn btn-secondary" id="${id}-go"
          onclick="anonPreviewRun('${id}')">Preview redaction</button>
        <label class="anonprev__opt">
          <input type="checkbox" id="${id}-llm">
          Also run the LLM detector
          <span class="anonprev__cost">— spends tokens on every preview (about one call per 30k characters)</span>
        </label>
      </div>
      <div id="${id}-result"></div>
    </div>`;
}

/* textContent, never innerHTML: everything rendered below is the admin's own
   pasted document coming back through an API (security playbook §3). */
function _anonPrevText(el, value) {
  el.textContent = value;
}

/* The server's message is shown VERBATIM — "no key configured", "term 3 is a
   regex", "50,000 characters max" each name their own fix, and paraphrasing
   them into "something went wrong" is how an admin ends up guessing. */
function _anonPrevError(box, status, payload) {
  const detail = payload && payload.detail !== undefined ? payload.detail : payload;
  let message;
  if (detail && typeof detail === "object") {
    message = detail.message || JSON.stringify(detail);
  } else if (typeof detail === "string") {
    message = detail;
  } else {
    message = `HTTP ${status}`;
  }
  const div = document.createElement("div");
  div.className = "anonprev__err";
  _anonPrevText(div, message);
  box.replaceChildren(div);
}

async function anonPreviewRun(id) {
  const textEl = document.getElementById(`${id}-text`);
  const llmEl = document.getElementById(`${id}-llm`);
  const button = document.getElementById(`${id}-go`);
  const box = document.getElementById(`${id}-result`);
  if (!textEl || !box) return;

  const sample = textEl.value || "";
  if (!sample.trim()) {
    const hint = document.createElement("div");
    hint.className = "ext-sub";
    _anonPrevText(hint, "Paste a sample first.");
    box.replaceChildren(hint);
    return;
  }

  const wantsLlm = !!(llmEl && llmEl.checked);
  const pending = document.createElement("div");
  pending.className = "ext-sub";
  _anonPrevText(pending, wantsLlm ? "Running the LLM detector…" : "Redacting…");
  box.replaceChildren(pending);
  if (button) button.disabled = true;

  try {
    const r = await fetch("/api/admin/sharepoint/anonymization/preview", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sample, detector: wantsLlm ? "llm" : "regex" }),
    });
    let payload = null;
    try { payload = await r.json(); } catch (parseError) { payload = null; }
    if (!r.ok) { _anonPrevError(box, r.status, payload); return; }

    const pre = document.createElement("pre");
    pre.className = "anonprev__out";
    _anonPrevText(pre, payload.redacted_text || "");

    const chips = document.createElement("div");
    chips.className = "anonprev__counts";
    const counts = payload.counts_by_kind || {};
    const kinds = Object.keys(counts).sort();
    if (!kinds.length) {
      /* "Nothing matched" is a real answer and has to read as one — an empty
         panel would read as a failure. */
      const none = document.createElement("span");
      none.className = "ext-sub";
      _anonPrevText(none, "Nothing in this sample matched any detector.");
      chips.appendChild(none);
    }
    for (const kind of kinds) {
      const chip = document.createElement("span");
      chip.className = "anonprev__chip";
      _anonPrevText(chip, `${kind} × ${counts[kind]}`);
      chips.appendChild(chip);
    }

    /* An empty `usage` means NO tokens were spent — a different claim from
       "$0.00", the same distinction the run card draws. */
    const usage = payload.usage || {};
    const spend = Object.keys(usage).length
      ? `${payload.detector} detector · ${usage.input_tokens || 0} in / ${usage.output_tokens || 0} out tokens over ${usage.calls || 0} call(s)`
      : `${payload.detector} detector · no tokens spent`;
    const foot = document.createElement("div");
    foot.className = "ext-sub";
    _anonPrevText(foot, `${spend}. Nothing was stored.`);

    box.replaceChildren(pre, chips, foot);
  } catch (e) {
    _anonPrevError(box, 0, { detail: String(e && e.message ? e.message : e) });
  } finally {
    if (button) button.disabled = false;
  }
}

/* Append the panel to the config drawer without touching its renderer. */
if (typeof _extConfigHtml === "function") {
  const _anonPrevBaseConfigHtml = _extConfigHtml;
  _extConfigHtml = function (body) {
    return _anonPrevBaseConfigHtml(body) + _anonPrevPanel();
  };
}
