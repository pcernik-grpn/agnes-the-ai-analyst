/* Extracted from admin_data_sources.html (perf follow-up, 2026-09-03) — was inlined on every page load (uncacheable); now a normal cached static asset, versioned by static_url()'s ?v=<mtime> cache-buster. */
const SP_CONN_API = "/api/admin/source-connections";
const SP_API = "/api/admin/sharepoint/connections";
const SP_GROUPS_API = "/api/admin/groups";
const SP_DEFAULT_CERT_ENV = "SHAREPOINT_CERT_PRIVATE_KEY";
// One wording for the unique-permissions badge label, defined once so the
// tree row and (were it ever needed elsewhere) any other renderer agree.
const SP_UNIQUE_PERMS_LABEL = "&#9888; unique permissions";

/* Advisory client-side check of certificate material — answers before any
   network call; the PUT endpoint's server-side validation stays the
   authority. Mirrors its vocabulary: a CERTIFICATE block plus an
   UNENCRYPTED private-key block, concatenated in one PEM. */
function spPemFormatCheck(text) {
  if (/-----BEGIN ENCRYPTED PRIVATE KEY-----/.test(text)) {
    return { ok: false, message: "The private key is encrypted — export it unencrypted (PKCS#8) and try again." };
  }
  const hasCert = text.indexOf("-----BEGIN CERTIFICATE-----") !== -1;
  const hasKey = /-----BEGIN (?:RSA |EC )?PRIVATE KEY-----/.test(text);
  if (hasCert && hasKey) return { ok: true, message: "Certificate and private key found." };
  if (!hasCert && !hasKey) return { ok: false, message: "No PEM blocks found — expected the certificate followed by its private key." };
  if (!hasCert) return { ok: false, message: "Missing the CERTIFICATE block — pick the certificate file too (you can select both files at once)." };
  return { ok: false, message: "Missing the PRIVATE KEY block — pick the key file too (you can select both files at once)." };
}

/* Shared by the wizard's picker and each source card's rotate row (inline
   `onchange`). Reads every selected file, concatenates (block order is
   irrelevant to the server's parser), fills the target textarea so the
   admin sees exactly what will be submitted, and reports the format
   verdict in the status element. Values flow through `.value` and
   `.textContent` only — never innerHTML. */
async function spCertFilePicked(inputEl, targetId, statusId) {
  const files = Array.from((inputEl && inputEl.files) || []);
  if (!files.length) return;
  const parts = [];
  for (const f of files) parts.push((await f.text()).trim());
  const combined = parts.join("\n") + "\n";
  document.getElementById(targetId).value = combined;
  const verdict = spPemFormatCheck(combined);
  const status = document.getElementById(statusId);
  status.textContent = verdict.message;
  status.style.display = "";
  status.style.color = verdict.ok ? "" : "var(--ds-accent-danger-ink)";
  try { inputEl.value = ""; } catch (e) { /* re-picking the same file must still fire change */ }
}
document.getElementById("spw-cert-file").addEventListener("change", (e) => {
  spCertFilePicked(e.target, "spw-cert-pem", "spw-cert-status");
});

let spConnId = null;
let spCertChoice = "upload";
let spLevel = { site_id: null, drive_id: null, item_id: null };
let spCrumbs = [];        // [{label, site_id, drive_id, item_id}]
let spItems = [];         // current level's tree items
let spScopes = {};        // source_scope_id -> scope row (server shape, incl. no_group_warning)
let spGroups = null;      // cached [{id, name}]
let spPendingGroups = {}; // source_scope_id -> Set(group_id), locally ticked but not yet applied
let spTreeFilterQuery = "";   // client-side instant filter (#2) over spItems, reset on navigation
let spLastSearchMatches = null; // last server-search (#3) result list, or null if none run yet
// item_id -> true|false|null, accumulated from `?with_permissions=1` tree
// responses across THIS wizard session only — never a full crawl, so
// "unknown" (not present here, or `null`) must never render as "no unique
// permissions" anywhere that reads this map. ADVISORY ONLY (Decision #2:
// Agnes does not derive or enforce anything from a SharePoint ACL).
let spUniquePerms = {};
// site_id -> site row resolved via `?site_url=` — the Sites.Selected escape
// hatch (discovery is 403-forbidden under that permission, so these rows are
// the ONLY way the sites level can populate there). Merged into every
// sites-level render so an added site survives navigating away and back.
let spManualSites = {};
// True while the drawer is bound to an EXISTING connection (opened from a
// card, not from "+ Add source"). Step 1 then SHOWS that connection instead
// of offering a blank new-tenant form — see spBindStep1ToConnection.
let spBoundToExisting = false;
// The in-flight `?source_type=sharepoint` listing openSpWizard already
// starts, reused by the binding below so it costs no second request.
let spConnListPromise = null;

function spEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function spApi(url, opts) {
  opts = opts || {};
  opts.credentials = "include";
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  }
  return fetch(url, opts).then((r) => {
    if (!r.ok) {
      return r.json().catch(() => ({})).then((b) => {
        const d = b && b.detail;
        const msg = d && typeof d === "object" ? (d.message || d.error || ("HTTP " + r.status))
                                                 : (d || ("HTTP " + r.status));
        const err = new Error(msg);
        err.detail = d;
        err.status = r.status;
        throw err;
      });
    }
    return r.status === 204 ? null : r.json();
  });
}

/* ── Open / close / steps ─────────────────────────────────────────────── */

function openSpWizard() {
  spConnId = null;
  spCertChoice = "upload";
  spLevel = { site_id: null, drive_id: null, item_id: null };
  spCrumbs = [];
  spItems = [];
  spScopes = {};
  spGroups = null;
  spPendingGroups = {};
  spTreeFilterQuery = "";
  spLastSearchMatches = null;
  spUniquePerms = {};
  spManualSites = {};

  document.getElementById("spw-site-by-url").value = "";
  document.getElementById("spw-tree-filter").value = "";
  document.getElementById("spw-search-q").value = "";
  document.getElementById("spw-search-truncated").style.display = "none";
  document.getElementById("spw-search-skipped").style.display = "none";
  document.getElementById("spw-search-results").style.display = "none";
  document.getElementById("spw-search-results").innerHTML = "";
  document.getElementById("spw-search-error").style.display = "none";
  // The drawer is reused across connections — a stale "Saved scopes" list
  // or discovery guidance from whichever connection was open last must not
  // flash before the new one's own scopes load.
  document.getElementById("spw-saved-scopes").style.display = "none";
  document.getElementById("spw-saved-scopes").innerHTML = "";
  document.getElementById("spw-discovery-guidance").style.display = "none";
  document.getElementById("spw-discovery-guidance").textContent = "";
  document.getElementById("spw-site-by-url-wrap").classList.remove("sp-search--emphasis");
  document.getElementById("spw-name").value = "";
  document.getElementById("spw-tenant").value = "";
  document.getElementById("spw-client").value = "";
  document.getElementById("spw-cert-pem").value = "";
  document.getElementById("spw-cert-env-name").value = "";
  document.getElementById("spw-client-secret").value = "";
  document.getElementById("spw-cert-status").style.display = "none";
  document.getElementById("spw-connect-error").style.display = "none";
  // The drawer is reused, so the bound presentation must be undone here or
  // it would survive into the next "+ Add source" and make connecting a new
  // tenant impossible (read-only fields carrying someone else's values).
  spBoundToExisting = false;
  ["spw-name", "spw-tenant", "spw-client"].forEach((id) => {
    document.getElementById(id).readOnly = false;
  });
  document.getElementById("spw-credential-field").style.display = "";
  document.getElementById("spw-new-tenant-sep").style.display = "";
  document.getElementById("spw-existing-picker").style.display = "";
  spSetCertChoice("upload");
  spGoStep(1);

  const sel = document.getElementById("spw-existing-select");
  spConnListPromise = spApi(SP_CONN_API + "?source_type=sharepoint");
  spConnListPromise.then((rows) => {
    rows = Array.isArray(rows) ? rows : [];
    const existing = document.getElementById("spw-existing");
    if (!rows.length) { existing.hidden = true; return; }
    sel.innerHTML = rows.map((c) => `<option value="${spEsc(c.id)}">${spEsc(c.name || c.id)}</option>`).join("");
    // With exactly one existing connection there is nothing to actually
    // choose — pre-select it so "Continue to scope" is a single click
    // rather than an empty-looking picker with one option in it.
    if (rows.length === 1) sel.value = rows[0].id;
    _syncDropdownRebuild(sel);
    existing.hidden = false;
  }).catch(() => { document.getElementById("spw-existing").hidden = true; });

  document.getElementById("sp-wizard-overlay").classList.add("show");
  document.body.style.overflow = "hidden";
  setTimeout(() => document.getElementById("spw-tenant").focus({ preventScroll: true }), 60);
}

/* Card entry point (spec follow-up, TCRD-240/241: "make it clickable"
   rather than wizard-only) — opens the SAME wizard bound to an EXISTING
   connection, skipping step 1 entirely (never creates a duplicate
   connection). `opts.highlightScopeId` additionally jumps straight to
   step 3 ("Share") and highlights that one scope row — the only pane with
   a flat, source_scope_id-keyed list of every confirmed scope regardless
   of where it lives in the folder tree; step 2 only shows scopes that
   happen to be visible at the currently-browsed tree level, and a stored
   scope carries no site_id/drive_id to reconstruct a tree path from, so
   driving the tree to the right folder isn't possible from a scope row
   alone. Mirrors the "Continue an existing connection" step-1 button's own
   click handler below, without needing the (async) existing-connections
   list to have loaded first — the caller already knows the id. */
/* Step 1 for a wizard bound to an EXISTING connection: show that
   connection instead of a blank form for a new one. Reported from a live
   instance (2026-09-01) — managing scopes and then looking at the Connect
   step gave an empty Connection name / Tenant ID / Client ID, so nothing
   on screen said which connection was being edited. The values were always
   in the `?source_type=sharepoint` listing the drawer already fetches (the
   source card renders the tenant id from it); step 1 was simply never told.

   Read-only on purpose: the SharePoint card offers no connection editor,
   and `spw-connect-btn` POSTs a NEW connection — so a prefilled, editable
   form under it would silently create a duplicate instead of saving an
   edit. Showing the truth is this fix; an editor is a separate feature.
   The credential block and that button are hidden here for the same
   reason: both belong to creating a tenant, not to reading one. */
function spBindStep1ToConnection(connId) {
  spBoundToExisting = true;
  document.getElementById("spw-credential-field").style.display = "none";
  document.getElementById("spw-new-tenant-sep").style.display = "none";
  document.getElementById("spw-connect-btn").style.display = "none";
  // "Continue an existing connection" asks a question already answered —
  // this drawer was opened FROM one. The block around it stays (it carries
  // "Continue to scope", the way back to step 2, which reads spConnId
  // rather than the hidden picker while bound).
  document.getElementById("spw-existing-picker").style.display = "none";
  document.getElementById("spw-existing").hidden = false;
  ["spw-name", "spw-tenant", "spw-client"].forEach((id) => {
    document.getElementById(id).readOnly = true;
  });
  // Reuses openSpWizard's in-flight listing — no second request. A failure
  // leaves the fields empty rather than blocking the wizard: step 2 is what
  // the caller came for, and it does not depend on any of this.
  return (spConnListPromise || Promise.resolve([]))
    .then((rows) => {
      const row = (Array.isArray(rows) ? rows : []).find((c) => c.id === connId);
      if (!row) return;
      const config = row.config || {};
      document.getElementById("spw-name").value = row.name || "";
      document.getElementById("spw-tenant").value = config.tenant_id || "";
      document.getElementById("spw-client").value = config.client_id || "";
    })
    .catch(() => {});
}

function openSpWizardForConnection(connId, opts) {
  opts = opts || {};
  openSpWizard();
  spConnId = connId;
  spBindStep1ToConnection(connId);
  spEnableStep(2);
  spEnableStep(3);
  spLevel = { site_id: null, drive_id: null, item_id: null };
  spCrumbs = [];
  if (opts.highlightScopeId) {
    spGoStep(3);
    spLoadShare().then(() => _spHighlightShareRow(opts.highlightScopeId));
  } else {
    spGoStep(2);
    spLoadScopesThenTree();
  }
}

/* Scrolls a step-3 share row into view and gives it a brief highlight —
   the landing effect for a scope row clicked on the card. A no-op if the
   row isn't in the currently-rendered share list (nothing to highlight
   yet, e.g. while it is still loading). */
function _spHighlightShareRow(scopeId) {
  const row = document.querySelector(`[data-spw-share-row="${CSS.escape(scopeId)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  row.classList.add("sp-share-row--highlight");
  setTimeout(() => row.classList.remove("sp-share-row--highlight"), 2500);
}

async function closeSpWizard() {
  document.getElementById("sp-wizard-overlay").classList.remove("show");
  document.body.style.overflow = "";
  // Same contract as the table wizard's own exit: this drawer connects a
  // source, scopes it and shares it, and every card behind it was drawn
  // before any of that happened. The rows too — scopes live in the
  // connection's own `config`, so the card's identity line and its scope
  // list are as stale as the strip.
  await refreshSourcePipelines();
  await loadConnections();
}

function spGoStep(step) {
  document.querySelectorAll("#sp-wizard-overlay [data-spw-pane]").forEach((p) =>
    p.classList.toggle("is-on", p.dataset.spwPane === String(step)));
  document.querySelectorAll("#sp-wizard-overlay [data-spw-step]").forEach((b) => {
    const n = Number(b.dataset.spwStep);
    b.classList.toggle("is-now", n === step);
    b.classList.toggle("is-done", n < step);
  });
  // Never on a bound connection: that button creates a NEW connection, and
  // step 1 there is a read-only view of the one already being edited.
  document.getElementById("spw-connect-btn").style.display =
    step === 1 && !spBoundToExisting ? "" : "none";
  document.getElementById("spw-scope-continue-btn").style.display = step === 2 ? "" : "none";
  document.getElementById("spw-finish-btn").style.display = step === 3 ? "" : "none";
  document.getElementById("spw-done-btn").style.display = "none";
  document.getElementById("spw-body").scrollTop = 0;
}

function spEnableStep(n) {
  document.querySelector(`#sp-wizard-overlay [data-spw-step="${n}"]`).disabled = false;
}

/* ── Step 1: connect ──────────────────────────────────────────────────── */

function spSetCertChoice(choice) {
  spCertChoice = choice;
  document.getElementById("spw-cert-choice").value = choice;
  document.getElementById("spw-cert-upload").style.display = choice === "upload" ? "" : "none";
  document.getElementById("spw-cert-env").style.display = choice === "env" ? "" : "none";
  document.getElementById("spw-cert-secret").style.display = choice === "secret" ? "" : "none";
}
document.getElementById("spw-cert-choice").addEventListener("change", (e) => spSetCertChoice(e.target.value));

function spConnectErr(msg) {
  const el = document.getElementById("spw-connect-error");
  el.textContent = msg;
  el.style.display = "block";
}

document.getElementById("spw-existing-btn").addEventListener("click", () => {
  // While bound the picker is hidden, so its value is whatever the listing
  // happened to preselect — the connection this drawer was opened for is
  // the only right answer.
  const id = spBoundToExisting ? spConnId : document.getElementById("spw-existing-select").value;
  if (!id) return;
  spConnId = id;
  spEnableStep(2);
  spEnableStep(3);
  spGoStep(2);
  spLevel = { site_id: null, drive_id: null, item_id: null };
  spCrumbs = [];
  spLoadScopesThenTree();
});

document.getElementById("spw-connect-btn").addEventListener("click", () => {
  const tenant = document.getElementById("spw-tenant").value.trim();
  const client = document.getElementById("spw-client").value.trim();
  document.getElementById("spw-connect-error").style.display = "none";
  if (!tenant || !client) {
    spConnectErr("Tenant ID and Client ID are both required.");
    return;
  }
  const name = document.getElementById("spw-name").value.trim() || `SharePoint (${tenant})`;
  const config = { tenant_id: tenant, client_id: client };
  if (spCertChoice === "env") {
    const envName = document.getElementById("spw-cert-env-name").value.trim();
    if (envName) config.cert_private_key_env = envName;
  }
  if (spCertChoice === "secret") config.auth_method = "client_secret";

  const btn = document.getElementById("spw-connect-btn");
  btn.disabled = true;
  btn.textContent = "Connecting…";

  spApi(SP_CONN_API, { method: "POST", body: { name: name, source_type: "sharepoint", config: config } })
    .then((row) => {
      spConnId = row.id;
      if (spCertChoice === "upload" || spCertChoice === "secret") {
        const value = spCertChoice === "secret"
          ? document.getElementById("spw-client-secret").value.trim()
          : document.getElementById("spw-cert-pem").value.trim();
        if (!value) return Promise.resolve(row);
        return spApi(`${SP_CONN_API}/${encodeURIComponent(row.id)}/secret`, {
          method: "PUT", body: { value: value, kind: "storage" },
        }).then(() => row);
      }
      return row;
    })
    .then(() => {
      btn.disabled = false;
      btn.textContent = "Connect & validate";
      spEnableStep(2);
      spEnableStep(3);
      spLevel = { site_id: null, drive_id: null, item_id: null };
      spCrumbs = [];
      spGoStep(2);
      spLoadScopesThenTree();
    })
    .catch((e) => {
      btn.disabled = false;
      btn.textContent = "Connect & validate";
      spConnectErr("Could not save the connection: " + e.message);
    });
});

/* ── Step 2: scope (folder tree) ──────────────────────────────────────── */

function spTreeErr(msg) {
  const el = document.getElementById("spw-tree-error");
  if (!msg) { el.style.display = "none"; el.textContent = ""; return; }
  el.textContent = msg;
  el.style.display = "block";
}

/* Informational sibling of spTreeErr — `html` is built ONLY from spEsc'd
   values plus our own static markup (the Library link), never raw server
   text. */
function spTreeNotice(html) {
  const el = document.getElementById("spw-tree-notice");
  if (!html) { el.style.display = "none"; el.innerHTML = ""; return; }
  el.innerHTML = html;
  el.style.display = "block";
}

/* GUIDANCE sibling of spTreeErr, for `sharepoint_discovery_forbidden`
   only: an app registration holding `Sites.Selected` cannot enumerate
   sites BY DESIGN — that permission is the recommended least-privilege
   posture, not a fault, so it must never read as a red failure to
   dismiss. Rendered inside the "Add a site by URL" box (the box IS the
   fix), which the emphasis class marks as the primary action while this
   is showing. */
function spTreeGuidance(msg) {
  const el = document.getElementById("spw-discovery-guidance");
  const wrap = document.getElementById("spw-site-by-url-wrap");
  if (!msg) {
    el.style.display = "none"; el.textContent = "";
    wrap.classList.remove("sp-search--emphasis");
    return;
  }
  el.textContent = msg;
  el.style.display = "block";
  wrap.classList.add("sp-search--emphasis");
}

/* "Saved scopes": every confirmed scope on this connection, from the scope
   rows alone — independent of whatever the live tree can currently browse
   to. A folder scope carries no site/drive identity to rebuild a
   navigable tree row from (spSeedManualSitesFromScopes handles the site
   case only), so this is the only place a folder scope several levels
   deep in a site is visible at all once discovery is forbidden. Its
   anonymize checkbox reuses spConfirmScope — the same idempotent
   confirm-on-source_scope_id path the tree's own checkbox uses, and it
   never sends `group_ids`, so toggling it here cannot touch this scope's
   sharing. */
function spRenderSavedScopes() {
  const host = document.getElementById("spw-saved-scopes");
  const scopes = Object.values(spScopes);
  if (!scopes.length) { host.style.display = "none"; host.innerHTML = ""; return; }
  host.style.display = "block";
  host.innerHTML = '<p class="sp-saved-scopes__title">Saved scopes</p>' + scopes.map((s) => {
    const anon = !!s.anonymize;
    const badge = s.collection
      ? `<span class="sp-badge${anon ? (s.anonymization_declared ? " sp-badge--anon-declared" : " sp-badge--anon") : ""}">&rarr; ${spEsc(s.collection.slug)}</span>`
      : "";
    return `<div class="sp-saved-scopes__row" data-spw-saved="${spEsc(s.source_scope_id)}">` +
      `<span class="sp-saved-scopes__path" title="${spEsc(s.display_path)}">${spEsc(s.display_path)}</span>` +
      badge +
      `<label class="sp-tree__anon"><input type="checkbox" data-spw-saved-anon="${spEsc(s.source_scope_id)}"${anon ? " checked" : ""}> anonymize</label>` +
      `</div>`;
  }).join("");
  host.querySelectorAll("[data-spw-saved-anon]").forEach((cb) => {
    cb.addEventListener("change", () => spOnSavedAnonToggle(cb));
  });
}

function spOnSavedAnonToggle(cb) {
  const id = cb.dataset.spwSavedAnon;
  const scope = spScopes[id];
  if (!scope) return;
  spConfirmScope(id, scope.display_path, cb.checked);
}

function spLoadScopesThenTree() {
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/scopes`).then((body) => {
    spScopes = {};
    (body.items || []).forEach((s) => { spScopes[s.source_scope_id] = s; });
    spRenderSavedScopes();
    spSeedManualSitesFromScopes();
    spSeedManualSitesFromConnectionConfig();
  }).catch(() => { spScopes = {}; spRenderSavedScopes(); }).then(spLoadTree);
}

/* A saved SITE scope can rebuild its own sites-level row: its
   source_scope_id IS the Graph site id ("host,siteCollection,web" — the
   only scope id containing commas, the same structural discriminator the
   crawler's _scope_kind keys on) and its
   display_path IS the site name (sites are ticked at the sites level,
   where the crumb path is just the name). Seeding spManualSites here is
   what keeps a saved site visible when the wizard reopens on an existing
   connection: the live listing can't be relied on to include it —
   Sites.Selected 403-forbids discovery outright, and list_sites is
   first-page-only — and spManualSites is reset on every wizard open.
   Folder/drive scopes carry no site identity, so no row can be rebuilt
   for them (the flat step-3 list still shows every scope). */
function spSeedManualSitesFromScopes() {
  Object.values(spScopes).forEach((s) => {
    const id = String(s.source_scope_id || "");
    if (id.indexOf(",") !== -1 && !spManualSites[id]) {
      spManualSites[id] = { id: id, name: s.display_path || id };
    }
  });
}

/* Every site an admin has EVER added by URL on this connection
   (2026-09-01 persistence fix — `POST/DELETE .../manual-sites`), regardless
   of whether it is also a confirmed scope: before this, a site added by URL
   lived ONLY in this same client-side `spManualSites` map, which is reset
   on every wizard open (see `openSpWizard`) — closing and reopening the
   wizard, or just reloading the page, silently forgot it and the admin had
   to re-paste the same URL. `_connections` is the SAME page-level cache
   `loadConnections()` already populated before this wizard could have been
   opened (the card whose click opened it was itself rendered from that
   list) — reading it here is simpler than a dedicated GET endpoint, at the
   cost of trusting it is fresh enough (a stale miss here only means a
   site added moments ago in a DIFFERENT tab fails to pre-populate, never a
   correctness issue with this wizard's own reads/writes). Existing entries
   win — never overwrites a row `spSeedManualSitesFromScopes` above already
   placed. */
function spSeedManualSitesFromConnectionConfig() {
  const conn = (_connections || []).find((c) => c.id === spConnId);
  const stored = (conn && conn.config && conn.config.manual_sites) || [];
  stored.forEach((s) => { if (s && s.id && !spManualSites[s.id]) spManualSites[s.id] = s; });
}

/* Forget a site added by URL (see `spSeedManualSitesFromConnectionConfig`
   above) — the sites-level row's own "x", rendered only for a manual site
   that is NOT currently a confirmed scope (`spRenderTree`). Purely local
   bookkeeping server-side (no collection, no grants), so there is nothing
   else to reconcile — unlike `spRemoveScope` below. */
function spRemoveManualSite(siteId) {
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/manual-sites?site_id=${encodeURIComponent(siteId)}`, {
    method: "DELETE",
  }).then(() => {
    delete spManualSites[siteId];
    spItems = spItems.filter((item) => item.id !== siteId);
    if (spCurrentLevel() === "sites") spRenderTree("sites");
  }).catch((e) => {
    spTreeErr("Could not forget that site: " + e.message);
  });
}

/* The current browse level, derived from spLevel — one place, since
   confirm/remove/filter all need to know it to re-render the right rows. */
function spCurrentLevel() {
  if (spLevel.drive_id) return "items";
  if (spLevel.site_id) return "drives";
  return "sites";
}

/* Splice the `?site_url=`-resolved sites into a sites-level listing —
   under Sites.Selected they are the only rows the level can ever show,
   and under normal discovery they must survive alongside listed sites
   without duplicating one that discovery already returned. */
function spMergeManualSites() {
  const have = new Set(spItems.map((i) => i.id));
  Object.values(spManualSites).forEach((s) => { if (!have.has(s.id)) spItems.push(s); });
}

function spSyncSiteByUrlVisibility() {
  document.getElementById("spw-site-by-url-wrap").style.display =
    spCurrentLevel() === "sites" ? "" : "none";
}

function spLoadTree() {
  spTreeErr("");
  spTreeGuidance("");
  spTreeFilterQuery = "";
  document.getElementById("spw-tree-filter").value = "";
  document.getElementById("spw-tree").innerHTML = '<div class="ds-loading">Loading…</div>';
  spRenderCrumbs();
  spSyncSiteByUrlVisibility();
  const params = new URLSearchParams();
  if (spLevel.site_id) params.set("site_id", spLevel.site_id);
  if (spLevel.drive_id) params.set("drive_id", spLevel.drive_id);
  if (spLevel.item_id) params.set("item_id", spLevel.item_id);
  // Only the "items" level ever lists folders — sites/drives have nothing
  // to probe, so the param is only worth sending (and paying for) there.
  if (spLevel.drive_id) params.set("with_permissions", "1");
  const qs = params.toString();
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/tree${qs ? "?" + qs : ""}`)
    .then((body) => {
      spItems = body.items || [];
      // Accumulate into the session-wide map — a folder seen `true` once
      // stays known `true` even after navigating away and back (re-browsing
      // does not re-clear it); `false`/`undefined` never overwrite an
      // earlier `true` for the same id, since the flag describes the
      // SOURCE's state, not this one response.
      spItems.forEach((item) => {
        if (item.unique_permissions === true) spUniquePerms[item.id] = true;
        else if (!(item.id in spUniquePerms)) spUniquePerms[item.id] = item.unique_permissions ?? null;
      });
      if (body.level === "sites") spMergeManualSites();
      spRenderTree(body.level);
    })
    .catch((e) => {
      spItems = [];
      document.getElementById("spw-tree").innerHTML = "";
      // A site added by URL, and a saved site scope seeded from
      // `spSeedManualSitesFromScopes`, are LOCAL state — neither is read
      // back from Graph — so no listing failure is a reason to hide them.
      // This rescue used to live inside the `sharepoint_discovery_forbidden`
      // branch alone, which made it true only for Sites.Selected: a real
      // instance failing with `sharepoint_cert_unresolved` still drew an
      // empty Sites list beside a card reading "1 scope" (live run,
      // 2026-09-01). Only at the sites level — deeper levels list drives
      // and folders, where a site row would answer a different question
      // than the breadcrumb asks. The error/guidance below still shows
      // either way: the rows are real AND the rest of the tree is
      // genuinely missing.
      if (spCurrentLevel() === "sites") {
        spMergeManualSites();
        if (spItems.length) spRenderTree("sites");
      }
      if (e.detail && e.detail.error === "sharepoint_cert_unresolved") {
        spTreeErr("No certificate configured yet — " + e.detail.message);
      } else if (e.detail && e.detail.error === "sharepoint_discovery_forbidden") {
        // Sites.Selected 403-forbidding discovery is the intended
        // least-privilege posture, not a fault — guidance toward "Add a
        // site by URL", never `.ds-wizard-error`'s failure styling (live
        // report, 2026-09-01: the red banner trained an operator to
        // ignore it and buried the one working affordance below it).
        spTreeGuidance(e.detail.message);
      } else if (e.detail && e.detail.error === "sharepoint_graph_error") {
        spTreeErr("SharePoint did not answer: " + e.detail.message);
      } else {
        spTreeErr("Could not load the folder tree: " + e.message);
      }
    });
}

/* POSTs to the PERSISTING endpoint (2026-09-01 fix) rather than the plain
   `?site_url=` resolve-only GET on `.../tree` — the same resolution and the
   same typed errors (`invalid_site_url`, `sharepoint_site_not_granted`,
   `sharepoint_graph_error`), but the result now also survives a wizard
   reopen (see `spSeedManualSitesFromConnectionConfig`), never just this
   in-memory `spManualSites` map. */
function spAddSiteByUrl() {
  const input = document.getElementById("spw-site-by-url");
  const btn = document.getElementById("spw-site-by-url-btn");
  const url = input.value.trim();
  if (!url || !spConnId) return;
  btn.disabled = true;
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/manual-sites`, {
    method: "POST",
    body: { site_url: url },
  })
    .then((site) => {
      btn.disabled = false;
      spManualSites[site.id] = site;
      input.value = "";
      if (spCurrentLevel() === "sites") {
        spTreeErr("");
        spMergeManualSites();
        spRenderTree("sites");
      }
    })
    .catch((e) => {
      btn.disabled = false;
      const msg = e.detail && e.detail.message ? e.detail.message : e.message;
      spTreeErr("Could not add the site: " + msg);
    });
}

document.getElementById("spw-site-by-url-btn").addEventListener("click", spAddSiteByUrl);
document.getElementById("spw-site-by-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); spAddSiteByUrl(); }
});

function spRenderCrumbs() {
  const nav = document.getElementById("spw-crumbs");
  const parts = [{ label: "Sites", site_id: null, drive_id: null, item_id: null }].concat(spCrumbs);
  nav.innerHTML = parts.map((c, i) => {
    const last = i === parts.length - 1;
    const btn = `<button type="button" data-spw-crumb="${i}" ${last ? "disabled" : ""}>${spEsc(c.label)}</button>`;
    return i === 0 ? btn : `<span class="sep">/</span>${btn}`;
  }).join("");
  nav.querySelectorAll("[data-spw-crumb]").forEach((b) => {
    b.addEventListener("click", () => {
      const i = Number(b.dataset.spwCrumb);
      const target = ([{ site_id: null, drive_id: null, item_id: null }].concat(spCrumbs))[i];
      spCrumbs = spCrumbs.slice(0, i);
      spLevel = { site_id: target.site_id, drive_id: target.drive_id, item_id: target.item_id };
      spLoadTree();
    });
  });
}

function spDisplayPath(name) {
  return spCrumbs.map((c) => c.label).concat([name]).join(" / ");
}

/* ── Client-side instant filter (#2): narrows spItems already loaded at
   the current level — no server round trip. Case/diacritics-insensitive
   (unlike the server search's NFC-only compare — see the fold note on
   spFilterKey), so "elektrina" narrows to "Elektřina". ─────────────────── */

function spFoldDiacritics(s) {
  // NFD decomposes a precomposed accented letter into base + combining
  // mark(s); stripping the "Combining Diacritical Marks" block then leaves
  // just the base letters — "Přehledy" and "Prehledy" fold to the same key.
  return String(s == null ? "" : s).normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function spFilterKey(s) {
  return spFoldDiacritics(s).toLocaleLowerCase();
}

function spRowMatchesFilter(name) {
  if (!spTreeFilterQuery) return true;
  return spFilterKey(name).indexOf(spFilterKey(spTreeFilterQuery)) !== -1;
}

/* Wraps the matched substring in <mark>. Falls back to plain escaped text
   whenever folding changed the string's length (a rare composition case) —
   a highlight is a nicety, never worth mis-slicing a folder name over. */
function spHighlight(name) {
  const raw = String(name == null ? "" : name);
  if (!spTreeFilterQuery) return spEsc(raw);
  const foldedName = spFilterKey(raw);
  const foldedQuery = spFilterKey(spTreeFilterQuery);
  const idx = foldedName.indexOf(foldedQuery);
  if (idx === -1 || foldedName.length !== raw.length || !foldedQuery) return spEsc(raw);
  const before = spEsc(raw.slice(0, idx));
  const match = spEsc(raw.slice(idx, idx + foldedQuery.length));
  const after = spEsc(raw.slice(idx + foldedQuery.length));
  return `${before}<mark>${match}</mark>${after}`;
}

function spRenderTree(level) {
  const host = document.getElementById("spw-tree");
  if (!spItems.length) {
    host.innerHTML = '<div class="sp-tree__row"><span class="sp-tree__name"><span class="leaf">Nothing here.</span></span></div>';
    return;
  }
  let anyVisible = false;
  host.innerHTML = spItems.map((item) => {
    // Sites/drives are always navigable; an item (folder) at the "items"
    // level is navigable too (TCRD-240 subfolder browsing) — a FILE never
    // is, regardless of level.
    const navigable = level === "sites" || level === "drives" || (level === "items" && item.is_folder);
    const visible = spRowMatchesFilter(item.name);
    if (visible) anyVisible = true;
    const scope = spScopes[item.id];
    const checked = !!scope;
    const anon = scope ? !!scope.anonymize : false;
    // Requested vs declared (spec §9.2) applies here too — "· anon" alone
    // read as a completed fact even mid-wizard; "declared" earns the word
    // "anonymized", everything else stays the lighter "· anon" hint.
    const anonDeclared = scope ? !!scope.anonymization_declared : false;
    const path = spDisplayPath(item.name);
    const nameHtml = navigable
      ? `<button type="button" data-spw-drill="${spEsc(item.id)}" data-spw-name="${spEsc(item.name)}">${spHighlight(item.name)}</button>`
      : `<span class="leaf">${spHighlight(item.name)}</span>`;
    const anonSuffix = anon ? (anonDeclared ? " · anonymized" : " · anon requested") : "";
    const badge = scope && scope.collection
      ? `<span class="sp-badge${anon ? (anonDeclared ? " sp-badge--anon-declared" : " sp-badge--anon") : ""}">&rarr; ${spEsc(scope.collection.slug)}${anonSuffix}</span>`
      : "";
    // ADVISORY ONLY (Decision #2) — a probe result of `false` or unknown
    // (absent, or `null`) renders NOTHING here; only a bare `true` earns
    // the badge, so a probe failure or an un-probed item never reads as a
    // false reassurance of "no unique permissions".
    const uniquePermsBadge = spUniquePerms[item.id] === true
      ? `<span class="sp-badge sp-badge--unique-perms" title="Has its own permissions in the source — people who can't open it there may still see its content once this scope is shared here.">${SP_UNIQUE_PERMS_LABEL}</span>`
      : "";
    // A site added by URL that is NOT (or no longer) a confirmed scope can
    // be forgotten again (2026-09-01 persistence fix) — a CONFIRMED site
    // scope already has its own removal path (unticking the checkbox,
    // `spRemoveScope`), which also tidies up its collection; offering a
    // second one here would only confuse which one an admin just used.
    const unlinkBtn = level === "sites" && !checked && spManualSites[item.id]
      ? `<button type="button" class="sp-tree__unlink" data-spw-unlink="${spEsc(item.id)}" title="Forget this site" aria-label="Forget ${spEsc(item.name)}">&times;</button>`
      : "";
    return `<div class="sp-tree__row" data-spw-item="${spEsc(item.id)}" data-spw-path="${spEsc(path)}"${visible ? "" : " hidden"}>` +
      `<input type="checkbox" data-spw-select="${spEsc(item.id)}" aria-label="Select ${spEsc(item.name)}"${checked ? " checked" : ""}>` +
      `<span class="sp-tree__name">${nameHtml}</span>` +
      `<label class="sp-tree__anon"><input type="checkbox" data-spw-anon="${spEsc(item.id)}"${anon ? " checked" : ""}${checked ? "" : " disabled"}> anonymize</label>` +
      badge +
      uniquePermsBadge +
      unlinkBtn +
      `</div>`;
  }).join("") + (anyVisible ? "" : '<div class="ds-empty" data-spw-nomatch>No folders match the filter.</div>');

  host.querySelectorAll("[data-spw-unlink]").forEach((b) => {
    b.addEventListener("click", () => spRemoveManualSite(b.dataset.spwUnlink));
  });
  host.querySelectorAll("[data-spw-drill]").forEach((b) => {
    b.addEventListener("click", () => {
      const id = b.dataset.spwDrill;
      const name = b.dataset.spwName;
      if (level === "sites") {
        spCrumbs = spCrumbs.concat([{ label: name, site_id: id, drive_id: null, item_id: null }]);
        spLevel = { site_id: id, drive_id: null, item_id: null };
      } else if (level === "drives") {
        spCrumbs = spCrumbs.concat([{ label: name, site_id: spLevel.site_id, drive_id: id, item_id: null }]);
        spLevel = { site_id: spLevel.site_id, drive_id: id, item_id: null };
      } else {
        // "items" level: drilling into a folder at any depth (TCRD-240).
        spCrumbs = spCrumbs.concat([{ label: name, site_id: spLevel.site_id, drive_id: spLevel.drive_id, item_id: id }]);
        spLevel = { site_id: spLevel.site_id, drive_id: spLevel.drive_id, item_id: id };
      }
      spLoadTree();
    });
  });
  host.querySelectorAll("[data-spw-select]").forEach((cb) => {
    cb.addEventListener("change", () => spOnSelectToggle(cb));
  });
  host.querySelectorAll("[data-spw-anon]").forEach((cb) => {
    cb.addEventListener("change", () => spOnAnonToggle(cb));
  });
}

document.getElementById("spw-tree-filter").addEventListener("input", (e) => {
  spTreeFilterQuery = e.target.value;
  spRenderTree(spCurrentLevel());
});

function spOnSelectToggle(cb) {
  const id = cb.dataset.spwSelect;
  const row = cb.closest("[data-spw-item]");
  const path = row.dataset.spwPath;
  const anonBox = row.querySelector("[data-spw-anon]");
  if (cb.checked) {
    anonBox.disabled = false;
    spConfirmScope(id, path, anonBox.checked);
  } else {
    anonBox.disabled = true;
    spRemoveScope(id);
  }
}

function spOnAnonToggle(cb) {
  const id = cb.dataset.spwAnon;
  if (!spScopes[id]) return; // not selected yet — nothing to update server-side
  const row = cb.closest("[data-spw-item]");
  spConfirmScope(id, row.dataset.spwPath, cb.checked);
}

/* Re-renders both the tree AND the last search result list (if any is on
   screen) after a scope changes — a confirm/remove reached from either
   list must update the badge/checkbox state in the OTHER one too. */
function spRefreshVisibleLists() {
  spRenderTree(spCurrentLevel());
  if (spLastSearchMatches) spRenderSearchResults(spLastSearchMatches);
  spRenderSavedScopes();
}

/* `ConfirmScopeBody`'s own docstring calls `access_mode`, `drive_id` and
   `include_excluded_subtrees` "always persisted on confirm" — unlike
   `group_ids`/`audience_classes`, omitting them is NOT "leave unchanged"
   server-side, it is "set to the default" (manual / null / false). A
   re-confirm that only means to change one field (anonymize here, sharing
   in the Share & finish handler below) must round-trip the rest of an
   EXISTING scope's own last-known values or it silently blanks them —
   `drive_id` most severely: without it the built-in crawler cannot
   address a folder scope on Graph at all, so the next crawl enumerates
   nothing for it while still reporting the run as done (live report,
   2026-09-01). A brand-new scope (not yet in `spScopes`) has no prior
   state to round-trip, so this returns `{}` and the server's own
   defaults apply — unchanged behavior for a first-time pick. */
function spExistingScopeFields(scope) {
  if (!scope) return {};
  const out = {
    access_mode: scope.access_mode || "manual",
    include_excluded_subtrees: !!scope.include_excluded_subtrees,
  };
  if (scope.drive_id) out.drive_id = scope.drive_id;
  return out;
}

function spConfirmScope(sourceScopeId, displayPath, anonymize) {
  spTreeErr("");
  spTreeNotice("");
  const body = Object.assign(
    { source_scope_id: sourceScopeId, display_path: displayPath, anonymize: !!anonymize },
    spExistingScopeFields(spScopes[sourceScopeId])
  );
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/scopes`, {
    method: "POST",
    body: body,
  }).then((scope) => {
    spScopes[sourceScopeId] = scope;
    spRefreshVisibleLists();
  }).catch((e) => {
    spTreeErr("Could not confirm that scope: " + e.message);
    spLoadTree(); // resync checkbox state with the server
    spRenderSavedScopes();
  });
}

function spRemoveScope(sourceScopeId) {
  spTreeErr("");
  spTreeNotice("");
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/scopes?source_scope_id=${encodeURIComponent(sourceScopeId)}`, {
    method: "DELETE",
  }).then((resp) => {
    delete spScopes[sourceScopeId];
    spRefreshVisibleLists();
    /* An empty collection is tidied away server-side; one with indexed
       files is kept — say so, and point at where the deliberate delete
       lives, instead of leaving the admin to discover the leftover later. */
    if (resp && resp.collection_kept && resp.collection) {
      spTreeNotice(
        `Scope removed. Its collection <strong>${spEsc(resp.collection.slug)}</strong> already holds indexed files, ` +
        `so it was kept — delete it in the <a href="/library?section=files">Library</a> if it should go too. ` +
        `Re-ticking this folder re-attaches to the same collection.`
      );
    }
  }).catch((e) => {
    spTreeErr("Could not remove that scope: " + e.message);
    spLoadTree();
  });
}

/* ── Server search (#3/#4): bounded BFS over the live tree, never Graph's
   own /search (see graph_client's module docstring). Results enter the
   SAME scope basket as tree picks — checking a result row is an ordinary
   spConfirmScope call, so bulk-select is just N ordinary confirms. ────── */

function spSearchErr(msg) {
  const el = document.getElementById("spw-search-error");
  if (!msg) { el.style.display = "none"; el.textContent = ""; return; }
  el.textContent = msg;
  el.style.display = "block";
}

/* A site/folder the app registration cannot read is a routine permissions
   fact (TCRD-240 skip hardening) — reported here, quietly, never folded
   into `spw-search-error` (that stays for calls that failed outright) or
   `spw-search-truncated` (a cap, not a permission refusal — a different
   fact with a different fix). `textContent` only: skipped names come from
   the tenant (site display names, folder paths) and are untrusted. */
function spSkipLabel(s) {
  if (s.scope === "site") return s.site_name || s.site_id || "a site";
  return s.display_path || "a folder";
}

function spRenderSkipped(skipped) {
  const el = document.getElementById("spw-search-skipped");
  if (!skipped || !skipped.length) { el.style.display = "none"; el.textContent = ""; return; }
  const names = skipped.map(spSkipLabel).join(", ");
  const noun = skipped.length === 1 ? "location was" : "locations were";
  el.textContent = `${skipped.length} ${noun} not accessible to this connection and skipped: ${names}.`;
  el.style.display = "block";
}

function spRenderSearchResults(matches) {
  const host = document.getElementById("spw-search-results");
  if (!matches.length) {
    host.innerHTML = '<div class="ds-empty">No folders matched.</div>';
    host.style.display = "";
    return;
  }
  const rowsHtml = matches.map((m) => {
    const scope = spScopes[m.item_id];
    const checked = !!scope;
    const anon = scope ? !!scope.anonymize : false;
    const badge = scope && scope.collection
      ? `<span class="sp-badge${anon ? " sp-badge--anon" : ""}">&rarr; ${spEsc(scope.collection.slug)}${anon ? " · anon" : ""}</span>`
      : "";
    return `<div class="sp-tree__row" data-spw-sr="${spEsc(m.item_id)}" data-spw-sr-drive="${spEsc(m.drive_id)}" data-spw-sr-path="${spEsc(m.display_path)}">` +
      `<input type="checkbox" data-spw-sr-select="${spEsc(m.item_id)}" aria-label="Select ${spEsc(m.display_path)}"${checked ? " checked" : ""}>` +
      `<span class="sp-tree__name"><span class="leaf">${spEsc(m.display_path)}</span></span>` +
      `<label class="sp-tree__anon"><input type="checkbox" data-spw-sr-anon="${spEsc(m.item_id)}"${anon ? " checked" : ""}${checked ? "" : " disabled"}> anonymize</label>` +
      badge +
      `</div>`;
  }).join("");
  host.innerHTML = `<label class="sp-search__selectall"><input type="checkbox" id="spw-search-select-all"> Select all (${matches.length})</label>` + rowsHtml;
  host.style.display = "";

  document.getElementById("spw-search-select-all").addEventListener("change", (e) => {
    const want = e.target.checked;
    host.querySelectorAll("[data-spw-sr-select]").forEach((cb) => {
      if (cb.checked !== want) { cb.checked = want; cb.dispatchEvent(new Event("change")); }
    });
  });
  host.querySelectorAll("[data-spw-sr-select]").forEach((cb) => {
    cb.addEventListener("change", () => spOnSearchSelectToggle(cb));
  });
  host.querySelectorAll("[data-spw-sr-anon]").forEach((cb) => {
    cb.addEventListener("change", () => spOnSearchAnonToggle(cb));
  });
}

function spOnSearchSelectToggle(cb) {
  const id = cb.dataset.spwSrSelect;
  const row = cb.closest("[data-spw-sr]");
  const path = row.dataset.spwSrPath;
  const anonBox = row.querySelector("[data-spw-sr-anon]");
  if (cb.checked) {
    anonBox.disabled = false;
    spConfirmScope(id, path, anonBox.checked);
  } else {
    anonBox.disabled = true;
    spRemoveScope(id);
  }
}

function spOnSearchAnonToggle(cb) {
  const id = cb.dataset.spwSrAnon;
  if (!spScopes[id]) return;
  const row = cb.closest("[data-spw-sr]");
  spConfirmScope(id, row.dataset.spwSrPath, cb.checked);
}

function spRunSearch() {
  const q = document.getElementById("spw-search-q").value.trim();
  document.getElementById("spw-search-truncated").style.display = "none";
  document.getElementById("spw-search-skipped").style.display = "none";
  spSearchErr("");
  if (q.length < 2) {
    spSearchErr("Type at least 2 characters.");
    return;
  }
  const mode = document.getElementById("spw-search-mode").value;
  const params = new URLSearchParams({ q: q, mode: mode });
  const btn = document.getElementById("spw-search-btn");
  btn.disabled = true;
  btn.textContent = "Searching…";
  spApi(`${SP_API}/${encodeURIComponent(spConnId)}/tree/search?${params.toString()}`)
    .then((body) => {
      btn.disabled = false;
      btn.textContent = "Search";
      spLastSearchMatches = body.matches || [];
      spRenderSearchResults(spLastSearchMatches);
      spRenderSkipped(body.skipped);
      if (body.truncated) {
        const el = document.getElementById("spw-search-truncated");
        // `hint` is the server's own next-step wording (admin_sharepoint.py
        // `_SEARCH_TRUNCATED_HINT`) — one place owns the copy; the fallback
        // only covers a response shape this build has never seen.
        const hint = body.hint || "Scope the search to a site or folder, or narrow the pattern.";
        el.textContent = `Stopped after ${body.visited} folder(s) visited. ${hint}`;
        el.style.display = "block";
      }
    })
    .catch((e) => {
      btn.disabled = false;
      btn.textContent = "Search";
      spLastSearchMatches = null;
      document.getElementById("spw-search-results").style.display = "none";
      document.getElementById("spw-search-results").innerHTML = "";
      if (e.detail && e.detail.error === "sharepoint_cert_unresolved") {
        spSearchErr("No certificate configured yet — " + e.detail.message);
      } else if (e.detail && e.detail.error === "sharepoint_graph_error") {
        spSearchErr("SharePoint did not answer: " + e.detail.message);
      } else if (e.detail && e.detail.error === "invalid_search_pattern") {
        spSearchErr("That pattern isn't valid: " + e.detail.message);
      } else {
        spSearchErr("Search failed: " + e.message);
      }
    });
}
document.getElementById("spw-search-btn").addEventListener("click", spRunSearch);
document.getElementById("spw-search-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); spRunSearch(); }
});

document.getElementById("spw-scope-continue-btn").addEventListener("click", () => {
  spGoStep(3);
  spLoadShare();
});

/* ── Step 3: share preview ────────────────────────────────────────────── */

function spShareErr(msg) {
  const el = document.getElementById("spw-share-error");
  if (!msg) { el.style.display = "none"; el.textContent = ""; return; }
  el.textContent = msg;
  el.style.display = "block";
}

function spLoadGroups() {
  if (spGroups) return Promise.resolve(spGroups);
  return spApi(SP_GROUPS_API).then((body) => {
    spGroups = Array.isArray(body) ? body : (body && body.groups) || [];
    return spGroups;
  }).catch(() => { spGroups = []; return spGroups; });
}

function spLoadShare() {
  document.getElementById("spw-share-rows").innerHTML = '<div class="ds-loading">Loading…</div>';
  // Returns the promise (unlike before) so `openSpWizardForConnection` can
  // chain a row-highlight onto "rendered", not just "requested".
  return Promise.all([
    spApi(`${SP_API}/${encodeURIComponent(spConnId)}/scopes`),
    spLoadGroups(),
  ]).then(([body]) => {
    const items = body.items || [];
    (items || []).forEach((s) => { spScopes[s.source_scope_id] = s; });
    spRenderShare(items);
  }).catch((e) => {
    document.getElementById("spw-share-rows").innerHTML = "";
    spShareErr("Could not load the share preview: " + e.message);
  });
}

function spRenderShare(items) {
  const host = document.getElementById("spw-share-rows");
  if (!items.length) {
    host.innerHTML = '<div class="ds-empty">No scopes confirmed yet — go back to step 2 and pick at least one.</div>';
    return;
  }
  // ADVISORY ONLY (Decision #2 — Agnes never derives or enforces anything
  // from a SharePoint ACL): counts only scopes THIS WIZARD SESSION actually
  // saw flagged `true` while browsing step 2 (spUniquePerms) — never a full
  // re-check of every selected scope, and never a gate on Share & finish.
  const uniquePermsFlaggedCount = items.filter((s) => spUniquePerms[s.source_scope_id] === true).length;
  const advisoryHtml = uniquePermsFlaggedCount === 0 ? "" : `
    <div class="apg-strip apg-strip--warn apg-strip--block" role="note">
      <span>&#9888; ${uniquePermsFlaggedCount} of your selected scope${uniquePermsFlaggedCount === 1 ? "" : "s"} had unique permissions in the source when last browsed. Agnes does not read or enforce those — the groups you choose below are the only access control that applies once this is shared here.</span>
    </div>`;
  host.innerHTML = advisoryHtml + items.map((s) => {
    const pending = spPendingGroups[s.source_scope_id] || new Set(s.group_ids || []);
    spPendingGroups[s.source_scope_id] = pending;
    // Spec §9.2/§13.2: the checkbox alone is a WISH, never rendered as
    // "anonymized" by itself — that badge is earned only once the latest
    // ingest run actually declares this collection anonymized
    // (`anonymization_declared`, computed server-side in
    // app/api/admin_sharepoint.py::_scope_out). Requested-but-not-declared
    // still gets a badge (so the admin sees the flag took), just a
    // different word and tone.
    const anonBadge = !s.anonymize ? "" : s.anonymization_declared
      ? '<span class="sp-badge sp-badge--anon-declared">anonymized</span>'
      : '<span class="sp-badge sp-badge--anon">anonymization requested</span>';
    const collBadge = s.collection ? `<span class="sp-badge">${spEsc(s.collection.name)}</span>` : "";
    const noGroup = pending.size === 0;
    const warn = noGroup ? '<span class="sp-warn">&#9888; indexed but invisible — no group yet</span>' : "";
    const groupsHtml = (spGroups || []).map((g) => {
      const on = pending.has(g.id);
      return `<label><input type="checkbox" data-spw-share-group="${spEsc(s.source_scope_id)}" value="${spEsc(g.id)}"${on ? " checked" : ""}> ${AgnesKindGlyph.groupGlyph()}${spEsc(g.name)}</label>`;
    }).join("") || '<span class="ds-drawer__opt">No groups yet — create one in <a href="/admin/access">Access</a>.</span>';
    return `<div class="sp-share-row" data-spw-share-row="${spEsc(s.source_scope_id)}">` +
      `<div class="sp-share-row__main">` +
      `<div class="sp-share-row__title">${spEsc(s.display_path)}</div>` +
      `<div class="sp-share-row__badges">${collBadge}${anonBadge}${warn}</div>` +
      `<div class="sp-share-row__groups">${groupsHtml}</div>` +
      `</div></div>`;
  }).join("");
  host.querySelectorAll("[data-spw-share-group]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = cb.dataset.spwShareGroup;
      const set = spPendingGroups[id] || new Set();
      if (cb.checked) set.add(cb.value); else set.delete(cb.value);
      spPendingGroups[id] = set;
      // Re-render just the warning state without a round trip.
      const row = cb.closest("[data-spw-share-row]");
      const warnEl = row.querySelector(".sp-warn");
      const nowEmpty = set.size === 0;
      if (warnEl && !nowEmpty) warnEl.remove();
      if (!warnEl && nowEmpty) {
        row.querySelector(".sp-share-row__badges").insertAdjacentHTML(
          "beforeend", '<span class="sp-warn">&#9888; indexed but invisible — no group yet</span>');
      }
    });
  });
}

document.getElementById("spw-finish-btn").addEventListener("click", () => {
  spShareErr("");
  const btn = document.getElementById("spw-finish-btn");
  btn.disabled = true;
  btn.textContent = "Sharing…";
  const calls = Object.keys(spScopes).map((id) => {
    const s = spScopes[id];
    const groupIds = Array.from(spPendingGroups[id] || []);
    // Every scope round-trips its own access_mode/drive_id/
    // include_excluded_subtrees (spExistingScopeFields) — "Share & finish"
    // re-confirms EVERY scope on the connection in one pass, so without
    // this it silently blanked drive_id (and reverted any mirrored scope
    // to manual) on every single wizard completion, not just an edited row.
    return spApi(`${SP_API}/${encodeURIComponent(spConnId)}/scopes`, {
      method: "POST",
      body: Object.assign(
        { source_scope_id: id, display_path: s.display_path, anonymize: !!s.anonymize, group_ids: groupIds },
        spExistingScopeFields(s)
      ),
    });
  });
  Promise.allSettled(calls).then((results) => {
    btn.disabled = false;
    btn.textContent = "Share & finish";
    const failures = results.filter((r) => r.status === "rejected").length;
    if (failures) {
      spShareErr(`${failures} scope(s) could not be shared. The rest were applied — try again for the rest.`);
      spLoadShare();
      return;
    }
    btn.style.display = "none";
    document.getElementById("spw-done-btn").style.display = "";
    refreshSourcePipelines().then(loadConnections);
  });
});

document.getElementById("spw-done-btn").addEventListener("click", closeSpWizard);
document.getElementById("sp-wizard-close").addEventListener("click", closeSpWizard);
document.getElementById("spw-cancel").addEventListener("click", closeSpWizard);
document.querySelectorAll("#sp-wizard-overlay [data-spw-close]").forEach((el) => {
  el.addEventListener("click", closeSpWizard);
});
document.querySelectorAll("#sp-wizard-overlay [data-spw-step]").forEach((b) => {
  b.addEventListener("click", () => {
    if (b.disabled) return;
    const n = Number(b.dataset.spwStep);
    spGoStep(n);
    if (n === 2) spLoadScopesThenTree();
    if (n === 3) spLoadShare();
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.getElementById("sp-wizard-overlay").classList.contains("show")) closeSpWizard();
});
