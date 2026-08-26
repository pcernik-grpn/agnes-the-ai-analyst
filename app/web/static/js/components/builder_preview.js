/* =====================================================================
 * builder_preview.js — a real session with the thing you are building.
 *
 * The other half of the builder shell. `builder_shell.js` is pure view
 * functions; this is deliberately NOT — a preview is a session, a socket and
 * a stream of tokens, and pretending otherwise would push that state back
 * into each page to keep separately.
 *
 * Both builders need the identical thing: open a chat session bound to a
 * slug, stream the answer, translate engine failures into something the
 * author can act on. What differs is only WHICH agent it runs as, and that
 * is one callback:
 *
 *   var preview = BuilderPreview({
 *     resolveSlug: function () { return Promise<string>; },
 *     onUpdate:    function () { … repaint the pane … },
 *   });
 *
 * /agents resolves to the agent being edited (saving it first — the agent
 * runs server-side, so it can only answer as a configuration the server
 * has). /skills resolves by pointing a scratch agent at the draft template
 * and returning its slug.
 *
 * `preview.state` is the pane's model: {session, msgs, busy, err, stream,
 * draft, slug}. Read it, render it, never write it — every mutation happens
 * here and is followed by `onUpdate`.
 *
 * Answers are rendered by the CALLER, as text. Neither builder page has an
 * HTML sanitizer and an engine answer is model output; see the note in
 * BuilderShell.message.
 * ===================================================================== */

(function (window) {
  'use strict';

  function blankState() {
    return { session: null, socket: null, msgs: [], busy: false, err: null, stream: '', draft: '', slug: null };
  }

  /* Engine errors arrive as internal kinds (`kai_integration_not_configured`,
     `concurrency_cap`). Pasted verbatim they read as a broken page and send
     the author looking for a mistake in a configuration that is in fact fine
     — what is missing is instance-level plumbing only an admin can supply.
     Say which of the two it is; keep the kind in the console. */
  function errorCopy(raw) {
    var msg = String(raw || '');
    if (/not_configured|no_provider|provider_unavailable|integration/i.test(msg)) {
      return 'Preview needs a chat engine, and none is configured on this instance — ' +
        'an admin sets one up. Your work is saved either way.';
    }
    if (/concurrency_cap/i.test(msg)) {
      return 'Too many sessions are running right now. Try the preview again in a moment.';
    }
    if (/budget|429/i.test(msg)) {
      return 'This agent has used its budget for the month. An admin can raise it.';
    }
    return 'The preview could not answer. The details are in the browser console.';
  }

  function create(opts) {
    var state = blankState();
    var resolveSlug = opts.resolveSlug;
    var onUpdate = opts.onUpdate || function () {};
    var label = opts.label || 'preview';

    function closeSocket() {
      if (state.socket) {
        try { state.socket.close(); } catch (e) { /* already gone */ }
      }
      state.socket = null;
      state.session = null;
      state.slug = null;
    }

    function reset() {
      closeSocket();
      state = blankState();
    }

    /* Open (or reuse) a session, then run `then`. A session is reusable only
       while it is bound to the SAME slug — otherwise the author would be
       talking to the previous draft under the new one's name. */
    function open(then) {
      Promise.resolve()
        .then(function () { return resolveSlug(); })
        .then(function (slug) {
          if (!slug) throw new Error('Nothing to preview yet.');
          if (state.session && state.socket && state.slug === slug) { then(); return null; }
          closeSocket();
          state.err = null; state.busy = true; onUpdate();
          return fetch('/api/chat/sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ surface: 'web', agent_slug: slug }),
          }).then(function (r) {
            if (!r.ok) {
              return r.json().catch(function () { return {}; }).then(function (b) {
                var d = (b && b.detail) || {};
                throw new Error(d.hint || (r.status === 403 || r.status === 401
                  ? 'Chat is not enabled on your account — an admin grants it to your group.'
                  : 'Could not start a preview session (HTTP ' + r.status + ').'));
              });
            }
            return r.json();
          }).then(function (session) {
            state.session = session; state.slug = slug;
            var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
            var ws = new WebSocket(proto + location.host + session.ws_url);
            state.socket = ws;
            ws.onmessage = function (ev) {
              var frame;
              try { frame = JSON.parse(ev.data); } catch (e) { return; }
              if (frame.type === 'token') {
                state.stream += (frame.text || ''); onUpdate();
              } else if (frame.type === 'assistant_message') {
                state.msgs.push({ role: 'assistant', text: frame.content || state.stream || '' });
                state.stream = ''; state.busy = false; onUpdate();
              } else if (frame.type === 'error') {
                state.err = errorCopy(frame.message);
                state.stream = ''; state.busy = false; onUpdate();
              }
            };
            ws.onerror = function () {
              state.err = 'The preview connection dropped.'; state.busy = false; onUpdate();
            };
            ws.onopen = function () { state.busy = false; then(); };
          });
        })
        .catch(function (err) {
          console.error(label + ': preview session failed', err);
          state.err = err.message; state.busy = false; onUpdate();
        });
    }

    /* The turn is only shown once the socket is actually open. Pushing it
       optimistically would leave a message sitting in the transcript that was
       never sent when the session fails to start — and the failure renders
       right below it, which reads as "it answered with an error" rather than
       "it never got there". */
    function send(text) {
      if (!text || state.busy) return;
      open(function () {
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
          state.err = 'The preview is not connected.'; state.busy = false; onUpdate();
          return;
        }
        // Frame shape is the web-chat protocol's, not this module's — see
        // app/api/notifications_ws.py and the chat page's own sender.
        state.socket.send(JSON.stringify({ type: 'user_msg', text: text }));
        state.msgs.push({ role: 'user', text: text });
        state.draft = ''; state.busy = true; state.err = null; state.stream = '';
        onUpdate();
      });
    }

    return {
      get state() { return state; },
      setDraft: function (v) { state.draft = v; },
      open: open,
      send: send,
      reset: reset,
      close: closeSocket,
    };
  }

  window.BuilderPreview = create;
  window.BuilderPreview.errorCopy = errorCopy;
})(window);
