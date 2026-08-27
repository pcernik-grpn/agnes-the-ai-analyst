"""``import_documents``'s F3 detach-aware sync behavior (PG-only, A3 ratchet)
— the three importer changes in ``src/semantic/importer.py``:

1. A detached row is never overwritten (``repo.upsert`` is never called for
   it), but the latest source hash is parked via
   ``update_source_content_hash``.
2. A detached row survives ``delete_missing`` even when its slug drops out
   of the incoming batch.
3. That drop is tracked via ``mark_source_missing`` / ``clear_source_missing``
   so "source changed" and "source deleted this slug entirely" stay two
   distinct, visible states (N3).

These all touch PG-only columns, so this file only runs against Postgres —
uses the ``state_backend`` parametrized harness, skipping the DuckDB param
(matches ``tests/db_pg/test_semantic_autodraft_sweep_pg.py``'s pattern).
"""

from __future__ import annotations

import pytest


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


SOURCE = {"id": "s1", "kind": "connection", "adapter": "native", "source": "keboola_metastore", "source_ref": "proj1"}


def _doc(slug, metric="revenue"):
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "    metrics:\n"
        f"      - name: {metric}\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        "              expression: SUM(amount)\n"
    )


def test_detached_row_is_never_overwritten_by_sync(system_db):
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail")])
    row = semantic_model_repo().get_by_slug("retail")
    semantic_model_repo().detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

    import_documents(SOURCE, [_doc("retail", metric="profit")])  # different content, same slug

    after = semantic_model_repo().get_by_slug("retail")
    assert after["document"] == row["document"]
    assert after["content_hash"] == row["content_hash"]
    assert after["sync_mode"] == "detached"


def test_detached_row_parks_the_latest_source_hash(system_db):
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail")])
    row = semantic_model_repo().get_by_slug("retail")
    semantic_model_repo().detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

    import_documents(SOURCE, [_doc("retail", metric="profit")])

    after = semantic_model_repo().get_by_slug("retail")
    assert after["source_content_hash"] is not None
    assert after["source_content_hash"] != row["content_hash"]


def test_detached_row_parking_is_a_no_op_write_when_source_is_unchanged(system_db):
    """Cheap re-sync: an identical incoming document must not keep rewriting
    `source_content_hash` (and its `updated_at`) on every tick."""
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail")])
    row = semantic_model_repo().get_by_slug("retail")
    semantic_model_repo().detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])
    import_documents(SOURCE, [_doc("retail", metric="profit")])
    first_updated_at = semantic_model_repo().get_by_slug("retail")["updated_at"]

    import_documents(SOURCE, [_doc("retail", metric="profit")])  # identical to the previous run

    assert semantic_model_repo().get_by_slug("retail")["updated_at"] == first_updated_at


def test_detached_row_survives_prune_when_source_drops_the_slug(system_db):
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail"), _doc("wholesale")])
    row = semantic_model_repo().get_by_slug("retail")
    semantic_model_repo().detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

    report = import_documents(SOURCE, [_doc("wholesale")])  # source no longer sends "retail"

    assert row["id"] not in report.models_pruned
    still_there = semantic_model_repo().get_by_slug("retail")
    assert still_there is not None
    assert still_there["sync_mode"] == "detached"


def test_synced_row_is_pruned_as_before_when_source_drops_the_slug(system_db):
    """Negative control: the detach exception must be scoped to detached
    rows — a normal synced row's prune behavior is unchanged."""
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail"), _doc("wholesale")])
    row = semantic_model_repo().get_by_slug("retail")

    report = import_documents(SOURCE, [_doc("wholesale")])

    assert row["id"] in report.models_pruned
    assert semantic_model_repo().get_by_slug("retail") is None


def test_source_missing_since_is_set_when_slug_drops_and_cleared_when_it_returns(system_db):
    from src.repositories import semantic_model_repo
    from src.semantic.importer import import_documents

    import_documents(SOURCE, [_doc("retail")])
    row = semantic_model_repo().get_by_slug("retail")
    semantic_model_repo().detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

    import_documents(SOURCE, [])  # source drops the slug entirely
    dropped = semantic_model_repo().get_by_slug("retail")
    assert dropped["source_missing_since"] is not None

    import_documents(SOURCE, [_doc("retail")])  # source sends it again
    returned = semantic_model_repo().get_by_slug("retail")
    assert returned["source_missing_since"] is None
