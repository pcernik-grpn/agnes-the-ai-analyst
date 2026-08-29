/* =====================================================================
 * package_drawer.js — bringing a Data Package into existence, from
 * wherever the reader noticed one was missing.
 *
 * This was a centred modal that lived in admin_tables.html, and it had
 * two problems the drawer fixes by construction:
 *
 *   1. It was the tallest form in the admin surface (name, slug,
 *      description, lifecycle, category, and
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
    bindConversation(root);
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
      // Icon, Colour and Cover image are GONE. Under the paper/rail redesign
      // the resource hero draws a kind glyph (`cards.kind_glyph`), so a
      // package's presentation is decided by its KIND rather than by three
      // fields an admin had to fill in for every package they made. The
      // detail page no longer paints a cover either — see
      // catalog_package_detail.html.
      // Composition, in BOTH modes. It was edit-only on the reasoning that a
      // package being created has no id to attach a table to — but the group
      // picks below are collected the same way and applied after the POST
      // answers, so "no id yet" was never the obstacle it looked like. A
      // create that cannot choose tables makes an empty package and sends the
      // admin to a second surface to fill it.
      // The panel shows THE PACKAGE, not the warehouse. It used to render the
      // whole registry — ~500 rows in a project › bucket tree with tri-state
      // boxes — with the members ticked somewhere inside it, so the five
      // tables you had chosen were five ticks scattered through a hundred
      // collapsed groups, and a table the conversation PROPOSED landed
      // somewhere you would never see it. The tree itself is good and is kept
      // verbatim — it moved inside the picker, where browsing belongs.
      '      <div class="ds-drawer__field" id="pdw-tables-field">' +
      '        <label id="pdw-tables-label">Tables in this package</label>' +
      '        <p class="ds-drawer__hint" style="margin:0 0 8px;">What an analyst receives.' +
      '          <strong>Buckets</strong> come from the source project — they are not Agnes containers.</p>' +
      '        <div id="pdw-tables" class="pdw-tables"></div>' +
      '        <p class="ds-drawer__hint" id="pdw-tables-reach" hidden></p>' +
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
      '  <div id="pdw-picker"></div>' +
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
      access: root.querySelector('#pdw-access'),
      groups: root.querySelector('#pdw-groups'),
      tablesField: root.querySelector('#pdw-tables-field'),
      tablesLabel: root.querySelector('#pdw-tables-label'),
      tables: root.querySelector('#pdw-tables'),
      tablesReach: root.querySelector('#pdw-tables-reach'),
      picker: root.querySelector('#pdw-picker'),
      err: root.querySelector('#pdw-err'),
      submit: root.querySelector('#pdw-submit'),
      foot: root.querySelector('.ds-drawer__foot'),
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
    // The search lives in the picker now, so it is delegated: the modal is
    // re-rendered on every tick and a listener bound to the input would die
    // with it.
    els.picker.addEventListener('input', function (e) {
      if (!st) return;
      if (e.target.getAttribute && e.target.getAttribute('data-ag-search') === 'pdw-tables') {
        pickerQuery = e.target.value;
        renderPickerRows();
      }
    });
    els.picker.addEventListener('change', function (e) {
      if (!st) return;
      var box = e.target.closest('input[type="checkbox"]');
      if (!box) return;
      var id = box.getAttribute('data-table-id');
      if (id) {
        if (box.checked) st.tablesSelected.add(id); else st.tablesSelected.delete(id);
        // Re-render so the group boxes above it re-tally — a bucket that reads
        // "all" after one of its tables was unticked is worse than no summary.
        renderPickerRows();
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
      renderPickerRows();
    });
    els.picker.addEventListener('click', function (e) {
      // A click on the group's checkbox must not also open/close the <details>
      // it lives in the <summary> of.
      if (e.target.closest('.pdw-grp__box')) { e.stopPropagation(); return; }
      if (e.target.closest('[data-ag-pick-close]')) { closePicker(); return; }
      // Outside the card but inside the overlay — the standard way out.
      if (e.target.hasAttribute && e.target.hasAttribute('data-ag-pick-backdrop')) closePicker();
    });
    // Remove, and the way back in, both live on the panel.
    els.tables.addEventListener('click', function (e) {
      if (!st) return;
      if (e.target.closest('[data-pdw-openpick]')) { openPicker(); return; }
      var rm = e.target.closest('[data-pdw-unpick]');
      if (rm) {
        st.tablesSelected.delete(rm.getAttribute('data-pdw-unpick'));
        renderTables();
      }
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
    // The shell header carries the same title in builder mode.
    if (els.shellTitle) els.shellTitle.textContent = els.title.textContent;
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
    // One label for both modes now. It used to read "Tables to include" on a
    // create, which was a instruction to go ticking; the panel shows what IS
    // in the package in either mode, so the noun is the same either way.
    els.tablesLabel.textContent = 'Tables in this package';
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
    var q = (pickerQuery || '').trim().toLowerCase();
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

  /* Panel state: only what is in the package. */
  var pickerQuery = '', pickerOpen = false;

  function selectedTables() {
    if (!st) return [];
    var byId = {};
    st.registry.forEach(function (t) { byId[t.id] = t; });
    var out = [];
    st.tablesSelected.forEach(function (id) {
      out.push(byId[id] || { id: id, name: id, source_type: '', query_mode: '', project: '' });
    });
    out.sort(function (a, b) { return String(a.name || a.id).localeCompare(String(b.name || b.id)); });
    return out;
  }

  /* An admin has to know `query_mode IN ('local','materialized') AND NOT
     server_only` to predict what a package actually delivers — see
     app/api/data.py::_DISTRIBUTABLE_QUERY_MODES. Say it instead. */
  function reachCount(rows) {
    return rows.filter(function (t) {
      return !t.server_only && (t.query_mode === 'local' || t.query_mode === 'materialized');
    }).length;
  }

  function renderTables() {
    if (!st) return;
    var rows = selectedTables();
    var body;
    if (!rows.length) {
      // Same shape as the agent builder's empty slots (`emptySlot` in
      // agents.html → .ag-slot): a bold line naming the state, one sentence
      // on how to leave it, then the add row. A one-line grey sentence reads
      // as a caption on a broken list rather than as an invitation.
      body = '<div class="ag-slot">' +
        '<p class="ag-slot-head">Nothing in it yet.</p>' +
        '<p class="ag-slot-body">Say what it should carry — “our sales pipeline tables” — ' +
        'and the builder proposes them. Or add them by hand.</p>' +
      '</div>';
    } else {
      body = '<div class="ag-rows">' + rows.map(function (t) {
        // Dedupe: for an internal table the project, the source type and the
        // query mode are all the same word, and "internal · internal ·
        // internal" reads as a rendering bug.
        var seen = {};
        var sub = [t.project, t.source_type, t.query_mode].filter(function (v) {
          var k = String(v || '').toLowerCase();
          if (!k || seen[k]) return false;
          seen[k] = 1;
          return true;
        }).map(esc).join(' · ');
        return '<div class="ag-row">' +
          '<div class="ag-row-body">' +
            '<div class="ag-row-name">' + esc(t.name || t.id) + '</div>' +
            (sub ? '<div class="ag-row-desc">' + sub + '</div>' : '') +
          '</div>' +
          '<button type="button" class="ag-tglbtn ag-tglbtn--rm" data-pdw-unpick="' + esc(t.id) + '">Remove</button>' +
        '</div>';
      }).join('') + '</div>';
    }
    els.tables.innerHTML = body +
      '<button type="button" class="ag-addrow" data-pdw-openpick>+ Add tables</button>';
    if (els.tablesLabel) {
      var count = els.tablesLabel.querySelector('.pdw-count');
      if (!count) {
        count = document.createElement('span');
        count.className = 'pdw-count';
        els.tablesLabel.appendChild(count);
      }
      count.textContent = rows.length
        ? rows.length + (rows.length === 1 ? ' table' : ' tables')
        : 'none yet';
    }
    if (els.tablesReach) {
      var reach = reachCount(rows);
      els.tablesReach.hidden = !rows.length;
      els.tablesReach.textContent = rows.length
        ? (reach === rows.length
            ? (reach === 1 ? 'This table reaches an analyst’s laptop through agnes pull.'
                           : 'All ' + reach + ' reach an analyst’s laptop through agnes pull.')
            : reach + ' of ' + rows.length + ' reach an analyst’s laptop through agnes pull; the rest stay server-side.')
        : '';
    }
    renderPicker();
  }

  /* ── The picker ──
     The project › bucket tree, unchanged, behind a `+`. Browsing a registry is
     a detour from describing a package, and it should end by returning you to
     what you were describing — the same reason /agents' ingredient picker and
     the plugin builder's contents picker are modals. */
  function pickerRowsHtml() {
    var projects = tableGroups();
    if (!projects.length) {
      return '<p class="ag-emptyrows">No table matches that.</p>';
    }
    var searching = !!(pickerQuery || '').trim();
    var html = projects.map(function (p) {
      var pTables = [];
      p.order.forEach(function (bk) { pTables = pTables.concat(p.buckets[bk].tables); });
      var pState = tallyState(pTables);
      // Open when searching (the match is the point), or when the group
      // already contributes to the package — a member you cannot see is a
      // member you cannot remove. Also when it is the ONLY project: now that
      // the tree lives behind a `+`, an instance with one source would open
      // the picker onto a single collapsed heading and nothing to add.
      var pOpen = searching || pState !== 'none' || projects.length === 1;
      var buckets = p.order.map(function (bk) {
        var b = p.buckets[bk];
        var bState = tallyState(b.tables);
        // Same reasoning one level down: one project with one bucket is a flat
        // list, and two clicks to reach it is two clicks of nothing.
        var bOpen = searching || bState !== 'none' || (projects.length === 1 && p.order.length === 1);
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
    return html;
  }

  function pickerHtml() {
    if (!pickerOpen) return '';
    var shown = 0, total = 0;
    (st ? st.registry : []).forEach(function () { total++; });
    tableGroups().forEach(function (p) {
      p.order.forEach(function (bk) { shown += p.buckets[bk].tables.length; });
    });
    return BuilderShell.picker({
      key: 'pdw-tables',
      title: 'Add tables to this package',
      sub: 'Everything registered on this instance. A bucket or a project ticks everything under it.',
      searchPlaceholder: 'Search tables…',
      query: pickerQuery,
      shown: shown,
      total: total,
      rows: pickerRowsHtml(),
      foot: 'Not here? Register it in <a href="/admin/tables">Tables</a> first.',
    });
  }

  function renderPicker() {
    if (!els || !els.picker) return;
    els.picker.innerHTML = pickerHtml();
    // `indeterminate` is a PROPERTY with no HTML attribute, so it cannot ride
    // the markup above and has to be set after the paint.
    els.picker.querySelectorAll('[data-indeterminate]').forEach(function (b) {
      b.indeterminate = true;
    });
  }

  /* Rows + count only, so the search box keeps its focus and caret. */
  function renderPickerRows() {
    if (!els || !els.picker) return;
    var host = els.picker.querySelector('[data-rows="pdw-tables"]');
    if (!host) { renderPicker(); return; }
    host.innerHTML = pickerRowsHtml();
    host.querySelectorAll('[data-indeterminate]').forEach(function (b) { b.indeterminate = true; });
    var shown = 0;
    tableGroups().forEach(function (p) {
      p.order.forEach(function (bk) { shown += p.buckets[bk].tables.length; });
    });
    var cnt = els.picker.querySelector('[data-count="pdw-tables"]');
    if (cnt) cnt.textContent = shown + ' of ' + (st ? st.registry.length : 0);
  }

  function openPicker() {
    pickerOpen = true;
    pickerQuery = '';
    renderPicker();
    var box = els.picker.querySelector('[data-ag-search="pdw-tables"]');
    if (box) box.focus();
  }

  function closePicker() {
    if (!pickerOpen) return;
    pickerOpen = false;
    // renderTables repaints the panel with whatever was picked and clears the
    // modal host on its way through renderPicker.
    renderTables();
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
          server_only: !!t.server_only,
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
                             query_mode: '', server_only: false, project: 'Other' });
        }
      });
      renderTables();
    }).catch(function (e) {
      els.tables.innerHTML = '<p class="ds-drawer__hint">Could not load tables: ' + esc(e.message) + '</p>';
    });
  }

  /* ── Builder mode ──
     Opened from the Library's "+ New" this is a workspace: a conversation on
     the left proposing what the package should be, the form on the right. The
     SAME drawer, grown — opened from the chip input on /admin/tables (where
     you are mid-sentence assigning a table) it stays the compact in-place
     panel it has always been. One implementation, two sizes; a second
     authoring surface for one thing is how two of them drift apart.

     The form is MOVED into the shell's configuration slot rather than
     re-authored, so every cached node in `els` and every handler bound to it
     keeps working untouched. */
  var conv = [], convBusy = false, convErr = null, convDraft = '', convChips = [], convEngine = null;

  function enterBuilderLayout() {
    if (!els || els.root.classList.contains('is-builder-built')) return;
    els.root.classList.add('is-builder-built');
    var panel = els.root.querySelector('.ds-drawer__panel');
    var body = els.root.querySelector('.ds-drawer__body');
    var panes = Array.prototype.slice.call(body.children);

    /* The same header every other builder has: leave on the left, the verb
       that commits on the right. The drawer's own title bar and its footer
       button row are the compact drawer's pattern, not this one — a workspace
       whose primary action is parked in a footer below a scrolling form reads
       as a dialog, and you lose it the moment the form is long enough to
       scroll. The Create button is MOVED, not rebuilt, so `#pdw-submit` and
       everything bound to it keeps working. */
    var head = document.createElement('div');
    head.innerHTML = BuilderShell.head({
      backLabel: (st && st.backLabel) || 'Library',
      title: 'New data package',
      titleId: 'pdw-shell-title',
      actionsId: 'pdw-shell-actions',
    });
    panel.insertBefore(head.firstChild, body);

    var work = document.createElement('div');
    work.innerHTML = BuilderShell.workspace({
      left: '<div class="pdw-conv" id="pdw-conv"></div>',
      cfgTitle: 'Package',
      cfgSub: 'what it carries and who gets it, editable by hand',
      cfgBodyId: 'pdw-cfg',
    });
    body.appendChild(work.firstChild);
    var slot = body.querySelector('#pdw-cfg');
    panes.forEach(function (node) { slot.appendChild(node); });
    els.convHost = body.querySelector('#pdw-conv');

    els.shellTitle = els.root.querySelector('#pdw-shell-title');
  }

  /* Put the commit button where the CURRENT size wants it. It is one node in
     one DOM, so this has to happen on every open, not once at build: after a
     builder open the button was living in the shell header, and the next
     compact open showed a footer with nothing in it but Cancel. */
  function placeSubmit() {
    if (!els || !els.submit) return;
    var actions = els.root.querySelector('#pdw-shell-actions');
    if (st && st.builder && actions) actions.appendChild(els.submit);
    else if (els.foot) els.foot.appendChild(els.submit);
  }

  function renderConv() {
    if (!els || !els.convHost) return;
    els.convHost.innerHTML =
      // The engine, named. Every turn reports it and this builder used to
      // discard it — see BuilderShell.engineNotice for why that is worse than
      // not having the badge at all.
      BuilderShell.engineNotice(convEngine) +
      BuilderShell.conversation({
        id: 'pdw-conv-scroll',
        rows: [{ role: 'assistant', text: OPENING }].concat(conv),
        busy: convBusy,
        err: convErr,
      }) +
      BuilderShell.composer({
        kind: 'create', value: convDraft, busy: convBusy,
        placeholder: 'Describe the package you need…',
        chips: convBusy ? [] : (convChips.length ? convChips : STARTERS),
      });
    var el = els.convHost.querySelector('#pdw-conv-scroll');
    if (el) el.scrollTop = el.scrollHeight;
  }

  var OPENING = 'Tell me what this package should carry and who it is for. ' +
    'I will propose the tables and the groups — you review the access before anything is written.';
  var STARTERS = ['Our sales pipeline tables', 'Everything finance needs for invoicing', 'Which tables are not in a package yet?'];

  /* One turn. Proposes into the drawer; writes nothing. The reply is inserted
     as TEXT (BuilderShell.message) — model output, no sanitizer here. */
  function sendTurn(text) {
    if (convBusy) return;
    conv = conv.concat([{ role: 'user', text: text }]);
    convBusy = true; convErr = null; convDraft = ''; convChips = [];
    renderConv();
    api(PKG_API + '/builder/turn', {
      method: 'POST',
      body: JSON.stringify({
        message: text,
        history: conv.slice(0, -1),
        draft: {
          name: els.name.value || '',
          description: els.desc.value || '',
          tables: Array.from(st.tablesSelected),
          // What is TICKED, not only what was already saved. `grantsOriginal`
          // is the edit-mode baseline and is empty in create mode, so sending
          // it meant the conversation never saw the groups on screen — and
          // kept re-proposing ones the admin had already accepted.
          groups: chosenGrants().map(function (g) { return g.group_id; }),
        },
      }),
    }).then(function (body) {
      conv = conv.concat([{ role: 'assistant', text: body.reply || '' }]);
      convEngine = body.engine || null;
      convChips = (body.suggestions && body.suggestions.length) ? body.suggestions : [];
      applyPatch(body.patch || {});
    }).catch(function (err) {
      console.error('package drawer: turn failed', err);
      convErr = (err && err.message) || 'The assistant could not answer.';
    }).finally(function () {
      convBusy = false;
      renderConv();
    });
  }

  /* Merge a proposal into the form. Nothing is saved — Create still writes,
     and the admin sees the tables and the access matrix first. */
  function applyPatch(patch) {
    if (typeof patch.name === 'string' && patch.name) {
      els.name.value = patch.name;
      if (!st.slugTouched) els.slug.value = slugify(patch.name);
    }
    if (typeof patch.description === 'string') els.desc.value = patch.description;
    if (Array.isArray(patch.tables)) {
      patch.tables.forEach(function (id) { st.tablesSelected.add(id); });
      renderTables();
    }
    if (Array.isArray(patch.groups) && patch.groups.length) {
      /* Two things were wrong here and each alone was enough to drop the
         builder's proposal on the floor:

         the selector named `[data-pdw-group]`, an attribute nothing emits —
         a group row is `[data-group-id]` wrapping an unlabelled checkbox;

         and in CREATE mode the rows do not exist yet at all. Groups are
         hydrated lazily, only when the admin opens the access disclosure
         (see hydrateGroups' note on why), so a patch arriving before that
         had nothing to tick even with the right selector.

         So: fetch the rows if they are not there, then tick, then open the
         disclosure — a box ticked inside a collapsed <details> is a silent
         change to who can reach the data, which is the one thing on this
         panel that must never happen quietly. */
      // Compared against the attribute rather than interpolated into a
      // selector: a group id is server data and may hold a quote, which
      // would break the selector (or worse) — and there is no CSS.escape
      // to lean on in the browsers this ships to.
      var wanted = {};
      patch.groups.forEach(function (id) { wanted[String(id)] = true; });
      Promise.resolve(hydrateGroups()).then(function () {
        var ticked = 0;
        els.groups.querySelectorAll('[data-group-id]').forEach(function (row) {
          if (!wanted[row.getAttribute('data-group-id')]) return;
          var box = row.querySelector('input[type="checkbox"]');
          if (box && !box.checked) { box.checked = true; ticked += 1; }
        });
        if (ticked && els.access && !els.access.open) els.access.open = true;
      });
    }
  }

  function open(opts) {
    opts = opts || {};
    build();
    var mode = opts.mode === 'edit' ? 'edit' : 'create';
    st = {
      mode: mode,
      builder: !!opts.builder,
      //: A page container to render into instead of the overlay.
      mount: opts.mount || null,
      // The label and the destination are one promise; taking them together
      // stops the header saying "Library" while the button goes elsewhere.
      backHref: opts.backHref || '/library',
      backLabel: opts.backLabel || 'Library',
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
    // Grow into a workspace, or stay the compact in-place drawer.
    els.root.classList.toggle('ds-drawer--builder', st.builder);
    // Reset either way: a transcript from a previous open must not be sitting
    // there when the drawer is next used, in either size.
    conv = []; convBusy = false; convErr = null; convDraft = ''; convChips = []; convEngine = null;
    if (st.builder) {
      enterBuilderLayout();
      renderConv();
    } else if (els.convHost) {
      els.convHost.innerHTML = '';
    }
    placeSubmit();
    var typed = opts.typed || '';
    els.name.value = typed;
    els.slug.value = slugify(typed);
    els.desc.value = '';
    els.status.value = 'prod';
    els.category.value = '';
    els.access.open = false;
    els.groups.innerHTML = '';
    els.err.hidden = true;
    els.submit.disabled = false;
    applyMode(mode);

    /* PAGE MODE. Given a `mount`, this stops being an overlay: the panel is
       moved into the page's own container, the backdrop and the modal role go
       away, and the body keeps its scrolling. Everything else — the fields,
       the pickers, the requests — is identical, which is the point of doing
       it this way rather than writing a second package form. */
    if (st.mount) {
      els.root.classList.add('is-page');
      els.root.hidden = false;
      var panel = els.root.querySelector('.ds-drawer__panel');
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
      /* Move the ROOT, not just the panel. Every rule that dresses this thing
         is scoped from the root (`.ds-drawer--builder .ds-drawer__head`, and
         so on); relocating the panel alone leaves those selectors matching
         nothing, and the drawer arrives on the page wearing its overlay
         chrome and none of its builder chrome. */
      if (els.root.parentNode !== st.mount) st.mount.appendChild(els.root);
      document.body.classList.add('ag-building');
    } else {
      els.root.hidden = false;
      els.root.classList.add('is-open');
      document.body.style.overflow = 'hidden';
    }
    els.body.scrollTop = 0;
    // The picker's own state belongs to the drawer session, not the page: a
    // second open must not inherit the last search or a left-open modal.
    pickerQuery = '';
    pickerOpen = false;
    if (els.picker) els.picker.innerHTML = '';
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
      els.submit.disabled = false;
    }).catch(function (e) {
      fail('Could not load the package: ' + e.message);
    });
  }

  /* The conversation's controls. Scoped to this drawer's root so they cannot
     collide with a builder page underneath. */
  function bindConversation(root) {
    root.addEventListener('click', function (e) {
      /* The shell's back button is this drawer's close. Nothing is lost by
         leaving — the package does not exist until Create, and there is no
         draft store here to preserve — so it does not confirm. */
      if (e.target.closest('[data-ag-back]')) {
        // On a page there is nothing to close — leaving means navigating.
        if (st && st.backHref) window.location.href = st.backHref;
        else close();
        return;
      }
      var t = e.target.closest('[data-ag-send],[data-ag-chip]');
      if (!t) return;
      if (t.hasAttribute('data-ag-chip')) { sendTurn(t.getAttribute('data-ag-chip')); return; }
      var box = root.querySelector('[data-ag-comp]');
      var text = box ? box.value.trim() : '';
      if (text) sendTurn(text);
    });
    root.addEventListener('input', function (e) {
      if (e.target.getAttribute && e.target.getAttribute('data-ag-comp')) convDraft = e.target.value;
    });
    root.addEventListener('keydown', function (e) {
      if (!e.target.getAttribute || !e.target.getAttribute('data-ag-comp')) return;
      if (e.key !== 'Enter' || e.shiftKey) return;
      e.preventDefault();
      var text = e.target.value.trim();
      if (text) sendTurn(text);
    });
  }

  function close() {
    if (!els) return;
    if (st && st.mount) return;   // a page is left by navigating, not closed
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

  /* ── Who gets it ──────────────────────────────────────────────────────
     Groups are lazy — most packages are created and shared later from the
     package's own page or a group's Access tab, and this is a request the
     collapsed state should not have made. */

  /* Returns a promise so a caller that needs the ROWS (not just the paint)
     can wait — `applyPatch` ticks boxes that do not exist until this lands. */
  function hydrateGroups() {
    if (st.groupsLoaded) return Promise.resolve();
    st.groupsLoaded = true;
    els.groups.innerHTML = '<p class="ds-drawer__empty">Loading groups…</p>';
    return api(GROUPS_API).then(function (body) {
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
          + '<span class="pdw-pick__name">'
          + (window.AgnesKindGlyph ? window.AgnesKindGlyph.groupGlyph() + ' ' : '')
          + esc(gname) + '</span>'
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
      // `category` honours an empty-string-clears contract server-side (see
      // update_data_package), so an emptied field must send "" rather than
      // null — null means "leave unchanged", which would make clearing a
      // category impossible from here.
      api(PKG_API + '/' + encodeURIComponent(pkgId), {
        method: 'PUT',
        body: JSON.stringify({
          name: name,
          description: els.desc.value.trim() || null,
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
