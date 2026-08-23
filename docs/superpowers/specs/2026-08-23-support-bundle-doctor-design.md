# Support-bundle doctor (`agnes doctor`) — design

Date: 2026-08-23
Status: implemented in the same PR as this spec

## Problem

When an analyst reports "Agnes gave me a wrong/failed answer", support burns
its first N round-trips collecting context: which CLI version, which server,
is auth even working, did the last `agnes pull` succeed, what does the server
think its sync state is, has retrieval silently degraded to lexical-only.
Every one of those questions has an existing surface (`agnes diagnose`,
`agnes status`, `/api/health/detailed`, `agnes admin doctor --new-instance`),
but no single artifact a user can attach to a ticket. The support loop needs
"run one command, attach one file".

## Shape

One new CLI command and one new admin REST endpoint. No new checks are
invented where an existing surface already computes the answer — the doctor
*collects and formats*; it does not fork the diagnostics vocabulary.

```
agnes doctor                    # writes ./agnes-doctor-<UTC ts>.md, prints path
agnes doctor --output PATH      # choose the destination
agnes doctor --json             # print the structured bundle to stdout instead
```

The bundle is a single Markdown file with two origin-labeled sections:

1. **Client section** — always present, works fully offline:
   - CLI version, platform, config dir
   - server URL + auth verdict (reachable? authenticated? as whom/role) —
     verdict only, never the token
   - workspace: path, initialized?, local tables (queryable vs
     downloaded-no-view), last pull time, pending session uploads
     (same sources as `agnes status`)
   - local-delivery comparison (reuses `_local_delivery_check` from
     `agnes diagnose` — manifest offered vs on-disk)
   - tail of `~/.config/agnes/last-error.log` (client-side transport
     failures), scrubbed
2. **Server section** — fetched from `GET /api/admin/doctor/support`
   (admin-only). When the caller is not an admin or the server is
   unreachable, the section is REPLACED by an explicit one-line reason
   (silent partial scope is forbidden — command-UX standard). The client
   section is still written; the moments you need a doctor most are exactly
   the moments the server half is unavailable.

## Server endpoint

`GET /api/admin/doctor/support` in `app/api/admin_doctor.py`
(`Depends(require_admin)`), collector in `app/services/support_bundle.py`.
Every sub-collector runs isolated (the `instance_doctor._isolated` contract):
one crashing resolver reports itself as an `error` entry instead of killing
the report.

Response sections:

| key | content | source of truth |
|---|---|---|
| `build` | version, channel, image tag, commit sha, deployed_at, uptime | same env vars `/api/version` + `/api/health/detailed` read |
| `schema` | backend (duckdb/postgres), current vs expected, verdict | `app.api.health._check_db_schema` (reused, not re-derived) |
| `retrieval` | `hybrid` \| `lexical_only` + verdict (`warning` on lexical_only — the degradation this key exists to make loud, #898) | `src.ingest.retrieval.retrieval_mode()` |
| `sync` | per **source_type** rollup: table count, ok/error/stale counts, newest last_sync, up to 5 most recent `{table_id, error, at}` failures | `sync_state_repo()` × `table_registry_repo()` join in Python |
| `disk` | data-dir filesystem total/used/free, state+analytics DB file sizes | `shutil.disk_usage`, `Path.stat` |
| `process` | state backend (duckdb/pg), python version, data-apps enabled | `use_pg()`, `sys.version`, switches |
| `secrets` | curated env-var names → `present`/`absent` — **names and booleans only, never values** | `os.environ` membership |

"Container health" is deliberately reported as *process-level* signals
(deployed_at/uptime, backend reachability, sync recency): the app cannot see
the Docker daemon from inside its own container, and probing other containers
from in-process is the wrong layer. Host-side container checks belong to the
`scripts/ops/` siblings (same split as `post-deploy-smoke-test.sh`), out of
scope here.

## Redaction

- The server payload is built from structured fields; it never embeds env
  values. The `secrets` section carries presence booleans only.
- The CLI scrubs the final rendered text before writing: the configured API
  token's literal value (strongest guarantee — read from config and
  string-replaced), plus pattern scrubs for `Bearer <...>`, `Authorization:`
  headers, and `token=`/`api_key=` query fragments that may appear in the
  error-log tail.

## RBAC / coverage classification

- Endpoint gate: `require_admin` (app-level operator diagnostic).
- REST×CLI×MCP: CLI-reachable via `agnes doctor`; **deliberately never
  MCP-exposed** — the response enumerates security/auth configuration posture
  (which secrets exist, schema state, build fingerprints), i.e. exactly the
  reconnaissance the standing "operator security-posture diagnostics"
  exemption in CONTRIBUTING.md exists for. Classified `_EXEMPT` in
  `tests/test_documentation_api_triple_surface.py`.
- Docs: row in `docs/api-reference.md` endpoint inventory.

## Non-goals (v1)

- No zip bundle — one Markdown file is attachable everywhere and diffable.
- No log shipping of server-side container logs (size + secrets risk; the
  operator has `docker logs`).
- No auto-upload of the bundle anywhere; the file lands on the user's disk
  and the human decides where it goes.

## Naming

`agnes doctor` (support bundle, any authenticated user, degrades offline)
vs the pre-existing `agnes admin doctor --new-instance` (deployment gate,
admin, active checks). Docstrings cross-reference both directions.
