### Changed
- **The install prompt no longer requires a pre-saved login token before
  starting.** `agnes onboard` (step 2) already resolves auth itself — a
  bootstrap token file, a saved credential, or, with neither, its own
  interactive browser sign-in — so the prompt's separate bash pre-check
  ("is `~/.agnes/token` there yet? if not, stop and go sign in first") was
  answering a question the CLI already answers better, one step later,
  with a real fallback instead of a dead end. The preamble now states
  sign-in as an either/or instead of a hard prerequisite. The git-bindable
  seed template (`source_mode='git'` instances) carries the same fix, plus
  the Requirements line and first-question nudge it had drifted behind.
