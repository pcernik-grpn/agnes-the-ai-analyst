"""Internal data source — Agnes serving its own telemetry tables through
the same query plumbing analysts use for Keboola / BigQuery / Jira.

The motivation is recursive observability: AI agents on the analyst side
can query their own usage history (which tools fail, which sessions
stalled, which audit actions they trigger) through the same DuckDB-shaped
catalog they already know. Admins get the unfiltered view; everyone else
gets row-level scoped views built per-request.

Four tables exposed today (all read-only):
- ``agnes_sessions``  → ``usage_session_summary`` filtered by ``user_id``
- ``agnes_telemetry`` → ``usage_events`` filtered by ``user_id``
- ``agnes_audit``     → ``audit_log`` filtered by ``user_id``
- ``agnes_turns``     → ``usage_turns`` filtered by ``user_id`` — **Postgres
  backend only**: ``usage_turns`` landed after the A3 freeze and has no
  DuckDB counterpart, so the id is not registered at all on a DuckDB-backed
  instance (``PG_ONLY_INTERNAL_TABLE_IDS`` in ``registry.py``)

Reaching them at all is an ordinary Data Package grant: they are members of
the seeded ``agnes-usage`` package (``connectors/internal/registry.py``), so
a caller without it does not see the tables. Only the row scope above is
special-cased here.

Source-of-truth contract:
- ``connectors/internal/access.py`` owns the table → (source, filter column,
  filter value resolver) mapping. Adding a new internal table is one row
  in ``INTERNAL_TABLES`` plus an ``ensure_registered`` entry — and, when the
  source table exists on Postgres only, its id in
  ``registry.PG_ONLY_INTERNAL_TABLE_IDS``.
- The /api/query path in ``app/api/query.py`` checks for internal-table
  references and routes the SQL to ``execute_internal_query`` instead of
  the analytics-DB path. Mixing internal tables with BQ / local registry
  rows in a single SQL statement is rejected in v1.
"""
