"""Sync endpoints — manifest, trigger, sync-settings, table-subscriptions."""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, List

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import distribution_signed_urls_mode
from app.job_correlation import stamp_request_id
from app.utils import get_data_dir as _get_data_dir
from src.audit_helpers import client_kind_from_user, log_safe
from src.distribution import cached_mirror_index
from src.object_store import ObjectStore, object_store
from src.rbac import get_accessible_tables
from src.scheduler import filter_due_tables, is_table_due
from src.sync_state_key import resolve_sync_state_key_for_row

from src.repositories import (
    audit_repo,
    connection_secrets_repo,
    data_packages_repo,
    file_corpora_repo,
    jobs_repo,
    memory_domains_repo,
    profile_repo,
    source_connections_repo,
    sync_settings_repo,
    sync_state_repo,
    table_registry_repo,
    usage_repo,
    users_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])

# Process-wide guard against overlapping `_run_sync` invocations. Two
# concurrent extractor subprocesses both write `extract.duckdb` and fight
# for its file lock — the first sync stalls, the second crashes, and the
# `/api/health` check times out long enough that Docker flips the
# container to `unhealthy`, which (behind a `reverse_proxy` upstream)
# bricks external traffic until contention drains.
#
# wave-2B: `POST /api/sync/trigger` no longer calls `_run_sync` inline via
# `BackgroundTasks` — it enqueues a `data-refresh` job (see `trigger_sync`
# below) that the worker loop's job handler
# (`app.worker.kinds._run_data_refresh`) executes, possibly in a different
# OS process on a role-split deployment. `_sync_lock` is therefore no
# longer the "is a sync in progress" signal for the trigger handler (the
# job queue's idempotency dedup is — reported via `enqueue()`'s
# `"deduped"` return key, see `JobsRepository.enqueue`'s docstring) — it
# is now purely defense-in-depth INSIDE `_run_sync` against two
# invocations racing within the SAME process (e.g. a same-process worker
# lane plus any lingering direct caller), same role it always had there.
_sync_lock = threading.Lock()

# Race-protection for ``GET /api/sync/status`` (the host-side
# ``agnes-auto-upgrade.sh`` defer probe, which polls this endpoint to avoid
# `docker compose up -d` SIGKILLing a mid-flight extractor). In the default
# single-container topology the worker loop runs in THIS SAME process, so
# `_sync_lock` still reflects real in-process sync activity once the worker
# claims the enqueued job — but that claim can now lag the trigger by up to
# a worker poll interval (previously: the few-hundred-ms gap between the
# trigger handler returning 200 and `BackgroundTasks` invoking `_run_sync`).
# Stamping `_recent_trigger_at` at trigger time still narrows that window
# for the common case (operator triggers, immediately polls status), even
# though it can no longer promise the tight bound the original comment
# described. ``_TRIGGER_HOLD_SEC`` is the width of that best-effort window.
_TRIGGER_HOLD_SEC = 30
_recent_trigger_at: float = 0.0  # monotonic clock; 0 = never triggered


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_extractor_stats(stdout: Optional[str]) -> Optional[dict]:
    """Parse the Keboola extractor subprocess's stats dict back out of its
    stdout (#754).

    The subprocess (the inline ``-c`` script built in ``_run_sync``) prints
    exactly one line — ``print(json.dumps(result))`` — as the LAST thing it
    writes to stdout, right before exiting with a code computed by
    ``compute_exit_code``. It cannot write per-table failures to
    ``system.duckdb`` itself: the parent process holds that connection's
    lock for the duration of the sync (see the module docstring on
    ``_sync_lock``), so a second writer would fight it. This stdout line is
    therefore the only channel for ``{tables_extracted, tables_failed,
    errors: [{table, error}]}`` to reach the parent, which is what
    previously discarded per-table extractor errors, leaving `agnes admin`
    / the admin UI with no explanation for "N total, 0 synced" beyond a
    generic exit-code message.

    Defensive: a truncated/garbled/empty stdout (e.g. the subprocess was
    SIGKILLed mid-flush) returns ``None`` rather than raising — the caller
    already has an exit-code-derived fallback message.
    """
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _materialize_table(
    *,
    table_id: str,
    sql: str,
    bq,
    output_dir: str,
    max_bytes: Optional[int],
    fetch_timeout_s: Optional[float] = None,
) -> dict:
    """Thin wrapper around `connectors.bigquery.extractor.materialize_query`
    so the trigger pass can be unit-tested by patching this seam without
    touching the real BqAccess factory or the duckdb import."""
    from connectors.bigquery.extractor import materialize_query

    return materialize_query(
        table_id=table_id,
        sql=sql,
        bq=bq,
        output_dir=output_dir,
        max_bytes=max_bytes,
        fetch_timeout_s=fetch_timeout_s,
    )


def _materialize_databricks_table(
    *,
    table_id: str,
    row: dict,
    client,
    catalog: str,
    output_dir: str,
    max_bytes: Optional[int],
    statement_timeout_s: Optional[float] = None,
) -> dict:
    """Thin wrapper around `connectors.databricks.extractor.materialize_query`
    so the trigger pass can be unit-tested by patching this seam without a
    live warehouse — same role `_materialize_table` plays for BQ."""
    from connectors.databricks.extractor import materialize_query

    return materialize_query(
        table_id=table_id,
        client=client,
        output_dir=output_dir,
        source_query=row.get("source_query"),
        catalog=catalog,
        bucket=row.get("bucket"),
        source_table=row.get("source_table"),
        max_bytes=max_bytes,
        statement_timeout_s=statement_timeout_s,
    )


def _is_permanent_upstream_error(exc: Exception) -> bool:
    """True when retrying the table can never succeed — the upstream object
    is gone (Keboola Storage ``storage.tables.notFound`` → HTTP 404, e.g. a
    table deleted or moved to another bucket after registration).

    Used to stamp ``permanent: True`` on the per-table error entry so
    ``_run_sync`` can exclude these from the job-failure decision: a
    ``data-refresh`` job that retries a deleted upstream table stays red
    forever and masks real (transient) failures from monitoring. The fix for
    a permanent failure is registry surgery (re-point or unregister), which a
    retry loop cannot perform.
    """
    from connectors.keboola.storage_api import StorageApiError

    return isinstance(exc, StorageApiError) and exc.status == 404


class _KeboolaCredentialError(Exception):
    """No resolvable Keboola ``stack_url``/token for a sync pass.

    Raised by ``_resolve_keboola_credentials`` — callers must record a
    per-row/per-group error and skip the row rather than falling back to a
    different credential. A Keboola connection is per-project; silently
    substituting a different token (or the instance's global one) extracts
    the WRONG project's data instead of failing loudly (#B2)."""


def _resolve_keboola_credentials(conn_id: Optional[str]) -> tuple:
    """Resolve ``(stack_url, token)`` for a Keboola sync pass.

    ``conn_id=None`` resolves the instance-level/global credential —
    ``data_source.keboola.stack_url`` + the configured token env var
    (``KEBOOLA_STORAGE_TOKEN`` by default), vault fallback last. This is
    today's unscoped, backwards-compatible path — unchanged.

    A non-null ``conn_id`` resolves the NAMED ``source_connections`` row:
    vault takes priority (``connection_secrets_repo().get(conn_id)``),
    falling back to the env var named in the row's own ``token_env``.

    Always raises ``_KeboolaCredentialError`` rather than returning a
    partial/empty pair, so every caller fails loudly instead of guessing.

    Shared by ``_run_materialized_pass`` (materialized rows) and
    ``_run_sync``'s extractor-subprocess dispatch (local/remote rows) —
    the two Keboola sync passes that both need a per-``connection_id``
    credential (#B2 — pre-fix only the materialized pass resolved one; the
    extractor subprocess used a single global env pair for every row).
    """
    if conn_id:
        sc = source_connections_repo().get(conn_id)
        if not sc:
            raise _KeboolaCredentialError(f"connection_id {conn_id!r} not found in source_connections")
        sc_url = sc["config"].get("stack_url", "")
        sc_token = connection_secrets_repo().get(conn_id) or os.environ.get(sc.get("token_env") or "", "")
        if not (sc_url and sc_token):
            raise _KeboolaCredentialError(f"connection {conn_id!r} missing URL or token")
        return sc_url, sc_token

    from app.instance_config import get_value

    sc_url = get_value("data_source", "keboola", "stack_url", default="") or os.environ.get("KEBOOLA_STACK_URL", "")
    token_env = (
        get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN") or "KEBOOLA_STORAGE_TOKEN"
    )
    sc_token = os.environ.get(token_env, "")
    if not sc_token:
        from app.datasource_secrets import datasource_secret as _ds_secret

        sc_token = _ds_secret("KEBOOLA_STORAGE_TOKEN") or ""
    if not (sc_url and sc_token):
        raise _KeboolaCredentialError(
            f"Keboola URL/token not configured (data_source.keboola.stack_url + env {token_env})"
        )
    return sc_url, sc_token


def _run_materialized_pass(
    conn: duckdb.DuckDBPyConnection,
    bq,
    tables: Optional[List[str]] = None,
    source_type: Optional[str] = None,
) -> dict:
    """Walk `table_registry` for `query_mode='materialized'` rows and run any
    that are due, dispatching by ``source_type`` to the correct connector's
    materialize_query. Honors per-table `sync_schedule` via `is_table_due()`,
    computes the file hash inline, and updates `sync_state` so the manifest
    can serve the row to `agnes pull` without re-hashing on every request.

    ``tables`` (when not None) restricts the pass to a specific subset —
    targeted re-syncs from the operator (POST /api/sync/trigger with a
    body) need this, otherwise an admin asking to re-sync `kbc_job` would
    re-process every other materialized row that's also due. Matched
    against both the registry id and name (admins often pass either).
    A row that survives this filter (i.e. is explicitly targeted) also
    BYPASSES the `sync_schedule` `due_check` gate below (#1620) — an
    operator naming a specific table is a bounded, explicit request and
    must not be silently dropped by the routine hourly cadence just
    because a prior sync attempt already stamped `sync_state`. An
    untargeted sweep (`tables=None`) still honors `due_check` exactly as
    before.

    ``source_type`` (when not None) restricts the pass to rows whose
    registry ``source_type`` matches — the partial-rebuild path
    (POST /api/sync/trigger?source=bigquery) uses it so a BQ-only
    rebuild leaves Keboola materialized rows untouched, and vice versa.

    BigQuery rows go through BqAccess + bigquery_query() (jobs API),
    optionally cost-guarded by ``max_bytes_per_materialize``.
    Keboola rows go through KeboolaAccess + ATTACH-and-COPY, no
    guardrail (extension has no dry-run primitive).
    Databricks rows go through the Statement Execution API client
    (``connectors/databricks``), result-size-capped by
    ``data_source.databricks.max_bytes_per_materialize`` (the API's
    byte_limit — no dry-run primitive exists there either).

    Returns:
        ``{"materialized": [ids], "skipped": [ids], "errors": [{table, error}]}``

    Errors are aggregated per row — one budget-blown table doesn't stop a
    healthy sibling. ``MaterializeBudgetError`` is caught and rendered with
    its structured fields so operator alerting can pick out the cap-vs-actual
    bytes from the log line.

    #754: skip reasons bounded enough to be worth persisting across a
    process restart (``source_filter``, ``not_in_target``, ``in_flight``)
    are ALSO written to ``sync_state`` via ``state.set_skipped(...)`` so
    ``GET /api/admin/registry`` / ``agnes admin list-tables`` can explain
    a "0 synced" run. The routine per-tick ``due_check`` skip is
    deliberately NOT persisted — it fires on nearly every scheduler tick
    for every scheduled table and would otherwise turn every tick into an
    UPDATE storm for information the row's own ``last_sync`` already
    conveys.
    """
    from app.instance_config import get_value
    from connectors.bigquery.extractor import MaterializeBudgetError, MaterializeInFlightError

    bq_output_dir = str(Path(_get_data_dir()) / "extracts" / "bigquery")
    kb_output_dir = Path(_get_data_dir()) / "extracts" / "keboola" / "data"
    dbx_output_dir = str(Path(_get_data_dir()) / "extracts" / "databricks")
    sf_output_dir = str(Path(_get_data_dir()) / "extracts" / "snowflake")

    # Sentinel: max_bytes <= 0 (or None) disables the guardrail. `get_value()`
    # treats YAML `null` as "missing" → returns the default; operators must use
    # the explicit `0` sentinel to disable. See config/instance.yaml.example.
    # YAML accepts floats too (e.g. `10737418240.0`), and operators may
    # write `1e10` for readability; coerce to int and tolerate non-numeric
    # entries by falling through to the disable path with a warning.
    raw_max = get_value(
        "data_source",
        "bigquery",
        "max_bytes_per_materialize",
        default=10 * 2**30,
    )
    try:
        n = int(raw_max) if raw_max is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.bigquery.max_bytes_per_materialize is not numeric "
            "(%r); cost guardrail disabled. Set an integer or 0 to disable.",
            raw_max,
        )
        n = 0
    bq_max_bytes = n if n > 0 else None

    # Fetch-phase watchdog for the COPY's client-side result download —
    # the phase the BQ extension's own timeout does not cover. Default
    # 15 min (a healthy fetch of a multi-hundred-MB result is ~1 min; a
    # wedged stream otherwise holds the per-table lock for hours and
    # starves the daily schedule). Explicit `0` disables.
    raw_fetch_timeout = get_value(
        "data_source",
        "bigquery",
        "materialize_fetch_timeout_seconds",
        default=900,
    )
    try:
        t = float(raw_fetch_timeout) if raw_fetch_timeout is not None else 900.0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.bigquery.materialize_fetch_timeout_seconds is not "
            "numeric (%r); using the 900s default. Set a number of seconds "
            "or 0 to disable.",
            raw_fetch_timeout,
        )
        t = 900.0
    bq_fetch_timeout_s = t if t > 0 else None

    # Databricks guardrails. No dry-run primitive exists on the Statement
    # Execution API, so the cap bounds the RESULT size via the API's
    # byte_limit (manifest flagged `truncated` past it → MaterializeBudgetError
    # in the extractor), not the scanned bytes the way BQ's dry-run does.
    raw_dbx_max = get_value(
        "data_source",
        "databricks",
        "max_bytes_per_materialize",
        default=10 * 2**30,
    )
    try:
        n = int(raw_dbx_max) if raw_dbx_max is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.databricks.max_bytes_per_materialize is not numeric "
            "(%r); cost guardrail disabled. Set an integer or 0 to disable.",
            raw_dbx_max,
        )
        n = 0
    dbx_max_bytes = n if n > 0 else None

    # Client-side deadline on the statement poll loop — a wedged warehouse
    # statement is cancelled and surfaced instead of holding the pass.
    raw_dbx_timeout = get_value(
        "data_source",
        "databricks",
        "statement_timeout_seconds",
        default=900,
    )
    try:
        t = float(raw_dbx_timeout) if raw_dbx_timeout is not None else 900.0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.databricks.statement_timeout_seconds is not numeric "
            "(%r); using the 900s default. Set a number of seconds or 0 to disable.",
            raw_dbx_timeout,
        )
        t = 900.0
    dbx_statement_timeout_s = t if t > 0 else None

    # Snowflake guardrails (result-size cap, not a dry-run scan cap).
    raw_sf_max = get_value(
        "data_source",
        "snowflake",
        "max_bytes_per_materialize",
        default=10 * 2**30,
    )
    try:
        n = int(raw_sf_max) if raw_sf_max is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.snowflake.max_bytes_per_materialize is not numeric "
            "(%r); cost guardrail disabled. Set an integer or 0 to disable.",
            raw_sf_max,
        )
        n = 0
    sf_max_bytes = n if n > 0 else None

    # Lazily-built Databricks client — one per pass, first databricks row
    # constructs it; a misconfigured instance yields per-row errors instead
    # of failing the whole pass (mirrors the Keboola client cache below).
    databricks_client = None
    databricks_client_error: Optional[str] = None
    databricks_catalog = ""

    # Lazily-resolved Snowflake settings — first snowflake row resolves once.
    sf_settings: Optional[dict] = None
    sf_settings_error: Optional[str] = None

    registry = table_registry_repo()
    state = sync_state_repo()

    summary = {"materialized": [], "skipped": [], "errors": []}
    # Per-connection-id cache of KeboolaStorageClient instances.
    # Keyed by connection_id (str) or None for the global/instance token.
    # A single client is shared across all rows that share the same
    # connection_id — requests.Session inside it reuses the HTTP keep-alive
    # pool across rows, same as the old single-client pattern.
    keboola_clients: dict = {}

    # Targeted-trigger filter. Compare against both id and name so an admin
    # who passes either form (the registry id slug, or the human-friendly
    # name) gets the same result. `None` means "no filter — process all
    # due materialized rows".
    target_set: Optional[set] = set(tables) if tables is not None else None

    for row in registry.list_all():
        if row.get("query_mode") != "materialized":
            continue

        # The parquet filename (and the manifest's flat `tables{}` key) is
        # keyed by `table_registry.name` (matches Keboola's `_meta.
        # table_name`) — that convention is unrelated to this key and
        # stays untouched; `_build_manifest_for_user` resolves it off the
        # registry row, not off `sync_state.table_id`.
        ref_name = row["name"]
        # B1: `sync_state.table_id` / `sync_history.table_id` are keyed by
        # the registry `id` — every admin-status reader (`/api/admin/
        # registry`, the data-sources pipeline strip, the Tables lens'
        # delivery map) joins sync state against the registry on `id`, and
        # pre-fix this row wrote under `name`, so a table registered with a
        # display name that isn't already a valid id (spaces, uppercase —
        # e.g. `name="Web Sessions"`, id `web_sessions`) showed healthy sync
        # status on one surface and "never synced" on another for the exact
        # same sync. `row` is already the registry row, so this is a plain
        # field read, not a second lookup — see `resolve_sync_state_key`
        # for the name-only variant `_update_sync_state` uses.
        sync_key = resolve_sync_state_key_for_row(ref_name, row)

        # Partial-rebuild scoping (POST /api/sync/trigger?source=…). Compute
        # the row's source_type once, with the same `or "bigquery"` legacy
        # default the dispatch below uses, so the filter and the dispatch
        # agree on how a NULL-source_type row is classified.
        row_source_type = row.get("source_type") or "bigquery"  # legacy default
        if source_type is not None and row_source_type != source_type:
            summary["skipped"].append({"table": ref_name, "reason": "source_filter"})
            # Persisted (#754) — a partial `?source=` rebuild is an explicit,
            # bounded-frequency request (not a routine per-tick skip), so an
            # operator later looking at `GET /api/admin/registry` for "why
            # didn't this sync" sees the real reason instead of a stale row.
            state.set_skipped(sync_key, "source_filter")
            continue

        # `explicitly_targeted` is True once we know this row survived the
        # `target_set` filter above — i.e. the operator named this exact
        # table (by id or name), not a routine untargeted sweep.
        explicitly_targeted = target_set is not None
        if explicitly_targeted and not (ref_name in target_set or row.get("id") in target_set):
            summary["skipped"].append({"table": ref_name, "reason": "not_in_target"})
            state.set_skipped(sync_key, "not_in_target")
            continue

        last = state.get_last_sync(sync_key)
        last_iso = last.isoformat() if last else None
        # Per-table schedule wins; fall through to AGNES_DEFAULT_SYNC_SCHEDULE
        # (operator override), then to ``every 1h`` (OSS-historical default).
        # The env knob lets a deployment dial down the platform-wide refresh
        # cadence without having to PUT every registry row — useful when
        # data freshness budget is "once per day" and the hourly default
        # over-fetches.
        schedule = row.get("sync_schedule") or os.environ.get("AGNES_DEFAULT_SYNC_SCHEDULE", "").strip() or "every 1h"
        # #1620: the `due_check` cadence gate exists for the routine,
        # untargeted sweep (scheduler tick / unscoped `POST /api/sync/
        # trigger`) — it must NOT swallow an explicit, bounded operator
        # request naming this exact table (`agnes admin sync <table>`).
        # Without this bypass, a table whose `sync_state.last_sync` was
        # already stamped (even by a prior attempt that produced nothing
        # useful — e.g. before its `query_mode`/`source_query` were fixed)
        # would silently skip every re-sync attempt within the schedule
        # window, no matter how many times an operator retriggered it.
        if not explicitly_targeted and not is_table_due(schedule, last_iso):
            summary["skipped"].append({"table": ref_name, "reason": "due_check"})
            continue

        # Dispatch by source_type. BQ rows keep using `_materialize_table`
        # (the existing test seam); Keboola rows use the new Keboola
        # materialize_query via a lazily-initialized KeboolaAccess.
        try:
            if row_source_type == "bigquery":
                stats = _materialize_table(
                    table_id=ref_name,
                    sql=row["source_query"],
                    bq=bq,
                    output_dir=bq_output_dir,
                    max_bytes=bq_max_bytes,
                    fetch_timeout_s=bq_fetch_timeout_s,
                )
            elif row_source_type == "keboola":
                conn_id = row.get("connection_id")
                if conn_id not in keboola_clients:
                    from connectors.keboola.storage_api import KeboolaStorageClient

                    try:
                        sc_url, sc_token = _resolve_keboola_credentials(conn_id)
                    except _KeboolaCredentialError as cred_err:
                        summary["errors"].append({"table": ref_name, "error": str(cred_err)})
                        state.set_error(sync_key, str(cred_err))
                        continue
                    keboola_clients[conn_id] = KeboolaStorageClient(
                        url=sc_url,
                        token=sc_token,
                    )
                keboola_access = keboola_clients[conn_id]
                kb_output_dir.mkdir(parents=True, exist_ok=True)
                from connectors.keboola.extractor import (
                    materialize_query as kb_materialize_query,
                )

                # Storage API needs the bucket+table split — registry rows
                # carry both fields per the standard register-table schema.
                bucket = row.get("bucket", "")
                source_table = row.get("source_table") or ref_name
                if not bucket:
                    summary["errors"].append(
                        {
                            "table": ref_name,
                            "error": (
                                "materialized keboola row is missing 'bucket'; re-register with --bucket <in.c-...>"
                            ),
                        }
                    )
                    continue
                kb_stats = kb_materialize_query(
                    table_id=ref_name,
                    bucket=bucket,
                    source_table=source_table,
                    source_query=row.get("source_query"),
                    storage_client=keboola_access,
                    output_dir=kb_output_dir,
                )
                # Normalize Keboola materialize_query output to the shape the
                # BQ branch uses for downstream sync_state updates. KB returns
                # {table_id, path, rows, bytes, md5}; map to
                # {rows, size_bytes, hash}.
                stats = {
                    "rows": kb_stats["rows"],
                    "size_bytes": kb_stats["bytes"],
                    "hash": kb_stats["md5"],
                    "query_mode": "materialized",
                }
            elif row_source_type == "databricks":
                if databricks_client is None and databricks_client_error is None:
                    from connectors.databricks.semantic_layer import (
                        resolve_databricks_settings,
                    )

                    settings = resolve_databricks_settings()
                    if settings is None:
                        databricks_client_error = (
                            "Databricks not configured for materialized path "
                            "(data_source.databricks.host + warehouse_id + "
                            "DATABRICKS_TOKEN env/vault secret)"
                        )
                    else:
                        from connectors.databricks.client import (
                            DatabricksStatementClient,
                        )

                        try:
                            databricks_client = DatabricksStatementClient(
                                host=settings["host"],
                                token=settings["token"],
                                warehouse_id=settings["warehouse_id"],
                            )
                            databricks_catalog = settings.get("catalog") or ""
                        except ValueError as e:
                            databricks_client_error = f"Databricks client init failed: {e}"
                if databricks_client is None:
                    summary["errors"].append({"table": ref_name, "error": databricks_client_error})
                    state.set_error(sync_key, databricks_client_error)
                    continue
                stats = _materialize_databricks_table(
                    table_id=ref_name,
                    row=row,
                    client=databricks_client,
                    catalog=databricks_catalog,
                    output_dir=dbx_output_dir,
                    max_bytes=dbx_max_bytes,
                    statement_timeout_s=dbx_statement_timeout_s,
                )
            elif row_source_type == "snowflake":
                if sf_settings is None and sf_settings_error is None:
                    from connectors.snowflake.settings import resolve_snowflake_settings

                    sf_settings = resolve_snowflake_settings()
                    if sf_settings is None:
                        sf_settings_error = (
                            "Snowflake not configured for materialized path "
                            "(data_source.snowflake.* + SNOWFLAKE_PASSWORD env/vault secret)"
                        )
                if sf_settings is None:
                    summary["errors"].append({"table": ref_name, "error": sf_settings_error})
                    state.set_error(sync_key, sf_settings_error)
                    continue
                from connectors.snowflake.extractor import materialize_query as sf_materialize_query

                stats = sf_materialize_query(
                    table_id=ref_name,
                    output_dir=sf_output_dir,
                    source_query=row.get("source_query"),
                    database=sf_settings.get("database"),
                    bucket=row.get("bucket"),
                    source_table=row.get("source_table"),
                    settings=sf_settings,
                    max_bytes=sf_max_bytes,
                )
            else:
                summary["errors"].append(
                    {
                        "table": ref_name,
                        "error": (f"materialized path not supported for source_type={row_source_type!r}"),
                    }
                )
                continue
        except MaterializeInFlightError:
            # In-flight on a sibling worker / scheduler tick — treat as
            # 'skipped, in-flight'. Do NOT call state.set_error: that
            # would flip status='error' on a healthy concurrent run and
            # the registry UI would surface a false-positive failure.
            # set_skipped (#754) persists the same non-error distinction so
            # `GET /api/admin/registry` explains the miss instead of leaving
            # the row's prior state unexplained.
            summary["skipped"].append({"table": ref_name, "reason": "in_flight"})
            state.set_skipped(sync_key, "in_flight")
            continue
        except MaterializeBudgetError as e:
            logger.warning(
                "Materialize cap exceeded for %s: %s bytes > %s bytes",
                e.table_id,
                f"{e.current:,}",
                f"{e.limit:,}",
            )
            summary["errors"].append(
                {
                    "table": ref_name,
                    "error": str(e),
                    "current": e.current,
                    "limit": e.limit,
                }
            )
            # Persist the failure so `GET /api/admin/registry` can surface
            # `last_sync_error` to the admin UI / `agnes admin status`.
            # Without this, scheduler stderr was the only place the cap
            # failure showed up and operators had no API path to it.
            state.set_error(sync_key, str(e))
            continue
        except Exception as e:
            logger.exception("Materialize failed for %s", ref_name)
            entry: dict = {"table": ref_name, "error": str(e)}
            if _is_permanent_upstream_error(e):
                # Upstream object is gone — retries can't heal this row, so
                # mark it permanent; _run_sync excludes these from the
                # job-failure decision (registry surgery is the fix, not a
                # retry). sync_state still records the error below, so the
                # admin registry UI keeps surfacing it per-table.
                entry["permanent"] = True
            summary["errors"].append(entry)
            state.set_error(sync_key, str(e))
            continue

        # `materialize_query` returns the parquet's MD5 inline — hashing
        # there means we don't re-read a multi-GB file on the request
        # thread. Fallback to `_file_hash(parquet_path)` if for some
        # reason the stats dict didn't carry it (defensive).
        parquet_hash = stats.get("hash")
        if not parquet_hash:
            # Keyed by source_type rather than an if/elif chain with a
            # Keboola `else`: a connector added later would otherwise
            # silently inherit the Keboola directory and hash a path that
            # does not exist (which is exactly what Snowflake rows did).
            # An unknown source_type is surfaced as a per-row error instead
            # of being hashed against a guessed directory.
            output_dir_for_hash = {
                "bigquery": bq_output_dir,
                "databricks": dbx_output_dir,
                "snowflake": sf_output_dir,
                "keboola": str(kb_output_dir.parent),
            }.get(row_source_type)
            if output_dir_for_hash is None:
                summary["errors"].append(
                    {
                        "table": ref_name,
                        "error": (
                            "materialize returned no hash and no extract directory is "
                            f"mapped for source_type={row_source_type!r}"
                        ),
                    }
                )
                continue
            parquet_path = Path(output_dir_for_hash) / "data" / f"{ref_name}.parquet"
            parquet_hash = _file_hash(parquet_path)
        # `update_sync` resets `status='ok'` / `error=NULL` on the upsert
        # path (its argument defaults), so a row that previously errored
        # has the failure cleared by this call. No separate clear_error
        # needed here — the test invariant is that a successful materialize
        # leaves status='ok' and error='', which `update_sync` already
        # establishes.
        state.update_sync(
            table_id=sync_key,
            rows=stats["rows"],
            file_size_bytes=stats["size_bytes"],
            hash=parquet_hash,
        )
        summary["materialized"].append(ref_name)

    return summary


# Credential-bearing statements the extractor may echo back. DuckDB puts the
# offending SQL in the message for a whole class of errors -- a Catalog error
# renders `LINE 1: <the statement>` verbatim -- and the Keboola path builds
# `ATTACH '<url>' AS kbc (TYPE keboola, TOKEN '<token>')`. That text reaches
# stderr, and `_record_extractor_crash` persists it to `sync_state`, where the
# admin UI renders it: a failure would move the storage token out of the
# process's stdout and into the app-state database. Redact the literal that
# follows a credential keyword before anything is stored.
# The keyword may be part of a larger identifier (`BEARER_TOKEN`,
# `storage_token`), so match any word CONTAINING it rather than the bare word.
_SECRET_LITERAL_RE = re.compile(
    r"(?i)([\w-]*(?:token|secret|password|passwd|pwd|apikey|api[_-]key|bearer)[\w-]*)"
    r"(\s*[:=]?\s*)('[^']*'|\"[^\"]*\"|[^\s,()]+)"
)

#: Cap the stored detail: an error is a UI cell, not a log sink, and a
#: multi-kilobyte message would be written once per attempted table.
_MAX_ERROR_DETAIL = 500


def _redact_secrets(text: str) -> str:
    """Blank out credential literals in a message bound for `sync_state`."""
    return _SECRET_LITERAL_RE.sub(r"\1\2[REDACTED]", text or "")[:_MAX_ERROR_DETAIL]


def _record_extractor_crash(*, table_configs: list, returncode: int, stderr: str) -> None:
    """Persist an error state for every table a DEAD extractor run attempted.

    The per-table ``set_error`` path fires only when the subprocess printed a
    parseable stats line. A credential failure kills the extractor at startup,
    so nothing is parsed and nothing is recorded -- and every admin surface
    that reports sync health reads ``sync_state``: the registry's error
    column, the source card's failing cell, and ``_resolve_sync_failures``,
    which feeds the ``/admin`` Needs-fixing zone. Without this the only copy
    of the cause was the server process's stdout, so the UI showed a green
    "Sync started" and then, indefinitely, "Never synced" with no error
    anywhere.

    Deliberately best-effort: this runs ON the failure path, and a second
    exception here would turn a reportable sync failure into a 500.
    """
    tail = (stderr or "").strip().splitlines()
    detail = _redact_secrets(tail[-1].strip()) if tail else ""
    message = (
        f"extractor failed (exit {returncode}): {detail}"
        if detail
        else f"extractor failed (exit {returncode}) -- see server logs for the cause"
    )
    try:
        state = sync_state_repo()
        registry_by_name = {r["name"]: r for r in table_registry_repo().list_all()}
        for tc in table_configs or []:
            name = tc.get("name") or tc.get("id")
            if not name:
                continue
            state.set_error(resolve_sync_state_key_for_row(name, registry_by_name.get(name)), message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record extractor crash in sync_state: %s", exc)


def _invoke_keboola_extractor_subprocess(
    table_configs: List[dict],
    env: dict,
    collected_errors: List[dict],
    synced_table_names: set,
    merge: bool = False,
) -> None:
    """Run the Keboola extractor subprocess once for ``table_configs``
    against ``env`` (must carry ``KEBOOLA_STACK_URL`` + ``KEBOOLA_STORAGE_
    TOKEN`` — see ``_resolve_keboola_credentials``). Mutates
    ``collected_errors`` / ``synced_table_names`` in place.

    ``merge=False`` (the first credential group of a pass) keeps the
    historical semantics: ``extractor.run()`` rebuilds ``extract.duckdb``
    from scratch, which is also the implicit prune for deleted/renamed
    registry rows. ``merge=True`` (every LATER group of the same pass)
    makes ``run()`` seed its temp build from the current extract and
    replace only its own tables — without it, each group's atomic
    tmp-then-move swap clobbered the previous group's output and only the
    LAST connection's tables survived the pass (every earlier group's
    ``_meta`` rows and views vanished from analytics at the next
    orchestrator rebuild).

    Extracted out of ``_run_sync`` (#B2) so the per-``connection_id`` group
    dispatch there can call this once per credential group instead of
    duplicating the subprocess plumbing per group — a UI-created Keboola
    connection was previously invisible here: this pass always used the
    single global env pair (``KEBOOLA_STACK_URL``/``KEBOOLA_STORAGE_
    TOKEN``), even for a ``local``/``remote`` row attributed to a
    different, non-default connection (wrong-project extraction on a
    multi-connection instance, or a silent no-op on a fresh one).
    """
    import json as _json
    import sys as _sys

    # v26: incremental + partitioned strategies need last_sync from
    # sync_state to compute changedSince. The subprocess MUST NOT
    # reopen system.duckdb (parent holds the lock — see contract at
    # the top of this function), so the parent reads watermarks
    # here and injects them into each table_config under the key
    # `__last_sync__`. extractor.run() picks them up via
    # _read_last_sync's first-check-config-then-fall-back pattern.
    ws_repo = sync_state_repo()
    for tc in table_configs:
        if tc.get("sync_strategy") in ("incremental", "partitioned"):
            state = ws_repo.get_table_state(tc.get("id") or tc.get("name"))
            if state and state.get("status") != "error":
                ls = state.get("last_sync")
                if ls is not None:
                    tc["__last_sync__"] = ls

    # Serialize configs — strip non-serializable fields
    serializable = []
    for tc in table_configs:
        serializable.append(
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in tc.items() if v is not None}
        )

    # Run extractor subprocess with table configs via stdin
    # Subprocess does NOT open system.duckdb — no lock conflict
    cmd = [
        _sys.executable,
        "-c",
        """
import json, sys, os, logging, signal
from pathlib import Path

# Subprocess inherits no logging config — without basicConfig, Python's
# lastResort handler only surfaces WARNING+ to stderr and INFO-level
# extraction progress from connectors.keboola.extractor.run() is silently
# dropped. capture_output=True in the parent then swallows the rest.
# Devin BUG_0002 on PR #136 review.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Convert SIGTERM into a controlled SystemExit so the ProcessPoolExecutor
# `with` block in connectors.keboola.extractor.run() runs its __exit__
# (shutdown/wait_for_workers) before this process dies. Without this,
# SIGTERM kills the parent abruptly, leaving the OS to clean up the pool
# children — but each worker holds an open Keboola Storage export job
# whose lifetime is tied to the HTTP poll loop, and those leak until the
# Keboola side TTLs them out. The parent extractor calls this from
# app.api.sync._run_sync after `subprocess.Popen(start_new_session=True)`
# + `os.killpg(SIGTERM)` on timeout.
def _exit_on_sigterm(signum, frame):
    sys.exit(143)
signal.signal(signal.SIGTERM, _exit_on_sigterm)

configs = json.load(sys.stdin)
url = os.environ.get("KEBOOLA_STACK_URL", "")
token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

if not url or not token:
    print("ERROR: Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN", file=sys.stderr)
    sys.exit(1)

from connectors.keboola.extractor import run, compute_exit_code
data_dir = Path(os.environ.get("DATA_DIR", "./data"))
# `--merge` (per-connection group 2..N of one sync pass): seed the temp
# build from the current extract.duckdb instead of rebuilding from
# scratch, so this group's swap does not clobber the previous group's
# tables. See app.api.sync._invoke_keboola_extractor_subprocess.
merge = "--merge" in sys.argv
result = run(str(data_dir / "extracts" / "keboola"), configs, url, token, merge=merge)
print(json.dumps(result))
# Issue #81 Group B: surface partial-failure as exit 2 so the API
# caller can distinguish "every table failed" from "9/10 succeeded".
sys.exit(compute_exit_code(result, len(configs)))
""",
    ]
    if merge:
        cmd.append("--merge")

    print(f"[SYNC] Starting extractor subprocess for {len(table_configs)} tables", file=_sys.stderr, flush=True)

    # Run in a new process group (start_new_session=True) so a
    # timeout can take down the whole tree — the extractor itself
    # plus any ProcessPoolExecutor workers it spawned for parallel
    # legacy-fallback. Without this, plain `subprocess.run` on
    # timeout SIGKILLs only the immediate child; the pool workers
    # are reparented to PID 1 and continue holding open Keboola
    # Storage export jobs, blocking the next sync cycle's
    # connectivity to those same job IDs.
    extractor_timeout = int(os.environ.get("AGNES_EXTRACTOR_TIMEOUT_SEC", "3600"))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent.parent),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=_json.dumps(serializable), timeout=extractor_timeout)
        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        # SIGTERM the whole process group first to give workers a
        # chance to shut down cleanly (release Keboola export jobs,
        # close DuckDB conns), then SIGKILL the stragglers after a
        # short grace window.
        import signal

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # Catch the timeout LOCALLY so the materialized BQ pass and
        # orchestrator rebuild below still fire — pre-fix the timeout
        # propagated to the outer except handler and skipped the rest
        # of `_run_sync` (Devin BUG_0001 on PR #148 commit 2219255).
        print(
            f"[SYNC] Extractor timed out after {extractor_timeout}s — process "
            "group killed; continuing to materialized pass + orchestrator rebuild",
            file=_sys.stderr,
            flush=True,
        )
        result = None
        # Record the timeout so the per-table webhook alert fires —
        # this LOCAL catch (the common timeout path) sets result=None
        # and skips the exit-code error collection below, so without
        # this append a clean materialized pass + rebuild would leave
        # collected_errors empty and the operator never learns the
        # extractor stalled (#397, #648 review).
        collected_errors.append(
            {
                "table": "(keboola extractor)",
                "error": f"extractor timed out after {extractor_timeout}s — process group killed",
            }
        )

    if result is not None:
        if result.stdout:
            print(f"[SYNC] Extractor stdout: {result.stdout.strip()[-500:]}", file=_sys.stderr, flush=True)
        if result.stderr:
            print(f"[SYNC] Extractor stderr: {result.stderr[-500:]}", file=_sys.stderr, flush=True)

        # #754 — recover the subprocess's per-table stats (it can't
        # write system.duckdb itself; the parent holds that lock for
        # the duration of the sync) and persist real failures via
        # sync_state.set_error so `GET /api/admin/registry` /
        # `agnes admin list-tables` can explain a "N total, 0 synced"
        # run instead of an operator having to trawl container logs
        # for the 500-char stdout tail above.
        extractor_stats = _parse_extractor_stats(result.stdout)
        extractor_table_errors = (extractor_stats or {}).get("errors") or []
        if extractor_table_errors:
            err_state = sync_state_repo()
            # One registry read for this batch of errors, not one
            # per entry — mirrors the same fix in
            # src.orchestrator._update_sync_state.
            err_registry_by_name = {r["name"]: r for r in table_registry_repo().list_all()}
            for entry in extractor_table_errors:
                tname = entry.get("table")
                terror = entry.get("error")
                if tname and terror:
                    # B1: resolve to the registry id (see
                    # `src.sync_state_key`) so this error lands under
                    # the same key `_update_sync_state` will use for
                    # this table on the next successful rebuild.
                    err_state.set_error(resolve_sync_state_key_for_row(tname, err_registry_by_name.get(tname)), terror)
                    collected_errors.append({"table": tname, "error": terror})

        # Issue #81 Group B: three exit codes. 0 = full success,
        # 1 = full failure, 2 = partial. Partial is a data-quality
        # alert, not a crash — the orchestrator's per-table _meta
        # machinery already captured which tables succeeded; we just
        # need to log loudly so operator alerting can pick it up.
        if result.returncode == 0:
            print("[SYNC] Extractor OK", file=_sys.stderr, flush=True)
        elif result.returncode == 2:
            print(
                "[SYNC] Extractor PARTIAL FAILURE (exit 2) — some tables "
                "succeeded, some failed; see stderr for per-table errors. "
                "Successful tables will still be published by the orchestrator.",
                file=_sys.stderr,
                flush=True,
            )
            # Real per-table entries (just persisted above) are more
            # actionable than this placeholder — only fall back to it
            # when the stats line couldn't be recovered at all.
            if not extractor_table_errors:
                collected_errors.append(
                    {
                        "table": "(keboola extractor)",
                        "error": "partial failure (exit 2) — see server logs for per-table errors",
                    }
                )
                # Same blind spot as the exit-1 branch below: no stats line
                # was recovered, so without this nothing reaches sync_state
                # and the partial failure is invisible to every admin surface.
                _record_extractor_crash(
                    table_configs=table_configs,
                    returncode=result.returncode,
                    stderr=result.stderr or "",
                )
        else:
            print(f"[SYNC] Extractor FAILED (exit {result.returncode})", file=_sys.stderr, flush=True)
            if not extractor_table_errors:
                collected_errors.append(
                    {
                        "table": "(keboola extractor)",
                        "error": f"extractor failed (exit {result.returncode}) — see server logs",
                    }
                )
                # …and record it where the ADMIN UI looks. `collected_errors`
                # only ever reached `notify_sync_failure`, which no-ops without
                # an alert webhook, so a run that died before printing stats
                # left every surface reporting "Never synced" and nothing
                # wrong. Every table this run attempted gets the cause.
                _record_extractor_crash(
                    table_configs=table_configs,
                    returncode=result.returncode,
                    stderr=result.stderr or "",
                )

        # Record which of THIS run's attempted tables actually landed
        # data, for notify_sync_completed below. "Attempted minus
        # recovered errors" over-claims in two ways, so both are
        # excluded here — an analyst-facing "N table(s) refreshed"
        # must never count a table this run did not write:
        #
        #  - Only `local` rows land parquet through this extractor.
        #    A `tables=[…]` operator trigger reads registry rows
        #    directly (`repo.get`), so table_configs can also carry
        #    `materialized` rows — which the extractor `continue`s
        #    over without recording anything, because
        #    `_run_materialized_pass` owns them and contributes its
        #    own positively-accounted names below (and may itself
        #    have skipped the row on its due/in_flight check) — and
        #    `remote` rows, which only get a view over the source:
        #    no data is downloaded, and `agnes pull` skips them.
        #  - Exit 2 means SOME table failed. When the stats line
        #    couldn't be parsed there is no per-table error list to
        #    subtract (that's the fallback branch above), so we know
        #    a failure happened but not whose — claim none rather
        #    than announce the failures as refreshes. Exit 0 carries
        #    no failures by construction (`compute_exit_code`), so
        #    it needs no such evidence.
        _stats_recovered = result.returncode == 0 or bool(extractor_table_errors)
        if result.returncode in (0, 2) and _stats_recovered:
            _failed_names = {e.get("table") for e in extractor_table_errors}
            for _tc in table_configs:
                _name = _tc.get("name")
                if not _name or (_tc.get("query_mode") or "local") != "local":
                    continue
                if _name not in _failed_names:
                    synced_table_names.add(_name)


def _run_sync(
    tables: Optional[List[str]] = None,
    source_type_filter: Optional[str] = None,
    result_sink: Optional[dict] = None,
) -> Optional[bool]:
    """Run extractor as subprocess + orchestrator rebuild.

    Reads table configs from DuckDB (in main process which has the shared
    connection), passes them as JSON via stdin to the extractor subprocess.
    This avoids DuckDB lock conflicts — subprocess never opens system.duckdb.

    ``source_type_filter`` (POST /api/sync/trigger?source=…) restricts the
    rebuild to a single registered source:

      - the local-mode list is selected with ``list_local(source_type_filter)``
        so only matching rows reach the extractor subprocess;
      - the Keboola extractor subprocess (which only knows how to extract
        Keboola rows) is skipped entirely unless the filter is None or
        ``"keboola"``;
      - the materialized pass receives the same filter so only matching
        ``source_type`` rows are rebuilt.

    The orchestrator rebuild always runs — it re-ATTACHes whatever
    ``extract.duckdb`` files exist on disk and never rewrites the ones it
    reads, so a scoped rebuild leaves the other source's extract untouched.

    Singleton: only one invocation runs at a time per process (see
    `_sync_lock` module-level docstring for the wave-2B update to this
    guard's role).

    Returns:
        ``True`` if the sync ran to completion with no fatal exception and
        no *transient* per-table failure recorded in ``collected_errors``
        (entries stamped ``permanent: True`` — upstream object deleted, see
        ``_is_permanent_upstream_error`` — do not fail the run: retrying
        cannot heal them, and they stay visible via per-table ``sync_state``
        errors + the operator notifier); ``False`` if a fatal exception was
        caught (subprocess timeout or otherwise) OR any transient per-table
        failure was recorded; ``None`` if this call short-circuited because
        another ``_run_sync`` already held ``_sync_lock`` in this same
        process (a no-op, not a failure — the in-flight run produces its
        own outcome).

        This function used to swallow every failure internally (log +
        best-effort webhook notify) and return nothing — fine for the old
        `BackgroundTasks` path, which had no result to report anywhere, but
        dishonest for the wave-2B `data-refresh` job path: without a real
        return value, ``app.worker.kinds._run_data_refresh`` could never
        tell the worker a sync failed, so the job always finalized
        ``'done'`` even on a fatal or partial failure (review carry-over,
        wave-2B W2B-4/7). ``_run_data_refresh`` raises ``RuntimeError`` when
        this returns ``False`` so the job's failure/retry semantics apply;
        it treats ``None`` the same as ``True`` (no-op, not a failure).

    ``result_sink`` (#1620, observability): when given a dict, this call
    populates it (in the ``finally`` below, so every return path except
    the immediate lock-contention no-op above fills it in) with
    ``{"materialized": <_run_materialized_pass summary or None if that
    pass never ran/blew up>, "errors": [...], "synced_tables": [...]}``.
    Does NOT change the True/False/None return contract above — existing
    callers that ignore this kwarg see no behavior change.
    ``app.worker.kinds._run_data_refresh`` passes one through so the
    per-table skip/error detail (previously visible only in server logs)
    surfaces via ``GET /api/jobs/{id}``'s stored ``payload_json["result"]``
    (``JobsRepository.complete(..., result=...)``).
    """
    import sys as _sys

    if not _sync_lock.acquire(blocking=False):
        print(
            "[SYNC] another sync is already in flight — skipping",
            file=_sys.stderr,
            flush=True,
        )
        return None

    # Accumulates per-table failures across the sync (materialized pass +
    # extractor) so both the per-table operator alert below and the fatal-path
    # alert in the outer `except` can report the same context.
    collected_errors: List[dict] = []

    # Names of tables THIS run actually attempted-and-succeeded syncing
    # (extractor-driven local tables minus any per-table failure, plus
    # materialized rows the pass reports as `materialized`). Used to narrow
    # the orchestrator's full rebuild result down to "what changed this
    # tick" for `notify_sync_completed` (#412 review: the rebuild result
    # covers every table from every prior sync too, not just this run's —
    # passing it straight through spammed a "tables refreshed" notification
    # on every scheduler tick even when nothing was due).
    synced_table_names: set = set()

    # `_run_materialized_pass`'s summary (#1620) — surfaced via
    # `result_sink` below. Stays None if the pass never ran or blew up
    # before returning (see the `except Exception` around the call).
    mat_summary_result: Optional[dict] = None

    try:
        from app.instance_config import get_data_source_type
        from src.db import get_system_db

        source_type = get_data_source_type()
        # Partial-rebuild scoping: when an explicit `?source=` filter is set,
        # it overrides the instance's configured source_type for row
        # selection (a dual-source deployment can ask to rebuild only BQ or
        # only Keboola). Falls back to the instance source_type for the
        # default full sweep.
        effective_source_type = source_type_filter or source_type
        data_dir = _get_data_dir()

        # Reclaim orphaned `kbc-export-*` staging dirs left behind when a
        # previous sync worker was hard-killed mid-export (SIGKILL / OOM /
        # auto-upgrade container recreate) so TemporaryDirectory.__exit__
        # never ran. Runs here — under `_sync_lock`, before any new scratch
        # is created — so it can never race a live in-flight export from this
        # process (and the age-gate covers any other container). Best-effort:
        # a sweep failure must never block the sync itself.
        try:
            from connectors.keboola.storage_api import sweep_orphaned_scratch

            sweep_orphaned_scratch()
        except Exception as _sweep_exc:  # pragma: no cover - defensive
            print(
                f"[SYNC] orphaned-scratch sweep skipped: {_sweep_exc}",
                file=_sys.stderr,
                flush=True,
            )

        # Read table configs in main process (has shared DuckDB connection)
        # Track whether the REGISTRY (not the post-filter/post-lookup list)
        # was empty. Auto-discovery must only fire on a truly empty
        # registry — computed from the WHOLE registry (`list_all()`) in
        # BOTH branches below, never from the caller-supplied subset.
        # If the schedule filter returned [] because nothing was due, or a
        # scoped `tables=[...]` id no longer exists in the registry (e.g. it
        # was deleted between queuing and running), re-discovering would
        # bypass the schedule / re-register the entire source project even
        # though the registry is populated. (Devin BUG_0001 on ebb8cc9;
        # #1253 hardened the `tables` branch the same way.)
        repo = table_registry_repo()
        registry_has_tables = bool(repo.list_all())
        if tables:
            # Manual operator override — bypass schedule filter entirely
            # so an admin saying "sync these specific tables now" wins.
            all_configs = [repo.get(t) for t in tables]
            table_configs = [c for c in all_configs if c is not None]
        else:
            table_configs = repo.list_local(effective_source_type) if effective_source_type else repo.list_local()
            # Without this filter, every scheduler tick would re-sync
            # every table regardless of its sync_schedule cadence,
            # making the field a no-op at trigger time. Tables with
            # no schedule pass through unchanged (opt-in feature).
            state_repo = sync_state_repo()
            table_configs = filter_due_tables(table_configs, state_repo)

        if not table_configs:
            # Auto-discover tables on first sync when registry is empty.
            # `not registry_has_tables` is the load-bearing guard — without
            # it, "filter excluded everything" looks identical to "registry
            # empty" and we'd re-discover + re-sync every tick regardless of
            # sync_schedule.
            if not os.environ.get("KEBOOLA_STORAGE_TOKEN"):
                try:
                    from app.datasource_secrets import datasource_secret as _ds  # noqa: PLC0415

                    _kbc_token_available = bool(_ds("KEBOOLA_STORAGE_TOKEN"))
                except Exception:
                    _kbc_token_available = False
            else:
                _kbc_token_available = True
            if not registry_has_tables and source_type == "keboola" and _kbc_token_available:
                logger.info("No tables registered — running auto-discovery from Keboola")
                try:
                    from app.api.admin import _discover_and_register_tables
                    from src.repositories import use_pg

                    # _discover_and_register_tables routes through the repository
                    # factory and ignores ``conn``; on Postgres pass None so the
                    # system DuckDB is never opened (forbidden invariant).
                    auto_conn = None if use_pg() else get_system_db()
                    try:
                        result = _discover_and_register_tables(auto_conn, "auto-discovery")
                        logger.info("Auto-discovered %d tables, skipped %d", result["registered"], result["skipped"])
                    finally:
                        if auto_conn is not None:
                            auto_conn.close()
                    # Re-read table configs after auto-registration
                    table_configs = table_registry_repo().list_local(effective_source_type)
                except Exception as e:
                    logger.warning("Auto-discovery failed: %s", e)

        # CRITICAL: don't early-return when local-mode tables are empty.
        # `list_local("bigquery")` is always empty on BQ-only deployments
        # (BQ rows are always remote or materialized, never local), so an
        # early return would prevent the materialized pass AND the
        # orchestrator rebuild from ever firing on a BQ-only instance.
        # Devin BUG_0002 on PR #148 commit 2fa44f2. Just flag whether the
        # Keboola subprocess + custom-connectors should run; everything
        # below (materialized pass, orchestrator rebuild, profiler) runs
        # unconditionally so a registry with materialized rows but no
        # local rows still publishes them.
        # The extractor subprocess below only knows how to extract Keboola
        # rows (it runs `connectors.keboola.extractor`). A partial rebuild
        # scoped to a non-Keboola source must never invoke it — otherwise a
        # `?source=bigquery` trigger would rewrite the Keboola extract.duckdb
        # via the subprocess and the rebuild would not be isolated.
        keboola_extract_in_scope = source_type_filter in (None, "keboola")
        run_extractor_subprocess = bool(table_configs) and keboola_extract_in_scope
        if not run_extractor_subprocess:
            logger.info(
                "No local-mode tables to sync for source_type=%s "
                "(filter=%s) — skipping extractor subprocess; materialized "
                "pass + orchestrator rebuild still run.",
                effective_source_type,
                source_type_filter,
            )

        env = {**os.environ}
        if not env.get("KEBOOLA_STORAGE_TOKEN"):
            from app.datasource_secrets import datasource_secret

            _vt = datasource_secret("KEBOOLA_STORAGE_TOKEN")
            if _vt:
                env["KEBOOLA_STORAGE_TOKEN"] = _vt

        if run_extractor_subprocess:
            # Group by connection_id — a row with no connection_id keeps
            # today's global-env behavior (one call, below); a row
            # attributed to a named connection gets its OWN subprocess
            # call against THAT connection's resolved credential (#B2).
            # Never fall back to the global env pair for a connection-
            # attributed row: on a multi-connection instance the global
            # pair belongs to a DIFFERENT project, and using it would
            # silently extract the wrong data rather than fail loudly.
            _by_connection: dict = {}
            for _tc in table_configs:
                _by_connection.setdefault(_tc.get("connection_id"), []).append(_tc)

            # Every group writes the SAME extracts/keboola/extract.duckdb,
            # and extractor.run()'s default mode rebuilds it from scratch
            # (temp build + atomic move). So: the FIRST invocation of the
            # pass runs fresh — keeping the whole-pass implicit-prune
            # semantics — and every later group passes merge=True so it
            # adds its own tables to the file instead of clobbering the
            # previous group's. Order the global (None) group first: with
            # remote rows on several stacks, the `kbc` `_remote_attach`
            # alias is first-writer-wins and must stay with the global
            # stack, whose token_env is the one the orchestrator can
            # resolve at re-ATTACH time.
            _first_group = True
            for _conn_id in sorted(_by_connection, key=lambda c: (c is not None, c or "")):
                _group_configs = _by_connection[_conn_id]
                if _conn_id is None:
                    _invoke_keboola_extractor_subprocess(
                        _group_configs,
                        env,
                        collected_errors,
                        synced_table_names,
                        merge=not _first_group,
                    )
                    _first_group = False
                    continue
                try:
                    _sc_url, _sc_token = _resolve_keboola_credentials(_conn_id)
                except _KeboolaCredentialError:
                    err_state = sync_state_repo()
                    for _tc in _group_configs:
                        _tname = _tc.get("name")
                        if _tname:
                            # B1: `_tc` is the registry row, so resolve the
                            # canonical id-first sync_state key directly.
                            err_state.set_error(resolve_sync_state_key_for_row(_tname, _tc), "missing_connection_token")
                            collected_errors.append({"table": _tname, "error": "missing_connection_token"})
                    continue
                _group_env = {**env, "KEBOOLA_STACK_URL": _sc_url, "KEBOOLA_STORAGE_TOKEN": _sc_token}
                _invoke_keboola_extractor_subprocess(
                    _group_configs,
                    _group_env,
                    collected_errors,
                    synced_table_names,
                    merge=not _first_group,
                )
                _first_group = False

            # Run custom connectors (Tier A: local mount) — only when there
            # were local-mode tables to drive the extractor. Custom connectors
            # currently piggyback on the same env as the Keboola extractor.
            connectors_dir = Path(
                os.environ.get("CONNECTORS_DIR", str(Path(__file__).parent.parent.parent / "connectors" / "custom"))
            )
            if connectors_dir.exists():
                for connector_dir in sorted(connectors_dir.iterdir()):
                    if not connector_dir.is_dir():
                        continue
                    extractor = connector_dir / "extractor.py"
                    if not extractor.exists():
                        continue
                    logger.info("Running custom connector: %s", connector_dir.name)
                    try:
                        custom_result = subprocess.run(
                            [_sys.executable, str(extractor)],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=600,
                            cwd=str(Path(__file__).parent.parent.parent),
                        )
                        if custom_result.returncode != 0:
                            logger.error(
                                "Custom connector %s failed: %s", connector_dir.name, custom_result.stderr[-500:]
                            )
                            # Symmetry with the Keboola extractor exit-code
                            # path — a failed custom connector must also reach
                            # the webhook alert, not just stderr (#648 review).
                            collected_errors.append(
                                {
                                    "table": f"(custom connector: {connector_dir.name})",
                                    "error": f"connector failed (exit {custom_result.returncode}) — see server logs",
                                }
                            )
                        else:
                            logger.info("Custom connector %s completed", connector_dir.name)
                    except subprocess.TimeoutExpired:
                        logger.error("Custom connector %s timed out", connector_dir.name)
                        collected_errors.append(
                            {
                                "table": f"(custom connector: {connector_dir.name})",
                                "error": "connector timed out after 600s",
                            }
                        )

        # Materialized SQL pass — runs admin-registered SQL through the
        # source's DuckDB extension (BQ via BqAccess, Keboola via
        # KeboolaAccess) and writes parquet for due rows. _run_materialized_pass
        # itself dispatches by source_type, so we always run it regardless of
        # which (or both) source types have a `project` / `stack_url` set —
        # Keboola-only instances would otherwise silently skip Keboola
        # materialized rows just because no BQ project is configured (Devin
        # finding 2026-05-01: BUG_pr-review-job-3fbd31c9_0001). The BQ
        # branch inside _run_materialized_pass uses a per-row try/except so
        # the sentinel BqAccess (not_configured) raises a typed error that
        # gets recorded against that row only — no cascade.
        try:
            from connectors.bigquery.access import get_bq_access
            from src.db import get_system_db as _get_system_db
            from src.repositories import use_pg

            bq_access = get_bq_access()  # sentinel if no BQ project; OK
            # _run_materialized_pass routes through the repository factory and
            # ignores ``conn``; on Postgres pass None so the system DuckDB is
            # never opened (forbidden invariant).
            mat_conn = None if use_pg() else _get_system_db()
            try:
                mat_summary = _run_materialized_pass(
                    mat_conn,
                    bq_access,
                    tables=tables,
                    source_type=source_type_filter,
                )
            finally:
                if mat_conn is not None:
                    mat_conn.close()
            mat_summary_result = mat_summary
            skipped_count = len(mat_summary["skipped"])
            in_flight_count = sum(1 for s in mat_summary["skipped"] if s.get("reason") == "in_flight")
            print(
                f"[SYNC] Materialized SQL: {len(mat_summary['materialized'])} ok, "
                f"{skipped_count} skipped (in_flight={in_flight_count}), "
                f"{len(mat_summary['errors'])} errors",
                file=_sys.stderr,
                flush=True,
            )
            for err in mat_summary["errors"]:
                print(
                    f"[SYNC]   {err['table']}: {err['error']}",
                    file=_sys.stderr,
                    flush=True,
                )
            # Carry the per-table failures forward for the operator alert
            # (fired after this block, and also surfaced if a later fatal
            # error hits the outer except).
            collected_errors.extend(mat_summary["errors"])
            # `materialized` already lists only the rows this pass wrote
            # successfully this tick — feed straight into the "what did THIS
            # run actually sync" set for notify_sync_completed below.
            synced_table_names.update(mat_summary["materialized"])
        except Exception as e:
            print(
                f"[SYNC] Materialized SQL pass FAILED: {e}",
                file=_sys.stderr,
                flush=True,
            )
            traceback.print_exc()
            # The whole materialized pass blowing up is itself a per-table-ish
            # failure operators should hear about; record it so the alert below
            # (and the fatal-path alert) include it.
            collected_errors.append({"table": "(materialized pass)", "error": str(e)})

        # Rebuild master views (reads extract.duckdb files, no write conflict)
        from src.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator()
        views = orch.rebuild()
        print(
            f"[SYNC] Orchestrator rebuild: {{{', '.join(f'{k}: {len(v)}' for k, v in views.items())}}}",
            file=_sys.stderr,
            flush=True,
        )
        # A source directory whose name fails identifier validation is
        # skipped by the orchestrator's scan (see InvalidSourceNameError's
        # docstring) — it never appears in `views` above, which otherwise
        # would look like a clean run. Feed it into the same per-run
        # operator alert every other per-table/per-pass sync failure uses.
        # getattr: several tests substitute a minimal orchestrator stub that
        # only implements rebuild() — treat "no attribute" the same as "no
        # errors" rather than requiring every stub to grow the field.
        rejected_sources = getattr(orch, "last_rebuild_errors", None)
        if rejected_sources:
            print(
                f"[SYNC] Orchestrator rebuild rejected {len(rejected_sources)} source(s): {rejected_sources}",
                file=_sys.stderr,
                flush=True,
            )
            for rejected_source, reason in rejected_sources.items():
                collected_errors.append({"table": f"(source: {rejected_source})", "error": reason})

        # Auto-profile synced tables (best-effort, don't fail sync on profile error).
        #
        # Each profile runs in a fresh Python subprocess (``src._profiler_worker``)
        # so all DuckDB allocator state — including the anon mmap arenas that
        # ``profile_table`` accumulates per call — is reliably reclaimed by the
        # OS on subprocess exit. Pre-subprocess, running this loop in-process
        # against ~30 tables would drift the resident set up by ~100-300 MiB
        # per iteration (Python's malloc keeps freed arenas, libc keeps the
        # heap), eventually tripping the cgroup OOM around 4 GiB even though
        # each individual ``profile_table`` cleaned up its DuckDB session
        # correctly. See PR notes for the empirical traces.
        #
        # The parent still owns the repository ``save(...)`` write so the
        # system.duckdb lock semantics stay single-writer: the worker
        # returns the profile dict, the parent persists it.
        try:
            from src._subprocess_runner import run_subprocess_job, SubprocessJobError

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            extracts_dir = data_dir / "extracts"

            profiles = profile_repo()
            profiled = 0
            for source_name, table_names in views.items():
                for table_name in table_names[:10]:  # Limit per sync
                    pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                    if not pq_path.exists():
                        continue
                    try:
                        profile = run_subprocess_job(
                            "src._profiler_worker",
                            {
                                "table_name": table_name,
                                "table_id": table_name,
                                "parquet_path": str(pq_path),
                            },
                            timeout_sec=600,
                        )
                        profiles.save(table_name, profile)
                        profiled += 1
                    except SubprocessJobError as pe:
                        # Worker-side failure — log subprocess stderr tail
                        # to surface the actual traceback to operators.
                        print(
                            f"[SYNC] Profile {table_name}: {pe}\n  stderr tail: {pe.stderr[-500:]}",
                            file=_sys.stderr,
                            flush=True,
                        )
                    except Exception as pe:
                        print(f"[SYNC] Profile {table_name}: {pe}", file=_sys.stderr, flush=True)
            print(f"[SYNC] Profiled {profiled} tables", file=_sys.stderr, flush=True)
        except Exception as e:
            print(f"[SYNC] Profiler skipped: {e}", file=_sys.stderr, flush=True)

        # Operator alert on per-table sync errors (non-fatal). Fired at the
        # END of the try — AFTER the orchestrator rebuild — not mid-flow:
        # if a later step (rebuild) raises, the fatal handler below sends a
        # single combined alert (failed_tables=collected_errors, fatal=e)
        # instead of this firing first and the fatal path firing a second,
        # overlapping POST for the same run (#648 review). Best-effort:
        # notify_sync_failure no-ops without a webhook and never raises.
        if collected_errors:
            try:
                from app.services.sync_notifier import notify_sync_failure

                notify_sync_failure(failed_tables=collected_errors, fatal=None)
            except Exception:
                logger.exception("sync-failure notifier raised on per-table path")

        # Analyst desktop notification (#412: `agnes watch`) — fired here,
        # AFTER the error accounting above, not right after the rebuild: the
        # payload carries the run's outcome, so a run with per-table
        # failures that still rebuilt views announces status="partial" +
        # the error count instead of an unqualified success. (If the
        # rebuild itself raises, control jumps to the fatal handler and no
        # completed event fires at all.) Best-effort: notify_sync_completed
        # never raises on its own, but wrap anyway — same "second line of
        # defence" pattern as notify_sync_failure above.
        #
        # `views` is the orchestrator's FULL rebuild result — every table of
        # every source that has ever synced, not just this run's. Passing it
        # straight through fired a "tables refreshed" notification on every
        # scheduler tick, even ones where `filter_due_tables` selected
        # nothing and the rebuild just re-attached the same unchanged
        # extracts (review finding: notification spam). Narrow it down to
        # `synced_table_names` — the tables THIS run actually attempted and
        # landed — so a tick that synced nothing sends nothing.
        synced_views = {
            source_name: [t for t in table_names if t in synced_table_names]
            for source_name, table_names in views.items()
        }
        synced_views = {k: v for k, v in synced_views.items() if v}
        try:
            from app.services.sync_notifier import notify_sync_completed

            notify_sync_completed(synced_views, error_count=len(collected_errors))
        except Exception:
            logger.exception("sync-completed notifier raised")

        # Honest outcome for the `data-refresh` job path (see docstring):
        # only *transient* per-table failures flip the run to False (job
        # 'failed', retry engages). Entries stamped `permanent: True`
        # (upstream object gone — see _is_permanent_upstream_error) are
        # excluded: retrying can never heal them, so failing the job would
        # keep it red forever and mask real failures from monitoring. They
        # stay visible via sync_state per-table errors + the operator
        # notifier above.
        transient_errors = [e for e in collected_errors if not e.get("permanent")]
        if collected_errors and not transient_errors:
            logger.warning(
                "sync completed with %d permanently-failing table(s) (upstream "
                "object gone): %s — not failing the data-refresh job; re-point "
                "or unregister these registry rows",
                len(collected_errors),
                ", ".join(str(e.get("table")) for e in collected_errors),
            )
        return not transient_errors

    except subprocess.TimeoutExpired as e:
        # Outer-handler fallback for any subprocess.run call site (e.g.
        # custom-connectors below) that didn't already catch its own
        # TimeoutExpired. Concrete timeout value isn't available here —
        # log generically.
        print("[SYNC] Extractor subprocess timed out", file=_sys.stderr, flush=True)
        # A swallowed timeout is exactly the silent failure this feature
        # exists to surface — alert operators, same best-effort wrapping as
        # the generic-exception path below (#397, #648 review).
        try:
            from app.services.sync_notifier import notify_sync_failure

            notify_sync_failure(failed_tables=collected_errors, fatal=e)
        except Exception:
            logger.exception("sync-failure notifier raised on timeout path")
        return False
    except Exception as e:
        print(f"[SYNC] FAILED: {e}", file=_sys.stderr, flush=True)
        traceback.print_exc()
        # Operator alert on the fatal path. Best-effort: notify_sync_failure
        # never raises, but wrap anyway so an import-time issue can't mask the
        # original failure or leave _sync_lock held.
        try:
            from app.services.sync_notifier import notify_sync_failure

            notify_sync_failure(failed_tables=collected_errors, fatal=e)
        except Exception:
            logger.exception("sync-failure notifier raised on fatal path")
        return False
    finally:
        # #1620: fills in `result_sink` (when the caller passed one) on
        # every path through the try above — success, per-table failure,
        # or the fatal-exception handlers — using whatever was collected
        # before things went wrong. Runs before `_sync_lock.release()`;
        # order between the two doesn't matter (`result_sink` is caller-
        # owned, not synchronized by the lock).
        if result_sink is not None:
            result_sink["materialized"] = mat_summary_result
            result_sink["errors"] = list(collected_errors)
            result_sink["synced_tables"] = sorted(synced_table_names)
        _sync_lock.release()


# ---- Manifest ----

# Three-plane wave 2-H, WS F, task WF-2 (signed-URL distribution) — see
# docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md.
# 15-minute presign TTL per the wave plan's non-negotiable design decisions
# ("TTL ≈ 15 min bounds leakage").
_SIGNED_URL_TTL_S = 900


def _resolve_signed_url_context() -> tuple[Optional[ObjectStore], dict]:
    """Resolve the object store + its (TTL-cached) mirror index ONCE per
    manifest build — never per-table.

    Returns ``(None, {})`` when signed-URL distribution is off (explicit
    ``distribution.signed_urls: off`` escape hatch) or no store is
    configured (``auto``/``on`` with nothing set up); callers then skip
    ``signed_url`` entirely, so an instance with no object store produces a
    byte-for-byte identical manifest to before this feature existed.

    The single :func:`~src.distribution.cached_mirror_index` call is the
    only store touch this makes — no per-table HEAD/GET — and it is itself
    fail-open (a store outage degrades to an empty index, never a manifest
    build failure), keeping the manifest p95 < 300ms budget intact even
    when the object store is slow or down.
    """
    if distribution_signed_urls_mode() == "off":
        return None, {}
    store = object_store()
    if store is None:
        return None, {}
    return store, cached_mirror_index(store)


def _apply_signed_url(
    entry: dict,
    table_id: str,
    *,
    query_mode: str,
    server_only: bool,
    store: Optional[ObjectStore],
    mirror_index: dict,
) -> None:
    """Mutate *entry* in place, adding ``signed_url`` / ``signed_url_expires_at``
    when — and only when — ALL of the following hold:

    - a store is configured and signed-URL distribution isn't off
      (*store* is ``None`` otherwise, per :func:`_resolve_signed_url_context`);
    - the table is downloadable (``query_mode`` != ``remote``, not
      ``server_only`` — remote tables have no server parquet at all, and
      server_only ones are deliberately not distributed);
    - the table is not one of the internal row-level-RBAC tables
      (``agnes_sessions`` / ``agnes_telemetry`` / ``agnes_audit`` — see
      ``connectors.internal.access.is_internal_table``). Those tables are
      accessible to every user at the table level, but access is scoped
      per-row via a WHERE clause applied at query time
      (``src.rbac.get_accessible_tables``, ``connectors/internal/access.py``);
      a signed URL would serve the *entire* parquet — every user's rows —
      bypassing that row filter. In practice these tables never reach the
      sync_state/mirror pipeline today, so this is defense-in-depth against
      a future change that starts mirroring them;
    - the table_id is present in *mirror_index* with an md5 that matches
      this entry's own md5 exactly — an absent or stale mirror entry means
      the object either doesn't exist yet or is behind the latest sync, so
      the client must fall back to ``/api/data/{id}/download`` rather than
      get a presigned URL to the wrong (or missing) bytes.

    RBAC note: this is called only for entries already in the caller's
    accessible-table set (filtered upstream in ``_build_manifest_for_user``
    via ``get_accessible_tables``) — signed URLs are added to already-
    authorized entries, never widen access.
    """
    from connectors.internal.access import is_internal_table

    if store is None or server_only or query_mode == "remote" or is_internal_table(table_id):
        return
    md5 = entry.get("hash") or ""
    if not md5 or mirror_index.get(table_id) != md5:
        return
    entry["signed_url"] = store.presign_get(f"{table_id}.parquet", ttl_s=_SIGNED_URL_TTL_S)
    entry["signed_url_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=_SIGNED_URL_TTL_S)).isoformat()


def _compute_manifest_policy_fingerprint(reg: dict, principal) -> "str | None":
    """The per-caller policy fingerprint for one manifest table entry
    (table access policies §3.4, §10.3; plan Task 18) — what a local
    ``agnes pull`` compares a snapshot's stored ``SnapshotMeta.
    policy_fingerprint`` against to detect that the policy (or the
    puller's own group membership) drifted since ``agnes snapshot
    create``/``refresh`` ran, and withhold that snapshot's view via the
    same ``snapshot_views_blocked`` mechanism #1129 already built for a
    de-authorized or newly-``server_only`` table.

    ``None`` whenever there's nothing to protect: no ``principal`` in hand
    (a direct caller of ``_table_manifest_entry`` with none to give — every
    such site keeps compiling and simply gets a null fingerprint, exactly
    as a non-policied table would), or no policy on the table (checked
    BEFORE touching ``src.access_policy`` so the overwhelmingly common
    non-policied table pays nothing extra building its manifest entry).
    ``policy_fingerprint`` itself returns ``None`` for the remaining case —
    the admin bypass (§12).

    Defensive: a manifest build must never fail FOR THE WHOLE CALLER
    because ONE table's fingerprint computation raised (an unresolvable
    principal shape — a co-drive ``SessionPrincipal`` reaching this via
    some future caller — or a registry race). The direct caller here
    (``_build_data_packages_section``) is itself wrapped by a coarse
    ``try/except`` in ``_build_manifest_for_user`` that would otherwise
    blank out the ENTIRE ``data_packages`` section — not just this one
    field — for that request.
    """
    if principal is None or not reg.get("access_policy_sql"):
        return None
    table_id = reg.get("id") or reg.get("name")
    if not table_id:
        return None

    from src.access_policy import policy_fingerprint

    try:
        return policy_fingerprint(table_id, principal)
    except Exception:
        logger.exception("access-policy fingerprint computation failed for table %r; manifest omits it", table_id)
        return None


def _table_manifest_entry(state: dict, reg: dict, *, principal=None) -> dict:
    """Shape one ``sync_state`` row + registry metadata into the per-table
    manifest object used in ``data_packages[].tables`` and ``direct_tables``.

    Tolerant to empty ``state`` (table is registered but never synced) and
    empty ``reg`` (sync_state row outlives the registry — race on unregister).
    Both happen in real installs; the manifest is the read path so we must
    not blow up on a partially-consistent snapshot.

    ``principal`` (plan Task 18) is the caller this manifest is being built
    for — used ONLY to compute ``access_policy_fingerprint`` below.
    Optional, default ``None``, so every existing direct caller of this
    helper (tests, and any future caller with no principal in hand) keeps
    compiling unchanged and simply gets a ``None`` fingerprint.

    ``name`` prefers ``reg["name"]`` (B1: ``state["table_id"]`` is the
    registry ``id`` when a matching row existed at write time — see
    ``src.sync_state_key`` — not the on-disk parquet stem) so this field
    keeps meaning what every caller already treats it as: the flat parquet
    stem ``agnes pull`` downloads under (its own docstring: "the authorized
    table-name set is the union of every typed entry's `name` field — which
    equals the flat parquet stem"). Falls back to ``state["table_id"]`` for
    a sync_state row that outlived its registry row (no ``reg`` to read a
    name from).
    """
    name = reg.get("name") or state.get("table_id") or reg.get("id") or ""
    entry = {
        "id": reg.get("id") or name,
        "name": name,
        "hash": state.get("hash", ""),
        "md5": state.get("hash", ""),
        "size_bytes": state.get("file_size_bytes", 0),
        "rows": state.get("rows", 0),
        "query_mode": reg.get("query_mode") or "local",
        # #607 — distribution flag. Listed in the manifest (catalog + RBAC)
        # but `agnes pull` skips its parquet download when true.
        "server_only": bool(reg.get("server_only")),
        # Task 11 (§10 item 4) — disclosure marker, not an enforcement gate:
        # `agnes pull` reads this to name policied tables in a
        # `.claude/rules/` entry BEFORE an agent writes a query, the only
        # link in the disclosure chain that reaches an agent's context
        # ahead of the fact rather than after a response comes back.
        "access_policy": bool(reg.get("access_policy_sql")),
        # Task 18 (§3.4, §10.3) — the CURRENT, per-caller policy fingerprint.
        # See `_compute_manifest_policy_fingerprint` for the full contract.
        "access_policy_fingerprint": _compute_manifest_policy_fingerprint(reg, principal),
        "source_type": reg.get("source_type") or "",
        "updated": (state.get("last_sync").isoformat() if state.get("last_sync") else None),
    }
    # Per-partition manifest for partitioned tables (partitioned distribution).
    # Added ONLY when present so single-file entries stay byte-identical. The
    # whole-table ``hash`` above is the rollup of the sorted part hashes, so the
    # cheap "changed?" compare + object-store mirror keep working.
    if state.get("parts") is not None:
        entry["parts"] = state.get("parts")
    return entry


def _build_data_packages_section(conn, user, registry_by_name: dict, states_by_table_id: dict) -> tuple[list, set, set]:
    """Build the ``data_packages`` array per Section 5.1 of the design.

    Returns ``(data_packages, packaged_table_ids, non_download_table_names)``:

    * ``packaged_table_ids`` — ``table_registry.id`` values surfaced via at
      least one package — used to subtract from ``direct_tables`` so a
      table belonging to a package doesn't double-render.
    * ``non_download_table_names`` — ``table_registry.name`` values (the key
      the flat ``tables`` manifest dict uses) belonging to a package that is
      granted-but-not-materialized for this user (auto-membership: the
      package is visible/authorized, per :meth:`StackResolver.stack`, but
      the user never subscribed to a local copy). The caller ORs this into
      the flat dict's ``server_only`` flag so ``agnes pull`` lists the table
      as authorized+queryable without fetching its parquet — the same
      listed-but-not-downloaded treatment ``server_only`` already gets
      (#607), reused here rather than inventing a second flag.
    """
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver
    from app.auth.session_principal import PRINCIPAL_TYPES

    resolver = StackResolver(conn)
    stack_subject = user if isinstance(user, PRINCIPAL_TYPES) else user["id"]
    pkg_entries = resolver.stack(stack_subject, ResourceType.DATA_PACKAGE)
    if not pkg_entries:
        return [], set(), set()
    repo = data_packages_repo()
    packaged_table_ids: set = set()
    non_download_table_names: set = set()
    out: list = []
    for entry in pkg_entries:
        pkg = repo.get(entry.id)
        if not pkg:
            continue
        table_rows = repo.list_tables(entry.id)
        tables_payload: list = []
        total_size_bytes = 0
        for t in table_rows:
            packaged_table_ids.add(t["id"])
            # registry_by_name keys on name (unaffected by B1 — `t` is a
            # genuine data_packages junction row, not a sync_state lookup).
            # `states_by_table_id` keys on whatever a writer stored: the
            # registry id (B1, the common case going forward) or — for a
            # legacy/unmatched row — the name. Try both.
            reg = registry_by_name.get(t["name"]) or {}
            state = states_by_table_id.get(t["id"]) or states_by_table_id.get(t["name"]) or {}
            entry_obj = _table_manifest_entry(state, reg or {"id": t["id"]}, principal=user)
            if not entry.materialized:
                # Auto-membership: authorized + listed, but not downloaded
                # until the user subscribes (mirrors server_only semantics).
                entry_obj["server_only"] = True
                non_download_table_names.add(t["name"])
            tables_payload.append(entry_obj)
            total_size_bytes += int(entry_obj.get("size_bytes") or 0)
        out.append(
            {
                "id": pkg["id"],
                "slug": pkg["slug"],
                "name": pkg["name"],
                "icon": pkg.get("icon"),
                "color": pkg.get("color"),
                "description": pkg.get("description"),
                "requirement": entry.requirement,
                "tables": tables_payload,
                "total_size_bytes": total_size_bytes,
            }
        )
    return out, packaged_table_ids, non_download_table_names


def _build_knowledge_artifacts_section(user) -> list:
    """``knowledge_artifacts`` manifest array: K3 chunk artifacts + K4 digests.

    Two independent ``kind`` families share this one list (the seam K3 left
    open, ``src/knowledge_packaging.py`` module docstring): ``kind:"chunks"``
    per-corpus ``knowledge.duckdb`` artifacts, and ``kind:"digest"`` maintained
    digests (K4, #799). Each family has its own RBAC filter and its own
    "nothing built yet" empty case — a caller with digests but zero packaged
    corpora (or vice versa) must still see the family they DO have access to,
    so neither branch early-returns on the other's empty state. Both helpers
    always return a (possibly empty) list, so this key is ALWAYS present in
    the manifest — ``agnes pull`` gates its prune on key presence, mirroring
    the typed-sections gate.
    """
    return _chunk_artifact_entries(user) + _digest_entries(user)


def _chunk_artifact_entries(user) -> list:
    """Per-corpus knowledge.duckdb artifacts (K3, #798), collection-grant filtered.

    Reads ``DATA_DIR/knowledge/state.json`` (written by the packaging pass) and
    lists only corpora the caller may access — the same fail-closed filter as
    ``/api/collections``.
    """
    from app.api.collections import _accessible_corpus_ids
    from src.knowledge_packaging import artifacts_dir, load_state

    state = load_state()
    if not state:
        return []
    allowed = set(_accessible_corpus_ids(user))
    names = {c["id"]: c.get("name") for c in file_corpora_repo().list_all()}
    out = []
    for cid in sorted(state):
        if cid not in allowed or not (artifacts_dir() / f"{cid}.duckdb").exists():
            continue
        entry = state[cid]
        out.append(
            {
                "kind": "chunks",
                "corpus_id": cid,
                "name": names.get(cid),
                "md5": entry.get("md5", ""),
                "size_bytes": entry.get("size_bytes", 0),
                "chunks": entry.get("chunks", 0),
                "built_at": entry.get("built_at"),
                "url": f"/api/knowledge/artifacts/{cid}/download",
            }
        )
    return out


def _digest_entries(user) -> list:
    """``kind:"digest"`` manifest entries (K4, #799), knowledge-digest-grant filtered.

    Frozen shape (see the K4 plan): ``{kind, id, slug, title, status,
    status_reason, generated_at, md5, url}``. ``md5`` is a change-detection
    token — not a byte-level integrity check of the downloaded content (the
    JSON body over TLS+PAT is the truth, the per-domain md5 posture,
    ``_build_memory_domains_section`` above) — computed over
    ``slug|status|status_reason|generated_at|output_md`` so it flips when
    EITHER content OR staleness changes: a digest going stale must re-fetch
    so the staleness banner reaches ``agnes pull``'s ``.claude/rules/`` copy.

    Digests with no ``output_md`` yet (``status='pending'``, never
    generated) are never listed — nothing to distribute. Sorted by slug.
    """
    from app.api.knowledge_search import _caller_can_read_digest
    from src.repositories import knowledge_digests_repo

    out = []
    for d in knowledge_digests_repo().list():
        output_md = d.get("output_md") or ""
        if not output_md.strip():
            continue
        if not _caller_can_read_digest(user, d["id"]):
            continue
        status = d.get("status") or "pending"
        status_reason = d.get("status_reason")
        generated_at = d.get("generated_at")
        generated_at_str = generated_at.isoformat() if generated_at else None
        token = f"{d['slug']}|{status}|{status_reason or ''}|{generated_at_str or ''}|{output_md}"
        out.append(
            {
                "kind": "digest",
                "id": d["id"],
                "slug": d["slug"],
                "title": d["title"],
                "status": status,
                "status_reason": status_reason,
                "generated_at": generated_at_str,
                "md5": hashlib.md5(token.encode()).hexdigest(),
                "url": f"/api/knowledge/digests/{d['id']}/content",
            }
        )
    return sorted(out, key=lambda e: e["slug"])


def _build_memory_domains_section(conn, user) -> list:
    """Build the ``memory_domains`` array per Section 5.1.

    Each entry carries a per-domain ``md5`` derived from the concatenated
    item content/titles inside the domain — when the bundle changes the
    md5 flips so the CLI knows to re-fetch.

    TODO(phase-7): ``bundle_url`` points at a yet-to-implement per-domain
    bundle endpoint (``/api/memory/bundle?domain=<slug>``). The CLI in
    Phase 7 will need it; for now we emit the URL the future endpoint
    will live at so older clients keep parsing the manifest cleanly.
    """
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver
    from app.auth.session_principal import PRINCIPAL_TYPES
    from app.api.memory import (
        resolve_distribution_mode,
        select_distributable_items,
        _caller_upvoted_item_ids,
    )
    from src.repositories import knowledge_repo

    resolver = StackResolver(conn)
    stack_subject = user if isinstance(user, PRINCIPAL_TYPES) else user["id"]
    dom_entries = resolver.stack(stack_subject, ResourceType.MEMORY_DOMAIN)
    if not dom_entries:
        return []
    repo = memory_domains_repo()
    # #1573: same predicate the JSON bundle and per-domain markdown apply —
    # computed once per request, not per domain, since it doesn't vary by
    # domain (the caller's votes and the configured mode are global).
    distribution_mode = resolve_distribution_mode()
    upvoted_ids = _caller_upvoted_item_ids(user, knowledge_repo()) if distribution_mode == "hybrid" else set()
    out: list = []
    for entry in dom_entries:
        dom = repo.get(entry.id)
        if not dom:
            continue
        items = repo.list_items_of_domain(entry.id, limit=10000)
        # Per-domain md5 — concatenate sorted item tuples so the hash
        # is stable under list ordering and flips on any content
        # mutation. MUST include ``is_required`` and ``content``
        # because the bundle rendered by ``_build_per_domain_markdown``
        # routes items between "## Required" and "## Approved" by
        # ``is_required`` and embeds the full ``content`` body; without
        # these in the hash, an admin edit of either dimension leaves
        # the manifest md5 unchanged → ``agnes pull`` skips the
        # re-fetch → analyst keeps a stale bundle.md.
        #
        # Filter through the SAME function the renderer calls
        # (``select_distributable_items``, #1573) — any ``is_required``
        # item unconditionally, plus whichever approved items
        # ``distribution_mode`` grants THIS caller — so edits to
        # pending/rejected/not-yet-opted-in items don't flip the md5
        # against an identical-bytes bundle, and a distribution_mode
        # change or a vote flips it exactly when the rendered bytes
        # would change (the original Devin review flagged this asymmetry
        # for BUG-0001; this predicate is the one place both surfaces
        # must keep calling, not re-deriving).
        h = hashlib.md5()
        renderable = select_distributable_items(items, distribution_mode, upvoted_ids)
        for it in sorted(renderable, key=lambda r: r["id"]):
            h.update(
                f"{it['id']}|{it.get('title', '')}|{it.get('status', '')}|"
                f"{it.get('is_required', False)}|{it.get('content', '')}|".encode()
            )
        required_count = sum(1 for it in items if (it.get("status") == "approved" and it.get("is_required")))
        out.append(
            {
                "id": dom["id"],
                "slug": dom["slug"],
                "name": dom["name"],
                "icon": dom.get("icon"),
                "color": dom.get("color"),
                "description": dom.get("description"),
                "requirement": entry.requirement,
                "bundle_url": f"/api/memory/bundle?domain={dom['slug']}",
                "md5": h.hexdigest(),
                "items_count": len(items),
                "required_count": required_count,
            }
        )
    return out


def _build_direct_tables_section(
    conn,
    user: dict,
    registry_by_name: dict,
    states_by_table_id: dict,
    packaged_table_ids: set,
) -> list:
    """Always returns ``[]`` — per-table grants no longer manifest for
    analysts.

    The unified-stack design routes all analyst access through data
    packages: admins manage RBAC by adding tables to a package and
    granting the package. Ad-hoc ``resource_grants(group, 'table', …)``
    rows that aren't wrapped in a package used to ship as
    ``direct_tables[]`` here (for backwards-compat with pre-unified
    CLIs); that BC is now dropped because it silently leaked
    individually-granted tables into ``agnes catalog`` and the
    user-facing manifest, contradicting the "stack is the unit of
    access" promise of the new design.

    The empty array is kept in the manifest payload (instead of
    omitting the key) so older CLIs that destructure
    ``manifest["direct_tables"]`` don't KeyError.
    """
    return []


def _build_manifest_for_user(conn, user: dict) -> dict:
    """Build manifest dict filtered by user's accessible tables.

    Joins ``sync_state`` with ``table_registry`` so each table entry exposes
    ``query_mode`` and ``source_type``. The CLI uses these to decide whether
    to download a parquet (local) or skip it (remote, e.g. BigQuery views).

    Defensive defaults: if a sync_state row has no matching registry entry
    (race / manual deletion), fall back to ``query_mode='local'`` and
    ``source_type=''`` so the manifest still serializes cleanly.

    v49: extended with ``data_packages`` / ``memory_domains`` /
    ``direct_tables`` arrays per Section 5.1 of the unified-stack design.
    Legacy ``tables`` dict stays in parallel for one release — older CLIs
    still parse it; newer clients prefer the typed sections.
    """
    sync_repo = sync_state_repo()
    table_repo = table_registry_repo()
    all_states = sync_repo.get_all_states()
    # B1: `sync_state.table_id` is the registry `id` when a matching row
    # existed at write time (`src.sync_state_key.resolve_sync_state_key`);
    # a legacy row a backfill hasn't reached yet, or one from a table with
    # no registry match at write time, is still keyed by name. Every lookup
    # below tries `id` first, `name` second, via `_reg_for`.
    #
    # The manifest itself, however, must keep exposing the actual on-disk
    # parquet stem — `table_registry.name`, what the extractor / materialize
    # pass names files after, NOT `id` — as its flat `tables{}` key and as
    # the `signed_url`/download identifier. `agnes pull` and the
    # distribution mirror job (`app/worker/kinds.py`) both treat that key as
    # a literal filename, so this key migration must never leak into it.
    all_tables = table_repo.list_all()
    registry_by_id = {t["id"]: t for t in all_tables}
    registry_by_name = {t["name"]: t for t in all_tables}

    def _reg_for(raw_table_id: str) -> dict:
        return registry_by_id.get(raw_table_id) or registry_by_name.get(raw_table_id) or {}

    # Filter by user's accessible tables. `get_accessible_tables` resolves the
    # caller's accessible id set ONCE (None => admin/all) instead of the old
    # per-row `can_access_table` call — same admin shortcut and stack-gated
    # semantics, collapsed from an N+1 to a single resolution + in-memory
    # membership test (FAI-132).
    def _id_for(state):
        reg = _reg_for(state["table_id"])
        return reg.get("id") or state["table_id"]

    _accessible_ids = get_accessible_tables(user, conn)
    _allowed = None if _accessible_ids is None else set(_accessible_ids)
    all_states = [s for s in all_states if _allowed is None or _id_for(s) in _allowed]

    data_dir = _get_data_dir()

    # v-next (auto-membership): resolve which of the accessible tables belong
    # to a granted-but-not-materialized data package BEFORE building the flat
    # `tables` dict, so the per-user download-skip flag can be OR'd into each
    # entry's `server_only` below. Built early (states_by_table_id only needs
    # the already-filtered `all_states`) rather than in its historical spot
    # after the flat loop, purely to make that OR possible in one pass.
    states_by_table_id = {s["table_id"]: s for s in all_states}
    try:
        data_packages, packaged_ids, non_download_table_names = _build_data_packages_section(
            conn,
            user,
            registry_by_name,
            states_by_table_id,
        )
    except Exception:
        logger.exception("manifest data_packages section build failed")
        data_packages, packaged_ids, non_download_table_names = [], set(), set()

    # WF-2 (signed-URL distribution) — resolved ONCE per manifest build, not
    # per-table. See `_resolve_signed_url_context`'s docstring for the
    # perf/fail-open rationale. `signed_url`/`signed_url_expires_at` are
    # added ONLY to this flat `tables` dict — the shape
    # `cli/lib/pull.py:run_pull`'s download loop actually reads
    # (`manifest.get("tables", {})`, hash-compared per row). The typed
    # `data_packages[].tables[]` section (`_table_manifest_entry`) is only
    # consulted by `run_pull` to build a name-based RBAC filter, never for
    # hash/download decisions, so it intentionally does not get these
    # fields.
    _signed_url_store, _mirror_index = _resolve_signed_url_context()
    tables = {}
    for state in all_states:
        reg = _reg_for(state["table_id"])
        # The flat dict's key IS the parquet stem `agnes pull` downloads
        # under and the distribution mirror uploads under — see the
        # docstring note above. Falls back to the raw sync_state key for an
        # orphaned row (no registry match at all).
        table_id = reg.get("name") or state["table_id"]
        query_mode = reg.get("query_mode") or "local"
        # #607 registry-level flag OR'd with the v-next per-user
        # auto-membership flag: authorized+listed but not downloaded until
        # the user subscribes to a local copy of the owning data package.
        server_only = bool(reg.get("server_only")) or table_id in non_download_table_names
        entry = {
            "hash": state.get("hash", ""),
            "updated": state.get("last_sync").isoformat() if state.get("last_sync") else None,
            "size_bytes": state.get("file_size_bytes", 0),
            "rows": state.get("rows", 0),
            "query_mode": query_mode,
            # #607 — distribution flag consumed by the cli/lib/pull.py
            # download-set loop: listed here but its parquet is not fetched.
            "server_only": server_only,
            "source_type": reg.get("source_type") or "",
        }
        # Per-partition manifest — the cli/lib/pull.py download-set loop reads
        # THIS flat dict (not the typed sections), so `parts` MUST live here
        # for partitioned tables to route to the per-part sync. Added ONLY for
        # partitioned tables (like signed_url below), so single-file entries
        # stay byte-identical for old CLIs / manifest-parity tests.
        if state.get("parts") is not None:
            entry["parts"] = state.get("parts")
        _apply_signed_url(
            entry,
            table_id,
            query_mode=query_mode,
            server_only=server_only,
            store=_signed_url_store,
            mirror_index=_mirror_index,
        )
        tables[table_id] = entry

    # Asset hashes
    docs_dir = data_dir / "docs"
    assets = {}
    for asset_name, asset_path in [
        ("docs", docs_dir),
        ("profiles", data_dir / "src_data" / "metadata" / "profiles.json"),
    ]:
        if asset_path.exists():
            if asset_path.is_file():
                assets[asset_name] = {"hash": _file_hash(asset_path)}
            else:
                newest = max(
                    (f.stat().st_mtime for f in asset_path.rglob("*") if f.is_file()),
                    default=0,
                )
                assets[asset_name] = {"hash": str(int(newest))}

    # v49 unified-stack manifest extensions (Section 5.1).
    # DEPRECATED v49: ``tables`` dict above is kept paralel for one release —
    # older CLIs depend on it; new clients prefer ``direct_tables`` +
    # ``data_packages[].tables``. ``data_packages``/``packaged_ids`` were
    # already resolved above (needed early for the flat-dict download-skip
    # overlay); only the remaining sections build here.
    try:
        memory_domains = _build_memory_domains_section(conn, user)
    except Exception:
        logger.exception("manifest memory_domains section build failed")
        memory_domains = []
    try:
        direct_tables = _build_direct_tables_section(
            conn,
            user,
            registry_by_name,
            states_by_table_id,
            packaged_ids,
        )
    except Exception:
        logger.exception("manifest direct_tables section build failed")
        direct_tables = []
    try:
        knowledge_artifacts = _build_knowledge_artifacts_section(user)
    except Exception:
        logger.exception("manifest knowledge_artifacts section build failed")
        knowledge_artifacts = []

    return {
        "tables": tables,
        "assets": assets,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "data_packages": data_packages,
        "memory_domains": memory_domains,
        "direct_tables": direct_tables,
        "knowledge_artifacts": knowledge_artifacts,
    }


@router.get("/manifest")
def sync_manifest(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user.

    Side-effect: stamps ``users.last_pull_at`` so the /home status frame
    can show when the analyst last pulled. This GET is the canonical
    "I am about to sync" signal — agnes pull hits it first, then
    downloads parquets whose hash changed. UI bumps (manifest browsed in
    a browser session) also count; cheap and accurate enough for a
    homepage card.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    # ``last_pull_at`` / the audit row belong to a HUMAN pull. A restricted
    # principal (co-session or agent-session sandbox) has no user row to
    # stamp — and an agent pulling on its own schedule must not masquerade
    # as its owner in the /home "last pulled" card.
    if not isinstance(user, PRINCIPAL_TYPES):
        try:
            users_repo().update(user["id"], last_pull_at=datetime.now(timezone.utc))
            # Also emit an audit_log row so /me/stats Sync activity has a
            # timeline of pulls (the column UPDATE only retains the most
            # recent one). Action `manifest.fetch` covers both `agnes pull`
            # via PAT and browser-driven manifest peeks; clients can
            # disambiguate via client_kind.
            audit_repo().log(
                user_id=user["id"],
                action="manifest.fetch",
                resource="manifest",
                result="success",
                client_kind="api",
            )
        except Exception:
            # Never block a pull because the stamp UPDATE / audit row hit a
            # transient issue (locked WAL, partial migration window). The
            # manifest itself is the load-bearing payload.
            pass
        # v49 Section 9.2 — emit a server-side ``sync.pull_started`` event so
        # /admin/telemetry can count distinct pulls per user per day. Best-effort.
        try:
            usage_repo().emit_server_event(
                event_type="sync.pull_started",
                user_id=user["id"],
                username=user.get("email") or user["id"],
                props={"client_kind": client_kind_from_user(user)},
            )
        except Exception:
            pass
    return _build_manifest_for_user(conn, user)


# ---- Pull confirm (Phase 7, Task 7.6) ----


class PullConfirmTypeReport(BaseModel):
    added: int = 0
    updated: int = 0
    removed: int = 0


class PullConfirmRequest(BaseModel):
    """Per-type aggregate the CLI submits after every pull finishes.

    Pairs with the ``sync.pull_started`` event emitted by GET /manifest
    so admin telemetry can compute pull-success rates + duration
    distributions. Optional fields fall back to zero counts — older CLI
    versions that don't track a section emit nothing for it.
    """

    duration_ms: Optional[int] = None
    direct_tables: Optional[PullConfirmTypeReport] = None
    data_packages: Optional[PullConfirmTypeReport] = None
    memory_domains: Optional[PullConfirmTypeReport] = None
    errors: int = 0


@router.post("/pull-confirm")
def pull_confirm(
    payload: PullConfirmRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Telemetry hook the CLI fires at the end of every ``agnes pull``.

    Best-effort: a telemetry insert failure must NOT bubble up to the
    CLI (the user already has their parquets, the pull succeeded). The
    response is a fixed shape ``{"recorded": True}`` so older clients
    that ignore the body keep working when the field set evolves.
    """
    props: dict = {
        "duration_ms": payload.duration_ms,
        "errors": payload.errors,
        "client_kind": client_kind_from_user(user),
    }
    for section in ("direct_tables", "data_packages", "memory_domains"):
        section_payload = getattr(payload, section)
        if section_payload is not None:
            props[f"{section}_added"] = section_payload.added
            props[f"{section}_updated"] = section_payload.updated
            props[f"{section}_removed"] = section_payload.removed

    try:
        usage_repo().emit_server_event(
            event_type="sync.pull_completed",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props=props,
        )
    except Exception:
        logger.warning("usage_events emit failed for sync.pull_completed")
    log_safe(
        user_id=user["id"],
        action="sync.pull_confirmed",
        resource="sync:pull",
        params={k: v for k, v in props.items() if k != "client_kind"},
        result="success",
    )
    return {"recorded": True}


# ---- Status ----


@router.get("/status")
def sync_status():
    """Whether a sync is currently in flight on this app process.

    Public (no auth) — used by the host-side ``agnes-auto-upgrade.sh``
    cron to decide whether to skip a `docker compose up -d` that would
    kill a running extractor / materialized pass mid-flight. Cheap to
    serve (single Lock.locked() check) and contains no sensitive data.

    Returns:
        ``{"locked": bool}`` — True if `_sync_lock` is currently held by
        a `_run_sync` invocation, OR a `data-refresh` job was triggered
        within the last ``_TRIGGER_HOLD_SEC`` seconds (so the worker loop
        hasn't yet claimed it and acquired the lock). Without the
        trigger-hold window, an auto-upgrade probe firing in the gap
        between the trigger handler's response and the worker's
        ``_sync_lock.acquire()`` would see ``locked=False`` and proceed
        with ``up -d`` — killing the just-spawning extractor.
    """
    locked = _sync_lock.locked()
    if not locked and _recent_trigger_at:
        # Monotonic deadline; clock skew / DST jumps don't matter.
        locked = (time.monotonic() - _recent_trigger_at) < _TRIGGER_HOLD_SEC
    return {"locked": locked}


# ---- Trigger ----

#: Shared idempotency key for every `data-refresh` job enqueued from this
#: process — the scheduler (services/scheduler/__main__.py) enqueues its
#: cadence-driven `data-refresh` row under the SAME key, so an operator's
#: manual trigger and the next scheduled tick dedup into one queued/running
#: job instead of racing two extractor subprocesses.
_DATA_REFRESH_IDEMPOTENCY_KEY = "sync"


@router.post("/trigger")
def trigger_sync(
    body: Optional[Any] = Body(None),
    source: Optional[str] = Query(
        None,
        description=(
            "Restrict the rebuild to one registered source_type (e.g. `keboola`, `bigquery`). Omit for a full sweep."
        ),
    ),
    user: dict = Depends(require_admin),
):
    """Trigger data sync from configured source. Admin only.

    Enqueues a ``data-refresh`` job (``src/repositories/jobs.py``, worker
    runtime wave-2B) instead of running the sync inline via
    ``BackgroundTasks`` — the worker loop (in-process by default; a
    dedicated ``worker`` role/process on split deployments) claims and
    executes it. This is the SAME queue + idempotency key
    (``"sync"``) the scheduler's cadence-driven trigger uses
    (``services/scheduler/__main__.py``), so a manual trigger and the next
    scheduled tick collapse into one in-flight job instead of racing two
    extractor subprocesses.

    Body accepts three shapes (all optional — empty body / `null` syncs
    every registered table):

      - ``["kbc_job", "orders"]`` — bare JSON array of table ids
      - ``{"tables": ["kbc_job", "orders"]}`` — object with a ``tables``
        key (matches the wire shape of the response, more discoverable
        for clients building requests by hand)
      - ``null`` / no body — sync everything

    Both array forms have shipped at different times; accepting both
    keeps older clients (PR-build CLIs, helper scripts) working while
    surfacing the shape that mirrors the response payload. Anything
    else returns HTTP 422 with a structured detail.

    ``?source=<source_type>`` scopes the rebuild to a single registered
    source (partial rebuild): only that source's local + materialized
    rows are rebuilt, and the other source's ``extract.duckdb`` is left
    untouched. Useful on dual-source deployments where a BQ refresh
    should not pay the cost of re-extracting every Keboola table.

    Returns 409 (``detail={"error": "sync_already_in_progress", "job_id":
    ...}``) if a ``data-refresh`` job is already queued or running under
    the shared ``"sync"`` idempotency key — this status code is kept
    (rather than 200 ``already_running``) because `agnes admin sync`
    (``cli/commands/admin.py``) and the admin web UI
    (``app/web/templates/admin_sync.html``) both branch on 409 today for a
    friendlier "already running" message; both now also get ``job_id`` in
    the body. On success (a new job was enqueued), the response keeps its
    existing shape plus ``job_id`` so a caller can poll
    ``GET /api/jobs/{job_id}``.

    The 200-vs-409 decision is made from ``enqueue()``'s own ``"deduped"``
    return key (see ``JobsRepository.enqueue`` docstring), not a
    pre-``enqueue()`` peek at the job queue. A separate peek-then-enqueue
    is inherently racy: two concurrent triggers can both see "no in-flight
    job" during the peek, then both call ``enqueue()`` — the loser's call
    dedups server-side (returns the winner's row), but a pre-check-based
    handler has no way to tell it apart from "I just created this job",
    so it would incorrectly report 200 "triggered" for a job it didn't
    create. Branching on the return value of the SAME ``enqueue()`` call
    that produced ``job`` is race-free: only the caller whose row was
    actually inserted (``deduped=False``) gets 200.
    """
    if body is None:
        tables: Optional[List[str]] = None
    elif isinstance(body, list):
        tables = list(body)
    elif isinstance(body, dict):
        tables = body.get("tables")
        if tables is not None and not isinstance(tables, list):
            raise HTTPException(
                status_code=422,
                detail="`tables` must be a list of strings",
            )
    else:
        raise HTTPException(
            status_code=422,
            detail=("body must be a list of table ids, an object with a `tables` list, or null"),
        )
    if tables is not None and not all(isinstance(t, str) for t in tables):
        raise HTTPException(
            status_code=422,
            detail="all entries in `tables` must be strings",
        )

    # Normalize + validate the `?source=` partial-rebuild filter. Reuse the
    # registry's canonical source-type set so an unknown value fails fast
    # with a clear 422 instead of silently rebuilding nothing.
    if source is not None:
        source = source.strip().lower()
        if not source:
            source = None
    if source is not None:
        from app.api.admin import _VALID_SOURCE_TYPES

        if source not in _VALID_SOURCE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(f"source must be one of {sorted(_VALID_SOURCE_TYPES)}, got {source!r}"),
            )

    _t0 = time.monotonic()
    resource = ((tables[0] if len(tables) == 1 else f"{len(tables)} tables") if tables else "all_tables")[:256]

    # NOTE: the old `if _sync_lock.locked(): raise HTTPException(409, ...)`
    # fast-fail that used to live here is gone. `_sync_lock` now only ever
    # gets acquired inside `_run_sync` itself (see its module-level
    # docstring), which the WORKER calls from its own job handler
    # (`app.worker.kinds._run_data_refresh`) — potentially in a different
    # process than this one on a role-split deployment. Checking THIS
    # process's `_sync_lock` here would say "not locked" even while a
    # worker elsewhere is mid-sync, so it can no longer serve as the
    # "already in progress" signal. `enqueue()`'s own `"deduped"` return
    # key is the authoritative source of that signal now — a pre-check
    # peek here would race a concurrent trigger between the peek and this
    # very `enqueue()` call (see the docstring above).
    job = jobs_repo().enqueue(
        "data-refresh",
        stamp_request_id({"tables": tables, "source": source}),
        idempotency_key=_DATA_REFRESH_IDEMPOTENCY_KEY,
    )
    already_in_progress = job["deduped"]

    if already_in_progress:
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="sync.trigger",
                resource=resource,
                params={
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "tables": tables,
                    "source": source,
                    "job_id": job["id"],
                },
                result="error.in_progress",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for sync.trigger (in_progress); continuing")
        raise HTTPException(
            status_code=409,
            detail={"error": "sync_already_in_progress", "job_id": job["id"]},
        )

    # Stamp the trigger time so `/api/sync/status` reports locked=True for
    # the next ``_TRIGGER_HOLD_SEC`` seconds. Best-effort now, not a precise
    # race-cover: in the default single-container topology the worker loop
    # runs in THIS process, so `_run_sync` (called from the job handler)
    # still acquires the module-level `_sync_lock` here once the worker
    # claims the job — but that claim can now lag the enqueue by up to a
    # worker poll interval, not the few-hundred-ms `BackgroundTasks` dispatch
    # gap this window was originally sized for.
    global _recent_trigger_at
    _recent_trigger_at = _t0
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="sync.trigger",
            resource=resource,
            params={
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "tables": tables,
                "source": source,
                "job_id": job["id"],
            },
            result="success",
            duration_ms=int((time.monotonic() - _t0) * 1000),
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for sync.trigger; continuing")
    return {
        "status": "triggered",
        "tables": tables or "all",
        "source": source or "all",
        "job_id": job["id"],
        "message": "Data sync enqueued. Check GET /api/jobs/{job_id} or /api/health for progress.",
    }


# ---- Sync Settings (dataset subscriptions) ----


class SyncSettingsUpdate(BaseModel):
    datasets: dict  # {dataset_name: bool}


@router.get("/settings")
def get_sync_settings(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's dataset sync settings."""
    repo = sync_settings_repo()
    settings = repo.get_user_settings(user["id"])
    enabled = repo.get_enabled_datasets(user["id"])
    return {
        "user_id": user["id"],
        "settings": settings,
        "enabled_datasets": enabled,
    }


@router.post("/settings")
def update_sync_settings(
    request: SyncSettingsUpdate,
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update user's dataset sync settings.

    A dataset can only be enabled when the user has access (via
    ``resource_grants(group, "table", dataset)`` or Admin membership). The
    user_sync_settings layer is per-user preference, not authorization —
    the gate stops users from enabling sync on tables they cannot read.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(403, "co_session cannot mutate user settings")
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    settings_repo = sync_settings_repo()
    results = {}
    for dataset, enabled in request.datasets.items():
        if not can_access(user["id"], ResourceType.TABLE.value, dataset, conn):
            results[dataset] = {"error": "no permission"}
            continue
        settings_repo.set_dataset_enabled(user["id"], dataset, enabled)
        results[dataset] = {"enabled": enabled}

    log_safe(
        user_id=user["id"],
        action="sync.settings_update",
        resource="sync:settings",
        params={"datasets": sorted(request.datasets.keys())},
    )
    return {"updated": results}


# ---- Table Subscriptions ----


class TableSubscriptionUpdate(BaseModel):
    table_mode: str = "all"  # "all" or "explicit"
    tables: dict = Field(default_factory=dict, max_length=500)  # {table_name: bool}


@router.get("/table-subscriptions")
def get_table_subscriptions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's per-table subscription settings."""
    repo = sync_settings_repo()
    settings = repo.get_user_settings(user["id"])
    return {"user_id": user["id"], "subscriptions": settings}


@router.post("/table-subscriptions")
def update_table_subscriptions(
    request: TableSubscriptionUpdate,
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update per-table subscription preferences.

    Mirrors the RBAC gate in POST /settings: a table can only be subscribed
    to when the user holds a resource_grants row for it (or is Admin). This
    prevents an authenticated user from subscribing to tables they cannot read.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(403, "co_session cannot mutate user settings")
    from app.auth.access import can_access
    from app.resource_types import ResourceType

    repo = sync_settings_repo()
    results = {}
    for table_name, enabled in request.tables.items():
        if not can_access(user["id"], ResourceType.TABLE.value, table_name, conn):
            results[table_name] = {"error": "no permission"}
            continue
        repo.set_dataset_enabled(user["id"], table_name, enabled)
        results[table_name] = {"enabled": enabled}
    log_safe(
        user_id=user["id"],
        action="sync.subscriptions_update",
        resource="sync:table_subscriptions",
        params={"table_mode": request.table_mode, "tables": sorted(request.tables.keys())},
    )
    return {"table_mode": request.table_mode, "updated": results}
