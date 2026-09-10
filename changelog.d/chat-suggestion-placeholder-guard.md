### Fixed
- **Chat "suggested actions" chips no longer send a fill-in-the-blank
  template verbatim.** A `next_actions` suggestion whose text still carries
  an unfilled placeholder (`[name]`, `<title>`, `{{amount}}`, a bare `TBD`)
  now pre-fills the composer for the reader to complete instead of
  submitting immediately on click — clicking such a chip used to send the
  model's own template straight back to it as the user's message,
  guaranteeing a dead turn.
- The detector keys on digits and operators, not on letter case. A citation
  marker (`[1]`) and comparison phrasing (`revenue <10 and >10`) keep their
  one-click submit — both used to read as placeholders — while a slot is
  caught whether it is written `[name]` or `[Client name]`, since a model
  capitalises one as readily as the other. The deliberate residue is an
  already-filled bracketed name (`[Acme Corp]`) reading as a slot: it
  pre-fills instead of sending, which costs a keystroke, where the opposite
  guess costs a whole turn.
- Clicking a placeholder chip now re-syncs the composer's autosize height,
  prompt-history position, and slash-menu state — the programmatic
  pre-fill used to leave those stale (e.g. the slash menu stayed open).
