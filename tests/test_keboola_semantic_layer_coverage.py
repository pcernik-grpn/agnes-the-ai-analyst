"""compute_semantic_coverage() — what reaches Agnes, and which gaps deserve a warning.

The distinction under test is the whole point of the function: a metric whose
table nobody registered is the ordinary steady state and must stay quiet, while
a project where NOTHING binds, or a metric that cannot be composed from its own
definition, is a real finding.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _register_keboola_table(bucket: str, source_table: str, name: str):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name,
            name=name,
            source_type="keboola",
            bucket=bucket,
            source_table=source_table,
            query_mode="local",
        )
    finally:
        conn.close()


def _model_item(uuid="model-1", name="core"):
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _metric_item(name, sql, dataset, model_uuid="model-1"):
    return {
        "type": "semantic-metric",
        "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


def _dataset_item(table_id, model_uuid="model-1"):
    return {
        "type": "semantic-dataset",
        "id": f"ds-{table_id}",
        "attributes": {"name": table_id.rpartition(".")[2], "tableId": table_id, "modelUUID": model_uuid},
    }


def _run(metrics, datasets=(), glossary=(), *, storage_project=("12345", "Demo"), master_project=("12345", "Demo")):
    """Run the coverage computation against a fake Metastore and fake token
    identities. Returns the single source entry."""
    from connectors.keboola import semantic_layer

    fake_metastore = MagicMock()
    fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
        "semantic-model": [_model_item()],
        "semantic-dataset": list(datasets),
        "semantic-metric": list(metrics),
        "semantic-relationship": [],
        "semantic-glossary": list(glossary),
    }[item_type]

    def fake_identity(url, token):
        pair = master_project if token == "master-tok" else storage_project
        return None if pair is None else {"id": pair[0], "name": pair[1]}

    with (
        patch.object(
            semantic_layer,
            "_enumerate_master_sources",
            return_value=[
                {
                    "connection_id": "conn-1",
                    "name": "Demo project",
                    "stack_url": "https://connection.keboola.com",
                    "token": "master-tok",
                }
            ],
        ),
        patch.object(semantic_layer, "_connection_storage_token", return_value="storage-tok"),
        patch.object(semantic_layer, "_project_identity", side_effect=fake_identity),
        patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
    ):
        result = semantic_layer.compute_semantic_coverage()

    assert len(result["sources"]) == 1
    return result["sources"][0]


def _warning_codes(source) -> set[str]:
    return {w["code"] for w in source["warnings"]}


class TestCoverageCounts:
    def test_metric_on_a_registered_table_is_importable(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        assert source["metrics"] == {"upstream": 1, "importable": 1}
        assert source["unregistered_tables"] == []
        assert _warning_codes(source) == set()

    def test_glossary_is_counted_independently_of_any_table(self, e2e_env):
        source = _run([], glossary=[{"id": "g1", "attributes": {"term": "MRR", "definition": "…"}}])

        assert source["glossary"]["upstream"] == 1


class TestUnregisteredTablesAreNotAProblem:
    """The steady state: a semantic layer describes more of a project than the
    instance registers. Those metrics are MEANT to stay out."""

    def test_partial_coverage_raises_no_warning(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [
                _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("headcount", "COUNT(*)", "in.c-hr.employees"),
            ],
            datasets=[_dataset_item("in.c-example_source.orders"), _dataset_item("in.c-hr.employees")],
        )

        assert source["metrics"] == {"upstream": 2, "importable": 1}
        # Reported as fact...
        assert source["unregistered_tables"] == ["in.c-hr.employees"]
        # ...and NOT as something anybody has to act on.
        assert _warning_codes(source) == set()

    def test_unregistered_metrics_never_land_in_blocked(self, e2e_env):
        source = _run(
            [_metric_item("headcount", "COUNT(*)", "in.c-hr.employees")],
            datasets=[_dataset_item("in.c-hr.employees")],
        )

        assert source["blocked"] == []
        assert source["unregistered_tables"] == ["in.c-hr.employees"]


class TestWarnings:
    def test_nothing_binding_at_all_is_a_warning(self, e2e_env):
        source = _run(
            [_metric_item("headcount", "COUNT(*)", "in.c-hr.employees")],
            datasets=[_dataset_item("in.c-hr.employees")],
        )

        assert source["metrics"] == {"upstream": 1, "importable": 0}
        assert "no_metrics_bound" in _warning_codes(source)

    def test_a_project_publishing_no_metrics_is_not_a_warning(self, e2e_env):
        source = _run([])

        assert source["metrics"] == {"upstream": 0, "importable": 0}
        assert "no_metrics_bound" not in _warning_codes(source)

    def test_metric_blocked_by_its_own_definition_is_reported_separately(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [
                _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders"),
                # A trailing comment swallows the composed FROM clause — no
                # amount of registering fixes this one.
                _metric_item("broken", 'SUM("amount") -- fixme', "in.c-example_source.orders"),
            ],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        assert source["metrics"] == {"upstream": 2, "importable": 1}
        assert [b["metric"] for b in source["blocked"]] == ["broken"]
        assert source["blocked"][0]["reason"] == "embedded_sql_comment"
        assert "metrics_blocked" in _warning_codes(source)
        assert source["unregistered_tables"] == []

    def test_tokens_pointing_at_different_projects_is_a_warning(self, e2e_env):
        source = _run([], storage_project=("4451", "Other"), master_project=("12345", "Demo"))

        assert source["token_project_mismatch"] is True
        assert "token_project_mismatch" in _warning_codes(source)
        message = next(w["message"] for w in source["warnings"] if w["code"] == "token_project_mismatch")
        assert "4451" in message and "12345" in message

    def test_same_project_on_both_tokens_is_not_a_warning(self, e2e_env):
        source = _run([], storage_project=("12345", "Demo"), master_project=("12345", "Demo"))

        assert source["token_project_mismatch"] is False
        assert "token_project_mismatch" not in _warning_codes(source)

    def test_unknown_storage_identity_does_not_claim_a_mismatch(self, e2e_env):
        """A token we cannot resolve is unknown, not different — claiming a
        mismatch there would send an admin chasing a config error that isn't."""
        source = _run([], storage_project=None, master_project=("12345", "Demo"))

        assert source["token_project_mismatch"] is False
        assert "token_project_mismatch" not in _warning_codes(source)


class TestUpstreamFailure:
    def test_metastore_failure_is_captured_not_raised(self, e2e_env):
        from connectors.keboola import semantic_layer
        from connectors.keboola.metastore_client import MetastoreApiError

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = MetastoreApiError("boom", status=502)

        with (
            patch.object(
                semantic_layer,
                "_enumerate_master_sources",
                return_value=[
                    {
                        "connection_id": "conn-1",
                        "name": "Demo project",
                        "stack_url": "https://connection.keboola.com",
                        "token": "master-tok",
                    }
                ],
            ),
            patch.object(semantic_layer, "_connection_storage_token", return_value=""),
            patch.object(semantic_layer, "_project_identity", return_value=None),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = semantic_layer.compute_semantic_coverage()

        source = result["sources"][0]
        assert source["error"] is not None
        assert "fetch_failed" in _warning_codes(source)


class TestNameConflicts:
    """Since the flat-table cutover the projector enforces no name-ownership
    gate: a name another source already holds no longer blocks a Keboola
    metric from landing — both definitions coexist under distinct scoped ids.
    The coverage report surfaces the collision as purely informational
    (`conflicts`), without subtracting it from `importable`.
    """

    def _seed_foreign_metric(self, name: str):
        from src.repositories import metric_repo

        metric_repo().create(
            id=f"sales_revenue/{name}",
            name=name,
            display_name=name,
            category="sales",
            description="",
            sql="SELECT 1",
            source="yaml_import",
        )

    def test_a_name_another_source_holds_is_not_counted_importable(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        self._seed_foreign_metric("mrr")

        source = _run(
            [
                _metric_item("mrr", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("order_count", "COUNT(*)", "in.c-example_source.orders"),
            ],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        # Both definitions coexist now — the collision no longer costs the
        # Keboola metric its spot.
        assert source["metrics"] == {"upstream": 2, "importable": 2}
        assert [c["metric"] for c in source["conflicts"]] == ["mrr"]
        assert source["conflicts"][0]["held_by"] == "yaml_import"
        assert "name_conflict" in _warning_codes(source)
        # Not a table problem — registering something must not be the hint.
        assert source["unregistered_tables"] == []
        assert source["blocked"] == []

    def test_no_conflict_when_the_name_is_free(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [_metric_item("mrr", 'SUM("amount")', "in.c-example_source.orders")],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        assert source["metrics"] == {"upstream": 1, "importable": 1}
        assert source["conflicts"] == []
        assert "name_conflict" not in _warning_codes(source)


class TestCoverageNoticesARefusedSync:
    """Devin Review on #1248: the report compared only storage vs master.

    The connection's RECORDED project (#1242) is a third identity, and the
    one the sync enforces — a master token opening a different project than
    the connection is locked to is refused outright, so the sync never runs.
    Reporting coverage as healthy for such a connection is the single most
    misleading thing this page can say.
    """

    def test_a_master_token_for_the_wrong_project_is_warned_about(self):
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer.compute_semantic_coverage)
        assert 'config") or {}).get("project_id")' in src, "the recorded binding is never consulted"
        assert "master_token_project_mismatch" in src
        # …and it must set the same flag the page already renders on.
        i = src.index("master_token_project_mismatch")
        assert 'entry["token_project_mismatch"] = True' in src[:i]

    def test_the_message_names_both_ways_out(self):
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer.compute_semantic_coverage)
        i = src.index("master_token_project_mismatch")
        msg = src[i : i + 700]
        assert "unbind" in msg
        assert "master token for the project it is bound to" in msg


class TestCoverageAppliesTheSyncsMasterTokenPreflight:
    """Devin Review on #1248: the sync aborts, the report did not say so.

    `_sync_one_source` runs `check_master_token` and aborts with
    `MasterTokenRequiredError` when the stored token has been downgraded;
    the Metastore rejects it with an opaque "Failed to create project scope".
    A coverage report that skips that check describes a sync that never runs.
    """

    def test_identity_lookup_reports_master_ness(self):
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer._project_identity)
        assert '"is_master"' in src, "the payload's isMasterToken is thrown away"
        assert "verify_token()" in src, "…and it must stay one round-trip"

    def test_a_downgraded_token_is_warned_about(self):
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer.compute_semantic_coverage)
        assert "master_token_downgraded" in src
        i = src.index("master_token_downgraded")
        assert "owner token again" in src[i : i + 600], "the warning must name the remedy"

    def test_a_master_token_raises_no_such_warning(self):
        """`is_master` absent (an older payload) must not read as downgraded."""
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer.compute_semantic_coverage)
        assert '.get("is_master") is False' in src, "a missing key would fire the warning"


class TestTheWarningStripDoesNotSweepEveryMetastore:
    """Devin Review on #1248: drawing one line of text pulled every project's
    whole semantic model, on every view of the Data sources page."""

    def test_warnings_only_skips_the_metastore_enumeration(self):
        import inspect

        from connectors.keboola import semantic_layer

        src = inspect.getsource(semantic_layer.compute_semantic_coverage)
        i = src.index("if warnings_only:")
        j = src.index("MetastoreClient(")
        assert i < j, "the early exit must come before the Metastore is contacted"
        assert "continue" in src[i : i + 120]

    def test_the_endpoint_takes_the_flag(self):
        import inspect

        from app.api import keboola_semantic_layer_refresh as mod

        src = inspect.getsource(mod.get_semantic_layer_coverage)
        assert "warnings_only: bool = False" in src
        assert "warnings_only=warnings_only" in src

    def test_the_data_sources_page_asks_for_the_cheap_form(self):
        from pathlib import Path

        from tests._admin_data_sources_source import read_admin_data_sources_source

        page = read_admin_data_sources_source()
        assert "semantic-layer/coverage?warnings_only=true" in page
        # …and the Semantic layer page still wants the full report.
        full = (Path(__file__).resolve().parents[1] / "app/web/templates/admin_semantic_layer.html").read_text()
        assert "warnings_only" not in full


def _run_multi(models, items_by_model, *, project=("12345", "Demo")):
    """Run the coverage computation against a Metastore exposing SEVERAL
    models. ``items_by_model`` maps model uuid -> {item_type: [items]}.

    Deliberately separate from ``_run``: that helper hard-codes one model and
    ignores the ``model_uuid`` argument, which is exactly the blindness under
    test here — a fake that answers the same list for every model cannot tell
    a per-model loop from a ``models[0]`` read.
    """
    from connectors.keboola import semantic_layer

    fake_metastore = MagicMock()

    def list_items(item_type, model_uuid=None):
        if item_type == "semantic-model":
            return list(models)
        return list((items_by_model.get(model_uuid) or {}).get(item_type) or [])

    fake_metastore.list_items.side_effect = list_items

    with (
        patch.object(
            semantic_layer,
            "_enumerate_master_sources",
            return_value=[
                {
                    "connection_id": "conn-1",
                    "name": "Demo project",
                    "stack_url": "https://connection.keboola.com",
                    "token": "master-tok",
                }
            ],
        ),
        patch.object(semantic_layer, "_connection_storage_token", return_value="storage-tok"),
        patch.object(
            semantic_layer,
            "_project_identity",
            side_effect=lambda url, token: {"id": project[0], "name": project[1]},
        ),
        patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
    ):
        result = semantic_layer.compute_semantic_coverage()

    assert len(result["sources"]) == 1
    return result["sources"][0]


class TestEveryModelIsReported:
    """A project exposes more than one model the moment a model shared from
    another project is linked into it, and `sync_semantic_layer` imports every
    one of them. A report built from `models[0]` describes one model and stays
    silent about the rest — contradicting the importer standing next to it,
    which is worse than reporting nothing.
    """

    def test_metrics_of_every_model_are_counted(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _register_keboola_table("in.c-shared", "invoices", "fin_invoices")

        source = _run_multi(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item("in.c-example_source.orders", "model-1")],
                    "semantic-metric": [
                        _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item("in.c-shared.invoices", "model-2")],
                    "semantic-metric": [
                        _metric_item("invoice_count", 'COUNT("id")', "in.c-shared.invoices", "model-2")
                    ],
                },
            },
        )

        assert source["metrics"] == {"upstream": 2, "importable": 2}
        assert _warning_codes(source) == set()

    def test_every_model_is_named_in_the_report(self, e2e_env):
        source = _run_multi(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {"model-1": {}, "model-2": {}},
        )

        assert [(m["uuid"], m["name"]) for m in source["models"]] == [
            ("model-1", "core"),
            ("model-2", "shared"),
        ]

    def test_glossary_is_summed_across_models(self, e2e_env):
        source = _run_multi(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {"semantic-glossary": [{"id": "g1", "attributes": {"term": "MRR", "definition": "…"}}]},
                "model-2": {"semantic-glossary": [{"id": "g2", "attributes": {"term": "ARR", "definition": "…"}}]},
            },
        )

        assert source["glossary"]["upstream"] == 2

    def test_a_second_models_unregistered_table_is_named(self, e2e_env):
        """The unregistered-table list is the admin's to-do list. A table only
        the second model references must appear on it."""
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        source = _run_multi(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item("in.c-example_source.orders", "model-1")],
                    "semantic-metric": [
                        _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item("in.c-nowhere.ghosts", "model-2")],
                    "semantic-metric": [_metric_item("ghost_count", 'COUNT("id")', "in.c-nowhere.ghosts", "model-2")],
                },
            },
        )

        assert source["metrics"] == {"upstream": 2, "importable": 1}
        assert source["unregistered_tables"] == ["in.c-nowhere.ghosts"]

    def test_a_name_shared_by_two_models_is_importable_from_both(self, e2e_env):
        """Since the flat-table cutover there is no first-model-wins tie-break:
        two linked models each publishing `revenue` write two distinct
        scoped-id rows, and both coexist. Neither model's own metric conflicts
        with the other — `conflicts` only fires against a DIFFERENT source's
        pre-existing row (see TestNameConflicts).
        """
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _register_keboola_table("in.c-shared", "invoices", "fin_invoices")

        source = _run_multi(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item("in.c-example_source.orders", "model-1")],
                    "semantic-metric": [
                        _metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item("in.c-shared.invoices", "model-2")],
                    "semantic-metric": [_metric_item("revenue", 'SUM("total")', "in.c-shared.invoices", "model-2")],
                },
            },
        )

        assert source["metrics"] == {"upstream": 2, "importable": 2}
        assert source["conflicts"] == []
        assert "name_conflict" not in _warning_codes(source)
