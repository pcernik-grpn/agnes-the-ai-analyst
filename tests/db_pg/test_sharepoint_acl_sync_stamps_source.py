"""The ACL sync, RUN — not grepped.

Two of the guards written with this fix assert that `acl_sync.py` *contains*
`source=ACL_SYNC_GRANT_SOURCE`. That proves the line exists, not that it
executes, which is the same weakness this effort spent its time removing
from /admin/access. This file runs `_reconcile_grants` against a real
Postgres and reads back what the Access page would be told.

Postgres only, and that is the subject rather than a limitation:
`resource_grants.source` arrived in Alembic 0096, which is PG-only under the
A3 ratchet, so this mechanism does not exist on the frozen DuckDB backend.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa


@pytest.fixture
def pg_repos(pg_engine, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    return UserGroupsPgRepository(engine), ResourceGrantsPgRepository(engine), engine


def _collection(engine) -> str:
    """A file_corpora row for the grant's FK to point at."""
    cid = "col_" + uuid.uuid4().hex[:12]
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO file_corpora (id, name, slug, created_by) "
                "VALUES (:id, :n, :s, 'admin@x.com')"
            ),
            {"id": cid, "n": "SharePoint scope", "s": cid},
        )
    return cid


def test_a_new_mirrored_grant_is_written_with_the_syncs_own_source(pg_repos, monkeypatch):
    """The add path. Run the reconciler; the row it writes must carry the
    source, or /admin/access has no way to know a revoke cannot hold."""
    groups, grants, engine = pg_repos
    from connectors.sharepoint import acl_sync
    from src.grant_sources import describe

    grp = groups.create(name="sp-mirrored-" + uuid.uuid4().hex[:6], created_by="admin@x.com")
    col = _collection(engine)

    acl_sync._reconcile_grants(col, [grp["id"]], "scope-1")

    rows = [g for g in grants.list_all(resource_type="collection") if g["resource_id"] == col]
    assert len(rows) == 1, rows
    assert rows[0]["source"] == "sharepoint_acl_sync"
    # …and that is what makes the page withhold the Revoke.
    assert describe(rows[0]["source"])["revocable"] is False


def test_a_grant_the_sync_made_before_it_stamped_is_adopted(pg_repos):
    """The half that decides whether existing instances are fixed. Without
    it the change reaches only new grants, and every mirrored collection
    already out there keeps drawing a Revoke the next run undoes."""
    groups, grants, engine = pg_repos
    from connectors.sharepoint import acl_sync

    grp = groups.create(name="sp-legacy-" + uuid.uuid4().hex[:6], created_by="admin@x.com")
    col = _collection(engine)

    # Exactly what the old code wrote: sentinel owner, no source.
    grants.create(
        group_id=grp["id"],
        resource_type="collection",
        resource_id=col,
        assigned_by=acl_sync.ACL_SYNC_SENTINEL,
    )
    before = [g for g in grants.list_all(resource_type="collection") if g["resource_id"] == col]
    assert before[0]["source"] is None, "precondition: the legacy row has no source"

    # A run that changes nothing about membership still adopts it.
    acl_sync._reconcile_grants(col, [grp["id"]], "scope-1")

    after = [g for g in grants.list_all(resource_type="collection") if g["resource_id"] == col]
    assert len(after) == 1, "adoption must not duplicate the grant"
    assert after[0]["id"] == before[0]["id"], "adoption must not recreate it under a new id"
    assert after[0]["source"] == "sharepoint_acl_sync"


def test_a_wizard_grant_on_the_same_collection_is_left_alone(pg_repos):
    """The sync only ever adopts rows it owns. A wizard grant sitting on the
    same collection must keep its own source — relabelling it would move it
    from "you can change this" to "you cannot" behind the admin's back."""
    groups, grants, engine = pg_repos
    from connectors.sharepoint import acl_sync

    mirrored = groups.create(name="sp-m-" + uuid.uuid4().hex[:6], created_by="admin@x.com")
    manual = groups.create(name="sp-w-" + uuid.uuid4().hex[:6], created_by="admin@x.com")
    col = _collection(engine)

    grants.create(
        group_id=manual["id"],
        resource_type="collection",
        resource_id=col,
        assigned_by="admin@x.com",
        source="sharepoint_wizard",
    )
    acl_sync._reconcile_grants(col, [mirrored["id"]], "scope-1")

    by_group = {
        g["group_id"]: g
        for g in grants.list_all(resource_type="collection")
        if g["resource_id"] == col
    }
    assert by_group[manual["id"]]["source"] == "sharepoint_wizard", "the wizard's row is untouched"
    assert by_group[mirrored["id"]]["source"] == "sharepoint_acl_sync"


def test_the_wizards_own_grant_stays_revocable(pg_repos):
    """The reported bug, from the other side: an admin must be able to
    manage a wizard-created SharePoint collection from /admin/access."""
    from src.grant_sources import SECTION_CHANGE_HERE, describe, section_for

    assert describe("sharepoint_wizard")["revocable"] is True
    assert section_for("sharepoint_wizard") == SECTION_CHANGE_HERE
