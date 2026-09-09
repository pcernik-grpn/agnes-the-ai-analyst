### Fixed

- An expired browser session no longer reads as a broken chat. A chat request answering 401 rendered as `Could not start chat: 401` — a status code and no way forward, when the actual fix was to sign in again. The chat now says the session expired and sends the reader to `/login?next=<current page>`, so signing in returns them where they were. This covers every way into a conversation, not just sending a message: the New Chat button, picking an agent, a `?agent=` link, loading a conversation's history, and re-arming its socket. Any other failure keeps its own message.
- A revoked chat grant is no longer reported as an expired login. `require_chat_access` answers 403 to a signed-in caller whose group lost access; sending them to a login they pass and bounce straight off again is a loop. 403 now says access is missing and names who can grant it, and stays on the page.

### Added

- Caddy writes an access log (JSON, one line per request, to stdout) on every site it serves — the single-node proxy, the legacy-domain alias beside it, the multi-tier proxy, and the hosted-apps subdomain vhost. The app logs no HTTP access lines of its own, so a request served by a site without one left no record anywhere: a reported status could not be confirmed, dated, or counted afterwards. `log` is site-scoped in Caddy, so `tests/test_caddyfile_access_log.py` now holds every shipped config to the rule.
