"""Postgres-backed tests for ``connectors.sharepoint.crawler``'s
``load_state``/``save_state`` — the per-file ``ctags``/``failed_items``/
``empty_items`` split out of ``sharepoint_connection_state.payload``
(migration ``0110_sharepoint_crawl_items``).

Measured incident this fixes: a live connection with a few hundred
thousand documents grew ``ctags`` to ~26 MB, ``empty_items`` to ~11 MB and
``failed_items`` to ~2 MB inside ONE ``sharepoint_connection_state`` row's
``payload`` — every crawl checkpoint rewrote that whole payload, and every
Postgres UPDATE produces a brand-new toasted value for a changed ``jsonb``
column (even via ``jsonb_set`` targeting one key), so every checkpoint
orphaned the previous copy's ~21 MB of TOAST chunks (~21,900 dead
tuples/minute, ~60 GB/day of table growth measured on the running
instance).

Needs a REAL Postgres engine (unlike ``tests/test_sharepoint_crawler.py``'s
``TestItemTableSplit``, which exercises the same split through
``FakeStateStore`` with ``use_pg`` forced true — no database needed there).
Runs the full Alembic ladder via ``pg_env`` so both
``sharepoint_connection_state`` and ``sharepoint_crawl_items`` exist
exactly as a real deploy would create them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()
    return pg_engine


def _hot_payload(pg_engine, connection_id: str, kind: str = "crawl"):
    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                sa.text("SELECT payload FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"),
                {"cid": connection_id, "kind": kind},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def _item_rows(pg_engine, connection_id: str, kind: str = "crawl"):
    with pg_engine.connect() as conn:
        rows = (
            conn.execute(
                sa.text(
                    "SELECT stable_id, ctag, updated_at FROM sharepoint_crawl_items "
                    "WHERE connection_id = :cid AND kind = :kind"
                ),
                {"cid": connection_id, "kind": kind},
            )
            .mappings()
            .all()
        )
    return {r["stable_id"]: (r["ctag"], r["updated_at"]) for r in rows}


# ---------------------------------------------------------------------------
# 1. The regression that names the bug: the hot blob's payload does not
#    grow with the number of known files.
# ---------------------------------------------------------------------------


def test_the_hot_blob_payload_does_not_scale_with_the_number_of_known_files(pg_env):
    from connectors.sharepoint import crawler

    state = crawler.load_state("conn-a")
    for i in range(3000):
        state["ctags"][f"graph:{i}"] = f"c{i}"
    crawler.save_state("conn-a", state)

    payload = _hot_payload(pg_env, "conn-a")
    assert payload is not None
    # The whole point: `ctags` (and its two siblings) must never be part of
    # the row a checkpoint rewrites — this is what stopped the row from
    # growing to tens of megabytes in the first place.
    assert "ctags" not in payload
    assert "failed_items" not in payload
    assert "empty_items" not in payload
    # What's left is genuinely small regardless of how many files this
    # connection has ever seen.
    assert len(json.dumps(payload)) < 500


# ---------------------------------------------------------------------------
# 2. Two consecutive checkpoints, one file changed: only that file's row
#    is written.
# ---------------------------------------------------------------------------


def test_a_later_checkpoint_only_updates_the_one_changed_files_row(pg_env):
    from connectors.sharepoint import crawler

    state = crawler.load_state("conn-a")
    for i in range(30):
        state["ctags"][f"graph:{i}"] = f"c{i}"
    crawler.save_state("conn-a", state)

    before = _item_rows(pg_env, "conn-a")
    assert len(before) == 30

    state = crawler.load_state("conn-a")
    state["ctags"]["graph:15"] = "changed"
    crawler.save_state("conn-a", state)

    after = _item_rows(pg_env, "conn-a")
    assert after["graph:15"][0] == "changed"
    assert after["graph:15"][1] > before["graph:15"][1]
    # Every OTHER row's own ctag AND `updated_at` are untouched — the
    # checkpoint cost O(1 changed file), not O(30 known files).
    for stable_id, (ctag, updated_at) in before.items():
        if stable_id == "graph:15":
            continue
        assert after[stable_id] == (ctag, updated_at)


# ---------------------------------------------------------------------------
# 3. Resume reconstructs the same effective state (behaviour preservation).
# ---------------------------------------------------------------------------


def test_resume_reconstructs_the_same_effective_state(pg_env):
    from connectors.sharepoint import crawler

    state = crawler.load_state("conn-a")
    state["delta_links"]["drive1"] = "https://example/delta?token=1"
    state["ctags"]["graph:1"] = "c1"
    state["failed_items"]["graph:2"] = {"attempts": 1, "error_class": "ingest_error"}
    state["empty_items"]["graph:3"] = {"first_seen_at": "2026-09-05T00:00:00Z"}
    crawler.save_state("conn-a", state)

    resumed = crawler.load_state("conn-a")
    assert resumed["delta_links"] == {"drive1": "https://example/delta?token=1"}
    assert resumed["ctags"] == {"graph:1": "c1"}
    assert resumed["failed_items"] == {"graph:2": {"attempts": 1, "error_class": "ingest_error"}}
    assert resumed["empty_items"] == {"graph:3": {"first_seen_at": "2026-09-05T00:00:00Z"}}


# ---------------------------------------------------------------------------
# 4. The shard seam: a child's checkpoint never lands in the connection row.
# ---------------------------------------------------------------------------


def test_a_shard_childs_items_never_land_in_the_connection_level_row(pg_env):
    from connectors.sharepoint import crawler

    parent_state = crawler.load_state("conn-a")
    parent_state["ctags"]["graph:parent"] = "p"
    crawler.save_state("conn-a", parent_state)

    child_state = crawler.load_state("conn-a", shard_key="b!drive1")
    child_state["ctags"]["graph:child"] = "c"
    crawler.save_state("conn-a", child_state, shard_key="b!drive1")

    assert set(_item_rows(pg_env, "conn-a", "crawl")) == {"graph:parent"}
    assert set(_item_rows(pg_env, "conn-a", "crawl:b!drive1")) == {"graph:child"}

    # And reading each back through the crawler stays scoped too.
    assert crawler.load_state("conn-a")["ctags"] == {"graph:parent": "p"}
    assert crawler.load_state("conn-a", shard_key="b!drive1")["ctags"] == {"graph:child": "c"}


# ---------------------------------------------------------------------------
# 5. Existing-state handling: a connection mid-crawl when this change
#    deploys (its row still embeds the pre-split collections) is not
#    orphaned.
# ---------------------------------------------------------------------------


def test_a_pre_split_embedded_blob_is_migrated_on_first_load_after_deploy(pg_env):
    from connectors.sharepoint import crawler
    from src.repositories import sharepoint_state_repo

    # Simulate a live crawl mid-flight when this change deploys: the row
    # still has the OLD shape, written directly (never through the new
    # crawler.save_state).
    sharepoint_state_repo().put(
        "conn-a",
        "crawl",
        {
            "delta_links": {"drive1": "https://example/delta?token=9"},
            "ctags": {"graph:1": "c1", "graph:2": "c2"},
            "failed_items": {"graph:3": {"attempts": 2}},
            "empty_items": {},
        },
    )

    # The crawl resumes exactly where it left off — nothing is lost.
    state = crawler.load_state("conn-a")
    assert state["delta_links"] == {"drive1": "https://example/delta?token=9"}
    assert state["ctags"] == {"graph:1": "c1", "graph:2": "c2"}
    assert state["failed_items"] == {"graph:3": {"attempts": 2}}

    # The legacy copy already landed in the per-file table on this very read.
    assert _item_rows(pg_env, "conn-a")["graph:1"][0] == "c1"

    # And the very next checkpoint stops carrying the giant embedded
    # copies in the hot blob.
    crawler.save_state("conn-a", state)
    payload = _hot_payload(pg_env, "conn-a")
    assert "ctags" not in payload
    assert "failed_items" not in payload
    assert "empty_items" not in payload
    assert payload["delta_links"] == {"drive1": "https://example/delta?token=9"}


def test_a_migrated_connection_is_never_reimported_on_a_later_load(pg_env):
    """The one-time import must not refire once the per-file table already
    has this connection's rows — a later load reads from there, not the
    (by-then-stripped) blob."""
    from connectors.sharepoint import crawler
    from src.repositories import sharepoint_state_repo

    sharepoint_state_repo().put("conn-a", "crawl", {"delta_links": {}, "ctags": {"graph:1": "c1"}})
    crawler.load_state("conn-a")  # imports once, but does not yet save

    # A concurrent/second read before any save must not re-import (which
    # would be a no-op here, but must never overwrite newer per-file data
    # with a stale blob snapshot on a real race).
    from src.repositories import sharepoint_crawl_items_repo

    sharepoint_crawl_items_repo().apply_delta(
        "conn-a",
        "crawl",
        ctags={"set": {"graph:1": "advanced"}, "removed": [], "reset": False},
        failed={"set": {}, "removed": [], "reset": False},
        empty={"set": {}, "removed": [], "reset": False},
    )

    state = crawler.load_state("conn-a")
    assert state["ctags"] == {"graph:1": "advanced"}


# ---------------------------------------------------------------------------
# Retry backlog counts (admin fleet/status) — must keep working across both
# the legacy blob shape and the split shape.
# ---------------------------------------------------------------------------


def test_admin_backlog_counts_sum_the_legacy_blob_and_the_split_table(pg_env):
    from app.api.admin_extraction import _crawl_backlog_counts
    from src.repositories import sharepoint_crawl_items_repo, sharepoint_state_repo

    # A connection not yet migrated: counts come from the legacy blob.
    sharepoint_state_repo().put(
        "conn-legacy",
        "crawl",
        {"failed_items": {"graph:1": {}, "graph:2": {}}, "empty_items": {"graph:3": {}}},
    )
    assert _crawl_backlog_counts("conn-legacy") == {"failed_items_count": 2, "empty_items_count": 1}

    # A connection already split: counts come from the per-file table, and
    # the legacy blob has nothing left to contribute.
    sharepoint_crawl_items_repo().apply_delta(
        "conn-split",
        "crawl",
        ctags={"set": {}, "removed": [], "reset": False},
        failed={"set": {"graph:9": {}}, "removed": [], "reset": False},
        empty={"set": {}, "removed": [], "reset": False},
    )
    assert _crawl_backlog_counts("conn-split") == {"failed_items_count": 1, "empty_items_count": 0}
