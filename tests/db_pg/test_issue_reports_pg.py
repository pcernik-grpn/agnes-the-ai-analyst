"""Issue reports + comments — repository contract, on Postgres.

PG-side by necessity, not by preference: ``issue_reports``/``issue_comments``
are Postgres-only tables (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"), so Postgres is the only backend on which any of this can run at
all. Pattern follows ``tests/db_pg/test_semantic_feedback_pg.py``: a local
``repo`` fixture runs the Alembic ladder to head against ``pg_engine`` and
hands back the repository instance directly.
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

    from src.repositories.issue_reports_pg import IssueReportsPgRepository

    return IssueReportsPgRepository(pg_engine)


def test_create_returns_open_row_with_number(repo):
    row = repo.create(
        title="Tables render raw",
        body="while streaming",
        kind="bug",
        created_by="u1",
        created_by_email="a@example.com",
        source_surface="web",
        page_url="/chat?session=abc",
        context={"app_version": "0.98.3"},
    )
    assert row["id"].startswith("iss_") and row["status"] == "open"
    assert isinstance(row["number"], int) and row["comment_count"] == 0
    assert row["context_json"] == {"app_version": "0.98.3"}


def test_numbers_are_monotonic_and_get_accepts_every_form(repo):
    a = repo.create(
        title="a",
        body=None,
        kind="bug",
        created_by="u1",
        created_by_email=None,
        source_surface="cli",
        page_url=None,
        context=None,
    )
    b = repo.create(
        title="b",
        body=None,
        kind="request",
        created_by="u1",
        created_by_email=None,
        source_surface="mcp",
        page_url=None,
        context=None,
    )
    assert b["number"] == a["number"] + 1
    assert repo.get(a["id"])["id"] == a["id"]
    assert repo.get(str(a["number"]))["id"] == a["id"]
    assert repo.get(f"#{a['number']}")["id"] == a["id"]
    assert repo.get("999999") is None and repo.get("iss_nope") is None


def test_list_for_user_is_scoped_in_sql(repo):
    mine = repo.create(
        title="mine",
        body=None,
        kind="bug",
        created_by="u1",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    repo.create(
        title="theirs",
        body=None,
        kind="bug",
        created_by="u2",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    assert [r["id"] for r in repo.list_for_user("u1")] == [mine["id"]]
    assert repo.count_for_user("u1") == 1 and repo.count_all() == 2
    assert len(repo.list_all(limit=1)) == 1


def test_comment_bumps_activity_and_count(repo):
    row = repo.create(
        title="x",
        body=None,
        kind="question",
        created_by="u1",
        created_by_email="a@example.com",
        source_surface="web",
        page_url=None,
        context=None,
    )
    c = repo.add_comment(
        row["id"], author_id="admin1", author_email="ops@example.com", author_kind="admin", body="Looking into it"
    )
    assert c["id"].startswith("isc_") and c["issue_owner_id"] == "u1"
    again = repo.get(row["id"])
    assert again["comment_count"] == 1 and again["last_activity_at"] >= row["last_activity_at"]
    assert [x["body"] for x in repo.list_comments(row["id"])] == ["Looking into it"]


def test_resolve_is_a_guarded_transition(repo):
    from src.repositories.issue_reports_pg import IssueAlreadyResolved

    row = repo.create(
        title="x",
        body=None,
        kind="bug",
        created_by="u1",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    done = repo.resolve(row["id"], resolved_by="admin1", resolution_note="fixed in 0.99.0")
    assert done["status"] == "resolved" and done["resolved_by"] == "admin1"
    with pytest.raises(IssueAlreadyResolved):
        repo.resolve(row["id"], resolved_by="admin2", resolution_note=None)
    assert repo.resolve("iss_missing", resolved_by="admin1", resolution_note=None) is None


def test_screenshot_and_webhook_marks(repo):
    row = repo.create(
        title="x",
        body=None,
        kind="bug",
        created_by="u1",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    repo.set_screenshot(row["id"], f"issues/{row['id']}/screenshot.png")
    repo.mark_webhook_delivered(row["id"])
    again = repo.get(row["id"])
    assert again["screenshot_path"].endswith("screenshot.png") and again["webhook_delivered_at"] is not None
