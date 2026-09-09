"""Repository for `source_connections` (v74) — named data-source connections.

Spec: docs/superpowers/specs/2026-06-12-named-source-connections-design.md.
`config` is stored as a JSON string and returned as a dict. `is_default`
is unique per source_type — enforced here (both backends), not by the DB.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import duckdb

# config_patch() is the fix for a genuine write-write conflict: the nightly
# ACL sync and the weekly subtree sweep (connectors/sharepoint/acl_sync.py)
# both patch bookkeeping keys on the SAME connection's config. Two concurrent
# patches of the same row can lose an optimistic-concurrency race under
# DuckDB the same way concurrent logins race in
# user_group_members.replace_synced_groups — reuse that retry idiom here.
# Postgres serializes via a row lock (SELECT ... FOR UPDATE), so its sibling
# needs no retry.
_CONFIG_PATCH_CONFLICT_RETRIES = 3
_CONFIG_PATCH_CONFLICT_BACKOFF_S = 0.05


def _next_run_generation(current: Any) -> int:
    """``current + 1``, treating a missing or unparseable persisted value as
    ``0``. Mirrored verbatim in the Postgres sibling — see
    :meth:`SourceConnectionsRepository.claim_run_generation`."""
    try:
        return int(current or 0) + 1
    except (TypeError, ValueError):
        return 1


class SourceConnectionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row: Any, cols: list) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(zip(cols, row))
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _fetch_one(self, sql: str, params: list) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(sql, params).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        token_env: Optional[str] = None,
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> None:
        # Wrap the default-demotion UPDATE + INSERT in one transaction so a
        # mid-way failure can't leave the old default demoted with no new row
        # inserted — matches the PG sibling's engine.begin() atomicity.
        self.conn.execute("BEGIN")
        try:
            if is_default:
                self.conn.execute(
                    "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                    [source_type],
                )
            self.conn.execute(
                """INSERT INTO source_connections
                   (id, name, source_type, config, token_env, is_default, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [id, name, source_type, json.dumps(config), token_env, is_default, created_by],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE id = ?", [connection_id])

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE name = ?", [name])

    def get_default(self, source_type: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = ? AND is_default ORDER BY created_at LIMIT 1",
            [source_type],
        )

    def list(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if source_type:
            rows = self.conn.execute(
                "SELECT * FROM source_connections WHERE source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM source_connections ORDER BY name").fetchall()
        cols = [d[0] for d in self.conn.description]
        return [self._row_to_dict(r, cols) for r in rows]  # type: ignore[misc]

    def update(
        self,
        connection_id: str,
        *,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        token_env: Optional[str] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        # Atomic multi-column update — same transaction guarantee as the PG
        # sibling, so a failure between the UPDATEs can't half-apply.
        # `name` backs the "Add data source" wizard's post-test rename
        # (#755): the project name is only known after a successful
        # test-connection call, which requires the row to already exist, so
        # rename-after-create is the only way to honour the UX contract
        # without introducing a create-without-persisting endpoint.
        self.conn.execute("BEGIN")
        try:
            if name is not None:
                self.conn.execute(
                    "UPDATE source_connections SET name = ? WHERE id = ?",
                    [name, connection_id],
                )
            if config is not None:
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(config), connection_id],
                )
            if token_env is not None:
                self.conn.execute(
                    "UPDATE source_connections SET token_env = ? WHERE id = ?",
                    [token_env, connection_id],
                )
            if is_default is not None:
                if is_default:
                    # Promote: demote every other connection of the same
                    # source_type first (is_default is unique per source_type,
                    # enforced here — mirrors create()).
                    row = self.conn.execute(
                        "SELECT source_type FROM source_connections WHERE id = ?",
                        [connection_id],
                    ).fetchone()
                    if row:
                        self.conn.execute(
                            "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                            [row[0]],
                        )
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = TRUE WHERE id = ?",
                        [connection_id],
                    )
                else:
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = FALSE WHERE id = ?",
                        [connection_id],
                    )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def config_patch(self, connection_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Atomically merge ``patch``'s TOP-LEVEL keys into this row's
        current ``config``, leaving every other key untouched — including a
        key some OTHER writer committed after the caller's own last read.
        Returns the updated row, or ``None`` if ``connection_id`` doesn't
        exist.

        Closes a real bug (spec: NB-1 review finding on the SharePoint ACL
        sync): ``update(connection_id, config=snapshot)`` merges in Python
        against a caller-held snapshot, so two writers racing on one
        connection — e.g. the nightly ACL sync and the weekly subtree sweep
        (``connectors/sharepoint/acl_sync.py``) — can silently drop
        whichever wrote second's bookkeeping keys. This method re-reads
        ``config`` from the DB inside its own transaction instead, so the
        merge always starts from the latest committed value.

        Retries on a DuckDB write-write conflict the same way
        ``user_group_members.replace_synced_groups`` does (see that
        method's docstring) — two concurrent patches of the SAME row can
        lose an optimistic-concurrency race under DuckDB; Postgres
        serializes via a row lock, so its sibling needs no retry.
        """
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_CONFIG_PATCH_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                row = self.conn.execute(
                    "SELECT config FROM source_connections WHERE id = ?",
                    [connection_id],
                ).fetchone()
                if row is None:
                    self.conn.execute("ROLLBACK")
                    return None
                current = row[0]
                if isinstance(current, str):
                    try:
                        current = json.loads(current)
                    except (json.JSONDecodeError, TypeError):
                        current = {}
                elif not isinstance(current, dict):
                    current = {}
                merged = {**current, **patch}
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(merged), connection_id],
                )
                self.conn.execute("COMMIT")
                return self.get(connection_id)
            except duckdb.TransactionException as e:
                # Lost an optimistic-concurrency race with a concurrent
                # config_patch on the same row. Roll back (best-effort — the
                # txn may already be aborted) and retry with a short backoff.
                self._safe_rollback()
                last_err = e
                time.sleep(_CONFIG_PATCH_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        # Exhausted retries — surface the last conflict to the caller.
        if last_err is not None:
            raise last_err
        return None

    def merge_extraction(self, connection_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Atomically merge ``patch``'s keys into THIS row's ``config.
        extraction`` sub-object — the NESTED counterpart to
        :meth:`config_patch`'s own top-level merge, for a caller that only
        ever touches keys INSIDE ``extraction`` (never a sibling of it on
        ``config`` itself). Re-reads ``config`` fresh inside this same
        transaction, exactly like :meth:`config_patch` does, so a
        DIFFERENT writer's OWN key inside ``extraction`` — most
        pointedly, ``connectors.sharepoint.crawler.request_stop``'s
        ``stop_requested_at`` — committed after the caller's own last read
        can never be clobbered by this merge.

        2026-09-07 finding: ``_record_extraction_dispatch`` used to do its
        own ``repo.get()`` THEN ``repo.config_patch(..., {"extraction":
        extraction})`` — two separate calls, with a real (if narrow)
        window between them for a concurrent ``request_stop`` to commit a
        fresh flag that the second call's already-computed ``extraction``
        snapshot would then silently overwrite. This collapses both into
        ONE transaction so there is no such window at all.
        """
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_CONFIG_PATCH_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                row = self.conn.execute(
                    "SELECT config FROM source_connections WHERE id = ?",
                    [connection_id],
                ).fetchone()
                if row is None:
                    self.conn.execute("ROLLBACK")
                    return None
                current = row[0]
                if isinstance(current, str):
                    try:
                        current = json.loads(current)
                    except (json.JSONDecodeError, TypeError):
                        current = {}
                elif not isinstance(current, dict):
                    current = {}
                extraction = dict(current.get("extraction") or {})
                extraction.update(patch)
                merged = {**current, "extraction": extraction}
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(merged), connection_id],
                )
                self.conn.execute("COMMIT")
                return self.get(connection_id)
            except duckdb.TransactionException as e:
                self._safe_rollback()
                last_err = e
                time.sleep(_CONFIG_PATCH_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        if last_err is not None:
            raise last_err
        return None

    def claim_run_generation(self, connection_id: str) -> Optional[int]:
        """Bump ``config.extraction.run_generation`` and return the NEW
        value — the monotonic per-connection counter that says who owns this
        connection's extraction right now (issue #2333). ``None`` if
        ``connection_id`` doesn't exist.

        ONE transaction, one read-modify-write: a caller that read the
        counter and then wrote ``current + 1`` itself would let two
        concurrent triggers claim the SAME generation, and the whole point
        of the counter is that a claim can never be shared. This is the
        compare-and-set — the storage layer, under its own conflict retry
        (Postgres: the row lock), is what makes "bump" atomic, never the
        caller.

        Deliberately not backed by ``extraction_runs`` and deliberately not
        a new column: the counter has to work on BOTH app-state backends,
        because a DuckDB-backed instance always takes the INLINE crawl path
        (A3 ratchet) — exactly the path on which a zombie handler outlives
        its own force-cancelled job. It lives one key over from the
        cooperative-stop flag, in the same JSON sub-object, for the same
        reason that flag does (see :meth:`merge_extraction`).

        A missing or unparseable persisted value counts as ``0``, so the
        first claim on any connection — including one that predates this
        counter — is generation ``1`` and nothing has to backfill.
        """
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_CONFIG_PATCH_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                row = self.conn.execute(
                    "SELECT config FROM source_connections WHERE id = ?",
                    [connection_id],
                ).fetchone()
                if row is None:
                    self.conn.execute("ROLLBACK")
                    return None
                current = row[0]
                if isinstance(current, str):
                    try:
                        current = json.loads(current)
                    except (json.JSONDecodeError, TypeError):
                        current = {}
                elif not isinstance(current, dict):
                    current = {}
                extraction = dict(current.get("extraction") or {})
                claimed = _next_run_generation(extraction.get("run_generation"))
                extraction["run_generation"] = claimed
                merged = {**current, "extraction": extraction}
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(merged), connection_id],
                )
                self.conn.execute("COMMIT")
                return claimed
            except duckdb.TransactionException as e:
                self._safe_rollback()
                last_err = e
                time.sleep(_CONFIG_PATCH_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        if last_err is not None:
            raise last_err
        return None

    def clear_stop_requested_if_unchanged(self, connection_id: str, expected_stop_at: str) -> bool:
        """Clear ``config.extraction.stop_requested_at`` — but ONLY if its
        CURRENT value (re-read here, under the same transaction) still
        equals ``expected_stop_at``. The compare-and-delete counterpart to
        ``connectors.sharepoint.crawler.request_stop``'s write: a caller
        that decided this flag looks stale from an earlier read must not
        blindly overwrite whatever is there NOW — a `request_stop` that
        committed a FRESH flag in between would be silently lost to that
        stale snapshot, exactly the race ``config_patch`` (a precomputed
        whole-``extraction`` replacement) cannot protect against on its
        own. Same guard shape as ``JobsRepository.heartbeat``'s
        ``lease_token`` check. Returns ``True`` iff it actually cleared
        something.
        """
        last_err: Optional[duckdb.Error] = None
        for attempt in range(_CONFIG_PATCH_CONFLICT_RETRIES):
            try:
                self.conn.execute("BEGIN")
                row = self.conn.execute(
                    "SELECT config FROM source_connections WHERE id = ?",
                    [connection_id],
                ).fetchone()
                if row is None:
                    self.conn.execute("ROLLBACK")
                    return False
                current = row[0]
                if isinstance(current, str):
                    try:
                        current = json.loads(current)
                    except (json.JSONDecodeError, TypeError):
                        current = {}
                elif not isinstance(current, dict):
                    current = {}
                extraction = dict(current.get("extraction") or {})
                if extraction.get("stop_requested_at") != expected_stop_at:
                    self.conn.execute("ROLLBACK")
                    return False
                extraction.pop("stop_requested_at", None)
                merged = {**current, "extraction": extraction}
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(merged), connection_id],
                )
                self.conn.execute("COMMIT")
                return True
            except duckdb.TransactionException as e:
                self._safe_rollback()
                last_err = e
                time.sleep(_CONFIG_PATCH_CONFLICT_BACKOFF_S * (attempt + 1))
            except Exception:
                self._safe_rollback()
                raise
        if last_err is not None:
            raise last_err
        return False

    def _safe_rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    def delete(self, connection_id: str) -> None:
        self.conn.execute("DELETE FROM source_connections WHERE id = ?", [connection_id])
