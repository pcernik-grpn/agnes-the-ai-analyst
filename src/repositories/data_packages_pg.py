"""Postgres-backed repository for ``data_packages`` + ``data_package_tables``.

Mirrors ``src/repositories/data_packages.py`` (the DuckDB impl) on the
``DataPackagesRepository`` public surface. Cross-engine parity is covered
by ``tests/test_data_packages_contract.py`` (Task 1D.1).

Implementation differences vs. DuckDB:

- JSON list columns (``tags``, ``when_to_use``, ``when_not_to_use``,
  ``example_questions``) are stored as JSONB. Writes go through
  ``CAST(:p AS JSONB)`` with a ``json.dumps(value)`` bind; reads come
  back as native Python lists/dicts via psycopg's adapter — no
  manual ``json.loads`` round-trip on the read path (unlike the DuckDB
  side which json-encodes VARCHAR and decodes on every fetch).
- ``INSERT ... ON CONFLICT (package_id, table_id) DO NOTHING`` on the
  junction; the boolean return ("was it actually inserted?") comes from
  ``cursor.rowcount`` rather than a pre-SELECT race window.
- ``data_package_tables.package_id`` carries ``ON DELETE CASCADE`` in PG
  (the DuckDB constraint surface is narrower; the DuckDB sibling has to
  clear the junction manually in ``hard_delete``). The PG impl keeps the
  explicit junction wipe in ``hard_delete`` for parity but it's
  belt-and-braces — the FK would handle it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


# Subset of writable columns that carry a JSON list. Used by ``create`` /
# ``update`` to know which bind params need a ``CAST(:p AS JSONB)`` wrap.
_JSON_COLUMNS = {"tags", "when_to_use", "when_not_to_use", "example_questions"}


def _json_param(v: Optional[List[Any]]) -> Optional[str]:
    """Serialize list to JSON text for the ``CAST(:p AS JSONB)`` bind.

    ``None`` passes through unchanged so the DB stores SQL NULL, not the
    JSON ``null`` literal.
    """
    if v is None:
        return None
    return json.dumps(v)


class DataPackagesPgRepository:
    """Postgres twin of ``DataPackagesRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        slug: str,
        description: Optional[str],
        icon: Optional[str],
        color: Optional[str],
        created_by: str,
        cover_image_url: Optional[str] = None,
        status: str = "prod",
        category: Optional[str] = None,
        # v113: 'user' | 'organization'. Defaults to 'user' so a package is
        # never silently promoted — mirrors the DuckDB sibling.
        publisher_kind: str = "user",
        owner_name: Optional[str] = None,
        owner_team: Optional[str] = None,
        tags: Optional[List[str]] = None,
        long_description: Optional[str] = None,
        when_to_use: Optional[List[str]] = None,
        when_not_to_use: Optional[List[str]] = None,
        example_questions: Optional[List[str]] = None,
    ) -> str:
        """Insert a new package; returns the generated id.

        Raises an ``IntegrityError`` if ``slug`` collides — the UNIQUE
        constraint on ``data_packages.slug`` is the source of truth.
        """
        pkg_id = "pkg_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO data_packages
                      (id, slug, name, description, icon, color,
                       cover_image_url, status, category, publisher_kind,
                       owner_name, owner_team,
                       tags, long_description,
                       when_to_use, when_not_to_use, example_questions,
                       created_by)
                    VALUES
                      (:id, :slug, :name, :description, :icon, :color,
                       :cover_image_url, :status, :category, :publisher_kind,
                       :owner_name, :owner_team,
                       CAST(:tags AS JSONB), :long_description,
                       CAST(:when_to_use AS JSONB),
                       CAST(:when_not_to_use AS JSONB),
                       CAST(:example_questions AS JSONB),
                       :created_by)
                    """
                ),
                {
                    "id": pkg_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "icon": icon,
                    "color": color,
                    "cover_image_url": cover_image_url,
                    "status": status or "prod",
                    "category": category,
                    "publisher_kind": (publisher_kind if publisher_kind in ("user", "organization") else "user"),
                    "owner_name": owner_name,
                    "owner_team": owner_team,
                    "tags": _json_param(tags),
                    "long_description": long_description,
                    "when_to_use": _json_param(when_to_use),
                    "when_not_to_use": _json_param(when_not_to_use),
                    "example_questions": _json_param(example_questions),
                    "created_by": created_by,
                },
            )
        return pkg_id

    @staticmethod
    def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        """Default JSON list columns to ``[]`` when NULL.

        psycopg returns JSONB as native Python lists/dicts already, so
        the only normalisation needed is the NULL-to-empty-list defaulting
        that mirrors the DuckDB sibling's ``_decode_row``.

        v113: also clamps ``publisher_kind`` to the closed enum, for the same
        reason as the DuckDB sibling — a pre-column row reads NULL, and a
        template comparing NULL to 'organization' renders the wrong marker
        silently instead of failing.
        """
        if "publisher_kind" in row and row.get("publisher_kind") not in ("user", "organization"):
            row["publisher_kind"] = "user"
        for k in _JSON_COLUMNS:
            v = row.get(k)
            if v is None:
                row[k] = []
            elif isinstance(v, str):
                # Defensive: handle legacy string-encoded rows (none
                # exist in PG today, but cheap to be safe).
                try:
                    parsed = json.loads(v)
                    row[k] = parsed if isinstance(parsed, list) else []
                except (ValueError, TypeError):
                    row[k] = []
        return row

    def get(self, pkg_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(f"SELECT * FROM data_packages WHERE id = :id{guard}"),
                    {"id": pkg_id},
                )
                .mappings()
                .first()
            )
        return self._normalize_row(dict(row)) if row else None

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Resolve a package by its stable slug. Soft-deleted rows are hidden
        by default, exactly like :meth:`get`.

        ``include_deleted=True`` is what a *seeder* needs: the slug is UNIQUE
        across live AND deleted rows, so "no live row" alone cannot tell
        "never created" from "an admin deleted it" — and re-creating on the
        latter would resurrect a package the admin deliberately removed.
        """
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT id FROM data_packages WHERE slug = :slug{guard}"),
                {"slug": slug},
            ).first()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live packages, name-ordered, with optional name search."""
        query = "SELECT * FROM data_packages WHERE deleted_at IS NULL"
        params: Dict[str, Any] = {}
        if search:
            query += " AND name ILIKE :search"
            params["search"] = f"%{search}%"
        query += " ORDER BY name LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [self._normalize_row(dict(r)) for r in rows]

    def update(
        self,
        pkg_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        cover_image_url: Optional[str] = None,
        clear_cover_image: bool = False,
        status: Optional[str] = None,
        category: Optional[str] = None,
        clear_category: bool = False,
        # v113: 'user' | 'organization'; an unrecognized value is ignored
        # rather than written, mirroring the DuckDB sibling.
        publisher_kind: Optional[str] = None,
        owner_name: Optional[str] = None,
        owner_team: Optional[str] = None,
        tags: Optional[List[str]] = None,
        long_description: Optional[str] = None,
        when_to_use: Optional[List[str]] = None,
        when_not_to_use: Optional[List[str]] = None,
        example_questions: Optional[List[str]] = None,
    ) -> None:
        """Partial update. Matches the DuckDB sibling's Optional-is-no-op
        contract; ``clear_cover_image`` / ``clear_category`` are the
        explicit NULL-clearing escape hatches. JSON list fields accept
        an empty list to clear (writes ``"[]"`` JSON, normalises back
        to ``[]`` on read)."""
        # Plain string/text columns
        plain_candidates = {
            "name": name,
            "description": description,
            "icon": icon,
            "color": color,
            "status": status,
            "owner_name": owner_name,
            "owner_team": owner_team,
            "long_description": long_description,
        }
        json_candidates = {
            "tags": tags,
            "when_to_use": when_to_use,
            "when_not_to_use": when_not_to_use,
            "example_questions": example_questions,
        }

        fields: List[str] = []
        params: Dict[str, Any] = {}

        for col, val in plain_candidates.items():
            if val is not None:
                fields.append(f"{col} = :{col}")
                params[col] = val

        # Not in plain_candidates: this one is a closed enum, so an
        # unrecognized value must be dropped rather than written.
        if publisher_kind in ("user", "organization"):
            fields.append("publisher_kind = :publisher_kind")
            params["publisher_kind"] = publisher_kind

        # cover_image_url has the active-clear escape hatch
        if clear_cover_image:
            fields.append("cover_image_url = NULL")
        elif cover_image_url is not None:
            fields.append("cover_image_url = :cover_image_url")
            params["cover_image_url"] = cover_image_url

        if clear_category:
            fields.append("category = NULL")
        elif category is not None:
            fields.append("category = :category")
            params["category"] = category

        for col, val in json_candidates.items():
            if val is not None:
                fields.append(f"{col} = CAST(:{col} AS JSONB)")
                params[col] = json.dumps(val)

        if not fields:
            return

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params["pkg_id"] = pkg_id
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE data_packages SET {', '.join(fields)} WHERE id = :pkg_id"),
                params,
            )

    def delete(self, pkg_id: str) -> None:
        """Soft-delete: sets ``deleted_at`` to now. Junction rows and any
        ``resource_grants`` referencing this package are preserved so the
        undo flow can restore the package whole."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_packages SET deleted_at = CURRENT_TIMESTAMP WHERE id = :id"),
                {"id": pkg_id},
            )

    def restore(self, pkg_id: str) -> None:
        """Reverse a soft delete. Idempotent — no-op if the row isn't
        currently soft-deleted (guards against double-undo)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_packages SET deleted_at = NULL WHERE id = :id"),
                {"id": pkg_id},
            )

    def hard_delete(self, pkg_id: str) -> None:
        """Permanent delete — wipes the row + junction. The PG schema has
        ``ON DELETE CASCADE`` on ``data_package_tables.package_id`` so
        the explicit junction clear is belt-and-braces parity with the
        DuckDB sibling (whose constraint surface can't cascade)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_package_tables WHERE package_id = :id"),
                {"id": pkg_id},
            )
            conn.execute(
                sa.text("DELETE FROM data_packages WHERE id = :id"),
                {"id": pkg_id},
            )

    # ------------------------------------------------------------------
    # Junction (package ↔ tables)
    # ------------------------------------------------------------------

    def add_table(self, pkg_id: str, table_id: str, *, added_by: str) -> bool:
        """Insert a junction row. Returns True iff a new row was inserted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    """
                    INSERT INTO data_package_tables
                      (package_id, table_id, added_by)
                    VALUES (:pkg_id, :table_id, :added_by)
                    ON CONFLICT (package_id, table_id) DO NOTHING
                    """
                ),
                {
                    "pkg_id": pkg_id,
                    "table_id": table_id,
                    "added_by": added_by,
                },
            )
            return (result.rowcount or 0) > 0

    def remove_table(self, pkg_id: str, table_id: str) -> bool:
        """Drop a junction row. Returns True iff a row was deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM data_package_tables WHERE package_id = :pkg_id AND table_id = :table_id"),
                {"pkg_id": pkg_id, "table_id": table_id},
            )
            return (result.rowcount or 0) > 0

    def list_tables(self, pkg_id: str) -> List[Dict[str, Any]]:
        """Tables belonging to a package (name-ordered).

        Mirrors the DuckDB sibling: joins the junction against
        ``table_registry`` and returns ``{id, name}`` pairs.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                    SELECT tr.id AS id, tr.name AS name
                    FROM data_package_tables dpt
                    JOIN table_registry tr ON tr.id = dpt.table_id
                    WHERE dpt.package_id = :pkg_id
                    ORDER BY tr.name
                    """
                    ),
                    {"pkg_id": pkg_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def add_tool(self, pkg_id: str, tool_id: str) -> bool:
        """Attach an MCP tool to a package. Returns True iff a row was inserted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT INTO data_package_tools (package_id, tool_id) "
                    "VALUES (:pkg_id, :tool_id) ON CONFLICT DO NOTHING"
                ),
                {"pkg_id": pkg_id, "tool_id": tool_id},
            )
        return (result.rowcount or 0) > 0

    def remove_tool(self, pkg_id: str, tool_id: str) -> bool:
        """Detach an MCP tool from a package. Returns True iff a row was deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM data_package_tools WHERE package_id = :pkg_id AND tool_id = :tool_id"),
                {"pkg_id": pkg_id, "tool_id": tool_id},
            )
        return (result.rowcount or 0) > 0

    def list_tools(self, pkg_id: str) -> List[Dict[str, Any]]:
        """MCP tools attached to a package (exposed-name ordered).

        Mirrors the DuckDB sibling: joins tool_registry + mcp_sources.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                    SELECT t.tool_id, t.exposed_name, t.description, t.mode,
                           s.name AS source_name
                    FROM data_package_tools dpt
                    JOIN tool_registry t ON t.tool_id = dpt.tool_id
                    JOIN mcp_sources s   ON s.id = t.source_id
                    WHERE dpt.package_id = :pkg_id
                    ORDER BY t.exposed_name
                    """
                    ),
                    {"pkg_id": pkg_id},
                )
                .mappings()
                .all()
            )
        return [
            {
                "tool_id": r["tool_id"],
                "exposed_name": r["exposed_name"],
                "description": r["description"],
                "mode": r["mode"],
                "source_name": r["source_name"],
            }
            for r in rows
        ]

    def list_member_table_ids(self, package_ids: set) -> List[str]:
        """Distinct table IDs across the given live packages. Targeted query
        for per-user access-check paths (avoids a full-table scan).
        Returns ``[]`` when *package_ids* is empty.
        """
        if not package_ids:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT DISTINCT dpt.table_id "
                    "FROM data_package_tables dpt "
                    "JOIN data_packages dp ON dp.id = dpt.package_id "
                    "WHERE dp.deleted_at IS NULL AND dpt.package_id = ANY(:pkg_ids)"
                ),
                {"pkg_ids": list(package_ids)},
            ).all()
        return [r[0] for r in rows]

    def list_member_ids_bulk(self) -> Dict[str, List[str]]:
        """Bulk fetch of the ``{package_id → [table_id, ...]}`` mapping
        for every live (non-soft-deleted) package. Empty packages are
        omitted from the mapping — callers must default to ``[]`` on
        lookup misses."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT dpt.package_id, dpt.table_id
                    FROM data_package_tables dpt
                    JOIN data_packages dp ON dp.id = dpt.package_id
                    WHERE dp.deleted_at IS NULL
                    ORDER BY dpt.package_id
                    """
                )
            ).all()
        out: Dict[str, List[str]] = {}
        for pkg_id, table_id in rows:
            out.setdefault(pkg_id, []).append(table_id)
        return out

    def list_packages_of_table(self, table_id: str) -> List[Dict[str, Any]]:
        """Packages a given table belongs to (name-ordered)."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                    SELECT dp.id, dp.slug, dp.name, dp.description,
                           dp.icon, dp.color, dp.cover_image_url
                    FROM data_package_tables dpt
                    JOIN data_packages dp ON dp.id = dpt.package_id
                    WHERE dpt.table_id = :table_id
                    ORDER BY dp.name
                    """
                    ),
                    {"table_id": table_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]
