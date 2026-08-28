"""Shared contract for audit repositories.

Both the DuckDB-backed ``AuditRepository`` (``audit.py``) and the
Postgres-backed ``AuditPgRepository`` (``audit_pg.py``) implement this
Protocol. Tests in ``tests/db_pg/test_audit_contract.py`` parametrise
across both implementations; if either drifts from the shared surface,
the contract test fails red.

This is the pattern used for every repository that gets ported to
Postgres. Add a new repo by:
  1. Define the Protocol here (or in a sibling file).
  2. Write contract tests parametrised across [duckdb_impl, pg_impl].
  3. Build the PG impl until contract tests pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple


class AuditRepositoryProtocol(Protocol):
    """The minimal observable surface of an Agnes audit repository."""

    def log(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        resource: Optional[str] = None,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        *,
        params_before: Optional[dict] = None,
        client_ip: Optional[str] = None,
        client_kind: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Record one audit event; return the new row's id.

        ``duration_ms=None`` auto-fills from the request-timing contextvar
        (``src.audit_context.auto_duration_ms``) in both implementations —
        NULL is written only outside an HTTP request scope.
        """
        ...

    def query(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        result_class: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        source: Optional[str] = None,
        include_self_reads: bool = True,
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
        """Filtered list of audit rows + next-page cursor.

        Rows carry a computed ``source`` key (``AUDIT_SOURCE_CASE_SQL``).
        ``result_class`` filters via ``RESULT_CLASS_CASE_SQL``;
        ``include_self_reads=False`` hides ``activity.read`` rows.
        """
        ...

    def query_unified(
        self,
        *,
        trail: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        result_class: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        source: Optional[str] = None,
        include_self_reads: bool = True,
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
        """The Activity Center timeline, widened across every trail (E3
        slice 2): a UNION ALL of audit_log + sync_history + llm_usage +
        agent_scope_snapshots (never chat_messages — privacy decision),
        each mapped into the SAME row shape :meth:`query` returns, plus a
        literal ``trail`` column. Same filter/cursor/ordering contract as
        :meth:`query`; ``trail`` narrows to one physical trail (``"audit"``,
        ``"sync"``, ``"llm"``, ``"agent_scope"``) — unknown values raise
        ``ValueError``.
        """
        ...

    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Rows whose action is in the given list, newest first."""
        ...

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Activity timeline for one or more resource refs."""
        ...

    def count_for_user(self, user_id: str) -> int:
        """Total audit rows recorded for one user."""
        ...

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: Tuple[str, ...] = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Governance audit feed: corporate_memory.* + legacy km_* rows."""
        ...

    def upload_filenames_since(self, since: datetime) -> List[str]:
        """Distinct ``session.upload`` filenames at/after *since* — the
        health pulse's ingest-reconciliation source (joined against
        summary FILE basenames, never session_id)."""
        ...

    def last_scheduler_tick(self) -> Optional[datetime]:
        """Most recent scheduler-classified audit row timestamp."""
        ...

    def prune_older_than(self, days: int) -> int:
        """Delete rows older than ``days``; return the deleted-row count.
        The caller owns the "0/negative = keep forever" short-circuit."""
        ...

    def active_users_since(self, since: datetime) -> int:
        """Distinct non-NULL user_id count at/after *since*."""
        ...

    def facets(
        self,
        *,
        since: datetime,
        limit: int = 50,
        trail: Optional[str] = None,
        **filters: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Distinct facet buckets (users/actions/results/result_classes/
        resources/sources) over the unified timeline (audit_log +
        sync_history + llm_usage + agent_scope_snapshots), under the SAME
        filter kwargs as :meth:`query_unified` — including ``trail`` to
        narrow to one physical trail — so dropdown counts always describe
        rows the (now-unified) timeline can show.

        Source classification is rule-based (``AUDIT_SOURCE_CASE_SQL``) —
        no caller-supplied scheduler action list.
        """
        ...

    def kpis(
        self,
        *,
        since: datetime,
        trail: Optional[str] = None,
        **filters: Any,
    ) -> Dict[str, Any]:
        """Headline KPIs over the unified timeline, under the same filter
        kwargs as :meth:`query_unified` (including ``trail``):
        events_total, active_users (people — scheduler/system excluded),
        errors (result_class='error'), p95, duration_coverage."""
        ...
