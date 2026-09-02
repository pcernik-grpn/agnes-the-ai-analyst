"""Postgres-backed reports repository.

Mirrors ``src/repositories/reports.py`` — same method names and return shapes,
PG dialect (``:param`` binds, ``sa.text``). Most aggregate shapes stay close to
DuckDB (``COUNT(DISTINCT ...)``, ``SUM(CASE WHEN is_error THEN 1 ELSE 0 END)``).
The one deliberate divergence is UTC day bucketing: a bare ``CAST(ts AS DATE)``
would first shift to the Postgres session ``TimeZone``, so day labels would
drift on a non-UTC server. This file pins it with ``CAST((ts AT TIME ZONE
'UTC') AS DATE)`` — do NOT "simplify" that back to a bare cast; the method-level
comments guard the same invariant. The same rule applies to window boundaries:
``CURRENT_DATE`` is session-TimeZone-dependent, so "today" is always spelled
``CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE)`` here.
"""

from __future__ import annotations

import datetime as _dt
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

ItemKey = tuple[str, str, str, str]  # (source, type, parent_plugin, name)

# PG mirror of ``reports.SYNTHETIC_MODEL`` — keep the two literals in step.
SYNTHETIC_MODEL = "<synthetic>"


def _zero_fill_daily_series(rows) -> list[dict]:
    """PG mirror of ``reports._zero_fill_daily_series`` — pure Python,
    dialect-independent.
    """
    by_day = {str(r[0]): int(r[1] or 0) for r in rows}
    # UTC, not date.today() — same axis-vs-fact-day invariant as the module
    # docstring's UTC day-bucketing rule.
    today = _dt.datetime.now(_dt.UTC).date()
    series = []
    for offset in range(29, -1, -1):
        day_str = (today - _dt.timedelta(days=offset)).isoformat()
        series.append({"day": day_str, "invocations": by_day.get(day_str, 0)})
    return series


def _assemble_inner_items_by_parent(win_rows, trend_rows) -> dict[tuple[str, str], dict[str, object]]:
    """PG mirror of ``reports._assemble_inner_items_by_parent`` — pure
    Python, dialect-independent.
    """
    out: dict[tuple[str, str], dict[str, object]] = {}
    for name, item_type, inv, du in win_rows:
        out[(name, item_type)] = {
            "invocations_30d": int(inv or 0),
            "distinct_users_30d": int(du or 0),
            "trend_pct": None,
        }
    for name, item_type, recent, prior in trend_rows:
        key = (name, item_type)
        stat = out.setdefault(
            key,
            {
                "invocations_30d": 0,
                "distinct_users_30d": 0,
                "trend_pct": None,
            },
        )
        recent_i = int(recent or 0)
        prior_i = int(prior or 0)
        if prior_i >= 3:
            stat["trend_pct"] = (recent_i - prior_i) / prior_i * 100.0
    return out


def _assemble_invocation_stats(win_rows, trend_rows) -> dict[str, dict]:
    """PG mirror of ``reports._assemble_invocation_stats`` — pure Python,
    dialect-independent, kept identical so the two ``invocation_stats``
    implementations only differ in their SQL.
    """
    trend_by_name = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in trend_rows}

    out: dict[str, dict] = {}
    for period_label, name, inv, du in win_rows:
        stat = out.setdefault(
            name,
            {
                "invocations_30d": 0,
                "distinct_users_30d": 0,
                "invocations_7d": 0,
                "distinct_users_7d": 0,
                "trend_pct": None,
            },
        )
        if period_label == "last_30d":
            stat["invocations_30d"] = int(inv or 0)
            stat["distinct_users_30d"] = int(du or 0)
        elif period_label == "last_7d":
            stat["invocations_7d"] = int(inv or 0)
            stat["distinct_users_7d"] = int(du or 0)
    for name, (recent, prior) in trend_by_name.items():
        stat = out.setdefault(
            name,
            {
                "invocations_30d": 0,
                "distinct_users_30d": 0,
                "invocations_7d": 0,
                "distinct_users_7d": 0,
                "trend_pct": None,
            },
        )
        if prior >= 3:
            stat["trend_pct"] = (recent - prior) / prior * 100.0
    return out


class ReportsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # ---- usage_events / sessions windows ---------------------------------
    REAL_SESSION_PREDICATE = """NOT (COALESCE(primary_model, '') = '<synthetic>'
                                  AND COALESCE(tool_calls, 0) = 0
                                  AND COALESCE(active_seconds, 0) = 0)"""

    def event_window(self, start: datetime, end: datetime) -> dict:
        with self._engine.connect() as conn:
            r = conn.execute(
                sa.text(
                    """SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end"""
                ),
                {"start": start, "end": end},
            ).fetchone()
        return {"invocations": int(r[0] or 0), "active_users": int(r[1] or 0), "errors": int(r[2] or 0)}

    def session_count(self, start: datetime, end: datetime) -> int:
        """Real user sessions started in the window.

        Excludes the `<synthetic>` processing artifact: a row whose modal model
        is `<synthetic>` AND which recorded no tool calls and no active time.
        Counting those overstated the digest's session KPI substantially.

        All three conditions are required. `primary_model` is only the MOST
        FREQUENT model of the session (see the session processor), so filtering
        on it alone would drop a genuine session that happened to be
        synthetic-dominated but did real work. COALESCE keeps a NULL-model row
        countable — `NULL = <synthetic>` is NULL, and `NOT (NULL AND ...)` would
        silently discard it.
        """
        with self._engine.connect() as conn:
            r = conn.execute(
                sa.text(
                    f"""SELECT COUNT(DISTINCT session_id) FROM usage_session_summary
                       WHERE started_at >= :start AND started_at < :end
                         AND {self.REAL_SESSION_PREDICATE}"""
                ),
                {"start": start, "end": end},
            ).fetchone()
        return int(r[0] or 0)

    def events_daily(self, start: datetime, end: datetime) -> dict[date, dict]:
        # Bucket by the UTC calendar day. occurred_at is timestamptz; a plain
        # CAST(... AS DATE) would first shift to the session TimeZone, so a
        # non-UTC PG session would mis-bucket events near midnight UTC relative
        # to this report's UTC windows and day labels. AT TIME ZONE 'UTC' pins it.
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS d,
                              COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end
                       GROUP BY d"""
                ),
                {"start": start, "end": end},
            ).fetchall()
        return {
            r[0]: {"invocations": int(r[1] or 0), "active_users": int(r[2] or 0), "errors": int(r[3] or 0)}
            for r in rows
        }

    def by_source(self, start: datetime, end: datetime) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT source, COUNT(*), COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end
                         AND source IS NOT NULL
                       GROUP BY source ORDER BY 2 DESC"""
                ),
                {"start": start, "end": end},
            ).fetchall()
        return [
            {
                "source": r[0],
                "invocations": int(r[1] or 0),
                "distinct_users": int(r[2] or 0),
                "error_count": int(r[3] or 0),
            }
            for r in rows
        ]

    # ---- marketplace-item rollups ----------------------------------------
    def items_window(self, start_day: date, end_day: date) -> dict[ItemKey, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT source, type, parent_plugin, name,
                              SUM(count), SUM(distinct_users), SUM(error_count)
                       FROM usage_marketplace_item_daily
                       WHERE day >= :start AND day < :end
                       GROUP BY source, type, parent_plugin, name"""
                ),
                {"start": start_day, "end": end_day},
            ).fetchall()
        return {
            (r[0], r[1], r[2], r[3]): {
                "invocations": int(r[4] or 0),
                "distinct_users": int(r[5] or 0),
                "error_count": int(r[6] or 0),
            }
            for r in rows
        }

    def invocation_stats(self, source: str) -> dict[str, dict]:
        """PG mirror of ``ReportsRepository.invocation_stats`` (#728) — same
        shape and threshold semantics. ``day`` is a plain ``Date`` column on
        both engines, but the window boundary must be the UTC "today":
        PG's ``CURRENT_DATE`` is session-TimeZone-dependent, so the boundary
        is pinned as ``CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE)``
        (DuckDB's ``CURRENT_DATE`` is already UTC via the pinned session).
        """
        type_filter = "AND type = 'plugin'" if source == "curated" else "AND parent_plugin = ''"

        with self._engine.connect() as conn:
            win_rows = conn.execute(
                sa.text(
                    f"""
                    SELECT period_label, name, invocations, distinct_users
                    FROM usage_marketplace_item_window
                    WHERE period_label IN ('last_30d', 'last_7d')
                      AND source = :source
                      {type_filter}
                    """
                ),
                {"source": source},
            ).fetchall()

            trend_rows = conn.execute(
                sa.text(
                    f"""
                    SELECT
                        name,
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days' THEN count ELSE 0 END) AS inv_recent,
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                                  AND day <  CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days'
                                 THEN count ELSE 0 END) AS inv_prior
                    FROM usage_marketplace_item_daily
                    WHERE source = :source
                      {type_filter}
                      AND day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                    GROUP BY name
                    """
                ),
                {"source": source},
            ).fetchall()

        return _assemble_invocation_stats(win_rows, trend_rows)

    def plugin_daily_series(self, source: str, plugin_name: str) -> list[dict]:
        """PG mirror of ``ReportsRepository.plugin_daily_series`` (#728) —
        same shape, PG-dialect day arithmetic. Row selection mirrors
        ``invocation_stats`` (curated → plugin rows; flea → standalone
        entities) so the series matches the card's 30d total.
        """
        type_filter = "AND type = 'plugin'" if source == "curated" else "AND parent_plugin = ''"
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""
                    SELECT day, count
                    FROM usage_marketplace_item_daily
                    WHERE source = :source
                      {type_filter}
                      AND name = :name
                      AND day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '30 days'
                    ORDER BY day
                    """
                ),
                {"source": source, "name": plugin_name},
            ).fetchall()
        return _zero_fill_daily_series(rows)

    def inner_item_stats(
        self,
        source: str,
        parent_plugin: str,
        name: str,
        item_type: str,
    ) -> dict[str, object]:
        """PG mirror of ``ReportsRepository.inner_item_stats`` (#728) — same
        shape and threshold semantics, PG-dialect day arithmetic.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT invocations, distinct_users
                    FROM usage_marketplace_item_window
                    WHERE period_label = 'last_30d'
                      AND source = :source
                      AND type = :item_type
                      AND parent_plugin = :parent_plugin
                      AND name = :name
                    """
                ),
                {"source": source, "item_type": item_type, "parent_plugin": parent_plugin, "name": name},
            ).fetchone()
            inv30 = int(row[0]) if row and row[0] else 0
            du30 = int(row[1]) if row and row[1] else 0

            trend_row = conn.execute(
                sa.text(
                    """
                    SELECT
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days' THEN count ELSE 0 END) AS inv_recent,
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                                  AND day <  CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days'
                                 THEN count ELSE 0 END) AS inv_prior
                    FROM usage_marketplace_item_daily
                    WHERE source = :source
                      AND type = :item_type
                      AND parent_plugin = :parent_plugin
                      AND name = :name
                      AND day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                    """
                ),
                {"source": source, "item_type": item_type, "parent_plugin": parent_plugin, "name": name},
            ).fetchone()
            recent = int(trend_row[0]) if trend_row and trend_row[0] else 0
            prior = int(trend_row[1]) if trend_row and trend_row[1] else 0
            trend_pct = (recent - prior) / prior * 100.0 if prior >= 3 else None

            daily_rows = conn.execute(
                sa.text(
                    """
                    SELECT day, count
                    FROM usage_marketplace_item_daily
                    WHERE source = :source
                      AND type = :item_type
                      AND parent_plugin = :parent_plugin
                      AND name = :name
                      AND day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '30 days'
                    ORDER BY day
                    """
                ),
                {"source": source, "item_type": item_type, "parent_plugin": parent_plugin, "name": name},
            ).fetchall()

        return {
            "invocations_30d": inv30,
            "distinct_users_30d": du30,
            "trend_pct": trend_pct,
            "daily_series": _zero_fill_daily_series(daily_rows),
        }

    def inner_items_stats_by_parent(
        self,
        source: str,
        parent_plugin: str,
    ) -> dict[tuple[str, str], dict[str, object]]:
        """PG mirror of ``ReportsRepository.inner_items_stats_by_parent``
        (#728) — same shape, PG-dialect day arithmetic.
        """
        with self._engine.connect() as conn:
            win_rows = conn.execute(
                sa.text(
                    """
                    SELECT name, type, invocations, distinct_users
                    FROM usage_marketplace_item_window
                    WHERE period_label = 'last_30d'
                      AND source = :source
                      AND parent_plugin = :parent_plugin
                    """
                ),
                {"source": source, "parent_plugin": parent_plugin},
            ).fetchall()
            trend_rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        name, type,
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days' THEN count ELSE 0 END) AS inv_recent,
                        SUM(CASE WHEN day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                                  AND day <  CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '7 days'
                                 THEN count ELSE 0 END) AS inv_prior
                    FROM usage_marketplace_item_daily
                    WHERE source = :source
                      AND parent_plugin = :parent_plugin
                      AND day >= CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS DATE) - INTERVAL '14 days'
                    GROUP BY name, type
                    """
                ),
                {"source": source, "parent_plugin": parent_plugin},
            ).fetchall()
        return _assemble_inner_items_by_parent(win_rows, trend_rows)

    # ---- installs / adoption ---------------------------------------------
    #
    # PG mirror of ``reports.ReportsRepository``. `user_plugin_optouts` is the
    # historically-named curated-subscription ledger: since v28 row presence
    # means SUBSCRIBED and `opted_out_at` is effectively `subscribed_at`.
    #
    # A row whose plugin reaches EVERY account automatically got there by
    # platform action rather than by anyone choosing it, so it is
    # provisioning rather than adoption. That used to be the
    # `marketplace_plugins.is_system` flag; since 0098 it is a required grant
    # reaching everyone. NOT EXISTS rather than a join so an install whose
    # plugin row is missing entirely still counts as a real install instead
    # of silently vanishing.
    #
    # Matched by CARRIER group, not by `rg.scope`, so this string is
    # byte-identical to the DuckDB sibling's and the drift guard in
    # `tests/db_pg/test_reports_contract.py` can keep pinning them equal.
    # Correct on both: every everyone-scoped row is written against that one
    # group (the writer forces it — see `app/api/access.py::create_grant`),
    # and a plain required grant on the group reaches every account too,
    # because membership in it is automatic. The frozen DuckDB ladder has no
    # `scope` column at all, which is why the column cannot be the shared
    # spelling.
    #
    # Caveat: classification uses the grant as it stands NOW, so it describes
    # what a plugin is today, not what it was when the row was written.
    # Revoking the grant moves historical rollout rows back into the opt-in
    # figures. Fixing that properly needs provenance stamped on the
    # subscription row at subscribe time — a migration, deliberately not taken
    # here. Blast radius is limited: published reports are static HTML, so only
    # a re-generated report for a past window can shift.
    _NOT_SYSTEM = """
        NOT EXISTS (
            SELECT 1 FROM resource_grants rg
            WHERE rg.resource_type = 'marketplace_plugin'
              AND rg.resource_id = o.marketplace_id || '/' || o.plugin_name
              AND rg.requirement = 'required'
              AND rg.group_id IN (
                  SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system
              )
        )
    """

    def install_counts(self, start: datetime, end: datetime) -> dict:
        """Installs in the window.

        - ``curated`` / ``flea``: opt-in installs, counted as (user x item) rows.
        - ``system``: system-plugin subscriptions, kept visible but excluded from
          the adoption figures above.
        - ``distinct_installers``: PEOPLE who opted into at least one item — the
          only one of these in the same UNIT as ``active_users``. Same unit is
          not the same cohort: that figure counts distinct
          ``usage_events.username``, this one distinct ledger ``user_id``, and a
          person can install without being active in the window. Comparable in
          magnitude, not a ratio.
        """
        params = {"start": start, "end": end}
        with self._engine.connect() as conn:
            curated = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*) FROM user_plugin_optouts o
                        WHERE o.opted_out_at >= :start AND o.opted_out_at < :end
                          AND {self._NOT_SYSTEM}"""
                ),
                params,
            ).fetchone()[0]
            system = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*) FROM user_plugin_optouts o
                        WHERE o.opted_out_at >= :start AND o.opted_out_at < :end
                          AND NOT {self._NOT_SYSTEM}"""
                ),
                params,
            ).fetchone()[0]
            flea = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_store_installs WHERE installed_at >= :start AND installed_at < :end"
                ),
                params,
            ).fetchone()[0]
            # UNION (not UNION ALL) dedupes a user who installed on both sides.
            installers = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*) FROM (
                            SELECT o.user_id FROM user_plugin_optouts o
                            WHERE o.opted_out_at >= :start AND o.opted_out_at < :end
                              AND {self._NOT_SYSTEM}
                            UNION
                            SELECT user_id FROM user_store_installs
                            WHERE installed_at >= :start AND installed_at < :end
                        ) AS t"""
                ),
                params,
            ).fetchone()[0]
        return {
            "curated": int(curated or 0),
            "flea": int(flea or 0),
            "system": int(system or 0),
            "distinct_installers": int(installers or 0),
        }

    def installs_daily(self, start: datetime, end: datetime) -> dict[date, int]:
        """Per-UTC-day opt-in installs. Excludes system plugins so the trend
        series and the headline KPI cannot disagree."""
        out: dict[date, int] = {}
        # AT TIME ZONE 'UTC' so day buckets match this report's UTC day labels
        # regardless of the PG session TimeZone (see events_daily).
        stmts = (
            f"""SELECT CAST((o.opted_out_at AT TIME ZONE 'UTC') AS DATE) AS d, COUNT(*)
                FROM user_plugin_optouts o
                WHERE o.opted_out_at >= :start AND o.opted_out_at < :end
                  AND {self._NOT_SYSTEM}
                GROUP BY d""",
            """SELECT CAST((installed_at AT TIME ZONE 'UTC') AS DATE) AS d, COUNT(*)
               FROM user_store_installs
               WHERE installed_at >= :start AND installed_at < :end GROUP BY d""",
        )
        with self._engine.connect() as conn:
            for sql in stmts:
                for r in conn.execute(sa.text(sql), {"start": start, "end": end}).fetchall():
                    out[r[0]] = out.get(r[0], 0) + int(r[1] or 0)
        return out

    def installs_curated_detail(self, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT o.marketplace_id, o.plugin_name, COUNT(*) AS n
                        FROM user_plugin_optouts o
                        WHERE o.opted_out_at >= :start AND o.opted_out_at < :end
                          AND {self._NOT_SYSTEM}
                        GROUP BY o.marketplace_id, o.plugin_name ORDER BY n DESC LIMIT :lim"""
                ),
                {"start": start, "end": end, "lim": limit},
            ).fetchall()
        return [{"ref_id": f"{r[0]}/{r[1]}", "name": r[1], "installs": int(r[2] or 0)} for r in rows]

    def installs_flea_detail(self, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT usi.entity_id, s.name, COUNT(*) AS n
                       FROM user_store_installs usi
                       LEFT JOIN store_entities s ON s.id = usi.entity_id
                       WHERE usi.installed_at >= :start AND usi.installed_at < :end
                       GROUP BY usi.entity_id, s.name ORDER BY n DESC LIMIT :lim"""
                ),
                {"start": start, "end": end, "lim": limit},
            ).fetchall()
        return [{"entity_id": r[0], "name": r[1], "installs": int(r[2] or 0)} for r in rows]
