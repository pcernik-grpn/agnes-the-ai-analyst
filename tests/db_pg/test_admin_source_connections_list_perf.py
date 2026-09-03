"""Perf regression: ``GET /api/admin/source-connections`` issues a bounded
number of SQL statements regardless of connection count.

Before this fix, ``list_connections`` called ``_with_secret_status`` once
PER ROW, and that helper made 3 round trips per row (plain-secret presence,
master-secret presence, derived chat-tools lookup) — so an instance with N
source connections cost roughly 3N+1 statements. ``_with_secret_status_many``
(``app/api/admin_source_connections.py``) replaces that with 3 bulk reads
total, independent of N.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from tests.db_pg._parity_sweep_util import build_seeded_client

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_connections(n: int) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    for i in range(n):
        repo.create(
            id=f"conn_{i}_{uuid4().hex[:6]}",
            name=f"Connection {i}",
            source_type="snowflake",
            config={"account": f"acct{i}"},
        )


def test_list_connections_query_count_is_bounded_not_linear_in_connection_count(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _seed_connections(15)

    import src.db_pg as db_pg

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_pg.get_engine()
    sa.event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get(
            "/api/admin/source-connections",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 15

    # Before the fix this was ~46 (3*15 + 1); the bulk-read shape stays
    # small regardless of N. A generous ceiling that would fail loudly if
    # the per-row N+1 ever comes back, without pinning an exact count.
    assert len(statements) < 10, (
        f"GET /api/admin/source-connections issued {len(statements)} statements for 15 connections "
        f"— expected a small, connection-count-independent number"
    )
