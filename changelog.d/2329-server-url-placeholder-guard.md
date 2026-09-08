### Fixed
- **An install-prompt override could ship with a literal, un-substituted
  `{server_url}`.** The live-default install prompt uses `{server_url}` as a
  placeholder swapped in by a `str.replace` pass outside Jinja; an admin
  override is instead rendered through Jinja2, which only processes `{{ }}`.
  Seeding the override editor from the live default and saving it verbatim
  therefore shipped a prompt where `{server_url}` reached every install
  agent as literal text — no server to contact, every download/onboard step
  unusable. `PUT /api/admin/prompts/install` and
  `POST /api/admin/prompts/install/bind-git` now reject the bare placeholder
  at save/bind time with a pointer to the correct `{{ server.url }}` form,
  mirroring the existing `{token}` guard.
