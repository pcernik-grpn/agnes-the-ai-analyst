"""Projecting an Ossie document into the flat tables queries actually read
(metric_definitions, glossary_terms, column_metadata), scoped and pruned per
(source, source_ref) so two sources can never delete each other's rows.

Reuses the ``e2e_env`` DATA_DIR-isolation fixture from ``tests/conftest.py``
under the ``system_db`` name the plan assumed — ``tests/conftest.py`` has no
fixture literally named ``system_db``; ``e2e_env`` gives each test its own
DATA_DIR (and therefore its own system.duckdb, auto-migrated on first
``get_system_db()`` call), which is exactly the isolation these tests need.
"""

import json

import pytest

from src.semantic.projection import project_document, prune_model


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


DOC = {
    "semantic_model": [
        {
            "name": "retail",
            "datasets": [
                {
                    "name": "orders",
                    "source": "db.public.orders",
                    "fields": [
                        {
                            "name": "order_date",
                            "datatype": "Date",
                            "description": "when the order was placed",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "order_date"}]},
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "datatype": "Decimal",
                    "description": "total revenue",
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                },
                {
                    "name": "wh_only",
                    "expression": {"dialects": [{"dialect": "SNOWFLAKE", "expression": "TRY_CAST(x AS NUMBER)"}]},
                },
            ],
        }
    ]
}


def test_projects_metrics_and_columns(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    assert report.metrics_written == 1
    assert report.columns_written == 1


def test_unusable_metric_is_reported_not_written(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    skipped = [s for s in report.skipped if s["name"] == "wh_only"]
    assert len(skipped) == 1
    assert "SNOWFLAKE" in skipped[0]["reason"]


def _stub_dataset(name="orders"):
    # The real schema sets `minItems: 1` on `datasets` and requires
    # ["name", "datasets"] on a model, so `"datasets": []` is NOT a legal
    # document even though project_document never validates. Keep fixtures
    # schema-legal or they become a trap the moment anything validates them.
    return {"name": name, "source": f"db.public.{name}", "fields": []}


def test_reprojection_prunes_only_this_origin(system_db):
    project_document(DOC, source="git", source_ref="repo-a")
    other = {
        "semantic_model": [
            {
                "name": "fin",
                "datasets": [_stub_dataset("costs")],
                "metrics": [
                    {"name": "cost", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(c)"}]}}
                ],
            }
        ]
    }
    project_document(other, source="git", source_ref="repo-b")

    shrunk = {"semantic_model": [{"name": "retail", "datasets": [_stub_dataset()], "metrics": []}]}
    project_document(shrunk, source="git", source_ref="repo-a")

    from src.repositories import metric_repo

    # NOTE: the plan's Step-1 test body calls `metric_repo().list_all()`, but
    # MetricRepository has no `list_all` — only `list(category=None)`, which
    # already returns every metric when called with no argument. Adapted here
    # (see the report for this task).
    remaining = {m["name"] for m in metric_repo().list()}
    assert "revenue" not in remaining, "repo-a's dropped metric should be pruned"
    assert "cost" in remaining, "prune must not cross a source_ref boundary"


# --- Additional coverage beyond the plan's Step-1 body -----------------
#
# The plan's own test bodies never exercise the glossary-via-custom_extensions
# rule or the column-level prune, but both are explicit projection rules for
# this task. Covered here so the behavior has a regression test at all.


def test_glossary_is_projected_from_custom_extensions(system_db):
    doc = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [_stub_dataset()],
                "custom_extensions": [
                    {
                        "vendor_name": "AGNES",
                        "data": json.dumps(
                            {
                                "glossary": [
                                    {"term": "ARR", "definition": "Annual recurring revenue."},
                                ]
                            }
                        ),
                    }
                ],
            }
        ]
    }
    report = project_document(doc, source="git", source_ref="repo-a")
    assert report.glossary_written == 1

    from src.repositories import glossary_repo

    terms = {g["term"] for g in glossary_repo().list(limit=1000)}
    assert "ARR" in terms


def test_glossary_is_projected_from_a_lowercase_vendor_tag(system_db):
    """Devin #1398: the vendor tag matches case-insensitively (like the query
    validator and the browse UI), so a hand-authored document spelling it
    `agnes` projects rather than being silently dropped from the flat
    catalog while it still browses/validates."""
    doc = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [_stub_dataset()],
                "custom_extensions": [
                    {"vendor_name": "agnes", "data": json.dumps({"glossary": [{"term": "ARR", "definition": "x"}]})}
                ],
            }
        ]
    }
    report = project_document(doc, source="git", source_ref="repo-a")
    assert report.glossary_written == 1

    from src.repositories import glossary_repo

    assert "ARR" in {g["term"] for g in glossary_repo().list(limit=1000)}


def test_document_without_glossary_extension_writes_none(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    assert report.glossary_written == 0


def test_column_prune_removes_a_dropped_field(system_db):
    project_document(DOC, source="git", source_ref="repo-a")

    shrunk = {"semantic_model": [{"name": "retail", "datasets": [_stub_dataset()], "metrics": []}]}
    project_document(shrunk, source="git", source_ref="repo-a")

    from src.repositories import column_metadata_repo

    remaining = column_metadata_repo().list_for_table("db.public.orders")
    assert remaining == []


def test_glossary_custom_extension_from_another_vendor_is_ignored(system_db):
    doc = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [_stub_dataset()],
                "custom_extensions": [
                    {"vendor_name": "SNOWFLAKE", "data": json.dumps({"glossary": [{"term": "X", "definition": "y"}]})}
                ],
            }
        ]
    }
    report = project_document(doc, source="git", source_ref="repo-a")
    assert report.glossary_written == 0


# ---------------------------------------------------------------------------
# The AGNES custom_extensions block. Ossie's Metric has
# `additionalProperties: false` and no dataset link at all, so a metric cannot
# carry its table binding, its grain or its constraints in the core schema.
# The Keboola adapter already files all three under the AGNES vendor name
# (connectors/keboola/semantic_ossie.py) — this is the projector learning to
# read what the adapter has been writing, which is the whole flat-table
# cutover in one step.
# ---------------------------------------------------------------------------

_AGNES = "AGNES"


def _ext(payload: dict) -> dict:
    return {"vendor_name": _AGNES, "data": json.dumps(payload)}


def _register_table(source_type: str, bucket: str, source_table: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name,
            name=name,
            source_type=source_type,
            bucket=bucket,
            source_table=source_table,
            query_mode="local",
        )
    finally:
        conn.close()


def _register_keboola_table(bucket: str, source_table: str, name: str) -> None:
    _register_table("keboola", bucket, source_table, name)


def _doc(*, metric_ext=None, dataset_ext=None, model_ext=None, table_id="in.c-shop.orders"):
    metric: dict = {
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
    }
    if metric_ext is not None:
        metric["custom_extensions"] = [_ext(metric_ext)]

    dataset: dict = {"name": "orders", "source": table_id}
    if dataset_ext is not None:
        dataset["custom_extensions"] = [_ext(dataset_ext)]

    model: dict = {"name": "retail", "datasets": [dataset], "metrics": [metric]}
    if model_ext is not None:
        model["custom_extensions"] = [_ext(model_ext)]
    return {"semantic_model": [model]}


def _only_metric(source="keboola_metastore", source_ref="conn-1"):
    from src.repositories import metric_repo

    rows = [m for m in metric_repo().list() if (m.get("source") or "") == source]
    assert len(rows) == 1, rows
    row = dict(rows[0])
    # `validation` comes back as a JSON STRING on DuckDB (the repo's
    # `_row_to_dict` only zips columns) and as a dict on Postgres (JSONB).
    # Readers already cope with both — `cli/commands/catalog.py` parses a
    # string, `src/data_semantics_scaffold.py::_maybe_json` too — so these
    # tests normalize rather than pin one backend's representation.
    if isinstance(row.get("validation"), str):
        row["validation"] = json.loads(row["validation"])
    return row


class TestTableBinding:
    def test_a_bound_metric_gets_runnable_sql_and_its_table(self, system_db):
        """The legacy Keboola composer produced `SELECT <frag> FROM "view" AS t`.
        Projecting a bare fragment instead would be a regression at cutover:
        `agnes catalog --metrics --show` would hand the agent SQL it cannot run.
        """
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["table_name"] == "shop_orders"
        assert row["sql"].startswith("SELECT ")
        assert "SUM(amount)" in row["sql"]
        assert "FROM" in row["sql"]

    def test_an_unbound_metric_keeps_its_fragment(self, system_db):
        """A document with no AGNES binding — a plain upstream Ossie file from a
        git source — projects the fragment as-is. Asserted explicitly so nobody
        later "fixes" it into a guess about which table it belongs to.
        """
        project_document(_doc(), source="keboola_metastore", source_ref="conn-1")

        row = _only_metric()
        assert row["sql"] == "SUM(amount)"
        assert row["table_name"] is None
        # No binding to compose against — `expression` equals the fragment too.
        assert row["expression"] == "SUM(amount)"

    def test_a_binding_to_an_unregistered_table_is_skipped(self, system_db):
        """A metric that DECLARES a table binding it cannot honor is dropped,
        matching the legacy Keboola composer (which skips `unresolved_table`).
        The steady state — a semantic layer describing more tables than the
        instance registers — is exactly when this fires, and keeping the metric
        as a bare fragment would make the flat-table cutover start surfacing
        unrunnable metrics on tables nobody registered. Contrast
        `test_an_unbound_metric_keeps_its_fragment`: a metric that declares NO
        binding keeps its fragment, because it never claimed a table."""
        from src.repositories import metric_repo

        project_document(
            _doc(metric_ext={"dataset": "in.c-nowhere.ghosts"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        assert [m for m in metric_repo().list() if m.get("source") == "keboola_metastore"] == []

    def test_a_snowflake_shaped_binding_resolves_via_the_generic_path(self, system_db):
        """A hand-authored/uploaded model's dataset references a
        Snowflake/Databricks-style 3-segment identifier
        (`DATABASE.SCHEMA.TABLE`). The Keboola-shaped path (last-dot split)
        would misparse this as bucket='ESHOP_DEMO.RAW', table='ORDERS' and
        never match; the generic last-TWO-segments path matches
        bucket='RAW', table='ORDERS' against the registered row instead."""
        _register_table("snowflake", "RAW", "ORDERS", "snowflake_orders")

        project_document(
            _doc(metric_ext={"dataset": "ESHOP_DEMO.RAW.ORDERS"}, table_id="ESHOP_DEMO.RAW.ORDERS"),
            source="manual",
            source_ref=None,
        )

        row = _only_metric(source="manual", source_ref=None)
        assert row["table_name"] == "snowflake_orders"
        assert row["sql"].startswith("SELECT ")
        assert "SUM(amount)" in row["sql"]

    def test_a_case_mismatched_snowflake_binding_still_resolves(self, system_db):
        """Snowflake's information-schema identifiers come back UPPERCASE
        unless the object was created quoted, but a table can be registered
        with any case an admin chose — the generic path must fold case on
        both sides rather than require them to already agree."""
        _register_table("snowflake", "raw", "orders", "snowflake_orders")

        project_document(
            _doc(metric_ext={"dataset": "ESHOP_DEMO.RAW.ORDERS"}, table_id="ESHOP_DEMO.RAW.ORDERS"),
            source="manual",
            source_ref=None,
        )

        row = _only_metric(source="manual", source_ref=None)
        assert row["table_name"] == "snowflake_orders"

    def test_a_keboola_shaped_binding_still_resolves_when_other_tables_are_registered(self, system_db):
        """Regression guard: registering a non-Keboola table alongside a
        Keboola one must not change the Keboola path's own resolution."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")
        _register_table("snowflake", "RAW", "ORDERS", "snowflake_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["table_name"] == "shop_orders"

    def test_a_multi_segment_binding_to_an_unregistered_table_is_skipped(self, system_db):
        """The "never raises, resolves to None" contract holds for the
        generic path too: a 3-segment identifier matching nothing in the
        registry is skipped, not crashed on or bound to the wrong row."""
        from src.repositories import metric_repo

        project_document(
            _doc(metric_ext={"dataset": "ESHOP_DEMO.RAW.NOWHERE"}, table_id="ESHOP_DEMO.RAW.NOWHERE"),
            source="manual",
            source_ref=None,
        )

        assert [m for m in metric_repo().list() if m.get("source") == "manual"] == []


class TestConstraints:
    def test_model_constraints_reach_the_metric_they_name(self, system_db):
        project_document(
            _doc(
                model_ext={
                    "constraints": [
                        {
                            "name": "non_negative",
                            "constraint_type": "range",
                            "rule": "value >= 0",
                            "metrics": ["revenue"],
                            "severity": "error",
                        }
                    ]
                }
            ),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        validation = _only_metric()["validation"]
        assert validation is not None
        assert [r["name"] for r in validation["rules"]] == ["non_negative"]
        assert validation["rules"][0]["rule"] == "value >= 0"
        assert validation["rules"][0]["severity"] == "error"

    def test_a_constraint_naming_another_metric_is_not_attached(self, system_db):
        project_document(
            _doc(
                model_ext={
                    "constraints": [
                        {"name": "other_rule", "rule": "value < 10", "metrics": ["margin"], "severity": "warning"}
                    ]
                }
            ),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        assert _only_metric()["validation"] is None


class TestDatasetGrain:
    def test_dataset_grain_is_reported_as_a_note_not_as_the_metrics_grain(self, system_db):
        """A dataset's grain is a true fact about the DATASET. Writing it into
        `metric_definitions.grain` restates it as a fact about the metric, which
        is the misattribution wave 0 removed. As a note it keeps both the fact
        and its scope."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}, dataset_ext={"grain": "monthly"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["grain"] is None
        assert any("monthly" in n for n in (row["notes"] or []))

    def test_no_dataset_grain_means_no_note(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["grain"] is None
        assert not [n for n in (row["notes"] or []) if "grain" in n]


class TestColumnBinding:
    """The column leg keys `column_metadata` on the RESOLVED Agnes
    `table_registry` id (via `resolve_dataset_table`), the same identifier
    `/api/v2/schema/{table_id}` and every other `column_metadata` reader
    already key on — mirroring the metric leg's own table-binding above.
    Before this resolution step, a Keboola dataset's raw tableId
    (`in.c-shop.orders`) was stored verbatim: nothing else ever reads
    `column_metadata` under a raw Keboola tableId, so an imported column
    description was written but never surfaced anywhere (a silent no-op).

    Resolving onto a real, shared table id reopens a collision risk a prior
    attempt at this exact change hit and had to revert: the profiler / admin
    metadata API / ai_enrichment already own rows under that same id. The
    write path guards against it (see the regression test below) — an
    existing row owned by a DIFFERENT writer always wins over this
    projection's write, the same precedence the manual-model path already
    had (`_column_source`), now applied unconditionally since resolution can
    land ANY source's write on an id another writer already owns.
    """

    def test_keboola_field_descriptions_land_under_the_resolved_agnes_id(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "in.c-shop.orders",
                            "fields": [
                                {"name": "amount", "datatype": "Decimal", "description": "Order amount, in cents."}
                            ],
                        }
                    ],
                }
            ]
        }
        report = project_document(doc, source="keboola_metastore", source_ref="conn-1")
        assert report.columns_written == 1

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        under_resolved_id = repo.list_for_table("shop_orders")
        assert [c["column_name"] for c in under_resolved_id] == ["amount"]
        assert under_resolved_id[0]["description"] == "Order amount, in cents."
        # Nothing lands under the raw Keboola tableId anymore.
        assert repo.list_for_table("in.c-shop.orders") == []

        # And it's retrievable through the actual production read path
        # (`GET /api/v2/schema/{table_id}`), not just the repo directly.
        from app.api.v2_schema import _column_metadata_descriptions

        assert _column_metadata_descriptions("shop_orders") == {"amount": "Order amount, in cents."}

    def test_keboola_field_descriptions_fall_back_to_the_raw_id_when_unregistered(self, system_db):
        """A dataset whose Keboola tableId doesn't resolve to any registered
        table (yet) keeps the pre-fix behavior — written under the raw id —
        rather than being dropped outright."""
        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "in.c-ghost.nowhere",
                            "fields": [{"name": "amount", "datatype": "Decimal", "description": "n/a"}],
                        }
                    ],
                }
            ]
        }
        report = project_document(doc, source="keboola_metastore", source_ref="conn-1")
        assert report.columns_written == 1

        from src.repositories import column_metadata_repo

        under_raw_id = column_metadata_repo().list_for_table("in.c-ghost.nowhere")
        assert [c["column_name"] for c in under_raw_id] == ["amount"]

    def test_prune_stays_scoped_to_the_resolved_id(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        def _doc_with_fields(field_names):
            return {
                "semantic_model": [
                    {
                        "name": "retail",
                        "datasets": [
                            {
                                "name": "orders",
                                "source": "in.c-shop.orders",
                                "fields": [{"name": n} for n in field_names],
                            }
                        ],
                    }
                ]
            }

        project_document(_doc_with_fields(["amount", "region"]), source="keboola_metastore", source_ref="conn-1")
        project_document(_doc_with_fields(["amount"]), source="keboola_metastore", source_ref="conn-1")

        from src.repositories import column_metadata_repo

        remaining = {c["column_name"] for c in column_metadata_repo().list_for_table("shop_orders")}
        assert remaining == {"amount"}, "the dropped field must be pruned under the resolved id"

    def test_profiler_authored_description_survives_keboola_projection(self, system_db):
        """Regression guard for the reverted column-binding change: a
        profiler/admin-authored `column_metadata` row for a Keboola-registered
        table (keyed under the resolved Agnes id) must not be clobbered by a
        semantic layer sync for that same table — the projection now resolves
        onto the SAME id the profiler already wrote, so the write-path
        precedence guard (an existing row owned by a different writer wins)
        is what keeps this description intact, not a difference in keys."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        repo.save(
            table_id="shop_orders",
            column_name="amount",
            basetype="DECIMAL",
            description="Authored by the profiler.",
            source="profiler",
        )

        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "in.c-shop.orders",
                            # Keboola fields frequently have no description —
                            # the case that used to blank the profiler's row.
                            "fields": [{"name": "amount", "datatype": "Decimal", "description": None}],
                        }
                    ],
                }
            ]
        }
        project_document(doc, source="keboola_metastore", source_ref="conn-1")

        row = repo.get("shop_orders", "amount")
        assert row["description"] == "Authored by the profiler."
        assert row["source"] == "profiler"

    def test_sibling_keboola_models_sharing_a_resolved_table_do_not_prune_each_others_columns(self, system_db):
        """Regression: the column leg's write path resolves `table_id` via
        `resolve_dataset_table`, but `_sibling_column_claims` used to key its
        claims on the RAW dataset id — a mismatch invisible for `source=
        'manual'` (whose raw id already IS the resolved one) but live for
        every other source. A partial projection's `keep_by_table.get
        (resolved_id)` then always missed a sibling's claim, so projecting
        one Keboola model onto a shared table pruned a sibling model's
        columns for that same table outright."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        def _doc(model_name, field_names):
            return {
                "semantic_model": [
                    {
                        "name": model_name,
                        "datasets": [
                            {
                                "name": "orders",
                                "source": "in.c-shop.orders",
                                "fields": [{"name": n} for n in field_names],
                            }
                        ],
                    }
                ]
            }

        # `_sibling_column_claims` reads OTHER models' claims from their
        # STORED `semantic_models` rows (as the real importer writes them
        # before projecting, per `src/semantic/importer.py`) — a bare
        # `project_document` call alone never populates that table, so each
        # model's document must be seeded here too, or the sibling read sees
        # nothing and the test can't tell a real fix from a no-op one.
        from src.repositories import semantic_model_repo

        def _seed(model_name, field_names):
            doc = _doc(model_name, field_names)
            semantic_model_repo().upsert(
                id=f"keboola_metastore/conn-1/{model_name}",
                slug=model_name,
                name=model_name,
                description=None,
                document="version: '0.2.0.dev0'",
                document_json=doc,
                spec_version="0.2.0.dev0",
                content_hash=model_name,
                source="keboola_metastore",
                source_ref="conn-1",
                status="valid",
                validation_errors=None,
                validated_at=None,
            )
            return doc

        project_document(_seed("retail", ["col_a"]), source="keboola_metastore", source_ref="conn-1", partial=True)
        project_document(_seed("finance", ["col_b"]), source="keboola_metastore", source_ref="conn-1", partial=True)

        from src.repositories import column_metadata_repo

        remaining = {c["column_name"] for c in column_metadata_repo().list_for_table("shop_orders")}
        assert remaining == {"col_a", "col_b"}, "projecting finance must not prune retail's sibling columns"

    def test_deleting_a_keboola_model_prunes_its_own_columns_under_the_resolved_id(self, system_db):
        """Regression: `prune_model`'s `written_by_table` used to key on the
        RAW dataset id too, so `repo.list_for_table(raw_id)` found nothing
        for a Keboola model (whose rows now live under the resolved id) and
        deleting the model's document left its `column_metadata` rows
        orphaned — never cleaned up."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "in.c-shop.orders",
                            "fields": [{"name": "amount"}],
                        }
                    ],
                }
            ]
        }
        project_document(doc, source="keboola_metastore", source_ref="conn-1")

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        assert repo.get("shop_orders", "amount") is not None

        prune_model(doc, source="keboola_metastore", source_ref="conn-1")

        assert repo.get("shop_orders", "amount") is None, "deleting the model must not orphan its column_metadata row"

    def test_manual_dataset_source_is_never_resolved(self, system_db):
        """Regression (Devin, PR #1673): a manual dataset's `source` is
        ALREADY an Agnes table id by convention, so it must land under that
        literal string, not whatever `resolve_dataset_table` maps it to.
        `table_registry.id` is derived from `name` (e.g. `request.name
        .strip().lower().replace(" ", "_")` in `app/api/admin.py`), so a
        table registered with a display name containing spaces/uppercase
        has `id != name` — before this guard, a manual dataset whose
        `source` matched that NAME would silently resolve onto the
        DIFFERENT `id`, orphaning any pre-existing column row under the
        raw name key."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="orders_table",
                name="Orders Table",
                source_type="local",
                query_mode="local",
            )
        finally:
            conn.close()

        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "Orders Table",
                            "fields": [{"name": "amount"}],
                        }
                    ],
                }
            ]
        }
        project_document(doc, source="manual", source_ref=None)

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        assert repo.get("Orders Table", "amount") is not None, "must land under the raw dataset source, unresolved"
        assert repo.get("orders_table", "amount") is None, "must NOT resolve onto table_registry's derived id"


class TestDuplicateModelName:
    """A document with NO stable model identifier falls back to the model
    name as the id key, so two same-named models genuinely collide — the
    second must be skipped and reported, never silently overwrite the first.
    (A document that DOES carry one — every Keboola-composed document —
    cannot collide at all; see ``TestStableModelKey``.)"""

    def test_second_model_with_a_duplicate_name_is_reported_not_merged(self, system_db):
        doc = {
            "semantic_model": [
                {
                    "name": "core",
                    "datasets": [_stub_dataset("first")],
                    "metrics": [
                        {
                            "name": "metric_a",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(a)"}]},
                        }
                    ],
                },
                {
                    "name": "core",
                    "datasets": [_stub_dataset("second")],
                    "metrics": [
                        {
                            "name": "metric_b",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(b)"}]},
                        }
                    ],
                },
            ]
        }
        report = project_document(doc, source="git", source_ref="repo-a")

        assert report.metrics_written == 1
        assert {"kind": "model", "name": "core", "reason": "duplicate_model_key"} in report.skipped

        from src.repositories import metric_repo

        names = {m["name"] for m in metric_repo().list()}
        assert "metric_a" in names
        assert "metric_b" not in names


class TestNameCollision:
    """`metric_definitions.name` has no uniqueness constraint (see
    `src/db.py`'s comment on the table) — a same-named metric from a
    DIFFERENT source is a same-transaction WARN + count, never a block: both
    rows are written under their own ids."""

    def _doc(self, model_name: str, metric_name: str) -> dict:
        return {
            "semantic_model": [
                {
                    "name": model_name,
                    "datasets": [_stub_dataset(f"{model_name}_ds")],
                    "metrics": [
                        {
                            "name": metric_name,
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(x)"}]},
                        }
                    ],
                }
            ]
        }

    def test_same_name_different_source_is_counted_and_both_rows_land(self, system_db):
        project_document(self._doc("first", "revenue"), source="git", source_ref="repo-a")
        report = project_document(self._doc("second", "revenue"), source="git", source_ref="repo-b")

        assert report.name_collisions == 1
        assert report.metrics_written == 1

        from src.repositories import metric_repo

        rows = [m for m in metric_repo().list() if m["name"] == "revenue"]
        assert len(rows) == 2
        assert {r["source_ref"] for r in rows} == {"repo-a", "repo-b"}

    def test_same_name_same_source_ref_reprojection_is_not_a_collision(self, system_db):
        """Re-projecting the SAME (source, source_ref)'s own metric under its
        own id must never count as a collision against itself."""
        project_document(self._doc("first", "revenue"), source="git", source_ref="repo-a")
        report = project_document(self._doc("first", "revenue"), source="git", source_ref="repo-a")
        assert report.name_collisions == 0

    def test_no_collision_when_name_is_unclaimed(self, system_db):
        report = project_document(self._doc("first", "brand_new_metric"), source="git", source_ref="repo-a")
        assert report.name_collisions == 0


class TestGlossarySlugCollision:
    """`_scoped_id` keys a glossary row on `_slugify(term)`; two distinct
    terms that slugify identically must not collide and overwrite each
    other (the deleted `assign_glossary_id`'s numeric-suffix dedup)."""

    def test_two_same_slugging_terms_are_both_written_under_distinct_ids(self, system_db):
        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [_stub_dataset()],
                    "custom_extensions": [
                        {
                            "vendor_name": "AGNES",
                            "data": json.dumps(
                                {
                                    "glossary": [
                                        {"term": "Revenue (net)", "definition": "First definition."},
                                        {"term": "Revenue net", "definition": "Second definition."},
                                    ]
                                }
                            ),
                        }
                    ],
                }
            ]
        }
        report = project_document(doc, source="git", source_ref="repo-a")
        assert report.glossary_written == 2

        from src.repositories import glossary_repo

        rows = glossary_repo().list(limit=1000)
        base = "git/repo-a/retail/revenue_net"
        colliding = {r["id"]: r["term"] for r in rows if r["id"] == base or r["id"].startswith(f"{base}-")}
        assert len(colliding) == 2
        assert set(colliding.values()) == {"Revenue (net)", "Revenue net"}


# ---------------------------------------------------------------------------
# Model identity: projected ids key on a STABLE identifier, not a display name
# ---------------------------------------------------------------------------


def _model_with_metric(name: str, metric: str, *, metastore_id: str | None = None) -> dict:
    model: dict = {
        "name": name,
        "datasets": [{"name": "orders", "source": "db.public.orders"}],
        "metrics": [
            {
                "name": metric,
                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": f"SUM({metric})"}]},
            }
        ],
    }
    if metastore_id is not None:
        model["custom_extensions"] = [{"vendor_name": "AGNES", "data": json.dumps({"metastore_id": metastore_id})}]
    return model


class TestStableModelKey:
    """A model's ``name`` is a display name — neither unique nor stable.
    Keyed on it, two models named ``core`` produce identical metric ids;
    ``metric_repo().create`` upserts on id, so the later silently overwrites
    the earlier while ``metrics_written`` still counts both. Projected ids key
    on the model's stable upstream identifier (the ``metastore_id`` the
    Keboola adapter carries) so the collision cannot arise in the first
    place."""

    def test_like_named_models_with_stable_ids_do_not_overwrite_each_other(self, system_db):
        from src.repositories import metric_repo

        document = {
            "semantic_model": [
                _model_with_metric("core", "revenue", metastore_id="uuid-a"),
                _model_with_metric("core", "orders_count", metastore_id="uuid-b"),
            ]
        }
        report = project_document(document, source="keboola_metastore", source_ref="conn-a")

        assert report.metrics_written == 2
        assert report.skipped == []
        assert metric_repo().get("keboola_metastore/conn-a/uuid-a/revenue") is not None
        assert metric_repo().get("keboola_metastore/conn-a/uuid-b/orders_count") is not None
        # Neither was mistaken for the other's stale row.
        assert report.metrics_pruned == 0

    def test_the_display_name_still_rides_along_as_the_category(self, system_db):
        from src.repositories import metric_repo

        document = {"semantic_model": [_model_with_metric("core", "revenue", metastore_id="uuid-a")]}
        project_document(document, source="keboola_metastore", source_ref="conn-a")

        assert metric_repo().get("keboola_metastore/conn-a/uuid-a/revenue")["category"] == "core"


class TestPartialProjection:
    """``partial`` says the input is an incomplete picture of this (source,
    source_ref) — a model that belongs to it was dropped before the call (its
    composed document failed validation). Pruning at full scope then deletes
    that model's previously-written rows on the strength of a partial read;
    narrowing the prune to the models actually carried keeps reconciliation
    working for the models that ARE here."""

    def test_partial_projection_spares_a_model_absent_from_this_call(self, system_db):
        from src.repositories import metric_repo

        complete = {
            "semantic_model": [
                _model_with_metric("core", "revenue", metastore_id="uuid-a"),
                _model_with_metric("other", "orders_count", metastore_id="uuid-b"),
            ]
        }
        project_document(complete, source="keboola_metastore", source_ref="conn-a")
        assert metric_repo().get("keboola_metastore/conn-a/uuid-b/orders_count") is not None

        partial = {"semantic_model": [_model_with_metric("core", "revenue", metastore_id="uuid-a")]}
        report = project_document(partial, source="keboola_metastore", source_ref="conn-a", partial=True)

        assert report.metrics_pruned == 0
        assert metric_repo().get("keboola_metastore/conn-a/uuid-b/orders_count") is not None

    def test_a_surviving_model_is_still_reconciled_in_a_partial_pass(self, system_db):
        """Narrowing, not skipping: a model present in this call that really
        did lose a metric upstream is still pruned."""
        from src.repositories import metric_repo

        two_metrics = {
            "semantic_model": [
                {
                    **_model_with_metric("core", "revenue", metastore_id="uuid-a"),
                    "metrics": [
                        {
                            "name": "revenue",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(revenue)"}]},
                        },
                        {
                            "name": "refunds",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(refunds)"}]},
                        },
                    ],
                },
                _model_with_metric("other", "orders_count", metastore_id="uuid-b"),
            ]
        }
        project_document(two_metrics, source="keboola_metastore", source_ref="conn-a")

        partial = {"semantic_model": [_model_with_metric("core", "revenue", metastore_id="uuid-a")]}
        report = project_document(partial, source="keboola_metastore", source_ref="conn-a", partial=True)

        assert report.metrics_pruned == 1
        assert metric_repo().get("keboola_metastore/conn-a/uuid-a/refunds") is None
        assert metric_repo().get("keboola_metastore/conn-a/uuid-b/orders_count") is not None

    def test_a_complete_projection_still_reclaims_a_model_deleted_upstream(self, system_db):
        """The narrowing is opt-in for exactly this reason: the default full
        scope is what removes a model upstream really did delete."""
        from src.repositories import metric_repo

        complete = {
            "semantic_model": [
                _model_with_metric("core", "revenue", metastore_id="uuid-a"),
                _model_with_metric("other", "orders_count", metastore_id="uuid-b"),
            ]
        }
        project_document(complete, source="keboola_metastore", source_ref="conn-a")

        shrunk = {"semantic_model": [_model_with_metric("core", "revenue", metastore_id="uuid-a")]}
        report = project_document(shrunk, source="keboola_metastore", source_ref="conn-a")

        assert report.metrics_pruned == 1
        assert metric_repo().get("keboola_metastore/conn-a/uuid-b/orders_count") is None

    def test_partial_projection_spares_a_models_glossary_terms_too(self, system_db):
        from src.repositories import glossary_repo

        def _with_glossary(name: str, term: str, metastore_id: str) -> dict:
            model = _model_with_metric(name, "revenue", metastore_id=metastore_id)
            model["custom_extensions"] = [
                {
                    "vendor_name": "AGNES",
                    "data": json.dumps({"metastore_id": metastore_id, "glossary": [{"term": term, "definition": "d"}]}),
                }
            ]
            return model

        complete = {
            "semantic_model": [
                _with_glossary("core", "MRR", "uuid-a"),
                _with_glossary("other", "Churn", "uuid-b"),
            ]
        }
        project_document(complete, source="keboola_metastore", source_ref="conn-a")
        assert glossary_repo().get("keboola_metastore/conn-a/uuid-b/churn") is not None

        partial = {"semantic_model": [_with_glossary("core", "MRR", "uuid-a")]}
        report = project_document(partial, source="keboola_metastore", source_ref="conn-a", partial=True)

        assert report.glossary_pruned == 0
        assert glossary_repo().get("keboola_metastore/conn-a/uuid-b/churn") is not None
