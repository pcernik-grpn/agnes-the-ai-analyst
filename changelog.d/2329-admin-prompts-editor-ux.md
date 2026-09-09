### Fixed
- **The `/admin/prompts` editor's raw-source textarea was mistakable for a
  ready-to-paste prompt.** It legitimately shows unrendered template source
  (including `{server_url}` and other placeholders substituted only at
  render time) so an admin can edit them — but nothing distinguished it from
  copy-ready text, so it was easy to copy raw source into an install session
  instead of using `/setup`'s already-correct clipboard button. A "Copy
  rendered" action now copies the actual rendered prompt (the same text
  `/preview` shows) straight from the editor.

### Changed
- **`/admin/prompts`: Save and Bind now ask for confirmation, matching
  Reset.** Both replace what every analyst gets from that prompt on from
  now on; previously only Reset asked. Save also stays disabled until the
  editor content actually diverges from what's saved (or the shipped
  default), instead of always appearing clickable.
