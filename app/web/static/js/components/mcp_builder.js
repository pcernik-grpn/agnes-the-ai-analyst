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
  var convEngine = null, opened = false;
  var convPatchNote = null;    // what the last reply wrote, and what it left alone
  var llmUnavailable = false;  // latched when a turn says this instance has no model

  /* This builder held EVERYTHING in module scope: a pasted endpoint, the auth
     env-var name, the introspected tool list and its curation, the group
     picks. A reload or a stray Back discarded all of it with no warning —
     /agents guards with beforeunload because its drafts are server rows,
     /skills persists per type. This does what /skills does. */
  var DRAFT_KEY = 'agnes_mcp_builder_draft_v1';
  function persistDraft() {
    if (!draft) return;
    try {
      var keep = JSON.parse(JSON.stringify(draft));
      delete keep.secret_value;   // a credential does not belong in localStorage
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ draft: keep, conv: conv }));
    } catch (e) { /* quota or private mode — soft-fail, same as /skills */ }
  }
  function restoreDraft() {
    try {
      var raw = window.localStorage.getItem(DRAFT_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      return parsed && parsed.draft ? parsed : null;
    } catch (e) { return null; }
  }
  function discardDraft() {
    try { window.localStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ }
  }

  /* What a turn changed, in the admin's vocabulary — the reply's prose is the
     model's account of itself; this is the page's. */
  var FIELD_WORDS = {
    name: 'the name', url: 'the endpoint', command: 'the command', args: 'the arguments',
    transport: 'the transport', auth_method: 'the auth method',
    auth_secret_env: 'the credential variable', scope: 'the scope',
  };
  function fieldWords(keys) {
    var w = keys.map(function (k) { return FIELD_WORDS[k] || k; });
    if (w.length < 2) return w[0] || '';
    return w.slice(0, -1).join(', ') + ' and ' + w[w.length - 1];
  }
  function patchNote(applied, kept) {
    if (!applied.length && !kept.length) return 'Nothing changed in the configuration.';
    var parts = [];
    if (applied.length) parts.push('Wrote ' + fieldWords(applied) + '.');
    if (kept.length) parts.push('Left ' + fieldWords(kept) + ' as you had ' + (kept.length > 1 ? 'them' : 'it') + '.');
    return parts.join(' ');
  }
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
        var msg = typeof d === 'string' ? d : (d && (d.message || d.hint || d.kind)) || ('HTTP ' + r.status);
        var e = new Error(msg);
        e.status = r.status;
        // The structured half, kept: a caller has to be able to tell WHICH
        // 409 it got. `grantGroup` reads `error` to separate "already granted"
        // from "there is nothing to grant" — swallowing both as done is how a
        // Save reported success over an access change that never happened.
        e.detail = d;
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

  /* A stable host, so a landed turn or a keystroke can repaint the progress
     line alone — re-rendering the panel would rebuild the form under whatever
     field has focus. */
  function syncProgress() {
    var host = document.getElementById('mcp-prog-host');
    if (host) host.innerHTML = progressHtml();
  }

  /* The interview's slots, as the PANEL can evaluate them.
     `convSlots` — the server's answer — arrived only on a turn, and the input
     handler deliberately skips repainting to protect the caret, so filling the
     form by hand left the line reading "0 of 4 · still to settle" beside a lit
     Register source. These mirror `_SLOTS` in app/api/mcp_builder.py, including
     `_endpoint_known` (transport decides which address counts) and
     `_auth_settled` ("decided: none" is a real answer, so the draft carries
     `auth_decided` rather than inferring it from a blank field).
     tests/test_mcp_builder_progress_mirror.py fails if the two drift. */
  function localSlots() {
    var endpoint = draft.transport === 'stdio'
      ? !!(draft.command || '').trim()
      : !!(draft.url || '').trim();
    var auth = !!draft.auth_decided &&
      (draft.auth_method === 'bearer' ? !!(draft.auth_secret_env || '').trim() : true);
    return [
      { key: 'endpoint', label: 'where it lives', known: endpoint },
      { key: 'auth', label: 'how it authenticates', known: auth },
      { key: 'name', label: 'a name', known: !!(draft.name || '').trim() },
      { key: 'tools', label: 'which tools to expose', known: !!draft.introspected },
    ];
  }

  /* ── The conversation ──────────────────────────────────────────────── */

  /* Mirrors the caps the endpoint enforces (app/api/builder_core.py). The
     transcript is replayed on every turn, so one over-long reply used to make
     every later turn 422 — a conversation with no way out. */

  function sendTurn(text) {
    if (convBusy) return;
    var opening = !text && !conv.length;
    if (!text && !opening) return;
    if (!opening) conv = conv.concat([{ role: 'user', text: text }]);
    convBusy = true; convErr = null; convDraft = ''; convChips = [];
    /* The panel as it stood when this turn was dispatched: a field the admin
       edits while it is in flight belongs to them, not to the reply. */
    var sentDraft = JSON.parse(JSON.stringify(draft));
    render();
    /* Through the shell — one implementation of the caps, the deadline and
       the typed failure, shared with every other builder. */
    BuilderShell.turn({
      url: TURN_API,
      message: text || '',
      body: {
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
      },
    }).then(function (body) {
      conv = BuilderShell.trimHistory(
        conv.concat([{ role: 'assistant', text: BuilderShell.clipMsg(body.reply || '') }]));
      // An opening turn's suggestions are invented — the author has said
      // nothing for them to be grounded in.
      convChips = (!opening && body.suggestions && body.suggestions.length) ? body.suggestions : [];
      convEngine = body.engine || null;
      /* Applying a patch used to be a blind merge followed by a full repaint,
         so a reply landing while the author typed took their caret and, if it
         touched that field, their text. */
      var patch = body.patch || {};
      var applied = [], kept = [];
      Object.keys(patch).forEach(function (k) {
        var el = document.querySelector('[data-mcp-field="' + k + '"]');
        var mine = (el && document.activeElement === el) ||
          JSON.stringify(draft[k] === undefined ? null : draft[k]) !==
          JSON.stringify(sentDraft[k] === undefined ? null : sentDraft[k]);
        if (mine) { kept.push(k); return; }
        draft[k] = patch[k];
        applied.push(k);
      });
      convPatchNote = opening ? null : patchNote(applied, kept);
      persistDraft();
      syncProgress();
    }).catch(function (e) {
      if (e.kind === 'llm_unavailable') llmUnavailable = true;
      convErr = e.message || 'The assistant could not answer.';
      // Hand the message back — it was cleared optimistically and, on a
      // failure, existed nowhere the author could retrieve it.
      if (text) {
        convDraft = text;
        if (conv.length && conv[conv.length - 1].role === 'user') conv = conv.slice(0, -1);
      }
    }).then(function () {
      // The shell clears its own timer.
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
            // `readOnlyHint` as the server gave it: true, false, or absent.
            // Absent stays absent — it is not a claim of safety.
            read_only: (t && typeof t.read_only === 'boolean') ? t.read_only : null,
          };
        });
        draft.enabled = {};
        draft.tools.forEach(function (t) { draft.enabled[t.name] = true; });
        draft.introspected = true;
        // Nothing to call it yet? The host is the honest first guess, and the
        // admin is standing right here to correct it.
        if (!nameOk(draft.name) && draft.url) {
          // Through the identifier rule — the raw host ("mcp.example.com") is
          // exactly the value the API refuses.
          draft.name = toIdentifier(String(draft.url).replace(/^https?:\/\//, '').split('/')[0]);
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

  /* 409 from the source-wide grant is not one thing.
     `no_tools_registered` means the source has no ENABLED tool row to grant,
     so nothing was granted and the group has no access — a failure, and the
     one this builder can actually cause: turn every tool off, pick a group,
     Save. A blanket "409 means already granted" reported that as success, and
     re-enabling the tools later does not go back and grant anyone.
     (Already-granted is not a 409 on this endpoint at all — it answers 200
     with an `already` count. The swallow stays narrowed rather than deleted so
     an endpoint that later adds one does not break the resumable Save.) */
  function grantGroup(sourceId, group) {
    return postJson(SOURCES_API + '/' + encodeURIComponent(sourceId) + '/grants', { group_id: group.id })
      .catch(function (e) {
        var kind = e && e.detail && e.detail.error;
        if (e && e.status === 409 && kind !== 'no_tools_registered') return null;  // already granted
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
      /* The server's own `readOnlyHint`, mapped onto `mutating`. Sending
         nothing meant every tool registered as non-mutating and the
         group-grant handed out write tools — the sibling registration path
         has always honoured this. An UNANNOTATED tool counts as mutating:
         "the server said nothing" is not "this is safe". */
      mutating: tool.read_only !== true,
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
        /* Clear the draft on the same tick it commits, BEFORE the redirect.
           Persisting without this meant the next "+ Add → Connect an MCP
           source" resumed a source that was already registered — endpoint and
           tool curation prefilled — and Save then attempted a duplicate
           registration. A builder that keeps a draft owns discarding it. */
        discardDraft();
        window.location.href = '/admin/mcp-sources/' + encodeURIComponent(savedId);
      })
      .catch(function (e) {
        saveErr = (e.message || 'Could not register the source.') +
                  (savedId ? ' The source is registered — press Save again to finish the rest.' : '');
        saving = false;
        render();
      });
  }

  /* Mirrors `is_safe_identifier` in src/sql_safe.py — the source name becomes
     a DuckDB identifier, so the API refuses anything else. The page did not
     know that: the placeholder read "Acme CRM", the post-check auto-fill used
     the URL host, and both are refused AFTER the click, in the sync engine's
     vocabulary. tests/test_mcp_builder_name_rule.py keeps the two in step. */
  var NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
  function nameOk(n) { return NAME_RE.test((n || '').trim()); }
  /* Turn anything into a name the API will take: lowercase, non-alphanumerics
     to underscores, digits pushed off the front. */
  function toIdentifier(raw) {
    var v = String(raw || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    if (!v) return '';
    if (/^[0-9]/.test(v)) v = 'mcp_' + v;
    return v.slice(0, 64);
  }

  function canSave() {
    if (!nameOk(draft.name)) return false;
    if (!draft.introspected) return false;   // a source with no tools exposes nothing
    return draft.transport === 'stdio' ? !!draft.command.trim() : !!draft.url.trim();
  }
  /* What Register source is waiting for, from the same predicates that
     disable it — so the button and its explanation cannot name different
     things. The button rendered `disabled` from first paint with nothing
     saying why. */
  function saveBlocker() {
    if (!draft.name.trim()) return 'Add a name first — letters, digits and underscores.';
    if (!nameOk(draft.name)) {
      return 'The name becomes a database identifier: letters, digits and underscores only, ' +
        'starting with a letter. Try "' + (toIdentifier(draft.name) || 'acme_crm') + '".';
    }
    if (draft.transport === 'stdio') {
      if (!draft.command.trim()) return 'Add the command to run first.';
    } else if (!draft.url.trim()) {
      return 'Add the endpoint URL first.';
    }
    /* Registering before checking produced a source with no tools: enabled,
       granted, and unable to answer anything. */
    if (!draft.introspected) {
      return 'Check the connection first — until Agnes has read the tool list there is nothing to register.';
    }
    return '';
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
      field('Name', 'name', 'acme_crm', 'what it is called in the source list');
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
      // The whole safety decision used to rest on an admin eyeballing an
      // unmarked list. Say which ones can change data upstream.
      var writes = t.read_only !== true
        ? ' <span class="mcp-writes" title="This tool can change data on the server. Unmarked tools count as writes.">writes</span>'
        : '';
      return '<div class="ag-row">' +
        '<div class="ag-row-body">' +
          '<div class="ag-row-name">' + esc(t.name) + writes + '</div>' +
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
            '<div class="ag-row-body"><div class="ag-row-name">' + (window.AgnesKindGlyph ? window.AgnesKindGlyph.groupGlyph() : '') + esc(g.name) + '</div></div>' +
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
    var slots = localSlots();
    if (!slots.length) return '';
    var known = slots.filter(function (s) { return s.known; }).length;
    var open = slots.filter(function (s) { return !s.known; });
    return '<div class="ag-prog">' +
      '<span class="ag-prog-n">' + known + ' of ' + slots.length + '</span>' +
      '<span class="ag-prog-t">' + (open.length
        ? 'still to settle: ' + open.map(function (s) { return esc(s.label); }).join(', ')
        : 'nothing missing — ready to save') + '</span>' +
    '</div>';
  }

  function panelHtml() {
    var sec = window.BuilderShell.section;
    return (saveErr ? '<div class="ag-note ag-note--err">' + esc(saveErr) + '</div>' : '') +
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
        sub: 'What the server actually offers, read from the server itself. Tools marked "writes" can change data ' +
             'upstream — a tool the server does not vouch for counts as one. Turn off anything agents should not call.',
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
    /* With no model configured the page used to render a greeting, a red
       error under it, and a live composer + chips — every one of which failed
       identically. One standing notice instead. */
    var notice = llmUnavailable
      ? '<div class="ag-note ag-note--warn">No AI credential is configured on this instance, so the ' +
        'assistant cannot draft anything. The configuration on the right is editable by hand and ' +
        'Register source works normally — or ask an admin to set a model up.</div>'
      : window.BuilderShell.engineNotice(convEngine);
    return notice +
      window.BuilderShell.conversation({
        id: 'mcp-conv', rows: llmUnavailable && !conv.length ? [] : rows, busy: convBusy,
        busyText: conv.length ? 'Thinking…' : 'Getting started…',
        err: llmUnavailable ? null : convErr,
      }) +
      (convPatchNote && !convBusy ? '<p class="sk-patch-note">' + esc(convPatchNote) + '</p>' : '') +
      window.BuilderShell.composer({
        kind: 'create', value: convDraft, busy: convBusy,
        // readOnly, not disabled: a disabled textarea is unselectable in
        // Chrome, so a restored message would be visible and uncopyable.
        readOnly: llmUnavailable,
        placeholder: llmUnavailable
          ? 'No AI is configured here — fill the configuration on the right by hand.'
          : 'Tell me what you are connecting…',
        chips: convBusy || llmUnavailable ? [] : convChips,
      });
  }

  function render() {
    if (!mount) return;
    mount.innerHTML =
      window.BuilderShell.head({
        backLabel: 'Library', title: draft.name || 'New MCP source', titleId: 'mcp-title',
        actionsId: 'mcp-actions',
        actions: '<button type="button" class="cc-btn cc-btn--primary" id="mcp-save"' +
          (canSave() && !saving ? '' : ' disabled title="' + esc(saveBlocker() || 'Saving…') + '"') + '>' +
          (saving ? 'Registering…' : 'Register source') + '</button>',
      }) +
      window.BuilderShell.workspace({
        left: leftHtml(),
        cfgTitle: 'Configuration',
        cfgSub: 'everything this source is, editable by hand',
        /* The durable definition. The only orientation was inside the
           conversation — which disappears entirely on an instance with no
           model, leaving a title, a warning, and no statement of what an MCP
           source IS or what registering one does to this instance. */
        cfgAside: '<div id="mcp-prog-host">' + progressHtml() + '</div>' +
          '<p class="ag-cfg-blurb">An MCP source is an outside tool server — a CRM, a ticket tracker, a docs ' +
          'search — that Agnes dials on your behalf. Register one and the tools you approve become callable by ' +
          'agents, and by analysts in Claude Code, for the groups you grant.</p>',
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
        // like — repainting here would take the caret with it. These two
        // hosts are the exceptions: they report the form, so they have to
        // answer to typing, and neither contains an input.
        var save = document.getElementById('mcp-save');
        if (save) {
          save.disabled = !(canSave() && !saving);
          var why = saveBlocker();
          if (why) save.setAttribute('title', why); else save.removeAttribute('title');
        }
        persistDraft();
        syncProgress();
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
      var saved = restoreDraft();
      draft = saved ? Object.assign(newDraft(), saved.draft) : newDraft();
      conv = (saved && Array.isArray(saved.conv)) ? saved.conv : [];
      wire();
      render();
      /* Greet only a blank page. A resumed draft already answers "what are you
         connecting", and the opening turn's own prompt says the author has not
         said anything yet. */
      if (!opened && !conv.length && !draft.name.trim() && !draft.url.trim() && !draft.command.trim()) {
        opened = true;
        sendTurn('');
      }
    },
  };
})(window);
