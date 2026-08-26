"""Unit tests for `src/semantic/cache_render.py` (semantic-layer physical
cache, Fáze 1). Pure transformation over `semantic_models` row dicts — no
database, mirrors `tests/test_data_semantics_scaffold.py`'s posture.
"""

from __future__ import annotations

import json

import yaml

from src.semantic.cache_render import DEFAULT_TTL_SECONDS, render_semantic_cache

GENERATED_AT = "2026-08-26T12:00:00Z"


def _row(
    *,
    slug="retail",
    name="retail",
    description="Retail semantic model.",
    content_hash="abc123",
    source="manual",
    source_ref=None,
    models=None,
):
    if models is None:
        models = [
            {
                "name": name,
                "description": description,
                "datasets": [
                    {
                        "name": "orders",
                        "source": "db.public.orders",
                        "fields": [{"name": "region", "expression": {"dialects": []}}],
                    }
                ],
                "metrics": [
                    {
                        "name": "revenue",
                        "description": "Total revenue across all orders, no filters applied by default.",
                        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                    }
                ],
            }
        ]
    return {
        "slug": slug,
        "name": name,
        "description": description,
        "content_hash": content_hash,
        "source": source,
        "source_ref": source_ref,
        "document_json": {"semantic_model": models},
    }


def test_renders_brief_tables_and_metrics_for_one_model():
    files = render_semantic_cache([_row()], generated_at=GENERATED_AT)

    assert set(files) == {
        "retail/_brief.md",
        "retail/tables/orders.yml",
        "retail/metrics/revenue.yml",
    }
    assert "# retail" in files["retail/_brief.md"]
    assert "orders" in files["retail/_brief.md"]
    assert "revenue" in files["retail/_brief.md"]


def test_no_glossary_file_when_document_declares_no_terms():
    files = render_semantic_cache([_row()], generated_at=GENERATED_AT)
    assert "retail/glossary.md" not in files


def test_glossary_file_written_from_custom_extensions():
    models = [
        {
            "name": "retail",
            "datasets": [{"name": "orders", "source": "db.public.orders"}],
            "custom_extensions": [
                {
                    "vendor_name": "AGNES",
                    "data": json.dumps({"glossary": [{"term": "ARR", "definition": "Annual recurring revenue."}]}),
                }
            ],
        }
    ]
    files = render_semantic_cache([_row(models=models)], generated_at=GENERATED_AT)

    assert "retail/glossary.md" in files
    body = files["retail/glossary.md"]
    assert "## ARR" in body
    assert "Annual recurring revenue." in body


def test_header_carries_generated_at_content_hash_slug_and_ttl():
    files = render_semantic_cache([_row(content_hash="deadbeef")], generated_at=GENERATED_AT, ttl_seconds=3600)

    for relpath in ("retail/tables/orders.yml", "retail/metrics/revenue.yml"):
        text = files[relpath]
        assert f"generated_at: {GENERATED_AT}" in text
        assert "content_hash: deadbeef" in text
        assert "source_slug: retail" in text
        assert "ttl_seconds: 3600" in text

    brief = files["retail/_brief.md"]
    assert f"generated_at: {GENERATED_AT}" in brief
    assert "content_hash: deadbeef" in brief


def test_default_ttl_is_24_hours():
    assert DEFAULT_TTL_SECONDS == 24 * 60 * 60


def test_table_and_metric_files_are_valid_yaml_round_tripping_the_document():
    files = render_semantic_cache([_row()], generated_at=GENERATED_AT)

    table = yaml.safe_load(files["retail/tables/orders.yml"])
    assert table["name"] == "orders"
    assert table["source"] == "db.public.orders"

    metric = yaml.safe_load(files["retail/metrics/revenue.yml"])
    assert metric["name"] == "revenue"
    assert metric["expression"]["dialects"][0]["expression"] == "SUM(amount)"


def test_multiple_rows_render_into_separate_slug_directories():
    files = render_semantic_cache(
        [_row(slug="retail", name="retail"), _row(slug="finance", name="finance", content_hash="h2")],
        generated_at=GENERATED_AT,
    )
    assert "retail/_brief.md" in files
    assert "finance/_brief.md" in files


def test_row_with_no_slug_is_skipped():
    row = _row()
    row["slug"] = ""
    assert render_semantic_cache([row], generated_at=GENERATED_AT) == {}


def test_row_with_no_document_json_still_renders_an_empty_brief_not_raised():
    """The caller (``_accessible_valid_rows``) already filters out rows with
    no ``document_json`` before this module ever sees them — but the
    renderer itself must degrade gracefully rather than raise, matching the
    never-guess/never-crash posture of the rest of the document-reading
    surface (`src/semantic_context.py`)."""
    row = _row()
    row["document_json"] = None
    files = render_semantic_cache([row], generated_at=GENERATED_AT)
    assert set(files) == {"retail/_brief.md"}
    assert "No datasets declared." in files["retail/_brief.md"]
    assert "No metrics declared." in files["retail/_brief.md"]


def test_empty_rows_list_renders_nothing():
    assert render_semantic_cache([], generated_at=GENERATED_AT) == {}
    assert render_semantic_cache(None, generated_at=GENERATED_AT) == {}
