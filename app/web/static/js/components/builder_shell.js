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
  /* `after` is pre-built markup that rides the SAME row as the search box —
     a Filter button belongs beside the field it refines, not on a line of its
     own under it. It also has to be a flex ITEM of this row: the shared
     `.fbar-filter` is `position: relative` and its menu is pinned `right: 0`
     against it, so as a block-level element in a wrapper of its own it spanned
     the full width and threw the menu to the far edge of the modal. */
  function toolbar(o) {
    return (
      '<div class="ag-toolbar">' +
        '<input type="text" class="ag-search" data-ag-search="' + esc(o.key) + '" ' +
          'value="' + esc(o.value || '') + '" placeholder="' + esc(o.placeholder) + '" autocomplete="off">' +
        (o.after || '') +
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

  /* What the thing you are building IS — one short paragraph at the head of
     the transcript, above the opening line.

     One component, one position, every builder. It had been none of those:
     /skills wore it as a full-bleed bordered band across the workspace, the
     MCP builder as a fourth line of the configuration header, the package
     builder as a bare paragraph in the conversation, and /agents not at all.
     Three looks for one sentence is how a product stops reading as one
     product.

     The transcript is the right home. It is where a reader looks first, the
     sentence answers the same question the opening line goes on to act on,
     and it scrolls away with the conversation instead of sitting over every
     field forever — which is what a definition should do once it has been
     read. It renders UNDER the opening line (see `conversation`), not over
     it: the assistant's first message is what the reader came to act on.
     `iconSvg` is pre-built and should come from the canonical set
     (macros/_icon.html, or the skill/plugin/agent glyphs the Library and the
     builders already share); `accent` tints the tile to the entity kind
     (--ds-kind-*), defaulting to the assistant accent. */
  /* `check-circle` from the canonical set (macros/_icon.html). Exported so the
     progress lines on /skills and the MCP builder mark "settled" with the same
     glyph the rest of the UI marks it with. */
  var TICK_SVG = '<span class="ag-prog-tick" aria-hidden="true">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-4.5"/></svg></span>';

  function about(o) {
    if (!o || !o.html) return '';
    var tile = o.iconSvg
      ? '<span class="ag-about-ico' + (o.accent ? ' ag-about-ico--' + esc(o.accent) : '') +
        '" aria-hidden="true">' + o.iconSvg + '</span>'
      : '';
    return '<div class="ag-about">' + tile + '<div class="ag-about-body">' + o.html + '</div></div>';
  }

  /* The transcript. `rows` is the whole thing the host wants shown, opening
     line included — this does not prepend one, because what a builder opens
     with is the most page-specific sentence on the screen. `about` is
     pre-built (see above) and sits after the FIRST message — under the
     opening line, not above it — falling back to the head of an empty
     transcript, which is the no-model case where there is no opening line to
     sit under. */
  function conversation(o) {
    var rows = (o.rows || []).slice();
    if (o.busy) rows.push({ role: 'assistant', text: o.busyText || 'Thinking…', busy: true });
    var msgs = rows.map(message);
    var html = o.about
      ? (msgs.length ? msgs[0] + o.about + msgs.slice(1).join('') : o.about)
      : msgs.join('');
    if (o.err) html += '<div class="ag-conv-err">' + esc(o.err) + '</div>';
    return '<div class="ag-conv" id="' + esc(o.id) + '"><div class="ag-conv-in">' + html + '</div></div>';
  }

  /* Run a full re-render without throwing away where the reader was in the
     configuration column.

     Every builder rebuilds its whole view for changes a partial render cannot
     express — picking a tone, switching a mode — and that replaces the
     `.ag-cfg-body` node, so its scrollTop resets to 0. Clicking a tone chip
     two thirds of the way down the panel threw you back to the top, which is
     the sort of thing that makes a form feel like it is fighting you.
     Section collapse already avoids this by mutating in place; this covers
     everything that genuinely has to re-render. */
  function keepCfgScroll(id, write) {
    var before = document.getElementById(id);
    var top = before ? before.scrollTop : 0;
    write();
    if (!top) return;
    var after = document.getElementById(id);
    if (after) after.scrollTop = top;
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
    /* `readOnly` is the "you cannot send, but this is still your text" state:
       a DISABLED textarea is not selectable in Chrome, so a message restored
       into one after a failure is visible and impossible to copy back out.
       Send is disabled either way. */
    var ro = o.readOnly && !o.busy;
    return (
      '<div class="ag-comp"><div class="ag-comp-in">' + chips +
        '<div class="ag-comp-box">' +
          '<textarea rows="1" data-ag-comp="' + esc(o.kind) + '" ' +
            'placeholder="' + esc(o.placeholder) + '"' + off + (ro ? ' readonly' : '') + '>' +
            esc(o.value || '') + '</textarea>' +
          /* The chat's own send control: a circular icon button carrying the
             up-arrow glyph, with the name on `aria-label` rather than in
             visible text (chat.html → .cloud-chat-send-btn). A labelled
             "Send →" pill next to a round arrow on the neighbouring page is
             two send buttons in one product. */
          '<button type="button" class="ag-send" data-ag-send="' + esc(o.kind) + '"' +
            (off || (ro ? ' disabled' : '')) + ' aria-label="Send">' +
            '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
            '<path d="M12 19V5M6 11l6-6 6 6" stroke="currentColor" stroke-width="2.2" ' +
            'stroke-linecap="round" stroke-linejoin="round"/></svg></button>' +
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
    /* `titleHtml` REPLACES the heading block, rather than filling the <h2> —
       /skills hangs a menu off its title, and the <h2> clips its own overflow
       to ellipsise a long name, which swallowed the popup. A host that wants
       a control there owns the whole block; pages that pass only `title` get
       the plain heading. */
    var titleBlock = o.titleHtml || '<h2 id="' + esc(o.titleId) + '">' + esc(o.title) + '</h2>';
    return (
      '<div class="ag-build-head">' +
        '<button type="button" class="ag-back" data-ag-back>← ' + esc(o.backLabel) + '</button>' +
        (o.badge ? '<div class="ag-build-badge">' + o.badge + '</div>' : '') +
        '<div style="min-width:0;flex:1">' + titleBlock + '</div>' +
        '<div class="ag-build-actions" id="' + esc(o.actionsId) + '">' + (o.actions || '') + '</div>' +
      '</div>' +
      /* One alert region, directly under the header and above the workspace.
         Refusals landed in three different places across the builders — one of
         them the bottom of a scrolling form, while the button that caused it
         had been moved to the header. Pre-built HTML, host-owned; a page that
         passes no `alertsId` renders nothing. */
      (o.alertsId ? '<div class="ag-alerts" id="' + esc(o.alertsId) + '">' + (o.alerts || '') + '</div>' : '')
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
          /* `cfgAside` is a second line under the title — for the one thing a
             builder has to say ABOUT the configuration rather than in it:
             how much of it is still open. It rode inside the body as a boxed
             card, which made the first thing in the column a status widget
             instead of the first field. Optional and pre-built; a pane that
             passes none renders exactly as before. */
          '<div class="ag-cfg-head">' +
            '<div class="ag-cfg-head-main"><h3>' + esc(o.cfgTitle) + '</h3>' +
              '<p>' + esc(o.cfgSub) + '</p></div>' +
            (o.cfgAside || '') +
          '</div>' +
          '<div class="ag-cfg-body" id="' + esc(o.cfgBodyId) + '">' + (o.cfg || '') + '</div>' +
        '</div>' +
      '</div>'
    );
  }

  /* The pooled-ingredient picker, over the app-wide modal surface rather
     than a private overlay. A detour from building, so it ends by returning
     you where you were. `rows` and `foot` are pre-built.

     `controls` is an OPTIONAL pre-built strip under the search box, for a
     picker whose pool is too big to work with a substring match alone — the
     package builder slices ~500 registered tables by source, by query mode
     and by "in no package yet". A caller that passes none renders exactly as
     before, so the other pickers are untouched. */
  function picker(o) {
    return (
      '<div class="modal-backdrop is-open" data-ag-pick-backdrop>' +
        /* `ag-pick--fill` fixes the card's height so FILTERING cannot resize
           it. Without it the card sizes to its rows: narrowing 22 results to
           2 collapsed the dialog by several hundred pixels, moving the search
           box, the chips and Done out from under the pointer that was still
           using them. Only for a pool big enough to overflow the card in the
           first place — a five-row picker held at 86vh would be mostly empty
           space, which is the same mistake in the other direction. */
        '<div class="modal-card ag-pick' + ((o.total || 0) > 8 ? ' ag-pick--fill' : '') +
          '" role="dialog" aria-modal="true" aria-label="' + esc(o.title) + '">' +
          '<div class="ag-pick-head">' +
            '<div><h3>' + esc(o.title) + '</h3><p class="sub">' + esc(o.sub) + '</p></div>' +
            '<button type="button" class="ag-pick-x" data-ag-pick-close aria-label="Close">✕</button>' +
          '</div>' +
          toolbar({
            key: o.key, placeholder: o.searchPlaceholder, shown: o.shown, total: o.total,
            value: o.query, after: o.toolbarExtra,
          }) +
          (o.controls || '') +
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
      '<div class="ag-note ag-note--warn">Scripted stand-in — no AI credential is configured (or ' +
      '<code>AGNES_BUILDER_STUB</code> is set), so the replies are canned. The panel and Save ' +
      'work normally.</div>'
    );
  }

  /* ── One turn, for every builder that has a conversation ──────────────
     The shell said it "cannot enforce anything… that contract lives in the
     pages and in their tests", and four pages then proved it: two capped the
     message and trimmed the transcript and two did not, two restored a failed
     message and two lost it, two latched a no-model state and two kept
     inviting. Every one of those is POLICY, not markup, and none of it needs
     the DOM — so it can live here without the shell growing state.

     This function still reads no page state and touches no DOM. It takes the
     turn, applies the caps the endpoint enforces, and either resolves with the
     body or rejects with an error already carrying `kind` and a sentence the
     page can show. What to DO about each kind — restore the composer, latch a
     banner, pop the optimistic row — stays with the page, because that is
     state.

     `MAX_*` mirror app/api/builder_core.py. The mirror tests pin them. */
  var MAX_MSG_CHARS = 4000;
  var MAX_HISTORY = 40;
  var TURN_TIMEOUT_MS = 60000;

  function clipMsg(t) {
    t = t || '';
    return t.length > MAX_MSG_CHARS ? t.slice(0, MAX_MSG_CHARS) : t;
  }

  /* Trim a transcript to what the server will actually replay. A page that
     uploads everything while the prompt reads the last N leaves the model
     silently missing the start of a conversation still on screen — which
     reads as the assistant being careless rather than as a limit. */
  function trimHistory(rows) {
    var list = rows || [];
    return list.length > MAX_HISTORY ? list.slice(-MAX_HISTORY) : list;
  }

  function turnError(kind, message) {
    var e = new Error(message);
    e.kind = kind;
    return e;
  }

  function turn(o) {
    var text = o.message || '';
    /* Refused HERE, with the author's text still in their hands. Sending it
       earned a validation error whose `detail` is an ARRAY, so `hint`/`kind`
       were both undefined and the page blamed the assistant for a paste. */
    if (text.length > MAX_MSG_CHARS) {
      return Promise.reject(turnError(
        'too_long',
        'That message is ' + text.length + ' characters and the limit is ' + MAX_MSG_CHARS +
        '. Shorten it, or put the long part in the configuration on the right.'
      ));
    }

    var ctl = window.AbortController ? new window.AbortController() : null;
    var timedOut = false;
    var ms = o.timeoutMs || TURN_TIMEOUT_MS;
    var timer = window.setTimeout(function () { timedOut = true; if (ctl) ctl.abort(); }, ms);

    var body = o.body || {};
    if (body.history) body.history = trimHistory(body.history);

    return window.fetch(o.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: ctl ? ctl.signal : undefined,
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (payload) {
        if (r.ok) return payload;
        var d = (payload && payload.detail) || {};
        if (r.status === 422 || Array.isArray(d)) {
          throw turnError('too_long',
            'This conversation carries more text than one turn can send. Shorten your last ' +
            'message, or start a fresh one — the configuration keeps your work.');
        }
        if (d.kind === 'builder_llm_unavailable') {
          throw turnError('llm_unavailable', d.hint || 'No AI credential is configured on this instance.');
        }
        throw turnError(d.kind || 'http',
          d.hint || d.message || d.kind || ('The assistant could not answer (HTTP ' + r.status + ').'));
      });
    }).catch(function (e) {
      if (timedOut) {
        throw turnError('timeout',
          'That turn took longer than ' + Math.round(ms / 1000) + ' seconds and was given up on. Try again.');
      }
      throw (e && e.kind) ? e : turnError('http', (e && e.message) || 'The assistant could not answer.');
    }).then(function (payload) {
      window.clearTimeout(timer);
      return payload;
    }, function (e) {
      window.clearTimeout(timer);
      throw e;
    });
  }

  /* The standing notice a builder shows once it knows this instance has no
     model — in place of a greeting, a red error under it, and a live composer
     that all fail the same way. */
  function noModelNotice(what) {
    return (
      '<div class="ag-note ag-note--warn">No AI credential is configured, so the assistant cannot ' +
      'draft anything. Fill the ' + esc(what || 'configuration on the right') +
      ' by hand — saving works normally.</div>'
    );
  }

  /* A primary action that says what it is waiting for. `blocked` is a sentence
     or empty; when set the button is disabled and carries it as its title and
     accessible name, so a dark button and its explanation cannot drift apart. */
  function action(o) {
    var blocked = o.blocked || '';
    var busy = !!o.busy;
    var attrs = ' id="' + esc(o.id) + '"';
    if (busy || blocked) attrs += ' disabled';
    if (blocked && !busy) {
      attrs += ' title="' + esc(blocked) + '" aria-label="' + esc(o.label + ' — ' + blocked) + '"';
    }
    return '<button type="button" class="cc-btn' + (o.primary ? ' cc-btn--primary' : '') + '"' + attrs + '>' +
      esc(busy && o.busyLabel ? o.busyLabel : o.label) + '</button>';
  }

  /* A picker row is the target, not the button sitting on it.

     Every builder renders `.ag-row` with a single toggle on the right, and
     every one of them made you hit that button to add a row — a needless act
     of precision when the pool is two hundred entries long. One delegated
     listener here forwards a click anywhere on the row to that toggle, so the
     behaviour arrives on all four builders at once and cannot drift between
     them; each page keeps its own `data-*` handler and learns nothing new.

     Clicks that land on a real control (the toggle itself, a link in the
     description) are left alone — forwarding those would double-fire. */
  document.addEventListener('click', function (e) {
    var row = e.target.closest && e.target.closest('.ag-pick-rows .ag-row');
    if (!row) return;
    if (e.target.closest('button, a, input, select, textarea, label')) return;
    var toggle = row.querySelector('button[data-ag-kn], button[data-ag-cap], button[data-sk-group], ' +
      'button[data-sk-comp], button[data-pdw-pick], button[data-pdw-unpick], button[data-ag-tgl], ' +
      '.ag-tglbtn');
    if (toggle && !toggle.disabled) toggle.click();
  });

  window.BuilderShell = {
    esc: esc,
    engineNotice: engineNotice,
    noModelNotice: noModelNotice,
    action: action,
    turn: turn,
    clipMsg: clipMsg,
    trimHistory: trimHistory,
    MAX_MSG_CHARS: MAX_MSG_CHARS,
    MAX_HISTORY: MAX_HISTORY,
    section: section,
    toolbar: toolbar,
    message: message,
    about: about,
    TICK_SVG: TICK_SVG,
    keepCfgScroll: keepCfgScroll,
    conversation: conversation,
    conversationEmpty: conversationEmpty,
    composer: composer,
    tabs: tabs,
    head: head,
    workspace: workspace,
    picker: picker,
  };
})(window);
