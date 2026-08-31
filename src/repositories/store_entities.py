"""Repository for community-uploaded Store entities.

A row represents one skill/agent/plugin that an authenticated user has
uploaded through the ``/store/new`` page or ``POST /api/store/entities``.
The row is the index over the on-disk plugin tree at
``${DATA_DIR}/store/<entity_id>/plugin/`` — see ``app/api/store.py`` for the
upload + bake pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb

# v104: the two trust axes on a store entity. Duplicated verbatim in
# ``store_entities_pg.py`` (the same convention ``resource_grants`` uses for its
# ``requirement`` enum) — the cross-engine contract test pins both.
#
# 'organization' is written ONLY by the explicit admin "publish as the
# organization" action, never derived from the owner's current Admin-group
# membership: group membership is mutable and re-synced from the identity
# provider, so a derived value would silently reclassify already-published
# skills when an author moves out of the Admin group.
PUBLISHER_KINDS = ("organization", "user")

# 'requested' and 'changes_requested' are author-visible workflow states; other
# users only ever see 'verified'. There is deliberately no "unverified" LABEL —
# absence of a chip is the neutral default, because on an instance with no
# reviewer a negative label would print on ~every card.
VERIFICATION_STATES = ("none", "requested", "verified", "changes_requested")


class StoreEntitiesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
        d = dict(zip(columns, row))
        for k in ("doc_paths",):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    d[k] = []
            elif v is None:
                d[k] = []
        # v37: version_history is a JSON array of past version metadata.
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
        # v49 phase-1: title and synthetic_name fall back to derived values
        # when caller doesn't supply them — keeps the column NOT NULL invariant
        # without forcing every test/utility caller to recompute the same
        # deterministic formula. Production code (POST /api/store/entities)
        # passes both explicitly so the upload form's user-edited title is
        # honored.
        if not title:
            from src.store_naming import humanize_name

            title = humanize_name(name) or name or "Untitled"
        if not synthetic_name:
            synthetic_name = f"{name}-by-{owner_username}"
        now = datetime.now(timezone.utc)
        # v37: seed version_history with the v1 entry on create so the
        # edit feature's append_version always has a baseline to build
        # on. submission_id is filled in by the API layer post-INSERT
        # via update_history_submission_id when the submission row is
        # created.
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
        self.conn.execute(
            """INSERT INTO store_entities
                (id, owner_user_id, owner_username, type, name, description,
                 category, version, photo_path, video_url, doc_paths,
                 file_size, install_count, visibility_status,
                 version_no, version_history,
                 title, tagline, synthetic_name, publisher_kind,
                 verification_state,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?,
                    'none', ?, ?)""",
            [
                id,
                owner_user_id,
                owner_username,
                type,
                name,
                description,
                category,
                version,
                photo_path,
                video_url,
                json.dumps(doc_paths or []),
                int(file_size),
                visibility_status,
                json.dumps([v1_entry]),
                title,
                tagline,
                synthetic_name,
                publisher_kind,
                now,
                now,
            ],
        )
        return self.get(id)  # type: ignore[return-value]

    def update_history_submission_id(
        self,
        id: str,
        version_no: int,
        submission_id: str,
    ) -> None:
        """Backfill the ``submission_id`` field on a version_history
        entry once the submission row has been written. Used by the
        upload + edit endpoints to link version → submission verdict
        without requiring a two-phase write that would race with the
        v1 seed in ``create``.
        """
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
            self.conn.execute(
                "UPDATE store_entities SET version_history = ? WHERE id = ?",
                [json.dumps(history), id],
            )

    def set_visibility(self, id: str, status: str) -> None:
        """Flip visibility_status on a submission's resolution.

        Called by the guardrail runner after the LLM review completes
        ('approved' on safe, 'hidden' on review_error/blocked) and by
        the admin override path ('approved' after force-publish).
        Soft-delete uses 'archived' via :meth:`archive` instead of this
        helper so the actor + timestamp are recorded.

        When transitioning OUT of 'archived' (admin override re-publishes
        an archived row), the archive metadata is cleared in the same
        UPDATE so a future read of the row doesn't show stale
        archived_at / archived_by alongside the new status.
        """
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        now = datetime.now(timezone.utc)
        if status == "archived":
            # Use :meth:`archive` instead — this branch shouldn't be hit
            # but is left permissive so existing call sites work; the
            # actor + timestamp won't be recorded though.
            self.conn.execute(
                "UPDATE store_entities SET visibility_status = ?, updated_at = ? WHERE id = ?",
                [status, now, id],
            )
            return
        # Transitioning to a non-archived state — null out the archive
        # metadata so old archive forensics don't bleed into the new row
        # state, AND strip the archive-rename suffix so the original
        # display name is restored. Conflict path: if the original name
        # slot was taken by a re-upload, append `-restored-N` so the
        # un-archive doesn't 409.
        from src.store_naming import is_archived_name, strip_archive_suffix

        row = self.get(id)
        new_name = row["name"] if row else None
        if row and is_archived_name(row["name"]):
            stripped = strip_archive_suffix(row["name"])
            owner_id = row["owner_user_id"]
            # Probe for collision against active rows (skip self).
            taken = self.conn.execute(
                "SELECT id FROM store_entities WHERE owner_user_id = ? AND name = ? AND id != ?",
                [owner_id, stripped, id],
            ).fetchone()
            if not taken:
                new_name = stripped
            else:
                # Find the next free `-restored-N` suffix.
                n = 1
                while True:
                    candidate = f"{stripped}-restored-{n}"
                    clash = self.conn.execute(
                        "SELECT id FROM store_entities WHERE owner_user_id = ? AND name = ? AND id != ?",
                        [owner_id, candidate, id],
                    ).fetchone()
                    if not clash:
                        new_name = candidate
                        break
                    n += 1
                    if n > 100:  # paranoid bound
                        new_name = f"{stripped}-restored-{int(now.timestamp())}"
                        break
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = ?,
                      name = ?,
                      archived_at = NULL,
                      archived_by = NULL,
                      updated_at = ?
                WHERE id = ?""",
            [status, new_name, now, id],
        )

    def set_visibility_if_pending(self, id: str, status: str) -> bool:
        """Background-task safe variant of :meth:`set_visibility`.

        Only flips the row while its ``visibility_status`` is still
        ``'pending'`` — i.e. genuinely awaiting a verdict. Returns ``True``
        if the update applied, ``False`` if the row was in another state
        (already archived, approved-and-installed, hidden, or hard-deleted
        between the BG task's start and verdict-write).

        ``'hidden'`` is deliberately NOT flippable: it is a deliberate
        decision by someone (an admin hiding an entity, or an owner saving a
        skill as Private from the builder), and a review verdict landing
        afterwards must not silently publish it. This matches the review
        runner's own comment that "archive / hidden-by-admin paths leave
        alone" — before this the method contradicted it.

        Used by the LLM review runner so a verdict landing late doesn't
        clobber an admin decision (e.g. archive while review was in
        flight).
        """
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        # Read-then-update: DuckDB doesn't expose rowcount on the python
        # API in a portable way across versions, so we check first. Both
        # statements run on the same connection, serialized writer, so a
        # concurrent admin archive between SELECT and UPDATE is a
        # vanishingly thin window. The defensive guard in the WHERE
        # clause closes the loop.
        row = self.conn.execute(
            "SELECT visibility_status FROM store_entities WHERE id = ?",
            [id],
        ).fetchone()
        if row is None:
            return False
        current = row[0]
        if current != "pending":
            return False
        now = datetime.now(timezone.utc)
        # Re-check via WHERE so an admin archive that landed between the
        # SELECT and the UPDATE doesn't get clobbered.
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = ?,
                      archived_at = NULL,
                      archived_by = NULL,
                      updated_at = ?
                WHERE id = ?
                  AND visibility_status = 'pending'""",
            [status, now, id],
        )
        # Re-read to confirm the flip applied (admin may have raced in).
        confirm = self.conn.execute(
            "SELECT visibility_status FROM store_entities WHERE id = ?",
            [id],
        ).fetchone()
        return confirm is not None and confirm[0] == status

    def archive(self, id: str, *, by_user_id: str) -> Dict[str, str]:
        """Soft-delete: flip visibility to 'archived' + record actor +
        timestamp + rename the row's ``name`` so the (owner, name) slot
        and the global ``<name>-by-<username>`` suffix slot free up for
        re-upload.

        Returns a dict with ``original_name`` and ``new_name`` so the
        caller (the DELETE handler in ``app/api/store.py``) can rename
        the on-disk skill/agent/plugin subdir + audit-log the original
        name.

        Existing user_store_installs continue to serve the bundle
        through marketplace.zip / .git (filter is approved + archived
        in :class:`UserStoreInstallsRepository.list_for_user`); the
        served slug carries the new suffixed name so consumers see the
        plugin renamed on next sync.

        Idempotent: archiving an already-archived row no-ops (the
        existing suffix is preserved; we don't re-rename).
        """
        from src.store_naming import is_archived_name, make_archive_name

        row = self.get(id)
        if row is None:
            return {"original_name": "", "new_name": ""}
        # Re-archive: keep the existing suffixed name to avoid churning
        # the on-disk path on every redundant archive call.
        if row.get("visibility_status") == "archived" and is_archived_name(row.get("name") or ""):
            return {
                "original_name": row.get("name") or "",
                "new_name": row.get("name") or "",
            }

        original = row.get("name") or ""
        owner_username = row.get("owner_username") or ""
        now = datetime.now(timezone.utc)
        new_name = make_archive_name(original, int(now.timestamp()))
        # v49: synthetic_name tracks the canonical <name>-by-<owner> string;
        # rename it alongside `name` so the deterministic formula stays in
        # sync with the actually-stored slug on disk.
        new_synthetic = f"{new_name}-by-{owner_username}"
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = 'archived',
                      name = ?,
                      synthetic_name = ?,
                      archived_at = ?,
                      archived_by = ?,
                      updated_at = ?
                WHERE id = ?""",
            [new_name, new_synthetic, now, by_user_id, now, id],
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
        """Append a new version entry to ``version_history`` and return
        its ``n`` value. **Does NOT promote** — entity row's
        ``version_no`` / ``version`` / ``file_size`` stay at the
        previous current.

        Used by the PUT edit path + restore endpoint to record that a
        new version exists in history (with its verdict) without
        affecting what installers see. Promotion happens separately
        once the LLM approves: see :meth:`promote_version`.
        """
        row = self.get(id)
        if row is None:
            raise ValueError(f"entity not found: {id!r}")
        history = list(row.get("version_history") or [])
        # Pick the next n above the largest existing entry. Defaults to
        # 1 for empty history (shouldn't happen post-v37 since create()
        # seeds v1, but defensive).
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
        self.conn.execute(
            "UPDATE store_entities SET version_history = ?, updated_at = ? WHERE id = ?",
            [json.dumps(history), now, id],
        )
        return new_n

    def promote_version(self, id: str, version_no: int) -> bool:
        """Promote a version_history entry to current.

        Looks up the ``n=version_no`` entry in the entity's
        ``version_history`` and copies its ``hash``/``size`` onto the
        entity row's ``version_no`` / ``version`` / ``file_size``.
        Returns ``True`` on success, ``False`` when the entry is
        missing.

        Caller is responsible for swapping the on-disk live
        ``plugin/`` dir from the matching version dir
        (``versions/v<version_no>/plugin/``).
        """
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
        self.conn.execute(
            """UPDATE store_entities
                  SET version_no = ?,
                      version = ?,
                      file_size = ?,
                      updated_at = ?
                WHERE id = ?""",
            [
                int(version_no),
                target.get("hash"),
                int(size) if size is not None else row.get("file_size"),
                datetime.now(timezone.utc),
                id,
            ],
        )
        return True

    # Back-compat alias — old callers still call append_version.
    # Kept as the PUT-equivalent shorthand: append + promote in one
    # step. Used by code paths where we want immediate promotion
    # (guardrails disabled, or test-only flows).
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
        """Append + promote in one shot. New code paths should call
        :meth:`append_version_history` and :meth:`promote_version`
        separately so they can defer promotion until an LLM verdict
        approves the new version."""
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
        """Return the version_history entry for the given version_no, or
        ``None`` if the entity / version is unknown."""
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
        """Partial update — only the supplied columns change. Returns the
        updated row, or None if no row matched.

        ``name`` change is allowed (v37 edit feature). The caller is
        responsible for collision checks BEFORE invoking this method
        (per-owner UNIQUE + global suffix uniqueness) and for
        renaming the on-disk skill/agent/plugin slug to match.

        ``synthetic_name`` must be re-supplied by the caller when ``name``
        changes — the repo does not recompute it automatically (avoids a
        round-trip to fetch ``owner_username`` and keeps the deterministic
        formula in one place: the API layer).

        ``tagline`` accepts an empty string to clear the field; pass
        ``None`` to leave it unchanged.
        """
        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if category is not None:
            sets.append("category = ?")
            params.append(category)
        if version is not None:
            sets.append("version = ?")
            params.append(version)
        if photo_path is not None:
            sets.append("photo_path = ?")
            params.append(photo_path)
        if video_url is not None:
            sets.append("video_url = ?")
            params.append(video_url)
        if doc_paths is not None:
            sets.append("doc_paths = ?")
            params.append(json.dumps(doc_paths))
        if file_size is not None:
            sets.append("file_size = ?")
            params.append(int(file_size))
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if tagline is not None:
            # empty string clears tagline; sentinel preserved here
            sets.append("tagline = ?")
            params.append(tagline or None)
        if synthetic_name is not None:
            sets.append("synthetic_name = ?")
            params.append(synthetic_name)
        if not sets:
            return self.get(id)
        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc))
        params.append(id)
        self.conn.execute(
            f"UPDATE store_entities SET {', '.join(sets)} WHERE id = ?",
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
        """Whether any row already uses ``synthetic_name`` (the global flat-
        namespace uniqueness check at upload time). Optionally excludes one
        entity id and archived rows. Routed through the factory so the read
        hits the active backend — a raw query here read the frozen DuckDB
        system file on Postgres instances (#518)."""
        sql = "SELECT id FROM store_entities WHERE synthetic_name = ?"
        params: List[Any] = [synthetic_name]
        if exclude_entity_id:
            sql += " AND id != ?"
            params.append(exclude_entity_id)
        if exclude_archived:
            sql += " AND visibility_status != 'archived'"
        return bool(self.conn.execute(sql, params).fetchone())

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM store_entities WHERE id = ?", [id]).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    def get_with_version_approvals(
        self,
        id: str,
    ) -> Optional[Dict[str, Any]]:
        """Same as :meth:`get` but each ``version_history`` entry gets
        an additional ``submission_status`` field populated from
        ``store_submissions``.

        Used by the detail page + restore endpoint to gate which
        versions are restorable. Legacy v1 rows created pre-v37 carry
        ``submission_id=None`` (the v1 seed predates the
        backfill) — those map to ``submission_status=None`` and the
        consumer treats them as approved (back-compat).
        """
        entity = self.get(id)
        if entity is None:
            return None
        history = list(entity.get("version_history") or [])
        if not history:
            return entity
        sub_ids = [entry.get("submission_id") for entry in history if entry.get("submission_id")]
        status_by_id: Dict[str, str] = {}
        if sub_ids:
            placeholders = ",".join("?" for _ in sub_ids)
            rows = self.conn.execute(
                f"SELECT id, status FROM store_submissions WHERE id IN ({placeholders})",
                sub_ids,
            ).fetchall()
            for sub_id, status in rows:
                status_by_id[sub_id] = status
        # Defensive copy of each history entry before mutating — today
        # ``self.get()`` re-parses JSON each call so the mutation can't
        # leak across calls, but copying costs nothing and protects any
        # future caching layer from carrying the annotated
        # ``submission_status`` key into a subsequent plain ``get()``.
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
        """Fetch a single entity by (owner, name).

        ``exclude_archived=True`` skips rows whose
        ``visibility_status='archived'`` — used by the upload conflict
        check so a freshly archived prior doesn't block a same-name
        re-upload (the archive flow renames the row to free the slot,
        but skipping here is belt-and-braces).
        """
        sql = "SELECT * FROM store_entities WHERE owner_user_id = ? AND name = ?"
        params: List[Any] = [owner_user_id, name]
        if exclude_archived:
            sql += " AND visibility_status != 'archived'"
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    def delete(self, id: str) -> None:
        self.conn.execute("DELETE FROM store_entities WHERE id = ?", [id])

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
        """Filtered + paginated listing.

        Returns ``(items, total)`` where ``total`` is the unfiltered count
        across pages — used to render pagination controls.

        ``visibility_status`` whitelists which guardrail states are visible.
        Non-admin browse passes ``["approved"]``; admin/owner views pass
        ``None`` (no filter) so pending/hidden entries surface in their UIs.

        ``include_ids`` is the same escape hatch for entities the caller's
        groups were GRANTED: a hidden entity a group holds a ``store_entity``
        grant for belongs in that group's browse, because private means "not
        everyone", not "nobody but me". Resolved by the caller — this layer
        does not read grants.

        ``include_owner_id`` is the "show me my own pending stuff too"
        knob: when set alongside a ``visibility_status`` whitelist, the
        SQL becomes ``(visibility IN (...) OR owner_user_id = :uid)`` so
        the caller's own non-approved entries surface in an otherwise
        approved-only listing. Used by the marketplace + store browse
        pages so submitters spot their own under-review uploads in the
        grid.

        ``publisher_kind`` / ``exclude_owner_user_id`` / ``verification_state``
        back the v104 Publisher + Verification facets. None of them is an access
        control: they narrow an already-authorized listing, and
        ``verification_state`` in particular must never be used to hide an
        unverified entity from a caller who is otherwise entitled to see it.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if type:
            clauses.append("type = ?")
            params.append(type)
        if category:
            # "Other" is the synthetic bucket for entities with NULL / empty
            # category (matches what /api/marketplace/categories reports for
            # the Flea tab). An explicit category=='Other' string also lands
            # here so a user who picked "Other" at upload time stays grouped.
            if category == "Other":
                clauses.append("(category IS NULL OR TRIM(category) = '' OR category = ?)")
                params.append(category)
            else:
                clauses.append("category = ?")
                params.append(category)
        if owner_user_id:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        # v104 Publisher facet. "Organization" is publisher_kind alone; "Me" is
        # publisher_kind='user' + owner_user_id (the caller passes both); "Other
        # users" is publisher_kind='user' + exclude_owner_user_id. The
        # me/other-users split is derived per request, never stored.
        if publisher_kind:
            if publisher_kind not in PUBLISHER_KINDS:
                raise ValueError(f"invalid publisher_kind: {publisher_kind!r}")
            clauses.append("publisher_kind = ?")
            params.append(publisher_kind)
        if exclude_owner_user_id:
            clauses.append("owner_user_id != ?")
            params.append(exclude_owner_user_id)
        if verification_state:
            for state in verification_state:
                if state not in VERIFICATION_STATES:
                    raise ValueError(f"invalid verification_state: {state!r}")
            placeholders = ",".join("?" for _ in verification_state)
            clauses.append(f"verification_state IN ({placeholders})")
            params.extend(verification_state)
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
            like = f"%{search.lower()}%"
            params.extend([like, like])
        if visibility_status:
            placeholders = ",".join("?" for _ in visibility_status)
            granted = [str(g) for g in (include_ids or [])]
            # Archived is excluded here for the same reason it is for the
            # owner's own rows below: browse never shows it.
            granted_sql = (
                " OR (id IN (" + ",".join("?" for _ in granted) + ") AND visibility_status != 'archived')"
                if granted
                else ""
            )
            if include_owner_id:
                # Approved (or whatever the whitelist allows) for everyone,
                # plus the caller's OWN entries that aren't archived.
                # Archived stays admin-only across browse — even the
                # owner's own archived rows must NOT appear in browse
                # listings (per user direction "only admins should see
                # archived submissions"). My AI Stack uses a different
                # path (user_store_installs.list_for_user) and DOES
                # surface archived for already-installed plugins.
                clauses.append(
                    f"(visibility_status IN ({placeholders}) "
                    f"OR (owner_user_id = ? AND visibility_status != 'archived')"
                    f"{granted_sql})"
                )
                params.extend(visibility_status)
                params.append(include_owner_id)
                params.extend(granted)
            else:
                clauses.append(f"(visibility_status IN ({placeholders}){granted_sql})")
                params.extend(visibility_status)
                params.extend(granted)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        count_row = self.conn.execute(
            f"SELECT COUNT(*) FROM store_entities {where}",
            params,
        ).fetchone()
        total = int(count_row[0]) if count_row else 0

        rows = self.conn.execute(
            f"""SELECT * FROM store_entities {where}
                ORDER BY created_at DESC, id
                LIMIT ? OFFSET ?""",
            [*params, int(limit), int(skip)],
        ).fetchall()
        if not rows:
            return [], int(total)
        columns = [d[0] for d in self.conn.description]
        items = [self._row_to_dict(columns, r) for r in rows]
        return items, int(total)

    def category_counts(
        self,
        *,
        type: Optional[str] = None,
        visibility_status: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Per-category entity counts for the Flea tab category rail.

        Replicates the GROUP BY behind ``GET /api/marketplace/categories``
        (``app/api/marketplace.py`` ``list_categories``): NULL / empty
        ``category`` collapses into the synthetic ``'Other'`` bucket, and
        the WHERE mirrors the listing endpoint so the counts match what the
        grid actually shows.

        Filters (all optional, combined with AND):
        - ``type`` — restrict to one entity type.
        - ``visibility_status`` — whitelist of visible guardrail states.
          Admin callers pass ``ADMIN_BROWSE_VISIBILITY`` (everything except
          the ``archived`` soft-delete state); non-admins pass
          ``["approved"]``. ``None`` counts every state including deleted
          rows and is only appropriate for moderation surfaces.
        - ``owner_id`` — when set ALONGSIDE ``visibility_status``, the
          visibility clause becomes ``(visibility_status IN (...) OR
          (owner_user_id = ? AND visibility_status != 'archived'))`` so the
          caller's own non-archived under-review uploads are counted too
          (parity with the handler's non-admin branch + :meth:`list`).
        """
        clauses: List[str] = []
        params: List[Any] = []
        if type:
            clauses.append("type = ?")
            params.append(type)
        if visibility_status:
            placeholders = ",".join("?" for _ in visibility_status)
            if owner_id:
                clauses.append(
                    f"(visibility_status IN ({placeholders}) "
                    f"OR (owner_user_id = ? AND visibility_status != 'archived'))"
                )
                params.extend(visibility_status)
                params.append(owner_id)
            else:
                clauses.append(f"visibility_status IN ({placeholders})")
                params.extend(visibility_status)
        elif owner_id:
            clauses.append("owner_user_id = ?")
            params.append(owner_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM(category),''), 'Other') AS cat, "
            f"COUNT(*) FROM store_entities {where} GROUP BY cat",
            params,
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def set_publisher_kind(
        self,
        id: str,
        publisher_kind: str,
        *,
        by_user_id: str,
    ) -> bool:
        """Flip who stands behind an entity ('organization' | 'user').

        Admin-only at the API layer. Flipping TO 'organization' also clears any
        verification state: an org-published item carries the stronger claim, so
        a checkmark on top of it would invent a fourth trust tier (invariant 1 —
        ``publisher_kind='organization' ⇒ verification_state='none'``).

        Returns ``False`` when the row does not exist.
        """
        if publisher_kind not in PUBLISHER_KINDS:
            raise ValueError(f"invalid publisher_kind: {publisher_kind!r}")
        if self.get(id) is None:
            return False
        now = datetime.now(timezone.utc)
        if publisher_kind == "organization":
            self.conn.execute(
                """UPDATE store_entities
                      SET publisher_kind = ?,
                          verification_state = 'none',
                          verified_at = NULL,
                          verified_by = ?,
                          verification_note = NULL,
                          updated_at = ?
                    WHERE id = ?""",
                [publisher_kind, by_user_id, now, id],
            )
        else:
            self.conn.execute(
                """UPDATE store_entities
                      SET publisher_kind = ?, updated_at = ?
                    WHERE id = ?""",
                [publisher_kind, now, id],
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
        """Set the org's advisory verification verdict on a USER-published item.

        ``verified_at`` / ``verified_by`` are stamped only for the terminal
        ``'verified'`` state and cleared otherwise, so "when was this verified"
        can never read stale after a ``changes_requested`` or a removal.

        Refuses (returns ``False``) on an organization-published row, upholding
        invariant 1 from the other direction. Also returns ``False`` when the
        row does not exist — the caller distinguishes via :meth:`get`.

        This value NEVER gates a read. It feeds the card chip and the
        Verification filter only; wiring it into ``_enforce_visibility`` or the
        listing clause would rebuild the approval queue this design removes.
        """
        if verification_state not in VERIFICATION_STATES:
            raise ValueError(f"invalid verification_state: {verification_state!r}")
        row = self.get(id)
        if row is None or row.get("publisher_kind") == "organization":
            return False
        now = datetime.now(timezone.utc)
        verified_at = now if verification_state == "verified" else None
        self.conn.execute(
            """UPDATE store_entities
                  SET verification_state = ?,
                      verified_at = ?,
                      verified_by = ?,
                      verification_note = ?,
                      updated_at = ?
                WHERE id = ?""",
            [verification_state, verified_at, by_user_id, note, now, id],
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
        """Counts for the Publisher facet: ``{organization, me, other_users}``.

        The honest replacement for the retired per-tab counts. ``me`` /
        ``other_users`` are a per-request split of ``publisher_kind='user'``
        against ``viewer_user_id`` — that split is derived, never stored, so a
        row needs no rewrite when a different person browses. With no
        ``viewer_user_id`` (an anonymous/system caller) every user-published row
        lands in ``other_users``.

        Mirrors :meth:`category_counts`' WHERE so facet counts agree with the
        grid they annotate.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if type:
            clauses.append("type = ?")
            params.append(type)
        if visibility_status:
            placeholders = ",".join("?" for _ in visibility_status)
            if owner_id:
                clauses.append(
                    f"(visibility_status IN ({placeholders}) "
                    f"OR (owner_user_id = ? AND visibility_status != 'archived'))"
                )
                params.extend(visibility_status)
                params.append(owner_id)
            else:
                clauses.append(f"visibility_status IN ({placeholders})")
                params.extend(visibility_status)
        elif owner_id:
            clauses.append("owner_user_id = ?")
            params.append(owner_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT publisher_kind, owner_user_id, COUNT(*) "
            f"FROM store_entities {where} GROUP BY publisher_kind, owner_user_id",
            params,
        ).fetchall()
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
        """Adjust install_count by delta (signed). Floors at 0 — concurrent
        uninstall + delete races shouldn't push the number negative.
        """
        self.conn.execute(
            "UPDATE store_entities SET install_count = GREATEST(install_count + ?, 0) WHERE id = ?",
            [int(delta), id],
        )

    def list_approved_synthetic_types(self) -> Dict[str, str]:
        """Map ``synthetic_name -> type`` for every approved (flea) store
        entity. Backs ``MarketplaceItemLookup`` (usage-telemetry attribution)
        — Claude Code writes flea-bundle identifiers as
        ``flea:<synthetic_name>``, and the resolver needs the entity's type
        (skill/agent/plugin) without a per-event query. Only
        ``visibility_status='approved'`` entities are eligible — pending /
        hidden / archived entities must not attribute telemetry. Routed
        through the factory so the read hits the active backend."""
        rows = self.conn.execute(
            "SELECT synthetic_name, type FROM store_entities WHERE visibility_status='approved'"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
