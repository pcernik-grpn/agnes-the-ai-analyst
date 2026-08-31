"""Re-key `data_app` grants that were written with an app's row id.

A ``data_app`` grant is keyed by the app's SLUG: ``ResourceTypeSpec`` declares
``id_format="<slug>"``, ``_can_view`` calls ``can_access(…, row["slug"])``, and
the Library's apps band tests ``da["slug"] in granted_ids``. The apps builder's
"Publish to the Library" sent the app's row id (``app_<hex>``) instead, so every
grant it wrote was unreadable: publishing reported success and the granted group
still could not see a single app. The MCP builder's apps section published
through the same shared panel, so it wrote the same dead rows.

The write side is fixed (``linked_apps_panel.js``), which stops NEW dead rows.
This repairs the ones already there. Without it an instance keeps apps that look
published and are not — and the symptom is invisible from the admin UI, because
the grant row exists and lists correctly on /admin/access.

Why a script and not a migration: the DuckDB app-state ladder is frozen (A3),
so a ``_vN_to_v(N+1)`` step is not available, and an Alembic revision would
repair only the Postgres instances. Going through the repository factory covers
whichever backend is active, exactly like ``backfill_marketplace_rollup.py``.

Idempotent and safe to re-run: a second run finds nothing to do. It never
touches a grant whose ``resource_id`` is not the id of a known app, so a
slug-keyed grant (the correct shape) and a grant naming an app that no longer
exists are both left alone — the latter deliberately, since deleting it would
destroy the only record of intent if the app is re-linked.

Usage::

    python scripts/repair_data_app_grant_keys.py --dry-run   # look first
    python scripts/repair_data_app_grant_keys.py
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.resource_types import ResourceType
from src.repositories import data_apps_repo, resource_grants_repo, user_groups_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def repair(*, dry_run: bool) -> int:
    """Re-key every id-keyed ``data_app`` grant. Returns the number repaired."""
    grants = resource_grants_repo()
    apps = data_apps_repo()

    # include_drafts so a draft's grant is re-keyed too, and a high limit
    # because the repo's default cap applies before any filtering.
    id_to_slug = {r["id"]: r["slug"] for r in apps.list(include_drafts=True, limit=100000)}
    group_names = {g["id"]: g["name"] for g in user_groups_repo().list_all()}

    rows = grants.list_all(resource_type=ResourceType.DATA_APP.value)
    # Which (group, slug) pairs already exist, so a re-key that would collide
    # with a correct grant deletes the dead row instead of duplicating it.
    have = {(r["group_id"], r["resource_id"]) for r in rows}

    broken = [r for r in rows if r["resource_id"] in id_to_slug]
    if not broken:
        log.info("nothing to repair: no data_app grant is keyed by a row id (%d grants checked)", len(rows))
        return 0

    repaired = 0
    for r in broken:
        slug = id_to_slug[r["resource_id"]]
        group = group_names.get(r["group_id"], r["group_id"])
        already = (r["group_id"], slug) in have
        verb = "drop (slug grant already present)" if already else f"re-key -> {slug}"
        log.info("%s: %s %s [%s]", "would" if dry_run else "will", group, r["resource_id"], verb)
        if dry_run:
            repaired += 1
            continue
        if not already:
            # ensure_grant first, delete second: a crash between the two leaves
            # the app reachable rather than unreachable.
            grants.ensure_grant(r["group_id"], ResourceType.DATA_APP.value, slug, assigned_by=r.get("assigned_by"))
            have.add((r["group_id"], slug))
        grants.delete(r["id"])
        repaired += 1

    log.info("%s %d grant(s)", "would repair" if dry_run else "repaired", repaired)
    return repaired


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report what would change and exit")
    args = ap.parse_args()
    try:
        repair(dry_run=args.dry_run)
    except Exception:
        log.exception("repair failed — no partial state is left behind for a grant that was not logged above")
        sys.exit(1)


if __name__ == "__main__":
    main()
