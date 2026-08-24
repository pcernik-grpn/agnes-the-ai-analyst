"""A semantic model created (or replaced) through the admin API must project
into the flat tables (``metric_definitions``) queries actually read — the
same projection the source importer runs (``src/semantic/importer.py``), not
just a document stored and forgotten.

B3 (remediation program) acceptance test. Must fail (0 rows projected)
against the unfixed ``POST /api/admin/semantic-models`` handler.
"""

from __future__ import annotations


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


class TestProjectionOnWrite:
    def test_create_projects_metrics_with_manual_provenance(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("retail", ["revenue", "order_count"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text

        from src.repositories import metric_repo

        repo = metric_repo()
        revenue = repo.get("manual/_/retail/revenue")
        order_count = repo.get("manual/_/retail/order_count")
        assert revenue is not None, "metric was stored but never projected"
        assert order_count is not None
        assert revenue["source"] == "manual"
        assert revenue["source_ref"] in (None, "")
        assert revenue["sql"] == "SUM(revenue)"

    def test_replacing_the_document_prunes_dropped_metrics_scoped_to_this_model(self, seeded_app):
        """Re-POSTing the same slug with fewer metrics prunes the dropped
        one — but must NEVER touch a sibling manual model's own metrics,
        since every manual model shares (source='manual', source_ref=None)."""
        c = seeded_app["client"]

        # A second, unrelated manual model whose metric must survive the
        # first model's re-post untouched.
        r_other = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("finance", ["arr"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r_other.status_code == 201, r_other.text

        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("retail", ["revenue", "order_count"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text

        from src.repositories import metric_repo

        repo = metric_repo()
        assert repo.get("manual/_/retail/revenue") is not None
        assert repo.get("manual/_/retail/order_count") is not None

        # Replace with a single-metric document.
        r2 = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("retail", ["revenue"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r2.status_code == 201, r2.text

        assert repo.get("manual/_/retail/revenue") is not None
        assert repo.get("manual/_/retail/order_count") is None, "dropped metric must be pruned"
        assert repo.get("manual/_/finance/arr") is not None, "an unrelated manual model must survive untouched"

    def test_update_via_put_does_not_break_projection(self, seeded_app):
        """The PUT (name/description only) path must not regress the
        projected rows created at POST time."""
        c = seeded_app["client"]
        created = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc("retail", ["revenue"])},
            headers=_auth(seeded_app["admin_token"]),
        ).json()

        r = c.put(
            f"/api/admin/semantic-models/{created['id']}",
            json={"name": "Retail (renamed)"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        from src.repositories import metric_repo

        assert metric_repo().get("manual/_/retail/revenue") is not None
