/* =====================================================================
 * modal.js — app-wide replacements for the native browser dialogs
 * (#497 §1). Exposes three promise-based globals that render the
 * design-system modal (.modal-backdrop > .modal-card) instead of the
 * unstyleable, event-loop-blocking confirm() / alert() / prompt():
 *
 *   await confirmModal('Delete X?')            -> Promise<boolean>
 *   await alertModal('Save failed: ' + msg)    -> Promise<void>
 *   await promptModal('Connection string:')    -> Promise<string|null>
 *
 * Each accepts either a plain string (the message) or an options object:
 *   { title, message, confirmText, cancelText, okText, danger,
 *     defaultValue, placeholder, inputType }
 * `inputType` (promptModal only, default 'text') is passed straight to the
 * rendered <input type>— pass 'password' to mask a secret being entered
 * (e.g. a DB connection string containing a password).
 * When `title` is given it becomes the heading and `message` the muted
 * sub-line; with only a string the message is the heading.
 *
 * Structural CSS lives in style-custom.css (.modal-backdrop/.modal-card/
 * .modal-actions). Buttons use the canonical .btn/.btn-* variants, same
 * as the ds.button macro. Loaded synchronously near the top of
 * _app_scripts.html so these are defined before any page script runs.
 *
 * The global Escape handler in _app_scripts.html targets .modal-overlay/
 * [id$="Modal"]/.modal.is-open and so does NOT match .modal-backdrop —
 * this helper owns its own Esc / backdrop-click / focus handling and
 * resolves the promise accordingly (Esc & backdrop = cancel).
 * ===================================================================== */
(function () {
  'use strict';

  // Topmost-only Esc: only the last-opened modal reacts, so a modal
  // opened from within another (rare) closes inner-first.
  const _stack = [];

  function _coerce(arg) {
    return (typeof arg === 'string' || typeof arg === 'number')
      ? { message: String(arg) }
      : (arg || {});
  }

  function _btn(label, variant) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-' + variant;
    b.textContent = label;
    return b;
  }

  // Builds the backdrop/card scaffold shared by all three dialogs and
  // returns the pieces the callers wire up. `settle` is the single exit
  // point: tears down DOM + listeners, restores focus, resolves once.
  function _build(opts, resolve) {
    const heading = opts.title || opts.message || '';
    const sub = opts.title ? opts.message : '';

    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    // Opt out of the global Escape handler — we handle Esc ourselves.
    backdrop.dataset.noEscClose = '1';

    const card = document.createElement('div');
    card.className = 'modal-card';
    // These three dialogs are the reason `white-space: pre-wrap` exists on
    // modal copy: a caller passes a message with real `\n` in it (a bulleted
    // error list, say) and expects the breaks to survive, the way the native
    // alert() they replace did. Static modal copy in templates wants the
    // opposite — its newlines are source formatting — so the rule is opt-in
    // and this is the opt-in. See style-custom.css `[data-preserve-newlines]`.
    card.dataset.preserveNewlines = '1';

    if (heading) {
      const h = document.createElement('h3');
      h.textContent = heading;
      const hid = 'modal-h-' + (_stack.length + 1);
      h.id = hid;
      backdrop.setAttribute('aria-labelledby', hid);
      card.appendChild(h);
    }
    if (sub) {
      const p = document.createElement('p');
      p.className = 'sub';
      p.textContent = sub;
      card.appendChild(p);
    }

    const actions = document.createElement('div');
    actions.className = 'modal-actions';
    card.appendChild(actions);
    backdrop.appendChild(card);

    const prevFocus = document.activeElement;
    let done = false;
    function settle(value) {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      const i = _stack.indexOf(backdrop);
      if (i !== -1) _stack.splice(i, 1);
      backdrop.remove();
      if (prevFocus && typeof prevFocus.focus === 'function') {
        try { prevFocus.focus(); } catch (_) { /* element gone */ }
      }
      resolve(value);
    }

    // onCancel is set by each dialog (false / null / undefined).
    backdrop._onCancel = function () { settle(undefined); };

    function onKey(e) {
      if (_stack[_stack.length - 1] !== backdrop) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation(); // don't also trip the app-global Esc handler
        backdrop._onCancel();
      } else if (e.key === 'Tab') {
        // Minimal focus trap so Tab cycles within the card.
        const f = card.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    }

    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) backdrop._onCancel();
    });
    document.addEventListener('keydown', onKey, true);

    document.body.appendChild(backdrop);
    _stack.push(backdrop);
    // Force reflow before adding .is-open isn't needed (no transition),
    // but flipping the class keeps parity with the existing pattern.
    backdrop.classList.add('is-open');

    return { backdrop, card, actions, settle };
  }

  // ---- confirm -------------------------------------------------------
  // Resolves true on confirm, false on cancel / Esc / backdrop.
  window.confirmModal = function (arg) {
    const opts = _coerce(arg);
    const danger = opts.danger !== false; // destructive by default
    return new Promise(function (resolve) {
      const ui = _build(opts, resolve);
      ui.backdrop._onCancel = function () { ui.settle(false); };
      const cancel = _btn(opts.cancelText || 'Cancel', 'secondary');
      const ok = _btn(opts.confirmText || 'Confirm', danger ? 'danger' : 'primary');
      cancel.addEventListener('click', function () { ui.settle(false); });
      ok.addEventListener('click', function () { ui.settle(true); });
      ui.actions.appendChild(cancel);
      ui.actions.appendChild(ok);
      ok.focus();
    });
  };

  // ---- alert ---------------------------------------------------------
  // Resolves (void) when dismissed. Esc / backdrop also dismiss.
  window.alertModal = function (arg) {
    const opts = _coerce(arg);
    return new Promise(function (resolve) {
      const ui = _build(opts, resolve);
      const ok = _btn(opts.okText || 'OK', 'primary');
      ok.addEventListener('click', function () { ui.settle(); });
      ui.actions.appendChild(ok);
      ok.focus();
    });
  };

  // ---- prompt --------------------------------------------------------
  // Resolves the entered string, or null on cancel / Esc / backdrop —
  // matching native prompt()'s contract.
  window.promptModal = function (arg) {
    const opts = _coerce(arg);
    return new Promise(function (resolve) {
      const ui = _build(opts, resolve);
      ui.backdrop._onCancel = function () { ui.settle(null); };

      const input = document.createElement('input');
      input.type = opts.inputType || 'text';
      input.value = opts.defaultValue != null ? String(opts.defaultValue) : '';
      if (opts.placeholder) input.placeholder = opts.placeholder;
      // Insert the input above the actions row.
      ui.card.insertBefore(input, ui.actions);

      const cancel = _btn(opts.cancelText || 'Cancel', 'secondary');
      const ok = _btn(opts.confirmText || 'OK', 'primary');
      cancel.addEventListener('click', function () { ui.settle(null); });
      ok.addEventListener('click', function () { ui.settle(input.value); });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); ui.settle(input.value); }
      });
      ui.actions.appendChild(cancel);
      ui.actions.appendChild(ok);
      input.focus();
      input.select();
    });
  };
})();
