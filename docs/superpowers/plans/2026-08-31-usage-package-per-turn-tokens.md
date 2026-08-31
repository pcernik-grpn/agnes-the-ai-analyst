# Usage Package + Per-Turn Tokens + Realtime Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the executor is `/agnes-build` (agnes-decomposer → agnes-builder workers → agnes-integrator); each builder MUST read `.claude/skills/agnes-conventions/SKILL.md` and the referenced playbook for its task type before writing code.

**Goal:** Fix the broken `/me/activity` token panels, record tokens per assistant turn (incl. cache) across Claude Code and all chat surfaces, make usage data appear in seconds instead of on a 10-minute tick, and put the internal `agnes_*` tables behind an admin-grantable "Agnes Usage" data package (own-rows only; admin sees everything).

**Architecture:** Three independent strands that meet at the end: (A) correctness fixes on the existing pipeline (identity key, cache capture), (B) a new PG-only `usage_turns` table fed by the usage processor (uploads) and ChatManager (live chat), (C) packaging + RBAC stack-gating of the internal tables with an explicit gate in `/api/query`'s internal short-circuit. A shared read-model + one contract test pins all four read surfaces to the same numbers.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic (PG app-state), DuckDB (frozen app-state pair + analytics), pytest (`shared_app`/`seeded_app` fixtures).

**Spec:** `docs/superpowers/specs/2026-08-31-usage-package-and-per-turn-tokens-design.md` — read it first; it carries the verified `file:line` evidence and the product decisions.

## Global Constraints

- **A3 PG-first ratchet:** no new DuckDB app-state repo, no new `src/db.py` `_vN_to_v(N+1)` step, ever. New schema = Alembic revision only (`migrations/versions/`), latest head is `0086_claims_audience`. PG-only repo resolution on DuckDB raises `RequiresPostgresBackend` → typed 501 (`app/main.py` handler).
- **Frozen pairs stay mirrored:** any method change in `src/repositories/usage.py` lands in `usage_pg.py` in the same task + contract test (`tests/db_pg/`).
- **RBAC tests use non-admin callers** — Admin god-mode short-circuits every check and makes visibility tests vacuous.
- **CHANGELOG:** every task adds its bullet under `## [Unreleased]` (integrator folds duplicates). RBAC task's bullet carries the `**BREAKING**` prefix.
- **Vendor-agnostic:** no customer names/hosts anywhere, `example.com` in fixtures.
- Run tests as `.venv/bin/pytest <paths> --tb=short -q` (worktrees symlink the shared venv). Per-task: the specific test file; pre-integration: `--lane impacted`, then `--lane fast`.
- **Do NOT touch** `app/api/v2_catalog.py` fetch_via hint or the `bucket="Agnes Internal"` comment in `connectors/internal/registry.py` — PR #1910 (separate branch) already fixes those; the integrator rebases over it.
- Every new/changed route declares audit posture in `src/audit_posture.py`; new audit actions register in `src/audit_events.py` `CATALOG`.

---

### Task 1: Fix `/me/activity` zero-token bug (identity key)

**Files:**
- Modify: `app/api/me_stats.py` (delete `_username_for_stats`, lines ~50-66; fix stale comment ~146)
- Modify: `src/repositories/usage.py` (five self-stats reads at ~760, ~782, ~813, ~848, ~879: `WHERE username = ?` → `WHERE user_id = ?`, rename the parameter `username` → `user_id`)
- Modify: `src/repositories/usage_pg.py` (mirror, ~925 and siblings — locate with `grep -n "WHERE username" src/repositories/usage_pg.py`)
- Test: `tests/test_me_stats_identity.py` (new), extend `tests/db_pg/test_usage_contract.py` (or the existing usage-cluster contract file found via `grep -rln "usage_session_summary" tests/db_pg/`)

**Interfaces:**
- Produces: `usage_repo().list_sessions_for_user_self(user_id: str)` and the three window/total aggregate methods now take `user_id` (the `users.id` UUID). Task 10 consumes these.

- [ ] **Step 1: Write the failing test.** Seed a summary row the way the pipeline writes it since v60 — `username` = full email, `user_id` = UUID — then call the me-stats API as that user and assert non-zero tokens:

```python
def test_me_activity_counts_tokens_for_email_keyed_rows(seeded_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(seeded_app)
    user = login_as_analyst(client)          # helper from tests/conftest.py; non-admin
    usage_repo().upsert_session_summary(     # exact method name: grep "def upsert" src/repositories/usage.py
        session_file=f"{user['id']}/s1.jsonl",
        session_id="s1", username=user["email"], user_id=user["id"],
        input_tokens=100, output_tokens=200,
        cache_read_tokens=300, cache_creation_tokens=40,
    )
    r = client.get("/api/me/stats/tokens", headers=user["headers"])   # confirm exact route via grep "me/stats" app/api/me_stats.py
    assert r.status_code == 200
    totals = r.json()
    assert totals_sum(totals) >= 300         # in+out present, not zero
```

- [ ] **Step 2: Run it — must FAIL** (today the filter compares `username` to the UUID): `.venv/bin/pytest tests/test_me_stats_identity.py -q`
- [ ] **Step 3: Implement.** In `me_stats.py` delete `_username_for_stats` and pass `user["id"]` straight through; in both repo backends switch the five reads to `WHERE user_id = ?` with renamed parameters. Correct the docstring and the `:146` comment (the runner writes `dir_name`, not `f"{username}/…"`).
- [ ] **Step 4: Both-backend proof.** Extend the usage contract test so it drives the renamed methods through DuckDB and PG parametrization. Run the contract file.
- [ ] **Step 5: Legacy rows.** Rows with `user_id IS NULL` (pre-v45) simply drop out of self-views — assert that in the test (seed one NULL-user_id row, totals unchanged). No backfill.
- [ ] **Step 6: CHANGELOG** Fixed bullet: "`/me/activity` token and session panels no longer show zero for every user (self-stats filtered on the wrong identity column since the v60 email canonicalization)."
- [ ] **Step 7: Commit.**

### Task 2: Pricing helper + config (cost computed at read)

**Files:**
- Create: `src/pricing.py`
- Modify: `app/chat/manager.py` (~86-87 constants, single use at ~4505)
- Modify: `config/instance.yaml.example` (new commented `pricing:` section)
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces: `ModelPrice` dataclass and

```python
def price_for_model(model: str | None) -> ModelPrice: ...
def cost_usd(model: str | None, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float: ...
```

  Task 10 consumes `cost_usd`.

- [ ] **Step 1: Failing tests** — known model returns defaults; `pricing.models` in instance config overrides; unknown model falls back to `pricing.default` else zero-cost; cache tokens priced (read multiplier < input price, write > input price per provider list pricing).

```python
def test_cost_usd_uses_config_override(monkeypatch):
    monkeypatch.setattr("src.pricing._pricing_config", lambda: {
        "models": {"claude-opus-5": {"input": 10.0, "output": 50.0,
                                     "cache_read": 1.0, "cache_write": 12.5}}})
    assert cost_usd("claude-opus-5", 1_000_000, 0) == pytest.approx(10.0)
    assert cost_usd("claude-opus-5", 0, 0, cache_read_tokens=2_000_000) == pytest.approx(2.0)

def test_unknown_model_is_zero_cost_not_crash():
    assert cost_usd("mystery-model", 5, 5) == 0.0
```

- [ ] **Step 2: Run → FAIL** (`src.pricing` missing).
- [ ] **Step 3: Implement** `src/pricing.py`: `DEFAULT_PRICES: dict[str, ModelPrice]` for current Claude models (match on model-id prefix so dated variants resolve), `_pricing_config()` reading `pricing` via `app.instance_config.get_value`, lookup order: exact config match → prefix config match → exact default → prefix default → `pricing.default` → `ModelPrice(0,0,0,0)`.
- [ ] **Step 4: Migrate the chat guardrail** — replace the `_PRICE_IN_PER_MTOK`/`_PRICE_OUT_PER_MTOK` constants in `app/chat/manager.py` with `cost_usd(...)` at the single call site; keep the guardrail's behavior test green (find it via `grep -rn "_PRICE_IN_PER_MTOK" tests/ app/`).
- [ ] **Step 5:** `instance.yaml.example` commented section documenting `pricing.models.<model-id>` + `pricing.default`, USD per MTok.
- [ ] **Step 6: CHANGELOG** Added bullet. **Commit.**

### Task 3: `usage_turns` table — Alembic + model + PG-only repo

**Files:**
- Create: `migrations/versions/0087_usage_turns.py` (`down_revision = "0086_claims_audience"`)
- Modify: the SQLAlchemy model module holding `usage_session_summary` (locate: `grep -rln "usage_session_summary" src/models/`) + `src/models/__init__.py`
- Create: `src/repositories/usage_turns_pg.py`
- Modify: `src/repositories/__init__.py` (PG-only `_REGISTRY` entry + `usage_turns_repo()` factory, pattern: `facts_repo` at ~945)
- Test: `tests/db_pg/test_usage_turns_repo.py`, `tests/test_usage_turns_requires_pg.py`

**Interfaces:**
- Produces:

```python
usage_turns_repo().insert_batch(rows: list[dict]) -> int      # ON CONFLICT (session_file, turn_uuid) DO NOTHING; returns inserted count
usage_turns_repo().list_for_session_file(session_file: str) -> list[dict]
usage_turns_repo().cache_totals_for_session_file(session_file: str) -> dict  # {"cache_read_tokens": int, "cache_creation_tokens": int}
usage_turns_repo().totals_for_user(user_id: str | None, since_days: int | None) -> dict  # per-model rows: model, input/output/cache_read/cache_creation sums
```

  Row dict keys = column names below. Tasks 4, 5, 10 consume these.

- [ ] **Step 1: Failing repo test** (PG fixture from `tests/db_pg/conftest.py`): insert two rows, re-insert the same `(session_file, turn_uuid)` → count unchanged; `totals_for_user` groups by model.
- [ ] **Step 2: Migration + model.** Columns: `id` (uuid pk default), `session_file` TEXT NOT NULL, `session_id` TEXT, `user_id` TEXT, `surface` TEXT NOT NULL DEFAULT `'claude_code'`, `turn_uuid` TEXT NOT NULL, `parent_uuid` TEXT, `model` TEXT, `input_tokens`/`output_tokens`/`cache_read_tokens`/`cache_creation_tokens` BIGINT NOT NULL DEFAULT 0, `occurred_at` TIMESTAMPTZ, `processor_version` INT NOT NULL DEFAULT 0, `extracted_at` TIMESTAMPTZ NOT NULL DEFAULT now. `UniqueConstraint("session_file", "turn_uuid")`, index on `(user_id, occurred_at)`. Follow `docs/migrations.md` §"Adding a PG-only feature" steps 1–8 exactly (model import in `src/models/__init__.py`, no `src/db.py` change).
- [ ] **Step 3: Repo + registry.** `usage_turns_pg.py` implements the four methods with plain SQLAlchemy; `_REGISTRY` entry carries only the PG backend; factory docstring notes PG-only + `RequiresPostgresBackend`.
- [ ] **Step 4: Fail-clean test:** on a DuckDB-backend instance `usage_turns_repo()` raises `RequiresPostgresBackend` (assert the exception type, and that no route change is needed yet — no new parameterless route in this task).
- [ ] **Step 5:** Ratchet guards must stay green: `.venv/bin/pytest tests/test_repository_registry.py tests/test_repository_registry_pg_first_ratchet.py tests/db_pg/test_repo_module_pg_first_ratchet.py -q` (a PG-only entry is the allowed shape; if a frozen-key pin fails, the pin list needs the documented one-line extension the test's failure message names).
- [ ] **Step 6: CHANGELOG** Added bullet. **Commit.**

### Task 4: Usage processor emits turns + single-file entry point

**Files:**
- Modify: `services/session_processors/usage_lib.py` (~397-417 token walk: also collect per-turn dicts)
- Modify: `services/session_processors/usage.py` (`process_session`: `insert_batch` when `use_pg()`; skip `chat-*.jsonl`)
- Modify: `services/session_pipeline/runner.py` (new `process_single_session(dir_name: str, filename: str) -> bool` reusing the existing identity-resolution + `session_processor_state` bookkeeping; bump `USAGE_PROCESSOR_VERSION`)
- Test: `tests/test_usage_processor_turns.py` (fixture jsonl with 2 assistant turns incl. cache fields)

**Interfaces:**
- Consumes: Task 3 repo methods.
- Produces: `process_single_session(dir_name, filename)` — Task 6 consumes it. Turn rows carry `surface='claude_code'`, `turn_uuid` = the jsonl assistant event `uuid`, `occurred_at` = event `timestamp`.

- [ ] **Step 1: Failing test** — run the processor over a fixture jsonl (write it in the test; two `"type": "assistant"` events with `message.usage` incl. `cache_read_input_tokens`), assert two `usage_turns` rows with exact token values and that re-processing inserts zero (idempotency via unique key + processor_state hash).
- [ ] **Step 2: Implement the walk** — per assistant event append `{turn_uuid, parent_uuid, model, 4 tokens, occurred_at}`; `process_session` batches them with `session_file`/`session_id`/`user_id`/`surface`. Guard: `if not use_pg(): skip` (log once at DEBUG); guard: `if session_file.rsplit("/",1)[-1].startswith("chat-"): skip turn emission` and instead overlay summary cache totals via `cache_totals_for_session_file` (Task 5 writes those rows; the overlay is a no-op until then).
- [ ] **Step 3: Single-file entry** — `process_single_session` resolves identity for `dir_name` exactly like the sweep loop (reuse `_identity_cache` population), runs only the `usage` processor for that one file, returns success bool, never raises (log + False).
- [ ] **Step 4: Tests green, bump `USAGE_PROCESSOR_VERSION` (9 → 10)** so existing sessions re-process once and backfill turns. **CHANGELOG** Added bullet. **Commit.**

### Task 5: Chat cache capture + live turn writes (all chat surfaces)

**Files:**
- Modify: `app/chat/runner.py` (~1735-1753: accumulate `cache_read_input_tokens`/`cache_creation_input_tokens` alongside in/out; put `cache_read`, `cache_creation` on the `assistant_message` frame next to `tokens_in`)
- Modify: `app/chat/manager.py` (~2323-2336 persist site: after `append_message`, write the turn row)
- Test: `tests/test_chat_usage_turns.py`

**Interfaces:**
- Consumes: Task 3 `insert_batch`.
- Produces: chat turn rows with `session_file = f"chat-{chat_id}.jsonl"`, `surface = live.surface` (the same value `chat_sessions.surface` holds: `web`/`slack_dm`/`slack_thread`/`telegram`/…), `turn_uuid = str(uuid4())`, `user_id` resolved from `live.user_email` via the users repo (nullable when unresolved), `occurred_at = now`.

- [ ] **Step 1: Failing test** — drive the manager's frame-persist path (pattern: existing tests found via `grep -rln "append_message" tests/`) with a frame carrying `tokens_in/out`, `cache_read`, `cache_creation`; assert one `usage_turns` row with all four values and `surface='web'`.
- [ ] **Step 2: Implement** — runner accumulates the two cache counters from `msg.usage` (AssistantMessage adds, ResultMessage overrides, mirroring in/out at 1750-1753); manager wraps the write in `try/except` + `use_pg()` guard (a chat turn must never fail because telemetry is down; log at WARNING).
- [ ] **Step 3: Assert independence** — test that `append_message` still succeeds and the frame flows when `usage_turns_repo` raises (monkeypatch it to raise; message persisted, no exception escapes).
- [ ] **Step 4: `chat_messages` untouched** — no schema change (Global Constraints); the turn row is the only home for chat cache tokens.
- [ ] **Step 5: CHANGELOG** Fixed bullet (cache tokens were dropped for chat). **Commit.**

### Task 6: Realtime ingest on upload

**Files:**
- Modify: `app/api/upload.py` (sessions endpoint, after the audit write ~189: `BackgroundTasks`)
- Test: `tests/test_upload_realtime_processing.py`

**Interfaces:**
- Consumes: Task 4 `process_single_session(dir_name, filename)`.

- [ ] **Step 1: Failing test** — `TestClient` POST a small jsonl to `/api/upload/sessions`; assert a `usage_session_summary` row exists for it **without** invoking the sweep (TestClient executes FastAPI background tasks on response completion).
- [ ] **Step 2: Implement** — add `background: BackgroundTasks` to the endpoint signature; after the audit block: `background.add_task(process_single_session, user_id, filename)`. No queue, no new service: the sweep (unchanged, 10 min) remains the catch-up for collector-ingested files and failed one-shots.
- [ ] **Step 3:** Audit posture unchanged (same route). Latency claim in CHANGELOG says "seconds after upload (previously up to one 10-minute tick)". **Commit.**

### Task 7: Seed the `agnes-usage` package + drop UI exclusions + manifest filter

**Files:**
- Modify: `connectors/internal/registry.py` (new `ensure_internal_package_seeded()`)
- Modify: `app/main.py` (call it right after `ensure_internal_tables_registered()` at ~1159)
- Modify: `app/web/router.py:7330` (package picker) and `:7158` (unpackaged tray) — remove the `source_type == 'internal'` skips
- Modify: `app/resource_types.py:250` (grant tree), `app/services/admin_dashboard.py:293` (signals) — same removal
- Modify: `app/api/sync.py` `_build_data_packages_section` (~1865-1890): skip member rows where `is_internal_table(table_id)`
- Test: `tests/test_internal_package_seed.py`, extend `tests/test_pull_sync.py`

**Interfaces:**
- Produces: a `data_packages` row with `slug='agnes-usage'`, `name='Agnes Usage'`, `status='prod'`, `publisher_kind='organization'`, `created_by='system_seed'`, members = every currently registered `INTERNAL_TABLES` id. Task 8's stack check consumes package membership; Task 9 relies on member reconciliation.

- [ ] **Step 1: Failing tests** — (a) fresh boot seeds the package with 3 members; (b) second boot is a no-op (idempotent); (c) a soft-deleted `agnes-usage` is NOT resurrected (`repo.get_by_slug` including deleted → skip; exact getter name: `grep -n "def get_by_slug\|slug" src/repositories/data_packages.py`); (d) a member the admin removed is NOT re-added when the package row exists — reconciliation only fills members on first creation, plus adds ids newly appearing in `INTERNAL_TABLES` that were never members (track via junction presence, keyed add-once per id).
- [ ] **Step 2: Implement seeding** using `data_packages_repo().create(name=…, slug="agnes-usage", description=…, status="prod", publisher_kind="organization", …)` + `add_table` per member (both backends work — `data_packages` is a frozen pair). Wrap in the same never-fatal try/except style `ensure_internal_tables_registered` uses.
- [ ] **Step 3: Remove the four exclusions**; run the templates/pages tests the grep for those lines points to (`.venv/bin/pytest --lane impacted`).
- [ ] **Step 4: Manifest filter** — internal members never enter `manifest.data_packages[].tables[]`; extend `tests/test_pull_sync.py:861-881` to assert a packaged `agnes_audit` is absent from the manifest and `agnes pull` has nothing to materialize. The `is_internal_table` guard at `app/api/sync.py:1727` stays (now load-bearing — say so in its comment).
- [ ] **Step 5: CHANGELOG** Added bullet. **Commit.**

### Task 8: RBAC stack-gating + `/api/query` gate (**BREAKING**)

**Files:**
- Modify: `src/rbac.py` (`can_access_table` ~97-111: principals keep internal access; dict users lose the short-circuit and fall through to the existing stack check at ~153-164. `get_accessible_tables` ~246-252 + ~306-311: unconditional append only for principals; dict users get internal ids via package membership like any table)
- Modify: `app/auth/access.py:280-284` (`can_access`: drop the internal carve-out, delegate to the standard path)
- Modify: `app/api/query.py` (~1708-1737 internal branch: gate before `_run_internal_query`)
- Modify: `docs/RBAC.md`, `docs/observability.md` (operator step: grant `agnes-usage` after upgrade)
- Test: `tests/test_internal_tables_stack_gated.py` (new), keep green: `tests/test_agent_scope_seams.py:184-193`, `tests/test_copresence_datapath.py:40-42`, `tests/test_query_internal_session_principal.py`

**Interfaces:**
- Consumes: Task 7's package (grant it in fixtures via `resource_grants` with `resource_type='data_package'`).

- [ ] **Step 1: Failing tests, all with NON-ADMIN callers:**

```python
def test_query_denied_without_package(pg_or_duck_client, analyst):
    r = pg_or_duck_client.post("/api/query", json={"sql": "SELECT COUNT(*) FROM agnes_sessions"},
                               headers=analyst.headers)
    assert r.status_code == 403
    assert "data package" in r.json()["detail"].lower()   # hint tells the next step (command-UX rule)

def test_query_allowed_with_package_grants_own_rows_only(...):
    grant_package_to_group("agnes-usage", analyst.group_id)
    # seed rows for analyst + another user; COUNT must equal analyst's rows only

def test_catalog_hides_internal_without_package(...):   # /api/v2/catalog
def test_mcp_query_path_gated(...)                      # through app/api/mcp/foundation_tools.py:1098 proxy
def test_admin_unscoped_unchanged(...)                  # admin still sees all rows
```

- [ ] **Step 2: Implement the query gate** inside the `internal_refs` branch (`app/api/query.py` ~1708), BEFORE `_run_internal_query`: for dict users, `for rid in internal_refs: if not can_access_table(user, rid, conn=None): raise HTTPException(403, detail=…hint…)`. Principals skip the loop (own-rows filter already binds them). This is the single most important edit in the plan — without it the RBAC change is an illusion (`/api/query` never consults the helpers on this path).
- [ ] **Step 3: Implement the rbac/access edits.** Order inside `can_access_table`: principal branch first (`if isinstance(user, PRINCIPAL_TYPES): return True if is_internal_table(table_id) else can_access_session(...)`), then the dict flow with no internal short-circuit. Document the principal carve-out as deliberate (spec §2).
- [ ] **Step 4: Blast-radius sweep** — `grep -rln "agnes_sessions\|agnes_telemetry\|agnes_audit" tests/ | xargs .venv/bin/pytest -q`; every test that assumed implicit visibility either gains a package-grant fixture line or (if it asserted the old contract) is updated with a comment pointing at this plan. The three principal-pinning tests must pass UNCHANGED.
- [ ] **Step 5: Docs + CHANGELOG** — `**BREAKING**` bullet: internal tables now require the `agnes-usage` data package; operator grants it post-upgrade; admin unaffected. **Commit.**

### Task 9: `agnes_turns` internal table

**Files:**
- Modify: `connectors/internal/access.py` (~69-96: append the 4th `InternalTable(registry_id="agnes_turns", source_table="usage_turns", filter_column="user_id", filter_kind="user_id", display_name="Agnes turns", description="Per-assistant-turn token usage (incl. cache) across Claude Code and chat. Postgres-backed instances only.")`)
- Modify: `connectors/internal/registry.py` (`ensure_internal_tables_registered`: register + include in `canonical_ids` only when `use_pg()`; on DuckDB the id is pruned/absent, never a broken row)
- Test: `tests/test_agnes_turns_internal_table.py`

**Interfaces:**
- Consumes: Tasks 3+7 (package member reconciliation picks the new id up on next boot via the add-once rule).

- [ ] **Step 1: Failing tests** — PG backend: `agnes_turns` registered, member of `agnes-usage`, non-admin with the package sees own rows only, `SELECT` routes through the internal materializer; DuckDB backend: id absent from registry and catalog, `SELECT … FROM agnes_turns` fails with the standard unknown-table error (assert it is NOT a 500).
- [ ] **Step 2: Implement.** `canonical_ids = [t.registry_id for t in INTERNAL_TABLES if use_pg() or t.registry_id != "agnes_turns"]`; same filter for the register loop. `INTERNAL_TABLES` itself stays a static tuple — `_TABLE_REF_RE`/`_INTERNAL_ALIAS_NAMES` keep their import-time build (an unregistered id matched in SQL resolves to "not registered" downstream, which Step 1 asserts).
- [ ] **Step 3:** Verify the generic materializer (`connectors/internal/access.py` `_materialized_internal_duckdb_*`) needs no per-table code (it copies `source_table` with `filter_column`; PG source path must read via the PG engine — confirm with the test, extend the source-read branch only if the test proves it DuckDB-only).
- [ ] **Step 4: CHANGELOG** Added bullet. **Commit.**

### Task 10: Shared read model, dashboard cost, consistency contract

**Files:**
- Create: `app/services/usage_stats.py`
- Modify: `app/api/me_stats.py`, `app/api/admin_usage.py` (telemetry KPIs), `app/api/admin_adoption.py` — route their session/token aggregates through the new module (response shapes unchanged; add `cost_usd` fields)
- Modify: `app/web/templates/me_activity.html` (Token usage tab: cost column; empty-state hint "No sessions have been uploaded to this server yet" + last `session.upload` audit timestamp when present)
- Test: `tests/test_usage_surfaces_agree.py`

**Interfaces:**
- Consumes: Task 1 (`user_id`-keyed repo reads), Task 2 (`cost_usd`), Task 3 (`totals_for_user`).
- Produces:

```python
# app/services/usage_stats.py
def token_totals(user_id: str | None, since_days: int | None) -> dict:
    """{"input":…, "output":…, "cache_read":…, "cache_creation":…, "cost_usd":…,
        "by_model":[{"model":…, four token sums, "cost_usd":…}, …]}
    user_id=None → instance-wide (admin surfaces only)."""
def sessions_for_user(user_id: str) -> list[dict]: ...
```

- [ ] **Step 1: Failing consistency test** — seed two users' summaries (+turns on PG); as non-admin analyst (package granted): `/api/me/stats/*`, a `SELECT SUM(...) FROM agnes_sessions`, and as admin: `/api/admin/telemetry/kpis`, `/api/admin/adoption` for that user — assert the four report identical input/output/cache totals for the same window. This test is the regression net for "the numbers must agree".
- [ ] **Step 2: Implement** `usage_stats.py` over `usage_repo()` (summaries; DuckDB+PG) + `usage_turns_repo()` (per-model breakdown, PG only — `except RequiresPostgresBackend: by_model=[]`), cost via `src.pricing.cost_usd` per model row (summary fallback uses `primary_model`).
- [ ] **Step 3: Wire the three APIs** — mechanical delegation, keep every existing response key; add `cost_usd`. Template: cost column + empty-state hint (server-side render, `ds.*` tokens only, no raw hex — design-system contract).
- [ ] **Step 4:** Extend the consistency test with a chat session (turns from Task 5 + summary overlay from Task 4) on PG so cache tokens agree across chat and CLI paths. **CHANGELOG** Added/Changed bullets. **Commit.**

### Task 11: Verification sweep + docs finish

**Files:**
- Modify: `CHANGELOG.md` (dedupe task bullets under one `## [Unreleased]` pass), `docs/RBAC.md`, `docs/observability.md` (cross-check both carry: package flow, per-turn table, realtime note, pricing config)
- No new code.

- [ ] **Step 1:** `python scripts/verify_syncmap.py` — fix every row it names (command-UX rows will fire for the 403 hint; REST×CLI×MCP row for the gated query path).
- [ ] **Step 2:** `.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`, then `--lane fast`.
- [ ] **Step 3:** Re-read the spec top to bottom against the diff (spec-coverage pass); update the spec's Status line to "Implemented (PR #1912)".
- [ ] **Step 4: Commit; hand off to `/agnes-review`.**

---

## Dependency graph (for the decomposer)

- Independent starts: **1, 2, 3, 7**
- **4 → 3**; **5 → 3**; **6 → 4**; **8 → 7**; **9 → 3+7+8**; **10 → 1+2+3(+5 for the chat leg)**; **11 last**
- Migration serialization rule: Task 3 is the only Alembic task — no other task adds a revision, so no ladder conflict; the integrator applies it before 4/5/9/10.
