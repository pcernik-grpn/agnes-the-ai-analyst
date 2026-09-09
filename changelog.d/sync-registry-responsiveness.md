### Fixed

- Keep admin sync-dashboard registry reads off the HTTP event loop so a slow registry poll does not freeze unrelated requests.
- Prevent overlapping sync-dashboard polls and bound concurrent dashboard registry reads so slow polls cannot exhaust HTTP workers, while preserving ordinary CLI and admin UI reads.
