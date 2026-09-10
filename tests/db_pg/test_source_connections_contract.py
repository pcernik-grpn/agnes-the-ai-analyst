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


# ---------------------------------------------------------------------------
# merge_extraction — the NESTED counterpart to config_patch, atomically
# merging into config.extraction itself rather than a top-level sibling of
# it (2026-09-07 finding: _record_extraction_dispatch's own separate
# get()-then-config_patch() pair still left a window for a concurrent
# request_stop to commit a stop_requested_at that got silently overwritten).
# ---------------------------------------------------------------------------


def test_merge_extraction_merges_into_the_nested_sub_object(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={"tenant_id": "t", "extraction": {}})

    row = repo.merge_extraction("c1", {"last_run_at": "t1", "last_job_id": "job-1"})

    assert row["config"]["extraction"]["last_run_at"] == "t1"
    assert row["config"]["extraction"]["last_job_id"] == "job-1"
    assert row["config"]["tenant_id"] == "t"
    # Persisted, not just returned.
    assert repo.get("c1")["config"]["extraction"]["last_job_id"] == "job-1"


def test_merge_extraction_preserves_a_flag_committed_just_before_by_a_different_writer(repo):
    # The exact race the finding names: `request_stop` (a plain
    # `config_patch` call on the SAME nested sub-object) commits its flag
    # AFTER whatever the caller of `merge_extraction` last read, but
    # BEFORE this call — `merge_extraction` must still see it, since it
    # re-reads fresh under its own transaction rather than trusting a
    # snapshot the caller might be holding.
    repo.create(id="c1", name="a", source_type="sharepoint", config={"extraction": {}})
    repo.config_patch("c1", {"extraction": {"stop_requested_at": "fresh"}})

    row = repo.merge_extraction("c1", {"last_run_at": "t1", "last_job_id": "job-1"})

    assert row["config"]["extraction"]["stop_requested_at"] == "fresh"
    assert row["config"]["extraction"]["last_run_at"] == "t1"
    assert row["config"]["extraction"]["last_job_id"] == "job-1"


def test_merge_extraction_preserves_other_extraction_siblings(repo):
    repo.create(
        id="c1",
        name="a",
        source_type="sharepoint",
        config={"extraction": {"facts": {"retry_mode": "off"}, "crawl": {"min_modified": "2023-12-31"}}},
    )

    row = repo.merge_extraction("c1", {"last_run_at": "t1", "last_job_id": "job-1"})

    ext = row["config"]["extraction"]
    assert ext["facts"] == {"retry_mode": "off"}
    assert ext["crawl"] == {"min_modified": "2023-12-31"}


def test_merge_extraction_unknown_id_returns_none(repo):
    assert repo.merge_extraction("nope", {"last_run_at": "t1"}) is None


# ---------------------------------------------------------------------------
# clear_stop_requested_if_unchanged — the compare-and-delete counterpart to
# connectors.sharepoint.crawler.request_stop's write (2026-09-06 incident:
# `_clear_stale_stop`'s own read-then-config_patch could clobber a FRESH
# stop request committed in between).
# ---------------------------------------------------------------------------


def test_clear_stop_requested_if_unchanged_clears_a_matching_flag(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={"extraction": {"stop_requested_at": "t1"}})

    cleared = repo.clear_stop_requested_if_unchanged("c1", "t1")

    assert cleared is True
    assert "stop_requested_at" not in repo.get("c1")["config"]["extraction"]


def test_clear_stop_requested_if_unchanged_preserves_a_fresher_flag(repo):
    # The exact race the finding names: something else (`request_stop`)
    # commits a NEW flag after the caller read "t1" but before this call —
    # the stale "t1" snapshot must never win over the current "t2".
    repo.create(id="c1", name="a", source_type="sharepoint", config={"extraction": {"stop_requested_at": "t1"}})
    repo.config_patch("c1", {"extraction": {"stop_requested_at": "t2"}})

    cleared = repo.clear_stop_requested_if_unchanged("c1", "t1")

    assert cleared is False
    assert repo.get("c1")["config"]["extraction"]["stop_requested_at"] == "t2"


def test_clear_stop_requested_if_unchanged_preserves_sibling_extraction_keys(repo):
    repo.create(
        id="c1",
        name="a",
        source_type="sharepoint",
        config={"extraction": {"stop_requested_at": "t1", "last_run_at": "t0"}},
    )

    repo.clear_stop_requested_if_unchanged("c1", "t1")

    extraction = repo.get("c1")["config"]["extraction"]
    assert "stop_requested_at" not in extraction
    assert extraction["last_run_at"] == "t0"


def test_clear_stop_requested_if_unchanged_leaves_no_stop_target_behind(repo):
    """2026-09-08 review finding: the clear dropped the timestamp but left
    `stop_job_id` — the id of the job the stop was AIMED at — behind
    indefinitely, so the persisted `config.extraction` went on naming a stop
    target for a stop that is no longer requested. Nothing reads it once the
    timestamp is gone, but an operator (or the next reader of this row)
    cannot tell that from a live stop."""
    repo.create(
        id="c1",
        name="a",
        source_type="sharepoint",
        config={"extraction": {"stop_requested_at": "t1", "stop_job_id": "job1", "last_run_at": "t0"}},
    )

    assert repo.clear_stop_requested_if_unchanged("c1", "t1") is True

    extraction = repo.get("c1")["config"]["extraction"]
    assert "stop_requested_at" not in extraction
    assert "stop_job_id" not in extraction
    # Still surgical: only the stop's own two keys go.
    assert extraction["last_run_at"] == "t0"


def test_clear_stop_requested_if_unchanged_keeps_the_stop_target_when_it_refuses(repo):
    """The other half: a REFUSED clear must leave the flag whole. Dropping
    the job id while the fresher timestamp survives would strip a live
    stop's attribution and make `_clear_stale_stop`'s next decision about
    it unanswerable."""
    repo.create(
        id="c1",
        name="a",
        source_type="sharepoint",
        config={"extraction": {"stop_requested_at": "t1", "stop_job_id": "job1"}},
    )
    repo.config_patch("c1", {"extraction": {"stop_requested_at": "t2", "stop_job_id": "job2"}})

    assert repo.clear_stop_requested_if_unchanged("c1", "t1") is False

    extraction = repo.get("c1")["config"]["extraction"]
    assert extraction["stop_requested_at"] == "t2"
    assert extraction["stop_job_id"] == "job2"


def test_clear_stop_requested_if_unchanged_is_a_noop_when_nothing_is_set(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={})

    assert repo.clear_stop_requested_if_unchanged("c1", "t1") is False


def test_clear_stop_requested_if_unchanged_unknown_id_returns_false(repo):
    assert repo.clear_stop_requested_if_unchanged("nope", "t1") is False

# ---------------------------------------------------------------------------
# claim_run_generation — the monotonic per-connection extraction-ownership
# counter (issue #2333). A cancel force-finalizes the owning job row and
# closes `extraction_runs` immediately, but cannot kill the handler thread;
# this counter is what makes "you are superseded" a signal that handler must
# see and a retrigger cannot clear. It must behave identically on both
# app-state backends, because a DuckDB-backed instance always takes the
# INLINE crawl path — exactly the path a zombie handler outlives its job on.
# ---------------------------------------------------------------------------


def test_claim_run_generation_starts_at_one_and_is_strictly_monotonic(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={})

    claimed = [repo.claim_run_generation("c1") for _ in range(3)]

    assert claimed == [1, 2, 3]
    assert repo.get("c1")["config"]["extraction"]["run_generation"] == 3


def test_claim_run_generation_continues_from_the_persisted_value(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={"extraction": {"run_generation": 41}})

    assert repo.claim_run_generation("c1") == 42


def test_claim_run_generation_preserves_sibling_extraction_keys(repo):
    """It shares `config.extraction` with the stop flag and the dispatch
    bookkeeping — a claim must never wipe either."""
    repo.create(
        id="c1",
        name="a",
        source_type="sharepoint",
        config={"extraction": {"stop_requested_at": "t1", "stop_job_id": "job1", "last_run_at": "t0"}},
    )

    repo.claim_run_generation("c1")

    extraction = repo.get("c1")["config"]["extraction"]
    assert extraction["run_generation"] == 1
    assert extraction["stop_requested_at"] == "t1"
    assert extraction["stop_job_id"] == "job1"
    assert extraction["last_run_at"] == "t0"


def test_claim_run_generation_preserves_config_siblings_outside_extraction(repo):
    repo.create(id="c1", name="a", source_type="sharepoint", config={"tenant_id": "t", "scopes": [{"id": "s"}]})

    repo.claim_run_generation("c1")

    config = repo.get("c1")["config"]
    assert config["tenant_id"] == "t"
    assert config["scopes"] == [{"id": "s"}]


def test_claim_run_generation_treats_a_corrupt_counter_as_zero(repo):
    """A hand-edited or otherwise unparseable counter must not make every
    future claim raise — the crawl checks it at every checkpoint."""
    repo.create(id="c1", name="a", source_type="sharepoint", config={"extraction": {"run_generation": "banana"}})

    assert repo.claim_run_generation("c1") == 1


def test_claim_run_generation_unknown_id_returns_none(repo):
    assert repo.claim_run_generation("nope") is None
