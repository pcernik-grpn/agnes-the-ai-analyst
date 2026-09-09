### Added
- **An operator can turn the analyst onboarding off for everyone.** The new
  `onboarding` switch (`features.onboarding_enabled`, env
  `AGNES_ONBOARDING_ENABLED`, editable in `/admin/server-config`) silences the
  whole unattended onboarding layer: all four guided coach-mark tours (the
  welcome walkthrough on `/chat`, the `/agents` and Connect cards, the
  skill-builder mark) and the analyst checklist card in the rail foot, with its
  popover, its replay control and the profile menu's "Start over onboarding"
  entry. **On by default** — it is a kill switch for a surface every instance
  already has, so an upgrade changes nothing. Off gates UI only:
  `/api/chat/journey` keeps serving and keeps recording steps, so turning it
  back on resumes every user exactly where they were. Not covered, by design:
  the ADMIN setup chain (a different rail card — an operator silencing the
  walkthrough keeps their own instance-setup progress), the chat's greeting,
  and the empty-Stack question that recommends data packages, which answer a
  user's question rather than narrating over it. Enforced at one seam per
  layer — `tour.js`'s three exported entry points and `chat_onboarding.js`'s
  two — so a new call site cannot escape the switch; a missing flag reads as ON,
  so only an explicit `false` disables. The switch is visible to operator
  tooling as well as the web UI: it resolves through
  `instance_config.get_onboarding_enabled()`, which is the single read behind
  both the pages and `GET /api/admin/config-surface`, so `agnes admin` and the
  operator MCP tools report its effective value and whether it came from `env`,
  `yaml` or the default. See [`docs/feature-flags.md`](docs/feature-flags.md)
  and [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

### Fixed
- **A present-but-empty `AGNES_*` override is no longer blamed on YAML.**
  `/api/admin/config-surface` decided a knob's `source` from the env var's
  content, which is right for the string and select knobs (they read
  `os.environ.get(X) or get_value(...)`, so an empty value is ignored) and
  wrong for the switch-backed booleans (`feature_enabled` honours any PRESENT
  value and coerces `""` to `False`). A rendered `.env` line with nothing after
  the `=` therefore turned such a feature off while the inventory reported
  `source: "yaml"`, sending an operator to look for a config line that does not
  exist. Rows now opt in with `env_empty_overrides`, set on the four knobs that
  need it — `onboarding_enabled`, `agent_profiles_enabled`,
  `home_automode_visibility` and `home_status_frame_visibility` — and a sweep in
  `tests/test_config_surface_api.py` fails if a future row honours an empty
  override without declaring it, or declares it without honouring one.

### Internal
- The design-system reference's accent vocabulary named `--ds-kai-*`, a token
  family that no longer exists — it was renamed to `--ds-assistant-*` when the
  assistant name was retired, and only `--ds-assistant-*` is defined in
  `app/web/static/css/`. Writing the documented token left the surface
  uncoloured. The entry now names the shipped family and records the rename.
