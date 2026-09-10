# Issue Reporting Step 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any signed-in user report a problem from every Agnes surface with the page/session/version context attached automatically, keep the record in the instance, and mirror a text summary to the operator's chat webhook.

**Architecture:** One Postgres-only table pair (`issue_reports`, `issue_comments`) behind a PG-only repository, served by `app/api/issues.py`; three thin clients (web dialog, `agnes issue …`, MCP `report_issue` family) call that API; the two tables are also exposed as `agnes_issues` / `agnes_issue_comments` internal tables so any agent can query them with the per-user row filter; a `BackgroundTasks` notifier posts `{"text": …}` to `issues.webhook_url`.

**Tech Stack:** FastAPI, SQLAlchemy + Alembic (PG-only), Typer CLI, FastMCP (HTTP foundation + stdio), Jinja2 + vanilla JS, vendored `html2canvas` 1.4.1.

**Spec:** `docs/superpowers/specs/2026-09-09-issue-reporting-step1-design.md`

## Global Constraints

- PG-first ratchet (A3): no `src/db.py` step, no DuckDB repository, repo registered `PG`-only; every route resolves the repo as a FastAPI dependency so DuckDB answers the typed `501 requires_postgres_backend`.
- Every new `/api/*` route needs a CLI command and an MCP tool (`tests/test_documentation_api_triple_surface.py::_COHORT`) or a documented `_EXEMPT` reason, plus a line in `docs/api-reference.md`.
- Every mutating route/tool/GET declares audit posture (`src/audit_posture.py`); every action string is in `src/audit_events.py::CATALOG`; writes go through `src.audit_helpers.log_safe`.
- Vendor-neutral copy: no customer names, hostnames or project ids anywhere (code, tests, docs, commits).
- Web: `--ds-*` tokens only, no raw hex, page CSS never inline in the body; CSRF for `/api/**` is the Origin gate (no token needed).
- Command UX: `--json`, `--limit` with disclosed truncation, no new boolean scope flags, "not found" hints via `cli/query_hints.py`.
- Kinds: `bug` · `wrong_answer` · `request` · `question` · `other`. Statuses: `open` · `resolved`. Surfaces: `web` · `cli` · `mcp`.
- Ids: issues `iss_<16 hex>`, comments `isc_<16 hex>`; every client also accepts the bare number (`42`, `#42`).
- Caps at the API: title 200, body 8 000, page_url 2 000, context 32 KB with each string value ≤ 300 chars, screenshot 3 MiB PNG only.
- Run tests as `.venv/bin/python -m pytest … -q` from the worktree root (the package is a non-editable wheel; cwd-on-path imports the worktree). Locally run only `--lane impacted` and the guards you touch; the full suite is CI's.
- Commit per task with a clean message, no AI attribution in the body except the mandated `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer. Never `git add -A`; stage explicit paths. Do NOT edit `CHANGELOG.md`; the fragment `changelog.d/issue-reporting-step1.md` is written once at integration.

---

### Task 1: Storage, internal tables, operator notifier

**Files:**
- Create: `src/models/issue_reports.py`
- Create: `migrations/versions/0119_issue_reports.py`
- Modify: `src/models/__init__.py:90-94` (alphabetized import block)
- Create: `src/repositories/issue_reports_pg.py`
- Modify: `src/repositories/__init__.py:696` (`_REGISTRY`) and `:1213` (factory functions)
- Modify: `connectors/internal/access.py:83` (`INTERNAL_TABLES`)
- Modify: `connectors/internal/registry.py:39` (`PG_ONLY_INTERNAL_TABLE_IDS`) and `:63-102` (package prose)
- Create: `app/services/issue_notifier.py`
- Modify: `config/instance.yaml.example:649-656` (new `issues:` block next to operator alerting)
- Test: `tests/db_pg/test_issue_reports_pg.py`, `tests/db_pg/test_agnes_issues_internal_table_pg.py`, `tests/test_issue_notifier.py`, `tests/test_internal_table_descriptions.py` (existing, must stay green)

**Interfaces:**
- Consumes: `src.db_pg.Base`; `connectors.internal.access.InternalTable`; `services.telegram_bot.sender.post_webhook(url, payload, *, timeout=10.0) -> bool`; `app.instance_config.get_value(section, key, default)`.
- Produces:
  - `src.repositories.issue_reports_repo() -> IssueReportsPgRepository` with methods
    `create(*, title, body, kind, created_by, created_by_email, source_surface, page_url, context) -> dict`,
    `get(issue_ref: str) -> dict | None` (accepts `iss_…`, `42`, `#42`),
    `list_for_user(user_id, *, status=None, limit=50) -> list[dict]`,
    `list_all(*, status=None, limit=100) -> list[dict]`,
    `count_for_user(user_id, *, status=None) -> int`, `count_all(*, status=None) -> int`,
    `add_comment(issue_id, *, author_id, author_email, author_kind, body) -> dict`,
    `list_comments(issue_id) -> list[dict]`,
    `resolve(issue_id, *, resolved_by, resolution_note) -> dict | None` (None when not found; raises `IssueAlreadyResolved` when already resolved),
    `set_screenshot(issue_id, relative_path) -> None`, `mark_webhook_delivered(issue_id) -> None`.
    Every row dict carries `comment_count: int`.
  - `src.models.issue_reports.ISSUE_KINDS`, `ISSUE_STATUSES`, `ISSUE_SURFACES` tuples; `IssueAlreadyResolved` exception in the repo module.
  - `app.services.issue_notifier.notify_issue_filed(row: dict, *, public_base_url: str) -> bool` and `issues_webhook_url() -> str`.
  - Internal tables `agnes_issues` and `agnes_issue_comments`.

- [ ] **Step 1: Write the model**

`src/models/issue_reports.py`:

```python
"""SQLAlchemy models behind issue reporting (step 1).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)" and the design
``docs/superpowers/specs/2026-09-09-issue-reporting-step1-design.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Sequence, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

ISSUE_KINDS: tuple[str, ...] = ("bug", "wrong_answer", "request", "question", "other")
ISSUE_STATUSES: tuple[str, ...] = ("open", "resolved")
ISSUE_SURFACES: tuple[str, ...] = ("web", "cli", "mcp")
COMMENT_AUTHOR_KINDS: tuple[str, ...] = ("reporter", "admin")

ISSUE_NUMBER_SEQ = Sequence("issue_reports_number_seq")


class IssueReport(Base):
    """One report — a bug, a wrong answer, a missing thing, or "I don't get it".

    ``created_by`` is the user id (not the email): the internal-table row
    filter (``connectors/internal/access.py``) keys ``filter_kind='user_id'``
    on ``user["id"]``. ``number`` is the human id (``#42``); every client
    accepts it interchangeably with ``id``.
    """

    __tablename__ = "issue_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    number: Mapped[int] = mapped_column(
        Integer, ISSUE_NUMBER_SEQ, nullable=False, unique=True, server_default=ISSUE_NUMBER_SEQ.next_value()
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'bug'"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'open'"))
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_by_email: Mapped[str | None] = mapped_column(String, nullable=True)
    source_surface: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'web'"))
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_issue_reports_status", "status"),
        Index("idx_issue_reports_created_by", "created_by"),
    )


class IssueComment(Base):
    """A public note on an issue by its reporter or an admin.

    ``issue_owner_id`` is denormalized from ``issue_reports.created_by`` so
    the internal table ``agnes_issue_comments`` can filter rows per user
    without a join — a reporter must see an admin's reply on THEIR issue.
    """

    __tablename__ = "issue_comments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    issue_id: Mapped[str] = mapped_column(String, ForeignKey("issue_reports.id", ondelete="CASCADE"), nullable=False)
    issue_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_email: Mapped[str | None] = mapped_column(String, nullable=True)
    author_kind: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        Index("idx_issue_comments_issue_created", "issue_id", "created_at"),
        Index("idx_issue_comments_owner", "issue_owner_id"),
    )
```

Add to `src/models/__init__.py` in the alphabetized block (between `SemanticHealthMute`-ish neighbours, keep the order the file uses):

```python
from src.models.issue_reports import IssueComment, IssueReport
```

- [ ] **Step 2: Write the migration**

Check the head first: `ls migrations/versions | tail -3` — if a `0114_*` already exists, take the next number and revise the newest head. `migrations/versions/0119_issue_reports.py`:

```python
"""issue_reports + issue_comments (issue reporting, step 1)

PG-ONLY (A3 PG-first ratchet): this table pair landed after the DuckDB
app-state backend was frozen, so there is no ``src/db.py`` ladder step and
no DuckDB repository. ``SCHEMA_VERSION`` does not move.

Revision ID: 0119_issue_reports
Revises: 0113_data_apps_data_identity
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0119_issue_reports"
down_revision = "0113_data_apps_data_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("issue_reports_number_seq"), if_not_exists=True))
    op.create_table(
        "issue_reports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("number", sa.Integer(), nullable=False, server_default=sa.text("nextval('issue_reports_number_seq')")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default=sa.text("'bug'")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_by_email", sa.String(), nullable=True),
        sa.Column("source_surface", sa.String(), nullable=False, server_default=sa.text("'web'")),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("screenshot_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("webhook_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("number", name="uq_issue_reports_number"),
    )
    op.create_index("idx_issue_reports_status", "issue_reports", ["status"])
    op.create_index("idx_issue_reports_created_by", "issue_reports", ["created_by"])
    op.create_table(
        "issue_comments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issue_id", sa.String(), sa.ForeignKey("issue_reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_owner_id", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("author_email", sa.String(), nullable=True),
        sa.Column("author_kind", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_issue_comments_issue_created", "issue_comments", ["issue_id", "created_at"])
    op.create_index("idx_issue_comments_owner", "issue_comments", ["issue_owner_id"])


def downgrade() -> None:
    op.drop_index("idx_issue_comments_owner", table_name="issue_comments")
    op.drop_index("idx_issue_comments_issue_created", table_name="issue_comments")
    op.drop_table("issue_comments")
    op.drop_index("idx_issue_reports_created_by", table_name="issue_reports")
    op.drop_index("idx_issue_reports_status", table_name="issue_reports")
    op.drop_table("issue_reports")
    op.execute(sa.schema.DropSequence(sa.Sequence("issue_reports_number_seq"), if_exists=True))
```

Run: `.venv/bin/python -m pytest tests/db_pg/test_alembic_skeleton.py tests/db_pg/test_alembic_roundtrip.py -q`
Expected: PASS (roundtrip, drift and pairwise). If the drift test reports the model's `server_default` for `number` differs from the migration's, align the model to `server_default=sa.text("nextval('issue_reports_number_seq')")` — the migration is the source of truth for DDL text.

- [ ] **Step 3: Write the failing repo tests**

`tests/db_pg/test_issue_reports_pg.py` — copy the fixture usage of `tests/db_pg/test_semantic_feedback_pg.py` (read it first; it shows how the PG engine and `issue_reports_repo()` resolve under the PG-pinned backend). Cases:

```python
def test_create_returns_open_row_with_number(pg_repo_env):
    from src.repositories import issue_reports_repo
    repo = issue_reports_repo()
    row = repo.create(title="Tables render raw", body="while streaming", kind="bug",
                      created_by="u1", created_by_email="a@example.com",
                      source_surface="web", page_url="/chat?session=abc", context={"app_version": "0.98.3"})
    assert row["id"].startswith("iss_") and row["status"] == "open"
    assert isinstance(row["number"], int) and row["comment_count"] == 0
    assert row["context_json"] == {"app_version": "0.98.3"}


def test_numbers_are_monotonic_and_get_accepts_every_form(pg_repo_env):
    repo = issue_reports_repo()
    a = repo.create(title="a", body=None, kind="bug", created_by="u1", created_by_email=None, source_surface="cli", page_url=None, context=None)
    b = repo.create(title="b", body=None, kind="request", created_by="u1", created_by_email=None, source_surface="mcp", page_url=None, context=None)
    assert b["number"] == a["number"] + 1
    assert repo.get(a["id"])["id"] == a["id"]
    assert repo.get(str(a["number"]))["id"] == a["id"]
    assert repo.get(f"#{a['number']}")["id"] == a["id"]
    assert repo.get("999999") is None and repo.get("iss_nope") is None


def test_list_for_user_is_scoped_in_sql(pg_repo_env):
    repo = issue_reports_repo()
    mine = repo.create(title="mine", body=None, kind="bug", created_by="u1", created_by_email=None, source_surface="web", page_url=None, context=None)
    repo.create(title="theirs", body=None, kind="bug", created_by="u2", created_by_email=None, source_surface="web", page_url=None, context=None)
    assert [r["id"] for r in repo.list_for_user("u1")] == [mine["id"]]
    assert repo.count_for_user("u1") == 1 and repo.count_all() == 2
    assert len(repo.list_all(limit=1)) == 1


def test_comment_bumps_activity_and_count(pg_repo_env):
    repo = issue_reports_repo()
    row = repo.create(title="x", body=None, kind="question", created_by="u1", created_by_email="a@example.com", source_surface="web", page_url=None, context=None)
    c = repo.add_comment(row["id"], author_id="admin1", author_email="ops@example.com", author_kind="admin", body="Looking into it")
    assert c["id"].startswith("isc_") and c["issue_owner_id"] == "u1"
    again = repo.get(row["id"])
    assert again["comment_count"] == 1 and again["last_activity_at"] >= row["last_activity_at"]
    assert [x["body"] for x in repo.list_comments(row["id"])] == ["Looking into it"]


def test_resolve_is_a_guarded_transition(pg_repo_env):
    from src.repositories.issue_reports_pg import IssueAlreadyResolved
    repo = issue_reports_repo()
    row = repo.create(title="x", body=None, kind="bug", created_by="u1", created_by_email=None, source_surface="web", page_url=None, context=None)
    done = repo.resolve(row["id"], resolved_by="admin1", resolution_note="fixed in 0.99.0")
    assert done["status"] == "resolved" and done["resolved_by"] == "admin1"
    with pytest.raises(IssueAlreadyResolved):
        repo.resolve(row["id"], resolved_by="admin2", resolution_note=None)
    assert repo.resolve("iss_missing", resolved_by="admin1", resolution_note=None) is None


def test_screenshot_and_webhook_marks(pg_repo_env):
    repo = issue_reports_repo()
    row = repo.create(title="x", body=None, kind="bug", created_by="u1", created_by_email=None, source_surface="web", page_url=None, context=None)
    repo.set_screenshot(row["id"], f"issues/{row['id']}/screenshot.png")
    repo.mark_webhook_delivered(row["id"])
    again = repo.get(row["id"])
    assert again["screenshot_path"].endswith("screenshot.png") and again["webhook_delivered_at"] is not None
```

Use whatever fixture name `tests/db_pg/test_semantic_feedback_pg.py` uses instead of `pg_repo_env`. Run: `.venv/bin/python -m pytest tests/db_pg/test_issue_reports_pg.py -q` — Expected: FAIL (`ImportError: cannot import name 'issue_reports_repo'`).

- [ ] **Step 4: Write the repository and register it**

`src/repositories/issue_reports_pg.py`:

```python
"""Postgres repository for ``issue_reports`` / ``issue_comments``.

PG-ONLY (A3 PG-first ratchet). No DuckDB sibling; registered ``PG``-only in
:data:`src.repositories._REGISTRY`; resolving it on a DuckDB-backed instance
raises :class:`src.repositories.RequiresPostgresBackend` (a typed ``501`` at
the API). ``created_by`` is the USER ID, which is what the internal-table
row filter compares against.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_NUMBER_RE = re.compile(r"^#?(\d{1,9})$")

_ROW_SELECT = """
    SELECT r.*,
           (SELECT COUNT(*) FROM issue_comments c WHERE c.issue_id = r.id) AS comment_count
    FROM issue_reports r
"""


class IssueAlreadyResolved(Exception):
    """``resolve()`` on a row that is already ``resolved`` — the first
    resolver's signature must not be overwritten."""


def _row(mapping: Any) -> Dict[str, Any]:
    d = dict(mapping)
    ctx = d.get("context_json")
    if isinstance(ctx, str):
        d["context_json"] = json.loads(ctx)
    return d


class IssueReportsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # -- reads ---------------------------------------------------------
    def get(self, issue_ref: str) -> Optional[Dict[str, Any]]:
        m = _NUMBER_RE.match(issue_ref.strip())
        where = "r.number = :n" if m else "r.id = :id"
        params = {"n": int(m.group(1))} if m else {"id": issue_ref.strip()}
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(_ROW_SELECT + f" WHERE {where}"), params).mappings().first()
        return _row(row) if row else None

    def list_for_user(self, user_id: str, *, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self._list("r.created_by = :uid", {"uid": user_id}, status=status, limit=limit)

    def list_all(self, *, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self._list("TRUE", {}, status=status, limit=limit)

    def count_for_user(self, user_id: str, *, status: Optional[str] = None) -> int:
        return self._count("created_by = :uid", {"uid": user_id}, status)

    def count_all(self, *, status: Optional[str] = None) -> int:
        return self._count("TRUE", {}, status)

    def list_comments(self, issue_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM issue_comments WHERE issue_id = :id ORDER BY created_at, id"), {"id": issue_id}
            ).mappings().all()
        return [dict(r) for r in rows]

    # -- writes --------------------------------------------------------
    def create(self, *, title: str, body: Optional[str], kind: str, created_by: str, created_by_email: Optional[str],
               source_surface: str, page_url: Optional[str], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        issue_id = f"iss_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("""
                    INSERT INTO issue_reports
                      (id, title, body, kind, status, created_by, created_by_email, source_surface, page_url, context_json,
                       created_at, updated_at, last_activity_at)
                    VALUES
                      (:id, :title, :body, :kind, 'open', :created_by, :created_by_email, :source_surface, :page_url,
                       CAST(:context AS jsonb), current_timestamp, current_timestamp, current_timestamp)
                """),
                {"id": issue_id, "title": title, "body": body, "kind": kind, "created_by": created_by,
                 "created_by_email": created_by_email, "source_surface": source_surface, "page_url": page_url,
                 "context": json.dumps(context) if context is not None else None},
            )
        return self.get(issue_id)  # type: ignore[return-value]

    def add_comment(self, issue_id: str, *, author_id: Optional[str], author_email: Optional[str],
                    author_kind: str, body: str) -> Dict[str, Any]:
        comment_id = f"isc_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            owner = conn.execute(sa.text("SELECT created_by FROM issue_reports WHERE id = :id"), {"id": issue_id}).scalar()
            if owner is None:
                raise KeyError(issue_id)
            conn.execute(
                sa.text("""
                    INSERT INTO issue_comments (id, issue_id, issue_owner_id, author_id, author_email, author_kind, body, created_at)
                    VALUES (:id, :issue_id, :owner, :author_id, :author_email, :author_kind, :body, current_timestamp)
                """),
                {"id": comment_id, "issue_id": issue_id, "owner": owner, "author_id": author_id,
                 "author_email": author_email, "author_kind": author_kind, "body": body},
            )
            conn.execute(
                sa.text("UPDATE issue_reports SET last_activity_at = current_timestamp, updated_at = current_timestamp WHERE id = :id"),
                {"id": issue_id},
            )
            row = conn.execute(sa.text("SELECT * FROM issue_comments WHERE id = :id"), {"id": comment_id}).mappings().first()
        return dict(row)

    def resolve(self, issue_id: str, *, resolved_by: str, resolution_note: Optional[str]) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            status = conn.execute(sa.text("SELECT status FROM issue_reports WHERE id = :id"), {"id": issue_id}).scalar()
            if status is None:
                return None
            if status == "resolved":
                raise IssueAlreadyResolved(issue_id)
            conn.execute(
                sa.text("""
                    UPDATE issue_reports
                       SET status = 'resolved', resolved_at = current_timestamp, resolved_by = :by,
                           resolution_note = :note, updated_at = current_timestamp, last_activity_at = current_timestamp
                     WHERE id = :id AND status <> 'resolved'
                """),
                {"id": issue_id, "by": resolved_by, "note": resolution_note},
            )
        return self.get(issue_id)

    def set_screenshot(self, issue_id: str, relative_path: str) -> None:
        self._update(issue_id, "screenshot_path = :v", {"v": relative_path})

    def mark_webhook_delivered(self, issue_id: str) -> None:
        self._update(issue_id, "webhook_delivered_at = current_timestamp", {})

    # -- helpers -------------------------------------------------------
    def _list(self, where: str, params: Dict[str, Any], *, status: Optional[str], limit: int) -> List[Dict[str, Any]]:
        if status:
            where += " AND r.status = :status"
            params = {**params, "status": status}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(_ROW_SELECT + f" WHERE {where} ORDER BY r.last_activity_at DESC, r.number DESC LIMIT :limit"),
                {**params, "limit": max(1, min(int(limit), 500))},
            ).mappings().all()
        return [_row(r) for r in rows]

    def _count(self, where: str, params: Dict[str, Any], status: Optional[str]) -> int:
        if status:
            where += " AND status = :status"
            params = {**params, "status": status}
        with self._engine.connect() as conn:
            return int(conn.execute(sa.text(f"SELECT COUNT(*) FROM issue_reports WHERE {where}"), params).scalar() or 0)

    def _update(self, issue_id: str, set_clause: str, params: Dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE issue_reports SET {set_clause}, updated_at = current_timestamp WHERE id = :id"),
                {**params, "id": issue_id},
            )
```

In `src/repositories/__init__.py`: add `"issue_reports": {PG: ("src.repositories.issue_reports_pg", "IssueReportsPgRepository")},` to `_REGISTRY` next to `"semantic_feedback"`, and a factory beside `semantic_feedback_repo()`:

```python
def issue_reports_repo() -> Any:
    """Issue reports + comments (PG-only, step 1 of issue reporting)."""
    return _build("issue_reports")
```

(Copy the exact body shape of `semantic_feedback_repo()` — if it uses a different helper than `_build`, use that.) Add the name to the module's `__all__` if one exists.

Run: `.venv/bin/python -m pytest tests/db_pg/test_issue_reports_pg.py tests/test_repository_registry.py tests/test_repository_registry_pg_first_ratchet.py tests/db_pg/test_repo_module_pg_first_ratchet.py -q` — Expected: PASS.

- [ ] **Step 5: Internal tables — failing test first**

`tests/db_pg/test_agnes_issues_internal_table_pg.py`, modelled on `tests/db_pg/test_agnes_turns_internal_table_pg.py` (read it whole; reuse its `_boot()`, `_auth`, package-membership helpers and the `seeded_app_both` fixture). Cases:

```python
ISSUES_ID = "agnes_issues"
COMMENTS_ID = "agnes_issue_comments"


def test_registered_and_in_usage_package_on_pg_only(seeded_app_both):
    _boot()
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)  # use the lookup the turns test uses
    members = _member_ids(pkg["id"])
    if seeded_app_both["backend"] == "pg":
        assert {ISSUES_ID, COMMENTS_ID} <= members
    else:
        assert not ({ISSUES_ID, COMMENTS_ID} & members)


def test_reporter_sees_only_own_rows_and_admin_replies(seeded_app_both):
    if seeded_app_both["backend"] != "pg":
        pytest.skip("PG-only table")
    _boot()
    repo = issue_reports_repo()
    mine = repo.create(title="mine", body=None, kind="bug", created_by=seeded_app_both["analyst_id"], created_by_email=None, source_surface="web", page_url=None, context={"k": "v"})
    repo.create(title="theirs", body=None, kind="bug", created_by="someone-else", created_by_email=None, source_surface="web", page_url=None, context=None)
    repo.add_comment(mine["id"], author_id="admin", author_email="ops@example.com", author_kind="admin", body="on it")
    client = seeded_app_both["client"]
    r = client.post("/api/query", json={"sql": f"SELECT number, title, context_json FROM {ISSUES_ID} ORDER BY number"}, headers=_auth(seeded_app_both["analyst_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"] if "rows" in r.json() else r.json()["data"]
    assert [row[1] for row in rows] == ["mine"]          # adjust to the response shape the turns test asserts on
    c = client.post("/api/query", json={"sql": f"SELECT body FROM {COMMENTS_ID}"}, headers=_auth(seeded_app_both["analyst_token"]))
    assert c.status_code == 200 and [row[0] for row in (c.json().get("rows") or c.json().get("data"))] == ["on it"]


def test_admin_sees_every_row(seeded_app_both):
    if seeded_app_both["backend"] != "pg":
        pytest.skip("PG-only table")
    _boot()
    repo = issue_reports_repo()
    repo.create(title="a", body=None, kind="bug", created_by="u1", created_by_email=None, source_surface="web", page_url=None, context=None)
    repo.create(title="b", body=None, kind="bug", created_by="u2", created_by_email=None, source_surface="web", page_url=None, context=None)
    r = seeded_app_both["client"].post("/api/query", json={"sql": f"SELECT COUNT(*) FROM {ISSUES_ID}"}, headers=_auth(seeded_app_both["admin_token"]))
    assert r.status_code == 200 and int((r.json().get("rows") or r.json().get("data"))[0][0]) >= 2
```

Take the exact fixture keys (`analyst_id`, `analyst_token`, `admin_token`, `backend`) and the `/api/query` response shape from the turns test; the analyst must be a member of the `agnes-usage` package the way that test arranges it. Run: expect FAIL (`agnes_issues` unknown table).

- [ ] **Step 6: Declare the internal tables**

Append to `INTERNAL_TABLES` in `connectors/internal/access.py` (read `InternalTable`'s fields at `:44` and one existing entry for the exact keyword set; every column gets a description or `tests/test_internal_table_descriptions.py` fails):

```python
InternalTable(
    registry_id="agnes_issues",
    source_table="issue_reports",
    filter_column="created_by",
    filter_kind="user_id",
    display_name="My issue reports",
    description=(
        "Problems, wrong answers, missing things and questions reported through "
        "'Report a problem', `agnes issue report` or the report_issue tool. "
        "Own rows only — a reporter sees the issues they filed; admins see all. "
        "Server-side only (Postgres backend); not synced by agnes pull."
    ),
    column_descriptions={
        "id": "Issue id (iss_…); every command also accepts the number.",
        "number": "Human id shown as #N.",
        "title": "One-line summary as typed by the reporter.",
        "body": "What happened, as typed by the reporter (may be empty).",
        "kind": "bug | wrong_answer | request | question | other.",
        "status": "open | resolved.",
        "created_by": "Reporter's user id (the row filter).",
        "created_by_email": "Reporter's email for display.",
        "source_surface": "web | cli | mcp — where the report was filed from.",
        "page_url": "Page the reporter was on (web) or the URL they passed (cli/mcp).",
        "context_json": "Auto-captured context as JSON text: app version, commit, browser, chat session id, recent client errors.",
        "screenshot_path": "Relative path of the PNG when one was attached; fetch it via GET /api/issues/{id}/screenshot.",
        "created_at": "When it was filed.",
        "updated_at": "Last change of any field.",
        "last_activity_at": "Last comment or status change — the column to sort by for 'what is new'.",
        "resolved_at": "When an admin resolved it.",
        "resolved_by": "Admin who resolved it.",
        "resolution_note": "What the admin wrote when resolving.",
        "webhook_delivered_at": "When the operator webhook accepted the summary; NULL means not configured or delivery failed.",
    },
),
InternalTable(
    registry_id="agnes_issue_comments",
    source_table="issue_comments",
    filter_column="issue_owner_id",
    filter_kind="user_id",
    display_name="Comments on my issue reports",
    description=(
        "Public replies on issue reports — by the reporter or an admin. Own issues only: "
        "a reporter sees every comment on the issues they filed; admins see all. "
        "Server-side only (Postgres backend); not synced by agnes pull."
    ),
    column_descriptions={
        "id": "Comment id (isc_…).",
        "issue_id": "The issue this comment belongs to (join to agnes_issues.id).",
        "issue_owner_id": "Reporter's user id, copied from the issue (the row filter).",
        "author_id": "Who wrote it (user id).",
        "author_email": "Who wrote it (email).",
        "author_kind": "reporter | admin.",
        "body": "The comment text.",
        "created_at": "When it was written.",
    },
),
```

In `connectors/internal/registry.py`: add both ids to `PG_ONLY_INTERNAL_TABLE_IDS`; extend `USAGE_PACKAGE_LONG_DESCRIPTION` with one sentence about issue reports, add the questions "What issues have I reported that are still open?" and "What changed on my issue #42 since yesterday?" to the questions list, and `"issues"` to `USAGE_PACKAGE_TAGS`. Read the tests in `tests/test_internal_table_descriptions.py` for the exact phrases they grep ("own rows", "admin", "not local").

Run: `.venv/bin/python -m pytest tests/test_internal_table_descriptions.py tests/test_internal_package_seed.py tests/test_internal_tables_stack_gated.py tests/test_agent_scope_seams.py tests/db_pg/test_agnes_issues_internal_table_pg.py -q` — Expected: PASS.

- [ ] **Step 7: Notifier — failing test first**

`tests/test_issue_notifier.py`:

```python
from app.services import issue_notifier as n


def _row(**over):
    base = {"id": "iss_abc", "number": 42, "kind": "bug", "title": "Tables render raw while streaming",
            "body": "During streaming I see raw | and --- until the answer completes. " * 10,
            "created_by_email": "analyst@example.com", "page_url": "https://agnes.example.com/chat?session=35b6",
            "context_json": {"app_version": "0.98.3", "app_commit": "95bc14b", "chat_session_id": "35b6"},
            "screenshot_path": "issues/iss_abc/screenshot.png"}
    base.update(over)
    return base


def test_text_names_number_kind_title_page_version_and_links():
    text = n.build_text(_row(), public_base_url="https://agnes.example.com")
    assert text.startswith("New issue #42 (bug) from analyst@example.com")
    assert "Tables render raw while streaming" in text
    assert "https://agnes.example.com/chat?session=35b6" in text
    assert "0.98.3 (95bc14b)" in text
    assert "https://agnes.example.com/api/issues/iss_abc/screenshot" in text
    assert "agnes admin issue show 42" in text
    excerpt_line = [l for l in text.splitlines() if l.startswith("> ")][0]
    assert len(excerpt_line) <= 305 and "\n" not in excerpt_line


def test_no_screenshot_line_without_screenshot():
    assert "Screenshot:" not in n.build_text(_row(screenshot_path=None), public_base_url="https://x")


def test_unconfigured_webhook_is_a_noop(monkeypatch):
    monkeypatch.delenv("AGNES_ISSUES_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(n, "_config_value", lambda: "")
    called = []
    monkeypatch.setattr(n, "post_webhook", lambda url, payload, **kw: called.append(url) or True)
    assert n.notify_issue_filed(_row(), public_base_url="https://x") is False
    assert called == []


def test_posts_text_payload_and_reports_delivery(monkeypatch):
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/abc")
    seen = {}
    monkeypatch.setattr(n, "post_webhook", lambda url, payload, **kw: seen.update(url=url, payload=payload) or True)
    assert n.notify_issue_filed(_row(), public_base_url="https://x") is True
    assert seen["url"] == "https://hooks.example.com/abc" and set(seen["payload"]) == {"text"}


def test_env_beats_config(monkeypatch):
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://env.example.com/h")
    monkeypatch.setattr(n, "_config_value", lambda: "https://cfg.example.com/h")
    assert n.issues_webhook_url() == "https://env.example.com/h"
```

Run: expect FAIL (module missing).

- [ ] **Step 8: Write the notifier**

`app/services/issue_notifier.py`:

```python
"""Operator mirror for issue reports: one ``{"text": …}`` post per report.

The record in ``issue_reports`` is the source of truth; this is the copy that
lets a team start recording problems in the chat channel they already watch.
``issues.webhook_url`` (env ``AGNES_ISSUES_WEBHOOK_URL``) is deliberately a
different key from ``notifications.alert_webhook_url``: user reports would
drown watchdog and sync alerts. The URL is operator configuration, so it
takes the same posture as the alert webhook (no SSRF pinning); a
user-supplied URL must never reach this function — route it through
``app/chat/webhook_delivery.py`` instead.
"""

from __future__ import annotations

import logging
import os

from services.telegram_bot.sender import post_webhook

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 300


def _config_value() -> str:
    try:
        from app.instance_config import get_value

        return str(get_value("issues", "webhook_url", default="") or "")
    except Exception:  # config not loadable → no mirror, never a crash
        return ""


def issues_webhook_url() -> str:
    return (os.environ.get("AGNES_ISSUES_WEBHOOK_URL") or _config_value()).strip()


def _excerpt(body: str | None) -> str:
    flat = " ".join((body or "").split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 1] + "…"


def build_text(row: dict, *, public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    ctx = row.get("context_json") or {}
    who = row.get("created_by_email") or row.get("created_by") or "unknown"
    lines = [f"New issue #{row['number']} ({row.get('kind', 'bug')}) from {who}", str(row.get("title", "")).strip()]
    excerpt = _excerpt(row.get("body"))
    if excerpt:
        lines.append(f"> {excerpt}")
    meta = []
    if row.get("page_url"):
        meta.append(f"Page: {row['page_url']}")
    version = ctx.get("app_version")
    if version:
        commit = ctx.get("app_commit")
        meta.append(f"Version {version} ({commit})" if commit else f"Version {version}")
    if ctx.get("chat_session_id"):
        meta.append(f"chat session {ctx['chat_session_id']}")
    if meta:
        lines.append("  ·  ".join(meta))
    if row.get("screenshot_path"):
        lines.append(f"Screenshot: {base}/api/issues/{row['id']}/screenshot  (login required)")
    lines.append(f"Show: agnes admin issue show {row['number']}")
    return "\n".join(lines)


def notify_issue_filed(row: dict, *, public_base_url: str) -> bool:
    """Post the summary; ``True`` only when the webhook answered 2xx."""
    url = issues_webhook_url()
    if not url:
        logger.debug("issues.webhook_url not configured; report %s kept in the instance only", row.get("id"))
        return False
    return bool(post_webhook(url, {"text": build_text(row, public_base_url=public_base_url)}))
```

Add to `config/instance.yaml.example` right after the operator-alerting block:

```yaml
# --- Issue reporting (optional) ---
# Where "Report a problem" (web), `agnes issue report` and the report_issue
# MCP tool mirror a one-message summary: an incoming webhook that accepts
# {"text": "..."} (Slack, Mattermost, Google Chat, Discord). The report is
# always stored in the instance; this is the copy for the team's channel.
# Env override: AGNES_ISSUES_WEBHOOK_URL. Requires the Postgres app-state
# backend.
issues:
  webhook_url: ""
```

Run: `.venv/bin/python -m pytest tests/test_issue_notifier.py tests/test_instance_config*.py -q` (the second only if such a test parses the example file) — Expected: PASS.

- [ ] **Step 9: Guards and commit**

Run: `.venv/bin/python scripts/verify_syncmap.py` and `.venv/bin/python -m pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`. Expected: clean / PASS.

```bash
git add src/models/issue_reports.py src/models/__init__.py migrations/versions/0119_issue_reports.py \
        src/repositories/issue_reports_pg.py src/repositories/__init__.py \
        connectors/internal/access.py connectors/internal/registry.py \
        app/services/issue_notifier.py config/instance.yaml.example \
        tests/db_pg/test_issue_reports_pg.py tests/db_pg/test_agnes_issues_internal_table_pg.py tests/test_issue_notifier.py
git commit -m "feat(issues): PG-only issue_reports storage, internal tables and operator webhook notifier

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: REST API, audit posture, docs inventory

**Files:**
- Create: `app/api/issues.py`
- Modify: `app/main.py:593` (import) and `:3150` (`include_router`, right after `semantic_feedback_router`)
- Modify: `src/audit_events.py:1526` (append a group at the end of `CATALOG`)
- Modify: `src/audit_posture.py:756` (`POSTURE`), `:1495` (`READ_POSTURE`, before the catch-all)
- Modify: `tests/db_pg/test_get_status_parity_sweep.py:59`, `tests/db_pg/test_mutation_status_parity_sweep.py:80`
- Modify: `tests/test_documentation_api_triple_surface.py:29` (`_COHORT`), `:830` (`_EXEMPT`)
- Modify: `docs/api-reference.md` (endpoint inventory appendix)
- Test: `tests/test_issues_endpoint.py` (DuckDB default fixture: RBAC + typed 501), `tests/db_pg/test_issues_api_pg.py` (behaviour on Postgres)

**Interfaces:**
- Consumes: Task 1's `issue_reports_repo()` and method signatures; `app.services.issue_notifier.notify_issue_filed`; `app.auth.access.require_admin`; `app.auth.dependencies.get_current_user`; `app.utils.get_data_dir`; `src.audit_helpers.log_safe`; `app.version.APP_VERSION`; `os.environ["AGNES_COMMIT_SHA"]`.
- Produces the routes below with these exact paths and JSON shapes; Task 3 and Task 4 code against them:
  - `POST /api/issues` body `IssueCreate{title: str≤200, body?: str≤8000, kind?: Literal[kinds]="bug", page_url?: str≤2000, context?: dict}` → 201 row (`id, number, title, body, kind, status, created_by, created_by_email, source_surface, page_url, context_json, screenshot_path, created_at, updated_at, last_activity_at, resolved_at, resolved_by, resolution_note, webhook_delivered_at, comment_count`)
  - `PUT /api/issues/{issue_id}/screenshot` raw `image/png` → 204
  - `GET /api/issues/{issue_id}/screenshot` → PNG
  - `GET /api/issues/mine?status=open|resolved&limit=` → `{"data": [...], "count": n, "truncated": {"limit": l, "total": t} | null}`
  - `GET /api/issues/{issue_id}` → row + `"comments": [...]`
  - `POST /api/issues/{issue_id}/comments` body `{body: str≤8000}` → 201 comment
  - `GET /api/admin/issues?status=&limit=` → same envelope as `/mine`
  - `POST /api/admin/issues/{issue_id}/resolve` body `{resolution_note?: str≤4000}` → 200 row; 409 `{"error": "already_resolved", ...}`
  - Error bodies: `{"error": "<code>", "message": "...", "hint"?: "..."}` with codes `missing_title`, `invalid_kind`, `context_too_large`, `screenshot_too_large`, `screenshot_not_png`, `already_resolved`, `issue_not_found`.
  - `source_surface` is derived server-side from the `X-Agnes-Client` header when present (`cli` / `mcp`), else `web`. Task 3's clients send that header (the CLI client may already set one — check `cli/client.py` for an existing client-kind header and reuse it).

- [ ] **Step 1: RBAC + 501 contract test on the default fixture**

`tests/test_issues_endpoint.py`, in the shape of `tests/test_semantic_feedback_endpoint.py` (read it; reuse `_assert_typed_501` with `feature == "issue_reports"` and the `duckdb_backend_pinned` fixture):

```python
_CREATE = "/api/issues"
_MINE = "/api/issues/mine"
_QUEUE = "/api/admin/issues"


class TestWhoMayReport:
    def test_create_requires_authentication(self, seeded_app):
        assert seeded_app["client"].post(_CREATE, json={"title": "x"}).status_code == 401

    def test_create_is_open_to_a_non_admin_and_fails_clean_on_duckdb(self, seeded_app, duckdb_backend_pinned):
        resp = seeded_app["client"].post(_CREATE, json={"title": "Tables render raw"}, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code != 403
        _assert_typed_501(resp)

    def test_mine_is_open_to_a_non_admin(self, seeded_app, duckdb_backend_pinned):
        _assert_typed_501(seeded_app["client"].get(_MINE, headers=_auth(seeded_app["analyst_token"])))


class TestTheQueueIsAdminOnly:
    def test_queue_refuses_a_non_admin(self, seeded_app):
        assert seeded_app["client"].get(_QUEUE, headers=_auth(seeded_app["analyst_token"])).status_code == 403

    def test_resolve_refuses_a_non_admin(self, seeded_app):
        r = seeded_app["client"].post(f"{_QUEUE}/iss_x/resolve", json={}, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403
```

Run: expect FAIL (404 — router not registered).

- [ ] **Step 2: Behaviour tests on Postgres**

`tests/db_pg/test_issues_api_pg.py`, using the fixture `tests/db_pg/test_semantic_feedback_pg.py` uses for an app with a PG backend and two tokens (analyst + admin; add a second analyst if the fixture offers one, else create the row for "someone else" through the repo with `created_by="other-user"`):

```python
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_report_then_mine_then_show(pg_app):
    c, tok = pg_app["client"], _auth(pg_app["analyst_token"])
    r = c.post("/api/issues", json={"title": "Tables render raw", "body": "while streaming", "kind": "bug",
                                    "page_url": "/chat?session=abc", "context": {"user_agent": "UA", "recent_errors": []}},
               headers=tok)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "open" and row["source_surface"] == "web"
    assert row["context_json"]["app_version"] and "request_id" in row["context_json"]
    mine = c.get("/api/issues/mine", headers=tok).json()
    assert mine["count"] == 1 and mine["data"][0]["id"] == row["id"] and mine["truncated"] is None
    show = c.get(f"/api/issues/{row['number']}", headers=tok).json()
    assert show["id"] == row["id"] and show["comments"] == []


def test_validation_errors_are_typed(pg_app):
    c, tok = pg_app["client"], _auth(pg_app["analyst_token"])
    assert c.post("/api/issues", json={"title": "   "}, headers=tok).json()["error"] == "missing_title"
    assert c.post("/api/issues", json={"title": "x", "kind": "nope"}, headers=tok).status_code == 422 or \
           c.post("/api/issues", json={"title": "x", "kind": "nope"}, headers=tok).json()["error"] == "invalid_kind"
    big = {"title": "x", "context": {"blob": "a" * 40_000}}
    assert c.post("/api/issues", json=big, headers=tok).json()["error"] == "context_too_large"


def test_owner_boundary_is_404_not_403(pg_app):
    c = pg_app["client"]
    theirs = issue_reports_repo().create(title="theirs", body=None, kind="bug", created_by="other-user", created_by_email=None, source_surface="web", page_url=None, context=None)
    assert c.get(f"/api/issues/{theirs['id']}", headers=_auth(pg_app["analyst_token"])).status_code == 404
    assert c.get(f"/api/issues/{theirs['id']}", headers=_auth(pg_app["admin_token"])).status_code == 200
    assert c.put(f"/api/issues/{theirs['id']}/screenshot", content=PNG, headers={**_auth(pg_app["analyst_token"]), "Content-Type": "image/png"}).status_code == 404


def test_screenshot_roundtrip_and_png_check(pg_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c, tok = pg_app["client"], _auth(pg_app["analyst_token"])
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()
    bad = c.put(f"/api/issues/{row['id']}/screenshot", content=b"\xff\xd8\xff" + b"\x00" * 16, headers={**tok, "Content-Type": "image/png"})
    assert bad.status_code == 400 and bad.json()["error"] == "screenshot_not_png"
    ok = c.put(f"/api/issues/{row['id']}/screenshot", content=PNG, headers={**tok, "Content-Type": "image/png"})
    assert ok.status_code == 204
    assert (tmp_path / "issues" / row["id"] / "screenshot.png").read_bytes() == PNG
    got = c.get(f"/api/issues/{row['id']}/screenshot", headers=tok)
    assert got.status_code == 200 and got.headers["content-type"].startswith("image/png")
    assert "frame-ancestors" in got.headers.get("content-security-policy", "")
    assert c.get(f"/api/issues/{row['id']}/screenshot", headers=_auth(pg_app["admin_token"])).status_code == 200


def test_comments_and_resolve(pg_app):
    c, tok, adm = pg_app["client"], _auth(pg_app["analyst_token"]), _auth(pg_app["admin_token"])
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()
    mine = c.post(f"/api/issues/{row['id']}/comments", json={"body": "more detail"}, headers=tok).json()
    theirs = c.post(f"/api/issues/{row['id']}/comments", json={"body": "on it"}, headers=adm).json()
    assert (mine["author_kind"], theirs["author_kind"]) == ("reporter", "admin")
    assert c.get(f"/api/admin/issues", headers=adm).json()["count"] >= 1
    done = c.post(f"/api/admin/issues/{row['id']}/resolve", json={"resolution_note": "fixed"}, headers=adm)
    assert done.status_code == 200 and done.json()["status"] == "resolved"
    again = c.post(f"/api/admin/issues/{row['id']}/resolve", json={}, headers=adm)
    assert again.status_code == 409 and again.json()["error"] == "already_resolved"
    assert [x["body"] for x in c.get(f"/api/issues/{row['id']}", headers=tok).json()["comments"]] == ["more detail", "on it"]


def test_webhook_is_posted_in_the_background_and_marked(pg_app, monkeypatch):
    from app.services import issue_notifier
    seen = {}
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr(issue_notifier, "post_webhook", lambda url, payload, **kw: seen.update(payload) or True)
    c, tok = pg_app["client"], _auth(pg_app["analyst_token"])
    row = c.post("/api/issues", json={"title": "Tables render raw", "kind": "bug"}, headers=tok).json()
    assert f"#{row['number']} (bug)" in seen["text"]
    assert c.get(f"/api/issues/{row['id']}", headers=tok).json()["webhook_delivered_at"] is not None


def test_unconfigured_webhook_leaves_delivery_null(pg_app, monkeypatch):
    monkeypatch.delenv("AGNES_ISSUES_WEBHOOK_URL", raising=False)
    from app.services import issue_notifier
    monkeypatch.setattr(issue_notifier, "_config_value", lambda: "")
    c, tok = pg_app["client"], _auth(pg_app["analyst_token"])
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()
    assert row["webhook_delivered_at"] is None
```

`pg_app` stands for whatever the semantic-feedback PG test names its app fixture. Run: expect FAIL.

- [ ] **Step 3: Write the API module**

`app/api/issues.py` (mirror `app/api/semantic_feedback.py` for the docstring, `_issues_repo` dependency, `_clean`, and audit calls):

```python
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.utils import get_data_dir
from src.models.issue_reports import ISSUE_KINDS, ISSUE_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["issues"])

_MAX_CONTEXT_BYTES = 32 * 1024
_MAX_CONTEXT_STRING = 300
_MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _issues_repo() -> Any:
    """Resolve the PG-only repo AS A DEPENDENCY (typed 501 before body validation)."""
    from src.repositories import issue_reports_repo

    return issue_reports_repo()


class IssueCreate(BaseModel):
    title: str = Field(max_length=200)
    body: Optional[str] = Field(default=None, max_length=8000)
    kind: Literal["bug", "wrong_answer", "request", "question", "other"] = "bug"
    page_url: Optional[str] = Field(default=None, max_length=2000)
    context: Optional[dict[str, Any]] = None


class CommentCreate(BaseModel):
    body: str = Field(max_length=8000)


class IssueResolve(BaseModel):
    resolution_note: Optional[str] = Field(default=None, max_length=4000)


def _err(status: int, code: str, message: str, hint: str | None = None) -> HTTPException:
    detail: dict[str, Any] = {"error": code, "message": message}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=status, detail=detail)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = value.strip()
    return s or None


def _cap_context(ctx: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Truncate every string leaf to 300 chars, refuse >32 KB. Client-supplied,
    so shape is not trusted: anything that is not a dict becomes None."""
    if not isinstance(ctx, dict):
        return None

    def cap(v: Any) -> Any:
        if isinstance(v, str):
            return v if len(v) <= _MAX_CONTEXT_STRING else v[:_MAX_CONTEXT_STRING]
        if isinstance(v, dict):
            return {str(k)[:64]: cap(x) for k, x in list(v.items())[:64]}
        if isinstance(v, list):
            return [cap(x) for x in v[:50]]
        return v

    capped = cap(ctx)
    if len(json.dumps(capped)) > _MAX_CONTEXT_BYTES:
        raise _err(400, "context_too_large", "context must be under 32 KB after truncation")
    return capped


def _server_context(request: Request) -> dict[str, Any]:
    from app.version import APP_VERSION

    return {
        "app_version": APP_VERSION,
        "app_commit": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        "request_id": request.headers.get("x-request-id") or getattr(request.state, "request_id", None),
    }


def _surface(request: Request) -> str:
    kind = (request.headers.get("x-agnes-client") or "").lower()
    return kind if kind in ("cli", "mcp") else "web"


def _public_base_url(request: Request) -> str:
    try:
        from app.instance_config import get_value

        configured = str(get_value("server", "public_url", default="") or "").strip()
    except Exception:
        configured = ""
    return configured.rstrip("/") or str(request.base_url).rstrip("/")


def _is_admin(user: dict) -> bool:
    from app.auth.access import is_user_admin  # use the helper require_admin uses; check its real name

    return bool(is_user_admin(user))


def _owned_or_admin(repo: Any, issue_ref: str, user: dict) -> dict:
    """404 (not 403) for someone else's issue: ids must not be probeable."""
    row = repo.get(issue_ref)
    if row is None or (row["created_by"] != user["id"] and not _is_admin(user)):
        raise _err(404, "issue_not_found", f"No issue {issue_ref!r} you can see.", hint="List yours: agnes issue list")
    return row


def _screenshot_dir(issue_id: str) -> Path:
    base = (Path(get_data_dir()) / "issues").resolve()
    target = (base / issue_id).resolve()
    if not target.is_relative_to(base):
        raise _err(400, "issue_not_found", "invalid issue id")
    return target


def _envelope(rows: list[dict], total: int, limit: int) -> dict[str, Any]:
    truncated = {"limit": limit, "total": total} if total > len(rows) else None
    return {"data": rows, "count": len(rows), "truncated": truncated}


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _status_or_400(status: Optional[str]) -> Optional[str]:
    if status in (None, "", "all"):
        return None
    if status not in ISSUE_STATUSES:
        raise _err(400, "invalid_status", f"status must be one of {', '.join(ISSUE_STATUSES)} or all")
    return status


@router.post("/api/issues", status_code=201)
async def create_issue(body: IssueCreate, request: Request, background: BackgroundTasks,
                       user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    """Report a problem (any signed-in caller). Stored in the instance; a text
    summary is mirrored to ``issues.webhook_url`` in the background."""
    title = _clean(body.title)
    if not title:
        raise _err(400, "missing_title", "title is required")
    context = {**(_cap_context(body.context) or {}), **{k: v for k, v in _server_context(request).items() if v}}
    row = repo.create(title=title, body=_clean(body.body), kind=body.kind, created_by=user["id"],
                      created_by_email=user.get("email"), source_surface=_surface(request),
                      page_url=_clean(body.page_url), context=context)
    from src.audit_helpers import log_safe

    log_safe(user_id=user.get("id"), action="issue.report", resource=row["id"], params={"kind": row["kind"], "surface": row["source_surface"]})
    base_url = _public_base_url(request)

    def _mirror() -> None:
        from app.services.issue_notifier import notify_issue_filed

        if notify_issue_filed(row, public_base_url=base_url):
            repo.mark_webhook_delivered(row["id"])

    background.add_task(_mirror)
    return row


@router.put("/api/issues/{issue_id}/screenshot", status_code=204)
async def put_screenshot(issue_id: str, request: Request, user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    row = _owned_or_admin(repo, issue_id, user)
    if row["created_by"] != user["id"]:
        raise _err(404, "issue_not_found", "only the reporter can attach the screenshot")
    data = await request.body()
    if len(data) > _MAX_SCREENSHOT_BYTES:
        raise _err(413, "screenshot_too_large", "screenshot must be under 3 MiB")
    if not data.startswith(_PNG_MAGIC):
        raise _err(400, "screenshot_not_png", "screenshot must be a PNG")
    target = _screenshot_dir(row["id"])
    target.mkdir(parents=True, exist_ok=True)
    (target / "screenshot.png").write_bytes(data)
    repo.set_screenshot(row["id"], f"issues/{row['id']}/screenshot.png")
    from src.audit_helpers import log_safe

    log_safe(user_id=user.get("id"), action="issue.screenshot", resource=row["id"], params={"bytes": len(data)})
    return Response(status_code=204)


@router.get("/api/issues/{issue_id}/screenshot")
async def get_screenshot(issue_id: str, user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    row = _owned_or_admin(repo, issue_id, user)
    if not row.get("screenshot_path"):
        raise _err(404, "issue_not_found", "no screenshot on this issue")
    path = _screenshot_dir(row["id"]) / "screenshot.png"
    if not path.is_file():
        raise _err(404, "issue_not_found", "screenshot file is missing")
    return FileResponse(path, media_type="image/png", headers={
        "Content-Disposition": 'inline; filename="screenshot.png"',
        "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
    })


@router.get("/api/issues/mine")
async def list_my_issues(status: Optional[str] = None, limit: int = 50,
                         user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    st, lim = _status_or_400(status), _clamp(limit)
    rows = repo.list_for_user(user["id"], status=st, limit=lim)
    return _envelope(rows, repo.count_for_user(user["id"], status=st), lim)


@router.get("/api/issues/{issue_id}")
async def get_issue(issue_id: str, user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    row = _owned_or_admin(repo, issue_id, user)
    return {**row, "comments": repo.list_comments(row["id"])}


@router.post("/api/issues/{issue_id}/comments", status_code=201)
async def add_comment(issue_id: str, body: CommentCreate, user: dict = Depends(get_current_user), repo: Any = Depends(_issues_repo)):
    row = _owned_or_admin(repo, issue_id, user)
    text = _clean(body.body)
    if not text:
        raise _err(400, "missing_body", "comment body is required")
    kind = "reporter" if row["created_by"] == user["id"] else "admin"
    comment = repo.add_comment(row["id"], author_id=user.get("id"), author_email=user.get("email"), author_kind=kind, body=text)
    from src.audit_helpers import log_safe

    log_safe(user_id=user.get("id"), action="issue.comment", resource=row["id"], params={"author_kind": kind})
    return comment


@router.get("/api/admin/issues")
async def list_issue_queue(status: Optional[str] = None, limit: int = 100,
                           _admin: dict = Depends(require_admin), repo: Any = Depends(_issues_repo)):
    st, lim = _status_or_400(status), _clamp(limit)
    return _envelope(repo.list_all(status=st, limit=lim), repo.count_all(status=st), lim)


@router.post("/api/admin/issues/{issue_id}/resolve")
async def resolve_issue(issue_id: str, body: IssueResolve, admin: dict = Depends(require_admin), repo: Any = Depends(_issues_repo)):
    from src.repositories.issue_reports_pg import IssueAlreadyResolved

    row = repo.get(issue_id)
    if row is None:
        raise _err(404, "issue_not_found", f"No issue {issue_id!r}.", hint="Find the id: agnes admin issue list")
    try:
        done = repo.resolve(row["id"], resolved_by=admin.get("email") or admin.get("id"), resolution_note=_clean(body.resolution_note))
    except IssueAlreadyResolved:
        raise _err(409, "already_resolved", f"#{row['number']} was already resolved by {row.get('resolved_by')} at {row.get('resolved_at')}")
    from src.audit_helpers import log_safe

    log_safe(user_id=admin.get("id"), action="issue.resolved", resource=row["id"], params={})
    return done
```

Check `app/auth/access.py` for the real name of the admin predicate `require_admin` uses (`is_user_admin` or similar) and use it in `_is_admin`; check `app/main.py`'s `RequiresPostgresBackend` handler to see how `feature` is named (it should come out as `issue_reports` from the registry key — adjust `_assert_typed_501` in the test to that value). Register the router in `app/main.py` next to the semantic-feedback lines.

Run both test files. Expected: PASS.

- [ ] **Step 4: Audit catalog and posture**

`src/audit_events.py`, append inside `CATALOG` after `access_policy.columns_view`:

```python
    # -- issue reports (app/api/issues.py) ----------------------------------
    # "Report a problem" from any surface; the record stays in the instance.
    "issue.report": AuditEvent("issue.report", "mutation", "A user filed an issue report"),
    "issue.screenshot": AuditEvent("issue.screenshot", "mutation", "The reporter attached a page screenshot to an issue"),
    "issue.comment": AuditEvent("issue.comment", "mutation", "A reporter or admin commented on an issue"),
    "issue.resolved": AuditEvent("issue.resolved", "mutation", "An admin resolved an issue"),
```

`src/audit_posture.py`:

```python
# POSTURE, new group at the end:
    # -- app.api.issues ------------------------------------------------------
    "POST /api/issues": "issue.report",
    "PUT /api/issues/{issue_id}/screenshot": "issue.screenshot",
    "POST /api/issues/{issue_id}/comments": "issue.comment",
    "POST /api/admin/issues/{issue_id}/resolve": "issue.resolved",
# READ_POSTURE, alphabetically before "GET /{full_path:path}":
    "GET /api/admin/issues": "exempt:ui_support",
    "GET /api/issues/mine": "exempt:ui_support",
    "GET /api/issues/{issue_id}": "exempt:ui_support",
    "GET /api/issues/{issue_id}/screenshot": "exempt:ui_support",
```

Run: `.venv/bin/python -m pytest tests/test_audit_catalog.py tests/test_audit_route_posture.py tests/test_audit_read_posture.py tests/test_audit_declared_actions.py -q` — Expected: PASS.

- [ ] **Step 5: Guards — parity sweeps, triple surface, docs inventory**

`tests/db_pg/test_mutation_status_parity_sweep.py::_PG_ONLY_ROUTE_EXEMPTIONS`:
```python
    "POST /api/issues": "filing an issue report writes `issue_reports`, a PG-only table (issue reporting step 1)",
```
`tests/db_pg/test_get_status_parity_sweep.py::_PG_ONLY_ROUTE_EXEMPTIONS`:
```python
    "GET /api/issues/mine": "a reporter's own issues read `issue_reports`, a PG-only table (issue reporting step 1)",
    "GET /api/admin/issues": "the issue queue reads `issue_reports`, a PG-only table (issue reporting step 1)",
```

`tests/test_documentation_api_triple_surface.py::_COHORT` (CLI names are the Typer path without `agnes`; MCP names are Task 3's tool names — they must exist by the time CI runs, which Task 3 guarantees; until then run this test only after Task 3 lands):
```python
    "/api/issues": ("issue report", "report_issue"),
    "/api/issues/mine": ("issue list", "list_my_issues"),
    "/api/issues/{issue_id}": ("issue show", "get_issue"),
    "/api/issues/{issue_id}/comments": ("issue comment", "issue_comment"),
    "/api/admin/issues": ("admin issue list", "issue_queue_list"),
    "/api/admin/issues/{issue_id}/resolve": ("admin issue resolve", "issue_resolve"),
```
`_EXEMPT`, with a hoisted reason constant in the file's style:
```python
_ISSUE_SCREENSHOT_REASON = (
    "the issue screenshot is a raw PNG body: PUT uploads bytes the browser captured, GET streams "
    "them back for the browser to draw — neither has a JSON analogue for MCP, and the CLI attaches "
    "a file through `agnes issue report --screenshot` against the same PUT (binary-body precedent: "
    "_COLLECTIONS_FILES_REASON / _LIBRARY_RAW_REASON)"
)
...
    "/api/issues/{issue_id}/screenshot": _ISSUE_SCREENSHOT_REASON,
```

`docs/api-reference.md`: add a short "Issue reports" section (who may call what, the envelope, the typed 501) and every new path verbatim in the endpoint inventory appendix, in the file's existing format.

Run: `.venv/bin/python -m pytest tests/test_api_docs_coverage.py tests/db_pg/test_get_status_parity_sweep.py tests/db_pg/test_mutation_status_parity_sweep.py -q` — Expected: PASS. (`test_documentation_api_triple_surface.py` passes once Task 3's CLI/MCP names exist; note that in the commit message.)

- [ ] **Step 6: Commit**

```bash
.venv/bin/python scripts/verify_syncmap.py
git add app/api/issues.py app/main.py src/audit_events.py src/audit_posture.py docs/api-reference.md \
        tests/test_issues_endpoint.py tests/db_pg/test_issues_api_pg.py \
        tests/db_pg/test_get_status_parity_sweep.py tests/db_pg/test_mutation_status_parity_sweep.py \
        tests/test_documentation_api_triple_surface.py
git commit -m "feat(issues): REST surface for issue reports with audit posture and docs inventory

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: CLI and MCP clients

**Files:**
- Create: `cli/commands/issue.py`, `cli/commands/admin_issue.py`
- Modify: `cli/main.py:304-372` (`app.add_typer(issue_app, name="issue")`), `cli/commands/admin.py:46-91` (`admin_app.add_typer(admin_issue_app, name="issue", help=...)`)
- Modify: `cli/query_hints.py` (add `issue_not_found_hint`)
- Modify: `app/api/mcp/foundation_tools.py` (seven tools + `FOUNDATION_TOOL_NAMES` entries)
- Modify: `cli/mcp/server.py` (four stdio tools), `tests/test_mcp_tool_parity.py:240` (`STDIO_TOOL_NAMES`)
- Modify: `src/audit_posture.py:1854` (`MCP_TOOL_POSTURE`)
- Modify: `app/initial_workspace_default/CLAUDE.md` (the "offer to file, never file silently" rule names `report_issue` / `agnes issue report`)
- Test: `tests/test_cli_issue.py`, `tests/test_mcp_issue_tools.py`, `tests/test_cli_api_parity.py` (add classes only if `parity_env` can run on a PG backend — check; `semantic_feedback` has none because the env is DuckDB-backed, in which case add a comment in the parity file's preamble naming the PG-only precedent instead)

**Interfaces:**
- Consumes: Task 2's routes and envelopes exactly as listed; `cli.client.api_get/api_post/api_put` (check `cli/client.py` for the raw-body PUT helper or use `get_client().put(...)`), `cli.v2_client.api_get_json/api_post_json` for stdio tools; `cli.error_render.render_error`; `cli.query_hints`.
- Produces: `agnes issue report|list|show|comment`, `agnes admin issue list|show|reply|resolve`; MCP `report_issue`, `list_my_issues`, `get_issue`, `issue_comment`, `issue_queue_list`, `issue_reply`, `issue_resolve` (HTTP), and `report_issue`, `list_my_issues`, `get_issue`, `issue_comment` (stdio).

- [ ] **Step 1: CLI tests first**

`tests/test_cli_issue.py` using `typer.testing.CliRunner` over `cli.main.app` and monkeypatched `cli.commands.issue.api_post/api_get` (the way `tests/test_cli_agent*.py` or the semantic-model CLI tests stub transport — read one and copy its `_FakeResp` helper):

```python
def test_report_prints_number_and_id(runner, fake_api):
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 42, "webhook_delivered_at": None})
    r = runner.invoke(app, ["issue", "report", "Tables render raw", "-m", "while streaming", "--kind", "bug", "--url", "/chat?session=x"])
    assert r.exit_code == 0 and "Filed #42 (iss_abc)" in r.stdout
    assert fake_api.last_post_json["kind"] == "bug" and fake_api.last_post_json["page_url"] == "/chat?session=x"
    assert "kept in this instance" in r.stdout          # webhook not delivered → say so


def test_report_json(runner, fake_api):
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 42})
    r = runner.invoke(app, ["issue", "report", "x", "--json"])
    assert json.loads(r.stdout)["number"] == 42


def test_report_attach_doctor_embeds_client_section(runner, fake_api, monkeypatch):
    monkeypatch.setattr("cli.commands.issue._doctor_client_section", lambda: {"cli_version": "0.98.3"})
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 1})
    runner.invoke(app, ["issue", "report", "x", "--attach-doctor"])
    assert fake_api.last_post_json["context"]["doctor"] == {"cli_version": "0.98.3"}


def test_report_screenshot_puts_png(runner, fake_api, tmp_path):
    png = tmp_path / "s.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8)
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 1})
    fake_api.put["/api/issues/iss_abc/screenshot"] = (204, None)
    r = runner.invoke(app, ["issue", "report", "x", "--screenshot", str(png)])
    assert r.exit_code == 0 and fake_api.last_put_bytes.startswith(b"\x89PNG")


def test_list_table_and_truncation(runner, fake_api):
    fake_api.get["/api/issues/mine"] = (200, {"data": [{"number": 42, "kind": "bug", "status": "open", "comment_count": 1, "created_at": "2026-09-09T10:00:00Z", "title": "Tables render raw"}], "count": 1, "truncated": {"limit": 1, "total": 3}})
    r = runner.invoke(app, ["issue", "list", "--limit", "1"])
    assert "#42" in r.stdout and "bug" in r.stdout and "1 reply" in r.stdout and "showing 1 of 3" in r.stdout
    j = runner.invoke(app, ["issue", "list", "--limit", "1", "--json"])
    assert json.loads(j.stdout)["truncated"] == {"limit": 1, "total": 3}


def test_list_empty_points_forward(runner, fake_api):
    fake_api.get["/api/issues/mine"] = (200, {"data": [], "count": 0, "truncated": None})
    r = runner.invoke(app, ["issue", "list"])
    assert "agnes issue report" in r.stdout


def test_show_404_hints(runner, fake_api):
    fake_api.get["/api/issues/99"] = (404, {"detail": {"error": "issue_not_found", "message": "nope"}})
    r = runner.invoke(app, ["issue", "show", "99"])
    assert r.exit_code == 1 and "agnes issue list" in r.stderr


def test_501_renders_one_sentence(runner, fake_api):
    fake_api.post["/api/issues"] = (501, {"error": "requires_postgres_backend", "feature": "issue_reports"})
    r = runner.invoke(app, ["issue", "report", "x"])
    assert r.exit_code == 1 and "Postgres" in r.stderr


def test_admin_resolve_409(runner, fake_api):
    fake_api.post["/api/admin/issues/42/resolve"] = (409, {"detail": {"error": "already_resolved", "message": "#42 was already resolved by ops at T"}})
    r = runner.invoke(app, ["admin", "issue", "resolve", "42", "--note", "fixed"])
    assert r.exit_code == 1 and "already resolved" in r.stderr
```

Build the `fake_api` fixture to record `last_post_json`, `last_put_bytes` and serve per-path tuples for `api_get/api_post` and the PUT helper you use. Run: expect FAIL (no `issue` command).

- [ ] **Step 2: Write the user CLI**

`cli/commands/issue.py`:

```python
"""`agnes issue …` — report a problem and follow your own reports.

Subcommand → route (all accept the default `agnes login` credential):
    issue report TITLE      POST /api/issues (+ PUT /api/issues/{id}/screenshot)
    issue list              GET  /api/issues/mine
    issue show ID           GET  /api/issues/{id}
    issue comment ID TEXT   POST /api/issues/{id}/comments
ID is `42`, `#42` or `iss_…`. The admin queue is `agnes admin issue …`
(placement follows authority — same split as `semantic-model feedback` vs
`admin semantic feedback`).
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post, get_client
from cli.error_render import render_error
from cli.query_hints import issue_not_found_hint

issue_app = typer.Typer(help="Report a problem and follow your own reports", no_args_is_help=True)

KINDS = ("bug", "wrong_answer", "request", "question", "other")
_CLIENT_HEADER = {"X-Agnes-Client": "cli"}


def _fail(resp, *, what: str = "This command") -> None:
    if resp.status_code == 501:
        typer.echo(f"{what} needs the Postgres app-state backend — this instance still runs the frozen DuckDB "
                   "backend. Migrate it (see docs/migrations.md) to use issue reporting.", err=True)
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(issue_not_found_hint(), err=True)
    render_error(resp)
    raise typer.Exit(1)


def _doctor_client_section() -> dict:
    """The client half of `agnes doctor` (never the admin-only server half)."""
    from cli.commands.doctor import build_client_section  # check the real name in cli/commands/doctor.py

    return build_client_section()


def _ref(issue_id: str) -> str:
    return issue_id.lstrip("#")


@issue_app.command("report")
def report(title: str = typer.Argument(..., help="One line: what is wrong"),
           body: Optional[str] = typer.Option(None, "-m", "--body", help="What happened"),
           kind: str = typer.Option("bug", "--kind", help="bug | wrong_answer | request | question | other"),
           url: Optional[str] = typer.Option(None, "--url", help="Page or object the problem is about"),
           screenshot: Optional[Path] = typer.Option(None, "--screenshot", exists=True, dir_okay=False, help="PNG to attach"),
           attach_doctor: bool = typer.Option(False, "--attach-doctor", help="Embed the client section of `agnes doctor`"),
           as_json: bool = typer.Option(False, "--json")):
    if kind not in KINDS:
        typer.echo(f"--kind must be one of: {', '.join(KINDS)}", err=True)
        raise typer.Exit(2)
    from cli import __version__ as cli_version  # or the module the CLI uses for its version

    context = {"cli_version": cli_version, "platform": platform.platform(), "captured_at": datetime.now(timezone.utc).isoformat()}
    if attach_doctor:
        context["doctor"] = _doctor_client_section()
    resp = api_post("/api/issues", json={"title": title, "body": body, "kind": kind, "page_url": url, "context": context},
                    headers=_CLIENT_HEADER)
    if resp.status_code != 201:
        _fail(resp, what="Reporting an issue")
    row = resp.json()
    if screenshot is not None:
        data = screenshot.read_bytes()
        put = get_client().put(f"/api/issues/{row['id']}/screenshot", content=data, headers={"Content-Type": "image/png", **_CLIENT_HEADER})
        if put.status_code != 204:
            typer.echo(f"Filed #{row['number']} but the screenshot was refused: {put.text}", err=True)
    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return
    typer.echo(f"Filed #{row['number']} ({row['id']})")
    if not row.get("webhook_delivered_at"):
        typer.echo("The report is kept in this instance; no operator channel confirmed delivery (yet).")
    typer.echo("Follow it: agnes issue show " + str(row["number"]))
```

`list` (columns `#`, kind, status, replies, age, title; `--status open|resolved|all` default `open`; `--limit` default 50; truncation line `showing N of M — raise --limit`; empty → `No issues yet. Report one with: agnes issue report "<what is wrong>"`), `show ID` (header line `#42 · bug · open · filed <created_at> by <email>`, body, then `--- N comments` with `[<author_kind>] <email> <created_at>` and the text), `comment ID TEXT` (prints `Comment added to #42`). Register in `cli/main.py` with `app.add_typer(issue_app, name="issue")`.

`cli/query_hints.py`:
```python
def issue_not_found_hint() -> str:
    return "Not found. List your reports: agnes issue list   ·   admins: agnes admin issue list"
```

`cli/commands/admin_issue.py`: `admin_issue_app` with `list [--status open|resolved|all] [--limit 100] [--json]` (`GET /api/admin/issues`, columns add `reporter`), `show ID` (`GET /api/issues/{id}`), `reply ID TEXT` (`POST /api/issues/{id}/comments`), `resolve ID [--note]` (`POST /api/admin/issues/{id}/resolve`; 409 → `already resolved` sentence on stderr, exit 1). Register in `cli/commands/admin.py`: `admin_app.add_typer(admin_issue_app, name="issue", help="Issue reports from users: the queue, replies, resolution")`.

Run `tests/test_cli_issue.py`. Expected: PASS.

- [ ] **Step 3: MCP tests first**

`tests/test_mcp_issue_tools.py` in the style of the existing feedback-tool tests (find them: `grep -rn "flag_semantic_issue" tests/`): assert the seven names are in `FOUNDATION_TOOL_NAMES`, registered on both HTTP transports (reuse `tests/test_mcp_tool_parity.py` helpers), that each declares behaviour flags (`report_issue` not read-only, `list_my_issues` read-only), and — with `httpx` mocked the way those tests do — that `report_issue(title="x")` POSTs `/api/issues` with `X-Agnes-Client: mcp` merged into `headers_fn()`, and `list_my_issues()` GETs `/api/issues/mine?status=open&limit=50`. For stdio: import `cli.mcp.server`, assert the four tools exist and `report_issue` calls `api_post_json("/api/issues", {...})`.

- [ ] **Step 4: Write the MCP tools**

In `register_foundation_tools` (`app/api/mcp/foundation_tools.py`), after the semantic-feedback tools:

```python
    _ISSUE_HEADERS = {"X-Agnes-Client": "mcp"}

    @tool(read_only=False, idempotent=False)
    async def report_issue(title: str, body: str | None = None, kind: str = "bug",
                           page_url: str | None = None, context: dict | None = None) -> dict:
        """Report a problem — a bug, a wrong answer, something missing, or something unclear.

        Any signed-in caller. The report is stored in this instance and a summary
        is mirrored to the operator's channel when one is configured. Attach what
        you know in ``context`` (query, table, tool that failed); never file
        silently on the user's behalf — offer, then call.
        Mirrors ``POST /api/issues`` and ``agnes issue report``.
        Args:
            title: one line, what is wrong (≤200 chars)
            body: what happened (≤8000)
            kind: bug | wrong_answer | request | question | other
            page_url: page or object the problem is about
            context: free-form JSON, strings capped at 300 chars server-side
        """
        payload = {k: v for k, v in {"title": title, "body": body, "kind": kind, "page_url": page_url, "context": context}.items() if v is not None}
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}/api/issues", json=payload, headers={**headers_fn(), **_ISSUE_HEADERS}, timeout=30)
            _raise_for_status_with_detail(r)
            return r.json()

    @tool(read_only=True, idempotent=True)
    async def list_my_issues(status: str = "open", limit: int = 50) -> dict:
        """List the caller's own issue reports (scoped server-side to the caller).
        status: open | resolved | all. Mirrors ``GET /api/issues/mine`` / ``agnes issue list``;
        admins see the whole queue with ``issue_queue_list``."""
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{base_url}/api/issues/mine", params={"status": status, "limit": limit}, headers={**headers_fn(), **_ISSUE_HEADERS}, timeout=30)
            _raise_for_status_with_detail(r)
            return ensure_output_size(r.json(), "list_my_issues", hint="lower `limit` or filter by status")
```

`get_issue(issue_id)` → `GET /api/issues/{issue_id}`; `issue_comment(issue_id, body)` → `POST …/comments`; `issue_queue_list(status="open", limit=100)` → `GET /api/admin/issues` (docstring: admin PAT); `issue_reply(issue_id, body)` → `POST /api/issues/{issue_id}/comments` (admin); `issue_resolve(issue_id, resolution_note=None)` → `POST /api/admin/issues/{issue_id}/resolve`. Append all seven to `FOUNDATION_TOOL_NAMES` with the sibling comment (`# REST /api/issues · CLI agnes issue report`).

`cli/mcp/server.py`: four sync tools with identical names/params/defaults:

```python
@tool(read_only=False)
def report_issue(title: str, body: str | None = None, kind: str = "bug", page_url: str | None = None, context: dict | None = None) -> dict:
    """Report a problem to this Agnes instance (bug | wrong_answer | request | question | other).
    Same tool as the server's `report_issue`; the report goes to the same queue."""
    payload = {k: v for k, v in {"title": title, "body": body, "kind": kind, "page_url": page_url, "context": context}.items() if v is not None}
    try:
        return api_post_json("/api/issues", payload, headers={"X-Agnes-Client": "mcp"})  # add the kwarg if v2_client supports headers; else drop it
    except V2ClientError as exc:
        raise ValueError(_mcp_error("report_issue", exc)) from exc
```

plus `list_my_issues`, `get_issue`, `issue_comment`. Add the four names to `STDIO_TOOL_NAMES` in `tests/test_mcp_tool_parity.py` under a new `# Issue reports` comment. `src/audit_posture.py::MCP_TOOL_POSTURE`:

```python
    "report_issue": "issue.report",
    "list_my_issues": "exempt:ui_support",
    "get_issue": "exempt:ui_support",
    "issue_comment": "issue.comment",
    "issue_queue_list": "exempt:ui_support",
    "issue_reply": "issue.comment",
    "issue_resolve": "issue.resolved",
```

`app/initial_workspace_default/CLAUDE.md`: in the rule that says to offer filing (around line 131), add one sentence: "For any problem that is not about the semantic layer — a tool that fails, a query that errors, a missing capability, an answer the user says is wrong — offer `report_issue` (or `agnes issue report`) with the context you have; `list_my_issues` answers 'what have I reported'." If a byte-identical builtin-marketplace copy of this file exists (grep for the same rule text under `src/_builtin_marketplace/`), mirror it.

Run: `.venv/bin/python -m pytest tests/test_mcp_issue_tools.py tests/test_mcp_tool_parity.py tests/test_documentation_api_triple_surface.py tests/test_audit_nonhttp_posture.py -q` — Expected: PASS.

- [ ] **Step 5: Parity, guards, commit**

Check `tests/test_cli_api_parity.py::parity_env` — if it can be pointed at a PG backend, add `TestIssueReportParity`, `TestIssueCommentParity`, `TestIssueResolveParity` in the file's class shape; if not (the `semantic_feedback` precedent), add one comment line in the file's preamble listing the issue routes under the PG-only exception. Run `.venv/bin/python scripts/verify_syncmap.py` and `--lane impacted`.

```bash
git add cli/commands/issue.py cli/commands/admin_issue.py cli/main.py cli/commands/admin.py cli/query_hints.py \
        app/api/mcp/foundation_tools.py cli/mcp/server.py src/audit_posture.py app/initial_workspace_default/CLAUDE.md \
        tests/test_cli_issue.py tests/test_mcp_issue_tools.py tests/test_mcp_tool_parity.py tests/test_cli_api_parity.py
git commit -m "feat(issues): agnes issue / admin issue CLI and report_issue MCP tools on both transports

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Web — rail entry, report dialog, client diagnostics, vendored html2canvas

**Files:**
- Modify: `app/web/router.py:6988-7100` (`_chrome_ctx` gains `can_report_issue`)
- Modify: `app/web/templates/_app_rail.html:638-700` (rail-foot button) and `:1077-1113` (user-menu button)
- Create: `app/web/templates/_issue_dialog.html`
- Modify: `app/web/templates/base_ds.html` (include the partial inside the `can_report_issue` gate, next to the rail include at `:158`)
- Modify: `app/web/templates/_app_scripts.html:1-30` (`window._agHtml2CanvasUrl`; `client_diag.js` first and non-deferred; `issue_report.js` deferred)
- Create: `app/web/static/js/client_diag.js`, `app/web/static/js/issue_report.js`, `app/web/static/css/issue_dialog.css` (or the dialog styles in the CSS file the modal system uses — follow `modal.js`'s stylesheet)
- Create: `app/web/static/vendor/html2canvas.min.js` (1.4.1 from cdnjs) + section in `app/web/static/vendor/LICENSES.md`
- Create: `docs/issue-reporting.md`; Modify: `docs/README.md` (index link)
- Test: `tests/test_web_static_assets.py` (presence + size), `tests/test_issue_dialog_template.py`

**Interfaces:**
- Consumes: Task 2's `POST /api/issues` (JSON, `credentials: "include"`, no CSRF token — Origin gate) and `PUT /api/issues/{id}/screenshot` (raw PNG); `src.repositories.use_pg()`; `window.appToast`, `modal.js` (global Escape), `ds.drawer_field`/`ds.drawer_select` macros, `static_url()`.
- Produces: `window.AgnesDiag.snapshot() -> {recent_errors: [...]}`; `window.AgnesIssueReport.open()`; DOM ids `rail-report-issue`, `rail-report-issue-menu`, `issue-dialog`.

- [ ] **Step 1: Template test first**

`tests/test_issue_dialog_template.py` (use `shared_app` + a signed-in client the way `tests/test_web_*` template tests render a page; find one that asserts on rail markup, e.g. grep `rail-i` in `tests/`):

```python
def test_rail_has_report_button_on_postgres(page_client, monkeypatch):
    monkeypatch.setattr("app.web.router.use_pg", lambda: True)   # patch where _chrome_ctx imports it
    html = page_client.get("/library").text
    assert 'id="rail-report-issue"' in html and "Report a problem" in html
    assert 'id="issue-dialog"' in html and "Include a screenshot of this page" in html
    assert "client_diag.js" in html and "issue_report.js" in html


def test_rail_hides_report_button_on_duckdb(page_client, monkeypatch):
    monkeypatch.setattr("app.web.router.use_pg", lambda: False)
    html = page_client.get("/library").text
    assert 'id="rail-report-issue"' not in html and 'id="issue-dialog"' not in html
```

In `tests/test_web_static_assets.py` add `test_html2canvas_present_and_substantial` (`> 100_000` bytes) and extend the license test's expectation if it checks section names. Run: expect FAIL.

- [ ] **Step 2: Chrome flag, rail, partial**

`app/web/router.py`, in `_chrome_ctx` next to `"can_data_apps"`:
```python
        "can_report_issue": _issue_reporting_available(),
```
with, near `_data_apps_nav_enabled`:
```python
def _issue_reporting_available() -> bool:
    """Issue reports are a PG-only table; show no button on a DuckDB instance
    rather than a button that answers 501. Read live, never cached."""
    try:
        from src.repositories import use_pg

        return bool(use_pg())
    except Exception:
        return False
```

`_app_rail.html` — rail foot, before "Take {brand} to your tools":
```html
{% if can_report_issue %}
<button type="button" class="rail-i" id="rail-report-issue">
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/>
        <circle cx="12" cy="12" r="3.5" stroke="currentColor" stroke-width="1.6"/>
        <path d="M5.6 5.6l3.9 3.9M14.5 14.5l3.9 3.9M18.4 5.6l-3.9 3.9M9.5 14.5l-3.9 3.9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    </svg>
    <span class="rail-i-label">Report a problem</span>
</button>
{% endif %}
```
Check `rail.css` for `button.rail-i` — if only `a.rail-i` is styled, add `button.rail-i { all: unset; … }` equivalents in `rail.css` under the existing `html[data-ui-layout="rail"]` scope so the button renders like the links. User menu, in the "Get your bearings" group:
```html
{% if can_report_issue %}
<button type="button" class="app-user-menu-item app-user-menu-btn" role="menuitem" id="rail-report-issue-menu">Report a problem</button>
{% endif %}
```

`_issue_dialog.html` (included from `base_ds.html` inside `{% if can_report_issue %}`; hidden by default via `hidden`):
```html
<div id="issue-dialog" class="modal-backdrop" hidden role="dialog" aria-modal="true" aria-labelledby="issue-dialog-title">
  <div class="modal issue-dialog">
    <h2 id="issue-dialog-title" class="modal-title">Report a problem</h2>
    <p class="issue-dialog__sub">We attach where you are. Just say what's wrong.</p>
    <div class="issue-dialog__kinds" role="radiogroup" aria-label="Kind">
      <label><input type="radio" name="issue-kind" value="bug" checked> Bug</label>
      <label><input type="radio" name="issue-kind" value="wrong_answer"> Wrong answer</label>
      <label><input type="radio" name="issue-kind" value="request"> Missing</label>
      <label><input type="radio" name="issue-kind" value="question"> Unclear</label>
    </div>
    {{ ds.drawer_field(id="issue-title", label="Title", placeholder="One line: what is wrong", maxlength=200) }}
    <label class="drawer-field" for="issue-body"><span class="drawer-field__label">What happened</span>
      <textarea id="issue-body" maxlength="8000" rows="4"></textarea></label>
    <div class="issue-dialog__attached">
      <span class="drawer-field__label">Attached automatically</span>
      <div id="issue-context-chips" class="issue-dialog__chips"></div>
    </div>
    <label class="issue-dialog__shot"><input type="checkbox" id="issue-screenshot" checked> Include a screenshot of this page
      <span class="issue-dialog__hint">The screenshot shows this page as you see it now.</span></label>
    <div id="issue-fallback" class="issue-dialog__fallback" hidden>
      <p>Couldn't send. Copy the text below and try again later.</p>
      <textarea id="issue-fallback-text" rows="5" readonly></textarea>
    </div>
    <div class="modal-actions">
      <button type="button" class="ds-btn" id="issue-cancel">Cancel</button>
      <button type="button" class="ds-btn ds-btn--primary" id="issue-submit">Report</button>
    </div>
  </div>
</div>
```
Use the real class names `modal.js` expects for backdrop/dialog/actions (read `modal.js` and its CSS first) so Escape and focus handling apply. Dialog-specific styles go in `app/web/static/css/issue_dialog.css` linked from the partial's head slot or from `base_ds.html`'s stylesheet list — never `<style>` in the body; `--ds-*` tokens only.

- [ ] **Step 3: Client diagnostics and dialog JS**

`app/web/static/js/client_diag.js` (loaded first, non-deferred):
```js
(function () {
  var MAX = 20, LIMIT = 300, buf = [];
  function push(entry) { buf.push(entry); if (buf.length > MAX) buf.shift(); }
  function trunc(s) { s = String(s == null ? "" : s); return s.length > LIMIT ? s.slice(0, LIMIT) : s; }
  function stripQuery(u) { try { var x = new URL(u, location.href); return x.origin + x.pathname; } catch (e) { return trunc(u); } }
  window.addEventListener("error", function (e) {
    push({ ts: new Date().toISOString(), kind: "error", message: trunc(e.message), source: trunc((e.filename || "") + ":" + (e.lineno || 0)) });
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason; push({ ts: new Date().toISOString(), kind: "rejection", message: trunc(r && (r.message || r)) });
  });
  if (window.fetch) {
    var orig = window.fetch;
    window.fetch = function (input, init) {
      var method = (init && init.method) || (input && input.method) || "GET";
      var url = typeof input === "string" ? input : (input && input.url) || "";
      return orig.apply(this, arguments).then(function (res) {
        if (!res.ok) push({ ts: new Date().toISOString(), kind: "fetch", message: method.toUpperCase() + " " + stripQuery(url), status: res.status });
        return res;
      }, function (err) {
        push({ ts: new Date().toISOString(), kind: "fetch", message: method.toUpperCase() + " " + stripQuery(url) + " failed: " + trunc(err && err.message) });
        throw err;
      });
    };
  }
  window.AgnesDiag = { snapshot: function () { return { recent_errors: buf.slice() }; } };
})();
```

`app/web/static/js/issue_report.js` (deferred):
```js
(function () {
  var dlg = document.getElementById("issue-dialog");
  if (!dlg) return;
  var SENSITIVE = ["token", "access_token", "code", "state"];
  function pageUrl() {
    var u = new URL(location.href); SENSITIVE.forEach(function (k) { u.searchParams.delete(k); }); return u.toString().slice(0, 2000);
  }
  function chatSession() { return new URL(location.href).searchParams.get("session") || null; }
  var versionCache = null;
  function version() {
    if (versionCache) return Promise.resolve(versionCache);
    return fetch("/api/version", { credentials: "include" }).then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (v) { versionCache = v; return v; }).catch(function () { return {}; });
  }
  function envelope(v) {
    var diag = window.AgnesDiag ? window.AgnesDiag.snapshot() : { recent_errors: [] };
    return { app_version: v.version, user_agent: navigator.userAgent, viewport: innerWidth + "x" + innerHeight,
             language: navigator.language, chat_session_id: chatSession(), recent_errors: diag.recent_errors,
             captured_at: new Date().toISOString() };
  }
  function chip(text) { var s = document.createElement("span"); s.className = "issue-chip"; s.textContent = text; return s; }
  function renderChips(env) {
    var box = document.getElementById("issue-context-chips"); box.textContent = "";
    box.appendChild(chip(new URL(pageUrl()).pathname));
    if (env.app_version) box.appendChild(chip("v" + env.app_version));
    box.appendChild(chip((navigator.userAgentData && navigator.userAgentData.brands && navigator.userAgentData.brands.slice(-1)[0].brand) || "browser"));
    if (env.chat_session_id) box.appendChild(chip("chat " + env.chat_session_id.slice(0, 8)));
    if (env.recent_errors.length) box.appendChild(chip(env.recent_errors.length + " recent errors"));
  }
  function open() {
    version().then(function (v) { renderChips(envelope(v)); dlg.hidden = false; document.getElementById("issue-title").focus(); });
  }
  function close() { dlg.hidden = true; document.getElementById("issue-fallback").hidden = true; }
  function loadHtml2Canvas() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    return new Promise(function (res, rej) {
      var s = document.createElement("script"); s.src = window._agHtml2CanvasUrl || "/static/vendor/html2canvas.min.js";
      s.onload = function () { res(window.html2canvas); }; s.onerror = rej; document.head.appendChild(s);
    });
  }
  function screenshotBlob() {
    dlg.hidden = true;                      // never capture the dialog itself
    return loadHtml2Canvas().then(function (h2c) { return h2c(document.body, { useCORS: true, scale: 1, logging: false }); })
      .then(function (canvas) { return new Promise(function (res) { canvas.toBlob(res, "image/png"); }); })
      .finally(function () { dlg.hidden = false; });
  }
  function submit() {
    var title = document.getElementById("issue-title").value.trim();
    if (!title) { document.getElementById("issue-title").focus(); return; }
    var body = document.getElementById("issue-body").value.trim();
    var kind = (dlg.querySelector('input[name="issue-kind"]:checked') || {}).value || "bug";
    var wantShot = document.getElementById("issue-screenshot").checked;
    var btn = document.getElementById("issue-submit"); btn.disabled = true;
    version().then(function (v) {
      var payload = { title: title, body: body || null, kind: kind, page_url: pageUrl(), context: envelope(v) };
      return fetch("/api/issues", { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
        .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
    }).then(function (res) {
      if (res.status !== 201) {
        var msg = (res.body && (res.body.message || (res.body.detail && res.body.detail.message))) || "Couldn't send the report.";
        if (res.status === 501) msg = "Reporting needs the Postgres app-state backend on this instance.";
        window.appToast && window.appToast({ kind: "warn", msg: msg });
        btn.disabled = false; return;
      }
      var row = res.body;
      var done = wantShot ? screenshotBlob().then(function (blob) {
        return fetch("/api/issues/" + row.id + "/screenshot", { method: "PUT", credentials: "include", headers: { "Content-Type": "image/png" }, body: blob });
      }).then(function (r) { if (!r.ok) window.appToast && window.appToast({ kind: "warn", msg: "Reported, but the screenshot was not attached." }); })
        .catch(function () { window.appToast && window.appToast({ kind: "warn", msg: "Reported, but the screenshot could not be captured." }); }) : Promise.resolve();
      return done.then(function () {
        close(); btn.disabled = false;
        document.getElementById("issue-title").value = ""; document.getElementById("issue-body").value = "";
        window.appToast && window.appToast({ kind: "ok", msg: "Reported as #" + row.number + " — you'll hear back." });
      });
    }).catch(function () {
      var fb = document.getElementById("issue-fallback"); fb.hidden = false;
      document.getElementById("issue-fallback-text").value = "[" + kind + "] " + title + "\n\n" + body + "\n\nPage: " + pageUrl();
      btn.disabled = false;
    });
  }
  ["rail-report-issue", "rail-report-issue-menu"].forEach(function (id) { var el = document.getElementById(id); if (el) el.addEventListener("click", open); });
  document.getElementById("issue-cancel").addEventListener("click", close);
  document.getElementById("issue-submit").addEventListener("click", submit);
  window.AgnesIssueReport = { open: open };
})();
```

Wire in `_app_scripts.html`: add `window._agHtml2CanvasUrl = "{{ static_url('vendor/html2canvas.min.js') }}";` to the cache-buster block; `<script src="{{ static_url('js/client_diag.js') }}"></script>` as the FIRST script tag in the file (before the cache-buster block is fine — it has no dependencies); `<script src="{{ static_url('js/issue_report.js') }}" defer></script>` after `modal.js`.

Vendor: `curl -sSL https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js -o app/web/static/vendor/html2canvas.min.js` and append to `LICENSES.md` in the file's section format (Project html2canvas · Version 1.4.1 · License MIT · Source https://github.com/niklasvh/html2canvas · Used in issue_report.js, lazy-loaded when a report includes a screenshot).

- [ ] **Step 4: Docs**

`docs/issue-reporting.md`: what the button does, what is attached (list the envelope keys, say explicitly that nothing is sent until "Report" is clicked and the screenshot is the page as rendered), privacy (screenshot and context stay in the instance; the webhook carries title/kind/page/version/links only), the config key with its env override, the CLI and MCP commands, how an admin works the queue today (`agnes admin issue …`), the Postgres requirement, and "what comes next" (support agent, `/me/issues`, tracker sync). Link it from `docs/README.md`.

- [ ] **Step 5: Verify in a browser, then commit**

Run `tests/test_issue_dialog_template.py tests/test_web_static_assets.py tests/test_design_system_contract.py tests/test_web_admin_nav.py -q` — Expected: PASS. Then with a Postgres dev stack (see `docs/QUICKSTART.md`; the `local-chat-ui` skill explains running the server locally) open `/chat`, click "Report a problem", submit with the screenshot on, and confirm: the 201, the PNG under `DATA_DIR/issues/<id>/`, `agnes issue list` shows it. If no PG stack is available locally, state that in the commit body and leave the live check to the PR author.

```bash
git add app/web/router.py app/web/templates/_app_rail.html app/web/templates/_issue_dialog.html app/web/templates/base_ds.html \
        app/web/templates/_app_scripts.html app/web/static/js/client_diag.js app/web/static/js/issue_report.js \
        app/web/static/css/issue_dialog.css app/web/static/css/rail.css \
        app/web/static/vendor/html2canvas.min.js app/web/static/vendor/LICENSES.md \
        docs/issue-reporting.md docs/README.md tests/test_issue_dialog_template.py tests/test_web_static_assets.py
git commit -m "feat(issues): Report a problem from every page — rail entry, dialog with auto-captured context and screenshot

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Integration notes (for the orchestrator, not the builders)

- Order: Task 1 first (everything imports its repo), then Task 2, then Tasks 3 and 4 in parallel (disjoint files; both code against Task 2's routes).
- One migration in the whole plan (Task 1); nothing else touches `migrations/`.
- `changelog.d/issue-reporting-step1.md` is written once at integration:
  `### Added` — "Report a problem from any page (rail + user menu) with page, version, chat session, recent client errors and an optional screenshot attached automatically; `agnes issue report|list|show|comment`, `agnes admin issue list|show|reply|resolve`; MCP `report_issue` / `list_my_issues` / `get_issue` / `issue_comment` on both transports; `agnes_issues` / `agnes_issue_comments` internal tables so any agent can answer 'what have I reported'; optional `issues.webhook_url` mirror. Postgres app-state backend required."
- After integration: `scripts/verify_syncmap.py`, `--lane impacted`, `/agnes-review`, then push and open a **draft** PR; CI runs the full suite.
