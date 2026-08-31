# Unstructured-data pipeline — E2E test pack

Small, dependency-light Python scripts that exercise the fact-graph-over-
Collections pipeline against a **live, running Agnes instance** over its
real HTTP API — never mocked, never in-process. They prove (or catch a
regression in) the acceptance gates described in
[`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`](../../../docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md)
§15.1 (security S-probes), §15.4 (anonymization), and the extraction seam
from §7.5/§9. Background: [`docs/anonymization.md`](../../../docs/anonymization.md)
and the first live execution of this style of probe,
[`docs/superpowers/runs/2026-08-28-run-p-planted.md`](../../../docs/superpowers/runs/2026-08-28-run-p-planted.md).

This is an *ops/verification* tool, not a pytest suite — it belongs beside
the pytest-based `tests/db_pg/test_facts_*.py` files (which prove the same
gates at the repository layer, in-process, with a real Postgres fixture),
not instead of them. Run this pack when you need to know whether a
**deployed** instance actually enforces these gates end-to-end.

## Prerequisites

- A running Agnes instance with a Postgres backend (`facts_repo()` and
  `facts_ingest_runs_repo()` are PG-only — the A3 ratchet; every check that
  needs them fails clean with `501 requires_postgres_backend` on a
  DuckDB-backed instance rather than crashing, but the checks themselves
  won't pass).
- An admin bearer token (PAT or scheduler shared secret) for that instance.
- Python 3.11+ with this repo's own dependencies installed (`httpx` is a
  base dependency — see `pyproject.toml`; no extra install needed beyond
  what `uv pip install ".[dev,server]"` already gives you).
- For probe 03 (visibility): two non-admin persona accounts with issued
  bearer tokens, and one of them's Agnes group id.
- For probe 05 (extraction): `extraction.enabled` on, and a SharePoint
  connection id already registered on the instance.

Nothing here needs a checkout of the producer pipeline (crawl/convert/
anonymize/extract) — that lives outside this repo (spec §9, §7.1). Probes
04 and 05 test the seam Agnes exposes to it, not the pipeline itself.

## Environment variables

| Variable | Required by | Meaning |
|---|---|---|
| `AGNES_BASE_URL` | all | Base URL of the instance, e.g. `https://dev.example.internal` |
| `AGNES_ADMIN_TOKEN` | all | Admin bearer token (PAT or scheduler shared secret) |
| `AGNES_E2E_TOKEN_A` | 01 (optional), 03 | Bearer token for persona A (a non-admin analyst account) |
| `AGNES_E2E_TOKEN_B` | 01 (optional), 03 | Bearer token for persona B — must NOT share persona A's group memberships |
| `AGNES_E2E_GROUP_A` | 03 (optional — narrows scope if unset) | Agnes group id persona A belongs to and persona B does **not**. Find it via `GET /api/admin/access/groups` or `/admin/access` |
| `AGNES_E2E_PLANTED` | 04 | A string known to have existed in the **pre-anonymization** source content, that should now appear nowhere in Agnes |
| `AGNES_E2E_ANON_COLLECTION_ID` | 04 (optional — narrows scope if unset) | Collection id backing an anonymize-marked scope, to walk its files' `/preview` and `/raw` endpoints |
| `AGNES_E2E_ANON_SAMPLE_LIMIT` | 04 (optional, default `300`) | Cap on how many subjects' claims probe 04 samples — it is a **bounded sample**, not exhaustive, on a large corpus |
| `AGNES_E2E_SP_CONNECTION_ID` | 05 | A registered SharePoint connection id (`source_type=sharepoint`) to trigger extraction for |
| `AGNES_E2E_EXTRACT_TIMEOUT_SECONDS` | 05 (optional, default `120`) | How long to poll the `corpus-extraction` job before giving up |

Every script degrades gracefully when an *optional* variable is missing: it
SKIPs the checks that need it (printed in the table, exit code unaffected)
rather than failing outright. `AGNES_BASE_URL` and `AGNES_ADMIN_TOKEN` are
the only two variables every script hard-requires.

## Run order

```bash
export AGNES_BASE_URL=https://dev.example.internal
export AGNES_ADMIN_TOKEN=...            # admin PAT or scheduler shared secret

python scripts/e2e/unstructured/01_smoke_instance.py      # always run first
python scripts/e2e/unstructured/02_ingest_gates.py
python scripts/e2e/unstructured/03_visibility_probes.py   # needs TOKEN_A/_B (+ GROUP_A for full coverage)
python scripts/e2e/unstructured/04_anonymization_probe.py # needs AGNES_E2E_PLANTED
python scripts/e2e/unstructured/05_extraction_seam.py     # needs extraction.enabled + a connection id
```

Each script is standalone and idempotent — run any subset, in any order,
any number of times. 01 is the recommended first step only because its
output (facts/extraction flag state) tells you which of 02-05 can possibly
pass before you spend time chasing a false failure.

## What each PASS proves

- **01** — the instance is reachable and its DB schema check passes; the
  admin token actually authenticates (and a missing/garbage token is
  actually refused — auth that always says yes would pass this test too if
  it only checked the happy path); the live `facts`/`extraction` flag
  states are reported (informational — a flag being off is not itself a
  failure of this script).
- **02** — `POST /api/facts/ingest` enforces its two door-gates *before*
  touching the database: a malformed `evidence[].audience` tag is refused
  whole-batch (422), and a batch documenting an anonymize-marked corpus
  with no `anonymization` declaration is refused (403
  `anonymization_not_declared`) — closing the fail-open path a silent
  connect-wizard config wipe used to open (see `docs/anonymization.md`).
- **03** — RBAC on the fact graph holds over the real HTTP surface: an
  ungranted caller sees nothing of a fact evidenced only in collections
  they cannot read (S1); a hidden attribute never surfaces via `search`'s
  projection *or* its `filters` (S2, "the attribute oracle" — the one most
  likely to regress silently); a nonexistent subject and a real-but-
  ungranted one 404 identically, so a caller can never distinguish "there
  is nothing here" from "there is something you can't see" (S6); an edge's
  visibility is never inferred from its endpoints' visibility (S3).
- **04** — a string that should have been redacted before it ever reached
  Agnes does not resurface on any Agnes-side reader surface (facts search,
  a bounded sample of claims, collections search, and optionally a named
  collection's document preview/raw endpoints). **This does not prove the
  producer's anonymizer ran or ran correctly** — that stage is external to
  this repo. A PASS means Agnes did not re-expose an already-anonymized
  batch; it is not proof the batch was anonymized in the first place.
- **05** — the one seam Agnes owns end-to-end: triggering `corpus-
  extraction` for a SharePoint connection enqueues a job, a worker claims
  it, and it reaches a terminal state. Everything upstream of "run the
  configured producer command" is out of scope (external pipeline).

## Triage table

| Symptom | Likely cause | Where to look |
|---|---|---|
| `401` on everything, even with a token set | Token expired/revoked, or wrong instance URL | Reissue the PAT; confirm `AGNES_BASE_URL` |
| `403 anonymization_not_declared` on a batch you did NOT expect to be anonymize-marked | A stale SharePoint scope from a prior test run, or a real connection's scope config | `GET /api/admin/sharepoint/connections/{id}/scopes` — check `anonymize` per row |
| `501 requires_postgres_backend` on any facts endpoint | The instance is DuckDB-backed | Facts is PG-only by design (A3 ratchet) — point the pack at a PG-backed instance, or expect 02/03/04's facts-dependent checks to SKIP/fail clean, never crash |
| `404` on every `/api/facts/*` call | `facts.enabled` is off | Check via 01's flag report, or `POST /api/admin/server-config` with `{"facts": {"enabled": true}}` |
| 05 SKIPs with "extraction.enabled is off" | Feature flag off by default | `instance.yaml`'s `extraction:` block, or `AGNES_EXTRACTION_ENABLED=1` |
| 05 fails with `extraction_producer_not_configured` | `extraction.enabled` is on but no producer command/module is set | `instance.yaml`'s `extraction.producer.command`/`.module` |
| 03 SKIPs "full plant-based probes" | `AGNES_E2E_GROUP_A` not set | Only the baseline 404-parity check runs; set the group id for full S1/S2/S3 coverage |
| 03's setup step times out waiting for indexing | Ingestion pipeline (chunking) is stalled or slow on this instance | Check worker logs / job queue depth; retry with a longer wait by editing `_INDEX_TIMEOUT_SECONDS` locally, or investigate the instance directly |
| 04 reports a leak | A real finding — the planted string reached an Agnes surface | Confirm which surface (facts claims / collections search / preview / raw) from the check's detail column, then trace whether the producer's anonymizer actually ran on that document, or whether an un-anonymized upload bypassed the connect-wizard scope entirely |

## Adding a new script

Follow the numbering convention (`0N_<area>.py`), import the shared
`_common` module for `Config`/`Report`/`client_for`, and keep it idempotent
— name every created resource with `unique_suffix()` and clean up in a
`finally` block, so a failed or interrupted run never blocks the next one.
