"""Canonical chat icon vocabulary (issue #1503).

One list, three consumers, kept in sync by ``tests/test_chat_icons.py``:

- the SVG sprite ``app/web/static/vendor/lucide-sprite.svg`` (a vendored
  Lucide subset, ISC — see ``app/web/static/vendor/LICENSES.md``) must carry
  a ``<symbol id>`` for every name here;
- the browser allowlist in ``app/web/static/js/chat_icons.js`` (what the chat
  actually renders for an ``icon:<name>`` token) must equal
  ``CHAT_INLINE_ICON_NAMES``;
- the prompt rule in ``config/claude_md_template.txt`` receives
  ``CHAT_INLINE_ICON_NAMES`` as the ``chat_icons`` template variable, so the
  model is only ever told about names the UI renders. The static fallback
  ``app/initial_workspace_default/CLAUDE.md`` hardcodes the same list.

Names are Lucide's own canonical icon names — no mapping layer, the sprite
symbol id IS the name. ``CHROME_ICON_NAMES`` are UI-chrome-only icons
(tool-card chevrons, spinners, the greeting hand): in the sprite, usable by
the frontend, deliberately NOT offered to the model.
"""

from __future__ import annotations

#: Inline vocabulary the model may use in chat answers as `icon:<name>`.
CHAT_INLINE_ICON_NAMES: tuple[str, ...] = (
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
)

#: UI-chrome icons: in the sprite, not offered to the model.
CHROME_ICON_NAMES: tuple[str, ...] = (
    "chevron-down",
    "chevron-left",
    "chevron-right",
    "chevron-up",
    "loader-circle",
)

#: Every symbol the sprite must carry.
ALL_ICON_NAMES: tuple[str, ...] = tuple(sorted({*CHAT_INLINE_ICON_NAMES, *CHROME_ICON_NAMES}))
