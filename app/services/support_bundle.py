"""Support-bundle doctor — the server half of ``agnes doctor``.

One structured, **redacted** snapshot of the instance an operator can attach
to a support ticket: build fingerprint, schema/migration verdict, per-source
sync rollup with the most recent failures, data-dir disk usage, process
signals, retrieval mode, and secret *presence* (names and booleans only —
never values; the redaction test pins this).

Unlike its sibling ``instance_doctor`` (the new-instance deployment gate,
active checks with pass/fail verdicts), this module *collects state*: the
questions support asks first, answered from the database and the process
environment. Every collector runs isolated — one crashing resolver reports
itself as an ``{"status": "error"}`` section instead of killing the report,
because a doctor that dies when things are broken is no doctor at all (same
containment contract as ``instance_doctor._isolated``).

"Container health" is deliberately process-level (deployed-at, backend
reachability implied by the schema/sync sections succeeding): the app cannot
see the Docker daemon from inside its own container; host-side container
checks belong to the ``scripts/ops/`` siblings.

Consumed by ``GET /api/admin/doctor/support`` (``app/api/admin_doctor.py``)
and ``agnes doctor`` (``cli/commands/doctor.py``).
Design: docs/superpowers/specs/2026-08-23-support-bundle-doctor-design.md.
"""

import logging
import os
import platform
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_STALE_AFTER = timedelta(hours=24)  # same threshold /api/health/detailed uses
_MAX_ERRORS_PER_SOURCE = 5

# Env vars whose PRESENCE is worth reporting to support. Names only — the
# collector never reads the values into the payload. Sourced from
# config/.env.template and docs/DEPLOYMENT.md; all vendor-agnostic knobs of
# this distribution.
SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "E2B_API_KEY",
    "JWT_SECRET_KEY",
    "SESSION_SECRET",
    "AGNES_VAULT_KEY",
    "APPS_RUNNER_TOKEN",
    "DATABASE_URL",
    "SCHEDULER_API_TOKEN",
    "SENDGRID_API_KEY",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET",
    "KEBOOLA_STORAGE_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "TELEGRAM_BOT_TOKEN",
)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _collect_build() -> dict:
    # Same sources /api/version and /api/health/detailed read; the env vars
    # are stamped by the image build, APP_VERSION by package metadata.
    from app.api.health import _DEPLOYED_AT
    from app.version import APP_VERSION

    return {
        "status": "ok",
        "version": os.environ.get("AGNES_VERSION", "dev"),
        "package_version": APP_VERSION,
        "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
        "image_tag": os.environ.get("AGNES_TAG", "unknown"),
        "commit_sha": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        "deployed_at": _DEPLOYED_AT,
    }


def _collect_schema() -> dict:
    # Reuse the backend-aware check (DuckDB schema_version vs Alembic head)
    # rather than re-deriving it — one verdict format, not two.
    from app.api.health import _check_db_schema
    from src.repositories import use_pg

    result = _check_db_schema()
    verdict = result.get("db_schema")
    return {
        "status": "ok" if verdict == "ok" else "error",
        "backend": "postgres" if use_pg() else "duckdb",
        **result,
    }


def _collect_retrieval() -> dict:
    # Module-attribute call on purpose: the mode must be read at collect
    # time (embedding capability can change with the environment), and the
    # tests patch `src.ingest.retrieval.retrieval_mode`.
    from src.ingest import retrieval

    mode = retrieval.retrieval_mode()
    if mode == "hybrid":
        return {"status": "ok", "mode": mode, "detail": "semantic + lexical scoring active"}
    return {
        "status": "warning",
        "mode": mode,
        "detail": (
            "semantic scoring is INACTIVE — document/knowledge search is ranking "
            "lexically only (#898). Install the embeddings extra (or restore the "
            "embedding backend) if hybrid retrieval is expected on this instance."
        ),
    }


def _collect_sync() -> dict:
    """Per-source rollup of the sync bookkeeping.

    Joins ``table_registry`` (which source a table belongs to) against
    ``sync_state`` (what happened last) in Python — both repos already
    expose full-listing reads; no new repo method needed.

    The counters are independent FACETS, not a partition of ``tables``: a
    row that failed its first-ever sync is both an ``error`` and
    ``never_synced``, so the columns can legitimately sum past the table
    count. Each answers a different support question ("is it failing" vs
    "has it ever landed") and collapsing them would lose one.
    """
    from src.repositories import sync_state_repo, table_registry_repo

    states = {s["table_id"]: s for s in sync_state_repo().get_all_states()}
    now = datetime.now(timezone.utc)

    sources: dict[str, dict] = {}
    for row in table_registry_repo().list_all():
        source = row.get("source_type") or "unknown"
        agg = sources.setdefault(
            source,
            {
                "tables": 0,
                "ok": 0,
                "errors": 0,
                "stale": 0,
                "never_synced": 0,
                "last_sync_max": None,
                "last_errors": [],
            },
        )
        agg["tables"] += 1

        state = states.get(row["id"]) or states.get(row.get("name", ""))
        if not state:
            agg["never_synced"] += 1
            continue

        last_sync = state.get("last_sync")
        if last_sync is not None and getattr(last_sync, "tzinfo", None) is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        if last_sync is None:
            agg["never_synced"] += 1
        else:
            if agg["last_sync_max"] is None or last_sync > agg["last_sync_max"]:
                agg["last_sync_max"] = last_sync
            # Staleness only matters for rows a scheduler is expected to
            # refresh; remote rows have no parquet to go stale.
            if (row.get("query_mode") or "local") != "remote" and now - last_sync > _STALE_AFTER:
                agg["stale"] += 1

        if state.get("status") == "error":
            agg["errors"] += 1
            if len(agg["last_errors"]) < _MAX_ERRORS_PER_SOURCE:
                agg["last_errors"].append(
                    {
                        "table_id": row["id"],
                        "error": state.get("error"),
                        "last_sync": _iso(last_sync),
                    }
                )
        elif state.get("status") == "ok":
            agg["ok"] += 1

    for agg in sources.values():
        agg["last_sync_max"] = _iso(agg["last_sync_max"])

    worst = "ok"
    if any(a["errors"] for a in sources.values()):
        worst = "warning"
    return {"status": worst, "sources": sources}


def _collect_disk() -> dict:
    from src.db import _get_data_dir

    data_dir = _get_data_dir()
    usage = shutil.disk_usage(data_dir)

    def _size(p: Path) -> int | None:
        try:
            return p.stat().st_size
        except OSError:
            return None

    free_ratio = usage.free / usage.total if usage.total else 0
    return {
        # <5% free disk is the incident precursor worth flagging loudly.
        "status": "ok" if free_ratio >= 0.05 else "warning",
        "data_dir": str(data_dir),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "system_db_bytes": _size(data_dir / "state" / "system.duckdb"),
        "analytics_db_bytes": _size(data_dir / "analytics" / "server.duckdb"),
    }


def _collect_process() -> dict:
    from app.roles import active_roles
    from src.repositories import use_pg

    return {
        "status": "ok",
        "state_backend": "postgres" if use_pg() else "duckdb",
        "roles": sorted(r.value for r in active_roles()),
        "python": platform.python_version(),
    }


def _collect_secrets() -> dict:
    # Presence booleans ONLY. Never values, never lengths, never prefixes.
    return {name: bool(os.environ.get(name)) for name in SECRET_ENV_NAMES}


def _isolated(name: str, fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — containment is the point
        logger.exception("support doctor section %s crashed", name)
        return {"status": "error", "detail": f"section crashed: {e}"}


def build_support_bundle() -> dict:
    """Collect every section; blocking DB reads, call off the event loop."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build": _isolated("build", _collect_build),
        "schema": _isolated("schema", _collect_schema),
        "retrieval": _isolated("retrieval", _collect_retrieval),
        "sync": _isolated("sync", _collect_sync),
        "disk": _isolated("disk", _collect_disk),
        "process": _isolated("process", _collect_process),
        "secrets": _isolated("secrets", _collect_secrets),
    }
