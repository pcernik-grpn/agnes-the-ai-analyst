### Fixed
- **Excluding a SharePoint folder now retroactively purges what was already
  ingested from an anonymize-marked scope.** Such a scope stores a redacted
  `corpus_files.path`, so the ACL sweep's cleanup was comparing it against the
  admin's real folder path and matching nothing: a folder exclusion, or a
  folder newly promoted to its own permission zone, left the already-ingested
  copies in place. The cleanup now derives the comparison prefix by running
  that real path through the crawl's own per-segment anonymization (same
  instance key and detector) and matches both forms, so a scope crawled before
  it was marked keeps being reachable too. File-kind exclusions, matched by the
  Graph item's stable id, were never affected. Exact on the regex detector
  tier; see `docs/anonymization.md` → *Current limits* for what the LLM tier
  can still miss.
