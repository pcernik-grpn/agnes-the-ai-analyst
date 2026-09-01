/* =====================================================================
 * file_preview.js — "what IS this file?" in a modal.
 *
 * One preview surface for every file the product shows: the Library's two
 * item pages (a row click on the collection detail page, the Preview action
 * on a file's own detail page) and the chat's session-files drawer. It asks
 * the server what to show and renders exactly the shape that comes back:
 *
 *   image / pdf → the real bytes, drawn by the browser from `raw_url`
 *   slides      → a deck's text, slide by slide (chat deliverables — a
 *                 browser cannot draw a .pptx, so the server sends its words)
 *   sheets      → a workbook's cells, sheet by sheet (same reason for .xlsx)
 *   text        → source (textual uploads) or the extracted text, in a <pre>
 *   none        → the server's own sentence about why not, verbatim
 *
 * The endpoint differs per caller (`previewUrl`), the vocabulary does not:
 * a new `kind` is added HERE once and both surfaces get it.
 *
 * The client deliberately does NOT decide which extensions are viewable —
 * that list is a security boundary (an uploaded .html must never be
 * rendered inline), so it lives server-side and this file just obeys.
 *
 * Every server string lands via textContent. Nothing here interpolates a
 * filename, a reason or extracted text into markup.
 *
 * Usage:
 *   openFilePreview({
 *     collectionId, fileId,          // Library: builds the collections URL
 *     previewUrl,                    // …or pass the endpoint outright
 *     filename, fileType, sizeLabel, // header hints (optional)
 *     detailHref,                    // shows "Open file page" in the foot
 *     downloadHref, downloadName,    // shows a Download action in the foot
 *   })
 * ===================================================================== */
(function () {
  'use strict';

  var DOC_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14 3v4a1 1 0 0 0 1 1h4"/>' +
    '<path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/></svg>';

  var open = null; // { backdrop, teardown } for the modal currently on screen

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function close() {
    if (!open) return;
    var cur = open;
    open = null;
    document.removeEventListener('keydown', cur.onKey);
    if (cur.backdrop.parentNode) cur.backdrop.parentNode.removeChild(cur.backdrop);
    if (cur.restoreFocus && cur.restoreFocus.focus) {
      try { cur.restoreFocus.focus(); } catch (_) {}
    }
  }

  function build(opts) {
    var backdrop = el('div', 'fp-backdrop');
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-label', 'Preview of ' + (opts.filename || 'file'));
    // Opt out of the app-wide Escape handler in _app_scripts.html — this
    // modal owns its own Esc / backdrop-click teardown.
    backdrop.dataset.noEscClose = '1';

    var card = el('div', 'fp-card');

    var head = el('div', 'fp-head');
    var glyph = el('span', 'fp-head__glyph');
    glyph.innerHTML = DOC_SVG; // static markup, no interpolation
    var headBody = el('div', 'fp-head__body');
    var name = el('h3', 'fp-head__name', opts.filename || 'File');
    var meta = el('p', 'fp-head__meta');
    headBody.appendChild(name);
    headBody.appendChild(meta);
    var closeBtn = el('button', 'fp-close', '×');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Close preview');
    head.appendChild(glyph);
    head.appendChild(headBody);
    head.appendChild(closeBtn);

    var body = el('div', 'fp-body');
    body.appendChild(el('div', 'fp-msg', 'Loading preview…'));

    var foot = el('div', 'fp-foot');
    var note = el('p', 'fp-note');
    foot.appendChild(note);
    if (opts.detailHref) {
      var link = el('a', 'btn btn-secondary', 'Open file page');
      link.href = opts.detailHref;
      foot.appendChild(link);
    }
    // The chat drawer opens this modal ON the deliverable, so "I've seen it,
    // give me the file" has to be reachable without closing it first and
    // hunting for the row again.
    if (opts.downloadHref) {
      var dl = el('a', 'btn btn-secondary', 'Download');
      dl.href = opts.downloadHref;
      dl.setAttribute('download', opts.downloadName || opts.filename || '');
      foot.appendChild(dl);
    }
    var done = el('button', 'btn btn-primary', 'Close');
    done.type = 'button';
    foot.appendChild(done);

    card.appendChild(head);
    card.appendChild(body);
    card.appendChild(foot);
    backdrop.appendChild(card);

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    }
    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) close();
    });
    closeBtn.addEventListener('click', close);
    done.addEventListener('click', close);

    return { backdrop: backdrop, meta: meta, body: body, note: note, onKey: onKey, closeBtn: closeBtn };
  }

  function humanBytes(n) {
    if (!n && n !== 0) return '';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    var v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
  }

  function renderMeta(metaEl, data, opts) {
    var bits = [];
    var type = data.file_type || opts.fileType;
    if (type) bits.push(String(type).toUpperCase());
    if (opts.sizeLabel) bits.push(opts.sizeLabel);
    else if (data.size_bytes) bits.push(humanBytes(data.size_bytes));
    metaEl.textContent = bits.join(' · ');
  }

  function render(ui, data, opts) {
    ui.body.textContent = '';
    ui.note.textContent = '';

    if (data.kind === 'image') {
      var img = el('img', 'fp-img');
      img.alt = 'Preview of ' + (data.filename || data.name || opts.filename || 'the file');
      img.src = data.raw_url;
      img.addEventListener('error', function () {
        ui.body.textContent = '';
        ui.body.appendChild(el('div', 'fp-msg', 'This image could not be loaded.'));
      });
      ui.body.appendChild(img);
      return;
    }

    if (data.kind === 'pdf') {
      var frame = el('iframe', 'fp-frame');
      frame.title = 'Preview of ' + (data.filename || data.name || opts.filename || 'the file');
      // Safe without `sandbox`: the endpoint pins the response to
      // application/pdf + nosniff, so the body can never be interpreted as
      // HTML on our origin — and a fully sandboxed frame would also disable
      // the browser's built-in PDF viewer, which is the whole point here.
      frame.src = data.raw_url;
      ui.body.appendChild(frame);
      return;
    }

    if (data.kind === 'slides') {
      // A .pptx has no browser renderer, so the server sends the deck's
      // words and this draws them as slide cards — enough to confirm the
      // agent built the deck that was asked for before spending a download.
      var deck = el('div', 'fp-slides');
      (data.slides || []).forEach(function (s) {
        var card = el('article', 'fp-slide');
        card.appendChild(el('span', 'fp-slide__num', 'Slide ' + s.index));
        if (s.title) card.appendChild(el('h4', 'fp-slide__title', s.title));
        if (s.lines && s.lines.length) {
          var ul = el('ul', 'fp-slide__lines');
          s.lines.forEach(function (line) { ul.appendChild(el('li', null, line)); });
          card.appendChild(ul);
        }
        if (!s.title && !(s.lines && s.lines.length)) {
          card.appendChild(el('p', 'fp-slide__empty', 'No text on this slide.'));
        }
        deck.appendChild(card);
      });
      ui.body.appendChild(deck);
      var deckNotes = ['Slide text only — not the rendered layout.'];
      if (data.truncated) deckNotes.push('Showing the first slides.');
      ui.note.textContent = deckNotes.join(' ');
      return;
    }

    if (data.kind === 'sheets') {
      // A .xlsx has no browser renderer either, so the server sends the
      // stored cells and this draws them as grids — one per sheet, in tab
      // order. Each table scrolls in its own wrapper so a wide export never
      // makes the modal scroll sideways.
      var book = el('div', 'fp-sheets');
      var anyClipped = false;
      (data.sheets || []).forEach(function (s) {
        var section = el('section', 'fp-sheet');
        section.appendChild(el('span', 'fp-sheet__name', s.name || 'Sheet'));
        var rows = s.rows || [];
        if (!rows.length) {
          section.appendChild(el('p', 'fp-sheet__empty', 'This sheet is empty.'));
        } else {
          var wrap = el('div', 'fp-sheet__wrap');
          var table = el('table', 'fp-sheet__grid');
          var body = document.createElement('tbody');
          rows.forEach(function (cells) {
            var tr = document.createElement('tr');
            (cells || []).forEach(function (cell) {
              tr.appendChild(el('td', null, cell));
            });
            body.appendChild(tr);
          });
          table.appendChild(body);
          wrap.appendChild(table);
          section.appendChild(wrap);
        }
        if (s.truncated) anyClipped = true;
        book.appendChild(section);
      });
      ui.body.appendChild(book);
      var bookNotes = ['Stored cell values — not the rendered formatting.'];
      if (anyClipped) bookNotes.push('Showing the first rows and columns.');
      if (data.truncated) bookNotes.push('Showing the first sheets.');
      ui.note.textContent = bookNotes.join(' ');
      return;
    }

    if (data.kind === 'text') {
      ui.body.appendChild(el('pre', 'fp-text', data.text || ''));
      var notes = [];
      if (data.source === 'extracted') notes.push('Extracted text — not the original layout.');
      if (data.truncated) notes.push('Showing the beginning of the file.');
      ui.note.textContent = notes.join(' ');
      return;
    }

    ui.body.appendChild(el('div', 'fp-msg', data.reason || 'No preview is available for this file.'));
  }

  window.openFilePreview = function (opts) {
    opts = opts || {};
    // Either an endpoint outright (the chat drawer) or the Library's pair.
    var url = opts.previewUrl;
    if (!url) {
      if (!opts.collectionId || !opts.fileId) return;
      url =
        '/api/collections/' + encodeURIComponent(opts.collectionId) +
        '/files/' + encodeURIComponent(opts.fileId) + '/preview';
    }
    close(); // one preview at a time

    var ui = build(opts);
    open = { backdrop: ui.backdrop, onKey: ui.onKey, restoreFocus: document.activeElement };
    document.addEventListener('keydown', ui.onKey);
    document.body.appendChild(ui.backdrop);
    ui.closeBtn.focus();
    renderMeta(ui.meta, {}, opts);

    fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        if (!open || open.backdrop !== ui.backdrop) return; // closed meanwhile
        renderMeta(ui.meta, data, opts);
        render(ui, data, opts);
      })
      .catch(function () {
        if (!open || open.backdrop !== ui.backdrop) return;
        ui.body.textContent = '';
        ui.body.appendChild(el('div', 'fp-msg', 'The preview could not be loaded.'));
      });
  };

  window.closeFilePreview = close;

  /* Delegated opener: any element carrying data-preview-file="<file_id>"
     plus data-preview-collection="<collection_id>" opens the modal. Lets a
     server-rendered row stay a plain <a> to the file's detail page (so
     middle-click, Cmd-click and no-JS all still work) while a plain click
     previews in place. */
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-preview-file]') : null;
    if (!t) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    var cid = t.getAttribute('data-preview-collection');
    var fid = t.getAttribute('data-preview-file');
    if (!cid || !fid) return;
    e.preventDefault();
    window.openFilePreview({
      collectionId: cid,
      fileId: fid,
      filename: t.getAttribute('data-preview-name') || t.textContent.trim(),
      fileType: t.getAttribute('data-preview-type') || '',
      sizeLabel: t.getAttribute('data-preview-size') || '',
      detailHref: t.getAttribute('data-preview-detail') || t.getAttribute('href') || '',
    });
  });
})();
