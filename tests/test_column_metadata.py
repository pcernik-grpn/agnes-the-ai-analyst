"""Tests for ColumnMetadataRepository."""

import json
import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


@pytest.fixture
def repo(db_conn):
    from src.repositories.column_metadata import ColumnMetadataRepository
    return ColumnMetadataRepository(db_conn)


class TestColumnMetadataCreate:
    def test_save_single_column(self, repo):
        result = repo.save("orders", "id", basetype="STRING", description="Order ID")
        assert result["table_id"] == "orders"
        assert result["column_name"] == "id"
        assert result["basetype"] == "STRING"
        assert result["description"] == "Order ID"
        assert result["confidence"] == "manual"
        assert result["source"] == "manual"

    def test_upsert_overwrites(self, repo):
        repo.save("orders", "id", basetype="STRING", description="Old")
        result = repo.save("orders", "id", basetype="INTEGER", description="New", confidence="high")
        assert result["basetype"] == "INTEGER"
        assert result["description"] == "New"
        assert result["confidence"] == "high"
        # Should still be only one row
        rows = repo.list_for_table("orders")
        assert len(rows) == 1

    def test_save_accepts_but_does_not_persist_source_ref(self, repo):
        """``source_ref`` is Postgres-only (A3 PG-first ratchet, see
        ``src/repositories/column_metadata.py::save``'s docstring): the
        DuckDB app-state schema is frozen and never gained this column, so
        it has no key in the returned record at all (not even ``None``).
        ``save()`` still accepts the kwarg for signature parity with the PG
        repo, it just never lands anywhere."""
        result = repo.save(
            "orders",
            "region",
            basetype="STRING",
            source="databricks_metrics",
            source_ref="dbc-test.cloud.databricks.com",
        )
        assert "source_ref" not in result
        assert "source_ref" not in repo.get("orders", "region")


class TestColumnMetadataRead:
    def test_list_for_table_filters_by_table(self, repo):
        repo.save("orders", "id", basetype="STRING")
        repo.save("orders", "total", basetype="NUMERIC")
        repo.save("orders", "status", basetype="STRING")
        repo.save("customers", "email", basetype="STRING")

        orders_cols = repo.list_for_table("orders")
        assert len(orders_cols) == 3
        assert all(c["table_id"] == "orders" for c in orders_cols)

        customer_cols = repo.list_for_table("customers")
        assert len(customer_cols) == 1
        assert customer_cols[0]["column_name"] == "email"

    def test_list_for_table_ordered_by_column_name(self, repo):
        repo.save("orders", "total", basetype="NUMERIC")
        repo.save("orders", "id", basetype="STRING")
        repo.save("orders", "status", basetype="STRING")

        cols = repo.list_for_table("orders")
        names = [c["column_name"] for c in cols]
        assert names == sorted(names)

    def test_get_missing_returns_none(self, repo):
        result = repo.get("orders", "nonexistent")
        assert result is None

    def test_list_all_returns_rows_across_every_table(self, repo):
        """Unlike ``list_for_table``, ``list_all`` is not scoped to one
        ``table_id`` — needed to find rows whose ``table_id`` no longer
        names a live ``table_registry`` row at all (Block 5 of #1707), which
        cannot be looked up by the very id that is missing."""
        repo.save("orders", "id", basetype="STRING")
        repo.save("customers", "email", basetype="STRING")

        rows = repo.list_all()
        assert {(r["table_id"], r["column_name"]) for r in rows} == {
            ("orders", "id"),
            ("customers", "email"),
        }

    def test_list_all_empty_instance_returns_empty_list(self, repo):
        assert repo.list_all() == []


class TestColumnMetadataDelete:
    def test_delete_column(self, repo):
        repo.save("orders", "id", basetype="STRING")
        deleted = repo.delete("orders", "id")
        assert deleted is True
        assert repo.get("orders", "id") is None

    def test_delete_missing_returns_false(self, repo):
        result = repo.delete("orders", "does_not_exist")
        assert result is False


class TestColumnMetadataProposal:
    def test_import_proposal_count(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "Order ID", "confidence": "high"},
                        "total": {"basetype": "NUMERIC", "description": "Total amount"},
                    }
                },
                "customers": {
                    "columns": {
                        "email": {"basetype": "STRING", "description": "Customer email", "confidence": "medium"},
                    }
                },
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        count = repo.import_proposal(str(path))
        assert count == 3

    def test_import_proposal_data(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "Order ID", "confidence": "high"},
                    }
                }
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        repo.import_proposal(str(path))

        result = repo.get("orders", "id")
        assert result is not None
        assert result["basetype"] == "STRING"
        assert result["description"] == "Order ID"
        assert result["confidence"] == "high"

    def test_import_sets_source_ai_enrichment(self, repo, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING"},
                    }
                }
            }
        }
        path = tmp_path / "proposal.json"
        path.write_text(json.dumps(proposal))

        repo.import_proposal(str(path))

        result = repo.get("orders", "id")
        assert result["source"] == "ai_enrichment"


class TestPushAddressesTheFullKeboolaTableId:
    """The Storage API addresses a table by `<bucket>.<table>`.

    The registry keeps the bucket in its own column, so `source_table` alone is a
    valid tableId only for a legacy wizard row that still carries the prefix. For
    a correctly registered row the push URL came out as
    `/v2/storage/tables/orders/columns/...` — no bucket — so it only ever worked
    on the rows that were themselves wrong. The exact inverse of the doubled-prefix
    bug this PR fixes (Devin Review on #1189).
    """

    def test_composition_includes_the_bucket(self):
        """Unit-level: the id handed to the Storage API must be `<bucket>.<table>`
        for a correctly registered row, and must not double for a legacy one."""
        from connectors.keboola.storage_api import normalize_source_table

        def compose(bucket, source_table):
            bare = normalize_source_table(bucket, source_table)
            return f"{bucket}.{bare}" if bucket else bare

        assert compose("in.c-main", "orders") == "in.c-main.orders"
        assert compose("in.c-main", "in.c-main.orders") == "in.c-main.orders"
        assert compose("", "orders") == "orders"

    def test_the_endpoint_source_uses_that_composition(self):
        """Guards against the composition drifting back to `source_table` alone —
        asserted on the source, since the endpoint builds the URL inline."""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "app" / "api" / "metadata.py").read_text()
        assert 'source_table = f"{_bucket}.{_bare}" if _bucket else _bare' in src
        assert 'source_table = table.get("source_table") or table_id' not in src, (
            "the bucket-less form addresses a tableId that does not exist for a "
            "correctly registered row"
        )
