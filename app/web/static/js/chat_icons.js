// app/web/static/js/chat_icons.js
//
// The chat UI's one icon seam (issue #1503): a vendored Lucide sprite
// (static/vendor/lucide-sprite.svg), injected inline by icon_sprite.js and
// referenced via SAME-DOCUMENT <svg><use href="#name"> (external-document
// <use> only resolves in Safari 17.1+, so it is never used). Two jobs:
//
//   1. iconEl(name) — build a chrome icon for UI code (tool-card status,
//      approval shield, chevrons). Any sprite symbol is fair game.
//   2. applyInlineIcons(root) — render the model-facing `icon:<name>` token
//      vocabulary inside an already-sanitized answer body. Allowlist-only:
//      an unknown name stays as the plain text it arrived as — never markup.
//
// The allowlist below MUST equal src/chat_icons.py's CHAT_INLINE_ICON_NAMES
// (that list is what the prompt tells the model about); a contract test
// (tests/test_chat_icons.py) parses this file and fails on drift.

// Model-facing inline vocabulary. Keep sorted; keep equal to
// src/chat_icons.py CHAT_INLINE_ICON_NAMES.
export const CHAT_INLINE_ICON_NAMES = [
  "arrow-down",
  "arrow-left",
  "arrow-right",
  "arrow-up",
  "ban",
  "bell",
  "book-open",
  "bookmark",
  "box",
  "calendar",
  "chart-bar",
  "chart-line",
  "chart-pie",
  "check",
  "circle-alert",
  "circle-check",
  "circle-help",
  "circle-x",
  "clock",
  "cloud",
  "copy",
  "database",
  "download",
  "external-link",
  "eye",
  "file",
  "file-text",
  "filter",
  "flag",
  "folder",
  "gauge",
  "git-branch",
  "globe",
  "hand",
  "hard-drive",
  "history",
  "hourglass",
  "info",
  "key",
  "layers",
  "lightbulb",
  "link",
  "list",
  "list-checks",
  "lock",
  "mail",
  "minus",
  "package",
  "pause",
  "pencil",
  "pin",
  "play",
  "plus",
  "refresh-cw",
  "rocket",
  "search",
  "server",
  "settings",
  "shield",
  "sparkles",
  "star",
  "table",
  "trash-2",
  "trending-down",
  "trending-up",
  "triangle-alert",
  "upload",
  "user",
  "users",
  "workflow",
  "wrench",
  "x",
  "zap",
];

const _INLINE_ICON_SET = new Set(CHAT_INLINE_ICON_NAMES);

const SVG_NS = "http://www.w3.org/2000/svg";

/** Build an <svg><use> element referencing the sprite. `name` must be a
 *  sprite symbol id — callers pass literals or allowlisted names only; the
 *  element is built via createElementNS, so even a hostile name could not
 *  become markup, it would just reference a missing symbol and render empty.
 *  Same-document `#name` — icon_sprite.js injects the sprite, and <use>
 *  resolution is live, so an icon built before that fetch settles still
 *  renders the moment the sprite lands. */
export function iconEl(name, className) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className ? `ds-icon ${className}` : "ds-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${name}`);
  svg.appendChild(use);
  return svg;
}

// `icon:<name>` — the exact token grammar the prompt teaches. Names are
// kebab-case lucide ids; nothing else matches, so ordinary prose colons
// ("ratio 1:2", "src:app") are untouched.
const _TOKEN_RE = /icon:([a-z0-9]+(?:-[a-z0-9]+)*)/g;

/** Render `icon:<name>` tokens inside an answer body that has ALREADY been
 *  through renderMarkdownSafe. Two carriers, matching what models actually
 *  emit: an inline code span whose entire text is one token (the prompted
 *  form, `` `icon:database` ``), and bare tokens inside plain text nodes.
 *  Replacement nodes are built with createElementNS from the matched name —
 *  no HTML parsing, so this pass cannot introduce markup. Unknown names are
 *  left exactly as they arrived (plain text — the degrade the issue asks
 *  for). Skips <pre> so fenced code samples keep their literal text, and is
 *  idempotent because replaced tokens no longer exist as text. */
export function applyInlineIcons(root) {
  if (!root) return;
  // 1. Inline code spans: <code>icon:name</code> (not inside <pre>).
  root.querySelectorAll("code").forEach((code) => {
    if (code.closest("pre")) return;
    const m = /^icon:([a-z0-9]+(?:-[a-z0-9]+)*)$/.exec(code.textContent.trim());
    if (!m || !_INLINE_ICON_SET.has(m[1])) return;
    code.replaceWith(iconEl(m[1], "cloud-chat-inline-icon"));
  });
  // 2. Bare tokens in text nodes.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || p.closest("pre, code, script, style")) return NodeFilter.FILTER_REJECT;
      return node.nodeValue.includes("icon:")
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) {
    const text = node.nodeValue;
    _TOKEN_RE.lastIndex = 0;
    let m;
    let last = 0;
    let frag = null;
    while ((m = _TOKEN_RE.exec(text)) !== null) {
      if (!_INLINE_ICON_SET.has(m[1])) continue; // unknown → stays plain text
      frag = frag || document.createDocumentFragment();
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(iconEl(m[1], "cloud-chat-inline-icon"));
      last = m.index + m[0].length;
    }
    if (!frag) continue;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.replaceWith(frag);
  }
}
