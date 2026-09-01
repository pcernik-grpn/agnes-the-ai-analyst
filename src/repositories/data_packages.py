"""Repository for ``data_packages`` + ``data_package_tables`` (v49).

A Data Package is an admin-curated bundle of tables (M:N to ``table_registry``)
that serves as the unit of "Add to stack" on /catalog. Seeded inline from the
``/admin/tables`` typeahead per Section 7 of the unified-stack design doc.

The FK on ``data_package_tables.package_id REFERENCES data_packages(id)`` does
not declare ``ON DELETE CASCADE`` (DuckDB constraint surface is narrower than
Postgres). ``delete()`` clears the junction explicitly so callers don't have
to remember the order.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class DataPackagesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # -- CRUD --------------------------------------------------------------

    # v51: column list — kept as a single constant so the SELECT in every
    # method stays in sync after future schema additions. Touch this when
    # adding columns.
    _COLS = [
        "id",
        "slug",
        "name",
        "description",
        "icon",
        "color",
        "cover_image_url",
        "status",
        "category",
        # v113: stored trust axis, same vocabulary as store_entities
        # ('user' | 'organization'). Replaces the render-time-derived
        # `curated` badge — see the DDL comment in src/db.py.
        "publisher_kind",
        # v56: extended content surface for /catalog/p/<slug> rewrite.
        # JSON columns ("tags", "when_to_use", "when_not_to_use",
        # "example_questions") are stored as VARCHAR and decoded on read
        # by ``_decode_row`` below; long_description stays as TEXT.
        "owner_name",
        "owner_team",
        "tags",
        "long_description",
        "when_to_use",
        "when_not_to_use",
        "example_questions",
        "created_by",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)
    # Subset of _COLS that carry a JSON list. Decoded on read; NULL → [].
    _JSON_LIST_COLS = ("tags", "when_to_use", "when_not_to_use", "example_questions")

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
        # never silently promoted — the caller (the admin create endpoint)
        # decides, which is the whole point of storing it rather than
        # deriving it from the creator's current group membership.
        publisher_kind: str = "user",
        # v56 extended content — all optional, all NULL when unset.
        owner_name: Optional[str] = None,
        owner_team: Optional[str] = None,
        tags: Optional[List[str]] = None,
        long_description: Optional[str] = None,
        when_to_use: Optional[List[str]] = None,
        when_not_to_use: Optional[List[str]] = None,
        example_questions: Optional[List[str]] = None,
    ) -> str:
        """Insert a new package; returns the generated id.

        Raises ``duckdb.ConstraintException`` if ``slug`` collides — the
        UNIQUE constraint on ``data_packages.slug`` is the source of truth
        (no pre-check race window).
        """
        import json as _json

        pkg_id = "pkg_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_packages"
            "(id, slug, name, description, icon, color, cover_image_url, "
            " status, category, publisher_kind, owner_name, owner_team, tags, "
            " long_description, when_to_use, when_not_to_use, "
            " example_questions, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                pkg_id,
                slug,
                name,
                description,
                icon,
                color,
                cover_image_url,
                status or "prod",
                category,
                publisher_kind if publisher_kind in ("user", "organization") else "user",
                owner_name,
                owner_team,
                _json.dumps(tags) if tags is not None else None,
                long_description,
                _json.dumps(when_to_use) if when_to_use is not None else None,
                _json.dumps(when_not_to_use) if when_not_to_use is not None else None,
                _json.dumps(example_questions) if example_questions is not None else None,
                created_by,
            ],
        )
        return pkg_id

    @classmethod
    def _decode_row(cls, row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """v56: decode JSON-list columns to Python lists. NULL → [].

        v113: also normalizes ``publisher_kind`` to the closed enum, so every
        read path can treat it as non-null. A row written before the column
        existed reads NULL (the DEFAULT applies to inserts only), and the
        migration's own normalize does not help a database whose ladder has
        not run yet — a template comparing NULL to 'organization' would
        silently render the wrong marker rather than fail.
        """
        import json as _json

        if row_dict.get("publisher_kind") not in ("user", "organization"):
            row_dict["publisher_kind"] = "user"
        for k in cls._JSON_LIST_COLS:
            v = row_dict.get(k)
            if v is None or v == "":
                row_dict[k] = []
                continue
            if isinstance(v, list):
                continue
            try:
                parsed = _json.loads(v) if isinstance(v, str) else v
                row_dict[k] = parsed if isinstance(parsed, list) else []
            except Exception:
                row_dict[k] = []
        return row_dict

    def get(self, pkg_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        # v54: soft-deleted rows are hidden by default. include_deleted=True
        # is the escape hatch the /restore endpoint uses to find them.
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_packages WHERE id = ?{guard}",
            [pkg_id],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Resolve a package by its stable slug. Soft-deleted rows are hidden
        by default, exactly like :meth:`get`.

        ``include_deleted=True`` is what a *seeder* needs: the slug is UNIQUE
        across live AND deleted rows, so "no live row" alone cannot tell
        "never created" from "an admin deleted it" — and re-creating on the
        latter would resurrect a package the admin deliberately removed.
        """
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT id FROM data_packages WHERE slug = ?{guard}",
            [slug],
        ).fetchone()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        # v54: filter soft-deleted rows; combined with optional search.
        query = f"SELECT {self._SELECT} FROM data_packages WHERE deleted_at IS NULL"
        params: List[Any] = []
        if search:
            query += " AND name ILIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

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
        # v113: 'user' | 'organization'. Same Optional-is-no-op contract; an
        # unrecognized value is ignored rather than written, so the column
        # stays a closed enum every read path can trust.
        publisher_kind: Optional[str] = None,
        # v56 extended content. Optional-is-no-op; pass an empty list
        # explicitly to clear a JSON-list column (json.dumps([]) writes
        # "[]" which decodes back to []).
        owner_name: Optional[str] = None,
        owner_team: Optional[str] = None,
        tags: Optional[List[str]] = None,
        long_description: Optional[str] = None,
        when_to_use: Optional[List[str]] = None,
        when_not_to_use: Optional[List[str]] = None,
        example_questions: Optional[List[str]] = None,
    ) -> None:
        """Partial update. ``cover_image_url`` follows the same Optional-is-no-op
        contract as the rest; pass ``clear_cover_image=True`` to actively NULL
        the column (admin removed the uploaded image). ``clear_category`` is
        the same NULL-clearing escape hatch for the v51 category field.

        v56 extended fields use the same Optional-is-no-op contract.
        JSON-list fields accept an empty list to explicitly clear (writes
        ``"[]"``, decodes back to ``[]``). Pass ``None`` to skip.
        """
        import json as _json

        fields: List[str] = []
        params: List[Any] = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if description is not None:
            fields.append("description = ?")
            params.append(description)
        if icon is not None:
            fields.append("icon = ?")
            params.append(icon)
        if color is not None:
            fields.append("color = ?")
            params.append(color)
        if clear_cover_image:
            fields.append("cover_image_url = NULL")
        elif cover_image_url is not None:
            fields.append("cover_image_url = ?")
            params.append(cover_image_url)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if publisher_kind in ("user", "organization"):
            fields.append("publisher_kind = ?")
            params.append(publisher_kind)
        if clear_category:
            fields.append("category = NULL")
        elif category is not None:
            fields.append("category = ?")
            params.append(category)
        # v56 — additive surface; preserves the existing Optional-is-no-op
        # contract for every existing caller.
        if owner_name is not None:
            fields.append("owner_name = ?")
            params.append(owner_name)
        if owner_team is not None:
            fields.append("owner_team = ?")
            params.append(owner_team)
        if tags is not None:
            fields.append("tags = ?")
            params.append(_json.dumps(tags))
        if long_description is not None:
            fields.append("long_description = ?")
            params.append(long_description)
        if when_to_use is not None:
            fields.append("when_to_use = ?")
            params.append(_json.dumps(when_to_use))
        if when_not_to_use is not None:
            fields.append("when_not_to_use = ?")
            params.append(_json.dumps(when_not_to_use))
        if example_questions is not None:
            fields.append("example_questions = ?")
            params.append(_json.dumps(example_questions))
        if not fields:
            return
        fields.append("updated_at = current_timestamp")
        params.append(pkg_id)
        self.conn.execute(
            f"UPDATE data_packages SET {', '.join(fields)} WHERE id = ?",
            params,
        )

    def delete(self, pkg_id: str) -> None:
        """v54: soft delete — sets ``deleted_at`` to now. The junction
        (``data_package_tables``) and any ``resource_grants`` referencing
        this package are intentionally preserved so the undo flow can
        restore the package whole. ``list()`` / ``get()`` filter
        ``deleted_at IS NULL`` so soft-deleted rows are invisible to
        every caller except the explicit ``restore`` path.

        Hard-delete (with junction cascade) is available via
        ``hard_delete`` — keep it for future admin cleanup workflows.
        """
        self.conn.execute(
            "UPDATE data_packages SET deleted_at = current_timestamp WHERE id = ?",
            [pkg_id],
        )

    def restore(self, pkg_id: str) -> None:
        """Reverse a soft delete. No-op if the row isn't currently
        soft-deleted (idempotent — guards against double-undo)."""
        self.conn.execute(
            "UPDATE data_packages SET deleted_at = NULL WHERE id = ?",
            [pkg_id],
        )

    def hard_delete(self, pkg_id: str) -> None:
        """Permanent delete — wipes the row + junction. Use when an admin
        wants the resource gone for good. Not currently wired into any
        endpoint; kept for completeness."""
        self.conn.execute("DELETE FROM data_package_tables WHERE package_id = ?", [pkg_id])
        self.conn.execute("DELETE FROM data_packages WHERE id = ?", [pkg_id])

    # -- Junction (package ↔ tables) ---------------------------------------

    def add_table(self, pkg_id: str, table_id: str, *, added_by: str) -> bool:
        """Insert a junction row. Returns True iff a new row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO data_package_tables(package_id, table_id, added_by) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [pkg_id, table_id, added_by],
        )
        return True

    def remove_table(self, pkg_id: str, table_id: str) -> bool:
        """Drop a junction row. Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        )
        return True

    def list_tables(self, pkg_id: str) -> List[Dict[str, Any]]:
        """Tables belonging to a package (name-ordered)."""
        rows = self.conn.execute(
            "SELECT tr.id, tr.name "
            "FROM data_package_tables dpt "
            "JOIN table_registry tr ON tr.id = dpt.table_id "
            "WHERE dpt.package_id = ? ORDER BY tr.name",
            [pkg_id],
        ).fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]

    # --- MCP tool junction (v64, RFC #461 §6) -----------------------------
    #
    # Symmetric with the table-junction methods above. ``related_tools``
    # surfaces alongside ``tables`` in the package detail response so an
    # analyst opening the "Customer Lifecycle" package sees both the
    # orders table and a passthrough ``crm.searchAccounts`` tool in the
    # same UI.

    def add_tool(self, pkg_id: str, tool_id: str) -> bool:
        """Attach an MCP tool to a package. Returns True iff a row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tools WHERE package_id = ? AND tool_id = ?",
            [pkg_id, tool_id],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO data_package_tools(package_id, tool_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            [pkg_id, tool_id],
        )
        return True

    def remove_tool(self, pkg_id: str, tool_id: str) -> bool:
        """Detach an MCP tool from a package. Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tools WHERE package_id = ? AND tool_id = ?",
            [pkg_id, tool_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM data_package_tools WHERE package_id = ? AND tool_id = ?",
            [pkg_id, tool_id],
        )
        return True

    def list_tools(self, pkg_id: str) -> List[Dict[str, Any]]:
        """MCP tools attached to a package (exposed-name ordered).

        Joins tool_registry + mcp_sources so the response carries enough
        for a UI to render: tool_id, exposed_name (display), description,
        source name (badge), and mode (materialize vs passthrough).
        """
        rows = self.conn.execute(
            """SELECT t.tool_id, t.exposed_name, t.description, t.mode,
                      s.name AS source_name
                 FROM data_package_tools dpt
                 JOIN tool_registry t ON t.tool_id = dpt.tool_id
                 JOIN mcp_sources s   ON s.id     = t.source_id
                WHERE dpt.package_id = ?
                ORDER BY t.exposed_name""",
            [pkg_id],
        ).fetchall()
        return [
            {
                "tool_id": r[0],
                "exposed_name": r[1],
                "description": r[2],
                "mode": r[3],
                "source_name": r[4],
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
        rows = self.conn.execute(
            "SELECT DISTINCT dpt.table_id "
            "FROM data_package_tables dpt "
            "JOIN data_packages dp ON dp.id = dpt.package_id "
            "WHERE dp.deleted_at IS NULL AND dpt.package_id = ANY(?)",
            [list(package_ids)],
        ).fetchall()
        return [r[0] for r in rows]

    def list_member_ids_bulk(self) -> Dict[str, List[str]]:
        """v54: single-query bulk fetch of the {package_id → [table_id, …]}
        mapping for every live (non-soft-deleted) package. Used by the
        /admin/tables page hydrator to collapse the prior N+1 fan-out
        (one ``list_tables(pkg_id)`` call per package after the list)
        into a single round-trip. Empty packages are omitted from the
        mapping — callers must default to ``[]`` on lookup misses.
        """
        rows = self.conn.execute(
            "SELECT dpt.package_id, dpt.table_id "
            "FROM data_package_tables dpt "
            "JOIN data_packages dp ON dp.id = dpt.package_id "
            "WHERE dp.deleted_at IS NULL "
            "ORDER BY dpt.package_id"
        ).fetchall()
        out: Dict[str, List[str]] = {}
        for pkg_id, table_id in rows:
            out.setdefault(pkg_id, []).append(table_id)
        return out

    def list_packages_of_table(self, table_id: str) -> List[Dict[str, Any]]:
        """Packages a given table belongs to (name-ordered)."""
        rows = self.conn.execute(
            "SELECT dp.id, dp.slug, dp.name, dp.description, dp.icon, dp.color, "
            "       dp.cover_image_url "
            "FROM data_package_tables dpt "
            "JOIN data_packages dp ON dp.id = dpt.package_id "
            "WHERE dpt.table_id = ? ORDER BY dp.name",
            [table_id],
        ).fetchall()
        return [
            {
                "id": r[0],
                "slug": r[1],
                "name": r[2],
                "description": r[3],
                "icon": r[4],
                "color": r[5],
                "cover_image_url": r[6],
            }
            for r in rows
        ]
