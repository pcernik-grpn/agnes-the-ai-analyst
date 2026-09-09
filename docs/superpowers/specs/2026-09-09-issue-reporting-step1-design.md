# Issue reporting, step 1: report from anywhere, keep the record, tell the operator

Date: 2026-09-09
Status: approved design, step 1 of 3
Branch: `zs/agnes-issue-reporting-1db265`

## Why

A person using Agnes has no way to say "this is wrong / missing / confusing"
from inside the product. The only report channel today is the narrow
`semantic_feedback` ("that answer looked wrong"), which is unreachable from
the web UI, invisible to its own reporter and dead for the sandbox agent
(`flag_semantic_issue` is not in the stdio tool set the sandbox uses).
`agnes doctor` ends with "attach this file to your support ticket" and there
is no ticket. The 403 page copies a sentence to the clipboard and files
nothing. Support today is a Slack thread and a human re-typing it into a
tracker, which loses the one thing the tracker needs most: *which* session,
page, query and version the problem happened on.

Step 1 gives every signed-in user a single gesture on every surface that
captures that context automatically, stores the report in the instance,
and mirrors a summary to the operator's chat channel so the team can start
recording problems now. Later steps add the support agent that tries to
help first (step 2) and the loop back to the reporter with status and
comments from an external tracker (step 3). This document is step 1 only;
the three-step shape is recorded so step 1 does not paint itself into a
corner.

## Scope

In:

- A **report dialog** reachable from every page of the rail chrome (rail foot
  button + user-menu item), which auto-attaches page URL, app version and
  commit, browser, chat session id, the last client-side errors, and an
  optional DOM screenshot of the current page.
- A **Postgres-only record** (`issue_reports`, `issue_comments`) with a
  two-state lifecycle (`open` → `resolved`) and a public comment thread
  between the reporter and an admin.
- **Operator mirror**: a text summary posted to a configured incoming
  webhook (Slack, or anything accepting `{"text": …}`) when a report is
  filed. The record is the source of truth; the webhook is a copy.
- **Reporter self-service** on three clients: REST, `agnes issue …`, and MCP
  tools `report_issue` / `list_my_issues` / `get_issue` / `issue_comment`,
  on both the HTTP foundation server and the stdio server the sandbox agent
  runs.
- **Any agent can answer "what are my open issues?"** through the existing
  internal-table mechanism: `agnes_issues` and `agnes_issue_comments` are
  registered as `query_mode='internal'` tables with the per-user row filter,
  so `agnes query "SELECT … FROM agnes_issues"` works everywhere `agnes
  query` works.
- **Admin queue** on CLI and MCP (`agnes admin issue …`, `issue_queue_list`
  / `issue_reply` / `issue_resolve`). No admin web page in step 1.

Out (later steps, named so nobody builds them here):

- Step 2: the `support` agent profile opened from the dialog with the
  captured context, resolve-first triage, `report_issue` as its tool.
- Step 3: `/me/issues` and `/admin/issues` pages, unread badge, comments and
  status flowing back from Linear/Jira/email, `semantic_feedback` folded in
  as `kind='wrong_answer'`, the "shipped in vX" notice.
- Slack/Telegram *intake* commands, screenshot annotation, session replay,
  attachments other than the one screenshot.

## Data model (Postgres-only, A3 ratchet)

Alembic revision `0115_issue_reports` (revises `0114_corpus_chunks_tsv`;
re-check the head before writing). SQLAlchemy models in
`src/models/issue_reports.py`, imported from `src/models/__init__.py`. No
`src/db.py` step, no DuckDB repository.

`issue_reports`:

| column | type | notes |
|---|---|---|
| `id` | text PK | `iss_<16 hex>` |
| `number` | integer, unique, not null | human id (`#42`), from a PG sequence `issue_reports_number_seq` |
| `title` | text, not null | ≤ 200 chars at the API |
| `body` | text, nullable | ≤ 8 000 chars |
| `kind` | text, not null | `bug` · `wrong_answer` · `request` · `question` · `other` |
| `status` | text, not null, default `'open'` | `open` · `resolved` |
| `created_by` | text, not null | **user id** (the internal-table filter keys on `user["id"]`) |
| `created_by_email` | text, nullable | display + webhook text |
| `source_surface` | text, not null | `web` · `cli` · `mcp` |
| `page_url` | text, nullable | ≤ 2 000 chars, sensitive query params stripped client-side (`token`, `access_token`, `code`, `state`) |
| `context_json` | jsonb, nullable | the envelope below |
| `screenshot_path` | text, nullable | path relative to `DATA_DIR`, set by the upload route |
| `created_at`, `updated_at`, `last_activity_at` | timestamptz | `last_activity_at` bumps on comment and resolve |
| `resolved_at`, `resolved_by`, `resolution_note` | | `resolve()` writes these; guarded `WHERE status <> 'resolved'` → second resolver gets 409 |
| `webhook_delivered_at` | timestamptz, nullable | set when the mirror returned 2xx; NULL means "not configured or failed", so the CLI can show it |

Indices: `status`, `created_by`, `number` (unique).

`issue_comments`:

| column | notes |
|---|---|
| `id` | text PK, `isc_<16 hex>` |
| `issue_id` | FK → `issue_reports.id`, on delete cascade |
| `issue_owner_id` | text, not null — denormalized `issue_reports.created_by`, the column the internal-table row filter needs |
| `author_id`, `author_email` | who wrote it |
| `author_kind` | `reporter` · `admin` |
| `body` | text, not null, ≤ 8 000 |
| `created_at` | timestamptz |

Index: `(issue_id, created_at)`, `issue_owner_id`.

Context envelope (`context_json`), all keys optional, client-supplied keys
capped at 300 chars each and the whole document at 32 KB at the API:

```json
{
  "app_version": "0.98.3", "app_commit": "95bc14b",
  "user_agent": "…", "viewport": "1440x900", "language": "en-US",
  "chat_session_id": "…",              // from ?session= in the page URL
  "recent_errors": [                    // last ≤ 20, newest last, from window.AgnesDiag
    {"ts": "…", "kind": "error|rejection|fetch", "message": "…", "source": "…", "status": 500}
  ],
  "request_id": "…",                    // server adds: X-Request-ID of the create call
  "captured_at": "…",
  "doctor": { "…client section of agnes doctor…" }   // CLI --attach-doctor only
}
```

The server adds `app_version`, `app_commit` (`AGNES_COMMIT_SHA`) and
`request_id` itself when the client did not; it never adds client IP.

## Repository

`src/repositories/issue_reports_pg.py::IssueReportsPgRepository`, registered
PG-only as `"issue_reports"` in `_REGISTRY`, factory `issue_reports_repo()`.
Methods, all returning plain dicts:

- `create(*, title, body, kind, created_by, created_by_email, source_surface, page_url, context) -> row`
- `get(issue_id) -> row | None` — accepts either the `iss_` id or a bare
  number string (`"42"`), so every client can address `#42`
- `list_for_user(user_id, *, status=None, limit) -> list[row]` — scoped **in SQL**
- `list_all(*, status=None, limit) -> list[row]` — admin
- `add_comment(issue_id, *, author_id, author_email, author_kind, body) -> row` — bumps `last_activity_at`
- `list_comments(issue_id) -> list[row]`
- `resolve(issue_id, *, resolved_by, resolution_note) -> row | None` — guarded transition
- `set_screenshot(issue_id, path)`, `mark_webhook_delivered(issue_id)`

Rows carry `comment_count` (subquery) so a list can say "2 replies" without a
second round trip.

## REST (`app/api/issues.py`, tag `issues`)

Every route resolves the repo **as a dependency** (`_issues_repo`), so a
DuckDB-backed instance answers the typed `501 requires_postgres_backend`
before body validation (`docs/migrations.md` → "Adding a PG-only feature").

| route | auth | notes |
|---|---|---|
| `POST /api/issues` | any signed-in | JSON `{title, body?, kind?, page_url?, context?}` → 201 row. Audit `issue.report`. Schedules the operator mirror as a `BackgroundTasks` job. |
| `PUT /api/issues/{issue_id}/screenshot` | owner | raw body, `Content-Type: image/png`, ≤ 3 MiB, **PNG magic bytes verified** (`\x89PNG\r\n\x1a\n`), stored at `DATA_DIR/issues/<issue_id>/screenshot.png` with realpath containment. 204. Audit `issue.screenshot`. |
| `GET /api/issues/{issue_id}/screenshot` | owner or admin | `image/png`, `Content-Disposition: inline`, `Content-Security-Policy: frame-ancestors 'self'; object-src 'none'; base-uri 'none'` (the `raw_session_file` posture). 404 when absent. |
| `GET /api/issues/mine?status=&limit=` | any signed-in | `{data, count, truncated?: {limit, total}}`; `limit` clamped to 1..500, default 50. |
| `GET /api/issues/{issue_id}` | owner or admin | row + `comments[]`. |
| `POST /api/issues/{issue_id}/comments` | owner or admin | `{body}` → 201. `author_kind` derived from the caller, never from the body. Audit `issue.comment`. |
| `GET /api/admin/issues?status=&limit=` | admin | same envelope as `/mine`. |
| `POST /api/admin/issues/{issue_id}/resolve` | admin | `{resolution_note?}` → row; 409 `already_resolved`. Audit `issue.resolved`. |

Owner-or-admin is a small helper in this module (`_owned_or_admin`), the
`can_access_collection` idiom: ownership stays here and never leaks into the
generic grant primitives. No new `ResourceType`; an issue is not grantable.

Error vocabulary: `missing_title`, `invalid_kind`, `context_too_large`,
`screenshot_too_large`, `screenshot_not_png`, `already_resolved`,
`issue_not_found`. 404 bodies carry a `hint` naming the next command.

## Operator mirror (`app/services/issue_notifier.py`)

Config: `issues.webhook_url` in `instance.yaml`, env override
`AGNES_ISSUES_WEBHOOK_URL`, resolved exactly like
`app/services/sync_notifier.py::_alert_webhook_url` (env → `get_value` →
`""`). Documented in `config/instance.yaml.example` next to the operator
alerting block and in `docs/issue-reporting.md`. This is the one new config
key in step 1; it exists because the operator asked for the Slack copy, not
speculatively. It is deliberately **not** the alert webhook: user reports
would drown watchdog and sync alerts.

Payload is the lowest-common-denominator `{"text": …}` sent through the
existing `services.telegram_bot.sender.post_webhook` (never raises, 2xx →
`mark_webhook_delivered`). Text:

```
New issue #42 (bug) from analyst@example.com
Tables render as raw markdown while the chat streams
> During streaming I see raw | and --- until the answer completes…
Page: https://<host>/chat?session=35b6…  ·  Version 0.98.3 (95bc14b)  ·  chat session 35b6…
Screenshot: https://<host>/api/issues/iss_…/screenshot  (login required)
Show: agnes admin issue show 42
```

Body excerpt ≤ 300 chars, single line. The public host comes from
`server.public_url` when set, else the request's base URL. The webhook URL is
operator configuration, so it takes the same posture as
`alert_webhook_url` (no SSRF pinning); if a user-supplied URL ever enters
this path it must go through `app/chat/webhook_delivery.py` instead.

Delivery runs in `BackgroundTasks` after the 201; the report is never lost
because the channel is down. Unconfigured webhook → no-op, logged once at
debug.

## Internal tables (`connectors/internal/access.py`)

Append to `INTERNAL_TABLES`:

- `agnes_issues` — `source_table="issue_reports"`, `filter_column="created_by"`,
  `filter_kind="user_id"`, full `column_descriptions` (every column, incl.
  that `context_json` arrives as JSON text and `number` is the human id).
- `agnes_issue_comments` — `source_table="issue_comments"`,
  `filter_column="issue_owner_id"`, `filter_kind="user_id"`.

Both ids go into `PG_ONLY_INTERNAL_TABLE_IDS` (on DuckDB they are simply
not registered). Update `USAGE_PACKAGE_LONG_DESCRIPTION` /
`USAGE_PACKAGE_*_QUESTIONS` / `USAGE_PACKAGE_TAGS` in
`connectors/internal/registry.py` so `tests/test_internal_table_descriptions.py`
sees them ("What issues have I reported that are still open?", "What changed
on my issue #42 since yesterday?"). `tests/db_pg/test_agnes_turns_internal_table_pg.py`
is the template for the PG-side test: a reporter sees only their rows, an
admin sees all, and a comment by an admin on my issue is visible to me.

## Web

**Chrome flag.** `_chrome_ctx` gains `"can_report_issue": use_pg()`
(`src/repositories.use_pg`, read live like `can_data_apps`). A DuckDB
instance shows no button rather than a button that 501s.

**Rail.** `_app_rail.html`: a `.rail-foot` entry
`<button type="button" class="rail-i" id="rail-report-issue">` with a
life-buoy SVG in the house pattern (`viewBox 0 0 24 24`, `stroke="currentColor"
stroke-width="1.6"`, no `title`), label "Report a problem", placed before
"Take {brand} to your tools"; and a user-menu button of the same id family
(`rail-report-issue-menu`) in the "Get your bearings" group. Both gated on
`can_report_issue`. A button that opens a modal does not touch the web-guide
skill sync (routes only), but `docs/issue-reporting.md` is linked from
`docs/README.md`.

**Dialog.** New partial `app/web/templates/_issue_dialog.html`, included
from `base_ds.html` inside the `can_report_issue` gate, and
`app/web/static/js/issue_report.js` loaded from `_app_scripts.html`
(deferred). Markup uses `ds.drawer_field` / `ds.drawer_select` for fields
and the existing modal CSS classes so Escape and focus trapping come from
`modal.js`; page CSS in `head_extra`-equivalent of the partial, `--ds-*`
tokens only, no hex. Contents:

1. Kind segmented control: **Bug** (`bug`) · **Wrong answer** (`wrong_answer`)
   · **Missing** (`request`) · **Unclear** (`question`). Default `bug`.
2. Title (required, ≤ 200), "What happened" textarea (≤ 8 000).
3. "Attached automatically" chips rendered from the envelope: page path,
   version · commit, browser, `chat session <id>` when present, `N recent
   errors` when the ring buffer is non-empty.
4. Checkbox "Include a screenshot of this page" (default on). Label
   underneath, plain: "The screenshot shows this page as you see it now."
5. Buttons: Cancel · **Report** (pill primary). On success:
   `appToast("Reported as #42 — you'll hear back.")`. On 501: the typed
   message from the server. On network failure: "Couldn't send. Copy the
   text below and try again later" with the composed text in a textarea, so
   nothing typed is lost.

Submit flow: `POST /api/issues` with the envelope → on 201, if screenshot
checked, lazy-load `html-to-image` (vendored, see below), render
`document.body` at scale 1 with `useCORS: true`, `canvas.toBlob("image/png")`,
`PUT /api/issues/{id}/screenshot`. A screenshot failure is reported as a
toast but does not fail the report. The dialog closes on the 201.

**Client diagnostics ring buffer.** `app/web/static/js/client_diag.js`,
loaded **non-deferred and first** in `_app_scripts.html` so it sees errors
from later scripts: `window.onerror`, `unhandledrejection`, and a `fetch`
wrapper that records non-2xx responses (`method`, path without query,
status). Keeps the last 20 entries in memory only, messages truncated at
300 chars, nothing sent anywhere until the dialog submits. Exposed as
`window.AgnesDiag.snapshot()`.

**Vendored library.** `html-to-image` 1.11.11 (MIT) at
`app/web/static/vendor/html-to-image.min.js` — not html2canvas: the paper
skin's `color-mix()` colors are reported by `getComputedStyle` as
`color(srgb …)`, which html2canvas 1.4.1 cannot parse, so it failed on every
page in the live check; html-to-image paints through an SVG `<foreignObject>`
and the browser renders the CSS itself. Section appended to
`app/web/static/vendor/LICENSES.md` in the existing format, URL published as
`window._agHtmlToImageUrl` next to `_agMermaidUrl`, loaded lazily on first
use like mermaid. `tests/test_web_static_assets.py` gets a presence + minimum
size test. No CSP change is needed (`security_headers.py` sets no
`script-src`).

**Chat page.** Nothing chat-specific in step 1; the dialog reads
`chat_session_id` from the page URL, which `chat.js` already keeps in sync.

## CLI

`cli/commands/issue.py`, registered `app.add_typer(issue_app, name="issue")`;
admin half in `cli/commands/admin_issue.py`, registered
`admin_app.add_typer(admin_issue_app, name="issue")`. Module docstrings map
every subcommand to its route and state which credential it accepts, in the
`cli/commands/agent.py` style.

| command | route |
|---|---|
| `agnes issue report TITLE [-m/--body TEXT] [--kind bug\|wrong_answer\|request\|question\|other] [--url URL] [--screenshot PATH] [--attach-doctor] [--json]` | `POST /api/issues` (+ `PUT …/screenshot` when `--screenshot`). `--attach-doctor` embeds the **client** section of the `agnes doctor` bundle under `context.doctor`. Prints `Filed #42 (iss_…)` and, when the server reports no webhook delivery, one line saying the record is kept in the instance. |
| `agnes issue list [--status open\|resolved\|all] [--limit N] [--json]` | `GET /api/issues/mine`; columns `#`, kind, status, replies, age, title; `--json` emits `{items, count, truncated}`; truncation disclosed on stdout and in JSON. |
| `agnes issue show ID` | `GET /api/issues/{id}`; comments in order; 404 → hint from `cli/query_hints.py::issue_not_found_hint`. |
| `agnes issue comment ID TEXT` | `POST /api/issues/{id}/comments` |
| `agnes admin issue list [--status] [--limit] [--json]` | `GET /api/admin/issues` |
| `agnes admin issue show ID` | `GET /api/issues/{id}` (admin reads any) |
| `agnes admin issue reply ID TEXT` | `POST /api/issues/{id}/comments` |
| `agnes admin issue resolve ID [--note TEXT]` | `POST /api/admin/issues/{id}/resolve`; 409 → "already resolved by X at T". |

`ID` accepts `42`, `#42` or `iss_…`. A 501 is rendered by the
`_fail_needs_postgres` sentence. No `--scope` flag: the queue is
single-source.

## MCP

Foundation tools (`app/api/mcp/foundation_tools.py`, appended to
`FOUNDATION_TOOL_NAMES` with their REST/CLI siblings in the comment), each
a thin HTTP self-call with `headers_fn()`:

- `report_issue(title, body=None, kind="bug", page_url=None, context=None)` — `read_only=False`
- `list_my_issues(status="open", limit=50)` — `read_only=True`, output through `ensure_output_size`
- `get_issue(issue_id)` — `read_only=True`
- `issue_comment(issue_id, body)` — `read_only=False`
- `issue_queue_list(status="open", limit=100)` — admin, `read_only=True`
- `issue_reply(issue_id, body)` — admin
- `issue_resolve(issue_id, resolution_note=None)` — admin

Docstrings say who may call them, that `list_my_issues` is owner-scoped on
the server, and name the sibling surfaces.

Stdio server (`cli/mcp/server.py`): `report_issue`, `list_my_issues`,
`get_issue`, `issue_comment` with identical names, parameters and defaults,
calling `api_post_json` / `api_get_json`. `STDIO_TOOL_NAMES` in
`tests/test_mcp_tool_parity.py` is extended on purpose. This is what makes
the sandbox agent able to file a report natively; the workspace prompt
(`app/initial_workspace_default/CLAUDE.md`, the "offer to file, never file
silently" rule) is updated to name `report_issue` alongside the semantic
flag, and the builtin-marketplace mirror is kept byte-identical where the
test demands it.

## Audit

`src/audit_events.py` `CATALOG`, new group `# -- issue reports (app/api/issues.py)`:
`issue.report`, `issue.screenshot`, `issue.comment`, `issue.resolved`, all
`mutation`. `src/audit_posture.py`: `POSTURE` entries for the four mutating
routes; `READ_POSTURE` `exempt:ui_support` for the four GETs, inserted
before the `/{full_path:path}` catch-all; `MCP_TOOL_POSTURE` mirrors the
route each tool self-calls (`report_issue` → `issue.report`, list/get →
`exempt:ui_support`, …). Writes go through `log_safe`.

## Guards this change must satisfy

- Triple surface: `_COHORT` entries for the six JSON routes; `_EXEMPT` for
  the two screenshot routes citing the binary-body precedent
  (`_COLLECTIONS_FILES_REASON` / `_LIBRARY_RAW_REASON`).
- `docs/api-reference.md` endpoint inventory lists every new path verbatim.
- `tests/test_cli_api_parity.py`: parity classes for `issue report`,
  `issue comment`, `admin issue resolve`.
- Parity sweeps: `"POST /api/issues"` in the mutation sweep,
  `"GET /api/issues/mine"` and `"GET /api/admin/issues"` in the GET sweep,
  each with a one-line reason; the DuckDB side must answer the typed 501.
- `tests/db_pg/test_alembic_roundtrip.py` + skeleton for revision 0114.
- Internal-table description tests; PG internal-table visibility test.
- Static assets test for the vendored library and its license entry.
- `changelog.d/issue-reporting-step1.md` fragment (`### Added`).
- `scripts/verify_syncmap.py` clean; `--lane impacted` green locally; the
  full suite is CI's on the draft PR.

## Testing beyond the guards

- PG repo tests: create/list scoping/comment/resolve guard/number sequence
  monotonic across two creates/`get("42")` resolution.
- API tests on the PG test fixture: owner cannot read another user's issue
  (404, not 403, so ids do not leak), admin can; non-owner cannot upload a
  screenshot; a JPEG with a `.png` content type is refused; oversize
  context refused with the typed error; webhook body text contains number,
  kind, title, page and screenshot link; webhook unconfigured → no call;
  `webhook_delivered_at` set only on 2xx.
- Template test: rail markup contains the button when `can_report_issue`
  and not otherwise.
- One end-to-end check in a live browser on a Postgres dev stack before the
  PR leaves draft: open the dialog on `/chat?session=…`, submit with a
  screenshot, confirm the row, the PNG on disk, and the webhook text.

## Later steps, for orientation only

- Step 2 adds an agent profile `support` and a `POST /api/issues` caller
  inside it; the dialog grows an "Ask the support agent first" path that
  opens `/chat?session=` with the envelope as the first message.
- Step 3 adds `/me/issues`, `/admin/issues`, an unread badge computed from
  `last_activity_at` vs a per-subscriber `last_read_at`, external-tracker
  callbacks (Linear/Jira webhooks) writing comments and status, and folds
  `semantic_feedback` in as `kind='wrong_answer'` with the old routes kept
  as aliases.
