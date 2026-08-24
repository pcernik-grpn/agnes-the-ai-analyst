"""DELETE /api/admin/semantic-models must prune the flat projection rows
(metric_definitions, glossary_terms, column_metadata) it previously wrote —
otherwise, now that POST/PUT project (see
tests/test_semantic_projection_on_write.py), a delete leaves them orphaned
forever.

Review finding on PR #1528 (B3, remediation program). Must fail (orphaned
rows survive the delete) against the unfixed DELETE handler.
"""

from __future__ import annotations

import json as jsonlib


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _doc(slug: str, metric_names: list[str]) -> str:
    metrics = "\n".join(
        f"      - name: {name}\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        f"              expression: SUM({name})\n"
        for name in metric_names
    )
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "        fields: []\n"
        "    metrics:\n" + metrics
    )


def _doc_with_glossary(slug: str, metric_names: list[str], glossary_term: str) -> str:
    metrics = "\n".join(
        f"      - name: {name}\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        f"              expression: SUM({name})\n"
        for name in metric_names
    )
    ext_data = jsonlib.dumps({"glossary": [{"term": glossary_term, "definition": "a definition"}]})
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "        fields: []\n"
        "    metrics:\n" + metrics + "    custom_extensions:\n"
        "      - vendor_name: agnes\n"
        f"        data: {jsonlib.dumps(ext_data)}\n"
    )


class TestProjectionOnDelete:
    def test_delete_prunes_projected_rows_and_spares_a_sibling_model(self, seeded_app):
        c = seeded_app["client"]

        # Sibling manual model whose own projections must survive the
        # delete below untouched — every source='manual' row shares one
        # provenance tuple, so an unscoped prune would sweep it up too.
        r_other = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("finance", ["arr"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r_other.status_code == 201, r_other.text

        created = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_glossary("retail", ["revenue", "order_count"], "retail_glossary_term")},
            headers=_auth(seeded_app["admin_token"]),
        ).json()

        from src.repositories import glossary_repo, metric_repo

        # Preconditions: everything landed before we delete it.
        assert metric_repo().get("manual/_/retail/revenue") is not None
        assert metric_repo().get("manual/_/retail/order_count") is not None
        glossary_before = [g for g in glossary_repo().list(limit=1000) if g["id"].startswith("manual/_/retail/")]
        assert glossary_before, "glossary term was never projected — precondition broken"
        assert metric_repo().get("manual/_/finance/arr") is not None

        r = c.delete(
            f"/api/admin/semantic-models/{created['id']}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 204, r.text

        assert metric_repo().get("manual/_/retail/revenue") is None, "metric survived the delete — orphaned row"
        assert metric_repo().get("manual/_/retail/order_count") is None, "metric survived the delete — orphaned row"
        glossary_after = [g for g in glossary_repo().list(limit=1000) if g["id"].startswith("manual/_/retail/")]
        assert glossary_after == [], "glossary term survived the delete — orphaned row"

        # Sibling model's own projections must be untouched.
        assert metric_repo().get("manual/_/finance/arr") is not None, "an unrelated manual model's metric was pruned"

    def test_delete_of_a_model_never_projected_is_a_no_op_not_an_error(self, seeded_app):
        """A source-owned (git/upload) row's `document_json` may exist but
        was never projected by THIS API — the guard on `document_json`
        truthiness must not raise on a model with no datasets/metrics at
        all, and deleting must still succeed."""
        from src.repositories import semantic_model_repo

        row = semantic_model_repo().upsert(
            id="manual/_/empty_model",
            slug="empty_model",
            name="empty_model",
            description=None,
            document="version: '0.2.0.dev0'\nsemantic_model:\n  - name: empty_model\n",
            document_json={"semantic_model": [{"name": "empty_model"}]},
            spec_version="0.2.0.dev0",
            content_hash="hash-empty",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=None,
        )
        c = seeded_app["client"]
        r = c.delete(f"/api/admin/semantic-models/{row['id']}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text
