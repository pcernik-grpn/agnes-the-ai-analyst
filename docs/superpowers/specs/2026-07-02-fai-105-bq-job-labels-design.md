# FAI-105 — BigQuery Job Labels (cost attribution) — Design

- **Ticket:** FAI-105 (Story, Epic FAI-31) — *FoundryAI BQ cost attribution & usage telemetry*
- **Date:** 2026-07-02
- **Scope of this spec:** the **labels-only** slice of FAI-105. Completing deferred bytes capture, dollarizing the usage dashboard, and reconciling against the Billing export are **out of scope** and will be separate slices.

## Problem

Foundry's BigQuery jobs are issued through the service account with **no job labels** (`client.query()` in `src/remote_query.py` and the `BqAccess`-backed paths are unlabeled). As a result, BQ usage in `INFORMATION_SCHEMA.JOBS` / the Cloud Billing export cannot be attributed per user or per workload — which is exactly what the reservation model needs, since under the shared 100-slot reservation attribution is by **slot-seconds per labeled dimension**, not by raw bytes.

## Goal

Every Foundry-issued BigQuery **job that we control** carries a consistent set of labels so usage is groupable per user and per workload in `INFORMATION_SCHEMA.JOBS` / Cloud Billing.

### Non-goals (this slice)

- Completing the deferred `bytes_scanned` / `bytes_billed` / `bq_job_id` capture on `/api/v2/scan` (later slice).
- Dollarizing the usage dashboard/export (later slice).
- Reconciling Foundry-recorded bytes vs the Billing export (stretch).
- Any GCP-layer spend ceiling / quota / budget (that was FAI-83 Phase 2).

## Label schema

Authoritative schema per the FAI-105 comment (infra / Deepak, 2026-06-26), reconciled to BigQuery's label rules:

| Key | Value | Source |
|---|---|---|
| `workload_type` | `foundryai` (constant) | literal (lowercased from Deepak's `foundryAI`) |
| `agent_name` | `query` \| `scan` \| `hybrid` | the callsite/code path issuing the job |
| `environment` | `dev` \| `production` | config (`instance.environment`); **omitted if unset** |
| `user_id` | email local-part, sanitized (e.g. `pcernik`) | requesting user; **omitted if no human user** |

**BigQuery label rules enforced:** keys and values must be lowercase letters, digits, `-`, `_`, ≤ 63 chars; keys must start with a lowercase letter. Any label whose value sanitizes to empty is **dropped** (BQ rejects empty *keys* but allows empty *values* — we drop them for cleanliness and to avoid a label conveying nothing).

**Full identity is not lost:** `user_id` is only the sanitized local-part for grouping; the complete identity (full email) is already recorded in `usage_events` / the audit row, which satisfies the "capture who ran each query" requirement.

## Architecture — Approach A (pure helper + per-callsite injection)

A single pure function owns all label construction and sanitization; each labelable callsite injects the result into its `QueryJobConfig`.

### Component 1 — `connectors/bigquery/labels.py` (new)

```python
def build_bq_job_labels(
    user: dict | None,
    agent_name: str,
    environment: str | None,
) -> dict[str, str]:
    """Build the BQ job-label dict for a Foundry-issued query.

    Pure + total: never raises. Applies BQ label rules and drops any
    label whose value is empty after sanitization. `user_id` is derived
    from the user's email local-part; omitted when there is no human
    user (None, or the scheduler service user).
    """
```

- `_sanitize_label_value(raw: str) -> str` — lowercase, replace every char outside `[a-z0-9_-]` with `_`, collapse is not required, truncate to 63.
- `user_id` derivation: take `user["email"]` (fall back to `user["id"]`), split local-part on `@`, sanitize. Omit entirely when `user is None` or the user is the scheduler service account (`client_kind_from_user(user) == "scheduler"`).
- `workload_type` is the constant `"foundryai"`.
- `environment` label included only when a non-empty sanitized value is provided.
- The function is **total** — any internal problem yields a smaller (or empty) label dict, never an exception.

### Component 2 — injection at the three labelable callsites

Each callsite builds `QueryJobConfig(labels=build_bq_job_labels(user, <agent_name>, env))` and passes it to `client.query(sql, job_config=...)`:

| Path | File | `agent_name` | `user` in scope? |
|---|---|---|---|
| `/api/query --remote` | `app/api/query.py` (`run_remote_select_to_arrow`) | `query` | yes |
| `/api/v2/scan` (exec + dry-run estimate at `:63`) | `app/api/v2_scan.py` | `scan` | yes |
| `/api/query/hybrid` (`count_job` + `data_job`) | `src/remote_query.py` (`RemoteQueryEngine.register_bq`) | `hybrid` | via threaded labels |

### Component 3 — `environment` config

Read via `get_value("instance", "environment", default="")`. When empty, the `environment` label is omitted (graceful). A small **infra follow-up** renders `environment` per-VM into `instance.yaml` (mirrors the FAI-83 `locals.tf → startup.sh` pattern); the app change is self-contained and does not depend on it.

## Data flow & threading

The API layer knows both the `user` and its own `agent_name`, so it builds the labels there and passes `QueryJobConfig(labels=...)` into `client.query()`.

`src/remote_query.py` is a data layer that must stay ignorant of the auth/user model. So `RemoteQueryEngine.register_bq()` gains an optional parameter:

```python
def register_bq(self, alias: str, bq_sql: str, *, job_labels: dict[str, str] | None = None) -> ...:
```

`app/api/query_hybrid.py` (which has `user` in scope) builds the labels via `build_bq_job_labels(user, "hybrid", env)` and threads them down as `job_labels`. Both the `count_job` and `data_job` inside `register_bq` apply them. This keeps the data layer decoupled from the user model.

## Error handling

Labeling is **best-effort telemetry and must never break a query.** `build_bq_job_labels` is total (never raises). Injection is additionally defensive: if constructing the `QueryJobConfig` or reading config throws, the callsite logs at `warning` and proceeds **unlabeled** rather than failing the user's query.

## Coverage & known gap

Labels apply to the paths that use the `google-cloud-bigquery` `client.query()` API (the three above). The **DuckDB BigQuery-extension ATTACH path** (`connectors/bigquery/extractor.py:656`, used by sync / snapshot) executes BQ jobs *inside DuckDB*, which owns the job config and exposes no label hook — so those jobs remain **unlabeled**. This is an accepted limitation for this slice: they are batch jobs under the service account, attributable by SA + time window. Documented as future work (would need an upstream DuckDB-extension capability or a rewrite of that path to the jobs API).

## Testing (TDD)

**Unit — `build_bq_job_labels` (the sanitization is where all the risk lives):**
- `pcernik@example.com` → `user_id="pcernik"`.
- Values with `.`, `+`, uppercase, spaces → sanitized to `[a-z0-9_-]`.
- Value > 63 chars → truncated to 63.
- `user=None` and scheduler user → `user_id` omitted.
- Empty / unset `environment` → `environment` label omitted.
- `workload_type` always `"foundryai"`; `agent_name` passed through (and sanitized).
- All returned keys/values match BQ's label grammar (regex assertion) and count ≤ 64.
- Function never raises on malformed `user` dicts.

**Guard tests — one per labelable path:** mock the BQ client, invoke the path, assert the captured `job_config.labels` contains the expected keys/values. Prevents a silently-missed callsite regressing coverage.

## Not applicable

- **DuckDB ↔ Postgres parity:** no repository / DB-state methods are added or changed — this is purely BQ job-config construction. The dual-backend rule does not apply.

## Required repo hygiene

- `CHANGELOG.md` `[Unreleased]` entry (Added: BQ job labels for cost attribution).

## Acceptance criteria

- A Foundry-issued query on each of the three paths carries the label set, verified in the BQ job metadata (`INFORMATION_SCHEMA.JOBS.labels` on the FoundryAI billing project, or the Cloud Billing export) by whoever holds JOBS/billing-export access (infra).
- Labeling failures never surface to the user (a forced label-build error still returns query results, unlabeled).
- Unit + guard tests pass; Snowflake/other behavior unchanged.

## Open items / dependencies

- Infra to render `environment` per-VM into `instance.yaml` (small `locals.tf`/`startup.sh` add; app is graceful without it).
- Verification of labels in `INFORMATION_SCHEMA.JOBS` / Billing export requires access that sits with infra (Deepak) — the app-side change is independently mergeable and testable.

## Related

- FAI-83 (limits / cost-ceiling) — shipped; the 100-slot reservation (`res-foundry-ai`) is why attribution is slot-seconds, not bytes.
- Later FAI-105 slices: bytes/slot-time capture on `/api/v2/scan`; dollarized usage dashboard; Billing-export reconciliation.
