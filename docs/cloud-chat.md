# Cloud-hosted Claude Code (`/chat` + Slack)

This page documents the cloud chat surface — what end users see, how
admins enable it, and what to know about cost / isolation.

## What it is

A zero-install web chat at `/chat` and a Slack DM bot, both backed by
the same `claude-agent-sdk` Python runner spawned inside a per-session
sandbox. Users get the full Agnes harness (skills, marketplace, slash
commands, `agnes` CLI, sub-agents) without installing anything locally.

Three sandbox providers, one web/Slack surface:

- **`e2b`** (default) — an ephemeral cloud microVM per session, with the
  per-user workspace synced in at spawn time. Needs an E2B account.
- **`docker`** — a local container per session on the host's own Docker
  daemon, with the workspace bind-mounted instead of uploaded. No cloud
  dependency. See [Docker provider (self-hosted)](#docker-provider-self-hosted).
- **`kai-agent`** — sessions run on the **embedded kai-agent turn engine**
  (the same integration `/api/kai/*` hosts, `docs/api-reference.md`) instead
  of the native `claude-agent-sdk` runner. See
  [kai-agent provider (embedded turn engine)](#kai-agent-provider-embedded-turn-engine).

Everything below describes the E2B setup unless a section says otherwise.

## Enabling on an instance

Default is **off**. To enable with the E2B provider (for the self-hosted
container provider, jump to
[Docker provider (self-hosted)](#docker-provider-self-hosted)):

1. **Obtain an E2B account + API key.** E2B is the cloud microVM
   provider — sign up at https://e2b.dev, copy the API key from the
   dashboard.
2. **Build the chat sandbox template.** Run `e2b auth login` then `e2b
   template build` inside
   `app/initial_workspace_default/e2b-template/` (see that directory's
   README). The returned template id goes into `instance.yaml`.
3. **Edit `${DATA_DIR}/state/instance.yaml`:**

   ```yaml
   chat:
     enabled: true
     provider: e2b
     e2b_template_id: "agnes-chat"        # from step 2
     e2b_workspace_max_bytes: 104857600    # 100 MB (default)
   ```
4. **Set environment variables on the Agnes server:**

   - `ANTHROPIC_API_KEY` — required; the in-sandbox runner calls
     Anthropic directly via this key.
   - `E2B_API_KEY` — required; Agnes mints sandboxes via the E2B SDK
     using this key.
   - `JWT_SECRET_KEY` — 32+ bytes; mints session JWTs the in-sandbox
     `agnes` CLI uses to auth back to the Agnes REST API.

5. **Restart the Agnes server.** Watch the log for
   `ChatManager started (provider=e2b, template=...)`.
6. **Visit `/chat` while logged in.**

If any of the gates fail (API keys missing, template id missing,
`UVICORN_WORKERS > 1`), the manager refuses to start with a fatal log
line and all chat endpoints return 503 `chat_disabled`.

## Host requirements

Because the sandboxed runner now lives in E2B's cloud, the Agnes host
itself only needs RAM/CPU for the FastAPI app, ChatManager state, the
chat_repo (DuckDB) and any open WebSockets. A 2 GB / 1 vCPU box is
plenty for a small team. Per-sandbox compute floors (CPU/memory) are
set in `app/initial_workspace_default/e2b-template/e2b.toml` and billed
in the operator's E2B dashboard.

**Single-worker constraint.** ChatManager state is still in-memory; the
server refuses to enable chat if `UVICORN_WORKERS > 1`. HA support
(manager state in DuckDB/Redis) is a follow-up spec.

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
| Workspace push cap | 100 MB |
| Sandbox pause after disconnect linger | 60 s (`chat.detach_linger_seconds`) |
| Paused sandbox GC TTL | 7 days (`chat.paused_ttl_seconds`) |
| On-detach policy | `pause` (`chat.on_detach`) |

**Session lifecycle.** Each chat session spawns a fresh E2B microVM.
When the last WebSocket disconnects, Agnes waits for any in-flight turn
to finish (so the answer is never lost), then holds the sandbox alive for
a linger window (`chat.detach_linger_seconds`, default 60 s). If no
client reconnects during the linger window, the sandbox is
**paused** — the E2B microVM takes a memory snapshot preserving the
running Claude Code process and its full agent context. The sandbox
billingmeter stops while paused.

When the user reconnects (web or Slack), Agnes **resumes** the paused
sandbox: the same process reattaches with its in-memory context intact,
and any in-progress turn output is replayed to the new WebSocket so
mid-turn reconnects are seamless.

**Active-time cap.** `chat.max_session_seconds` counts only time the
session is ACTIVE (not the wall-clock including paused intervals), so
pausing does not burn the session's allotted active time.

**Paused-TTL GC.** Sandboxes that have been paused for longer than
`chat.paused_ttl_seconds` (default 7 days) are garbage-collected
by the reaper: the E2B sandbox is destroyed and the session row is
cleared. The session history remains in the DB for the user to browse.

**Keepalive heartbeat.** While sinks are attached the manager sends a
periodic keepalive to the E2B sandbox so its external timeout always
exceeds the in-process idle-TTL horizon. The `lifecycle on_timeout=pause`
flag on every sandbox acts as a crash net — if the heartbeat misses, the
sandbox pauses rather than dies.

**Crash-net for mid-turn kills.** If a session is force-killed while a
turn is in flight (e.g. an admin kill or idle-TTL expiry during streaming),
the partial token output accumulated so far is persisted as an interrupted
assistant message so the conversation history is never silently truncated.

**Legacy kill-on-disconnect.** Set `chat.on_detach: kill` to restore the
pre-pause behavior (sandbox is hard-killed when the last WS disconnects).
The old `chat.e2b_kill_on_ws_disconnect` key still maps to this but is
deprecated — use `on_detach: kill` instead.

Operators monitor sandbox cost in the E2B dashboard — Agnes does not yet
surface per-session cost in its admin UI.

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
decision doesn't require an architecture change.

## Security model

Single-tenant: all users in one Agnes instance trust each other. The
E2B microVM bounds FS / process / kernel isolation. **Egress is enforced
at the VM level**: `E2BProvider.spawn` passes
`network={"allow_out": …, "deny_out": [ALL_TRAFFIC]}`, so anything
outside `chat.egress_allow_out` is blocked by the platform, outside the
sandbox's reach. The bundled PreToolUse hook in the workspace template
(`.claude/hooks/pre_tool_use.py`) additionally refuses
workspace-destructive bash, and marks high-blast-radius commands as
needing user confirmation (`ask`) — those now surface as a real
approve/deny card in the chat rather than being silently executed. The
hook is defense-in-depth only: it is fail-open, inspects Bash alone, and
is a workspace file the agent could rewrite. The VM-level deny-list
survives its removal. (This supersedes the original Q4 decision, which
shipped the allowlist in the hook alone.)

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
instead of an E2B microVM — no E2B account, no cloud dependency. Everything
else (web chat, Slack, the agent API, artifact harvest, budgets, the reapers)
is unchanged.

### Prerequisites

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
**No `E2B_API_KEY`.** Watch the startup log for
`ChatManager started (provider=docker, image=…, egress=…)`, and use
*Test connections* in `/admin/server-config` for a live daemon + image probe.

### Workspace: bind-mounted, and durable

The per-session directory is bind-mounted as `/work`, and the user's workspace
is mounted at the same absolute path the server sees, so the session's
symlinks (`.claude`, `CLAUDE.md`, `snapshots`, …) resolve natively. Consequences,
all deliberate:

- **No 100 MB cap and no per-spawn upload** — `chat.e2b_workspace_max_bytes` is
  inert under this provider, and spawn latency does not scale with workspace size.
- **Files the agent writes persist on the host.** Under E2B everything written
  in `/work` dies with the sandbox; here it stays in
  `${DATA_DIR}/users/<email>/workspace`. That includes agent-created
  `node_modules` / `.venv` directories, which the upload path used to filter out.
- **Concurrent sessions of the same user share that workspace**
  (`chat.concurrency_per_user`, default 3). Agent-profile sessions do NOT get
  the shared workspace mount at all: their `.claude`/`CLAUDE.md` are private
  copies, and mounting the workspace root would hand the profiled agent the
  shared originals anyway — so only the targets of the session's data
  symlinks are mounted (`snapshots` read-write, `scripts`/`scaffolds`/
  `CLAUDE.local.md` read-only), preserving the isolation E2B provided
  structurally by uploading nothing but the session dir.
- **Co-drive sessions mount only their ephemeral directory** — no personal
  workspace is mounted at all, so "never persist back" holds structurally.

### Pause / resume vs E2B — honest comparison

| | E2B | Docker |
|---|---|---|
| Pause | microVM memory snapshot | `docker pause` (SIGSTOP) |
| Survives a paused sandbox's process memory | yes | yes, while the daemon lives |
| Survives a daemon restart / host reboot | yes | **no** |
| Resume | reattach to the same process | unpause + reattach (the daemon refuses attach on a paused container, so the order is forced) |
| On resume failure | fresh sandbox + restore-context | same |
| Cost while paused | billing stops | container keeps its memory reservation |
| Session lifetime cap | clamped to E2B's 1 h platform max | `chat.max_session_seconds` applies as configured (default 4 h) |

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
  A gateway restart drops E2B's SDK callbacks the same way; this is not a
  regression against the E2B provider.
- *Detach → pause.* `pause()` closes the attach before the daemon pauses the
  container, so a frame emitted in that sub-second window is likewise only in
  the container log.
- *Unpause → reattach.* The mirror image on wake-up. The order cannot be
  inverted: the daemon refuses `attach` on a paused container (409, "unpause
  the container before attach"), so the attach necessarily opens a beat after
  execution resumes. An idle runner emits nothing spontaneously — the window
  only matters for a session that was paused mid-turn.

In both cases the session self-heals on the next message — the runner is idle
and answers normally; what's lost is the rendering of the missed frames, not
agent state. If this ever bites in practice, the known follow-up is
replay-with-dedup (tail the container log with `since=` and drop
already-delivered frames).

### Egress

| Mode | Behavior |
|---|---|
| `open` (default) | normal bridge — the sandbox can reach the internet, so in-sandbox `pip install` / `npm install` work. Weaker than E2B's per-hostname allowlist. |
| `none` | the sandbox joins an `internal` Docker network (`<docker_network>-internal`) with no route off the host. Stronger than E2B — but the Agnes app must also be attached to that network for the rails to work, and in-sandbox package installs stop working. |
| `allowlist` | the `none` internal network **plus** the `services/egress_proxy` sidecar dual-homed onto it (compose profile `chat-docker-egress`). Sandboxes get `HTTP(S)_PROXY` pointed at the proxy and may reach exactly `chat.docker_egress_allow_hosts` (exact names or `*.suffix` wildcards) — each connection is re-checked **after DNS resolution** against link-local/metadata/private ranges and connects to the vetted address, closing the DNS-rebinding gap; cloud metadata endpoints stay blocked even if listed. The proxy env is cooperative, but ignoring it is not a bypass: the internal network has no other route out. E2B `allow_out` parity, with rebinding protection E2B doesn't have. **Requires the rails URL to be internally reachable** — the sandbox's `NO_PROXY` carries whatever host `AGNES_SERVER` resolves to, so a public `SERVER_URL` would be forced onto a direct connection the no-route-out network cannot make. Use `AGNES_INTERNAL_URL` (e.g. `http://app:8000` under compose), as the rest of this page already instructs. |

To enable `allowlist` mode under Compose: set `chat.docker_egress_mode:
allowlist` + `chat.docker_egress_allow_hosts` in `instance.yaml`, export
`EGRESS_ALLOW_HOSTS` (the same list, comma-separated — the proxy is the
enforcing copy), and start the stack with `--profile chat-docker-egress`.
The `app` service already joins `agnes-apps-internal`, so the rails URL
(`AGNES_INTERNAL_URL=http://app:8000`) keeps working — the provider adds
it to `NO_PROXY` automatically.

**The E2B key `chat.egress_allow_out` remains E2B-only.** The in-workspace
PreToolUse hook still applies in all modes as defense-in-depth.

### Isolation

`runner.py` runs the agent with `permission_mode=bypassPermissions`, justified
by the sandbox boundary — and a container is a weaker boundary than a microVM.
The compensating controls, all applied by the sidecar on every create: non-root
user, `cap_drop: ALL`, `no-new-privileges`, a pids limit, memory + CPU limits,
exactly two bind mounts (never an operator-supplied path), no Docker socket
inside the sandbox, an image-prefix allowlist, and a container-name confinement
(`agnes-chatsbx-*`) that keeps this API away from every other container on the
host. As with E2B, no secret enters the container's environment: the Anthropic
key stays server-side behind the broker, and per-session tickets arrive over
stdin.

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

`chat.provider: kai-agent` routes every web/Slack chat session through the
**embedded kai-agent turn engine** — the integration whose host wiring lives at
`/api/kai/*` (sessions, per-turn broker tickets, the LLM broker, the workspace
tarball; see `docs/api-reference.md`). The engine owns the agent loop, the
conversation transcript (its own Postgres) and the remote execution sandbox,
so Agnes spawns nothing per session: the provider
(`app/chat/kai_engine_provider.py`) translates the engine's SSE turn stream
into the same runner frame protocol the native providers speak, and the web
client renders it over the existing WebSocket with no frontend changes —
history, mid-turn reconnect replay, message-rate limits and the per-user
concurrency cap all behave as with the native providers. (Token-derived caps
do not — see the limitations below.)

```yaml
chat:
  enabled: true
  provider: kai-agent
  kai_agent_url: "http://kai-agent:3000"   # default; the engine's compose service
```

Requirements and semantics:

- **`KAI_HOST_JWT_SECRET` must be set** (the same shared secret that turns on
  `/api/kai/*`). The boot gate refuses the manager without it. Each session
  authenticates to the engine with a host-minted session JWT
  (`mint_engine_session_token`, the `POST /api/kai/sessions` claim contract),
  re-minted automatically as it nears expiry.
- **The engine's own env decides the rest** — LLM provider/upstream, E2B key,
  `HOST_BROKER_MCP_URL` for the instance's MCP tool surface (paired with
  `kai.broker_mcp_enabled`, see `docs/feature-flags.md`). `ANTHROPIC_API_KEY`
  on the Agnes server is still required: the engine's LLM traffic rides
  `/api/broker/anthropic`.
- **Session ids are UUIDs.** The engine stores the chat id in a uuid column,
  so engine-backed sessions are created with uuid ids (both the web path and
  the Slack producer path). A conversation created under a native provider
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
  `e2b` default, the standard precedence). The `customer-instance` module's
  per-VM `chat_provider` field writes it, so the choice lives in
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

## Operator setup details

### `agnes-chat:latest` is a mutable tag

Per Q2 the E2B template uses the mutable `:latest` tag rather than per-
release content hashes. Any teammate with E2B push access can rebuild
the template; the next sandbox spawn on every Agnes deployment picks up
the new image. **Test rebuilds on a dev Agnes first** — an incompatible
`claude-agent-sdk` bump will break the runner silently.

### Extending the E2B template

Edit `app/initial_workspace_default/e2b-template/Dockerfile` to add
runtime dependencies the runner needs, then `e2b template build` again.
The template README walks through the full flow.

**A stale image silently loses features, it does not fail.** Both sandbox
images ship `matplotlib` so the agent can draw a chart; an image built before
that was added has no way to obtain it at runtime (E2B's per-hostname egress
allowlist, and `docker_egress_mode: none` on the Docker provider, both put
PyPI out of reach), so the agent falls back to prose or a markdown table
instead of a chart, with nothing in the logs to say why. After upgrading
Agnes, rebuild the template — `e2b template build`, or
`docker build -t agnes-chat-sandbox:latest app/initial_workspace_default/docker-sandbox`
for the self-hosted provider — and confirm the contract label reads `2`:
`docker inspect -f '{{ index .Config.Labels "agnes.chat-sandbox.contract" }}'`.

### Per-user workspace size

Workspaces live on the Agnes host at
`${DATA_DIR}/users/<email>/workspace`. The 100 MB push cap
(`chat.e2b_workspace_max_bytes`) bounds the per-spawn upload to keep
session-start latency under a few seconds. Users who exceed the cap
get a `workspace_too_large` error frame; raise the cap or have them
trim local files.

## Known limitations (v1)

- No cloud↔local workspace sync. A user with local Claude Code and
  cloud chat has two independent workspaces.
- Slack: DM only. Channel `@agnes` mentions land in a follow-up PR.
- Single uvicorn worker only (see § Host requirements).
- **Bundled workspace ships no sub-agents.** `app/initial_workspace_default/.claude/agents/` is empty. Sub-agent dispatch (Task tool) requires the operator to install marketplace plugins that ship `agnes-*.md` agent definitions; without them the chat agent will answer directly without sub-agent delegation. The E2E test `tests/e2e/test_sub_agent_dispatch.py::F.9` auto-skips when no agents are present in the workspace.
- **Startup gates refuse chat with a clear log line on any missing prerequisite.** `ANTHROPIC_API_KEY` + `JWT_SECRET_KEY` always; `E2B_API_KEY` + `chat.e2b_template_id` under `provider: e2b`; a non-loopback rails URL + a reachable sidecar with the sandbox image present under `provider: docker`.
- **Egress is enforced at the VM level**, not by the in-sandbox hook. `E2BProvider.spawn` passes `network={"allow_out": …, "deny_out": [ALL_TRAFFIC]}`, so everything outside `chat.egress_allow_out` (default: the Agnes host, loopback, `api.anthropic.com`, `api.github.com`) is blocked by the platform. The workspace `PreToolUse` hook is defense-in-depth only: it is fail-open, inspects Bash alone, and is a workspace file the agent could rewrite — the VM-level deny-list survives its removal. (This supersedes the original Q4 fail-open decision.)
- **A "Continue on web" click that lands on another replica loses the pending approval.** `attach()` resolves a session owned by a different gateway through a claim-then-respawn takeover, and a fresh runner has no memory of the suspended tool call — the old sandbox's gate dies with it. The user sees the turn restart rather than the card. Single-replica deployments are unaffected; on a multi-replica one, answer from a browser attached to the owning gateway (or just re-ask).
- **Slash-command sessions get no approval nudge of their own.** `EphemeralCommandSink` posts to a `response_url` and carries no `chat_id`/`web_base` to build a deep link from, so it stays silent on `approval_request`. In practice those sessions also carry a `SlackSinkBridge`, which does post the nudge.
- **The approval gate matches `Bash` tool calls only.** The bundled workspace hook returns `allow` for every non-Bash tool, so no policy is lost as shipped. An operator override that adds `ask` rules for `Write`/`Edit`/`WebFetch` would find them inert in cloud chat until the SDK hook matcher (`app/chat/runner.py`) is widened past `Bash` — a deliberate scope choice (gating every `Read`/`Write` through a per-call file-hook subprocess adds real latency).
- **`audit_log.user_id` for chat rows holds the user email, not the user UUID.** Joining `audit_log` to `users` for chat events requires `audit_log.user_id = users.email` for `action LIKE 'chat.%'` and the usual `audit_log.user_id = users.id` for everything else. Documented in `app/chat/audit.py::write_audit`.
- **`_real_agent_loop` enforces a turn-level wall-clock cap, not per-tool.** `claude-agent-sdk` 0.2.x doesn't expose per-tool dispatch hooks; the runner enforces `tool_calls_per_turn_budget` and a turn-level timeout instead of per-tool granularity. Revisit when the SDK ships per-tool hooks.
- **E2B SDK 1.x uses the mutable `:latest` template tag.** Per Q2 a teammate rebuild propagates to every live deployment on its next spawn — test rebuilds on a dev Agnes first.
- **E2B API outage → chat unavailable, no automatic fallback.** There is no runtime failover between providers: chat returns 503 until the E2B SDK recovers. Operators monitor E2B status separately. An instance that cannot depend on E2B at all should run `provider: docker` instead (a deliberate, restart-scoped configuration choice, not a hot standby).
- **Per-session E2B billing is operator-visible only in the E2B dashboard**, not yet in Agnes admin UI.
