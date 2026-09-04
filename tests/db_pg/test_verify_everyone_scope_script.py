"""The operator's verification script, driven across a real migration.

`scripts/verify_everyone_scope_migration.py` is what the release process
actually leans on: an operator points it at a COPY of a live instance,
snapshots, upgrades, snapshots again, and refuses to ship on a diff. A
script nobody has run is not a safety net, so this drives it the same way
an operator would — real Postgres, real `alembic upgrade`, real JSON files
— and then proves it can still FAIL, because a checker that only ever
passes is indistinguishable from one that always passes.

`tests/db_pg/test_alembic_0098_everyone_scope.py` covers the migration's
own behaviour. This covers the tool.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cfg(url: str):
    from alembic.config import Config

    c = Config(str(REPO_ROOT / "alembic.ini"))
    c.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    c.attributes["sqlalchemy.url"] = url
    return c


def _seed(engine) -> None:
    """A small instance with the shapes that matter: an everyone-reaching
    flag, a group grant, an account in no group, and a channel marker."""
    with engine.begin() as conn:
        everyone = uuid.uuid4().hex
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, 'Everyone', 'System', TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": everyone},
        )
        everyone = conn.execute(
            sa.text("SELECT id FROM user_groups WHERE name = 'Everyone'")
        ).scalar_one()
        team = uuid.uuid4().hex
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, is_system, created_by) "
                "VALUES (:id, 'team', FALSE, 'admin')"
            ),
            {"id": team},
        )
        for uid in ("u1", "u2", "u3"):
            conn.execute(
                sa.text("INSERT INTO users (id, email, name) VALUES (:u, :e, :u)"),
                {"u": uid, "e": f"{uid}@example.com"},
            )
        # u3 is in nothing — the account only a scope can reach.
        for uid, gid in (("u1", everyone), ("u2", everyone), ("u2", team)):
            conn.execute(
                sa.text(
                    "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                    "VALUES (:u, :g, 'system_seed', 'test') "
                    "ON CONFLICT (user_id, group_id) DO NOTHING"
                ),
                {"u": uid, "g": gid},
            )
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, requirement) "
                "VALUES (:id, :g, 'chat', 'chat', 'available')"
            ),
            {"id": uuid.uuid4().hex, "g": everyone},
        )
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, requirement) "
                "VALUES (:id, :g, 'slack_channel', 'C0DEADBEEF', 'available')"
            ),
            {"id": uuid.uuid4().hex, "g": everyone},
        )
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_registry (id, name, url) "
                "VALUES ('mk', 'mk', 'https://example.test/mk.git')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO marketplace_plugins (marketplace_id, name, is_system, admin_disabled) "
                "VALUES ('mk', 'everybody', TRUE, FALSE)"
            )
        )


def test_the_script_reports_identical_across_a_real_upgrade(pg_engine, monkeypatch, tmp_path):
    """Snapshot, upgrade, snapshot, compare — the operator's own sequence."""
    from alembic import command

    import scripts.verify_everyone_scope_migration as verify

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    url = str(pg_engine.url)
    cfg = _cfg(url)
    command.upgrade(cfg, "0097_resource_grants_scope")
    _seed(pg_engine)

    before = verify.snapshot(url)
    assert before["side"] == "before", "the script misread which side of the migration this is"
    assert before["rules"] == "old"
    assert before["counts"]["pairs"] > 0, "nothing seeded; an identical AFTER would prove nothing"
    # The flag reached u3, who is in no group at all — that is the reach the
    # old model could only express with a flag.
    assert any(k.startswith("u3\x1f") for k in before["pairs"])

    command.upgrade(cfg, "0098_everyone_becomes_a_scope")

    after = verify.snapshot(url)
    assert after["side"] == "after", "the script did not notice the migration had run"
    assert after["rules"] == "new"

    ok, problems = verify.compare(before, after)
    assert ok, "\n".join(problems)
    assert before["open_slack_channels"] == after["open_slack_channels"] == ["C0DEADBEEF"]


def test_the_script_writes_and_reads_its_snapshot_files(pg_engine, monkeypatch, tmp_path):
    """The CLI surface, not just the functions — an operator uses files."""
    from alembic import command

    import scripts.verify_everyone_scope_migration as verify

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    url = str(pg_engine.url)
    cfg = _cfg(url)
    command.upgrade(cfg, "0097_resource_grants_scope")
    _seed(pg_engine)

    b, a = tmp_path / "before.json", tmp_path / "after.json"
    monkeypatch.setattr("sys.argv", ["v", "snapshot", "--url", url, "--out", str(b)])
    assert verify.main() == 0
    command.upgrade(cfg, "0098_everyone_becomes_a_scope")
    monkeypatch.setattr("sys.argv", ["v", "snapshot", "--url", url, "--out", str(a)])
    assert verify.main() == 0
    monkeypatch.setattr("sys.argv", ["v", "compare", str(b), str(a)])
    assert verify.main() == 0, "the script reported a diff where the migration is neutral"

    assert json.loads(b.read_text())["side"] == "before"
    assert json.loads(a.read_text())["side"] == "after"


def test_the_script_can_actually_fail(pg_engine, monkeypatch):
    """A checker that only ever passes proves nothing.

    Three ways it must refuse, each one a real mistake an operator or a
    future migration could make.
    """
    import scripts.verify_everyone_scope_migration as verify

    good_before = {
        "backend": "postgres",
        "side": "before",
        "rules": "old",
        "pairs": {"u1\x1fchat\x1fchat": "available"},
        "open_slack_channels": ["C1"],
        "counts": {"pairs": 1, "channels": 1},
    }

    # A widening — the defect the governing rule exists to catch.
    widened = dict(good_before, side="after", rules="new")
    widened["pairs"] = dict(good_before["pairs"], **{"u9\x1fchat\x1fchat": "available"})
    ok, problems = verify.compare(good_before, widened)
    assert not ok
    assert any("WIDENED" in p for p in problems)

    # A narrowing is equally unacceptable.
    ok, problems = verify.compare(good_before, {**good_before, "side": "after", "rules": "new", "pairs": {}})
    assert not ok
    assert any("NARROWED" in p for p in problems)

    # A closed Slack channel — not person-reach, so it is checked separately
    # and would otherwise slip through entirely.
    ok, problems = verify.compare(
        good_before,
        {**good_before, "side": "after", "rules": "new", "open_slack_channels": []},
    )
    assert not ok
    assert any("SLACK CHANNEL CLOSED" in p for p in problems)

    # And comparing a database to itself must not read as success.
    ok, problems = verify.compare(good_before, dict(good_before))
    assert not ok
    assert any("opposite sides" in p for p in problems)
