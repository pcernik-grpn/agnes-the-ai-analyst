"""The reconciler that finishes what `0098_everyone_becomes_a_scope` declined.

`0098`'s step 3 is guarded in two directions and, when either fires, logs
"a later release converts the rows once …". Nothing implemented that release:
`UPDATE resource_grants SET scope` existed only inside the migration, which is
stamped and never runs again. So an instance that tripped a guard stayed
half-converted permanently — new writes using the column, the pre-0098 rows
group-shaped, nothing closing the gap. Two instances tripped a different arm
each on the day 0098 shipped.

The test that matters here is not the happy path, it is
`test_the_reconciler_is_no_looser_than_the_migration`: the guards are
re-implemented (a migration must stay frozen against code drift, so neither
side can import the other), and a reconciler with a LOOSER guard would hand
out access the migration deliberately refused.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.repositories.resource_grants_pg import ResourceGrantsPgRepository

MIGRATION = Path("migrations/versions/0098_everyone_becomes_a_scope.py")
EVERYONE = "Everyone"


# ── fixtures: the smallest schema the reconciler reads ───────────────────────


@pytest.fixture
def db(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS resource_grants, user_group_members, user_groups, users"))
        conn.execute(
            sa.text(
                "CREATE TABLE users (id VARCHAR PRIMARY KEY, email VARCHAR, "
                "kind VARCHAR NOT NULL DEFAULT 'human')"
            )
        )
        conn.execute(sa.text("CREATE TABLE user_groups (id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE NOT NULL)"))
        conn.execute(
            sa.text(
                "CREATE TABLE user_group_members (user_id VARCHAR NOT NULL, group_id VARCHAR NOT NULL, "
                "PRIMARY KEY (user_id, group_id))"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE resource_grants (id VARCHAR PRIMARY KEY, group_id VARCHAR NOT NULL, "
                "resource_type VARCHAR NOT NULL, resource_id VARCHAR NOT NULL, scope VARCHAR)"
            )
        )
    return pg_engine


def _group(conn, name: str) -> str:
    gid = uuid.uuid4().hex
    conn.execute(sa.text("INSERT INTO user_groups (id, name) VALUES (:i, :n)"), {"i": gid, "n": name})
    return gid


def _user(conn, kind: str = "human", *, in_group: str | None = None) -> str:
    uid = uuid.uuid4().hex
    conn.execute(
        sa.text("INSERT INTO users (id, email, kind) VALUES (:i, :e, :k)"),
        {"i": uid, "e": f"{uid[:8]}@example.com", "k": kind},
    )
    if in_group:
        conn.execute(
            sa.text("INSERT INTO user_group_members (user_id, group_id) VALUES (:u, :g)"),
            {"u": uid, "g": in_group},
        )
    return uid


def _grant(conn, group_id: str, rtype: str = "data_package") -> str:
    gid = uuid.uuid4().hex
    conn.execute(
        sa.text(
            "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, scope) "
            "VALUES (:i, :g, :t, :r, NULL)"
        ),
        {"i": gid, "g": group_id, "t": rtype, "r": uuid.uuid4().hex},
    )
    return gid


def _scopes(engine) -> list[str | None]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(sa.text("SELECT scope FROM resource_grants ORDER BY id"))]


# ── the guards ───────────────────────────────────────────────────────────────


def test_a_person_outside_the_group_blocks_the_conversion(db):
    """0098's widening arm. Converting would hand that person everything the
    group holds — the governing rule, and the reason the migration refused."""
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _user(conn)  # a person who is NOT a member
        _grant(conn, everyone)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()

    assert report["status"] == "blocked"
    assert report["blocked_by"] == "people_outside_group"
    assert report["people_outside_group"] == 1
    assert report["converted"] == 0
    assert _scopes(db) == [None], "a blocked run must not write"


def test_a_service_account_inside_the_group_blocks_the_conversion(db):
    """0098's narrowing arm. The scope reaches people only (#2256), so
    converting would TAKE the grants away from an account holding them today."""
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _user(conn, kind="service", in_group=everyone)
        _grant(conn, everyone)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()

    assert report["status"] == "blocked"
    assert report["blocked_by"] == "non_people_inside_group"
    assert report["non_people_inside_group"] == 1
    assert _scopes(db) == [None]


def test_both_arms_are_reported_together(db):
    """An operator fixing one arm must not be surprised by the other on the
    next run, so a blocked report always carries both counts."""
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _user(conn)
        _user(conn, kind="service", in_group=everyone)
        _grant(conn, everyone)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()
    assert report["blocked_by"] == "both"
    assert (report["people_outside_group"], report["non_people_inside_group"]) == (1, 1)


def test_a_service_account_OUTSIDE_the_group_is_not_a_refusal(db):
    """The normal state on every instance: a headless identity belongs to no
    group. Counting it as "outside" would block every instance forever."""
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _user(conn, kind="service")
        _grant(conn, everyone)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()
    assert report["status"] == "converted"
    assert report["converted"] == 1
    assert _scopes(db) == ["everyone"]


# ── the conversion ───────────────────────────────────────────────────────────


def test_a_clean_instance_converts_and_is_idempotent(db):
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _user(conn, in_group=everyone)
        _grant(conn, everyone)
        _grant(conn, everyone, "agent")

    repo = ResourceGrantsPgRepository(db)
    first = repo.reconcile_everyone_scope()
    assert (first["status"], first["converted"]) == ("converted", 2)
    assert _scopes(db) == ["everyone", "everyone"]

    second = repo.reconcile_everyone_scope()
    assert second["status"] == "nothing_to_do"
    assert second["converted"] == 0


def test_dry_run_reports_without_writing(db):
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        _grant(conn, everyone)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope(dry_run=True)
    assert report["would_convert"] == 1
    assert report["converted"] == 0
    assert report["dry_run"] is True
    assert _scopes(db) == [None], "--dry-run must not write"


def test_withheld_types_are_never_converted(db):
    """Same four types 0098 withholds, for the same reasons — a slack_channel
    grant marks a channel open, not an audience."""
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        _user(conn, in_group=everyone)
        for t in ("slack_channel", "table", "memory_domain", "memory_item"):
            _grant(conn, everyone, t)
        _grant(conn, everyone, "data_package")

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()
    assert report["converted"] == 1, "only the audience-taking type converts"
    with db.connect() as conn:
        rows = dict(
            conn.execute(sa.text("SELECT resource_type, scope FROM resource_grants")).all()  # type: ignore[arg-type]
        )
    assert rows["slack_channel"] is None and rows["table"] is None
    assert rows["data_package"] == "everyone"


def test_grants_on_other_groups_are_untouched(db):
    with db.begin() as conn:
        everyone = _group(conn, EVERYONE)
        other = _group(conn, "Analysts")
        _user(conn, in_group=everyone)
        _grant(conn, other)

    report = ResourceGrantsPgRepository(db).reconcile_everyone_scope()
    assert report["status"] == "nothing_to_do"
    assert _scopes(db) == [None]


def test_an_instance_with_no_everyone_group_is_a_no_op(db):
    with db.begin() as conn:
        _grant(conn, _group(conn, "Analysts"))
    assert ResourceGrantsPgRepository(db).reconcile_everyone_scope()["status"] == "nothing_to_do"


# ── the equivalence pin: the reason this file exists ─────────────────────────


def test_the_reconciler_is_no_looser_than_the_migration():
    """The guards are re-implemented, not shared — a migration must stay frozen
    against code drift, so it cannot import from `src/`, and this cannot import
    from a migration. That makes drift the risk worth a test: a reconciler with
    a LOOSER guard hands out access the migration refused.

    Pinned structurally, on the four things a wrong guard would get wrong: the
    widening arm's shape, the narrowing arm's shape, the human-kind predicate,
    and the withheld-type exclusion.
    """
    migration = MIGRATION.read_text(encoding="utf-8")
    reconciler = Path("src/repositories/resource_grants_pg.py").read_text(encoding="utf-8")
    body = reconciler[reconciler.index("def reconcile_everyone_scope") :]

    def norm(s: str) -> str:
        """Read SQL the way Python assembles it, not the way it is typed.

        Both sides split their statements across adjacent string literals, so
        the source carries `" … "\n  " … "` where the SQL has one space. Join
        those first, then collapse whitespace — otherwise this pin fails on
        formatting and gets "fixed" by weakening it, which is exactly the
        failure mode a drift guard must not have.
        """
        joined = re.sub(r'"\s*"', "", s)
        return re.sub(r"\s+", " ", joined)

    widening = "SELECT 1 FROM user_group_members m WHERE m.user_id = u.id AND m.group_id = :g"
    narrowing = "JOIN user_group_members m ON m.user_id = u.id AND m.group_id = :g WHERE u.kind <> :human"
    for needle in (widening, narrowing):
        assert needle in norm(migration), f"the migration no longer contains {needle!r} — re-derive this pin"
        assert needle in norm(body), f"the reconciler's guard drifted from the migration: {needle!r}"

    # The widening arm must count PEOPLE only. Counting every account would
    # block every instance that has ever minted a service account.
    assert "u.kind = :human" in norm(body), "the widening arm must be restricted to people"

    # And the conversion must exclude the same types the migration withholds.
    assert "resource_type NOT IN :withheld" in norm(body)
    assert "SCOPE_WITHHELD_TYPES" in body, "withheld types must come from src.grant_scopes, not a local copy"


def test_the_withheld_set_matches_the_migrations_frozen_copy():
    """`src.grant_scopes.SCOPE_WITHHELD_TYPES` is the live set; 0098 froze its
    own literal. They must agree, or the reconciler converts a type the
    migration deliberately left alone."""
    from src.grant_scopes import SCOPE_WITHHELD_TYPES

    frozen = re.search(r"SCOPE_WITHHELD_TYPES = \(([^)]*)\)", MIGRATION.read_text(encoding="utf-8"))
    assert frozen, "0098 no longer declares SCOPE_WITHHELD_TYPES as a literal tuple"
    assert set(re.findall(r'"([^"]+)"', frozen.group(1))) == set(SCOPE_WITHHELD_TYPES)


def test_the_duckdb_backend_fails_clean_rather_than_pretending():
    """The frozen DuckDB ladder has no `scope` column, so there is no
    half-converted state to close. Returning an empty report would read as
    "the conversion completed"; the typed error becomes a clean 501."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repository_errors import RequiresPostgresBackend

    with pytest.raises(RequiresPostgresBackend) as exc:
        ResourceGrantsRepository.reconcile_everyone_scope(object())  # type: ignore[arg-type]
    assert "everyone-scope" in exc.value.feature
