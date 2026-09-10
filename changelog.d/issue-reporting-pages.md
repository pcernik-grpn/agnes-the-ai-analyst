### Added
- **`/me/issues` and `/admin/issues` web pages for issue reports.** A reporter
  can now browse their own "Report a problem" filings — status, replies and age
  at a glance, with a filter for open/resolved/all — and open one to see the
  body, the auto-captured context, the screenshot when there is one, the
  comment thread, and a box to add a comment. Admins get the same view across
  every reporter at `/admin/issues` (Content section), plus a reply box and a
  Resolve action with an optional note; resolving an already-closed report
  surfaces the specific "already resolved by X at T" message instead of a
  generic failure. Both pages are static shells reading the existing issue
  REST API client-side — no new endpoints — and stay hidden (with no sidebar
  entry) on a DuckDB-backed instance, matching the "Report a problem" button's
  own Postgres-only gate. See `docs/issue-reporting.md`.
