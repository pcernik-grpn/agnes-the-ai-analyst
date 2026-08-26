"""PG-side tests for ``ResourceSourceTagsPgRepository``.

PG-ONLY by construction (A3 PG-first ratchet — CLAUDE.md -> "Dual-backend
discipline"): ``resource_source_tags`` landed after the DuckDB app-state
backend was frozen, so there is no DuckDB sibling to parametrize against and
no cross-engine contract test to write. Shaped after
``tests/db_pg/test_mcp_sources_contract.py``'s PG half /
``tests/db_pg/test_memory_domains_pg.py`` — alembic upgrade head, then drive
the repository directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.resource_source_tags_pg import ResourceSourceTagsPgRepository

    return ResourceSourceTagsPgRepository(pg_engine)


def test_create_returns_the_row(repo):
    row = repo.create(
        resource_type="marketplace_plugin",
        resource_id="mkt-1/revenue-skill",
        source_id="conn-a",
        tagged_by="admin@test.com",
    )
    assert row["id"]
    assert row["resource_type"] == "marketplace_plugin"
    assert row["resource_id"] == "mkt-1/revenue-skill"
    assert row["source_id"] == "conn-a"
    assert row["tagged_by"] == "admin@test.com"
    assert row["tagged_at"] is not None


def test_list_for_source_is_scoped_to_that_source(repo):
    repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="a")
    repo.create(resource_type="agent", resource_id="ag-2", source_id="conn-b", tagged_by="a")

    rows = repo.list_for_source("conn-a")
    assert [r["resource_id"] for r in rows] == ["ag-1"]
    assert repo.list_for_source("conn-zzz") == []


def test_list_for_resource_returns_every_source_a_resource_is_tagged_to(repo):
    repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="a")
    repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-b", tagged_by="a")
    repo.create(resource_type="memory_domain", resource_id="md_finance", source_id="conn-a", tagged_by="a")

    rows = repo.list_for_resource("agent", "ag-1")
    assert sorted(r["source_id"] for r in rows) == ["conn-a", "conn-b"]


def test_delete_removes_only_that_tag(repo):
    keep = repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="a")
    drop = repo.create(resource_type="agent", resource_id="ag-2", source_id="conn-a", tagged_by="a")

    assert repo.delete(drop["id"]) is True
    assert [r["id"] for r in repo.list_for_source("conn-a")] == [keep["id"]]


def test_delete_of_an_unknown_id_reports_false(repo):
    assert repo.delete("rst_does_not_exist") is False


def test_the_same_resource_cannot_be_tagged_to_one_source_twice(repo):
    """The UNIQUE (resource_type, resource_id, source_id) constraint is what
    lets the coverage endpoint answer 409 instead of growing duplicate rows
    that all mean the same thing."""
    from sqlalchemy.exc import IntegrityError

    repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="a")
    with pytest.raises(IntegrityError):
        repo.create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="someone-else")


def test_the_same_resource_id_under_a_different_type_is_a_different_tag(repo):
    """``resource_id`` is only unique WITHIN a ``resource_type`` (the
    ``resource_grants`` convention), so the constraint must span both."""
    repo.create(resource_type="agent", resource_id="shared-id", source_id="conn-a", tagged_by="a")
    repo.create(resource_type="memory_domain", resource_id="shared-id", source_id="conn-a", tagged_by="a")

    assert len(repo.list_for_source("conn-a")) == 2
