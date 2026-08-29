# Audit Full Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "every user/admin interaction leaves exactly one classified `audit_log` row" hold by construction: a typed action catalog, request context filled automatically, a middleware safety net for unannotated routes, closure of every known per-surface gap, a client-reported channel for offline CLI queries, and a bridge that materializes web-chat sessions into the analyst-sessions store.

**Architecture:** Audit today is opt-in per handler (~298 manual call sites, no middleware, no CI guard) — 155 of 352 mutating routes and several whole surfaces (SSO login, MCP tool calls, agent invocation, Telegram, worker jobs) write nothing. This plan (1) centralizes emission through `log_safe` with a typed catalog and contextvar-fed `client_ip`/`correlation_id`/`client_kind`, (2) adds an ASGI fallback that logs any authenticated mutating request whose handler wrote no row, plus a route-posture ratchet test so new routes must declare their audit stance, (3) closes the named gaps domain by domain, (4) adds a batched client-reported upload for local CLI query events, and (5) synthesizes chat transcripts into `${SESSION_DATA_DIR}` so the existing session pipeline and every read surface pick them up unchanged.

**Tech Stack:** FastAPI + pure-ASGI middleware, contextvars, DuckDB + Postgres dual-backend repos (`src/repositories/audit.py` / `audit_pg.py` — frozen pre-A3 pair, both sides in the same PR), pytest with `shared_app`/`seeded_app` fixtures.

**Spec:** The findings + phase design live in the session report (private artifact); the operative content is reproduced in this plan. Prior art: `docs/superpowers/specs/2026-07-28-activity-center-consistency-design.md` (naming/no-history-rewrite decisions this plan inherits).

## Global Constraints

- **Base branch: `integration`** (commit `4cdf2cc8f` at plan re-verification time). The findings were re-verified against this base on 2026-08-28: the SSO/magic-link login gap has since been closed upstream (`app/auth/login_audit.py` — actions `login_success`, `account_activated`, `setup_link_requested`, all five providers wired, guard test `tests/test_audit_login.py`), and `src/repositories/audit.py`/`audit_pg.py` gained `query_unified`/`_UNIFIED_UNION_SQL` and a `kpis(trail=...)` parameter — do not disturb those. Every other gap in this plan was re-confirmed present on this base.

- **Dual backend:** any change to `AuditRepository` must land in `src/repositories/audit.py` AND `src/repositories/audit_pg.py` in the same task, with the cross-engine contract test extended (`tests/db_pg/`). No new repos, no schema migrations anywhere in this plan (the v40 columns `client_ip`, `client_kind`, `correlation_id`, `params_before` already exist on both ladders).
- **Never build the app per test:** request `shared_app` / `seeded_app` fixtures (see CLAUDE.md → Writing tests). Guard: `tests/test_shared_app_contract.py`.
- **Audit failure must never fail the request it describes:** all new emission goes through `src.audit_helpers.log_safe`. Never call `audit_repo().log()` directly outside the repo layer.
- **No history rewrite:** legacy action names keep flowing from unmigrated call sites; the catalog records them as aliases. Read-side classification only (same decision as the Activity Center spec).
- **Content never, metadata always:** audit params carry identity, action, resource, hashes/sizes — never prompt text, SQL text, or payload bodies. Follow the existing `hash_args` pattern (`app/chat/audit.py:69`).
- **Vendor-agnostic:** no customer-specific names anywhere (repo rule).
- **CHANGELOG:** every task adds its own bullet under `## [Unreleased]` (grouped Added/Fixed), no version bumps.
- **Local verification:** `scripts/verify_syncmap.py` + the specific test files the task touches; the full suite runs in CI on the draft PR (open one after the first commit).
- Task 1 must merge (or at least be the base) before Tasks 2–9 start; Tasks 2–9 are mutually independent except where an Interfaces block says otherwise.

---

### Task 1: F0 — action catalog + automatic request context

**Files:**
- Create: `src/audit_events.py`
- Modify: `src/audit_context.py`
- Modify: `src/audit_helpers.py`
- Modify: `app/middleware/audit_timing.py`
- Modify: `src/repositories/audit.py` (the `log()` method), `src/repositories/audit_pg.py` (same)
- Modify: `app/auth/dependencies.py` (stamp audit identity after user resolution)
- Test: `tests/test_audit_catalog.py`, `tests/test_audit_context_autofill.py`, extend the existing `tests/db_pg/test_audit_contract.py` (present on the `integration` base) with one autofill assertion parametrized over both backends. Note the repo files on this base already carry `query_unified`/`_UNIFIED_UNION_SQL`/`kpis(trail=...)` — leave them untouched; only `log()` changes.

**Interfaces (produced — later tasks rely on these exact names):**
- `src/audit_events.py`:
  - `CATEGORY = Literal["auth", "mutation", "read", "system"]`
  - `@dataclass(frozen=True) class AuditEvent: action: str; category: str; description: str = ""`
  - `CATALOG: dict[str, AuditEvent]` — one entry per action name; seed with every action string currently emitted (the ~205 collected below) plus the new ones Tasks 2–9 register (each later task ADDS its entries here — append-only dict literals, low merge conflict).
  - `LEGACY_ALIASES: dict[str, str]` — read-side only mapping (e.g. `"km_approve": "corporate_memory.approve"`); no writer migration in this plan.
  - `DYNAMIC_ACTION_PREFIXES: tuple[str, ...] = ("run_session_processor:", "corporate_memory.", "authoring_suggestion.", "agent.memory.", "store.", "mcp_source.", "mcp_tool.", "data_app.", "marketplace.", "user_group.", "resource_grant.", "recipe.", "data_package.", "memory_domain.", "knowledge_digest.", "user.", "initial_workspace.")` — f-string writers whose suffix varies; the guard accepts any action starting with one of these.
  - `def is_cataloged(action: str) -> bool` — exact hit in `CATALOG` or prefix hit in `DYNAMIC_ACTION_PREFIXES`.
- `src/audit_context.py` additions (existing `mark_request_start`/`auto_duration_ms` unchanged):
  - `def set_request_meta(*, client_ip: str | None, correlation_id: str | None) -> None`
  - `def auto_client_ip() -> str | None`, `def auto_correlation_id() -> str | None`
  - `def set_client_kind(kind: str) -> None`, `def auto_client_kind() -> str | None`
  - `def set_audit_identity(user_id: str | None, email: str | None) -> None`, `def auto_audit_identity() -> tuple[str | None, str | None]`
  - `def mark_audit_written() -> None`, `def audit_written_count() -> int`
- `src/audit_helpers.py`: `CLIENT_KINDS = ("web", "cli", "mcp", "slack", "telegram", "agent", "broker", "scheduler")` and `client_kind_from_user` extended to return `"mcp"` for `token_type == "mcp_oauth"`-style principals (see step 6).
- Both `AuditRepository.log()` implementations autofill `client_ip`, `correlation_id`, `client_kind` from the `auto_*` functions when the caller passed `None`, and call `mark_audit_written()` on success.

**Steps:**

- [ ] **Step 1: Failing test — catalog exists and knows every emitted action**

```python
# tests/test_audit_catalog.py
import re
from pathlib import Path

import pytest

from src.audit_events import CATALOG, is_cataloged

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "src", "services", "cli")
# action="..." literals passed to audit writers; single- or double-quoted
ACTION_RE = re.compile(r"""action\s*=\s*(?:f?["'])([a-zA-Z0-9_.:{}-]+)["']""")


def _emitted_literals() -> set[str]:
    out: set[str] = set()
    for d in SCAN_DIRS:
        for p in (REPO / d).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if not any(w in text for w in ("log_safe(", "write_audit(", "audit_repo(")):
                continue
            for m in ACTION_RE.finditer(text):
                lit = m.group(1)
                if "{" in lit:  # f-string with a dynamic tail — prefix rule covers it
                    continue
                out.add(lit)
    return out


def test_every_emitted_action_is_cataloged():
    missing = sorted(a for a in _emitted_literals() if not is_cataloged(a))
    assert not missing, (
        "Actions emitted but not registered in src/audit_events.py CATALOG "
        f"(register them, don't rename silently): {missing}"
    )


def test_catalog_categories_are_valid():
    for ev in CATALOG.values():
        assert ev.category in ("auth", "mutation", "read", "system"), ev.action
```

- [ ] **Step 2: Run it — fails with `ModuleNotFoundError: src.audit_events`.**

Run: `.venv/bin/pytest tests/test_audit_catalog.py -x -q`

- [ ] **Step 3: Write `src/audit_events.py`.** Build the seed catalog by running the scan from the test locally (`python -c "from tests.test_audit_catalog import _emitted_literals; print(sorted(_emitted_literals()))"`) and classifying each action: `auth` (login/token/bootstrap/cli_auth/cowork_bundle), `read` (query.*, catalog.*, data.*, snapshot.estimate, activity.read, adoption.*, usage.summary/export, session_download, session.transcript_view, attachment.download, knowledge.*_download, manifest.fetch, access_policy.preview, data.access_check), `system` (run_*, *.tick, startup.*, broker_*, kai_credential_scope_mismatch, store.submission.review_error/bg_verdict_skipped), everything else `mutation`. Include the docstring note that the catalog is append-only and that renames are forbidden (aliases only).

- [ ] **Step 4: Run the catalog test until green.** Iterate on missing entries; add genuinely dynamic writers' prefixes to `DYNAMIC_ACTION_PREFIXES` instead of enumerating their products.

- [ ] **Step 5: Failing test — `log()` autofills context**

```python
# tests/test_audit_context_autofill.py
from src import audit_context


def test_log_autofills_context_fields(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    audit_context.set_request_meta(client_ip="203.0.113.7", correlation_id="rid-abc123")
    audit_context.set_client_kind("mcp")
    audit_repo().log(user_id="u1", action="catalog.list")
    rows, _ = audit_repo().query(action="catalog.list", limit=1)
    assert rows[0]["client_ip"] == "203.0.113.7"
    assert rows[0]["correlation_id"] == "rid-abc123"
    assert rows[0]["client_kind"] == "mcp"


def test_explicit_kwargs_beat_context(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    audit_context.set_request_meta(client_ip="203.0.113.7", correlation_id="rid-abc123")
    audit_repo().log(user_id="u1", action="catalog.list", client_ip="198.51.100.9")
    rows, _ = audit_repo().query(action="catalog.list", limit=1)
    assert rows[0]["client_ip"] == "198.51.100.9"


def test_audit_written_marker(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    before = audit_context.audit_written_count()
    audit_repo().log(user_id="u1", action="catalog.list")
    assert audit_context.audit_written_count() == before + 1
```

- [ ] **Step 6: Implement.**
  - `src/audit_context.py`: four new `ContextVar`s (`_request_meta: ContextVar[tuple[str | None, str | None] | None]`, `_client_kind: ContextVar[str | None]`, `_audit_identity: ContextVar[tuple[str | None, str | None] | None]`, `_written: ContextVar[int]` default 0) with the accessor functions from the Interfaces block. Same file/style as the existing timing var.
  - Both `AuditRepository.log()` bodies: after the `duration_ms` autofill, add `if client_ip is None: client_ip = auto_client_ip()` (same for `correlation_id`, `client_kind`), and `mark_audit_written()` just before `return`.
  - `app/middleware/audit_timing.py`: in the existing `if scope["type"] == "http":` branch, additionally build `request = Request(scope)` (import from `starlette.requests`), call `set_request_meta(client_ip=trusted_client_ip(request), correlation_id=request_id_var.get())` (imports: `app.auth.client_ip.trusted_client_ip`, `app.logging_config.request_id_var`). Note in the module docstring that this middleware must stay mounted INSIDE `RequestIdMiddleware` (it already is — `app/main.py:2584` mounts RequestId after, i.e. outermost).
  - `app/auth/dependencies.py`: at the point where the current user dict is finally resolved (the shared `get_current_user` return path), add `set_audit_identity(*identity_for_audit(user)); set_client_kind(client_kind_from_user(user))` — one place, every authenticated request. Do NOT override a kind already set to a non-web value (`if auto_client_kind() in (None, "web")`) so surface-specific stamps (mcp/slack/telegram, later tasks) survive.
  - `src/audit_helpers.py`: add `CLIENT_KINDS`; extend `client_kind_from_user` — before the PAT check, return `"mcp"` when `user.get("token_type") == "mcp_oauth"`. Then locate where MCP OAuth JWTs are resolved into the user dict (`app/auth/mcp_oauth.py` — the `typ="session"` mint at `:297,401` and its verify path) and make the resolver stamp `token_type="mcp_oauth"` on the returned user dict so the helper can see it. Fix the four hardcoded `client_kind="web"` sites (`app/api/activity.py:72`, `app/api/admin_sessions.py:278`, `app/api/admin_adoption.py:115`, `app/api/memory_domain_suggestions.py:113`) to pass nothing (context autofill now supplies the right value).
- [ ] **Step 7: Run both test files + the db_pg audit contract test; all green.** Extend the contract test with one autofill assertion parametrized over both backends.
- [ ] **Step 8: Static emitter guard (ratchet).** New test in `tests/test_audit_catalog.py`:

```python
KNOWN_DIRECT_LOG_CALLERS = frozenset({
    # frozen pre-existing direct audit_repo().log() call sites (file paths);
    # new code must use src.audit_helpers.log_safe instead.
})


def test_no_new_direct_audit_log_callers():
    offenders = set()
    for d in SCAN_DIRS:
        for p in (REPO / d).rglob("*.py"):
            rel = str(p.relative_to(REPO))
            if rel.startswith("src/repositories/") or rel == "src/audit_helpers.py":
                continue
            if "audit_repo().log(" in p.read_text(encoding="utf-8", errors="replace"):
                offenders.add(rel)
    new = offenders - KNOWN_DIRECT_LOG_CALLERS
    removed = KNOWN_DIRECT_LOG_CALLERS - offenders
    assert not new, f"New direct audit_repo().log() callers — use log_safe: {sorted(new)}"
    assert not removed, f"Prune KNOWN_DIRECT_LOG_CALLERS, these migrated: {sorted(removed)}"
```

  Seed `KNOWN_DIRECT_LOG_CALLERS` from the scan output (run the test, paste the offender list). This freezes the debt without a 66-file migration.
- [ ] **Step 9: CHANGELOG bullet (Added), `scripts/verify_syncmap.py`, commit.**

---

### Task 2: F1 — fallback middleware + route-posture ratchet

**Files:**
- Create: `app/middleware/audit_fallback.py`
- Create: `src/audit_posture.py`
- Modify: `app/main.py` (mount fallback middleware next to `AuditTimingMiddleware`, `app/main.py:2589-2591`)
- Modify: `CONTRIBUTING.md` (sync-map row: "new HTTP route ⇒ declare audit posture in `src/audit_posture.py`")
- Test: `tests/test_audit_fallback_middleware.py`, `tests/test_audit_route_posture.py`

**Interfaces:**
- Consumes from Task 1: `audit_written_count()`, `auto_audit_identity()`, `auto_client_kind()`, `log_safe`, catalog action `http.request` (register it in `CATALOG` as category `mutation`).
- Produces: `src/audit_posture.py` with
  - `POSTURE: dict[str, str]` keyed `"METHOD /path/template"` (e.g. `"POST /api/admin/configure"`), value = a catalog action name, or `"fallback"` (covered only by the generic middleware row — the shrinking debt list), or `"exempt:<reason>"` (deliberately unaudited).
  - `MUTATING = ("POST", "PUT", "PATCH", "DELETE")`
  - Tasks 3–9 flip entries from `"fallback"` to real action names as they close domains.

**Steps:**

- [ ] **Step 1: Failing middleware test**

```python
# tests/test_audit_fallback_middleware.py
def test_unannotated_mutation_gets_generic_row(tmp_path, monkeypatch, seeded_app, seeded_tokens):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(seeded_app)
    # pick any mutating route known to write no audit row itself, e.g.:
    r = client.post("/api/stack/subscribe", json={...}, headers=auth(seeded_tokens.analyst))
    from src.repositories import audit_repo
    rows, _ = audit_repo().query(action="http.request", limit=1)
    assert rows and rows[0]["resource"] == "POST /api/stack/subscribe"
    assert rows[0]["user_id"]  # identity came from the auth contextvar


def test_audited_route_gets_no_duplicate(tmp_path, monkeypatch, seeded_app, seeded_tokens):
    # a route that already audits (e.g. POST /api/sync/trigger) must NOT
    # additionally produce an http.request row
    ...


def test_unauthenticated_request_writes_nothing(...):
    ...
```

(Adapt fixture names to what `tests/conftest.py` actually provides — `seeded_app` ships four role users + tokens per CLAUDE.md.)

- [ ] **Step 2: Run — fails (no `http.request` rows).**
- [ ] **Step 3: Implement `AuditFallbackMiddleware`** (pure ASGI, same pattern as `audit_timing.py`):

```python
class AuditFallbackMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            await self.app(scope, receive, send)
            return
        status_holder = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if audit_written_count() > 0:
            return
        user_id, _email = auto_audit_identity()
        if user_id is None:
            return  # unauthenticated → not an audit_log concern
        route = scope.get("route")
        template = getattr(route, "path", scope.get("path", "?"))
        key = f"{scope['method']} {template}"
        if POSTURE.get(key, "").startswith("exempt:"):
            return
        log_safe(
            user_id=user_id,
            action="http.request",
            resource=key,
            params={"status": status_holder.get("status")},
        )
```

  Mount it in `app/main.py` immediately BEFORE the `AuditTimingMiddleware` mount (so timing/meta stamping wraps it and the contextvars are populated when it runs — verify order empirically in the test; Starlette's add-order-inverted stack makes the later `add_middleware` call outermost).
- [ ] **Step 4: Run middleware tests green.**
- [ ] **Step 5: Failing posture ratchet test**

```python
# tests/test_audit_route_posture.py
from src.audit_events import is_cataloged
from src.audit_posture import MUTATING, POSTURE


def _mutating_routes(app):
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods & set(MUTATING):
            out.add(f"{m} {r.path}")
    return out


def test_every_mutating_route_declares_posture(shared_app):
    routes = _mutating_routes(shared_app)
    undeclared = sorted(routes - POSTURE.keys())
    stale = sorted(POSTURE.keys() - routes)
    assert not undeclared, (
        "New mutating routes must declare audit posture in src/audit_posture.py "
        f"(a real action name, 'fallback', or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune POSTURE, routes gone: {stale}"


def test_posture_values_are_valid():
    for key, v in POSTURE.items():
        assert v == "fallback" or v.startswith("exempt:") or is_cataloged(v), (key, v)
```

- [ ] **Step 6: Seed `POSTURE`.** Run the test, paste the undeclared list; map the ~196 already-audited routes to their action names (the route-inventory table below in the appendix gives module→action mapping), the 155 unaudited ones to `"fallback"`, and a minimal exempt set (`"exempt:health"` for probes if any register as mutating, WS routes don't appear here). Iterate to green.
- [ ] **Step 7: Sync-map row in `CONTRIBUTING.md` + CHANGELOG bullet + commit.**

---

### Task 3: F2a — auth remainder (token reads, project import, posture flips)

> **Re-scoped 2026-08-28:** the SSO/magic-link/password login gap was closed upstream on `integration` (`app/auth/login_audit.py`: `login_success`, `account_activated`, `setup_link_requested`; guard test `tests/test_audit_login.py`). Do NOT re-implement provider login audit. This task covers only what upstream left open.

**Files:**
- Modify: `app/api/tokens.py` (the three GET routes: `/auth/tokens`, `/auth/tokens/{token_id}`, `/auth/admin/tokens`), `app/api/keboola_login_projects.py` (`POST /api/auth/keboola/projects`)
- Modify: `src/audit_events.py` (register), `src/audit_posture.py` (flip entries)
- Test: `tests/test_audit_gap_auth_remainder.py`

**Interfaces:**
- Consumes: `log_safe`, autofill context (Task 1).
- Produces catalog entries: `token.list` (read — params say which scope: `{"scope": "own"|"one"|"admin_all"}`), `keboola.projects_import` (mutation). The upstream auth actions (`login_success`, `login_failed`, `account_activated`, `setup_link_requested`, `token_created`, `password_changed`, `password_change_failed`) get registered in `CATALOG` by Task 1's scan seed — verify they are present, add them if the seed missed any.

**Steps:**

- [ ] **Step 1: Failing tests.** `GET /auth/admin/tokens` (admin) → one `token.list` row with `params={"scope": "admin_all"}`; `GET /auth/tokens` (own) → `{"scope": "own"}`; `POST /api/auth/keboola/projects` (stub the upstream verification the module already stubs in its own tests) → `keboola.projects_import` row.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement.** `log_safe(...)` after authorization resolves; identity/kind/ip come from autofill.
- [ ] **Step 4: Flip the auth-family rows in `POSTURE` (send-link/verify/setup/reset routes now map to the upstream actions; the two routes touched here map to the new actions); run posture + tests green.**
- [ ] **Step 5: CHANGELOG (Added: "audit rows for PAT enumeration and project-import; auth route posture mapped"), commit.**

---

### Task 4: F2b — agent invocation + worker/job execution + silent scheduler jobs

**Files:**
- Modify: `app/api/agent_runtime.py` (POST `/api/v1/agents/{slug}/responses`), `app/api/agent_sessions.py` (create/message/cancel/delete), `app/api/agent_webhooks.py` (create/delete), `app/worker/kinds.py` (one dispatch-level wrapper, not per-kind), `app/api/bq_metadata_refresh.py`, `app/api/keboola_semantic_layer_refresh.py`, `app/api/databricks_semantic_layer_refresh.py`, `app/api/store_lint_admin.py`
- Modify: `src/audit_events.py`, `src/audit_posture.py`
- Test: `tests/test_audit_gap_agent_runtime.py`, `tests/test_audit_gap_worker.py`

**Interfaces:**
- Produces catalog entries: `agent.invoke` (mutation), `agent.session.create/message/cancel/delete` (mutation), `agent.webhook.create/delete` (mutation), `job.run` (system), `run_bq_metadata_refresh` / `run_keboola_semantic_layer_refresh` / `run_databricks_semantic_layer_refresh` / `run_store_lint_audit` (system — the `run_*` prefix keeps `SCHEDULER_ACTION_SQL` liveness detection working, `src/audit_helpers.py:30`).

**Steps:**

- [ ] **Step 1: Failing tests.** `agent.invoke`: POST `/responses` with a stubbed runtime → row with `resource=f"agent:{slug}"`, `params={"session": ..., "mode": sync|job}`, `client_kind` from context. Worker: enqueue + run a trivial job kind through the worker's dispatch entry point (find the single function in `app/worker/kinds.py` that looks up and invokes a kind handler) → one `job.run` row with `params={"kind": ..., "outcome": "success"|"error", "job_id": ...}` and `duration_ms` measured around the handler (pass explicitly — no HTTP context in the worker). The four scheduler endpoints: POST each (admin token) → its `run_*` row (follow the existing pattern at `app/api/admin.py:7480` — tick row with `client_kind="scheduler"` when the scheduler token calls, autofill handles it now).
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement.** Worker wrapper: `t0 = time.monotonic()` around the kind handler; `log_safe(user_id=None, action="job.run", resource=f"job:{kind}", params={...}, result=..., duration_ms=int(...), client_kind="scheduler")` in a `finally`-adjacent success/except structure. Agent invoke: log after auth/scope resolution, before streaming starts (so a crashed stream still has the row); `result` updated is NOT required — one row per invocation, outcome in a second row only on error (`result="error:<class>"`), matching the query.remote error-path precedent (`app/api/query.py:2068`).
- [ ] **Step 4: Flip POSTURE entries, run green, CHANGELOG, commit.**

---

### Task 5: F2c — MCP surface (tool calls, passthrough, per-table, facts tools)

**Files:**
- Modify: `app/api/mcp/tools_generator.py` (or the single dispatch point wrapping foundation tool invocation — locate where registered tool handlers are called; wrap there, NOT per tool in `foundation_tools.py`), `app/api/mcp_passthrough.py`, `app/api/mcp_per_table.py`, `app/api/facts.py` (search/neighbors/claims/ingest)
- Modify: `src/audit_events.py`, `src/audit_posture.py`
- Test: `tests/test_audit_gap_mcp.py` (extend `tests/test_mcp_tool_parity.py` neighborhood conventions)

**Interfaces:**
- Consumes: `set_client_kind("mcp")` (Task 1) — stamp it in the MCP sub-app's auth/session resolution so every row logged during an MCP call carries `client_kind="mcp"` even when the tool proxies an internal HTTP endpoint.
- Produces catalog entries: `mcp.tool_call` (read — the call itself; the proxied endpoint's own row still records the data access), `mcp.passthrough_call` (read), `mcp.passthrough_denied` (system), `query.table_scoped` (read — the `mcp_per_table` endpoint), `facts.search` / `facts.neighbors` / `facts.claims` (read), `facts.ingest` (mutation).

**Steps:**

- [ ] **Step 1: Failing tests.** Drive one foundation tool through the MCP dispatch layer (in-process, follow how `tests/test_mcp_tool_parity.py` builds the tool registry) → `mcp.tool_call` row with `params={"tool": name, "args_hash": hash_args(args)}` (reuse `app/chat/audit.py:hash_args` — move it to `src/audit_helpers.py` and re-export from the old location to avoid import churn). Passthrough: allowed call → `mcp.passthrough_call` with `resource=f"mcp_source:{source_id}"`; policy-denied → `mcp.passthrough_denied` with `result="denied"`. `POST /api/mcp/query-table/{id}` → `query.table_scoped`. Facts endpoints → their rows.
- [ ] **Step 2–3: Run/implement.** One wrapper around tool invocation; `set_client_kind("mcp")` at MCP session resolution (both the SSE mount and `mcp_streamable.py` share an auth path — find it once).
- [ ] **Step 4: Flip POSTURE, green, CHANGELOG, commit.**

---

### Task 6: F2d — messaging surfaces (Telegram, Slack inbound, chat lifecycle)

**Files:**
- Modify: `services/telegram_bot/bot.py`, `services/telegram_bot/runner.py`, `services/slack_bot/events.py`, `services/slack_bot/commands.py`, `services/slack_bot/interactivity.py`, `app/api/chat.py` (session lifecycle routes), `app/chat/manager.py` (user_msg ingress), `app/api/chat_copresence.py` (invite/join/leave)
- Modify: `src/audit_events.py`, `src/audit_posture.py`
- Test: `tests/test_audit_gap_messaging.py`

**Interfaces:**
- Consumes: `write_audit` (chat-side writer, already backend-correct) for in-manager events; `log_safe` for route handlers; `client_kind="telegram"` / `"slack"` passed explicitly by the bots (they run outside HTTP request context — autofill returns None there, so the explicit kwarg matters).
- Produces catalog entries: `telegram.bind` (auth), `telegram.message` (read), `telegram.script_run` (mutation — **the sudo path**), `slack.message` (read), `slack.command` (mutation), `chat.session.create/delete/archive/ticket` (mutation), `chat.user_message` (read), `chat.copresence.invite/join/leave` (mutation).

**Steps:**

- [ ] **Step 1: Failing tests.** Highest value first: `telegram.script_run` — drive `handle_callback_query` with a stubbed `run_user_script` → row with `params={"script": name, "os_user": username}`, `result` reflecting the subprocess outcome, `client_kind="telegram"`. `chat.user_message` — through the manager's user_msg ingress (existing chat-manager tests show the harness) → row with `params={"session_id": ..., "chars": len(text)}` — **never the text**. Slack DM/mention/command → one row each with the resolved user. Chat REST lifecycle routes → rows.
- [ ] **Step 2–3: Run/implement.** Bots resolve `users.id` via their existing binding lookup (`services/slack_bot/binding.py` pattern); unresolvable → email string (documented fallback, same as `app/chat/audit.py`).
- [ ] **Step 4: Flip POSTURE (chat routes), green, CHANGELOG, commit.**

---

### Task 7: F2e — secrets, admin config, distribution channels, ingress

**Files:**
- Modify: `app/api/admin_source_connections.py` (full CRUD + secret set/clear + test), `app/api/mcp_user_secrets.py` (set/clear/test/read), `app/api/admin.py` (`/api/admin/configure`, server-config/overlay reads), `app/api/scripts.py` (deploy/run/delete), `app/api/admin_datasource_secrets.py` + `app/api/admin_slack_secrets.py` (GET reads), `app/api/tokens.py` (covered in Task 3 if merged first — skip duplicates), `app/marketplace_server/router.py` (zip endpoints), `app/marketplace_server/git_router.py` (GET/POST), `app/api/store.py` (`/api/store/bundle.zip`), `app/api/memory.py` (`/api/memory/bundle`), `app/api/jira_webhooks.py` (received/rejected), `app/api/upload.py` (`/artifacts`, `/local-md`), `app/api/sync.py` (pull-confirm, settings, table-subscriptions), `app/api/observability.py` (facets/kpis self-audit — reuse action `activity.read` so the existing self-read exclusion covers them)
- Modify: `src/audit_events.py`, `src/audit_posture.py`
- Test: `tests/test_audit_gap_secrets_distribution.py`

**Interfaces:**
- Produces catalog entries: `source_connection.create/update/delete/test` (mutation), `source_connection.secret.set/clear` (mutation), `mcp_user_secret.set/clear/test` (mutation), `datasource.secret.read` / `slack.secret.read` / `mcp_user_secret.read` / `server_config.read` (read), `instance.configure` (mutation — the `/api/admin/configure` env/instance writer), `script.deploy/run/delete` (mutation), `marketplace.bundle_download` / `marketplace.git_fetch` / `marketplace.git_push` (read/read/mutation), `store.bundle_download` (read), `memory.bundle_download` (read), `webhook.jira_received` (system), `webhook.jira_rejected` (system, `result="denied"`), `artifact.upload` / `local_md.upload` (mutation), `sync.pull_confirmed` / `sync.settings_update` / `sync.subscriptions_update` (mutation).

**Steps:**

- [ ] **Step 1: Failing tests, grouped by module.** Secrets reads: GET returns 200 AND a `*.secret.read` row exists (params never contain the secret value — assert the params dict has no key whose value equals the seeded secret). `instance.configure`: params list the CHANGED KEYS only, never values (`params={"keys": sorted(changed)}`). Marketplace git: one row per fetch/push with `resource=f"marketplace:{slug}"`, the PAT-resolved user; zip: one row per download. Jira webhook: valid signature → `webhook.jira_received` with `params={"event": type}` (existing file trail stays as payload storage); invalid → `webhook.jira_rejected`, `result="denied"`, `user_id=None`.
- [ ] **Step 2–3: Run/implement.** For `git_router.py` note the PAT Basic-auth in-handler resolution — log after it so `user_id` is real.
- [ ] **Step 4: Flip POSTURE (~40 entries leave `"fallback"`), green, CHANGELOG, commit.**

---

### Task 8: F4 — chat sessions bridge into the analyst-sessions store

**Files:**
- Create: `app/chat/session_export.py`
- Modify: `app/chat/manager.py` (finalize hook), `app/api/chat.py` (archive route triggers export), `services/session_pipeline/runner.py` (pre-scan sweep exports stale chat sessions), `config/instance.yaml.example` (`sessions.include_chat: true`), `docs/observability.md` (retention table row + revise the "no admin viewer — privacy decision" note: now flag-controlled, default on)
- Modify: `src/audit_events.py` (`chat.session_exported`, system)
- Test: `tests/test_chat_session_export.py`

**Interfaces:**
- Consumes: `chat_message_repo().list_messages(chat_id)` rows (`role`, `content`, `tool_calls`, `parts`, `tokens_in/out`, `model`, `sender_email` — `src/repositories/chat_messages_pg.py:47`), `chat_sessions` rows for owner email/user; `resolve_user_identity` conventions from `services/session_pipeline/runner.py:34`; `SESSION_DATA_DIR` default from `services/session_pipeline/runner.py:83`.
- Produces: `export_chat_session_jsonl(chat_id: str) -> pathlib.Path | None` — returns None when the flag is off, the session has no messages, or the owner can't be resolved to a `users.id`; otherwise writes `${SESSION_DATA_DIR}/<users.id>/chat-<chat_id>.jsonl` (atomic: write `.tmp`, `os.replace`) and returns the path. Idempotent: re-export overwrites; the pipeline's byte-size dedup handles re-processing.
- **PG-only reality check:** `chat_messages`/`chat_sessions` are full DuckDB+PG pairs today, but the export must still fail clean per the A3 rule if any repo in its call path resolves PG-only on a DuckDB app-state instance — `export_chat_session_jsonl` catches `RequiresPostgresBackend` and returns None (defensive; correction from review: the original text misstated `chat_messages` as PG-only).

**Steps:**

- [ ] **Step 1: Failing adapter test.** Feed a fixture list of chat_message rows (user text turn; assistant turn with `tool_calls` + `parts`; tool result part) → assert the JSONL turns match the Claude-Code shape the renderer consumes (`{"type": "user"|"assistant", "sessionId": chat_id, "timestamp": ..., "message": {"role": ..., "content": [{"type": "text", ...} | {"type": "tool_use", ...} | {"type": "tool_result", ...}]}}` — mirror what `services/session_pipeline/lib.parse_jsonl` + `app/api/admin_sessions.py:_render_transcript` accept; read those two before finalizing the shape, they are the contract).
- [ ] **Step 2: Failing end-to-end test.** Create a chat session + messages via repos (seeded PG test harness under `tests/db_pg/` conventions), call `export_chat_session_jsonl`, run `services/session_processors/usage.py` over the produced file (or `run_processor` scoped to the tmp SESSION_DATA_DIR) → `usage_session_summary` row exists with `session_file` ending `chat-<chat_id>.jsonl`; `/api/admin/sessions/list` (seeded admin client) shows it; the transcript endpoint renders it without error. Flag off (`sessions.include_chat: false` via config monkeypatch) → export returns None, no file.
- [ ] **Step 3: Implement adapter + flag + wiring.** Finalize hook: call export (best-effort, log-and-continue) from the manager's session-end/kill path and the archive route. Sweep: in `run_processor`'s entry, before scanning, list chat sessions whose `updated_at` > exported-file mtime (or no file) and export them — bounded (e.g. 200 per tick) to keep the tick cheap.
- [ ] **Step 4: Green; update `docs/observability.md`; CHANGELOG (**Added**, note the privacy-flag semantics explicitly); commit.**

---

### Task 9: F3 — client-reported CLI audit events

**Files:**
- Create: `cli/lib/audit_spool.py`
- Modify: `cli/commands/query.py` (`_run_local` success/error paths), `cli/commands/explore.py` (local path), `cli/commands/push.py` (upload the spool after sessions), `app/api/upload.py` (new endpoint)
- Modify: `src/audit_events.py`, `src/audit_posture.py`
- Test: `tests/test_audit_spool_cli.py`, `tests/test_audit_gap_client_events.py`

**Interfaces:**
- Produces CLI-side: `record_local_event(action: str, params: dict) -> None` — appends `{"action", "params", "observed_at"}` JSON line to `<agnes state dir>/audit_spool.jsonl` (same state directory the push ledger uses — locate it in `cli/commands/push.py`; never fails the command, IO errors swallowed); `drain_spool(max_events: int = 500) -> list[dict]` + `commit_drain()` two-phase so a failed upload loses nothing.
- Produces server-side: `POST /api/upload/audit-events` (auth: `get_current_user`), body `{"events": [{"action": str, "params": dict, "observed_at": iso8601}]}`. Validation: ≤500 events, `action` ∈ `CLIENT_REPORTED_ACTIONS = frozenset({"query.local_offline", "explore.local_offline"})` (server-side whitelist — clients cannot mint arbitrary actions), params ≤2KB serialized each. Each accepted event → `log_safe(user_id=<caller>, action=..., params={**params, "observed_at": ..., "client_reported": True}, result="success", client_kind="cli")`. Response `{"accepted": n, "rejected": m}`.
- Catalog entries: `query.local_offline` (read), `explore.local_offline` (read), `audit_events.upload` (system — one row for the batch itself, params `{"accepted": n}`).
- CLI event params: `{"tables": [...], "sql_hash": hash_args(sql), "rows": n, "duration_ms": t}` — **never SQL text**.

**Steps:**

- [ ] **Step 1: Failing server test.** Valid batch → rows appear with `client_reported: true` in params and `client_kind="cli"`; unknown action in batch → that event rejected (counted in `rejected`), others accepted; >500 events → 400; oversized params → rejected. A second identical upload double-inserts (documented: client dedups via drain/commit; server stays simple).
- [ ] **Step 2: Failing CLI test.** Run `_run_local` against a tmp DuckDB (existing local-query test harness in `tests/` for the CLI — follow `cli` test conventions) → spool file gains one line with `sql_hash` and no SQL text; `drain_spool`/`commit_drain` round-trip; simulated upload failure → spool intact.
- [ ] **Step 3: Implement both sides + push wiring** (spool upload is best-effort after session upload; server outage must not fail `agnes push` — same `; true` philosophy as the hooks).
- [ ] **Step 4: Green; POSTURE entry for the new route (its own action `audit_events.upload`); CHANGELOG (Added — call out the trust model: client-reported, flagged as such in params); commit.**

---

## Appendix — verified gap inventory the tasks consume

Mutating-route audit coverage at plan time: 196 of 352 audited. The 155 `"fallback"`-seeded routes and per-module details, the ~205-action catalog seed, and the per-surface findings (SSO/magic-link zero rows; MCP zero per-call rows; `client_ip`/`correlation_id` never written; four naming conventions; `log_safe` single-caller) were collected by four repo-wide sweeps on 2026-08-28. Line references in this plan were verified same-day; symbols move — locate by name, not line, when a reference misses.

## Self-review notes

- Spec coverage: F0→Task 1, F1→Task 2, F2→Tasks 3–7 (auth / agents+jobs / MCP / messaging / secrets+distribution+ingress), F3→Task 9, F4→Task 8. The report's §7 policy ("what deliberately stays unlogged") is encoded as `exempt:` posture entries + the metadata-only params rule in Global Constraints.
- Type consistency: `log_safe(**kwargs)` passes through to `AuditRepository.log(user_id, action, resource, params, result, duration_ms, *, params_before, client_ip, client_kind, correlation_id)` — all task snippets use only these names. `hash_args` moves to `src/audit_helpers.py` in Task 5; Tasks 6/9 import it from there (Task 6 may keep the `app/chat/audit` re-export if Task 5 hasn't merged — both import paths stay valid).
- Known collision points between tasks: `src/audit_events.py` and `src/audit_posture.py` (append/flip-only edits — integrator folds them), CHANGELOG (absorption on merge).
