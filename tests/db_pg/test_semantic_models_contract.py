"""Cross-engine contract tests for the semantic_models repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.semantic_models import SemanticModelsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SemanticModelsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.semantic_models_pg import SemanticModelsPgRepository

    return SemanticModelsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


_UNSET = object()


def _upsert(repo, *, id, slug, source="git", source_ref="repo-a", status="valid", document_json=_UNSET):
    return repo.upsert(
        id=id,
        slug=slug,
        name=slug.title(),
        description=None,
        document=f"version: '0.2.0.dev0'\nsemantic_model:\n  - name: {slug}\n",
        document_json={"semantic_model": [{"name": slug}]} if document_json is _UNSET else document_json,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=source_ref,
        status=status,
        validation_errors=None,
        validated_at=None,
    )


def test_upsert_then_get(repo):
    _upsert(repo, id="m1", slug="retail")
    row = repo.get("m1")
    assert row["slug"] == "retail"
    assert row["spec_version"] == "0.2.0.dev0"
    assert row["document"].startswith("version:")
    assert row["document_json"]["semantic_model"][0]["name"] == "retail"


def test_upsert_is_idempotent_on_same_origin(repo):
    _upsert(repo, id="m1", slug="retail")
    _upsert(repo, id="m1", slug="retail")
    assert len(repo.list_all()) == 1


def test_get_by_slug(repo):
    _upsert(repo, id="m1", slug="retail")
    assert repo.get_by_slug("retail")["id"] == "m1"
    assert repo.get_by_slug("nope") is None


def test_list_filters_by_origin(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m2", slug="finance", source="git", source_ref="repo-b")
    assert {r["id"] for r in repo.list_all(source="git", source_ref="repo-a")} == {"m1"}
    assert len(repo.list_all(source="git")) == 2


def test_delete_missing_is_scoped_to_one_origin(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m2", slug="stale", source="git", source_ref="repo-a")
    _upsert(repo, id="m3", slug="other", source="git", source_ref="repo-b")

    deleted = repo.delete_missing(source="git", source_ref="repo-a", keep_slugs=["retail"])

    assert deleted == ["m2"]
    assert repo.get("m1") is not None
    assert repo.get("m3") is not None, "prune must never cross a source_ref boundary"


def test_delete_missing_with_empty_keep_list_deletes_that_origin_only(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m3", slug="other", source="git", source_ref="repo-b")

    assert repo.delete_missing(source="git", source_ref="repo-a", keep_slugs=[]) == ["m1"]
    assert repo.get("m3") is not None


def test_package_links(repo):
    _upsert(repo, id="m1", slug="retail")
    repo.link_package("pkg1", "m1")
    assert [r["id"] for r in repo.list_for_package("pkg1")] == ["m1"]
    repo.link_package("pkg1", "m1")  # idempotent
    assert len(repo.list_for_package("pkg1")) == 1
    repo.unlink_package("pkg1", "m1")
    assert repo.list_for_package("pkg1") == []


def test_list_packages_for_model_is_the_reverse_lookup(repo):
    """The export/search RBAC gate (Task 10) needs "which packages grant
    access to this model" — the reverse of list_for_package, which answers
    "which models does this package grant"."""
    _upsert(repo, id="m1", slug="retail")
    assert repo.list_packages_for_model("m1") == []

    repo.link_package("pkg1", "m1")
    repo.link_package("pkg2", "m1")
    assert repo.list_packages_for_model("m1") == ["pkg1", "pkg2"]

    repo.unlink_package("pkg1", "m1")
    assert repo.list_packages_for_model("m1") == ["pkg2"]


def test_delete_missing_treats_a_null_source_ref_as_its_own_origin(repo):
    """A NULL source_ref is one origin among others, not a wildcard.

    SQL NULL is never equal to itself, so a naive `source_ref = ?` prunes
    nothing here on both engines — and the two engines express the null-safe
    comparison differently (IS NOT DISTINCT FROM vs an array cast), which is
    exactly where they can silently diverge.
    """
    _upsert(repo, id="m1", slug="kept", source="manual", source_ref=None)
    _upsert(repo, id="m2", slug="gone", source="manual", source_ref=None)
    _upsert(repo, id="m3", slug="other", source="manual", source_ref="repo-a")

    deleted = repo.delete_missing(source="manual", source_ref=None, keep_slugs=["kept"])

    assert deleted == ["m2"]
    assert repo.get("m1") is not None
    assert repo.get("m3") is not None, "a NULL-ref sync must not prune a non-NULL sibling"


def test_update_document_rewrites_content_in_place(repo):
    """F3: a plain UPDATE, not upsert()'s DELETE+INSERT — same id, same
    source/source_ref (provenance untouched)."""
    _upsert(repo, id="m1", slug="retail", source="keboola_metastore", source_ref="proj1")

    updated = repo.update_document(
        "m1",
        name="Retail",
        document="version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n  - name: extra\n",
        document_json={"semantic_model": [{"name": "retail"}, {"name": "extra"}]},
        content_hash="new-hash",
        description="edited",
        spec_version="0.2.0.dev0",
        validated_at=None,
    )

    assert updated["id"] == "m1"
    assert updated["content_hash"] == "new-hash"
    assert updated["description"] == "edited"
    assert len(updated["document_json"]["semantic_model"]) == 2
    assert updated["source"] == "keboola_metastore"
    assert updated["source_ref"] == "proj1"
    assert repo.get_by_slug("retail")["id"] == "m1", "still the only row at this slug, not a second one"


def test_list_all_source_ref_none_means_unfiltered_on_both_engines(repo):
    """`source_ref=None` on list_all means "don't filter", NOT "match NULL".

    It reads the same as delete_missing's None, which means the NULL origin —
    so the two are deliberately different and both engines must at least agree
    with each other. Pinned so the asymmetry is a decision, not a surprise.
    """
    _upsert(repo, id="m1", slug="kept", source="manual", source_ref=None)
    _upsert(repo, id="m3", slug="other", source="manual", source_ref="repo-a")

    assert {r["id"] for r in repo.list_all(source="manual", source_ref=None)} == {"m1", "m3"}


def test_count_valid_is_the_cheap_existence_gate(repo):
    """`POST /api/query` asks "is there a semantic layer at all?" on EVERY
    query. That question must cost one COUNT, not a full `list_all()` that
    drags `document` + `document_json` for every row across the wire.

    It counts exactly the rows `_accessible_valid_documents` can use:
    `status='valid'` AND a non-NULL `document_json`.
    """
    assert repo.count_valid() == 0

    _upsert(repo, id="m1", slug="retail")
    assert repo.count_valid() == 1

    _upsert(repo, id="m2", slug="broken", status="invalid")
    assert repo.count_valid() == 1, "a non-valid row is not a usable model"

    _upsert(repo, id="m3", slug="empty", document_json=None)
    assert repo.count_valid() == 1, "a row with no parsed document has nothing to validate against"

    _upsert(repo, id="m4", slug="finance")
    assert repo.count_valid() == 2


def test_count_valid_agrees_with_list_all_on_both_engines(repo):
    """The gate must never under-count what the loader would find — that
    would silently switch the advisory off. Pinned against the loader's own
    predicate rather than a hand-written number."""
    _upsert(repo, id="m1", slug="retail")
    _upsert(repo, id="m2", slug="broken", status="invalid")
    _upsert(repo, id="m3", slug="empty", document_json=None)

    usable = [r for r in repo.list_all() if r.get("status") == "valid" and r.get("document_json")]
    assert repo.count_valid() >= len(usable)
    assert repo.count_valid() == len(usable)


def test_counts_by_provenance_groups_every_origin(repo):
    """The read-time answer to "how many models does this source own".

    Keyed on the same ``(source, source_ref)`` pair ``delete_missing`` prunes
    on, but deliberately NOT equal to what a prune would delete — the prune is
    narrower (detached rows on PG, the ``safe_prune`` skip). "Owns" is "is
    stamped with this provenance", nothing more. One grouped query, not one
    COUNT per source.
    """
    assert repo.counts_by_provenance() == {}

    _upsert(repo, id="m1", slug="retail", source="ossie_git", source_ref="ss_a")
    _upsert(repo, id="m2", slug="finance", source="ossie_git", source_ref="ss_a")
    _upsert(repo, id="m3", slug="other", source="ossie_git", source_ref="ss_b")

    counts = repo.counts_by_provenance()
    assert counts[("ossie_git", "ss_a")] == 2
    assert counts[("ossie_git", "ss_b")] == 1
    assert ("ossie_git", "ss_c") not in counts, "a scope with no rows is absent, never a zero row"


def test_counts_by_provenance_treats_a_null_source_ref_as_its_own_origin(repo):
    """Same asymmetry ``delete_missing`` has: NULL is one origin among others,
    not a wildcard. Both engines must agree on the key it lands under."""
    _upsert(repo, id="m1", slug="kept", source="keboola_metastore", source_ref=None)
    _upsert(repo, id="m2", slug="other", source="keboola_metastore", source_ref="conn-a")

    counts = repo.counts_by_provenance()
    assert counts[("keboola_metastore", None)] == 1
    assert counts[("keboola_metastore", "conn-a")] == 1


def test_counts_by_provenance_counts_invalid_documents_too(repo):
    """An invalid document is still a row this source wrote and this source's
    next prune would reach. Excluding it would report "owns 0" for a source
    that is importing fine and failing validation — two different problems the
    health report already tells apart (``invalid_models``)."""
    _upsert(repo, id="m1", slug="retail", source="ossie_git", source_ref="ss_a")
    _upsert(repo, id="m2", slug="broken", source="ossie_git", source_ref="ss_a", status="invalid")

    assert repo.counts_by_provenance()[("ossie_git", "ss_a")] == 2
