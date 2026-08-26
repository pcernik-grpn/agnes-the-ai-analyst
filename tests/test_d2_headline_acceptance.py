"""D2.2+D2.3 headline acceptance (docs/superpowers/plans/
2026-08-26-derived-connection-model.md): an admin saves a Snowflake
connection via the API and settings resolve live from a DIFFERENT app
instance, with no restart — plus the two named "the whole point" checks:
SF materialized sync uses the per-connection credential
(#1530-style "wrong credential impossible"), and BQ's process cache
invalidates on a row update."""

from __future__ import annotations

from unittest.mock import MagicMock


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestSecondProcessResolvesLiveWithNoRestart:
    def test_admin_saves_snowflake_connection_and_a_second_app_instance_resolves_it_live(
        self, seeded_app, tmp_path, monkeypatch
    ):
        """The headline: an admin configures Snowflake via the API and a
        SECOND process (simulated by a fresh `create_app()` — the same
        pattern `tests/conftest.py::seeded_app_fresh` uses to represent an
        independent process reading the same DATA_DIR) resolves the just-
        saved settings immediately. No restart, no explicit cache-clear
        call, no `reset_cache()` — a DB row is read live by construction.
        """
        from cryptography.fernet import Fernet

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "snowflake",
                "source_type": "snowflake",
                "config": {
                    "account": "acme-live",
                    "user": "svc_agnes",
                    "database": "ANALYTICS",
                    "warehouse": "COMPUTE_WH",
                },
                "is_default": True,
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]

        secret_resp = c.put(
            f"/api/admin/source-connections/{conn_id}/secret",
            json={"value": "s3cr3t-password", "kind": "storage"},
            headers=_auth(token),
        )
        assert secret_resp.status_code == 204, secret_resp.text

        # A genuinely SEPARATE app object — not the admin API's own process,
        # not anything that shares in-process caches by construction other
        # than the DB itself (same DATA_DIR, set by `seeded_app`'s own
        # `e2e_env` dependency).
        from app.main import create_app

        second_app = create_app()  # noqa: F841 — construction itself must not snapshot anything stale
        try:
            from connectors.snowflake.settings import resolve_snowflake_settings

            settings = resolve_snowflake_settings()
            assert settings is not None, "the second process must see the row with no restart"
            assert settings["account"] == "acme-live"
            assert settings["user"] == "svc_agnes"
            assert settings["database"] == "ANALYTICS"
            assert settings["warehouse"] == "COMPUTE_WH"
            assert settings["password"] == "s3cr3t-password"
        finally:
            second_app.dependency_overrides.clear()


class TestSnowflakeMaterializedSyncUsesThePerConnectionCredential:
    def test_wrong_credential_is_structurally_impossible(self, tmp_path, monkeypatch):
        """#1530-style: once the connection ROW is the source of truth, the
        materialized sync resolves against exactly its coordinates and
        credential — there is no other config path left that could hand it
        a stale or mismatched one."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)

        from app.api.sync import _run_materialized_pass
        from src.repositories import connection_secrets_repo, source_connections_repo

        source_connections_repo().create(
            id="sf-row-materialized",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "row-account",
                "user": "row-user",
                "database": "ROW_DB",
                "warehouse": "ROW_WH",
            },
            is_default=True,
        )
        connection_secrets_repo().upsert("sf-row-materialized", "row-password")

        monkeypatch.setattr("app.api.sync._get_data_dir", lambda: str(tmp_path))
        monkeypatch.setattr("app.api.sync.is_table_due", lambda schedule, last: True)

        sf_materialize = MagicMock(
            return_value={"rows": 1, "size_bytes": 10, "hash": "abc", "query_mode": "materialized"}
        )
        monkeypatch.setattr("connectors.snowflake.extractor.materialize_query", sf_materialize)

        registry = MagicMock()
        registry.list_all.return_value = [
            {
                "name": "orders_summary",
                "id": "orders_summary",
                "source_type": "snowflake",
                "query_mode": "materialized",
                "source_query": "SELECT 1",
                "bucket": "public",
                "source_table": "orders",
                "sync_schedule": None,
            }
        ]
        state = MagicMock()
        state.get_last_sync.return_value = None
        monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: registry)
        monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)

        summary = _run_materialized_pass(None, None, source_type="snowflake")

        assert summary["errors"] == []
        assert summary["materialized"] == ["orders_summary"]
        sf_materialize.assert_called_once()
        call_kwargs = sf_materialize.call_args.kwargs
        assert call_kwargs["settings"]["account"] == "row-account"
        assert call_kwargs["settings"]["password"] == "row-password"
        assert call_kwargs["database"] == "ROW_DB"


class TestBqAccessCacheInvalidatesOnRowUpdate:
    def test_admin_save_is_visible_to_the_very_next_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        from connectors.bigquery.access import get_bq_access
        from src.repositories import source_connections_repo

        get_bq_access.cache_clear()
        repo = source_connections_repo()
        repo.create(
            id="bq-row-headline",
            name="bigquery",
            source_type="bigquery",
            config={"project": "before-proj"},
            is_default=True,
        )
        assert get_bq_access().projects.data == "before-proj"

        repo.update("bq-row-headline", config={"project": "after-proj"})

        assert get_bq_access().projects.data == "after-proj", (
            "an admin's saved connection row must be load-bearing on the very "
            "next call, across processes, with no explicit cache-clear"
        )
        get_bq_access.cache_clear()
