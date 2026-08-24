"""`/api/v2/schema/{table_id}` must carry real per-column descriptions
instead of the hardcoded ``""`` every source-type branch used to return.

Precedence: admin-authored ``column_metadata`` (the key shape
``app/api/metadata.py`` writes, ``(table_id, column_name)``) wins over an
Ossie dataset field description bound to the same ``table_id``; a column
with neither gets ``""``.

B3 (remediation program) acceptance test. Must fail (every description
``""``) against the unfixed ``app/api/v2_schema.py``.
"""

from __future__ import annotations

from pathlib import Path


def _seed_parquet(tmp_path: Path, table_id: str) -> None:
    import duckdb

    parquet_dir = tmp_path / "extracts" / "local" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(":memory:")
    conn.execute(
        f"COPY (SELECT 1 AS col_a, 2 AS col_b, 3 AS col_c) "
        f"TO '{parquet_dir / (table_id + '.parquet')}' (FORMAT PARQUET)"
    )
    conn.close()


def _upsert_bound_model(*, id: str, slug: str, table_id: str, field_name: str, description: str) -> None:
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="# native fixture, not schema-authored",
        document_json={
            "semantic_model": [
                {
                    "name": slug,
                    "datasets": [
                        {
                            "name": f"{slug}_ds",
                            "source": table_id,
                            "fields": [{"name": field_name, "description": description}],
                        }
                    ],
                }
            ]
        },
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


def _build(table_id: str, *, name: str | None = None) -> dict:
    from app.api.v2_schema import build_schema_uncached

    fake_row = {"id": table_id, "name": name or table_id, "source_type": "local", "query_mode": "local"}
    return build_schema_uncached(conn=None, table_id=table_id, bq=object(), row=fake_row)


class TestColumnDescriptions:
    def test_column_metadata_description_is_carried(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        from src.repositories import column_metadata_repo

        column_metadata_repo().save(table_id="orders", column_name="col_a", description="Column A, admin-authored")

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_a"] == "Column A, admin-authored"

    def test_ossie_field_description_is_carried_when_no_column_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        _upsert_bound_model(
            id="manual/_/retail",
            slug="retail",
            table_id="orders",
            field_name="col_b",
            description="Column B, from the semantic layer",
        )

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_b"] == "Column B, from the semantic layer"

    def test_column_with_neither_source_is_blank(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_c"] == ""

    def test_column_metadata_takes_precedence_over_ossie(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        _upsert_bound_model(
            id="manual/_/retail",
            slug="retail",
            table_id="orders",
            field_name="col_a",
            description="Ossie description — must lose",
        )
        from src.repositories import column_metadata_repo

        column_metadata_repo().save(table_id="orders", column_name="col_a", description="Admin description — must win")

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_a"] == "Admin description — must win"
