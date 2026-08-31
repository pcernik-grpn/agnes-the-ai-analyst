"""Tests for the seeded ``agnes-usage`` data package.

The internal tables (``agnes_sessions`` / ``agnes_telemetry`` /
``agnes_audit``) used to be excluded from every packaging surface, which
left admins with no way to say who may query usage data. They are now
members of a seeded package so a grant can carry them.

Coverage:
- fresh boot creates the package with every registered internal table;
- a second boot is a no-op (idempotent);
- a soft-deleted package is NOT resurrected (an admin's delete is a
  decision — ``POST /restore`` is the way back);
- a member an admin removed is NOT re-added, while an internal table
  that appears for the first time IS picked up (the add-once rule);
- a repository failure is logged, never fatal to startup.
"""

from __future__ import annotations

import pytest

from connectors.internal.access import INTERNAL_TABLES, InternalTable
from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.db import _ensure_schema
from src.repositories import data_packages_repo


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """Fresh system.duckdb, no internal rows registered yet.

    ``src.db._get_data_dir`` reads ``DATA_DIR``; setting it before
    ``get_system_db()`` runs reroutes the singleton to this test's path.
    """
    data_dir = tmp_path / "agnes_data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("STATE_DIR", raising=False)

    from src.db import close_system_db, get_system_db

    close_system_db()
    conn = get_system_db()
    _ensure_schema(conn)
    yield conn
    close_system_db()


def _boot() -> None:
    """One startup pass — exactly what ``app/main.py`` does."""
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


# ---------------------------------------------------------------------------
# (a) fresh boot
# ---------------------------------------------------------------------------


def test_fresh_boot_seeds_package_with_every_internal_table(system_db):
    _boot()

    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None, "boot must create the agnes-usage package"
    assert pkg["name"] == "Agnes Usage"
    assert pkg["status"] == "prod"
    assert pkg["publisher_kind"] == "organization"
    assert pkg["created_by"] == "system_seed"

    assert _member_ids(pkg["id"]) == {t.registry_id for t in INTERNAL_TABLES}


# ---------------------------------------------------------------------------
# (b) idempotence
# ---------------------------------------------------------------------------


def test_second_boot_is_a_no_op(system_db):
    _boot()
    pkg_id = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)["id"]
    before = _member_ids(pkg_id)

    _boot()
    _boot()

    repo = data_packages_repo()
    # Still exactly one package with this slug, same id, same members.
    assert [p for p in repo.list() if p["slug"] == USAGE_PACKAGE_SLUG] != []
    assert len([p for p in repo.list() if p["slug"] == USAGE_PACKAGE_SLUG]) == 1
    assert repo.get_by_slug(USAGE_PACKAGE_SLUG)["id"] == pkg_id
    assert _member_ids(pkg_id) == before


# ---------------------------------------------------------------------------
# (c) a soft-deleted package stays deleted
# ---------------------------------------------------------------------------


def test_soft_deleted_package_is_not_resurrected(system_db):
    _boot()
    repo = data_packages_repo()
    pkg_id = repo.get_by_slug(USAGE_PACKAGE_SLUG)["id"]
    repo.delete(pkg_id)

    _boot()

    # Not restored, and no duplicate created under the same slug.
    assert repo.get_by_slug(USAGE_PACKAGE_SLUG) is None
    assert repo.get(pkg_id, include_deleted=True) is not None
    assert [p for p in repo.list() if p["slug"] == USAGE_PACKAGE_SLUG] == []


def test_get_by_slug_can_see_soft_deleted_rows(system_db):
    """The seeder needs slug resolution that spans deleted rows — without it
    it cannot tell 'never created' from 'admin deleted it'."""
    repo = data_packages_repo()
    pkg_id = repo.create(
        name="Ghost",
        slug="ghost-pkg",
        description=None,
        icon=None,
        color=None,
        created_by="test",
    )
    repo.delete(pkg_id)

    assert repo.get_by_slug("ghost-pkg") is None
    assert repo.get_by_slug("ghost-pkg", include_deleted=True)["id"] == pkg_id
    assert repo.get_by_slug("never-existed", include_deleted=True) is None


# ---------------------------------------------------------------------------
# (d) add-once per id
# ---------------------------------------------------------------------------


def test_removed_member_is_not_re_added_but_a_brand_new_table_is(system_db, monkeypatch):
    _boot()
    repo = data_packages_repo()
    pkg_id = repo.get_by_slug(USAGE_PACKAGE_SLUG)["id"]

    # An admin decides the audit log does not belong in the package.
    assert repo.remove_table(pkg_id, "agnes_audit") is True

    # A later release ships a fourth internal table.
    future = InternalTable(
        registry_id="agnes_turns",
        source_table="usage_turns",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes turns",
        description="Per-turn token usage.",
    )
    monkeypatch.setattr(
        "connectors.internal.registry.INTERNAL_TABLES",
        INTERNAL_TABLES + (future,),
    )

    _boot()

    members = _member_ids(pkg_id)
    assert "agnes_turns" in members, "a first-time internal table joins the package"
    assert "agnes_audit" not in members, "an admin's removal must stick across boots"


def test_removed_member_stays_removed_across_many_boots(system_db):
    _boot()
    repo = data_packages_repo()
    pkg_id = repo.get_by_slug(USAGE_PACKAGE_SLUG)["id"]
    repo.remove_table(pkg_id, "agnes_telemetry")

    for _ in range(3):
        _boot()

    assert "agnes_telemetry" not in _member_ids(pkg_id)


# ---------------------------------------------------------------------------
# never fatal
# ---------------------------------------------------------------------------


def test_seed_failure_is_logged_not_raised(system_db, monkeypatch, caplog):
    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr("connectors.internal.registry.data_packages_repo", _boom)

    # Must not raise — startup continues without the package.
    ensure_internal_package_seeded(newly_registered={"agnes_audit"})

    assert any("agnes-usage" in r.message or "package" in r.message.lower() for r in caplog.records)


def test_registration_reports_only_freshly_inserted_ids(system_db):
    """The add-once key: an id is 'new' exactly once — the boot that first
    inserts its ``table_registry`` row."""
    first = ensure_internal_tables_registered()
    assert first == {t.registry_id for t in INTERNAL_TABLES}

    second = ensure_internal_tables_registered()
    assert second == set()
