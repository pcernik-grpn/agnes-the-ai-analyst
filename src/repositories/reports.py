"""Repository for the marketplace usage digest (admin reports).

Cross-table reporting reads kept behind the backend switch so the digest
endpoint never runs raw state SQL on the always-DuckDB ``_get_db`` connection
(the #518 backend-split bug). The PG mirror lives in ``reports_pg.py``.

Window convention: every ``(start, end)`` is a half-open interval with an
EXCLUSIVE upper bound; day-grain windows take ``date`` bounds, timestamp-grain
windows take aware ``datetime`` bounds.
"""

from __future__ import annotations

import datetime as _dt
from datetime import date, datetime

import duckdb

ItemKey = tuple[str, str, str, str]  # (source, type, parent_plugin, name)

# `primary_model` value Claude Code writes for synthetic assistant turns. The
# literal is an upstream transcript contract, not ours — the processor just
# records the most common model it saw. Sessions made only of these rows carry
# zero tool calls and zero active seconds, so they are excluded from session
# counts. Mirrored verbatim in ``reports_pg.py``.
SYNTHETIC_MODEL = "<synthetic>"


def _zero_fill_daily_series(rows) -> list[dict]:
    """Fold ``(day, count)`` rows into a 30-entry ``[{day, invocations}]``
    list, zero-padded for days without activity. Pure Python — duplicated
    verbatim in ``reports_pg.py`` (only the query feeding it differs).
    """
    by_day = {str(r[0]): int(r[1] or 0) for r in rows}
    # UTC, not date.today(): the fact rows are bucketed on UTC days (the
    # session timezone is pinned in src/duckdb_conn.py), so a local-clock
    # axis would drop today's bucket whenever local date != UTC date.
    today = _dt.datetime.now(_dt.UTC).date()
    series = []
    for offset in range(29, -1, -1):
        day_str = (today - _dt.timedelta(days=offset)).isoformat()
        series.append({"day": day_str, "invocations": by_day.get(day_str, 0)})
    return series


def _assemble_invocation_stats(win_rows, trend_rows) -> dict[str, dict]:
    """Fold ``invocation_stats``'s two raw row sets into the per-name stats
    dict. Pure Python, dialect-independent — duplicated verbatim in
    ``ReportsPgRepository.invocation_stats`` (only the two queries feeding it
    differ per backend).
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
    # Trend = recent_7 vs prior_7 from the daily fact (independent of the
    # window snapshot's freshness). Threshold preserved from v42 — trend is
    # noisy below 3 prior-week invocations so suppress to None.
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


def _assemble_inner_items_by_parent(win_rows, trend_rows) -> dict[tuple[str, str], dict[str, object]]:
    """Fold ``inner_items_stats_by_parent``'s two raw row sets into the
    ``(name, type)``-keyed stats dict. Pure Python, dialect-independent —
    duplicated verbatim in ``ReportsPgRepository.inner_items_stats_by_parent``.
    """
    out: dict[tuple[str, str], dict[str, object]] = {}
    for name, item_type, inv, du in win_rows:
        out[(name, item_type)] = {
            "invocations_30d": int(inv or 0),
            "distinct_users_30d": int(du or 0),
            "trend_pct": None,
        }
    # Trend threshold mirrors the listing card / hero chip — suppress to
    # None below 3 prior-week invocations (noise floor from v42).
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


class ReportsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ---- usage_events / sessions windows ---------------------------------
    REAL_SESSION_PREDICATE = """NOT (COALESCE(primary_model, '') = '<synthetic>'
                                  AND COALESCE(tool_calls, 0) = 0
                                  AND COALESCE(active_seconds, 0) = 0)"""

    def event_window(self, start: datetime, end: datetime) -> dict:
        r = self.conn.execute(
            """SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ?""",
            [start, end],
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
        return int(
            self.conn.execute(
                f"""SELECT COUNT(DISTINCT session_id) FROM usage_session_summary
               WHERE started_at >= ? AND started_at < ?
                 AND {self.REAL_SESSION_PREDICATE}""",
                [start, end],
            ).fetchone()[0]
            or 0
        )

    def events_daily(self, start: datetime, end: datetime) -> dict[date, dict]:
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS d,
                      COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ?
               GROUP BY d""",
            [start, end],
        ).fetchall()
        return {
            r[0]: {"invocations": int(r[1] or 0), "active_users": int(r[2] or 0), "errors": int(r[3] or 0)}
            for r in rows
        }

    def by_source(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.conn.execute(
            """SELECT source, COUNT(*), COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ? AND source IS NOT NULL
               GROUP BY source ORDER BY 2 DESC""",
            [start, end],
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
        rows = self.conn.execute(
            """SELECT source, type, parent_plugin, name,
                      SUM(count), SUM(distinct_users), SUM(error_count)
               FROM usage_marketplace_item_daily
               WHERE day >= ? AND day < ?
               GROUP BY source, type, parent_plugin, name""",
            [start_day, end_day],
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
        """Return the browse-panel telemetry map for one source (#728).

        Key: plugin name (curated) or flea entity/synthetic name. Value:
        ``{invocations_30d, distinct_users_30d, invocations_7d,
        distinct_users_7d, trend_pct}``.

        Reads ``usage_marketplace_item_window`` for the 30d/7d snapshot
        aggregates (true sliding distinct, sub-ms lookup) and
        ``usage_marketplace_item_daily`` for the recent-7-vs-prior-7 trend
        calc. Moved off the inline SQL that used to run directly on the
        always-DuckDB ``_get_db`` connection in
        ``app/api/marketplace.py::_load_invocation_stats`` — that bypassed
        the backend switch, so the browse panels stayed empty on a
        Postgres instance even after the rollup producer went dual-backend
        (#773).

        Card-level rows differ per source: curated cards are plugin-level
        rows (``type='plugin'``, aggregate of skill/agent invocations
        attributed to that plugin); flea cards are standalone entities
        (``parent_plugin=''``), any type.
        """
        type_filter = "AND type = 'plugin'" if source == "curated" else "AND parent_plugin = ''"

        win_rows = self.conn.execute(
            f"""
            SELECT period_label, name, invocations, distinct_users
            FROM usage_marketplace_item_window
            WHERE period_label IN ('last_30d', 'last_7d')
              AND source = ?
              {type_filter}
            """,
            [source],
        ).fetchall()

        trend_rows = self.conn.execute(
            f"""
            SELECT
                name,
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 7 DAY THEN count ELSE 0 END) AS inv_recent,
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 14 DAY
                          AND day <  CURRENT_DATE - INTERVAL 7  DAY
                         THEN count ELSE 0 END) AS inv_prior
            FROM usage_marketplace_item_daily
            WHERE source = ?
              {type_filter}
              AND day >= CURRENT_DATE - INTERVAL 14 DAY
            GROUP BY name
            """,
            [source],
        ).fetchall()

        return _assemble_invocation_stats(win_rows, trend_rows)

    def plugin_daily_series(self, source: str, plugin_name: str) -> list[dict]:
        """Return a 30-entry ``[{day, invocations}]`` list (missing days
        zero-filled) for one plugin-level card (#728).

        Row selection mirrors ``invocation_stats``: curated cards are
        plugin-level rows (``type='plugin'``), flea cards are standalone
        entities (``parent_plugin=''``, any type) — so the series always
        matches the 30d total shown on the same card / detail page. Moved
        off the inline SQL in
        ``app/api/marketplace.py::_load_plugin_daily_series``.
        """
        type_filter = "AND type = 'plugin'" if source == "curated" else "AND parent_plugin = ''"
        rows = self.conn.execute(
            f"""
            SELECT day, count
            FROM usage_marketplace_item_daily
            WHERE source = ?
              {type_filter}
              AND name = ?
              AND day >= CURRENT_DATE - INTERVAL 30 DAY
            ORDER BY day
            """,
            [source, plugin_name],
        ).fetchall()
        return _zero_fill_daily_series(rows)

    def inner_item_stats(
        self,
        source: str,
        parent_plugin: str,
        name: str,
        item_type: str,
    ) -> dict[str, object]:
        """Return a per-item telemetry dict for one curated inner skill/agent
        or one standalone flea entity (#728).

        For flea entities ``parent_plugin`` is ``''`` (matches the stored
        empty-string sentinel). For curated inner items it's the plugin
        name. Always returns a dict (never None) so the caller can render
        the hero chip from the same field shape regardless of activity
        level. Includes:

          * invocations_30d, distinct_users_30d — window snapshot lookup
          * trend_pct — recent-7 vs prior-7 calc, sourced from the daily
            fact for this item (same threshold as ``invocation_stats``)
          * daily_series — 30-entry zero-padded list of {day, invocations}

        Moved off the inline SQL in
        ``app/api/marketplace.py::_load_inner_item_stats``.
        """
        row = self.conn.execute(
            """
            SELECT invocations, distinct_users
            FROM usage_marketplace_item_window
            WHERE period_label = 'last_30d'
              AND source = ?
              AND type = ?
              AND parent_plugin = ?
              AND name = ?
            """,
            [source, item_type, parent_plugin, name],
        ).fetchone()
        inv30 = int(row[0]) if row and row[0] else 0
        du30 = int(row[1]) if row and row[1] else 0

        trend_row = self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 7 DAY THEN count ELSE 0 END) AS inv_recent,
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 14 DAY
                          AND day <  CURRENT_DATE - INTERVAL 7  DAY
                         THEN count ELSE 0 END) AS inv_prior
            FROM usage_marketplace_item_daily
            WHERE source = ?
              AND type = ?
              AND parent_plugin = ?
              AND name = ?
              AND day >= CURRENT_DATE - INTERVAL 14 DAY
            """,
            [source, item_type, parent_plugin, name],
        ).fetchone()
        recent = int(trend_row[0]) if trend_row and trend_row[0] else 0
        prior = int(trend_row[1]) if trend_row and trend_row[1] else 0
        trend_pct = (recent - prior) / prior * 100.0 if prior >= 3 else None

        daily_rows = self.conn.execute(
            """
            SELECT day, count
            FROM usage_marketplace_item_daily
            WHERE source = ?
              AND type = ?
              AND parent_plugin = ?
              AND name = ?
              AND day >= CURRENT_DATE - INTERVAL 30 DAY
            ORDER BY day
            """,
            [source, item_type, parent_plugin, name],
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
        """Bulk per-inner-item stats for one parent plugin (#728).

        Returns ``{(name, type): {invocations_30d, distinct_users_30d,
        trend_pct}}``. One query against ``usage_marketplace_item_window``
        + one against ``_daily`` per parent plugin (vs N+1 if each card
        were looked up individually). Type-keyed because skills and agents
        are allowed to share a name in the same bundle.

        Used by ``curated_detail`` and ``flea_detail`` to enrich the
        ``skills`` / ``agents`` lists they return. Moved off the inline SQL
        in ``app/api/marketplace.py::_load_inner_items_stats_by_parent``.
        """
        win_rows = self.conn.execute(
            """
            SELECT name, type, invocations, distinct_users
            FROM usage_marketplace_item_window
            WHERE period_label = 'last_30d'
              AND source = ?
              AND parent_plugin = ?
            """,
            [source, parent_plugin],
        ).fetchall()
        trend_rows = self.conn.execute(
            """
            SELECT
                name, type,
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 7 DAY THEN count ELSE 0 END) AS inv_recent,
                SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 14 DAY
                          AND day <  CURRENT_DATE - INTERVAL 7 DAY
                         THEN count ELSE 0 END) AS inv_prior
            FROM usage_marketplace_item_daily
            WHERE source = ?
              AND parent_plugin = ?
              AND day >= CURRENT_DATE - INTERVAL 14 DAY
            GROUP BY name, type
            """,
            [source, parent_plugin],
        ).fetchall()
        return _assemble_inner_items_by_parent(win_rows, trend_rows)

    # ---- installs / adoption ---------------------------------------------
    #
    # `user_plugin_optouts` is the historically-named curated-subscription
    # ledger: since v28 row presence means SUBSCRIBED, and `opted_out_at` is
    # effectively `subscribed_at`. Read it as an install ledger, not an opt-out
    # one.
    #
    # A row whose plugin reaches EVERY account automatically got there by
    # platform action rather than by anyone choosing it, so it is
    # provisioning rather than adoption and is reported separately. That used
    # to be the `marketplace_plugins.is_system` flag; since 0098 it is a
    # required grant at `scope='everyone'`. This backend has no `scope`
    # column (the ladder is frozen, A3), so the same statement is spelled as
    # a required grant held by the carrier group — see
    # `src/repositories/resource_grants.py`. NOT EXISTS rather than a join so
    # an install whose plugin row is missing entirely still counts as a real
    # install instead of silently vanishing.
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
        curated = int(
            self.conn.execute(
                f"""SELECT COUNT(*) FROM user_plugin_optouts o
                    WHERE o.opted_out_at >= ? AND o.opted_out_at < ?
                      AND {self._NOT_SYSTEM}""",
                [start, end],
            ).fetchone()[0]
            or 0
        )
        system = int(
            self.conn.execute(
                f"""SELECT COUNT(*) FROM user_plugin_optouts o
                    WHERE o.opted_out_at >= ? AND o.opted_out_at < ?
                      AND NOT {self._NOT_SYSTEM}""",
                [start, end],
            ).fetchone()[0]
            or 0
        )
        flea = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_store_installs WHERE installed_at >= ? AND installed_at < ?",
                [start, end],
            ).fetchone()[0]
            or 0
        )
        # UNION (not UNION ALL) dedupes a user who installed on both sides.
        installers = int(
            self.conn.execute(
                f"""SELECT COUNT(*) FROM (
                        SELECT o.user_id FROM user_plugin_optouts o
                        WHERE o.opted_out_at >= ? AND o.opted_out_at < ?
                          AND {self._NOT_SYSTEM}
                        UNION
                        SELECT user_id FROM user_store_installs
                        WHERE installed_at >= ? AND installed_at < ?
                    ) AS t""",
                [start, end, start, end],
            ).fetchone()[0]
            or 0
        )
        return {"curated": curated, "flea": flea, "system": system, "distinct_installers": installers}

    def installs_daily(self, start: datetime, end: datetime) -> dict[date, int]:
        """Per-UTC-day opt-in installs. Excludes system plugins so the trend
        series and the headline KPI cannot disagree."""
        out: dict[date, int] = {}
        queries = (
            f"""SELECT CAST(o.opted_out_at AS DATE) AS d, COUNT(*)
                FROM user_plugin_optouts o
                WHERE o.opted_out_at >= ? AND o.opted_out_at < ?
                  AND {self._NOT_SYSTEM}
                GROUP BY d""",
            """SELECT CAST(installed_at AS DATE) AS d, COUNT(*)
               FROM user_store_installs
               WHERE installed_at >= ? AND installed_at < ?
               GROUP BY d""",
        )
        for sql in queries:
            for r in self.conn.execute(sql, [start, end]).fetchall():
                out[r[0]] = out.get(r[0], 0) + int(r[1] or 0)
        return out

    def installs_curated_detail(self, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            f"""SELECT o.marketplace_id, o.plugin_name, COUNT(*) AS n
                FROM user_plugin_optouts o
                WHERE o.opted_out_at >= ? AND o.opted_out_at < ?
                  AND {self._NOT_SYSTEM}
                GROUP BY o.marketplace_id, o.plugin_name ORDER BY n DESC LIMIT ?""",
            [start, end, limit],
        ).fetchall()
        return [{"ref_id": f"{r[0]}/{r[1]}", "name": r[1], "installs": int(r[2] or 0)} for r in rows]

    def installs_flea_detail(self, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT usi.entity_id, s.name, COUNT(*) AS n
               FROM user_store_installs usi
               LEFT JOIN store_entities s ON s.id = usi.entity_id
               WHERE usi.installed_at >= ? AND usi.installed_at < ?
               GROUP BY usi.entity_id, s.name ORDER BY n DESC LIMIT ?""",
            [start, end, limit],
        ).fetchall()
        return [{"entity_id": r[0], "name": r[1], "installs": int(r[2] or 0)} for r in rows]
