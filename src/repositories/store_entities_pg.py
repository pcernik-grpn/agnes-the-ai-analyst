"""Postgres-backed store_entities repository.

Mirrors ``src/repositories/store_entities.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# v104 — duplicated verbatim from ``store_entities.py`` (same convention as
# ``resource_grants`` / ``resource_grants_pg`` for the ``requirement`` enum). See
# there for why 'organization' is stored rather than derived from Admin-group
# membership, and why there is no "unverified" label. Unlike DuckDB, PG enforces
# the matching CHECK constraints from migration 0051.
PUBLISHER_KINDS = ("organization", "user")
VERIFICATION_STATES = ("none", "requested", "verified", "changes_requested")


class StoreEntitiesPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _normalize_row(d: Dict[str, Any]) -> Dict[str, Any]:
        # JSONB columns come back as Python lists/dicts directly. Defensive
        # fallback only matters for the legacy string-encoded shape.
        v = d.get("doc_paths")
        if isinstance(v, str):
            try:
                d["doc_paths"] = json.loads(v)
            except (ValueError, TypeError):
                d["doc_paths"] = []
        elif v is None:
            d["doc_paths"] = []
        v = d.get("version_history")
        if isinstance(v, str):
            try:
                d["version_history"] = json.loads(v) if v else []
            except (ValueError, TypeError):
                d["version_history"] = []
        elif v is None:
            d["version_history"] = []
        return d

    def create(
        self,
        *,
        id: str,
        owner_user_id: str,
        owner_username: str,
        type: str,
        name: str,
        description: Optional[str],
        category: Optional[str],
        version: str,
        photo_path: Optional[str] = None,
        video_url: Optional[str] = None,
        doc_paths: Optional[List[str]] = None,
        file_size: int = 0,
        visibility_status: str = "pending",
        title: Optional[str] = None,
        tagline: Optional[str] = None,
        synthetic_name: Optional[str] = None,
        publisher_kind: str = "user",
    ) -> Dict[str, Any]:
        # v49 phase-1 parity with the DuckDB repo: title and synthetic_name fall
        # back to derived values when the caller doesn't supply them, so the
        # columns are never NULL for entities created by tests/utilities.
        # Production (POST /api/store/entities) passes them explicitly.
        if not title:
            from src.store_naming import humanize_name

            title = humanize_name(name) or name or "Untitled"
        if not synthetic_name:
            synthetic_name = f"{name}-by-{owner_username}"
        now = datetime.now(timezone.utc)
        v1_entry = {
            "n": 1,
            "hash": version,
            "sha256": None,
            "size": int(file_size) if file_size else None,
            "submission_id": None,
            "created_at": now.isoformat(),
            "created_by": owner_user_id,
        }
        if publisher_kind not in PUBLISHER_KINDS:
            raise ValueError(f"invalid publisher_kind: {publisher_kind!r}")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO store_entities
                        (id, owner_user_id, owner_username, type, name, description,
                         category, version, photo_path, video_url, doc_paths,
                         file_size, install_count, visibility_status,
                         version_no, version_history,
                         title, tagline, synthetic_name, publisher_kind,
                         verification_state,
                         created_at, updated_at)
                    VALUES (:id, :ou, :un, :t, :n, :d, :c, :v, :pp, :vu,
                            CAST(:dp AS JSONB), :fs, 0, :vs, 1,
                            CAST(:vh AS JSONB), :title, :tagline, :sname, :pk,
                            'none', :now, :now)"""
                ),
                {
                    "id": id,
                    "ou": owner_user_id,
                    "un": owner_username,
                    "t": type,
                    "n": name,
                    "d": description,
                    "c": category,
                    "v": version,
                    "pp": photo_path,
                    "vu": video_url,
                    "dp": json.dumps(doc_paths or []),
                    "fs": int(file_size),
                    "vs": visibility_status,
                    "vh": json.dumps([v1_entry]),
                    "title": title,
                    "tagline": tagline,
                    "sname": synthetic_name,
                    "pk": publisher_kind,
                    "now": now,
                },
            )
        return self.get(id)  # type: ignore[return-value]

    def update_history_submission_id(
        self,
        id: str,
        version_no: int,
        submission_id: str,
    ) -> None:
        row = self.get(id)
        if row is None:
            return
        history = list(row.get("version_history") or [])
        changed = False
        for entry in history:
            try:
                if int(entry.get("n")) == int(version_no):
                    entry["submission_id"] = submission_id
                    changed = True
                    break
            except (TypeError, ValueError):
                continue
        if changed:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text("UPDATE store_entities SET version_history = CAST(:vh AS JSONB) WHERE id = :id"),
                    {"vh": json.dumps(history), "id": id},
                )

    def set_visibility(self, id: str, status: str) -> None:
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        now = datetime.now(timezone.utc)
        if status == "archived":
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text("UPDATE store_entities SET visibility_status = :s, updated_at = :now WHERE id = :id"),
                    {"s": status, "now": now, "id": id},
                )
            return

        from src.store_naming import is_archived_name, strip_archive_suffix

        row = self.get(id)
        new_name = row["name"] if row else None
        if row and is_archived_name(row["name"]):
            stripped = strip_archive_suffix(row["name"])
            owner_id = row["owner_user_id"]
            with self._engine.connect() as conn:
                taken = conn.execute(
                    sa.text("SELECT id FROM store_entities WHERE owner_user_id = :o AND name = :n AND id != :id"),
                    {"o": owner_id, "n": stripped, "id": id},
                ).first()
            if not taken:
                new_name = stripped
            else:
                n = 1
                while True:
                    candidate = f"{stripped}-restored-{n}"
                    with self._engine.connect() as conn:
                        clash = conn.execute(
                            sa.text(
                                "SELECT id FROM store_entities WHERE owner_user_id = :o AND name = :n AND id != :id"
                            ),
                            {"o": owner_id, "n": candidate, "id": id},
                        ).first()
                    if not clash:
                        new_name = candidate
                        break
                    n += 1
                    if n > 100:
                        new_name = f"{stripped}-restored-{int(now.timestamp())}"
                        break

        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_entities
                          SET visibility_status = :s,
                              name = :n,
                              archived_at = NULL,
                              archived_by = NULL,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {"s": status, "n": new_name, "now": now, "id": id},
            )

    def set_visibility_if_pending(self, id: str, status: str) -> bool:
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT visibility_status FROM store_entities WHERE id = :id"),
                {"id": id},
            ).first()
            if row is None:
                return False
            current = row[0]
            if current != "pending":
                return False
            now = datetime.now(timezone.utc)
            conn.execute(
                sa.text(
                    """UPDATE store_entities
                          SET visibility_status = :s,
                              archived_at = NULL,
                              archived_by = NULL,
                              updated_at = :now
                        WHERE id = :id
                          AND visibility_status = 'pending'"""
                ),
                {"s": status, "now": now, "id": id},
            )
            confirm = conn.execute(
                sa.text("SELECT visibility_status FROM store_entities WHERE id = :id"),
                {"id": id},
            ).first()
        return confirm is not None and confirm[0] == status

    def archive(self, id: str, *, by_user_id: str) -> Dict[str, str]:
        from src.store_naming import is_archived_name, make_archive_name

        row = self.get(id)
        if row is None:
            return {"original_name": "", "new_name": ""}
        if row.get("visibility_status") == "archived" and is_archived_name(row.get("name") or ""):
            return {
                "original_name": row.get("name") or "",
                "new_name": row.get("name") or "",
            }
        original = row.get("name") or ""
        now = datetime.now(timezone.utc)
        new_name = make_archive_name(original, int(now.timestamp()))
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_entities
                          SET visibility_status = 'archived',
                              name = :n,
                              archived_at = :now,
                              archived_by = :by,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {"n": new_name, "now": now, "by": by_user_id, "id": id},
            )
        return {"original_name": original, "new_name": new_name}

    def append_version_history(
        self,
        id: str,
        *,
        version_hash: str,
        sha256: Optional[str],
        size: Optional[int],
        submission_id: Optional[str],
        created_by: str,
    ) -> int:
        row = self.get(id)
        if row is None:
            raise ValueError(f"entity not found: {id!r}")
        history = list(row.get("version_history") or [])
        max_n = max((int(e.get("n") or 0) for e in history), default=0)
        new_n = max_n + 1
        now = datetime.now(timezone.utc)
        history.append(
            {
                "n": new_n,
                "hash": version_hash,
                "sha256": sha256,
                "size": int(size) if size is not None else None,
                "submission_id": submission_id,
                "created_at": now.isoformat(),
                "created_by": created_by,
            }
        )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE store_entities SET version_history = CAST(:vh AS JSONB), updated_at = :now WHERE id = :id"
                ),
                {"vh": json.dumps(history), "now": now, "id": id},
            )
        return new_n

    def promote_version(self, id: str, version_no: int) -> bool:
        row = self.get(id)
        if row is None:
            return False
        target = None
        for entry in row.get("version_history") or []:
            try:
                if int(entry.get("n")) == int(version_no):
                    target = entry
                    break
            except (TypeError, ValueError):
                continue
        if target is None:
            return False
        size = target.get("size")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_entities
                          SET version_no = :vn,
                              version = :v,
                              file_size = :fs,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {
                    "vn": int(version_no),
                    "v": target.get("hash"),
                    "fs": int(size) if size is not None else row.get("file_size"),
                    "now": datetime.now(timezone.utc),
                    "id": id,
                },
            )
        return True

    def append_version(
        self,
        id: str,
        *,
        version_hash: str,
        sha256: Optional[str],
        size: Optional[int],
        submission_id: Optional[str],
        created_by: str,
    ) -> int:
        n = self.append_version_history(
            id,
            version_hash=version_hash,
            sha256=sha256,
            size=size,
            submission_id=submission_id,
            created_by=created_by,
        )
        self.promote_version(id, n)
        return n

    def get_version(
        self,
        id: str,
        version_no: int,
    ) -> Optional[Dict[str, Any]]:
        row = self.get(id)
        if row is None:
            return None
        for entry in row.get("version_history") or []:
            try:
                if int(entry.get("n")) == int(version_no):
                    return entry
            except (TypeError, ValueError):
                continue
        return None

    def update(
        self,
        id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        version: Optional[str] = None,
        photo_path: Optional[str] = None,
        video_url: Optional[str] = None,
        doc_paths: Optional[List[str]] = None,
        file_size: Optional[int] = None,
        title: Optional[str] = None,
        tagline: Optional[str] = None,
        synthetic_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        sets: List[str] = []
        params: Dict[str, Any] = {"id": id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if description is not None:
            sets.append("description = :description")
            params["description"] = description
        if category is not None:
            sets.append("category = :category")
            params["category"] = category
        if version is not None:
            sets.append("version = :version")
            params["version"] = version
        if photo_path is not None:
            sets.append("photo_path = :photo_path")
            params["photo_path"] = photo_path
        if video_url is not None:
            sets.append("video_url = :video_url")
            params["video_url"] = video_url
        if doc_paths is not None:
            sets.append("doc_paths = CAST(:doc_paths AS JSONB)")
            params["doc_paths"] = json.dumps(doc_paths)
        if file_size is not None:
            sets.append("file_size = :file_size")
            params["file_size"] = int(file_size)
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if tagline is not None:
            # empty string clears tagline (parity with the DuckDB repo)
            sets.append("tagline = :tagline")
            params["tagline"] = tagline or None
        if synthetic_name is not None:
            sets.append("synthetic_name = :synthetic_name")
            params["synthetic_name"] = synthetic_name
        if not sets:
            return self.get(id)
        sets.append("updated_at = :updated_at")
        params["updated_at"] = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE store_entities SET {', '.join(sets)} WHERE id = :id"),
                params,
            )
        return self.get(id)

    def synthetic_name_taken(
        self,
        synthetic_name: str,
        *,
        exclude_entity_id: Optional[str] = None,
        exclude_archived: bool = False,
    ) -> bool:
        """PG sibling of the DuckDB ``synthetic_name_taken``."""
        sql = "SELECT id FROM store_entities WHERE synthetic_name = :sn"
        params: Dict[str, Any] = {"sn": synthetic_name}
        if exclude_entity_id:
            sql += " AND id != :eid"
            params["eid"] = exclude_entity_id
        if exclude_archived:
            sql += " AND visibility_status != 'archived'"
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params).first()
        return row is not None

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM store_entities WHERE id = :id"),
                    {"id": id},
                )
                .mappings()
                .first()
            )
        return self._normalize_row(dict(row)) if row else None

    def get_with_version_approvals(
        self,
        id: str,
    ) -> Optional[Dict[str, Any]]:
        entity = self.get(id)
        if entity is None:
            return None
        history = list(entity.get("version_history") or [])
        if not history:
            return entity
        sub_ids = [entry.get("submission_id") for entry in history if entry.get("submission_id")]
        status_by_id: Dict[str, str] = {}
        if sub_ids:
            sid_keys = []
            params: Dict[str, Any] = {}
            for i, sid in enumerate(sub_ids):
                k = f"sid_{i}"
                sid_keys.append(f":{k}")
                params[k] = sid
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.text(f"SELECT id, status FROM store_submissions WHERE id IN ({','.join(sid_keys)})"),
                    params,
                ).all()
            for sub_id, status in rows:
                status_by_id[sub_id] = status
        annotated: List[Dict[str, Any]] = []
        for entry in history:
            entry = dict(entry)
            sid = entry.get("submission_id")
            entry["submission_status"] = status_by_id.get(sid) if sid else None
            annotated.append(entry)
        entity["version_history"] = annotated
        return entity

    def get_by_owner_and_name(
        self,
        owner_user_id: str,
        name: str,
        *,
        exclude_archived: bool = False,
    ) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM store_entities WHERE owner_user_id = :o AND name = :n"
        params: Dict[str, Any] = {"o": owner_user_id, "n": name}
        if exclude_archived:
            sql += " AND visibility_status != 'archived'"
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    def delete(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM store_entities WHERE id = :id"),
                {"id": id},
            )

    def list(
        self,
        *,
        skip: int = 0,
        limit: int = 24,
        type: Optional[str] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        visibility_status: Optional[List[str]] = None,
        include_owner_id: Optional[str] = None,
        include_ids: Optional[List[str]] = None,
        publisher_kind: Optional[str] = None,
        exclude_owner_user_id: Optional[str] = None,
        verification_state: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """PG sibling of the DuckDB ``list`` — see that docstring, including why
        the Publisher/Verification facets are narrowing filters and never access
        controls."""
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if type:
            clauses.append("type = :type")
            params["type"] = type
        if category:
            if category == "Other":
                clauses.append("(category IS NULL OR TRIM(category) = '' OR category = :category)")
            else:
                clauses.append("category = :category")
            params["category"] = category
        if owner_user_id:
            clauses.append("owner_user_id = :owner_user_id")
            params["owner_user_id"] = owner_user_id
        if publisher_kind:
            if publisher_kind not in PUBLISHER_KINDS:
                raise ValueError(f"invalid publisher_kind: {publisher_kind!r}")
            clauses.append("publisher_kind = :publisher_kind")
            params["publisher_kind"] = publisher_kind
        if exclude_owner_user_id:
            clauses.append("owner_user_id != :exclude_owner_user_id")
            params["exclude_owner_user_id"] = exclude_owner_user_id
        if verification_state:
            ver_keys: List[str] = []
            for i, state in enumerate(verification_state):
                if state not in VERIFICATION_STATES:
                    raise ValueError(f"invalid verification_state: {state!r}")
                k = f"ver_{i}"
                ver_keys.append(f":{k}")
                params[k] = state
            clauses.append(f"verification_state IN ({','.join(ver_keys)})")
        if search:
            clauses.append("(LOWER(name) LIKE :search OR LOWER(description) LIKE :search)")
            params["search"] = f"%{search.lower()}%"
        if visibility_status:
            vs_keys: List[str] = []
            for i, v in enumerate(visibility_status):
                k = f"vs_{i}"
                vs_keys.append(f":{k}")
                params[k] = v
            vs_in = f"visibility_status IN ({','.join(vs_keys)})"
            # Granted entities — see the DuckDB sibling's docstring.
            granted_sql = ""
            for i, gid in enumerate(include_ids or []):
                params[f"gi_{i}"] = str(gid)
            if include_ids:
                keys = ",".join(f":gi_{i}" for i in range(len(include_ids)))
                granted_sql = f" OR (id IN ({keys}) AND visibility_status != 'archived')"
            if include_owner_id:
                clauses.append(
                    f"({vs_in} OR (owner_user_id = :include_owner_id AND visibility_status != 'archived')"
                    f"{granted_sql})"
                )
                params["include_owner_id"] = include_owner_id
            else:
                clauses.append(f"({vs_in}{granted_sql})")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._engine.connect() as conn:
            total = (
                conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM store_entities {where}"),
                    params,
                ).scalar()
                or 0
            )

            list_params = {**params, "limit": int(limit), "offset": int(skip)}
            rows = (
                conn.execute(
                    sa.text(
                        f"""SELECT * FROM store_entities {where}
                        ORDER BY created_at DESC, id
                        LIMIT :limit OFFSET :offset"""
                    ),
                    list_params,
                )
                .mappings()
                .all()
            )
        items = [self._normalize_row(dict(r)) for r in rows]
        return items, int(total)

    def category_counts(
        self,
        *,
        type: Optional[str] = None,
        visibility_status: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """PG sibling of the DuckDB ``category_counts`` — see that docstring.

        Mirrors the WHERE + ``COALESCE(NULLIF(TRIM(category),''),'Other')``
        GROUP BY of ``GET /api/marketplace/categories`` so per-category
        counts match the grid on both backends.
        """
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if type:
            clauses.append("type = :type")
            params["type"] = type
        if visibility_status:
            vs_keys: List[str] = []
            for i, v in enumerate(visibility_status):
                k = f"vs_{i}"
                vs_keys.append(f":{k}")
                params[k] = v
            vs_in = f"visibility_status IN ({','.join(vs_keys)})"
            if owner_id:
                clauses.append(f"({vs_in} OR (owner_user_id = :owner_id AND visibility_status != 'archived'))")
                params["owner_id"] = owner_id
            else:
                clauses.append(vs_in)
        elif owner_id:
            clauses.append("owner_user_id = :owner_id")
            params["owner_id"] = owner_id
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"SELECT COALESCE(NULLIF(TRIM(category),''), 'Other') AS cat, "
                    f"COUNT(*) FROM store_entities {where} GROUP BY cat"
                ),
                params,
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def set_publisher_kind(
        self,
        id: str,
        publisher_kind: str,
        *,
        by_user_id: str,
    ) -> bool:
        """PG sibling of the DuckDB ``set_publisher_kind`` — see that docstring
        for invariant 1 (``organization`` ⇒ ``verification_state='none'``)."""
        if publisher_kind not in PUBLISHER_KINDS:
            raise ValueError(f"invalid publisher_kind: {publisher_kind!r}")
        if self.get(id) is None:
            return False
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            if publisher_kind == "organization":
                conn.execute(
                    sa.text(
                        """UPDATE store_entities
                              SET publisher_kind = :pk,
                                  verification_state = 'none',
                                  verified_at = NULL,
                                  verified_by = :by,
                                  verification_note = NULL,
                                  updated_at = :now
                            WHERE id = :id"""
                    ),
                    {"pk": publisher_kind, "by": by_user_id, "now": now, "id": id},
                )
            else:
                conn.execute(
                    sa.text(
                        """UPDATE store_entities
                              SET publisher_kind = :pk, updated_at = :now
                            WHERE id = :id"""
                    ),
                    {"pk": publisher_kind, "now": now, "id": id},
                )
        return True

    def set_verification(
        self,
        id: str,
        verification_state: str,
        *,
        by_user_id: str,
        note: Optional[str] = None,
    ) -> bool:
        """PG sibling of the DuckDB ``set_verification`` — see that docstring.

        Refuses on an organization-published row, and never gates a read.
        """
        if verification_state not in VERIFICATION_STATES:
            raise ValueError(f"invalid verification_state: {verification_state!r}")
        row = self.get(id)
        if row is None or row.get("publisher_kind") == "organization":
            return False
        now = datetime.now(timezone.utc)
        verified_at = now if verification_state == "verified" else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_entities
                          SET verification_state = :vs,
                              verified_at = :vat,
                              verified_by = :by,
                              verification_note = :note,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {
                    "vs": verification_state,
                    "vat": verified_at,
                    "by": by_user_id,
                    "note": note,
                    "now": now,
                    "id": id,
                },
            )
        return True

    def publisher_counts(
        self,
        *,
        type: Optional[str] = None,
        visibility_status: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
        viewer_user_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """PG sibling of the DuckDB ``publisher_counts`` — see that docstring."""
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if type:
            clauses.append("type = :type")
            params["type"] = type
        if visibility_status:
            vs_keys: List[str] = []
            for i, v in enumerate(visibility_status):
                k = f"vs_{i}"
                vs_keys.append(f":{k}")
                params[k] = v
            vs_in = f"visibility_status IN ({','.join(vs_keys)})"
            if owner_id:
                clauses.append(f"({vs_in} OR (owner_user_id = :owner_id AND visibility_status != 'archived'))")
                params["owner_id"] = owner_id
            else:
                clauses.append(vs_in)
        elif owner_id:
            clauses.append("owner_user_id = :owner_id")
            params["owner_id"] = owner_id
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"SELECT publisher_kind, owner_user_id, COUNT(*) "
                    f"FROM store_entities {where} "
                    f"GROUP BY publisher_kind, owner_user_id"
                ),
                params,
            ).all()
        out = {"organization": 0, "me": 0, "other_users": 0}
        for kind, owner, n in rows:
            if kind == "organization":
                out["organization"] += int(n)
            elif viewer_user_id and owner == viewer_user_id:
                out["me"] += int(n)
            else:
                out["other_users"] += int(n)
        return out

    def bump_install_count(self, id: str, delta: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE store_entities SET install_count = GREATEST(install_count + :d, 0) WHERE id = :id"),
                {"d": int(delta), "id": id},
            )

    def list_approved_synthetic_types(self) -> Dict[str, str]:
        """PG sibling of the DuckDB ``list_approved_synthetic_types``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT synthetic_name, type FROM store_entities WHERE visibility_status='approved'")
            ).all()
        return {r[0]: r[1] for r in rows}
