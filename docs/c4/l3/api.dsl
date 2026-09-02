    # -------------------------------------------------------------------------
    # L3 — role: api
    # -------------------------------------------------------------------------
    # Every path reaches an access gate before it reaches data. That is the
    # property the rest of the design rests on.
    # -------------------------------------------------------------------------

    apiRouters -> apiAccess "Every mutating route declares a gate"
    apiWeb -> apiAccess "Page routes resolve the caller before rendering"
    apiMcp -> apiAccess "Tool calls carry the same principal as REST"

    apiRouters -> apiPatResolver "Bearer tokens resolve here"
    apiWeb -> apiAuthProviders "Sign-in and callback routes"
    apiAuthProviders -> entra "Single-tenant OIDC; optional Graph group sync" "HTTPS"
    apiAuthProviders -> googleWorkspace "OAuth plus Cloud Identity group sync" "HTTPS"
    apiAuthProviders -> apiRepoFactory "Upserts the user and their group memberships"
    apiPatResolver -> apiRepoFactory "Hash lookup, expiry and revocation check"
    apiAccess -> apiRepoFactory "Reads groups and resource_grants"

    apiRouters -> apiQuery "Submitted SQL"
    apiWeb -> apiQuery "Catalog pages and charts"
    apiMcp -> apiQuery "Foundation query tool"
    apiQuery -> apiAccess "Checks the caller against every referenced view name"
    apiQuery -> apiPolicies "Substitutes an attached policy for the table"
    apiQuery -> apiRemoteEngines "Routes a remote statement to its engine"
    apiQuery -> analytics "Master views, legacy or DuckLake" "SQL"
    apiRemoteEngines -> bigquery "Dry-run cost cap, then push-down" "DuckDB BQ extension"
    apiRemoteEngines -> databricks "byte_limit; a capped result is refused, never returned short" "Statement Execution API"
    apiRemoteEngines -> snowflake "Remote statement on the warehouse" "SQL"

    apiRouters -> apiManifest "GET /api/sync/manifest"
    apiManifest -> apiAccess "Filters to the caller's accessible tables"
    apiManifest -> apiRepoFactory "Reads table_registry and sync_state"
    apiManifest -> extracts "md5 per parquet" "File"

    apiRepoFactory -> appState "One backend, reached only through *_repo()" "SQL"
    apiRouters -> apiAudit "Every mutation"
    apiAccess -> apiAudit "Every refusal"
    apiAudit -> apiRepoFactory "log_safe writes audit_log rows"
