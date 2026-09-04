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

  /* ── Which tool lists the apps ────────────────────────────────────────
     Nothing in the MCP protocol says "this one lists apps", so it has to be
     guessed — the same guess the projection endpoint makes when it is told
     `lister: true`.

     The guess used to be one substring test, name carries both "data" and
     "app", first match wins. On the connector this was built around that
     match is `create_python_js_data_app_git_credential` and the real lister,
     `get_data_apps`, is fifteenth: "Read the app list", offered as a read,
     put a WRITE tool into materialize mode and invoked it with `{}` (#2154).

     So the guess is ranked, and two things disqualify a tool outright rather
     than merely ranking it low — because a fallback that can still reach a
     write tool is the bug itself:

       * a write-shaped verb in front of the name. `create_…`, `deploy_…`,
         `modify_…` do not list, whatever the rest of the name says.
       * required arguments. The lister is called with `{}`, so a schema
         demanding `configuration_id` cannot answer that call however it is
         named. This is the one hard FACT here; the rest is a name.

     `readOnlyHint` deliberately does NOT gate this. It is a tri-state, most
     public servers still send nothing, and registration stores that as
     `mutating: true` — so "candidates must be declared read-only" would leave
     no lister at all on exactly the servers this feature exists for. It ranks
     a candidate up when present; it never removes one. */

  var WRITE_VERB = /^(create|delete|remove|drop|deploy|modify|update|patch|set|add|put|post|write|rename|move|copy|start|stop|restart|enable|disable|install|uninstall|run|execute|trigger|publish|unpublish|share|revoke|grant|import|upload)(_|$)/;
  var READ_VERB = /^(get|list|read|search|fetch|describe|show|find|query)(_|$)/;

  /* Both shapes reach here: a registry row (`original_name` + `mutating`,
     from a registered source) and a probe row (`name` + the upstream's own
     tri-state `read_only`, from the builder before anything is registered).
     A registry row cannot tell "declared write" from "server said nothing" —
     both are `mutating: true` — so it reports `null`, which ranks rather
     than disqualifies. */
  function normalizeTool(t) {
    if (!t) return null;
    var name = String(t.original_name || t.exposed_name || t.name || '');
    if (!name) return null;
    var required = t.input_schema && t.input_schema.required;
    return {
      name: name,
      toolId: t.tool_id ? String(t.tool_id) : '',
      declaredReadOnly: typeof t.read_only === 'boolean'
        ? t.read_only
        : (t.mutating === false ? true : null),
      requiresArgs: !!(required && required.length),
    };
  }

  /* -1 = not a candidate. Lower is better. */
  function listerRank(t) {
    if (!t) return -1;
    var n = t.name.toLowerCase();
    if (n.indexOf('data') < 0 || n.indexOf('app') < 0) return -1;
    if (WRITE_VERB.test(n)) return -1;
    if (t.requiresArgs) return -1;
    var reads = READ_VERB.test(n);
    if (t.declaredReadOnly === true && reads) return 0;
    if (reads) return 1;
    if (t.declaredReadOnly === true) return 2;
    return 3;
  }

  /* Best first. Ties hold the server's own order, so the pick is stable
     across reads rather than depending on how the list arrived. */
  function listerCandidates(tools) {
    return (tools || [])
      .map(function (t, i) { return { t: normalizeTool(t), i: i }; })
      .map(function (c) { c.rank = listerRank(c.t); return c; })
      .filter(function (c) { return c.rank >= 0; })
      .sort(function (a, b) { return a.rank - b.rank || a.i - b.i; })
      .map(function (c) { return { name: c.t.name, tool_id: c.t.toolId, rank: c.rank }; });
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
      /* WHICH tool reads the list is worth stating either way: a plausible
         tool that answers with the wrong list looks exactly like a server
         with odd apps, so the failure it guards is a silent one.

         One candidate is a fact, and says so in a line. Several is a choice,
         and takes a select — not a row of buttons. This is configuration,
         not a view switch, so the tab vocabulary would mis-signal it; and
         real tool names run past 40 characters, which a button row cannot
         hold. The select also keeps the ranking honest: Agnes made a pick and
         it is shown as the chosen VALUE, not as one of N equal options. */
      var cands = o.candidates || [];
      var current = cands.filter(function (c) { return c.tool_id === o.toolId; })[0];
      var chooser = '';
      if (cands.length === 1) {
        chooser = '<p class="ag-note">Reading with <code>' + esc(cands[0].name) + '</code>.</p>';
      } else if (cands.length > 1) {
        chooser = '<label class="ag-field"><span>Tool that reads the list' +
          '<em> — Agnes picked the one most likely to list apps</em></span>' +
          '<select data-la-lister aria-label="Tool that reads the app list">' +
          cands.map(function (c) {
            return '<option value="' + esc(c.tool_id) + '"' +
              (c.tool_id === o.toolId ? ' selected' : '') + '>' + esc(c.name) + '</option>';
          }).join('') + '</select></label>';
      }
      if (cands.length > 1 && !current) {
        chooser += '<div class="ag-note ag-note--warn">The chosen tool is no longer offered by this ' +
          'server. Pick another before reading.</div>';
      }
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
          '</div>' + chooser + refresh + fetchBtn;
      }
      if (!st.apps.length) {
        return '<div class="ag-slot">' +
            '<p class="ag-slot-head">The server listed no apps.</p>' +
            '<p class="ag-slot-body">It answered, and the list was empty.</p>' +
          '</div>' + chooser + refresh + fetchBtn;
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
      return '<div class="ag-rows">' + rows + '</div>' + skipped + chooser + refresh + fetchBtn;
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

    /* The select's counterpart to `handle`. A host routes its existing
       `input` listener here the way it routes clicks — the panel still owns
       what the change MEANS, which is the rule both hosts are held to. */
    function handleInput(t) {
      if (!t || !t.hasAttribute || !t.hasAttribute('data-la-lister')) return false;
      var tid = t.value;
      if (tid && tid !== o.toolId) {
        /* Re-pointing at another lister drops what was read, for the same
           reason `setSource` does: a list read through one tool, shown under
           another tool's name, is the bug that would replace this one. */
        o.toolId = tid;
        st.apps = []; st.chosen = {}; st.fetched = false; st.err = null; st.skipped = 0;
        repaint();
      }
      return true;
    }

    return {
      html: html,
      handle: handle,
      handleInput: handleInput,
      summary: summary,
      publish: publish,
      chosen: chosen,
      /* Re-point at another source. The catalogue belongs to the source it
         came from, so everything read so far is dropped — showing one
         server's apps under another's name is the bug this prevents. */
      setSource: function (sourceId, toolId, candidates) {
        o.sourceId = sourceId || null;
        o.toolId = toolId || null;
        o.candidates = candidates || [];
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

  window.LinkedAppsPanel = { create: create, listerCandidates: listerCandidates, SELECTOR: '[data-la-read],[data-la-app]',
    INPUT_SELECTOR: '[data-la-lister]' };
})(window);
