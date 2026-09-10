### Fixed
- **Windows: a self-upgrade blocked by Smart App Control now says so instead of
  failing opaquely.** On a clean-installed Windows 11 machine, Smart App Control
  can refuse the unsigned artifacts of a `uv tool install` — the generated
  `agnes.exe` launcher and the interpreter uv downloads — with `os error 4551`.
  Both places where a running agnes spawns the newly installed binary (the
  `agnes self-upgrade` smoke test and the Windows deferred-update helper's
  install + verify) now recognise that failure and print the diagnosis, the
  registry check that confirms it, and the honest workaround with its cost. The
  deferred helper records the same reason, and because an application-control
  block never clears on its own, it surfaces on the first failure rather than
  after three silent ones. A block on the binary *you* launch cannot be reported
  from inside the CLI — the process is refused before any Agnes code runs; see
  `docs/QUICKSTART.md` → "Windows: Smart App Control blocks the CLI". (#2342)
- **Windows deferred update no longer burns its 5-minute retry budget on a
  policy block.** An application-control failure was close enough to a file-lock
  message to be retried like one; it is now classified first and fails fast with
  the real reason.

### Added
- **A Windows troubleshooting section for `os error 4551`** in
  `docs/QUICKSTART.md`: the symptom, how to confirm it is Smart App Control
  (`VerifiedAndReputablePolicyState`), who is exposed (a clean-installed or
  reset Windows 11 machine, certain regions, before Microsoft's developer
  heuristic exempts the owner), and why turning Smart App Control off is a
  system-wide, one-way decision. It states plainly that a signed Windows
  install path does not exist yet. (#2342)
