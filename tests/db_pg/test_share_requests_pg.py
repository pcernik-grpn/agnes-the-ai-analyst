"""Repository-level tests for ``share_requests`` (Track C6 — agent-sharing
approval queue).

PG-only, no DuckDB half to parametrize against (A3 ratchet) — see
``docs/migrations.md`` -> "Adding a PG-only feature".
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.share_requests_pg import ShareRequestsPgRepository

    return ShareRequestsPgRepository(pg_engine)


def _seed_group(pg_engine, group_id="grp1", name="Analysts"):
    import sqlalchemy as sa

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, :name, 'test group', FALSE, 'test')"
            ),
            {"id": group_id, "name": name},
        )


def test_create_then_get(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    row = repo.create(
        resource_type="agent",
        resource_id="agent1",
        requested_group_id="grp1",
        requested_by="owner1",
    )
    assert row["status"] == "pending"
    assert row["resource_type"] == "agent"
    assert row["resource_id"] == "agent1"
    assert row["requested_group_id"] == "grp1"
    assert row["requested_by"] == "owner1"
    assert row["decided_by"] is None
    assert row["decided_at"] is None

    fetched = repo.get(row["id"])
    assert fetched["id"] == row["id"]


def test_create_is_idempotent_for_a_pending_duplicate(pg_engine):
    """A double-submitted PUT must not queue two pending rows for the same
    (resource, group)."""
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    first = repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")
    second = repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")
    assert first["id"] == second["id"]

    all_rows, total = repo.list_for_admin(status=["pending"])
    assert total == 1
    assert len(all_rows) == 1


def test_list_pending_for_resource(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine, "grp1", "A")
    _seed_group(pg_engine, "grp2", "B")
    repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")
    repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp2", requested_by="owner1")
    repo.create(resource_type="agent", resource_id="agent2", requested_group_id="grp1", requested_by="owner2")

    pending = repo.list_pending_for_resource("agent", "agent1")
    assert {p["requested_group_id"] for p in pending} == {"grp1", "grp2"}


def test_decide_approve_transitions_and_is_terminal(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    row = repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")

    decided = repo.decide(row["id"], status="approved", decided_by="admin1")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "admin1"
    assert decided["decided_at"] is not None

    # Deciding again is a no-op (guards against a double-click double-writing
    # the grant / flipping an already-approved request).
    again = repo.decide(row["id"], status="rejected", decided_by="admin2")
    assert again is None
    assert repo.get(row["id"])["status"] == "approved"


def test_decide_reject(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    row = repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")
    decided = repo.decide(row["id"], status="rejected", decided_by="admin1")
    assert decided["status"] == "rejected"


def test_decide_unknown_id_returns_none(pg_engine):
    repo = _repo(pg_engine)
    assert repo.decide("does-not-exist", status="approved", decided_by="admin1") is None


def test_decide_invalid_status_raises(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    row = repo.create(resource_type="agent", resource_id="agent1", requested_group_id="grp1", requested_by="owner1")
    try:
        repo.decide(row["id"], status="pending", decided_by="admin1")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for invalid decision status")


def test_list_for_admin_filters_by_status_and_paginates(pg_engine):
    repo = _repo(pg_engine)
    _seed_group(pg_engine)
    r1 = repo.create(resource_type="agent", resource_id="a1", requested_group_id="grp1", requested_by="o1")
    r2 = repo.create(resource_type="agent", resource_id="a2", requested_group_id="grp1", requested_by="o1")
    repo.decide(r1["id"], status="approved", decided_by="admin1")

    pending, total_pending = repo.list_for_admin(status=["pending"])
    assert total_pending == 1
    assert pending[0]["id"] == r2["id"]

    approved, total_approved = repo.list_for_admin(status=["approved"])
    assert total_approved == 1
    assert approved[0]["id"] == r1["id"]

    everything, total_all = repo.list_for_admin()
    assert total_all == 2
