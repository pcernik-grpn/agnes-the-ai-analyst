# Extraction lanes and horizontal scale

**Status:** design only, nothing implemented. **Nothing here is recommended
for implementation without the owner reading it first.** No code in this PR
beyond this document.

**Scope:** how the SharePoint extraction pipeline gets past one connection at
a time — the worker's extraction lane, what a second worker container needs
to be safe, how two workers are kept off one connection, whether the Batch
API becomes viable, and in what order the four scaling levers should land.

Related: [fact-graph over Collections](2026-08-27-fact-graph-over-collections-design.md)
§7.5 (the extraction lane), [fact-extraction cost levers](2026-09-02-fact-extraction-cost-levers.md)
§5 (the Batch API assessment this document revisits), [extraction observability](2026-08-31-extraction-observability-ui-design.md),
PR #2061 (standalone facts trigger), PR #2059 (facts-phase checkpoint),
PR #2058 (per-item conversion bound).

---

## 1. The constraint as measured

The extraction lane has **one slot** (`_DEFAULT_EXTRACTION_CONCURRENCY = 1`,
`app/worker/runtime.py:168`). One connection's `corpus-extraction` job
occupies it for the whole crawl *and* the facts pass chained onto the crawl's
tail (`connectors/sharepoint/crawler.py:3883`); every other connection waits.

On a live deployment a full pass over 1,182 documents took 4 h 50 min of wall
clock across two runs, at a measured ~12 documents/minute, and the site-wide
corpus is an order of magnitude larger. Extrapolating the per-document rate:

| corpus | one lane, ~12 docs/min |
|---:|---|
| 1,182 docs | ~1.6 h (the measured pass took 4 h 50 — see §8) |
| 10,000 docs | ~14 h |
| 50,000 docs | ~3 days |

**Wall clock, not cost, is the binding constraint, and it is structural** —
no per-document optimization changes the shape of a single serial slot.

Two things compound it:

1. **Crawl and facts are coupled.** One job, one `_Deadline`, one slot. The
   profiles are opposite: the crawl is I/O-bound and memory-heavy (the parent
   process reached 12 GB on the live deployment), the facts pass is I/O-bound
   and memory-light (~790 MB), waiting on model calls. PR #2061 gives the
   facts pass its own trigger and budget, but registers the new kind in the
   *same* lane ("shares its concurrency ceiling rather than getting its own",
   #2061's `kinds.py` diff) — so it loosens the deadline coupling and leaves
   the slot coupling exactly where it was.
2. **A blocking poll would hold the slot.** The cost-levers spec rejected the
   Batch API *for now* because a poll loop parked on the single slot would
   starve every other connection for up to 24 h (§5.2b there).

---

## 2. What exists today

Everything below was read from the code; line numbers are against `main`
plus the open PRs named.

### 2.1 Lanes, slots, and which process runs them

- Three lanes: `heavy` (1 slot), `light` (2), `extraction` (configurable
  1..8, default 1) — `app/worker/registry.py:21-33`,
  `app/worker/runtime.py:155-206`. A lane is a set of asyncio tasks
  (`_lane_slot`, `:584`) each running `claim_next(kinds=<lane's kinds>)` →
  handler in `asyncio.to_thread` → `complete()`/`fail()`.
- `AGNES_WORKER_LANES` selects which lanes *this process* spawns slots for
  (`selected_lanes`, `:229`). Unset = heavy+light. The `extraction-worker`
  compose service sets `AGNES_ROLE=worker`, `AGNES_WORKER_LANES=extraction`
  (`docker-compose.yml:240-241`).
- `extraction.concurrency` / `AGNES_EXTRACTION_CONCURRENCY` sizes the
  extraction lane (`_extraction_concurrency`, `:272`); resolved once at
  worker start; changing it needs a restart.
- Two kinds sit in the extraction lane: `corpus-extraction`
  (`app/worker/kinds.py:1635-1649`) and, with #2061,
  `sharepoint-facts-extraction`.

### 2.2 Leases, heartbeat, reclaim

- Every extraction kind has a 300 s lease (`_DEFAULT_HEARTBEAT_PROTECTED_LEASE_S`,
  `kinds.py:247`), renewed every 100 s by `_heartbeat_loop`
  (`runtime.py:430-461`). The lease is a *liveness* ceiling, not a duration
  ceiling — a handler runs for hours as long as its process lives.
- `claim_next` reclaims a `running` job whose lease expired while
  `attempts < max_attempts` (`src/repositories/jobs.py:271`; Postgres variant
  uses `FOR UPDATE SKIP LOCKED`, `jobs_pg.py`). `max_attempts` defaults to 3
  (`src/db.py:1846`). A job whose *last* attempt's lease expires is swept to
  `failed` by `reap_exhausted` (`jobs.py:428`).
- `retry_in_seconds=None` on both extraction kinds means a handler
  *exception* finalizes to `failed` immediately — but a *crash* (SIGKILL,
  container recreate past the 45 s drain) goes through lease expiry and is
  re-run automatically, up to `max_attempts` times. These are two different
  retry policies and only the first was chosen deliberately (§5.4).
- A heartbeat that returns `False` (lease reclaimed) stops renewing; the
  handler thread **keeps running to completion** (`runtime.py:71-80`). This
  is the one place today where two processes can be inside the same
  connection's crawl at once (§4.1).

### 2.3 Per-connection state and who writes it

| file | writer | in-process lock | cross-process lock |
|---|---|---|---|
| `${DATA_DIR}/state/sharepoint_crawl/<id>.json` — cTags, deltaLinks, `failed_items`, `last_run` | the crawl, per item (cTag) and per delta page | `_state_lock` (`threading.RLock`, `crawler.py:450`) | **none** |
| `${DATA_DIR}/state/sharepoint_facts/<id>.json` — per-document `{status, extracted_sha, model, prompt_fingerprint}` | the facts pass, on every ingest flush | **none** (`facts_extraction.py:288-294`) | **none** |

Both are whole-file atomic snapshots (`tmp` + `os.replace`). Both are read
once at run start and written back from memory; a second concurrent writer
does not corrupt the file — it *overwrites* the other run's progress with its
own view. On the crawl file that regresses `delta_links`/`ctags` (a re-crawl
at best; at worst the "delta cursor ran past documents it never ingested"
state that `resync` exists to repair). On the facts file it double-spends
model tokens on every document both runs planned.

The only enqueue-time guard is the idempotency key: `corpus-extraction:<id>`
(`app/api/admin_sharepoint.py:798-805`) and, with #2061, a *separate*
`sharepoint-facts-extraction:<id>`. Distinct keys by design ("a
facts-extraction trigger can never dedup against corpus-extraction") — which
means a crawl whose tail is about to run `maybe_run_after_crawl` and a
standalone facts job for the same connection **can run at the same time
today**, both writing the facts state file, on one worker with
`extraction.concurrency ≥ 2` or on any two workers.

The cooperative stop signal is already cross-process: it rides
`source_connections.config.extraction.stop_requested_at` and is re-read from
the repo at every quiescent point (`crawler.py:306-400`).

### 2.4 The crawl's memory shape

Per run (`_run_crawl_async`, `crawler.py:3740+`): one `ThreadPoolExecutor`
of `extraction.crawler.concurrency` threads (default 6, `crawler.py:152`) and
one `_ConvertProcessPool` of the same size, forked at run start
(`:3782-3788`) — an active child *plus a pre-forked spare* per slot, each
capped at `RLIMIT_AS` 1,536 MB and recycled after 40 documents or 512 MB
peak RSS (`:190,197,217`). Conversion is the only step across a process
boundary. Download, sha256, anonymize, chunking and (if the `embeddings`
extra is installed) sentence-transformers embedding all run **in the parent**
(`_prepare_document` `:2513`, `_Ingestor.ingest` `:1842-1886`,
`src/ingest/runner.py:44-68`). PR #2058 names this residual honestly: "hash,
anonymize and ingest still run in-thread and are not bounded".

The pool's forks must happen from a single-threaded point (`:2139-2145`).
That holds approximately at `extraction.concurrency = 1` (the worker's
event-loop and heartbeat threads are alive but quiescent) and **does not
hold at all at ≥ 2** — the second crawl forks while the first crawl's six
`sp-crawl` threads and its facts threads are mid-flight, with DuckDB/psycopg/
OpenSSL locks possibly held. Nothing in the code prevents an operator from
setting `extraction.concurrency: 2` today; the instance.yaml note warns about
memory and tenant throttling, not about this.

### 2.5 How the worker is provisioned

- Plain compose: `extraction-worker` behind the `extraction-worker` profile,
  `depends_on: app: condition: service_healthy`, `mem_limit` default 4g,
  `cpus` 2.0, `stop_grace_period: 60s` (`docker-compose.yml:223-260`). The
  worker runs the full uvicorn app (`command: uvicorn app.main:app …`) with
  `AGNES_ROLE=worker`, so it serves `/api/health` and `/readyz` too.
- Terraform (`infra/modules/customer-instance`): `extraction_worker_enabled`
  renders a `docker-compose.extraction.yml` overlay on the VM
  (`startup-script.sh.tpl:1086-1119`) adding a `redis` service and
  `profiles: !reset []` + `depends_on: redis: service_healthy` on the worker.
  Redis is mandatory because `AGNES_ROLE=worker` is a role split, and
  `validate_deployment` refuses a role split without Postgres app-state,
  explicit secrets and `coordination.backend: redis` (`app/startup_guards.py`).
  The worker is brought up *after* and *tolerantly of* the strict base stack
  (`:1657-1675`). One VM per instance; `/data` is the VM's attached disk.
- The state applier recreates `app scheduler` with `--no-deps --force-recreate`
  (`scripts/ops/agnes-state-applier.sh:530,877`) — no cascade. The
  auto-upgrade role-split branch recreates `worker gateway` with `--no-deps`
  (`agnes-auto-upgrade.sh:669`) — note it names `worker`, not
  `extraction-worker`. The ordinary paths (VM boot, `docker compose up -d`
  by hand, the non-role-split upgrade branch) do cascade through
  `depends_on`: recreating `app` recreates `extraction-worker`. This is the
  coupling the owner observed; it was not re-verified against a live compose
  here (§8).
- The host watchdog matches role containers by compose *service* name
  (`agnes-watchdog.sh:106`, `ROLE_CONTAINER_RE` includes `extraction-worker`),
  so scaled replicas of the same service are still seen; a *new* service
  name (e.g. `facts-worker`, §3) would not be until the regex is extended.

---

## 3. Q1 — Two lanes, not one lane with two slot classes

### 3.1 The argument

The question is whether crawl and facts should be separate job kinds with
separate concurrency (two lanes) or one lane whose slots are typed.

**Two lanes.** Three reasons, in order of weight:

1. **The lane is already the unit that maps onto a process's resource
   envelope.** `AGNES_WORKER_LANES` is per-process; `mem_limit` is
   per-container. A crawl slot needs a 12 GB-class envelope and must not
   share a process with another crawl (§2.4's fork hazard); a facts slot
   needs ~1 GB and can share a process with several others. Only lane
   selection lets an operator put those two on different containers with
   different limits. A typed slot inside one lane would have to be selected
   per process anyway — at which point it *is* a lane with a worse name.
2. **The machinery exists.** A new lane is: one constant in `registry.py`,
   one entry in `_LANE_CONCURRENCY` and `_ALL_LANES`, one resolver mirroring
   `_extraction_concurrency`, one token in `AGNES_WORKER_LANES`. Typed
   capacity would need a new predicate in `claim_next` (which is
   `kind IN (...)` today) and a second counter per slot class inside
   `_lane_slot` — new mechanism, same outcome.
3. **The facts pass must stop being the crawl's tail.** Whatever the lane
   shape, the wall-clock win only arrives when the crawl slot is released
   the moment the crawl finishes. That is a *job* boundary, i.e. two kinds —
   which #2061 already created. Two kinds in one lane still serialize.

**What a typed lane would buy that two lanes do not:** a single shared cap
("at most N extraction jobs of any kind on this host"). Nobody has asked for
that, and the host cap is really a memory cap, which the two-container split
expresses directly.

### 3.2 The design

- **New lane `facts`** (`FACTS_LANE = "facts"`), concurrency from
  `extraction.facts.lane_concurrency` / `AGNES_FACTS_LANE_CONCURRENCY`
  (default 2, clamped 1..8, resolved once at start like the extraction lane).
  `sharepoint-facts-extraction` registers in it. The existing `extraction`
  lane keeps its name and its single kind, `corpus-extraction`, for
  backward compatibility of every `.env` that already says
  `AGNES_WORKER_LANES=extraction`.
- **The crawl's tail enqueues instead of running.** `maybe_run_facts_extraction`
  (`crawler.py:3883`) becomes "if `extraction.facts.enabled` and
  `facts.enabled`, enqueue `sharepoint-facts-extraction` for this connection
  under its idempotency key". The enqueue is best-effort and logged, the
  same shape as `_maybe_enqueue_distribution_mirror` (`kinds.py:435`). The
  crawl's `extraction_runs` row finalizes as the crawl's own outcome; the
  facts pass gets its own row (§3.4). `_Deadline` is no longer shared, so the
  "900 s crawl left the chained pass an already-expired deadline" incident
  (#2061's motivation) cannot recur, and `extraction.facts.run_timeout_s`
  (#2061) becomes the *only* facts budget.
- **The `extraction-worker` service selects both lanes by default**
  (`AGNES_WORKER_LANES=extraction,facts`), so a one-container deployment
  changes capacity from "crawl then facts, serially" to "one crawl plus two
  facts passes, concurrently" with no new container. An operator who wants
  the memory split runs a second service, `facts-worker`, same image,
  `AGNES_WORKER_LANES=facts`, `mem_limit` sized to
  `lane_concurrency × ~1 GB` (§5.3).
- **Outbound model concurrency multiplies:** `lane_concurrency ×
  extraction.facts.concurrency` (the per-pass thread pool, default 3,
  `facts_extraction.py:1467`) is how many model calls are in flight from one
  worker. Document it next to the crawl's equivalent warning
  (`instance.yaml.example:1374-1380`).

### 3.3 Operator-facing knobs

| knob | where | default | meaning |
|---|---|---|---|
| `AGNES_WORKER_LANES` | env, per process | heavy,light | gains the `facts` token; `extraction-worker` ships `extraction,facts` |
| `extraction.concurrency` / `AGNES_EXTRACTION_CONCURRENCY` | yaml / env | 1 | crawl-lane slots per process — **keep at 1 per process until conversion-in-children lands (§2.4, §7)**; the yaml note should say so |
| `extraction.facts.lane_concurrency` / `AGNES_FACTS_LANE_CONCURRENCY` | yaml / env | 2 | facts-lane slots per process (new) |
| `extraction.facts.concurrency` | yaml | 3 | model calls in flight per pass (existing, unchanged) |
| `extraction.facts.run_timeout_s` | yaml | 3600 | the facts pass's own budget (#2061, now the only one) |
| `AGNES_FACTS_WORKER_MEM_LIMIT` / `_CPUS` | .env | 2g / 1.0 | for the optional split `facts-worker` service (new) |

No new switch for "chain facts after crawl": `extraction.facts.enabled` +
`facts.enabled` already say whether the pass runs at all; the only change is
*where*. An inline mode is deliberately not kept — it would preserve exactly
the coupling this document removes.

### 3.4 What an operator sees, and what breaks

- **Seen:** connection A's crawl ends at hour 3 and its card immediately
  shows *two* things — the finished crawl run and a queued/running facts run
  — while connection B's crawl starts in the freed slot. Today the card shows
  A "extracting facts · N/M" (#2059) for the next two hours and B "queued".
- **Gap (must ship with this):** #2061's standalone facts job writes **no
  `extraction_runs` row** ("a caller with no crawl run to attach to … runs
  cleanly with no checkpoint of its own", #2059). Once every facts pass is a
  standalone job, an operator has *only* the `jobs` row to look at. The
  facts job must open its own run row (`phase="facts"`, `job_id` set) and
  drive `checkpoint_facts` from `on_progress`, so `_derived_outcome`
  (`app/api/admin_extraction.py:193`) and the 30-minute stall rule work for
  it unchanged.
- **Breaks if the facts lane is selected nowhere:** the crawl enqueues a
  facts job that nothing ever claims; it sits `queued` forever, one per
  connection (the idempotency key prevents stacking). The card would show
  "facts: queued since <T>" indefinitely. Mitigation, cheap: every worker
  publishes its selected lanes to the coordination backend on each poll tick
  (`coordination().kv_set(f"worker:{worker_id}:lanes", ..., ttl_s=2×poll)`),
  and the admin extraction status endpoint renders "facts lane: 0 live
  workers" when no key names it. This also answers "is the extraction worker
  up at all", which today is only visible from the host watchdog.
- **Breaks (already, made likelier):** the crawl-vs-standalone-facts double
  writer from §2.3. Lane separation raises the chance that both run at once
  from "only with `extraction.concurrency ≥ 2`" to "whenever an operator
  clicks *run facts* during a crawl". §4 is therefore a prerequisite, not a
  follow-up.

---

## 4. Q3 — Per-connection exclusivity

### 4.1 Today's guarantee and its three holes

The idempotency key is an *enqueue-time* dedup of *jobs*: one `queued`/
`running` `corpus-extraction` per connection. It does not prevent:

1. **Reclaim-while-alive.** A worker whose heartbeat fails for > 300 s (DB
   hiccup, paused container, a `to_thread` starved by a runaway conversion)
   loses its lease; another slot or worker reclaims the job and starts the
   same connection's crawl; the original handler thread is *not* cancelled
   and keeps writing the state file (`runtime.py:71-80`). With one process
   `_state_lock` interleaves the two safely at the byte level and unsafely at
   the semantic level; with two processes there is no lock at all.
2. **Two kinds, two keys.** Crawl-tail facts vs. standalone facts (§2.3).
3. **Out-of-band writers.** `_apply_resync` (`crawler.py:3999`) rewrites the
   crawl file from the job handler before the crawl starts; it is inside the
   job today, but it is the pattern an admin "reset state" endpoint would
   follow, and it must take the same lock.

### 4.2 Design: a Postgres advisory lock per state file, held for the run

Two lock keys per connection, one per state file — `sp-crawl:<id>` and
`sp-facts:<id>` — each `hashtext()`-ed into a bigint. The crawl handler takes
`sp-crawl` for the whole `run_builtin_crawl`; the facts handler takes
`sp-facts` for the whole `run_standalone_facts_extraction`. Both are
`pg_try_advisory_lock` (fail, never block): a job that finds its lock held
finalizes as `failed` with a typed error `connection_busy` naming the
holder's `worker_id`/`job_id` — the same "refuse and name the fix" posture
the trigger endpoints already have — and the admin trigger, which already
pre-checks readiness, adds the lock owner to its 409 detail.

The precedent is `src/db_pg.py::rebuild_lease` (`:483-502`): a session-scoped
`pg_advisory_lock` on a pinned `engine.connect()` held across the critical
section. The one behavioural change is `try` instead of blocking — a crawl
must not queue behind another crawl on a worker slot; that is what the jobs
table is for.

**Checked at quiescent points, not just taken at start.** The holding session
can die under a live handler (the same class of failure as heartbeat loss).
So the crawl re-asserts ownership wherever `_StopWatcher` already polls —
every delta page and every 10 items (`crawler.py:_STOP_CHECK_EVERY_ITEMS`) —
via `pg_advisory_lock_shared`-free check: `SELECT … FROM pg_locks WHERE
objid = :key AND pid = pg_backend_pid()` on the pinned session (or simpler:
the session raising on its next statement *is* the signal). Loss → stop with
a new named reason `"lock_lost"` in `_STOP_REASONS`, which — like
`"timeout"`/`"stopped"`/`"throttled"` — licenses the "next run resumes" copy,
because the run persisted state at its last page boundary and the new holder
resumes from it.

The **idempotency key stays** as the cheap front door (202 vs 409 without
touching the lock); the advisory lock is the guarantee behind it.

### 4.3 A crashed holder

- **Process dies (SIGKILL, OOM, container recreate):** the kernel closes the
  socket; Postgres releases the advisory lock immediately. The job's lease
  expires ≤ 300 s later; any worker reclaims it (`attempts + 1`), takes the
  lock, loads the state file — which is the last complete page's snapshot —
  and resumes. Nothing an operator has to do; the card shows one run
  `failed`/`interrupted` and a new one `running` with the same `job_id`.
- **Host dies or network partitions with a *remote* Postgres:** the server
  keeps the session until TCP keepalive gives up. On managed Postgres that
  can be tens of minutes to hours by default. The reclaimed job would then
  fail with `connection_busy` naming a dead holder, and retry via
  `retry_in_seconds`… which is `None` on these kinds. Two mitigations, both
  needed: set `keepalives_idle`/`keepalives_interval` on the pinned lock
  session (libpq connection params, seconds not hours), and give
  `connection_busy` a bounded retry (`fail(..., retry_in_seconds=300)` for
  that error only) so a stale holder is a delay, not a dead job. Whether the
  production Postgres is a sidecar (dies with the VM — lock gone) or managed
  (§8) decides how much this matters.

### 4.4 Why not the alternatives

- **Coordination lease (`lease_acquire/renew`, `app/coordination/base.py:86`).**
  Redis is declared ephemeral (`--save "" --appendonly no`); its FLUSHALL
  story (`leases.py` docstring) is "every holder stops and re-acquires" —
  acceptable for a Slack socket, not for a five-hour crawl that would have to
  stop at its next page on every Redis restart. It also adds a second renew
  loop next to the jobs heartbeat with its own TTL to reason about. The
  advisory lock ties liveness to the *same* database session model the
  heartbeat already depends on.
- **`fcntl.flock` on the state file.** Works on one host over the shared
  volume, breaks silently on any network filesystem, and cannot name the
  holder. It would also lock the file, not the *connection*, so the facts
  file and crawl file need two anyway.
- **A `status` column on `source_connections`.** A row flag with no
  liveness — exactly the "SIGKILLed worker finalizes nothing" trap
  `_derived_outcome` exists to work around.

The advisory lock is Postgres-only, which is fine: the two-worker topology is
already Postgres-only by the startup guard, and a DuckDB single-process
instance keeps `_state_lock` and can never have a second claimer.

---

## 5. Q2 — Horizontal scale: a second `extraction-worker`

### 5.1 Job claiming: already safe

`claim_next` on Postgres is `FOR UPDATE SKIP LOCKED` on a single statement;
`worker_id` is `hostname:pid`, and a container's hostname is its container
id, so two replicas never collide on identity. `lease_token` per claim
already handles the reclaim race. **Nothing to build here.**

### 5.2 State files: one host, shared volume, and §4

Both containers mount the same `data:` volume, so `${DATA_DIR}/state/…` is
one directory. `os.replace` makes each write atomic; §4's lock makes the
writers exclusive. Corpus blobs (`store_corpus_bytes`, `src/file_storage.py`)
and the DuckDB extracts are on the same volume. **Without §4, two workers
are unsafe; with it, one host is safe.**

### 5.3 Conversion pool and the per-host memory budget

The pool is per run and per process; replicas do not share it, which is the
right thing — its fork constraint (§2.4) is per process. The budget is:

```
per crawl container ≈ parent RSS (12 GB observed) + 2 × crawler.concurrency children
                       (RLIMIT_AS 1.5 GB each is virtual; recycled at 512 MB RSS)
per facts container ≈ lane_concurrency × ~0.8 GB
```

The Terraform default `extraction_worker_mem_limit = "4g"` (`variables.tf:216`)
is three times smaller than the measured parent; either the live deployment
raised it or the cgroup is not where the 12 GB was measured (§8). A second
*crawl* container on the same VM is therefore a **machine-type decision
first**: on a VM that cannot hold 2 × (parent + children) plus `app`,
`scheduler`, `postgres` and `redis`, the second crawl container gets the
whole host OOM-killed, and the kernel's choice of victim is indiscriminate —
the observed "140 unrelated files failed" (#2058) at container scale. A
second *facts* container costs ~2 GB and is safe on any VM that already runs
the crawl.

So "horizontal" splits into two very different levers:

- **facts-lane replicas** — cheap, safe after §3+§4, and the one that scales
  the expensive phase linearly (the model endpoint is the ceiling).
- **crawl-lane replicas** — bounded by host RAM and by the parent's own
  footprint, which is what conversion-in-children is meant to shrink (§7).

### 5.4 `depends_on: app` — and `max_attempts` consumed by restarts

Recreating `app` cascades into `extraction-worker` on every path that does
not pass `--no-deps` (§2.5). Each cascade: SIGTERM → 45 s drain
(`AGNES_WORKER_DRAIN_TIMEOUT_S`) → the crawl is abandoned mid-page → the
container is gone before 60 s → the job stays `running` → lease expires
≤ 300 s later → reclaimed with `attempts + 1`. Progress loss is one page
(state is saved per page and per cTag), so the crawl itself is fine. **The
job is not:** `max_attempts` defaults to 3, so the *third* infrastructure
restart during one long crawl leaves it unreclaimable, and `reap_exhausted`
marks it `failed` with "lease expired after max attempts" — a crawl that did
nothing wrong, reported to the operator as broken, and not restarted until
someone clicks. On a 14-hour crawl with an auto-upgrade tick every 5 minutes
and a state-applier that recreates `app`, three restarts is not hypothetical.

Three changes, independent:

1. **Break the cascade.** The worker does not need the app's HTTP; it needs
   Postgres and Redis. Replace `depends_on: app: condition: service_healthy`
   with `service_started` (or drop it and depend on `redis` only in the
   overlay). What the healthy-gate was really buying is "the app has run the
   Alembic migrations before the worker boots against the schema" — and the
   worker already crash-loops cleanly on a schema-head mismatch
   (`restart: unless-stopped`, the "pin drift crash loop" the Terraform
   variable describes), so the gate is redundant with a behaviour that
   already exists. State that trade-off in the compose comment: a worker
   booting first spends a few restart cycles, a worker restarting mid-crawl
   spends a page and an attempt.
2. **Stop charging attempts for restarts.** Enqueue extraction kinds with a
   larger `max_attempts` (the enqueue call accepts it; e.g. 20), *or* have
   the crawl reset `attempts` on a clean page checkpoint. The first is one
   argument in two `enqueue` calls; the second needs a repo method. Prefer
   the first, and log the reclaim count on the run row so the card can say
   "resumed 3× after worker restarts".
3. **Name `extraction-worker` in the role-split recreate path.**
   `agnes-auto-upgrade.sh:669` recreates `worker gateway`; the extraction
   worker is a role-split container too and should be in the same
   `--no-deps` set, not left to the cascading default.

### 5.5 Replica mechanics

- `docker compose up -d --scale extraction-worker=2` works today (no
  `container_name`, no host port), but no script passes `--scale`. Put
  `deploy: replicas: ${AGNES_FACTS_WORKER_REPLICAS:-1}` on the *facts*
  service in the overlay (compose v2 honours `deploy.replicas` outside
  swarm) and leave the crawl service at 1 until §5.3's sizing is done.
- The watchdog matches by service name, so replicas are scanned; a new
  `facts-worker` service name must be added to `ROLE_CONTAINER_RE`.
- The startup script's tolerant bring-up (`:1657-1675`) needs the new
  service in its `pull`/`up -d` list; the Terraform variables need
  `facts_worker_replicas`, `facts_worker_mem_limit`.

### 5.6 Cross-host scale is blocked by `/data`

Every piece of per-connection state, every corpus blob and every extract is
on the VM's attached disk. A second VM cannot mount it. Moving the crawl and
facts state to Postgres rows (the facts file is already row-shaped: one
entry per `file_id`) and corpus blobs to the object store the distribution
mirror already knows how to talk to are the two prerequisites for a worker on
another host. Neither is in scope here; both are named so nobody plans
"second VM" as the next step after "second container".

---

## 6. Q4 — Batch API fit

### 6.1 Does lane separation make it viable?

**Partly.** The cost-levers spec's objection was that a blocking poll would
hold *the* slot and starve every other connection's crawl. With a facts lane,
a poll holds a *facts* slot; crawls are unaffected. But a facts slot parked
for up to 24 h still starves every other connection's facts pass — the facts
lane is small by design (§5.3) and a 24-hour occupant of one of two slots
halves it. So lane separation turns "must not" into "should not", and the
right shape is still **submit-and-return**. What lane separation *does*
settle is where the state machine runs and what it competes with: it runs
as ordinary short `sharepoint-facts-extraction` jobs in the facts lane,
competing only with other facts jobs.

It also moves the crossover. In-process facts throughput is now
`replicas × lane_concurrency × facts.concurrency` model calls in flight,
not 3. The wall-clock argument for batching (cost-levers §5.3) weakens
accordingly; the 50 % cost argument does not change at all. So the trigger
for building the machine becomes **cost**, not wall clock: roughly the point
where one pass's model bill is large enough that halving it pays for the
machine's upkeep — on today's $0.054/document that is ~10k documents per
pass (~$540, ~$270 saved), consistent with the cost-levers spec's "~10k+"
estimate, and about twice that once its levers 1–5 land.

### 6.2 The machine, built on what the jobs table already has

`jobs.run_after` (`src/db.py:1845`) is honoured by `claim_next`
(`jobs.py:270`). A job can therefore re-enqueue *itself* for later and
complete — no scheduler row, no new mechanism. The facts job gains a
`payload.mode`:

```
submit  ─▶ plan (unchanged _plan()) ─▶ build ≤5,500-doc batches (256 MB cap)
           ─▶ POST batches ─▶ persist {batch_id, custom_id→file_id} ─▶ mark docs in-batch
           ─▶ enqueue self {mode: collect, run_after: now+poll_s} ─▶ complete   (minutes)

collect ─▶ for each open batch: GET status
           ├─ processing ─▶ enqueue self {collect, run_after: now+backoff} ─▶ complete   (seconds)
           └─ ended ─▶ stream results by custom_id ─▶ verbatim gate ─▶ _BatchShipper
                        ─▶ mark docs done ─▶ failures? ─▶ submit retry batch (one cycle)
                        ─▶ close batch ─▶ complete
```

Each job is minutes at most, so the 300 s lease and the 100 s heartbeat are
untouched; the *batch* outlives the job by design, the *job* never outlives
its lease. The per-connection lock (§4) is taken per job, not across the
batch. The idempotency key `sharepoint-facts-extraction:<id>` still means
"one facts job per connection at a time" — which now includes a `collect`
parked on `run_after` in the future. The manual trigger's 409 must say
"awaiting batch results (N documents, submitted <T>)" rather than
"already running", or an operator will assume a hang.

### 6.3 State

Per connection, alongside the per-document entries — in
`sharepoint_facts/<id>.json` for v1 (single writer under §4's lock, no
migration) with the explicit note that it belongs in a Postgres table the day
§5.6 moves the file:

```
batches: { <batch_id>: { provider, submitted_at, expires_at, model,
                          prompt_fingerprint, status: submitted|ended|collected,
                          docs: { <custom_id>: <file_id> }, collected_cursor,
                          retry_of: <batch_id>|null } }
docs:    { <file_id>: { status: in-batch, batch_id, sha256 } | done | skipped-* }
```

`in-batch` is the third per-document state the cost-levers spec asked for
(§5.2c): `_plan()` skips it, so a document is never submitted twice while a
batch holding it is open; a batch past `expires_at` with no result flips its
docs back to unsubmitted. A crash mid-collect resumes from
`collected_cursor` against results that stay readable for 29 days —
strictly better durability than today's in-flight thread pool.

### 6.4 Observability

`extraction_runs` gains a run per `submit` and per *final* `collect`; the
intermediate `collect` ticks checkpoint the same row (`phase="facts-batch"`,
progress = the provider's `request_counts`). The 30-minute stall rule needs
one exception: a row whose phase is `facts-batch` is `waiting`, not
`stalled`, until `expires_at`.

### 6.5 When

Not in this sequence's first three steps. Build it when a deployment's facts
bill per pass crosses the §6.1 threshold, *after* the cost levers that shrink
the bill it halves, and after §3 — because §3 is what makes the machine's
jobs cheap to run and harmless to park.

---

## 7. Q5 — Sequencing the four levers

| # | lever | why here | prerequisite for |
|---|---|---|---|
| 0 | **Per-connection exclusivity (§4)** | Not one of the four, but every lever below adds a second claimer; the crawl-vs-facts pair is exposed *today* by #2061 + `extraction.concurrency ≥ 2`. Small. | 1, 3, 4 |
| 1 | **Lane separation (§3)**, incl. enqueue-chaining, the facts run row, and the lane-coverage kv | Removes the structural coupling; the only lever that changes the *shape* of the wall clock; unblocks facts replicas at ~2 GB each. Medium. | 3 (facts side), Batch |
| 2 | **Curation** — extract facts only from scopes that earn it | Independent of the machinery and multiplies every other lever: fewer documents is the cheapest wall-clock and cost win the facts phase has, and it may make crawl replicas unnecessary. Ships as a per-scope flag (like the anonymize checkbox) filtered in `_plan()`. Does nothing for the crawl phase, which must still index everything for search. Small. | — |
| 3 | **Conversion truly in child processes** (in flight, another agent) | The gate on *crawl-lane* concurrency > 1 per process (§2.4's fork hazard) and on the parent's 12 GB footprint (§5.3). Its interface decides whether "two crawls per container" is ever safe — if the pool becomes `spawn`-based or persistent-per-process rather than fork-per-run, the constraint lifts; if not, crawl scale stays one-per-container. | 4 (crawl side) |
| 4 | **Horizontal workers (§5)** — facts replicas first, crawl replicas after 3 and after host sizing | Facts replicas are safe after 0+1 and cost ~2 GB each. Crawl replicas need 3, §5.4's three fixes, and a VM that holds them. Cross-host needs §5.6 first. | Batch (capacity to park jobs) |
| 5 | **Batch API (§6)** | A cost lever with a state-machine price; worth it once a pass's bill is ~$500+, after the cost levers, on top of 1. | — |

The measured 4 h 50 min was two runs of crawl+facts in one slot. Levers 0–2
alone turn the next full pass into: crawl in one slot at the crawl's own
rate, facts for that connection starting the minute the crawl ends in a
second slot, over a curated subset — with no new container. That is the
recommended first milestone; §5 and §6 are what to build if it is not
enough.

---

## 8. What I could not determine

- **The crawl/facts split of the 4 h 50 min.** At ~12 documents/minute,
  1,182 documents is ~100 minutes of crawl; the remaining ~3 hours are the
  facts tail, throttling backoff, the second run's re-enumeration, or some
  mix. Pre-#2059 runs record no per-phase timing. The first run with #2059's
  `phase` checkpoints answers it and decides how much of §7 lever 2 vs
  lever 4 is worth.
- **Where the 12 GB parent RSS comes from.** Candidates: the `embeddings`
  extra (sentence-transformers → torch in the parent, `src/ingest/embeddings.py:28-36`),
  the LLM anonymizer tier's client, httpx download buffers, DuckDB. Whether
  the live image was built with `EXTRA_EXTRAS=",embeddings"` is not visible
  from the repo. This sets §5.3's per-container budget and how much
  conversion-in-children can recover.
- **Whether the cascade is exactly as described.** §5.4 takes the owner's
  observation ("restarts the worker whenever the app is recreated") as given
  and maps it onto the scripts that do and do not pass `--no-deps`; it was
  not reproduced against a live compose project here.
- **Sidecar vs managed Postgres on the live deployment.** Decides whether
  §4.3's dead-holder lock can outlive a dead host and whether the keepalive
  settings are load-bearing.
- **The live VM's machine type.** The module default is `e2-small`; the
  12 GB parent says the live one is much larger, but not how much headroom
  a second crawl container has.
- **What conversion-in-children will change about the fork model.** §7's
  lever 3 row assumes only that it shrinks the parent and keeps the
  pool-per-run shape; if it also moves anonymize/ingest out of the parent,
  crawl-lane concurrency per container may become safe sooner.
- **Batch API facts** (256 MB per batch, results only after the batch ends,
  29-day retention) are taken from the cost-levers spec, not re-verified
  against provider documentation here.
- **Compose `deploy.replicas` behaviour with the overlay's `profiles: !reset []`.**
  Expected to compose; not tested.

---

## 9. Recommendation in one paragraph

Split the facts pass into its own lane and make the crawl *enqueue* it
rather than run it (§3), guarded by a Postgres advisory lock per connection
state file (§4) — the lock first, because #2061 already lets a crawl and a
standalone facts pass write the same file. Do not run two crawls in one
process until conversion-in-children lands; do run facts replicas as soon as
§3 exists. Fix the three restart problems in §5.4 before any long crawl is
relied on, regardless of scale. Treat the Batch API as a cost lever to adopt
on top of the facts lane when a pass's bill justifies a resumable
submit/poll/collect job pair built on `jobs.run_after` (§6), not as a
wall-clock lever.
