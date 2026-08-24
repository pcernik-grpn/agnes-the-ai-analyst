"""Regression guard for PUT field preservation.

Locks the Pydantic semantics that the Phase F form-cleanup relies on:
when the Edit modal omits a field from its payload, the existing value
must survive. If a future maintainer flips ``model_dump()`` to
``exclude_unset=True`` or otherwise changes the partial-update semantics,
these tests fire before partitioned rows or primary keys silently
regress.
"""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_put_preserves_omitted_sync_strategy(seeded_app):
    """v26: sync_strategy drives the extractor dispatcher and is enforced
    against {full_refresh, incremental, partitioned}. partitioned requires
    partition_by, so we use partition_by + partition_granularity here to
    pass the model validator while still verifying the PUT-preservation
    invariant: a body that omits sync_strategy must not erase it."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "events_partitioned",
            "source_type": "keboola",
            "bucket": "in.c-events",
            "source_table": "events",
            "query_mode": "local",
            "sync_strategy": "partitioned",
            "partition_by": "event_date",
            "partition_granularity": "month",
        },
    )
    assert r.status_code == 201, r.text

    r = c.put(
        "/api/admin/registry/events_partitioned",
        headers=auth,
        json={
            "sync_schedule": "daily 03:00",
            "description": "now daily",
        },
    )
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "events_partitioned")
    assert row["sync_strategy"] == "partitioned"
    assert row["partition_by"] == "event_date"


def test_put_preserves_omitted_primary_key(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_with_pk",
            "source_type": "keboola",
            "bucket": "in.c-shop",
            "source_table": "orders",
            "query_mode": "local",
            "primary_key": ["order_id", "tenant_id"],
        },
    )
    assert r.status_code == 201, r.text

    r = c.put(
        "/api/admin/registry/orders_with_pk",
        headers=auth,
        json={
            "description": "shop orders",
        },
    )
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "orders_with_pk")
    assert row["primary_key"] == ["order_id", "tenant_id"]


def test_put_does_not_typeerror_once_a_policy_is_set(seeded_app):
    """v116 (table access policies, Task 2): PUT's read-modify-write loop
    merges the full ``SELECT *`` row back into ``register(**merged)``. Once
    an access policy is attached, that merged dict carries the five
    ``access_policy_*`` / ``policy_mapping`` keys ``register()`` does not
    accept as kwargs — regression guard for the ``register(**merged)`` trap
    (design doc §18): every PUT on a policied table would otherwise 500 with
    a ``TypeError``, not just ones that touch the policy fields.

    Registered ``server_only=true`` (not the plain ``local`` Task 2 used):
    Task 4's distribution interlock (design doc §3.1) now rejects ANY PUT
    that would leave a policy on a distributable row, so a policied-but-
    distributable fixture would 422 on the very ``description``-only PUT
    below for a real, unrelated reason — this test wants to isolate the
    ``register(**merged)`` TypeError regression alone."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "policied_tbl",
            "source_type": "keboola",
            "query_mode": "local",
            "server_only": True,
        },
    )
    assert r.status_code == 201, r.text
    table_id = r.json()["id"]

    from src.repositories import table_registry_repo

    table_registry_repo().set_access_policy(
        table_id,
        sql="SELECT * FROM policied_tbl WHERE owner = $user_email",
        note="restrict to owner",
        updated_by="admin@test.com",
    )

    r = c.put(
        f"/api/admin/registry/{table_id}",
        headers=auth,
        json={
            "description": "still fine",
        },
    )
    assert r.status_code == 200, r.text

    r = c.get("/api/admin/registry", headers=auth)
    row = next(t for t in r.json()["tables"] if t["id"] == table_id)
    assert row["description"] == "still fine"
    # An unrelated PUT must not disturb the policy already attached.
    assert row["access_policy_sql"] == "SELECT * FROM policied_tbl WHERE owner = $user_email"


def _register_two_tables(c, auth):
    """`Orders Alpha` (id `orders_alpha`, id != name) and `Orders Beta`
    (id `orders_beta`) — the id/name split on the first row is what lets
    the two collision tests below tell "collides on name" apart from
    "collides on id" (`orders_alpha` is Alpha's id but not its name)."""
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={"name": "Orders Alpha", "source_type": "keboola", "query_mode": "local"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "orders_alpha"

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={"name": "Orders Beta", "source_type": "keboola", "query_mode": "local"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "orders_beta"


def test_put_rename_rejects_collision_with_another_tables_name(seeded_app):
    """table_registry.name has no DB-level uniqueness constraint and PUT
    never re-derives `id` from a renamed `name` (unlike POST, whose
    slugified-id collision check catches this indirectly) — an unchecked
    rename could silently shadow another table's display name, the same
    concern register_table's own `existing_by_name` guard exists for."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)
    _register_two_tables(c, auth)

    r = c.put(
        "/api/admin/registry/orders_beta",
        headers=auth,
        json={"name": "Orders Alpha"},
    )
    assert r.status_code == 409, r.text
    assert "orders_alpha" in r.text


def test_put_rename_rejects_collision_with_another_tables_id(seeded_app):
    """B1: sync_state/manifest readers resolve a raw key against the
    registry BY ID first (`app/api/sync.py::_reg_for`, the distribution
    mirror job, `list_registry`) before falling back to name — a rename
    that collides with another table's id (not its name) would still
    misroute a legacy name-keyed sync_state row to the wrong registry
    entry, so this must be rejected exactly like a name collision."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)
    _register_two_tables(c, auth)

    r = c.put(
        "/api/admin/registry/orders_beta",
        headers=auth,
        json={"name": "orders_alpha"},  # Alpha's id, not its "Orders Alpha" name
    )
    assert r.status_code == 409, r.text
    assert "orders_alpha" in r.text


def test_put_rename_to_a_free_name_succeeds(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)
    _register_two_tables(c, auth)

    r = c.put(
        "/api/admin/registry/orders_beta",
        headers=auth,
        json={"name": "Orders Beta Renamed"},
    )
    assert r.status_code == 200, r.text

    r = c.get("/api/admin/registry", headers=auth)
    row = next(t for t in r.json()["tables"] if t["id"] == "orders_beta")
    assert row["name"] == "Orders Beta Renamed"
