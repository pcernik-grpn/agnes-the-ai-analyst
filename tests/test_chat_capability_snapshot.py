"""_chat_capability_snapshot — chat empty-state capability panel (FAI-132 review F4).

Direct coverage for the server-side snapshot helper. It must count only the
caller's accessible tables (``get_accessible_tables`` + in-memory filter),
and resolve that set with a SINGLE call — never a per-row
``can_access_table`` N+1. Prior to this the helper had no direct test.
"""

from __future__ import annotations


def _register(table_id: str, name: str, source_type: str = "keboola") -> None:
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    conn = get_system_db()
    try:
        table_registry_repo().register(
            id=table_id,
            name=name,
            description=name,
            source_type=source_type,
            query_mode="materialized",
        )
    finally:
        conn.close()


def test_admin_snapshot_counts_all_registered_tables(seeded_app):
    """Admin (``get_accessible_tables`` -> None) counts every registered table."""
    from app.web import router
    from src.db import get_system_db
    from src.repositories import table_registry_repo

    _register("cap_admin_a", "cap_admin_a")
    _register("cap_admin_b", "cap_admin_b")

    conn = get_system_db()
    try:
        expected_total = len(table_registry_repo().list_all())
        snap = router._chat_capability_snapshot(conn, {"id": "admin1"})
    finally:
        conn.close()

    assert snap["tables_total"] == expected_total
    assert snap["tables_total"] == sum(snap["tables_by_source"].values())


def test_analyst_snapshot_reflects_grants(seeded_app):
    """Granting a table via a data package increases the analyst's count by
    exactly one — proving the in-memory filter reflects the resolved set."""
    from app.web import router
    from src.db import get_system_db
    from tests.conftest import grant_table_via_package

    _register("cap_an_granted", "cap_an_granted")

    conn = get_system_db()
    try:
        before = router._chat_capability_snapshot(conn, {"id": "analyst1"})["tables_total"]
        grant_table_via_package(conn, "cap_an_granted", "analyst1")
        after = router._chat_capability_snapshot(conn, {"id": "analyst1"})["tables_total"]
    finally:
        conn.close()

    assert after == before + 1


def test_analyst_sees_fewer_tables_than_admin(seeded_app):
    """An ungranted table is counted for admin but not for the analyst —
    fail-closed filtering, not overcounting the empty state."""
    from app.web import router
    from src.db import get_system_db

    _register("cap_priv_x", "cap_priv_x")  # ungranted for analyst

    conn = get_system_db()
    try:
        admin_total = router._chat_capability_snapshot(conn, {"id": "admin1"})["tables_total"]
        analyst_total = router._chat_capability_snapshot(conn, {"id": "analyst1"})["tables_total"]
    finally:
        conn.close()

    assert analyst_total < admin_total


def test_snapshot_resolves_accessible_tables_once(seeded_app, monkeypatch):
    """N+1 regression guard: the snapshot must resolve the accessible set with
    a single ``get_accessible_tables`` call, not one per registered table."""
    from app.web import router
    from src.db import get_system_db
    import src.rbac as rbac_module

    for n in range(3):
        _register(f"cap_once_{n}", f"cap_once_{n}")

    calls = {"n": 0}
    real = rbac_module.get_accessible_tables

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    # The helper does a call-time ``from src.rbac import get_accessible_tables``,
    # so patching the source module attribute is what it resolves.
    monkeypatch.setattr(rbac_module, "get_accessible_tables", _counting)

    conn = get_system_db()
    try:
        router._chat_capability_snapshot(conn, {"id": "analyst1"})
    finally:
        conn.close()

    assert calls["n"] == 1


def test_the_snapshot_carries_is_admin(seeded_app):
    """The dashboard's admin starters hang off this one key.

    ``buildSuggestedActions`` in ``chat_dashboard.js`` picks ``ADMIN_TASKS`` /
    ``ADMIN_ZERO_TASKS`` from ``_capabilities.is_admin``, and the snapshot
    embedded in ``chat.html`` is the only thing that sets it. Drop the key and
    nothing errors — ``!!undefined`` is false, so every admin quietly falls
    through to the member starters and is offered "ask for access" on an
    instance they administer. Raised as a question by a review bot on PR #1679;
    the key was there, the guard was not.
    """
    from app.web import router
    from src.db import get_system_db

    conn = get_system_db()
    try:
        admin = router._chat_capability_snapshot(conn, {"id": "admin1"})
        analyst = router._chat_capability_snapshot(conn, {"id": "analyst1"})
    finally:
        conn.close()

    assert admin["is_admin"] is True
    assert analyst["is_admin"] is False


def test_the_zero_data_override_keeps_is_admin(seeded_app):
    """The chat route rewrites the snapshot when the caller has no tables
    (``tables_total``/``tables_by_source`` forced to empty). That rewrite is a
    spread, so it must not drop the key the zero-data starters need — which is
    precisely the state ``ADMIN_ZERO_TASKS`` exists for.
    """
    import inspect

    from app.web import router

    src = inspect.getsource(router)
    assert '{**ctx["chat_capabilities"], "tables_total": 0, "tables_by_source": {}}' in src, (
        "the zero-data override must spread the snapshot, not rebuild it — "
        "rebuilding drops is_admin and sends admins the member starters"
    )
