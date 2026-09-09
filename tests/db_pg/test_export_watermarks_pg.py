"""``export_watermarks`` — the conversation-corpus push sink's resume point,
on Postgres.

PG-side by necessity: the table is Postgres-only (A3 PG-first ratchet —
``CLAUDE.md`` -> "Dual-backend discipline"), so there is no DuckDB sibling to
parametrize against. The push job's behaviour over this repo is exercised in
``tests/db_pg/test_conversation_export_push_pg.py``; this file pins the repo's
own contract: a watermark is always a ``(timestamp, cursor id)`` pair, never a
bare timestamp (design ``2026-09-08-llm-observability-design.md`` §3.12).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    from src.repositories.export_watermarks_pg import ExportWatermarksPgRepository

    return ExportWatermarksPgRepository(pg_engine)


def test_a_sink_that_never_delivered_has_no_watermark(repo):
    assert repo.get("conversation-export") is None


def test_set_then_get_returns_the_keyset_pair(repo):
    ts = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    out = repo.set("conversation-export", ts, "sess_0001")
    assert out == {"name": "conversation-export", "watermark": ts, "cursor_id": "sess_0001"}
    got = repo.get("conversation-export")
    assert got is not None
    assert got[0] == ts
    assert got[1] == "sess_0001"


def test_set_is_an_upsert_that_replaces_both_fields(repo):
    ts = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    repo.set("conversation-export", ts, "sess_0001")
    later = ts + timedelta(minutes=5)
    repo.set("conversation-export", later, "sess_0002")
    assert repo.get("conversation-export") == (later, "sess_0002")


def test_names_are_independent(repo):
    ts = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    repo.set("conversation-export", ts, "sess_0001")
    assert repo.get("some-other-sink") is None
    assert repo.get("conversation-export") == (ts, "sess_0001")
