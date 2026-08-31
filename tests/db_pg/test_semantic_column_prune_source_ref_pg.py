"""``_prune_columns``'s ``(source, source_ref)`` scoping on Postgres
(``src/semantic/projection.py``, P1-2 of the post-#1707 semantic-layer
remediation).

Two sources sharing a ``source`` value but not a ``source_ref`` — two
registered ``ossie_git`` sources, or two Keboola connections — whose
documents describe datasets resolving to the SAME ``table_id`` used to
delete each other's ``column_metadata`` rows on every sync, because the
prune only ever read ``(table_id, source)``. Postgres has the
``source_ref`` column (``migrations/versions/0075_column_meta_source_ref.py``)
the prune now scopes on; DuckDB's frozen app-state schema does not, so this
collision — and its fix — is Postgres-only. PG-only, so this file only runs
against Postgres (matches ``tests/db_pg/test_semantic_importer_detach_pg.py``'s
pattern), skipping the DuckDB param of the ``state_backend`` harness.
"""

from __future__ import annotations

import pytest

from src.semantic.projection import project_document


def _skip_unless_pg(state_backend) -> None:
    if state_backend != "pg":
        pytest.skip("PG-only")


@pytest.fixture
def system_db(state_backend, tmp_path, monkeypatch):
    _skip_unless_pg(state_backend)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    yield


def _doc(model_name: str, field_name: str) -> dict:
    return {
        "semantic_model": [
            {
                "name": model_name,
                "datasets": [
                    {
                        "name": "orders",
                        # Unregistered, so `_column_table_id` falls back to
                        # this raw string as the table_id — same fallback
                        # `test_reprojection_prunes_only_this_origin` in
                        # `tests/test_semantic_projection.py` relies on.
                        "source": "db.public.orders",
                        "fields": [{"name": field_name}],
                    }
                ],
            }
        ]
    }


def test_two_source_refs_of_the_same_source_do_not_prune_each_others_columns(system_db):
    project_document(_doc("retail", "region"), source="ossie_git", source_ref="src1")
    # Same `source`, different `source_ref`, same resolved table_id. Before
    # the fix, this second call's prune read every `source="ossie_git"` row
    # for "db.public.orders" regardless of source_ref, saw src1's "region"
    # was not in ITS OWN written set ({"country"}), and deleted it.
    project_document(_doc("finance", "country"), source="ossie_git", source_ref="src2")

    from src.repositories import column_metadata_repo

    rows = column_metadata_repo().list_for_table("db.public.orders")
    remaining = {r["column_name"] for r in rows}
    assert remaining == {"region", "country"}, "a sync under a different source_ref must not prune this one's columns"

    by_name = {r["column_name"]: r for r in rows}
    assert by_name["region"]["source_ref"] == "src1"
    assert by_name["country"]["source_ref"] == "src2"


def test_reprojecting_the_same_source_ref_still_prunes_its_own_dropped_column(system_db):
    """The fix narrows the scope; it must not also disable pruning within a
    single (source, source_ref) — that would defeat the whole prune."""
    project_document(_doc("retail", "region"), source="ossie_git", source_ref="src1")
    project_document(_doc("finance", "country"), source="ossie_git", source_ref="src2")

    # Re-project src1 with its field dropped: only src1's own row should go.
    shrunk = {
        "semantic_model": [
            {"name": "retail", "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}]}
        ]
    }
    project_document(shrunk, source="ossie_git", source_ref="src1")

    from src.repositories import column_metadata_repo

    remaining = {r["column_name"] for r in column_metadata_repo().list_for_table("db.public.orders")}
    assert remaining == {"country"}, "src1 must still prune its own dropped column"
