#!/usr/bin/env python3
"""Operator script — measure how much volume `audit_log` writers create.

Reports rows/day over a lookback window, the top actions by volume, and a
size projection at the configured `audit.retention_days` — the two numbers
an operator needs before reaching for the levers in `src/audit_helpers.py`
(`should_sample`) or `config/instance.yaml.example`'s `audit.sampling`
block. See `docs/observability.md` -> "Audit log volume" for a worked
example with real measured numbers and what instance shape produced them.

Follows the repo's command-UX standard
(`.claude/skills/agnes-conventions/references/command-ux.md`): `--limit`
for the top-N cutoff, `--json` for machine use, and the report always
labels its result origin (which backend it read: DuckDB or Postgres, and
the exact `since` timestamp the window started at).

Run from the repo root, against whichever `DATA_DIR` (or `DATABASE_URL`
for a Postgres instance) the target instance uses:

    DATA_DIR=/path/to/data .venv/bin/python -m scripts.audit_volume_estimate
    DATA_DIR=/path/to/data .venv/bin/python -m scripts.audit_volume_estimate --days 30 --limit 10 --json
"""

from __future__ import annotations

import argparse
import json as json_module
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_WINDOW_DAYS = 7
DEFAULT_TOP_N = 20
ROW_SAMPLE_SIZE = 200
DOMINANT_ACTION_THRESHOLD = 0.25


def build_report(
    *,
    window_days: int,
    events_total: int,
    top_actions: list[dict[str, Any]],
    retention_days: int,
    avg_row_bytes: Optional[float] = None,
) -> dict[str, Any]:
    """Pure aggregation: raw counts -> the operator-facing report shape.

    ``top_actions`` is the ``AuditRepository.facets(...)["actions"]`` shape
    — ``[{"value": <action>, "count": <n>}, ...]``, largest first, already
    capped at the caller's ``--limit``. The dominant-action flag and every
    percentage are computed against ``events_total`` (the FULL window
    count), never the sum of the (possibly truncated) list, so a small
    ``--limit`` can never hide or fake the >25% flag.
    """
    rows_per_day = round(events_total / window_days, 1) if window_days else 0.0

    actions_out: list[dict[str, Any]] = []
    dominant: Optional[dict[str, Any]] = None
    for row in top_actions:
        count = row["count"]
        pct = round(count / events_total, 4) if events_total else 0.0
        actions_out.append({"action": row["value"], "count": count, "pct": pct})
        if dominant is None and pct > DOMINANT_ACTION_THRESHOLD:
            dominant = {"action": row["value"], "pct": pct}

    projected_rows: Optional[int]
    if retention_days and retention_days > 0:
        projected_rows = round(rows_per_day * retention_days)
    else:
        # 0/negative retention_days means "keep forever" (same convention as
        # src/audit_retention.py) — there is no bound to project against.
        projected_rows = None

    report: dict[str, Any] = {
        "window_days": window_days,
        "events_total": events_total,
        "rows_per_day": rows_per_day,
        "top_actions": actions_out,
        "dominant_action": dominant,
        "retention_days": retention_days,
        "projected_rows_at_retention": projected_rows,
    }
    if avg_row_bytes is not None:
        report["avg_row_bytes_sampled"] = round(avg_row_bytes, 1)
        report["projected_bytes_at_retention"] = (
            round(avg_row_bytes * projected_rows) if projected_rows is not None else None
        )
    return report


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def render_table(report: dict[str, Any], *, backend: str, since: datetime) -> str:
    """Plain-text rendering of :func:`build_report`'s output."""
    lines = [
        f"Audit volume report — backend={backend} window={report['window_days']}d since={since.isoformat()}",
        f"  events_total={report['events_total']}  rows_per_day={report['rows_per_day']}",
    ]
    if report.get("retention_days"):
        proj = report.get("projected_rows_at_retention")
        lines.append(f"  retention_days={report['retention_days']}  projected_rows_at_retention={proj}")
        if report.get("projected_bytes_at_retention") is not None:
            lines.append(
                f"  projected size at retention: {_human_bytes(report['projected_bytes_at_retention'])}"
                f" (avg {report['avg_row_bytes_sampled']} bytes/row, sampled)"
            )
    else:
        lines.append("  retention_days=0 (keep forever) — no bound to project against")

    if report.get("dominant_action"):
        d = report["dominant_action"]
        lines.append(f"  ! dominant action: {d['action']} = {d['pct']:.1%} of rows (>{DOMINANT_ACTION_THRESHOLD:.0%})")

    lines.append("")
    lines.append(f"  {'action':<40} {'count':>10} {'pct':>8}")
    for row in report["top_actions"]:
        lines.append(f"  {row['action']:<40} {row['count']:>10} {row['pct']:>8.1%}")
    if not report["top_actions"]:
        lines.append("  (no actions in this window)")
    return "\n".join(lines)


def _sample_avg_row_bytes(repo: Any, sample_size: int = ROW_SAMPLE_SIZE) -> Optional[float]:
    """Average JSON-encoded byte size over the most recent rows.

    A real (if approximate) measurement rather than an assumed constant —
    `params` payload size varies a lot by action, so sampling actual rows
    beats guessing a fixed row width.
    """
    rows, _ = repo.query(limit=sample_size)
    if not rows:
        return None
    total = sum(len(json_module.dumps(r, default=str)) for r in rows)
    return total / len(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate audit_log write volume and project its footprint at the configured retention window.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_TOP_N, help=f"Top N actions to report (default: {DEFAULT_TOP_N})."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a plain table.")
    args = parser.parse_args(argv)

    from app.instance_config import get_audit_retention_days
    from src.repositories import audit_repo, use_pg

    repo = audit_repo()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    kpis = repo.kpis(since=since, trail="audit")
    events_total = kpis["events_total"]
    top_actions = repo.facets(since=since, trail="audit", limit=args.limit)["actions"]
    retention_days = get_audit_retention_days()
    avg_row_bytes = _sample_avg_row_bytes(repo) if events_total else None

    report = build_report(
        window_days=args.days,
        events_total=events_total,
        top_actions=top_actions,
        retention_days=retention_days,
        avg_row_bytes=avg_row_bytes,
    )
    backend = "postgres" if use_pg() else "duckdb"

    if args.json:
        print(json_module.dumps({**report, "backend": backend, "since": since.isoformat()}, indent=2))
    else:
        print(render_table(report, backend=backend, since=since))

    if events_total == 0:
        print(
            "No audit_log rows found in this window. If this is unexpected, widen --days, "
            "or check `agnes admin activity` / `/admin/activity` to confirm rows are landing at all.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
