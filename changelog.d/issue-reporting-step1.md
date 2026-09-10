### Added
- **Report a problem from anywhere, and see what came of it.** Every signed-in user
  gets a "Report a problem" entry in the rail and the account menu on every page; the
  dialog attaches the page, app version and commit, browser, chat session id, the last
  client-side errors and an optional screenshot, then stores the report in the instance
  (`issue_reports` / `issue_comments`, Postgres app-state backend required — a DuckDB
  instance shows no button). The same report is filed from the terminal with
  `agnes issue report` (`--screenshot`, `--attach-doctor`) and by any agent through the
  MCP tools `report_issue` / `list_my_issues` / `get_issue` / `issue_comment` on both
  HTTP transports and the stdio server the sandbox uses. Reporters follow their own
  reports at **`/me/issues`** or with `agnes issue list|show|comment`; admins work the
  whole queue at **`/admin/issues`** (Content section) — reply, and resolve with an
  optional note — or with `agnes admin issue list|show|reply|resolve` and the
  `issue_queue_list` / `issue_reply` / `issue_resolve` tools. `agnes_issues` and
  `agnes_issue_comments` join the internal tables (own rows only, admins see all), so
  any agent can answer "what have I reported and what changed". An optional
  `issues.webhook_url` (`AGNES_ISSUES_WEBHOOK_URL`) mirrors a one-message summary to
  the operator's chat channel; the record in the instance is the source of truth. See
  `docs/issue-reporting.md`.
