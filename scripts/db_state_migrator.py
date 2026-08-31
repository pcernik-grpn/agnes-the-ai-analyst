"""Migration subprocess orchestrator for DB backend state machine.

Invoked by app/api/db_state.py as a child subprocess; writes job
status to /data/state/db-jobs/<job_id>.json so the API endpoint can
poll. Steps for duckdb → side_car:

  1. validate connectivity
  2. alembic upgrade head on target
  3. backup DuckDB snapshot (recovery point BEFORE any destructive write)
  4. data copy DuckDB → target (reuses scripts/migrate_duckdb_to_pg)
  5. verify row counts
  6. flip instance.yaml::database

For side_car → cloud, source is the side-car PG; data step uses the
same migrator with a different source connection.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _bounded_engine(url: str):
    """Return a SQLAlchemy engine with conservative network + query
    timeouts. The migrator runs unattended via the host applier; an
    unreachable target (DNS, firewall, dead SQL Proxy) must NOT hang
    indefinitely. ``connect_timeout`` covers the initial handshake;
    ``statement_timeout`` (PG-side) caps any single query at 5 min,
    enough for the heaviest tables in the current schema but short
    enough to surface a runaway as a clear error.
    """
    import sqlalchemy as sa

    return sa.create_engine(
        url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=300000",  # 5 min in ms
        },
        pool_pre_ping=True,
        pool_recycle=1800,
    )


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackendFlipNotVerified(RuntimeError):
    """The data moved, but the overlay does not name the target afterwards.

    Its own type because the generic ``except Exception`` handler reverts the
    backend to the source before marking the job failed — which is right when
    the failure happened BEFORE the rows moved, and exactly wrong here. At this
    point the data is already on the target, so reverting writes a config that
    disagrees with where the rows are: the source-vs-target split the H1
    machinery exists to prevent, arriving through the guard meant to catch it.

    So this is handled without the revert. The overlay is left as-is (whatever
    it names, an operator has to look), the job records the mismatch, and
    nothing writes over the one clue about what actually happened.
    """


class JobCancelled(RuntimeError):
    """Raised when the cancel sentinel is observed at a step boundary."""

    def __init__(self, step: str):
        super().__init__(f"Job cancelled at step={step}")
        self.step = step


@dataclass
class JobWriter:
    """Writes /data/state/db-jobs/<job_id>.json on each step transition.

    Atomic write via tmp + os.replace. Schema versioned at 1.
    """

    job_id: str
    jobs_dir: Path
    source: str
    target: str
    _started_at: str = field(default_factory=lambda: _utcnow_iso())

    @property
    def _path(self) -> Path:
        return self.jobs_dir / f"{self.job_id}.json"

    @property
    def cancel_path(self) -> Path:
        """Side-car sentinel file touched by the API cancel endpoint.

        The migrator subprocess polls this at step boundaries (B2).
        We use a separate file rather than a status-flag inside the
        job JSON so the API endpoint can signal cancellation without
        racing the migrator's own writes to the same file.
        """
        return self.jobs_dir / f"{self.job_id}.cancel"

    @property
    def alive_path(self) -> Path:
        """Side-car liveness file. Refreshed via :meth:`heartbeat` on
        every step boundary. The host applier polls its mtime to
        detect stuck-running jobs (host reboot, OOM-kill, docker daemon
        crash — see B5).
        """
        return self.jobs_dir / f"{self.job_id}.alive"

    def heartbeat(self) -> None:
        """Touch the alive file. Idempotent — repeated calls just bump
        mtime. Distinct from the JSON status write so a slow migrator
        between JSON updates still advertises liveness."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.alive_path.touch()

    def check_cancel_requested(self) -> bool:
        """Return True if the cancel sentinel exists.

        Cooperative cancellation: the migrator calls this at every
        step boundary in :func:`main` and raises :class:`JobCancelled`
        when it observes the sentinel. See B2 in the v9 plan.
        """
        return self.cancel_path.exists()

    def _write(self, data: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)
        self.heartbeat()

    def _read(self) -> dict[str, Any]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def write_initial(self) -> None:
        data = {
            "schema_version": 1,
            "job_id": self.job_id,
            "status": JobStatus.RUNNING.value,
            "source_backend": self.source,
            "target_backend": self.target,
            "started_at": self._started_at,
            "completed_at": None,
            "current_step": "validate",
            "progress_pct": 0,
            "summary": None,
            "error": None,
        }
        self._write(data)

    def update_step(self, step: str, *, progress_pct: int) -> None:
        data = self._read()
        data["current_step"] = step
        data["progress_pct"] = progress_pct
        self._write(data)

    def update_table_progress(self, current_table: str, tables_done: int, tables_total: int) -> None:
        data = self._read()
        data["current_step"] = "data_copy"
        data["table_progress"] = {
            "current_table": current_table,
            "tables_done": tables_done,
            "tables_total": tables_total,
        }
        data["progress_pct"] = int(40 + (tables_done / tables_total) * 40)  # 40-80% range
        self._write(data)

    def mark_success(self, summary: dict[str, Any]) -> None:
        data = self._read()
        data["status"] = JobStatus.SUCCESS.value
        data["completed_at"] = _utcnow_iso()
        data["progress_pct"] = 100
        data["summary"] = summary
        self._write(data)

    def mark_failed(self, *, step: str, error_class: str, error_message: str) -> None:
        data = self._read()
        data["status"] = JobStatus.FAILED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {
            "step": step,
            "class": error_class,
            "message": error_message,
        }
        self._write(data)

    def mark_cancelled(self, *, step: str) -> None:
        data = self._read()
        data["status"] = JobStatus.CANCELLED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {"step": step, "class": "Cancelled", "message": "Admin cancelled migration"}
        self._write(data)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_cancel_before_flip(job_path: Path, target_state: "BackendState") -> None:
    """Final cancel-sentinel re-check right before ``flip_backend``.

    H1-NEW: B2's sentinel cancellation polls at step boundaries
    (alembic, copy, verify). A cancel arriving in the window between
    the last poll and the flip was accepted by the API (writes the
    cancel sentinel + reverts ``instance.yaml`` to source) while the
    migrator continued to ``write_backend_state(target_state, ...)``.
    End state: instance.yaml said SOURCE but data was on TARGET.
    Re-check here so cancel ↔ flip is mutually exclusive.

    Raises ``JobCancelled`` (a ``RuntimeError`` subclass, declared at
    module top) if the cancel sentinel file exists or the job file has
    already transitioned to a terminal cancelled state. Raising
    ``JobCancelled`` (not bare ``RuntimeError``) is critical: the outer
    ``run_migration`` handler has ``except JobCancelled → mark_cancelled``
    and ``except Exception → mark_failed`` clauses. A bare ``RuntimeError``
    would fall through to ``mark_failed``, leaving the operator-visible
    status as ``failed`` instead of ``cancelled`` and confusing the host
    applier (which uses status to distinguish a cancel from a real failure).

    Returns normally if no sentinel is present, allowing the flip to
    proceed.
    """
    sentinel = job_path.with_suffix(".cancel")
    if sentinel.exists():
        raise JobCancelled(step="flip_backend")
    # Belt-and-suspenders: also check the job JSON itself in case the
    # sentinel file was removed but the JSON was already updated.
    if job_path.exists():
        try:
            data = json.loads(job_path.read_text())
        except Exception:
            pass
        else:
            if data.get("status") in ("cancelled", "cancel_requested"):
                raise JobCancelled(step="flip_backend")


#: Bound for the ``alembic upgrade head`` subprocess. Schema migrations
#: are bound to PG-side ``statement_timeout`` already (set by
#: ``_bounded_engine``), but the alembic process itself — script
#: discovery, file load, version-graph computation — also needs a
#: watchdog so a hung interpreter doesn't pin the migrator forever (H5).
ALEMBIC_UPGRADE_TIMEOUT_SEC = 300

#: Bound for ``gzip``/``pg_dump`` subprocess. Multi-GB DBs need
#: generous headroom; 30 min matches the migrator-overall watchdog.
BACKUP_TIMEOUT_SEC = 1800


def _format_alembic_timeout_message(target_url: str, timeout_sec: int) -> str:
    """Format the timeout error with the URL password masked.

    H3-NEW: pre-fix, the formatter embedded the bare ``target_url`` with
    its password via ``!r``. The migrator's outer handler then
    captured the message into ``job.error.message``. Mask here so a
    third party reading the job JSON never sees plaintext creds.
    """
    try:
        from sqlalchemy.engine import make_url

        safe = make_url(target_url).render_as_string(hide_password=True)
    except Exception:
        safe = "<unparseable-url>"
    return (
        f"alembic upgrade head timed out after {timeout_sec}s "
        f"(target={safe!r}). The migration target may be unreachable, "
        f"network-partitioned, or running out of disk."
    )


def alembic_upgrade_head(target_url: str) -> None:
    """Run ``alembic upgrade head`` against ``target_url``.

    Idempotent — alembic itself is a no-op when already at head.
    Raises ``RuntimeError`` on non-zero exit or on subprocess timeout.

    A wall-clock timeout (``ALEMBIC_UPGRADE_TIMEOUT_SEC``) guards
    against a hung alembic interpreter (DNS / TLS handshake / lock
    contention) — see H5.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": target_url}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(repo_root / "alembic.ini"),
                "upgrade",
                "head",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(repo_root),
            check=False,
            timeout=ALEMBIC_UPGRADE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(_format_alembic_timeout_message(target_url, ALEMBIC_UPGRADE_TIMEOUT_SEC)) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


#: Compiled regex for sensitive audit-log KEY names (case-insensitive).
#: Applied to JSON object keys only — never to values — by
#: :func:`_redact_sensitive_keys`. Word-boundary anchors prevent
#: innocent substrings like ``"keynote"`` or ``"secretary"`` from
#: matching.
_SENSITIVE_KEY_RE = re.compile(
    r"\b(password|passwd|token|secret|api[-_ ]?key|bearer|"
    r"client[-_ ]?secret|access[-_ ]?token|refresh[-_ ]?token|"
    r"private[-_ ]?key|signing[-_ ]?key)\b",
    re.IGNORECASE,
)

#: Sentinel value substituted for a redacted key's original value.
#: Recognisable post-migration so operators can tell the row was
#: redacted at migration time (vs scrubbed at write time by the
#: runtime sanitiser).
_REDACTED_SENTINEL = "<redacted-at-migration>"


def _redact_sensitive_keys(obj: Any) -> tuple[Any, bool]:
    """Walk ``obj``, replacing values under sensitive KEYS with
    ``_REDACTED_SENTINEL``. Returns ``(obj, changed_bool)``.

    LOW-1: pre-fix, the regex ran against ``str(obj)`` — values like
    ``"/reset-password"`` triggered wholesale row rewrite. Now only
    keys matched by :data:`_SENSITIVE_KEY_RE` have their values
    replaced; non-sensitive siblings and value-only matches are
    preserved.
    """
    if isinstance(obj, dict):
        changed = False
        for k, v in list(obj.items()):
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                if obj[k] not in (None, "", _REDACTED_SENTINEL):
                    obj[k] = _REDACTED_SENTINEL
                    changed = True
            else:
                _, sub_changed = _redact_sensitive_keys(v)
                changed = changed or sub_changed
        return obj, changed
    if isinstance(obj, list):
        changed = False
        for item in obj:
            _, sub_changed = _redact_sensitive_keys(item)
            changed = changed or sub_changed
        return obj, changed
    return obj, False


def scrub_audit_log_pii(duckdb_path: Path) -> dict[str, int]:
    """Pre-copy scrub of ``audit_log`` rows that carry PII in
    ``params`` / ``params_before``.

    Runs at the start of :func:`copy_duckdb_to_pg`. Opens DuckDB
    read-write (briefly) and rewrites offending rows IN THE SOURCE
    so:

    * The DuckDB backup taken right after (or right before) the
      copy step also has the redacted form — the backup IS the
      recovery artifact, leaving PII in it would defeat the
      purpose.
    * The PG target receives the redacted form via the normal
      copy loop with no extra transform pass.

    LOW-1 fix: walks JSON KEYS only via :func:`_redact_sensitive_keys`.
    Rows whose params is NULL, not valid JSON, or whose JSON contains
    no sensitive key are left unchanged. Non-sensitive sibling keys
    survive; only the value under the matching key is replaced with
    :data:`_REDACTED_SENTINEL`.

    Idempotent — re-running finds zero matches because previously
    scrubbed rows already carry the sentinel value. Schema-tolerant —
    silently no-ops on DBs that don't have an ``audit_log`` table
    (e.g. fresh installs migrated immediately after first boot).

    Returns ``{"rows_scanned", "rows_redacted"}`` for the JobWriter
    summary.
    """
    import duckdb  # needed to name duckdb.CatalogException in the except below

    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(duckdb_path))
    rows_redacted = 0
    rows_scanned = 0
    try:
        # Tolerant of fresh installs without an audit_log table.
        try:
            count_row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        except duckdb.CatalogException:
            return {"rows_scanned": 0, "rows_redacted": 0}
        rows_scanned = int(count_row[0] or 0) if count_row else 0
        if rows_scanned == 0:
            return {"rows_scanned": 0, "rows_redacted": 0}

        # Pull only id + the two JSON cols; rewrite the matching rows
        # in a single UPDATE-per-row pass. The DBs we care about have
        # O(10^5) audit rows; a per-row UPDATE is acceptable.
        rows = conn.execute("SELECT id, params, params_before FROM audit_log").fetchall()
        for rid, params, params_before in rows:
            new_params = params
            new_params_before = params_before
            changed_any = False

            for src_val, col_name in (
                (params, "params"),
                (params_before, "params_before"),
            ):
                if src_val is None:
                    continue
                try:
                    parsed = json.loads(src_val)
                except (ValueError, TypeError):
                    # Not JSON — leave as-is.
                    continue
                _, changed = _redact_sensitive_keys(parsed)
                if changed:
                    if col_name == "params":
                        new_params = json.dumps(parsed)
                    else:
                        new_params_before = json.dumps(parsed)
                    changed_any = True

            if not changed_any:
                continue

            # Build the UPDATE SET dynamically — only overwrite the
            # column(s) we actually changed.
            set_parts: list[str] = []
            bind: list[object] = []
            if new_params != params:
                set_parts.append("params = ?")
                bind.append(new_params)
            if new_params_before != params_before:
                set_parts.append("params_before = ?")
                bind.append(new_params_before)
            bind.append(rid)
            conn.execute(
                f"UPDATE audit_log SET {', '.join(set_parts)} WHERE id = ?",
                bind,
            )
            rows_redacted += 1
    finally:
        conn.close()
    return {"rows_scanned": rows_scanned, "rows_redacted": rows_redacted}


def copy_duckdb_to_pg(
    duckdb_path: Path,
    target_url: str,
    writer: "JobWriter | None" = None,
) -> dict[str, int]:
    """Copy all PG-mapped tables from DuckDB to target PG.

    Wraps :func:`scripts.migrate_duckdb_to_pg.run_all` — the same
    idempotent copy loop that the docker-compose data-migrate one-shot
    uses. Returns ``{rows_total, tables_migrated}`` where ``rows_total``
    is the sum of PG row counts across all migrated tables (per the
    validator report — ``ON CONFLICT DO NOTHING`` makes per-task
    rows-inserted untrustworthy, so we use post-copy PG counts).

    Before opening the source read-only, runs
    :func:`scrub_audit_log_pii` to redact any audit rows captured
    before the runtime sanitiser existed (H7), so the migrated PG
    never receives the raw credential. The scrub is idempotent, so
    :func:`main` runs it again against the source *before* the
    pre-flip backup on the side_car path — that is what keeps the
    ``.duckdb.gz`` recovery artifact free of the redacted secrets;
    this in-copy scrub only guarantees the copy itself is clean.

    Optional ``writer`` (C.1): when supplied, per-table progress
    flows into ``writer.update_table_progress`` so the admin UI's
    progress bar advances during the long data_copy step instead of
    freezing at 40%.
    """

    from scripts.migrate_duckdb_to_pg import reset_target_state_tables, run_all

    scrub_summary = scrub_audit_log_pii(duckdb_path)

    progress_cb = None
    if writer is not None:

        def progress_cb(table: str, done: int, total: int) -> None:
            try:
                writer.update_table_progress(table, done, total)
            except Exception:
                # Best-effort — never let progress-write failures
                # break the migration. The next step boundary write
                # via update_step will refresh the state anyway.
                pass

    from src.duckdb_conn import _open_duckdb

    duck_conn = _open_duckdb(str(duckdb_path), read_only=True)
    try:
        pg_engine = _bounded_engine(target_url)
        try:
            # B1 — start from an empty target so the ON CONFLICT DO NOTHING
            # copy can't keep stale rows from a prior failed attempt. This is
            # always a one-time cutover on the applier path, so reset
            # unconditionally (shared helper with the --reset-target CLI path).
            reset_discarded = reset_target_state_tables(pg_engine)
            reports = run_all(
                duck_conn,
                pg_engine,
                validate=True,
                progress_callback=progress_cb,
            )
        finally:
            pg_engine.dispose()
    finally:
        duck_conn.close()

    # Halt-on-failure (H6) introduced ``skipped: True`` entries — exclude
    # them from both the success and failure buckets. main() inspects
    # ``tables_failed`` to refuse the flip; skipped entries are
    # informational and surface separately as ``tables_skipped``.
    ok = [r for r in reports if "error" not in r and not r.get("skipped")]
    err = [r for r in reports if "error" in r]
    skipped = [r for r in reports if r.get("skipped")]
    return {
        "rows_total": sum(r.get("pg_rows", 0) for r in ok),
        "tables_migrated": len(ok),
        "target_rows_reset": reset_discarded,
        "tables_failed": [{"table": r["table"], "error": str(r["error"])} for r in err],
        "tables_skipped": [{"table": r["table"], "reason": r.get("reason", "")} for r in skipped],
        "audit_pii_scrub": scrub_summary,
    }


def _jsonb_columns_for(table_name: str) -> set[str]:
    """Return PG JSONB column names on ``table_name`` from Base.metadata.

    Used by :func:`copy_pg_to_pg` because PG→PG copy reads JSONB columns
    decoded (e.g. a stored JSON string ``"./foo"`` returns Python str
    ``./foo``); the target's ``CAST AS JSONB`` then fails on bare values.
    We re-encode every JSONB value with ``json.dumps`` on the copy path.
    """
    from sqlalchemy.dialects.postgresql import JSONB

    import src.models  # noqa: F401
    from src.db_pg import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return set()
    return {c.name for c in table.columns if isinstance(c.type, JSONB)}


def _json_dumps_for_jsonb(value):
    """JSON-encode ``value`` for a CAST AS JSONB bind.

    ``None`` and already-string-encoded JSON values pass through after
    encode — ``json.dumps`` always emits valid JSON, so the cast on the
    PG side is now guaranteed parseable. The DuckDB path's
    ``_normalize_for_pg`` does the same dict/list encoding but leaves
    strings alone (DuckDB stores JSON as already-encoded strings); the
    PG source returns decoded values, so we re-encode here.
    """
    import json as _json

    if value is None:
        return None
    return _json.dumps(value)


#: Stream batch size for PG→PG copy. Tuned to balance round-trip cost
#: (one INSERT per batch) against per-batch RAM (one batch of rows held
#: at a time, not the entire table). Matches the DuckDB-path chunk size.
PG_TO_PG_BATCH_SIZE = 500


def copy_pg_to_pg(
    source_url: str,
    target_url: str,
    writer: "JobWriter | None" = None,
) -> dict[str, int]:
    """Copy all PG-mapped tables from one PG to another in FK order.

    Used for ``side_car → cloud`` migration. Mirrors
    :func:`copy_duckdb_to_pg` but with a PG source instead of DuckDB.
    Reuses the same JSON / ARRAY / NOT-NULL-default coercion the DuckDB
    path uses — the column-introspection helpers in
    ``scripts.migrate_duckdb_to_pg.tasks`` work for any source.

    Streams the source via ``execution_options(yield_per=...)`` and
    flushes each chunk into its own ``target.begin()`` block. This
    keeps RAM bounded to one ``PG_TO_PG_BATCH_SIZE``-row chunk per
    table (rather than materialising entire million-row tables like
    ``audit_log`` / ``usage_events`` into the migrator container's
    heap, which previously risked OOM on production data — see H4).

    A mid-stream failure now only rolls back the in-flight batch; the
    next retry resumes via ``ON CONFLICT DO NOTHING``.

    Optional ``writer`` (C.1): when supplied, per-table progress
    flows into ``writer.update_table_progress`` so the admin UI gets
    movement during the data_copy step.

    Returns ``{rows_total, tables_migrated}`` based on post-copy target
    row counts (ON CONFLICT DO NOTHING makes per-task inserted-count
    untrustworthy).
    """
    import sqlalchemy as sa

    import src.models  # noqa: F401 — ensures every model is imported
    from scripts.migrate_duckdb_to_pg import _PK_COLUMNS
    from scripts.migrate_duckdb_to_pg.tasks import (
        _array_columns_for,
        _build_insert,
        _coerce_array_value,
        _normalize_for_pg,
        _not_null_columns_with_default,
        _substitute_default,
    )
    from src.db_pg import Base
    from src.sql_ident import quote_ident

    def _row_to_dict(row, cols, array_cols, jsonb_cols, default_cols):
        d: dict[str, Any] = {}
        for k, v in zip(cols, row):
            if k in array_cols:
                d[k] = _coerce_array_value(v)
            elif k in jsonb_cols:
                # Source is PG JSONB → SQLAlchemy returns dict / list /
                # str / int / bool depending on the stored value. CAST
                # AS JSONB expects a JSON-encoded string for ALL of
                # those — including bare strings (CAST('hello' AS JSONB)
                # fails; CAST('"hello"' AS JSONB) succeeds). json.dumps
                # any non-None value.
                d[k] = _json_dumps_for_jsonb(v)
            else:
                d[k] = _normalize_for_pg(v)
            if k in default_cols:
                d[k] = _substitute_default(d[k], default_cols[k])
        return d

    def _flush_batch(target_engine, insert_sql: str, batch: list[dict]) -> None:
        """Insert one batch in its own short transaction.

        Per-batch transactions cap how much an interrupt rolls back and
        keep the PG WAL footprint bounded — a single multi-million-row
        transaction would otherwise pin WAL until commit.
        """
        if not batch:
            return
        with target_engine.begin() as tgt_conn:
            tgt_conn.execute(sa.text(insert_sql), batch)

    source = _bounded_engine(source_url)
    target = _bounded_engine(target_url)
    rows_total = 0
    tables_migrated = 0
    all_tables = list(Base.metadata.sorted_tables)
    total_tables = len(all_tables)
    try:
        for i, table in enumerate(all_tables):
            tname = table.name
            if writer is not None:
                try:
                    writer.update_table_progress(tname, i, total_tables)
                except Exception:
                    pass
            cols = [c.name for c in table.columns]
            pk_cols = _PK_COLUMNS.get(tname, ["id"])
            array_cols = _array_columns_for(tname)
            default_cols = _not_null_columns_with_default(tname)
            jsonb_cols = _jsonb_columns_for(tname)
            insert_sql = _build_insert(tname, cols, pk_cols)

            # Stream the source. ``yield_per`` makes SQLAlchemy hold
            # only one batch of rows in memory at a time — required for
            # production tables that don't fit in the migrator
            # container's RAM. We iterate the Result directly; calling
            # ``.all()`` would defeat the streaming and is what H4
            # flagged.
            with source.connect() as src_conn:
                stmt = sa.text(f"SELECT {', '.join(cols)} FROM " + quote_ident(tname)).execution_options(
                    yield_per=PG_TO_PG_BATCH_SIZE
                )
                result = src_conn.execute(stmt)
                batch: list[dict] = []
                for r in result:
                    batch.append(_row_to_dict(r, cols, array_cols, jsonb_cols, default_cols))
                    if len(batch) >= PG_TO_PG_BATCH_SIZE:
                        _flush_batch(target, insert_sql, batch)
                        batch = []
                _flush_batch(target, insert_sql, batch)

            with target.connect() as tgt_conn:
                count = tgt_conn.execute(sa.text(f"SELECT COUNT(*) FROM {quote_ident(tname)}")).scalar()
            rows_total += int(count or 0)
            tables_migrated += 1
        # Final progress tick — UI sees done==total at the end of the
        # data_copy step, even though no further table iteration will
        # update the writer.
        if writer is not None and total_tables:
            try:
                writer.update_table_progress(all_tables[-1].name, total_tables, total_tables)
            except Exception:
                pass
    finally:
        source.dispose()
        target.dispose()

    # tables_failed is always empty here: copy_pg_to_pg raises on the
    # first per-table error rather than collecting them (unlike the
    # DuckDB path which uses run_all). Included for API shape parity with
    # copy_duckdb_to_pg so main() can apply the same guard uniformly.
    return {"rows_total": rows_total, "tables_migrated": tables_migrated, "tables_failed": []}


#: Sample size for the content-drift hash. Bounded so a multi-million-row
#: table doesn't add minutes to verify; large enough that randomly-drifted
#: rows in the first ``N`` PK-ordered rows are caught with high
#: probability. The drift modes the hash targets (pre-seeded Cloud SQL
#: with matching PKs but stale email/name from a prior failed
#: migration) are deterministic, so any non-zero sample suffices.
CONTENT_HASH_SAMPLE_SIZE = 1000


def _content_hash_sample(
    engine,
    table_name: str,
    pk_cols: list[str],
    non_pk_cols: list[str],
    sample_size: int = CONTENT_HASH_SAMPLE_SIZE,
) -> str:
    """Hash the first ``sample_size`` rows ordered by PK, considering
    only non-PK columns.

    Used by :func:`verify_pg_row_counts` to detect H12-style drift —
    source and target carry the same PK set but differ on non-PK
    content (e.g. preseeded Cloud SQL left over from a failed prior
    migration). Two databases with the same PK set + same non-PK
    content yield the same hash; any disagreement on a non-PK column
    surfaces as a different digest.

    Cheap-sample design keeps the verify step bounded — full equality
    on PKs is already covered by the per-task checksum in run_all, so
    we only need a representative non-PK sample here.
    """
    if not non_pk_cols:
        # No non-PK columns means there's nothing to drift; the
        # existing PK-count check covers everything. Constant sentinel
        # so the diff loop has a comparable value on both sides.
        return "no-non-pk-content"

    import hashlib

    import sqlalchemy as sa

    from src.sql_ident import quote_ident

    pk_order = ", ".join(quote_ident(c) for c in pk_cols)
    sel_cols = ", ".join(quote_ident(c) for c in non_pk_cols)
    sql = sa.text(f"SELECT {sel_cols} FROM {quote_ident(table_name)} ORDER BY {pk_order} LIMIT {int(sample_size)}")
    h = hashlib.sha256()
    with engine.connect() as conn:
        for row in conn.execute(sql):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def verify_pg_row_counts(source_url: str, target_url: str) -> list[dict]:
    """Compare PG row counts AND non-PK content samples between source
    and target.

    Mirrors :func:`verify_row_counts` but reads both sides as PG.
    Returns list of diffs — each entry carries a ``kind`` discriminator:

    - ``{"table", "kind": "row_count", "source_rows", "target_rows"}``
      when row counts disagree.
    - ``{"table", "kind": "content_drift", "source_hash", "target_hash"}``
      when row counts match but non-PK content differs (H12).

    Empty list = both checks pass, migration may proceed to flip.
    """
    import sqlalchemy as sa

    from scripts.migrate_duckdb_to_pg import _PK_COLUMNS
    from src.db_pg import Base
    from src.sql_ident import quote_ident

    diffs: list[dict] = []
    source = _bounded_engine(source_url)
    target = _bounded_engine(target_url)
    try:
        for table in Base.metadata.sorted_tables:
            tname = table.name
            try:
                with source.connect() as c:
                    src_count = c.execute(sa.text(f"SELECT COUNT(*) FROM {quote_ident(tname)}")).scalar()
            except sa.exc.ProgrammingError as exc:
                # Was: silent src_count=0. Hard-fail so the operator can act —
                # a missing source table means the schema is broken and migration
                # cannot be validated safely.
                raise RuntimeError(
                    f"verify_pg_row_counts: source table '{tname}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            try:
                with target.connect() as c:
                    tgt_count = c.execute(sa.text(f"SELECT COUNT(*) FROM {quote_ident(tname)}")).scalar()
            except sa.exc.ProgrammingError as exc:
                # Was: silent tgt_count=0. That collapsed to a 0==0 "match"
                # whenever source also had 0 rows, hiding typos and partial-
                # alembic states. Hard-fail with a specific message so the
                # operator can act.
                raise RuntimeError(
                    f"verify_pg_row_counts: target table '{tname}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            if src_count != tgt_count:
                diffs.append(
                    {
                        "table": tname,
                        "kind": "row_count",
                        "source_rows": int(src_count or 0),
                        "target_rows": int(tgt_count or 0),
                    }
                )
                # When counts disagree, content drift is implied; skip
                # the hash sample to keep the verify step bounded.
                continue

            # Counts match — verify non-PK content via sampled hash.
            pk_cols = _PK_COLUMNS.get(tname, ["id"])
            non_pk_cols = [c.name for c in table.columns if c.name not in pk_cols]
            src_hash = _content_hash_sample(source, tname, pk_cols, non_pk_cols)
            tgt_hash = _content_hash_sample(target, tname, pk_cols, non_pk_cols)
            if src_hash != tgt_hash:
                diffs.append(
                    {
                        "table": tname,
                        "kind": "content_drift",
                        "source_hash": src_hash[:16],
                        "target_hash": tgt_hash[:16],
                    }
                )
    finally:
        source.dispose()
        target.dispose()
    return diffs


def verify_row_counts(duckdb_path: Path, target_url: str) -> list[dict]:
    """Compare row counts per Base.metadata table between DuckDB and PG.

    Returns list of diffs ``[{table, source_rows, target_rows}]``.
    Empty list = all tables match. Tables present in only one side
    are also reported (the other side's count = 0).
    """
    import duckdb as _duckdb
    import sqlalchemy as sa

    from src.db_pg import Base
    from src.sql_ident import quote_ident

    diffs: list[dict] = []
    tables = [t.name for t in Base.metadata.sorted_tables]

    # E.1 — open the DuckDB file read-only. Verify is purely a SELECT
    # workload; a writable connection creates a .wal sidecar that adds
    # clutter and can confuse subsequent reads if the migrator crashes
    # between verify and flip.
    from src.duckdb_conn import _open_duckdb

    duck_conn = _open_duckdb(str(duckdb_path), read_only=True)
    pg_engine = _bounded_engine(target_url)
    try:
        for table in tables:
            try:
                src_count = duck_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
            except _duckdb.CatalogException:
                src_count = 0
            try:
                with pg_engine.connect() as pg_conn:
                    tgt_count = pg_conn.execute(sa.text(f"SELECT COUNT(*) FROM {quote_ident(table)}")).fetchone()[0]
            except sa.exc.ProgrammingError as exc:
                # Was: silent tgt_count=0. That collapsed to a 0==0 "match"
                # whenever source also had 0 rows, hiding typos and partial-
                # alembic states. Hard-fail with a specific message so the
                # operator can act.
                raise RuntimeError(
                    f"verify_row_counts: target table '{table}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            if src_count != tgt_count:
                diffs.append(
                    {
                        "table": table,
                        "source_rows": src_count,
                        "target_rows": tgt_count,
                    }
                )
    finally:
        duck_conn.close()
        pg_engine.dispose()
    return diffs


def checkpoint_duckdb(duckdb_path: Path) -> bool:
    """Fold the DuckDB WAL into the main file before backup + copy (B2).

    DuckDB writes commits to a ``.wal`` sidecar and only folds it into the
    main ``.duckdb`` file on a clean CHECKPOINT (normally at app shutdown).
    If the app was hard-killed — OOM, or a shutdown that overran its
    ``docker stop`` grace — the last committed changes live only in
    ``system.duckdb.wal``. ``backup_duckdb`` gzips the ``.duckdb`` file
    only, so the recovery artifact would silently lack those WAL-tail
    commits. CHECKPOINT here folds the WAL into the file so both the backup
    and the copy see a complete, self-contained database regardless of how
    the app stopped.

    The app container is already stopped (the applier stops it before
    launching the migrator), so the exclusive DuckDB file lock is free.

    Best-effort: returns ``True`` on success, ``False`` (with a WARNING) on
    failure. The copy itself is unaffected either way — it opens the file
    read-only, which replays the WAL into the read — so a checkpoint failure
    only risks a slightly-incomplete *backup*, not a lossy migration.
    """
    if not Path(duckdb_path).exists():
        return False
    from src.duckdb_conn import _open_duckdb

    try:
        conn = _open_duckdb(str(duckdb_path))  # writable — needed to CHECKPOINT
        try:
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning(
            "DuckDB CHECKPOINT before backup/copy failed (%s); the copy still "
            "captures WAL data via its read-only open, but the backup artifact "
            "may omit un-checkpointed WAL-tail commits.",
            e,
        )
        return False


def backup_duckdb(duckdb_path: Path, backups_dir: Path) -> Path:
    """gzip the DuckDB file to backups dir with timestamp.

    Returns path to backup file. Used before duckdb → side_car cutover
    so the operator has a recovery point if the side-car PG ever
    diverges and needs to be re-built from the frozen DuckDB.

    Implemented as ``subprocess.run(['gzip', ...], timeout=...)`` so a
    wedged compressor surfaces as a typed RuntimeError rather than
    pinning the migrator (H5). gzip(1) is universally available on
    customer-instance VMs and the migrator image (alpine + coreutils).
    """
    import subprocess

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"duckdb-pre-sidecar-{ts}.duckdb.gz"
    # ``gzip -c <in>`` writes to stdout; we capture into the output
    # file. ``-6`` matches the previous in-process compresslevel=6
    # default so the resulting artifact is byte-comparable in size.
    try:
        with open(out, "wb") as fp:
            result = subprocess.run(
                ["gzip", "-c", "-6", str(duckdb_path)],
                stdout=fp,
                stderr=subprocess.PIPE,
                check=False,
                timeout=BACKUP_TIMEOUT_SEC,
            )
    except subprocess.TimeoutExpired as exc:
        # Remove the half-written output so the operator's next retry
        # doesn't pick up an invalid artifact.
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"DuckDB backup timed out after {BACKUP_TIMEOUT_SEC}s "
            f"(source={duckdb_path}). File may be larger than expected "
            "or gzip is wedged; retry or investigate the host."
        ) from exc
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"DuckDB backup gzip failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
        )
    return out


def backup_sidecar_pg(container_name: str, backups_dir: Path) -> Path:
    """pg_dump custom format of side-car PG, via docker exec.

    Returns path to .dump file. Used before side_car → cloud cutover.

    Wrapped in a wall-clock timeout (``BACKUP_TIMEOUT_SEC``) — a
    hung pg_dump (locked tables, network partition between docker
    daemon and the container) would otherwise pin the migrator
    indefinitely (H5).
    """
    import subprocess

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"sidecar-pre-cloud-{ts}.dump"
    try:
        with open(out, "wb") as fp:
            result = subprocess.run(
                ["docker", "exec", container_name, "pg_dump", "-U", "agnes", "-F", "c", "agnes"],
                stdout=fp,
                stderr=subprocess.PIPE,
                check=False,
                timeout=BACKUP_TIMEOUT_SEC,
            )
    except subprocess.TimeoutExpired as exc:
        # Half-written .dump files are unusable; remove so a retry
        # starts clean.
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump timed out after {BACKUP_TIMEOUT_SEC}s "
            f"(container={container_name}). The side-car may be holding "
            "locks or the docker daemon is unresponsive."
        ) from exc
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
    return out


def main(
    *,
    job_id: str,
    to: str,
    target_url: str,
    duckdb_path: Path,
    jobs_dir: Path,
    backups_dir: Path,
    source_url: str | None = None,
    source_backend: str | None = None,
) -> int:
    """Run the migration job. Returns process exit code.

    Dispatches by the (source_backend, target_backend) pair. Source
    defaults to ``duckdb`` for ``to=side_car`` and ``side_car`` for
    ``to=cloud`` when not specified explicitly — those are the original
    forward-only paths. The applier passes ``--source-backend`` for
    every transition so the cloud → side_car rollback (which has the
    same target=side_car as the duckdb cutover) is dispatched
    correctly.

    Source ↔ Target dispatch:
      ============  ============  ===========================
      Source        Target        Steps
      ============  ============  ===========================
      duckdb        side_car      alembic + backup duckdb + duckdb→pg + verify + flip
      duckdb        cloud         alembic + duckdb→pg + verify + flip
      side_car      cloud         alembic + backup sidecar + pg→pg + verify + flip
      cloud         side_car      alembic + pg→pg + verify + flip
      ============  ============  ===========================

    DuckDB is never a target — once an instance is on Postgres, the
    DuckDB file is treated as immutable (the backup taken before the
    cutover is the recovery artifact, not a writable target).
    """
    from src.db_state_machine import BackendState, write_backend_state

    # Derive source from the legacy "to" if not passed (back-compat for
    # the original duckdb→side_car and side_car→cloud paths).
    if source_backend is None:
        source_backend = "duckdb" if to == "side_car" else "side_car"

    writer = JobWriter(
        job_id=job_id,
        jobs_dir=jobs_dir,
        source=source_backend,
        target=to,
    )
    writer.write_initial()
    # Boundary check 0 (pre-alembic): catches cancels that arrived
    # before the subprocess even got off the ground.
    if writer.check_cancel_requested():
        writer.mark_cancelled(step="validate")
        return 0

    # Argument validation BEFORE we touch any network — every PG→PG
    # transition requires source_url. Failing here keeps the failure
    # stack short and the error message actionable rather than burying
    # it under a connect-to-bogus-target traceback.
    pg_source = source_backend in ("side_car", "cloud")
    if pg_source and not source_url:
        writer.mark_failed(
            step="validate",
            error_class="ValueError",
            error_message="--source-url is required when source is a PG backend",
        )
        return 1

    try:
        writer.update_step("alembic", progress_pct=20)
        if writer.check_cancel_requested():
            raise JobCancelled(step="alembic")
        alembic_upgrade_head(target_url)

        if source_backend == "duckdb":
            # duckdb → side_car  OR  duckdb → cloud — both copy from the
            # DuckDB file to whichever PG target the operator picked.

            if to == "side_car":
                # H7 — scrub stale audit PII in the SOURCE *before* the
                # pre-flip backup taken below, so the .duckdb.gz recovery
                # artifact can't retain tokens/passwords captured before the
                # runtime sanitiser existed. The scrub must precede both the
                # checkpoint (so its rewrites get folded into the file) and
                # the backup (so the artifact is clean). copy_duckdb_to_pg
                # re-runs the same idempotent scrub for the copy path, so
                # the copy itself is unchanged. (The direct duckdb → cloud
                # path takes no backup, so its scrub stays inside the copy.)
                scrub_audit_log_pii(duckdb_path)

            # B2 — fold the WAL into the main file before we back it up or
            # read it, so a hard-killed app's last commits (and the scrub's
            # own rewrites, above) are captured in both the backup artifact
            # and the copy. Best-effort: the copy opens read-only (replays
            # the WAL) regardless.
            checkpoint_duckdb(duckdb_path)

            if to == "side_car":
                # Backup the DuckDB file BEFORE any destructive operation on
                # the target. If anything in the rest of the pipeline crashes
                # the operator still has the source snapshot they need to
                # retry. The backup runs here — not after verify — so that a
                # crash anywhere between copy and flip leaves the operator
                # with a valid recovery point.
                writer.update_step("backup", progress_pct=15)
                if writer.check_cancel_requested():
                    raise JobCancelled(step="backup")
                backup_duckdb(duckdb_path, backups_dir)

            writer.update_step("data_copy", progress_pct=40)
            if writer.check_cancel_requested():
                raise JobCancelled(step="data_copy")
            copy_summary = copy_duckdb_to_pg(duckdb_path, target_url, writer=writer)

            if copy_summary.get("tables_failed"):
                writer.mark_failed(
                    step="data_copy",
                    error_class="CopyTableError",
                    error_message=(
                        "Per-table copy failed: "
                        + ", ".join(f"{t['table']}={t['error']!r}" for t in copy_summary["tables_failed"])
                    ),
                )
                return 1

            writer.update_step("verify", progress_pct=80)
            if writer.check_cancel_requested():
                raise JobCancelled(step="verify")
            diffs = verify_row_counts(duckdb_path, target_url)
        else:
            # side_car → cloud  OR  cloud → side_car — PG→PG.
            if source_backend == "side_car":
                writer.update_step("backup", progress_pct=30)
                if writer.check_cancel_requested():
                    raise JobCancelled(step="backup")
                # B.4 — side_car → cloud: backup is a HARD requirement.
                # Pre-fix the failure was swallowed as a warning and the
                # operator discovered the missing recovery point only at
                # restore time. Now a failed backup short-circuits the
                # migration with mark_failed("BackupError"); operator
                # fixes the path (e.g. ensures the side-car container is
                # still up so pg_dump can reach it) and retries.
                try:
                    backup_sidecar_pg("agnes-postgres-1", backups_dir)
                except Exception as e:
                    writer.mark_failed(
                        step="backup",
                        error_class="BackupError",
                        error_message=(
                            f"side-car backup failed: {e!s}. "
                            "Side-car → cloud migration requires a successful "
                            "pre-cutover dump; investigate (pg_dump available? "
                            "container running? disk space?) and retry."
                        ),
                    )
                    # Revert state to source like the generic except path.
                    try:
                        from src.db_state_machine import BackendState as _BS
                        from src.db_state_machine import write_backend_state as _wbs

                        _wbs(_BS(source_backend), url=source_url)
                    except Exception:
                        pass
                    return 1

            writer.update_step("data_copy", progress_pct=40)
            if writer.check_cancel_requested():
                raise JobCancelled(step="data_copy")
            copy_summary = copy_pg_to_pg(source_url, target_url, writer=writer)

            if copy_summary.get("tables_failed"):
                writer.mark_failed(
                    step="data_copy",
                    error_class="CopyTableError",
                    error_message=(
                        "Per-table copy failed: "
                        + ", ".join(f"{t['table']}={t['error']!r}" for t in copy_summary["tables_failed"])
                    ),
                )
                return 1

            writer.update_step("verify", progress_pct=80)
            if writer.check_cancel_requested():
                raise JobCancelled(step="verify")
            diffs = verify_pg_row_counts(source_url, target_url)

        if diffs:
            writer.mark_failed(
                step="verify",
                error_class="VerifyMismatchError",
                error_message=f"Row count mismatch: {diffs[:5]}",
            )
            return 1

        # flip_backend: H1-NEW final cancel re-check immediately before the
        # flip so cancel ↔ flip is mutually exclusive. The API endpoint
        # already 409s on cancels for step >= flip_backend, BUT there is a
        # narrow window between the migrator's last sentinel poll (end of
        # verify) and this line. If a cancel lands in that window the API
        # writes the sentinel and reverts instance.yaml; without this check
        # the migrator would proceed to flip — leaving instance.yaml on
        # SOURCE while data is on TARGET. Re-checking here closes that gap.
        writer.update_step("flip_backend", progress_pct=95)
        target_state = BackendState(to)
        # H1-PARTIAL: hold MigrationLock across the cancel re-check AND
        # the write_backend_state call so a concurrent cancel cannot
        # land between them. Pre-fix the two-line sequence had a
        # microsecond window where the cancel handler's revert ran
        # AFTER the re-check but BEFORE the migrator's flip, producing
        # data-on-TARGET + instance.yaml-on-SOURCE. The retry-once on
        # MigrationInProgressError covers the case where the API
        # cancel_job is currently holding the lock for its revert; by
        # the time we re-acquire, the cancel sentinel is on disk and
        # the re-check will raise JobCancelled.
        from src.db_state_machine import MigrationInProgressError, MigrationLock, read_backend_state

        def _flip_and_verify() -> None:
            """Flip the backend and confirm it landed — both under the lock.

            The applier treats `status=success` as proof that instance.yaml
            already names the target; it downgrades its own write to a url
            normalization it can afford to lose on that basis. Nothing but
            call order was enforcing that, so a future path returning 0
            without writing would have the applier assert a move the config
            does not reflect. One read of a file we just wrote makes it
            explicit.

            Inside the lock, not after it: a cancel landing in the gap
            reverts instance.yaml to the source, the read-back sees a
            mismatch, and a bare RuntimeError routes to `except Exception`
            -> `mark_failed`, losing the cancelled/failed distinction that
            `_check_cancel_before_flip`'s own docstring calls out as
            load-bearing for the host applier. Holding the lock across both
            means a mismatch here really is the write not having landed.
            """
            _check_cancel_before_flip(job_path=writer._path, target_state=target_state)
            write_backend_state(target_state, url=target_url)
            landed, _ = read_backend_state()
            if landed != target_state:
                raise BackendFlipNotVerified(
                    f"refusing to report success: instance.yaml names {landed!r} after the flip "
                    f"to {target_state!r}. The data migration completed, but the backend was not "
                    "switched, so reporting success would leave the applier and the admin UI "
                    "asserting a move that the config does not reflect."
                )

        try:
            with MigrationLock():
                _flip_and_verify()
        except MigrationInProgressError:
            time.sleep(0.5)
            with MigrationLock():
                _flip_and_verify()

        # A completed DuckDB-source migration turns the source file into a
        # frozen snapshot — record that next to it so the docker-compose
        # data-migrate one-shot (which re-runs the ON CONFLICT DO NOTHING
        # copy on every `compose up`) skips it instead of resurrecting rows
        # deleted from Postgres after the cutover. Best-effort by contract:
        # the helper never raises, and a missing marker only costs the skip.
        if source_backend == "duckdb":
            from scripts.migrate_duckdb_to_pg.marker import write_completion_marker

            write_completion_marker(
                duckdb_path,
                tables_migrated=int(copy_summary.get("tables_migrated", 0)),
                source="db_state_migrator",
            )

        # The read-back that guards this lives in `_flip_and_verify` above,
        # inside the migration lock. The applier leans on the ordering: when
        # it sees status=success it treats instance.yaml as already naming
        # the target and its own write as a url normalization it can afford
        # to lose — so a success must not be reachable without the flip.
        writer.mark_success(summary=copy_summary)
        return 0

    except JobCancelled as cancel_exc:
        writer.mark_cancelled(step=cancel_exc.step)
        # Revert state machine to source — same logic as the
        # generic exception path. The API endpoint also reverts,
        # but doing it here too closes the race where the cancel
        # endpoint runs while the migrator is between step writes.
        try:
            revert_state = (
                BackendState(source_backend)
                if source_backend
                else (BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR)
            )
            write_backend_state(
                revert_state,
                url=source_url if source_backend in ("side_car", "cloud") else None,
            )
        except Exception:
            pass
        # Cancellation is a normal end — return 0. The applier looks at
        # status: cancelled in the job JSON, not the exit code.
        return 0

    except BackendFlipNotVerified as e:
        # NO revert. The rows are on the target by the time this can fire, so
        # writing the source back would create precisely the config/data split
        # the check exists to detect. Record it and leave the overlay alone.
        log.error(
            "backend flip could not be verified after a completed migration: %s. "
            "instance.yaml is NOT being reverted — the data is on the target. "
            "Inspect the overlay by hand.",
            e,
        )
        writer.mark_failed(
            step=writer._read().get("current_step", "unknown"),
            error_class=type(e).__name__,
            error_message=str(e),
        )
        return 1

    except Exception as e:
        # Revert state to the source backend (best-effort). The source
        # is the authoritative "what's still working" — the in-progress
        # state was just a transient flag the API endpoint set before
        # we started. If we fail, the operator should see the original
        # backend on the next /api/admin/db/state read, not the
        # *_in_progress that hung from a half-finished migration.
        try:
            revert_state = (
                BackendState(source_backend)
                if source_backend
                else (BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR)
            )
            # For the PG sources, preserve the URL we came from so the
            # app can keep reading after restart. For DuckDB source the
            # URL is intentionally None.
            write_backend_state(
                revert_state,
                url=source_url if source_backend in ("side_car", "cloud") else None,
            )
        except Exception:
            pass
        writer.mark_failed(
            step=writer._read().get("current_step", "unknown"),
            error_class=type(e).__name__,
            error_message=str(e),
        )
        return 1


def _log_backup_skip(writer: JobWriter, reason: str) -> None:
    """Annotate the job file with a non-fatal backup skip."""
    data = writer._read()
    data.setdefault("warnings", []).append(
        {
            "step": "backup",
            "message": f"backup skipped: {reason}",
        }
    )
    writer._write(data)


def _resolve_source_url_from_instance_yaml() -> str | None:
    """Read ``database.url`` from /data/state/instance.yaml.

    Default source for ``--to cloud`` when the applier hasn't passed
    ``--source-url`` explicitly. Returns None if the file doesn't exist
    or lacks the key — caller decides whether to error.
    """
    import yaml as _yaml

    overlay = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "instance.yaml"
    if not overlay.exists():
        return None
    try:
        data = _yaml.safe_load(overlay.read_text()) or {}
    except Exception:
        return None
    return (data.get("database") or {}).get("url")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--to", choices=["side_car", "cloud"], required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument(
        "--source-backend",
        choices=["duckdb", "side_car", "cloud"],
        help="Explicit source backend; the applier always passes this. "
        "When omitted, defaults to 'duckdb' for --to=side_car and "
        "'side_car' for --to=cloud (legacy forward-only paths).",
    )
    parser.add_argument(
        "--source-url",
        help="Source PG URL (any PG source). Defaults to instance.yaml's database.url.",
    )
    parser.add_argument("--duckdb-path", type=Path, default=Path("/data/state/system.duckdb"))
    parser.add_argument("--jobs-dir", type=Path, default=Path("/data/state/db-jobs"))
    parser.add_argument("--backups-dir", type=Path, default=Path("/data/state/backups"))
    args = parser.parse_args()

    source_backend = args.source_backend
    if source_backend is None:
        # Legacy default — keeps the old direct-invocation paths working.
        source_backend = "duckdb" if args.to == "side_car" else "side_car"

    source_url = args.source_url
    if source_backend in ("side_car", "cloud") and not source_url:
        source_url = _resolve_source_url_from_instance_yaml()

    sys.exit(
        main(
            job_id=args.job_id,
            to=args.to,
            target_url=args.target_url,
            duckdb_path=args.duckdb_path,
            jobs_dir=args.jobs_dir,
            backups_dir=args.backups_dir,
            source_url=source_url,
            source_backend=source_backend,
        )
    )
