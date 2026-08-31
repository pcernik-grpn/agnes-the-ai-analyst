"""Completion marker for the one-shot DuckDB → Postgres migration.

Once the migration has genuinely completed, the source ``system.duckdb``
becomes a frozen snapshot — Postgres is the source of truth from that
moment on. But the docker-compose ``data-migrate`` one-shot re-runs the
``INSERT … ON CONFLICT DO NOTHING`` copy on every ``compose up``: that
idempotency is what makes crash-RESUME safe, and exactly what makes
re-running AFTER completion harmful — any row an admin deletes from
Postgres that still exists in the snapshot is silently re-inserted with
its original values and no audit trail (registry rows, package
memberships, sync bookkeeping). Observed in production as "unregistered
tables come back after every container recreate".

A successful COMPLETE run therefore records completion in
``<duckdb>.migrated`` next to the source file, and the CLI skips the copy
when the marker is present (``--force`` / ``--reset-target`` override; see
``__main__``). The marker sits next to the DuckDB file — not in Postgres —
because it describes THIS snapshot file: move or replace the file and the
verdict no longer applies, and neither does the marker.

Helpers never raise: a marker that cannot be read is treated as absent
(the copy re-runs — the pre-marker behavior), and a marker that cannot be
written is logged and dropped (the copy stays correct; only the skip
optimization is lost until the next successful run).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: Suffix appended to the source DuckDB path — ``system.duckdb.migrated``.
MARKER_SUFFIX = ".migrated"


def marker_path(duckdb_path: Path | str) -> Path:
    """The completion-marker path for ``duckdb_path``."""
    p = Path(duckdb_path)
    return p.with_name(p.name + MARKER_SUFFIX)


def read_completion_marker(duckdb_path: Path | str) -> Optional[Dict[str, Any]]:
    """The recorded completion, or ``None`` when absent or unreadable.

    Unreadable covers a corrupt/truncated file and a non-dict payload —
    both degrade to "no marker" so a damaged marker can never brick the
    compose boot gate; the copy simply re-runs and rewrites it.
    """
    path = marker_path(duckdb_path)
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("ignoring unreadable completion marker %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        log.warning("ignoring non-object completion marker %s", path)
        return None
    return payload


def write_completion_marker(
    duckdb_path: Path | str,
    *,
    tables_migrated: int,
    source: str,
) -> Optional[Path]:
    """Record a completed migration; returns the marker path, or ``None``.

    ``source`` names the writer (``migrate_duckdb_to_pg`` for the CLI /
    compose one-shot, ``db_state_migrator`` for the applier-driven
    migration) so an operator reading the file knows which path finished.
    Write failures are logged and swallowed — the migration itself
    succeeded, and refusing to boot over a marker write would invert the
    marker's purpose.
    """
    path = marker_path(duckdb_path)
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "tables_migrated": int(tables_migrated),
        "source": source,
    }
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception as exc:
        log.warning("could not write completion marker %s: %s", path, exc)
        return None
    return path


def target_has_app_state(pg_engine) -> bool:
    """True when the target PG already holds app-state rows.

    The completion marker sits next to the DuckDB file, so it describes a
    migration into whichever database was the target BACK THEN. Honoring
    it against an empty target would be wrong: the disaster-recovery flow
    restores the DuckDB backup and starts from a fresh PG, and skipping
    the copy there boots an empty instance. ``users`` is the sentinel —
    every instance that ever ran has user rows (seed admin + system
    users), so "marker present but zero users" reads as "the target was
    replaced or restored" and the copy must run.

    A failed probe returns ``False`` (run the copy): the copy will hit the
    same broken target and fail loudly — exactly the pre-marker behavior —
    instead of this helper inventing a new way for boot to succeed
    silently against a database it could not even read.
    """
    import sqlalchemy as sa

    try:
        with pg_engine.connect() as conn:
            return bool(conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM users)")).scalar())
    except Exception as exc:
        log.warning("could not probe target app state; running the copy: %s", exc)
        return False
