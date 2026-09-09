# Issue reporting — "Report a problem"

Lets any signed-in user report a problem — a bug, a wrong answer, something
missing, or a question — from wherever they are in Agnes, with the context a
tracker needs (page, session, version, recent client errors, an optional
screenshot) attached automatically instead of re-typed into a Slack thread.

This is step 1 of a three-step effort. Step 1 (this document) is: file,
store, look up your own reports, and mirror a summary to an operator's chat
channel. Later steps add a support agent that tries to help first, dedicated
`/me/issues` and `/admin/issues` web pages, and status/comments flowing back
from an external tracker. See "What comes next" below.

**Requires the Postgres app-state backend.** Issue reports are stored in a
Postgres-only table pair (`issue_reports`, `issue_comments` — the A3
PG-first ratchet described in [`migrations.md`](migrations.md)). On a
DuckDB-backed instance the "Report a problem" button and menu item do not
render at all — there is no button that would answer a 501.

## What it does

Click **Report a problem** — the button at the foot of the rail, or the same
entry in the account (avatar) menu — and a dialog opens with:

- A **kind** picker: Bug · Wrong answer · Missing · Unclear.
- A **title** (required, one line) and an optional **"What happened"**
  description.
- A row of chips showing exactly what will be **attached automatically**:
  the current page path, the app version, your browser, the chat session id
  (when the page URL carries `?session=…`), and a count of recent client
  errors, when there are any.
- A checkbox, **on by default**, to **include a screenshot** of the page as
  you see it right now.

Nothing is sent until you click **Report**. On success you see a toast
naming the issue number ("Reported as #42 — you'll hear back."); the dialog
closes and clears itself for next time.

## What is attached, and what is not

The context envelope sent with every report:

| Key | Source | Notes |
|---|---|---|
| `page_url` | the page you were on | sensitive query parameters (`token`, `access_token`, `code`, `state`) are stripped **client-side** before the report is ever sent |
| `app_version`, `app_commit` | the server | the server fills these in from its own build info if the client didn't send them |
| `user_agent`, `viewport`, `language` | the browser | standard `navigator`/`window` values |
| `chat_session_id` | the page URL's `?session=` | omitted when you weren't on a chat page |
| `recent_errors` | a client-side ring buffer (`client_diag.js`) | the **last 20** JS errors, unhandled promise rejections, and failed (non-2xx) `fetch()` calls seen on the page **since it loaded** — kept in memory only, capped at 300 characters per message, and never sent anywhere until you click Report |
| `request_id` | the server | the request id of the create call, for cross-referencing server logs |
| `captured_at` | the browser | timestamp of when the dialog was submitted |

**Privacy.** The report body, the context envelope, and the screenshot (when
attached) stay in the instance — they are not sent to any third party. The
optional operator webhook mirror (below) carries only a short text summary:
issue number, kind, title, an excerpt of the body, the page URL, the app
version, and a link to the screenshot (which still requires login to view) —
never the full body, the raw context JSON, or the screenshot image itself.

The screenshot, when included, is a same-page render of `document.body`
captured client-side by the vendored [html2canvas](https://html2canvas.hertzen.com/)
(MIT license, `app/web/static/vendor/html2canvas.min.js`) — it never crosses
an iframe boundary or captures anything outside the current tab, and the
dialog itself is hidden for the moment of capture so the screenshot never
shows the report form asking for a screenshot. The library is fetched lazily,
only the first time a report is filed with the screenshot checkbox on — a
caller who never attaches one never downloads it.

## Looking up your own reports

```bash
agnes issue report "Tables render raw markdown while streaming" \
    --kind bug --body "During streaming I see raw | and --- until the answer completes." \
    [--url URL] [--screenshot PATH] [--attach-doctor] [--json]

agnes issue list [--status open|resolved|all] [--limit N] [--json]
agnes issue show ID                 # ID accepts 42, #42, or iss_...
agnes issue comment ID "some text"
```

Any agent (the sandbox's own assistant, or an agent profile) can also answer
"what have I reported?" through the internal-table mechanism, no different
from any other Agnes data:

```sql
SELECT number, title, status, last_activity_at FROM agnes_issues WHERE status = 'open';
SELECT body FROM agnes_issue_comments WHERE issue_id = 'iss_...';
```

`agnes_issues` and `agnes_issue_comments` are row-filtered per caller — a
reporter sees only the issues they filed and every comment (including an
admin's reply) on those issues; an admin sees every row. Server-side only
(Postgres backend); not synced by `agnes pull`.

## The admin queue

Step 1 ships a CLI/MCP-only admin queue — no dedicated `/admin/issues` web
page yet (see "What comes next"):

```bash
agnes admin issue list [--status open|resolved|all] [--limit N] [--json]
agnes admin issue show ID
agnes admin issue reply ID "some text"
agnes admin issue resolve ID [--note "fixed in 0.99.0"]
```

## MCP tools

Available on both the HTTP foundation server and the stdio server the
sandbox agent runs: `report_issue`, `list_my_issues`, `get_issue`,
`issue_comment` — and, admin-only, `issue_queue_list`, `issue_reply`,
`issue_resolve`.

## Mirroring reports to your team's chat

Configure an incoming webhook URL that accepts `{"text": "..."}` (Slack,
Mattermost, Google Chat, Discord, or anything with that shape) and every
filed report posts a one-message summary there — issue number, kind, title,
a short excerpt, the page and version, a login-gated link to the screenshot
when there is one, and the CLI command to look it up.

```yaml
# config/instance.yaml
issues:
  webhook_url: "https://hooks.example.com/services/..."
```

Env override: `AGNES_ISSUES_WEBHOOK_URL` (wins over the config value). The
record in the instance is always the source of truth — the report is stored
whether or not a webhook is configured, and whether or not delivery
succeeds; the webhook is a copy, delivered as a background task after the
report is filed so a down channel never blocks or loses a report. Leave
`webhook_url` empty (the default) to keep reports in the instance only.

## What comes next

Named here so nobody accidentally re-designs them as part of step 1:

- **Step 2** — a `support` agent profile, opened from the dialog with the
  captured context already loaded, that tries to help before a human has
  to; the dialog grows an "Ask the support agent first" path.
- **Step 3** — dedicated `/me/issues` and `/admin/issues` web pages, an
  unread badge, comments and status flowing back from an external tracker
  (e.g. Linear or Jira) via webhook, and the existing `semantic_feedback`
  ("that answer looked wrong") mechanism folded in as `kind='wrong_answer'`
  with its old routes kept as aliases.

## See also

- [`RBAC.md`](RBAC.md) — access control model (issue ownership is a simple
  owner-or-admin check, not a grantable resource type)
- [`migrations.md`](migrations.md) — the A3 PG-first ratchet that makes this
  a Postgres-only feature
- [`observability.md`](observability.md) — audit posture for the `issue.*`
  actions this feature writes
