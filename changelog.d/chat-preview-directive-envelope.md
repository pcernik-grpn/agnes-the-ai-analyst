### Fixed
- **Web chat: a data-app preview tool no longer renders "Preview unavailable."
  when its result arrives in an MCP envelope.** The engine provider forwards
  the raw `{content:[{type:"text",…}]}` envelope, so the
  `data_app_preview_refresh` / `data_app_credentials` directives sat one level
  below where the chat looked — the fallback copy appeared twice per turn and
  the shareable URL never rendered. The directive is now unwrapped from the
  envelope; a preview tool that genuinely fails shows its own error text
  instead of the constant; and the (cardless) preview tool call seals the
  streaming bubble like every other inline block, so the sentence before it
  and the one after no longer run together ("Let me refresh it:The preview
  should be refreshing now").
