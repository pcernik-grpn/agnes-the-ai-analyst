> Companion: [docs/PLATFORM_SETUP.md](./PLATFORM_SETUP.md) is the day-2 operator playbook — marketplaces, scheduler cadence, telemetry, privacy posture, daily routine. It complements this doc rather than replacing it.

# Quick Start Guide

## Prerequisites

- Python 3.10+
- Docker + Docker Compose (for production deployment)
- Data source credentials (Keboola token, BigQuery project, etc.)

## Local Development Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd agnes-the-ai-analyst
   ```

2. Create virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   uv pip install ".[dev,server]"
   ```

3. Configure your instance:
   ```bash
   cp config/instance.yaml.example config/instance.yaml
   # Edit config/instance.yaml with your settings
   ```

4. Set up environment variables:
   ```bash
   cp config/.env.template .env
   # Edit .env with your data source credentials
   ```

5. Register your tables via the admin API or CLI:
   ```bash
   # Via CLI — the first argument is the catalog name the table gets locally
   agnes admin register-table company --source-type keboola --bucket "in.c-crm" --source-table "company" --query-mode local

   # Or start the server and use the web UI at /admin/tables
   ```

6. Start the FastAPI server:
   ```bash
   uvicorn app.main:app --reload
   ```

7. Trigger a data sync:
   ```bash
   curl -X POST http://localhost:8000/api/sync/trigger
   ```

## Docker Deployment

The default install runs app-state on the bundled Postgres side-car
(`docker-compose.postgres.yml`), not single-file DuckDB. Set
`POSTGRES_PASSWORD` in `.env` (`config/.env.template` already ships
`COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml`, so plain
`docker compose up` includes the overlay automatically):

```bash
# Start app + scheduler + Postgres side-car
docker compose up

# Include telegram bot
docker compose --profile full up

# HTTPS mode — Caddy + corporate-CA certs
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
    --profile tls up -d
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for full server setup instructions.

### Legacy fallback: single-file DuckDB (existing installs)

Existing instances that predate the Postgres default keep running app-state
on single-file DuckDB — nothing changes for them. To run a **new** instance
this way (not recommended — see [DEPLOYMENT.md](DEPLOYMENT.md)), unset
`POSTGRES_PASSWORD` and `COMPOSE_FILE` in `.env` and start without the
overlay:

```bash
docker compose -f docker-compose.yml up
```

## Using with Claude Code

Open the project in Claude Code. The CLAUDE.md file will guide the AI assistant through setup and analysis workflows.

### Analyst Setup

The instance home page walks a new analyst through it; there is nothing to configure by hand.

1. Visit your instance URL (e.g., https://data.example.com) and sign in with your company email.
2. Follow the guided steps on `/home`: install Claude Code, create the workspace folder, open a terminal in it, save your login token to `~/.agnes/token`, and launch Claude Code there.
3. The last step hands you the install prompt — paste it into Claude Code. The prompt is thin: it installs the `agnes` CLI, then runs `agnes onboard --workspace .`.
4. Restart Claude Code when `agnes onboard` says so, and confirm what it reported.

`agnes onboard` is the whole setup, run as one deterministic command instead of a
list of instructions for the agent to follow: it checks the workspace directory,
runs `agnes init` (auth from the saved token, workspace files, Claude Code hooks,
first `agnes pull`), smoke-tests the catalog, checks `git` and `claude` are on
`PATH`, registers the Agnes marketplace, runs `agnes diagnose`, and prints a
summary with a `NEXT:` block. It is idempotent — re-run it any time a workspace
looks broken. `--json` emits the same report machine-readably.

Connecting tools (Jira, Asana, Google Workspace, …) is **not** part of first-run
setup any more. Once the workspace is up, just ask for it in Claude Code ("set up
Jira") and the connector skill walks you through it. `agnes connectors list` shows
what this instance offers, `agnes connectors show <slug>` prints one connector's
setup instructions.

### Analysis Workflow

1. Sync latest data: `curl -X POST https://data.example.com/api/sync/trigger`
2. Open Claude Code in your workspace directory
3. Ask Claude to analyze your data using DuckDB

### When something goes wrong

Run `agnes doctor`. It writes one redacted file (`agnes-doctor-<timestamp>.md`)
holding what a support request needs answered up front: CLI version, server and
auth state, workspace and last-pull state, recent client errors — plus, when an
admin runs it, the server side (image tag, migration verdict, per-source sync
failures, disk, whether retrieval has silently degraded to lexical-only). Secret
values are never collected, only whether each one is set, so the file is safe to
attach as-is.

It always produces the file: run it offline, or without admin rights, and the
parts it could not reach say so explicitly rather than going quiet. Use
`agnes diagnose` when you want live checks and a verdict instead of an artifact.

### Windows: Smart App Control blocks the CLI (`os error 4551`)

**Symptom.** On Windows 11, an `agnes` command — or the self-upgrade that runs
on session start — fails with `os error 4551`, or with
`[WinError 4551] An Application Control policy has blocked this file`. Launching
`agnes` may produce nothing at all: no traceback, no log line, just an
immediately dead process. It typically looks like "it worked yesterday and
nothing changed", because nothing on your side did.

The refusal comes from **Smart App Control** (SAC), Microsoft's
reputation-based application-control policy. It blocks binaries it cannot
verify at *load* time, before any of the program's own code runs — which is why
there is no Agnes error message to read. Two files a `uv tool install` produces
are exactly that kind of binary: the `agnes.exe` launcher uv generates locally,
and the portable Python interpreter uv downloads. Neither is signed.

Where Agnes *can* see the block, it now says so: a self-upgrade whose
freshly-installed binary is refused prints the diagnosis and the confirmation
command below instead of an opaque smoke-test failure, and records it so a
later command repeats it. A block on the binary you launch yourself cannot be
reported by Agnes at all — the process never starts.

**Confirm it is Smart App Control.** In a terminal:

```
reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState
```

`0x2` (2) = enforcing — this is your problem. `0x1` (1) = evaluation mode, where
SAC is watching and may switch itself to enforcing later. `0x0` (0) or a missing
value = off, so look elsewhere.

**Who is exposed.** A narrow but real group:

- SAC exists only on a Windows 11 machine that was **cleanly installed or
  reset** — an in-place upgrade from Windows 10 or an earlier Windows 11 build
  never has it.
- It ships enabled only in **some regions**.
- It starts in evaluation mode and Microsoft's heuristic **turns it off for
  users it detects as developers**. It flips to enforcing for the rest.

So the exposed case is a *new or freshly-reset* Windows 11 machine, before that
heuristic has classified its owner as a developer — including a developer whose
machine simply has not been used like one yet. This is also why it appears
overnight without a config change: evaluation mode became enforcement.

**Workaround, and its honest cost.** SAC has **no per-app allowlist** — unlike
SmartScreen, there is no "Run anyway" for one binary, and no signature you can
add locally. The available options today:

- Run the CLI where the policy does not apply: inside WSL or a Linux container.
- Turn Smart App Control off: *Windows Security → App & browser control → Smart
  App Control*. This is **system-wide** — it lowers protection for every
  program on the machine, not just Agnes — and per Microsoft it **cannot be
  turned back on without resetting or reinstalling Windows**. Treat it as a
  one-way decision.

**There is no signed Agnes install path yet.** A signed Windows executable
would fix this properly for everyone, and it is not available: it needs a
code-signing certificate and release-side signing infrastructure that Agnes
does not ship today. Until then the two options above are the whole list.

Reference:
[Smart App Control overview](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview)
(Microsoft Learn).

## Hackathon

See [`archive/HACKATHON.md`](archive/HACKATHON.md) for the deploy-and-develop playbook (archived event runbook). Per-developer dev VMs are the supported pattern — point your VM at your branch image with `gcloud compute ssh <vm> --command "sudo sed -i 's/^AGNES_TAG=.*/AGNES_TAG=dev-<slug>/' /opt/agnes/.env && sudo /usr/local/bin/agnes-auto-upgrade.sh"`.
