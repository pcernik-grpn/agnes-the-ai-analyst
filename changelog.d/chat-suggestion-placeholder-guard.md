### Fixed
- **Chat "suggested actions" chips no longer send a fill-in-the-blank
  template verbatim.** A `next_actions` suggestion whose text still carries
  an unfilled placeholder (`[name]`, `<title>`, `{{amount}}`, a bare `TBD`)
  now pre-fills the composer for the reader to complete instead of
  submitting immediately on click — clicking such a chip used to send the
  model's own template straight back to it as the user's message,
  guaranteeing a dead turn.
