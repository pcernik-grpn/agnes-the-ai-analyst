# Global distribution — Agnes in every repository

Make Agnes skills and data access available in **all** repositories on a
machine, not just the analyst workspace. Three audiences, three recipes.

## Engineer with the Agnes CLI (recommended)

Prerequisite: `agnes init` has run once on this machine (any workspace).

    agnes global enable

Idempotently converges five user-scope artifacts:

| Artifact | Where | What it does |
|---|---|---|
| Stack plugins | `claude plugin install <p>@agnes --scope user` | skills/commands from your stack load in every repo |
| MCP server | `claude mcp add --scope user agnes -- <agnes> mcp` | catalog/schema/query/query_local tools everywhere (first tools appear a few seconds into a fresh session — the stdio server has a short cold start) |

> The stdio server (`agnes mcp`) is internal wiring: `agnes global enable`
> registers it for you, and the hosted chat sandbox spawns its own. It is not
> a supported end-user command — do not add it to a client by hand. A machine
> without the CLI should use the remote HTTP transport below, which carries the
> full server-side tool set.

| Rails block | `~/.claude/CLAUDE.md` (marker-fenced) | the data-querying protocol in every session |
| SessionStart hook | `~/.claude/settings.json` | a detached `agnes update --quiet` keeps data + plugins fresh from any repo (skip with `--no-hook`) |
| Config flag | `global_scope` in the CLI config | `agnes update` re-converges the layer on every run |

Check with `agnes global status` (add `--json` for scripting); remove with
`agnes global disable` — it reverts exactly what enable wrote and never
touches your other marketplaces, MCP servers, or hooks.

Privacy: session transcripts are uploaded ONLY from the anchored analyst
workspace. Sessions in your other repositories are never pushed, with or
without the global layer.

## Machine without the CLI — remote MCP

    claude mcp add --scope user --transport http agnes https://<agnes-host>/api/mcp/http

Claude Code will report "Needs authentication" — open any session and run
`/mcp` to complete the OAuth consent in your browser (sign in with your
Agnes account). You get the full RBAC-filtered server-side tool set; no
local parquets, no CLI required.

## Operator — fleet-wide default

Managed settings (an MDM-deployed `managed-settings.json`, or server-managed
settings) can force the layer for every engineer:

    {
      "extraKnownMarketplaces": {
        "agnes": {"source": {"source": "git", "url": "https://<agnes-host>/marketplace.git"}}
      },
      "enabledPlugins": {"<plugin>@agnes": true},
      "strictKnownMarketplaces": false
    }

plus a managed MCP entry pointing at `https://<agnes-host>/api/mcp/http`.
Set `strictKnownMarketplaces: true` to lock plugin sources to the list
above. Consult the Claude Code managed-settings documentation for
deployment paths per OS.
