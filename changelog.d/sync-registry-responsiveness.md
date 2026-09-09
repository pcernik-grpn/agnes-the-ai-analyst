### Fixed

- Keep admin sync-dashboard registry reads off the HTTP event loop so a slow registry poll does not freeze unrelated requests.
- Prevent overlapping sync-dashboard polls and reject concurrent registry reads promptly so slow reads cannot exhaust HTTP workers.
