    # -------------------------------------------------------------------------
    # L3 — role: worker
    # -------------------------------------------------------------------------
    # Two pipelines share one job runtime. The data path ends in the analytics
    # plane; the document path ends in the knowledge store. The worker is the
    # only writer to either.
    # -------------------------------------------------------------------------

    wkRuntime -> appState "Claims jobs under a lease, heartbeats, reaps expired ones" "SQL"
    wkRuntime -> coordination "Cross-process job leases" "Redis"
    wkRuntime -> wkConnectors "data-refresh, jira-refresh"
    wkRuntime -> wkCrawler "corpus-extraction"
    wkRuntime -> wkMirror "distribution-mirror, chained off every successful data-refresh"
    wkRuntime -> wkMaintenance "ducklake-maintenance, daily"

    # --- the data path ---
    wkConnectors -> keboola "Batch pull to parquet" "DuckDB Keboola extension"
    wkConnectors -> bigquery "Remote views and materialized SQL" "DuckDB BQ extension"
    wkConnectors -> databricks "Warehouse materialization" "Statement Execution API"
    wkConnectors -> snowflake "Materialization to parquet" "SQL"
    wkConnectors -> extracts "Writes extract.duckdb plus data/*.parquet — the one contract" "File"
    wkOrchestrator -> extracts "Scans and ATTACHes every source" "DuckDB"
    wkOrchestrator -> analytics "Rebuilds master views under rebuild_mutex()" "SQL"
    wkOrchestrator -> appState "Updates sync_state and sync_history" "SQL"
    wkMirror -> extracts "Reads the parquet the manifest already serves" "File"
    wkMirror -> objectStore "Uploads only what changed, md5-compared" "S3"
    wkMaintenance -> analytics "Compacts files, expires snapshots, vacuums the catalog" "SQL"

    # --- the document path ---
    wkCrawler -> sharepoint "Per-drive delta; resumes from persisted links" "Microsoft Graph"
    wkCrawler -> wkConvert "Each changed document"
    wkConvert -> wkAnonymize "Clean markdown, headings and tables reconstructed"
    wkAnonymize -> wkFacts "Pseudonymized text — the model never sees a real identity"
    wkAnonymize -> vertex "Optional LLM detector tier, unioned with the deterministic one" "HTTPS"
    wkFacts -> vertex "Reads each document once behind the untrusted-data fence" "HTTPS"
    wkFacts -> appState "Facts and their verbatim evidence, through the facts/ingest chokepoint" "SQL"
    wkAnonymize -> appState "Corpus files and chunks" "SQL"
