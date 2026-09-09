"""Cross-engine contract tests for the data_apps repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_memory_domains_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.data_apps import DataAppsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return DataAppsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
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

    from src.repositories.data_apps_pg import DataAppsPgRepository

    return DataAppsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a data_apps repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


@pytest.fixture
def backend(request):
    return request.node.callspec.params["repo"]


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_create_then_get_consistent(repo):
    aid = repo.create(slug="sales-dash", name="Sales dashboard", owner_user_id="u1")
    row = repo.get(aid)
    assert row is not None
    assert aid.startswith("app_")
    assert row["id"] == aid
    assert row["slug"] == "sales-dash"
    assert row["name"] == "Sales dashboard"
    assert row["owner_user_id"] == "u1"
    assert row["state"] == "created"
    assert row["repo_mode"] == "internal"
    assert row["sleep_mode"] == "recreate"
    assert row["idle_timeout_s"] == 1800


def test_get_by_slug_consistent(repo):
    aid = repo.create(slug="x", name="X", owner_user_id="u1")
    found = repo.get_by_slug("x")
    assert found is not None
    assert found["id"] == aid
    assert repo.get_by_slug("nope") is None


def test_slug_unique_raises(repo):
    repo.create(slug="dup", name="A", owner_user_id="u1")
    with pytest.raises((duckdb.ConstraintException, sa.exc.IntegrityError)):
        repo.create(slug="dup", name="B", owner_user_id="u2")


def test_list_filters_by_owner_and_state(repo):
    a = repo.create(slug="a", name="A", owner_user_id="u1")
    repo.create(slug="b", name="B", owner_user_id="u2")
    repo.set_state(a, "running")

    by_owner = repo.list(owner_user_id="u1")
    assert {r["id"] for r in by_owner} == {a}

    by_state = repo.list(state="running")
    assert {r["id"] for r in by_state} == {a}

    assert len(repo.list(limit=1000)) == 2


def test_set_state_and_record_deploy(repo):
    aid = repo.create(slug="s", name="S", owner_user_id="u1")
    repo.set_state(aid, "deploying", detail="building image")
    row = repo.get(aid)
    assert row["state"] == "deploying"
    assert row["state_detail"] == "building image"

    repo.record_deploy(aid, "abc123")
    row = repo.get(aid)
    assert row["deployed_sha"] == "abc123"
    assert row["last_deploy_at"] is not None


def test_touch_last_request(repo):
    aid = repo.create(slug="t", name="T", owner_user_id="u1")
    assert repo.get(aid)["last_request_at"] is None
    repo.touch_last_request(aid)
    assert repo.get(aid)["last_request_at"] is not None


def test_list_idle_consistent(repo, backend):
    aid = repo.create(slug="i", name="I", owner_user_id="u1")
    repo.set_state(aid, "running")

    if backend == "duckdb":
        repo.conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [aid],
        )
    else:
        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_apps SET last_request_at = now() - INTERVAL '2 hours' WHERE id = :id"),
                {"id": aid},
            )

    assert [r["id"] for r in repo.list_idle(older_than_s=3600)] == [aid]
    assert repo.list_idle(older_than_s=3600 * 3) == []


def test_update_whitelist(repo):
    aid = repo.create(slug="w", name="W", owner_user_id="u1")
    assert repo.update(aid, mem_limit="2g", service_token_id="t1") is True
    row = repo.get(aid)
    assert row["mem_limit"] == "2g"
    assert row["service_token_id"] == "t1"

    with pytest.raises(ValueError):
        repo.update(aid, state="running")


def test_delete_round_trip(repo):
    aid = repo.create(slug="ghost", name="Ghost", owner_user_id="u1")
    assert repo.delete(aid) is True
    assert repo.get(aid) is None
    assert repo.delete(aid) is False


def test_draft_lifecycle(repo):
    p = repo.create(slug="cp", name="CP", owner_user_id="u1")
    d = repo.create_draft(parent_app_id=p, slug="cp--i", branch="i", owner_user_id="u1")
    got = repo.get(d)
    assert bool(got["is_draft"]) is True  # duckdb bool vs pg bool
    assert got["parent_app_id"] == p
    assert got["draft_branch"] == "i"
    assert [r["id"] for r in repo.list_drafts(p)] == [d]
    assert "cp--i" not in {r["slug"] for r in repo.list(include_drafts=False)}


# ---------------------------------------------------------------------------
# linked (externally-hosted) data apps — v108
# ---------------------------------------------------------------------------


def test_upsert_linked_insert_then_update(repo):
    row1 = repo.upsert_linked(
        slug="kbc-sales",
        source_ref="conn1:app1",
        name="Sales",
        description="orig",
        external_url="https://example.com/app1",
    )
    assert row1["repo_mode"] == "linked"
    assert bool(row1["managed"]) is True
    assert row1["external_url"] == "https://example.com/app1"
    assert row1["source_ref"] == "conn1:app1"
    assert row1["state"] == "linked"

    # second upsert with the same source_ref updates in place (no new row)
    row2 = repo.upsert_linked(
        slug="kbc-sales",
        source_ref="conn1:app1",
        name="Sales v2",
        description="new",
        external_url="https://example.com/app1b",
    )
    assert row2["id"] == row1["id"]
    assert row2["name"] == "Sales v2"
    assert row2["external_url"] == "https://example.com/app1b"
    assert len(repo.list_linked()) == 1


def test_description_override_survives_resync(repo):
    repo.upsert_linked(
        slug="kbc-a",
        source_ref="c:a",
        name="A",
        description="synced",
        external_url="https://example.com/a",
    )
    assert repo.set_description_override("kbc-a", "admin desc") is True
    row = repo.get_by_slug("kbc-a")
    assert repo.effective_description(row) == "admin desc"

    # a re-sync changes the synced description but the admin override still wins
    repo.upsert_linked(
        slug="kbc-a",
        source_ref="c:a",
        name="A",
        description="synced-2",
        external_url="https://example.com/a2",
    )
    row = repo.get_by_slug("kbc-a")
    assert row["description"] == "synced-2"
    assert row["description_override"] == "admin desc"
    assert repo.effective_description(row) == "admin desc"


def test_effective_description_falls_back_to_synced(repo):
    repo.upsert_linked(
        slug="kbc-b",
        source_ref="c:b",
        name="B",
        description="synced only",
        external_url="https://example.com/b",
    )
    row = repo.get_by_slug("kbc-b")
    assert repo.effective_description(row) == "synced only"


def test_soft_delete_missing_linked_scoped_per_source(repo):
    repo.upsert_linked(
        slug="a1", source_ref="conn1:a1", name="A1", description="", external_url="https://example.com/a1"
    )
    repo.upsert_linked(
        slug="a2", source_ref="conn1:a2", name="A2", description="", external_url="https://example.com/a2"
    )
    repo.upsert_linked(
        slug="b1", source_ref="conn2:b1", name="B1", description="", external_url="https://example.com/b1"
    )

    # conn1 reconcile keeps only a1 → a2 hidden; conn2's b1 untouched
    hidden = repo.soft_delete_missing_linked(source_ref_prefix="conn1:", keep_source_refs=["conn1:a1"])
    assert hidden == 1

    active = {r["slug"] for r in repo.list_linked()}
    assert active == {"a1", "b1"}

    all_including = {r["slug"] for r in repo.list_linked(include_hidden=True)}
    assert all_including == {"a1", "a2", "b1"}


def test_reappearing_linked_app_relinks_losslessly(repo):
    repo.upsert_linked(
        slug="r1", source_ref="conn1:r1", name="R1", description="", external_url="https://example.com/r1"
    )
    repo.set_description_override("r1", "kept")
    repo.soft_delete_missing_linked(source_ref_prefix="conn1:", keep_source_refs=[])
    assert {r["slug"] for r in repo.list_linked()} == set()

    # app reappears in Keboola → re-upsert reactivates the SAME row + keeps override
    row = repo.upsert_linked(
        slug="r1", source_ref="conn1:r1", name="R1", description="back", external_url="https://example.com/r1"
    )
    assert row["state"] == "linked"
    assert row["description_override"] == "kept"
    assert {r["slug"] for r in repo.list_linked()} == {"r1"}


def test_data_identity_default_and_set(repo, backend):
    """`data_identity` is a Postgres-only column (A3: revision 0113, no DuckDB
    ladder step). Both backends READ as 'owner' by default through the
    shared helper; only Postgres can WRITE it, DuckDB refuses typed."""
    from src.data_apps.identity import data_identity_of
    from src.repository_errors import RequiresPostgresBackend

    repo.create(slug="ident", name="I", owner_user_id="u1")
    assert data_identity_of(repo.get_by_slug("ident")) == "owner"

    if backend == "pg":
        assert repo.get_by_slug("ident")["data_identity"] == "owner"  # the column itself, with its default
        assert repo.set_data_identity("ident", "viewer") is True
        assert repo.get_by_slug("ident")["data_identity"] == "viewer"
        assert data_identity_of(repo.get_by_slug("ident")) == "viewer"
        assert repo.set_data_identity("nope", "viewer") is False
        with pytest.raises(ValueError):
            repo.set_data_identity("ident", "everyone")
    else:
        assert "data_identity" not in repo.get_by_slug("ident")
        with pytest.raises(RequiresPostgresBackend) as exc:
            repo.set_data_identity("ident", "viewer")
        assert exc.value.feature == "data_app.data_identity"
