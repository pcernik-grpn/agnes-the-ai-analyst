"""Cross-engine contract tests for the source_connections repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.source_connections import SourceConnectionsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SourceConnectionsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.source_connections_pg import SourceConnectionsPgRepository

    return SourceConnectionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_get_roundtrip(repo):
    repo.create(
        id="c1",
        name="kbc_eu",
        source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
        token_env="KBC_EU_TOKEN",
        is_default=True,
        created_by="t@example.com",
    )
    row = repo.get("c1")
    assert row["name"] == "kbc_eu"
    assert row["config"]["stack_url"] == "https://connection.example.com"
    assert repo.get_by_name("kbc_eu")["id"] == "c1"
    assert repo.get("nope") is None


def test_list_filters_by_source_type(repo):
    repo.create(id="c1", name="kbc", source_type="keboola", config={"stack_url": "https://a"})
    repo.create(id="c2", name="bq", source_type="bigquery", config={"project": "p"})
    assert {r["id"] for r in repo.list()} == {"c1", "c2"}
    assert [r["id"] for r in repo.list(source_type="keboola")] == ["c1"]


def test_default_is_unique_per_source_type(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"}, is_default=True)
    repo.create(id="c2", name="b", source_type="keboola", config={"stack_url": "https://b"}, is_default=True)
    rows = repo.list(source_type="keboola")
    defaults = [r for r in rows if r["is_default"]]
    assert [r["id"] for r in defaults] == ["c2"]  # last set wins
    assert repo.get_default("keboola")["id"] == "c2"
    assert repo.get_default("bigquery") is None


def test_update_and_delete(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"})
    repo.update("c1", config={"stack_url": "https://b"}, token_env="X")
    assert repo.get("c1")["config"]["stack_url"] == "https://b"
    assert repo.get("c1")["token_env"] == "X"
    repo.delete("c1")
    assert repo.get("c1") is None


def test_update_renames_connection(repo):
    # Backs the "Add data source" wizard's rename-after-test-connection step
    # (#755) — the project name returned by test-connection is only known
    # once the row already exists.
    repo.create(id="c1", name="draft", source_type="keboola", config={"stack_url": "https://a"})
    repo.update("c1", name="Production")
    row = repo.get("c1")
    assert row["name"] == "Production"
    assert row["config"]["stack_url"] == "https://a"  # untouched
    assert repo.get_by_name("Production")["id"] == "c1"
    assert repo.get_by_name("draft") is None


def test_update_rename_collision_visible_to_get_by_name(repo):
    # The API layer's 409 pre-check relies on get_by_name seeing the taken
    # name identically on both engines — pin that contract here so the
    # rename flow can't diverge (e.g. transaction visibility) per backend.
    repo.create(id="c1", name="alpha", source_type="keboola", config={"stack_url": "https://a"})
    repo.create(id="c2", name="beta", source_type="keboola", config={"stack_url": "https://b"})
    taken = repo.get_by_name("alpha")
    assert taken is not None and taken["id"] == "c1"
    # rename c2 onto a free name, then confirm the old name frees up and the
    # taken one still resolves to its owner — the exact reads the 409
    # pre-check performs.
    repo.update("c2", name="gamma")
    assert repo.get_by_name("beta") is None
    assert repo.get_by_name("alpha")["id"] == "c1"
    assert repo.get_by_name("gamma")["id"] == "c2"


def test_update_promotes_default_and_demotes_siblings(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"}, is_default=True)
    repo.create(id="c2", name="b", source_type="keboola", config={"stack_url": "https://b"})
    # Promoting c2 must demote the previous default c1 (unique per source_type).
    repo.update("c2", is_default=True)
    assert repo.get("c2")["is_default"]
    assert not repo.get("c1")["is_default"]
    assert repo.get_default("keboola")["id"] == "c2"
    # Demoting c2 leaves no default for the type.
    repo.update("c2", is_default=False)
    assert not repo.get("c2")["is_default"]
    assert repo.get_default("keboola") is None


def test_config_patch_merges_top_level_keys_without_touching_others(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={"stack_url": "https://a", "keep": "me"})
    row = repo.config_patch("c1", {"acl_sync_last_run": {"ok": True}})
    assert row["id"] == "c1"
    assert row["config"]["acl_sync_last_run"] == {"ok": True}
    assert row["config"]["stack_url"] == "https://a"
    assert row["config"]["keep"] == "me"
    # Persisted, not just returned.
    assert repo.get("c1")["config"]["acl_sync_last_run"] == {"ok": True}


def test_config_patch_unknown_id_returns_none(repo):
    assert repo.config_patch("nope", {"k": "v"}) is None


def test_config_patch_preserves_concurrently_written_other_key(repo):
    # Regression for NB-1: a patch of ONE key must not clobber a DIFFERENT
    # key some other writer already committed after this caller's own last
    # read of the row (config_patch re-reads fresh rather than trusting a
    # caller-held snapshot).
    repo.create(id="c1", name="a", source_type="sharepoint", config={"scopes": []})
    stale_snapshot = repo.get("c1")  # e.g. what a long-running sync holds onto
    # A concurrent writer (e.g. the weekly sweep) commits its own key first.
    repo.config_patch("c1", {"acl_sweep_last_full": "2026-08-30T00:00:00+00:00"})
    # The sync patches its own key without ever re-reading `stale_snapshot`.
    assert "acl_sweep_last_full" not in (stale_snapshot["config"] or {})
    row = repo.config_patch("c1", {"acl_sync_last_run": {"ok": True}})
    assert row["config"]["acl_sweep_last_full"] == "2026-08-30T00:00:00+00:00"
    assert row["config"]["acl_sync_last_run"] == {"ok": True}
