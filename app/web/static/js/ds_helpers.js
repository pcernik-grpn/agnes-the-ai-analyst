/* Data-sources helpers shared by every surface on the Data section.
 *
 * A CLASSIC script, not a module, deliberately: a top-level `function f()`
 * here is a global, so the ~100 call sites that used to sit beside these in
 * one inline <script> — including the ones inside generated HTML strings as
 * `onclick="…"` — keep resolving with no edit at all. That makes the
 * extraction pure movement, which is the only kind that cannot change
 * behaviour.
 *
 * Loaded before the page's own script, and before add_data_wizard.js, which
 * is the reason any of this moved: the wizard had to leave admin_data_sources
 * so the data-package builder could open the same drawer over the package
 * being written, and it could not leave while it shared these with the
 * connections cards.
 *
 * Nothing here holds state or touches a page-specific element — that is the
 * membership test for this file. Anything with state belongs in the picker
 * layer or in the wizard component.
 */

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
function _scopeNote(data) {
  // Server marks bucket-scoped (custom access) tokens whose listing came
  // from the token's own bucketPermissions rather than the project-wide
  // listing — tell the admin the picker is intentionally partial.
  return data && data.scope === "token_buckets"
    ? `<div class="ds-scope-note">Bucket-scoped token — showing only the buckets this token can read.</div>`
    : "";
}
