/* =====================================================================
 * linked_apps_builder.js — publishing externally-hosted apps, in the shell.
 *
 * What this replaces: a three-step wizard whose step 1 was "choose a Keboola
 * MCP source", with a dead end under it — "Not registered yet? Register an MCP
 * source first, then come back." Step 2 asked the admin to pick the lister
 * tool and then fill in a PROJECTION MAP: which response field is the app's
 * name, which is its URL. That is an integration author's artifact, not an
 * operator's, and exposing it as configuration is the defect the wizard was
 * built around rather than fixing.
 *
 * Two things make it a build instead:
 *
 * 1. The source and its lister tool are DETECTED, not chosen. There is
 *    normally one Keboola MCP source with one data-app lister; asking which is
 *    asking the admin to know the pipeline's internals. When there are several
 *    the section offers the choice; when there are none it says what to do.
 * 2. The projection map is an escape hatch, not a step. src/data_apps/
 *    keboola_adapter.py already falls back to alias guesses (id/app_id/
 *    config_id, url/app_url/deployment_url, …) which fit the upstreams that
 *    suit them. The mapping fields appear ONLY when rows came back that the
 *    aliases could not read — the one case they exist for.
 *
 * Honest about writing, unlike the MCP builder next door: fetching apps runs
 * the materialize path, which ingests the catalogue. That is a real write and
 * the panel says so. What Save adds is who can see them.
 * ===================================================================== */
(function (window) {
  'use strict';

  var esc = window.BuilderShell.esc;

  var SOURCES_API = '/api/admin/mcp-sources';
  var TOOLS_API = '/api/admin/mcp-tools';
  var APPS_API = '/api/data-apps';
  var GROUPS_API = '/api/admin/groups';
  var GRANTS_API = '/api/admin/grants';

  var mount = null;
  var sources = null, sourcesErr = false;
  var tools = null;
  var picked = { sourceId: '', toolId: '' };
  var apps = [], fetched = false, skipped = 0;
  var mapping = { id: '', url: '', name: '', description: '' };
  var mappingOpen = false;
  var chosen = {};            // app id -> true
  var grantGroups = [];       // [{id, name}]
  var groups = null, groupsErr = false;
  var collapsed = {};
  var fetching = false, fetchErr = null, saving = false, saveErr = null;
  var pickerOpen = false, pickerQuery = '';

  /* ── Server calls ──────────────────────────────────────────────────── */

  function readJson(r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) {
        var d = j && j.detail;
        var msg = typeof d === 'string' ? d : (d && (d.hint || d.kind)) || ('HTTP ' + r.status);
        var e = new Error(msg);
        // The status, not just the sentence: `save()` has to tell an
        // already-granted 409 from a real failure, and matching on the
        // message text is how that breaks the day the wording changes.
        e.status = r.status;
        throw e;
      }
      return j;
    });
  }
  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {})).then(readJson);
  }
  function send(url, method, body) {
    return api(url, {
      method: method, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  /* ── Detection ──────────────────────────────────────────────────────
     The wizard made this the admin's first decision. It is the pipeline's
     internals: a Keboola MCP source, and within it the tool whose output the
     linked-app projection reads. Detect it, say what was detected, and only
     ask when the answer is genuinely ambiguous. */

  function looksKeboola(s) {
    return /keboola/i.test(String(s.name || '') + ' ' + String(s.url || ''));
  }

  function listerTools() {
    return (tools || []).filter(function (t) {
      var n = String(t.original_name || t.name || '').toLowerCase();
      return n.indexOf('data') >= 0 && n.indexOf('app') >= 0;
    });
  }

  function loadSources() {
    return api(SOURCES_API).then(function (body) {
      var rows = Array.isArray(body) ? body : (body && body.sources) || [];
      sources = rows.map(function (s) {
        return { id: String(s.id), name: String(s.name || s.id), url: String(s.url || '') };
      });
      var keboola = sources.filter(looksKeboola);
      var only = keboola.length === 1 ? keboola[0] : (sources.length === 1 ? sources[0] : null);
      if (only) picked.sourceId = only.id;
      sourcesErr = false;
    }).catch(function () { sources = null; sourcesErr = true; });
  }

  function loadTools() {
    if (!picked.sourceId) { tools = []; return Promise.resolve(); }
    return api(TOOLS_API + '?source_id=' + encodeURIComponent(picked.sourceId))
      .then(function (body) {
        tools = Array.isArray(body) ? body : (body && body.tools) || [];
        var listers = listerTools();
        if (listers.length === 1) picked.toolId = String(listers[0].tool_id || listers[0].id);
      })
      .catch(function () { tools = []; });
  }

  /* ── Fetch the catalogue ───────────────────────────────────────────── */

  function fetchApps() {
    if (fetching || !picked.toolId) return;
    fetching = true; fetchErr = null;
    render();
    // Idempotent: the tool has to be in materialize mode for the lister path
    // to write a table for the projection to read.
    send(TOOLS_API + '/' + encodeURIComponent(picked.toolId), 'PUT',
         { mode: 'materialize', schedule: 'daily 03:00' })
      .catch(function () { /* already in the right mode — not fatal */ })
      .then(function () {
        return send(SOURCES_API + '/' + encodeURIComponent(picked.sourceId) + '/materialize', 'POST',
                    { tool_id: picked.toolId, lister: true });
      })
      .then(function (res) {
        var proj = res && res.linked_projection;
        skipped = (proj && proj.skipped) || 0;
        // Rows the aliases could not read are the ONLY reason to show the
        // column mapping, so its visibility is derived rather than chosen.
        if (skipped) mappingOpen = true;
        return api(APPS_API + '?kind=linked&source=' + encodeURIComponent(picked.sourceId));
      })
      .then(function (body) {
        var rows = Array.isArray(body) ? body : (body && body.apps) || (body && body.items) || [];
        apps = rows.map(function (a) {
          return {
            id: String(a.id), name: String(a.name || a.slug || a.id),
            url: String(a.external_url || a.url || ''),
            description: String(a.description || ''),
          };
        });
        apps.forEach(function (a) { if (chosen[a.id] === undefined) chosen[a.id] = true; });
        fetched = true;
      })
      .catch(function (e) { fetchErr = e.message || 'Could not fetch the apps.'; })
      .then(function () { fetching = false; render(); });
  }

  function saveMapping() {
    var m = {};
    Object.keys(mapping).forEach(function (k) { if (mapping[k].trim()) m[k] = mapping[k].trim(); });
    return send(TOOLS_API + '/' + encodeURIComponent(picked.toolId) + '/projection-map', 'PUT',
                { projection_map: Object.keys(m).length ? m : null })
      .then(function () { fetchApps(); })
      .catch(function (e) { fetchErr = 'Could not save the column mapping: ' + e.message; render(); });
  }

  /* ── Save: who gets them ───────────────────────────────────────────── */

  function chosenApps() { return apps.filter(function (a) { return chosen[a.id]; }); }

  function canSave() { return fetched && chosenApps().length > 0 && grantGroups.length > 0; }

  /* Save is one grant per (app × group), and the two things that matter are
     both about partial success.

     A grant that already exists answers 409. That is the END STATE this save
     is asking for, so it counts as done — the pre-builder wizard already read
     it that way (it matched the message text; this reads `e.status`). Counting
     it as a failure is not a cosmetic wrong answer: `Promise.all` rejects on
     the first failure, so one already-granted pair failed the whole save, and
     every retry failed identically because the successful pairs from the first
     attempt were now 409s too. Save became permanently unreachable.

     So no call is allowed to reject: each resolves to its own outcome, and
     what the admin gets told is which pairs are still not granted, by name. */

  function grantPair(pair, label) {
    return send(GRANTS_API, 'POST', pair)
      .then(function () { return null; })
      .catch(function (e) {
        if (e && e.status === 409) return null;  // already granted
        return label + ': ' + (e.message || 'unknown error');
      });
  }

  function save() {
    if (saving || !canSave()) return;
    saving = true; saveErr = null;
    render();
    var calls = [];
    chosenApps().forEach(function (a) {
      grantGroups.forEach(function (g) {
        calls.push(grantPair(
          { group_id: g.id, resource_type: 'data_app', resource_id: a.id },
          a.name + ' → ' + g.name
        ));
      });
    });
    Promise.all(calls).then(function (results) {
      var failed = results.filter(function (r) { return r; });
      if (!failed.length) {
        window.location.href = '/library?kind=data_app';
        return;
      }
      saveErr = 'Granted ' + (results.length - failed.length) + ' of ' + results.length +
                '. Still not granted — ' + failed.join('; ') +
                '. Press Save again to retry just those.';
      saving = false;
      render();
    });
  }

  /* ── Groups picker ─────────────────────────────────────────────────── */

  function loadGroups() {
    if (groups !== null) return Promise.resolve();
    return api(GROUPS_API).then(function (body) {
      groups = (Array.isArray(body) ? body : (body && body.groups) || []).map(function (g) {
        return { id: String(g.id || g.name), name: String(g.name || g.id), members: g.member_count };
      });
      groupsErr = false;
    }).catch(function () { groups = null; groupsErr = true; });
  }
  function groupPicked(id) { return grantGroups.some(function (g) { return g.id === id; }); }
  function toggleGroup(id) {
    if (groupPicked(id)) {
      grantGroups = grantGroups.filter(function (g) { return g.id !== id; });
    } else {
      var f = (groups || []).filter(function (g) { return g.id === id; })[0];
      if (f) grantGroups = grantGroups.concat([{ id: f.id, name: f.name }]);
    }
    renderPickerRows();
  }
  function pickerMatches() {
    var q = (pickerQuery || '').trim().toLowerCase();
    if (!q) return groups || [];
    return (groups || []).filter(function (g) { return g.name.toLowerCase().indexOf(q) >= 0; });
  }
  function pickerRowsHtml() {
    if (groupsErr) return '<p class="ag-emptyrows">Could not load the groups — try reloading.</p>';
    if (groups === null) return '<p class="ag-emptyrows">Loading…</p>';
    var rows = pickerMatches();
    if (!rows.length) {
      return '<p class="ag-emptyrows">' + (groups.length
        ? 'Nothing matches that.'
        : 'No groups yet — make one in <a href="/admin/access">Access</a>.') + '</p>';
    }
    return rows.map(function (g) {
      var on = groupPicked(g.id);
      return '<div class="ag-row"><div class="ag-row-body">' +
        '<div class="ag-row-name">' + esc(g.name) + '</div>' +
        (typeof g.members === 'number'
          ? '<div class="ag-row-desc">' + g.members + (g.members === 1 ? ' member' : ' members') + '</div>' : '') +
        '</div><button type="button" class="ag-tglbtn' + (on ? ' on' : '') +
        '" data-la-pick="' + esc(g.id) + '">' + (on ? '✓ Added' : '+ Add') + '</button></div>';
    }).join('');
  }
  function pickerHtml() {
    if (!pickerOpen) return '';
    return window.BuilderShell.picker({
      key: 'la-groups',
      title: 'Who sees these apps',
      sub: 'A linked app opens on the system that hosts it. Granting a group puts it in their Library.',
      searchPlaceholder: 'Search groups…',
      query: pickerQuery,
      shown: groups === null ? 0 : pickerMatches().length,
      total: groups === null ? 0 : groups.length,
      rows: pickerRowsHtml(),
      foot: 'Groups are managed in <a href="/admin/access">Access</a>.',
    });
  }
  function renderPicker() {
    var host = document.getElementById('la-picker');
    if (host) host.innerHTML = pickerHtml();
  }
  function renderPickerRows() {
    var el = document.querySelector('[data-rows="la-groups"]');
    if (!el) { renderPicker(); return; }
    el.innerHTML = pickerRowsHtml();
    var cnt = document.querySelector('[data-count="la-groups"]');
    if (cnt && groups !== null) cnt.textContent = pickerMatches().length + ' of ' + groups.length;
  }
  function openPicker() {
    pickerOpen = true; pickerQuery = '';
    renderPicker();
    loadGroups().then(function () { if (pickerOpen) renderPickerRows(); });
    var box = document.querySelector('[data-ag-search="la-groups"]');
    if (box) box.focus();
  }
  function closePicker() { if (pickerOpen) { pickerOpen = false; render(); } }

  /* ── Panel ─────────────────────────────────────────────────────────── */

  function sourceBody() {
    if (sourcesErr) return '<div class="ag-note ag-note--err">Could not load the MCP sources.</div>';
    if (sources === null) return '<p class="ag-emptyrows">Loading…</p>';
    if (!sources.length) {
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">No MCP source to read apps from.</p>' +
          '<p class="ag-slot-body">Externally-hosted apps are read through a tool server that lists them. ' +
          'Connect one first, then come back.</p>' +
        '</div>' +
        '<a class="ag-addrow" href="/admin/mcp-sources/new">+ Connect an MCP source</a>';
    }
    var listers = listerTools();
    var rows = sources.map(function (s) {
      var on = s.id === picked.sourceId;
      return '<div class="ag-row"><div class="ag-row-body">' +
        '<div class="ag-row-name">' + esc(s.name) +
          (looksKeboola(s) ? '<span class="ag-kind ag-kind--plugin">apps</span>' : '') + '</div>' +
        (s.url ? '<div class="ag-row-desc">' + esc(s.url) + '</div>' : '') +
        '</div><button type="button" class="ag-tglbtn' + (on ? ' on' : '') +
        '" data-la-source="' + esc(s.id) + '">' + (on ? '✓ Using' : 'Use') + '</button></div>';
    }).join('');
    var toolNote = '';
    if (picked.sourceId) {
      if (tools === null) toolNote = '<div class="ag-note">Reading its tools…</div>';
      else if (!listers.length) {
        toolNote = '<div class="ag-note ag-note--err">This source exposes no tool that lists apps. ' +
          'Introspect it on its own page, or pick another source.</div>';
      } else if (listers.length === 1) {
        toolNote = '<div class="ag-note">Reading apps from <code>' +
          esc(String(listers[0].original_name || listers[0].name)) + '</code>, the one tool here that lists them.</div>';
      } else {
        toolNote = '<div class="ag-note">Several tools could be the lister. Using <code>' +
          esc(String((listers[0] || {}).original_name || '')) + '</code>.</div>';
      }
    }
    return '<div class="ag-rows">' + rows + '</div>' + toolNote;
  }

  function mappingBody() {
    // Only ever rendered when the aliases failed — see the module header.
    return '<div class="ag-note ag-note--warn">' + skipped + ' row(s) came back with no id or URL that ' +
      'Agnes could read. Name the columns that carry them and fetch again.</div>' +
      ['id', 'url', 'name', 'description'].map(function (k) {
        return '<label class="ag-field"><span>' + esc(k) + ' column</span>' +
          '<input type="text" data-la-map="' + esc(k) + '" value="' + esc(mapping[k]) + '" ' +
          'placeholder="column name" autocomplete="off"></label>';
      }).join('') +
      '<button type="button" class="ag-addrow" data-la-savemap>Save mapping and fetch again</button>';
  }

  function appsBody() {
    var fetchBtn = '<button type="button" class="ag-addrow" data-la-fetch' +
      (fetching || !picked.toolId ? ' disabled' : '') + '>' +
      (fetching ? 'Fetching…' : (fetched ? 'Fetch again' : '+ Fetch apps')) + '</button>';
    if (fetchErr) fetchBtn += '<div class="ag-note ag-note--err">' + esc(fetchErr) + '</div>';
    if (!fetched) {
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">Nothing fetched yet.</p>' +
          '<p class="ag-slot-body">Agnes asks the server what apps exist and catalogues them. ' +
          'This one DOES write — the catalogue is ingested. Nobody can see any of it until you ' +
          'grant a group below.</p>' +
        '</div>' + fetchBtn;
    }
    if (!apps.length) {
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">The server listed no apps.</p>' +
          '<p class="ag-slot-body">Either there are none, or the columns it returned could not be read.</p>' +
        '</div>' + (skipped ? mappingBody() : '') + fetchBtn;
    }
    var rows = apps.map(function (a) {
      var on = !!chosen[a.id];
      return '<div class="ag-row"><div class="ag-row-body">' +
        '<div class="ag-row-name">' + esc(a.name) + '</div>' +
        '<div class="ag-row-desc">' + esc(a.url || a.description || '') + '</div>' +
        '</div><button type="button" class="ag-tglbtn' + (on ? ' on' : '') +
        '" data-la-app="' + esc(a.id) + '">' + (on ? '✓ On' : 'Off') + '</button></div>';
    }).join('');
    return '<div class="ag-rows">' + rows + '</div>' +
      (mappingOpen ? mappingBody() : '') + fetchBtn;
  }

  function accessBody() {
    var rows = grantGroups.length
      ? '<div class="ag-rows">' + grantGroups.map(function (g) {
          return '<div class="ag-row"><div class="ag-row-body">' +
            '<div class="ag-row-name">' + esc(g.name) + '</div></div>' +
            '<button type="button" class="ag-tglbtn ag-tglbtn--rm" data-la-unpick="' + esc(g.id) + '">Remove</button></div>';
        }).join('') + '</div>'
      : '<div class="ag-slot">' +
          '<p class="ag-slot-head">Nobody yet.</p>' +
          '<p class="ag-slot-body">A fetched app is catalogued and invisible until a group is granted it.</p>' +
        '</div>';
    return rows + '<button type="button" class="ag-addrow" data-la-openpick>+ Add groups</button>';
  }

  function panelHtml() {
    var sec = window.BuilderShell.section;
    var src = (sources || []).filter(function (s) { return s.id === picked.sourceId; })[0];
    return (saveErr ? '<div class="ag-note ag-note--err">' + esc(saveErr) + '</div>' : '') +
      sec({
        key: 'source', no: 1, title: 'Where they come from', note: 'the server that lists them',
        collapsed: !!collapsed.source,
        sub: 'Externally-hosted apps are read through a tool server. Normally there is one, and Agnes picks it.',
        summary: src ? src.name : 'none',
        body: sourceBody(),
      }) +
      sec({
        key: 'apps', no: 2, title: 'Apps', note: 'what to publish',
        collapsed: !!collapsed.apps,
        sub: 'Everything the server lists. Turn off anything that should not appear in the Library.',
        summary: fetched ? (chosenApps().length + ' of ' + apps.length) : 'not fetched',
        body: appsBody(),
      }) +
      sec({
        key: 'access', no: 3, title: 'Access', note: 'who sees them',
        collapsed: !!collapsed.access,
        sub: 'A linked app opens on the system that hosts it — Agnes catalogues and links it, nothing more.',
        summary: grantGroups.length ? grantGroups.length + ' group' + (grantGroups.length === 1 ? '' : 's') : 'nobody',
        body: accessBody(),
      });
  }

  /* The left pane, where the other builders put a conversation.

     This one deliberately does not have one. Every decision here is a pick
     from a list the SERVER supplies — which source, which apps, which groups —
     so there is no intent for a model to turn into configuration, and a
     conversation would be a costume over three clicks. (Same judgement the
     conversational-entity-builder spec reaches about "Upload a file".)

     What it carries instead is the procedure with live state on it, so the
     pane still answers "where am I and what is next" — which is the job the
     conversation does next door. */
  function stepsPaneHtml() {
    var src = (sources || []).filter(function (s) { return s.id === picked.sourceId; })[0];
    var steps = [
      { done: !!src, head: 'Where they come from',
        body: src ? 'Reading from ' + src.name + '.'
                  : (sources && !sources.length ? 'No tool server to read apps from yet.'
                                                : 'Picking the server that lists your apps.') },
      { done: fetched, head: 'Fetch the catalogue',
        body: fetched ? apps.length + ' app' + (apps.length === 1 ? '' : 's') + ' found; ' +
                        chosenApps().length + ' selected to publish.'
                      : 'Agnes asks the server what exists. This step writes — it ingests the catalogue.' },
      { done: grantGroups.length > 0, head: 'Decide who sees them',
        body: grantGroups.length
          ? 'Visible to ' + grantGroups.map(function (g) { return g.name; }).join(', ') + ' once published.'
          : 'A fetched app is catalogued and invisible until a group is granted it.' },
    ];
    return '<div class="ag-conv-in" style="padding:24px 20px">' +
      '<div class="ag-slot" style="padding-top:0">' +
        '<p class="ag-slot-head">Apps you already run, in one place.</p>' +
        '<p class="ag-slot-body">Agnes does not host these — it catalogues them and links to them, so ' +
        'people find them beside everything else instead of in a bookmark.</p>' +
      '</div>' +
      '<div class="ag-rows">' + steps.map(function (st, i) {
        return '<div class="ag-row">' +
          '<span class="ag-sec-no">' + (st.done ? '✓' : String(i + 1)) + '</span>' +
          '<div class="ag-row-body">' +
            '<div class="ag-row-name">' + esc(st.head) + '</div>' +
            '<div class="ag-row-desc">' + esc(st.body) + '</div>' +
          '</div>' +
        '</div>';
      }).join('') + '</div>' +
    '</div>';
  }

  function leftHtml() { return stepsPaneHtml(); }

  function render() {
    if (!mount) return;
    mount.innerHTML =
      window.BuilderShell.head({
        backLabel: 'Library', title: 'Link external apps', titleId: 'la-title',
        badge: '<span class="sk-typechip sk-typechip--agent">Linked apps</span>',
        actionsId: 'la-actions',
        actions: '<button type="button" class="cc-btn cc-btn--primary" id="la-save"' +
          (canSave() && !saving ? '' : ' disabled') + '>' +
          (saving ? 'Publishing…' : 'Publish to Library') + '</button>',
      }) +
      window.BuilderShell.workspace({
        left: leftHtml(),
        cfgTitle: 'Configuration',
        cfgSub: 'everything this link is, editable by hand',
        cfgBodyId: 'la-steps',
        cfg: panelHtml(),
      }) +
      '<div id="la-picker"></div>';
    renderPicker();
    document.body.classList.add('ag-building');
  }

  /* ── Events ────────────────────────────────────────────────────────── */

  function wire() {
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-la-source],[data-la-fetch],[data-la-app],[data-la-openpick],' +
        '[data-la-pick],[data-la-unpick],[data-la-savemap],[data-ag-pick-close],' +
        '[data-ag-toggle-sec],[data-ag-back],#la-save');
      if (!t) {
        if (pickerOpen && e.target.hasAttribute && e.target.hasAttribute('data-ag-pick-backdrop')) closePicker();
        return;
      }
      if (t.hasAttribute('data-la-source')) {
        picked.sourceId = t.getAttribute('data-la-source');
        picked.toolId = '';
        tools = null;
        // The catalogue belongs to the source it came from.
        fetched = false; apps = []; chosen = {}; mappingOpen = false; skipped = 0;
        render();
        loadTools().then(render);
      }
      else if (t.hasAttribute('data-la-fetch')) { fetchApps(); }
      else if (t.hasAttribute('data-la-savemap')) { saveMapping(); }
      else if (t.hasAttribute('data-la-app')) {
        var id = t.getAttribute('data-la-app');
        chosen[id] = !chosen[id];
        render();
      }
      else if (t.hasAttribute('data-la-openpick')) { openPicker(); }
      else if (t.hasAttribute('data-la-pick')) { toggleGroup(t.getAttribute('data-la-pick')); }
      else if (t.hasAttribute('data-la-unpick')) {
        grantGroups = grantGroups.filter(function (g) { return g.id !== t.getAttribute('data-la-unpick'); });
        render();
      }
      else if (t.hasAttribute('data-ag-pick-close')) { closePicker(); }
      else if (t.hasAttribute('data-ag-toggle-sec')) {
        var sk = t.getAttribute('data-ag-toggle-sec');
        collapsed[sk] = !collapsed[sk];
        var el = mount.querySelector('[data-sec="' + sk + '"]');
        if (el) { el.classList.toggle('collapsed', collapsed[sk]); t.setAttribute('aria-expanded', String(!collapsed[sk])); }
        else render();
      }
      else if (t.hasAttribute('data-ag-back')) { window.location.href = '/library'; }
      else if (t.id === 'la-save') { save(); }
    });

    document.addEventListener('input', function (e) {
      var el = e.target;
      if (el.getAttribute && el.getAttribute('data-ag-search') === 'la-groups') {
        pickerQuery = el.value; renderPickerRows(); return;
      }
      var m = el.getAttribute && el.getAttribute('data-la-map');
      if (m) mapping[m] = el.value;
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && pickerOpen) closePicker();
    });
  }

  window.AgnesLinkedAppsBuilder = {
    open: function (opts) {
      mount = opts.mount;
      wire();
      render();
      loadSources().then(function () {
        render();
        return loadTools();
      }).then(render);
    },
  };
})(window);
