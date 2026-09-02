// A minimal DOM for node-executing the SHIPPED front-end modules that need one.
// This repo carries no jsdom (see tests/test_chat_facts_rendering_ui.py for the
// same trade-off at function scope); what this covers is an element tree,
// querySelector over the selector shapes those modules actually use (#id, .cls,
// tag, [attr], [attr="v"], :not(...), descendant combinations), bubbling click
// and change events, and read-back of whatever the module wrote. Deliberately
// not a browser: no layout (every rect is zero), no CSS cascade, no parsing of
// HTML strings.
//
// Used by: tests/test_web_chats_filter_clear.py (filter_toolbar.js).
class ClassList {
  constructor(el) { this.el = el; }
  get _set() { return new Set((this.el.className || '').split(/\s+/).filter(Boolean)); }
  _write(s) { this.el.className = Array.from(s).join(' '); }
  add(c) { const s = this._set; s.add(c); this._write(s); }
  remove(c) { const s = this._set; s.delete(c); this._write(s); }
  contains(c) { return this._set.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); }
}

class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.nodeType = 1;
    this.children_ = [];
    this.parentNode = null;
    this.attrs = {};
    this.text = '';
    this.hidden = false;
    this.classList = new ClassList(this);
    this.listeners = {};
    this.value = '';
    this.checked = false;
    this.type = '';
    this.title = '';
    this.style = { setProperty() {}, removeProperty() {} };
  }
  get className() { return this.attrs['class'] || ''; }
  set className(v) { this.attrs['class'] = v; }
  get dataset() {
    const d = {};
    for (const k of Object.keys(this.attrs)) {
      const m = /^data-(.+)$/.exec(k);
      if (m) d[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = this.attrs[k];
    }
    return d;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    // Real elements reflect these attributes onto the IDL property, which is
    // what the engine reads (`input.value`, `input.checked`).
    if (k === 'value') this.value = String(v);
    if (k === 'checked') this.checked = true;
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  get childNodes() { return this.children_.slice(); }
  get children() { return this.children_.filter(n => n.nodeType === 1); }
  get firstChild() { return this.children_[0] || null; }
  get lastChild() { return this.children_[this.children_.length - 1] || null; }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.children_.push(n); return n; }
  insertBefore(n, ref) {
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    const i = ref ? this.children_.indexOf(ref) : -1;
    if (i === -1) this.children_.push(n); else this.children_.splice(i, 0, n);
    return n;
  }
  removeChild(n) { const i = this.children_.indexOf(n); if (i !== -1) { this.children_.splice(i, 1); n.parentNode = null; } return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get textContent() {
    if (!this.children_.length) return this.text;
    return this.children_.map(n => n.textContent).join('');
  }
  set textContent(v) { this.children_.forEach(n => { n.parentNode = null; }); this.children_ = []; this.text = String(v); }
  set innerHTML(v) { if (v === '') { this.children_.forEach(n => { n.parentNode = null; }); this.children_ = []; } }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const a = this.listeners[type] || [];
    const i = a.indexOf(fn); if (i !== -1) a.splice(i, 1);
  }
  focus() { doc.activeElement = this; }
  contains(n) { for (let p = n; p; p = p.parentNode) if (p === this) return true; return false; }
  closest(sel) { for (let p = this; p; p = p.parentNode) if (p.nodeType === 1 && matches(p, sel)) return p; return null; }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  scrollIntoView() {}
  querySelector(sel) { return query(this, sel)[0] || null; }
  querySelectorAll(sel) { return query(this, sel); }
  click() { dispatch(this, 'click'); }
}

class TextNode {
  constructor(t) { this.nodeType = 3; this.text = String(t); this.parentNode = null; }
  get textContent() { return this.text; }
  set textContent(v) { this.text = String(v); }
}

// Bubbles from the target to the document, and HONOURS stopPropagation — the
// engine relies on it: a chip's handlers stop the click so the document-level
// "click outside closes the menu" listener does not shut the popover they just
// opened. A shim that ignored it would report the opposite of what ships.
function dispatch(el, type) {
  let stopped = false;
  const ev = {
    type, target: el,
    stopPropagation() { stopped = true; },
    preventDefault() {},
  };
  for (let p = el; p; p = p.parentNode) {
    (p.listeners && p.listeners[type] || []).forEach(fn => fn.call(p, ev));
    if (stopped) return;
  }
  (doc.listeners[type] || []).forEach(fn => fn.call(doc, ev));
}

// ── selector matching: #id, .cls, tag, [attr], [attr="v"], :not(...), and
//    descendant combinations of those. Enough for this engine's selectors.
function matchSimple(el, part) {
  if (el.nodeType !== 1) return false;
  let rest = part;
  const re = /^(\*|[a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[^\]]+\]|:not\([^)]+\))*)$/;
  const m = re.exec(rest);
  if (!m) throw new Error('unsupported selector part: ' + part);
  if (m[1] && m[1] !== '*' && el.tagName !== m[1].toUpperCase()) return false;
  const tokens = m[2].match(/[#.][\w-]+|\[[^\]]+\]|:not\([^)]+\)/g) || [];
  for (const t of tokens) {
    if (t[0] === '#') { if (el.getAttribute('id') !== t.slice(1)) return false; }
    else if (t[0] === '.') { if (!el.classList.contains(t.slice(1))) return false; }
    else if (t[0] === '[') {
      const inner = t.slice(1, -1);
      const eq = /^([\w-]+)(?:\s*=\s*"([^"]*)")?$/.exec(inner);
      if (!eq) throw new Error('unsupported attr selector: ' + t);
      if (eq[2] === undefined) { if (!el.hasAttribute(eq[1])) return false; }
      else if (el.getAttribute(eq[1]) !== eq[2]) return false;
    } else { // :not(...)
      if (matchSimple(el, t.slice(5, -1))) return false;
    }
  }
  return true;
}
function matches(el, sel) {
  return sel.split(',').map(s => s.trim()).some(one => {
    const parts = one.split(/\s+/);
    if (!matchSimple(el, parts[parts.length - 1])) return false;
    let p = el.parentNode;
    for (let i = parts.length - 2; i >= 0; i--) {
      let found = false;
      while (p) { if (matchSimple(p, parts[i])) { found = true; p = p.parentNode; break; } p = p.parentNode; }
      if (!found) return false;
    }
    return true;
  });
}
function descendants(root, out) {
  for (const c of root.children_ || []) { if (c.nodeType === 1) { out.push(c); descendants(c, out); } }
  return out;
}
function query(root, sel) {
  return descendants(root, []).filter(el => matches(el, sel));
}

const doc = new El('document');
doc.listeners = {};
doc.activeElement = null;
doc.createElement = tag => new El(tag);
doc.createTextNode = t => new TextNode(t);
doc.documentElement = new El('html');
const win = {
  document: doc,
  innerWidth: 1200,
  innerHeight: 800,
  listeners: {},
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  removeEventListener() {},
  getComputedStyle: () => ({ position: 'static' }),
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};
win.window = win;

module.exports = { El, doc, win, dispatch, matches, query };
