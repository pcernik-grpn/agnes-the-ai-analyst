    # -------------------------------------------------------------------------
    # Agnes — L2 containers
    # -------------------------------------------------------------------------
    # One image, one entrypoint; AGNES_ROLE selects which planes a process
    # runs. `all` (the default) is a single process running all three.
    # Components are declared inline where an L3 view exists.
    # -------------------------------------------------------------------------

    caddy = container "Caddy" "TLS termination, security headers, reverse proxy. The only listener the network reaches." "Caddy" {
        tags "Infrastructure"
    }

    api = container "role: api" "REST routers, Jinja web pages and the MCP server. Resolves the caller to a principal and re-checks authority at every read and write." "Python, FastAPI, Uvicorn" {

        apiRouters = component "REST routers" "query · data · catalog · sync · admin · users · agents · memory · jobs · stack." "FastAPI"
        apiWeb = component "Web pages" "Jinja over base_ds / base_page: chat, catalog, stack, /admin console." "Jinja2"
        apiMcp = component "MCP server" "Streamable HTTP and stdio. Foundation tools defined once, parity-tested against REST and CLI." "Python"
        apiAuthProviders = component "Auth providers" "Google OIDC, Entra ID, magic link, password, Keboola OAuth." "Python"
        apiPatResolver = component "PAT resolver" "Hash, expiry, revocation and IP audit for user and agent tokens." "Python"
        apiAccess = component "Access gates" "require_admin for app-level mutations; require_resource_access(type, id) for entity-scoped ones." "Python"
        apiQuery = component "Query sandbox" "SELECT / WITH only; file and URL functions blocked; RBAC checked against every referenced view name." "Python, DuckDB"
        apiRemoteEngines = component "Remote engines" "Picks the engine a statement needs and refuses one straddling two (remote_cross_engine_unsupported)." "Python"
        apiPolicies = component "Access policies" "One SQL policy substituted for the table on every server-side read; rows filtered and columns masked per caller." "Python"
        apiManifest = component "Manifest builder" "Per-user table list, md5 per table, optional signed URL — what agnes pull reads." "Python"
        apiRepoFactory = component "Repositories factory" "*_repo() dispatches on the configured backend; callsites never instantiate a repository class." "Python"
        apiAudit = component "Audit log" "Every action string cataloged in src/audit_events.py; every route declares its audit posture." "Python"
    }

    gateway = container "role: gateway" "Chat and agent sessions: spawns a sandbox per conversation, pumps the turn, streams frames back, and brokers every call out." "Python, FastAPI, Uvicorn" {

        gwChatManager = component "ChatManager" "Owns the session lifecycle — spawn, turn pump, idle reap, foreign-session takeover." "Python"
        gwRouting = component "Routing lease" "chat:{id} names the one gateway holding a session's live sandbox; renewed on the idle-reaper heartbeat." "Python"
        gwReplay = component "Replay + inbound streams" "Monotonic seq per outbound frame; a reconnect with ?last_seq= gets exactly the gap. Commands landing on a non-owning replica are forwarded." "Python"
        gwProvider = component "Sandbox provider" "Docker, or the kai-agent engine. Staging materializes the caller's stack into the workspace before spawn." "Python"
        gwBroker = component "Secret broker" "Ticket-gated egress for the sandbox. Keys stay server-side, the agent's model is pinned, token_budget_monthly returns 429 budget_exhausted." "Python"
        gwPrincipal = component "AgentPrincipal" "Owner grants intersected with agent scope, bound live at every brokered request — never an audit-only verdict." "Python"
        gwDelegation = component "Delegation" "Depth-1, one per turn. The delegate spawns under the ORIGINAL CALLER's identity, never either agent owner's." "Python"
        gwArtifacts = component "Artifact harvest" "Files a turn produced come back into the chat, scoped to the session that made them." "Python"
        gwNotifications = component "Notifications WS" "/api/notifications/ws, gateway-only, riding the notify:{user} coordination channel." "Python"
    }

    worker = container "role: worker" "Durable jobs with lease and heartbeat. Owns the analytics rebuild and the document pipeline — the only writer to the analytics plane." "Python, FastAPI, Uvicorn" {

        wkRuntime = component "Job runtime" "Registry, lease and heartbeat, idempotency_key dedup, expired-lease reaping. Heavy lane (1) and light lane (2)." "Python"
        wkConnectors = component "Connectors" "keboola · bigquery · databricks · snowflake · jira · sharepoint · local upload. Each writes the same extract.duckdb contract and nothing else does." "Python"
        wkOrchestrator = component "SyncOrchestrator" "Scans the extracts tree, validates every identifier, ATTACHes each source and rebuilds master views under rebuild_mutex()." "Python, DuckDB"
        wkCrawler = component "Document crawler" "Per-drive Graph delta with persisted links, mid-crawl token refresh, kill-safe incremental state." "Python"
        wkConvert = component "Document conversion" "Office via markitdown, PDF via pypdfium2 with structure reconstruction; degrades per page rather than emitting a wrong table." "Python"
        wkAnonymize = component "Anonymization" "Per-instance HMAC pseudonyms with alias unification. An anonymize-marked document that cannot be anonymized is skipped, never ingested raw." "Python"
        wkFacts = component "Fact extraction" "Each document read once behind the untrusted-data fence; quotes verbatim-checked with one corrective retry." "Python, LLM"
        wkMirror = component "Distribution mirror" "Uploads changed parquet to the object store; never moves or rewrites the extracts tree." "Python"
        wkMaintenance = component "DuckLake maintenance" "merge_adjacent_files, expire_snapshots, cleanup_old_files, catalog VACUUM — mutually exclusive with a rebuild." "Python"
    }

    scheduler = container "Scheduler" "Holds no state of its own. Calls REST on offset cadences with a shared secret." "Python" {
        tags "Infrastructure"
    }

    appsRunner = container "apps-runner" "The only process holding the Docker socket. Image allowlist, fixed mounts, no registry access, no RBAC of its own." "Python" {
        tags "Infrastructure"
    }

    egressProxy = container "egress-proxy" "Fail-closed CONNECT allowlist. The sandbox network has no other route out, so the proxy is the policy layer." "Python" {
        tags "Infrastructure"
    }

    surfaceBots = container "Surface bots" "Slack Socket Mode, Telegram long-poll, Teams. One leader lease per workspace." "Python" {
        tags "Infrastructure"
    }

    sandbox = container "Chat sandbox" "One per chat or agent session. The caller's stack materialized — skills, data, CLAUDE.md, notebook. No host filesystem, no network route except the egress proxy." "Docker" {
        tags "Ephemeral"
    }

    dataApp = container "Hosted data app" "Analyst-authored Flask, Dash or static SPA running next to the data. RBAC-gated ingress, wake on request, idle sleep." "Docker" {
        tags "Ephemeral"
    }

    coordination = container "Coordination backend" "Leases, pub/sub, TTL keys and counters behind one contract with two implementations. Redis is what makes a role split and multi-replica chat possible." "Redis, or in-process" {
        tags "Infrastructure"
    }

    appState = container "App state" "users · groups · grants · table_registry · jobs · chat · knowledge · semantic models · agents · tokens · audit_log. Postgres is required for any role split." "PostgreSQL | DuckDB" {
        tags "Datastore"
    }

    extracts = container "Extracts tree" "/data/extracts/<source>/extract.duckdb plus data/*.parquet. The distribution artifact AND the rollback truth for both analytics backends." "Filesystem" {
        tags "Datastore"
    }

    analytics = container "Analytics plane" "legacy: rebuild a temp file and swap it in atomically. ducklake: catalog in Postgres, per-source copy-ingest, readers hold one attach and get MVCC snapshots." "DuckDB | DuckLake" {
        tags "Datastore"
    }
