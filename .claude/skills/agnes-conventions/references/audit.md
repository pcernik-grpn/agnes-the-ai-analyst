# Audit coverage playbook

Agnes answers "who did what, through which surface, when" from one table:
`audit_log`. The invariant is **every user or admin interaction leaves
exactly one classified row** — and it holds by construction, not by
diligence: a surface that does not declare its audit posture fails CI.

Read this before adding any route, job kind, MCP tool, or bot command.

## The three files

| File | What it is |
|---|---|
| `src/audit_events.py` | `CATALOG` — the closed set of action names Agnes may write. Append-only: renaming a key silently rewrites history for every row already written under the old name (use `LEGACY_ALIASES` instead). |
| `src/audit_posture.py` | What each surface *means*: `POSTURE` (mutating routes), `READ_POSTURE` (GET), `WS_POSTURE` (WebSocket). A value is a cataloged action, or `exempt:<reason>` from the closed `EXEMPT_REASONS` vocabulary. |
| `src/audit_helpers.py` | `log_safe(...)` — the only sanctioned writer. Audit failure must never fail the request it describes. |

## How emission actually works

A posture entry is **not documentation, it is the route's declared action.**
`AuditFallbackMiddleware` emits it (building `resource` from the route's own
path params) whenever the handler wrote no row itself. So:

- **Most routes need no code at all** — declare the action and you are done.
- **Write `log_safe(...)` in the handler only when you can say more** than
  the middleware can: a resource id it cannot derive, a `params_before` for
  a diff, a denied branch, a domain-specific payload.
- Never write both for the same event. The middleware detects the handler's
  write through a per-request counter and stays silent — see the
  "context copy" note below for why that counter is a mutable box.

## What gets an action, and what is exempt

A **mutating** route always gets a real action.

A **read** gets a real action when it returns:
- data content (query results, samples, downloads, exports, bundles),
- secrets or tokens,
- another user's data (admin cross-user reads),
- the audit trail itself.

Everything else is `exempt:<reason>` from the closed vocabulary
(`health`, `self`, `ui_support`, `static`, `noise`). The vocabulary is
closed on purpose: wanting a reason outside it is a signal the surface
probably deserves an action. `tests/test_audit_read_posture.py` pins the
categories that may never be exempt.

## Naming

`domain.object.verb`, describing the **domain effect** — `prompt.delete`,
`collection.file_add`, `source_connection.secret.set`. Never the HTTP shape
(`prompts_delete_endpoint`), never a flat `admin.*` bucket: an admin route
belongs to its own sub-domain (`ontology_draft.*`, `semantic_model.*`).
Reuse an existing cataloged action when the surface genuinely performs it;
mint a new one only when none fits, and give it a description saying what
the row MEANS, not which route wrote it.

Scheduler-triggered endpoints keep the `run_*` prefix — `SCHEDULER_ACTION_SQL`
in `src/audit_helpers.py` uses it for the Activity Center's liveness pulse,
so a differently-named sweep runs without ever advancing that signal.

## Content never, metadata always

Prompts, SQL text, request bodies, and secret values never enter `params`.
Use identifiers, counts, sizes, and hashes (`src.audit_helpers.hash_args`).
The chat surface records `{"session_id", "chars"}`, never the message; the
CLI spool records `sql_hash`, never the SQL. A secret-read row names the
secret, never its value — `tests/test_audit_gap_secrets_distribution.py`
asserts this.

## Adding a new `/api/*` route: the seven places

A new route touches more than the posture map, and CI reports the misses one
at a time across three shards. Do all seven in the same change:

1. `src/audit_posture.py` — `POSTURE` / `READ_POSTURE` / `WS_POSTURE` entry.
2. `src/audit_events.py` — `CATALOG` entry for any newly minted action.
3. `tests/test_documentation_api_triple_surface.py` — a CLI command + MCP
   tool that reach it, or an `_EXEMPT` entry with a stated reason.
4. `docs/api-reference.md` — the path, or `tests/test_api_docs_coverage.py` fails.
5. `tests/test_route_auth_guard.py` — only if the route carries no user auth
   dependency (a system-to-system channel): `_EXEMPT` with the reason.
6. `tests/db_pg/test_endpoints_smoke.py` — dual-backend smoke coverage, or a
   `KNOWN_UNTESTED` entry with a justification.
7. `tests/snapshots/openapi.json` — regenerate:
   `.venv/bin/python scripts/generate_openapi.py > tests/snapshots/openapi.json`

## Traps that have actually bitten

**The write counter is a mutable box, not an int.** Starlette's
`BaseHTTPMiddleware` (several are mounted below the audit pair) runs the
downstream app in its own asyncio task, and a new task gets a *copy* of the
context. A `ContextVar` holding a plain int loses the handler's increment on
the way back out, so "did the handler audit itself?" answers no and the
request gets a duplicate row. `AuditTimingMiddleware` installs one shared box
per request, outside every `BaseHTTPMiddleware`. Don't "simplify" it back.

**A thread-offloaded read handler that audits itself.** A plain `def` GET
handler writing its own row belongs in `READ_SELF_AUDITING`, which the
middleware skips unconditionally. The read path deliberately has no
correlation-id tie-break query — that is a DB round-trip per request, and the
read path is far hotter than the mutating one.

**Windowed writers must not also be declared.** `data_app.access` is written
by the subdomain middleware with a 15-minute per-user/per-app window.
Declaring the same route in `READ_POSTURE` would make the read path emit an
unwindowed row on every repeat, defeating the dedup. Its entry says so.

**A shared secret is compared in constant time.** `hmac.compare_digest` plus
a length floor that treats a too-short secret as auth disabled — see
`app/auth/scheduler_token.py`. Fix both directions of a shared secret at once;
half a fix leaves the key guessable through the other door.

## Non-HTTP surfaces

Worker job kinds, MCP tools, scheduler jobs, and bot commands are audited
too, and carry their own posture declarations. Bots run outside a request, so
they pass `client_kind` explicitly (`"slack"`, `"telegram"`) — autofill has
no request to read. Identity comes from the binding lookup; an unresolvable
sender falls back to the raw email/id rather than dropping the row.

## Volume

The middleware writes a row per unaudited mutating request and per declared
sensitive read, and `audit.retention_days` defaults to 365.
`scripts/audit_volume_estimate.py` measures rows/day, the top actions, and
the projected table size; `audit.sampling.<action>` is an opt-in,
deterministic per-action sampler. Never sample a security-relevant action.
