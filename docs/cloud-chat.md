# Cloud-hosted Claude Code (`/chat` + Slack)

This page documents the cloud chat surface — what end users see, how
admins enable it, and what to know about cost / isolation.

## What it is

A zero-install web chat at `/chat` and a Slack DM bot, both backed by a
per-session agent sandbox. Users get the full Agnes harness (skills,
marketplace, slash commands, `agnes` CLI, sub-agents) without installing
anything locally.

Two chat providers, one web/Slack surface:

- **`kai-agent`** (default) — sessions run on the **embedded kai-agent turn
  engine** (the same integration `/api/kai/*` hosts, `docs/api-reference.md`).
  The engine owns the agent loop, the conversation transcript and the remote
  execution sandbox; Agnes spawns nothing per session. See
  [kai-agent provider (embedded turn engine)](#kai-agent-provider-embedded-turn-engine).
- **`docker`** — a local container per session on the host's own Docker
  daemon, running the native `claude-agent-sdk` Python runner, with the
  user's workspace bind-mounted. See
  [Docker provider (self-hosted)](#docker-provider-self-hosted).

Both providers own workspace delivery themselves: the docker provider
bind-mounts the user's workspace into the container, and the kai-agent
engine fetches the instance workspace tarball from `GET /api/kai/workspace`.
There is no manager-side workspace upload.

Not sure which to run? See [Choosing a provider](#choosing-a-provider).

## Enabling on an instance

Default is **off**. The provider defaults to `kai-agent`; resolution is
`AGNES_CHAT_PROVIDER` env > `chat.provider` in `instance.yaml` >
`kai-agent`. To enable with the default provider (for the self-hosted
container provider, jump to
[Docker provider (self-hosted)](#docker-provider-self-hosted)):

1. **Deploy the embedded kai-agent engine.** In a normal deployment the
   `customer-instance` Terraform module's engine overlay runs it as the
   `kai-agent` compose service next to Agnes. For local development there
   is a scripted stand-in engine — see
   [`docs/kai-agent-local-dev.md`](kai-agent-local-dev.md).
2. **Edit `${DATA_DIR}/state/instance.yaml`:**

   ```yaml
   chat:
     enabled: true
     provider: kai-agent                      # the default — may be omitted
     # kai_agent_url: "http://kai-agent:3000" # default; override if the
     #                                        # engine lives elsewhere
   ```
3. **Set environment variables on the Agnes server:**

   - `ANTHROPIC_API_KEY` — required; the engine's LLM traffic rides the
     server-side broker (`/api/broker/anthropic`), so the key never leaves
     the Agnes host.
   - `KAI_HOST_JWT_SECRET` — required; the shared secret that turns on the
     `/api/kai/*` host surface and signs the per-session engine JWTs.
   - `JWT_SECRET_KEY` — 32+ bytes; mints session JWTs the in-sandbox
     `agnes` CLI uses to auth back to the Agnes REST API.

4. **Restart the Agnes server.** Watch the log for
   `ChatManager started (provider=kai-agent, engine=...)`.
5. **Visit `/chat` while logged in.**

If any of the gates fail (keys missing, engine URL malformed, provider
backing absent), the manager refuses to start with a fatal log line and
all chat endpoints return 503 `chat_disabled`.

## Removed: the `e2b` provider

The E2B cloud-microVM provider was **removed in 0.89.0**. An instance
whose config still says `provider: e2b` boots with chat disabled and
logs:

> chat.provider=e2b is no longer supported — the E2B provider was removed
> in 0.89.0. Set chat.provider in instance.yaml (or AGNES_CHAT_PROVIDER /
> the customer-instance module's chat_provider field) to 'kai-agent' (the
> embedded turn engine) or 'docker' (self-hosted containers via the
> apps-runner sidecar; see docs/cloud-chat.md), then restart. Chat stays
> disabled until then.

Migration checklist:

- Set `chat.provider` to `kai-agent` or `docker` (or just delete the key —
  `kai-agent` is the default), then restart.
- Remove the E2B-only config keys; they are gone, not deprecated:
  `chat.e2b_template_id`, `chat.egress_allow_out`,
  `chat.e2b_workspace_max_bytes`, `chat.e2b_kill_on_ws_disconnect`. The
  last one no longer implies `on_detach: kill` — a stale config gets the
  `pause` default, with a warn-and-ignore log; set `chat.on_detach: kill`
  explicitly if you want kill-on-disconnect.
- Remove `E2B_API_KEY` from the Agnes server environment — the app no
  longer reads it.

## Host requirements

- **`kai-agent`**: Agnes spawns nothing per session, so the Agnes host
  only needs RAM/CPU for the FastAPI app, ChatManager state, the
  chat_repo and any open WebSockets. The engine sidecar (and whatever
  sandbox infrastructure it manages) is sized and provisioned by the
  deployment, not by Agnes.
- **`docker`**: sandboxes run on the Agnes host's own Docker daemon and
  contend with the gateway for resources. Per-sandbox bounds
  (`chat.docker_mem_limit`, `chat.docker_cpus`, `chat.docker_pids_limit`)
  are always applied, and `chat.docker_max_total_sandboxes` (default 10)
  caps the host-wide total.

**Multi-worker / multi-replica.** ChatManager state must be shared across
processes for chat to run on `UVICORN_WORKERS > 1` or a role-split
topology — that requires `coordination.backend: redis` (see
`docs/DEPLOYMENT.md` → *Multi-replica chat HA*). With the default
in-memory coordination backend the server refuses to enable chat on a
multi-worker topology, with a fatal log line.

## Slack install

1. At api.slack.com/apps → Create New App → From manifest, paste
   `services/slack_bot/manifest.yaml` (replace `YOUR-AGNES-HOST` with
   your server's public hostname).
2. Install to your workspace; copy the Bot User OAuth Token to
   `SLACK_BOT_TOKEN` and the Signing Secret to `SLACK_SIGNING_SECRET`
   in Agnes env.
3. Slack users DM the bot to receive a 6-digit verification code,
   which they paste at `/setup` while logged into Agnes.

### Slash commands

The manifest also registers three slash commands, all pointed at
`https://YOUR-AGNES-HOST/api/slack/commands` (a separate Request URL
from the Events endpoint):

| Command | What it does |
|---|---|
| `/agnes <question>` | Asks Agnes; runs on your persistent DM session, so the answer also appears on web `/chat`. |
| `/agnes-new` | Archives your current Agnes DM session so the next `/agnes` starts fresh. |
| `/agnes-status` | Shows your active session count vs. the per-user cap, plus a `/chat` deep link. |
| `/agnes help` | Lists these commands (answered inline, no async work). |

Each command acks within Slack's 3 s budget and delivers its answer
asynchronously (ephemerally) via the command's `response_url`. Under
Socket Mode the commands arrive over the socket instead of the HTTP
Request URL — no manifest `url:` is needed in that mode.

### Manifest stanzas: HTTP vs Socket Mode

Two transports, two manifest shapes. Pick the one matching your
`chat.slack.transport` setting. Replace `<your-host>` with your public
Agnes hostname.

**HTTP (default).** Slack delivers events, slash commands and interactivity
over HTTPS to your public endpoints:

```yaml
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
    request_url: "https://<your-host>/api/slack/interactivity"
  socket_mode_enabled: false
```

**Socket Mode (optional).** All three event classes arrive over one
WebSocket; no public `request_url` is needed — interactivity routes through
the same `dispatch_interaction`:

```yaml
settings:
  event_subscriptions:
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

## Cost & limits

Per-user defaults (configurable in `/admin/server-config`):

| Setting | Default |
|---|---|
| Concurrent sessions per user | 3 |
| Idle TTL | 30 min |
| Anthropic spend cap | $20 / day |
| Cumulative tokens per session | 200 k |
| Per-tool-call wall clock | 90 s |
| BigQuery scan per session | 20 GiB |
| Sandbox pause after disconnect linger | 60 s (`chat.detach_linger_seconds`) |
| Paused sandbox GC TTL | 7 days (`chat.paused_ttl_seconds`) |
| On-detach policy | `pause` (`chat.on_detach`) |

Token-derived caps (the spend cap and per-session token cap) are metered
on the docker provider only — the kai-agent engine's stream carries no
usage numbers, so engine sessions never trip them (see that provider's
limitations below). Message-rate and concurrency caps apply everywhere.

**Session lifecycle.** What a session holds depends on the provider:
under `docker` it is a per-session container; under `kai-agent` it is a
connection to the engine, which owns the sandbox. When the last WebSocket
disconnects, Agnes waits for any in-flight turn to finish (so the answer
is never lost), then holds the session alive for a linger window
(`chat.detach_linger_seconds`, default 60 s). If no client reconnects
during the linger window, the session is **paused** — `docker pause`
(SIGSTOP, process memory preserved while the daemon lives) under the
docker provider; bookkeeping only under kai-agent, where the engine keeps
the transcript and parks its own sandbox.

When the user reconnects (web or Slack), Agnes **resumes** the paused
session — unpause + reattach under docker, a fresh engine connection by
chat id under kai-agent — and any in-progress turn output is replayed to
the new WebSocket so mid-turn reconnects are seamless.

**Active-time cap.** `chat.max_session_seconds` counts only time the
session is ACTIVE (not the wall-clock including paused intervals), so
pausing does not burn the session's allotted active time.

**Paused-TTL GC.** Sessions that have been paused for longer than
`chat.paused_ttl_seconds` (default 7 days) are garbage-collected by the
reaper: the sandbox (if any) is destroyed and the session row is cleared.
The session history remains in the DB for the user to browse.

**Crash-net for mid-turn kills.** If a session is force-killed while a
turn is in flight (e.g. an admin kill or idle-TTL expiry during streaming),
the partial token output accumulated so far is persisted as an interrupted
assistant message so the conversation history is never silently truncated.

**Kill-on-disconnect.** Set `chat.on_detach: kill` to hard-kill the
sandbox when the last WS disconnects instead of pausing. (The removed
`chat.e2b_kill_on_ws_disconnect` key no longer maps to this — see
[Removed: the `e2b` provider](#removed-the-e2b-provider).)

## Agent harness

The engine driving the in-sandbox session is selected through the
`AgentHarness` seam (`app/chat/harness.py` — the same pattern as the
`SandboxProvider` protocol): `chat.harness` in `instance.yaml`, validated
against `APPROVED_HARNESSES` at boot (unknown explicit values refuse
chat), passed to the runner as `AGNES_HARNESS`, and resolved there
against the runner's own id→loop registry (an *inherited* unknown id —
version-skewed sandbox — degrades to the default with a stderr warning
instead of crashing). `claude-code` (the claude-agent-sdk CLI) is the
only production harness; the platform deliberately leans on its
ecosystem — skills, marketplace re-serving, hooks — so a second adapter
is a when-needed decision, not a roadmap item. The seam exists so that
decision doesn't require an architecture change. (The seam governs the
native sandbox runner, i.e. the `docker` provider; the kai-agent engine
owns its own agent loop.)

### Marketplace plugins in a chat session

A plugin in the user's stack reaches their chat session whole — skills, agents,
slash commands, hooks and MCP servers — not just its skills. Agnes ships the
caller's RBAC-filtered marketplace from the server (same content builder as the
served ZIP an analyst's `agnes refresh-marketplace` downloads, so a sandbox and
a laptop get byte-identical plugins), in one of two shapes:

| Provider | Shape | How |
|---|---|---|
| `docker` | **real plugins** | the marketplace is written into the workspace as a directory (`.claude/agnes-marketplace/`) and the sandbox's own CLI installs from it *offline* — `claude plugin marketplace add <dir>` + `claude plugin install <name>@agnes --scope user` (`app/chat/runner.py::_register_workspace_marketplace`) |
| `kai-agent` | **flattened components** | Agnes never enters that provider's sandbox, and a plugin install writes the CLI's own HOME registry — out of reach. The components ride the workspace tarball as project files instead (`app/api/kai.py`) |

Both shapes deliver every component type. What differs is the invocation token,
because Claude Code namespaces a plugin's components but not a project's —
verified against the CLI's init handshake, not assumed:

| Component | Installed as a plugin | Flattened to project |
|---|---|---|
| skill | `/keboola-cli` | `/keboola-cli` |
| slash command | `/kbl:kbl-ship` | `/kbl-ship` |
| agent (Task tool) | `kbl:kbl-reviewer` | `kbl-reviewer` |
| MCP server | `plugin:kbl:probe-mcp` | `probe-mcp` |

`GET /api/chat/skills` reports the token for the running provider, which is what
keeps the composer's slash menu honest. Two notes on the flattened shape: a
plugin hook whose command needs `${CLAUDE_PLUGIN_ROOT}` is dropped (there is no
installed plugin root to resolve, so shipping it would fail mid-turn instead),
and its MCP servers are added to `enabledMcpjsonServers` — without that
allow-list entry the CLI never spawns a project-scope server, and nobody can
approve one interactively in a headless sandbox.

Delivery is gated by `chat.bootstrap_marketplace` (the
`chat_bootstrap_marketplace` switch), **on** by default. Turning it off also
removes marketplace entries from the slash menu — the menu never offers what
nothing delivers, which is the bug this replaced: it listed skills while nothing
installed them, so picking one answered `Unknown command: /<skill>`. The earlier
attempt ran `agnes refresh-marketplace --bootstrap` *inside* the sandbox to
clone the marketplace, which cannot work from there — the git endpoint is
PAT-gated, the sandbox deliberately holds no PAT, and the in-sandbox relay
routes no marketplace path. Shipping the marketplace as files is what makes the
install offline, and therefore possible.

## Security model

Single-tenant: all users in one Agnes instance trust each other. FS /
process isolation is the provider's sandbox boundary: under `docker` a
hardened per-session container (see
[Isolation](#isolation) in the docker section), under `kai-agent` the
engine's own remote execution sandbox, governed by the engine's policy.

**Egress** is enforced by layers outside the agent's reach. On the docker
provider that is `chat.docker_egress_mode`: `none` joins the sandbox to
an internal Docker network with no route off the host, and `allowlist`
adds the egress-proxy sidecar so only `chat.docker_egress_allow_hosts`
are reachable (see [Egress](#egress) below). On kai-agent, egress policy
belongs to the engine's own sandbox. The bundled PreToolUse hook in the
workspace template (`.claude/hooks/pre_tool_use.py`) additionally refuses
workspace-destructive bash, and marks high-blast-radius commands as
needing user confirmation (`ask`) — those surface as a real approve/deny
card in the chat rather than being silently executed. The hook is
**advisory, defense-in-depth only**: it is fail-open, inspects Bash
alone, and is a workspace file the agent could rewrite. The enforcing
layers above survive its removal.

The full trust model, the controls behind it, and the known limitations
are in [`../SECURITY.md`](../SECURITY.md).

**Approval gate.** Under `permission_mode="bypassPermissions"` the CLI
executes a file-hook `ask` verdict without prompting anyone, so `ask`
rules used to be silently inert in cloud chat. The runner now re-runs
the workspace hook from an SDK in-process PreToolUse hook
(`ApprovalGate` in `app/chat/runner.py`): an `ask` verdict suspends the
tool call, emits an `approval_request` frame (web chat renders an
Allow once / Allow for session / Deny card; co-drive participants may
answer too), and resolves to allow or deny from the user's
`approval_decision`. No answer within `chat.approval_timeout_seconds`
(default 300), a Stop, or the operator kill-switch (`chat.approvals_enabled:
false`, which the manager passes to the sandbox as `AGNES_APPROVALS=off`) all
resolve to deny. Set it in `instance.yaml`, not in the server's own
environment: the sandbox environment is built by the manager and the host's is
not merged in.

The gate is armed on every surface. Whether a given request can be
answered is decided per request, at the manager's fan-out, from the
sinks attached at that moment — the web WebSocket's `GapReplayGate` is
the only sink that both renders the card and carries a decision back
(`supports_approvals`), so a new sink is assumed unable to approve until
it implements both halves. A session with no such sink attached keeps
the request pending for the full timeout, because one can still arrive:
a chat started in Slack and opened through the "Continue on web" deep
link replays the pending card out of `turn_buffer` and can approve it
normally. Slack renders no buttons of its own, so the bridge posts the
command, the reason, and the Continue-on-web button when nobody is
holding the card, and reports the outcome afterwards. The exception is
`Surface.API`: an agent-API session has a `HeadlessSink`/`StreamingSink`
by construction and a program, not a person, on the other end, so its
requests resolve immediately to a deny that explains why rather than
stalling the caller for 300 s.

**Question gate (AskUserQuestion).** The agent's AskUserQuestion tool —
Claude Code's built-in multiple-choice clarifying-question tool — reaches
the runner through the SDK's `can_use_tool` control channel: the tool's
own permission check is unconditionally "ask", which `bypassPermissions`
does *not* swallow (unlike ordinary tools' prompts). With no callback
registered the SDK raised "canUseTool callback is not provided" and the
tool call died, so questions never reached any UI. The runner now
registers a callback (`QuestionGate` in `app/chat/runner.py`): a call
suspends on a `question_request` frame (web chat renders the questions
with their options as an interactive card — multi-select and an
"Other…" free-text answer included; co-drive participants may answer
too), and the user's `question_answer` resolves it. An answer is
returned to the SDK as the tool input's `answers` map — the same shape
the CLI's own interactive UI produces — so the model sees the canonical
"Your questions have been answered" tool result. A dismissal, a Stop, or
no answer within `chat.approval_timeout_seconds` (the same knob as the
approval gate — both bound "how long a tool call may wait on a human")
resolves to a deny whose message tells the agent to continue with its
best judgment. Attendance follows the approval gate's rules exactly:
same `supports_approvals` sink capability, same pending-card replay to a
late-attaching browser, same Slack Continue-on-web nudge, and the same
immediate actionable deny on `Surface.API`.

**Warehouse data is sent to Anthropic by design** — do not store data
the operator does not want Anthropic to process.

## Docker provider (self-hosted)

`chat.provider: docker` runs each session in a **local Docker container**
on the host's own daemon — no cloud sandbox dependency. Everything else
(web chat, Slack, the agent API, artifact harvest, budgets, the reapers)
is unchanged.

### Prerequisites

On a VM built by the `customer-instance` Terraform module, setting
`chat_provider = "docker"` on that instance does **items 1–3** for you: it
mints `APPS_RUNNER_TOKEN`, resolves `DOCKER_GID`, activates the `apps` compose
profile, and builds the sandbox image at boot from the context inside the app
image (`scripts/ops/agnes-chat-sandbox-image.sh`, re-run by the upgrade tick
whenever that context changes). It does **not** enable hosted data apps —
`data_apps_enabled` stays a separate choice. Anywhere else, do items 1–3 by
hand as described below.

**Item 4, the rails URL, is not module-provisioned — read it even on a
module-built VM.** The module pins `SERVER_URL` to the instance's *public*
origin (OAuth redirects, magic links and the MCP issuer resolve from it too)
and writes no `AGNES_INTERNAL_URL`. `SERVER_URL` wins the rails resolution and
the `.env` heredoc is rewritten whole on every boot, so adding
`AGNES_INTERNAL_URL` by hand there neither takes effect nor survives a
reboot. The sandbox consequently reaches Agnes at the public address — on a
domain VM `https://<domain>`, which verifies fine against the public-CA
certificate the bundled Caddy obtains but routes sandbox↔Agnes traffic out
through the proxy and back; on a domain-less VM the pinned
`http://<external-ip>:8000`. Both are refused outright by
`docker_egress_mode: none`, which leaves the sandbox no route off-host. Boot
logs a warning, not a refusal. Serving the rails from the in-network address
on a module-built VM needs a split-horizon module variable that does not exist
yet.

1. **A Docker daemon on the host** that runs the Agnes gateway.
2. **The apps-runner sidecar.** It is the only process that touches
   `/var/run/docker.sock`; the gateway reaches it over a token-gated HTTP API.
   Under Compose: `docker compose --profile apps up -d apps-runner` with
   `APPS_RUNNER_TOKEN` (`openssl rand -hex 32`) and `DOCKER_GID`
   (`stat -c '%g' /var/run/docker.sock`) set in `.env`. On a bare host, run it
   as a plain process: `python -m services.apps_runner` (same env; set
   `APPS_RUNNER_URL` so the gateway can find it).
3. **The sandbox image**, built by the operator:

   ```bash
   docker build -t agnes-chat-sandbox:latest app/initial_workspace_default/docker-sandbox
   ```

   The Agnes release pipeline does not publish it — see that directory's
   README. `CHAT_SANDBOX_IMAGE_PREFIX` (default `agnes-chat-sandbox`) is the
   sidecar's image allowlist; a tag outside it is refused at create time.
4. **A container-reachable rails URL.** The sandbox's only network dependency
   is `{AGNES_SERVER}/api/broker/*`. Set `AGNES_INTERNAL_URL=http://app:8000`
   under Compose, or `http://host.docker.internal:8000` on a bare host (the
   sidecar adds the `host-gateway` mapping for you on Linux). Chat refuses to
   start on a loopback or unset value — inside the sandbox's own network
   namespace `127.0.0.1` is the sandbox, not Agnes.

   Use the plain-HTTP internal URL, not an `https://` public one: the
   in-sandbox relay verifies certificates and has no CA-bundle knob, so a
   private-CA certificate fails every brokered call.

### Configuration

```yaml
chat:
  enabled: true
  provider: docker
  docker_image: "agnes-chat-sandbox:latest"
  docker_network: "agnes-apps"      # must be a network the Agnes app is on
  docker_mem_limit: "2g"
  docker_cpus: 1.0
  docker_pids_limit: 512
  docker_egress_mode: open          # open | none
  docker_max_total_sandboxes: 10
```

Server env: `ANTHROPIC_API_KEY`, `JWT_SECRET_KEY` and `APPS_RUNNER_TOKEN`.
Watch the startup log for
`ChatManager started (provider=docker, image=…, egress=…)`, and use
*Test connections* in `/admin/server-config` for a live daemon + image probe.

### Workspace: bind-mounted, and durable

The per-session directory is bind-mounted as `/work`, and the user's workspace
is mounted at the same absolute path the server sees, so the session's
symlinks (`.claude`, `CLAUDE.md`, `snapshots`, …) resolve natively. Consequences,
all deliberate:

- **No size cap and no per-spawn upload** — spawn latency does not scale
  with workspace size.
- **Files the agent writes persist on the host** — they stay in
  `${DATA_DIR}/users/<email>/workspace`, including agent-created
  `node_modules` / `.venv` directories.
- **Concurrent sessions of the same user share that workspace**
  (`chat.concurrency_per_user`, default 3). Agent-profile sessions do NOT get
  the shared workspace mount at all: their `.claude`/`CLAUDE.md` are private
  copies, and mounting the workspace root would hand the profiled agent the
  shared originals anyway — so only the targets of the session's data
  symlinks are mounted (`snapshots` read-write, `scripts`/`scaffolds`/
  `CLAUDE.local.md` read-only), preserving the isolation a
  session-dir-only delivery would provide structurally.
- **Co-drive sessions mount only their ephemeral directory** — no personal
  workspace is mounted at all, so "never persist back" holds structurally.

### Pause / resume

- **Pause is `docker pause` (SIGSTOP), not a memory snapshot.** The
  container's process memory survives while the Docker daemon lives; it
  does **not** survive a daemon restart or host reboot. A paused container
  also keeps its memory reservation.
- **Resume is unpause + reattach** — the daemon refuses attach on a paused
  container, so the order is forced. On any resume failure the manager
  falls back to a fresh sandbox seeded with restore-context.
- **`chat.max_session_seconds` applies as configured** (default 4 h).

After a host reboot, the next attach to a paused session produces a *fresh*
sandbox seeded with the restored-conversation transcript — the same path a
crash respawn takes. No crash loop, no stuck session; the in-flight turn (if
any) is lost.

**Reattach gaps (v1, accepted).** Three bounded windows exist where runner
output reaches only the container log, not the gateway:

- *Gateway restart while the container keeps running.* The post-restart resume
  reattaches without replay — there is no offset-tracking in the attach API,
  so replaying would re-deliver every frame since session start. Whatever the
  runner emitted while no gateway was attached (typically the tail of a turn
  that was in flight when the gateway died) is not delivered or persisted.
- *Detach → pause.* `pause()` closes the attach before the daemon pauses the
  container, so a frame emitted in that sub-second window is likewise only in
  the container log.
- *Unpause → reattach.* The mirror image on wake-up. The order cannot be
  inverted: the daemon refuses `attach` on a paused container (409, "unpause
  the container before attach"), so the attach necessarily opens a beat after
  execution resumes. An idle runner emits nothing spontaneously — the window
  only matters for a session that was paused mid-turn.

In all cases the session self-heals on the next message — the runner is idle
and answers normally; what's lost is the rendering of the missed frames, not
agent state. If this ever bites in practice, the known follow-up is
replay-with-dedup (tail the container log with `since=` and drop
already-delivered frames).

### Egress

| Mode | Behavior |
|---|---|
| `open` (default) | normal bridge — the sandbox can reach the internet, so in-sandbox `pip install` / `npm install` work. |
| `none` | the sandbox joins an `internal` Docker network (`<docker_network>-internal`) with no route off the host. The Agnes app must also be attached to that network for the rails to work, and in-sandbox package installs stop working. |
| `allowlist` | the `none` internal network **plus** the `services/egress_proxy` sidecar dual-homed onto it (compose profile `chat-docker-egress`). Sandboxes get `HTTP(S)_PROXY` pointed at the proxy and may reach exactly `chat.docker_egress_allow_hosts` (exact names or `*.suffix` wildcards) — each connection is re-checked **after DNS resolution** against link-local/metadata/private ranges and connects to the vetted address, closing the DNS-rebinding gap; cloud metadata endpoints stay blocked even if listed. The proxy env is cooperative, but ignoring it is not a bypass: the internal network has no other route out. **Requires the rails URL to be internally reachable** — the sandbox's `NO_PROXY` carries whatever host `AGNES_SERVER` resolves to, so a public `SERVER_URL` would be forced onto a direct connection the no-route-out network cannot make. Use `AGNES_INTERNAL_URL` (e.g. `http://app:8000` under compose), as the rest of this page already instructs. |

To enable `allowlist` mode under Compose: set `chat.docker_egress_mode:
allowlist` + `chat.docker_egress_allow_hosts` in `instance.yaml`, export
`EGRESS_ALLOW_HOSTS` (the same list, comma-separated — the compose-owned
proxy env is the enforcing copy), and start the stack with
`--profile chat-docker-egress`. The `app` service already joins
`agnes-apps-internal`, so the rails URL
(`AGNES_INTERNAL_URL=http://app:8000`) keeps working — the provider adds
it to `NO_PROXY` automatically.

The in-workspace PreToolUse hook still applies in all modes as
defense-in-depth (advisory only — see the Security model above).

### Isolation

`runner.py` runs the agent with `permission_mode=bypassPermissions`, justified
by the sandbox boundary — and a container is a weaker boundary than a microVM.
The compensating controls, all applied by the sidecar on every create: non-root
user, `cap_drop: ALL`, `no-new-privileges`, a pids limit, memory + CPU limits,
exactly two bind mounts (never an operator-supplied path), no Docker socket
inside the sandbox, an image-prefix allowlist, and a container-name confinement
(`agnes-chatsbx-*`) that keeps this API away from every other container on the
host. No secret enters the container's environment: the Anthropic key stays
server-side behind the broker, and per-session tickets arrive over stdin.

### Limitations

- **Single-gateway only.** Cross-gateway takeover assumes any gateway can
  destroy any sandbox, which is false when each host runs its own daemon.
  Multi-gateway (`mtier`) with the docker provider is unsupported in v1.
- **Sandboxes contend with the gateway for host resources.**
  `chat.docker_max_total_sandboxes` (default 10) is the host-wide ceiling on top
  of `chat.concurrency_per_user`; a spawn past it fails rather than
  oversubscribing the host.
- Leftover containers from a crashed gateway are reconciled by ownership label
  at gateway start and on the reaper tick.

## kai-agent provider (embedded turn engine)

`chat.provider: kai-agent` — the default — routes every web/Slack chat session
through the **embedded kai-agent turn engine** — the integration whose host
wiring lives at `/api/kai/*` (sessions, per-turn broker tickets, the LLM
broker, the workspace tarball; see `docs/api-reference.md`). The engine owns
the agent loop, the conversation transcript (its own Postgres) and the remote
execution sandbox, so Agnes spawns nothing per session: the provider
(`app/chat/kai_engine_provider.py`) translates the engine's SSE turn stream
into the same runner frame protocol the native provider speaks, and the web
client renders it over the existing WebSocket with no frontend changes —
history, mid-turn reconnect replay, message-rate limits and the per-user
concurrency cap all behave as with the native provider. (Token-derived caps
do not — see the limitations below.)

```yaml
chat:
  enabled: true
  provider: kai-agent
  kai_agent_url: "http://kai-agent:3000"   # default; the engine's compose service
```

The default needs no override in a normal deployment: `kai-agent` is the
service name the `customer-instance` module's engine overlay uses. Override it
with `AGNES_CHAT_KAI_AGENT_URL` (env > `instance.yaml` > default) when the
engine lives somewhere else — including local development, where the stand-in
engine runs as `kai-agent-stub` (see
[`docs/kai-agent-local-dev.md`](kai-agent-local-dev.md)).

Requirements and semantics:

- **`KAI_HOST_JWT_SECRET` must be set** (the same shared secret that turns on
  `/api/kai/*`). The boot gate refuses the manager without it. Each session
  authenticates to the engine with a host-minted session JWT
  (`mint_engine_session_token`, the `POST /api/kai/sessions` claim contract),
  re-minted automatically as it nears expiry.
- **The engine's own env decides the rest** — LLM provider/upstream, its
  sandbox infrastructure and credentials, `HOST_BROKER_MCP_URL` for the
  instance's MCP tool surface (paired with `kai.broker_mcp_enabled`, see
  `docs/feature-flags.md`). `ANTHROPIC_API_KEY` on the Agnes server is still
  required: the engine's LLM traffic rides `/api/broker/anthropic`.
- **Session ids are UUIDs.** The engine stores the chat id in a uuid column,
  so engine-backed sessions are created with uuid ids (both the web path and
  the Slack producer path). A conversation created under the docker provider
  (`chat_<hex>` id) cannot be resumed after switching the provider —
  reopening it answers every message with a clear error card; users start a
  new conversation.
- **Tool approvals round-trip.** The engine raises approval-requiring tool
  calls as events; they render as the normal web approval card, and the
  decision is delivered to the engine's approval endpoint. `allow_session`
  collapses to a plain allow (the engine has no per-session grant), and
  `chat.approvals_enabled: false` auto-denies each request instantly — the
  same kill-switch semantics as the native gate.
- **Pause/resume is bookkeeping only.** There is no Agnes-side sandbox to
  snapshot; a paused session simply drops its engine connection and a resume
  re-attaches by chat id — the transcript and agent state live in the engine.
- **Not per-conversation.** The provider is an instance-level choice; native
  sandbox chat and engine chat do not run side by side on one instance.
- **Pin it in infrastructure, not by hand.** `AGNES_CHAT_PROVIDER` in the
  server env overrides `chat.provider` in `instance.yaml` (env > yaml >
  `kai-agent` default, the standard precedence). The `customer-instance`
  module's per-VM `chat_provider` field writes it, so the choice lives in
  code-reviewed Terraform and survives a fresh data disk — a hand-edited
  `instance.yaml` overlay survives reboots and VM recreates but not that,
  and is invisible in review.

Limitations specific to this provider (each also noted in the module
docstring):

- **Token-derived caps are not metered.** The engine's stream carries no
  usage numbers, so `chat.daily_anthropic_spend_usd` and
  `chat.max_session_tokens` never trip on engine sessions, and the admin
  spend view reads zero for them. Message-rate (`rate_messages_per_hour`)
  and per-user concurrency caps still apply. Cost control belongs on the
  engine's own limits and the LLM broker.
- **Per-session personas do not reach the engine.** The engine's workspace
  comes from `GET /api/kai/workspace` (the instance-wide template), so agent
  profiles, agent memories and the co-drive grant-intersection workspace are
  not materialized into engine turns. The two narrowed session kinds fail
  **closed**, not open: `POST /api/kai/mcp` answers a co-session or a
  scope-limited agent `403` (`mcp_not_available_to_co_session` /
  `mcp_not_available_to_scoped_agent`) rather than resolving it to the owner,
  and `GET /api/kai/workspace` ships the unfiltered bundled `CLAUDE.md`
  instead of the owner's RBAC-filtered Workspace Prompt. So a collaborator's
  engine turn reaches **no** Agnes tool surface — it cannot inherit the
  owner's wider authority. Co-drive on this provider is therefore a
  conversation without host data access, not an unscoped one.
- **`chat.per_tool_call_seconds` and `chat.tool_calls_per_turn_budget` are
  inert** — the engine enforces its own tool policies.
- **Single-gateway deployments only** (like the docker provider): the
  cross-gateway takeover path assumes a destroyable remote sandbox, which
  this provider does not have.

## Choosing a provider

Both providers serve the same web/Slack surface; the trade is where the
agent loop and the sandbox live.

| | `kai-agent` (default) | `docker` |
|---|---|---|
| Agent loop | the embedded engine's own | native `claude-agent-sdk` runner in the sandbox |
| Spawned per session | nothing — Agnes holds a connection | one container on the host daemon |
| Workspace delivery | engine fetches the instance-wide template tarball (`GET /api/kai/workspace`) | user's own workspace bind-mounted, writes persist |
| Agent profiles / memories / co-drive workspaces | not materialized into engine turns (fail closed — see limitations) | fully supported |
| Token spend metering (`daily_anthropic_spend_usd`, `max_session_tokens`) | not metered | metered |
| Per-tool knobs (`per_tool_call_seconds`, `tool_calls_per_turn_budget`) | inert (engine's own policies) | enforced |
| Pause | bookkeeping (engine keeps transcript + sandbox) | `docker pause`; lost on daemon restart/reboot |
| Egress control | the engine's own sandbox policy | `docker_egress_mode: open / none / allowlist` |
| Host prerequisites | engine sidecar + `KAI_HOST_JWT_SECRET` | Docker daemon + apps-runner sidecar + operator-built image |
| Marketplace delivery | flattened project components | real Claude Code plugins |
| Multi-gateway | single-gateway only | single-gateway only |

Rules of thumb: run the default `kai-agent` when the deployment already
provisions the engine (the `customer-instance` module does) and you want
nothing spawned on the Agnes host per session. Run `docker` when you need
the native runner's full feature surface — per-user durable workspaces,
agent profiles/memories, Agnes-side token metering, operator-controlled
egress — and are prepared to operate the daemon, sidecar and sandbox
image yourself.

## Operator setup details

### Keep the sandbox image fresh (docker provider)

**A stale image silently loses features, it does not fail.** The sandbox
image ships `matplotlib` so the agent can draw a chart; an image built
before that was added may have no way to obtain it at runtime
(`docker_egress_mode: none`, or an allowlist without PyPI, puts package
installs out of reach), so the agent falls back to prose or a markdown
table instead of a chart, with nothing in the logs to say why. After
upgrading Agnes, rebuild the image —
`docker build -t agnes-chat-sandbox:latest app/initial_workspace_default/docker-sandbox`
— and confirm the contract label reads `2`:
`docker inspect -f '{{ index .Config.Labels "agnes.chat-sandbox.contract" }}'`.

### Per-user workspace size

Workspaces live on the Agnes host at
`${DATA_DIR}/users/<email>/workspace`. Under the docker provider the
workspace is bind-mounted — no size cap and no per-spawn upload. Under
kai-agent, per-user workspace files do not reach the engine at all: it
materializes the instance-wide workspace template served by
`GET /api/kai/workspace`.

## Known limitations (v1)

- No cloud↔local workspace sync. A user with local Claude Code and
  cloud chat has two independent workspaces.
- Slack: DM only. Channel `@agnes` mentions land in a follow-up PR.
- Multi-worker / multi-replica topologies require the redis coordination
  backend (see § Host requirements); without it chat refuses to start on
  `UVICORN_WORKERS > 1`.
- **Bundled workspace ships no sub-agents.** `app/initial_workspace_default/.claude/agents/` is empty. Sub-agent dispatch (Task tool) requires the operator to install marketplace plugins that ship `agnes-*.md` agent definitions; without them the chat agent will answer directly without sub-agent delegation. The E2E test `tests/e2e/test_sub_agent_dispatch.py::F.9` auto-skips when no agents are present in the workspace.
- **Startup gates refuse chat with a clear log line on any missing prerequisite.** `ANTHROPIC_API_KEY` + `JWT_SECRET_KEY` always; `KAI_HOST_JWT_SECRET` + a well-formed `chat.kai_agent_url` under `provider: kai-agent`; a non-loopback rails URL + a reachable sidecar with the sandbox image present under `provider: docker`.
- **A "Continue on web" click that lands on another replica loses the pending approval.** `attach()` resolves a session owned by a different gateway through a claim-then-respawn takeover, and a fresh runner has no memory of the suspended tool call — the old sandbox's gate dies with it. The user sees the turn restart rather than the card. Single-replica deployments are unaffected; on a multi-replica one, answer from a browser attached to the owning gateway (or just re-ask).
- **Slash-command sessions get no approval nudge of their own.** `EphemeralCommandSink` posts to a `response_url` and carries no `chat_id`/`web_base` to build a deep link from, so it stays silent on `approval_request`. In practice those sessions also carry a `SlackSinkBridge`, which does post the nudge.
- **The approval gate matches `Bash` tool calls only.** The bundled workspace hook returns `allow` for every non-Bash tool, so no policy is lost as shipped. An operator override that adds `ask` rules for `Write`/`Edit`/`WebFetch` would find them inert in cloud chat until the SDK hook matcher (`app/chat/runner.py`) is widened past `Bash` — a deliberate scope choice (gating every `Read`/`Write` through a per-call file-hook subprocess adds real latency).
- **`audit_log.user_id` for chat rows holds the user email, not the user UUID.** Joining `audit_log` to `users` for chat events requires `audit_log.user_id = users.email` for `action LIKE 'chat.%'` and the usual `audit_log.user_id = users.id` for everything else. Documented in `app/chat/audit.py::write_audit`.
- **`_real_agent_loop` enforces a turn-level wall-clock cap, not per-tool.** `claude-agent-sdk` 0.2.x doesn't expose per-tool dispatch hooks; the runner enforces `tool_calls_per_turn_budget` and a turn-level timeout instead of per-tool granularity. Revisit when the SDK ships per-tool hooks.
- **No runtime failover between providers.** The provider is a restart-scoped
  configuration choice, not a hot standby: if the configured provider's
  backing (the engine sidecar, or the Docker daemon/sidecar) is down, chat
  returns 503 until it recovers.
