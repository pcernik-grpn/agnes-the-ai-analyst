/* =====================================================================
 * builder_shell.js — the markup half of the builder workspace.
 *
 * Companion to css/builder.css. Together they are the shell a builder page
 * hosts: a conversation on the left that writes a configuration, the
 * configuration itself on the right, and the chrome around both.
 *
 * WHY A VIEW LIBRARY AND NOT A COMPONENT
 * --------------------------------------
 * Every function here is pure: arguments in, HTML string out. Nothing reads
 * page state, nothing touches the DOM, nothing registers a listener. The
 * host page keeps owning its own state and its own event delegation.
 *
 * The alternative — a stateful `new BuilderShell(...)` that owns the
 * transcript, the dirty flag and the open section — was rejected because the
 * things being built are not alike. An agent is one server row with a scope
 * derivation behind it; a skill is a markdown body; a plugin is a zip whose
 * metadata is editable but whose contents are not; a data package writes
 * grants. Forcing one state shape on all four would either bloat the
 * component with per-type branches or push each page into pretending its
 * entity is shaped like an agent's. Sharing the VIEW gets the consistency
 * that was actually wanted — the same shell, the same affordances, the same
 * behaviour on screen — at none of that cost.
 *
 * The corollary: this file cannot enforce anything. If two builders disagree
 * about when work is saved, no rule here will catch it. That contract lives
 * in the pages and in their tests.
 *
 * LOADING
 * -------
 * Synchronously, BEFORE the page's own script — a builder page renders on
 * boot, so a `defer`ed load would leave `BuilderShell` undefined at first
 * paint. Same reason modal.js is loaded the way it is.
 *
 * ESCAPING
 * --------
 * `BuilderShell.esc` is the one escaper. Host pages delegate to it rather
 * than keeping their own copy: two escapers on two pages is two chances for
 * one of them to be weaker, and everything flowing through here — model
 * replies, entity names, ids from a catalogue — is untrusted. Every
 * interpolation below is escaped except the ones documented as pre-built
 * HTML the caller assembled (`body`, `head`, `left`, `right`).
 * ===================================================================== */

(function (window) {
  'use strict';

  var ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ENTITIES[c];
    });
  }

  var CHEVRON =
    '<svg viewBox="0 0 24 24" fill="none"><path d="M5 8.5l7 7 7-7" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  /* ── A numbered, collapsible configuration section ──
     `body` is pre-built HTML — the host renders what the section contains,
     because that is the part only it knows.

     `addKey`, when given, puts a "+" in the HEADER. In the header rather
     than the body so it is reachable while the section is collapsed, which
     is the state most sections are in most of the time. It sits inside a
     header that is itself a toggle; the host's delegated handler must match
     the button before the header, which `closest()` does naturally since the
     button is the inner element.

     The description leads the BODY, not the header: in a narrow pane a
     two-line sub under every collapsed title wrapped to four lines and
     crowded out the summary, and the sentence explaining a section is worth
     reading when you have opened it to act, not while scanning past it. */
  function section(o) {
    var collapsed = !!o.collapsed;
    var add = o.addKey
      ? '<button type="button" class="ag-addbtn" data-ag-add="' + esc(o.addKey) + '" ' +
        'title="Add by hand" aria-label="Add to ' + esc(o.title) + '">+</button>'
      : '';
    return (
      '<div class="ag-sec' + (collapsed ? ' collapsed' : '') + '" data-sec="' + esc(o.key) + '">' +
        '<div class="ag-sec-head" data-ag-toggle-sec="' + esc(o.key) + '" role="button" ' +
          'tabindex="0" aria-expanded="' + !collapsed + '">' +
          '<span class="ag-sec-no">' + esc(o.no) + '</span>' +
          '<div class="ag-sec-grow"><h3>' + esc(o.title) +
            (o.note ? ' <span class="ag-sec-note">' + esc(o.note) + '</span>' : '') +
          '</h3></div>' +
          '<span class="ag-sec-sum">' + esc(o.summary || '') + '</span>' + add +
          '<span class="ag-sec-chev">' + CHEVRON + '</span>' +
        '</div>' +
        '<div class="ag-sec-body">' +
          (o.sub ? '<p class="ag-sec-sub">' + esc(o.sub) + '</p>' : '') +
          (o.body || '') +
        '</div>' +
      '</div>'
    );
  }

  /* Search + live count for a pooled list. The count is a separate node so
     the host can repaint it on keystroke without rebuilding the input and
     losing the caret. */
  function toolbar(o) {
    return (
      '<div class="ag-toolbar">' +
        '<input type="text" class="ag-search" data-ag-search="' + esc(o.key) + '" ' +
          'value="' + esc(o.value || '') + '" placeholder="' + esc(o.placeholder) + '" autocomplete="off">' +
        '<span class="ag-toolcount" data-count="' + esc(o.key) + '">' +
          esc(o.shown) + ' of ' + esc(o.total) +
        '</span>' +
      '</div>'
    );
  }

  /* One turn. Owner and assistant are told apart by alignment and surface,
     never by colour alone. Text only — `white-space: pre-wrap` in the sheet
     keeps the newlines, and a model reply is never trusted as HTML. */
  function message(m) {
    if (m.role === 'user') {
      return '<div class="ag-msg ag-msg--me"><div class="ag-msg-text">' + esc(m.text) + '</div></div>';
    }
    return (
      '<div class="ag-msg' + (m.busy ? ' ag-msg--busy' : '') + '">' +
        '<span class="ag-msg-dot" aria-hidden="true"></span>' +
        '<div class="ag-msg-text">' + esc(m.text) + '</div>' +
      '</div>'
    );
  }

  /* The transcript. `rows` is the whole thing the host wants shown, opening
     line included — this does not prepend one, because what a builder opens
     with is the most page-specific sentence on the screen. */
  function conversation(o) {
    var rows = (o.rows || []).slice();
    if (o.busy) rows.push({ role: 'assistant', text: o.busyText || 'Thinking…', busy: true });
    var html = rows.map(message).join('');
    if (o.err) html += '<div class="ag-conv-err">' + esc(o.err) + '</div>';
    return '<div class="ag-conv" id="' + esc(o.id) + '"><div class="ag-conv-in">' + html + '</div></div>';
  }

  /* An empty transcript's placeholder — centred in the scroll area. `icon`
     is pre-built SVG. */
  function conversationEmpty(o) {
    return (
      '<div class="ag-conv" id="' + esc(o.id) + '"><div class="ag-conv-in">' +
        '<div class="ag-conv-empty">' +
          '<span class="ag-conv-empty-ring">' + (o.icon || '') + '</span>' +
          '<span>' + esc(o.text) + '</span>' +
        '</div>' +
      '</div></div>'
    );
  }

  /* Chips + input. `chips` are suggested replies; they are shown only when
     there is something to suggest and the shell is not mid-turn. */
  function composer(o) {
    var chips = '';
    if (o.chips && o.chips.length) {
      chips = '<div class="ag-chips">' + o.chips.map(function (c) {
        return '<button type="button" class="ag-chip" data-ag-chip="' + esc(c) + '">' + esc(c) + '</button>';
      }).join('') + '</div>';
    }
    var off = o.busy ? ' disabled' : '';
    return (
      '<div class="ag-comp"><div class="ag-comp-in">' + chips +
        '<div class="ag-comp-box">' +
          '<textarea rows="1" data-ag-comp="' + esc(o.kind) + '" ' +
            'placeholder="' + esc(o.placeholder) + '"' + off + '>' + esc(o.value || '') + '</textarea>' +
          '<button type="button" class="ag-send" data-ag-send="' + esc(o.kind) + '"' + off + '>' +
            'Send <span aria-hidden="true">→</span></button>' +
        '</div>' +
      '</div></div>'
    );
  }

  /* The Create | Preview switch. */
  function tabs(o) {
    return (
      '<div class="ag-tabs"><div class="ag-tabs-in" role="tablist">' +
        o.tabs.map(function (t) {
          return '<button type="button" class="ag-tab" role="tab" data-ag-tab="' + esc(t.id) + '" ' +
            'aria-selected="' + (t.id === o.active) + '">' + esc(t.label) + '</button>';
        }).join('') +
      '</div></div>'
    );
  }

  /* The header strip: leave, identity, state, act. `actions` is pre-built —
     which verbs belong there depends on where the thing is in its life, and
     that is the host's call. */
  /* `badge` is pre-built HTML shown between the back link and the title —
     for what the thing IS, when that is fixed for the session rather than
     configurable. /skills puts the entity type there: it is identity, not
     configuration, and a whole panel section spent re-asking a question the
     "+ Add" menu already answered was the least useful card in the most
     valuable slot. Optional, so the agent builder is unaffected. */
  function head(o) {
    return (
      '<div class="ag-build-head">' +
        '<button type="button" class="ag-back" data-ag-back>← ' + esc(o.backLabel) + '</button>' +
        (o.badge ? '<div class="ag-build-badge">' + o.badge + '</div>' : '') +
        '<div style="min-width:0;flex:1">' +
          '<h2 id="' + esc(o.titleId) + '">' + esc(o.title) + '</h2>' +
        '</div>' +
        '<div class="ag-build-actions" id="' + esc(o.actionsId) + '">' + (o.actions || '') + '</div>' +
      '</div>'
    );
  }

  /* The two panes. `left` and `right` are pre-built. Equal width by the
     sheet — the configuration is the source of truth, not a sidebar
     summarising the chat, and it is sized to say so. */
  function workspace(o) {
    return (
      '<div class="ag-work">' +
        '<div class="ag-pane">' + (o.left || '') + '</div>' +
        '<div class="ag-pane ag-pane--cfg">' +
          '<div class="ag-cfg-head"><h3>' + esc(o.cfgTitle) + '</h3>' +
            '<p>' + esc(o.cfgSub) + '</p></div>' +
          '<div class="ag-cfg-body" id="' + esc(o.cfgBodyId) + '">' + (o.cfg || '') + '</div>' +
        '</div>' +
      '</div>'
    );
  }

  /* The pooled-ingredient picker, over the app-wide modal surface rather
     than a private overlay. A detour from building, so it ends by returning
     you where you were. `rows` and `foot` are pre-built. */
  function picker(o) {
    return (
      '<div class="modal-backdrop is-open" data-ag-pick-backdrop>' +
        '<div class="modal-card ag-pick" role="dialog" aria-modal="true" aria-label="' + esc(o.title) + '">' +
          '<div class="ag-pick-head">' +
            '<div><h3>' + esc(o.title) + '</h3><p class="sub">' + esc(o.sub) + '</p></div>' +
            '<button type="button" class="ag-pick-x" data-ag-pick-close aria-label="Close">✕</button>' +
          '</div>' +
          toolbar({ key: o.key, placeholder: o.searchPlaceholder, shown: o.shown, total: o.total, value: o.query }) +
          '<div class="ag-pick-rows ag-rows" data-rows="' + esc(o.key) + '">' + (o.rows || '') + '</div>' +
          '<div class="ag-note">' + (o.foot || '') + '</div>' +
          '<div class="modal-actions">' +
            '<button type="button" class="btn btn-primary" data-ag-pick-close>Done</button>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  /* The engine badge, in the shell rather than per page.

     Every turn endpoint reports which engine answered it (`engine` in the
     response — see app/api/builder_core.py), because a scripted stand-in that
     looks identical to the real thing is a lie the product tells every day
     someone runs it locally. Two of the four builders rendered that and two
     dropped it on the floor, which is the same failure one tier up: the badge
     existed and the instance still could not be trusted to say so. One
     implementation, so a builder cannot forget. */
  function engineNotice(engine) {
    if (engine !== 'stub') return '';
    return (
      '<div class="ag-note ag-note--warn">Scripted stand-in — this instance has no AI ' +
      'credential configured (or <code>AGNES_BUILDER_STUB</code> is set), so the replies are ' +
      'canned. The panel and Save work normally.</div>'
    );
  }

  window.BuilderShell = {
    esc: esc,
    engineNotice: engineNotice,
    section: section,
    toolbar: toolbar,
    message: message,
    conversation: conversation,
    conversationEmpty: conversationEmpty,
    composer: composer,
    tabs: tabs,
    head: head,
    workspace: workspace,
    picker: picker,
  };
})(window);
