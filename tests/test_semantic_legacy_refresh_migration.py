"""Block 3 steps 3-4 of issue #1707 — the legacy Keboola/Databricks scheduled
refreshes move onto the generic ``semantic_sources`` sweep.

The heart of this file is the GOLDEN REGRESSION: over one fixture upstream,
the legacy path (``connectors.keboola.semantic_layer.sync_semantic_layer``)
and the generic path (auto-migrated ``semantic_sources`` row ->
``src.semantic.transports.import_source``) must produce byte-identical
``semantic_models`` / ``metric_definitions`` / ``glossary_terms`` /
``column_metadata`` rows — same provenance, same content, same ids. Without
that green, the legacy trigger cannot be removed: two paths writing the same
upstream under two provenance labels is silent duplication, not a conflict.

The two runs use two separate ``DATA_DIR``s rather than one wiped database,
so neither run can be flattered by rows the other left behind.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.test_keboola_semantic_layer_sync import (
    _fake_clients,
    _glossary_item,
    _make_master_connection,
    _metric_item,
    _register_keboola_table,
    _run_sync,
)

_UPLOAD_DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
)

CONNECTION_ID = "conn-golden"
LEGACY_ID = "keboola_legacy_credentials"
STACK_URL = "https://connection.example.com"
MASTER_TOKEN = "master-tok"
TABLE_NAME = "crm_orders"

PROJECTS = {
    MASTER_TOKEN: {
        "owner_id": 4242,
        "model_uuid": "model-1",
        "metrics": [
            _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders"),
            _metric_item("order_count", "COUNT(*)", "in.c-example_source.orders"),
        ],
        "glossary": [_glossary_item("Revenue", "Money billed to customers.")],
    }
}


@pytest.fixture
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch):
    """These tests resolve credentials from connections, never the legacy env
    pair — clear it so an ambient value can't shadow the source loop."""
    monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)


def _seed_instance() -> None:
    """The upstream-independent state both paths read: one registered Keboola
    table and one connection holding a master token."""
    _register_keboola_table("in.c-example_source", "orders", TABLE_NAME)
    _make_master_connection(CONNECTION_ID, stack_url=STACK_URL, token=MASTER_TOKEN, is_default=True)


def _switch_data_dir(monkeypatch, tmp_path_factory) -> None:
    """Point DATA_DIR at a second, empty instance directory. Repositories read
    DATA_DIR at call time, so this is a full state reset."""
    second = tmp_path_factory.mktemp("golden-generic")
    for sub in ("extracts", "analytics", "state"):
        (second / sub).mkdir()
    monkeypatch.setenv("DATA_DIR", str(second))


_MODEL_KEYS = (
    "slug",
    "name",
    "description",
    "document",
    "spec_version",
    "content_hash",
    "source",
    "source_ref",
    "status",
)
_METRIC_KEYS = (
    "name",
    "display_name",
    "category",
    "description",
    "type",
    "unit",
    "grain",
    "table_name",
    "tables",
    "expression",
    "time_column",
    "dimensions",
    "filters",
    "synonyms",
    "notes",
    "sql",
    "sql_variants",
    "validation",
    "source",
    "source_ref",
)
_TERM_KEYS = ("term", "definition", "see_also", "model_uuid", "source", "source_ref")
_COLUMN_KEYS = ("column_name", "basetype", "description", "confidence", "source")


def _snapshot() -> dict:
    """Every row the two paths are supposed to agree on, timestamps excluded
    (they are wall-clock, never content)."""
    from src.repositories import column_metadata_repo, glossary_repo, metric_repo, semantic_model_repo

    return {
        "models": {m["id"]: {k: m.get(k) for k in _MODEL_KEYS} for m in semantic_model_repo().list_all()},
        "metrics": {m["id"]: {k: m.get(k) for k in _METRIC_KEYS} for m in metric_repo().list()},
        "terms": {t["id"]: {k: t.get(k) for k in _TERM_KEYS} for t in glossary_repo().list(limit=1000)},
        "columns": {
            c["column_name"]: {k: c.get(k) for k in _COLUMN_KEYS}
            for c in column_metadata_repo().list_for_table(TABLE_NAME)
        },
    }


def _import_migrated_source(projects: dict, source_id: str):
    from src.semantic.transports import import_source

    storage_factory, metastore_factory = _fake_clients(projects)
    with (
        patch("connectors.keboola.storage_api.KeboolaStorageClient", side_effect=storage_factory),
        patch("connectors.keboola.metastore_client.MetastoreClient", side_effect=metastore_factory),
    ):
        return import_source(source_id)


# ---------------------------------------------------------------------------
# The golden regression
# ---------------------------------------------------------------------------


def test_generic_path_reproduces_the_legacy_keboola_rows_exactly(e2e_env, vault_key, monkeypatch, tmp_path_factory):
    from src.semantic.legacy_migration import ensure_legacy_semantic_sources

    # --- run 1: the legacy path, in the e2e_env DATA_DIR -------------------
    _seed_instance()
    legacy_result = _run_sync(PROJECTS)
    assert legacy_result["status"] == "ok", legacy_result
    assert legacy_result["created_or_updated"] == 2
    assert legacy_result["glossary_created_or_updated"] == 1
    legacy_snapshot = _snapshot()
    assert legacy_snapshot["metrics"], "fixture wrote no metrics — the comparison would be vacuous"
    assert legacy_snapshot["models"], "fixture wrote no models — the comparison would be vacuous"
    assert legacy_snapshot["terms"], "fixture wrote no glossary terms — the comparison would be vacuous"

    # --- run 2: the generic path, in a second, empty DATA_DIR --------------
    _switch_data_dir(monkeypatch, tmp_path_factory)
    _seed_instance()
    migrated = ensure_legacy_semantic_sources()
    assert [m["config"]["connection_id"] for m in migrated] == [CONNECTION_ID]
    report = _import_migrated_source(PROJECTS, migrated[0]["id"])
    assert report.projection is not None
    assert report.projection.metrics_written == 2

    assert _snapshot() == legacy_snapshot


def test_generic_path_prunes_a_metric_removed_upstream_exactly_as_the_legacy_one(
    e2e_env, vault_key, monkeypatch, tmp_path_factory
):
    """The prune scope, not just the first write: a metric dropped upstream
    disappears on the second pass on both paths, and nothing else moves."""
    from src.semantic.legacy_migration import ensure_legacy_semantic_sources

    shrunk = {
        MASTER_TOKEN: {
            **PROJECTS[MASTER_TOKEN],
            "metrics": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
        }
    }

    _seed_instance()
    _run_sync(PROJECTS)
    _run_sync(shrunk)
    legacy_snapshot = _snapshot()

    _switch_data_dir(monkeypatch, tmp_path_factory)
    _seed_instance()
    source_id = ensure_legacy_semantic_sources()[0]["id"]
    _import_migrated_source(PROJECTS, source_id)
    _import_migrated_source(shrunk, source_id)

    assert _snapshot() == legacy_snapshot
    assert not any(m["name"] == "order_count" for m in _snapshot()["metrics"].values())


# ---------------------------------------------------------------------------
# Auto-migration
# ---------------------------------------------------------------------------


class TestKeboolaAutoMigration:
    def test_registers_one_enabled_source_per_master_connection(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.example.com", token="tok-b")

        ensure_legacy_semantic_sources()

        rows = {r["config"].get("connection_id"): r for r in semantic_source_repo().list_all()}
        assert set(rows) == {"conn-a", "conn-b"}
        for connection_id, row in rows.items():
            assert row["kind"] == "connection"
            assert row["adapter"] == "keboola_metastore"
            assert row["enabled"] is True
            # The provenance override is the whole point: rows this source
            # writes stay owned by the label the legacy path stamped.
            assert row["config"]["provenance"] == {
                "source": "keboola_metastore",
                "source_ref": connection_id,
            }
            assert row["config"]["safe_prune"] is True
            # Never a credential — only scope.
            assert "token" not in row["config"]

    def test_is_idempotent_and_never_duplicates(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)

        first = ensure_legacy_semantic_sources()
        assert len(first) == 1
        second = ensure_legacy_semantic_sources()
        assert second == []
        assert len(semantic_source_repo().list_all()) == 1

    def test_leaves_an_admin_created_row_for_the_same_connection_alone(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        repo = semantic_source_repo()
        repo.create(
            id="ss_admin",
            kind="connection",
            name="Admin's own Keboola source",
            adapter="keboola_metastore",
            config={"connection_id": "conn-a"},
            enabled=False,
        )

        assert ensure_legacy_semantic_sources() == []
        rows = repo.list_all()
        assert [r["id"] for r in rows] == ["ss_admin"]
        assert rows[0]["enabled"] is False
        assert rows[0]["config"] == {"connection_id": "conn-a"}

    def test_admin_disabling_a_migrated_row_survives_the_next_sweep(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        source_id = ensure_legacy_semantic_sources()[0]["id"]
        repo = semantic_source_repo()
        repo.update(source_id, enabled=False)

        ensure_legacy_semantic_sources()
        assert repo.get(source_id)["enabled"] is False

    def test_registers_nothing_when_no_connection_has_a_master_token(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo, source_connections_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        source_connections_repo().create(
            id="conn-no-master",
            name="No master token",
            source_type="keboola",
            config={"stack_url": "https://a.example.com"},
            is_default=True,
            created_by="test",
        )

        assert ensure_legacy_semantic_sources() == []
        assert semantic_source_repo().list_all() == []


class TestLegacyCredentialsMigration:
    """The pre-connection fallback: KEBOOLA_STACK_URL + KEBOOLA_STORAGE_TOKEN
    with no per-connection master token. ``sync_semantic_layer`` still syncs
    that instance, so the migration must carry it too — or removing the
    scheduler entry would silently stop it."""

    def test_registers_a_legacy_credentials_source(self, e2e_env, vault_key, monkeypatch):
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        monkeypatch.setenv("KEBOOLA_STACK_URL", STACK_URL)
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", MASTER_TOKEN)

        created = ensure_legacy_semantic_sources()
        assert len(created) == 1
        row = semantic_source_repo().get(created[0]["id"])
        assert row["adapter"] == "keboola_metastore"
        assert row["config"]["legacy_credentials"] is True
        # NULL source_ref — exactly what the legacy env pair stamps.
        assert row["config"]["provenance"] == {"source": "keboola_metastore", "source_ref": None}

    def test_a_master_token_connection_supersedes_the_legacy_row(self, e2e_env, vault_key, monkeypatch):
        """``sync_semantic_layer`` ignores the legacy env pair the moment any
        connection holds a master token. Left enabled, the migrated legacy row
        would import the same project a second time under NULL provenance —
        the duplicate this whole sequencing exists to avoid."""
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        monkeypatch.setenv("KEBOOLA_STACK_URL", STACK_URL)
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", MASTER_TOKEN)
        legacy_id = ensure_legacy_semantic_sources()[0]["id"]

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        ensure_legacy_semantic_sources()

        repo = semantic_source_repo()
        assert repo.get(legacy_id)["enabled"] is False
        assert any((r["config"] or {}).get("connection_id") == "conn-a" and r["enabled"] for r in repo.list_all())


class TestAWizardConnectionLaterGainsAMasterToken:
    """The upgrade path that a naive provenance claim strands.

    A wizard-connected project (``/admin/data-sources`` -> a
    ``source_connections`` row holding a STORAGE token, no master token) is
    synced through the legacy-credentials row, which the first sweep stamps
    with THAT connection's id as its ``source_ref`` — the credentials came
    from the connection, so the rows are the connection's.

    When an admin later adds a master token to the same connection, the sweep
    has to hand the scope over: the connection-backed row takes it, the
    legacy-credentials row steps down. Getting the order wrong leaves the
    connection with no enabled source at all and every later sweep repeating
    the skip, because the disabled row still claims the provenance.
    """

    def _wizard_connection(self, conn_id: str, *, token: str) -> str:
        """A connection as the admin wizard leaves it: a storage token in the
        connection's own vault slot, nothing in the master slot."""
        from src.repositories import connection_secrets_repo, source_connections_repo

        source_connections_repo().create(
            id=conn_id,
            name=f"name-{conn_id}",
            source_type="keboola",
            config={"stack_url": STACK_URL},
            is_default=True,
            created_by="test",
        )
        connection_secrets_repo().upsert(conn_id, token)
        return conn_id

    def _enabled_keboola_sources(self) -> list[dict]:
        from src.repositories import semantic_source_repo

        return [
            r
            for r in semantic_source_repo().list_all()
            if r["adapter"] == "keboola_metastore" and r["enabled"] is not False
        ]

    def test_the_scope_moves_to_the_connection_row_and_keeps_syncing(self, e2e_env, vault_key):
        from app.api.admin_source_connections import master_secret_key
        from src.repositories import connection_secrets_repo, metric_repo, semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        conn_id = "conn-wizard"
        _register_keboola_table("in.c-example_source", "orders", TABLE_NAME)
        self._wizard_connection(conn_id, token=MASTER_TOKEN)

        # --- sweep 1: no master token anywhere, so the legacy-credentials row
        # carries this connection's scope and syncs it.
        created = ensure_legacy_semantic_sources()
        assert [r["id"] for r in created] == [LEGACY_ID]
        assert created[0]["config"]["provenance"] == {"source": "keboola_metastore", "source_ref": conn_id}
        _import_migrated_source(PROJECTS, LEGACY_ID)
        assert {m["source_ref"] for m in metric_repo().list()} == {conn_id}
        assert len(metric_repo().list()) == 2

        # --- the admin adds a master token to that same connection.
        connection_secrets_repo().upsert(master_secret_key(conn_id), MASTER_TOKEN)

        # --- sweep 2: the connection is now master-capable.
        ensure_legacy_semantic_sources()

        repo = semantic_source_repo()
        enabled = self._enabled_keboola_sources()
        # Exactly ONE enabled source, connection-backed, and carrying the SAME
        # provenance the legacy row had — the rows already written stay owned.
        assert len(enabled) == 1, [r["id"] for r in enabled]
        assert enabled[0]["config"]["connection_id"] == conn_id
        assert enabled[0]["config"]["provenance"] == {"source": "keboola_metastore", "source_ref": conn_id}
        assert enabled[0]["config"]["safe_prune"] is True
        # And the legacy row stepped down rather than lingering as a second
        # writer of the same scope.
        assert repo.get(LEGACY_ID)["enabled"] is False

        # --- and the next import still lands under that same scope: no
        # orphaned rows, no duplicate set beside them.
        _import_migrated_source(PROJECTS, enabled[0]["id"])
        assert {m["source_ref"] for m in metric_repo().list()} == {conn_id}
        assert len(metric_repo().list()) == 2

        # --- steady state: nothing new on any later sweep.
        assert ensure_legacy_semantic_sources() == []
        assert len(self._enabled_keboola_sources()) == 1

    def test_the_legacy_row_stays_enabled_when_the_handover_could_not_happen(self, e2e_env, vault_key):
        """The handover is create-then-supersede, and the supersede is
        conditional on the create having landed. A connection row that could
        not be created (its derived id already taken by an admin's row for a
        different scope) must not cost the scope its only writer."""
        from app.api.admin_source_connections import master_secret_key
        from connectors.keboola.semantic_layer import semantic_source_id
        from src.repositories import connection_secrets_repo, semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        conn_id = "conn-wizard"
        self._wizard_connection(conn_id, token=MASTER_TOKEN)
        ensure_legacy_semantic_sources()

        repo = semantic_source_repo()
        # An admin's own, unrelated row squatting on the id the migration
        # would derive — and disabled, so it cannot take over the scope.
        repo.create(
            id=semantic_source_id(conn_id),
            kind="upload",
            name="Admin's own row on that id",
            adapter="native",
            config={"documents": []},
            enabled=False,
        )
        connection_secrets_repo().upsert(master_secret_key(conn_id), MASTER_TOKEN)

        ensure_legacy_semantic_sources()

        assert repo.get(LEGACY_ID)["enabled"] is True
        assert len(self._enabled_keboola_sources()) == 1


class TestDatabricksAutoMigration:
    def test_registers_the_workspace_only_when_it_is_configured(self, e2e_env):
        from connectors.databricks.semantic_layer import DATABRICKS_SEMANTIC_SOURCE_ID
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        # Unconfigured: registering a row here would give every instance a
        # source that fails on every sweep, forever.
        assert ensure_legacy_semantic_sources() == []
        assert semantic_source_repo().get(DATABRICKS_SEMANTIC_SOURCE_ID) is None

        settings = {
            "host": "example.cloud.databricks.com",
            "warehouse_id": "w1",
            "catalog": "main",
            "catalogs": ["main"],
            "token": "t",
        }
        with patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=settings):
            created = ensure_legacy_semantic_sources()
            assert [c["id"] for c in created] == [DATABRICKS_SEMANTIC_SOURCE_ID]
            # Idempotent.
            assert ensure_legacy_semantic_sources() == []

        row = semantic_source_repo().get(DATABRICKS_SEMANTIC_SOURCE_ID)
        assert row["adapter"] == "databricks_metric_views"
        # Databricks already writes under the generic provenance since the
        # Track D6 cutover — no override, or its rows would move scope.
        assert "provenance" not in (row["config"] or {})
        # The full-wipe guard IS shared with the Keboola side: the adapter
        # skips a metric view it cannot read, so a transient warehouse fault
        # is a successful sync returning nothing. See
        # `tests/test_databricks_semantic_source_e2e.py` for the end-to-end.
        assert row["config"]["safe_prune"] is True


# ---------------------------------------------------------------------------
# Provenance override + safe_prune (the transport/importer knobs)
# ---------------------------------------------------------------------------


class TestProvenanceOverride:
    def test_override_is_refused_for_a_label_outside_the_legacy_allowlist(self, e2e_env):
        from src.repositories import semantic_source_repo
        from src.semantic.transports import import_source

        semantic_source_repo().create(
            id="ss_hijack",
            kind="upload",
            name="Hijack attempt",
            adapter="native",
            config={"documents": [], "provenance": {"source": "manual", "source_ref": None}},
        )

        with pytest.raises(ValueError, match="provenance"):
            import_source("ss_hijack")

        # And the failure is recorded on the source row, not swallowed.
        assert semantic_source_repo().get("ss_hijack")["last_sync_status"] == "error"

    def test_a_source_without_an_override_keeps_the_generic_provenance(self, e2e_env):
        from src.repositories import semantic_model_repo, semantic_source_repo
        from src.semantic.transports import import_source

        semantic_source_repo().create(
            id="ss_plain", kind="upload", name="Plain", adapter="native", config={"documents": [_UPLOAD_DOC]}
        )
        import_source("ss_plain")

        model = semantic_model_repo().get_by_slug("retail")
        assert model["source"] == "ossie_upload"
        assert model["source_ref"] == "ss_plain"


class TestSafePrune:
    def test_an_empty_upstream_does_not_wipe_a_safe_prune_source(self, e2e_env, vault_key):
        """Keboola's ``safe_prune=True`` valve, preserved through the generic
        path: an upstream that answers with nothing usable must not delete the
        instance's whole metric registry for that scope."""
        from src.repositories import metric_repo, semantic_model_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _seed_instance()
        source_id = ensure_legacy_semantic_sources()[0]["id"]
        _import_migrated_source(PROJECTS, source_id)
        assert len(metric_repo().list()) == 2

        empty = {MASTER_TOKEN: {**PROJECTS[MASTER_TOKEN], "metrics": [], "glossary": []}}
        _import_migrated_source(empty, source_id)

        assert len(metric_repo().list()) == 2
        assert len(semantic_model_repo().list_all()) == 1

    def test_safe_prune_also_guards_the_stored_document_prune(self, e2e_env):
        """A transport that returns NO documents at all is the same
        can\'t-tell-empty-from-broken signal one layer up: with the valve on,
        the stored documents survive instead of being pruned to nothing while
        their projection (never re-run, because there is nothing to project)
        stays behind, orphaned."""
        from src.repositories import semantic_model_repo, semantic_source_repo
        from src.semantic.transports import import_source

        repo = semantic_source_repo()
        repo.create(
            id="ss_guarded",
            kind="upload",
            name="Guarded",
            adapter="native",
            config={"documents": [_UPLOAD_DOC], "safe_prune": True},
        )
        import_source("ss_guarded")
        assert semantic_model_repo().get_by_slug("retail") is not None

        repo.update("ss_guarded", config={"documents": [], "safe_prune": True})
        import_source("ss_guarded")
        assert semantic_model_repo().get_by_slug("retail") is not None

    def test_a_plain_source_still_prunes_an_emptied_upstream(self, e2e_env):
        """The valve is opt-in: a git/upload source emptying a model is a real
        delete signal and must stay one."""
        from src.repositories import semantic_model_repo, semantic_source_repo
        from src.semantic.transports import import_source

        repo = semantic_source_repo()
        repo.create(id="ss_plain", kind="upload", name="Plain", adapter="native", config={"documents": [_UPLOAD_DOC]})
        import_source("ss_plain")
        assert semantic_model_repo().get_by_slug("retail") is not None

        repo.update("ss_plain", config={"documents": []})
        import_source("ss_plain")
        assert semantic_model_repo().get_by_slug("retail") is None


# ---------------------------------------------------------------------------
# Post-import reconciliation — the legacy-row cleanup each retired endpoint
# used to run inline
# ---------------------------------------------------------------------------


class TestReconcileAfterImport:
    def test_a_migrated_keboola_import_purges_its_pre_cutover_rows(self, e2e_env, vault_key):
        """Rows the retired flat writer left under `keboola_semantic_layer`
        are superseded by the projection, and the current pipeline's own prune
        can never reach them — a different scope. The legacy sync purged them
        on every pass; the migrated one must too, or they linger as permanent
        duplicates of their freshly-projected twins."""
        from src.repositories import glossary_repo, metric_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import

        _seed_instance()
        metric_repo().create(
            id="keboola_semantic_layer/legacy_revenue",
            name="legacy_revenue",
            display_name="legacy_revenue",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref=CONNECTION_ID,
        )
        glossary_repo().create(
            id="keboola_semantic_layer/legacy_term",
            term="Legacy term",
            definition="Written before the cutover.",
            source="keboola_semantic_layer",
            source_ref=CONNECTION_ID,
        )

        source = ensure_legacy_semantic_sources()[0]
        report = _import_migrated_source(PROJECTS, source["id"])
        purged = reconcile_after_import(source, report)

        assert purged == {"metrics": 1, "glossary": 1}
        assert metric_repo().get("keboola_semantic_layer/legacy_revenue") is None
        # Idempotent: the next sweep finds none.
        assert reconcile_after_import(source, report) == {}

    def test_another_connections_legacy_rows_are_out_of_scope(self, e2e_env, vault_key):
        from src.repositories import metric_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import

        _seed_instance()
        metric_repo().create(
            id="keboola_semantic_layer/other_project",
            name="other",
            display_name="other",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-somebody-else",
        )

        source = ensure_legacy_semantic_sources()[0]
        report = _import_migrated_source(PROJECTS, source["id"])

        assert reconcile_after_import(source, report) == {}
        assert metric_repo().get("keboola_semantic_layer/other_project") is not None

    def test_a_databricks_import_purges_the_retired_direct_writers_rows(self, e2e_env):
        from connectors.databricks.semantic_layer import LEGACY_METRIC_SOURCE
        from src.repositories import metric_repo
        from src.semantic.importer import ImportReport
        from src.semantic.legacy_migration import reconcile_after_import

        metric_repo().create(
            id="databricks/main.sales.orders/revenue",
            name="revenue",
            display_name="revenue",
            category="databricks",
            sql="SELECT MEASURE(`revenue`) FROM `main`.`sales`.`orders`",
            source=LEGACY_METRIC_SOURCE,
            source_ref="dbc-old.cloud.databricks.com",
        )

        source = {"id": "databricks_default", "adapter": "databricks_metric_views", "config": {}}
        assert reconcile_after_import(source, ImportReport()) == {"metrics": 1}
        assert metric_repo().get("databricks/main.sales.orders/revenue") is None

    def test_a_plain_source_has_nothing_to_reconcile(self, e2e_env):
        from src.semantic.importer import ImportReport
        from src.semantic.legacy_migration import reconcile_after_import

        source = {"id": "ss_git", "adapter": "native", "config": {}}
        assert reconcile_after_import(source, ImportReport()) == {}


class TestProvenanceOverrideCannotBeForged:
    """The allowlist checks the LABEL; the ref is what actually selects whose
    rows get pruned. A source that may claim ``keboola_metastore`` must also
    prove the ref it claims is its OWN — otherwise a hand-registered
    ``kind=upload`` source could name connection A's ref and have its (empty)
    import prune A's models, metrics, glossary terms and column descriptions.
    """

    def test_an_upload_source_may_not_carry_a_legacy_provenance(self, e2e_env):
        from src.repositories import semantic_source_repo
        from src.semantic.transports import import_source

        semantic_source_repo().create(
            id="ss_forged_adapter",
            kind="upload",
            name="Forged",
            adapter="native",
            config={"documents": [], "provenance": {"source": "keboola_metastore", "source_ref": CONNECTION_ID}},
        )

        with pytest.raises(ValueError, match="adapter"):
            import_source("ss_forged_adapter")
        assert semantic_source_repo().get("ss_forged_adapter")["last_sync_status"] == "error"

    def test_a_keboola_source_may_not_claim_another_connections_ref(self, e2e_env, vault_key):
        from src.repositories import semantic_source_repo
        from src.semantic.transports import import_source

        semantic_source_repo().create(
            id="ss_forged_ref",
            kind="connection",
            name="Forged ref",
            adapter="keboola_metastore",
            config={
                "connection_id": "conn-mine",
                "provenance": {"source": "keboola_metastore", "source_ref": "conn-somebody-else"},
            },
        )

        with pytest.raises(ValueError, match="source_ref"):
            import_source("ss_forged_ref")
        assert semantic_source_repo().get("ss_forged_ref")["last_sync_status"] == "error"

    def test_a_legacy_credentials_source_may_only_claim_null_or_the_default_connection(self, e2e_env, vault_key):
        from src.semantic.transports import resolve_provenance

        _make_master_connection(CONNECTION_ID, stack_url=STACK_URL, token=MASTER_TOKEN, is_default=True)

        def _row(ref):
            return {
                "id": LEGACY_ID,
                "kind": "connection",
                "adapter": "keboola_metastore",
                "config": {
                    "legacy_credentials": True,
                    "provenance": {"source": "keboola_metastore", "source_ref": ref},
                },
            }

        # The two refs that path has ever stamped.
        assert resolve_provenance(_row(None)) == ("keboola_metastore", None)
        assert resolve_provenance(_row(CONNECTION_ID)) == ("keboola_metastore", CONNECTION_ID)
        # Anything else is another connection's scope.
        with pytest.raises(ValueError, match="source_ref"):
            resolve_provenance(_row("conn-somebody-else"))

    def test_the_migrated_rows_the_migration_itself_writes_still_resolve(self, e2e_env, vault_key):
        """The guard must not break what it protects: every row
        ``ensure_legacy_semantic_sources`` creates resolves its own scope."""
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources
        from src.semantic.transports import resolve_provenance

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.example.com", token="tok-b")

        for row in ensure_legacy_semantic_sources():
            assert resolve_provenance(row) == ("keboola_metastore", row["config"]["connection_id"])


class TestOneUpstreamProjectPerSweep:
    """Two connections may point at ONE upstream project (uniqueness is on
    connection NAME). The legacy multi-source loop threaded
    ``seen_project_keys`` across them for exactly that reason: both syncing
    would write one project's metrics under two refs and cross-wipe each
    other every run. The per-source sweep has to keep that guard."""

    def test_the_second_connection_on_one_project_is_skipped(self, e2e_env, vault_key):
        from app.api.semantic_sources_refresh import _run_sweep
        from src.repositories import metric_repo, semantic_source_repo

        _register_keboola_table("in.c-example_source", "orders", TABLE_NAME)
        _make_master_connection("conn-a", stack_url=STACK_URL, token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url=STACK_URL, token="tok-b")
        # One upstream project (same stack host, same token owner) behind both.
        projects = {
            "tok-a": {**PROJECTS[MASTER_TOKEN]},
            "tok-b": {**PROJECTS[MASTER_TOKEN]},
        }

        storage_factory, metastore_factory = _fake_clients(projects)
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", side_effect=storage_factory),
            patch("connectors.keboola.metastore_client.MetastoreClient", side_effect=metastore_factory),
        ):
            result = _run_sweep()

        statuses = {s["id"]: s["status"] for s in result["sources"]}
        assert statuses["keboola_conn-a"] == "ok"
        assert statuses["keboola_conn-b"] == "skipped_duplicate_project"
        assert result["synced"] == 1
        assert result["skipped_duplicate_project"] == 1

        # One project's metrics, under ONE ref — never a duplicate set.
        refs = {m["source_ref"] for m in metric_repo().list()}
        assert refs == {"conn-a"}
        # And the skip is visible on the row, not only in this response.
        skipped = semantic_source_repo().get("keboola_conn-b")
        assert skipped["last_sync_status"] == "skipped"
        assert "conn-a" in (skipped["last_sync_error"] or "") or "keboola_conn-a" in (
            skipped["last_sync_error"] or ""
        )

    def test_two_distinct_projects_both_import(self, e2e_env, vault_key):
        """The guard keys on the resolved upstream identity, not on "two
        Keboola sources exist" — the normal multi-project instance must be
        unaffected."""
        from app.api.semantic_sources_refresh import _run_sweep
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", TABLE_NAME)
        _make_master_connection("conn-a", stack_url=STACK_URL, token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://other.example.com", token="tok-b")
        projects = {
            "tok-a": {**PROJECTS[MASTER_TOKEN]},
            "tok-b": {**PROJECTS[MASTER_TOKEN], "owner_id": 9999},
        }

        storage_factory, metastore_factory = _fake_clients(projects)
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", side_effect=storage_factory),
            patch("connectors.keboola.metastore_client.MetastoreClient", side_effect=metastore_factory),
        ):
            result = _run_sweep()

        assert result["synced"] == 2, result
        assert result["skipped_duplicate_project"] == 0
        assert {m["source_ref"] for m in metric_repo().list()} == {"conn-a", "conn-b"}


class TestUnstampedLegacyRowAdoption:
    """Rows written before provenance existed carry ``source_ref IS NULL``.
    The legacy loop let ONLY the default connection claim them
    (``adopt_null=connection_id == default_id``); the migrated sweep must
    apply the same rule, or those rows survive every sweep as permanent
    duplicates of their freshly-projected twins."""

    def _legacy_null_metric(self) -> str:
        from src.repositories import metric_repo

        metric_repo().create(
            id="keboola_semantic_layer/pre_provenance",
            name="pre_provenance",
            display_name="pre_provenance",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref=None,
        )
        return "keboola_semantic_layer/pre_provenance"

    def test_the_default_connections_sweep_adopts_them(self, e2e_env, vault_key):
        from src.repositories import metric_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import

        _seed_instance()  # CONNECTION_ID is the default connection
        metric_id = self._legacy_null_metric()

        source = ensure_legacy_semantic_sources()[0]
        report = _import_migrated_source(PROJECTS, source["id"])

        assert reconcile_after_import(source, report) == {"metrics": 1}
        assert metric_repo().get(metric_id) is None

    def test_a_non_default_connections_sweep_leaves_them_alone(self, e2e_env, vault_key):
        from src.repositories import metric_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import

        _register_keboola_table("in.c-example_source", "orders", TABLE_NAME)
        _make_master_connection("conn-default", stack_url=STACK_URL, token="tok-default", is_default=True)
        _make_master_connection(CONNECTION_ID, stack_url=STACK_URL, token=MASTER_TOKEN)
        metric_id = self._legacy_null_metric()

        sources = {r["config"]["connection_id"]: r for r in ensure_legacy_semantic_sources()}
        source = sources[CONNECTION_ID]
        report = _import_migrated_source(PROJECTS, source["id"])

        assert reconcile_after_import(source, report) == {}
        assert metric_repo().get(metric_id) is not None


class TestReconcileGatesOnADetachedHoldBack:
    def test_a_held_back_detached_model_blocks_the_purge_like_an_invalid_one(self, e2e_env, vault_key):
        """``partial`` is what keeps a pass that did not rewrite every model
        in its scope from deleting that model's legacy rows. A detached model
        held back by the importer is exactly that case — the pass wrote
        metrics, but not the detached model's — so it must gate the purge the
        same way an invalid document does."""
        from src.repositories import metric_repo
        from src.semantic.importer import ImportReport
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import
        from src.semantic.projection import ProjectionReport

        _seed_instance()
        metric_repo().create(
            id="keboola_semantic_layer/legacy_revenue",
            name="legacy_revenue",
            display_name="legacy_revenue",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref=CONNECTION_ID,
        )
        source = ensure_legacy_semantic_sources()[0]
        report = ImportReport(
            projection=ProjectionReport(metrics_written=1, glossary_written=1),
            detached_excluded=True,
        )

        assert reconcile_after_import(source, report) == {}
        assert metric_repo().get("keboola_semantic_layer/legacy_revenue") is not None


class TestMigrationLoopIsolation:
    def test_one_connections_registration_failure_does_not_skip_the_others(self, e2e_env, vault_key):
        """The loop registers one row per connection. A row already sitting on
        the deterministic id (an admin's own, under a different scope) makes
        that one ``create`` raise — which must cost that connection, not every
        connection after it."""
        from connectors.keboola.semantic_layer import semantic_source_id
        from src.repositories import semantic_source_repo
        from src.semantic.legacy_migration import ensure_legacy_semantic_sources

        _make_master_connection("conn-a", stack_url="https://a.example.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.example.com", token="tok-b")
        repo = semantic_source_repo()
        # Same id, different adapter — so `_claims_provenance` does not treat
        # it as already owning conn-a's scope and the create is reached.
        repo.create(
            id=semantic_source_id("conn-a"),
            kind="upload",
            name="Squatter",
            adapter="native",
            config={},
        )

        created = ensure_legacy_semantic_sources()

        assert [r["config"]["connection_id"] for r in created] == ["conn-b"]
        assert repo.get(semantic_source_id("conn-a"))["adapter"] == "native"
