# Recipe: an announcement-drafting agent on Agnes

A worked example of assembling a fully autonomous agent from Agnes
primitives — no external orchestrator, no separate agent-hosting vendor.

**The shape:** your team posts shipped-feature announcements in a Slack
channel. An agent profile is bound to that channel; when someone @mentions
the bot on a post, the agent reads the thread, decides whether the feature
deserves a customer-facing release note, drafts one in your house style, and
creates a **draft** post in your CMS via a write-capable MCP tool — then
replies in the thread with a preview. A human publishes from the CMS editor;
the agent never can.

Everything below is instance configuration (data), not code. Placeholders
throughout — substitute your own hosts, ids, and channel.

## Prerequisites

- Agnes with chat + the Slack surface configured (`docs/slack-manifest-http.md`
  or `-socket.md` — note the bot needs the `reactions:write` scope for the
  mention acknowledgement).
- An MCP server exposing your CMS as tools, reachable from the Agnes server
  (e.g. a small FastMCP service wrapping the CMS admin API; hold the CMS
  credential in that server, expose only draft-creating tools). Same for any
  Slack read/write tools you want the agent to have beyond the surface's
  own reply path.
- `chat.bootstrap_marketplace` left ON (the default) if the agent should
  load marketplace skills (e.g. a house style guide) in its sandbox.

## 1. Register the tool servers

```bash
agnes admin mcp source add --name cms --transport http --url https://cms-mcp.example.com/mcp
agnes admin mcp source set-secret cms   # bearer token for the server
agnes admin mcp source introspect cms   # registers its tools in the registry
```

Repeat for a Slack tool server if used. Tools arrive in `tool_registry`;
write-capable ones should be marked `mutating` (Agnes records a tool as
mutating whenever the upstream does not annotate `readOnlyHint`).

## 2. Grant the tools — and opt into the write surface

Create a group for the agent's owner (or reuse one), then grant the tools.
A plain grant is read-only; each write tool needs the explicit per-tool
opt-in:

```bash
agnes admin mcp source grant cms --group <group_id>          # read tools, in bulk
agnes admin mcp tool grant cms__create_draft --group <group_id> --allow-mutating
```

The mutating opt-in is deliberately per-tool: opting a group into every
write tool of an upstream in one action is too coarse an act to be one flag
away. Agent profiles ride their owner's groups (with the admin short-circuit
stripped), so this grant is exactly what makes the draft-creation tool
reachable for the agent — and only inside its declared connection scope.

## 3. Create the agent profile

```bash
agnes agent create "Release Notes Drafter" --slug release-notes \
    --prompt-file ./system-prompt.md
```

The system prompt carries the worthiness rubric, house style, reply format,
and hard constraints ("always draft, never publish"). New agents default all
four scope modes to `selected` with an empty scope — zero capability until
you grant it.

## 4. Scope it

```bash
agnes agent scope set release-notes \
    --connection <cms_source_id> --connection <slack_source_id> \
    --plugin <marketplace_slug>/<style_skill_plugin> \
    --slack-channel <CHANNEL_ID>
```

- `--connection` rows narrow which MCP sources the agent may reach.
- `--plugin` rows attach skills (requires `chat.bootstrap_marketplace`).
- `--slack-channel` is the **routing** item: @mentions in that channel now run
  this agent — the session carries its persona, scope, and budget; the first
  turn is prefixed with a `[slack context: channel=… thread_ts=… message_ts=…
  sender=…]` header so the agent can call Slack tools against the right
  thread; and the mention gets an instant 👀 reaction. One agent per channel
  (`409 slack_channel_taken`).

Also allowlist the channel for the Slack surface itself (default-deny):
`agnes admin grant create Everyone slack_channel <CHANNEL_ID>`.

## 5. Invoke it

- **On mention** — automatic, via the binding above.
- **Headless / scheduled** — `POST /api/v1/agents/release-notes/responses`
  with a prompt (e.g. a daily "sweep the channel for undrafted announcements"
  run from your scheduler of choice). Set a `token_budget_monthly` on the
  agent so an unattended loop cannot spend unbounded tokens.

## Safety properties worth keeping

- **Draft-only by construction**: expose no publish tool from the CMS MCP
  server. The mutating grant then bounds the blast radius to draft creation.
- **Tools execute server-side** (Universal MCP passthrough): the CMS
  credential never enters the agent sandbox, and no sandbox egress needs
  opening.
- **Scope is live-enforced**: narrowing the agent or revoking a grant takes
  effect on the next request; the agent can never exceed
  (owner grants ∩ agent scope).
- **A binding is a shared surface, on purpose**: anyone in the bound,
  admin-allowlisted channel who passes the Slack identity + CHAT gates can
  drive the agent, and the turn runs with the AGENT's authority — not the
  mentioning user's. That is what makes "anyone on the team asks it to
  draft" work; it is also why you scope the agent narrowly and bind only
  channels where that is intended.
