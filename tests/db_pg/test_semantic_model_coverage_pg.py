"""Per-domain status logic of the cross-domain coverage report.

PG-side by necessity, not by preference: the report reads
``resource_source_tags``, a Postgres-only table (A3 PG-first ratchet), so
Postgres is the only backend on which the logic can be exercised at all. The
DuckDB side's contract — the admin gate, then a typed
``501 requires_postgres_backend`` — is pinned in
``tests/test_semantic_model_coverage_endpoint.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_COVERAGE = "/api/admin/semantic-model/coverage"
_TAGS = "/api/admin/semantic-model/coverage/tags"


@pytest.fixture
def pg_state(pg_engine, tmp_path, monkeypatch):
    """Postgres app-state backend, migrated to head, repos routed to it."""
    import importlib

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    import src.repositories

    importlib.reload(src.repositories)
    return pg_engine


def _connection(conn_id: str, *, source_type: str, name: str, is_default: bool = False) -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=conn_id,
        name=name,
        source_type=source_type,
        config={},
        is_default=is_default,
        created_by="test",
    )
    return conn_id


def _table(table_id: str, *, connection_id, source_type: str = "keboola") -> None:
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id=table_id,
        name=table_id,
        source_type=source_type,
        connection_id=connection_id,
    )


def _metric(metric_id: str, *, table_name: str) -> None:
    from src.repositories import metric_repo

    metric_repo().create(
        id=metric_id,
        name=metric_id.replace("/", "_"),
        display_name=metric_id,
        category="revenue",
        sql="SELECT 1",
        table_name=table_name,
    )


def _domains(report: dict, source_id: str) -> dict:
    matching = [s for s in report["sources"] if s["source_id"] == source_id]
    assert matching, f"{source_id} missing from {[s['source_id'] for s in report['sources']]}"
    return matching[0]["domains"]


class TestMetrics:
    def test_a_table_without_a_metric_makes_the_source_partial(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")
        _table("returns", connection_id="conn-a")
        _metric("revenue/orders", table_name="orders")

        domains = _domains(compute_cross_domain_coverage(), "conn-a")
        assert domains["metrics"]["status"] == "partial"
        assert "1 of 2" in domains["metrics"]["detail"]
        assert domains["metrics"]["action"]["href"]

    def test_every_table_covered_is_ok(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")
        _metric("revenue/orders", table_name="orders")

        assert _domains(compute_cross_domain_coverage(), "conn-a")["metrics"]["status"] == "ok"

    def test_no_metric_at_all_is_missing(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")

        assert _domains(compute_cross_domain_coverage(), "conn-a")["metrics"]["status"] == "missing"

    def test_a_source_with_no_registered_table_is_not_applicable_not_missing(self, pg_state):
        """ "Missing" would invent work: there is nothing to write a metric
        against until a table is registered."""
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")

        assert _domains(compute_cross_domain_coverage(), "conn-a")["metrics"]["status"] == "not_applicable"

    def test_a_null_connection_id_falls_back_to_the_source_types_default_connection(self, pg_state):
        """Regression: every Snowflake table-registration path (CLI, web UI)
        left ``connection_id`` NULL, so such a table used to land in the
        synthetic "no connection" bucket -- a real, populated Snowflake
        connection reported ``not_applicable``/no tables even though it had
        registered tables. NULL now falls back to the source_type's default
        connection, mirroring
        ``src/connection_resolver.py::resolve_connection``."""
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Warehouse", is_default=True)
        _table("orders", connection_id=None, source_type="snowflake")
        _metric("revenue/orders", table_name="orders")

        domains = _domains(compute_cross_domain_coverage(), "conn-sf")
        assert domains["metrics"]["status"] == "ok"

    def test_a_join_metric_covers_both_of_its_tables(self, pg_state):
        """A metric binds through ``table_name`` OR the multi-table ``tables``
        array; counting only the former reported a covered table as bare."""
        from src.repositories import metric_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")
        _table("customers", connection_id="conn-a")
        metric_repo().create(
            id="revenue/joined",
            name="joined",
            display_name="Joined",
            category="revenue",
            sql="SELECT 1",
            table_name="orders",
            tables=["orders", "customers"],
        )

        assert _domains(compute_cross_domain_coverage(), "conn-a")["metrics"]["status"] == "ok"


class TestSemantic:
    def test_a_source_type_with_no_adapter_is_not_applicable(self, pg_state):
        """BigQuery has no semantic-layer adapter in this build, so "missing"
        plus an action link into a create flow that does not exist for it
        would be a lie in two directions."""
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-bq", source_type="bigquery", name="Warehouse")

        semantic = _domains(compute_cross_domain_coverage(), "conn-bq")["semantic"]
        assert semantic["status"] == "not_applicable"
        assert semantic["action"] is None

    def test_a_keboola_connection_without_an_owner_token_is_visible_and_missing(self, pg_state):
        """The wrapper's whole reason to exist.

        K0.5 enumerates only Keboola connections that ALREADY hold a master
        token, so a connection without one appeared in no row at all — the
        state every wizard-connected instance starts in. It must now be a
        row that names its own cause.
        """
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-kbc", source_type="keboola", name="Production Project")

        semantic = _domains(compute_cross_domain_coverage(), "conn-kbc")["semantic"]
        assert semantic["status"] == "missing"
        assert "owner" in semantic["detail"]
        assert semantic["action"]["href"] == "/admin/data-sources"

    def test_the_keboola_provider_record_rides_along_as_raw(self, pg_state, monkeypatch):
        """One general place for detail, filled to whatever depth the source
        type can compute — rich here, thin for a native adapter."""
        from src.semantic import coverage as coverage_module
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-kbc", source_type="keboola", name="Production Project")
        monkeypatch.setattr(
            coverage_module,
            "_keboola_coverage_by_connection",
            lambda: {
                "conn-kbc": {
                    "connection_id": "conn-kbc",
                    "models": [{"uuid": "m1", "name": "core"}],
                    "metrics": {"upstream": 10, "importable": 4},
                    "blocked": [{"metric": "mrr", "reason": "embedded_sql_comment"}],
                    "unregistered_tables": ["in.c-demo.orders"],
                    "warnings": [{"code": "metrics_blocked", "message": "…"}],
                    "error": None,
                }
            },
        )

        semantic = _domains(compute_cross_domain_coverage(), "conn-kbc")["semantic"]
        assert semantic["status"] == "partial"
        assert semantic["raw"]["blocked"][0]["metric"] == "mrr"
        assert semantic["raw"]["unregistered_tables"] == ["in.c-demo.orders"]

    def test_a_native_adapter_source_with_no_linked_semantic_source_is_missing(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Snowflake")

        semantic = _domains(compute_cross_domain_coverage(), "conn-sf")["semantic"]
        assert semantic["status"] == "missing"
        assert "semantic source" in semantic["detail"]

    def test_a_native_adapter_source_with_an_imported_model_is_ok(self, pg_state):
        from datetime import datetime, timezone

        from src.repositories import semantic_model_repo, semantic_source_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Snowflake")
        semantic_source_repo().create(
            id="ss_1",
            kind="connection",
            name="Snowflake semantic views",
            adapter="snowflake_semantic",
            config={"connection_id": "conn-sf"},
        )
        semantic_model_repo().upsert(
            id="ossie_connection/ss_1/sales",
            slug="sales",
            name="sales",
            description=None,
            document="version: '0.2.0.dev0'",
            document_json={"semantic_model": [{"name": "sales"}]},
            spec_version="0.2.0.dev0",
            content_hash="abc",
            source="ossie_connection",
            source_ref="ss_1",
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(timezone.utc),
        )

        semantic = _domains(compute_cross_domain_coverage(), "conn-sf")["semantic"]
        assert semantic["status"] == "ok"
        assert semantic["raw"]["models"] == 1

    def test_a_semantic_source_recording_no_connection_is_credited_to_nobody(self, pg_state):
        """Attributing an unlinked source to every connection of its type
        would credit one project's model to its neighbour."""
        from src.repositories import semantic_source_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Snowflake")
        semantic_source_repo().create(
            id="ss_1",
            kind="connection",
            name="Unlinked",
            adapter="snowflake_semantic",
            config={},
        )

        assert _domains(compute_cross_domain_coverage(), "conn-sf")["semantic"]["status"] == "missing"


class TestGlossary:
    def test_terms_stamped_with_the_connection_count(self, pg_state):
        from src.repositories import glossary_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        glossary_repo().create(id="g1", term="MRR", definition="…", source_ref="conn-a")

        glossary = _domains(compute_cross_domain_coverage(), "conn-a")["glossary"]
        assert glossary["status"] == "ok"
        assert "1 term" in glossary["detail"]

    def test_no_terms_is_missing_with_an_action(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")

        glossary = _domains(compute_cross_domain_coverage(), "conn-a")["glossary"]
        assert glossary["status"] == "missing"
        assert glossary["action"]["href"]

    def test_terms_stamped_with_a_linked_semantic_source_count_for_its_connection(self, pg_state):
        """``glossary_terms.source_ref`` carries two namespaces.

        The Keboola metastore sync stamps the ``source_connections.id``
        (covered above), but ``src/semantic/importer.py`` stamps the
        ``semantic_sources.id`` the document arrived through
        (``import_source()`` -> ``"source_ref": source_id``). Reading only the
        first namespace reported ``missing`` for every source fed by a
        registered semantic source, next to an "Add glossary terms" link, while
        the terms sat in the database.
        """
        from src.repositories import glossary_repo, semantic_source_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Snowflake")
        semantic_source_repo().create(
            id="ss_1",
            kind="connection",
            name="Snowflake semantic views",
            adapter="snowflake_semantic",
            config={"connection_id": "conn-sf"},
        )
        glossary_repo().create(id="g1", term="MRR", definition="…", source_ref="ss_1")

        glossary = _domains(compute_cross_domain_coverage(), "conn-sf")["glossary"]
        assert glossary["status"] == "ok"
        assert "1 term" in glossary["detail"]

    def test_terms_from_a_linked_source_are_not_also_counted_in_the_local_bucket(self, pg_state):
        """The other half of the same bug: those terms were counted NOWHERE.

        The synthetic bucket claims only ``source_ref IS NULL``, so a term
        stamped with a semantic-source id fell out of its connection's column
        AND out of the bucket — the exact "counted nowhere" hole the bucket
        exists to close.
        """
        from src.repositories import glossary_repo, semantic_source_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-sf", source_type="snowflake", name="Snowflake")
        semantic_source_repo().create(
            id="ss_1",
            kind="connection",
            name="Snowflake semantic views",
            adapter="snowflake_semantic",
            config={"connection_id": "conn-sf"},
        )
        _table("t1", connection_id=None, source_type="local")
        glossary_repo().create(id="g1", term="MRR", definition="…", source_ref="ss_1")

        report = compute_cross_domain_coverage()
        assert _domains(report, "conn-sf")["glossary"]["status"] == "ok"
        # the bucket exists (an unattributed table put it there) but claims no
        # glossary term of its own
        assert _domains(report, "__local__")["glossary"]["status"] == "missing"

    def test_a_semantic_source_linked_to_another_connection_does_not_leak(self, pg_state):
        """Resolving the second namespace must not over-credit: a term owned by
        one connection's semantic source stays out of its neighbour's column."""
        from src.repositories import glossary_repo, semantic_source_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="snowflake", name="A")
        _connection("conn-b", source_type="snowflake", name="B")
        semantic_source_repo().create(
            id="ss_a",
            kind="connection",
            name="A's views",
            adapter="snowflake_semantic",
            config={"connection_id": "conn-a"},
        )
        glossary_repo().create(id="g1", term="MRR", definition="…", source_ref="ss_a")

        report = compute_cross_domain_coverage()
        assert _domains(report, "conn-a")["glossary"]["status"] == "ok"
        assert _domains(report, "conn-b")["glossary"]["status"] == "missing"


class TestTagBackedDomains:
    def test_an_untagged_source_is_missing_in_all_three(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")

        domains = _domains(compute_cross_domain_coverage(), "conn-a")
        for name in ("skill", "agent", "knowledge_base"):
            assert domains[name]["status"] == "missing", name
            assert "tag_source=conn-a" in domains[name]["action"]["href"]

    def test_a_tag_flips_only_its_own_domain(self, pg_state):
        from src.repositories import resource_source_tags_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        resource_source_tags_repo().create(
            resource_type="marketplace_plugin",
            resource_id="mkt-1/revenue-skill",
            source_id="conn-a",
            tagged_by="admin@test.com",
        )

        domains = _domains(compute_cross_domain_coverage(), "conn-a")
        assert domains["skill"]["status"] == "ok"
        assert domains["skill"]["raw"]["resource_ids"] == ["mkt-1/revenue-skill"]
        assert domains["agent"]["status"] == "missing"
        assert domains["knowledge_base"]["status"] == "missing"

    def test_a_tag_on_another_source_does_not_leak(self, pg_state):
        from src.repositories import resource_source_tags_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="A")
        _connection("conn-b", source_type="bigquery", name="B")
        resource_source_tags_repo().create(resource_type="agent", resource_id="ag-1", source_id="conn-a", tagged_by="x")

        report = compute_cross_domain_coverage()
        assert _domains(report, "conn-a")["agent"]["status"] == "ok"
        assert _domains(report, "conn-b")["agent"]["status"] == "missing"


class TestTheSyntheticLocalBucket:
    def test_tables_with_no_connection_get_their_own_row(self, pg_state):
        from src.semantic.coverage import LOCAL_BUCKET_NAME, compute_cross_domain_coverage

        _table("uploaded_csv", connection_id=None, source_type="local")

        report = compute_cross_domain_coverage()
        local = [s for s in report["sources"] if s["source_id"] == "__local__"]
        assert local, "tables with no connection were counted nowhere"
        assert local[0]["name"] == LOCAL_BUCKET_NAME
        assert local[0]["domains"]["metrics"]["status"] == "missing"

    def test_the_bucket_does_not_appear_when_nothing_is_unattributed(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")

        report = compute_cross_domain_coverage()
        assert [s["source_id"] for s in report["sources"]] == ["conn-a"]

    def test_the_bucket_cannot_offer_a_tag_it_has_no_source_for(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _table("uploaded_csv", connection_id=None, source_type="local")

        domains = _domains(compute_cross_domain_coverage(), "__local__")
        for name in ("skill", "agent", "knowledge_base"):
            assert domains[name]["status"] == "not_applicable", name
            assert domains[name]["action"] is None

    def test_unattributed_glossary_terms_surface_in_the_bucket(self, pg_state):
        """The retired page's "legacy / unattributed" row existed because a
        NULL-``source_ref`` term was otherwise counted nowhere. Same hole,
        same fix, now for every source type."""
        from src.repositories import glossary_repo
        from src.semantic.coverage import compute_cross_domain_coverage

        glossary_repo().create(id="g1", term="MRR", definition="…", source_ref=None)

        assert _domains(compute_cross_domain_coverage(), "__local__")["glossary"]["status"] == "ok"


class TestTheSourceFilter:
    def test_source_narrows_the_report(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="A")
        _connection("conn-b", source_type="bigquery", name="B")

        report = compute_cross_domain_coverage("conn-b")
        assert [s["source_id"] for s in report["sources"]] == ["conn-b"]

    def test_an_unknown_source_is_an_empty_list_not_an_error(self, pg_state):
        from src.semantic.coverage import compute_cross_domain_coverage

        _connection("conn-a", source_type="bigquery", name="A")

        assert compute_cross_domain_coverage("conn-nope") == {"sources": []}


class TestTheEndpointOnPostgres:
    def test_coverage_answers_200_with_the_report(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("DuckDB side is pinned in tests/test_semantic_model_coverage_endpoint.py")
        _connection("conn-a", source_type="bigquery", name="Warehouse")

        resp = seeded_app_both["client"].get(
            _COVERAGE, headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        )
        assert resp.status_code == 200, resp.text
        assert [s["source_id"] for s in resp.json()["sources"]] == ["conn-a"]

    def test_tag_create_then_delete_round_trips(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")
        auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        client = seeded_app_both["client"]

        created = client.post(
            _TAGS,
            json={"resource_type": "agent", "resource_id": "ag-1", "source_id": "conn-a"},
            headers=auth,
        )
        assert created.status_code == 201, created.text
        tag_id = created.json()["id"]

        report = client.get(_COVERAGE, headers=auth).json()
        assert report["sources"][0]["domains"]["agent"]["status"] == "ok"

        assert client.delete(f"{_TAGS}/{tag_id}", headers=auth).status_code == 204
        report = client.get(_COVERAGE, headers=auth).json()
        assert report["sources"][0]["domains"]["agent"]["status"] == "missing"

    def test_tagging_the_same_triple_twice_is_a_409(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")
        auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        client = seeded_app_both["client"]
        payload = {"resource_type": "agent", "resource_id": "ag-1", "source_id": "conn-a"}

        assert client.post(_TAGS, json=payload, headers=auth).status_code == 201
        conflict = client.post(_TAGS, json=payload, headers=auth)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error"] == "already_tagged"

    def test_an_untaggable_resource_type_is_refused(self, state_backend, seeded_app_both):
        """A tag on a type no coverage column reads would be a row nobody
        ever looks at."""
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")

        resp = seeded_app_both["client"].post(
            _TAGS,
            json={"resource_type": "table", "resource_id": "orders", "source_id": "conn-a"},
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "unknown_resource_type"

    def test_a_tag_pointing_at_no_source_is_refused(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        resp = seeded_app_both["client"].post(
            _TAGS,
            json={"resource_type": "agent", "resource_id": "ag-1", "source_id": "conn-ghost"},
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_source"

    def test_deleting_an_unknown_tag_is_a_404(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        resp = seeded_app_both["client"].delete(
            f"{_TAGS}/rst_nope", headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        )
        assert resp.status_code == 404


class TestTheCli:
    """``agnes semantic-model coverage …`` over the same endpoints.

    Lives here rather than in ``tests/test_cli_api_parity.py``: that harness
    snapshots the DuckDB system DB directly to diff API-vs-CLI state, and this
    feature has no DuckDB table to snapshot.
    """

    def test_coverage_prints_a_grid_and_names_the_gaps(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _table("orders", connection_id="conn-a")

        result = cli_client_both["invoke"](["semantic-model", "coverage"])
        assert result.exit_code == 0, result.output
        # The grid …
        assert "SOURCE" in result.output
        assert "Warehouse" in result.output
        # … and, beneath it, what to do about each gap. A grid alone is a
        # scoreboard, and this report is not one.
        assert "metrics:" in result.output
        assert "/admin/studio/semantic-layer" in result.output

    def test_json_emits_the_endpoint_payload(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        import json

        _connection("conn-a", source_type="bigquery", name="Warehouse")

        result = cli_client_both["invoke"](["semantic-model", "coverage", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [s["source_id"] for s in payload["sources"]] == ["conn-a"]

    def test_source_narrows_the_grid(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")
        _connection("conn-b", source_type="bigquery", name="Lakehouse")

        result = cli_client_both["invoke"](["semantic-model", "coverage", "--source", "conn-b"])
        assert result.exit_code == 0, result.output
        assert "Lakehouse" in result.output
        assert "Warehouse" not in result.output

    def test_show_is_the_explicit_form_of_the_bare_command(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")

        bare = cli_client_both["invoke"](["semantic-model", "coverage", "--json"])
        explicit = cli_client_both["invoke"](["semantic-model", "coverage", "show", "--json"])
        assert bare.exit_code == explicit.exit_code == 0
        assert bare.output == explicit.output

    def test_tag_then_untag_round_trips(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        import json

        _connection("conn-a", source_type="bigquery", name="Warehouse")

        tagged = cli_client_both["invoke"](["semantic-model", "coverage", "tag", "agent", "ag-1", "conn-a"])
        assert tagged.exit_code == 0, tagged.output
        assert "Tagged" in tagged.output

        report = json.loads(cli_client_both["invoke"](["semantic-model", "coverage", "--json"]).output)
        agent_domain = report["sources"][0]["domains"]["agent"]
        assert agent_domain["status"] == "ok"

        tag_id = tagged.output.split("(tag ")[1].rstrip(")\n")
        untagged = cli_client_both["invoke"](["semantic-model", "coverage", "untag", tag_id])
        assert untagged.exit_code == 0, untagged.output

        report = json.loads(cli_client_both["invoke"](["semantic-model", "coverage", "--json"]).output)
        assert report["sources"][0]["domains"]["agent"]["status"] == "missing"

    def test_an_untaggable_type_is_refused_before_the_call(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")

        result = cli_client_both["invoke"](["semantic-model", "coverage", "tag", "table", "orders", "conn-a"])
        assert result.exit_code == 1
        assert "marketplace_plugin" in result.output

    def test_a_duplicate_tag_hints_the_next_step(self, state_backend, cli_client_both):
        """Command-UX standard: a refusal names what to do next."""
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        _connection("conn-a", source_type="bigquery", name="Warehouse")

        assert (
            cli_client_both["invoke"](["semantic-model", "coverage", "tag", "agent", "ag-1", "conn-a"]).exit_code == 0
        )
        again = cli_client_both["invoke"](["semantic-model", "coverage", "tag", "agent", "ag-1", "conn-a"])
        assert again.exit_code == 1
        assert "Already tagged" in again.output
        assert "coverage --json" in again.output

    def test_untagging_an_unknown_id_hints_the_next_step(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        result = cli_client_both["invoke"](["semantic-model", "coverage", "untag", "rst_nope"])
        assert result.exit_code == 1
        assert "coverage --json" in result.output

    def test_a_duckdb_instance_is_told_it_needs_postgres(self, state_backend, cli_client_both):
        """The other direction of the same fixture: the CLI must translate the
        typed 501 into the migration instruction, not a bare status code."""
        if state_backend != "duckdb":
            pytest.skip("this is the DuckDB half")

        result = cli_client_both["invoke"](["semantic-model", "coverage"])
        assert result.exit_code == 1
        assert "Postgres" in result.output
        assert "docs/migrations.md" in result.output
