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


def _doc_with_fields(slug: str, table_id: str, field_names: list[str]) -> str:
    """A valid document whose single dataset binds ``source: <table_id>`` —
    the collision shape: the raw dataset id under which the projection keys
    ``column_metadata`` is exactly the table id the admin metadata API also
    writes. Fields carry NO description (the blanking case)."""
    fields = "\n".join(
        f"          - name: {name}\n"
        "            expression:\n"
        "              dialects:\n"
        "                - dialect: ANSI_SQL\n"
        f"                  expression: {name}\n"
        for name in field_names
    )
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        f"      - name: {slug}_ds\n"
        f"        source: {table_id}\n"
        "        fields:\n" + fields
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

    def test_admin_authored_column_description_survives_manual_model_projection(self, seeded_app):
        """RED regression (Devin, PR #1528): the admin metadata API
        (`POST /api/admin/metadata/{table_id}`) writes `column_metadata` under
        `(table_id, column_name)` with `source='manual'` — the same key AND
        source the manual-model projection used to write, so projecting a
        model whose dataset binds to that table silently overwrote (and, via
        `_prune_columns`, deleted) admin-authored descriptions. The projection
        now writes under its own distinct source (`semantic_model`) and never
        touches a row another writer owns."""
        c = seeded_app["client"]

        # Admin authors a description through the metadata API path.
        r = c.post(
            "/api/admin/metadata/orders",
            json={"columns": [{"column_name": "amount", "basetype": "DECIMAL", "description": "Admin: order amount"}]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        # A manual model binds a dataset to the SAME table id, declaring the
        # same column (with no description — the blanking case) plus one of
        # its own.
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("retail", "orders", ["amount", "region"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        admin_row = repo.get("orders", "amount")
        assert admin_row is not None
        assert admin_row["description"] == "Admin: order amount", "admin-authored description must win"
        assert admin_row["source"] == "manual"

        model_row = repo.get("orders", "region")
        assert model_row is not None, "a column the admin never authored is still projected"
        assert model_row["source"] == "semantic_model"

        # Replacing the model with fewer fields prunes ONLY the projection's
        # own rows — the admin-authored one is out of the prune's source scope.
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("retail", "orders", ["amount"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
        assert repo.get("orders", "region") is None, "the model's own dropped field must be pruned"
        assert repo.get("orders", "amount")["description"] == "Admin: order amount"
        assert repo.get("orders", "amount")["source"] == "manual"

    def test_sibling_manual_models_sharing_a_table_do_not_prune_each_others_columns(self, seeded_app):
        """Follow-up RED regression (Devin, PR #1528): every manual model's
        column rows share `(source='semantic_model', source_ref=None)`, and
        each admin-API write projects alone (`partial=True`) — but
        `_prune_columns` scopes only on `(table_id, source)`, so projecting
        model A used to delete sibling model B's projected columns whenever
        both bound a dataset to the same table id. The partial call must
        spare the columns its in-scope siblings still claim, while pruning
        its OWN dropped fields normally."""
        c = seeded_app["client"]

        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("retail", "orders", ["col_a", "col_shared"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text

        # Projecting the sibling used to prune retail's columns outright.
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("finance", "orders", ["col_b"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        for name in ("col_a", "col_shared", "col_b"):
            row = repo.get("orders", name)
            assert row is not None, f"{name} must survive the sibling model's projection"
            assert row["source"] == "semantic_model"

        # Re-projecting retail WITHOUT col_a prunes its own dropped field —
        # and only that: finance's col_b (and the still-shared col_shared)
        # survive.
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("retail", "orders", ["col_shared"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
        assert repo.get("orders", "col_a") is None, "the model's own dropped field must still be pruned"
        assert repo.get("orders", "col_b") is not None, "a sibling model's column must survive re-projection"
        assert repo.get("orders", "col_shared") is not None

        # Deleting finance prunes col_b (nobody else claims it) but spares
        # col_shared, which retail still claims.
        finance_id = "manual/_/finance"
        r = c.delete(f"/api/admin/semantic-models/{finance_id}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text
        assert repo.get("orders", "col_b") is None, "the deleted model's own column must be pruned"
        assert repo.get("orders", "col_shared") is not None, "a surviving sibling's claim must spare the shared column"

    def test_deleting_a_manual_model_spares_admin_authored_columns(self, seeded_app):
        """DELETE prunes the model's own projected columns but must never
        reach an admin-authored `source='manual'` row for the same table."""
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/metadata/orders",
            json={"columns": [{"column_name": "amount", "description": "Admin: order amount"}]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        created = c.post(
            "/api/admin/semantic-models",
            json={"document": _doc_with_fields("retail", "orders", ["amount", "region"])},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert created.status_code == 201, created.text

        r = c.delete(
            f"/api/admin/semantic-models/{created.json()['id']}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 204, r.text

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        assert repo.get("orders", "region") is None, "the deleted model's own column rows must be pruned"
        admin_row = repo.get("orders", "amount")
        assert admin_row is not None, "an admin-authored row must survive the model's deletion"
        assert admin_row["description"] == "Admin: order amount"
        assert admin_row["source"] == "manual"

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
