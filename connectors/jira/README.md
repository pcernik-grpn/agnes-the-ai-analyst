# Jira Integration

Real-time sync of Jira support tickets for AI-powered analysis.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           JIRA CLOUD                                        │
│                    (your-org.atlassian.net)                                 │
│                                                                             │
│  Issue created/updated/deleted  ───►  Webhook POST                          │
│  Comment added/updated          ───►  with HMAC signature                   │
│  Attachment uploaded            ───►                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        DATA BROKER SERVER                                   │
│                    (your-instance.example.com)                              │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Flask Webapp (/webhooks/jira)                                      │   │
│  │                                                                     │   │
│  │  1. Verify HMAC-SHA256 signature                                    │   │
│  │  2. Log raw webhook event                                           │   │
│  │  3. Extract issue key from payload                                  │   │
│  │  4. Fetch complete issue data via Jira REST API                     │   │
│  │  5. Overlay SLA fields via JSM service account (cloud API)          │   │
│  │  6. Save issue JSON to disk                                         │   │
│  │  7. Download all attachments                                        │   │
│  │  8. Trigger incremental Parquet transform                           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  /data/src_data/raw/jira/                                           │   │
│  │  ├── issues/              # Raw JSON per issue                      │   │
│  │  │   ├── SUPPORT-15186.json                                         │   │
│  │  │   └── SUPPORT-15190.json                                         │   │
│  │  ├── attachments/         # Downloaded files                        │   │
│  │  │   └── SUPPORT-15190/                                             │   │
│  │  │       └── 56340_image.png                                        │   │
│  │  └── webhook_events/      # Audit log                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  incremental_jira_transform.py (called automatically)               │   │
│  │                                                                     │   │
│  │  • Load saved issue JSON                                            │   │
│  │  • Extract fields, convert ADF to plain text                        │   │
│  │  • Upsert into monthly Parquet (only affected month)                │   │
│  │  • Copy to distribution directory                                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  /data/src_data/parquet/jira/    (monthly partitioned)              │   │
│  │  ├── issues/              # 49 columns, clean schema                │   │
│  │  │   ├── 2025-01.parquet                                            │   │
│  │  │   └── 2025-02.parquet                                            │   │
│  │  ├── comments/            # Extracted comment text                  │   │
│  │  ├── attachments/         # Metadata + local paths                  │   │
│  │  ├── changelog/           # Field change history                    │   │
│  │  ├── issuelinks/          # Links between issues                    │   │
│  │  └── remote_links/        # External links (Confluence, Slack)      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ rsync
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ANALYST MACHINE                                     │
│                                                                             │
│  ~/data-analysis/                                                           │
│  └── server/                                                                │
│      └── parquet/                                                           │
│          └── jira/           # Synced Parquet + attachments                 │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Claude Code + DuckDB                                               │   │
│  │                                                                     │   │
│  │  -- Query all months with glob pattern                              │   │
│  │  SELECT * FROM 'server/parquet/jira/issues/*.parquet'               │   │
│  │  WHERE severity LIKE '%Medium%';                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Jira Webhook Configuration

**Location:** https://your-org.atlassian.net/plugins/servlet/webhooks

| Setting | Value |
|---------|-------|
| URL | `https://your-instance.example.com/webhooks/jira` |
| Secret | Same as `JIRA_WEBHOOK_SECRET` in server `.env` |
| JQL Filter | `project = "Your Project"` |

**Subscribed Events:**
- Issue: created, updated, deleted
- Comment: created, updated
- Attachment: created
- Issue link: created

Every event is handled by refetching the issue from the API, so the body's own
contents matter only when that refetch fails. Two facts about those bodies are
worth knowing when reading the fallback path in `service.py`:

- Since **January 2018**, Cloud `jira:issue_*` bodies carry **no comment objects
  at all**. Only `comment_created` / `comment_updated` embed an issue, and
  without `expand`. So the fallback's comment update is confined to those two
  events even before any completeness check runs.
- The embedded body carries no `changelog` key either. That is why
  `transform_changelog` returns `None` for an absent key and the incremental
  transform preserves the issue's stored history instead of wiping it.

Only an ISSUE deletion (`jira:issue_deleted` / `issue_deleted`) removes stored
rows. A sub-entity deletion — `comment_deleted`, `attachment_deleted`,
`worklog_deleted` — is handled as an ordinary content change: the issue is
refetched, which is what drops the deleted comment from the stored thread. Until
0.83.88 these were matched by a `"deleted" in webhookEvent` substring and each
one tombstoned the *whole* issue.

### 2. Webhook Receiver

**File:** `connectors/jira/webhook.py`

Flask blueprint that handles incoming webhooks:

```python
@jira_bp.route("/jira", methods=["POST"])
def receive_jira_webhook():
    # 1. Verify HMAC signature
    # 2. Parse JSON payload
    # 3. Log event to webhook_events/
    # 4. Call jira_service.process_webhook_event()
```

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhooks/jira` | POST | Receive webhooks from Jira |
| `/webhooks/jira/health` | GET | Health check, shows config status |
| `/webhooks/jira/test` | POST | Manual issue fetch (debug mode only) |

### 3. Jira Service

**File:** `connectors/jira/service.py`

Handles Jira API communication and data persistence:

```python
class JiraService:
    def fetch_issue(issue_key) -> dict
        # GET /rest/api/3/issue/{key}?expand=renderedFields,changelog&fields=*all

    def fetch_refresh_fields(issue_key) -> dict | None
        # GET the configured JIRA_REFRESH_FIELDS with the primary token
        # (domain URL, or api.atlassian.com gateway when JIRA_CLOUD_ID is set)

    def save_issue(issue_data) -> Path
        # 1. Fetch remote links
        # 2. Overlay SLA fields from service account
        # 3. Save to /data/src_data/raw/jira/issues/{key}.json
        # 4. Download attachments

    def download_attachment(attachment, issue_key) -> Path
        # GET attachment content URL with auth
        # Save to attachments/{issue_key}/{id}_{filename}
```

**Why fetch after webhook?**
- Webhook payload contains minimal data
- Full issue data requires API call with `fields=*all`
- Ensures we have complete, consistent data

**Why two API tokens?**
- Personal token fetches all fields except SLA (lacks JSM Agent licence)
- JSM service account token fetches SLA fields via Atlassian Cloud API
- SLA data is overlayed into the issue JSON before saving

### 4. Data Transformation

Two transformation modes are available:

#### 4a. Incremental Transform (Real-Time)

**File:** `connectors/jira/incremental_transform.py`

Called automatically by webhook handler after saving issue JSON and attachments. Updates only the affected monthly Parquet file.

```python
# Called from jira_service.py after save_issue()
from connectors.jira.incremental_transform import transform_single_issue

transform_single_issue(
    issue_key="SUPPORT-1234",
    deleted=False,  # or True for deletion events
)
```

**How it works:**
1. Loads the saved JSON for the issue
2. Determines the month from `created_at` date
3. Loads existing Parquet for that month (if any)
4. Upserts issue data (removes old, adds new)
5. Saves updated Parquet
6. Copies to distribution directory for rsync

**Benefits:**
- Data available within seconds of Jira change
- Only updates one monthly file (~50-100KB)
- Rsync transfers only changed files

#### 4b. Batch Transform (Initial Load / Recovery)

**File:** `connectors/jira/transform.py`

Used for initial historical load or to rebuild all Parquet from raw JSON.

```bash
python -m connectors.jira.transform \
    --raw-dir /data/src_data/raw/jira \
    --output-dir /data/src_data/parquet/jira \
    --attachments-dir /data/src_data/raw/jira/attachments
```

> **Repair caveat:** the rebuild skips issues marked `_deleted_at` and only
> writes months that still have at least one live issue. A month whose issues
> have since ALL been deleted is left untouched (a per-month WARNING is
> logged) — if you are repairing a corrupt partition in such a month, remove
> the file instead: every row it held belongs to a deleted issue.

**Common transformations (both modes):**
- Extracts plain text from ADF (Atlassian Document Format), inlining link URLs
  into the text — see [ADF link URLs](#adf-link-urls)
- Maps custom field IDs to human-readable names
- Normalizes nested structures into flat tables
- Links attachments to local file paths
- Enforces explicit PyArrow schema for consistent types across months

#### ADF link URLs

`extract_text_from_adf` feeds three stored columns — `comments.body`,
`issues.description` and `issues.context` — and two ADF constructs keep their
target *outside* any text node, so a text-node walk lost both:

| construct | where the URL lives | rendered as |
|---|---|---|
| `inlineCard` / `blockCard` / `embedCard` (smart link) | `attrs.url` (or `attrs.data.url` / `attrs.data["@id"]` on a resolved blockCard) — **no text child at all** | the bare URL, as a token in the sentence flow |
| `text` node with a `link` mark | `marks[].attrs.href` — only the anchor text is a text node | `anchor text (href)` |

Hrefs are emitted verbatim, `mailto:` scheme included, **except that a
credential-bearing parameter value is replaced with `REDACTED`**. Jira hands out
URLs carrying bearer credentials in the query string (a Service Desk
`unsubscribe?jwt=…` link is a signed token authorising an action), and these
columns are distributed to analyst laptops as parquet and read by agents — so
the URL is kept whole and identifiable and only the values go. Path, scheme,
ordering, separators and non-secret parameters are untouched, and the fragment
is covered as well as the query, because OAuth's implicit flow puts the token
after the `#`.

Parameter names are matched in two tiers, because over-redacting is the same
bug class as the loss this renderer exists to fix:

| tier | names | covers |
|---|---|---|
| separator-insensitive substring | `token`, `jwt`, `secret`, `password`, `passwd`, `signature`, `credential`, `apikey` | `access_token`, `refresh_token`, `mfaManagementToken`, `api_key`, `api-key` |
| whole name only | `tok`, `sig`, `key`, `pin`, `auth`, `code`, `sdata` | as substrings these would fire on `design`, `author`, `keyword` |

Two boundaries worth knowing. The autolink comparison runs on the **raw** href:
redacting first would make a tokened URL stop matching its own anchor text, and
the labelled form would then print the credential it had just removed. And
anchor text is prose, never rewritten — an author documenting
`?token=<project-token>` keeps it, exactly as before this change. Only URLs the
renderer itself emits are redacted.

The parenthesised copy is suppressed where the anchor text already *is* the
target: an href differing only by a trailing slash emits the href, and one that
is the anchor text plus a scheme Jira's autolinker inferred emits the anchor
text — Jira links a bare `SKILL.md` to `http://SKILL.md`, and emitting that href
would rewrite an authored word into a URL that never existed.

A fragment that renders to nothing — a `media` node, a `hardBreak`, the
space-only text node Jira appends after an inline card — is dropped rather than
joined, so it leaves no gap in the sentence. Only fragment *edges* are
normalised: a `codeBlock`'s newlines and indentation are content and survive
verbatim, as does a run of spaces an author typed.

Two things this does **not** cover:

- `changelog.from_value` / `to_value` never touch this renderer. They are Jira's
  own wiki-markup strings from `fromString` / `toString`.
- **Rows written before this landed keep their lossy text**, and keep any
  credential a pre-fix walk had already stored from a pasted tokened URL.
  Re-transforming them is a separate operation over the cached raw JSON — no
  Jira traffic needed — and it applies the redaction above as it goes. Table
  structure (cell and row boundaries collapse to whitespace) and `media` alt
  text are likewise still flattened away.

### 5. Data Distribution

Analysts sync data via rsync (same as other data):

```bash
bash server/scripts/sync_data.sh
```

This syncs:
- `server/parquet/jira/` - Parquet tables (issues, comments, attachments metadata, changelog, issuelinks, remote_links, organizations)

For attachment files, see [Attachment Access](#attachment-access) section below.

## Data Flow Timeline (Real-Time)

```
T+0ms    Jira: Issue updated
T+50ms   Jira: Webhook POST to our server
T+100ms  Server: Verify signature, log event
T+150ms  Server: GET /rest/api/3/issue/{key} from Jira API
T+400ms  Server: GET SLA fields via JSM service account (cloud API)
T+500ms  Server: Save JSON (with SLA overlay) to raw/jira/issues/
T+600ms  Server: Download attachments (parallel)
T+800ms  Server: Incremental transform → update monthly Parquet
T+900ms  Server: Copy to distribution directory
T+1000ms Server: Return 200 OK to Jira

(analyst sync - any time)
T+Xsec   Analyst: bash sync_data.sh
T+Xsec   Analyst: rsync downloads only changed monthly file (~50KB)
T+Xsec   Analyst: Query with DuckDB - sees latest data
```

**Key improvement:** Incremental transform runs immediately after webhook processing, so data is available for sync within seconds of the Jira change.

## Configuration

### Server Environment Variables

In `<install-dir>/.env` (typically the directory you run `docker compose` from):

```bash
# Jira webhook integration (single token)
JIRA_WEBHOOK_SECRET=<random 64-char hex string>
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token from Atlassian; the account needs a JSM Agent licence for SLA>

# Custom fields to refresh onto tickets — generic, no defaults (per instance).
# field_id or field_id:column, comma-separated. Discover with:
#   python -m connectors.jira.scripts.verify_sla_access --list-fields
# Each becomes a JSON column on `issues` named by the alias (or field id). A column
# that would collide with a built-in issues column (e.g. `resolution`, `status`) is
# prefixed with `cf_` so the built-in is never overwritten.
JIRA_REFRESH_FIELDS=customfield_10328:first_response,customfield_10161:time_to_resolution

# Optional: set ONLY for a scoped API token (forces the api.atlassian.com
# gateway). Classic tokens use the site domain URL and need nothing here.
JIRA_CLOUD_ID=
```

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `JIRA_WEBHOOK_SECRET` | HMAC secret for webhook verification |
| `JIRA_DOMAIN` | Jira Cloud domain |
| `JIRA_EMAIL` | Email for API authentication |
| `JIRA_API_TOKEN` | Primary API token (account needs a JSM Agent licence for SLA) |
| `JIRA_REFRESH_FIELDS` | Custom fields to refresh onto tickets (field_id or field_id:column) |
| `JIRA_CLOUD_ID` | Optional; set only for a scoped API token |

### Getting Jira API Token

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Name it (e.g., "Data Analyst Integration")
4. Copy token to `JIRA_API_TOKEN`

**⚠️ IMPORTANT: API tokens expire after 365 days maximum (Atlassian limitation).**

Set a calendar reminder to rotate the token before expiration. When rotating:
1. Create new token in Atlassian
2. Update `JIRA_API_TOKEN` in GitHub Secrets and server `.env`
3. Restart webapp: `sudo systemctl restart webapp`
4. Test: `curl https://your-instance.example.com/webhooks/jira/health`

## Directory Structure

```
/data/src_data/
├── raw/
│   └── jira/                      # Raw data from webhooks
│       ├── issues/                # One JSON file per issue
│       │   ├── SUPPORT-15186.json
│       │   ├── SUPPORT-15189.json
│       │   └── SUPPORT-15190.json
│       ├── attachments/           # Downloaded files (by issue key)
│       │   ├── SUPPORT-15189/
│       │   │   ├── 56337_image.png
│       │   │   └── 56338_image-20260203-110549.png
│       │   └── SUPPORT-15190/
│       │       └── 56340_image.png
│       └── webhook_events/        # Audit log of all webhooks
│           ├── 20260203_105203_jira_issue_updated.json
│           └── 20260203_110457_comment_created.json
│
└── parquet/
    └── jira/                      # Transformed data (monthly partitioned)
        ├── issues/                # Main issues table
        │   ├── 2025-01.parquet
        │   ├── 2025-02.parquet
        │   └── ...
        ├── comments/              # Issue comments
        │   └── YYYY-MM.parquet
        ├── attachments/           # Attachment metadata
        │   └── YYYY-MM.parquet
        ├── changelog/             # Field change history
        │   └── YYYY-MM.parquet
        ├── issuelinks/            # Links between issues
        │   └── YYYY-MM.parquet
        └── remote_links/          # External links (Confluence, Slack, etc.)
            └── YYYY-MM.parquet
```

**Monthly Partitioning Benefits:**
- Efficient rsync: only changed months are transferred
- Better performance: smaller files for ~15,000 total tickets
- Incremental updates: new months don't rewrite old data

## Monitoring

### Health Check

```bash
curl https://your-instance.example.com/webhooks/jira/health
```

Response:
```json
{
  "status": "ok",
  "configured": true,
  "webhook_secret_set": true,
  "jira_domain": "your-org.atlassian.net"
}
```

### Logs

```bash
# Webapp logs (webhook processing)
docker compose logs app --tail 200 | grep -i jira

# Recent webhook events
ls -lt /data/src_data/raw/jira/webhook_events/ | head -20

# Issue count
ls /data/src_data/raw/jira/issues/ | wc -l

# Attachment count
find /data/src_data/raw/jira/attachments/ -type f | wc -l
```

## Security

| Layer | Protection |
|-------|------------|
| Webhook | HMAC-SHA256 signature verification |
| API Auth | HTTP Basic Auth (email + API token) |
| Storage | Server directories with `data-ops` group permissions |
| Transport | HTTPS only (Let's Encrypt certificate) |

**Webhook Signature Verification:**
```python
expected = hmac.new(
    secret.encode('utf-8'),
    request.get_data(),
    hashlib.sha256
).hexdigest()

if not hmac.compare_digest(signature, expected):
    abort(401)
```

## Troubleshooting

### Webhook not received

1. Check Jira webhook is enabled and URL is correct
2. Verify JQL filter matches the issue's project
3. Check server firewall allows HTTPS from Atlassian IPs

### Signature verification fails

1. Verify `JIRA_WEBHOOK_SECRET` matches in both Jira and server `.env`
2. Check for trailing whitespace in secret
3. Restart webapp after changing `.env`

### Attachments not downloading

1. Check `JIRA_API_TOKEN` is valid
2. Verify API token has read access to attachments
3. Check disk space on `/data` partition
4. Large attachments (>50MB) are skipped by design

### Missing data in Parquet

1. Run transformation manually:
   ```bash
   python -m connectors.jira.transform \
       --raw-dir /data/src_data/raw/jira \
       --output-dir /data/src_data/parquet/jira \
       --attachments-dir /data/src_data/raw/jira/attachments
   ```
2. Check for errors in transformation output
3. Verify raw JSON files exist in `raw/jira/issues/`
4. Note: Output files are partitioned by month (e.g., `issues/2026-01.parquet`)

### Consistency check: what a non-zero exit now means (operators)

`jira-consistency.service` runs `consistency_check` every 30 minutes and exits
non-zero when `alert_level == "ERROR"` **or** `status != "success"`. Two inputs
to that were widened in #1363, so a unit that used to go green can now go red on
a class of run it previously stayed silent about:

- **`transform_failed`** — a per-issue re-transform that failed or hit its 120 s
  subprocess timeout. It was recorded in the report body but reached neither
  `status` nor `alert_level`, so a run that fixed nothing still reported
  `success`/`INFO`. It now flips `status` to `partial_success`, matching what
  `backfill_failed` already did on the JSON side. **A single flaky issue is
  enough**, so alerting keyed on the unit's exit code will see transient
  failures it did not see before.
- **`parquet_read_failed`** — a Parquet part that could not be read at all.
  This forces `alert_level` to `ERROR`, and it should page: an unreadable part
  is what turned this job into an unbounded re-transform loop in the first
  place.

If per-issue transform flakiness is noisy in your deployment, alert on the
report's `alert_level` and `discrepancies.parquet_read_failed` rather than on
the exit code — the exit code deliberately cannot distinguish "one issue failed
to transform" from "the corpus is unreadable".

There is one more way this unit can now sit red, and it is not a bug:

- **An instance that has raw JSON but has never been transformed to Parquet.**
  Every issue is genuinely missing on the Parquet side, which is over
  `AUTO_FIX_THRESHOLD` (20), so the checker refuses to fix it one 120-second
  subprocess at a time and reports `manual review required` at `ERROR` — the
  same response `missing_in_json` has always given to a gap that size. The fix
  is to run the batch transform once (see *Historical Backfill* below); the
  unit goes green on the next run. Before #1363 this state auto-"fixed" itself
  by shelling out once per issue for the whole corpus, which is precisely the
  churn that issue was filed about.

Note the deliberate non-case: if DuckDB is unavailable the Parquet scan cannot
run at all, and the checker reports `parquet_read_failed` with a
`<parquet-scan-unavailable: …>` marker instead of claiming the corpus is
missing. A run that never looked must not name issue keys as missing — install
DuckDB and the check resumes.

## Schema Reference

The Jira tables and their columns are described in [`docs/DATA_SOURCES.md`](../../docs/DATA_SOURCES.md). At runtime, inspect the live schema with `agnes schema <table>` and `agnes describe <table>`.

### `comments.public_visibility` (BOOLEAN, nullable)

Separates customer-facing replies from internal agent notes:

| Value | Meaning |
|---|---|
| `true` | Visible to the customer — an agent reply, or the customer's own portal/email reply |
| `false` | Internal note, visible only to agents |
| `NULL` | **Unknown.** The payload carried no JSON-boolean `jsdPublic` (absent, explicit null, or mistyped), or the row was written before this column existed and has not been re-transformed. |

The value comes from **`jsdPublic`**, the Jira platform API's documented
read-only projection of the state JSM stores in the `sd.public.comment` entity
property. `jsdPublic` rides along on the comments embedded in a plain
`GET /issue/{key}` — no `expand`, no extra request per issue — so the column
costs no additional Jira traffic. It is present as a JSON boolean on every
comment observed live: a project-wide sweep of each issue's newest-20 comment
window (112,859 comments, 2022-2026 — that window is what
`search/jql?fields=comment` actually embeds per issue) plus full page-throughs
of the longest threads covering the older tails the sweep cannot see (697/697,
including 2022-era thread heads), with zero string-typed and zero
explicit-null values anywhere.

The entity property is deliberately not read. Doing so would require
`expand=properties`, and any payload carrying the property carries `jsdPublic`
too, so a property fallback would be unreachable rather than merely unused.
Anyone adding `expand=properties` later should note that `value.internal` is
not consistently typed — the same instance stores both a JSON boolean and the
string `"false"` — so it must be coerced by content; a plain `bool()` reads any
non-empty string as truthy and would flip a public comment to internal. The
same strictness already applies to `jsdPublic` itself: only a JSON boolean is
trusted — any other type resolves to `NULL` and is counted, never
`bool()`-coerced.

**`NULL` is never coerced to `true`.** A missing flag is counted and logged as a
WARNING naming the issue key, not defaulted. This is deliberate: a boolean that
is confidently wrong is worse than one that admits the gap, because nothing
downstream can distinguish a defaulted value from an observed one. Queries that
need a hard split should say which side they want the unknowns on:

```sql
-- internal notes only, unknowns excluded
SELECT count(*) FROM comments WHERE public_visibility IS FALSE;

-- audit the gap
SELECT strftime(created_at, '%Y-%m') AS month, count(*) AS unknown
FROM comments WHERE public_visibility IS NULL GROUP BY 1 ORDER BY 1;
```

#### What `NULL` means, per deployment kind

`jsdPublic` is a **platform** field on Jira Cloud, not a JSM-licensed one.
Licensing gates the *value*, never the key's presence — Atlassian's v2/v3
OpenAPI schema documents it as defaulting to `true` "when the site doesn't use
Jira Service Desk", and that was confirmed live in August 2026 against two
JSM-unlicensed public Cloud instances, which serialized a boolean on every
comment sampled (including thread heads from 2003 and 2006).

| Deployment | `public_visibility` behaviour |
|---|---|
| Jira Cloud, JSM licensed | `true`/`false` as observed. `NULL` means: a mistyped or explicitly-null flag, a row written before this column existed and not yet re-transformed, or a comment version written from a flagless webhook-fallback embed and not yet refetched. |
| Jira Cloud, no JSM license | The flag is still serialized on every comment; values are uniformly `true` (the platform default — the column is well-defined but there is no internal/public distinction to make). `NULL` as above, and rare. |
| Jira Data Center / Server | Out of scope by construction. The field does not exist there, and neither does the `/rest/api/3` surface this connector hardcodes — a DC-pointed install fails every fetch long before this column matters. |

#### Carry-forward: an unflagged comment never nulls an observed value

The incremental comments write is an issue-scoped delete-then-insert, so a
comment arriving without a boolean `jsdPublic` would not merely fail to add
information — it would REPLACE a stored value with `NULL`. The write layer
(`incremental_transform._carry_forward_public_visibility`) therefore fills a
`NULL` from the stored row when, and only when, that row is **the same comment
version**: same `(issue_key, comment_id)` and an identical `updated_at`. An
incoming boolean always wins; a differing `updated_at` means the comment was
edited — and a visibility flip rides exactly such an edit — so the value stays
honestly `NULL` until the next successful refetch rather than serving a possibly
pre-flip boolean as observed. Each incremental write logs how many values it
carried and how many stayed `NULL`, so a silent never-carry regression is
visible in the logs rather than only in the data.

**Durability caveat.** Carry-forward is a *parquet-layer* repair: it fills the
row being written, and does not write anything back into the cached raw JSON. If
a comment version was saved from a flagless webhook-fallback embed, a full batch
re-transform (4b) rebuilds that month from the raw JSON and re-`NULL`s those
rows, because the flag is genuinely not in the cache. Refetch the affected issues
first (see *Historical Backfill*) if you need a rebuild to keep them.

**Nothing schedules that refetch for you.** Until 0.83.88 a flagless embed failed
the completeness gate, which marked the issue `_comments_incomplete` and wrote
the `.incomplete` sidecar, so the next `--skip-existing` backfill refetched it
and healed the raw cache. That heal came bundled with the cost this release
removes: the issue's *entire* comment update was deferred until that refetch
happened. Reusing the marker would reinstate the deferral, so it is deliberately
not set for a flag gap. In practice the cache heals on ordinary activity anyway —
any later successful refetch of the issue (its next webhook event, an SLA poll
while it is open, a consistency-check repair) rewrites the JSON with the flag
present. It persists only for an issue that goes quiet immediately afterwards.
The signal that it happened is the per-issue WARNING naming the issue key and the
count; treat a rebuild of a month containing one as "refetch those keys first".

Rows written before this column existed read as `NULL` (the extract views use
`union_by_name=true`, so adding the column is non-breaking). To fill them in,
re-run the batch transform (4b above) — the flag is already present in the
cached raw JSON, so this is a pure re-transform with no Jira traffic. Verify the
result by charting the internal share per month: a cliff at any date means the
backfill defaulted rows rather than reading them. Do not run the backfill until
the release carrying this column has survived the post-merge smoke gate: a
rollback to pre-column code silently strips `public_visibility` from any month
partition its webhook/poll writes rewrite (the old schema projection drops
unknown columns), which would quietly undo the backfill month by month.

## Historical Backfill

For initial setup or recovery, use the backfill script to download all historical issues.

**File:** `connectors/jira/scripts/backfill.py`

```bash
# Download all SUPPORT tickets (idempotent, skips existing)
python -m connectors.jira.scripts.backfill --parallel 4

# Environment variables required:
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token>
JIRA_DATA_DIR=/data/src_data/raw/jira  # optional, default path
```

**Features:**
- Uses new Jira Cloud API (`POST /rest/api/3/search/jql` with `nextPageToken`)
- Parallel downloads (configurable workers)
- Downloads all attachments
- Idempotent - skips already downloaded issues
- Handles rate limiting gracefully

**Field backfill** (separate script, primary token):

**File:** `connectors/jira/scripts/backfill_sla.py`

```bash
# Fetch the configured JIRA_REFRESH_FIELDS for all issues
python -m connectors.jira.scripts.backfill_sla --parallel 8

# Dry run (count files needing update):
python -m connectors.jira.scripts.backfill_sla --dry-run
```

The configured fields (`JIRA_REFRESH_FIELDS`, no defaults) are ordinary issue
custom fields, fetched with the primary token and embedded into existing raw JSON
files — the site domain URL, or the `api.atlassian.com` gateway when
`JIRA_CLOUD_ID` is set (scoped token). The account needs whatever read permission
each field requires (e.g. a JSM Agent licence for SLA fields). Discover field ids
via `verify_sla_access --list-fields`.

**After backfill, run batch transform:**
```bash
python -m connectors.jira.transform \
    --raw-dir /data/src_data/raw/jira \
    --output-dir /data/src_data/parquet/jira \
    --attachments-dir /data/src_data/raw/jira/attachments

# Copy to distribution directory
cp -r /data/src_data/parquet/jira/* ~/server/parquet/jira/

# The organizations dimension is not produced by the batch transform — the daily
# refresh writes it to the extract tree. Copy it too, or analysts on the legacy
# path get no jira_organizations view (sync_jira.sh skips an empty glob):
cp -r /data/extracts/jira/data/organizations ~/server/parquet/jira/
```

## Organizations Table (Daily Refresh)

Tickets carry organization **ids** in `issues.organization_ids` (a JSON array — the Jira field is multi-valued). The `organizations` table resolves those ids to the organization's current name plus one column per organization detail field named in `JIRA_ORG_DETAIL_FIELDS`.

**File:** `connectors/jira/organizations.py` · **Job kind:** `jira-org-refresh` · **Cadence:** `daily 05:00`

**Why ids and not names.** `issues.organizations` holds names captured at ingest. Rename an organization and its existing tickets keep the old name, so a name join silently splits one customer into several. Ids do not change on rename.

**Why a table and not a column on `issues`.** A detail value belongs to the organization, not the ticket. Denormalizing it would freeze a copy per ticket, so correcting a detail later would leave all history wrong until a full re-transform. One row here fixes every ticket retroactively.

**Configuration** (no defaults — detail ids are per-instance):

```bash
JIRA_ORG_DETAIL_FIELDS=38:crm_account_id,41:region
```

Each key is matched against a detail's `id` first, then its `name`. Prefer the id: it survives a rename of the detail field. An alias colliding with a built-in column (`org_id`, `name`) is prefixed `detail_`, as is an entry with no usable alias.

**Discovering detail ids.** Only the per-organization endpoint returns them:

```
GET https://api.atlassian.com/jsm/csm/cloudid/{cloudId}/api/v1/organization/{orgId}
  -> {"id": "...", "name": "...", "details": [{"id": "38", "name": "CRM ID", "values": ["..."]}]}
```

`GET /organization/details` and `POST /organization/profile/fetch` return detail **names only**, which is why the refresh makes one request per organization instead of batching 25 at a time — batching would force matching on the label. At a daily cadence the extra requests do not matter. Enumeration uses `GET /rest/servicedeskapi/organization` (paginated) because CSM exposes no list operation: `GET /organization` answers 405, and `POST` there *creates* an organization.

**Failure semantics.** Enumeration failure aborts before any write — a partial list is indistinguishable from organizations having been deleted. A per-organization fetch failure carries the previous row forward, so a transient 429 or 5xx costs freshness but never data. Only a 404 (organization gone) drops a row.

A sweep that would drop more than half the existing rows is refused rather than published, and logs what to check. Rows disappear in two ways that never show up in the failure count: an enumerated organization 404ing on the CSM read — which means enumerated-but-unreadable (a wrong `JIRA_CLOUD_ID`, or an account without Customer Service Management access), since a genuinely deleted organization is simply absent from enumeration — and a short enumeration losing organizations by omission. The guard does not clear itself: if the removals are real, re-run with `--force`.

**When the refresh refuses.** Five outcomes leave the existing rows standing rather than publishing, and each one fails the job (visible in job history) instead of finishing green:

| `skipped_reason` | Meaning | Clears itself? |
|---|---|---|
| `existing_unreadable` | the current table could not be read, so there is no baseline | yes, once the parquet reads |
| `cloud_id_unresolved` | the site's cloud id could not be resolved, so nothing can be read | yes, once reachable |
| `all_fetches_failed` | nothing fresh resolved | yes, once the API recovers |
| `enumeration_empty` | enumeration returned nothing while the table holds rows | **no** — `--force` |
| `mass_removal_guard` | the sweep would drop more than half the rows | **no** — `--force` |

The last two do not clear themselves: they deliberately leave the rows in place, so the next run sees the same state and refuses again. `--force` publishes anyway — including publishing an *empty* table when every organization really was deleted, which is the recovery on a site that has legitimately removed all of them. `jira_not_configured` is not in this list: an instance without Jira ingest skips the job rather than failing it.

**Layout.** Unpartitioned: a single `data/organizations/data.parquet`, written temp-then-`os.replace()` so a reader never sees a truncated file. The extract view uses `union_by_name=true` but no `hive_partitioning` — there is no partition key, because an organization has one current state rather than a history.

```bash
# Manual run
python -m connectors.jira.organizations

# Enumerate only, no fetch or write
python -m connectors.jira.organizations --dry-run

# Publish anyway when the mass-removal guard fires and the removals are real
python -m connectors.jira.organizations --force
```

### Backfilling `issues.organization_ids` on an existing install

The `organizations` table needs no backfill — it is current-state, so the first refresh populates it completely. `issues.organization_ids` is different: it is written by the transform, so partitions produced before this shipped read NULL and those tickets will not join. Nothing errors; the join just silently covers recent tickets only.

The ids are already in the stored raw issue JSON, so no re-fetch from Jira is needed. Re-running the batch transform over the existing raw files is enough. Note this is a **full re-transform of all six partitioned tables**, not an `issues`-only operation — omitting `--attachments-dir` would rewrite all of `attachments` history with `local_path = NULL`:

```bash
python -m connectors.jira.transform \
  --raw-dir /data/src_data/raw/jira \
  --output-dir /data/extracts/jira/data \
  --attachments-dir /data/src_data/raw/jira/attachments
```

Then check the backfilled history reaches as far back as `issues` itself does — the `FILTER` on `min`/`max` is what makes this catch an incomplete backfill (unfiltered, they always report the full range of `issues`, backfilled or not):

```sql
SELECT min(month) FILTER (WHERE organization_ids IS NOT NULL) AS earliest_backfilled,
       max(month) FILTER (WHERE organization_ids IS NOT NULL) AS latest_backfilled,
       count(*) FILTER (WHERE organization_ids IS NOT NULL) AS with_ids
FROM issues;
```

`earliest_backfilled` should be the earliest month in `issues` (compare `SELECT min(month) FROM issues`), not the month this was deployed. A re-transformed row always carries a non-NULL value — `'[]'` when the ticket has no organizations — so NULL means the partition has not been re-transformed yet.

Joining a ticket to its organizations, keeping tickets that have none. The `LEFT JOIN LATERAL … ON true` is load-bearing: a bare `FROM issues i, UNNEST(…)` is an *inner* lateral join, so a ticket whose `organization_ids` is `'[]'` or NULL produces zero unnest rows and silently disappears no matter what the later join says:

```sql
SELECT i.issue_key, o.name, o.crm_account_id
FROM issues i
LEFT JOIN LATERAL UNNEST(from_json(i.organization_ids, '["VARCHAR"]')) AS t(org_id) ON true
LEFT JOIN organizations o ON o.org_id = t.org_id;
```

Use the comma form only when you deliberately want organization-bearing tickets alone.

## Field Refresh Polling (Open Tickets)

Configured field values (`JIRA_REFRESH_FIELDS`) only update on the ticket when a webhook fires. For idle open tickets these values go stale, so a poll re-fetches them periodically.

**File:** `connectors/jira/scripts/poll_sla.py`

The polling job runs every 15 minutes via systemd timer (`jira-sla-poll.timer`) as `root:data-ops` and:

1. Reads Parquet to find open issues (`status_category != 'Done'`)
2. Fetches the configured fields **and status** with the primary token
3. Updates raw JSON atomically (`tempfile.mkstemp()` + `os.fchmod(fd, 0o660)` + `os.replace()`)
4. Triggers incremental Parquet transform (inside advisory file lock)

**Self-healing:** The poll fetches `status`, `resolution`, `resolutiondate`, and `updated` alongside the SLA fields. If a ticket is resolved in Jira but still appears "open" in Parquet (e.g. due to a missed webhook), the poll automatically corrects the status in JSON and re-transforms to Parquet. Log output: `Self-healing: SUPPORT-XXXX is resolved in Jira`. This was added in response to [#203](https://github.com/keboola/agnes-the-ai-analyst/issues/203) where 12 tickets were permanently stale after a permission bug prevented webhooks from updating JSON files.

**File locking:** The entire read-modify-write + Parquet transform is wrapped in a per-issue advisory file lock (`connectors/jira/file_lock.py`) to prevent races with the webhook handler. The webhook handler (`connectors/jira/service.py`) uses the same lock. Different issue keys don't block each other.

**Important — `mkstemp` and ACL:** The `issues/` directory uses POSIX ACLs with `default:mask::rwx`. `tempfile.mkstemp()` creates files with mode `0600`, which overrides the ACL mask to `---` and breaks group access for www-data (webhook handler) and deploy (batch transform). The `os.fchmod(fd, 0o660)` call immediately after `mkstemp()` restores the mask to `rw-`, preserving ACL-based access. See [#203](https://github.com/keboola/agnes-the-ai-analyst/issues/203) for the full incident report.

```bash
# Manual run
python -m connectors.jira.scripts.poll_sla

# Dry run (count open issues)
python -m connectors.jira.scripts.poll_sla --dry-run

# Verbose logging
python -m connectors.jira.scripts.poll_sla --verbose
```

**Return states:**
- `updated` — configured fields refreshed, status unchanged
- `healed` — status corrected (ticket was resolved in Jira but stale locally)
- `skipped` — no fresh field data and ticket not resolved
- `failed` — API error or transform failure

**Querying refreshed fields:** each configured field is a JSON-text column on
`issues` (column = the alias from `JIRA_REFRESH_FIELDS`). Extract parts with
DuckDB's JSON functions — e.g. for an SLA field aliased `first_response`:
```sql
SELECT issue_key,
    json_extract(first_response, '$.ongoingCycle.elapsedTime.millis') AS first_response_elapsed_millis
FROM 'server/parquet/jira/issues/*.parquet'
WHERE first_response IS NOT NULL
```

## Analyst Sync Configuration

Whether an analyst sees Jira tables locally is decided server-side: an admin
must register the Jira tables, add them to a data package, and grant the
package to one of the analyst's groups (per-table
`resource_grants(resource_type='table')` rows are no longer consulted for
analyst visibility — see `src/rbac.py`). Once the package is in the analyst's
stack — automatically when the grant is marked required, otherwise after the
analyst subscribes via `agnes stack add data_package <id>` — the manifest advertises the tables
and `agnes pull` downloads the parquets to the analyst's workspace on the
next session.

Views are created automatically for whichever tables have data. **They are named
after the table, with no `jira_` prefix** — the master view is claimed on the bare
`_meta` table name (`src/orchestrator.py`, `CREATE OR REPLACE VIEW <table_name>`),
so this is what to query:

- `issues` — main issues table
- `comments` — issue comments
- `attachments` — attachment metadata (filenames, sizes, URLs)
- `changelog` — field change history
- `issuelinks` — links between issues (blocks, duplicates, relates to)
- `remote_links` — external links (Confluence, Slack, etc.)
- `organizations` — one row per JSM organization: id, current name, and any
  configured detail fields (see "Organizations Table" above)

```bash
agnes query "SELECT count(*) FROM issues"
```

The `jira_`-prefixed names (`jira_issues`, `jira_comments`, …) belong to the legacy
Data Broker path only: `connectors/jira/scripts/sync_jira.sh` creates them in an
analyst's local `user/duckdb/analytics.duckdb` after an rsync. They are not what a
server-side `agnes query` resolves.

## Attachment Access

Attachments (images, logs, PDFs) are stored on the server alongside parquet
data and are **not** distributed via `agnes pull` (the manifest only
advertises parquet tables). The `attachments` catalogue table (the
connector's table names are unprefixed) has a `local_path`
column with the server-side filesystem path:

```sql
SELECT
    issue_key,
    filename,
    local_path,
    size_bytes
FROM attachments
WHERE issue_key = 'SUPPORT-1234';
```

Result:
```
issue_key     | filename        | local_path                                           | size_bytes
SUPPORT-1234  | screenshot.png  | /data/src_data/raw/jira/attachments/SUPPORT-1234/... | 45678
```

To pull the actual file to a workstation, fetch it by id over the
authenticated API — no SSH to the host required:

```bash
agnes attachment get jira 56340            # writes the original filename
agnes attachment get jira 56340 -o img.png
```

(`GET /api/attachments/jira/{attachment_id}/download` underneath.) The gate
is read access to the `attachments` catalogue table — the same RBAC as the
parquet download — and every fetch is audited.

Setup note: the parquet pipeline itself never requires the metadata-only
`attachments` table to be *registered*, so on many deployments it is not —
and an unregistered table fails closed, meaning analysts get the
table-not-in-your-stack 403 until an admin registers `attachments`
(`POST /api/admin/register-table` or `/admin/tables`) and adds it to a data
package granted to their group. Admins pass via god-mode either way. A 404 with code
`attachment_not_stored` means the catalogue row exists but the server holds
no bytes (over-50MB skip or transform-time miss): fall back to the Jira REST
API for exactly those.

Both attachment publishers pin the published file's mode to `0o660` (group
rw, no world-read) — the same pin the connector's issue-JSON writer uses.
That assumes the documented storage setup: the serving process shares the
data group (`data-ops` in the recipes above, or an equivalent POSIX ACL)
with whatever wrote the file — the webhook writer IS the API process, but
the batch backfill runs as `root:data-ops`, so an API process outside that
group gets `EACCES` on backfill-written files. That misconfiguration is
deliberately loud, not a silent miss: every fetch answers 503
`attachment_unreadable` with a server-log warning naming the path — align
the groups (or the ACL) rather than widening the file mode; world-readable
attachments were rejected in review.

Rollout note for existing deployments: `local_path` is written at
*transform* time, and the webhook path historically ran its transform
BEFORE the attachment download (worker-timeout rationale) — so attachments
first ingested via a single webhook event carry `local_path = NULL` even
though the bytes are on disk, and the endpoint answers
`attachment_not_stored` for them. New events heal their own issue (the
webhook now re-transforms after a download actually lands new files), but
history does not heal itself: after upgrading, run the one-off full
re-transform from the backfill section above — **with `--attachments-dir`**,
which is also mandatory for any batch invocation; omitting it rewrites the
whole `attachments` history with `local_path = NULL` and produces a
catalogue this endpoint can never serve from. The catalogue declaration (table, id/path columns,
permitted root) lives in `src/attachment_sources.py`; any connector that
stores attachments adds its own declaration there and reuses the same
route and CLI command.

## Future Improvements

- [x] ~~Automatic Parquet regeneration after each webhook~~ (Implemented: incremental transform)
- [x] ~~Incremental Parquet updates~~ (Implemented: upsert by issue_key)
- [x] ~~Full historical sync from Jira~~ (Implemented: jira_backfill.py)
- [x] ~~SLA polling for open tickets~~ (Implemented: jira_poll_sla.py, 15min timer)
- [ ] Comment attachment extraction (inline images in ADF)
- [ ] Custom field name resolution from Jira metadata API
- [x] ~~Attachment binary access for analysts~~ (Implemented: lazy per-id fetch via
  `agnes attachment get jira <id>` / `GET /api/attachments/jira/{id}/download` —
  deliberately not manifest sync, so no analyst carries a multi-GB mirror)
