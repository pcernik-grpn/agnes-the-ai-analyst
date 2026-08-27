"""PG-backend round trip for the auto-draft sweep's dedup flag.

``table_registry.semantic_draft_pending_at`` is a Postgres-only column (A3
PG-first ratchet — the DuckDB app-state migration ladder is frozen at v124,
see CLAUDE.md -> "Dual-backend discipline" and
``migrations/versions/0076_semantic_draft_pending.py``). The DuckDB-side
no-op is covered in ``tests/test_semantic_apply.py``
(``test_approve_succeeds_despite_pg_only_dedup_column`` /
``test_reject_succeeds_despite_pg_only_dedup_column``) and
``tests/test_semantic_autodraft.py``'s ``TestClearPendingForDocument``; this
file proves the capability actually works on a Postgres-backend instance —
first the HTTP round trip through the real
``/api/admin/authoring-suggestions/{id}/approve`` and ``/reject`` endpoints,
then ``src.semantic_autodraft.clear_pending_for_document`` directly for the
multi-table / partial-clear cases the pre-A3 file used to pin.

Uses ``state_backend`` + ``seeded_app_both`` (the dual-backend endpoint
harness) and skips the DuckDB param per the fixture's own documented
pattern for PG-only assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: support\n"
    "    datasets:\n"
    "      - name: tickets\n"
    "        source: db.public.tickets\n"
    "        fields: []\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _apply(client, token):
    return client.post(
        "/api/semantic-models/apply",
        headers=_auth(token),
        json={"document": DOC},
    )


@pytest.fixture
def registry(pg_engine, monkeypatch):
    """Per-test ``table_registry`` PG repo bound to a freshly-migrated schema —
    for direct ``clear_pending_for_document`` calls, no HTTP layer involved."""
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

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    return TableRegistryPgRepository(db_pg.get_engine())


def test_clears_the_flag_for_every_resolved_table(registry):
    from src.semantic_autodraft import clear_pending_for_document

    registry.register(id="orders", name="orders", source_type="local", query_mode="local")
    registry.register(id="customers", name="customers", source_type="local", query_mode="local")
    registry.mark_semantic_draft_pending("orders")
    registry.mark_semantic_draft_pending("customers")

    document = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [
                    {"name": "orders", "source": "orders", "fields": []},
                    {"name": "customers", "source": "customers", "fields": []},
                ],
            }
        ]
    }
    clear_pending_for_document(document)

    assert registry.get("orders")["semantic_draft_pending_at"] is None
    assert registry.get("customers")["semantic_draft_pending_at"] is None


def test_leaves_other_tables_pending_flag_untouched(registry):
    from src.semantic_autodraft import clear_pending_for_document

    registry.register(id="orders", name="orders", source_type="local", query_mode="local")
    registry.register(id="untouched", name="untouched", source_type="local", query_mode="local")
    registry.mark_semantic_draft_pending("orders")
    registry.mark_semantic_draft_pending("untouched")

    clear_pending_for_document(
        {"semantic_model": [{"name": "m", "datasets": [{"name": "orders", "source": "orders"}]}]}
    )

    assert registry.get("orders")["semantic_draft_pending_at"] is None
    assert registry.get("untouched")["semantic_draft_pending_at"] is not None


def test_approve_clears_semantic_draft_pending_flag_on_pg(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only")

    from src.repositories import table_registry_repo

    registry = table_registry_repo()
    registry.register(id="db.public.tickets", name="db.public.tickets", source_type="local")
    registry.mark_semantic_draft_pending("db.public.tickets")

    c = seeded_app_both["client"]
    sid = _apply(c, seeded_app_both["analyst_token"]).json()["suggestion_id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app_both["admin_token"]),
        json={},
    )
    assert r.status_code == 200, r.text

    row = registry.get("db.public.tickets")
    assert row["semantic_draft_pending_at"] is None


def test_reject_also_clears_semantic_draft_pending_flag_on_pg(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only")

    from src.repositories import table_registry_repo

    registry = table_registry_repo()
    registry.register(id="db.public.tickets", name="db.public.tickets", source_type="local")
    registry.mark_semantic_draft_pending("db.public.tickets")

    c = seeded_app_both["client"]
    sid = _apply(c, seeded_app_both["analyst_token"]).json()["suggestion_id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/reject",
        headers=_auth(seeded_app_both["admin_token"]),
        json={"note": "not good enough"},
    )
    assert r.status_code == 200, r.text

    row = registry.get("db.public.tickets")
    assert row["semantic_draft_pending_at"] is None
