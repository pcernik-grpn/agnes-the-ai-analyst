    # -------------------------------------------------------------------------
    # External systems
    # -------------------------------------------------------------------------
    # Everything Agnes talks to but does not run. Cloud providers live here
    # (L1) and are excluded from L2: every container depends on the cloud it
    # runs in, so drawing it a dozen times informs nobody.
    #
    # The model is IDEALIZED — it shows every source, identity provider and
    # model provider Agnes supports. Any one instance connects a subset.
    # -------------------------------------------------------------------------

    aws = softwareSystem "Amazon Web Services" "S3-compatible object storage for the distribution mirror." {
        tags "Vendor" "CloudProvider"
    }
    azure = softwareSystem "Microsoft Azure" "Hosts the instance; Entra ID and Microsoft Graph." {
        tags "Vendor" "CloudProvider"
    }
    gcp = softwareSystem "Google Cloud" "Hosts the instance; BigQuery, Vertex AI, Cloud Identity." {
        tags "Vendor" "CloudProvider"
    }

    keboola = softwareSystem "Keboola Storage" "Batch pull through the DuckDB Keboola extension; master token or per-project OAuth." {
        tags "Vendor" "DataSource"
    }
    bigquery = softwareSystem "Google BigQuery" "Remote attach through the DuckDB BQ extension, plus materialized SQL. Dry-run scan cap." {
        tags "Vendor" "DataSource"
    }
    databricks = softwareSystem "Databricks" "SQL warehouse via the Statement Execution API; Unity Catalog metric views." {
        tags "Vendor" "DataSource"
    }
    snowflake = softwareSystem "Snowflake" "Registered source connection; remote statements execute on the warehouse." {
        tags "Vendor" "DataSource"
    }
    jira = softwareSystem "Atlassian Jira" "HMAC-signed webhooks plus REST consistency polls; incremental parquet shards." {
        tags "Vendor" "DataSource"
    }
    sharepoint = softwareSystem "Microsoft SharePoint" "Per-drive delta crawl over Microsoft Graph; documents converted and anonymized on ingest." {
        tags "Vendor" "DataSource"
    }

    entra = softwareSystem "Microsoft Entra ID" "Single-tenant OIDC; optional Graph group sync." {
        tags "Vendor" "Identity"
    }
    googleWorkspace = softwareSystem "Google Workspace" "OAuth sign-in; Cloud Identity group sync." {
        tags "Vendor" "Identity"
    }

    anthropic = softwareSystem "Anthropic API" "Claude models, reached directly." {
        tags "Vendor" "AI"
    }
    vertex = softwareSystem "Google Vertex AI" "Claude models on Vertex; keyless via workload identity." {
        tags "Vendor" "AI"
    }
    openaiCompat = softwareSystem "OpenAI-compatible gateway" "LiteLLM, OpenRouter, vLLM — any endpoint speaking the OpenAI API." {
        tags "Vendor" "AI"
    }

    objectStore = softwareSystem "Object store" "S3-compatible bucket holding the mirrored parquet, served to the CLI behind 15-minute signed URLs." {
        tags "Vendor" "Storage"
    }
    marketplaceRepos = softwareSystem "Marketplace git repositories" "Admin-registered Claude Code marketplaces, cloned nightly and re-served as one RBAC-filtered feed." {
        tags "Vendor"
    }
    slack = softwareSystem "Slack" "Socket Mode or HTTP events; /agnes slash command and threaded replies." {
        tags "Vendor" "Messaging"
    }
    telegram = softwareSystem "Telegram" "Long-poll bot; notification dispatch." {
        tags "Vendor" "Messaging"
    }
