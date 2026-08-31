/*
 * linked_apps_page.js — publishing apps from a server that is already connected.
 *
 * The other half of the MCP builder's apps section. Same panel, different
 * errand: there, you are connecting a server and its apps are the next move;
 * here, the server was connected weeks ago and today's job is "put those apps
 * in front of the finance team". Making that go through the connection form
 * would be asking someone to re-answer questions they answered once.
 *
 * What the retired standalone builder got wrong, and this does not:
 *
 *   • It DETECTED the source instead of asking — "if there is exactly one,
 *     use that" — and printed "✓ Using" for a choice nobody made. Here the
 *     list is on screen and the pick is a click.
 *   • Its empty state was a dead end: "no sources yet? register one first,
 *     then come back". Connecting one is an action ON this page; it hands off
 *     to the MCP builder, which carries the same panel, so the errand
 *     finishes there rather than sending you back.
 *   • It was reachable on an instance with data apps switched off and failed
 *     at the last step. The route is gated on the same predicate that hides
 *     the apps hub.
 */
(function (window) {
  'use strict';

  var SOURCES_API = '/api/admin/mcp-sources';
  var GROUPS_API = '/api/admin/groups';

  var mount = null;
  var sources = [];          // [{id, name, url, listerToolId}] — only ones that list apps
  var sourcesErr = null;
  var loading = true;
  var picked = null;         // the chosen source row
  var groups = null;         // [{id, name}] or null before the picker loads them
  var groupsErr = false;
  var chosenGroups = [];     // [{id, name}] the apps are granted to
  var pickerOpen = false;
  var pickerQuery = '';
  var publishing = false;
  var publishErr = null;
  var panel = null;

  function esc(s) { return window.BuilderShell.esc(s); }

  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {})).then(function (r) {
      if (r.status === 204) return null;
      return r.json().catch(function () { return null; }).then(function (body) {
        if (!r.ok) {
          var detail = body && (body.detail || body.error);
          var msg = (detail && (detail.message || detail.code)) || detail || ('HTTP ' + r.status);
          var e = new Error(String(msg));
          e.status = r.status;
          throw e;
        }
        return body;
      });
    });
  }

  /* Only sources that actually expose a lister tool. A server with no such
     tool cannot answer "what apps do you have", and offering it here would be
     a row whose only outcome is a failure two clicks later. */
  function loadSources() {
    return api(SOURCES_API)
      .then(function (body) {
        var rows = Array.isArray(body) ? body : (body && body.sources) || [];
        return Promise.all(rows.map(function (s) {
          return api(SOURCES_API + '/' + encodeURIComponent(s.id))
            .then(function (full) {
              var lister = (full.tools || []).filter(function (t) {
                return window.LinkedAppsPanel.isLister(t.original_name || t.exposed_name);
              })[0];
              if (!lister) return null;
              return {
                id: String(full.id), name: String(full.name || full.id),
                url: String(full.url || full.command || ''),
                listerToolId: String(lister.tool_id),
              };
            })
            .catch(function () { return null; });
        }));
      })
      .then(function (rows) { sources = rows.filter(Boolean); })
      .catch(function (e) { sourcesErr = (e && e.message) || 'Could not read the connected servers.'; })
      .then(function () { loading = false; render(); });
  }

  function loadGroups() {
    if (groups !== null) return Promise.resolve();
    return api(GROUPS_API).then(function (body) {
      groups = (Array.isArray(body) ? body : (body && body.groups) || []).map(function (g) {
        return { id: String(g.id || g.name), name: String(g.name || g.id), members: g.member_count };
      });
      groupsErr = false;
    }).catch(function () { groups = null; groupsErr = true; });
  }

  function thePanel() {
    if (!panel) {
      panel = window.LinkedAppsPanel.create({
        sourceId: null,
        toolId: null,
        groups: function () { return chosenGroups; },
        onChange: function () { render(); },
        idlePrompt: {
          head: 'Pick a server first.',
          body: 'Its apps are read from the server itself, so there is nothing to show until you choose one.',
        },
      });
    }
    return panel;
  }

  function pickSource(id) {
    var row = sources.filter(function (s) { return s.id === id; })[0];
    if (!row) return;
    picked = row;
    thePanel().setSource(row.id, row.listerToolId);
    render();
  }

  /* ── Sections ───────────────────────────────────────────────────────── */

  function sourcesBody() {
    if (loading) return '<p class="ag-note">Reading the connected servers…</p>';
    if (sourcesErr) return '<div class="ag-note ag-note--err">' + esc(sourcesErr) + '</div>';
    var connect = '<a class="ag-addrow" href="/admin/mcp-sources/new">+ Connect another server</a>';
    if (!sources.length) {
      /* Not a dead end: the thing you need is one click away, and the builder
         it opens carries this same panel, so the errand finishes there. */
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">No connected server lists apps.</p>' +
          '<p class="ag-slot-body">Apps are catalogued from a tool server that can list them. Connect one ' +
          'and its apps can be published from the same page that registers it, or come back here ' +
          'afterwards.</p>' +
        '</div>' + connect;
    }
    var rows = sources.map(function (s) {
      var on = picked && picked.id === s.id;
      return '<div class="ag-row">' +
        '<div class="ag-row-body"><div class="ag-row-name">' + esc(s.name) + '</div>' +
          (s.url ? '<div class="ag-row-desc">' + esc(s.url) + '</div>' : '') + '</div>' +
        '<button type="button" class="ag-tglbtn' + (on ? ' on' : '') + '" data-la-src="' + esc(s.id) + '">' +
          (on ? '✓ Chosen' : 'Choose') + '</button>' +
      '</div>';
    }).join('');
    return '<div class="ag-rows">' + rows + '</div>' + connect;
  }

  function accessBody() {
    // The same row + Remove the MCP builder uses for a granted group, rather
    // than a chip shape that means something else two stylesheets over.
    var body = chosenGroups.length
      ? '<div class="ag-rows">' + chosenGroups.map(function (g) {
          return '<div class="ag-row">' +
            '<div class="ag-row-body"><div class="ag-row-name">' + esc(g.name) + '</div></div>' +
            '<button type="button" class="ag-tglbtn ag-tglbtn--rm" data-la-ungroup="' + esc(g.id) + '">Remove</button>' +
          '</div>';
        }).join('') + '</div>'
      : '<div class="ag-slot">' +
          '<p class="ag-slot-head">Nobody yet.</p>' +
          '<p class="ag-slot-body">Until you grant a group, the apps are catalogued and invisible — which ' +
          'is the safe state to stop in if you are not sure.</p>' +
        '</div>';
    return body + '<button type="button" class="ag-addrow" data-la-openpick>+ Add groups</button>';
  }

  function pickerRowsHtml() {
    if (groupsErr) return '<p class="ag-note ag-note--err">Could not load the groups.</p>';
    if (groups === null) return '<p class="ag-note">Loading…</p>';
    var q = pickerQuery.trim().toLowerCase();
    var rows = q ? groups.filter(function (g) { return g.name.toLowerCase().indexOf(q) !== -1; }) : groups;
    if (!rows.length) return '<p class="ag-note">No group matches that.</p>';
    return rows.map(function (g) {
      var on = chosenGroups.some(function (c) { return c.id === g.id; });
      return '<div class="ag-row">' +
        '<div class="ag-row-body"><div class="ag-row-name">' + esc(g.name) + '</div>' +
          (g.members ? '<div class="ag-row-desc">' + g.members + ' member' + (g.members === 1 ? '' : 's') + '</div>' : '') +
        '</div>' +
        '<button type="button" class="ag-tglbtn' + (on ? ' on' : '') + '" data-la-pick="' + esc(g.id) + '">' +
          (on ? '✓ Added' : 'Add') + '</button>' +
      '</div>';
    }).join('');
  }

  function pickerHtml() {
    if (!pickerOpen) return '';
    return window.BuilderShell.picker({
      key: 'la-groups',
      title: 'Who gets these apps',
      sub: 'Everyone in a granted group sees the apps in their Library.',
      searchPlaceholder: 'Search groups…',
      query: pickerQuery,
      shown: groups === null ? 0 : groups.length,
      total: groups === null ? 0 : groups.length,
      rows: pickerRowsHtml(),
    });
  }

  function canPublish() {
    return !!picked && thePanel().chosen().length > 0 && chosenGroups.length > 0 && !publishing;
  }

  function publishBlocker() {
    if (!picked) return 'Choose a server first.';
    if (!thePanel().chosen().length) return 'Read the app list and leave at least one app on.';
    if (!chosenGroups.length) return 'Add a group — the apps reach nobody until you do.';
    return '';
  }

  function publish() {
    if (!canPublish()) return;
    publishing = true; publishErr = null;
    render();
    thePanel().publish().then(function (failures) {
      publishing = false;
      if (failures.length) {
        /* Named here rather than carried through a redirect: a grant that did
           not land is the difference between "shared" and "invisible", and the
           admin is the only one who can retry it. */
        publishErr = 'These grants did not land: ' + failures.join('; ');
        render();
        return;
      }
      // `section=`, not `kind=` — the Library validates the section against
      // its own keys and opens the tab that holds it. `kind=` was read by
      // nothing, so the only confirmation this action had was a page that
      // looked exactly like not having done it.
      window.location.href = '/library?section=data_app';
    });
  }

  /* ── Render ─────────────────────────────────────────────────────────── */

  function render() {
    if (!mount) return;
    var sec = window.BuilderShell.section;
    mount.innerHTML =
      window.BuilderShell.head({
        backLabel: 'Library', title: 'Publish apps', titleId: 'la-title',
        actionsId: 'la-actions',
        actions: '<button type="button" class="cc-btn cc-btn--primary" id="la-publish"' +
          (canPublish() ? '' : ' disabled title="' + esc(publishBlocker() || 'Publishing…') + '"') + '>' +
          (publishing ? 'Publishing…' : 'Publish to the Library') + '</button>',
      }) +
      (publishErr ? '<div class="ag-note ag-note--err">' + esc(publishErr) + '</div>' : '') +
      window.BuilderShell.workspace({
        left: '<div class="ag-cfg-blurb">' +
            '<p>An app hosted somewhere else — a dashboard, a small tool — is catalogued here so it appears ' +
            'in the Library beside everything else, for the groups you choose. Agnes does not host it or ' +
            'proxy it; it records where it lives and who may see it.</p>' +
            '<p>The list comes from a connected tool server. Publishing apps while you connect one is the ' +
            'same three steps, in the builder that registers it.</p>' +
          '</div>',
        cfgTitle: 'Configuration',
        cfgSub: 'which server, which apps, and who gets them',
        cfgBodyId: 'la-steps',
        cfg:
          sec({
            key: 'source', no: 1, title: 'Server', note: 'where the apps are listed',
            sub: 'Connected servers that expose a tool for listing apps. One at a time — the catalogue ' +
                 'belongs to the server it came from.',
            summary: picked ? picked.name : 'none chosen',
            body: sourcesBody(),
          }) +
          sec({
            key: 'apps', no: 2, title: 'Apps', note: 'what it lists',
            sub: 'Read from the server itself. Turn off anything that should not appear in the Library.',
            summary: thePanel().summary(),
            body: thePanel().html(),
          }) +
          sec({
            key: 'access', no: 3, title: 'Access', note: 'who gets them',
            sub: 'Everyone in a granted group sees these apps in their Library.',
            summary: chosenGroups.length
              ? chosenGroups.length + ' group' + (chosenGroups.length === 1 ? '' : 's')
              : 'nobody',
            body: accessBody(),
          }),
      }) +
      '<div id="la-picker">' + pickerHtml() + '</div>';
    document.body.classList.add('ag-building');
  }

  function wire() {
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-la-src],[data-la-openpick],[data-la-pick],[data-la-ungroup],' +
                               '[data-ag-pick-close],[data-ag-back],[data-ag-toggle-sec],#la-publish,' +
                               window.LinkedAppsPanel.SELECTOR);
      if (!t) {
        if (pickerOpen && e.target.hasAttribute && e.target.hasAttribute('data-ag-pick-backdrop')) {
          pickerOpen = false; render();
        }
        return;
      }
      if (t.hasAttribute('data-la-src')) { pickSource(t.getAttribute('data-la-src')); }
      else if (t.hasAttribute('data-la-openpick')) {
        pickerOpen = true; pickerQuery = '';
        render();
        loadGroups().then(function () { if (pickerOpen) render(); });
      }
      else if (t.hasAttribute('data-la-pick')) {
        var gid = t.getAttribute('data-la-pick');
        if (chosenGroups.some(function (g) { return g.id === gid; })) {
          chosenGroups = chosenGroups.filter(function (g) { return g.id !== gid; });
        } else {
          var found = (groups || []).filter(function (g) { return g.id === gid; })[0];
          if (found) chosenGroups = chosenGroups.concat([{ id: found.id, name: found.name }]);
        }
        render();
      }
      else if (t.hasAttribute('data-la-ungroup')) {
        chosenGroups = chosenGroups.filter(function (g) { return g.id !== t.getAttribute('data-la-ungroup'); });
        render();
      }
      else if (t.hasAttribute('data-ag-pick-close')) { pickerOpen = false; render(); }
      else if (t.hasAttribute('data-ag-back')) { window.location.href = '/library'; }
      else if (t.hasAttribute('data-ag-toggle-sec')) {
        var sk = t.getAttribute('data-ag-toggle-sec');
        var el = mount.querySelector('[data-sec="' + sk + '"]');
        if (el) el.classList.toggle('collapsed');
      }
      else if (t.id === 'la-publish') { publish(); }
      else { thePanel().handle(t); }
    });

    document.addEventListener('input', function (e) {
      if (e.target.getAttribute && e.target.getAttribute('data-ag-search') === 'la-groups') {
        pickerQuery = e.target.value;
        var host = document.querySelector('[data-rows="la-groups"]');
        if (host) host.innerHTML = pickerRowsHtml();
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && pickerOpen) { pickerOpen = false; render(); }
    });
  }

  window.AgnesLinkedApps = {
    open: function (opts) {
      mount = opts.mount;
      wire();
      render();
      loadSources().then(function () {
        // Arriving from "Connect another server" with exactly one now
        // connected is not a guess — it is the source that did not exist a
        // minute ago. Anything else stays an explicit pick.
        if (opts.preferSourceId) pickSource(String(opts.preferSourceId));
      });
    },
  };
})(window);
