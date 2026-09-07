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
- **Web chat: the data-app preview pane is visible again under the rail
  layout.** The rail layout collapses the chat shell to one grid column with
  a rule that outranks the pane's own, so an open preview landed in an
  implicit 0px row below the thread — the "App preview" header and nothing
  under it, which is how a working dashboard read as "I can't see the
  dashboard". The pane now gets its own column beside the thread (widening on
  large screens) and stacks under the thread as a bottom sheet on viewports
  narrower than 900px.
