### Fixed

- Keep admin sync-dashboard registry reads off the HTTP event loop so a slow registry poll does not freeze unrelated requests.
