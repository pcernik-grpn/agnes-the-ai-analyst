# Contributing to Agnes

This file is the single source of truth for change-safety invariants. The
`/agnes-review` review team walks the sync-map below; human contributors should
too. Full design: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md`.

## Dev workflow

1. Work on a branch (or an isolated git worktree).
2. TDD: write the failing test first, then the minimal implementation.
3. Keep changes vendor-agnostic — this is the public OSS distribution. No
   customer-specific deployments, project IDs, internal hostnames, or
   cross-references to private repos in code, config, comments, docs, or commits.
4. Run the **fast lane** before pushing (~3 min): `.venv/bin/pytest tests/ connectors/ --lane fast --tb=short -n auto -q`. The full suite runs in CI on the push — do not run it locally as a matter of routine.
5. Add a `## [Unreleased]` CHANGELOG bullet for any user-visible behavior change.

## Testing conventions

**A visibility/filtering assertion needs a non-admin caller as its proof.**
`Admin` is a god-mode short-circuit on every authorization check (see
`app/auth/access.py`'s module docstring) — a test that asserts what a caller
can see, which rows are filtered, or which names/values are readable, while
calling *only* as an admin, passes identically whether the underlying gate
works or is completely broken. This is not hypothetical: a broken
alias-visibility migration reached review because every non-admin fixture
seeded its provenance row explicitly, and every test that omitted it
happened to assert through an admin token — silently vacuous instead of red.

- The primary assertion for a visibility property must go through a
  non-admin caller (an ordinary user, a scoped `AgentPrincipal`, a
  `SessionPrincipal`) that is denied or narrowed by construction — e.g. a
  fixture uploaded/owned by someone else, a grant deliberately withheld.
- An admin-sees-everything case is legitimate and worth keeping, but as a
  **named sibling** ("positive control"), never the only case — see
  `tests/db_pg/test_facts_read_pg.py` (`test_count_visible_edges_for_
  collections_admin_sees_everything` next to its caller-scoped sibling) and
  `tests/test_api_collections.py` (`test_admin_search_sees_all` next to
  `test_search_fail_closed_excludes_ungranted`) for the pattern.
- There is deliberately no automated guard for this: a grep for "admin"
  cannot distinguish an admin-only visibility claim from the many legitimate
  admin-only tests (admin-gated endpoints, seeding helpers, unrelated
  assertions) without a false-positive rate that gets the check disabled.
  Reviewers (`agnes-reviewer-rbac`) apply this by reading the assertion, not
  a script.

## Verification loop

Before claiming a change is done, run the checks cheapest-first and fix what
fails until each passes:

```bash
python3 scripts/verify_syncmap.py                                       # instant, no venv
.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q   # seconds to ~2 min
.venv/bin/pytest tests/ connectors/ --lane fast --tb=short -n auto -q       # ~3 min
/agnes-review                            # judgment only, once the above are green
```

`scripts/verify_syncmap.py` covers the sync-map rows that have no test guard.
The ordering is the point: anything a script can decide should never cost an LLM
reviewer a finding. The step-by-step loop (which guards to run for which diff,
how to treat WARN findings, when to add a new check) is
`.claude/skills/verify-agnes-change/SKILL.md`.

### Test lanes — what to run locally, and what CI runs

**The full suite is CI's job, not yours.** It is ~24 000 tests: 12 parallel jobs
of ~15 minutes each in CI, and 12+ minutes locally. Running it before every push
— then again after each review round — is where a two-line fix turns into a
two-hour merge, and it buys nothing CI is not about to compute anyway.

| Lane | Command | What it is |
|---|---|---|
| `impacted` | `pytest tests/ connectors/ --lane impacted -n auto -q` | Only the test files this branch's diff plausibly touches. Falls back to `fast` — never to the full suite — when the diff is too broad to target (a merge magnet like `src/db.py`, or a match set covering >25% of the suite's runtime). Inspect the selection with `python3 scripts/dev/impacted_tests.py --json`. |
| `fast` | `pytest tests/ connectors/ --lane fast -n auto -q` | ~13 000 tests, ~3 min. Every test whose recorded runtime sits under the ~150 ms floor a test pays the moment it builds a `system.duckdb` — i.e. the half of the suite that is pure unit work — **plus every test with no recorded duration**, so a test you just wrote is always in. |
| full | `pytest tests/ connectors/ -n auto -q` | ~12 min. CI runs this on every push. Locally: when CI is red and the failure will not reproduce under a narrower lane, or when you touched a merge magnet and want the answer before the round trip. |

Both lanes print which lane ran in the pytest header and a kept/deselected line
in the summary, so a green run can never be mistaken for a full one. Machinery:
the root `conftest.py` and `scripts/dev/impacted_tests.py`; ratchets in
`tests/test_test_lanes.py` (which fail if the fast lane stops being fast, or
starts covering so little that green means nothing).

**Where a full local run still earns its keep:** a change to `src/db.py`, the
repository factory, `tests/conftest.py` or `app/main.py` — the merge magnets the
selector refuses to guess about. Everything else: push, and read CI.

**A PR that gets no CI is not reviewable, whatever it targets.** `ci.yml`'s
`pull_request` trigger therefore carries no `branches:` filter — a PR into a
stack base (`mf/semantic-layer-v0`, a `claude/*` branch, anything) runs the
same suite as one into `main`. This is worth stating because the failure mode
is silent: with a filter, GitHub fires no workflow at all and the PR shows a
**green rollup that asserted nothing**, which reads exactly like a passing run.
When you check a PR's status, confirm the check NAMES are present
(`test-shard (1..8)`, `test-pg (1..4)`) — "no red" is not the same as "tested".
Stack bases are also unprotected, so `gh pr merge --auto` on one merges
immediately rather than waiting for anything.

## Sync-map

Surfaces that must change together — and that CI does **not** fully guard. When
you touch the left column, update the middle column **in the same change**. Each
review finding cites two `file:line`: where the change landed, and where the
mirror is missing.

| Change | Mirror surface that MUST update | Severity | CI guard? |
|---|---|---|---|
| Method on an EXISTING (frozen pre-A3) `src/repositories/X.py` pair | sibling in `src/repositories/X_pg.py` | BLOCKING | partial |
| New app-state repo (PG-first ratchet, A3 — DuckDB app-state is frozen) | `src/repositories/<name>_pg.py` ONLY, registered `PG`-only in the `_REGISTRY` factory table — no DuckDB module, no DuckDB `_REGISTRY` entry | BLOCKING | `tests/test_repository_registry.py` + `tests/test_repository_registry_pg_first_ratchet.py` + `tests/db_pg/test_repo_module_pg_first_ratchet.py` (all static) |
| New callsite reading app-state | go through a `*_repo()` factory fn — never direct repo instantiation or raw `get_system_db()` | BLOCKING | `tests/test_backend_split_guard.py` (static) + `tests/db_pg/_parity_sweep_util.py` (dynamic; PG-only routes use the documented `_PG_ONLY_ROUTE_EXEMPTIONS` mechanism, not a bare skip) |
| New repo method on an EXISTING (frozen pre-A3) pair | extend the matching `tests/db_pg/test_<cluster>_contract.py` | BLOCKING | partial |
| New schema change (A3 — DuckDB ladder frozen) | Alembic revision ONLY (`migrations/versions/`) + `src/db_pg.py` `Base.metadata` — no `_vN_to_v(N+1)` step in `src/db.py`; `SCHEMA_VERSION` must stay at `FROZEN_DUCKDB_SCHEMA_VERSION` | BLOCKING | `tests/test_db_schema_version_frozen.py` (freeze) — `tests/test_db_schema_version.py` remains the integration gate for the frozen ladder's pre-A3 steps |
| `SCHEMA_VERSION` bump in `src/db.py` | the version stated in `docs/runbooks/wal-recovery.md` — it is what an operator compares a recovered database against, so a stale one sends them to restore a database the binary would reject | BLOCKING | `tests/test_runbook_wal_recovery.py` |
| New `ResourceType` enum value | `ResourceTypeSpec` in `app/resource_types.py` `RESOURCE_TYPES` | BLOCKING | `scripts/verify_syncmap.py` (full sweep) |
| New entity-scoped endpoint | `Depends(require_admin)` or `require_resource_access(...)` from `app/auth/access.py` | BLOCKING | `tests/test_route_auth_guard.py` (proves *some* auth) + `scripts/verify_syncmap.py` (WARN on authn-only entity routes) |
| New REST `/api/*` endpoint | a CLI command + an MCP tool that reach it (see "API coverage" below) | BLOCKING | `tests/test_documentation_api_triple_surface.py` (triple-surface ratchet) + `tests/test_api_docs_coverage.py` (docs) |
| New `POST`/`PUT`/`PATCH`/`DELETE` HTTP route | declare its audit posture in `src/audit_posture.py`'s `POSTURE` dict — a real cataloged action from `src/audit_events.py`, or `"exempt:<reason>"` using a reason from the closed `EXEMPT_REASONS` vocabulary. The declared action is what `AuditFallbackMiddleware` emits when the handler writes no row itself, so it must describe the domain effect (`prompt.delete`), not the HTTP shape. **There is no `"fallback"` value any more** | BLOCKING | `tests/test_audit_route_posture.py` + `tests/test_audit_declared_actions.py` |
| New `GET` or WebSocket route | declare it in `READ_POSTURE` / `WS_POSTURE` the same way. A read gets a real action when it returns data content, secrets/tokens, another user's data, or the audit trail itself; everything else is `"exempt:<reason>"` | BLOCKING | `tests/test_audit_read_posture.py` |
| New audit action string | an entry in `src/audit_events.py`'s `CATALOG` (or a prefix in `DYNAMIC_ACTION_PREFIXES`), and the write goes through `src.audit_helpers.log_safe` — never `audit_repo().log()` directly outside the repo layer | BLOCKING | `tests/test_audit_catalog.py` |
| User-visible behavior change | `## [Unreleased]` bullet in `CHANGELOG.md` — never a version bump; that is the dedicated cut PR's job, see `docs/RELEASING.md` | BLOCKING | `scripts/verify_syncmap.py` (skipped on a release-cut) |
| New connector extractor | `_meta` table contract (`table_name, description, rows, size_bytes, extracted_at, query_mode`); see `connectors/keboola/extractor.py` as canonical example | BLOCKING | partial |
| `query_mode='remote'` table | `_remote_attach` row in `extract.duckdb` | BLOCKING | `scripts/verify_syncmap.py` (connector must mention `_remote_attach`) |
| New function-scoped test fixture needing a FastAPI app | request the session-shared `shared_app` (or `seeded_app`) — never call `create_app()` per test (~430 ms each) | BLOCKING | `tests/test_shared_app_contract.py` |
| New web page | extends `base_ds.html` / `base_page.html` (never `base.html`); CSS in `head_extra` | BLOCKING | `tests/test_design_system_contract.py` (partial) |
| New/renamed/removed user-facing web page or admin-nav entry | the chat agent's web-UI guide `app/initial_workspace_default/.claude/skills/agnes-web-guide/` — add/update/remove the page's entry so the agent describes the same product the user sees | BLOCKING | `tests/test_web_guide_skill_sync.py` (both directions: unmentioned live page, mentioned dead path) |
| New/changed CLI or MCP read/find command | command-UX standard (`.claude/skills/agnes-conventions/references/command-ux.md`): default scope = auto/everywhere, origin labeled, `--scope` (never a new boolean scope flag), positional term + `--limit` + `--json`, "not found" hints the next step | BLOCKING | `scripts/verify_syncmap.py` (new boolean scope flag only — the rest is review) |
| New MCP foundation tool | defined in `app/api/mcp/foundation_tools.py` + name appended to `FOUNDATION_TOOL_NAMES` — never hand-added to a single transport module | BLOCKING | `tests/test_mcp_tool_parity.py` |
| New user-visible switch (feature flag, theme, layout, mode) | an entry in `app.switches.SWITCHES` + a row in `docs/feature-flags.md` (see that doc's "How to add a switch") — never a hand-rolled `os.environ.get(...)` / `get_value(...)` pair | BLOCKING | `tests/test_switches.py` (registry integrity) + `tests/test_admin_configure_api.py` (editable-section derivation) |
| Feature/fix PR | never a version bump, `server.json` edit, or `[Unreleased]` rename — the daily `release-cut`-labeled PR (`.github/workflows/daily-cut.yml`) owns the cut, once a day, per `docs/RELEASING.md` | BLOCKING | `scripts/verify_syncmap.py` (a version bump suppresses the CHANGELOG-bullet check, so a feature PR sneaking one in evades that guard — review catches it) |
| Prompt rule edited in `app/initial_workspace_default/CLAUDE.md` (chat-sandbox-only bundled fallback) | mirror the same section in `config/claude_md_template.txt` (server-rendered — `WorkdirManager.run_init` overwrites the bundled file with this on the sandbox's common path, `is_sandbox=True`; `agnes init` on a laptop also renders this template via `GET /api/welcome`, but with `is_sandbox=False`, so a section whose wording differs by surface must branch on `is_sandbox` rather than stay literally identical — see the "Charts" section for the pattern) | BLOCKING | `tests/test_chat_answer_provenance_and_charts.py::test_the_say_where_it_came_from_section_does_not_drift` (surface-invariant sections) + `::test_the_charts_sandbox_wording_does_not_drift` + `::test_the_file_handover_wording_does_not_drift` (surface-dependent sections — pin the bundled file against the template's `is_sandbox=True` *render*, not its raw source) |

### Parity enforcement reality

Parity is not just `X.py` ↔ `X_pg.py`. Backend selection lives in
`src/repositories/__init__.py` (a `{backend: (module, class)}` dispatch table
keyed off `use_pg()` / `DATABASE_URL`); callsites import `*_repo()` factory
functions, not repo classes. Two guards back the sync-map:

- **Static:** `tests/test_backend_split_guard.py` scans for direct repo
  instantiation + `get_system_db()` callers.
- **Dynamic:** `tests/db_pg/_parity_sweep_util.py` drives both backends through a
  `TestClient` and diffs the HTTP status of every parameter-free route. A
  route backed by a Postgres-only repo (PG-first ratchet, A3) is legitimately
  expected to diverge — it is listed (route → one-line reason) in the sweep's
  `_PG_ONLY_ROUTE_EXEMPTIONS` (`dict[str, str]`) and excluded from the diff,
  but `assert_pg_only_exemptions_fail_clean` still requires it to answer a
  TYPED `501` on DuckDB — status `501` AND `body["error"] ==
  "requires_postgres_backend"` (the translated `RequiresPostgresBackend`) —
  never a raw 500, and never an unrelated 4xx accepted just because it's
  also an error status.

The parity reviewer flags exactly what these guards cannot see.

### API coverage (REST × CLI × MCP)

Every new REST `/api/*` endpoint — except health checks, webhooks, OAuth
callbacks, and internal/SSE routes — must also be:

> **Standing exemption — admin credential-provisioning writes.** Endpoints
> whose request body carries or reconfigures upstream credential trust (vault
> secret writes, OAuth client registration/config) are CLI-reachable but
> deliberately **never** MCP-exposed: an agent-invokable tool that can
> re-point which upstream a credential authenticates against is a
> privilege-escalation seam, not a convenience. Classify them `_EXEMPT` with
> a pointer to this paragraph.

> **Standing exemption — operator security-posture diagnostics.** Endpoints
> whose *response* enumerates the instance's security/auth configuration
> posture (which login doors exist, whether bootstrap is still open, mail
> transport state — e.g. the new-instance doctor) are CLI-reachable but
> deliberately **never** MCP-exposed: an agent-invokable one-call posture
> scanner hands a prompt-injected session exactly the reconnaissance it
> needs. Classify them `_EXEMPT` with a pointer to this paragraph.

- **CLI-reachable:** a command under `cli/commands/` that calls the endpoint over
  HTTP via `cli/client.py`. State-changing endpoints also get a parity case in
  `tests/test_cli_api_parity.py`.
- **MCP-exposed:** either a static `@mcp.tool()` in `cli/mcp/server.py` that calls
  the endpoint, or a `tool_registry` passthrough row registered by
  `app/api/mcp/tools_generator.py`.

Refresh the endpoint inventory with `make update-openapi-snapshot` (generated by
`scripts/generate_openapi.py` into `tests/snapshots/openapi.json`).

**Enforcement reality:** structurally gated. `tests/test_api_docs_coverage.py`
fails if a public `/api/*` endpoint is undocumented;
`tests/test_documentation_api_triple_surface.py` is a ratchet that fails if a NEW
endpoint is neither classified as triple-surface (`_COHORT`, CLI + MCP verified)
nor consciously REST-only (`_EXEMPT`). Existing endpoints are grandfathered. The
review check below catches wiring *quality* the gates can't see (e.g. a CLI
command that exists but calls the wrong endpoint).
