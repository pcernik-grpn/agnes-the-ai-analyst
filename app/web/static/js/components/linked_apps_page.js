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
  var sources = [];          // [{id, name, url, listerToolId}] — the choosable ones
  var otherSources = [];     // connected, but exposing no tool that can list apps
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

  /* Only a source exposing a lister tool is CHOOSABLE — a server with no such
     tool cannot answer "what apps do you have", and offering it as a pick would
     be a row whose only outcome is a failure two clicks later.

     But it is still LISTED. Dropping it from the response entirely made the
     empty state ambiguous in the one way that matters: "no connected server
     lists apps" read identically whether nothing was connected at all or five
     servers were connected and none of them could list, and those want
     opposite next moves (connect one vs. add a tool to one you have). Keeping
     both lists lets the section show what is connected and say which half of
     it is usable. */
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
              return {
                id: String(full.id), name: String(full.name || full.id),
                url: String(full.url || full.command || ''),
                listerToolId: lister ? String(lister.tool_id) : '',
              };
            })
            // A source whose detail call failed is dropped, as before: nothing
            // can be said about whether it lists apps, so it is neither
            // choosable nor honestly listable as "cannot".
            .catch(function () { return null; });
        }));
      })
      .then(function (rows) {
        var all = rows.filter(Boolean);
        sources = all.filter(function (s) { return s.listerToolId; });
        otherSources = all.filter(function (s) { return !s.listerToolId; });
      })
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

  /* A re-click CLEARS the pick. It was set-only, on a control that is the same
     `.ag-tglbtn` as the panel's own `✓ On` / `Off` rows two sections down and
     wears the same `on` state — so it read as a toggle and behaved as a latch,
     and a caller who chose the wrong server had no way back to "none chosen".
     Still single-select: choosing a DIFFERENT server switches, as before.

     Clearing resets the panel — `setSource(null, …)` drops the app list, the
     per-app choices and any error. That is exactly what SWITCHING servers
     already did silently, and it is the only coherent option: an app list
     belongs to the server it was read from, so keeping one after the pick is
     gone would leave a list on screen that nothing accounts for. Re-choosing
     the server reads it again. Every downstream reader already handles the
     no-source state — the panel renders its idle prompt, its "Read the app
     list" button disables, and `publishBlocker()` returns to "Choose a server
     first." — so nothing else needs a branch for it. */
  function pickSource(id) {
    if (picked && picked.id === id) {
      picked = null;
      thePanel().setSource(null, null);
      render();
      return;
    }
    var row = sources.filter(function (s) { return s.id === id; })[0];
    if (!row) return;
    picked = row;
    thePanel().setSource(row.id, row.listerToolId);
    render();
  }

  /* ── Sections ───────────────────────────────────────────────────────── */

  /* Connecting a server is the one action here that LEAVES the page, so the
     section says so before the click rather than letting the caller discover
     it. Both endings are real and neither is a dead end: the source builder
     carries this same apps panel, so the errand can finish there, or the
     caller can return and find the new server in the list. */
  function connectRow() {
    return '<a class="ag-addrow" href="/admin/mcp-sources/new">+ Connect another server</a>' +
      '<p class="ag-note">This opens the source builder, so you will leave this page. You can publish ' +
      'the new server\'s apps there — it carries this same panel — or come back here afterwards and ' +
      'pick it from the list.</p>';
  }

  /* Connected servers that cannot list apps, shown rather than hidden: the
     reader's question is "where is my server?", and the answer is the row plus
     the reason, not its absence. Inert by construction — no `data-la-src`, so
     the click handler has nothing to match. */
  function offRowsHtml() {
    if (!otherSources.length) return '';
    return '<div class="ag-rows">' + otherSources.map(function (s) {
      return '<div class="ag-row ag-row--off">' +
        '<div class="ag-row-body"><div class="ag-row-name">' + esc(s.name) +
          '<span class="ag-row-meta">cannot list apps</span></div>' +
          (s.url ? '<div class="ag-row-desc">' + esc(s.url) + '</div>' : '') + '</div>' +
        '<button type="button" class="ag-tglbtn" disabled>Choose</button>' +
      '</div>';
    }).join('') + '</div>';
  }

  function sourcesBody() {
    if (loading) return '<p class="ag-note">Reading the connected servers…</p>';
    if (sourcesErr) return '<div class="ag-note ag-note--err">' + esc(sourcesErr) + '</div>';
    if (!sources.length) {
      /* Two different empty states, because they have two different fixes.
         Something IS connected → the gap is a lister tool on a server you
         already have, and the rows below name which ones. Nothing is → the
         gap is a server. */
      var slot = otherSources.length
        ? '<p class="ag-slot-head">None of your connected servers can list apps.</p>' +
          '<p class="ag-slot-body">A server has to expose a tool that lists apps before its apps can be ' +
          'catalogued here. These are connected, but none of them do — add a lister tool to one of them, ' +
          'or connect a server that has one.</p>'
        : '<p class="ag-slot-head">No server is connected yet.</p>' +
          '<p class="ag-slot-body">Apps are catalogued from a connected tool server that can list ' +
          'them.</p>';
      return '<div class="ag-slot">' + slot + '</div>' + offRowsHtml() + connectRow();
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
    return '<div class="ag-rows">' + rows + '</div>' + offRowsHtml() + connectRow();
  }

  /* ── The Library preview — the shell's LEFT pane ──────────────────────
     Both other callers of `BuilderShell.workspace()` put an assistant
     transcript here (mcp_builder, package_drawer). This page has no assistant
     and should not grow one: every step is a pick from a short enumerated
     list, so there is nothing to draft, and an assistant would make the page
     depend on an LLM credential it does not need. So the pane held three
     paragraphs of static prose — in a 50%-width column sized for a chat, in
     `.ag-cfg-blurb`, which is a 12.5px CENTRED CAPTION component. Half an
     empty screen, set as a caption.

     What earns the space is the one thing the configuration cannot show: what
     the primary button will DO. These are the rows that will appear in the
     Library, for the groups granted, updating as apps and groups are toggled.

     Strictly read-only — no `data-*` hook the page's click handler could
     match. Every control stays in the configuration on the right, so there is
     exactly one place to change any given thing.

     Names and descriptions come from an external tool server, so they are
     escaped here like everywhere else that renders them. */

  var APP_GLYPH =
    '<svg class="la-prev-glyph" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5" stroke="currentColor" stroke-width="1.6"/>' +
      '<path d="M3.5 9h17" stroke="currentColor" stroke-width="1.6"/>' +
      '<circle cx="6.6" cy="6.8" r="0.9" fill="currentColor"/></svg>';

  function previewNothing(head, body) {
    return '<div class="la-prev-body la-prev-body--empty"><div class="la-prev-empty">' +
        '<svg class="la-prev-empty-glyph" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
          '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5" stroke="currentColor" stroke-width="1.6"/>' +
          '<path d="M3.5 9h17" stroke="currentColor" stroke-width="1.6"/>' +
          '<circle cx="6.6" cy="6.8" r="0.9" fill="currentColor"/></svg>' +
        '<p class="la-prev-empty-head">' + esc(head) + '</p>' +
        '<p class="la-prev-empty-body">' + esc(body) + '</p>' +
      '</div></div>';
  }

  /* Who will see them. Rendered inside the preview because "published to
     nobody" is the outcome most worth seeing before you commit it — the
     configuration says `nobody` in a summary line, which is easy to read past. */
  function previewAudience() {
    if (!chosenGroups.length) {
      return '<div class="la-prev-lbl"><span>Visible to</span></div>' +
        '<p class="la-prev-none">Nobody yet — they would be catalogued and invisible. Grant a group ' +
        'in step 3.</p>';
    }
    return '<div class="la-prev-lbl"><span>Visible to</span></div>' +
      '<div class="la-prev-pills">' + chosenGroups.map(function (g) {
        return '<span class="ag-instack">' + esc(g.name) + '</span>';
      }).join('') + '</div>';
  }

  function previewBody() {
    if (!picked) {
      return previewNothing('Nothing to show yet',
        'Choose a server and read its app list. What you leave on will appear here, exactly as the ' +
        'groups you grant will find it in their Library.');
    }
    var progress = thePanel().state();
    if (progress.fetching) return previewNothing('Reading…', 'Asking ' + picked.name + ' what apps it has.');
    if (progress.err) {
      /* The panel already shows the error, with the retry next to it. Saying it
         twice in two panes would read as two failures. */
      return previewNothing('Nothing to show yet',
        'The app list could not be read — step 2 has the details.');
    }
    if (!progress.fetched) {
      return previewNothing('Not read yet',
        'Read ' + picked.name + '’s app list in step 2. What you leave on will appear here.');
    }
    if (!progress.total) {
      return previewNothing('That server lists no apps',
        picked.name + ' answered, but with nothing in it. There is nothing to publish from here.');
    }
    var apps = thePanel().chosen();
    if (!apps.length) {
      return previewNothing('Every app is switched off',
        'All ' + progress.total + ' of them. Leave at least one on in step 2 — otherwise publishing ' +
        'catalogues nothing.');
    }
    var rows = apps.map(function (a) {
      return '<div class="ag-row la-prev-row">' + APP_GLYPH +
        '<div class="ag-row-body">' +
          '<div class="ag-row-name">' + esc(a.name) + '<span class="ag-row-meta">Data app</span></div>' +
          (a.description ? '<div class="ag-row-desc">' + esc(a.description) + '</div>' : '') +
        '</div>' +
      '</div>';
    }).join('');
    return '<div class="la-prev-body">' +
        '<div class="la-prev-lbl"><span>Apps</span>' +
          '<span class="ag-row-meta">' + apps.length + ' of ' + progress.total + ' on</span></div>' +
        '<div class="ag-rows">' + rows + '</div>' +
        previewAudience() +
        '<p class="ag-note">Agnes catalogues where each app lives and who may open it. It does not ' +
        'host the app or proxy traffic to it.</p>' +
      '</div>';
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

  /* Every interaction in the configuration repaints the WHOLE mount, which
     destroys the scrolling column and rebuilds it at offset 0 — so choosing a
     group in section 3 threw the reader back up to section 1, and picking the
     second of a long list of groups reset the picker's own scroll each time.
     The offsets are read before the write and re-applied after it.

     FOUR of them, because which element scrolls depends on the viewport and on
     what is open: `.ag-cfg-body` is the configuration column (builder.css),
     `.la-prev-body` the Library preview in the left pane, `.ag-pick-rows` the
     picker modal's list, and below the two-pane breakpoint the layout hands
     scrolling back to the PAGE (`body.ag-building .idx` goes
     `overflow: visible`), where the same repaint loses `window.scrollY`
     instead. Restoring an offset the element held a moment ago is always
     right; `scrollTop` clamps itself when the new content is shorter.

     Add a scroller to this page → add it here, or it silently jumps. */
  var SCROLLERS = ['.ag-cfg-body', '.la-prev-body', '.ag-pick-rows'];

  function scrollState() {
    var tops = SCROLLERS.map(function (sel) {
      var el = mount.querySelector(sel);
      return el ? el.scrollTop : 0;
    });
    return { tops: tops, win: window.scrollY };
  }

  function restoreScroll(was) {
    SCROLLERS.forEach(function (sel, i) {
      var el = mount.querySelector(sel);
      if (el && was.tops[i]) el.scrollTop = was.tops[i];
    });
    if (was.win) window.scrollTo(0, was.win);
  }

  function render() {
    if (!mount) return;
    var was = scrollState();
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
        /* Mirrors the configuration pane's own header component, so the two
           columns start on the same line — which is most of what stops the
           left one reading as leftover space. */
        left: '<div class="ag-cfg-head"><div class="ag-cfg-head-main">' +
            '<h3>In the Library</h3><p>what the groups you chose will see</p>' +
          '</div></div>' + previewBody(),
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
    restoreScroll(was);
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
