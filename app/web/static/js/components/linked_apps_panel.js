/*
 * linked_apps_panel.js — cataloguing the apps an MCP server lists, once.
 *
 * Two surfaces need this and they are not the same errand:
 *
 *   • The MCP builder, while you are connecting a server that happens to list
 *     apps. Publishing them there is the obvious next move and asking you to
 *     go somewhere else for it is a round trip nobody wanted.
 *   • /admin/linked-apps, when the server was connected last month and today's
 *     job is "put those apps in front of the finance team". That errand starts
 *     from the app list, not from a connection form.
 *
 * The failure mode this file exists to prevent is the two drifting: the old
 * standalone builder and the section that replaced it would each have their
 * own idea of what "read the list" writes, and only one of them would be
 * right. So the state, the requests and the markup live here, and each host
 * supplies a source id, the groups to grant to, and a repaint callback.
 *
 * What a host still owns: where the panel sits, when it is shown, and what
 * happens after publishing.
 */
(function (window) {
  'use strict';

  var SOURCES_API = '/api/admin/mcp-sources';
  var TOOLS_API = '/api/admin/mcp-tools';
  var APPS_API = '/api/data-apps';
  var GRANTS_API = '/api/admin/grants';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {})).then(function (r) {
      if (r.status === 204) return null;
      return r.json().catch(function () { return null; }).then(function (body) {
        if (!r.ok) {
          var detail = body && (body.detail || body.error);
          var msg = (detail && (detail.message || detail.code)) || detail || ('HTTP ' + r.status);
          var e = new Error(String(msg));
          e.status = r.status;
          e.detail = detail;
          throw e;
        }
        return body;
      });
    });
  }

  function sendJson(url, body, method) {
    return api(url, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  /* A tool that lists data apps. Name-shaped rather than declared, because
     nothing in the MCP protocol says "this one lists apps" — the same guess
     the projection endpoint makes when it is told `lister: true`. */
  function isLister(name) {
    var n = String(name || '').toLowerCase();
    return n.indexOf('data') >= 0 && n.indexOf('app') >= 0;
  }

  /**
   * @param {object} o
   *   sourceId   — the registered source to read from (may be null until one exists)
   *   toolId     — the lister tool's registry id
   *   groups     — () => [{id, name}] the apps are granted to
   *   onChange   — () => void, called when the panel needs repainting
   *   idlePrompt — {head, body} shown when there is no source yet
   */
  function create(o) {
    var st = {
      fetching: false, fetched: false, err: null,
      apps: [], chosen: {}, skipped: 0,
    };
    var repaint = o.onChange || function () {};

    function chosen() {
      return st.apps.filter(function (a) { return st.chosen[a.id] !== false; });
    }

    /* Reading the list needs the tool in materialize mode — the projection
       reads a table the run writes — and the registry refuses that mode
       without a schedule string (`tool_registry.upsert`).
       
       So the schedule is sent, and the UI does NOT dress it up as a refresh
       job: nothing in the scheduler reads a tool_registry row, so the
       `daily 03:00` the old wizard wrote never fired. Offering "keep this
       list current" would be promising a refresh the product does not
       perform. What is true is that the catalogue is a snapshot, and reading
       again is what updates it — which is what the panel says. */
    function fetchApps() {
      if (st.fetching || !o.sourceId || !o.toolId) return;
      st.fetching = true; st.err = null;
      repaint();
      var mode = { mode: 'materialize', schedule: 'daily 03:00' };
      sendJson(TOOLS_API + '/' + encodeURIComponent(o.toolId), mode, 'PUT')
        .catch(function (e) { if (!e || e.status !== 409) throw e; })
        .then(function () {
          return sendJson(SOURCES_API + '/' + encodeURIComponent(o.sourceId) + '/materialize',
                          { tool_id: o.toolId, lister: true });
        })
        .then(function (res) {
          st.skipped = (res && res.linked_projection && res.linked_projection.skipped) || 0;
          /* `/materialize` answers 200 with an `errors[]` body when the run
             itself failed — an upstream validation error, a missing argument.
             Without this the list below simply comes back empty and the panel
             renders "none listed", which reads as "this server has no apps"
             rather than "the read failed". Carried over from the inline
             wizard this component replaced, where the same response was
             reported as "Materialized, but this tool wrote no table to
             project from" for exactly the same reason. */
          var errs = (res && res.errors) || [];
          if (errs.length) {
            st.err = 'Materialize failed: ' + errs.map(function (e) {
              return (e.tool ? e.tool + ': ' : '') + (e.error || e.code || 'unknown error');
            }).join(' · ');
          }
          return api(APPS_API + '?kind=linked&source=' + encodeURIComponent(o.sourceId));
        })
        .then(function (body) {
          var rows = Array.isArray(body) ? body : (body && body.apps) || (body && body.items) || [];
          st.apps = rows.map(function (a) {
            return {
              id: String(a.id), name: String(a.name || a.slug || a.id),
              /* Kept alongside `id` because the two are used for different
                 things and are NOT interchangeable: `id` is this panel's own
                 per-app key (`st.chosen`), while a `data_app` grant is keyed
                 by SLUG — `id_format="<slug>"` on the ResourceTypeSpec, and
                 `_can_view` looks up `can_access(…, row["slug"])`. */
              slug: String(a.slug || a.id),
              url: String(a.external_url || a.url || ''),
              description: String(a.description || ''),
            };
          });
          st.apps.forEach(function (a) { if (st.chosen[a.id] === undefined) st.chosen[a.id] = true; });
          st.fetched = true;
        })
        .catch(function (e) {
          st.err = (e && e.message) === 'data_apps_disabled'
            ? 'Data apps are switched off on this instance, so its apps cannot be catalogued.'
            : ((e && e.message) || 'Could not read the app list.');
        })
        .then(function () { st.fetching = false; repaint(); });
    }

    /* One grant per (app × group). A 409 is the end state it asked for, and a
       real failure is RETURNED rather than thrown: "shared with Finance" and
       "shared with nobody" look identical on the next page, so the caller has
       to be able to say which happened. */
    /* `resource_id` is the app's SLUG, not its row id. Every reader of a
       `data_app` grant looks it up by slug — `_can_view` calls
       `can_access(…, row["slug"])`, the Library's apps band tests
       `da["slug"] in granted_ids`, and the ResourceTypeSpec declares
       `id_format="<slug>"`. Sending `a.id` wrote grant rows that nothing ever
       reads: publishing reported success, redirected to the Library, and the
       granted group still could not see the apps. */
    function publish() {
      var groups = (o.groups && o.groups()) || [];
      var calls = [];
      chosen().forEach(function (a) {
        groups.forEach(function (g) {
          calls.push(
            sendJson(GRANTS_API, { group_id: g.id, resource_type: 'data_app', resource_id: a.slug })
              .then(function () { return null; })
              .catch(function (e) {
                if (e && e.status === 409) return null;
                return a.name + ' → ' + g.name + ': ' + ((e && e.message) || 'unknown error');
              })
          );
        });
      });
      return Promise.all(calls).then(function (results) { return results.filter(Boolean); });
    }

    function summary() {
      if (!st.fetched) return 'not read yet';
      if (!st.apps.length) return 'none listed';
      return chosen().length + ' of ' + st.apps.length;
    }

    function html() {
      var fetchBtn = '<button type="button" class="ag-addrow" data-la-read' +
        (st.fetching || !o.sourceId ? ' disabled' : '') + '>' +
        (st.fetching ? 'Reading…' : (st.fetched ? 'Read again' : '+ Read the app list')) + '</button>';
      /* Said, not offered: there is no refresh switch because there is no
         refresher. Reading again is the whole update mechanism. */
      var refresh = st.fetched
        ? '<p class="ag-note">This is what the server listed when you read it. Read again to pick up ' +
          'anything added since.</p>'
        : '';
      if (st.err) fetchBtn += '<div class="ag-note ag-note--err">' + esc(st.err) + '</div>';
      if (!o.sourceId) {
        var idle = o.idlePrompt || {};
        return '<div class="ag-slot">' +
            '<p class="ag-slot-head">' + esc(idle.head || 'No server chosen yet.') + '</p>' +
            '<p class="ag-slot-body">' + esc(idle.body || 'Pick one to read its app list.') + '</p>' +
          '</div>';
      }
      if (!st.fetched) {
        return '<div class="ag-slot">' +
            '<p class="ag-slot-head">This server lists apps.</p>' +
            '<p class="ag-slot-body">Agnes can read the list and catalogue it here, so the apps show up in the ' +
            'Library for the groups you grant. Reading it writes the catalogue; nothing is shared until you grant.</p>' +
          '</div>' + refresh + fetchBtn;
      }
      if (!st.apps.length) {
        return '<div class="ag-slot">' +
            '<p class="ag-slot-head">The server listed no apps.</p>' +
            '<p class="ag-slot-body">It answered, and the list was empty.</p>' +
          '</div>' + refresh + fetchBtn;
      }
      var rows = st.apps.map(function (a) {
        var on = st.chosen[a.id] !== false;
        return '<div class="ag-row">' +
          '<div class="ag-row-body"><div class="ag-row-name">' + esc(a.name) + '</div>' +
            (a.description ? '<div class="ag-row-desc">' + esc(a.description) + '</div>' : '') + '</div>' +
          '<button type="button" class="ag-tglbtn' + (on ? ' on' : '') + '" data-la-app="' + esc(a.id) + '">' +
            (on ? '✓ On' : 'Off') + '</button>' +
        '</div>';
      }).join('');
      var skipped = st.skipped
        ? '<div class="ag-note ag-note--warn">' + st.skipped + ' row' + (st.skipped > 1 ? 's' : '') +
          ' could not be read — the columns did not match what Agnes expects of an app.</div>'
        : '';
      return '<div class="ag-rows">' + rows + '</div>' + skipped + refresh + fetchBtn;
    }

    /* One click handler for the whole panel. Returns true when it acted, so a
       host can fall through to its own targets. */
    function handle(t) {
      if (!t || !t.hasAttribute) return false;
      if (t.hasAttribute('data-la-read')) { fetchApps(); return true; }
      if (t.hasAttribute('data-la-app')) {
        var id = t.getAttribute('data-la-app');
        st.chosen[id] = st.chosen[id] === false;
        repaint();
        return true;
      }
      return false;
    }

    return {
      html: html,
      handle: handle,
      summary: summary,
      publish: publish,
      chosen: chosen,
      /* Re-point at another source. The catalogue belongs to the source it
         came from, so everything read so far is dropped — showing one
         server's apps under another's name is the bug this prevents. */
      setSource: function (sourceId, toolId) {
        o.sourceId = sourceId || null;
        o.toolId = toolId || null;
        st.apps = []; st.chosen = {}; st.fetched = false; st.err = null; st.skipped = 0;
      },
      sourceId: function () { return o.sourceId; },
      /* Read-only view of the panel's progress, for a host that renders
         something ABOUT the catalogue rather than the catalogue itself (the
         standalone page's Library preview). `chosen()` alone cannot tell "not
         read yet" from "read, and everything switched off" — both are an empty
         array — and those need different things said about them. A fresh
         object each call: the internals stay the panel's. */
      state: function () {
        return { fetched: st.fetched, fetching: st.fetching, total: st.apps.length, err: st.err };
      },
    };
  }

  window.LinkedAppsPanel = { create: create, isLister: isLister, SELECTOR: '[data-la-read],[data-la-app]' };
})(window);
