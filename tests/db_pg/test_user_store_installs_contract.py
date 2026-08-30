"""Cross-engine contract tests for the user_store_installs repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both
backends; the same rows must come back.

The rule under test is ``list_for_user``'s visibility filter — the single
chokepoint every surface reads through (Library, /stack, marketplace browse,
and the served marketplace.zip). It is not a plain ``IN`` list: an entity kept
Private serves to its own author and to nobody else, and guardrail quarantine
writes the same ``visibility_status='hidden'`` as the Private choice, so the
branch carries a correlated ``NOT EXISTS`` over ``store_submissions``. Two
engines hand-maintaining that clause is exactly the drift this file exists to
catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

OWNER = "owner-1"
OTHER = "other-1"


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)

    def seed_group(group_id, user_ids):
        conn.execute(
            "INSERT INTO user_groups (id, name, is_system) VALUES (?, ?, FALSE)",
            [group_id, f"grp-{group_id}"],
        )
        for uid in user_ids:
            conn.execute(
                "INSERT INTO user_group_members (user_id, group_id, source) VALUES (?, ?, 'admin')",
                [uid, group_id],
            )

    return (
        UserStoreInstallsRepository(conn),
        StoreEntitiesRepository(conn),
        StoreSubmissionsRepository(conn),
        conn,
        seed_group,
    )


def _make_pg(pg_engine, monkeypatch):
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

    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository

    engine = db_pg.get_engine()

    def seed_group(group_id, user_ids):
        import sqlalchemy as sa

        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO user_groups (id, name, is_system) VALUES (:i, :n, FALSE)"),
                {"i": group_id, "n": f"grp-{group_id}"},
            )
            for uid in user_ids:
                conn.execute(
                    sa.text("INSERT INTO user_group_members (user_id, group_id, source) VALUES (:u, :g, 'admin')"),
                    {"u": uid, "g": group_id},
                )

    return (
        UserStoreInstallsPgRepository(engine),
        StoreEntitiesPgRepository(engine),
        StoreSubmissionsPgRepository(engine),
        None,
        seed_group,
    )


@pytest.fixture(params=["duckdb", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(installs, entities, submissions)`` bound to one backend."""
    if request.param == "duckdb":
        installs, entities, submissions, conn, _seed = _make_duckdb(tmp_path)
        yield installs, entities, submissions
        if conn is not None:
            conn.close()
    else:
        installs, entities, submissions, _, _seed = _make_pg(pg_engine, monkeypatch)
        yield installs, entities, submissions


@pytest.fixture(params=["duckdb", "pg"])
def repos_with_groups(request, tmp_path, pg_engine, monkeypatch):
    """``(installs, entities, seed_group)`` — the Required-tier fan-out needs
    ``user_group_members`` rows, which the plain ``repos`` fixture does not seed."""
    if request.param == "duckdb":
        installs, entities, _subs, conn, seed = _make_duckdb(tmp_path)
        yield installs, entities, seed
        if conn is not None:
            conn.close()
    else:
        installs, entities, _subs, _, seed = _make_pg(pg_engine, monkeypatch)
        yield installs, entities, seed


def _entity(entities, *, name: str, visibility_status: str, owner: str = OWNER) -> str:
    eid = f"ent-{name}"
    entities.create(
        id=eid,
        owner_user_id=owner,
        owner_username=owner,
        type="skill",
        name=name,
        description="A description long enough to look like a real one.",
        category=None,
        version="1.0.0",
        file_size=100,
        visibility_status=visibility_status,
    )
    return eid


def _reject(submissions, entity_id: str, *, status: str = "blocked_llm") -> None:
    submissions.create(
        submitter_id=OWNER,
        submitter_email="owner@x.com",
        type="skill",
        name=entity_id,
        version="1.0.0",
        status=status,
        entity_id=entity_id,
        file_size=100,
        bundle_sha256="0" * 64,
    )


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["approved", "archived"])
def test_approved_and_archived_serve_to_any_installer(repos, status):
    installs, entities, _subs = repos
    eid = _entity(entities, name=f"pub-{status}", visibility_status=status)
    installs.install(OTHER, eid)
    assert [r["id"] for r in installs.list_for_user(OTHER)] == [eid]


def test_own_hidden_serves_to_its_author(repos):
    """The Private tier: an entity kept private reaches the Stack of the person
    who authored it."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="mine", visibility_status="hidden")
    installs.install(OWNER, eid)
    assert [r["id"] for r in installs.list_for_user(OWNER)] == [eid]


def test_hidden_never_serves_to_a_third_party(repos):
    installs, entities, _subs = repos
    eid = _entity(entities, name="notyours", visibility_status="hidden")
    installs.install(OTHER, eid)
    assert installs.list_for_user(OTHER) == []


def test_a_granted_hidden_entity_serves_to_the_group(repos):
    """The read side of a `store_entity` grant. It used to be accepted and mean
    nothing — the row was written and this chokepoint served the group
    nothing — so both engines now take the granted set and both must honour
    it identically."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="granted", visibility_status="hidden")
    installs.install(OTHER, eid)
    assert installs.list_for_user(OTHER) == [], "it served without the grant, so the next line proves nothing"
    assert [r["id"] for r in installs.list_for_user(OTHER, [eid])] == [eid]


def test_a_grant_does_not_override_a_rejection(repos):
    """A grant widens who may be served; it never resurrects a bundle the
    guardrails blocked. Hand-maintained in two dialects, so it is asserted in
    both."""
    installs, entities, subs = repos
    eid = _entity(entities, name="granted-but-blocked", visibility_status="hidden")
    _reject(subs, eid)
    installs.install(OTHER, eid)
    assert installs.list_for_user(OTHER, [eid]) == []


def test_an_unrelated_grant_does_not_widen_anything(repos):
    """The granted set is matched by id, not merely counted."""
    installs, entities, _subs = repos
    mine = _entity(entities, name="wanted", visibility_status="hidden")
    other = _entity(entities, name="unrelated", visibility_status="hidden")
    installs.install(OTHER, mine)
    assert installs.list_for_user(OTHER, [other]) == []


def test_rejected_hidden_never_serves_even_to_its_author(repos):
    """Quarantine writes the same status as Private, so the blocking verdict is
    the only separator. A bundle review rejected stays out."""
    installs, entities, subs = repos
    eid = _entity(entities, name="rejected", visibility_status="hidden")
    _reject(subs, eid)
    installs.install(OWNER, eid)
    assert installs.list_for_user(OWNER) == []


def test_review_error_is_not_a_rejection(repos):
    """No verdict is not the same as a rejection — an instance without LLM
    credentials must not silently stop serving its users' Private uploads."""
    installs, entities, subs = repos
    eid = _entity(entities, name="noverdict", visibility_status="hidden")
    _reject(subs, eid, status="review_error")
    installs.install(OWNER, eid)
    assert [r["id"] for r in installs.list_for_user(OWNER)] == [eid]


def test_pending_never_serves(repos):
    """Only 'hidden' gets the owner exemption; an entity still awaiting review
    on the share path is excluded for everyone, author included."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="inreview", visibility_status="pending")
    installs.install(OWNER, eid)
    assert installs.list_for_user(OWNER) == []


def test_list_for_user_projection_matches_across_engines(repos):
    """The joined projection is what marketplace_filter builds plugin entries
    from — a column missing on one engine is a serve-time KeyError."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="shaped", visibility_status="approved")
    installs.install(OTHER, eid)
    row = installs.list_for_user(OTHER)[0]
    for key in (
        "id",
        "owner_user_id",
        "owner_username",
        "type",
        "name",
        "description",
        "version",
        "visibility_status",
        "synthetic_name",
        "installed_at",
    ):
        assert key in row, f"{key} missing from list_for_user projection"
    assert row["owner_user_id"] == OWNER
    assert row["visibility_status"] == "approved"


# ---------------------------------------------------------------------------
# Required-tier fan-out (install_for_group_members / install_required_for_user)
#
# These two materialize the Required tier: a store entity granted `required` to a
# group must end up as a real row per member, or the "locked, can't uninstall"
# state is a label with nothing behind it. They were added to both backends with
# matching SQL but no cross-engine coverage, so the two implementations' RETURN
# VALUES came from different mechanisms — DuckDB diffs `installer_count` either
# side of the INSERT, Postgres counts `RETURNING` rows — with nothing asserting
# they agree. Reached in production via `app/api/access.py`'s
# `_fanout_required_store_entity` and `src/marketplace_filter.py`.
# ---------------------------------------------------------------------------


def test_install_for_group_members_installs_once_per_member(repos_with_groups):
    installs, entities, seed_group = repos_with_groups
    eid = _entity(entities, name="req-fanout", visibility_status="approved")
    seed_group("g-req", ["u-1", "u-2", "u-3"])

    created = installs.install_for_group_members("g-req", eid)

    assert created == 3
    assert installs.installer_count(eid) == 3
    for uid in ("u-1", "u-2", "u-3"):
        assert installs.is_installed(uid, eid) is True


def test_install_for_group_members_is_idempotent(repos_with_groups):
    """Re-running the fan-out must report 0 new rows, not re-count the group.

    This is where the two engines could disagree: the DuckDB return value is a
    before/after count difference and the Postgres one is a `RETURNING` row
    count, and `ON CONFLICT DO NOTHING` makes the second call insert nothing on
    both. A regression on either side shows up here as a non-zero second call.
    """
    installs, entities, seed_group = repos_with_groups
    eid = _entity(entities, name="req-idem", visibility_status="approved")
    seed_group("g-idem", ["u-1", "u-2"])

    assert installs.install_for_group_members("g-idem", eid) == 2
    assert installs.install_for_group_members("g-idem", eid) == 0
    assert installs.installer_count(eid) == 2


def test_install_for_group_members_counts_only_new_rows(repos_with_groups):
    """One member already had it installed by hand — only the other two are new."""
    installs, entities, seed_group = repos_with_groups
    eid = _entity(entities, name="req-partial", visibility_status="approved")
    seed_group("g-partial", ["u-1", "u-2", "u-3"])
    installs.install("u-2", eid)

    assert installs.install_for_group_members("g-partial", eid) == 2
    assert installs.installer_count(eid) == 3


def test_install_for_group_members_on_an_empty_group(repos_with_groups):
    installs, entities, seed_group = repos_with_groups
    eid = _entity(entities, name="req-empty", visibility_status="approved")
    seed_group("g-empty", [])

    assert installs.install_for_group_members("g-empty", eid) == 0
    assert installs.installer_count(eid) == 0


def test_install_for_group_members_ignores_other_groups(repos_with_groups):
    """The fan-out is scoped to one group — a member of a different group must
    not be swept in, or a Required grant would install for the whole instance."""
    installs, entities, seed_group = repos_with_groups
    eid = _entity(entities, name="req-scoped", visibility_status="approved")
    seed_group("g-in", ["u-in"])
    seed_group("g-out", ["u-out"])

    assert installs.install_for_group_members("g-in", eid) == 1
    assert installs.is_installed("u-in", eid) is True
    assert installs.is_installed("u-out", eid) is False


def test_install_required_for_user_is_idempotent_and_counts_new(repos_with_groups):
    """The join-later half: a user added after the grant catches up on resolve."""
    installs, entities, _seed = repos_with_groups
    a = _entity(entities, name="req-a", visibility_status="approved")
    b = _entity(entities, name="req-b", visibility_status="approved")

    assert installs.install_required_for_user("u-late", [a, b]) == 2
    assert installs.install_required_for_user("u-late", [a, b]) == 0
    assert installs.install_required_for_user("u-late", [a, b, "ent-req-a"]) == 0
    assert installs.installer_count(a) == 1
    assert installs.installer_count(b) == 1


def test_install_required_for_user_with_no_ids(repos_with_groups):
    installs, _entities, _seed = repos_with_groups
    assert installs.install_required_for_user("u-none", []) == 0
