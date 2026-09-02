model {

    # Each level states its own relationships in the words that level's reader
    # needs. Structurizr would otherwise roll a component's wording up to its
    # container and its system and collide with the sentence written there.
    !impliedRelationships false

    # -------------------------------------------------------------------------
    # People
    # -------------------------------------------------------------------------

    analyst = person "Data analyst" "Pulls governed parquet to a laptop, works in Claude Code, publishes what they learn back as corporate memory."
    businessUser = person "Business user" "Asks questions in web chat, Slack or Telegram; opens hosted data apps."
    administrator = person "Administrator" "Registers sources and tables, grants access, sets model and token budgets, reviews the audit trail."

    # -------------------------------------------------------------------------
    # External systems
    # -------------------------------------------------------------------------

    !include ../l3/external-systems.dsl

    clientApp = softwareSystem "Client application" "Calls a named agent profile with a PAT and gets a one-shot or streamed answer." {
        tags "Vendor"
    }

    # -------------------------------------------------------------------------
    # L1 — the system in focus
    # -------------------------------------------------------------------------

    agnes = softwareSystem "Agnes" "Source-available, self-hosted AI harness. Governed data access, agent profiles with a public agent API, skills marketplace, corporate memory, hosted data apps, and agent surfaces. One image, one entrypoint; AGNES_ROLE selects which planes a process runs." {

        !include ../l2/containers.dsl
        !include ../l3/api.dsl
        !include ../l3/gateway.dsl
        !include ../l3/worker.dsl
    }

    workstation = softwareSystem "Analyst workstation" "The analyst's own machine. Converged by Claude Code hooks rather than managed — SessionStart runs agnes update, SessionEnd runs agnes push." {

        cli = container "agnes CLI" "pull · query · snapshot · push · stack · agent · chat · admin. scope=auto runs local or server-side and says which on stderr." "Python"
        localAnalytics = container "Local analytics" "DuckDB views over pulled parquet, plus snapshots of remote tables. A query that lands here costs nothing." "DuckDB, Parquet" {
            tags "Datastore"
        }
        claudeCode = container "Claude Code workspace" "The stack materialized: skills, data, memory rules, CLAUDE.md." "Claude Code"
    }

    # -------------------------------------------------------------------------
    # L1 relationships
    # -------------------------------------------------------------------------

    analyst -> agnes "Pulls governed data, queries, publishes findings"
    analyst -> workstation "Works here"
    businessUser -> agnes "Asks questions in chat, Slack or Telegram"
    administrator -> agnes "Registers sources, grants access, sets budgets, reviews the audit trail"
    clientApp -> agnes "POST /api/v1/agents/{slug}/responses, PAT-authenticated"
    workstation -> agnes "Pulls the RBAC-filtered manifest and parquet; pushes sessions and CLAUDE.local.md back"

    agnes -> aws "Object storage for the distribution mirror"
    agnes -> azure "Hosts the instance; Entra ID and Microsoft Graph"
    agnes -> gcp "Hosts the instance; BigQuery, Vertex AI, Cloud Identity"
    agnes -> keboola "Batch pull through the DuckDB extension"
    agnes -> bigquery "Remote attach and materialized SQL, under a dry-run scan cap"
    agnes -> databricks "SQL warehouse materialization and per-query remote execution"
    agnes -> snowflake "Registered source connection; remote statements run on the warehouse"
    agnes -> jira "Signed webhooks plus REST consistency polls"
    agnes -> sharepoint "Per-drive delta crawl over Microsoft Graph"
    agnes -> entra "Signs users in; optional Graph group sync"
    agnes -> googleWorkspace "Signs users in; nightly group sync"
    agnes -> anthropic "Sends prompts on an agent's behalf"
    agnes -> vertex "Sends prompts on an agent's behalf, keyless"
    agnes -> openaiCompat "Sends prompts on an agent's behalf"
    agnes -> objectStore "Mirrors changed parquet behind 15-minute signed URLs"
    agnes -> marketplaceRepos "Clones registered marketplaces nightly"
    agnes -> slack "Socket Mode or HTTP events"
    agnes -> telegram "Long-poll bot and notification dispatch"

    # -------------------------------------------------------------------------
    # L2 relationships — into and inside the deployment
    # -------------------------------------------------------------------------

    businessUser -> caddy "Web chat, dashboards, data apps" "HTTPS"
    administrator -> caddy "/admin console" "HTTPS"
    analyst -> cli "Runs pull, query, snapshot, chat"
    analyst -> claudeCode "Works here"
    cli -> caddy "Manifest, downloads, queries, push" "HTTPS + Bearer PAT"
    clientApp -> caddy "Agent API" "HTTPS + Bearer PAT"

    caddy -> api "Forwards REST, web and MCP requests" "HTTP"
    caddy -> gateway "Forwards chat and notification WebSockets" "HTTP"
    caddy -> dataApp "RBAC-gated ingress to /apps/<slug>" "HTTP"

    api -> appState "Reads and writes every app-state row through the factory" "SQL"
    api -> analytics "Answers SELECT-only queries against master views" "SQL"
    api -> extracts "Serves parquet downloads and builds the manifest" "File"
    api -> coordination "Rate limits, cache invalidation, auth tickets" "Redis"
    gateway -> appState "Chat sessions, messages, agent memory, usage" "SQL"
    gateway -> coordination "Routing leases, replay streams, notification pub/sub" "Redis"
    gateway -> appsRunner "Requests a sandbox for a session" "HTTP"
    worker -> appState "Claims jobs; writes catalog, knowledge, facts and audit rows" "SQL"
    worker -> extracts "Connectors write the extract.duckdb contract here" "File"
    worker -> analytics "Rebuilds master views — the only writer" "SQL"
    worker -> coordination "Job leases and heartbeats" "Redis"

    scheduler -> api "Triggers recurring work on offset cadences" "HTTPS + shared secret"
    appsRunner -> sandbox "Creates and destroys the container" "Docker socket"
    appsRunner -> dataApp "Creates, wakes and sleeps the container" "Docker socket"
    sandbox -> egressProxy "All outbound traffic" "CONNECT"
    egressProxy -> api "The service API is what an agent is granted" "HTTPS"
    gateway -> sandbox "Streams the turn in and the frames out" "stdio"
    surfaceBots -> gateway "Relays messages onto the session API" "HTTPS"
    surfaceBots -> coordination "One leader lease per workspace" "Redis"

    # -------------------------------------------------------------------------
    # L2 relationships — out of the deployment
    # -------------------------------------------------------------------------

    api -> entra "Signs users in; optional group sync" "OpenID Connect"
    api -> googleWorkspace "Signs users in; nightly group sync" "OpenID Connect"
    api -> bigquery "Remote statements, dry-run cost-capped" "DuckDB BQ extension"
    api -> databricks "Remote statements, byte-limited" "Statement Execution API"
    api -> snowflake "Remote statements" "SQL"
    gateway -> anthropic "Brokered prompts; keys never enter the sandbox" "HTTPS"
    gateway -> vertex "Brokered prompts; keyless via workload identity" "HTTPS"
    gateway -> openaiCompat "Brokered prompts" "HTTPS"
    worker -> keboola "Batch pull" "DuckDB Keboola extension"
    worker -> bigquery "Materialized SQL to parquet" "DuckDB BQ extension"
    worker -> databricks "Warehouse materialization to parquet" "Statement Execution API"
    worker -> snowflake "Materialization to parquet" "SQL"
    worker -> sharepoint "Per-drive delta crawl" "Microsoft Graph"
    worker -> objectStore "Mirrors changed parquet" "S3"
    worker -> marketplaceRepos "Nightly clone" "git"
    jira -> api "Pushes issue changes as they happen" "Signed webhook"
    surfaceBots -> slack "Socket Mode or HTTP events" "HTTPS"
    surfaceBots -> telegram "Long poll" "HTTPS"
    cli -> objectStore "Prefers the signed URL, falls back to the app route" "HTTPS"

    # -------------------------------------------------------------------------
    # L2 relationships — on the workstation
    # -------------------------------------------------------------------------

    cli -> localAnalytics "Rebuilds views over pulled parquet" "DuckDB"
    claudeCode -> cli "Skills and hooks shell out to it"
    claudeCode -> localAnalytics "Queries what was pulled" "DuckDB"

}
