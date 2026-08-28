"""Repository for marketplace registry.

Mirrors TableRegistryRepository. One row per marketplace git repo that the
nightly sync should clone/update into ${DATA_DIR}/marketplaces/<slug>/.

Tokens never live here — only the name of the env var (`token_env`) that
holds the PAT. The secret itself is persisted to data/state/.env_overlay
by the admin API, same pattern as Keboola/BigQuery secrets.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class MarketplaceRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def register(
        self,
        id: str,
        name: str,
        url: str,
        branch: Optional[str] = None,
        token_env: Optional[str] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
        curator_name: Optional[str] = None,
        curator_email: Optional[str] = None,
        is_builtin: bool = False,
        ref: Optional[str] = None,
    ) -> None:
        # ON CONFLICT updates curator fields too — but ONLY when the caller
        # supplied a non-None value. Passing curator_name=None on an UPDATE
        # path (e.g. an admin "edit URL only" flow that didn't touch the
        # curator inputs) must NOT clobber an existing curator with NULL.
        # COALESCE(excluded.curator_name, marketplace_registry.curator_name)
        # gives that semantics: when excluded is non-null it wins, otherwise
        # the prior value survives.
        #
        # `ref` (tag/commit pin) is always overwritten on conflict, same as
        # `branch` — both are "current desired state" fields, not sticky
        # metadata like curator, so re-registering with ref=None clears a
        # previous pin (mirrors the admin API's explicit-None-means-unset
        # PATCH semantics for branch).
        #
        # is_builtin is NOT included in the ON CONFLICT SET — the built-in
        # flag is immutable after the initial seed so re-seeding on upgrade
        # cannot accidentally flip admin-registered rows.
        #
        # last_error self-heal: a built-in row has no git remote, so it can
        # never legitimately fail a sync — any last_error sitting on one is
        # a fossil from before sync_one() refused built-in rows up front
        # (see MarketplaceNotSyncable). Clear it on every re-register of a
        # built-in row (i.e. every boot's seed_builtin_marketplace() call)
        # so a stale error can't get permanently stuck with no way to clear
        # it. Non-builtin rows are untouched — an admin edit must not wipe
        # a real sync failure off the board.
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO marketplace_registry
                (id, name, url, branch, token_env, description, registered_by,
                 registered_at, curator_name, curator_email, is_builtin, ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                url = excluded.url,
                branch = excluded.branch,
                token_env = excluded.token_env,
                description = excluded.description,
                curator_name = COALESCE(excluded.curator_name, marketplace_registry.curator_name),
                curator_email = COALESCE(excluded.curator_email, marketplace_registry.curator_email),
                ref = excluded.ref,
                last_error = CASE WHEN excluded.is_builtin THEN NULL ELSE marketplace_registry.last_error END""",
            [
                id,
                name,
                url,
                branch,
                token_env,
                description,
                registered_by,
                now,
                curator_name,
                curator_email,
                is_builtin,
                ref,
            ],
        )

    def unregister(self, marketplace_id: str) -> None:
        self.conn.execute("DELETE FROM marketplace_registry WHERE id = ?", [marketplace_id])

    def get(self, marketplace_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM marketplace_registry WHERE id = ?", [marketplace_id]).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM marketplace_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_builtin(self) -> List[Dict[str, Any]]:
        """Return only rows where is_builtin=TRUE, ordered by name."""
        results = self.conn.execute(
            "SELECT * FROM marketplace_registry WHERE is_builtin = TRUE ORDER BY name"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_non_builtin(self) -> List[Dict[str, Any]]:
        """Return only admin-registered (non-built-in) rows, ordered by name.

        Used by the nightly git-sync path so it never tries to git-clone the
        built-in marketplace (which has no remote URL).
        """
        results = self.conn.execute(
            "SELECT * FROM marketplace_registry WHERE is_builtin = FALSE ORDER BY name"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def update_sync_status(
        self,
        marketplace_id: str,
        *,
        commit_sha: Optional[str] = None,
        synced_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update last_synced_at / last_commit_sha / last_error after a sync attempt.

        Passing None for any field leaves it untouched — except `error`,
        which is always written (clear on success by passing error=None AND
        a non-None synced_at/commit_sha will null out the error column).
        """
        sets = []
        params: List[Any] = []
        if synced_at is not None:
            sets.append("last_synced_at = ?")
            params.append(synced_at)
        if commit_sha is not None:
            sets.append("last_commit_sha = ?")
            params.append(commit_sha)
        # last_error: clear on success (commit_sha present), otherwise write provided value
        if commit_sha is not None and error is None:
            sets.append("last_error = NULL")
        elif error is not None:
            sets.append("last_error = ?")
            params.append(error)
        if not sets:
            return
        params.append(marketplace_id)
        self.conn.execute(
            f"UPDATE marketplace_registry SET {', '.join(sets)} WHERE id = ?",
            params,
        )
