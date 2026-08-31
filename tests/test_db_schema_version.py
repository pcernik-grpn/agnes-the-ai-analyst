"""v20 adds source_query column to table_registry.

Backs query_mode='materialized' for BigQuery: admin registers a SQL body
that the scheduler runs through the DuckDB BQ extension and writes as a
parquet to /data/extracts/bigquery/data/<id>.parquet.

The v19 step (#150) drops dataset_permissions, access_requests tables and
users.role, table_registry.is_public columns; v20 then ALTERs the post-v19
table_registry to add the source_query column.
"""

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_62():
    # v27 → v28: explicit-install (Model B) for curated marketplace plugins.
    # user_plugin_optouts row presence flips meaning from "excluded" to
    # "subscribed"; migration wipes existing rows so the inverted reading
    # starts from a clean baseline. Also adds marketplace_plugins.created_at
    # (per-plugin "newest first" sort on /marketplace), backfilled from
    # parent marketplace_registry.registered_at.
    # v28 → v29: /home page rollout — instance_templates singleton
    # consolidation (welcome_template + claude_md_template merged) + new
    # users.onboarded column. See tests/test_v29_home_migration.py for
    # the exhaustive coverage of that step.
    # v29 → v30: news_template — single versioned table for the /home
    # news perex + /news permalink page. See
    # tests/test_news_template_repository.py.
    # v30 → v31: session-pipeline framework — session_processor_state
    #            replaces session_extraction_state with composite PK.
    # v31 → v32 (PR #233): flea-market upload guardrails — adds
    #            store_entities.visibility_status + creates store_submissions.
    # v32 → v33 (PR #233): forensic columns on store_submissions —
    #            file_size, bundle_sha256, bundle_purged_at. Underpins the
    #            persist-blocked-bundle behavior so admins can Rescan /
    #            Override / Download; 30-day TTL purge clears bytes while
    #            keeping the row + sha intact. See docs/STORE_GUARDRAILS.md.
    # v33 → v34: drop store_submissions.retry_count — counter mixed LLM
    #            error count + admin rescan count, redundant with audit_log.
    # v34 → v35 (PR #233): store_entities gains 'archived' visibility
    #            state + archived_at + archived_by audit columns. Owner
    #            soft-delete writes 'archived'; existing user_store_installs
    #            keep serving the bundle through marketplace.zip / .git.
    #            Hard delete (DELETE ?hard=true) remains admin-only.
    # v35 → v36 (PR #233 follow-up): re-apply NOT NULL + DEFAULT 'pending'
    #            on store_entities.visibility_status. Lost in the v34→v35
    #            column rebuild. Without this, an INSERT that omits the
    #            column lands NULL → repo reads None → undefined behavior
    #            in the visibility gates. Value-list invariant remains
    #            enforced application-side (DuckDB ADD CHECK on existing
    #            column not supported).
    # v36 → v37: curated marketplace enrichment from
    #            `.claude-plugin/marketplace-metadata.json` plus mandatory curator
    #            identity on marketplace_registry. Adds curator_name +
    #            curator_email to marketplace_registry, and
    #            cover_photo_url + video_url + doc_links to
    #            marketplace_plugins.
    # v37 → v38: flea-market edit feature with version
    #            history. Adds store_entities.version_no INTEGER and
    #            version_history JSON. Each new bundle upload via
    #            PUT bumps version_no and appends to version_history;
    #            metadata-only edits don't bump. Existing rows backfill
    #            to version_no=1 with a single-entry history seeded
    #            from the row's current `version` (hash). Bundle bytes
    #            for each version live on disk under
    #            ${DATA_DIR}/store/<id>/versions/v<N>/plugin/.
    # v38 → v39: system plugin tier — admin-toggleable mandatory plugin
    #            set. Adds marketplace_plugins.is_system BOOLEAN DEFAULT
    #            FALSE. The flag drives a fanout that materializes
    #            resource_grants + user_plugin_optouts rows for every
    #            existing user_groups + users row, so the resolver's
    #            existing (rbac ∩ subscriptions) computation naturally
    #            pulls system plugins into every user's stack. UI then
    #            locks the corresponding controls so users can't
    #            unsubscribe and admins can't revoke per-group grants.
    # v39 → v40: persistent BigQuery metadata cache. Adds
    #            bq_metadata_cache(table_id PK, rows, size_bytes,
    #            partition_by, clustered_by, refreshed_at, error_at,
    #            error_msg).
    # v40 → v41: Activity Center schema — audit_log gains params_before
    #            (JSON), client_ip (VARCHAR), client_kind (VARCHAR),
    #            correlation_id (VARCHAR). Three indices on (timestamp),
    #            (user_id, timestamp), (action, timestamp).
    # v41 → v42 (this PR): platform telemetry schema — 7 new usage_*
    #            tables: usage_events (per-event log), usage_session_summary
    #            (per-session aggregate), usage_tool_daily + usage_plugin_daily
    #            (daily rollups), usage_attribution_skills/agents/commands
    #            (plugin manifest attribution). 10 indices for fast queries.
    # v42 → v43: user_observability_views — per-user saved
    #            filter combinations backing the unified /admin/activity
    #            page (UNIQUE(user_id, name)). Schema is intentionally
    #            opaque JSON because the UI evolves faster than DB.
    # v43 → v44: homepage status frame backing columns —
    #            users.last_pull_at (per-user manifest fetch timestamp,
    #            bumped by GET /api/sync/manifest) plus four BIGINT token
    #            counters on usage_session_summary (input_tokens,
    #            output_tokens, cache_read_tokens, cache_creation_tokens).
    #            USAGE_PROCESSOR_VERSION simultaneously bumps 1→2 so the
    #            reprocess loop backfills tokens on next tick.
    # v44 → v45: user_id column on usage_session_summary + usage_events
    #            (stable RBAC filter — replaces the unstable email-local-part
    #            ``username`` column) plus matching indices.
    # v45 → v46: per-user opt-out (dismiss) for curated memory
    #            items. New table ``knowledge_item_user_dismissed``
    #            ((user_id, item_id) PK, dismissed_at) + index on user_id
    #            for the EXISTS subquery used by list_items / search /
    #            count_items / bundle. Mandatory items are governance-
    #            protected: the API rejects POSTs against them, and the
    #            SQL filter exempts ``status = 'mandatory'`` so any stale
    #            row from before an item was mandated is silently ignored.
    # v46 → v47: DuckDB FTS BM25 index over knowledge_items(title, content).
    #            Replaces ``ILIKE '%q%'`` ranking-by-insertion-order in
    #            ``KnowledgeRepository.search`` with BM25 relevance scoring.
    #            Migration is soft-fail: a missing fts extension leaves the
    #            DB at v46 (search falls back to ILIKE).
    # v47 → v48 (this PR): marketplace telemetry refactor. Drops 4 legacy
    #            tables (usage_attribution_skills/_agents/_commands,
    #            usage_plugin_daily — all verified empty or derivable).
    #            Adds usage_marketplace_item_daily (per-day fact with
    #            count + distinct_users + error_count) and
    #            usage_marketplace_item_window (sliding-window snapshot,
    #            labels 'last_7d' refreshed every tick, 'last_30d' hourly).
    #            New attribution logic = prefix split on `<plugin>:<local>`
    #            identifier + live lookup against marketplace_plugins /
    #            store_entities — no mapping tables needed.
    # v48 → v49: phase-1 Flea refactor — title, tagline, synthetic_name on
    #            store_entities, backfilled via humanize_name(strip_archive_suffix).
    # v49 → v50: UNIQUE INDEX on store_entities.synthetic_name (canonical
    #            attribution key — rollup keyspace, JSONL prefix, marketplace
    #            bundle naming). Migration pre-checks for duplicates and
    #            raises RuntimeError listing them rather than letting the
    #            CREATE UNIQUE INDEX fail mid-way.
    # v50 → v51: nullable ``table_registry.bq_fqn`` (issue #343) — fully-
    #            qualified BigQuery path that decouples the UX/RBAC
    #            ``bucket`` label from the physical BQ dataset name. Rows
    #            without it fall back to the legacy
    #            bucket+source_table+remote_attach.project path.
    #            Released on main as 0.54.29 (PR #346).
    # v51 → v52: unified stack — Data Packages + Memory Domains. Adds
    #            resource_grants.requirement enum, knowledge_items.is_required
    #            (splitting the status='mandatory' overload), data_packages
    #            + data_package_tables, memory_domains +
    #            knowledge_item_domains junction, and
    #            user_stack_subscriptions for per-user opt-in. Drops the
    #            scalar knowledge_items.domain column. (Originally v49
    #            on the branch; renumbered to v52 on the second merge
    #            with main to make room for main's v51 bq_fqn release.)
    # v52 → v53: cover_image_url on data_packages + memory_domains.
    # v53 → v54: lifecycle status + classification category for /catalog
    #            cards (data_packages adds status + category, memory_domains
    #            adds status only).
    # v54 → v55: per-table docs columns on table_registry — feeds the
    #            /catalog/t/<id> detail page (sample_questions,
    #            things_to_know, pairs_well_with).
    # v55 → v56: recipes table — admin-curated multi-table query templates
    #            surfaced as a third "Recipes" tab on /catalog.
    # v56 → v57: soft-delete columns (``deleted_at TIMESTAMP``) on
    #            data_packages, memory_domains, recipes for the Undo
    #            toast flow.
    # v57 → v58: ``memory_domain_suggestions`` table backs the non-admin
    #            "Suggest a domain" affordance on /corporate-memory's
    #            empty state.
    # v58 → v59: extended-content columns on ``data_packages``
    #            (owner_name, owner_team, tags, long_description,
    #            when_to_use, when_not_to_use, example_questions) +
    #            structured per-table doc columns on ``table_registry``
    #            (grain, platforms, partition_col, history, gotchas) for
    #            the /catalog/p/<slug> rewrite per the extended-
    #            descriptions admin spec. All additive + NULLABLE.
    # v59 → v60: backfill ``usage_events.username`` and
    #            ``usage_session_summary.username`` from ``users.email``
    #            where ``user_id`` is non-null. Collapses the admin
    #            telemetry dropdown which previously listed the same
    #            user under multiple identities (email from REST writers,
    #            UUID from upload-API sessions, OS-username from the
    #            legacy collector).
    # v60 → v61: ``cli_auth_codes`` table (browser-loopback login).
    # v61 → v62: per-type FK columns on ``resource_grants`` (PR #455).
    # v62 → v63: ``setup_tokens`` table for Agnes Cowork one-click setup.
    # v63 → v64: ``mcp_sources``, ``tool_registry``, ``tool_grants``
    #            for Universal MCP inbound connector (RFC #461).
    # v63 → v64: ``mcp_secrets`` shared vault for MCP source auth.
    # v64 → v65: ``mcp_user_secrets`` per-user vault.
    # v65 → v66: ``data_package_tools`` junction.
    # v67 → v68: cloud chat tables — chat_sessions, chat_messages,
    #            user_workdirs + two regular indexes.
    # v68 → v69: mcp_sources.env — per-source non-secret env vars for
    #            stdio MCP sources.
    # v69 → v70: live co-drive foundation — chat_session_participants +
    #            is_co_session/ephemeral/sender_email.
    # v77 → v78: built-in marketplace — is_builtin on marketplace_registry,
    #            admin_disabled on marketplace_plugins.
    # v81 → v82: collections (file_corpora / corpus_files / corpus_chunks).
    # v88 → v89: knowledge_digests table — maintained digests (K4, #799).
    # v94 → v95: drop usage_session_summary's 3 secondary indexes
    #            (idx_usage_session_user, idx_usage_session_started,
    #            idx_usage_session_user_id) — a corrupt entry in one of
    #            them, rewritten on every usage session-processor tick,
    #            was invalidating the whole DuckDB connection.
    assert SCHEMA_VERSION >= 80


def test_v37_marketplace_curator_columns(tmp_path):
    """Fresh install reaches the current schema with the v37 marketplace
    columns present."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    registry_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_registry'"
        ).fetchall()
    }
    assert {"curator_name", "curator_email"} <= registry_cols, (
        f"curator columns missing from marketplace_registry: {registry_cols}"
    )

    plugin_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert {"cover_photo_url", "video_url", "doc_links"} <= plugin_cols, (
        f"enrichment columns missing from marketplace_plugins: {plugin_cols}"
    )
    conn.close()


def test_v36_db_migrates_to_current(tmp_path):
    """Pre-existing v36 DB upgrades cleanly through v37 (curator
    enrichment) and v38 (flea edit version history) without losing
    existing rows."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up a minimal v36-shape registry + plugin row, plus the
    # schema_version row that pins us to 36.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (36)")
    conn.execute("""CREATE TABLE marketplace_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        url VARCHAR NOT NULL, branch VARCHAR, token_env VARCHAR,
        description TEXT, registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp,
        last_synced_at TIMESTAMP, last_commit_sha VARCHAR, last_error TEXT
    )""")
    conn.execute("""CREATE TABLE marketplace_plugins (
        marketplace_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
        description TEXT, version VARCHAR, author_name VARCHAR,
        homepage VARCHAR, category VARCHAR, source_type VARCHAR,
        source_spec JSON, raw JSON,
        created_at TIMESTAMP DEFAULT current_timestamp,
        updated_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (marketplace_id, name)
    )""")
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES ('legacy', 'Legacy', 'https://example.com/repo.git')"
    )
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('legacy', 'foo')")

    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    # v37 enrichment columns exist; existing rows preserved with NULL.
    row = conn.execute("SELECT curator_name, curator_email FROM marketplace_registry WHERE id = 'legacy'").fetchone()
    assert row == (None, None)

    row = conn.execute(
        "SELECT cover_photo_url, video_url, doc_links FROM marketplace_plugins "
        "WHERE marketplace_id = 'legacy' AND name = 'foo'"
    ).fetchone()
    assert row == (None, None, None)
    conn.close()


def test_v39_adds_marketplace_plugins_is_system(tmp_path):
    """Fresh install reaches the current schema with the v39 is_system
    column on marketplace_plugins. Default value is FALSE (not NULL) so
    the fanout helpers don't need to special-case absent rows."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert "is_system" in cols, f"is_system missing from {cols}"

    # New rows default to FALSE — required so a freshly-synced plugin
    # doesn't accidentally land in everyone's stack.
    conn.execute("INSERT INTO marketplace_registry (id, name, url) VALUES ('m', 'M', 'https://example.com/repo.git')")
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('m', 'p')")
    row = conn.execute("SELECT is_system FROM marketplace_plugins WHERE marketplace_id = 'm' AND name = 'p'").fetchone()
    assert row[0] is False, f"new plugin defaulted to {row[0]!r}, expected False"
    conn.close()


def test_v38_db_migrates_to_v39(tmp_path):
    """Pre-existing v38 DB upgrades to v39 cleanly — adds is_system
    column, existing rows backfill to FALSE, schema_version updates."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up the v38 minimal shape: schema_version row + the two
    # marketplace tables + a pre-existing plugin row that must survive
    # the migration with is_system = FALSE.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (38)")
    conn.execute("""CREATE TABLE marketplace_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        url VARCHAR NOT NULL, branch VARCHAR, token_env VARCHAR,
        description TEXT, registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp,
        last_synced_at TIMESTAMP, last_commit_sha VARCHAR, last_error TEXT,
        curator_name VARCHAR, curator_email VARCHAR
    )""")
    conn.execute("""CREATE TABLE marketplace_plugins (
        marketplace_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
        description TEXT, version VARCHAR, author_name VARCHAR,
        homepage VARCHAR, category VARCHAR, source_type VARCHAR,
        source_spec JSON, raw JSON,
        created_at TIMESTAMP DEFAULT current_timestamp,
        updated_at TIMESTAMP DEFAULT current_timestamp,
        cover_photo_url VARCHAR, video_url VARCHAR, doc_links JSON,
        PRIMARY KEY (marketplace_id, name)
    )""")
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES ('legacy', 'Legacy', 'https://example.com/repo.git')"
    )
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('legacy', 'foo')")

    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert "is_system" in cols

    # Existing pre-v39 row backfilled to FALSE — no plugin lands in
    # everyone's stack just because we ran the migration.
    row = conn.execute(
        "SELECT is_system FROM marketplace_plugins WHERE marketplace_id = 'legacy' AND name = 'foo'"
    ).fetchone()
    assert row[0] is False, f"pre-existing row backfilled to {row[0]!r}"
    conn.close()


def test_v20_adds_source_query(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols, f"source_query missing from {cols}"
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_claude_md_template_seeded_in_instance_templates(tmp_path):
    """v23 introduced claude_md_template as a singleton table; v28 consolidates
    it into instance_templates keyed 'claude_md'. Post-v28 the legacy table is
    dropped — the canonical lookup is `instance_templates WHERE key='claude_md'`.

    See tests/test_v28_migration.py for the migration path coverage. This test
    just verifies the seeded row is present on a fresh install.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }
    assert "instance_templates" in tables
    assert "claude_md_template" not in tables, "claude_md_template should be consolidated away post-v28"

    row = conn.execute("SELECT key, content FROM instance_templates WHERE key = 'claude_md'").fetchone()
    assert row is not None
    assert row[0] == "claude_md"
    assert row[1] is None  # default = no override
    conn.close()


def test_v19_db_migrates_to_v20(tmp_path):
    """Pre-existing v19 DB (post-RBAC-drop) without source_query upgrades
    cleanly without losing data."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Simulate a v19 DB at minimal but realistic shape: schema_version row +
    # a table_registry row in the post-v19 column shape (no is_public column,
    # since v19 finalize dropped it via the table-rebuild idiom).
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (19)")
    conn.execute("""CREATE TABLE table_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        source_type VARCHAR, bucket VARCHAR, source_table VARCHAR,
        sync_strategy VARCHAR DEFAULT 'full_refresh',
        query_mode VARCHAR DEFAULT 'local',
        sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true,
        primary_key VARCHAR, folder VARCHAR, description TEXT,
        registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp
    )""")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('foo', 'foo')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION  # bumped 19→28 forward
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols
    # Existing row preserved, new column NULL
    row = conn.execute("SELECT id, source_query FROM table_registry WHERE id='foo'").fetchone()
    assert row == ("foo", None)
    conn.close()


def _make_v34_store_entities(conn):
    """Build a minimal v34-shape store_entities table for v34→v35 path tests.

    Only includes the columns the v34→v35 migration touches; the rest of
    the schema isn't needed because the function operates only on
    store_entities's column set.
    """
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            visibility_status VARCHAR DEFAULT 'pending'
        )
    """)
    conn.execute(
        "INSERT INTO store_entities (id, visibility_status) VALUES ('a', 'approved'), ('b', 'pending'), ('c', 'hidden')"
    )


def test_v34_to_v35_clean_path_rebuilds_visibility_column(tmp_path):
    """Standard v34 → v35 path: ``visibility_status`` is present, no temp
    column. Migration rebuilds the column without the legacy CHECK so
    'archived' becomes a valid value, preserves all row values, and adds
    the audit columns.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _make_v34_store_entities(conn)

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols, "temp column must be cleaned up"
    assert "archived_at" in cols
    assert "archived_by" in cols

    rows = dict(conn.execute("SELECT id, visibility_status FROM store_entities ORDER BY id").fetchall())
    assert rows == {"a": "approved", "b": "pending", "c": "hidden"}, f"row values must survive the rebuild: {rows}"
    conn.close()


def test_v34_to_v35_recovers_from_partial_rebuild_missing_visibility(tmp_path):
    """Partial-rebuild recovery: a previous migration attempt completed
    steps 3-5 (added _vis_v35, copied values, dropped visibility_status)
    but failed before step 6 (RENAME). Subsequent restarts hit
    DROP visibility_status (no IF EXISTS guard) and looped on the same
    error, leaving the DB stranded with schema_version stuck pre-v35.

    The new code detects this state — _vis_v35 present, visibility_status
    absent — and finishes the rebuild with the RENAME alone instead of
    re-running the full destructive sequence.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    # Hand-build the broken state: store_entities with _vis_v35 instead of
    # visibility_status, populated with the canonical values.
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO store_entities (id, _vis_v35) VALUES ('a', 'approved'), ('b', 'pending'), ('c', 'hidden')"
    )

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols
    assert "archived_at" in cols
    assert "archived_by" in cols

    rows = dict(conn.execute("SELECT id, visibility_status FROM store_entities ORDER BY id").fetchall())
    assert rows == {"a": "approved", "b": "pending", "c": "hidden"}, (
        f"row values must come back via RENAME, not be lost: {rows}"
    )
    conn.close()


def test_v34_to_v35_recovers_from_partial_rebuild_both_columns(tmp_path):
    """Edge state: a prior attempt aborted before the DROP, leaving both
    visibility_status (canonical) and _vis_v35 (temp) on the table.
    The recovery path drops _vis_v35 and keeps visibility_status — the
    rest of the schema expects that name.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            visibility_status VARCHAR,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute("INSERT INTO store_entities (id, visibility_status, _vis_v35) VALUES ('a', 'approved', 'approved')")

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols, "temp column must be dropped"

    row = conn.execute("SELECT id, visibility_status FROM store_entities WHERE id = 'a'").fetchone()
    assert row == ("a", "approved")
    conn.close()


def test_v32_db_with_partial_v35_recovers_through_full_ladder(tmp_path):
    """End-to-end: a DB stranded at schema_version=32 with the half-applied
    v34→v35 state (visibility_status dropped, _vis_v35 left behind) must
    upgrade cleanly through the full ladder when ``_ensure_schema`` runs.

    This is the production scenario observed in operator instances after
    the original list-form ``_V34_TO_V35_MIGRATIONS`` failed mid-run on
    a fresh restart.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up the broken state. We only need enough of the schema for the
    # migration ladder to run — ``_ensure_schema`` will create the rest
    # via ``_SYSTEM_SCHEMA``'s IF NOT EXISTS guards.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (32)")
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            owner_user_id VARCHAR,
            owner_username VARCHAR,
            type VARCHAR,
            name VARCHAR,
            archived_at TIMESTAMP,
            archived_by VARCHAR,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute("INSERT INTO store_entities (id, type, name, _vis_v35) VALUES ('a', 'skill', 'alpha', 'approved')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols
    # Existing row preserved, value carried over from _vis_v35.
    row = conn.execute("SELECT id, visibility_status FROM store_entities WHERE id = 'a'").fetchone()
    assert row == ("a", "approved")
    conn.close()


def test_v35_to_v36_reapplies_visibility_constraints(tmp_path):
    """v34→v35 dropped NOT NULL + DEFAULT when rebuilding the column to
    drop the legacy CHECK; v35→v36 re-applies them. Verifies that on a
    freshly migrated DB, an INSERT omitting visibility_status either
    inherits the default 'pending' or fails — never lands NULL.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = conn.execute(
        "SELECT column_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'store_entities' "
        "  AND column_name = 'visibility_status'"
    ).fetchall()
    assert cols, "visibility_status column missing from store_entities"
    name, is_nullable, default_expr = cols[0]
    assert is_nullable == "NO", f"visibility_status must be NOT NULL after v36; got is_nullable={is_nullable!r}"
    # DuckDB renders the default as a quoted literal — match either form.
    assert default_expr is not None, "visibility_status DEFAULT must be set"
    assert "pending" in str(default_expr).lower(), f"visibility_status DEFAULT must be 'pending'; got {default_expr!r}"

    conn.close()


def test_v70_copresence_columns_and_table(tmp_path):
    """Fresh install reaches v70 with co-presence additions on chat tables
    and the chat_session_participants table present."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    sess_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_sessions'"
        ).fetchall()
    }
    assert "is_co_session" in sess_cols, f"is_co_session missing from chat_sessions: {sess_cols}"
    assert "ephemeral" in sess_cols, f"ephemeral missing from chat_sessions: {sess_cols}"

    msg_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_messages'"
        ).fetchall()
    }
    assert "sender_email" in msg_cols, f"sender_email missing from chat_messages: {msg_cols}"

    tables = {
        r[0]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }
    assert "chat_session_participants" in tables, f"chat_session_participants table missing: {tables}"

    conn.close()


def test_v69_to_v70_migration(tmp_path):
    """A DB at v69 (post-MCP-env, pre-co-presence) upgrades cleanly to v70."""
    from src.db import _v69_to_v70

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Minimal v69 shape: chat_sessions + chat_messages without co-presence cols.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (69)")
    conn.execute("""
        CREATE TABLE chat_sessions (
            id VARCHAR PRIMARY KEY,
            user_email VARCHAR NOT NULL,
            surface VARCHAR NOT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            archived BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    conn.execute("""
        CREATE TABLE chat_messages (
            id VARCHAR PRIMARY KEY,
            session_id VARCHAR NOT NULL,
            role VARCHAR NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    _v69_to_v70(conn)

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 70

    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()}
    assert "is_co_session" in sess_cols
    assert "ephemeral" in sess_cols

    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_messages')").fetchall()}
    assert "sender_email" in msg_cols

    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert "chat_session_participants" in tables

    conn.close()


def test_v78_builtin_marketplace_columns(tmp_path):
    """Fresh install reaches v78 with is_builtin on marketplace_registry and
    admin_disabled on marketplace_plugins. Both default to FALSE so existing rows
    and freshly-registered admin marketplaces are unaffected."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    reg_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_registry'"
        ).fetchall()
    }
    assert "is_builtin" in reg_cols, f"is_builtin missing from marketplace_registry: {reg_cols}"

    plugin_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert "admin_disabled" in plugin_cols, f"admin_disabled missing from marketplace_plugins: {plugin_cols}"

    # New admin-registered rows default to is_builtin=FALSE.
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) "
        "VALUES ('admin-reg', 'Admin Reg', 'https://example.com/reg.git')"
    )
    row = conn.execute("SELECT is_builtin FROM marketplace_registry WHERE id = 'admin-reg'").fetchone()
    assert row[0] is False, f"admin row defaulted to is_builtin={row[0]!r}"

    # New plugin rows default to admin_disabled=FALSE.
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('admin-reg', 'plug')")
    row = conn.execute(
        "SELECT admin_disabled FROM marketplace_plugins WHERE marketplace_id = 'admin-reg' AND name = 'plug'"
    ).fetchone()
    assert row[0] is False, f"new plugin defaulted to admin_disabled={row[0]!r}"

    conn.close()


def test_v77_to_v78_migration(tmp_path):
    """A DB at v77 upgrades cleanly to v78 — adds is_builtin + admin_disabled,
    existing rows survive with both columns defaulting to FALSE."""
    from src.db import _v77_to_v78

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (77)")
    conn.execute("""
        CREATE TABLE marketplace_registry (
            id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
            url VARCHAR NOT NULL, curator_name VARCHAR, curator_email VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE marketplace_plugins (
            marketplace_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
            is_system BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (marketplace_id, name)
        )
    """)
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES ('old', 'Old', 'https://example.com/old.git')"
    )
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('old', 'foo')")

    _v77_to_v78(conn)

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 78

    reg_row = conn.execute("SELECT is_builtin FROM marketplace_registry WHERE id = 'old'").fetchone()
    assert reg_row[0] is False, f"pre-existing registry row got is_builtin={reg_row[0]!r}"

    plug_row = conn.execute(
        "SELECT admin_disabled FROM marketplace_plugins WHERE marketplace_id = 'old' AND name = 'foo'"
    ).fetchone()
    assert plug_row[0] is False, f"pre-existing plugin row got admin_disabled={plug_row[0]!r}"


def test_v89_knowledge_digests_table(tmp_path):
    """v89 (K4, #799): knowledge_digests exists on fresh installs, ladder is
    idempotent, and schema_version lands at SCHEMA_VERSION."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('knowledge_digests')").fetchall()}
    assert {
        "id",
        "slug",
        "title",
        "instructions",
        "source_corpus_ids",
        "output_md",
        "source_fingerprint",
        "generated_at",
        "model",
        "status",
        "status_reason",
    } <= cols
    assert get_schema_version(conn) == SCHEMA_VERSION

    # idempotency — re-running the step must not raise
    from src.db import _v88_to_v89

    _v88_to_v89(conn)
    conn.close()


def test_v90_chat_broker_tickets_table(tmp_path):
    """v90 (#849): chat_broker_tickets exists on fresh installs, the ladder is
    idempotent, and schema_version lands at SCHEMA_VERSION."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_broker_tickets')").fetchall()}
    assert {"token", "session_id", "scope", "expires_at", "created_at"} <= cols
    assert get_schema_version(conn) == SCHEMA_VERSION

    # idempotency — re-running the step must not raise
    from src.db import _v89_to_v90

    _v89_to_v90(conn)
    conn.close()


def test_v95_fresh_install_has_no_usage_session_summary_secondary_indexes(tmp_path):
    """v95 (index-corruption hotfix): a fresh install never creates
    idx_usage_session_user / idx_usage_session_started / idx_usage_session_user_id
    on usage_session_summary, and the migration step is idempotent."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    idx_names = {
        r[0]
        for r in conn.execute(
            "SELECT index_name FROM duckdb_indexes WHERE table_name='usage_session_summary'"
        ).fetchall()
    }
    assert idx_names == set(), f"usage_session_summary must have no secondary indexes, found {idx_names}"

    # idempotency — re-running the step must not raise
    from src.db import _v94_to_v95

    _v94_to_v95(conn)
    conn.close()


def test_v94_db_with_indexes_upgrades_to_v95_and_drops_them(tmp_path):
    """A pre-v95 DB that still carries the 3 secondary indexes (the state a
    live instance was in before this hotfix) climbs to v95, loses the
    indexes, and keeps the session_file PRIMARY KEY plus existing rows
    intact."""
    db_path = tmp_path / "v94.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    # Force back to the pre-v95 state that still has the 3 indexes.
    conn.execute("UPDATE schema_version SET version = 94")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_user ON usage_session_summary(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_started ON usage_session_summary(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_user_id ON usage_session_summary(user_id)")
    conn.execute(
        "INSERT INTO usage_session_summary (session_file, session_id, username, processor_version) "
        "VALUES ('s/keep.jsonl', 's1', 'keeper', 1)"
    )
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    idx_names = {
        r[0]
        for r in conn.execute(
            "SELECT index_name FROM duckdb_indexes WHERE table_name='usage_session_summary'"
        ).fetchall()
    }
    assert idx_names == set(), f"upgrade must drop all 3 indexes, found {idx_names}"
    row = conn.execute("SELECT username FROM usage_session_summary WHERE session_file='s/keep.jsonl'").fetchone()
    assert row == ("keeper",)
    conn.close()


def test_v98_chat_sessions_relay_protocol_version_column(tmp_path):
    """v98 (Tier 1 restart-invariant sandbox reuse): a fresh install carries
    ``chat_sessions.relay_protocol_version``, and the migration step is
    idempotent."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()}
    assert "relay_protocol_version" in cols
    assert get_schema_version(conn) == SCHEMA_VERSION

    # idempotency — re-running the step must not raise
    from src.db import _v97_to_v98

    _v97_to_v98(conn)
    conn.close()


def test_v97_db_upgrades_to_v98(tmp_path):
    """A DB pinned at v97 (a live instance's state before this migration)
    climbs to v98 via the upgrade-block dispatch, keeping existing rows
    intact with ``relay_protocol_version`` reading back NULL (unknown/
    legacy) — DuckDB cannot DROP a column with FK-dependents (chat_messages
    references chat_sessions), so this exercises the ``current < 98``
    dispatch + idempotent guarded ALTER directly rather than simulating a
    column-free table."""
    db_path = tmp_path / "v97.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 97")
    conn.execute(
        "INSERT INTO chat_sessions (id, user_email, surface, started_at, last_message_at, message_count, archived) "
        "VALUES ('chat_keep', 'keeper@example.com', 'web', current_timestamp, NULL, 0, FALSE)"
    )
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()}
    assert "relay_protocol_version" in cols
    row = conn.execute("SELECT relay_protocol_version FROM chat_sessions WHERE id = 'chat_keep'").fetchone()
    assert row == (None,)
    conn.close()


def test_v99_db_migrates_to_v100_adds_sync_state_parts(tmp_path):
    """A v99 DB whose ``sync_state`` predates the ``parts`` column upgrades
    to v100 via ``_v99_to_v100``, adding ``parts`` (NULL on existing rows)
    without losing data. ``parts`` has no FK dependents, so this simulates a
    genuinely column-free table by dropping it."""
    db_path = tmp_path / "v99.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    # Simulate the pre-v100 shape: sync_state without `parts`, version 99.
    conn.execute("ALTER TABLE sync_state DROP COLUMN parts")
    conn.execute("UPDATE schema_version SET version = 99")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('keep', 7, 'h', 'ok')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {r[1] for r in conn.execute("PRAGMA table_info('sync_state')").fetchall()}
    assert "parts" in cols
    row = conn.execute("SELECT rows, parts FROM sync_state WHERE table_id = 'keep'").fetchone()
    assert row == (7, None)  # data preserved, parts NULL on the legacy row
    conn.close()


# Columns stranded by the paper-theme ladder renumbering, and the step each one
# is normally added by. A DB that climbed the branch's OLD numbering is stamped
# past these version numbers, so every one of these steps is skipped forever.
_STRANDED = {
    # relay_protocol_version: _v97_to_v98's main-side half, dropped on DBs that
    # climbed the branch ladder (its _v97_to_v98 replaced the body wholesale).
    "chat_sessions": ["agent_id", "relay_protocol_version"],  # _v100_to_v101, _v97_to_v98
    "personal_access_tokens": ["agent_id", "surface"],  # _v100_to_v101, _v105_to_v106
    "sync_state": ["parts"],  # _v99_to_v100
    "data_apps": [  # _v98_to_v99 + _v107_to_v108
        "parent_app_id",
        "is_draft",
        "draft_branch",
        "external_url",
        "source_ref",
        "managed",
        "description_override",
    ],
}


def _strand(conn):
    """Rewind a fresh DB to the stranded shape: the ``_STRANDED`` columns gone,
    everything else intact.

    DuckDB refuses to drop a column while an index or an inbound FOREIGN KEY
    references the table, so the dependents are parked and replayed from their
    own DDL. (``ADD COLUMN`` has no such restriction — which is why the heal
    itself works on a live DB carrying all of them.)
    """
    targets = tuple(_STRANDED)
    placeholders = ", ".join("?" for _ in targets)

    index_sql = [
        r[0]
        for r in conn.execute(
            f"SELECT sql FROM duckdb_indexes() WHERE table_name IN ({placeholders})", list(targets)
        ).fetchall()
    ]
    index_names = [
        r[0]
        for r in conn.execute(
            f"SELECT index_name FROM duckdb_indexes() WHERE table_name IN ({placeholders})", list(targets)
        ).fetchall()
    ]
    # Tables holding an FK into any target, discovered rather than hardcoded so
    # this keeps working as the schema grows.
    dependents = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT table_name FROM duckdb_constraints() "
            "WHERE constraint_type = 'FOREIGN KEY' AND ("
            + " OR ".join(f"constraint_text ILIKE '%REFERENCES {t}(%'" for t in targets)
            + ")"
        ).fetchall()
    ]
    dependent_sql = (
        [
            r[0]
            for r in conn.execute(
                f"SELECT sql FROM duckdb_tables() WHERE table_name IN ({', '.join('?' for _ in dependents)})",
                dependents,
            ).fetchall()
        ]
        if dependents
        else []
    )

    for name in index_names:
        conn.execute(f"DROP INDEX {name}")
    for name in dependents:
        conn.execute(f"DROP TABLE {name}")
    for table, columns in _STRANDED.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for sql in dependent_sql + index_sql:
        conn.execute(sql)


def test_v114_db_stranded_by_renumbering_is_healed(tmp_path):
    """A DB stamped at the head under the paper-theme branch's OLD step numbering is
    missing every column added by the main-side steps that renumbering shifted
    underneath it — most visibly ``chat_sessions.agent_id``, which 500s every
    chat read and write with ``Binder Error: ... agent_id``.

    Reproduces the shape observed on a live preview instance: version already at
    the head, so no `if current < N` guard fires and the columns never land —
    which is why ``_heal_stranded_ladder_columns`` checks for the columns instead
    of trusting the stamp.
    """
    db_path = tmp_path / "stranded.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _strand(conn)
    # Stamped at the head under the old numbering — the whole point of the bug.
    conn.execute("UPDATE schema_version SET version = 114")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('keep', 7, 'h', 'ok')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    for table, columns in _STRANDED.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        assert set(columns) <= cols, f"{table} still missing {set(columns) - cols}"

    # Pre-existing rows survive the heal, and `managed` is backfilled rather
    # than left NULL (it is NOT NULL in the fresh-install DDL).
    assert conn.execute("SELECT rows FROM sync_state WHERE table_id = 'keep'").fetchone() == (7,)
    assert conn.execute("SELECT count(*) FROM data_apps WHERE managed IS NULL").fetchone() == (0,)
    conn.close()


def test_v114_heal_is_idempotent_on_healthy_db(tmp_path):
    """The heal must be a no-op for DBs that climbed the ladder cleanly — it
    runs on every instance that upgrades past 114, not just the stranded ones.
    """
    db_path = tmp_path / "healthy.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 114")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    for table, columns in _STRANDED.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        assert set(columns) <= cols
    conn.close()


def test_v114_heal_lets_a_stranded_db_mint_tokens_again(tmp_path):
    """`personal_access_tokens.surface` (_v105_to_v106) is stranded by the same
    renumbering, and every PAT mint names it — including the CLI sign-in
    exchange. Healing chat but not this would fix the browser and leave the
    operator locked out of the CLI (Devin Review on #1158)."""
    db_path = tmp_path / "stranded_pat.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _strand(conn)
    conn.execute("UPDATE schema_version SET version = 114")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('personal_access_tokens')").fetchall()}
    assert "surface" in cols, "a stranded DB still cannot mint a token"
    # the column is usable, not merely present
    conn.execute(
        "INSERT INTO personal_access_tokens (id, user_id, token_hash, prefix, name, surface) "
        "VALUES ('t1', 'u1', 'h', 'agn_', 'cli', 'stack')"
    )
    assert conn.execute("SELECT surface FROM personal_access_tokens WHERE id='t1'").fetchone() == ("stack",)
    conn.close()


def test_v114_heal_flushes_its_ddl_to_the_main_db_file(tmp_path):
    """The post-migration CHECKPOINT sits inside `if current < SCHEMA_VERSION`,
    and a stranded DB is stamped AT the head — so on exactly the databases these
    heals exist for it never runs, and their ALTER TABLE ... ADD COLUMN
    statements would sit in the WAL. That is the shape the migration checkpoint
    documents as able to leave system.duckdb unrecoverable on a cross-version
    WAL replay after an abrupt restart (Devin Review on #1158).

    Asserted by reading the healed DB from a SECOND connection after an
    unclean-looking handoff: what is visible there came from the main file.
    """
    db_path = tmp_path / "stranded_wal.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _strand(conn)
    conn.execute("UPDATE schema_version SET version = 114")
    conn.close()

    healer = duckdb.connect(str(db_path))
    _ensure_schema(healer)  # heals + checkpoints
    wal = db_path.with_suffix(".duckdb.wal")
    healer.close()

    reader = duckdb.connect(str(db_path))
    for table, columns in _STRANDED.items():
        cols = {r[1] for r in reader.execute(f"PRAGMA table_info('{table}')").fetchall()}
        assert set(columns) <= cols, f"{table} lost {set(columns) - cols} — heal DDL was not durable"
    reader.close()
    assert not wal.exists() or wal.stat().st_size == 0, "WAL still holds the heal DDL after the checkpoint"


def test_every_stranded_column_is_covered_by_some_heal():
    """Derives the repair list from the ladder instead of trusting it.

    The renumbering strands every column added by a step in the v97..v113
    window. `stranded` is hand-maintained, and it drifted twice — first missing
    `personal_access_tokens.surface`, then three more — each time shipping a
    repair that fixed one screen and left another broken. This asserts the
    comparison rather than the contents: any future step in that window adding
    a column neither heal covers fails here (Devin Review on #1158).
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "db.py").read_text()

    adds: dict[tuple[str, str], list[str]] = {}
    for m in re.finditer(r"^def _v(\d+)_to_v(\d+)\(conn.*?(?=^def )", src, re.S | re.M):
        lo = int(m.group(1))
        if not 97 <= lo <= 113:
            continue
        for table, col in re.findall(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+(\w+)", m.group(0)):
            adds.setdefault((table, col), []).append(f"_v{m.group(1)}_to_v{m.group(2)}")

    # Strip comments first: a tuple surviving only inside a `# …` line must
    # count as UNdeclared, or deleting the real entry while keeping its
    # commented tombstone silently satisfies this guard.
    _block = src.split("stranded = [")[1].split("\n    ]")[0]
    _block = re.sub(r"#[^\n]*", "", _block)
    declared = {(t, c) for t, c in re.findall(r'\(\s*"(\w+)",\s*"(\w+)",\s*"[^"]*"\s*\)', _block)}
    # agents is rebuilt wholesale by _heal_legacy_agents_table from the
    # canonical DDL, so its columns need no entry in `stranded`.
    uncovered = {k: v for k, v in adds.items() if k not in declared and k[0] != "agents"}
    assert not uncovered, "steps add columns no heal repairs: " + ", ".join(
        f"{t}.{c} ({'/'.join(w)})" for (t, c), w in sorted(uncovered.items())
    )


def test_the_agents_exemption_is_real():
    """Pins the exemption above: a fresh DB's agents table must actually carry
    the columns those steps ALTER in, or the exemption is hiding a gap."""
    import duckdb as _d

    conn = _d.connect(":memory:")
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('agents')").fetchall()}
    assert {"greeting", "knowledge", "plugins", "role", "status", "surfaces", "tone"} <= cols
    conn.close()


def _insert_agent(conn, *, id, slug, status, is_default=False, name="Agent"):
    conn.execute(
        "INSERT INTO agents (id, owner_user_id, name, slug, status, is_default, created_at, updated_at) "
        "VALUES (?, 'u1', ?, ?, ?, ?, current_timestamp, current_timestamp)",
        [id, name, slug, status, is_default],
    )


def test_v115_reclassifies_pre_existing_governance_agents_as_ready(tmp_path):
    """v114→v115: a `draft` agent whose `id` does NOT carry the builder's
    `agt_` prefix and is not the seeded default agent must have arrived
    through the governance API (nothing else could have created it — see
    `_v114_to_v115`'s docstring) — reclassified to `ready` so the
    draft-rename rule (app/api/agents.py::_draft_slug_rename) freezes its
    address. Every `agt_`-prefixed builder row and the seeded default agent
    are left alone, regardless of slug — including a builder draft the user
    already named, which is the case a slug-only discriminator got wrong.
    See `test_v114_to_v115_promotes_by_id_prefix_not_slug` for that
    regression pinned in isolation.
    """
    db_path = tmp_path / "v114.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 114")
    _insert_agent(conn, id="gov1", slug="revenue-bot", status="draft", name="Revenue Bot")
    _insert_agent(conn, id="agt_builder1", slug="agent", status="draft", name="")
    _insert_agent(conn, id="agt_builder2", slug="agent-2", status="draft", name="")
    _insert_agent(conn, id="agt_named1", slug="finance-bot", status="draft", name="Finance Bot")
    _insert_agent(conn, id="def1", slug="default", status="draft", is_default=True, name="Default")
    _insert_agent(conn, id="ready1", slug="already-ready", status="ready", name="Already Ready")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    rows = dict(
        conn.execute(
            "SELECT id, status FROM agents WHERE id IN "
            "('gov1', 'agt_builder1', 'agt_builder2', 'agt_named1', 'def1', 'ready1')"
        ).fetchall()
    )
    assert rows["gov1"] == "ready", "a governance-created agent's slug is deliberately chosen — freeze it"
    assert rows["agt_builder1"] == "draft", "an unnamed builder placeholder must keep re-deriving its slug"
    assert rows["agt_builder2"] == "draft", "…including a suffixed placeholder"
    assert rows["agt_named1"] == "draft", (
        "a builder draft already named by the user is still unpublished — a slug-based "
        "discriminator would wrongly freeze it because 'finance-bot' isn't placeholder-shaped"
    )
    assert rows["def1"] == "draft", "the seeded default agent is a PERMANENT draft by design"
    assert rows["ready1"] == "ready", "already-ready rows are untouched"
    conn.close()


def test_v114_to_v115_promotes_by_id_prefix_not_slug(tmp_path):
    """Regression pin: a builder-created draft (`agt_`-prefixed id) that the
    user already gave a real, non-placeholder-shaped name/slug — e.g.
    "Finance Bot" -> `finance-bot` — must stay `draft`. It is not yet
    published: `_draft_slug_rename` (app/api/agents.py) only freezes a
    draft's slug once `status` reaches `'ready'`, so promoting it here would
    permanently freeze an address for an agent nobody ever marked ready. A
    discriminator keyed off the slug's shape (matching only the unnamed
    placeholder lineage `agent` / `agent-N`) gets this wrong — the `id`
    prefix is what actually distinguishes a builder row from a
    governance/default one, in every naming state.
    """
    from src.db import _v114_to_v115

    db_path = tmp_path / "named_draft.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _insert_agent(conn, id="agt_finance1", slug="finance-bot", status="draft", name="Finance Bot")

    _v114_to_v115(conn)

    assert conn.execute("SELECT status FROM agents WHERE id = 'agt_finance1'").fetchone()[0] == "draft"
    conn.close()


def test_v114_to_v115_is_idempotent(tmp_path):
    """Re-running the step (as a fresh install's ladder walk does) must not
    raise and must not un-freeze an already-migrated row."""
    from src.db import _v114_to_v115

    db_path = tmp_path / "idempotent.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _insert_agent(conn, id="gov1", slug="revenue-bot", status="draft", name="Revenue Bot")

    _v114_to_v115(conn)
    _v114_to_v115(conn)  # must not raise, must not toggle anything back

    assert conn.execute("SELECT status FROM agents WHERE id = 'gov1'").fetchone()[0] == "ready"
    conn.close()


def test_v114_to_v115_guards_a_legacy_shaped_agents_table(tmp_path):
    """A database built by the pre-merge paper-theme branch reaches this step
    with an `agents` table in that branch's own shape — `created_by` /
    `instructions`, no `is_default` — stamped somewhere in the 10x range (see
    `_heal_legacy_agents_table`, which alone knows how to rebuild it, and
    which runs at the *bottom* of `_ensure_schema`, after the migration
    ladder). Before this step guarded on column existence,
    `UPDATE agents ... is_default ...` raised a DuckDB Binder Error against
    that table and aborted startup before the heal ever got a chance to run.
    """
    db_path = tmp_path / "legacy_agents.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 114")
    conn.execute("DROP TABLE agents")
    conn.execute(
        """
        CREATE TABLE agents (
            id           VARCHAR PRIMARY KEY,
            created_by   VARCHAR NOT NULL,
            name         VARCHAR NOT NULL,
            slug         VARCHAR NOT NULL UNIQUE,
            instructions TEXT,
            role         VARCHAR,
            tone         VARCHAR DEFAULT 'concise',
            greeting     TEXT,
            knowledge    TEXT DEFAULT '[]',
            plugins      TEXT DEFAULT '[]',
            surfaces     TEXT DEFAULT '{}',
            status       VARCHAR DEFAULT 'draft',
            created_at   TIMESTAMP DEFAULT current_timestamp,
            updated_at   TIMESTAMP DEFAULT current_timestamp,
            deleted_at   TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO agents (id, created_by, name, slug, instructions, status, created_at, updated_at) "
        "VALUES ('legacy1', 'u1', 'Legacy Agent', 'revenue-bot', 'be helpful', "
        "'draft', current_timestamp, current_timestamp)"
    )
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)  # must not raise Binder Error: is_default
    assert get_schema_version(conn) == SCHEMA_VERSION

    # _heal_legacy_agents_table runs after the ladder and still gets its
    # chance to rebuild the table onto the canonical shape.
    cols = {r[1] for r in conn.execute("PRAGMA table_info('agents')").fetchall()}
    assert "owner_user_id" in cols, "the legacy table should have been rebuilt onto the canonical shape"
    assert conn.execute("SELECT owner_user_id, slug FROM agents WHERE id = 'legacy1'").fetchone() == (
        "u1",
        "revenue-bot",
    )
    conn.close()


def test_add_column_default_reaches_pre_existing_rows():
    """Pins what the heals may assume about ADD COLUMN ... DEFAULT.

    `src/db.py` asserted both ways: one comment said a pre-existing row reads
    NULL and the default applies only to inserts, while several heals add a
    column with a DEFAULT and no backfill and let read paths filter on the
    value (`data_apps.is_draft = FALSE` hides an app if it reads NULL). Only
    one of those can be true, so it is measured here rather than argued
    (Devin Review on #1158).
    """
    import duckdb as _d

    conn = _d.connect(":memory:")
    conn.execute("CREATE TABLE t (id VARCHAR)")
    conn.execute("INSERT INTO t VALUES ('pre-existing')")
    conn.execute("ALTER TABLE t ADD COLUMN IF NOT EXISTS enum_col VARCHAR DEFAULT 'none'")
    conn.execute("ALTER TABLE t ADD COLUMN flag_col BOOLEAN DEFAULT FALSE")
    conn.execute("ALTER TABLE t ADD COLUMN no_default TIMESTAMP")

    row = conn.execute("SELECT enum_col, flag_col, no_default FROM t WHERE id='pre-existing'").fetchone()
    assert row[0] == "none", "a DEFAULT must reach rows that predate the column"
    assert row[1] is False, "…for BOOLEAN too — is_draft = FALSE filters on it"
    assert row[2] is None, "…and a column with no DEFAULT still reads NULL"
    conn.close()


def test_v116_table_registry_access_policy_columns(tmp_path):
    """v115→v116 (table access policies, §4): a fresh install carries the
    five access-policy columns on ``table_registry`` — access_policy_sql /
    access_policy_note / access_policy_updated_at / access_policy_updated_by
    default NULL, policy_mapping defaults FALSE — and the migration step is
    idempotent."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    # `>=`, matching this file's own convention (see the v80 check above): the
    # claim under test is that a fresh install carries the v116 columns, not
    # that the ladder stops at 116. An equality here fails on every later
    # migration for reasons having nothing to do with access policies.
    assert SCHEMA_VERSION >= 116
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {r[1] for r in conn.execute("PRAGMA table_info('table_registry')").fetchall()}
    assert {
        "access_policy_sql",
        "access_policy_note",
        "access_policy_updated_at",
        "access_policy_updated_by",
        "policy_mapping",
    } <= cols, f"access-policy columns missing from table_registry: {cols}"

    conn.execute("INSERT INTO table_registry (id, name) VALUES ('t1', 'orders')")
    row = conn.execute(
        "SELECT access_policy_sql, access_policy_note, access_policy_updated_at, "
        "access_policy_updated_by, policy_mapping FROM table_registry WHERE id = 't1'"
    ).fetchone()
    assert row == (None, None, None, None, False), f"new row's access-policy columns: {row}"

    # idempotency — re-running the step must not raise
    from src.db import _v115_to_v116

    _v115_to_v116(conn)
    conn.close()


def test_v115_db_migrates_to_v116_adds_access_policy_columns(tmp_path):
    """A DB pinned at v115 (a live instance's state before this migration)
    climbs to v116 via the upgrade-block dispatch, keeping an existing row
    intact with the five new columns reading their documented defaults.

    ``table_registry`` is REFERENCES'd by ``sync_state``/``sync_history``,
    and DuckDB refuses to DROP a column from a table other tables have FKs
    into — the same restriction ``test_v97_db_upgrades_to_v98`` hit for
    ``chat_sessions`` (referenced by ``chat_messages``). So, like that test,
    this exercises the ``current < 116`` dispatch + idempotent guarded ALTER
    directly rather than simulating a column-free table.
    """
    db_path = tmp_path / "v115.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 115")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('keep', 'orders')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {r[1] for r in conn.execute("PRAGMA table_info('table_registry')").fetchall()}
    assert {
        "access_policy_sql",
        "access_policy_note",
        "access_policy_updated_at",
        "access_policy_updated_by",
        "policy_mapping",
    } <= cols

    row = conn.execute("SELECT id, access_policy_sql, policy_mapping FROM table_registry WHERE id = 'keep'").fetchone()
    assert row == ("keep", None, False), f"existing row must survive the upgrade with defaulted columns: {row}"

    # idempotency — re-running the step must not raise
    from src.db import _v115_to_v116

    _v115_to_v116(conn)
    conn.close()


def test_v120_agent_schedules_table(tmp_path):
    """v119→v120 (agent-schedules design): agent_schedules exists on fresh
    installs, the ladder is idempotent, and schema_version lands at
    SCHEMA_VERSION."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert SCHEMA_VERSION >= 120
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {r[1] for r in conn.execute("PRAGMA table_info('agent_schedules')").fetchall()}
    assert {
        "id",
        "agent_id",
        "name",
        "schedule",
        "prompt",
        "enabled",
        "last_run_at",
        "last_status",
        "last_job_id",
        "created_at",
        "updated_at",
    } <= cols

    # idempotency — re-running the step must not raise
    from src.db import _v119_to_v120

    _v119_to_v120(conn)
    conn.close()


def test_v119_db_migrates_to_v120_adds_agent_schedules(tmp_path):
    """A DB pinned at v119 (a live instance's state before this migration)
    climbs to v120 via the upgrade-block dispatch and gains agent_schedules."""
    db_path = tmp_path / "v119.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 119")
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_schedules'").fetchone()
    assert exists is not None

    conn.execute(
        "INSERT INTO agent_schedules (id, agent_id, name, schedule, prompt) "
        "VALUES ('s1', 'a1', 'morning', 'daily 07:00', 'do the thing')"
    )
    row = conn.execute("SELECT enabled FROM agent_schedules WHERE id = 's1'").fetchone()
    assert row == (True,)
    conn.close()


def test_v122_backfills_builder_agent_scope(tmp_path):
    """v121→v122: a builder row (``agt_`` prefix) still on the all-'all'
    passthrough shape has its knowledge/plugins declaration turned into
    ``agent_scope`` rows and its four modes flipped to 'selected'; the
    seeded default agent, a governance row, and an already-narrowed agent
    are left alone. Mirrors ``tests/db_pg/test_alembic_0070_builder_scope.py``.
    """
    import json

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    pkg_id = "pkg-1"
    conn.execute(
        "INSERT INTO data_packages (id, name, slug, created_at, updated_at) "
        "VALUES (?, 'Pkg', 'pkg', current_timestamp, current_timestamp)",
        [pkg_id],
    )

    def _add(agent_id, slug, knowledge=(), plugins=(), modes="all", is_default=False):
        conn.execute(
            "INSERT INTO agents (id, owner_user_id, name, slug, knowledge, plugins, "
            "tables_mode, plugins_mode, connections_mode, memory_mode, is_default, "
            "created_at, updated_at) VALUES (?, 'u1', ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "current_timestamp, current_timestamp)",
            [
                agent_id,
                slug,
                slug,
                json.dumps(list(knowledge)),
                json.dumps(list(plugins)),
                modes,
                modes,
                modes,
                modes,
                is_default,
            ],
        )

    _add("agt_" + "a" * 32, "scoped", knowledge=[pkg_id], plugins=["plug-1"])
    _add("agt_" + "b" * 32, "ghost", knowledge=["no-such-resource"])
    _add("agt_" + "c" * 32, "hand-scoped", knowledge=[pkg_id], modes="selected")
    _add("default-uuid", "default", is_default=True)
    _add("governance-uuid", "governance")

    from src.db import _v121_to_v122

    _v121_to_v122(conn)

    def _modes(agent_id):
        return set(
            conn.execute(
                "SELECT tables_mode, plugins_mode, connections_mode, memory_mode FROM agents WHERE id = ?",
                [agent_id],
            ).fetchone()
        )

    def _scope(agent_id):
        return {
            tuple(r)
            for r in conn.execute(
                "SELECT item_type, item_id FROM agent_scope WHERE agent_id = ?", [agent_id]
            ).fetchall()
        }

    assert _modes("agt_" + "a" * 32) == {"selected"}
    assert _scope("agt_" + "a" * 32) == {("data_package", pkg_id), ("plugin", "plug-1")}

    # An id resolving nowhere yields no row, but the agent still leaves the
    # passthrough shape — "0 sources" enforced is the fail-closed reading.
    assert _scope("agt_" + "b" * 32) == set()
    assert _modes("agt_" + "b" * 32) == {"selected"}

    # Already narrowed by hand → out of the cohort, scope untouched.
    assert _scope("agt_" + "c" * 32) == set()

    # Web chat's attribution row and a governance row keep passing the
    # owner's own authority through.
    assert _modes("default-uuid") == {"all"}
    assert _modes("governance-uuid") == {"all"}

    # idempotency — re-running the step must not raise or duplicate rows
    _v121_to_v122(conn)
    assert _scope("agt_" + "a" * 32) == {("data_package", pkg_id), ("plugin", "plug-1")}
    conn.close()
