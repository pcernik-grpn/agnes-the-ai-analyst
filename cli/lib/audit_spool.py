"""Client-reported CLI audit events (F3 — audit-full-coverage plan, Task 9).

`agnes query`/`agnes explore` running against the local DuckDB (`--scope
local`/`auto` when the local path succeeds or fails) never touches the
server, so today those runs leave zero rows in `audit_log` — a real
coverage gap for an analyst who works offline for hours. This module is the
client-side half of the fix: a durable, append-only JSONL spool that
`record_local_event` writes to and `agnes push` later drains and uploads in
one batch via `POST /api/upload/audit-events`.

State lives in the SAME directory the push ledger uses
(`<workspace_root>/.claude/`, see `cli/lib/upload_log.py`) — one state dir
per workspace, no new config key. Every write/read here is best-effort: no
workspace_root configured, a permission error, a full disk — none of these
may ever fail the CLI command that's just trying to run a query.

Two-phase drain: `drain_spool()` reads (does not remove) up to
`max_events` events; only a subsequent `commit_drain()` call removes
exactly what the last `drain_spool()` returned. A caller that fails to
upload the drained batch simply never calls `commit_drain()` — the spool
stays intact and the next push retries the same events (plus whatever
`record_local_event` appended since).

Content discipline: params carry metadata only (table names, a SQL hash,
row/duration counts) — NEVER the SQL text itself. See
`src/audit_helpers.hash_args`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli.config import get_workspace_root

_SPOOL_FILENAME = "audit_spool.jsonl"

# Set by the most recent `drain_spool()` call — the exact count of raw
# (non-blank) lines it read, so `commit_drain()` removes precisely that
# many without racing a `record_local_event` appended in between (new lines
# always land at the end, so `lines[count:]` still keeps them).
_last_drain_count: Optional[int] = None


def _spool_path() -> Optional[Path]:
    """Same `.claude` state dir the push ledger uses, or None when no
    `workspace_root` is configured (nothing to anchor the spool to)."""
    workspace_root = get_workspace_root()
    if not workspace_root:
        return None
    d = Path(workspace_root) / ".claude"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / _SPOOL_FILENAME


def _read_nonblank_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def record_local_event(action: str, params: dict[str, Any]) -> None:
    """Append one `{"action", "params", "observed_at"}` JSON line to the
    audit spool. Never raises — IO errors (no workspace configured, disk
    full, permission denied) are swallowed so a local query command never
    fails because of audit bookkeeping.
    """
    path = _spool_path()
    if path is None:
        return
    event = {
        "action": action,
        "params": params,
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except OSError:
        pass


def drain_spool(max_events: int = 500) -> list[dict[str, Any]]:
    """Return up to `max_events` spooled events, oldest first, WITHOUT
    removing them from disk yet — call `commit_drain()` after a successful
    upload to actually remove them. Returns `[]` when there's no
    workspace configured, no spool file, or it can't be read. A line that
    fails to parse as JSON is skipped (and still counted against the
    removal window `commit_drain()` uses, so a corrupt line doesn't wedge
    the spool forever).
    """
    global _last_drain_count
    path = _spool_path()
    if path is None:
        _last_drain_count = None
        return []
    lines = _read_nonblank_lines(path)
    selected = lines[:max_events]
    events: list[dict[str, Any]] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    _last_drain_count = len(selected)
    return events


def commit_drain() -> None:
    """Remove exactly the lines the most recent `drain_spool()` call read.

    No-op if `drain_spool()` was never called, returned nothing, or IO
    fails — a failed commit just means the next `drain_spool()` sees the
    same events again (idempotent re-upload on the server side, per the
    `POST /api/upload/audit-events` contract).
    """
    global _last_drain_count
    count = _last_drain_count
    _last_drain_count = None
    if not count:
        return
    path = _spool_path()
    if path is None:
        return
    lines = _read_nonblank_lines(path)
    remaining = lines[count:]
    try:
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for line in remaining:
                f.write(line + "\n")
        tmp_path.replace(path)
    except OSError:
        pass
