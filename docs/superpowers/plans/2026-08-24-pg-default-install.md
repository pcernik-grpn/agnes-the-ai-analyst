# PG Default Install (A1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fresh Agnes install — VM-provisioned or local compose — runs its
app-state on the Postgres side-car by default; DuckDB app-state becomes the
explicit legacy fallback for existing instances only.

**Architecture:** Do NOT fold Postgres into the base `docker-compose.yml`
(see Deviation below). Instead flip the single lever the fleet machinery
already respects: the first-boot seed of `database.backend` in
`instance.yaml`. The compose-file resolver
(`scripts/ops/agnes-compose-file.sh::agnes_resolve_compose_file`) already
assembles `docker-compose.postgres.yml` + `docker-compose.postgres-host-mount.yml`
whenever the persisted backend is `side_car`, the startup script already
mints `POSTGRES_PASSWORD` from Secret Manager and pre-chowns
`/data/postgres`, and the `migrate`/`data-migrate` one-shots already gate
app boot. New instances therefore go PG with a one-line seed change plus
docs, doctor, and the release smoke gate.

**Tech Stack:** Terraform-templated bash (`startup-script.sh.tpl`), POSIX sh
resolver, docker compose overlays, pytest tpl-grep guards (existing
`tests/test_startup_*.py` pattern), GitHub Actions smoke job.

**Spec:** `docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`
Track A / A1 (as amended — see Deviation).

## Deviation from the master plan (recorded 2026-08-24)

The master plan's original A1 sketch said "fold the postgres service into the
default compose graph". Investigation killed that: the resolver keys the
postgres overlays off the *persisted backend state*, and an existing
DuckDB-state VM that suddenly received a `postgres` service + `DATABASE_URL`
from the base graph would run `data-migrate` and flip to PG on the next
auto-upgrade tick — an unreviewed stealth migration of the whole fleet,
exactly what A2 exists to do deliberately, instance by instance. The seed
flip below changes **new installs only** and reuses every battle-tested
piece. The master plan's A1 section carries a pointer to this deviation.

## Global Constraints

- Inherits every Global Constraint of the master program plan (CHANGELOG in
  same PR, vendor-neutral, draft PR after first commit, CI green = completed
  AND jobs>0 AND success, `/agnes-review` before ready).
- Dual-backend discipline still applies program-wide until A3 lands; this
  plan adds **no schema change** and **no repo change**, so no ladder work.
- Never run the full test suite locally; run the named test files only.
- Python: `.venv/bin/pytest`.
- Branch for this plan: `zs/a1-pg-default-install` off `origin/main`; one PR.
- Reserved files (do not touch): `app/api/agents*.py`, `app/api/broker*.py`,
  `src/repositories/__init__.py`, `connectors/jira/file_lock.py`.

## File Structure

- Modify: `infra/modules/customer-instance/startup-script.sh.tpl` (~line 111
  first-boot seed block; comment block ~100-105)
- Modify: `src/db_state_machine.py` (module docstring + the fresh-install
  default constant/helper — locate `DUCKDB` "fresh-install default" wording)
- Modify: `app/api/admin_doctor.py` (new-instance doctor: backend check)
- Modify: `config/.env.template` (~line 22, POSTGRES_PASSWORD guidance),
  `config/instance.yaml.example` (database section default),
  `docs/QUICKSTART.md`, `docs/DEPLOYMENT.md` (default = PG compose chain)
- Modify: `.github/workflows/release.yml` (smoke job compose invocation) +
  `scripts/smoke-test.sh` if it asserts backend anywhere
- Create: `tests/test_startup_pg_default.py`, `tests/test_doctor_backend.py`
- Modify: `CHANGELOG.md` (`## [Unreleased]`, `**BREAKING**` bullet)

---

### Task 1: startup script seeds `side_car` for new instances

**Files:**
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl:100-111`
- Create: `tests/test_startup_pg_default.py`

**Interfaces:**
- Produces: on a VM whose `/data/state/instance.yaml` does not yet exist,
  the seeded file contains `backend: side_car`; existing files are never
  rewritten (the seed block is already guarded by `if [ ! -f ... ]` — keep
  that guard intact).
- Consumes: `agnes_resolve_compose_file` (unchanged) — with the seeded value
  it emits the postgres overlays; `POSTGRES_PASSWORD` minting at tpl:276 and
  `DATABASE_URL` write at tpl:742-743 (both already unconditional).

- [x] **Step 1: Write the failing test** — follow the exact style of
  `tests/test_startup_dispatcher_pg_password.py` (read it first; it greps the
  tpl). New file:

```python
"""A fresh instance seeds Postgres side-car app-state, not DuckDB (A1)."""
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()


def test_first_boot_seed_is_side_car():
    # The first-boot instance.yaml seed block must default new instances
    # to the Postgres side-car backend.
    assert "backend: side_car" in TPL


def test_duckdb_is_not_the_seeded_default():
    # The literal seed line for the backend must no longer say duckdb.
    # (DuckDB remains a valid *persisted* state for existing instances;
    # only the fresh-install seed changes.)
    assert "backend: duckdb" not in TPL
```

- [x] **Step 2: Run it, confirm both assertions fail** —
  `.../bin/pytest tests/test_startup_pg_default.py -v` → 2 failed.
- [x] **Step 3: Edit the tpl seed block** — in the `if [ ! -f "$INSTANCE_YAML" ]`
  first-boot block (~line 111) change `backend: duckdb` → `backend: side_car`
  and rewrite the comment above (~100-105) to say: new instances start on the
  Postgres side-car; the state machine can still migrate them anywhere;
  existing instances keep their persisted backend untouched.
- [x] **Step 4: Re-run the test file** → 2 passed. Also run the neighboring
  guards that read the tpl: `.../bin/pytest tests/test_startup_guards.py tests/test_startup_dispatcher_pg_password.py tests/test_startup_instance_yaml_perms.py -q` → all pass.
- [x] **Step 5: `terraform -chdir=infra/modules/customer-instance validate`**
  (or `terraform fmt -check` + validate if init is needed; if no local
  terraform, note it in the PR body — CI/plan pipeline covers it).
- [x] **Step 6: Commit** — `feat(infra): fresh instances default to the Postgres side-car app-state`

### Task 2: state machine + config declare the new default

**Files:**
- Modify: `src/db_state_machine.py` (docstring lines 1-14; any
  `fresh-install default` wording; if a helper returns the default state for
  a missing persisted value, flip it — locate with
  `grep -n "duckdb" src/db_state_machine.py` and read each hit; the
  *persisted-state read* for existing instances must keep returning DUCKDB
  when the file says so)
- Modify: `config/instance.yaml.example` database section — document
  `backend: side_car` as the fresh-install default, `duckdb` as legacy for
  existing single-process installs (keep the key commented-out if it is
  commented today; accuracy of prose is the deliverable)
- Test: extend `tests/test_startup_pg_default.py`

**Interfaces:**
- Produces: `DbBackendState` default-for-missing-state used by any code path
  that asks "what backend is a brand-new install" says SIDE_CAR. IMPORTANT:
  `use_pg()` in `src/repositories/__init__.py` is a **reserved file** — if
  the missing-state default lives there, do NOT edit it; instead confirm the
  seeded instance.yaml (Task 1) is what drives it and record that in the
  test as a comment. Only touch `src/db_state_machine.py`'s own default.

- [x] **Step 1: Locate the default** — `grep -n "DUCKDB" src/db_state_machine.py`
  and read the hits. If the module has a function like
  `current_state(default=...)`/`initial_state()`, that default is the target.
  If the ONLY notion of "fresh default" is the seeded instance.yaml, then
  this task reduces to docstring + example prose — verify and proceed.
- [x] **Step 2: Failing test (only if a code default exists)**:

```python
def test_state_machine_fresh_default_is_side_car(tmp_path, monkeypatch):
    # A brand-new install (no persisted state file) resolves to SIDE_CAR.
    from src import db_state_machine as sm
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert sm.DbBackendState.SIDE_CAR == sm.fresh_install_default()
```

  Adjust the call to the module's real API discovered in Step 1 — if no such
  API exists, skip Steps 2-3 and only update prose (record the decision in
  the PR body).
- [x] **Step 3: Flip the default, re-run** → pass. Run the module's guard:
  `.../bin/pytest tests/test_db_state_machine.py -q` → all pass (fix any
  test that pinned the old default — those pins are the point of this task).
- [x] **Step 4: Update docstring + instance.yaml.example prose.**
- [x] **Step 5: Commit** — `feat: side_car is the fresh-install app-state default`

### Task 3: local/OSS quickstart defaults to the PG chain

**Files:**
- Modify: `config/.env.template` (~line 22): uncomment/promote
  `POSTGRES_PASSWORD=<generate one>` into the REQUIRED section with one line
  of why; add the canonical
  `COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml` line for
  local-dev PG.
- Modify: `docs/QUICKSTART.md` + `docs/DEPLOYMENT.md`: the primary
  `docker compose up` instruction becomes the PG chain (COMPOSE_FILE line or
  explicit `-f` pair); DuckDB single-file mode moves to a clearly-labelled
  "legacy fallback (existing installs)" subsection. Also update the
  `docker-compose.postgres.yml` header comment (lines 1-8) which still
  frames PG as the opt-in overlay — reword to "default for new installs;
  omit only on a legacy DuckDB instance".
- Test: `tests/test_docker_compose_postgres.py` — extend with a docs-honesty
  guard.

- [x] **Step 1: Failing test** — add to a NEW file
  `tests/test_quickstart_pg_default.py` (ruff-hook rule: new tests in new
  files):

```python
"""QUICKSTART's primary path is the Postgres compose chain (A1)."""
from pathlib import Path


def test_quickstart_primary_path_is_pg():
    text = Path("docs/QUICKSTART.md").read_text()
    assert "docker-compose.postgres.yml" in text


def test_env_template_requires_postgres_password():
    text = Path("config/.env.template").read_text()
    # An uncommented assignment line, not just prose mention.
    assert any(
        line.strip().startswith("POSTGRES_PASSWORD=")
        for line in text.splitlines()
    )
```

- [x] **Step 2: Run → both fail. Step 3: make the doc/template edits.
  Step 4: re-run → pass; also run `.../bin/pytest tests/test_docker_compose_postgres.py tests/test_compose_overlays_parse.py -q`.**
- [x] **Step 5: Commit** — `docs: Postgres compose chain is the default install path`

### Task 4: new-instance doctor asserts the backend

**Files:**
- Modify: `app/api/admin_doctor.py` (the new-instance doctor check list —
  read the module first; checks return structured pass/fail entries)
- Create: `tests/test_doctor_backend.py`

**Interfaces:**
- Produces: doctor check id `app_state_backend`: PASS when the resolved
  backend is `side_car`/`cloud`; FAIL with remediation text "fresh installs
  must run Postgres app-state (see QUICKSTART); DuckDB is legacy-only" when
  `duckdb`. (The doctor is explicitly a NEW-instance gate — running it on a
  legacy instance failing this check is correct and informative.)
- Consumes: backend resolution — read it the way the rest of the app does
  (instance-config/state read; if the only clean accessor lives in the
  reserved factory module, import the existing public helper — do not add
  code there).

- [x] **Step 1: Failing test** (TestClient over the doctor endpoint, seeded
  admin, monkeypatched backend state both ways — follow the existing doctor
  tests' fixture style; find them with `grep -rn "doctor" tests/ --include="*.py" -l`):

```python
def test_doctor_flags_duckdb_backend(admin_client, force_duckdb_state):
    body = admin_client.post("/api/admin/doctor/new-instance").json()
    check = next(c for c in body["checks"] if c["id"] == "app_state_backend")
    assert check["status"] == "fail"


def test_doctor_passes_side_car_backend(admin_client, force_side_car_state):
    body = admin_client.post("/api/admin/doctor/new-instance").json()
    check = next(c for c in body["checks"] if c["id"] == "app_state_backend")
    assert check["status"] == "pass"
```

  (Fixture names illustrative — reuse the doctor test module's real fixtures;
  write the two monkeypatch fixtures locally in the new file.)
- [x] **Step 2: fails (KeyError: no such check). Step 3: implement the check.
  Step 4: green; run the doctor module's existing tests too. Step 5:
  `make update-openapi-snapshot` if the doctor response docstring changed.**
- [x] **Step 6: Commit** — `feat(doctor): new-instance gate checks the app-state backend`

### Task 5: release smoke gate boots the PG chain

**Files:**
- Modify: `.github/workflows/release.yml` (smoke-test job, ~lines 247-321:
  read the job first — it boots a compose stack from the just-built image)
- Modify: `scripts/smoke-test.sh` — grep it for `duckdb`, `backend`,
  `COMPOSE_FILE`, `postgres` and update any assertion that pins the DuckDB
  default (this is the stale-smoke-assertion class that has rolled back
  releases before — treat every stale string found as in-scope).

**Interfaces:**
- Produces: the post-merge smoke job boots
  `docker-compose.yml:docker-compose.postgres.yml` with a generated
  `POSTGRES_PASSWORD`, waits for health, and runs the existing smoke
  assertions against a PG-backed app. A release that breaks PG-default boot
  now fails the gate *before* the fleet sees it.

- [x] **Step 1: Read the smoke job + script end to end; list every place the
  compose file set or backend is assumed.** Paste the list into the PR body.
- [x] **Step 2: Edit the workflow**: export `POSTGRES_PASSWORD=$(openssl rand -hex 16)`
  into the job env; set the compose invocation to include the postgres
  overlay (mirror however the job currently composes its `-f` chain).
- [x] **Step 3: Update `scripts/smoke-test.sh`** for any stale assumption
  found in Step 1; add one positive assertion: query the app's health/state
  endpoint that reports the backend (find it: `grep -rn "backend" app/api/health.py`)
  and require `side_car`.
- [x] **Step 4: Validate the workflow file** — `actionlint` if available
  locally, else note for CI. Run any smoke-script unit guards:
  `ls tests/ | grep -i smoke` → run the matching python guards.
- [x] **Step 5: Commit** — `ci: release smoke gate boots the Postgres-default stack`

### Task 6: changelog + PR assembly

- [x] **Step 1: CHANGELOG** under `## [Unreleased]`:

```markdown
### Changed
- **BREAKING**: Fresh installs now run app-state on the bundled Postgres
  side-car by default (VM provisioning seeds `database.backend: side_car`;
  QUICKSTART's compose chain includes `docker-compose.postgres.yml`, which
  requires `POSTGRES_PASSWORD` in `.env`). Existing instances keep their
  persisted backend; DuckDB app-state is now legacy-only for new deploys.
  The new-instance doctor gains an `app_state_backend` check and the release
  smoke gate boots the Postgres chain.
```

- [x] **Step 2:** `.../bin/python scripts/verify_syncmap.py` → fix flags.
- [x] **Step 3:** Push `git push origin HEAD:refs/heads/zs/a1-pg-default-install`,
  `gh pr create --draft` (body: deviation note, smoke-assumption list from
  Task 5 Step 1, no release cut), verify `gh pr checks` shows jobs.
  → PR #1545, 16 jobs registered.
- [ ] **Step 4:** `/agnes-review` on the branch; fix findings; `gh pr ready`
  only after CI green + review clean. Do not merge.

## Self-review notes

- No schema change → no ladder/alembic work → A3 independence holds.
- Reserved files untouched; doctor reads backend via existing public helper.
- The one genuinely risky surface is Task 5 (smoke gate) — its Step 1
  inventory-first approach is the mitigation for the known
  stale-assertion-rollback failure class.
