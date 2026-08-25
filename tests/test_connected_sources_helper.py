"""Tests for `_connected_sources()` and its two consumers.

"Is source type X reachable on this instance?" had two stores that each
answered it PARTIALLY:

- The `source_connections` registry (`_connected_source_types()`) — true for
  a source added through the multi-connection wizard, but Snowflake and
  Databricks are credentialed at the instance level and no registry row is
  ever seeded for them (`app/connections_seed.py` only seeds keboola +
  bigquery).
- The legacy `data_source.type` scalar (`get_data_source_type()`) — true for
  whatever an instance was first configured with, but `"local"` (and its
  CLI-facing alias `"csv"`) is the UNSET SENTINEL, not an assertion that
  local files are connected.

`_connected_sources()` (Task 1) is the union of both stores plus the
existing instance-level credential probes for BigQuery/Snowflake/Databricks.
`/admin/tables` (Task 2/3) and the derived Keboola card on
`/admin/data-sources` (Task 4) are its two consumers exercised here.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeConnRepo:
    """Stand-in for `source_connections_repo()` — `.list()` only."""

    def __init__(self, rows=None, raise_on_list: bool = False):
        self._rows = rows or []
        self._raise = raise_on_list

    def list(self, *args, **kwargs):
        if self._raise:
            raise RuntimeError("registry unreadable (test)")
        return list(self._rows)


def _patch_registry(monkeypatch, rows=None, raise_on_list=False):
    monkeypatch.setattr(
        "src.repositories.source_connections_repo",
        lambda: _FakeConnRepo(rows, raise_on_list=raise_on_list),
        raising=False,
    )


def _patch_credential_probes(monkeypatch, router_module, *, bigquery=False, snowflake=False, databricks=False):
    monkeypatch.setattr(router_module, "_bigquery_credentialed", lambda: bigquery)
    monkeypatch.setattr(router_module, "_snowflake_credentialed", lambda: snowflake)
    monkeypatch.setattr(router_module, "_databricks_credentialed", lambda: databricks)


class TestConnectedSourcesHelper:
    """Direct, dependency-free tests against `_connected_sources()`."""

    def test_registry_only_instance(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[{"source_type": "Keboola"}])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "local", raising=False)
        _patch_credential_probes(monkeypatch, router_module)

        assert router_module._connected_sources() == ["keboola"]

    def test_l1_scalar_only_instance(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "bigquery", raising=False)
        _patch_credential_probes(monkeypatch, router_module)

        assert router_module._connected_sources() == ["bigquery"]

    @pytest.mark.parametrize("sentinel", ["local", "csv", "LOCAL", " csv ", ""])
    def test_unset_sentinel_never_reported(self, monkeypatch, sentinel):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: sentinel, raising=False)
        _patch_credential_probes(monkeypatch, router_module)

        assert router_module._connected_sources() == []

    def test_credentialed_snowflake_with_no_registry_row_is_reported(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "local", raising=False)
        _patch_credential_probes(monkeypatch, router_module, snowflake=True)

        assert router_module._connected_sources() == ["snowflake"]

    def test_unreadable_registry_degrades_instead_of_500(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, raise_on_list=True)
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "bigquery", raising=False)
        _patch_credential_probes(monkeypatch, router_module)

        # Must not raise — degrades to whatever the other stores can prove.
        assert router_module._connected_sources() == ["bigquery"]

    def test_throwing_credential_probe_degrades_instead_of_500(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "local", raising=False)

        def _boom():
            raise RuntimeError("credential probe exploded (test)")

        monkeypatch.setattr(router_module, "_bigquery_credentialed", _boom)
        monkeypatch.setattr(router_module, "_snowflake_credentialed", lambda: False)
        monkeypatch.setattr(router_module, "_databricks_credentialed", lambda: False)

        assert router_module._connected_sources() == []

    def test_throwing_scalar_read_degrades_instead_of_500(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[{"source_type": "keboola"}])

        def _boom():
            raise RuntimeError("instance config unreadable (test)")

        monkeypatch.setattr("app.instance_config.get_data_source_type", _boom, raising=False)
        _patch_credential_probes(monkeypatch, router_module)

        assert router_module._connected_sources() == ["keboola"]

    def test_union_dedupes_and_sorts(self, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[{"source_type": "keboola"}])
        monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "keboola", raising=False)
        _patch_credential_probes(monkeypatch, router_module, bigquery=True)

        assert router_module._connected_sources() == ["bigquery", "keboola"]


class TestAdminTablesRouteContext:
    """Task 2/3: the `/admin/tables` route's context dict."""

    def _capture_ctx(self, monkeypatch, router_module):
        captured = {}

        def _fake_template_response(request, name, context=None, *args, **kwargs):
            from fastapi.responses import HTMLResponse

            captured["name"] = name
            captured["ctx"] = context
            return HTMLResponse("ok")

        monkeypatch.setattr(router_module.templates, "TemplateResponse", _fake_template_response)
        return captured

    def test_both_keys_coexist(self, seeded_app, monkeypatch):
        import app.web.router as router_module

        captured = self._capture_ctx(monkeypatch, router_module)
        _patch_registry(monkeypatch, rows=[{"source_type": "bigquery"}, {"source_type": "keboola"}])
        _patch_credential_probes(monkeypatch, router_module, snowflake=True)

        c = seeded_app["client"]
        resp = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200

        ctx = captured["ctx"]
        assert ctx["connected_source_types"] == ["bigquery", "keboola"]
        assert ctx["connected_sources"] == ["bigquery", "keboola", "snowflake"]

    def test_registered_tables_key_is_gone(self, seeded_app, monkeypatch):
        """Task 3: `registered_tables` hydrated no template consumer (verified
        with `grep -a` — plain `grep` trips its binary heuristic on this
        file) and the per-page-load `table_registry_repo().list_all()` scan
        that fed it is gone with it."""
        import app.web.router as router_module

        captured = self._capture_ctx(monkeypatch, router_module)
        _patch_registry(monkeypatch, rows=[])
        _patch_credential_probes(monkeypatch, router_module)

        c = seeded_app["client"]
        resp = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "registered_tables" not in captured["ctx"]

    def test_no_unconditional_registry_scan(self, seeded_app, monkeypatch):
        """The route must render even when a full `list_all()` scan would
        blow up — proving that scan is no longer made on every page load."""
        import app.web.router as router_module

        self._capture_ctx(monkeypatch, router_module)
        _patch_registry(monkeypatch, rows=[])
        _patch_credential_probes(monkeypatch, router_module)

        def _boom():
            raise AssertionError("table_registry_repo().list_all() must not be called by admin_tables")

        class _BoomingTableRegistryRepo:
            def list_all(self):
                _boom()

        monkeypatch.setattr(
            "app.web.router.table_registry_repo",
            lambda: _BoomingTableRegistryRepo(),
            raising=False,
        )

        c = seeded_app["client"]
        resp = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200


class TestKeboolaDerivedCard:
    """Task 4: an instance-level-only Keboola configuration surfaces as a
    derived card on /admin/data-sources — but never duplicates a real
    connection's card."""

    def test_derived_card_appears_when_credentialed_and_no_registry_row(self, seeded_app, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        derived_types = {d["source_type"] for d in inventory["derived"]}
        assert "keboola" in derived_types

    def test_no_card_when_not_credentialed_and_no_tables(self, seeded_app, monkeypatch):
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: False)
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        derived_types = {d["source_type"] for d in inventory["derived"]}
        assert "keboola" not in derived_types

    def test_no_duplicate_when_a_real_connection_row_exists(self, seeded_app, monkeypatch):
        import app.web.router as router_module

        _patch_registry(
            monkeypatch,
            rows=[{"id": "conn1", "source_type": "keboola", "name": "Prod", "config": {}}],
        )
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        derived_types = [d["source_type"] for d in inventory["derived"]]
        assert derived_types.count("keboola") == 0
        # The real connection's own pipeline entry must still be present —
        # it must not be silently swallowed by the derived-card logic.
        assert "conn1" in inventory["pipelines"]

    def test_derived_card_carries_stack_url_and_token_env_for_import(self, seeded_app, monkeypatch):
        """The derived card's "Import as managed connection" button needs the
        instance-level `stack_url` + `token_env` to POST straight to
        `/api/admin/source-connections` without a second round trip — see
        `_keboola_instance_config()`."""
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        monkeypatch.setattr(
            router_module,
            "_keboola_instance_config",
            lambda: ("https://connection.keboola.com", "KEBOOLA_STORAGE_TOKEN"),
        )
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        card = next(d for d in inventory["derived"] if d["source_type"] == "keboola")
        assert card["stack_url"] == "https://connection.keboola.com"
        assert card["token_env"] == "KEBOOLA_STORAGE_TOKEN"

    def test_token_env_allowlisted_flag_is_true_for_the_default_name(self, seeded_app, monkeypatch):
        """Devin Review: `create_connection` runs `_reject_disallowed_token_env`
        before anything else, so a credentialed card whose configured
        `token_env` isn't on the remote-attach allowlist would 400 on Import
        despite looking ready. `KEBOOLA_STORAGE_TOKEN` is on the default
        allowlist, so the common case is unaffected."""
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        monkeypatch.setattr(
            router_module,
            "_keboola_instance_config",
            lambda: ("https://connection.keboola.com", "KEBOOLA_STORAGE_TOKEN"),
        )
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        card = next(d for d in inventory["derived"] if d["source_type"] == "keboola")
        assert card["token_env_allowlisted"] is True

    def test_token_env_allowlisted_flag_is_false_for_a_custom_unallowlisted_name(self, seeded_app, monkeypatch):
        """A custom `token_env` that isn't on the remote-attach allowlist
        must not offer the import button — the card can still be
        `credentialed` (via the generic env var or the instance vault) while
        the configured `token_env` itself is some other, unallowlisted name."""
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        monkeypatch.setattr(
            router_module,
            "_keboola_instance_config",
            lambda: ("https://connection.keboola.com", "SOME_UNALLOWLISTED_NAME"),
        )
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        card = next(d for d in inventory["derived"] if d["source_type"] == "keboola")
        assert card["credentialed"] is True
        assert card["token_env_allowlisted"] is False

    def test_credentialed_flag_is_true_when_the_probe_says_so(self, seeded_app, monkeypatch):
        """The "Import as managed connection" button (`app/web/templates/
        admin_data_sources.html`) gates on this flag — a stack_url with no
        working token anywhere has nothing to import."""
        import app.web.router as router_module

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: True)
        monkeypatch.setattr(
            router_module,
            "_keboola_instance_config",
            lambda: ("https://connection.keboola.com", "KEBOOLA_STORAGE_TOKEN"),
        )
        _patch_credential_probes(monkeypatch, router_module)

        inventory = router_module._source_inventory()
        card = next(d for d in inventory["derived"] if d["source_type"] == "keboola")
        assert card["credentialed"] is True

    def test_credentialed_flag_is_false_when_stack_url_set_but_nothing_credentials_it(self, seeded_app, monkeypatch):
        """A stack_url alone (no token in env or vault) must not offer the
        import button — the card still renders because it owns tables, but
        importing would create a connection with nothing to actually reach
        Keboola with."""
        import uuid

        from src.repositories import table_registry_repo

        tid = f"kbcnocred-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"kbc_nocred_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-main",
            source_table="orders",
            query_mode="local",
        )
        try:
            import app.web.router as router_module

            _patch_registry(monkeypatch, rows=[])
            monkeypatch.setattr(router_module, "_keboola_credentialed", lambda: False)
            monkeypatch.setattr(
                router_module,
                "_keboola_instance_config",
                lambda: ("https://connection.keboola.com", "KEBOOLA_STORAGE_TOKEN"),
            )
            _patch_credential_probes(monkeypatch, router_module)

            inventory = router_module._source_inventory()
            card = next(d for d in inventory["derived"] if d["source_type"] == "keboola")
            assert card["credentialed"] is False
        finally:
            table_registry_repo().unregister(tid)

    def test_keboola_credentialed_probe_is_called_at_most_once(self, seeded_app, monkeypatch):
        """The card's visibility check and the button's gating flag must
        share one evaluation of `_keboola_credentialed()`, not call it twice
        per render."""
        import app.web.router as router_module

        calls = []

        def _probe():
            calls.append(1)
            return True

        _patch_registry(monkeypatch, rows=[])
        monkeypatch.setattr(router_module, "_keboola_credentialed", _probe)
        monkeypatch.setattr(
            router_module,
            "_keboola_instance_config",
            lambda: ("https://connection.keboola.com", "KEBOOLA_STORAGE_TOKEN"),
        )
        _patch_credential_probes(monkeypatch, router_module)

        router_module._source_inventory()
        assert len(calls) == 1

    def test_other_derived_cards_carry_no_keboola_import_fields(self, seeded_app, monkeypatch):
        """`stack_url`/`token_env` are Keboola-only additions to the derived
        row — the other derived connectors (bigquery, jira, local, …) are out
        of scope for the import affordance and must not grow these keys."""
        import uuid

        from src.repositories import table_registry_repo

        tid = f"bqnoimport-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"bq_noimport_{tid[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="noimport",
            query_mode="remote",
        )
        try:
            import app.web.router as router_module

            inventory = router_module._source_inventory()
            card = next(d for d in inventory["derived"] if d["source_type"] == "bigquery")
            assert "stack_url" not in card
            assert "token_env" not in card
            assert "token_env_allowlisted" not in card
        finally:
            table_registry_repo().unregister(tid)
