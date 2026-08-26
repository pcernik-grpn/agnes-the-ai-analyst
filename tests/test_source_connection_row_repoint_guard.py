"""D2.3: connection identity for Snowflake/Databricks moved off the
``data_source.<type>`` server-config yaml overlay and onto the connection
row — the repoint 409 guard (`app.connection_identity.identity_changes`)
follows it here, onto ``PUT /api/admin/source-connections/{id}``."""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_snowflake_table(table_id: str) -> None:
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id=table_id,
        name=table_id,
        source_type="snowflake",
        bucket="GOLD",
        source_table=table_id.upper(),
        query_mode="remote",
    )


class TestRowRepointGuard:
    def test_identity_change_with_registrations_is_refused(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-guarded",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"
        assert detail["changes"] == [{"field": "database", "before": "PROD", "after": "GOLD"}]
        assert "confirm_connection_change" in detail["hint"]

    def test_refused_repoint_leaves_the_row_untouched(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-untouched",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_untouched")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text

        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["account"] == "acme-prod"

    def test_repoint_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-confirmed",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_confirmed")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={
                "config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"},
                "confirm_connection_change": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["config"]["database"] == "GOLD"

    def test_tuning_field_is_not_guarded(self, seeded_app):
        """A tuning knob (not connection identity) is not a repoint."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-tuning",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_tuning")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={
                "config": {
                    "account": "acme-prod",
                    "user": "svc",
                    "database": "PROD",
                    "warehouse": "WH",
                    "max_bytes_per_materialize": 1024,
                }
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_repoint_without_registrations_is_not_guarded(self, seeded_app):
        """First-time setup — nothing registered yet — has nothing to break."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-fresh",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_create_with_is_default_repointing_the_default_is_refused(self, seeded_app):
        """RBAC review Finding 1 (CREATE bypass): ``POST`` a new connection
        with ``is_default: true`` for a source_type that already has a
        default connection WITH registrations must trip the same 409 the
        config-repoint guard applies — the repo's create() unconditionally
        demotes the current default, so this is a repoint, only via
        ``is_default`` instead of ``config``."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-current-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]
        _register_snowflake_table("create_bypass_orders")

        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-new-default",
                "source_type": "snowflake",
                "config": {"account": "different-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"

        # The demoted-by-bypass connection must still be the default.
        row = c.get(f"/api/admin/source-connections/{first_id}", headers=_auth(token)).json()
        assert row["is_default"] is True

    def test_create_with_is_default_repoint_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-current-default-2",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        first_id = first.json()["id"]
        _register_snowflake_table("create_bypass_confirmed_orders")

        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-new-default-2",
                "source_type": "snowflake",
                "config": {"account": "different-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
                "confirm_connection_change": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        second_id = resp.json()["id"]
        assert resp.json()["is_default"] is True

        row = c.get(f"/api/admin/source-connections/{first_id}", headers=_auth(token)).json()
        assert row["is_default"] is False
        row2 = c.get(f"/api/admin/source-connections/{second_id}", headers=_auth(token)).json()
        assert row2["is_default"] is True

    def test_update_is_default_repoint_without_config_is_refused(self, seeded_app):
        """RBAC review Finding 1 (UPDATE bypass): ``PUT /{other_id}
        {is_default: true}`` with NO ``config`` key never reached
        ``_guard_row_repoint`` (gated on ``config is not None``), yet
        ``other_id`` silently becomes the default, demoting the current one."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-first-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        first_id = first.json()["id"]
        second = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-second-not-default",
                "source_type": "snowflake",
                "config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        second_id = second.json()["id"]
        assert second.json()["is_default"] is False
        _register_snowflake_table("update_bypass_orders")

        resp = c.put(
            f"/api/admin/source-connections/{second_id}",
            json={"is_default": True},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"

        row = c.get(f"/api/admin/source-connections/{first_id}", headers=_auth(token)).json()
        assert row["is_default"] is True

    def test_update_is_default_repoint_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-first-default-2",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        first_id = first.json()["id"]
        second = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-second-not-default-2",
                "source_type": "snowflake",
                "config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        second_id = second.json()["id"]
        _register_snowflake_table("update_bypass_confirmed_orders")

        resp = c.put(
            f"/api/admin/source-connections/{second_id}",
            json={"is_default": True, "confirm_connection_change": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_default"] is True

        row = c.get(f"/api/admin/source-connections/{first_id}", headers=_auth(token)).json()
        assert row["is_default"] is False

    def test_empty_config_put_on_registrations_backed_row_is_refused(self, seeded_app):
        """RBAC review Finding 2: ``PUT`` REPLACES ``config`` wholesale, and
        ``identity_changes`` treated an absent leaf as "untouched" — correct
        for the yaml-overlay PATCH caller, wrong here. ``{config: {}}`` on a
        registrations-backed row must trip the guard instead of silently
        wiping account/user/token_env."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-wipe",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("wipe_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"

        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["account"] == "acme-prod"

    def test_empty_config_put_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-wipe-confirmed",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("wipe_confirmed_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {}, "confirm_connection_change": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["config"] == {}

    def test_empty_config_put_on_fresh_row_still_works(self, seeded_app):
        """The wizard's bootstrap flow — an empty config on a brand-new row
        with no registrations — must keep working unconfirmed."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={"name": "sf-bootstrap", "source_type": "snowflake", "config": {}},
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_bigquery_row_is_not_guarded_by_this_slice(self, seeded_app):
        """Keboola/BigQuery identity relocation is out of scope for D2.3 —
        their row edits are unaffected by this guard."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={"name": "bq-unguarded", "source_type": "bigquery", "config": {"project": "proj-a"}},
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id="rguard_bq",
            name="rguard_bq",
            source_type="bigquery",
            bucket="analytics",
            source_table="SESSIONS",
            query_mode="remote",
        )

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"project": "proj-b"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text


class TestDefaultDemoteToNoneGuard:
    """Second RBAC review round (2026-08-26): ``_guard_default_repoint`` only
    fired on a promote-a-different-row transition. ``PUT /{current_default_id}
    {is_default: false}`` demotes the current default to NO default at all —
    ``resolve_source_connection(type)`` then returns ``None`` and every
    unpinned registration of that source_type silently falls back to the
    legacy ``data_source.<type>.*`` yaml (which this slice does not delete),
    resurrecting a possibly stale/different upstream with no confirmation.
    Same threat model as promoting a different row; guarded the same way."""

    def test_demote_to_none_on_registrations_backed_default_is_refused(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-demote-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("demote_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"is_default": False},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"

        # Unconfirmed — the row must still be the default.
        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert row["is_default"] is True

    def test_demote_to_none_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-demote-confirmed",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("demote_confirmed_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"is_default": False, "confirm_connection_change": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_default"] is False

    def test_demote_to_none_without_registrations_is_not_guarded(self, seeded_app):
        """First-time setup — nothing registered yet — has nothing to break."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-demote-fresh",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"is_default": False},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_demote_to_false_on_a_row_that_is_not_the_default_is_a_no_op(self, seeded_app):
        """``is_default: false`` on a row that is already not the default
        changes nothing about which row resolves — no guard needed."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-noop-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        second = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-noop-not-default",
                "source_type": "snowflake",
                "config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        second_id = second.json()["id"]
        _register_snowflake_table("noop_orders")

        resp = c.put(
            f"/api/admin/source-connections/{second_id}",
            json={"is_default": False},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        row = c.get(f"/api/admin/source-connections/{first.json()['id']}", headers=_auth(token)).json()
        assert row["is_default"] is True


class TestDeleteDefaultConnectionGuard:
    """Second RBAC review round (2026-08-26): ``delete_connection`` only
    blocked when a table_registry row is PINNED to the deleted connection via
    ``connection_id``. It never checked whether the row being deleted is the
    source_type's current DEFAULT, whose unpinned registrations resolve
    THROUGH it (``resolve_source_connection``) — deleting the sole/default
    Snowflake/Databricks connection broke every such registration's
    resolution with no 409/confirm. Reuses ``_guard_default_repoint``'s
    affected-registrations notion."""

    def test_delete_default_with_registrations_is_refused(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-delete-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("delete_default_orders")

        resp = c.delete(f"/api/admin/source-connections/{conn_id}", headers=_auth(token))
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"

        # Unconfirmed — the row must still exist.
        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token))
        assert row.status_code == 200

    def test_delete_default_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-delete-default-confirmed",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("delete_default_confirmed_orders")

        resp = c.delete(
            f"/api/admin/source-connections/{conn_id}?confirm_connection_change=true",
            headers=_auth(token),
        )
        assert resp.status_code == 204, resp.text

        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token))
        assert row.status_code == 404

    def test_delete_non_default_connection_still_works(self, seeded_app):
        """A non-default connection has nothing resolving through it by
        default — deleting it is unaffected by this guard (the pre-existing
        pinned-table check still applies separately)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-delete-keep-default",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        second = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-delete-not-default",
                "source_type": "snowflake",
                "config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        second_id = second.json()["id"]
        _register_snowflake_table("delete_not_default_orders")

        resp = c.delete(f"/api/admin/source-connections/{second_id}", headers=_auth(token))
        assert resp.status_code == 204, resp.text

    def test_delete_default_with_no_registrations_still_works(self, seeded_app):
        """First-time setup — nothing registered yet — has nothing to break."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-delete-fresh",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
                "is_default": True,
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]

        resp = c.delete(f"/api/admin/source-connections/{conn_id}", headers=_auth(token))
        assert resp.status_code == 204, resp.text
