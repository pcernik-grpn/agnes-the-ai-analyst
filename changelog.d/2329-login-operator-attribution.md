### Fixed
- **The login page now carries readable operator attribution, and the doctor
  catches it when it doesn't.** An unbranded instance (neither `instance.name`
  nor `instance.brand` set) rendered a login page with no text tying the host
  to its operator — an install agent verifying the page before trusting it
  could read that as impersonation and refuse to proceed. The "Made by"
  Keboola credit now carries a real text node (previously only an
  `aria-label`), the login page shows an "Operated by …" line fed by
  `instance.copyright` when set, and the `branding` doctor check
  (`agnes doctor` / `/api/admin/doctor/new-instance`) now runs unconditionally
  instead of skipping whenever `instance.brand` was still default — the exact
  state that let this go uncaught.
