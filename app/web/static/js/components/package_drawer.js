/* =====================================================================
 * package_drawer.js — bringing a Data Package into existence, from
 * wherever the reader noticed one was missing.
 *
 * This was a centred modal that lived in admin_tables.html, and it had
 * two problems the drawer fixes by construction:
 *
 *   1. It was the tallest form in the admin surface (name, slug,
 *      description, lifecycle, category, icon, colour, cover image, and
 *      a group-access matrix) inside a card sized for one decision — so
 *      on a laptop its footer sat on top of its own last field.
 *   2. Only /admin/tables carried the scaffolding, so the Packages lens
 *      — where "+ New package" actually belongs — had to LINK to it
 *      (`/admin/tables?new_package=1`). Clicking "new package" on the
 *      Packages tab switched you to the Tables tab to fill the form in.
 *
 * So it is a drawer, and it is a component: the flow opens in place on
 * whatever lens you are standing on, and the page it was opened from
 * stays visible behind it.
 *
 *   window.AgnesPackageDrawer.open({
 *     typed:     'Sales bundle',   // prefill the name
 *     chipHost:  hostEl,           // chip-input to append the new chip to
 *     onCreated: function (pkg, grantFailures, tableFailures) { … },
 *   })
 *
 * No endpoint is new here — the same three the modal used:
 *   POST /api/admin/data-packages        (create)
 *   POST /api/admin/uploads/cover-image  (cover, on pick)
 *   POST /api/admin/grants               (one per chosen group)
 *
 * Chrome: css/drawer.css (the shared drawer) + css/filter_toolbar.css
 * (`.fbar-select`, `.fbar-seg` — the product's select and segmented
 * control) + css/stack_card.css (`.cf-palette-row`, hydrated globally by
 * _app_scripts.html). A consumer links those three and nothing else.
 * ===================================================================== */
(function () {
  'use strict';

  var PKG_API = '/api/admin/data-packages';
  var REGISTRY_API = '/api/admin/registry';
  var CONNECTIONS_API = '/api/admin/source-connections';
  var GRANTS_API = '/api/admin/grants';
  var GROUPS_API = '/api/admin/groups';
  var COVER_API = '/api/admin/uploads/cover-image';

  /* Display names for the sources that group tables when no source CONNECTION
     owns them (internal tables, and the connectors that have no connection
     row). Without this the group headings are raw enum values — `bigquery`,
     `jira` — sitting beside a real project's name, which reads as leaked data
     rather than a heading. */
  var SOURCE_LABELS = {
    keboola: 'Keboola', bigquery: 'BigQuery', jira: 'Jira',
    databricks: 'Databricks', snowflake: 'Snowflake',
    internal: 'Agnes internal', local: 'Uploaded files',
  };
  function sourceLabel(t) {
    if (!t) return 'Other';
    return SOURCE_LABELS[t] || (t.charAt(0).toUpperCase() + t.slice(1));
  }

  var els = null;          // built lazily on first open
  var st = null;           // per-open state

  function api(url, opts) {
    opts = opts || {};
    opts.credentials = 'include';
    if (opts.body) opts.headers = { 'Content-Type': 'application/json' };
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var d = b && b.detail;
          throw new Error(typeof d === 'string' ? d : 'HTTP ' + r.status);
        });
      }
      return r.status === 204 ? null : r.json();
    });
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* URL-safe identifier, same normalisation the server's seed step uses. */
  function slugify(name) {
    return (name || '').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  /* ── Scaffold ─────────────────────────────────────────────────────── */

  function build() {
    if (els) return els;
    var root = document.createElement('div');
    root.className = 'ds-drawer';
    root.hidden = true;
    // This drawer owns its own Escape / backdrop handling; opt out of the
    // global handler in _app_scripts.html, which hides overlays with an
    // inline display:none and would leave our state half-open.
    root.dataset.noEscClose = '1';
    root.innerHTML =
      '<div class="ds-drawer__backdrop" data-pdw-close></div>' +
      '<div class="ds-drawer__panel" role="dialog" aria-modal="true" aria-labelledby="pdw-title">' +
      '  <div class="ds-drawer__head">' +
      '    <div class="ds-drawer__head-main">' +
      '      <h2 class="ds-drawer__title" id="pdw-title">New data package</h2>' +
      '      <p class="ds-drawer__sub">A package is the unit an analyst receives — tables reach them only through one.</p>' +
      '    </div>' +
      '    <button type="button" class="ds-drawer__x" data-pdw-close aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="ds-drawer__body">' +
      '    <section class="ds-drawer__pane is-on">' +
      '      <p class="ds-drawer__lede">Name it after what it carries. Everything below this' +
      '        stays editable on the package’s own page afterwards.</p>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-name">Name</label>' +
      '        <input type="text" id="pdw-name" autocomplete="off" placeholder="Sales bundle">' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-slug">Slug</label>' +
      '        <input type="text" id="pdw-slug" autocomplete="off" placeholder="sales-bundle">' +
      '        <p class="ds-drawer__hint" id="pdw-slug-hint">URL-safe identifier; follows the name until you edit it.</p>' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-desc">Description <span class="ds-drawer__opt">(optional)</span></label>' +
      '        <textarea id="pdw-desc" autocomplete="off" placeholder="What is in here, and who it is for."></textarea>' +
      '      </div>' +
      '      <div class="ds-drawer__row">' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-status">Status</label>' +
      '          <span class="fbar-select ds-drawer__select">' +
      '            <select id="pdw-status">' +
      '              <option value="prod" selected>Prod — ready for analyst use</option>' +
      '              <option value="poc">POC — try-before-you-buy</option>' +
      '              <option value="coming-soon">Coming soon — visible, not usable yet</option>' +
      '              <option value="draft">Draft — admin-only, hidden from analysts</option>' +
      '            </select>' +
      '          </span>' +
      '        </div>' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-category">Category <span class="ds-drawer__opt">(optional)</span></label>' +
      '          <input type="text" id="pdw-category" autocomplete="off" placeholder="e.g. Sessions &amp; Traffic">' +
      '          <p class="ds-drawer__hint">The eyebrow line above the card title in the Library.</p>' +
      '        </div>' +
      '      </div>' +
      '      <div class="ds-drawer__row">' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-icon">Icon <span class="ds-drawer__opt">(optional)</span></label>' +
      '          <input type="text" id="pdw-icon" autocomplete="off" maxlength="4" placeholder="📦">' +
      '        </div>' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-color">Colour</label>' +
      '          <div class="pdw-color">' +
      '            <div class="cf-palette-row" data-target="pdw-color"></div>' +
      '            <input type="color" id="pdw-color" value="#0EA5B5" class="ds-drawer__color">' +
      '          </div>' +
      '        </div>' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-cover-file">Cover image <span class="ds-drawer__opt">(optional)</span></label>' +
      '        <div class="pdw-cover">' +
      '          <div class="pdw-cover__preview" id="pdw-cover-preview">No image</div>' +
      '          <div class="pdw-cover__pick">' +
      '            <input type="file" id="pdw-cover-file" class="ds-drawer__file"' +
      '                   accept="image/png,image/jpeg,image/gif,image/webp">' +
      '            <input type="hidden" id="pdw-cover-url" value="">' +
      '            <p class="ds-drawer__hint">PNG / JPEG / GIF / WebP, max 5 MiB.' +
      '              <button type="button" class="ds-drawer__linkbtn" id="pdw-cover-clear" hidden>Remove</button></p>' +
      '          </div>' +
      '        </div>' +
      '      </div>' +
      // Composition, in BOTH modes. It was edit-only on the reasoning that a
      // package being created has no id to attach a table to — but the group
      // picks below are collected the same way and applied after the POST
      // answers, so "no id yet" was never the obstacle it looked like. A
      // create that cannot choose tables makes an empty package and sends the
      // admin to a second surface to fill it.
      '      <div class="ds-drawer__field" id="pdw-tables-field">' +
      '        <label for="pdw-tables-search" id="pdw-tables-label">Tables in this package</label>' +
      '        <p class="ds-drawer__hint" style="margin:0 0 8px;">Tick a table to include it.' +
      '          <strong>Buckets</strong> come from the source project — they are not Agnes containers.</p>' +
      '        <input type="text" id="pdw-tables-search" class="pdw-tables__search"' +
      '               autocomplete="off" placeholder="Search tables…">' +
      '        <div id="pdw-tables" class="pdw-tables"></div>' +
      '      </div>' +
      '      <details class="ds-drawer__disclose" id="pdw-access">' +
      '        <summary>Who gets it <span class="ds-drawer__opt">(optional)</span></summary>' +
      '        <p class="ds-drawer__hint" style="margin:8px 0 12px;">' +
      '          <strong>Optional</strong> shows the package in that group’s Library for members to add;' +
      '          <strong>Automatic</strong> puts it in their workspace on the next sync.' +
      '          Leave this closed and the package is private until you share it.</p>' +
      '        <div id="pdw-groups"></div>' +
      '      </details>' +
      '      <div class="ds-drawer__err" id="pdw-err" hidden></div>' +
      '    </section>' +
      '  </div>' +
      '  <div class="ds-drawer__foot">' +
      '    <span class="ds-drawer__foot-gap"></span>' +
      '    <button type="button" class="btn btn-secondary" data-pdw-close>Cancel</button>' +
      '    <button type="button" class="btn btn-primary" id="pdw-submit">Create package</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);

    els = {
      root: root,
      panel: root.querySelector('.ds-drawer__panel'),
      body: root.querySelector('.ds-drawer__body'),
      // Edit mode rewrites the three pieces of copy that name the verb —
      // leaving "New data package" over a form full of an existing package's
      // values is the kind of mislabel that gets a rename saved as a create.
      title: root.querySelector('#pdw-title'),
      sub: root.querySelector('.ds-drawer__sub'),
      lede: root.querySelector('.ds-drawer__lede'),
      name: root.querySelector('#pdw-name'),
      slug: root.querySelector('#pdw-slug'),
      slugHint: root.querySelector('#pdw-slug-hint'),
      desc: root.querySelector('#pdw-desc'),
      status: root.querySelector('#pdw-status'),
      category: root.querySelector('#pdw-category'),
      icon: root.querySelector('#pdw-icon'),
      color: root.querySelector('#pdw-color'),
      coverFile: root.querySelector('#pdw-cover-file'),
      coverUrl: root.querySelector('#pdw-cover-url'),
      coverPreview: root.querySelector('#pdw-cover-preview'),
      coverClear: root.querySelector('#pdw-cover-clear'),
      access: root.querySelector('#pdw-access'),
      groups: root.querySelector('#pdw-groups'),
      tablesField: root.querySelector('#pdw-tables-field'),
      tablesLabel: root.querySelector('#pdw-tables-label'),
      tablesSearch: root.querySelector('#pdw-tables-search'),
      tables: root.querySelector('#pdw-tables'),
      err: root.querySelector('#pdw-err'),
      submit: root.querySelector('#pdw-submit'),
    };

    root.addEventListener('click', function (e) {
      if (e.target.closest('[data-pdw-close]')) close();
    });
    // Document-level, not panel-level: focus can legitimately sit outside
    // the panel (a click on the backdrop, the native colour picker), and
    // Escape has to close the drawer from there too.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && st) { e.stopPropagation(); close(); }
    });
    // The slug follows the name until the admin types in it — after that it
    // is theirs. (The modal derived it only at open, so typing a name into
    // an already-open form left the slug empty and the create silently
    // bounced on a required field.)
    els.name.addEventListener('input', function () {
      if (!st || st.slugTouched) return;
      els.slug.value = slugify(els.name.value);
    });
    els.slug.addEventListener('input', function () {
      if (st) st.slugTouched = true;
    });
    els.name.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); els.submit.click(); }
    });
    els.coverFile.addEventListener('change', onCoverPicked);
    els.coverClear.addEventListener('click', clearCover);
    els.access.addEventListener('toggle', function () {
      if (els.access.open) hydrateGroups();
    });
    // Tier segments inside the group list: a group is granted when it is
    // ticked, and ticking it defaults to Optional — the safe tier, since
    // Automatic writes into every member's workspace on their next sync.
    els.groups.addEventListener('click', function (e) {
      var btn = e.target.closest('.fbar-seg__btn');
      if (!btn) return;
      var row = btn.closest('[data-group-id]');
      var box = row.querySelector('input[type="checkbox"]');
      box.checked = true;
      row.querySelectorAll('.fbar-seg__btn').forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
    });
    els.groups.addEventListener('change', function (e) {
      var box = e.target.closest('input[type="checkbox"]');
      if (!box) return;
      var row = box.closest('[data-group-id]');
      var segs = row.querySelectorAll('.fbar-seg__btn');
      if (box.checked && !row.querySelector('.fbar-seg__btn.is-active')) {
        segs[0].classList.add('is-active');
        segs[0].setAttribute('aria-pressed', 'true');
      } else if (!box.checked) {
        segs.forEach(function (b) {
          b.classList.remove('is-active');
          b.setAttribute('aria-pressed', 'false');
        });
      }
    });
    // Re-render on search rather than filtering the DOM: the list is
    // re-sorted (members first) as ticks change, so one renderer owns both.
    els.tablesSearch.addEventListener('input', function () { if (st) renderTables(); });
    els.tables.addEventListener('change', function (e) {
      if (!st) return;
      var box = e.target.closest('input[type="checkbox"]');
      if (!box) return;
      var id = box.getAttribute('data-table-id');
      if (id) {
        if (box.checked) st.tablesSelected.add(id); else st.tablesSelected.delete(id);
        // Re-render so the group boxes above it re-tally — a bucket that reads
        // "all" after one of its tables was unticked is worse than no summary.
        renderTables();
        return;
      }
      if (!box.classList.contains('pdw-grp__box')) return;
      // A group box is a bulk action on what is under it, and `indeterminate`
      // reads as unchecked to `.checked` — so a click on a partly-selected
      // group means "select the rest", never "clear it".
      var want = box.checked;
      tablesUnder(box.getAttribute('data-project'), box.getAttribute('data-bucket')).forEach(function (t) {
        if (want) st.tablesSelected.add(t.id); else st.tablesSelected.delete(t.id);
      });
      renderTables();
    });
    // A click on the group's checkbox must not also open/close the <details>
    // it lives in the <summary> of.
    els.tables.addEventListener('click', function (e) {
      if (e.target.closest('.pdw-grp__box')) e.stopPropagation();
    });
    els.submit.addEventListener('click', submit);
    return els;
  }

  /* ── Open / close ─────────────────────────────────────────────────── */

  /* ── Mode ─────────────────────────────────────────────────────────────
     Two verbs, one form. Create writes POST + the grants the access matrix
     collects; edit writes PUT and the SAME matrix, hydrated from the grants
     that already exist and DIFFED on Save (add / retier / revoke) — so who
     gets a package is editable on whatever page the drawer opens on, and the
     admin workspace trip is optional for this errand too. The package's own
     page keeps what a drawer cannot hold: the delivery read-out and the
     per-group people counts. */

  function applyMode(mode) {
    var editing = mode === 'edit';
    els.title.textContent = editing ? 'Edit data package' : 'New data package';
    els.sub.textContent = editing
      ? 'What analysts read before they add it.'
      : 'A package is the unit an analyst receives — tables reach them only through one.';
    els.lede.hidden = editing;
    // The slug is in URLs and in grant rows, and `PUT /{id}` carries no slug
    // field — an editable box would silently discard what was typed.
    els.slug.disabled = editing;
    els.slug.title = editing ? 'Slug is permanent — used in URLs and grants' : '';
    // The hint has to move with the field: "follows the name until you edit
    // it" over a box you cannot type in describes the other mode's behaviour.
    els.slugHint.textContent = editing
      ? 'Permanent — it is in this package’s URL and in every grant written against it.'
      : 'URL-safe identifier; follows the name until you edit it.';
    els.access.hidden = false;
    // Both modes carry the list; only the label differs, because on a create
    // there is no existing membership for "in this package" to refer to.
    els.tablesLabel.textContent = editing ? 'Tables in this package' : 'Tables to include';
    els.submit.textContent = editing ? 'Save changes' : 'Create package';
  }

  /* ── Composition ──────────────────────────────────────────────────────
     Membership is DIFFED and applied on Save, not written on each tick: the
     drawer offers Cancel, and a click that had already hit the API would
     make that button a lie. */

  /* The registry is a flat list of ~500 rows across a handful of projects and
     a hundred-odd buckets, which is how the source systems are actually
     organised — so a package is almost never "these 3 arbitrary tables", it is
     "this bucket" or "everything from that project". Rendering it flat made
     the admin tick that structure back in by hand, one row at a time.

     Two levels, PROJECT › BUCKET, because those are the two containers the
     data really has: a project is a source connection, a bucket is its own
     grouping inside it. Neither is an Agnes concept — the package is. Both
     levels carry a tri-state box (all / some / none of their tables), so
     "add this bucket" is one click and stays truthful when one table under it
     is unticked. */

  function tableGroups() {
    var q = (els.tablesSearch.value || '').trim().toLowerCase();
    var projects = [];
    var byProject = {};
    st.registry.forEach(function (t) {
      if (q && (t.id + ' ' + (t.name || '') + ' ' + (t.bucket || '') + ' ' + (t.project || ''))
                 .toLowerCase().indexOf(q) === -1) return;
      var pk = t.project || 'Other';
      if (!byProject[pk]) {
        byProject[pk] = { key: pk, label: pk, buckets: {}, order: [] };
        projects.push(byProject[pk]);
      }
      var p = byProject[pk];
      // A table with no bucket still needs a home; naming it after the source
      // is truer than inventing an empty group.
      var bk = t.bucket || (t.source_type ? t.source_type : 'Ungrouped');
      if (!p.buckets[bk]) { p.buckets[bk] = { key: bk, label: bk, tables: [] }; p.order.push(bk); }
      p.buckets[bk].tables.push(t);
    });
    projects.forEach(function (p) {
      p.order.sort();
      p.order.forEach(function (bk) {
        p.buckets[bk].tables.sort(function (a, b) {
          return String(a.name || a.id).localeCompare(String(b.name || b.id));
        });
      });
    });
    projects.sort(function (a, b) { return a.label.localeCompare(b.label); });
    return projects;
  }

  function tallyState(tables) {
    var on = 0;
    tables.forEach(function (t) { if (st.tablesSelected.has(t.id)) on++; });
    return on === 0 ? 'none' : (on === tables.length ? 'all' : 'some');
  }

  function boxAttrs(state) {
    return (state === 'all' ? ' checked' : '') + (state === 'some' ? ' data-indeterminate="1"' : '');
  }

  function renderTables() {
    var projects = tableGroups();
    if (!projects.length) {
      els.tables.innerHTML = '<p class="ds-drawer__hint">No table matches that.</p>';
      return;
    }
    var searching = !!(els.tablesSearch.value || '').trim();
    var html = projects.map(function (p) {
      var pTables = [];
      p.order.forEach(function (bk) { pTables = pTables.concat(p.buckets[bk].tables); });
      var pState = tallyState(pTables);
      // Open when searching (the match is the point), or when the group
      // already contributes to the package — a member you cannot see is a
      // member you cannot remove.
      var pOpen = searching || pState !== 'none';
      var buckets = p.order.map(function (bk) {
        var b = p.buckets[bk];
        var bState = tallyState(b.tables);
        var bOpen = searching || bState !== 'none';
        var rows = b.tables.map(function (t) {
          var on = st.tablesSelected.has(t.id);
          var sub = [t.source_type, t.query_mode].filter(Boolean).map(esc).join(' · ');
          return '<label class="pdw-tables__row">' +
            '<input type="checkbox" data-table-id="' + esc(t.id) + '"' + (on ? ' checked' : '') +
            ' aria-label="' + esc(t.name || t.id) + '">' +
            '<span class="pdw-tables__g"><span class="pdw-tables__n">' + esc(t.name || t.id) + '</span>' +
            (sub ? '<span class="pdw-tables__s">' + sub + '</span>' : '') + '</span></label>';
        }).join('');
        return '<details class="pdw-grp pdw-grp--bucket"' + (bOpen ? ' open' : '') + '>' +
          '<summary class="pdw-grp__sum">' +
          '<input type="checkbox" class="pdw-grp__box" data-group="bucket"' +
          ' data-project="' + esc(p.key) + '" data-bucket="' + esc(b.key) + '"' + boxAttrs(bState) +
          ' aria-label="All tables in ' + esc(b.label) + '">' +
          '<span class="pdw-grp__name">' + esc(b.label) + '</span>' +
          '<span class="pdw-grp__n">' + b.tables.length + '</span>' +
          '</summary>' + rows + '</details>';
      }).join('');
      return '<details class="pdw-grp pdw-grp--project"' + (pOpen ? ' open' : '') + '>' +
        '<summary class="pdw-grp__sum">' +
        '<input type="checkbox" class="pdw-grp__box" data-group="project"' +
        ' data-project="' + esc(p.key) + '"' + boxAttrs(pState) +
        ' aria-label="All tables in ' + esc(p.label) + '">' +
        '<span class="pdw-grp__name">' + esc(p.label) + '</span>' +
        '<span class="pdw-grp__n">' + pTables.length + '</span>' +
        '</summary>' + buckets + '</details>';
    }).join('');
    els.tables.innerHTML = html;
    // `indeterminate` is a PROPERTY with no HTML attribute, so it cannot ride
    // the markup above and has to be set after the paint.
    els.tables.querySelectorAll('[data-indeterminate]').forEach(function (b) {
      b.indeterminate = true;
    });
  }

  /* Every table under a group, honouring the current search — ticking a group
     must mean what the reader can see under it, not the whole registry. */
  function tablesUnder(project, bucket) {
    var out = [];
    tableGroups().forEach(function (p) {
      if (p.key !== project) return;
      p.order.forEach(function (bk) {
        if (bucket && bk !== bucket) return;
        out = out.concat(p.buckets[bk].tables);
      });
    });
    return out;
  }

  /* `pkgId` is null on a create: the registry and the project names are
     fetched exactly the same way, and the member set is simply empty. */
  function hydrateTables(pkgId) {
    Promise.all([
      pkgId ? api(PKG_API + '/' + encodeURIComponent(pkgId))
            : Promise.resolve({ tables: [] }),
      api(REGISTRY_API),
      // The registry carries `connection_id`, not the project's NAME. A raw
      // uuid is not a group heading anyone can read, so resolve it here; a
      // failed lookup degrades to grouping by source type rather than
      // failing the list.
      api(CONNECTIONS_API).catch(function () { return []; }),
    ]).then(function (res) {
      if (!st || st.pkgId !== pkgId) return;
      var pkg = res[0];
      var reg = res[1];
      var conns = res[2];
      var connName = {};
      (Array.isArray(conns) ? conns : (conns.connections || conns.items || [])).forEach(function (c) {
        if (c && c.id) connName[c.id] = c.name || c.id;
      });
      st.registry = (Array.isArray(reg) ? reg : (reg.tables || [])).map(function (t) {
        return {
          id: t.id, name: t.name || t.id, bucket: t.bucket || '',
          source_type: t.source_type || '', query_mode: t.query_mode || '',
          // Project = the source connection this table came through. Tables
          // with no connection (internal, and the derived sources) fall back
          // to the source's own name, which is the truthful grouping for them.
          project: connName[t.connection_id] || sourceLabel(t.source_type),
        };
      });
      var members = (pkg.tables || []).map(function (t) { return t.id; });
      st.tablesOriginal = new Set(members);
      st.tablesSelected = new Set(members);
      // A member the registry no longer lists still has to be shown — a row
      // you cannot see is a row you cannot remove.
      var known = new Set(st.registry.map(function (t) { return t.id; }));
      members.forEach(function (id) {
        if (!known.has(id)) {
          st.registry.push({ id: id, name: id, bucket: 'Ungrouped', source_type: '',
                             query_mode: '', project: 'Other' });
        }
      });
      renderTables();
    }).catch(function (e) {
      els.tables.innerHTML = '<p class="ds-drawer__hint">Could not load tables: ' + esc(e.message) + '</p>';
    });
  }

  function open(opts) {
    opts = opts || {};
    build();
    var mode = opts.mode === 'edit' ? 'edit' : 'create';
    st = {
      mode: mode,
      pkgId: opts.pkgId || null,
      chipHost: opts.chipHost || null,
      onCreated: opts.onCreated || function () {},
      onSaved: opts.onSaved || function () {},
      slugTouched: mode === 'edit',
      groupsLoaded: false,
      grantsLoaded: false,
      grantsOriginal: new Map(),
      registry: [],
      tablesOriginal: new Set(),
      tablesSelected: new Set(),
      restoreFocus: document.activeElement,
    };
    var typed = opts.typed || '';
    els.name.value = typed;
    els.slug.value = slugify(typed);
    els.desc.value = '';
    els.status.value = 'prod';
    els.category.value = '';
    els.icon.value = '';
    els.color.value = '#0EA5B5';
    els.color.dispatchEvent(new Event('change', { bubbles: true }));
    // Reset the cover on every open so a picked-then-cancelled image can
    // never ride along into the next package.
    els.coverFile.value = '';
    els.coverUrl.value = '';
    renderCover('');
    els.access.open = false;
    els.groups.innerHTML = '';
    els.err.hidden = true;
    els.submit.disabled = false;
    applyMode(mode);

    els.root.hidden = false;
    els.root.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    els.body.scrollTop = 0;
    els.tablesSearch.value = '';
    els.tables.innerHTML = '<p class="ds-drawer__hint">Loading…</p>';
    if (mode === 'edit') {
      hydratePackage(opts.pkgId);
      hydrateTables(opts.pkgId);
      // Sharing is part of the edit form: open the disclose so the current
      // grants are visible without a click, and hydrate rows + grants (the
      // paint runs when the LATER of the two fetches lands).
      els.access.open = true;
      hydrateGroups();
      hydrateGrants(opts.pkgId);
    } else {
      // The registry is the same fetch in create mode; only the member set
      // differs (empty), so the picker is usable before the package exists.
      hydrateTables(null);
    }
    setTimeout(function () { els.name.focus({ preventScroll: true }); }, 60);
  }

  /* Fields are filled from the API rather than from whatever the caller had
     on screen: the page that opens this may be rendering a stale row, and a
     PUT built from stale values silently reverts someone else's edit. */
  function hydratePackage(pkgId) {
    els.submit.disabled = true;
    api(PKG_API + '/' + encodeURIComponent(pkgId)).then(function (pkg) {
      if (!st || st.pkgId !== pkgId) return;   // drawer moved on while we waited
      els.name.value = pkg.name || '';
      els.slug.value = pkg.slug || '';
      els.desc.value = pkg.description || '';
      els.status.value = pkg.status || 'prod';
      els.category.value = pkg.category || '';
      els.icon.value = pkg.icon || '';
      if (pkg.color) {
        els.color.value = pkg.color;
        els.color.dispatchEvent(new Event('change', { bubbles: true }));
      }
      els.coverUrl.value = pkg.cover_image_url || '';
      renderCover(pkg.cover_image_url || '');
      els.submit.disabled = false;
    }).catch(function (e) {
      fail('Could not load the package: ' + e.message);
    });
  }

  function close() {
    if (!els) return;
    els.root.classList.remove('is-open');
    els.root.hidden = true;
    document.body.style.overflow = '';
    var s = st;
    st = null;
    if (s && s.restoreFocus && s.restoreFocus.focus) {
      s.restoreFocus.focus({ preventScroll: true });
    }
  }

  function fail(msg) {
    els.err.textContent = msg;
    els.err.hidden = false;
    els.submit.disabled = false;
  }

  /* ── Cover image ──────────────────────────────────────────────────────
     Uploaded on pick rather than on save: the admin gets the preview and
     any failure immediately, and the returned URL rides along on the
     create body. */

  function renderCover(url) {
    els.coverClear.hidden = !url;
    if (!url) { els.coverPreview.textContent = 'No image'; return; }
    els.coverPreview.innerHTML = '<img src="' + esc(url) + '" alt="">';
  }

  function clearCover() {
    els.coverFile.value = '';
    els.coverUrl.value = '';
    renderCover('');
  }

  function onCoverPicked() {
    var file = els.coverFile.files && els.coverFile.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      clearCover();
      fail('That image is larger than 5 MiB.');
      return;
    }
    els.err.hidden = true;
    els.coverPreview.textContent = 'Uploading…';
    var fd = new FormData();
    fd.append('file', file);
    fetch(COVER_API, { method: 'POST', credentials: 'include', body: fd })
      .then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            throw new Error((b && b.detail) || 'HTTP ' + r.status);
          });
        }
        return r.json();
      })
      .then(function (body) {
        els.coverUrl.value = body.url || '';
        renderCover(els.coverUrl.value);
      })
      .catch(function (e) {
        clearCover();
        fail('The cover image did not upload: ' + e.message);
      });
  }

  /* ── Who gets it ──────────────────────────────────────────────────────
     Groups are lazy — most packages are created and shared later from the
     package's own page or a group's Access tab, and this is a request the
     collapsed state should not have made. */

  function hydrateGroups() {
    if (st.groupsLoaded) return;
    st.groupsLoaded = true;
    els.groups.innerHTML = '<p class="ds-drawer__empty">Loading groups…</p>';
    api(GROUPS_API).then(function (body) {
      var groups = Array.isArray(body) ? body : (body && body.groups) || [];
      if (!groups.length) {
        els.groups.innerHTML = '<p class="ds-drawer__empty">No groups yet — make one in '
          + '<a href="/admin/groups">Access</a>, then share this from the package’s page.</p>';
        return;
      }
      els.groups.innerHTML = groups.map(function (g) {
        var gid = String(g.id || g.name || '');
        var gname = String(g.name || gid);
        var n = g.member_count;
        var sub = typeof n === 'number' ? n + (n === 1 ? ' member' : ' members') : '';
        return '<div class="pdw-pick" data-group-id="' + esc(gid) + '">'
          + '<input type="checkbox" aria-label="Share with ' + esc(gname) + '">'
          + '<span class="pdw-pick__txt">'
          + '<span class="pdw-pick__name">' + esc(gname) + '</span>'
          + (sub ? '<span class="pdw-pick__sub">' + esc(sub) + '</span>' : '')
          + '</span>'
          // The tier's system word (what the API and the audit log call it)
          // rides the accessible name rather than a bare `title`: a tooltip
          // is not a label, and these two buttons are the one place in the
          // product where the reader's word and the system's differ.
          + '<span class="fbar-seg" role="group" aria-label="Access tier for ' + esc(gname) + '">'
          + '<button type="button" class="fbar-seg__btn" data-tier="available" aria-pressed="false"'
          + ' aria-label="Optional (available)">Optional</button>'
          + '<button type="button" class="fbar-seg__btn" data-tier="required" aria-pressed="false"'
          + ' aria-label="Automatic (required)">Automatic</button>'
          + '</span>'
          + '</div>';
      }).join('');
      // Edit mode may already hold the grants — the rows just appeared, so
      // paint them now (no-op in create mode / before grants load).
      paintGrantRows();
    }).catch(function (e) {
      st.groupsLoaded = false;
      els.groups.innerHTML = '<p class="ds-drawer__empty">Could not load the groups: '
        + esc(e.message) + '</p>';
    });
  }

  /* Edit mode: the grants that exist NOW, so the matrix shows the truth and
     Save can diff against it. Keyed by group_id; the grant row id rides
     along because retier is PUT /grants/{id} and revoke is DELETE on it. */
  function hydrateGrants(pkgId) {
    api(GRANTS_API + '?resource_type=data_package').then(function (rows) {
      if (!st || st.pkgId !== pkgId) return; // drawer re-opened on another package meanwhile
      st.grantsOriginal = new Map();
      (Array.isArray(rows) ? rows : []).forEach(function (r) {
        if (String(r.resource_id) === String(pkgId)) {
          st.grantsOriginal.set(String(r.group_id), { id: r.id, requirement: r.requirement });
        }
      });
      st.grantsLoaded = true;
      paintGrantRows();
    }).catch(function () {
      // Grants unreadable → the matrix stays a create-shaped blank and Save
      // must NOT diff against an empty map (it would read as "revoke all").
      st.grantsLoaded = false;
    });
  }

  /* Tick + tier every row according to grantsOriginal. Runs after whichever
     of the two fetches (groups list, grants) lands last. */
  function paintGrantRows() {
    if (!st || !st.grantsLoaded) return;
    els.groups.querySelectorAll('[data-group-id]').forEach(function (row) {
      var g = st.grantsOriginal.get(row.getAttribute('data-group-id'));
      var box = row.querySelector('input[type="checkbox"]');
      if (box) box.checked = !!g;
      row.querySelectorAll('.fbar-seg__btn').forEach(function (b) {
        var on = !!g && b.dataset.tier === (g.requirement === 'required' ? 'required' : 'available');
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    });
  }

  /* Chosen tiers, as [{group_id, requirement}]. A ticked group with no tier
     clicked means Optional. */
  function chosenGrants() {
    var out = [];
    els.groups.querySelectorAll('[data-group-id]').forEach(function (row) {
      var box = row.querySelector('input[type="checkbox"]');
      if (!box || !box.checked) return;
      var on = row.querySelector('.fbar-seg__btn.is-active');
      out.push({
        group_id: row.getAttribute('data-group-id'),
        requirement: (on && on.dataset.tier) || 'available',
      });
    });
    return out;
  }

  /* ── Create ───────────────────────────────────────────────────────── */

  function submit() {
    var name = els.name.value.trim();
    // The slug follows the name, so a create can only be missing one if the
    // admin cleared it by hand — re-derive rather than bounce them.
    var slug = els.slug.value.trim() || slugify(name);
    els.slug.value = slug;
    els.err.hidden = true;
    if (!name) {
      fail('A name is required — the slug derives from it.');
      els.name.focus({ preventScroll: true });
      return;
    }
    if (!slug) {
      fail('That name has no URL-safe characters in it — give the slug a value.');
      els.slug.focus({ preventScroll: true });
      return;
    }

    if (st && st.mode === 'edit') {
      els.submit.disabled = true;
      els.submit.textContent = 'Saving…';
      var pkgId = st.pkgId;
      // `category` and `cover_image_url` honour an empty-string-clears
      // contract server-side (see update_data_package), so an emptied field
      // must send "" rather than null — null means "leave unchanged", which
      // would make clearing a category impossible from here.
      api(PKG_API + '/' + encodeURIComponent(pkgId), {
        method: 'PUT',
        body: JSON.stringify({
          name: name,
          description: els.desc.value.trim() || null,
          icon: els.icon.value.trim() || null,
          color: els.color.value.trim() || null,
          cover_image_url: els.coverUrl.value || '',
          status: els.status.value || 'prod',
          category: els.category.value.trim(),
        }),
      }).then(function (saved) {
        // Membership diff, after the metadata write. Both directions are
        // idempotent server-side, so a retry cannot double-apply.
        var added = [], removed = [];
        st.tablesSelected.forEach(function (id) { if (!st.tablesOriginal.has(id)) added.push(id); });
        st.tablesOriginal.forEach(function (id) { if (!st.tablesSelected.has(id)) removed.push(id); });
        var calls = added.map(function (id) {
          return api(PKG_API + '/' + encodeURIComponent(pkgId) + '/tables', {
            method: 'POST', body: JSON.stringify({ table_id: id }),
          });
        }).concat(removed.map(function (id) {
          return api(PKG_API + '/' + encodeURIComponent(pkgId) + '/tables/' + encodeURIComponent(id), {
            method: 'DELETE',
          });
        }));
        // Sharing diff — only when the current grants actually loaded, so a
        // failed hydrate can never be misread as "revoke everything". Same
        // Save, same idempotent-ops rule as the table diff above.
        if (st.grantsLoaded) {
          var desired = new Map();
          chosenGrants().forEach(function (g) { desired.set(String(g.group_id), g.requirement); });
          desired.forEach(function (req, gid) {
            var cur = st.grantsOriginal.get(gid);
            if (!cur) {
              calls.push(api(GRANTS_API, {
                method: 'POST',
                body: JSON.stringify({
                  group_id: gid, resource_type: 'data_package',
                  resource_id: pkgId, requirement: req,
                }),
              }));
            } else if ((cur.requirement === 'required') !== (req === 'required')) {
              calls.push(api(GRANTS_API + '/' + encodeURIComponent(cur.id), {
                method: 'PUT', body: JSON.stringify({ requirement: req }),
              }));
            }
          });
          st.grantsOriginal.forEach(function (cur, gid) {
            if (!desired.has(gid)) {
              calls.push(api(GRANTS_API + '/' + encodeURIComponent(cur.id), { method: 'DELETE' }));
            }
          });
        }
        return Promise.allSettled(calls).then(function (results) {
          var failures = results.filter(function (r) { return r.status === 'rejected'; }).length;
          if (failures) {
            // The metadata IS saved by now, so this cannot be reported as a
            // failed save — name what actually did not happen and keep the
            // drawer open on the list the admin needs to look at.
            els.submit.disabled = false;
            els.submit.textContent = 'Save changes';
            fail(failures + ' change' + (failures === 1 ? '' : 's') +
                 ' (tables or sharing) could not be applied. The other details were saved.');
            hydrateTables(pkgId);
            hydrateGrants(pkgId);
            return;
          }
          var done = (st && st.onSaved) || function () {};
          close();
          try { done(saved || { id: pkgId, name: name }); } catch (_) { /* the caller's problem */ }
        });
      }).catch(function (e) {
        els.submit.disabled = false;
        els.submit.textContent = 'Save changes';
        fail('Could not save the package: ' + e.message);
      });
      return;
    }

    els.submit.disabled = true;
    els.submit.textContent = 'Creating…';
    var grants = chosenGrants();
    // Snapshot both collected sets before the POST, for the same reason the
    // group picks are snapshotted: `st` belongs to the open drawer, and the
    // writes below happen after it has been told to close.
    var tableIds = st ? Array.from(st.tablesSelected) : [];

    api(PKG_API, {
      method: 'POST',
      body: JSON.stringify({
        name: name,
        slug: slug,
        description: els.desc.value.trim() || null,
        icon: els.icon.value.trim() || null,
        color: els.color.value.trim() || null,
        cover_image_url: els.coverUrl.value || null,
        status: els.status.value || 'prod',
        category: els.category.value.trim() || null,
      }),
    }).then(function (created) {
      // The create endpoint answers with `{id}` and nothing else, so the
      // package handed to the caller is what we sent plus that id — a
      // callback reading `pkg.name` would otherwise get `undefined` (which
      // is exactly what the chip and the toast used to show).
      var pkg = { id: created.id, name: name, slug: slug };
      // Grants and tables are both secondary: a failure in either does NOT
      // roll the package back (it exists, and both are writable from its own
      // page), so the counts ride out to the caller for its own message.
      // They are counted SEPARATELY — "3 grants failed" and "3 tables failed"
      // send an admin to different places, so one merged number would be a
      // worse message than either.
      var grantCalls = grants.map(function (g) {
        return api(GRANTS_API, {
          method: 'POST',
          body: JSON.stringify({
            group_id: g.group_id,
            resource_type: 'data_package',
            resource_id: pkg.id,
            requirement: g.requirement,
          }),
        });
      });
      // Membership is written only now, because only now is there an id to
      // attach it to — the same collect-then-apply order the group picks use.
      var tableCalls = tableIds.map(function (id) {
        return api(PKG_API + '/' + encodeURIComponent(pkg.id) + '/tables', {
          method: 'POST', body: JSON.stringify({ table_id: id }),
        });
      });
      return Promise.all([
        Promise.allSettled(grantCalls),
        Promise.allSettled(tableCalls),
      ]).then(function (settled) {
        function rejected(r) { return r.status === 'rejected'; }
        var failures = settled[0].filter(rejected).length;
        var tableFailures = settled[1].filter(rejected).length;
        var host = st && st.chipHost;
        var done = (st && st.onCreated) || function () {};
        if (host && host.addChip) host.addChip({ id: pkg.id, name: pkg.name });
        close();
        // The package exists by now, so a caller's own error must not be
        // reported as a failed create on a drawer that has already closed.
        try { done(pkg, failures, tableFailures); } catch (_) { /* the caller's problem */ }
      });
    }).catch(function (e) {
      els.submit.textContent = 'Create package';
      fail('Could not create the package: ' + e.message);
    });
  }

  window.AgnesPackageDrawer = { open: open, close: close };
})();
