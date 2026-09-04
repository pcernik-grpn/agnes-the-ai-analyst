"""A revision that adds a column must survive finding it already there.

The incident this pins, the day the access stack shipped: `0096` and `0097`
add `resource_grants.source` and `.scope` with a bare `op.add_column`. The dev
database already had both — left behind by an image built from the branch the
columns were developed on, without the revisions being stamped — so `migrate`
died on `DuplicateColumn: column "source" of relation "resource_grants"
already exists`.

Boot is strict: `app`, `scheduler` and `data-migrate` wait for a successful
`migrate`, so this is not a log line. It is a 502 behind "Agnes is
upgrading…", retried every 30s, for as long as it takes a human to notice and
drop the column by hand. It lasted about two and a half hours.

Any instance that ever ran a branch build hits the same wall on upgrade, which
is a fleet-wide risk rather than a local mishap — and the end state is
identical whether the column is added or already present, so refusing to boot
over it buys nothing.

Two tests, deliberately different in kind:

* `test_the_two_columns_survive_being_added_twice` EXECUTES each revision's
  `upgrade()` against a Postgres that already has the column. It is the only
  check that proves the guard actually works.
* `test_every_resource_grants_column_add_is_existence_checked` is the ratchet:
  a NEW revision adding a column to this table with a bare `op.add_column`
  fails it. Scoped to `resource_grants` on purpose — that is where the
  branch-build overlap is real (the access stack developed both columns on a
  long-lived branch), and a repo-wide rule would be a much bigger claim than
  this incident supports.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

VERSIONS = Path("migrations/versions")
TARGET_TABLE = "resource_grants"

# The two the incident actually hit, by revision id.
INCIDENT_REVISIONS = {
    "0096_resource_grants_source": "source",
    "0097_resource_grants_scope": "scope",
}


def _upgrade_body(path: Path) -> str:
    """The revision's `upgrade()` source, or "" for a file that has none —
    `__init__.py` and any helper module living beside the revisions."""
    s = path.read_text(encoding="utf-8")
    i = s.find("def upgrade(")
    if i == -1:
        return ""
    j = s.find("\ndef ", i + 1)
    return s[i : j if j != -1 else len(s)]


@pytest.mark.parametrize("revision,column", sorted(INCIDENT_REVISIONS.items()))
def test_the_two_columns_survive_being_added_twice(pg_engine, revision, column):
    """Run the real `upgrade()` against a table that already has the column.

    Driven through Alembic's own `op` context so the revision runs exactly as
    `alembic upgrade` runs it — a hand-rolled call to the module's function
    with a bare connection would not exercise `op.get_bind()`.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = next(p for p in VERSIONS.glob(f"{revision}*.py"))
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)

    with pg_engine.begin() as conn:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {TARGET_TABLE}"))
        # The column is ALREADY here — the state a branch build leaves behind.
        conn.execute(
            sa.text(f"CREATE TABLE {TARGET_TABLE} (id VARCHAR PRIMARY KEY, {column} VARCHAR)")
        )

    with pg_engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()  # must not raise DuplicateColumn

        cols = {c["name"] for c in sa.inspect(conn).get_columns(TARGET_TABLE)}
        assert column in cols, "the column must still be there afterwards"
        # And exactly once — no shadow copy, no rename.
        assert sorted(cols) == ["id", column] or sorted(cols) == [column, "id"]


def test_a_fresh_database_still_gets_the_columns(pg_engine):
    """The guard must not turn the revision into a no-op on the case it is
    actually for: a database that does NOT have the column yet."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with pg_engine.begin() as conn:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {TARGET_TABLE}"))
        conn.execute(sa.text(f"CREATE TABLE {TARGET_TABLE} (id VARCHAR PRIMARY KEY)"))

    for revision, column in sorted(INCIDENT_REVISIONS.items()):
        path = next(p for p in VERSIONS.glob(f"{revision}*.py"))
        spec = importlib.util.spec_from_file_location(revision, path)
        assert spec
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
        with pg_engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                mod.upgrade()
            assert column in {c["name"] for c in sa.inspect(conn).get_columns(TARGET_TABLE)}


def test_every_resource_grants_column_add_is_existence_checked():
    """The ratchet. A new revision adding a column to `resource_grants` must
    check first — the branch-build overlap that caused the outage is a property
    of this table's history, and the next column developed the same way would
    reproduce it exactly."""
    # Floor, with a reason rather than a convenience: the hazard is a revision
    # some database has NOT applied yet, because that is the only one that can
    # meet a column already there. `0012` and `0013` also add columns to this
    # table with a bare `add_column`, and they are long since applied
    # everywhere — the ladder is at 0109 — so an instance cannot re-run them
    # and retro-fitting them would be churn on frozen history for zero risk.
    # 0096 is where the branch-build overlap began, and is therefore where the
    # rule starts. Lowering this floor is not a hardening; raising it past a
    # revision still in flight would be the real mistake.
    FLOOR = 96

    def _seq(name: str) -> int:
        return int(name[:4]) if name[:4].isdigit() else 0

    offenders = []
    for path in sorted(VERSIONS.glob("*.py")):
        if _seq(path.name) < FLOOR:
            continue
        body = _upgrade_body(path)
        if f'add_column(\n        "{TARGET_TABLE}"' not in body and f'add_column("{TARGET_TABLE}"' not in body:
            continue
        guarded = "get_columns(" in body or "IF NOT EXISTS" in body.upper()
        if not guarded:
            offenders.append(path.name)
    assert not offenders, (
        "these revisions add a column to resource_grants with a bare add_column, which "
        "raises DuplicateColumn on any database that already has it (an image built from "
        "the development branch) and, because boot is strict, serves 502 until someone "
        "drops the column by hand:\n  " + "\n  ".join(offenders)
    )
