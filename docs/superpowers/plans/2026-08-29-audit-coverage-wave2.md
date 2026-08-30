# Audit Coverage Wave 2 — Declarative Actions, Reads, and the Last Surfaces

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish what wave 1 deliberately left open, without patching: every mutating route emits a *semantically real* action instead of a generic `http.request`; read and WebSocket routes get the same declare-or-fail ratchet mutating routes already have; the three surfaces still writing nothing (apps runner, data-app subdomain proxy, notifications WS) start writing; and the volume this creates is measured and controllable instead of assumed.

**Architecture:** Wave 1 left 138 mutating routes declared `"fallback"` — covered by `AuditFallbackMiddleware`, but only under the meaningless action `http.request`. The instinct is to hand-write `log_safe()` into 138 handlers; that is the patch, and it is both a huge diff and a permanent maintenance tax. Instead this wave **changes the contract of `src/audit_posture.py`**: a posture entry stops being documentation and becomes the route's *declared action*, which the middleware emits (with `resource` derived from the route's own path params) when the handler wrote nothing itself. One middleware change plus a mechanical mapping replaces 138 handler edits, and the same mechanism then extends to reads and WebSockets. Handlers that carry resource ids or before-state keep their explicit `log_safe` call and the middleware stays silent for them — unchanged from wave 1.

**Tech Stack:** Pure-ASGI middleware, contextvars, FastAPI route introspection, DuckDB + Postgres dual-backend audit repo (frozen pre-A3 pair), pytest with `shared_app`/`seeded_app` fixtures.

**Spec:** Wave 1 plan and its "what this wave deliberately left" section: `docs/superpowers/plans/2026-08-28-audit-full-coverage.md`. Wave 1 shipped as PR #1758.

## Global Constraints

- **Base branch: `integration`.** Wave 1 (PR #1758) is expected to be merged first; if it is not, base on its head `1d494ffaa` and say so in the report.
- **No schema changes anywhere in this wave.** The `audit_log` columns are sufficient. No new repos; no `src/db.py` ladder step; no Alembic revision.
- **Dual backend:** if any change touches `src/repositories/audit*.py`, both files change in the same task with the contract test extended. (No task here is expected to need it.)
- **Emission goes through `src.audit_helpers.log_safe`**, never `audit_repo().log()` directly outside the repo layer. Every action string must be registered in `src/audit_events.py` `CATALOG` — append task-labeled blocks after the existing marker; never reorder existing entries.
- **Content never, metadata always.** No prompt text, SQL text, request bodies, or secret values in `params`. Hashes, counts, sizes, and identifiers only.
- **Performance is a correctness property in this wave.** The middleware runs on every request. `AuditFallbackMiddleware._already_covered_by_correlation_id()` issues a DB query; it must NEVER run on the read path (Task 2 states this explicitly) and must stay confined to the mutating path where it exists today.
- **`shared_app`/`seeded_app` fixtures only** — never `create_app()` in a function-scoped fixture (`tests/test_shared_app_contract.py` enforces this).
- **CHANGELOG:** each task reports its bullet; the integrator folds them. Builders do not edit `CHANGELOG.md` or `CLAUDE.md`.
- **Vendor-agnostic** (public repo): no customer names, project ids, or internal hostnames.
- **Local verification only:** the task's own test files plus the audit ratchets (`tests/test_audit_catalog.py`, `tests/test_audit_route_posture.py`) plus `scripts/verify_syncmap.py`. Never the full suite (CI runs it).
- Task 2 depends on Task 1 (it builds on Task 1's middleware contract and posture schema). Tasks 3 and 4 are independent of both and of each other.

---

### Task 1: Declarative action emission — retire `"fallback"` as a concept

**Files:**
- Modify: `app/middleware/audit_fallback.py`
- Modify: `src/audit_posture.py` (contract docstring + flip all 138 `"fallback"` values to real action names)
- Modify: `src/audit_events.py` (register the new action names)
- Modify: `tests/test_audit_route_posture.py` (the ratchet stops accepting the `"fallback"` literal)
- Test: `tests/test_audit_declared_actions.py`

**Interfaces (produced — Task 2 builds on these):**
- `src/audit_posture.py`:
  - `POSTURE: dict[str, str]` — value is now ALWAYS either a cataloged action name or `"exempt:<reason>"`. The literal `"fallback"` is removed from the vocabulary entirely.
  - `def declared_action(method: str, path_template: str) -> str | None` — returns the cataloged action for a route, or `None` when the route is exempt or undeclared. Task 2 reuses this for reads.
- `app/middleware/audit_fallback.py`:
  - `def resource_from_scope(scope) -> str` — builds a resource string from the route template plus `scope["path_params"]`: with params, `"<sorted k=v pairs>"` appended as `"{template} {k}={v}"`; without params, the template alone. Exact format pinned by the tests below.
  - The middleware emits `declared_action(...)` instead of the constant `"http.request"`. `http.request` stays in `CATALOG` (historical rows exist) but no writer emits it any more; the module docstring says so.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_audit_declared_actions.py
from src.audit_events import is_cataloged
from src.audit_posture import POSTURE, declared_action


def test_posture_has_no_fallback_values_left():
    leftovers = sorted(k for k, v in POSTURE.items() if v == "fallback")
    assert not leftovers, (
        "Every mutating route must declare a REAL action; 'fallback' is no "
        f"longer a legal posture value: {leftovers}"
    )


def test_every_declared_action_is_cataloged():
    bad = sorted(
        f"{k} -> {v}" for k, v in POSTURE.items()
        if not v.startswith("exempt:") and not is_cataloged(v)
    )
    assert not bad, f"Posture names an uncataloged action: {bad}"


def test_declared_action_resolves_and_skips_exempt():
    key = next(k for k, v in POSTURE.items() if not v.startswith("exempt:"))
    method, template = key.split(" ", 1)
    assert declared_action(method, template) == POSTURE[key]
    ex = next((k for k, v in POSTURE.items() if v.startswith("exempt:")), None)
    if ex:
        m, t = ex.split(" ", 1)
        assert declared_action(m, t) is None
    assert declared_action("POST", "/no/such/route") is None
```

```python
# same file — middleware behaviour
def test_unhandled_mutation_emits_its_declared_action(tmp_path, monkeypatch, seeded_app, seeded_tokens):
    """A route whose handler writes no row of its own gets ITS action, not a
    generic http.request row."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Pick a route the posture map declares but whose handler writes nothing.
    # (Use one this task flips, e.g. POST /api/stack/subscribe -> stack.subscribe.)
    ...
    rows, _ = audit_repo().query(action="stack.subscribe", limit=1)
    assert rows, "middleware must emit the DECLARED action"
    assert rows[0]["action"] != "http.request"


def test_resource_carries_path_params(...):
    """DELETE /api/stack/subscription/{rt}/{rid} records which subscription."""
    ...
    assert "rid=" in rows[0]["resource"]


def test_handler_written_row_is_not_duplicated(...):
    """A route that audits itself still produces exactly one row."""
    ...


def test_no_writer_emits_http_request_any_more():
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", '"http.request"', "app/", "src/"],
        capture_output=True, text=True,
    ).stdout
    offenders = [
        ln for ln in out.splitlines()
        if "audit_events.py" not in ln and "audit_fallback.py" not in ln
    ]
    assert not offenders, offenders
```

- [ ] **Step 2: Run — fails (`declared_action` missing, 138 `"fallback"` values present).**

- [ ] **Step 3: Implement the middleware + helper.** In `audit_fallback.py`: add `resource_from_scope`; replace the `action="http.request"` emission with `action = declared_action(scope["method"], template)` and `return` when it is `None` (exempt or undeclared — an undeclared route is a ratchet failure, not a runtime concern). Keep the existing `audit_written_count()` and correlation-id tie-break guards exactly as they are. Update both module docstrings (`audit_fallback.py`, `audit_posture.py`) to state the new contract: *a posture entry is the route's declared action; the handler may emit it itself with richer params, otherwise the middleware emits it.*

- [ ] **Step 4: Flip all 138 `"fallback"` entries to real action names.** Work module by module (the dict is grouped by module comment headers). Naming follows the dominant convention already in `CATALOG`: `domain.object.verb`, e.g. `POST /api/stack/subscribe` → `stack.subscribe`, `DELETE /api/admin/prompts/{kind}` → `prompt.delete`, `POST /api/collections/{cid}/files` → `collection.file_add`. Rules:
  - Reuse an existing cataloged action when the route genuinely performs that action; only mint a new name when none fits.
  - Name the *domain effect*, never the HTTP shape (`prompt.delete`, not `prompts_delete_endpoint`).
  - Register every newly minted action in `CATALOG` under a `# Wave 2 — Task 1` block with a one-line description saying what the row means.
  - The 65 `admin` routes are the bulk; group them by their admin sub-domain (`ontology.*`, `prompt.*`, `semantic_model.*`, `metric.*`, `source_connection.*`, …) rather than a flat `admin.*` namespace.

- [ ] **Step 5: Tighten the ratchet.** In `tests/test_audit_route_posture.py`, `test_posture_values_are_valid` must now reject the literal `"fallback"` (it is no longer in the vocabulary). Keep the undeclared/stale checks unchanged.

- [ ] **Step 6: Run the new file + both ratchets + `scripts/verify_syncmap.py`; all green. Commit.**

---

### Task 2: Read and WebSocket routes join the ratchet

**Files:**
- Modify: `src/audit_posture.py` (new `READ_POSTURE`, `WS_POSTURE`, and the exemption-reason vocabulary)
- Modify: `app/middleware/audit_fallback.py` (read-path emission, strictly guarded)
- Modify: `src/audit_events.py` (actions for the sensitive reads that were never audited)
- Modify: `tests/test_audit_route_posture.py` (extend the ratchet to reads + WS)
- Test: `tests/test_audit_read_posture.py`

**Interfaces:**
- Consumes from Task 1: `declared_action`, `resource_from_scope`, the "posture value = action" contract.
- Produces:
  - `READ_POSTURE: dict[str, str]` keyed `"GET /path/template"` — a cataloged action for a read worth recording, or `"exempt:<reason>"` for the rest. Every one of the ~406 read routes appears.
  - `WS_POSTURE: dict[str, str]` keyed `"WS /path/template"` — same rule for the 9 WebSocket routes.
  - `EXEMPT_REASONS: frozenset[str]` — the closed vocabulary of exemption reasons: `"health"`, `"self"` (caller reading their own data), `"ui_support"` (list/lookup backing a page, no sensitive content), `"static"`, `"noise"` (high-frequency polling with no security value). A reason outside this set fails the ratchet, so "exempt" can never become a junk drawer.

**Policy (decide once, apply mechanically):** a read gets a real action when it returns **data content** (query results, samples, downloads, exports, bundles), **secrets or tokens**, **another user's data** (admin cross-user reads), or **the audit trail itself**. Everything else is exempt with a reason. This is the same line wave 1 drew case-by-case; here it becomes explicit and enforced.

**Steps:**

- [ ] **Step 1: Write the failing ratchet test.**

```python
# tests/test_audit_read_posture.py
from src.audit_events import is_cataloged
from src.audit_posture import EXEMPT_REASONS, READ_POSTURE, WS_POSTURE


def _routes(app, methods):
    out = set()
    for r in app.routes:
        ms = getattr(r, "methods", None)
        if ms is None:                      # WebSocket route
            if "WS" in methods:
                out.add(f"WS {r.path}")
            continue
        for m in ms & methods:
            out.add(f"{m} {r.path}")
    return out


def test_every_read_route_declares_posture(shared_app):
    routes = _routes(shared_app, {"GET"})
    undeclared = sorted(routes - READ_POSTURE.keys())
    stale = sorted(READ_POSTURE.keys() - routes)
    assert not undeclared, (
        "New read routes must declare audit posture in src/audit_posture.py "
        f"(a cataloged action, or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune READ_POSTURE, routes gone: {stale}"


def test_every_ws_route_declares_posture(shared_app):
    routes = _routes(shared_app, {"WS"})
    assert not sorted(routes - WS_POSTURE.keys())
    assert not sorted(WS_POSTURE.keys() - routes)


def test_exempt_reasons_come_from_the_closed_vocabulary():
    for src in (READ_POSTURE, WS_POSTURE):
        for key, v in src.items():
            if v.startswith("exempt:"):
                assert v.split(":", 1)[1] in EXEMPT_REASONS, (key, v)
            else:
                assert is_cataloged(v), (key, v)


def test_sensitive_reads_are_not_exempt():
    """The categories the policy says must always be audited."""
    must_audit = [k for k in READ_POSTURE if any(
        s in k for s in ("/download", "/export", "bundle", "secret", "/sample")
    )]
    assert must_audit, "sanity: the fixture list should not be empty"
    wrongly_exempt = sorted(k for k in must_audit if READ_POSTURE[k].startswith("exempt:"))
    assert not wrongly_exempt, wrongly_exempt
```

- [ ] **Step 2: Run — fails (`READ_POSTURE` missing).**
- [ ] **Step 3: Seed `READ_POSTURE` and `WS_POSTURE`.** Run the ratchet to get the full route list; classify each per the policy above. Group by module with comment headers, same convention as `POSTURE`. Sensitive reads that wave 1 already audits (`data.download`, `catalog.sample`, `activity.read`, the secret reads, …) map to their existing actions; sensitive reads still unaudited get new cataloged actions under a `# Wave 2 — Task 2` block.
- [ ] **Step 4: Read-path emission in the middleware — with the performance guard.** Extend `AuditFallbackMiddleware.__call__` to handle `GET` when `READ_POSTURE` declares an action:

```python
# in __call__, after the mutating branch
if scope["method"] in ("GET", "HEAD"):
    await self.app(scope, receive, send_wrapper)
    action = declared_read_action(scope["method"], template)
    if action is None or audit_written_count() > 0:
        return
    # NOTE: no correlation-id tie-break query here. It is a DB round-trip
    # per request and the read path is far hotter than the mutating one;
    # the contextvar counter is authoritative for reads because no read
    # handler writes its audit row from a sync-offloaded thread.
    ...
```

  Verify that claim before relying on it: if any read handler that declares an action is a plain `def` (thread-offloaded) AND writes its own row, the counter can miss it and produce a duplicate. Find such handlers with a grep over the declared-action read routes; where one exists, leave it `exempt:` is NOT acceptable — instead make that handler's own row authoritative by declaring the route with the action it already writes and adding it to a small `READ_SELF_AUDITING` set the middleware skips. Document whichever case you find.
- [ ] **Step 5: Add a read-path performance test.**

```python
def test_read_path_adds_no_db_query(monkeypatch, seeded_app, seeded_tokens):
    """The read branch must not issue the correlation-id tie-break query."""
    calls = []
    from src.repositories import audit as audit_mod
    orig = audit_mod.AuditRepository.query
    monkeypatch.setattr(audit_mod.AuditRepository, "query",
                        lambda self, **kw: (calls.append(kw), orig(self, **kw))[1])
    seeded_app["client"].get("/api/v2/catalog", headers=...)
    assert not [c for c in calls if "correlation_id" in c]
```

- [ ] **Step 6: Green + `scripts/verify_syncmap.py`. Commit.**

---

### Task 3: The three surfaces that still write nothing

**Files:**
- Modify: `services/apps_runner/api.py`, `services/apps_runner/sandbox_api.py`
- Create: `services/apps_runner/audit_report.py`
- Modify: `app/api/data_apps.py` (the control-plane endpoint that receives runner-reported events)
- Modify: `app/data_apps_subdomain.py`
- Modify: `app/api/notifications_ws.py`
- Modify: `src/audit_events.py`, `src/audit_posture.py` (+ `READ_POSTURE`/`WS_POSTURE` if Task 2 merged first; if not, note it for the integrator)
- Test: `tests/test_audit_gap_surfaces.py`

**Interfaces:**
- **apps runner has no database access** (verified: `services/apps_runner/api.py` imports nothing from `src.` or `app.`). It therefore must NOT write audit rows directly. `services/apps_runner/audit_report.py` provides `report_event(action: str, params: dict) -> None` — a best-effort, non-blocking POST to a new control-plane endpoint `POST /api/data-apps/runner-events`, authenticated with the runner's existing shared `X-Runner-Token`; the control plane validates the action against a whitelist (`RUNNER_REPORTED_ACTIONS = frozenset({"data_app.container_up", "data_app.container_stop", "data_app.container_resume"})`) and writes the row with `client_kind="system"`. A runner that cannot reach the control plane logs and continues — a container action must never fail because its audit report did.
- Actions: `data_app.container_up/stop/resume` (mutation — note `resume` had no control-plane counterpart at all, which is why it was invisible), `data_app.access` (read — end-user traffic to a deployed app), `notifications.ws_connect` / `notifications.ws_rejected` (system).

**Volume decision for `data_app.access` (this is the one that can flood):** audit the **first request per (user, app, session-window)**, not every request. Implement as a small in-process TTL set in `app/data_apps_subdomain.py` (`_seen: dict[tuple[str, str], float]`, 15-minute TTL, bounded to 10k entries with the oldest evicted). One row per user per app per 15 minutes answers "who used which data app" without one row per asset fetch. State this explicitly in the row's `params` (`{"window_minutes": 15}`) so a reader never mistakes the count for a request count.

**Steps:**

- [ ] **Step 1: Failing tests.** Runner: `report_event` posts to the control plane and swallows a connection error (monkeypatch the HTTP client to raise; assert no exception escapes and the container action still returns). Control plane: the new endpoint accepts a whitelisted action with a valid runner token → row written with `client_kind="system"`; rejects an unknown action; rejects a bad token with 401. Subdomain proxy: two requests from the same user to the same app inside the window → exactly ONE `data_app.access` row; a request from a second user → a second row. Notifications WS: a successful connect writes `notifications.ws_connect`; a rejected token writes `notifications.ws_rejected` with `result="denied"`.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** all three surfaces plus the control-plane receiving endpoint (declare it in `POSTURE` with its action).
- [ ] **Step 4: Green + ratchets + `scripts/verify_syncmap.py`. Commit.**

---

### Task 4: Measure the volume this creates, and give operators a control

**Files:**
- Create: `scripts/audit_volume_estimate.py`
- Modify: `src/audit_helpers.py` (sampling helper)
- Modify: `src/repositories/audit.py` + `src/repositories/audit_pg.py` ONLY IF a repo method is needed for the estimate — prefer reusing the existing `facets`/`kpis`; if you do touch them, both backends + contract test in the same task
- Modify: `config/instance.yaml.example`, `docs/observability.md`
- Test: `tests/test_audit_volume_controls.py`

**Interfaces:**
- `scripts/audit_volume_estimate.py` — an operator script: reads the last N days of `audit_log` (default 7), reports rows/day overall and the top 20 actions by volume, projects the table size at the configured `audit.retention_days`, and flags any single action responsible for >25% of rows. Prints a plain table; `--json` for machine use (follow the repo's command-UX standard: `--limit`, `--json`, result origin labeled).
- `src/audit_helpers.py`: `def should_sample(action: str) -> bool` — returns False when `audit.sampling.<action>` is configured as a ratio and this call falls outside it; True otherwise. Deterministic per call via a counter, not `random` (a random sampler makes tests flaky and makes "1 in N" untrue for low-traffic instances). Default: no sampling configured for any action — sampling is opt-in per action, and the config comment says an action that matters for security must never be sampled.
- `config/instance.yaml.example`: an `audit:` block documenting `retention_days` (existing) and the new `sampling:` map with a worked example and the security caveat.

**Steps:**

- [ ] **Step 1: Failing tests.** `should_sample` returns True for an unconfigured action; with `audit.sampling.some.action: 0.1` configured it returns True exactly once per 10 calls (deterministic); a configured ratio of `1.0` always True; `0` never (and the config docs say this is how you turn an action off entirely). Estimate script: given a seeded `audit_log`, `--json` reports the right rows/day and identifies the dominant action.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the helper, the script, the config block, and the `docs/observability.md` section: "How much does audit logging cost you" — the measured numbers from a real run of the script on a populated instance, what drives them (the middleware now writes a row per unaudited mutating request and per declared sensitive read), and the two levers (retention, per-action sampling).
- [ ] **Step 4: Run the estimate script against a locally seeded instance and put the REAL measured numbers in the docs** — not a guess, and say in the doc which instance shape produced them.
- [ ] **Step 5: Green + `scripts/verify_syncmap.py`. Commit.**

---

## Self-review notes

- Covers all four items wave 1 listed as deliberately unfinished: the 138 semantic holes (Task 1), the missing read ratchet (Task 2), the three silent surfaces (Task 3), and the unmeasured volume (Task 4).
- The `"fallback"` literal disappears in Task 1; Tasks 2–4 never reintroduce it. `http.request` survives only as a historical catalog entry with a guard test asserting no writer emits it.
- Collision points for the integrator: `src/audit_events.py` (append-only, task-labeled blocks), `src/audit_posture.py` (Task 1 rewrites values, Task 2 adds two new dicts, Task 3 adds a handful of entries — Task 2 and 3 must be applied after Task 1), `CHANGELOG.md` (fold at the end).
- Deliberately NOT in this wave: auditing the content of chat messages or SQL (policy, not a gap); a second audit backend or export pipeline; rewriting historical rows.
