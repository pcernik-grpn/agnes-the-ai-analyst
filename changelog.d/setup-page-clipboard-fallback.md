### Fixed
- **`/setup`'s "Setup a new Claude Code" / copy buttons popped the manual
  "select all" fallback modal even when a silent copy would have worked.**
  `copyToClipboard()` only fell back to the `execCommand('copy')` path when
  the Clipboard API was absent, not when `navigator.clipboard.writeText()`
  rejected (browsers can reject it for reasons that don't affect the older
  path). Now catches the rejection and retries via the textarea fallback
  before giving up, matching `_claude_setup_cta.jinja`'s more robust
  `copyToClipboard`.
