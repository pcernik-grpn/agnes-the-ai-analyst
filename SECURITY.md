# Security

Agnes is a self-hosted AI harness that gives an LLM agent governed access to an
organization's data. That combination — an agent that executes code, and data
worth protecting — is the whole security problem, so this document states plainly
what Agnes defends, how, and where the edges are.

Nothing here is aspirational. Every control described below is implemented in
this repository, and every limitation is one we know about. If you find a gap
that is not listed, we want to hear about it (see
[Reporting a vulnerability](#reporting-a-vulnerability)).

## Reporting a vulnerability

Report privately through **GitHub's private vulnerability reporting** on this
repository (Security → Report a vulnerability). Please do not open a public
issue, a pull request, or a discussion for a suspected vulnerability.

Useful reports include the affected version or commit, the deployment shape
(Docker Compose, role-split, which optional features are enabled), and enough
detail to reproduce. We aim to acknowledge within a few business days.

## Supported versions

Agnes is pre-1.0 and released continuously. Security fixes land on `main` and
ship in the next release (the `:stable` image tag and the corresponding
`v0.X.Y` tag); older releases do not receive backports. Self-hosted operators
should track `:stable`.

## Scope

**In scope:** anything in this repository — the FastAPI application, the CLI,
the connectors, the agent runtime, the web surfaces, the packaging.

**Out of scope:** the operator's own infrastructure (VM, database, reverse
proxy, DNS, IdP); third-party services an instance is configured to use; and
issues that require an already-compromised operator or admin account. Findings
that reduce to "an org admin can read organization data" are working as
designed — see [Trust model](#trust-model).

## Deployment model

One Agnes instance serves **one organization**. There is no `org_id`/`tenant_id`
in the schema and no tenant boundary in the application: separation between
customers is achieved by running separate instances (separate VMs/projects), which
is how the Terraform module under `infra/modules/customer-instance/` is built.

**Agnes is not a hardened multi-tenant platform, and does not try to be.** If you
need to serve mutually distrusting organizations, run one instance per
organization.

Inside one instance, users are separated by RBAC, per-user workspaces, and
per-session sandboxes — but they share a process, a database, an audit log, and
the instance's LLM credential. The intended posture is *colleagues in one
organization*, not *mutually hostile parties*.

## Trust model

| Party | Trusted for | Notes |
|---|---|---|
| **Operator** | Everything | Controls the host, the DB, the keys, the reverse proxy. Fully trusted by construction. |
| **Org admin** | Reading and administering organization data | God-mode by design (see below). Actions are audited, not consent-gated. |
| **Authenticated user** | Their own grants | Bounded by RBAC; cannot self-elevate through the API. |
| **The agent / its sandbox** | **Nothing** | The agent never makes authorization decisions. The server re-derives authority on every request. |
| **Content the agent reads** | **Nothing** | Query results, connector data, skills, files, messages — all untrusted input. |
| **LLM provider** | Sees prompts and tool traffic | Inherent to the product. |

The load-bearing line is the fourth one: **the sandbox is not trusted to decide
anything**. A compromised or prompt-injected agent can misuse the authority it was
given, but it cannot widen it.

## What Agnes actually defends

### Credentials never enter the sandbox

This is the strongest property in the system and the one worth understanding
first.

Agent code runs in a sandbox that **never receives an API key or a user token**.
The spawn environment deliberately omits `ANTHROPIC_API_KEY` and `AGNES_TOKEN`
(`app/chat/manager.py`). Instead:

1. The server mints an opaque, scope-bound **ticket** — `secrets.token_urlsafe(32)`,
   stored only as a SHA-256 digest, TTL 1 hour (`src/repositories/ticket.py`).
2. The ticket is pushed to the sandbox over the runner's stdin, held in one
   process's memory — never in an environment variable, never on disk
   (`app/chat/relay.py`).
3. In-sandbox tooling is pointed at a **loopback relay** with a dummy key
   (`ANTHROPIC_API_KEY="sk-dummy-broker"`). The relay forwards to the server's
   broker, which injects the real credential server-side and forwards to a
   pinned upstream host (`app/api/broker.py`).

Consequences: a filesystem dump, a memory dump of the wrong process, an
exfiltrated environment, or a leaked transcript inside the sandbox yields **no
reusable credential** — at worst a bearer ticket that expires within the hour and
cannot reach admin routes.

The broker is not a passive proxy. It:

- rejects any replay targeting a route protected by `require_admin`, by walking
  the real route table rather than matching strings (`app/api/broker.py`);
- enforces ticket **scope** (`main` / `mcp` / `data_apps`), auditing mismatches;
- dispatches in-process over ASGI, so replayed requests pass through the **same**
  RBAC dependencies as any external call — there is no privileged backchannel;
- confines `data_apps` tickets to a path prefix and rejects `..` segments;
- revokes and re-mints tickets on every resume, respawn, and kill.

**Honest edge:** the property is *"no credential material inside the VM"*, not
*"no credential use"*. The relay does not authenticate its in-sandbox callers, so
any process inside that sandbox can use the ticket for as long as it is valid,
with the identity the ticket carries. The blast radius is one session's
authority, time-boxed, admin-free — not the instance's keys.

### Agent authority is re-derived per request

Named agent profiles get their own PATs and scopes. A `selected`-scoped agent's
effective authority is **owner grants ∩ agent scope**, and that intersection is
recomputed live on every brokered request (`src/agent_scope_intersection.py`,
`app/auth/pat_resolver.py`) — it is never baked into a token or trusted from a
snapshot. Unknown scope modes resolve to the empty set. Restricted principals
(agent sessions, co-drive sessions) are **hard-denied admin**: `require_admin`
refuses them by principal type, independent of what their owner may be.

The broker additionally enforces each agent's pinned model and monthly token
budget before any spend (`app/api/broker_agent_policy.py`).

### Data access is server-side, on every surface

An analyst's reachable tables are computed from their **stack** — data packages
granted to their groups — by a single authority (`src/rbac.py::can_access_table`).
That check is applied server-side at every read surface independently: the sync
manifest, parquet download, catalog, schema, sample, scan, per-table MCP tools,
the query API, metrics, and knowledge search. The manifest never lists a table the
caller cannot access, and the download endpoint re-checks rather than trusting the
manifest.

PAT `surface` narrows authority further: a token with an unrecognized surface
**fails closed** to the narrow one.

### Sandbox isolation

Chat sessions run in per-session sandboxes: under the `docker` provider a
hardened container created by the apps-runner sidecar (non-root, `cap_drop:
ALL`, `no-new-privileges`, resource limits, no Docker socket inside —
`app/chat/docker_provider.py`); under the default `kai-agent` provider, the
embedded engine's own remote execution sandbox. The in-sandbox
`pre_tool_use.py` hook is **advisory only** (fail-open, Bash-only, agent-
rewritable); the enforcing egress layers sit outside the sandbox's reach —
the docker provider's `chat.docker_egress_mode` (`none` joins an internal
network with no route off the host; `allowlist` adds the egress-proxy
sidecar, whose compose-owned `EGRESS_ALLOW_HOSTS` is the enforcing copy) and,
on kai-agent, the engine's own sandbox policy. Killing a session destroys
its sandbox.

### Untrusted input

The repository keeps an internal security playbook
(`.claude/skills/agnes-conventions/references/security.md`) whose rules each came
from a real prior finding, and a reviewer agent runs its checklist on every PR.
Implemented controls include:

- **SQL** — the query API is `SELECT`-only against a read-only DuckDB handle,
  with a blocklist covering file-reading functions, remote URL schemes, and
  statement separators, plus a sqlglot-based check that rejects file paths in
  `FROM`/`JOIN`. Direct BigQuery paths are registry-gated (`bq_path_not_registered`)
  and then grant-checked.
- **SSRF** — user-supplied URLs are resolved and rejected for private, loopback,
  and link-local targets, with a connection-pinning transport that resists DNS
  rebinding (`src/marketplace_asset_mirror.py`).
- **Path traversal** — archive extraction (marketplace, store, workspace
  templates) validates every entry, resolves, and containment-checks before
  writing; caps entry counts and declared sizes.
- **Credential egress** — a connector cannot aim a stored token at an arbitrary
  host: extensions, token env names, and destination hosts are separately
  allowlisted (`src/orchestrator_security.py`).
- **Rendering** — authored templates render in a Jinja2 `SandboxedEnvironment`;
  markdown reaching the browser is sanitized before insertion; server-side HTML
  goes through an allowlist sanitizer.
- **Client IP** — derived by counting back from trusted proxy hops, never the
  leftmost `X-Forwarded-For` value.

### Secrets at rest

Connector credentials, MCP secrets, Slack tokens, and named connection secrets are
stored **Fernet-encrypted** in the database, keyed by `AGNES_VAULT_KEY`
(`app/secrets_vault.py`). With no key configured, writes are **refused** outside
local-dev mode and the API returns `409 vault_key_not_configured` rather than
silently storing plaintext. PATs and CLI auth codes are stored as SHA-256 digests
— the plaintext is shown once and never persisted.

### Boot-time gates

The instance refuses to start, or refuses to enable a feature, rather than run
insecurely: `JWT_SECRET_KEY` must be present and ≥32 bytes in production;
chat refuses to enable without its required secrets; the apps sidecar's token
check fails closed on an empty token.

## Known limitations

These are real, current, and known. Several are deliberate trade-offs; all of them
are things an evaluator should weigh.

### Prompt injection is not screened

**There is no content screening, provenance labeling, or output filtering between
untrusted content and the model on the main agent path.** Query results, connector
data, marketplace skill bodies, files, and messages reach the model as-is.
(Provenance wrapping exists in three secondary pipelines — store-upload
guardrails, corporate-memory collection, and co-presence summaries — but not on
the product agent.)

Combined with the next item, a successful injection can use the agent's tools with
the session's authority. It cannot exceed that authority — the server re-derives
it — but within it, it acts as the user.

### The agent runs without per-tool approval

Inside the sandbox the agent runs with `permission_mode="bypassPermissions"` and
the full tool set (`app/chat/runner.py`). There is no human-in-the-loop approval
for individual tool calls, no command policy, and no allow/deny tool list. The
only in-turn limits are a per-turn tool-call budget and an idle watchdog.

**The sandbox is the boundary.** The bundled workspace `PreToolUse` hook blocks
some destructive and enumerating commands, but it is advisory: it is fail-open by
construction, it only inspects Bash, and it is a file inside the workspace the
agent could modify. Treat it as defense-in-depth, never as a control. The VM-level
egress deny-list survives its removal.

### Admin is god-mode

Membership in the `Admin` group short-circuits authorization checks. An admin can
read every table, every grant, and other users' chat sessions and transcripts.
Privacy-sensitive reads (transcript views, session downloads) write audit rows,
but there is **no consent gate**, and audit coverage of admin actions is not
enforced by construction — a live-session tail, for example, is not audited.

The scheduler's shared secret maps to an Admin-group identity, so it is
admin-equivalent: treat it like a root credential.

### Sessions are long-lived and not individually revocable

Browser session JWTs are valid for 30 days and carry no server-side session
record, so logout cannot invalidate a stolen cookie. The available kill switch is
deactivating the user. PATs, by contrast, are checked against the database on
every request and revoke immediately.

### CSRF: an origin gate above `SameSite`

State-changing browser requests are defended in layers. The session cookie is
`HttpOnly`, `SameSite=Lax`, and `Secure` when the deployment resolves to HTTPS,
which keeps it off a truly cross-site POST; the sensitive HTML web forms add an
explicit double-submit token. Above these, an application-wide
`CsrfOriginMiddleware` refuses any state-changing request authenticated *only*
by the session cookie when the browser reports it cross-origin
(`Sec-Fetch-Site: same-site`/`cross-site`, or an `Origin` that is neither
same-origin nor on the `CORS_ORIGINS` allowlist) — which also covers the case
`SameSite=Lax` cannot: a *same-site* request from a hosted data app on a sibling
sub-domain. Bearer/PAT API callers (CLI, MCP, agent API) are unaffected. The
gate acts only on positive cross-origin evidence and can be disabled with
`AGNES_CSRF_ORIGIN_ENFORCE=0`.

### Rate limiting is auth-only

Authentication endpoints are rate-limited. Query, chat, webhook, and marketplace
endpoints are not.

### Hosted data apps are coarsely isolated

Data apps share one Docker bridge network with the application container, and the
token injected into an app is, in the code's own words, functionally a
full-privilege token for its owner — the `data-app:<slug>` scope is a label, not
an enforced boundary. Treat a hosted app as code running with its owner's
authority. The feature is **off by default**.

### Audit is not tamper-evident

`audit_log` is a plain table: no hash chain, no signatures, no append-only
constraint, and no retention/pruning job. Audit writes are best-effort — a failure
is swallowed rather than blocking the audited action. Agent tool-call arguments
are hashed, not stored. Vault *reads* are not audited.

### Data at rest is not encrypted by Agnes

DuckDB files and parquet files are written unencrypted; Agnes relies on the
operator's disk/volume encryption. This extends to distributed data: `agnes pull`
writes plaintext parquet to analyst laptops at ambient permissions. After
download, the security model is *stack membership at pull time* — a later grant
revocation prunes files on the next pull, but there is no remote wipe or key
escrow for a laptop that never pulls again.

Where signed URLs are used for distribution, the URL is a **bearer capability**
until it expires (default 15 minutes).

### Supply chain

Dependencies are declared as ranges; the published image resolves them at build
time rather than installing from the lockfile. There is **no image signing, no
provenance attestation, and no SBOM**; operators verify by image digest. The
release pipeline runs a smoke test with automatic rollback, but that is a
correctness gate, not an integrity one.

Marketplace repositories are cloned nightly and, unless an operator pins a `ref`,
track the default branch. Their content — skills, agent definitions, prompts — is
re-served to user sessions **without signature verification**, and content from
curated marketplaces does not pass the store guardrail pipeline. A compromised
marketplace repository is therefore a prompt-injection vector into every session
that installs from it. Pin refs for repositories you do not control.

### Deliberate fail-open paths

Some paths choose availability over strictness, and you should know which:

- the remote-attach **host** allowlist is inactive until configured (the
  extension and token-env allowlists are always on);
- the BigQuery materialize cost guardrail is opt-in and its dry-run is
  best-effort;
- signed-URL distribution degrades to no signed URLs rather than failing a
  request;
- IdP group-sync failures preserve the previous group snapshot and allow login.

### Local development mode disables authentication

`LOCAL_DEV_MODE=1` auto-authenticates a development user. It must never be set on
an internet-reachable deployment.

## Operator responsibilities

Agnes secures what it controls; the rest is yours.

- **Set `JWT_SECRET_KEY` (≥32 bytes) and `AGNES_VAULT_KEY`.** Without the vault
  key, secret storage is refused, not silently downgraded.
- **Terminate TLS and do not expose the application port publicly.** The bundled
  Caddy profile fronts the app and closes the direct port; if you deploy without
  it, restrict the port at the firewall. Review the Terraform module's firewall
  rules against your own exposure requirements before applying them.
- **Restrict who is in the `Admin` group**, and treat the scheduler secret as an
  admin credential.
- **Set `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`** if you run connectors that attach
  to remote hosts with a stored token.
- **Pin marketplace refs** for repositories outside your control.
- **Encrypt the disk** hosting `DATA_DIR` and your database, and decide
  deliberately whether analysts may hold plaintext parquet on laptops.
- **Keep the instance updated** and verify image digests.
- **Enable optional features consciously.** Chat and data apps are off by
  default; both widen the attack surface materially.

## Hardening roadmap

Directionally, and without commitment to dates: per-tool approval and a command
policy for the agent runtime, provenance labeling for untrusted content,
tamper-evident audit, signed release artifacts, and per-app isolation for hosted
data apps.
