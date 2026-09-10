### Fixed
- **Chat "suggested actions" chips no longer send a fill-in-the-blank
  template verbatim.** A `next_actions` suggestion whose text still carries
  an unfilled placeholder (`[name]`, `<title>`, `{{amount}}`, a bare `TBD`)
  now pre-fills the composer for the reader to complete instead of
  submitting immediately on click — clicking such a chip used to send the
  model's own template straight back to it as the user's message,
  guaranteeing a dead turn.
- Narrowed the placeholder detector so ordinary bracket/angle usage in a
  suggested action no longer trips it: a citation marker (`[1]`), an
  already-filled-in bracketed name (`[Acme Corp]`), and comparison
  phrasing (`revenue <10 and >10`) all used to read as an unfilled
  placeholder and lose their one-click submit.
- Clicking a placeholder chip now re-syncs the composer's autosize height,
  prompt-history position, and slash-menu state — the programmatic
  pre-fill used to leave those stale (e.g. the slash menu stayed open).
- The placeholder detector ignores letter case: a slot written `[Client name]` or `<Start date>` is caught the same as a lowercase one. A filled name in brackets therefore pre-fills the composer rather than sending — one keystroke, against a wasted turn if the guess went the other way.
