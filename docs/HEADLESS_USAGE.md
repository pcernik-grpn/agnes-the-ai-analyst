> Companion: [docs/PLATFORM_SETUP.md](./PLATFORM_SETUP.md) is the day-2 operator playbook — marketplaces, scheduler cadence, telemetry, privacy posture, daily routine. It complements this doc rather than replacing it.

# Headless / CI usage

For unattended clients (CI, cron, Claude Code), authenticate with a Personal Access Token (PAT) rather than an interactive session.

## Create a PAT

**Via UI:** sign in, open `/me/profile`, create a token. Copy the raw value — it is shown exactly once.

**Via CLI (requires an interactive session):**

```bash
agnes auth token create --name "github-actions" --ttl 365d --raw
```

The `--raw` flag prints only the token, suitable for piping into a secret store.

## Use the PAT

Set the `AGNES_TOKEN` env var:

```bash
export AGNES_TOKEN=<your-token>
agnes query "SELECT 1"
```

### GitHub Actions example

```yaml
- name: Sync data
  env:
    AGNES_TOKEN: ${{ secrets.AGNES_TOKEN }}
    AGNES_SERVER: https://agnes.example.com
    # Required on a fresh runner: `agnes pull` refuses to download into a
    # directory that is not a workspace, and a runner has none. Naming the
    # target explicitly is what turns the refusal into a scaffold.
    AGNES_LOCAL_DIR: ${{ github.workspace }}/agnes-data
  run: |
    # Download via the unversioned /cli/download endpoint (-OJ honours
    # Content-Disposition, saving the real PEP-427 filename) rather than a
    # version-pinned /cli/wheel/<name> URL — the pinned form 404s if the
    # server upgrades between when this workflow was authored and when it
    # runs.
    curl -fsSL -OJ "$AGNES_SERVER/cli/download"
    WHEEL=$(ls agnes_the_ai_analyst-*.whl)
    uv tool install "$WHEEL"
    agnes pull
```

`agnes pull --workspace <dir>` is the per-invocation equivalent of
`AGNES_LOCAL_DIR`, for pipelines that sync more than one workspace.

Without either, `agnes pull` exits 1 with
`No workspace found — run 'agnes init' first, or pass --workspace <dir> /
set AGNES_LOCAL_DIR`. That is deliberate: an analyst standing in an
unrelated repository should not have a `server/parquet` + `user/duckdb`
tree scaffolded into it. CI is the case that wants the scaffold, so CI
says where.

## Revoke

```bash
agnes auth token list
agnes auth token revoke <id|prefix|name>
```

Or from `/me/profile` → Revoke.

## Renewal (interactive analysts)

`agnes auth login` (the browser loopback flow, not this doc's headless
`--ttl` path) mints a 90-day PAT. Rather than a refresh-token grant, the CLI
proactively reminds analysts to re-mint before that PAT expires: any
non-quiet command prints a one-line stderr nudge once the stored token is
within `AGNES_TOKEN_RENEW_DAYS` (default 7; `0` disables) of `exp`, at most
once per day. `agnes auth whoami` always shows the current status
(`Token: valid until <date> (<N> days)`). Renew by simply re-running:

```bash
agnes auth login
```

which overwrites the stored token in place. Renewal needs no `--server`
because the workspace's config already names one; on a machine that has never
been initialized, pass `--server https://<your-host>` (login refuses rather
than guessing a local instance). See [`docs/RBAC.md`](./RBAC.md#pat-lifetime--renewal)
for the rationale behind this model over a refresh-token grant.

For unattended/headless clients using `--ttl`-minted PATs (this doc's main
path), there is no nudge — rotate on your own schedule (CI secret rotation,
cron) since there's no interactive terminal to print a warning to.
