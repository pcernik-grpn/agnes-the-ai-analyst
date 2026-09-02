"""Repository for the per-marketplace plugin cache.

Each row is a single plugin listed in a marketplace's
`.claude-plugin/marketplace.json`. The rows are fully derived from the
cloned working copy on disk — treat this table as a cache that is
refreshed on every successful `src.marketplace.sync_one()` call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb


class MarketplacePluginsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
        d = dict(zip(columns, row))
        # ``doc_links`` joins ``source_spec`` / ``raw`` here — DuckDB stores
        # JSON columns as VARCHAR via our INSERT path, so each fetch returns
        # a string that the API layer wants as a parsed structure.
        for k in ("source_spec", "raw", "doc_links"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass
        return d

    def list_distinct_names(self) -> List[str]:
        """Every distinct plugin ``name`` across all marketplaces.

        Backs ``MarketplaceItemLookup`` (usage-telemetry attribution) — it
        needs the set of curated plugin names to recognise a Claude Code
        identifier prefix (``<plugin>:<local>``) without caring which
        marketplace registered it. Routed through the factory so the read
        hits the active backend (a raw DuckDB read here silently returned
        zero rows on Postgres instances)."""
        rows = self.conn.execute("SELECT DISTINCT name FROM marketplace_plugins").fetchall()
        return [r[0] for r in rows]

    def list_for_marketplace(self, marketplace_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM marketplace_plugins WHERE marketplace_id = ? ORDER BY name",
            [marketplace_id],
        ).fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [self._row_to_dict(columns, r) for r in rows]

    def get(self, marketplace_id: str, name: str) -> Optional[Dict[str, Any]]:
        """Fetch a single plugin row by (marketplace_id, name), or None.

        Used by the curated install/uninstall existence and admin-disabled
        checks so they go through the backend-aware factory instead of a raw
        DuckDB read.
        """
        row = self.conn.execute(
            "SELECT * FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
            [marketplace_id, name],
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, row)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM marketplace_plugins ORDER BY marketplace_id, name").fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [self._row_to_dict(columns, r) for r in rows]

    def count_by_marketplace(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT marketplace_id, COUNT(*) FROM marketplace_plugins GROUP BY marketplace_id"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def list_granted_for_groups(
        self,
        group_ids: Iterable[str],
    ) -> List[Dict[str, Any]]:
        """Plugins visible to any of ``group_ids``, granted via
        ``resource_grants``, ordered by parent marketplace registration
        time then plugin name.

        One path, since 0098. ``is_system`` used to be a second one, read
        here in an OR beside the grants — a plugin every account got
        without any grant row saying so. It is now an ordinary
        everyone-scoped grant, which this backend sees as a grant on the
        carrier group (see ``src/repositories/resource_grants.py``); the
        Postgres sibling matches the scope directly.

        Used by ``src.marketplace_filter.resolve_allowed_plugins`` —
        the resolver behind the served Claude Code marketplace
        (``/marketplace.git/``, ``/marketplace.zip``). Returns a list of
        ``{marketplace_id, name, version, raw}`` dicts (``raw`` is parsed
        JSON, matching ``list_for_marketplace`` semantics) where each
        plugin appears exactly once even if multiple groups grant it.

        Joins ``marketplace_registry`` so the ORDER BY uses the parent
        marketplace's registration time — deterministic across machines,
        gives the served marketplace a stable ETag / git commit SHA as
        long as the underlying content is unchanged.
        """
        gids = list(group_ids)
        # `NULL` rather than an empty `IN ()`, which is a syntax error on both
        # engines. A caller in no group now matches nothing here — on this
        # backend that is the honest answer, because an everyone-grant is a
        # grant on the carrier group and reaching it requires membership.
        placeholders = ",".join(["?"] * len(gids)) if gids else "NULL"
        # Driven off ``marketplace_plugins`` with a semi-join rather than a
        # JOIN against ``resource_grants``: EXISTS drops the DISTINCT the old
        # join needed to collapse one row per granting group.
        # ``mr.registered_at`` rides the projection for the ORDER BY (PG
        # requires it) and is dropped below.
        rows = self.conn.execute(
            "SELECT mp.marketplace_id, mp.name, mp.version, mp.raw, "
            "       mr.registered_at "
            "FROM marketplace_plugins mp "
            "JOIN marketplace_registry mr ON mr.id = mp.marketplace_id "
            "WHERE mp.admin_disabled = FALSE "
            "  AND EXISTS ("
            "        SELECT 1 FROM resource_grants rg "
            "        WHERE rg.resource_id = mp.marketplace_id || '/' || mp.name "
            "          AND rg.resource_type = 'marketplace_plugin' "
            f"          AND rg.group_id IN ({placeholders})"
            "      ) "
            "ORDER BY mr.registered_at, mp.name",
            list(gids),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for marketplace_id, name, version, raw, _registered_at in rows:
            parsed_raw: Any = raw
            if isinstance(raw, str):
                try:
                    parsed_raw = json.loads(raw)
                except (ValueError, TypeError):
                    parsed_raw = {}
            out.append(
                {
                    "marketplace_id": marketplace_id,
                    "name": name,
                    "version": version,
                    "raw": parsed_raw if isinstance(parsed_raw, dict) else {},
                }
            )
        return out

    def list_with_filters(
        self,
        *,
        group_ids: Iterable[str],
        search: Optional[str] = None,
        category: Optional[str] = None,
        skip: int = 0,
        limit: int = 24,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """RBAC-scoped browse listing for ``/marketplace`` (Curated tab).

        Joins ``resource_grants`` so only plugins explicitly granted to one
        of the caller's ``group_ids`` are visible. Returns ``(items, total)``
        sorted ``ORDER BY created_at DESC, name`` for "newest first" UI.
        Empty ``group_ids`` short-circuits to ``([], 0)`` — no implicit Everyone.
        """
        gids = list(group_ids)
        if not gids:
            return ([], 0)
        placeholders = ",".join(["?"] * len(gids))
        where_clauses = [
            f"rg.group_id IN ({placeholders})",
            "rg.resource_type = 'marketplace_plugin'",
            "rg.resource_id = mp.marketplace_id || '/' || mp.name",
            # Admin-disabled built-in plugins are hidden from the browse listing
            # too — mirrors the served-feed filter in list_granted_for_groups.
            "mp.admin_disabled = FALSE",
        ]
        params: List[Any] = list(gids)
        if search:
            where_clauses.append(
                "(LOWER(mp.name) LIKE ? OR LOWER(COALESCE(mp.description,'')) LIKE ? "
                "OR LOWER(COALESCE(mp.author_name,'')) LIKE ? "
                "OR LOWER(COALESCE(mp.category,'')) LIKE ?)"
            )
            needle = f"%{search.lower()}%"
            params.extend([needle, needle, needle, needle])
        if category:
            if category == "Other":
                # The Other bucket also catches plugins whose upstream
                # marketplace.json explicitly sets category='Other', not
                # just NULL / empty.
                where_clauses.append("(mp.category IS NULL OR TRIM(mp.category) = '' OR mp.category = ?)")
                params.append(category)
            else:
                where_clauses.append("mp.category = ?")
                params.append(category)

        where_sql = " AND ".join(where_clauses)

        total_row = self.conn.execute(
            f"SELECT COUNT(DISTINCT (mp.marketplace_id, mp.name)) "
            f"FROM marketplace_plugins mp "
            f"JOIN resource_grants rg ON 1=1 "
            f"WHERE {where_sql}",
            params,
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        if total == 0:
            return ([], 0)

        rows = self.conn.execute(
            f"SELECT DISTINCT mp.marketplace_id, mp.name, mp.description, mp.version, "
            f"       mp.author_name, mp.homepage, mp.category, mp.source_type, "
            f"       mp.source_spec, mp.raw, mp.cover_photo_url, mp.video_url, "
            f"       mp.doc_links, mp.created_at, mp.updated_at "
            f"FROM marketplace_plugins mp "
            f"JOIN resource_grants rg ON 1=1 "
            f"WHERE {where_sql} "
            f"ORDER BY mp.created_at DESC NULLS LAST, mp.name "
            f"LIMIT ? OFFSET ?",
            [*params, int(limit), int(skip)],
        ).fetchall()
        columns = [d[0] for d in self.conn.description]
        return ([self._row_to_dict(columns, r) for r in rows], total)

    def category_counts(
        self,
        *,
        group_ids: Iterable[str],
    ) -> Dict[str, int]:
        """Per-category plugin counts within the caller's RBAC scope.

        ``NULL`` / empty categories bucket into ``"Other"``. Returns only
        non-zero counts; the frontend hides categories not present here.
        """
        gids = list(group_ids)
        if not gids:
            return {}
        placeholders = ",".join(["?"] * len(gids))
        rows = self.conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM(mp.category),''), 'Other') AS cat, "
            f"       COUNT(DISTINCT (mp.marketplace_id, mp.name)) "
            f"FROM marketplace_plugins mp "
            f"JOIN resource_grants rg "
            f"  ON rg.resource_id = mp.marketplace_id || '/' || mp.name "
            f"WHERE rg.group_id IN ({placeholders}) "
            f"  AND rg.resource_type = 'marketplace_plugin' "
            f"  AND mp.admin_disabled = FALSE "
            f"GROUP BY cat",
            list(gids),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def replace_for_marketplace(
        self,
        marketplace_id: str,
        plugins: Iterable[Dict[str, Any]],
    ) -> int:
        """Refresh the full plugin set for one marketplace in a single transaction.

        Upsert pattern (preserves ``created_at`` for plugins that already
        existed; only freshly-discovered plugins receive the current sync's
        timestamp). Plugins that were in the previous snapshot but are no
        longer in the upstream ``marketplace.json`` are deleted.

        Each ``plugins`` entry is the full marketplace.json plugin dict
        optionally augmented with v32 enrichment keys produced by the
        marketplace-metadata.json reader:

        * ``cover_photo_url`` (str | None)  — already-resolved served URL
        * ``video_url`` (str | None)
        * ``doc_links`` (list[dict] | None) — list of resolved doc-link
          objects, each carrying ``{name, url, kind}`` where ``kind`` is
          ``internal`` / ``mirrored`` / ``external``.

        Absent keys are persisted as NULL — that's the steady state when the
        upstream marketplace ships no marketplace-metadata.json at all.

        Returns the number of plugins written.
        """
        plugins_list = list(plugins)
        now = datetime.now(timezone.utc)
        valid_names = {(p.get("name") or "").strip() for p in plugins_list if (p.get("name") or "").strip()}
        self.conn.execute("BEGIN")
        try:
            # Drop rows that no longer exist upstream — preserves created_at
            # for everything that survives.
            if valid_names:
                placeholders = ",".join(["?"] * len(valid_names))
                self.conn.execute(
                    f"DELETE FROM marketplace_plugins WHERE marketplace_id = ? AND name NOT IN ({placeholders})",
                    [marketplace_id, *valid_names],
                )
            else:
                self.conn.execute(
                    "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
                    [marketplace_id],
                )

            for p in plugins_list:
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                source_spec = p.get("source")
                source_type = _classify_source(source_spec)
                author = p.get("author") or {}
                author_name = author.get("name") if isinstance(author, dict) else None
                source_spec_json = json.dumps(source_spec) if source_spec is not None else None
                # `raw` continues to carry the unmerged upstream marketplace.json
                # plugin entry — marketplace-metadata enrichment is held in dedicated
                # columns, never folded into `raw`. Keeps the contract clean for
                # the synth marketplace flow that re-emits `raw` to Claude Code.
                raw_payload = {
                    k: v
                    for k, v in p.items()
                    if k
                    not in (
                        "cover_photo_url",
                        "video_url",
                        "doc_links",
                    )
                }
                raw_json = json.dumps(raw_payload)
                doc_links = p.get("doc_links")
                doc_links_json = json.dumps(doc_links) if isinstance(doc_links, list) else None
                # Upsert: ON CONFLICT keeps the existing created_at and
                # refreshes only the mutable fields. New rows get
                # CURRENT_TIMESTAMP via the column's DEFAULT.
                self.conn.execute(
                    """INSERT INTO marketplace_plugins
                        (marketplace_id, name, description, version, author_name,
                         homepage, category, source_type, source_spec, raw,
                         cover_photo_url, video_url, doc_links, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (marketplace_id, name) DO UPDATE SET
                        description     = EXCLUDED.description,
                        version         = EXCLUDED.version,
                        author_name     = EXCLUDED.author_name,
                        homepage        = EXCLUDED.homepage,
                        category        = EXCLUDED.category,
                        source_type     = EXCLUDED.source_type,
                        source_spec     = EXCLUDED.source_spec,
                        raw             = EXCLUDED.raw,
                        cover_photo_url = EXCLUDED.cover_photo_url,
                        video_url       = EXCLUDED.video_url,
                        doc_links       = EXCLUDED.doc_links,
                        updated_at      = EXCLUDED.updated_at""",
                    [
                        marketplace_id,
                        name,
                        p.get("description"),
                        p.get("version"),
                        author_name,
                        p.get("homepage"),
                        p.get("category"),
                        source_type,
                        source_spec_json,
                        raw_json,
                        p.get("cover_photo_url"),
                        p.get("video_url"),
                        doc_links_json,
                        now,
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sum(1 for p in plugins_list if (p.get("name") or "").strip())

    def clear_for_marketplace(self, marketplace_id: str) -> None:
        self.conn.execute(
            "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
            [marketplace_id],
        )

    def set_admin_disabled(self, marketplace_id: str, plugin_name: str, disabled: bool) -> bool:
        """Toggle the per-plugin admin disable flag.

        Returns True when the row existed and was updated, False when the
        (marketplace_id, plugin_name) pair is not in the table (no-op).
        Disabled plugins are filtered from the served feed for all callers
        regardless of their RBAC grants — distinct from per-user opt-outs.

        Disabling used to clear ``is_system`` in the same UPDATE, and
        re-enabling deliberately did not restore it. That contract has no
        home now the flag is a grant (0098): a grant is not touched by
        disabling, so re-enabling a plugin restores exactly the reach it had.
        Availability and distribution are separate switches on separate
        pages, which is the point.
        """
        # DuckDB does not populate cursor.rowcount for UPDATE (it stays -1/0),
        # so we can't trust it to detect whether a row matched. RETURNING is
        # deterministic on both engines: one row per updated row.
        updated = self.conn.execute(
            "UPDATE marketplace_plugins SET admin_disabled = ? "
            "WHERE marketplace_id = ? AND name = ? RETURNING name",
            [bool(disabled), marketplace_id, plugin_name],
        ).fetchall()
        return len(updated) > 0

    def list_admin_disabled(self, marketplace_id: str) -> List[str]:
        """Return the names of plugins that have admin_disabled=TRUE for a marketplace."""
        rows = self.conn.execute(
            "SELECT name FROM marketplace_plugins WHERE marketplace_id = ? AND admin_disabled = TRUE",
            [marketplace_id],
        ).fetchall()
        return [r[0] for r in rows]



def _classify_source(source: Optional[Any]) -> Optional[str]:
    """Return a coarse label for the `source` field of a plugin entry.

    Matches the Claude Code marketplace spec (code.claude.com/docs/plugin-marketplaces):
    relative-path string, or one of {github, url, git-subdir, npm}.
    """
    if source is None:
        return None
    if isinstance(source, str):
        return "path"
    if isinstance(source, dict):
        t = source.get("source")
        if isinstance(t, str):
            return t
    return "unknown"
