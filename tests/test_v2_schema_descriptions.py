"""`/api/v2/schema/{table_id}` must carry real per-column descriptions
instead of the hardcoded ``""`` every source-type branch used to return.

Precedence: admin-authored ``column_metadata`` (the key shape
``app/api/metadata.py`` writes, ``(table_id, column_name)``) wins over an
Ossie dataset field description bound to the same ``table_id``; a column
with neither gets ``""``.

``column_metadata`` carries no RBAC narrower than table access itself, so
``TestColumnMetadataDescriptions`` exercises it at the ``build_schema_uncached``
layer directly (fast, no HTTP). An Ossie dataset field description rides a
SEPARATE, narrower RBAC resource (``ResourceType.SEMANTIC_MODEL`` —
``app/api/semantic_models.py::_can_read_model``), so ``TestOssieDescriptionRbac``
exercises it through the real route with real grants — that boundary, and
the shared ``_schema_cache``'s cache-poisoning risk across callers, can only
be caught end-to-end.

B3 (remediation program) acceptance test, extended after an /agnes-review
finding on PR #1528 (RBAC leak: the Ossie merge originally ran inside the
cacheable ``build_schema_uncached``, baking one caller's model-permitted
text into the shared ``_schema_cache`` entry for the next, less-privileged
caller). ``TestColumnMetadataDescriptions`` must fail (every description
``""``) against the pre-B3 ``app/api/v2_schema.py``; ``TestOssieDescriptionRbac``
must fail against the RBAC-leaking intermediate state.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import create_mock_extract, grant_table_via_package


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# column_metadata — build_schema_uncached layer (no RBAC dimension of its own)
# ---------------------------------------------------------------------------


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


def _build(table_id: str, *, name: str | None = None) -> dict:
    from app.api.v2_schema import build_schema_uncached

    fake_row = {"id": table_id, "name": name or table_id, "source_type": "local", "query_mode": "local"}
    return build_schema_uncached(conn=None, table_id=table_id, bq=object(), row=fake_row)


class TestColumnMetadataDescriptions:
    def test_column_metadata_description_is_carried(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        from src.repositories import column_metadata_repo

        column_metadata_repo().save(table_id="orders", column_name="col_a", description="Column A, admin-authored")

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_a"] == "Column A, admin-authored"

    def test_column_with_neither_source_is_blank(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _seed_parquet(tmp_path, "orders")

        result = _build("orders")
        by_name = {c["name"]: c["description"] for c in result["columns"]}
        assert by_name["col_c"] == ""


# ---------------------------------------------------------------------------
# Ossie dataset field description — real route, real grants: the RBAC
# boundary AND the shared-cache poisoning risk can only be caught end-to-end.
# ---------------------------------------------------------------------------


def _seed_registered_table(seeded_app, table_name: str, rows: list[dict]) -> None:
    from src.orchestrator import SyncOrchestrator

    env = seeded_app["env"]
    create_mock_extract(env["extracts_dir"], "keboola", [{"name": table_name, "data": rows}])
    SyncOrchestrator().rebuild()
    resp = seeded_app["client"].post(
        "/api/admin/register-table",
        json={"name": table_name, "source_type": "keboola"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 201, resp.text


def _grant_table_to_analyst(table_id: str, group_name: str) -> None:
    from src.db import get_system_db

    conn = get_system_db()
    try:
        grant_table_via_package(conn, table_id, "analyst1", group_name=group_name)
    finally:
        conn.close()


def _grant_model_to_analyst(model_id: str, group_name: str) -> None:
    from src.db import get_system_db
    from src.repositories import resource_grants_repo, user_groups_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    group = user_groups_repo().create(name=group_name, description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    resource_grants_repo().create(
        group_id=gid,
        resource_type="semantic_model",
        resource_id=model_id,
        assigned_by="test",
    )


def _seed_manual_model(*, id: str, slug: str, table_id: str, field_name: str, description: str) -> dict:
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
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


class TestOssieDescriptionRbac:
    def test_table_access_without_model_access_hides_the_ossie_description(self, seeded_app):
        """(a) A caller with table access but no grant on the bound
        semantic model must not see its field description — column_metadata
        (a different, non-model-scoped resource) is unaffected."""
        _seed_registered_table(seeded_app, "salaries_a", [{"id": "1", "col_a": "x", "col_b": "y"}])
        _grant_table_to_analyst("salaries_a", "Table Readers A")

        from src.repositories import column_metadata_repo

        column_metadata_repo().save(table_id="salaries_a", column_name="col_a", description="Admin-authored col_a")
        _seed_manual_model(
            id="manual/_/salaries_a_model",
            slug="salaries_a_model",
            table_id="salaries_a",
            field_name="col_b",
            description="Ossie description for col_b",
        )
        # Deliberately no package link / no model grant for analyst1.

        c = seeded_app["client"]
        r = c.get("/api/v2/schema/salaries_a", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        by_name = {col["name"]: col["description"] for col in r.json()["columns"]}
        assert by_name["col_a"] == "Admin-authored col_a"
        assert by_name["col_b"] == "", "analyst without model access must not see the Ossie description"

    def test_granted_model_access_surfaces_the_ossie_description(self, seeded_app):
        """(b) A caller with an explicit grant on the semantic model sees
        the field description bound to a table it also has access to."""
        _seed_registered_table(seeded_app, "salaries_b", [{"id": "1", "col_b": "y"}])
        _grant_table_to_analyst("salaries_b", "Table Readers B")

        model = _seed_manual_model(
            id="manual/_/salaries_b_model",
            slug="salaries_b_model",
            table_id="salaries_b",
            field_name="col_b",
            description="Ossie description for col_b",
        )
        _grant_model_to_analyst(model["id"], "Model Readers B")

        c = seeded_app["client"]
        r = c.get("/api/v2/schema/salaries_b", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        by_name = {col["name"]: col["description"] for col in r.json()["columns"]}
        assert by_name["col_b"] == "Ossie description for col_b"

    def test_admin_bypass_surfaces_the_ossie_description(self, seeded_app):
        """(b, admin variant) Admin's god-mode bypass reaches every model
        regardless of grants."""
        _seed_registered_table(seeded_app, "salaries_b2", [{"id": "1", "col_b": "y"}])
        _seed_manual_model(
            id="manual/_/salaries_b2_model",
            slug="salaries_b2_model",
            table_id="salaries_b2",
            field_name="col_b",
            description="Ossie description for col_b",
        )

        c = seeded_app["client"]
        r = c.get("/api/v2/schema/salaries_b2", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        by_name = {col["name"]: col["description"] for col in r.json()["columns"]}
        assert by_name["col_b"] == "Ossie description for col_b"

    def test_admin_request_does_not_poison_the_cache_for_a_later_unprivileged_caller(self, seeded_app):
        """(c) Cache-poisoning regression: the admin's request must warm
        `_schema_cache` under the bare `table_id`, and a SUBSEQUENT request
        from a caller without model access must still not see the
        admin-visible Ossie text."""
        _seed_registered_table(seeded_app, "salaries_c", [{"id": "1", "col_b": "y"}])
        _grant_table_to_analyst("salaries_c", "Table Readers C")
        _seed_manual_model(
            id="manual/_/salaries_c_model",
            slug="salaries_c_model",
            table_id="salaries_c",
            field_name="col_b",
            description="Ossie description for col_b",
        )
        # Deliberately no package link / no model grant for analyst1.

        c = seeded_app["client"]
        admin_resp = c.get("/api/v2/schema/salaries_c", headers=_auth(seeded_app["admin_token"]))
        assert admin_resp.status_code == 200, admin_resp.text
        admin_by_name = {col["name"]: col["description"] for col in admin_resp.json()["columns"]}
        assert admin_by_name["col_b"] == "Ossie description for col_b", "precondition: admin must see it"

        analyst_resp = c.get("/api/v2/schema/salaries_c", headers=_auth(seeded_app["analyst_token"]))
        assert analyst_resp.status_code == 200, analyst_resp.text
        analyst_by_name = {col["name"]: col["description"] for col in analyst_resp.json()["columns"]}
        assert analyst_by_name["col_b"] == "", "the admin's warmed cache leaked the Ossie description to the analyst"
