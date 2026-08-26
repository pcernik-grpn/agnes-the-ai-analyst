"""Cross-engine behavioural contract for the repo methods ported to PG in
the #499/#513 drift-fix sweep.

``test_repo_method_parity.py`` guarantees these methods *exist* on both
backends with matching signatures (static check). This file guarantees they
*behave* identically — the same calls produce the same observable state on
DuckDB and Postgres. Each test is parametrised over both engines.

Covered:
  - ``MetricRepository.import_from_yaml`` / ``export_to_yaml``  (shared mixin)
  - ``ColumnMetadataRepository.import_proposal``                (shared mixin)
  - ``UsageRepository.emit_server_event``                       (PG port)
  - ``TableRegistryRepository.set_description``                 (PG port)
  - ``MarketplacePluginsRepository.get``                        (PG port)
  - ``StoreSubmissionsRepository.set_inline_result``            (PG port)
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Ctx:
    """Backend-bound factory for the repos under test + a raw scalar reader."""

    def __init__(self, backend, conn=None, engine=None):
        self.backend = backend
        self._conn = conn
        self._engine = engine

    def metrics(self):
        if self.backend == "duckdb":
            from src.repositories.metrics import MetricRepository
            return MetricRepository(self._conn)
        from src.repositories.metrics_pg import MetricPgRepository
        return MetricPgRepository(self._engine)

    def column_metadata(self):
        if self.backend == "duckdb":
            from src.repositories.column_metadata import ColumnMetadataRepository
            return ColumnMetadataRepository(self._conn)
        from src.repositories.column_metadata_pg import ColumnMetadataPgRepository
        return ColumnMetadataPgRepository(self._engine)

    def usage(self):
        if self.backend == "duckdb":
            from src.repositories.usage import UsageRepository
            return UsageRepository(self._conn)
        from src.repositories.usage_pg import UsagePgRepository
        return UsagePgRepository(self._engine)

    def table_registry(self):
        if self.backend == "duckdb":
            from src.repositories.table_registry import TableRegistryRepository
            return TableRegistryRepository(self._conn)
        from src.repositories.table_registry_pg import TableRegistryPgRepository
        return TableRegistryPgRepository(self._engine)

    def marketplace_plugins(self):
        if self.backend == "duckdb":
            from src.repositories.marketplace_plugins import MarketplacePluginsRepository
            return MarketplacePluginsRepository(self._conn)
        from src.repositories.marketplace_plugins_pg import MarketplacePluginsPgRepository
        return MarketplacePluginsPgRepository(self._engine)

    def store_submissions(self):
        if self.backend == "duckdb":
            from src.repositories.store_submissions import StoreSubmissionsRepository
            return StoreSubmissionsRepository(self._conn)
        from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository
        return StoreSubmissionsPgRepository(self._engine)

    def scalar(self, sql: str, **params):
        """Run a scalar query written with ``:name`` placeholders against
        either backend. DuckDB gets the named tokens rewritten to ``?`` in
        order of appearance; PG runs the named SQL as-is via SQLAlchemy."""
        if self.backend == "duckdb":
            import re

            order = re.findall(r":(\w+)", sql)
            duck_sql = re.sub(r":\w+", "?", sql)
            args = [params[name] for name in order]
            return self._conn.execute(duck_sql, args).fetchone()[0]
        with self._engine.connect() as c:
            return c.execute(sa.text(sql), params).scalar()


@pytest.fixture(params=["duckdb", "pg"])
def ctx(request, tmp_path, pg_engine, monkeypatch):
    backend = request.param
    if backend == "duckdb":
        from src.db import _ensure_schema

        conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
        _ensure_schema(conn)
        yield _Ctx("duckdb", conn=conn)
        conn.close()
    else:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
        command.upgrade(cfg, "head")

        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
        import src.db_pg as db_pg
        db_pg.dispose()
        db_pg.get_engine()

        yield _Ctx("pg", engine=db_pg.get_engine())


# ---------------------------------------------------------------------------
# metrics — import_from_yaml / export_to_yaml (shared MetricYamlMixin)
# ---------------------------------------------------------------------------

def test_metrics_yaml_import_then_list(ctx, tmp_path):
    """`agnes admin metrics import <dir>` round-trip — the documented
    starter-pack path that AttributeError-crashed on PG before the port."""
    repo = ctx.metrics()
    yaml_dir = tmp_path / "metrics_in"
    (yaml_dir / "revenue").mkdir(parents=True)
    (yaml_dir / "revenue" / "mrr.yml").write_text(
        "name: mrr\n"
        "display_name: Monthly Recurring Revenue\n"
        "category: revenue\n"
        "type: sum\n"
        "sql: SELECT SUM(amount) FROM subs\n"
        "description: MRR across active subscriptions\n"
    )

    n = repo.import_from_yaml(yaml_dir)
    assert n == 1

    rows = repo.list()
    assert len(rows) == 1
    m = rows[0]
    assert m["id"] == "revenue/mrr"
    assert m["name"] == "mrr"
    assert m["category"] == "revenue"
    assert m["display_name"] == "Monthly Recurring Revenue"


def test_metrics_yaml_export_round_trip(ctx, tmp_path):
    repo = ctx.metrics()
    repo.create(
        id="revenue/arr",
        name="arr",
        display_name="Annual Recurring Revenue",
        category="revenue",
        sql="SELECT SUM(amount) * 12 FROM subs",
        description="ARR",
    )
    out = tmp_path / "metrics_out"
    n = repo.export_to_yaml(out)
    assert n == 1
    written = out / "revenue" / "arr.yml"
    assert written.exists()
    assert "name: arr" in written.read_text()


# ---------------------------------------------------------------------------
# column_metadata — import_proposal (shared ColumnMetadataImportMixin)
# ---------------------------------------------------------------------------

def test_column_metadata_import_proposal(ctx, tmp_path):
    repo = ctx.column_metadata()
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "tables": {
                    "orders": {
                        "columns": {
                            "id": {"basetype": "STRING", "description": "Order id", "confidence": "high"},
                            "total": {"basetype": "DOUBLE", "description": "Order total"},
                        }
                    }
                }
            }
        )
    )

    n = repo.import_proposal(str(proposal))
    assert n == 2

    cols = {c["column_name"]: c for c in repo.list_for_table("orders")}
    assert set(cols) == {"id", "total"}
    assert cols["id"]["description"] == "Order id"
    assert cols["id"]["source"] == "ai_enrichment"


def test_column_metadata_save_accepts_source_ref(ctx):
    """``source_ref`` (mirrors metric_definitions/glossary_terms) is a
    Postgres-only column (A3 PG-first ratchet, ``docs/migrations.md`` ->
    "Adding a PG-only feature") — the DuckDB app-state schema is frozen and
    never gained it. Both backends' ``save()`` accept the kwarg (signature
    parity, ``tests/db_pg/test_repo_method_parity.py``), but only Postgres
    persists it; DuckDB silently drops it rather than erroring, so a shared
    caller (``src/semantic/projection.py``) works unmodified against either
    backend."""
    repo = ctx.column_metadata()

    saved = repo.save(
        table_id="orders",
        column_name="region",
        basetype="STRING",
        description="Sales region",
        source="databricks_metrics",
        source_ref="dbc-test.cloud.databricks.com",
    )
    fetched = repo.get("orders", "region")
    listed = {c["column_name"]: c for c in repo.list_for_table("orders")}

    if ctx.backend == "pg":
        assert saved["source_ref"] == "dbc-test.cloud.databricks.com"
        assert fetched["source_ref"] == "dbc-test.cloud.databricks.com"
        assert listed["region"]["source_ref"] == "dbc-test.cloud.databricks.com"
    else:
        assert saved["source_ref"] is None
        assert fetched["source_ref"] is None
        assert listed["region"]["source_ref"] is None

    # Omitted entirely — stays NULL on both backends, same as every other
    # nullable provenance column on this repo (no implicit default).
    no_ref = repo.save(table_id="orders", column_name="amount", basetype="DECIMAL", source="manual")
    assert no_ref["source_ref"] is None


# ---------------------------------------------------------------------------
# usage — emit_server_event (PG port)
# ---------------------------------------------------------------------------

def test_usage_emit_server_event(ctx):
    repo = ctx.usage()
    event_id = repo.emit_server_event(
        event_type="data_package.view",
        user_id="u-1",
        username="alice@example.com",
        props={"slug": "orders", "source": "browse"},
    )
    assert event_id

    cnt = ctx.scalar(
        "SELECT COUNT(*) FROM usage_events WHERE id = :id AND source = 'server'",
        id=event_id,
    )
    assert cnt == 1
    etype = ctx.scalar(
        "SELECT event_type FROM usage_events WHERE id = :id", id=event_id
    )
    assert etype == "data_package.view"


def test_usage_emit_server_event_null_props(ctx):
    """props=None must not break the JSONB/cast path on either backend."""
    repo = ctx.usage()
    event_id = repo.emit_server_event(
        event_type="memory.dismiss", user_id=None, props=None
    )
    cnt = ctx.scalar(
        "SELECT COUNT(*) FROM usage_events WHERE id = :id", id=event_id
    )
    assert cnt == 1


# ---------------------------------------------------------------------------
# table_registry — set_description (PG port)
# ---------------------------------------------------------------------------

def test_table_registry_set_description_is_surgical(ctx):
    """set_description() updates only `description`, leaving other fields."""
    reg = ctx.table_registry()
    reg.register(
        id="orders",
        name="Orders",
        query_mode="local",
        source_type="keboola",
        bucket="in.c-main",
        description="old",
    )
    reg.set_description("orders", "new description")

    row = reg.get("orders")
    assert row["description"] == "new description"
    # Surgical: other fields survive the targeted update.
    assert row["name"] == "Orders"
    assert row["bucket"] == "in.c-main"
    assert row["query_mode"] == "local"


# ---------------------------------------------------------------------------
# marketplace_plugins — get(marketplace_id, name) (PG port)
# ---------------------------------------------------------------------------

def test_marketplace_plugins_get(ctx):
    """get() backs the curated install/uninstall existence + is_system checks.
    A raw DuckDB read 404'd every plugin on a PG instance (the reported bug)."""
    repo = ctx.marketplace_plugins()
    repo.replace_for_marketplace(
        "mkt-1",
        [{"name": "alpha", "description": "A"}, {"name": "beta", "description": "B"}],
    )

    row = repo.get("mkt-1", "alpha")
    assert row is not None
    assert row["name"] == "alpha"
    assert row["marketplace_id"] == "mkt-1"
    # is_system is surfaced (defaults FALSE on a freshly-synced plugin) — the
    # uninstall guard reads it.
    assert "is_system" in row
    assert bool(row["is_system"]) is False

    # Misses return None on both backends (the 404 path).
    assert repo.get("mkt-1", "missing") is None
    assert repo.get("other-mkt", "alpha") is None


# ---------------------------------------------------------------------------
# store_submissions — set_inline_result (PG port; was raw subs.conn.execute)
# ---------------------------------------------------------------------------

def test_store_submissions_set_inline_result(ctx):
    """Admin rescan writeback — replace inline_checks, clear llm_findings, set
    status, unconditionally (even over a terminal 'approved' row)."""
    repo = ctx.store_submissions()
    sub_id = repo.create(
        submitter_id="u1",
        submitter_email="a@example.com",
        type="skill",
        name="My Skill",
        version="v1",
        status="approved",  # a terminal state — rescan must still overwrite
        llm_findings={"verdict": "ok"},
    )

    repo.set_inline_result(
        sub_id,
        inline_checks={"passed": False, "findings": ["bad import"]},
        status="blocked_inline",
    )

    row = repo.get(sub_id)
    assert row["status"] == "blocked_inline"
    assert row["inline_checks"] == {"passed": False, "findings": ["bad import"]}
    assert row["llm_findings"] is None  # cleared

    # Invalid status rejected identically on both backends.
    with pytest.raises(ValueError):
        repo.set_inline_result(sub_id, inline_checks=None, status="bogus")


def test_metrics_yaml_reconcile_prunes_on_both_backends(ctx, tmp_path):
    """`--prune` reaches only the rows this importer wrote, and it must behave
    identically on DuckDB and Postgres — the delete path is the one place the
    mixin can destroy data, so parity here is not a formality."""
    repo = ctx.metrics()
    yaml_dir = tmp_path / "metrics_prune"
    (yaml_dir / "revenue").mkdir(parents=True)
    (yaml_dir / "revenue" / "mrr.yml").write_text("name: mrr\ncategory: revenue\nsql: SELECT 1\n")
    (yaml_dir / "revenue" / "arr.yml").write_text("name: arr\ncategory: revenue\nsql: SELECT 2\n")

    repo.reconcile_from_yaml(yaml_dir)
    repo.create(id="manual/nps", name="nps", display_name="NPS", category="manual", sql="SELECT 3", source="manual")

    (yaml_dir / "revenue" / "arr.yml").unlink()
    report = repo.reconcile_from_yaml(yaml_dir, prune=True)

    assert report["deleted"] == ["revenue/arr"]
    ids = {m["id"] for m in repo.list()}
    assert ids == {"revenue/mrr", "manual/nps"}, "prune reached outside its source scope"


def test_metrics_yaml_reconcile_dry_run_writes_nothing_on_both_backends(ctx, tmp_path):
    repo = ctx.metrics()
    yaml_dir = tmp_path / "metrics_dry"
    (yaml_dir / "revenue").mkdir(parents=True)
    (yaml_dir / "revenue" / "mrr.yml").write_text("name: mrr\ncategory: revenue\nsql: SELECT 1\n")

    report = repo.reconcile_from_yaml(yaml_dir, dry_run=True)
    assert report["added"] == ["revenue/mrr"]
    assert repo.list() == []
