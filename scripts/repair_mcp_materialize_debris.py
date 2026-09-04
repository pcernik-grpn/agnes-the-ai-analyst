"""Return write-shaped MCP tools that the old lister guess left in
``materialize`` mode to ``passthrough``.

Reading a linked-app list is two steps: the panel flips the chosen tool into
``materialize`` mode, then asks the server to run it. Until #2154 the panel
chose that tool by taking the first one whose name contained "data" and "app",
which on a real server is often a WRITE tool — e.g.
``create_python_js_data_app_git_credential``.

#2249 refuses a write-shaped nomination at the second step, which closes the
route that CREATES this state. It does not clear what already exists: on any
instance where "Read the app list" was clicked before that fix, the wrongly
chosen tool is still sitting in ``materialize`` mode. That row is not inert
(#2251) — a source-level "Materialize now" runs every materialize-mode tool
and does not consult ``read_only``::

    # connectors/mcp/extractor.py
    tools = [t for t in all_tools if t["mode"] == MATERIALIZE and t.get("enabled", True)]

…so a deliberate admin click invokes the leftover write tool with no arguments,
which is the #2154 call reached by a different door. Tools are otherwise
registered as ``passthrough`` (``mcp_builder.js``), so a write-shaped
materialize row almost always exists only because of the bug.

**Almost** is why this reports before it changes anything. Materializing a
write-shaped tool is a legitimate thing for an admin to have chosen on purpose
— a read-only ``run_report`` is not a contradiction — and a name is a guess,
not a declaration. So the default run only prints what it would do, annotated
with the fingerprint of the buggy flow (``schedule = 'daily 03:00'``, which is
what the panel wrote and which nothing in the scheduler ever reads), and
``--apply`` is a separate, deliberate second run.

The narrower, behaviour-changing alternative — teaching the full-source path to
skip write-shaped tools — is deliberately NOT taken here. It would break the
legitimate setup above, and with #2249 in place there is no remaining *bug*
route into this state for it to defend.

Why a script and not a migration: the DuckDB app-state ladder is frozen (A3),
so a ``_vN_to_v(N+1)`` step is not available, and an Alembic revision would
repair only the Postgres instances. Going through the repository factory covers
whichever backend is active, exactly like ``repair_data_app_grant_keys.py``.

Idempotent and safe to re-run: a second run finds nothing to do.

Usage::

    python scripts/repair_mcp_materialize_debris.py             # report only
    python scripts/repair_mcp_materialize_debris.py --apply     # revert them
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List

from src.mcp_tool_shape import is_write_shaped, tool_name
from src.repositories import mcp_sources_repo, tool_registry_repo
from src.repositories.tool_registry import MATERIALIZE, PASSTHROUGH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

#: What the linked-apps panel wrote alongside the mode (``linked_apps_panel.js``).
#: Corroboration in the report, never a filter: the old data-sources wizard
#: wrote the same string for tools an admin did choose to materialize.
_LISTER_SCHEDULE = "daily 03:00"


def find_debris() -> List[Dict[str, Any]]:
    """Every write-shaped tool sitting in ``materialize`` mode.

    ``enabled_only=False``: a disabled row still carries the mode, and a run
    that re-enables it would invoke the tool — so it is debris too.
    """
    tools = tool_registry_repo()
    return [
        row
        for row in tools.list_by_mode(MATERIALIZE, enabled_only=False)
        if is_write_shaped(tool_name(row))
    ]


def repair(*, apply: bool) -> int:
    """Report the debris, and revert it when ``apply``. Returns the count."""
    rows = find_debris()
    if not rows:
        log.info("nothing to repair: no write-shaped tool is in materialize mode")
        return 0

    # `mcp_sources` keys on `id`; `tool_registry` refers to it as `source_id`.
    source_names = {s["id"]: s.get("name") or s["id"] for s in mcp_sources_repo().list_all()}
    tools = tool_registry_repo()

    log.info("%d write-shaped tool(s) in materialize mode:", len(rows))
    for row in rows:
        fingerprint = " [lister fingerprint]" if row.get("schedule") == _LISTER_SCHEDULE else ""
        log.info(
            "  %s.%s  schedule=%r enabled=%s%s",
            source_names.get(row["source_id"], row["source_id"]),
            tool_name(row),
            row.get("schedule"),
            row.get("enabled"),
            fingerprint,
        )

    if not apply:
        log.info("")
        log.info("Report only — nothing changed. Re-run with --apply to return these to passthrough.")
        log.info("A tool you materialize on purpose will be reverted too; check the list first.")
        return len(rows)

    for row in rows:
        # Re-upsert rather than a mode setter: the repositories have no
        # set_mode, and the PG-first ratchet makes adding one to a frozen
        # DuckDB<->PG pair work this repair does not need. Every field is
        # restated from the row, so nothing but mode and schedule moves.
        tools.upsert(
            tool_id=row["tool_id"],
            source_id=row["source_id"],
            original_name=row["original_name"],
            exposed_name=row["exposed_name"],
            mode=PASSTHROUGH,
            table_id=row.get("table_id"),
            input_schema=row.get("input_schema"),
            description=row.get("description"),
            mutating=bool(row.get("mutating")),
            pii_fields=row.get("pii_fields"),
            rate_limit_pm=row.get("rate_limit_pm"),
            # Cleared with the mode: the schedule only ever meant something to
            # a materialize row, and nothing reads it on a passthrough one.
            schedule=None,
            enabled=bool(row.get("enabled", True)),
            projection_map=row.get("projection_map"),
        )
        log.info(
            "reverted %s.%s to passthrough",
            source_names.get(row["source_id"], row["source_id"]),
            tool_name(row),
        )
    log.info("repaired %d tool(s)", len(rows))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually revert the tools to passthrough (default: report only)",
    )
    args = ap.parse_args()
    repair(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
