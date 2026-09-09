# Agnes Data Apps — Design

**Date:** 2026-07-21
**Status:** Draft for review
**Verified against:** `0.76.2` (HEAD `c8d9360a`)
**Upstream runtime verified against:** `keboola/data-app-python-js` (README + `docs/bootstrap.md`, 2026-07-21)

## 1. Context & goal

Agnes today serves data (parquets, catalog, query) and agent surfaces (web
chat, Slack, MCP, CLI), but has no way to **host a user-built web application**
next to the data. Analysts who build a dashboard or an internal tool on top of
Agnes data must find hosting elsewhere — losing the auth, RBAC, and data
locality Agnes already provides.

The upstream Keboola platform solved the same problem with **Data Apps**, and
its newer *Python/JS* app type ships as a reusable, MIT-licensed base Docker
image: [`keboola/data-app-python-js`](https://github.com/keboola/data-app-python-js).
The image is deliberately platform-independent — at container start it clones
an app repo from git, installs dependencies, and runs the app behind an
in-container nginx on port 8888. The only platform coupling (the Data Loader
input-mapping sidecar) is optional and gated behind a single env var.

**Goal:** add first-class Data Apps to Agnes by reusing that runtime image
verbatim, and building only the thin platform shell around it:

1. **Registry + lifecycle** — register, deploy, redeploy, stop, logs.
2. **Ingress** — apps served behind Agnes auth + RBAC at a stable URL.
3. **Auto-sleep** — idle apps release resources; wake on first request.
4. **AI authoring as a first-class flow** — the Agnes chat agent can scaffold,
   deploy, test, and iterate on a data app end-to-end, from a conversation.
   This is a hard requirement, not a nice-to-have: the design below treats the
   agent as the primary app author and the web UI as the secondary one.

**Non-goals (v1):**

- No Streamlit-specific runtime (the generic image runs Streamlit fine as
  "any Python framework" if someone wants it).
- No automatic wildcard-DNS provisioning — subdomain routing (§6) ships in v1
  as an opt-in, but supplying the wildcard DNS record + TLS is the operator's
  responsibility; path-prefix routing is the zero-config default.
- No emulation of the upstream Data Loader input-mapping API (apps read data
  through the Agnes REST API with a scoped token; see §8).
- No multi-VM app scheduling — apps run on the same host as the Agnes stack,
  consistent with the single-VM deployment model.

## 2. What we reuse — the upstream runtime contract

The base image (`keboola/data-app-python-js`, MIT) provides, per its README
and `docs/bootstrap.md`:

- **Base:** Debian Bookworm slim with Python (`uv`), Node.js (npm/yarn), and
  Bun — all optional per app. Runtime versions pinned via a release matrix
  (tags like `1.0.0_python-3.11_node-20`).
- **Entrypoint sequence:** read `/data/config.json` → export secrets as env
  vars → clone `dataApp.git.repository` (branch/`#sshKey`/`username`+`#password`
  supported) into `/app` → validate `keboola-config/` → run
  `keboola-config/setup.sh` → start Supervisord (or `/app/run.sh`).
- **App repo contract (fixed):**

  ```
  your-app/
  ├── keboola-config/
  │   ├── nginx/sites/default.conf       # required — routes :8888 → app port
  │   ├── supervisord/services/app.conf  # required — startup command
  │   └── setup.sh                       # optional — uv sync / npm install
  └── ... app code ...
  ```

- **Networking:** in-container nginx listens on **8888**; the app process
  listens on any internal port ≥1024 and nginx `proxy_pass`es to it.
- **Extension point:** stage 5 of the bootstrap (`hooks/bootstrap-app.sh`) is
  replaceable in derived images; the `git-clone.sh` helper is standalone.
- **Platform-coupling switch:** when `DATA_LOADER_API_URL` is **unset**, the
  Data Loader readiness gate, input mapping, and git commit-hash locking are
  all skipped and the container clones HEAD of the configured branch. Agnes
  runs the image in exactly this mode — zero upstream services needed.

**Compatibility stance:** Agnes adopts the `keboola-config/` app-repo contract
**unchanged**. An app written for upstream Data Apps deploys on Agnes without
modification and vice versa (data access aside). We do not fork the image; we
consume published tags and pin one per Agnes release
(`data_apps.runtime_image` in config, with a shipped default).

**Image distribution (resolved):** the image is already **publicly pullable
anonymously** from `keboolapublic.azurecr.io/data-app-python-js` (release tags
`1.6.x` plus reproducible matrix variants like `1.6.2_python-3.13_node-24`);
upstream CI pushes every release there. Agnes pins a full matrix tag from this
registry as the shipped default. The *source* repo staying private is
irrelevant to consumers — the runtime scripts are inspectable from the image
itself.

**Commit pinning without Data Loader:** upstream pins the deployed commit via
a state key in the Data Loader. Agnes gets the same determinism from git
itself: for internally hosted repos the control plane maintains an
**`agnes-live`** branch per app repo; *deploy* = fast-forward `agnes-live` to
the chosen SHA + (re)create the container with `dataApp.git.branch:
"agnes-live"`. A sleeping app that wakes re-clones `agnes-live` and therefore
always gets exactly the deployed commit. For external repos (BYO GitHub URL),
v1 pins the configured branch only (HEAD-at-wake); pinning by SHA for external
repos is future work.

## 3. Architecture overview

```
                       ┌─────────────────────────── VM / docker compose ───────────────────────────┐
   browser / agent     │                                                                            │
        │              │  ┌────────┐    /apps/{slug}/*   ┌─────────────────────────┐                │
        ├── https ────►│  │ caddy  ├──► agnes app ───────►  data-app container     │                │
        │              │  └────────┘    (FastAPI proxy,  │  (keboola/data-app-     │                │
        │              │                auth + RBAC +    │   python-js, nginx:8888)│                │
        │              │                touch last_seen) └─────────▲───────────────┘                │
        │              │                                            │ docker API                    │
        │              │  ┌───────────┐  HTTP (internal)  ┌────────┴────────┐                       │
        │              │  │ scheduler ├──► /api/data-apps │   apps-runner    │ ← /var/run/docker.sock│
        │              │  └───────────┘    /reap-idle     │   (sidecar)      │                       │
        │              │                                  └──────────────────┘                       │
        │              │  agnes app also serves:  /data-apps.git/{slug}/* (git http-backend, push)   │
        └──────────────┴────────────────────────────────────────────────────────────────────────────┘
```

Five moving parts, four of which extend existing Agnes subsystems:

| Part | What it is | Builds on |
|---|---|---|
| `data_apps` registry | app metadata + desired/observed state | repositories factory (DuckDB + PG pair) |
| `apps-runner` sidecar | the only process with the Docker socket; tiny lifecycle API | new service, modeled on `services/scheduler` |
| Internal git hosting | writable per-app bare repos over HTTP | `app/marketplace_server/git_router.py` plumbing |
| Ingress proxy | `/apps/{slug}/*` streamed proxy with auth/RBAC | `app/api/broker.py` proxy pattern + `require_resource_access` |
| Auto-sleep | idle reaper + wake-on-request | `services/scheduler` job + proxy holding page |

## 4. Registry — `data_apps`

New table (DuckDB `src/db.py` v95→v96 step + Alembic `0043_data_apps_v96.py`),
repo pair `src/repositories/data_apps.py` + `data_apps_pg.py`, factory entry
`data_apps_repo()`, contract test `tests/test_data_apps_repo.py`
(parametrized both backends) — the standard parity set.

```sql
CREATE TABLE data_apps (
    id              TEXT PRIMARY KEY,          -- app_<uuid>
    slug            TEXT UNIQUE NOT NULL,      -- url-safe, immutable after create
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    owner_user_id   TEXT NOT NULL,
    repo_mode       TEXT NOT NULL,             -- 'internal' | 'external'
    repo_url        TEXT DEFAULT '',           -- external mode only
    repo_branch     TEXT DEFAULT 'main',       -- external mode only
    deployed_sha    TEXT DEFAULT '',           -- internal mode: agnes-live target
    runtime_tag     TEXT DEFAULT '',           -- image tag override; '' = instance default
    state           TEXT NOT NULL DEFAULT 'created',
                    -- created|deploying|running|sleeping|stopped|error
    state_detail    TEXT DEFAULT '',           -- last error / progress note
    secrets_enc     TEXT DEFAULT '',           -- encrypted JSON {KEY: value}
    env             TEXT DEFAULT '{}',         -- non-secret env JSON
    cpu_limit       TEXT DEFAULT '',           -- '' = instance default
    mem_limit       TEXT DEFAULT '',
    idle_timeout_s  INTEGER DEFAULT 1800,
    sleep_mode      TEXT DEFAULT 'recreate',   -- 'recreate' | 'pause' (see §7)
    last_request_at TIMESTAMP,
    last_deploy_at  TIMESTAMP,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);
```

Notes:

- `state` is the **observed** state; the desired state is implicit (deploy →
  running; stop → stopped). The runner reconciles and the registry records.
- `secrets_enc` reuses the encryption helper already used for per-user MCP
  secrets (`app/api/mcp_user_secrets.py` path). Secrets are written into the
  generated `config.json` as `dataApp.secrets` (the image exports them as env
  vars, `#`-prefix stripped, uppercased).
- `slug` doubles as the container name suffix (`agnes-dataapp-<slug>`), the
  route prefix (`/apps/<slug>/`), and the internal repo name
  (`<slug>.git`) — one identity everywhere.

## 5. Container lifecycle — the `apps-runner` sidecar

**Problem:** the Agnes app container deliberately has no Docker socket and no
mechanism to launch sibling containers (verified: no socket mount, no docker
SDK usage anywhere in `app/`). Host-side lifecycle is systemd + `docker
compose`, which cannot serve interactive deploys.

**Decision:** a dedicated **`apps-runner`** sidecar service (compose profile
`apps`), running from the Agnes image (`python -m services.apps_runner`),
which is the *only* container with `/var/run/docker.sock` mounted. Rationale
over the alternatives:

- *Socket in the main app container* — rejected: the app container runs
  user-facing request handling and the chat stack; handing it root-equivalent
  host access is the largest possible blast radius.
- *Generalizing the E2B provider* — rejected for v1: E2B is an external paid
  dependency, apps would live off-VM (away from the data and behind another
  network hop), and self-hosted instances without E2B keys would lose the
  feature. The E2B pause/resume model *inspires* the sleep design but does not
  host it.
- *Host-side agent via systemd* — rejected: not available in dev/docker-only
  environments, harder to test, duplicates what a sidecar does.

**Runner API** (HTTP, bound only on the internal compose network, never
published; authenticated with a shared secret `APPS_RUNNER_TOKEN` — same
pattern as `app/auth/scheduler_token.py`):

| Endpoint | Action |
|---|---|
| `POST /apps/{slug}/up` | body: full container spec (image tag, config.json content, limits, networks). Writes `${DATA_DIR}/apps/{slug}/config.json`, then `docker run` (create+start). Idempotent: recreates if spec changed. |
| `POST /apps/{slug}/stop` | body: `{mode: "recreate"|"pause"}` → `docker rm -f` or `docker pause`. |
| `POST /apps/{slug}/resume` | `docker unpause` (pause mode only). |
| `GET /apps/{slug}/status` | container state + health of `:8888` (the wake-readiness signal). |
| `GET /apps/{slug}/logs?tail=N` | container logs — **the AI iteration loop depends on this**. |
| `GET /apps` | reconciliation listing (`agnes-dataapp-*` containers). |

The runner is intentionally dumb: no registry access, no auth logic, no
policy. All decisions (RBAC, quotas, when to sleep) live in the Agnes app; the
runner only translates them into Docker calls. It uses the `docker` Python SDK
(new dependency, runner-only).

**Container spec** the app service generates per deploy:

- image: `data_apps.runtime_image` (+ per-app `runtime_tag` override),
- name `agnes-dataapp-<slug>`, labels `agnes.data-app=<id>` (reconciliation key),
- volume: `${DATA_DIR}/apps/<slug>` → `/data` (config.json; nothing else),
- optional named cache volume `agnes-dataapp-cache-<slug>` → `/home/app/.cache`
  (uv/npm caches survive recreate-sleep → much faster wakes),
- network: dedicated bridge **`agnes-apps`** (see §10),
- no published ports — ingress reaches `agnes-dataapp-<slug>:8888` over the
  shared network,
- `mem_limit`/`cpus` from registry row or instance defaults,
- env: `AGNES_URL=http://app:8000` + `AGNES_APP_ID` + registry `env`;
  `DATA_LOADER_API_URL` **never set**.

## 6. Ingress — routing, auth, RBAC

**Two routing modes, both in v1** (per-instance; per-app override):

- **Path-prefix (default, zero-config):** `/apps/{slug}/{path:path}` on the
  FastAPI app — Caddy already forwards everything to `app:8000`, no Caddy
  changes needed.
- **Subdomain (opt-in):** `{slug}.{data_apps.subdomain_base}` — e.g.
  `sales-dash.apps.agnes.example.com`. Requires the operator to point a
  wildcard DNS record at the VM and enable wildcard TLS (Caddy on-demand TLS
  or a wildcard cert, extending the existing `CADDY_TLS` regimes). Caddy
  matches `*.{subdomain_base}` and forwards to the same FastAPI handler with
  the slug resolved from the `Host` header, so auth/RBAC/wake logic is shared
  — the modes differ only in URL shape. Subdomains eliminate the
  `X-Forwarded-Prefix` burden entirely, which is why WS-heavy frameworks
  (Streamlit, Dash) should prefer them; templates work under both.

Auth note for subdomains: the session cookie must be issued for the parent
domain (`Domain=.agnes.example.com`) when `subdomain_base` is configured, so
one login covers the dashboard and all apps.

The handler (shared by both modes):

1. authenticates the caller (session cookie or PAT — the normal chain),
2. authorizes via `Depends(require_resource_access(ResourceType.DATA_APP, "{slug}"))`,
3. touches `last_request_at` (debounced, ≥30 s between writes; via the
   coordination backend so multi-process deployments don't thrash the DB),
4. if state is `running` → streamed reverse proxy (httpx, request+response
   streaming, WebSocket upgrade passthrough) to
   `http://agnes-dataapp-<slug>:8888/{path}`,
5. if `sleeping`/`stopped` → trigger wake (§7) and return the **holding page**
   (HTML with JS polling `GET /api/data-apps/{slug}/readiness`; auto-redirects
   into the app when ready). `Accept: application/json` callers get a `503`
   with `{"status": "waking", "retry_after": …}` instead — agents and health
   checks must not parse HTML.

The proxy is modeled on the existing in-app streaming proxy
(`app/api/broker.py::anthropic_proxy`): upstream host is never derived from
caller input, hop-by-hop headers stripped, `X-Forwarded-Prefix: /apps/<slug>`
added so frameworks that honor it generate correct URLs.

**RBAC:** new `ResourceType.DATA_APP` member + `ResourceTypeSpec` in
`app/resource_types.py` with `list_blocks=_data_app_blocks` reading
`data_apps_repo().list(...)` (through the factory — the #518 rule). Grants
follow the standard model: `resource_grants(group, 'data_app', slug)`. The
app **owner** and `Admin` always pass; other users need a grant. Apps are
therefore private by default and shared by granting a group — same mental
model as tables and marketplace plugins.

**Subpath contract for app authors:** apps are served under `/apps/<slug>/`.
The nginx conf inside the container receives the *stripped* path (the proxy
forwards `{path}`, not the full URL), so most backends work unmodified; apps
that render absolute URLs must respect `X-Forwarded-Prefix` (all AI-authoring
templates in §9 do this out of the box, and the skill documents it).
`X-Frame-Options: DENY` stays global; app pages are visited directly, not
iframed, in v1.

## 7. Auto-sleep & wake

Two cooperating mechanisms, both required by the "auto-sleep chceme"
requirement:

**Idle reaper** — one new scheduler job (tuple in
`services/scheduler/__main__.py::build_jobs()`):
`("data-app-idle-reaper", "every 5m", "/api/data-apps/reap-idle", "POST", 300)`.
The endpoint (scheduler-token-gated) scans `state='running'` rows where
`now - last_request_at > idle_timeout_s`, and for each: runner `stop` with the
app's `sleep_mode`, state → `sleeping`. The scheduler stays a pure cron clock;
policy lives in the app — consistent with the existing architecture.

**Wake-on-request** — the ingress handler (§6, step 5) sets state →
`deploying` (with a coordination-backend lock so concurrent first requests
trigger exactly one wake), calls runner `up` (recreate mode) or `resume`
(pause mode), and the readiness endpoint polls runner `status` until `:8888`
answers → state `running`.

**Sleep modes** (per-app, default from instance config):

| Mode | Sleep action | Wake path | Trade-off |
|---|---|---|---|
| `recreate` (default) | `docker rm -f` | full bootstrap: clone `agnes-live` → `setup.sh` → start | frees RAM/CPU completely; wake = seconds-to-minutes (cache volume cuts most of `setup.sh`) |
| `pause` | `docker pause` | `docker unpause` | instant wake; holds RAM while asleep — for latency-sensitive apps on roomy VMs |

`recreate` is safe *because* of the `agnes-live` pinning (§2): a wake always
reproduces the deployed commit, and the per-app cache volume keeps `uv
sync`/`npm install` warm. This mirrors upstream behavior (their sleeping apps
also cold-boot through the full entrypoint) while adding the pause option
upstream doesn't have.

Timeout bounds: `idle_timeout_s` clamped to `[300, 86400]` (5 min–24 h),
matching upstream's configurable range.

## 8. Data access from apps

Upstream apps get data via the Data Loader sidecar (input mapping to
`/data/in/`). Agnes replaces this with its own idiom — **apps are API
clients**, exactly like the CLI and MCP surfaces:

- On deploy, the control plane mints an **app service token**: a PAT
  (`app/api/tokens.py` machinery) bound to the **app owner's identity** with
  scope `data-app:<slug>` (for audit attribution). The app's effective data
  access is therefore **the owner's grants, evaluated live per request** —
  the same semantics as the owner running a query themselves. No per-app
  grant configuration is needed. The token lands in the container as
  `AGNES_TOKEN` via `dataApp.secrets`; it is rotated on every deploy and
  revoked on stop/delete.
- Two documented consequences of owner-inherited access (dashboard-publishing
  semantics, deliberate): (a) a viewer granted access to the *app* sees data
  the app fetches under the *owner's* rights, even where their own grants are
  narrower — sharing an app is an act of publication, and the UI says so on
  the grant screen; (b) if the owner loses a grant or is deactivated, the app
  loses that data (deactivation revokes the PAT → app errors until an admin
  reassigns ownership, which re-mints the token).
- Inside the container the app calls the normal REST API (`AGNES_URL` is
  injected): `/api/data/...` for parquet download, `/api/query` for SQL,
  catalog endpoints for discovery. The `agnes` CLI also works if the app's
  `setup.sh` installs it, but plain REST keeps templates dependency-free.
- **Why not mount parquets read-only?** A bind mount would bypass RBAC (one
  mount = whatever the host path holds) and break on `remote`/`server_only`
  tables. Going through the API gives grant-checked access, audit logging,
  and the full query surface for free. If profiling later shows the HTTP hop matters
  for a hot app, a per-app filtered-parquet materialization can be added —
  it's an optimization, not a design change.

This is a deliberate, documented divergence from upstream (`input` section in
`config.json` is ignored/absent on Agnes). The AI-authoring skill teaches the
Agnes idiom.

Owner-inherited access is now the *default* rather than the only mode: the
proxy hands every app a signed viewer identity, and an app switched to
`data_identity=viewer` queries as **owner ∩ viewer** — see
[`2026-09-09-data-app-viewer-identity-and-sharing-design.md`](2026-09-09-data-app-viewer-identity-and-sharing-design.md).

## 9. AI authoring — the primary flow

The requirement: the Agnes chat agent must be able to create these apps with
**perfect alignment** — no manual glue between "agent writes code" and "app
runs". Design principle: *the agent uses the same public control plane as
humans, through the broker-ticket pattern already established for chat*.

### 9.1 Internal git hosting

Every `repo_mode='internal'` app gets a persistent **bare repo** at
`${DATA_DIR}/apps/git/<slug>.git`, served at `/data-apps.git/<slug>/{path}`
by the same `git http-backend` subprocess plumbing as
`app/marketplace_server/git_router.py` — with three deltas the existing code
doesn't need: persistent per-app repos (not the ETag cache), `http.receivepack
= true` (push enabled), and a **write-authorization check** (push allowed for
the app owner, `Admin`, and broker tickets scoped to the app; read for anyone
who passes the app's RBAC gate). Auth stays HTTP Basic with PAT-as-password
(git CLI compatible, same as marketplace.git).

Deploy semantics on top: pushing to the repo does **not** auto-deploy.
`POST /api/data-apps/{slug}/deploy` (optional body `{sha}`) fast-forwards
`agnes-live` to the target SHA and calls runner `up`. This separation is what
makes the agent loop safe — the agent can push intermediate commits freely and
deploy only when ready.

### 9.2 Broker surface for the sandboxed agent

The chat agent runs in an egress-gated E2B sandbox holding only a scoped
ticket, never a real PAT (`app/api/broker.py` pattern). New ticket scope
**`data_apps`**, honored by broker endpoints that replay onto the control
plane under the minted user identity:

| Broker endpoint | Maps to |
|---|---|
| `POST /api/broker/data-apps` | create app (internal repo initialized from a template, §9.3) |
| `POST /api/broker/data-apps/{slug}/push` | tar upload of the working tree → server-side commit to the internal repo (returns SHA). Exists so the agent doesn't need git-over-HTTP credentials in the sandbox; the server commits as the ticket's user. |
| `POST /api/broker/data-apps/{slug}/deploy` | deploy `{sha}` |
| `GET  /api/broker/data-apps/{slug}/status` | registry state + readiness |
| `GET  /api/broker/data-apps/{slug}/logs?tail=N` | container logs |
| `GET  /api/broker/data-apps/{slug}/http-probe?path=/` | server-side GET against the running app (status code + first N bytes) — lets the agent verify its app actually renders without sandbox egress to the app URL |

The sandbox egress allowlist already includes the Agnes host, so no E2B
config change is needed.

### 9.3 Bundled skill + templates

New bundled skill **`agnes-data-apps`** in
`app/initial_workspace_default/.claude/skills/` (flows to every chat session
via the existing `WorkdirManager` copy + `skills_catalog` merge). Contents:

- the app-repo contract (`keboola-config/` — nginx conf, supervisord conf,
  `setup.sh`; port discipline; absolute paths; `uv run` prefix),
- the Agnes-specific parts: `X-Forwarded-Prefix` handling, `AGNES_URL` +
  `AGNES_TOKEN` data access with copy-paste snippets (fetch parquet, run
  query, list catalog),
- the authoring loop: *scaffold from template → push → deploy → poll status →
  http-probe → read logs on failure → fix → repeat*,
- guardrails: never bake secrets into the repo (use the secrets API), keep
  `setup.sh` idempotent, respect the size limits.

Templates live in the skill's `references/templates/` as complete minimal
repos, and `POST /data-apps` accepts `template: <name>` to initialize the
internal repo server-side from the same source (one copy, two consumers):

| Template | Stack | Demonstrates |
|---|---|---|
| `fastapi-dashboard` | Python/FastAPI + htmx | query API → server-rendered table + chart |
| `flask-form` | Python/Flask | form input → write back via API |
| `node-static-spa` | Node build + static serve | pure frontend against Agnes REST |
| `dash-analytics` | Python/Dash | interactive analytics on a pulled parquet |

Every template ships a working `keboola-config/`, honors
`X-Forwarded-Prefix`, and reads `AGNES_URL`/`AGNES_TOKEN` from env — so the
agent's first deploy renders successfully *before* any custom code, which is
what makes the iteration loop converge fast.

### 9.4 Alignment invariants

To keep "AI can build it" true as the platform evolves, these are review-time
invariants (CONTRIBUTING.md sync-map rows):

- Any change to the app-repo contract, ingress path scheme, or injected env
  vars **must** update the `agnes-data-apps` skill + templates in the same PR.
- Every control-plane REST endpoint has CLI + MCP siblings (API-coverage
  ratchet applies) *and* — where agent-relevant — a broker sibling.
- Templates are smoke-tested in CI: scaffold → deploy against a compose stack
  → http-probe 200 (extends the existing e2e harness).

## 10. Security

- **Docker socket confinement:** only `apps-runner` mounts the socket; its API
  is unpublished, token-gated, and semantically narrow (no arbitrary image,
  no volume mounts beyond the fixed per-slug paths, image name allowlisted to
  the configured runtime image). A future hardening step can interpose a
  docker-socket-proxy; the API shape already permits it.
- **Network isolation:** app containers join only the `agnes-apps` bridge.
  The Agnes `app` service is attached to it (so ingress and `AGNES_URL` work);
  Postgres, Redis, and the scheduler are **not**. Apps can reach the internet
  in v1 (parity with upstream; many apps call external APIs); a per-app
  `egress: none` option (internal-only network) is a fast follow.
- **Resource limits:** per-app `mem_limit`/`cpus` with instance defaults
  (`data_apps.default_mem_limit`, default `1g` / `1.0`), plus a per-user app
  quota (`data_apps.max_apps_per_user`, default 3; Admin exempt) enforced at
  create, plus a fork-bomb `pids_limit` (`data_apps.container_pids_limit`,
  default 512).
- **Container hardening:** every data-app container runs `cap_drop: ALL` and
  `no-new-privileges` (both unconditional, no knob) alongside that
  `pids_limit`, mirroring the posture `services/apps_runner/sandbox_api.py`
  already applies to chat sandboxes — never applied to that path, which needs
  broader write access for agent-authored code. A compromised app therefore
  cannot escalate privileges, gains no capabilities Docker doesn't already
  withhold by default, and cannot fork-bomb the host. A read-only root
  filesystem is the one piece **off by default** (`data_apps.container_read_only`):
  it needs a tmpfs allowlist verified against the shipped runtime image, whose
  in-container nginx + supervisord write to `/var/run`, `/var/log` and
  `/var/cache/nginx` — paths the current `_READ_ONLY_TMPFS` list
  (`/tmp` + `/app`, the entrypoint's clone-and-install target) does not cover.
  Enabling it blind would crash-loop every hosted app, so the knob waits on
  someone booting the image with it and extending the list from the real
  failures; `tests/test_data_apps_e2e_docker.py` is where that verification
  belongs.
- **Tokens:** the app service token is a normal PAT — revocable in the
  existing token UI, `sha256`-stored, rotated per deploy, audited. Broker
  tickets are TTL-bound and scope-checked per request.
- **Container-name spoofing on the shared bridge:** the proxy connects by
  container name (`agnes-dataapp-<slug>`); the runner enforces that name and
  the `agnes.data-app` label at create so only registry-driven containers can
  hold it. A malicious *app* cannot register a name — Docker rejects
  duplicates and the runner is the only creator on that network.
- **Untrusted code disclosure:** apps run arbitrary user/AI code. The threat
  model equals the existing E2B sandbox: user-directed code with data access
  scoped to the owner's grants (§8). The additional exposure specific to apps
  is *sharing* — a granted viewer reaches data through the owner's rights —
  which is the documented publication semantics, surfaced in the grant UI.

## 11. Surfaces (REST × CLI × MCP × web)

Per the command-UX standard (scope auto, origin labeled, `--limit`/`--json`;
API-coverage ratchet):

- **REST** `app/api/data_apps.py`: public read router (`GET /api/data-apps`,
  `GET /api/data-apps/{slug}`, `GET …/readiness`) gated by resource access;
  owner/admin mutation router (create, deploy, stop, delete, secrets put,
  logs, `reap-idle` for the scheduler). Included from `app/main.py` next to
  the recipes/memory-domains routers it mirrors.
- **CLI** `cli/commands/data_apps.py`: `agnes app list|show|create|deploy|logs|open|stop|delete`
  (`open` prints/launches the app URL). Hints via `cli/query_hints.py` on
  not-found.
- **MCP** foundation tools: `data_apps_list`, `data_app_get`,
  `data_app_deploy`, `data_app_logs` in `app/api/mcp/foundation_tools.py`
  (guarded by the parity test).
- **Web**: `/apps` list page + detail page (state, URL, logs tail, deploy
  button, grants shortcut to `/admin/access`), templates extending
  `base_page.html`, chrome context spread per the established gotcha. Admin
  sees all; users see granted+owned.

## 12. Configuration

New top-level `instance.yaml` section (sibling of `chat:`), read via
`get_data_apps_config()` → `get_value("data_apps", default={})`:

```yaml
data_apps:
  enabled: false                 # feature flag; runner profile off by default
  runtime_image: "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24"
  subdomain_base: ""             # e.g. "apps.agnes.example.com"; "" = path-prefix only
  default_idle_timeout_s: 1800
  default_sleep_mode: recreate   # recreate | pause
  default_mem_limit: 1g
  default_cpus: 1.0
  max_apps_per_user: 3           # the only creation gate — no admin approval step
```

Compose: `apps-runner` service under profile `apps`; enabling the feature =
flag + profile (documented in DEPLOYMENT.md; the infra module gets the
profile toggle in a follow-up infra PR).

## 13. Testing

- Repo-pair contract test (both backends) + migration ladder test
  (`test_db_schema_version`).
- Runner unit tests with a fake docker client; one docker-marked integration
  test (real `docker run` of the runtime image with a fixture repo → `:8888`
  answers).
- Ingress tests: RBAC matrix (owner/granted/stranger/admin), wake path
  (sleeping → holding page → running), JSON-Accept 503 shape, header
  hygiene, `last_request_at` debounce.
- Git hosting: push-then-clone round-trip over HTTP with PAT; write-auth
  denial for non-owner.
- Sleep: reaper endpoint moves idle apps to sleeping; wake lock admits one
  concurrent waker.
- E2E (compose, existing harness): template scaffold → broker push → deploy
  → http-probe 200 → idle-reap → wake → 200. This test *is* the AI-alignment
  guarantee in CI.

## 14. Rollout

| Wave | Scope | Exit criterion |
|---|---|---|
| **1 — platform core** | registry + migration, apps-runner, internal git hosting, ingress (path-prefix **and** subdomain mode) + RBAC + holding page, deploy/logs, CLI + MCP + web list page, manual stop | human can create an app from a template repo and reach it at `/apps/<slug>/` (and at `<slug>.<subdomain_base>` where configured) behind login |
| **2 — auto-sleep** | idle reaper, wake-on-request, sleep modes, cache volume | idle app releases resources and wakes on visit within the timeout envelope |
| **3 — AI authoring** | broker scope + endpoints, `agnes-data-apps` skill, 4 templates, e2e loop test | chat request "build me a dashboard over table X" produces a running, RBAC-gated app with zero human shell access |
| **4 — polish** | pause mode default eval, egress `none` option, per-app external-repo SHA pinning | — |

Waves 1+2 land together as the first release (auto-sleep is required); wave 3
immediately after — it is the point of the feature.

## 15. Decisions log (2026-07-21 review)

1. **Runtime image distribution — resolved, no action.** The image is already
   anonymously pullable from `keboolapublic.azurecr.io/data-app-python-js`
   (verified: token-less pull flow lists release tags up to `1.6.2` + matrix
   variants). Agnes pins a matrix tag from there; the private source repo is
   not a blocker for anyone.
2. **Routing — both modes from v1.** Path-prefix as the zero-config default,
   subdomain (`{slug}.{subdomain_base}`) as operator opt-in; one shared
   handler, templates work under both (§6). WS-heavy frameworks are steered
   to subdomain mode instead of waiting on a v2 spike.
3. **No admin approval gate.** Creation is gated by `max_apps_per_user` quota
   only; apps are private-by-default via RBAC. Revisit if usage shows abuse.
4. **App data access inherits the owner's grants** (dashboard-publishing
   semantics) — no per-app grant principal, no schema change; consequences
   documented in §8 and surfaced in the grant UI.

## 16. Remaining open questions

1. **Session cookie domain scoping** — enabling `subdomain_base` requires
   issuing the auth cookie with `Domain=.<parent>`; verify this doesn't break
   existing single-host deployments that renew sessions (migration note for
   operators, not a design risk).
2. **WS proxy validation** — httpx WS passthrough vs. a dedicated ASGI
   WebSocket bridge: decide during wave 1 implementation with a Streamlit
   template smoke test under both routing modes.
