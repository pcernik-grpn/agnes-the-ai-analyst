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
  so only an explicit `false` disables. See
  [`docs/feature-flags.md`](docs/feature-flags.md).

### Internal
- The design-system reference's accent vocabulary named `--ds-kai-*`, a token
  family that no longer exists — it was renamed to `--ds-assistant-*` when the
  assistant name was retired, and only `--ds-assistant-*` is defined in
  `app/web/static/css/`. Writing the documented token left the surface
  uncoloured. The entry now names the shipped family and records the rename.
