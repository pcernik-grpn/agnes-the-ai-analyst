### Fixed
- **An agent can file a report again.** Every issue route read the caller as a
  plain dict, so a sandboxed agent — the one the workspace prompt tells to
  offer filing — got a 500 instead of a report. The caller is now normalized:
  an agent files as the person whose turn is running, a one-person chat files
  as that person, and a shared co-session or a data-app viewer is refused with
  a typed 403 rather than attributed to a guess.
- **Two admins resolving the same report no longer both get "resolved".** The
  guarded `UPDATE` decides the winner, so the loser gets the documented 409 and
  the first resolver's signature survives.
- **The operator message names the screenshot.** The upload arrives on a
  separate request after the report is created, so the mirror waited for the
  creation snapshot and never carried the link it promised; it now re-reads the
  report, briefly waiting when the client says a screenshot is coming.
