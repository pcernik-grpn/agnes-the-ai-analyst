### Added
- The MCP source detail page (`/admin/mcp-sources/{id}`) now has an "OAuth client" section for `auth_method='oauth'` sources: an admin can register an OAuth client via dynamic client registration (RFC 7591) or by pasting one already created upstream, right from the web UI — previously this was only reachable via the CLI or the raw API, even though the builder wizard's hint text promised it. The inline "Your connection" card's "Connect your account" control is now gated on a client actually being registered, instead of presenting a link that was doomed to fail.

### Fixed
- `/me/connections` now shows an admin a direct link to register the missing OAuth client when a connect attempt fails with `client_registration_missing`, instead of only a fixed error message with a "Try again" link that would just fail the same way again.
