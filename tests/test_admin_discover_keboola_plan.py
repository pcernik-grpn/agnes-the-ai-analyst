"""Pure-function tests for the Keboola auto-discovery planner.

The planner walks the discovered table list and the live registry,
classifying each entry as ``new`` / ``existing_match`` /
``existing_drift`` / ``invalid`` so the caller can decide whether
to write. The endpoint and CLI compose on top of this; the planner
itself touches no external services.

Two real-world incidents drove this split:

  - kbc_job arrived with the wrong bucket from a manual registration
    (``in.c-keboola-storage`` instead of ``in.c-kbc_telemetry``); a
    naive auto-discover re-run would have overwritten the admin's
    correction. The planner now classifies that as ``existing_drift``
    and the writer skips it, surfacing the divergence in the response.

  - Earlier auto-discover bug stripped the stage prefix off bucket ids
    (e.g. ``c-finance`` instead of ``in.c-finance``), inserting 137
    rows whose Storage API export-async calls all 404'd. The planner
    now uses the Keboola API's authoritative ``bucket_id`` field
    directly, falling back to id-string parsing only when the field
    isn't present.
"""

from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from app.api.admin import _build_keboola_discovery_plan, _split_keboola_table_id


# ---- _split_keboola_table_id (id parser fallback) --------------------------


class TestSplitKeboolaTableId:
    def test_three_segment_canonical(self):
        # in.c-finance.orders → bucket=in.c-finance, table=orders
        assert _split_keboola_table_id("in.c-finance.orders") == (
            "in.c-finance",
            "orders",
        )

    def test_three_segment_with_dotted_table_name(self):
        # Bucket-id always c-<word>; treat anything trailing as table.
        # 4-segment id → bucket = first three joined, table = last.
        assert _split_keboola_table_id("in.c-x.foo.bar") == (
            "in.c-x.foo",
            "bar",
        )

    def test_two_segment_no_stage(self):
        # Defensive: id missing the stage prefix → use what we have.
        assert _split_keboola_table_id("c-foo.events") == (
            "c-foo",
            "events",
        )

    def test_one_segment_falls_back_to_name(self):
        bucket, table = _split_keboola_table_id("orphan", fallback_name="orphan_t")
        assert bucket == ""
        assert table == "orphan_t"

    def test_empty_string_safe(self):
        bucket, table = _split_keboola_table_id("", fallback_name="x")
        assert bucket == ""
        assert table == "x"


# ---- _build_keboola_discovery_plan -----------------------------------------


def _make_repo(rows: dict[str, dict]):
    """Build a stub repo whose `.get(table_id)` returns the row in `rows`
    (or None). Mirror of TableRegistryRepository's lookup-by-id surface."""
    repo = MagicMock()
    repo.get.side_effect = lambda tid: rows.get(tid)
    return repo


@pytest.fixture(autouse=True)
def stub_table_registry(monkeypatch):
    """The planner instantiates `TableRegistryRepository(conn)` itself.
    Patch the class to return whatever fixture-provided repo we set up
    via ``request.param``-style indirection — but here a simpler
    pattern: per-test setup attaches the rows dict to a module-level
    cache that the fake reads."""
    state = {"rows": {}}

    class _FakeRepo:
        def __init__(self, conn):
            pass

        def get(self, tid):
            return state["rows"].get(tid)

        def list_all(self):
            # The planner uses list_all() once at the top to build the
            # name→row index for collision detection. Stamp `source_type`
            # on every row so the planner's keboola filter accepts them.
            return [{**v, "id": k, "source_type": v.get("source_type", "keboola")} for k, v in state["rows"].items()]

    # Patch the factory in src.repositories — the api module imports it
    # by name (``table_registry_repo``) and calls it with no args, so a
    # lambda returning a fresh _FakeRepo wires the stub into both
    # callsites (DuckDB and PG modes).
    fake_instance = _FakeRepo(None)
    monkeypatch.setattr("app.api.admin.table_registry_repo", lambda: fake_instance)
    return state


def test_plan_buckets_new_existing_match_drift_and_invalid(stub_table_registry):
    """Single test exercising the four buckets at once — easier to read
    than four separate tests; failures here surface the exact bucket
    that misclassified.

    Drift here is the **same-id, different-coords** flavour: registry
    has a row at id `kbc_organization` with stale bucket; discovery
    would write a different bucket under the same id. (The
    name-collision flavour gets its own test below.)"""
    stub_table_registry["rows"] = {
        # existing_match: registry agrees with discovery
        "in_c-sales_orders": {
            "name": "orders",
            "bucket": "in.c-sales",
            "source_table": "orders",
        },
        # existing_drift (same id): admin migrated bucket post-registration
        "in_c-kbc_telemetry_kbc_organization": {
            "name": "kbc_organization",
            "bucket": "in.c-OLD-bucket",
            "source_table": "kbc_organization",
        },
    }
    discovered = [
        # new
        {"id": "in.c-sales.invoices", "name": "invoices", "bucket_id": "in.c-sales"},
        # existing_match
        {"id": "in.c-sales.orders", "name": "orders", "bucket_id": "in.c-sales"},
        # existing_drift (same id, different bucket)
        {"id": "in.c-kbc_telemetry.kbc_organization", "name": "kbc_organization", "bucket_id": "in.c-kbc_telemetry"},
        # invalid — empty id
        {"id": "", "name": "broken", "bucket_id": ""},
    ]

    plan = _build_keboola_discovery_plan(MagicMock(), discovered)

    assert [e["table_id"] for e in plan["new"]] == ["in_c-sales_invoices"]
    assert [e["table_id"] for e in plan["existing_match"]] == ["in_c-sales_orders"]
    assert len(plan["existing_drift"]) == 1
    drift = plan["existing_drift"][0]
    assert drift["drift_kind"] == "same_id_diff_coords"
    assert drift["table_id"] == "in_c-kbc_telemetry_kbc_organization"
    assert drift["bucket"] == "in.c-kbc_telemetry"
    assert drift["registry_bucket"] == "in.c-OLD-bucket"
    assert len(plan["invalid"]) == 1
    assert plan["invalid"][0]["full_id"] == ""


def test_plan_drift_via_name_collision_kbc_job_real_world(stub_table_registry):
    """Real-world incident: kbc_job was registered manually as
    ``id='kbc_job', name='kbc_job', bucket='in.c-kbc_telemetry'``;
    Keboola's auto-discovery exposes the same logical table at
    ``id='in.c-keboola-storage.job', name='job'``. Without
    name-collision detection, the planner would have classified the
    discovered row as `new` and inserted a duplicate whose Storage
    API export-async 404s.

    With the fix, planner detects the **discovered.name == registry.name**
    (case-insensitive) collision, classifies as drift, surfaces the
    `registry_id` so an operator can reconcile."""
    stub_table_registry["rows"] = {
        "kbc_job": {
            "name": "kbc_job",
            "bucket": "in.c-kbc_telemetry",
            "source_table": "kbc_job",
        },
    }
    discovered = [
        {"id": "in.c-keboola-storage.kbc_job", "name": "kbc_job", "bucket_id": "in.c-keboola-storage"},
    ]
    plan = _build_keboola_discovery_plan(MagicMock(), discovered)

    assert plan["new"] == [], (
        "duplicate kbc_job must NOT be in new bucket — would 404 at next sync and clobber operator alerting"
    )
    assert len(plan["existing_drift"]) == 1
    drift = plan["existing_drift"][0]
    assert drift["drift_kind"] == "name_collision"
    assert drift["table_id"] == "in_c-keboola-storage_kbc_job"
    assert drift["registry_id"] == "kbc_job"
    assert drift["bucket"] == "in.c-keboola-storage"
    assert drift["registry_bucket"] == "in.c-kbc_telemetry"


def test_plan_prefers_api_bucket_id_over_id_parse(stub_table_registry):
    """Authoritative source for bucket is the API's `bucket_id` field
    (when present). Pre-fix, the parser stripped the stage prefix and
    inserted 137 broken rows — using `bucket_id` directly avoids that
    class of bug entirely."""
    discovered = [
        # bucket_id explicit + present, full id agrees: trivial
        {"id": "in.c-x.t1", "name": "t1", "bucket_id": "in.c-x"},
        # bucket_id present, full id messy / unreliable — bucket_id wins
        {"id": "weird-id-without-dots", "name": "t2", "bucket_id": "in.c-y"},
    ]
    plan = _build_keboola_discovery_plan(MagicMock(), discovered)
    by_id = {e["table_id"]: e for e in plan["new"]}
    assert by_id["in_c-x_t1"]["bucket"] == "in.c-x"
    assert by_id["weird-id-without-dots"]["bucket"] == "in.c-y"
    assert by_id["weird-id-without-dots"]["source_table"] == "t2"


def test_plan_falls_back_to_parser_when_bucket_id_missing(stub_table_registry):
    """Older Keboola SDK fallback path doesn't return `bucket_id`.
    Plan must still produce a usable bucket via the id parser."""
    discovered = [
        {"id": "in.c-z.events", "name": "events"},  # no bucket_id field
    ]
    plan = _build_keboola_discovery_plan(MagicMock(), discovered)
    assert plan["new"][0]["bucket"] == "in.c-z"
    assert plan["new"][0]["source_table"] == "events"


def test_plan_drift_skips_overwrite(stub_table_registry):
    """The plan classifies drift; the writer's contract (separate test
    in test_admin_configure_api.py against the endpoint) is to NOT
    overwrite. Verify here that drifted rows are NOT in the `new`
    bucket (which is the only bucket the writer iterates)."""
    stub_table_registry["rows"] = {
        "in_c-sales_orders": {
            "bucket": "in.c-OLD",
            "source_table": "orders_renamed",
        },
    }
    discovered = [
        {"id": "in.c-sales.orders", "name": "orders", "bucket_id": "in.c-sales"},
    ]
    plan = _build_keboola_discovery_plan(MagicMock(), discovered)
    assert plan["new"] == []
    assert len(plan["existing_drift"]) == 1


# ---- §3.2 physical-source twin of a policied table -------------------------


class TestPlanRefusesAPoliciedTablesPhysicalSourceTwin:
    """Bulk auto-discovery writes ``query_mode='materialized'`` rows with no
    ``server_only`` — the distributable shape ``agnes pull`` downloads — and
    never called the §3.2 interlock at all. The plan builder only skipped a
    source when a row already existed under the derived ``table_id`` or the
    same ``name``, so a policied row that had been RENAMED (or registered
    under a different id) was not matched: discovery happily inserted a twin
    of it, and the next sync tick wrote the raw, unfiltered rows to parquet.
    """

    def test_a_twin_of_a_policied_row_is_classified_invalid_not_new(self, stub_table_registry):
        stub_table_registry["rows"] = {
            # The policied row, under an id/name discovery will not match:
            # renamed by the admin, same physical source.
            "finance_invoices_governed": {
                "name": "invoices_governed",
                "bucket": "in.c-finance",
                "source_table": "invoices",
                "query_mode": "local",
                "server_only": True,
                "access_policy_sql": "SELECT * FROM invoices_governed",
            },
        }
        discovered = [
            {"id": "in.c-finance.invoices", "name": "invoices", "bucket_id": "in.c-finance"},
            {"id": "in.c-finance.products", "name": "products", "bucket_id": "in.c-finance"},
        ]

        plan = _build_keboola_discovery_plan(MagicMock(), discovered)

        assert [e["table_id"] for e in plan["new"]] == ["in_c-finance_products"], (
            "the policied table's physical-source twin must never reach the writer's `new` bucket"
        )
        assert len(plan["invalid"]) == 1
        offender = plan["invalid"][0]
        assert offender["table_id"] == "in_c-finance_invoices"
        assert "access_policy_physical_source_conflict" in offender["reason"]
        assert "finance_invoices_governed" in offender["reason"], (
            "the operator must be told WHICH policied row blocked it"
        )
        # The escape it names must be one that still works. It used to say
        # "register it by hand with server_only=true", which the §3.2 twin
        # interlock now refuses outright: distributability stopped deciding
        # whether the check fires, so a hand-registered server_only twin is
        # rejected exactly like a distributable one.
        assert "server_only=true" not in offender["reason"], offender["reason"]

    def test_a_source_matching_no_policied_row_is_unaffected(self, stub_table_registry):
        stub_table_registry["rows"] = {
            "finance_invoices_governed": {
                "name": "invoices_governed",
                "bucket": "in.c-finance",
                "source_table": "invoices",
                "query_mode": "local",
                "server_only": True,
                "access_policy_sql": "SELECT * FROM invoices_governed",
            },
        }
        discovered = [
            {"id": "in.c-sales.invoices", "name": "invoices_sales", "bucket_id": "in.c-sales"},
        ]

        plan = _build_keboola_discovery_plan(MagicMock(), discovered)

        assert [e["table_id"] for e in plan["new"]] == ["in_c-sales_invoices"]
        assert plan["invalid"] == []

    def test_an_unpolicied_registry_row_does_not_block_discovery(self, stub_table_registry):
        """The conflict is about a POLICY being routed around, not about
        two rows sharing a source."""
        stub_table_registry["rows"] = {
            "finance_invoices_copy": {
                "name": "invoices_copy",
                "bucket": "in.c-finance",
                "source_table": "invoices",
                "query_mode": "local",
            },
        }
        discovered = [
            {"id": "in.c-finance.invoices", "name": "invoices", "bucket_id": "in.c-finance"},
        ]

        plan = _build_keboola_discovery_plan(MagicMock(), discovered)

        assert [e["table_id"] for e in plan["new"]] == ["in_c-finance_invoices"]
        assert plan["invalid"] == []
