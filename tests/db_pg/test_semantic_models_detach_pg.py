"""``SemanticModelsPgRepository``'s F3 detach/override methods (PG-only,
A3 ratchet — see migrations/versions/0090_semantic_models_detach.py).

No DuckDB parametrization here: the DuckDB sibling has no columns to back
these methods, so it just raises ``RequiresPostgresBackend`` — that fail-
clean behavior is pinned separately, see
``tests/test_semantic_models_detach_fail_clean.py``.
"""

from __future__ import annotations


import pytest


@pytest.fixture
def repo(pg_engine_with_schema):
    from src.repositories.semantic_models_pg import SemanticModelsPgRepository

    return SemanticModelsPgRepository(pg_engine_with_schema)


def _seed(repo, *, id="manual/_/orders", slug="orders", source="manual", source_ref=None):
    return repo.upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="doc",
        document_json={"semantic_model": [{"name": slug}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source=source,
        source_ref=source_ref,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class TestDetach:
    def test_sets_sync_mode_and_audit_fields(self, repo):
        row = _seed(repo, source="keboola_metastore", source_ref="proj1")
        updated = repo.detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

        assert updated["sync_mode"] == "detached"
        assert updated["detached_by"] == "admin@example.com"
        assert updated["detached_at"] is not None
        assert updated["detach_base_hash"] == "h1"

    def test_leaves_document_and_provenance_untouched(self, repo):
        row = _seed(repo, source="keboola_metastore", source_ref="proj1")
        updated = repo.detach(row["id"], by="admin@example.com", base_hash=row["content_hash"])

        assert updated["document"] == "doc"
        assert updated["content_hash"] == "h1"
        assert updated["source"] == "keboola_metastore"
        assert updated["source_ref"] == "proj1"


class TestUpdateSourceContentHash:
    def test_sets_the_hash_without_touching_the_row_document(self, repo):
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")

        repo.update_source_content_hash(row["id"], "h2-from-upstream")

        updated = repo.get(row["id"])
        assert updated["source_content_hash"] == "h2-from-upstream"
        assert updated["document"] == "doc"
        assert updated["content_hash"] == "h1"


class TestMarkAndClearSourceMissing:
    def test_mark_sets_the_timestamp(self, repo):
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")

        repo.mark_source_missing(row["id"])

        updated = repo.get(row["id"])
        assert updated["source_missing_since"] is not None

    def test_mark_is_a_noop_once_already_set(self, repo):
        """The FIRST time the source stopped sending this slug is what
        matters — a later sweep tick must not keep bumping the timestamp
        forward."""
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")
        repo.mark_source_missing(row["id"])
        first = repo.get(row["id"])["source_missing_since"]

        repo.mark_source_missing(row["id"])
        second = repo.get(row["id"])["source_missing_since"]

        assert first == second

    def test_clear_resets_to_null(self, repo):
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")
        repo.mark_source_missing(row["id"])

        repo.clear_source_missing(row["id"])

        assert repo.get(row["id"])["source_missing_since"] is None


class TestReattach:
    def test_returns_to_synced_and_clears_all_detach_fields(self, repo):
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")
        repo.update_source_content_hash(row["id"], "h2")
        repo.mark_source_missing(row["id"])

        updated = repo.reattach(row["id"])

        assert updated["sync_mode"] == "synced"
        assert updated["detached_at"] is None
        assert updated["detached_by"] is None
        assert updated["detach_base_hash"] is None
        assert updated["source_content_hash"] is None
        assert updated["source_missing_since"] is None

    def test_does_not_touch_document_or_content_hash_itself(self, repo):
        """Re-attach flips the flag; a subsequent sync run is what actually
        rewrites ``document``/``content_hash`` back onto the sync path
        (importer, not this method) — see phase3.md §7."""
        row = _seed(repo)
        repo.detach(row["id"], by="admin@example.com", base_hash="h1")

        updated = repo.reattach(row["id"])

        assert updated["document"] == "doc"
        assert updated["content_hash"] == "h1"
