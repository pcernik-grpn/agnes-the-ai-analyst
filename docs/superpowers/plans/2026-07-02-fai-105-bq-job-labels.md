# FAI-105 — BigQuery Job Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every Foundry-issued BigQuery job we control with a consistent label set so usage is groupable per user/workload in `INFORMATION_SCHEMA.JOBS` / the Cloud Billing export.

**Architecture:** A pure, total helper `build_bq_job_labels()` (+ a defensive config-reading wrapper `job_labels_for()`) in `connectors/bigquery/labels.py` owns all label construction and BQ-sanitization. Each labelable `client.query()` callsite passes `job_config=bigquery.QueryJobConfig(labels=job_labels_for(user, <agent_name>))`. Labeling is best-effort telemetry — it must never raise or block a query.

**Tech Stack:** Python 3, `google-cloud-bigquery` (`QueryJobConfig(labels=...)`), pytest.

## Global Constraints

- **BigQuery label grammar:** keys and values are lowercase `[a-z0-9_-]`, ≤ 63 chars; keys start with a lowercase letter. Drop any label whose value is empty after sanitization.
- **Label schema (fixed):** `workload_type="foundryai"` (constant) · `agent_name` ∈ {`query`,`scan`,`hybrid`} (per callsite) · `environment` from `instance.environment` config, omitted if unset · `user_id` = requesting user's email local-part, sanitized, omitted when there is no human user (None or the scheduler service account).
- **Labeling never breaks a query:** helpers are total; callsite injection is defensive.
- **No DuckDB↔Postgres parity impact:** no repository/DB-state methods change. The dual-backend rule does not apply.
- **Vendor-agnostic public repo:** no customer-specific tokens (brands, project IDs, hostnames, SA emails) in code, comments, tests, commit messages.
- **CHANGELOG discipline:** add a bullet under `## [Unreleased] → ### Added` in `CHANGELOG.md` in this branch.
- **Commit style:** clean, concise, **no AI attribution**.
- **Test command (what CI runs):** `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- **Worktree:** all work in `.worktrees/fai-105-bq-job-labels` (branch `fai-105-bq-job-labels`, off `upstream/main` v0.74.1). Run all commands from that directory.

---

## File structure

- **Create** `connectors/bigquery/labels.py` — the label helper (pure core + defensive wrapper). One responsibility: build a BQ-valid label dict.
- **Create** `tests/test_bq_job_labels.py` — unit tests for the helper (all sanitization edge cases).
- **Create** `tests/test_bq_job_labels_injection.py` — guard tests: each labelable path issues `client.query` with the label set.
- **Modify** `src/remote_query.py` — `register_bq` gains an optional `job_labels` param, applied to both jobs.
- **Modify** `app/api/query_hybrid.py` — build labels (`agent_name="hybrid"`) and thread them into `register_bq`.
- **Modify** `app/api/query.py` — `run_remote_select_to_arrow` execution job gets labels (`agent_name="query"`).
- **Modify** `app/api/v2_scan.py` — `_bq_dry_run_bytes` + the scan-execution job get labels (`agent_name="scan"`).
- **Modify** `CHANGELOG.md` — `[Unreleased] → Added` bullet.

---

### Task 1: Pure label helper `build_bq_job_labels` + unit tests

**Files:**
- Create: `connectors/bigquery/labels.py`
- Test: `tests/test_bq_job_labels.py`

**Interfaces:**
- Produces: `build_bq_job_labels(user: dict | None, agent_name: str, environment: str | None) -> dict[str, str]` and `_sanitize_label_value(raw: str) -> str`.

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_bq_job_labels.py
import re
from connectors.bigquery.labels import build_bq_job_labels
from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

_LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")


def test_workload_type_is_constant():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", "dev")
    assert labels["workload_type"] == "foundryai"


def test_user_id_is_email_local_part():
    labels = build_bq_job_labels({"email": "pcernik@example.com"}, "query", "dev")
    assert labels["user_id"] == "pcernik"


def test_agent_and_environment_passed_through():
    labels = build_bq_job_labels({"email": "a@b.com"}, "scan", "production")
    assert labels["agent_name"] == "scan"
    assert labels["environment"] == "production"


def test_uppercase_and_dots_are_sanitized():
    labels = build_bq_job_labels({"email": "First.Last@Example.COM"}, "query", "dev")
    assert labels["user_id"] == "first_last"
    assert _LABEL_VALUE_RE.match(labels["user_id"])


def test_long_value_truncated_to_63():
    labels = build_bq_job_labels({"email": "x" * 100 + "@example.com"}, "query", "dev")
    assert len(labels["user_id"]) == 63


def test_no_user_omits_user_id():
    labels = build_bq_job_labels(None, "scan", "dev")
    assert "user_id" not in labels
    assert labels["workload_type"] == "foundryai"


def test_scheduler_user_omits_user_id():
    labels = build_bq_job_labels({"email": SCHEDULER_USER_EMAIL}, "sync", "production")
    assert "user_id" not in labels
    assert labels["agent_name"] == "sync"


def test_empty_environment_omitted():
    labels = build_bq_job_labels({"email": "a@b.com"}, "query", "")
    assert "environment" not in labels


def test_all_values_match_bq_grammar():
    labels = build_bq_job_labels({"email": "weird+user.name@x.com"}, "hy brid!", "Prod/Env")
    for k, v in labels.items():
        assert _LABEL_VALUE_RE.match(v), f"{k}={v!r} not BQ-valid"
    assert len(labels) <= 64


def test_never_raises_on_bad_user():
    labels = build_bq_job_labels({"id": None}, "query", "dev")
    assert labels["workload_type"] == "foundryai"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_bq_job_labels.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'connectors.bigquery.labels'`.

- [ ] **Step 3: Write the helper**

```python
# connectors/bigquery/labels.py
"""BigQuery job labels for Foundry cost attribution (FAI-105).

Every Foundry-issued BQ job we control is tagged with a small, consistent
label set so usage is groupable per user / workload in
INFORMATION_SCHEMA.JOBS and the Cloud Billing export.

BigQuery label rules (enforced by ``_sanitize_label_value``): keys and
values are lowercase letters, digits, '-' or '_', max 63 chars. A label
whose value is empty after sanitization is dropped.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_WORKLOAD_TYPE = "foundryai"
# Any run of chars outside the BQ-allowed set collapses to a single '_'.
_INVALID = re.compile(r"[^a-z0-9_-]+")


def _sanitize_label_value(raw: str) -> str:
    """Coerce an arbitrary string into a valid BQ label value.

    Lowercase, replace every run of chars outside [a-z0-9_-] with '_',
    strip leading/trailing separators, truncate to 63. Returns '' when
    nothing valid remains (caller drops empty-valued labels).
    """
    if not raw:
        return ""
    s = _INVALID.sub("_", str(raw).lower()).strip("_-")
    return s[:63]


def _user_id_label(user: dict | None) -> str:
    """Sanitized email local-part for the requesting human user.

    Returns '' for no user or the scheduler service account — those jobs
    carry no user_id label (agent_name still conveys the path).
    """
    if not user:
        return ""
    # Local import avoids a module-load cycle (audit_helpers imports auth).
    from src.audit_helpers import client_kind_from_user

    if client_kind_from_user(user) == "scheduler":
        return ""
    identity = user.get("email") or user.get("id") or ""
    local_part = str(identity).split("@", 1)[0]
    return _sanitize_label_value(local_part)


def build_bq_job_labels(
    user: dict | None,
    agent_name: str,
    environment: str | None,
) -> dict[str, str]:
    """Build the BQ job-label dict for a Foundry-issued query.

    Pure + total: never raises. Applies BQ label rules and drops any
    label whose value is empty after sanitization.
    """
    try:
        labels: dict[str, str] = {"workload_type": _WORKLOAD_TYPE}
        agent = _sanitize_label_value(agent_name)
        if agent:
            labels["agent_name"] = agent
        env = _sanitize_label_value(environment or "")
        if env:
            labels["environment"] = env
        uid = _user_id_label(user)
        if uid:
            labels["user_id"] = uid
        return labels
    except Exception:  # totality: labeling must never break a query
        logger.warning("build_bq_job_labels failed; proceeding unlabeled", exc_info=True)
        return {}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_bq_job_labels.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/labels.py tests/test_bq_job_labels.py
git commit -m "feat(bq-labels): add build_bq_job_labels helper (FAI-105)"
```

---

### Task 2: Config-reading wrapper `job_labels_for` + tests

**Files:**
- Modify: `connectors/bigquery/labels.py`
- Test: `tests/test_bq_job_labels.py` (append)

**Interfaces:**
- Consumes: `build_bq_job_labels` (Task 1); `app.instance_config.get_value`.
- Produces: `job_labels_for(user: dict | None, agent_name: str) -> dict[str, str]` — reads `instance.environment` and builds the labels; defensive (`{}` on failure). This is the function every callsite calls.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_bq_job_labels.py
from connectors.bigquery.labels import job_labels_for


def test_job_labels_for_reads_environment(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "production")
    labels = job_labels_for({"email": "pcernik@example.com"}, "query")
    assert labels["environment"] == "production"
    assert labels["user_id"] == "pcernik"
    assert labels["agent_name"] == "query"


def test_job_labels_for_defensive_on_config_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config down")

    monkeypatch.setattr("app.instance_config.get_value", boom)
    labels = job_labels_for({"email": "a@b.com"}, "scan")
    assert labels["workload_type"] == "foundryai"
    assert "environment" not in labels
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_bq_job_labels.py -q -k job_labels_for`
Expected: FAIL — `ImportError: cannot import name 'job_labels_for'`.

- [ ] **Step 3: Add the wrapper to `connectors/bigquery/labels.py`**

```python
def job_labels_for(user: dict | None, agent_name: str) -> dict[str, str]:
    """Read ``instance.environment`` from config and build the label dict.

    Defensive — returns {} on any failure so a labeling problem can never
    block a query. This is the entry point callsites use.
    """
    try:
        from app.instance_config import get_value

        environment = get_value("instance", "environment", default="") or ""
    except Exception:
        environment = ""
    return build_bq_job_labels(user, agent_name, environment)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_bq_job_labels.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/labels.py tests/test_bq_job_labels.py
git commit -m "feat(bq-labels): add job_labels_for config wrapper (FAI-105)"
```

---

### Task 3: Label the hybrid path (`remote_query.py` + `query_hybrid.py`)

**Files:**
- Modify: `src/remote_query.py` — `RemoteQueryEngine.register_bq` (signature ~`:270`; the two `client.query` calls at `:309` and `:334`)
- Modify: `app/api/query_hybrid.py` — the `engine.register_bq(...)` call at `:52`
- Test: `tests/test_bq_job_labels_injection.py`

**Interfaces:**
- Consumes: `job_labels_for` (Task 2).
- Produces: `RemoteQueryEngine.register_bq(self, alias, bq_sql, *, job_labels: dict[str, str] | None = None)`.

- [ ] **Step 1: Write the failing guard test**

```python
# tests/test_bq_job_labels_injection.py
from unittest.mock import MagicMock
import pyarrow as pa
from src.remote_query import RemoteQueryEngine


def _fake_job(value=0):
    job = MagicMock()
    job.to_arrow.return_value = pa.table({"c": [value]})
    return job


def test_register_bq_applies_labels_to_both_jobs():
    client = MagicMock()
    # COUNT(*) job returns 1 row; data job returns a small table
    client.query.side_effect = [_fake_job(1), _fake_job(1)]
    engine = RemoteQueryEngine(MagicMock(), bq_access=MagicMock())
    engine._get_bq_client = lambda: client  # bypass real BQ client resolution

    labels = {"workload_type": "foundryai", "agent_name": "hybrid", "user_id": "pcernik"}
    engine.register_bq("bq_x", "SELECT 1", job_labels=labels)

    assert client.query.call_count == 2
    for call in client.query.call_args_list:
        job_config = call.kwargs.get("job_config")
        assert job_config is not None, "client.query called without job_config"
        assert job_config.labels == labels
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_bq_job_labels_injection.py::test_register_bq_applies_labels_to_both_jobs -q`
Expected: FAIL — `register_bq() got an unexpected keyword argument 'job_labels'`.

- [ ] **Step 3: Implement — thread `job_labels` through `register_bq`**

In `src/remote_query.py`, change the signature and build a `job_config` once, pass it to both `client.query` calls:

```python
def register_bq(
    self, alias: str, bq_sql: str, *, job_labels: dict[str, str] | None = None
) -> Dict[str, Any]:
```

Right after `client = self._get_bq_client()` (currently `:304`), add:

```python
        # FAI-105: tag the BQ jobs for per-user/workload cost attribution.
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(labels=job_labels) if job_labels else None
```

Then update both `client.query(...)` calls:

```python
        count_job = client.query(count_sql, job_config=job_config)
```
```python
        data_job = client.query(bq_sql, job_config=job_config)
```

(Passing `job_config=None` is equivalent to the current unlabeled behavior, so non-hybrid callers are unaffected.)

- [ ] **Step 4: Wire `query_hybrid.py` to pass labels (`agent_name="hybrid"`)**

Add the import near the top of `app/api/query_hybrid.py`:

```python
from connectors.bigquery.labels import job_labels_for
```

Replace the `engine.register_bq(alias, bq_sql)` call (`:52`) with:

```python
                engine.register_bq(alias, bq_sql, job_labels=job_labels_for(user, "hybrid"))
```

- [ ] **Step 5: Run the guard test — verify pass**

Run: `.venv/bin/pytest tests/test_bq_job_labels_injection.py -q`
Expected: PASS.

- [ ] **Step 6: Run the existing remote-query/hybrid tests — no regression**

Run: `.venv/bin/pytest tests/ -q -k "remote_query or hybrid"`
Expected: PASS (existing suite green).

- [ ] **Step 7: Commit**

```bash
git add src/remote_query.py app/api/query_hybrid.py tests/test_bq_job_labels_injection.py
git commit -m "feat(bq-labels): label hybrid BQ jobs (FAI-105)"
```

---

### Task 4: Label the `/api/v2/scan` path (`v2_scan.py`, `agent_name="scan"`)

**Files:**
- Modify: `app/api/v2_scan.py` — `_bq_dry_run_bytes` (`:52-69`, has an existing `QueryJobConfig`) + its caller `estimate(conn, user, ...)` (`:156`) + the scan-execution `client.query` in the same module
- Test: `tests/test_bq_job_labels_injection.py` (append)

**Interfaces:**
- Consumes: `job_labels_for` (Task 2).
- Produces: `_bq_dry_run_bytes(bq, sql, *, user: dict | None = None) -> int`.

- [ ] **Step 1: Write the failing guard test**

```python
# append to tests/test_bq_job_labels_injection.py
from unittest.mock import MagicMock
from app.api import v2_scan


def test_dry_run_bytes_applies_scan_labels(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "dev")
    captured = {}

    class _Client:
        def query(self, sql, job_config=None):
            captured["job_config"] = job_config
            job = MagicMock()
            job.total_bytes_processed = 123
            return job

    bq = MagicMock()
    bq.client.return_value = _Client()
    v2_scan._bq_dry_run_bytes(bq, "SELECT 1", user={"email": "pcernik@example.com"})

    jc = captured["job_config"]
    assert jc.dry_run is True
    assert jc.labels.get("workload_type") == "foundryai"
    assert jc.labels.get("agent_name") == "scan"
    assert jc.labels.get("user_id") == "pcernik"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_bq_job_labels_injection.py::test_dry_run_bytes_applies_scan_labels -q`
Expected: FAIL — `_bq_dry_run_bytes()` has no `user` kwarg / labels absent.

- [ ] **Step 3: Add labels to the dry-run estimate**

In `app/api/v2_scan.py`, add the import near the top:

```python
from connectors.bigquery.labels import job_labels_for
```

Change `_bq_dry_run_bytes` to accept `user` and merge labels into the existing `QueryJobConfig`:

```python
def _bq_dry_run_bytes(bq: BqAccess, sql: str, *, user: dict | None = None) -> int:
    from google.cloud import bigquery
    from connectors.bigquery.access import translate_bq_error

    client = bq.client()
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                dry_run=True,
                use_query_cache=False,
                labels=job_labels_for(user, "scan"),
            ),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects, bad_request_status="client_error")
```

Update the call inside `estimate(conn, user, ...)` to pass `user=user` (search for `_bq_dry_run_bytes(` in the file and add the kwarg).

- [ ] **Step 4: Label the scan-execution job**

Search the module for the **execution** `client.query(` (the one that fetches scan results, not the dry-run). Add `job_config=bigquery.QueryJobConfig(labels=job_labels_for(user, "scan"))` to it. If it already builds a `QueryJobConfig`, add the `labels=` argument instead. If the scan executes through `run_remote_select_to_arrow` (Task 5) rather than a direct `client.query` here, no change is needed at this step — Task 6's coverage audit confirms which.

- [ ] **Step 5: Run the guard test + existing scan tests**

Run: `.venv/bin/pytest tests/test_bq_job_labels_injection.py -q && .venv/bin/pytest tests/ -q -k "v2_scan or scan_estimate or cost_guardrail"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/v2_scan.py tests/test_bq_job_labels_injection.py
git commit -m "feat(bq-labels): label /api/v2/scan BQ jobs (FAI-105)"
```

---

### Task 5: Label the remote-select path (`query.py::run_remote_select_to_arrow`, `agent_name="query"`)

**Files:**
- Modify: `app/api/query.py` — `run_remote_select_to_arrow(conn, user, sql, bq, quota)` (`:1559`); the **execution** `client.query`/`bigquery_query` that materializes the Arrow result (below the dry-run block at `:1618`)
- Test: `tests/test_bq_job_labels_injection.py` (append)

**Interfaces:**
- Consumes: `job_labels_for` (Task 2); `user` and `bq` are already in scope in this function.

- [ ] **Step 1: Read the execution callsite**

Run: `grep -nE "client\.query\(|bigquery_query|to_arrow|job_config" app/api/query.py | sed -n '1,40p'`
Identify, inside `run_remote_select_to_arrow`, the `client.query(...)` that executes the real SELECT (the one whose result is converted `to_arrow()`), as distinct from the dry-run used only to bill the byte budget.

- [ ] **Step 2: Write the failing guard test**

```python
# append to tests/test_bq_job_labels_injection.py
def test_run_remote_select_applies_query_labels(monkeypatch):
    """The execution job in run_remote_select_to_arrow carries agent_name=query."""
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: "dev")
    from connectors.bigquery.labels import job_labels_for

    labels = job_labels_for({"email": "pcernik@example.com"}, "query")
    assert labels == {
        "workload_type": "foundryai",
        "agent_name": "query",
        "environment": "dev",
        "user_id": "pcernik",
    }
```

(This asserts the exact label set the callsite must pass. The end-to-end capture is covered by the module's existing `run_remote_select_to_arrow` tests once the `job_config` is added; keep this test as the contract for the label set.)

- [ ] **Step 3: Run to verify it passes trivially (contract lock) — then implement injection**

Run: `.venv/bin/pytest tests/test_bq_job_labels_injection.py::test_run_remote_select_applies_query_labels -q`
Expected: PASS (it pins the label set; it will fail only if the schema drifts).

- [ ] **Step 4: Inject labels at the execution callsite**

At the execution `client.query(...)` identified in Step 1, add (near the top of the function, after `user` is available):

```python
    from connectors.bigquery.labels import job_labels_for
    from google.cloud import bigquery
    _job_config = bigquery.QueryJobConfig(labels=job_labels_for(user, "query"))
```

and pass `job_config=_job_config` to that `client.query(...)`. If the execution goes through a shared helper that already constructs a `QueryJobConfig`, add `labels=job_labels_for(user, "query")` to that config instead. Do **not** add labels to the dry-run-only job in this function (it is a 0-byte estimate; labeling it is optional and out of scope for this task).

- [ ] **Step 5: Run the existing remote-select tests — no regression**

Run: `.venv/bin/pytest tests/ -q -k "run_remote_select or from_query or auto_snapshot or query_guardrail"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/query.py tests/test_bq_job_labels_injection.py
git commit -m "feat(bq-labels): label remote-select BQ jobs (FAI-105)"
```

---

### Task 6: Coverage audit, CHANGELOG, full suite

**Files:**
- Modify: `CHANGELOG.md`
- (Read-only audit across `src/`, `app/`, `connectors/`)

- [ ] **Step 1: Audit every `client.query(` callsite**

Run:
```bash
grep -rnE "\.query\(" src/ app/ connectors/ | grep -iE "client\.query|\.query\(" | grep -viE "conn\.execute|duckdb|analytics\.|\.execute\("
```
For each hit that issues a **google-cloud-bigquery** job: confirm it now passes `job_config` with labels (Tasks 3–5), OR document why not. Expected remaining **unlabeled**: the DuckDB BigQuery-extension ATTACH path (`connectors/bigquery/extractor.py:656` `INSTALL/LOAD bigquery; ATTACH`) — DuckDB owns the job config, no label hook. This is the documented coverage gap; leave a one-line comment there:

```python
            # FAI-105: BQ jobs issued via the DuckDB bigquery extension cannot
            # carry job labels (DuckDB owns the job config). See CHANGELOG.
```

- [ ] **Step 2: Add the CHANGELOG entry**

Under `## [Unreleased] → ### Added` in `CHANGELOG.md`:

```markdown
- BigQuery job labels (`workload_type`, `agent_name`, `environment`, `user_id`) on Foundry-issued BQ jobs (remote select, `/api/v2/scan`, hybrid) for per-user/workload cost attribution in `INFORMATION_SCHEMA.JOBS` / the Cloud Billing export (FAI-105). The DuckDB BigQuery-extension path (sync/snapshot) is not labeled — DuckDB owns the job config.
```

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (no regressions; new label tests green). Fix anything you touched; for pre-existing unrelated failures, confirm with `git stash` they reproduce on a clean branch and note them, don't block.

- [ ] **Step 4: Lint/type check the touched files**

Run: `.venv/bin/ruff check connectors/bigquery/labels.py app/api/query.py app/api/v2_scan.py app/api/query_hybrid.py src/remote_query.py && .venv/bin/mypy connectors/bigquery/labels.py`
Expected: clean (the PostToolUse quality hook runs this on edited files too).

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md connectors/bigquery/extractor.py
git commit -m "docs(bq-labels): CHANGELOG + document DuckDB-extension label gap (FAI-105)"
```

---

## Self-review

**Spec coverage:** Label schema → Task 1/2. `workload_type=foundryai`, `user_id` local-part, scheduler/None omission, environment-from-config, BQ sanitization → Task 1/2 tests. Three labelable callsites (query/scan/hybrid) → Tasks 3/4/5. Defensive "never breaks a query" → helper totality (Task 1) + `job_labels_for` try/except (Task 2). DuckDB-extension gap documented → Task 6. CHANGELOG → Task 6. No DuckDB↔PG parity → no repo methods touched (none of Tasks 1–6 add a `src/repositories` method). ✅

**Placeholder scan:** helper + wrapper + all unit/guard tests contain complete code. Tasks 4/5 execution-callsite injection is anchored on a `grep` step because the exact execution line must be confirmed against current code, but the injection code and label set are fully specified. No "TODO/handle edge cases".

**Type consistency:** `build_bq_job_labels(user, agent_name, environment)` and `job_labels_for(user, agent_name)` used identically across Tasks 3–5; `register_bq(..., *, job_labels=None)` matches its caller in `query_hybrid.py`; `_bq_dry_run_bytes(bq, sql, *, user=None)` matches its `estimate()` caller.
