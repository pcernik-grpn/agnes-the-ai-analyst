/* =====================================================================
 * mcp_builder.js — connecting a tool server, in the builder shell.
 *
 * /admin/mcp-sources is a LIST with a create modal, and the modal is only the
 * first of five steps: register, store a secret, introspect, curate the tools,
 * grant them. An admin was expected to know that order, do it across two
 * pages, and find out whether the server was even reachable at step three.
 *
 * This is the same five things as one build. What makes an MCP source suit the
 * shell better than it looks: the procedure is FIXED, so the conversation is
 * not inventing content — it sequences work and reports what the server said.
 * The exact values still arrive by paste, into fields, because a URL and an
 * env var name are values from another system (the criterion in
 * docs/superpowers/specs/2026-08-26-conversational-entity-builder-design.md).
 *
 * Nothing is written until Save. Check connection dials through
 * POST /mcp-sources/preview-introspect, which builds a row-shaped dict and
 * throws it away — so the admin sees the real tool list before agreeing to
 * create anything. Save then does the whole sequence in order and reports
 * which step failed if one does.
 * ===================================================================== */
(function (window) {
  'use strict';

  var esc = window.BuilderShell.esc;

  var SOURCES_API = '/api/admin/mcp-sources';
  var GROUPS_API = '/api/admin/groups';
  var TURN_API = '/api/admin/mcp-sources/builder/turn';
  var TOOLS_API = '/api/admin/mcp-tools';

  var TRANSPORTS = [
    { id: 'http', label: 'HTTP', hint: 'streamable' },
    { id: 'sse', label: 'SSE', hint: 'server-sent events' },
    { id: 'stdio', label: 'stdio', hint: 'subprocess' },
  ];
  var AUTH = [
    { id: '', label: 'None' },
    { id: 'bearer', label: 'Bearer token' },
    { id: 'oauth', label: 'OAuth' },
  ];

  var mount = null, draft = null, groups = null, groupsErr = false;
  var conv = [], convBusy = false, convErr = null, convDraft = '', convChips = [];
  var convEngine = null, convSlots = null, opened = false;
  var collapsed = {};
  var checking = false, checkErr = null;
  var saving = false, saveErr = null;
  var pickerOpen = false, pickerQuery = '';

  function newDraft() {
    return {
      name: '', transport: 'http', url: '', command: '', args: [],
      auth_method: '', auth_secret_env: '', scope: 'shared',
      // "not asked yet" is distinguishable from "decided: none" — otherwise the
      // auth slot settles itself before the admin has chosen anything.
      auth_decided: false,
      secret_value: '',
      introspected: false,
      tools: [],          // [{name, description, input_schema}] from the server
      enabled: {},        // tool name -> true; every tool starts on
      groups: [],         // [{id, name}] to grant after create
    };
  }

  /* ── Server calls ──────────────────────────────────────────────────── */

  function readJson(r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) {
        var d = j && j.detail;
        var msg = typeof d === 'string' ? d : (d && (d.hint || d.kind)) || ('HTTP ' + r.status);
        var e = new Error(msg);
        e.status = r.status;
        throw e;
      }
      return j;
    });
  }
  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {})).then(readJson);
  }
  function postJson(url, body) {
    return api(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  }

  /* The connection as the server takes it. One shape, three consumers (the
     turn, the check, the create), so they cannot disagree about what the
     admin typed. */
  function connectionPayload() {
    var p = { transport: draft.transport };
    if (draft.transport === 'stdio') {
      p.command = draft.command;
      p.args = draft.args;
    } else {
      p.url = draft.url;
    }
    if (draft.auth_method) p.auth_method = draft.auth_method;
    if (draft.auth_secret_env) p.auth_secret_env = draft.auth_secret_env;
    return p;
  }

  /* ── The conversation ──────────────────────────────────────────────── */

  function sendTurn(text) {
    if (convBusy) return;
    var opening = !text && !conv.length;
    if (!text && !opening) return;
    if (!opening) conv = conv.concat([{ role: 'user', text: text }]);
    convBusy = true; convErr = null; convDraft = ''; convChips = [];
    render();
    postJson(TURN_API, {
      message: text || '',
      history: opening ? [] : conv.slice(0, -1),
      draft: {
        name: draft.name, transport: draft.transport, url: draft.url,
        command: draft.command, args: draft.args,
        auth_method: draft.auth_method, auth_secret_env: draft.auth_secret_env,
        scope: draft.scope, auth_decided: !!draft.auth_decided,
        introspected: !!draft.introspected,
        tool_names: (draft.tools || []).map(function (t) { return t.name; }),
      },
    }).then(function (body) {
      conv = conv.concat([{ role: 'assistant', text: body.reply || '' }]);
      convChips = body.suggestions || [];
      convEngine = body.engine || null;
      convSlots = body.slots || null;
      var patch = body.patch || {};
      Object.keys(patch).forEach(function (k) { draft[k] = patch[k]; });
    }).catch(function (e) {
      convErr = e.message || 'The assistant could not answer.';
    }).then(function () {
      convBusy = false;
      render();
    });
  }

  /* ── Check connection ──────────────────────────────────────────────── */

  function checkConnection() {
    if (checking) return;
    checking = true; checkErr = null;
    render();
    postJson(SOURCES_API + '/preview-introspect', connectionPayload())
      .then(function (body) {
        var tools = (body && body.tools) || [];
        draft.tools = tools.map(function (t) {
          return {
            name: String(t.name || t),
            description: String(t.description || ''),
            // Kept, not dropped: this is what a passthrough row stores so an
            // agent knows the tool's arguments. The mapper used to keep only
            // name + description, which was fine while nothing here was
            // written and wrong the moment Save started registering tools.
            input_schema: (t && typeof t.input_schema === 'object' && t.input_schema) || null,
          };
        });
        draft.enabled = {};
        draft.tools.forEach(function (t) { draft.enabled[t.name] = true; });
        draft.introspected = true;
        // Nothing to call it yet? The host is the honest first guess, and the
        // admin is standing right here to correct it.
        if (!draft.name.trim() && draft.url) {
          draft.name = String(draft.url).replace(/^https?:\/\//, '').split('/')[0];
        }
      })
      .catch(function (e) { checkErr = e.message || 'Could not reach the server.'; })
      .then(function () { checking = false; render(); });
  }

  /* ── Save ───────────────────────────────────────────────────────────
     The five steps, in the order they depend on each other, with the step
     named if one fails — "it didn't save" over a five-call sequence tells an
     admin nothing about which half of it happened.

     RESUMABLE, which is the part that is easy to get wrong: three of the steps
     run AFTER the source row exists, so a failure in one of them leaves a
     registered source and an error on screen. If Save then re-ran the whole
     sequence it would register a SECOND source — the admin's only recovery
     from "stored the secret but the grant failed" would be to create a
     duplicate. `savedId` remembers the row, so pressing Save again resumes
     from the step that failed.

     A grant that already exists answers 409, and for this sequence that is
     success: the end state Save is asking for is "this group can reach the
     source", and a 409 says it can. Treating it as a failure is what made the
     retry above unreachable even once the row was correct. */

  var savedId = null;

  function grantGroup(sourceId, group) {
    return postJson(SOURCES_API + '/' + encodeURIComponent(sourceId) + '/grants', { group_id: group.id })
      .catch(function (e) {
        if (e && e.status === 409) return null;  // already granted — the end state we wanted
        throw e;
      });
  }

  /* Register one introspected tool so agents can actually call it.

     A source with no `tool_registry` rows exposes NOTHING: the MCP server
     builds its tool list from `list_by_mode('passthrough', enabled_only=True)`
     (app/api/mcp/tools_generator.py), so Save used to hand back a registered
     source with zero callable tools — while the Tools panel said "turn off
     anything agents should not call" and counted "N of M" on. The toggles
     described an outcome Save did not produce, in both directions.

     `tool_id` is deterministic (`<source>__<original name>`, the same
     composite the source detail page uses) so pressing Save twice cannot
     create a second row for one tool, and the 409 a re-register answers reads
     as done — the same end-state-not-error rule the grant step follows.

     `mode` is passthrough: the mode the toggle is ABOUT (callable by an
     agent). Materialize needs a schedule and a table, which this panel does
     not ask for; the source's own page still offers it. */
  function registerTool(sourceId, tool) {
    return postJson(TOOLS_API, {
      tool_id: sourceId + '__' + tool.name,
      source_id: sourceId,
      original_name: tool.name,
      exposed_name: tool.name,
      mode: 'passthrough',
      description: tool.description || null,
      input_schema: tool.input_schema || null,
      enabled: true,
    }).catch(function (e) {
      if (e && e.status === 409) return null;  // already registered — the end state we wanted
      throw e;
    });
  }

  function enabledTools() {
    return draft.tools.filter(function (t) { return draft.enabled[t.name] !== false; });
  }

  function save() {
    if (saving || !canSave()) return;
    saving = true; saveErr = null;
    render();
    var body = Object.assign({ name: draft.name.trim(), enabled: true, scope: draft.scope },
                             connectionPayload());
    var step = savedId
      ? Promise.resolve({ id: savedId })
      : postJson(SOURCES_API, body).then(function (res) { savedId = res.id; return res; });
    step
      .then(function (res) {
        if (!draft.secret_value.trim()) return null;
        return api(SOURCES_API + '/' + encodeURIComponent(res.id) + '/secret', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: draft.secret_value }),
        }).catch(function (e) { throw new Error('Registered, but storing the secret failed: ' + e.message); });
      })
      .then(function () {
        // Before the grants: a group pointed at a source with no callable
        // tools has been given nothing.
        var wanted = enabledTools();
        if (!wanted.length) return null;
        return Promise.all(wanted.map(function (t) {
          return registerTool(savedId, t);
        })).catch(function (e) {
          throw new Error('Registered, but adding the tools failed: ' + e.message);
        });
      })
      .then(function () {
        if (!draft.groups.length) return null;
        return Promise.all(draft.groups.map(function (g) {
          return grantGroup(savedId, g);
        })).catch(function (e) {
          throw new Error('Registered, but granting access failed: ' + e.message);
        });
      })
      .then(function () {
        window.location.href = '/admin/mcp-sources/' + encodeURIComponent(savedId);
      })
      .catch(function (e) {
        saveErr = (e.message || 'Could not register the source.') +
                  (savedId ? ' The source is registered — press Save again to finish the rest.' : '');
        saving = false;
        render();
      });
  }

  function canSave() {
    if (!draft.name.trim()) return false;
    return draft.transport === 'stdio' ? !!draft.command.trim() : !!draft.url.trim();
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

  function groupPicked(id) {
    return draft.groups.some(function (g) { return g.id === id; });
  }

  function toggleGroup(id) {
    if (groupPicked(id)) {
      draft.groups = draft.groups.filter(function (g) { return g.id !== id; });
    } else {
      var found = (groups || []).filter(function (g) { return g.id === id; })[0];
      if (found) draft.groups = draft.groups.concat([{ id: found.id, name: found.name }]);
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
      return '<div class="ag-row">' +
        '<div class="ag-row-body">' +
          '<div class="ag-row-name">' + esc(g.name) + '</div>' +
          (typeof g.members === 'number'
            ? '<div class="ag-row-desc">' + g.members + (g.members === 1 ? ' member' : ' members') + '</div>'
            : '') +
        '</div>' +
        '<button type="button" class="ag-tglbtn' + (on ? ' on' : '') + '" data-mcp-pick="' + esc(g.id) + '">' +
          (on ? '✓ Added' : '+ Add') + '</button>' +
      '</div>';
    }).join('');
  }

  function pickerHtml() {
    if (!pickerOpen) return '';
    return window.BuilderShell.picker({
      key: 'mcp-groups',
      title: 'Who can use these tools',
      sub: 'Granting a group means every agent its members build can call this server. That is the widest thing you do here.',
      searchPlaceholder: 'Search groups…',
      query: pickerQuery,
      shown: groups === null ? 0 : pickerMatches().length,
      total: groups === null ? 0 : groups.length,
      rows: pickerRowsHtml(),
      foot: 'Groups are managed in <a href="/admin/access">Access</a>.',
    });
  }

  function renderPicker() {
    var host = document.getElementById('mcp-picker');
    if (host) host.innerHTML = pickerHtml();
  }
  function renderPickerRows() {
    var el = document.querySelector('[data-rows="mcp-groups"]');
    if (!el) { renderPicker(); return; }
    el.innerHTML = pickerRowsHtml();
    var cnt = document.querySelector('[data-count="mcp-groups"]');
    if (cnt && groups !== null) cnt.textContent = pickerMatches().length + ' of ' + groups.length;
  }
  function openPicker() {
    pickerOpen = true; pickerQuery = '';
    renderPicker();
    loadGroups().then(function () { if (pickerOpen) renderPickerRows(); });
    var box = document.querySelector('[data-ag-search="mcp-groups"]');
    if (box) box.focus();
  }
  function closePicker() {
    if (!pickerOpen) return;
    pickerOpen = false;
    render();
  }

  /* ── Panel sections ────────────────────────────────────────────────── */

  function segHtml(name, items, current) {
    return '<div class="ag-chips" role="group">' + items.map(function (it) {
      var on = it.id === current;
      return '<button type="button" class="ag-chip' + (on ? ' on' : '') + '" ' +
        'data-mcp-seg="' + esc(name) + '" data-mcp-val="' + esc(it.id) + '" ' +
        'aria-pressed="' + on + '">' + esc(it.label) +
        (it.hint ? ' <span class="ag-sec-note">' + esc(it.hint) + '</span>' : '') + '</button>';
    }).join('') + '</div>';
  }

  function field(label, key, placeholder, note) {
    return '<label class="ag-field"><span>' + esc(label) +
        (note ? ' <em>— ' + esc(note) + '</em>' : '') + '</span>' +
      '<input type="text" data-mcp-field="' + esc(key) + '" value="' + esc(draft[key] || '') + '" ' +
        'placeholder="' + esc(placeholder || '') + '" autocomplete="off" spellcheck="false"></label>';
  }

  function connectionBody() {
    var addr = draft.transport === 'stdio'
      ? field('Command', 'command', 'npx', 'the executable the subprocess runs') +
        '<label class="ag-field"><span>Arguments <em>— one per line</em></span>' +
          '<textarea rows="3" data-mcp-field="args" placeholder="-y&#10;@acme/crm-mcp">' +
          esc((draft.args || []).join('\n')) + '</textarea></label>'
      : field('Endpoint URL', 'url', 'https://mcp.example.com/sse');
    return segHtml('transport', TRANSPORTS, draft.transport) + addr +
      field('Name', 'name', 'Acme CRM', 'what it is called in the source list');
  }

  function authBody() {
    var body = segHtml('auth_method', AUTH, draft.auth_method);
    if (draft.auth_method === 'bearer') {
      body += field('Token variable', 'auth_secret_env', 'ACME_MCP_TOKEN',
                    'the NAME of the variable, never the token') +
        '<label class="ag-field"><span>Token <em>— stored in the vault, never shown again</em></span>' +
          '<input type="password" data-mcp-field="secret_value" value="' + esc(draft.secret_value) + '" ' +
            'placeholder="paste the token" autocomplete="off"></label>' +
        '<div class="ag-note">The token goes to the vault on Save and is never rendered back. ' +
        'The variable name is what the source stores and what you will see afterwards.</div>';
    } else if (draft.auth_method === 'oauth') {
      body += '<div class="ag-note">Each analyst connects their own account in the browser. ' +
        'Register the OAuth client on the source page after saving.</div>';
    } else {
      body += '<div class="ag-note">Nothing is sent with the connection. Fine for a server ' +
        'that is already reachable only from inside your network.</div>';
    }
    return body;
  }

  function toolsBody() {
    var check = '<button type="button" class="ag-addrow" data-mcp-check' +
      (checking ? ' disabled' : '') + '>' +
      (checking ? 'Connecting…' : (draft.introspected ? 'Check again' : '+ Check connection')) + '</button>';
    if (checkErr) {
      check += '<div class="ag-note ag-note--err">' + esc(checkErr) + '</div>';
    }
    if (!draft.introspected) {
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">Nothing checked yet.</p>' +
          '<p class="ag-slot-body">Agnes dials the server and lists what it exposes. ' +
          'Nothing is saved by checking — it is how you see the tools before agreeing to any of them.</p>' +
        '</div>' + check;
    }
    if (!draft.tools.length) {
      return '<div class="ag-slot">' +
          '<p class="ag-slot-head">Connected, but it exposes no tools.</p>' +
          '<p class="ag-slot-body">The server answered and listed nothing. Registering it is allowed ' +
          'and gives agents nothing to call.</p>' +
        '</div>' + check;
    }
    var rows = draft.tools.map(function (t) {
      var on = draft.enabled[t.name] !== false;
      return '<div class="ag-row">' +
        '<div class="ag-row-body">' +
          '<div class="ag-row-name">' + esc(t.name) + '</div>' +
          (t.description ? '<div class="ag-row-desc">' + esc(t.description) + '</div>' : '') +
        '</div>' +
        '<button type="button" class="ag-tglbtn' + (on ? ' on' : '') + '" ' +
          'data-mcp-tool="' + esc(t.name) + '">' + (on ? '✓ On' : 'Off') + '</button>' +
      '</div>';
    }).join('');
    return '<div class="ag-rows">' + rows + '</div>' + check;
  }

  function accessBody() {
    var rows = draft.groups.length
      ? '<div class="ag-rows">' + draft.groups.map(function (g) {
          return '<div class="ag-row">' +
            '<div class="ag-row-body"><div class="ag-row-name">' + esc(g.name) + '</div></div>' +
            '<button type="button" class="ag-tglbtn ag-tglbtn--rm" data-mcp-unpick="' + esc(g.id) + '">Remove</button>' +
          '</div>';
        }).join('') + '</div>'
      : '<div class="ag-slot">' +
          '<p class="ag-slot-head">Nobody yet.</p>' +
          '<p class="ag-slot-body">Until you grant a group, this source is registered and unreachable — ' +
          'which is the safe state to save in if you are not sure.</p>' +
        '</div>';
    return rows + '<button type="button" class="ag-addrow" data-mcp-openpick>+ Add groups</button>';
  }

  function toolSummary() {
    if (!draft.introspected) return 'not checked';
    return enabledTools().length + ' of ' + draft.tools.length;
  }

  function progressHtml() {
    if (!convSlots || !convSlots.length) return '';
    var known = convSlots.filter(function (s) { return s.known; }).length;
    var open = convSlots.filter(function (s) { return !s.known; });
    return '<div class="ag-prog">' +
      '<span class="ag-prog-n">' + known + ' of ' + convSlots.length + '</span>' +
      '<span class="ag-prog-t">' + (open.length
        ? 'still to settle: ' + open.map(function (s) { return esc(s.label); }).join(', ')
        : 'nothing missing — ready to save') + '</span>' +
    '</div>';
  }

  function panelHtml() {
    var sec = window.BuilderShell.section;
    return '<div id="mcp-prog-host">' + progressHtml() + '</div>' +
      (saveErr ? '<div class="ag-note ag-note--err">' + esc(saveErr) + '</div>' : '') +
      sec({
        key: 'connection', no: 1, title: 'Connection', note: 'where it lives',
        collapsed: !!collapsed.connection,
        sub: 'How Agnes reaches the server. Paste these from wherever the server is documented — nothing here is guessable.',
        summary: (draft.transport === 'stdio' ? draft.command : draft.url) || 'not set',
        body: connectionBody(),
      }) +
      sec({
        key: 'auth', no: 2, title: 'Authentication', note: 'how it proves who we are',
        collapsed: !!collapsed.auth,
        sub: 'What the connection carries. A token is stored in the vault; the source keeps only the variable name.',
        summary: draft.auth_method ? (draft.auth_method === 'bearer' ? 'bearer' : 'oauth') : 'none',
        body: authBody(),
      }) +
      sec({
        key: 'tools', no: 3, title: 'Tools', note: 'what it exposes',
        collapsed: !!collapsed.tools,
        sub: 'What the server actually offers, read from the server itself. Turn off anything agents should not call.',
        summary: toolSummary(),
        body: toolsBody(),
      }) +
      sec({
        key: 'access', no: 4, title: 'Access', note: 'who may call them',
        collapsed: !!collapsed.access,
        sub: 'Granting a group means every agent its members build can call these tools.',
        summary: draft.groups.length ? draft.groups.length + ' group' + (draft.groups.length === 1 ? '' : 's') : 'nobody',
        body: accessBody(),
      });
  }

  function leftHtml() {
    var rows = conv.length ? conv : (convBusy ? [] : [{ role: 'assistant', text:
      'Connecting a tool server takes four things: where it lives, how it authenticates, ' +
      'which of its tools to expose, and who may call them. Tell me what you are connecting.' }]);
    return window.BuilderShell.engineNotice(convEngine) +
      window.BuilderShell.conversation({
        id: 'mcp-conv', rows: rows, busy: convBusy,
        busyText: conv.length ? 'Thinking…' : 'Getting started…', err: convErr,
      }) +
      window.BuilderShell.composer({
        kind: 'create', value: convDraft, busy: convBusy,
        placeholder: 'Tell me what you are connecting…',
        chips: convBusy ? [] : convChips,
      });
  }

  function render() {
    if (!mount) return;
    mount.innerHTML =
      window.BuilderShell.head({
        backLabel: 'Library', title: draft.name || 'New MCP source', titleId: 'mcp-title',
        badge: '<span class="sk-typechip sk-typechip--plugin">MCP source</span>',
        actionsId: 'mcp-actions',
        actions: '<button type="button" class="cc-btn cc-btn--primary" id="mcp-save"' +
          (canSave() && !saving ? '' : ' disabled') + '>' +
          (saving ? 'Saving…' : 'Register source') + '</button>',
      }) +
      window.BuilderShell.workspace({
        left: leftHtml(),
        cfgTitle: 'Configuration',
        cfgSub: 'everything this source is, editable by hand',
        cfgBodyId: 'mcp-steps',
        cfg: panelHtml(),
      }) +
      '<div id="mcp-picker"></div>';
    renderPicker();
    document.body.classList.add('ag-building');
  }

  /* ── Events ────────────────────────────────────────────────────────── */

  function wire() {
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-mcp-seg],[data-mcp-field],[data-mcp-check],[data-mcp-tool],' +
        '[data-mcp-openpick],[data-mcp-pick],[data-mcp-unpick],[data-ag-pick-close],' +
        '[data-ag-toggle-sec],[data-ag-send],[data-ag-chip],[data-ag-back],#mcp-save');
      if (!t) {
        if (pickerOpen && e.target.hasAttribute && e.target.hasAttribute('data-ag-pick-backdrop')) closePicker();
        return;
      }
      if (t.hasAttribute('data-mcp-seg')) {
        var key = t.getAttribute('data-mcp-seg');
        draft[key] = t.getAttribute('data-mcp-val');
        // Choosing an auth method IS the decision, including choosing none —
        // which is why it is recorded rather than inferred from a blank field.
        if (key === 'auth_method') draft.auth_decided = true;
        // The tool list belongs to the connection it came from.
        if (key === 'transport') { draft.introspected = false; draft.tools = []; }
        render();
      }
      else if (t.hasAttribute('data-mcp-check')) { checkConnection(); }
      else if (t.hasAttribute('data-mcp-tool')) {
        var n = t.getAttribute('data-mcp-tool');
        draft.enabled[n] = draft.enabled[n] === false;
        render();
      }
      else if (t.hasAttribute('data-mcp-openpick')) { openPicker(); }
      else if (t.hasAttribute('data-mcp-pick')) { toggleGroup(t.getAttribute('data-mcp-pick')); }
      else if (t.hasAttribute('data-mcp-unpick')) {
        draft.groups = draft.groups.filter(function (g) { return g.id !== t.getAttribute('data-mcp-unpick'); });
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
      else if (t.hasAttribute('data-ag-send')) {
        var box = mount.querySelector('[data-ag-comp]');
        var text = box ? box.value.trim() : '';
        if (text) sendTurn(text);
      }
      else if (t.hasAttribute('data-ag-chip')) { sendTurn(t.getAttribute('data-ag-chip')); }
      else if (t.hasAttribute('data-ag-back')) { window.location.href = '/library'; }
      else if (t.id === 'mcp-save') { save(); }
    });

    document.addEventListener('input', function (e) {
      var el = e.target;
      if (el.getAttribute && el.getAttribute('data-ag-search') === 'mcp-groups') {
        pickerQuery = el.value; renderPickerRows(); return;
      }
      var f = el.getAttribute && el.getAttribute('data-mcp-field');
      if (f) {
        draft[f] = f === 'args' ? el.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean)
                                : el.value;
        // Typed values change what Save would do, not what the panel looks
        // like — repainting here would take the caret with it.
        var save = document.getElementById('mcp-save');
        if (save) save.disabled = !(canSave() && !saving);
        return;
      }
      var comp = el.getAttribute && el.getAttribute('data-ag-comp');
      if (comp) {
        convDraft = el.value;
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 140) + 'px';
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && pickerOpen) { closePicker(); return; }
      if (!e.target.getAttribute || !e.target.getAttribute('data-ag-comp')) return;
      if (e.key !== 'Enter' || e.shiftKey) return;
      e.preventDefault();
      var text = e.target.value.trim();
      if (text) sendTurn(text);
    });
  }

  window.AgnesMcpBuilder = {
    open: function (opts) {
      mount = opts.mount;
      draft = newDraft();
      wire();
      render();
      if (!opened) { opened = true; sendTurn(''); }
    },
  };
})(window);
