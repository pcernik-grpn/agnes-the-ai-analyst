"""Remediation B1: one key for sync state + status where admins look.

`sync_state.table_id` used to be written keyed by `table_registry.name`
(`app/api/sync.py`'s materialized pass, `src/orchestrator.py::
_update_sync_state`) while two admin-status readers joined it against
`table_registry` on `id` (`/admin/data-sources`'s pipeline strip,
`app/web/router.py::_table_delivery`). A table whose display name wasn't
already a valid identifier (spaces, uppercase — e.g. `name="Web Sessions"`,
id `web_sessions`) showed healthy sync status on one surface and "never
synced" on another for the exact same sync.

This file covers:
  - the writer/reader convergence (register a name != id table, write
    sync_state through the production helper, both readers agree);
  - the one-time backfill migration (`src.db._v123_to_v124`);
  - the new Sync column on `/admin/tables`.
"""

from __future__ import annotations

import duckdb


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Writer/reader convergence
# ---------------------------------------------------------------------------


def test_registry_and_table_delivery_agree_on_sync_status(seeded_app):
    """A table registered with a display name that isn't already a valid id
    (`name="Web Sessions"` -> id `web_sessions`) must show the SAME sync
    status on both `/api/admin/registry` and `_table_delivery()` (the
    data-sources pipeline strip / Tables lens delivery map) once a
    sync_state row is written through the production writer path.

    Before the fix, the write path wrote `sync_state.table_id="Web
    Sessions"` while `_table_delivery()` joins by registry `id`
    (`"web_sessions"`) — so this table's `last_sync` was non-null on the
    registry endpoint and null in the delivery map. Both must now agree.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "Web Sessions",
            "source_type": "keboola",
            "bucket": "in.c-web",
            "source_table": "sessions",
            "query_mode": "local",
        },
    )
    assert r.status_code == 201, r.text
    table_id = r.json()["id"]
    assert table_id == "web_sessions"
    assert table_id != "Web Sessions"

    # The production writer path: app/api/sync.py's materialized pass and
    # src/orchestrator.py::_update_sync_state both resolve the sync_state
    # key through this same helper before calling sync_state_repo().
    # update_sync() — never raw SQL.
    from src.repositories import sync_state_repo
    from src.sync_state_key import resolve_sync_state_key

    sync_state_repo().update_sync(
        table_id=resolve_sync_state_key("Web Sessions"),
        rows=42,
        file_size_bytes=1234,
        hash="deadbeef",
    )

    reg_resp = c.get("/api/admin/registry", headers=auth)
    assert reg_resp.status_code == 200
    reg_row = next(t for t in reg_resp.json()["tables"] if t["id"] == table_id)
    assert reg_row["last_sync"] is not None
    assert reg_row["last_sync_status"] == "ok"

    from app.web.router import _table_delivery

    delivery = _table_delivery()
    assert delivery.get(table_id, {}).get("last_sync") is not None


def test_resolve_sync_state_key_falls_back_to_name_when_unmatched(seeded_app):
    """A name with no matching table_registry row still gets a
    sync_state.table_id — under the name, not silently dropped."""
    from src.sync_state_key import resolve_sync_state_key

    assert resolve_sync_state_key("Nonexistent Table") == "Nonexistent Table"


# ---------------------------------------------------------------------------
# Backfill migration
# ---------------------------------------------------------------------------


def test_backfill_migrates_name_keyed_rows_to_registry_id(tmp_path):
    from src.db import _ensure_schema, _v123_to_v124

    db_path = tmp_path / "backfill.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    conn.execute("INSERT INTO table_registry (id, name) VALUES ('web_sessions', 'Web Sessions')")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('Web Sessions', 10, 'h1', 'ok')")
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, status) "
        "VALUES ('hist1', 'Web Sessions', current_timestamp, 'ok')"
    )
    # Orphan: no matching registry row — must survive unchanged, not be dropped.
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('Ghost Table', 5, 'h2', 'ok')")

    _v123_to_v124(conn)

    state_ids = {row[0] for row in conn.execute("SELECT table_id FROM sync_state").fetchall()}
    assert "web_sessions" in state_ids
    assert "Web Sessions" not in state_ids
    assert "Ghost Table" in state_ids

    history_ids = {row[0] for row in conn.execute("SELECT table_id FROM sync_history").fetchall()}
    assert "web_sessions" in history_ids
    assert "Web Sessions" not in history_ids

    # Data survives the rename.
    row = conn.execute("SELECT rows, hash FROM sync_state WHERE table_id = 'web_sessions'").fetchone()
    assert row == (10, "h1")
    conn.close()


def test_backfill_is_idempotent(tmp_path):
    from src.db import _ensure_schema, _v123_to_v124

    db_path = tmp_path / "backfill_idempotent.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    conn.execute("INSERT INTO table_registry (id, name) VALUES ('orders', 'orders')")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('orders', 3, 'h', 'ok')")

    _v123_to_v124(conn)
    _v123_to_v124(conn)  # re-run must not raise or change anything

    state_ids = {row[0] for row in conn.execute("SELECT table_id FROM sync_state").fetchall()}
    assert state_ids == {"orders"}
    conn.close()


def test_backfill_skips_collision_with_existing_row(tmp_path):
    """Pathological pre-existing state: a row already exists under the
    target id. The name-keyed row is left alone rather than raising on the
    sync_state.table_id primary-key collision."""
    from src.db import _ensure_schema, _v123_to_v124

    db_path = tmp_path / "backfill_collision.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    conn.execute("INSERT INTO table_registry (id, name) VALUES ('orders', 'Orders')")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('Orders', 1, 'h1', 'ok')")
    conn.execute("INSERT INTO sync_state (table_id, rows, hash, status) VALUES ('orders', 2, 'h2', 'ok')")

    _v123_to_v124(conn)  # must not raise

    state_ids = {row[0] for row in conn.execute("SELECT table_id FROM sync_state").fetchall()}
    assert state_ids == {"Orders", "orders"}
    conn.close()


# ---------------------------------------------------------------------------
# /admin/tables Sync column
# ---------------------------------------------------------------------------


def test_admin_tables_renders_sync_column(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    html = r.text
    assert "<th>Sync</th>" in html
    # The pill-rendering helper and its full status vocabulary (matching
    # admin_sync.html's `.status-pill`) — proves the column reuses the
    # /api/admin/registry fields the page already fetches, not a second
    # request.
    assert "function renderSyncPill(t)" in html
    assert "t.last_sync_status" in html
    assert "tbl-sync-pill--ok" in html
    assert "tbl-sync-pill--error" in html
    assert "tbl-sync-pill--pending" in html
