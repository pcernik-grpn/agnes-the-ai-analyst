### Fixed

- An expired browser session no longer reads as a broken chat. `POST /api/chat/sessions` answering 401 rendered as `Could not start chat: 401` — a status code and no way forward, when the actual fix was to sign in again. The chat now says the session expired and sends the reader to `/login?next=<current page>`, so signing in returns them where they were. Any other failure keeps its own message.

### Added

- Caddy writes an access log (JSON, stdout). It was off, and the app logs no HTTP access lines either, so a 401 left no trace on any surface — "how often does this hit people?" was unanswerable and the one report we have arrived as a user's screenshot.
