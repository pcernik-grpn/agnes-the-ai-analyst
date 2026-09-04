"""Postgres-backed marketplace_plugins repository.

Mirrors ``src/repositories/marketplace_plugins.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.grant_scopes import EVERYONE as SCOPE_EVERYONE
from src.repositories.marketplace_plugins import _classify_source


class MarketplacePluginsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _normalize_row(d: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("source_spec", "raw", "doc_links"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass
        return d

    def list_distinct_names(self) -> List[str]:
        """PG sibling of the DuckDB ``list_distinct_names``."""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT DISTINCT name FROM marketplace_plugins")).all()
        return [r[0] for r in rows]

    def list_for_marketplace(self, marketplace_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM marketplace_plugins WHERE marketplace_id = :m ORDER BY name"),
                    {"m": marketplace_id},
                )
                .mappings()
                .all()
            )
        return [self._normalize_row(dict(r)) for r in rows]

    def get(self, marketplace_id: str, name: str) -> Optional[Dict[str, Any]]:
        """Fetch a single plugin row by (marketplace_id, name), or None.

        Parity with the DuckDB repo — backs the curated install/uninstall
        existence and admin-disabled checks through the factory.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM marketplace_plugins WHERE marketplace_id = :m AND name = :n"),
                    {"m": marketplace_id, "n": name},
                )
                .mappings()
                .first()
            )
        return self._normalize_row(dict(row)) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text("SELECT * FROM marketplace_plugins ORDER BY marketplace_id, name"))
                .mappings()
                .all()
            )
        return [self._normalize_row(dict(r)) for r in rows]

    def count_by_marketplace(self) -> Dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT marketplace_id, COUNT(*) FROM marketplace_plugins GROUP BY marketplace_id")
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def list_granted_for_groups(
        self,
        group_ids: Iterable[str],
    ) -> List[Dict[str, Any]]:
        """PG mirror of ``MarketplacePluginsRepository.list_granted_for_groups``."""
        gids = list(group_ids)
        # An empty group list is deliberately NOT an early return: an
        # everyone-scoped grant reaches an account regardless of membership,
        # so the query still has work to do.
        gid_keys: List[str] = []
        params: Dict[str, Any] = {"everyone_scope": SCOPE_EVERYONE}
        for i, gid in enumerate(gids):
            k = f"g_{i}"
            gid_keys.append(f":{k}")
            params[k] = gid
        # Semi-join off ``marketplace_plugins``, EXISTS rather than a JOIN so
        # a plugin granted by several of the caller's groups yields one row
        # without a DISTINCT. ``mr.registered_at`` rides the projection for
        # the ORDER BY.
        #
        # The scope term is where this diverges from the DuckDB sibling: an
        # everyone-scoped grant (0098) reaches an account with NO group
        # memberships at all, so it cannot be folded into the group IN-list.
        # It replaces the `mp.is_system = TRUE` branch that used to sit here,
        # and reaches strictly the same people the flag did.
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT mp.marketplace_id, mp.name, mp.version, mp.raw, "
                    "       mr.registered_at "
                    "FROM marketplace_plugins mp "
                    "JOIN marketplace_registry mr ON mr.id = mp.marketplace_id "
                    "WHERE mp.admin_disabled = FALSE "
                    "  AND EXISTS ("
                    "        SELECT 1 FROM resource_grants rg "
                    "        WHERE rg.resource_id = mp.marketplace_id || '/' || mp.name "
                    "          AND rg.resource_type = 'marketplace_plugin' "
                    "          AND (rg.scope = :everyone_scope "
                    f"               OR rg.group_id IN ({','.join(gid_keys) or 'NULL'}))"
                    "      ) "
                    "ORDER BY mr.registered_at, mp.name"
                ),
                params,
            ).all()
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
        # No early return on an empty group set, and the audience test below
        # is the same one `list_granted_for_groups` uses. These two used to
        # disagree: this method matched `group_id IN (...)` alone, so the
        # browse tab and the served feed showed a different set to any
        # account an everyone-scoped grant reached without a membership.
        gids = list(group_ids)
        gid_keys: List[str] = []
        params: Dict[str, Any] = {"everyone_scope": SCOPE_EVERYONE}
        for i, gid in enumerate(gids):
            k = f"g_{i}"
            gid_keys.append(f":{k}")
            params[k] = gid

        where = [
            f"(rg.scope = :everyone_scope OR rg.group_id IN ({','.join(gid_keys) or 'NULL'}))",
            "rg.resource_type = 'marketplace_plugin'",
            "rg.resource_id = mp.marketplace_id || '/' || mp.name",
            # Admin-disabled built-in plugins are hidden from the browse listing
            # too — mirrors the served-feed filter in list_granted_for_groups.
            "mp.admin_disabled = FALSE",
        ]
        if search:
            where.append(
                "(LOWER(mp.name) LIKE :needle OR LOWER(COALESCE(mp.description,'')) LIKE :needle "
                "OR LOWER(COALESCE(mp.author_name,'')) LIKE :needle "
                "OR LOWER(COALESCE(mp.category,'')) LIKE :needle)"
            )
            params["needle"] = f"%{search.lower()}%"
        if category:
            if category == "Other":
                where.append("(mp.category IS NULL OR TRIM(mp.category) = '' OR mp.category = :cat)")
            else:
                where.append("mp.category = :cat")
            params["cat"] = category

        where_sql = " AND ".join(where)

        with self._engine.connect() as conn:
            total_row = conn.execute(
                sa.text(
                    f"SELECT COUNT(DISTINCT (mp.marketplace_id, mp.name)) "
                    f"FROM marketplace_plugins mp "
                    f"JOIN resource_grants rg ON TRUE "
                    f"WHERE {where_sql}"
                ),
                params,
            ).first()
            total = int(total_row[0]) if total_row else 0
            if total == 0:
                return ([], 0)

            list_params = {**params, "limit": int(limit), "offset": int(skip)}
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT DISTINCT mp.marketplace_id, mp.name, mp.description, mp.version, "
                        f"       mp.author_name, mp.homepage, mp.category, mp.source_type, "
                        f"       mp.source_spec, mp.raw, mp.cover_photo_url, mp.video_url, "
                        f"       mp.doc_links, mp.created_at, mp.updated_at "
                        f"FROM marketplace_plugins mp "
                        f"JOIN resource_grants rg ON TRUE "
                        f"WHERE {where_sql} "
                        f"ORDER BY mp.created_at DESC NULLS LAST, mp.name "
                        f"LIMIT :limit OFFSET :offset"
                    ),
                    list_params,
                )
                .mappings()
                .all()
            )
        return ([self._normalize_row(dict(r)) for r in rows], total)

    def category_counts(
        self,
        *,
        group_ids: Iterable[str],
    ) -> Dict[str, int]:
        # Same audience test as `list_with_filters` and
        # `list_granted_for_groups` — the category pills must count the set
        # the listing actually shows.
        gids = list(group_ids)
        gid_keys: List[str] = []
        params: Dict[str, Any] = {"everyone_scope": SCOPE_EVERYONE}
        for i, gid in enumerate(gids):
            k = f"g_{i}"
            gid_keys.append(f":{k}")
            params[k] = gid
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"SELECT COALESCE(NULLIF(TRIM(mp.category),''), 'Other') AS cat, "
                    f"       COUNT(DISTINCT (mp.marketplace_id, mp.name)) "
                    f"FROM marketplace_plugins mp "
                    f"JOIN resource_grants rg "
                    f"  ON rg.resource_id = mp.marketplace_id || '/' || mp.name "
                    f"WHERE (rg.scope = :everyone_scope "
                    f"       OR rg.group_id IN ({','.join(gid_keys) or 'NULL'})) "
                    f"  AND rg.resource_type = 'marketplace_plugin' "
                    f"  AND mp.admin_disabled = FALSE "
                    f"GROUP BY cat"
                ),
                params,
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def replace_for_marketplace(
        self,
        marketplace_id: str,
        plugins: Iterable[Dict[str, Any]],
    ) -> int:
        plugins_list = list(plugins)
        now = datetime.now(timezone.utc)
        valid_names = {(p.get("name") or "").strip() for p in plugins_list if (p.get("name") or "").strip()}

        with self._engine.begin() as conn:
            if valid_names:
                vn_keys = [f":vn_{i}" for i in range(len(valid_names))]
                params: Dict[str, Any] = {"mid": marketplace_id}
                for i, name in enumerate(valid_names):
                    params[f"vn_{i}"] = name
                conn.execute(
                    sa.text(
                        f"DELETE FROM marketplace_plugins "
                        f"WHERE marketplace_id = :mid AND name NOT IN ({','.join(vn_keys)})"
                    ),
                    params,
                )
            else:
                conn.execute(
                    sa.text("DELETE FROM marketplace_plugins WHERE marketplace_id = :mid"),
                    {"mid": marketplace_id},
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
                raw_payload = {k: v for k, v in p.items() if k not in ("cover_photo_url", "video_url", "doc_links")}
                raw_json = json.dumps(raw_payload)
                doc_links = p.get("doc_links")
                doc_links_json = json.dumps(doc_links) if isinstance(doc_links, list) else None
                conn.execute(
                    sa.text(
                        """INSERT INTO marketplace_plugins
                            (marketplace_id, name, description, version, author_name,
                             homepage, category, source_type, source_spec, raw,
                             cover_photo_url, video_url, doc_links, updated_at)
                        VALUES (:mid, :name, :desc, :ver, :an, :hp, :cat, :st,
                                CAST(:ss AS JSONB), CAST(:raw AS JSONB),
                                :cpu, :vu, CAST(:dl AS JSONB), :now)
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
                            updated_at      = EXCLUDED.updated_at"""
                    ),
                    {
                        "mid": marketplace_id,
                        "name": name,
                        "desc": p.get("description"),
                        "ver": p.get("version"),
                        "an": author_name,
                        "hp": p.get("homepage"),
                        "cat": p.get("category"),
                        "st": source_type,
                        "ss": source_spec_json,
                        "raw": raw_json,
                        "cpu": p.get("cover_photo_url"),
                        "vu": p.get("video_url"),
                        "dl": doc_links_json,
                        "now": now,
                    },
                )
        return sum(1 for p in plugins_list if (p.get("name") or "").strip())

    def clear_for_marketplace(self, marketplace_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM marketplace_plugins WHERE marketplace_id = :m"),
                {"m": marketplace_id},
            )

    def set_admin_disabled(self, marketplace_id: str, plugin_name: str, disabled: bool) -> bool:
        """Toggle the per-plugin admin disable flag.

        Returns True when the row existed and was updated, False when the
        (marketplace_id, plugin_name) pair is not in the table (no-op).

        See the DuckDB sibling for why disabling no longer clears a
        distribution flag: there is no flag, and a grant survives a
        disable/re-enable cycle unchanged.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE marketplace_plugins SET admin_disabled = :d "
                    "WHERE marketplace_id = :m AND name = :n"
                ),
                {"d": bool(disabled), "m": marketplace_id, "n": plugin_name},
            )
        return result.rowcount > 0

    def list_legacy_system_keys(self) -> List[Tuple[str, str]]:
        """The legacy ``is_system`` flag, for the one caller that still needs it.

        ``src.system_plugin_reconcile`` is the frozen DuckDB ladder's
        stand-in for migration 0098's step 4 — Alembic runs on Postgres only
        — and it cannot read this column any other way: the serve paths that
        used to (``list_granted_for_groups``, ``list_system_keys``) stopped,
        which is the whole point of 0098.

        Named *legacy* rather than restoring ``list_system_keys`` so nothing
        mistakes it for a live read. Both backends implement it identically:
        on Postgres 0098 clears the flag, so this returns nothing there after
        the migration has run, which is exactly the right answer.

        ``admin_disabled = FALSE`` is part of the match, not tidiness: both
        readers of the flag filtered on it, so a disabled plugin marked
        system reached NOBODY and must not be granted one.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT marketplace_id, name FROM marketplace_plugins "
                    "WHERE is_system = TRUE AND admin_disabled = FALSE"
                )
            ).all()
        return [(r[0], r[1]) for r in rows]

    def clear_legacy_system_flags(self) -> int:
        """Clear every legacy ``is_system`` flag. Returns rows affected.

        The last act of the DuckDB reconciliation, and what makes it
        idempotent: with the flag gone the next boot has nothing to convert.
        Also stops the column contradicting the grants for as long as it
        survives — the drop is the contract half of an expand/contract pair
        and ships a release later.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system = TRUE")
            )
        return int(result.rowcount or 0)

    def list_admin_disabled(self, marketplace_id: str) -> List[str]:
        """Return the names of plugins that have admin_disabled=TRUE for a marketplace."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT name FROM marketplace_plugins WHERE marketplace_id = :m AND admin_disabled = TRUE"),
                {"m": marketplace_id},
            ).all()
        return [r[0] for r in rows]


