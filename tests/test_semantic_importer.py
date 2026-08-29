"""Importing already-fetched Ossie documents: content-hash no-op, invalid
documents stored without aborting the run, scoped prune of dropped models.

Fetching (git clone / upload / connection) is Task 9's `transports.py`; this
module exercises the pipeline with documents passed in directly.

Reuses the `e2e_env` DATA_DIR-isolation fixture from `tests/conftest.py`
under the `system_db` name the plan assumed, the same adaptation
`tests/test_semantic_projection.py` made for Task 7 — `tests/conftest.py` has
no fixture literally named `system_db`.
"""

import pytest

from src.repositories import semantic_model_repo
from src.semantic.importer import import_documents


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


SOURCE = {"id": "s1", "kind": "upload", "adapter": "native", "source": "git", "source_ref": "repo-a"}


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


def test_unchanged_document_is_a_no_op_write(system_db):
    """Re-importing identical content must not bump updated_at."""
    import_documents(SOURCE, [_doc("retail")])
    first = semantic_model_repo().get_by_slug("retail")["updated_at"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_unchanged == 1
    assert report.models_written == 0
    assert semantic_model_repo().get_by_slug("retail")["updated_at"] == first


def test_invalid_document_is_stored_with_its_errors_and_does_not_abort_the_run(system_db):
    """One bad file must not cost the sync its good files."""
    report = import_documents(SOURCE, [_doc("retail"), "semantic_model: [oops"])

    assert report.models_written == 1
    assert len(report.invalid) == 1
    assert semantic_model_repo().get_by_slug("retail") is not None


def test_document_dropped_upstream_is_pruned(system_db):
    import_documents(SOURCE, [_doc("retail"), _doc("finance")])
    dropped = semantic_model_repo().get_by_slug("finance")["id"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_pruned == [dropped]
    assert semantic_model_repo().get_by_slug("finance") is None
    assert semantic_model_repo().get_by_slug("retail") is not None


def test_two_documents_in_one_import_keep_both_their_metrics(system_db):
    """Projection prunes per (source, source_ref), so projecting document by
    document under one origin makes each call delete the previous one's rows.

    The pipeline must project all valid documents of a call together. This
    asserts projected CONTENT — a count- or model-row-only assertion passes
    even when the data loss is happening.
    """
    from src.repositories import metric_repo

    import_documents(SOURCE, [_doc("retail", "revenue"), _doc("finance", "cost")])

    names = {m["name"] for m in metric_repo().list()}
    assert names >= {"revenue", "cost"}


def test_duplicate_model_names_in_one_import_are_disambiguated_not_dropped(system_db):
    """Two documents declaring the same model name must BOTH be stored.

    The row id derives from the slug, so without disambiguation the later
    document overwrites the earlier one. Treating the second as `invalid`
    (the first fix) traded silent overwrite for silent loss of a different
    kind: the document was never stored, the run was permanently `partial`
    (which narrows every subsequent prune), and a previously-stored
    `slug-<id>` row was deleted by `delete_missing`. The composer this
    pipeline replaced disambiguated instead — `slug-<stable id>` — and so
    does this.
    """
    report = import_documents(SOURCE, [_doc("retail", "revenue"), _doc("retail", "other")])

    slugs = sorted(r["slug"] for r in semantic_model_repo().list_all())
    assert len(slugs) == 2, slugs
    assert slugs[0] == "retail"  # first occurrence keeps the clean slug
    assert slugs[1].startswith("retail-")
    assert report.models_written == 2
    assert report.invalid == []


def test_a_disambiguated_duplicate_survives_the_next_sync(system_db):
    """The suffix is derived from the model, not from arrival order, so the
    same batch re-imported is a no-op — not a prune-and-recreate."""
    documents = [_doc("retail", "revenue"), _doc("retail", "other")]
    import_documents(SOURCE, documents)
    before = {r["slug"] for r in semantic_model_repo().list_all()}

    report = import_documents(SOURCE, documents)

    assert report.models_pruned == []
    assert report.models_unchanged == 2
    assert {r["slug"] for r in semantic_model_repo().list_all()} == before


def test_a_duplicate_name_with_a_stable_upstream_id_is_keyed_on_that_id(system_db):
    """A connector-composed document carries the upstream object's own id
    (`custom_extensions[AGNES].metastore_id`) — the same key the projection
    scopes its row ids on. Disambiguating on it keeps the stored document and
    its projection talking about the same model."""
    import json

    def _doc_with_id(model_id):
        return (
            "version: '0.2.0.dev0'\n"
            "semantic_model:\n"
            "  - name: retail\n"
            "    custom_extensions:\n"
            "      - vendor_name: AGNES\n"
            f"        data: '{json.dumps({'metastore_id': model_id})}'\n"
            "    datasets:\n"
            "      - name: orders\n"
            "        source: db.public.orders\n"
        )

    import_documents(SOURCE, [_doc_with_id("m-1"), _doc_with_id("m-2")])

    slugs = sorted(r["slug"] for r in semantic_model_repo().list_all())
    assert slugs == ["retail", "retail-m-2"]


def test_null_source_ref_does_not_read_across_other_refs(system_db):
    """The read scope must match the prune scope.

    `list_all(source_ref=None)` means "unfiltered", while
    `delete_missing(source_ref=None)` means "the NULL origin" — so an import
    under a NULL ref would otherwise compare its documents against rows owned
    by OTHER refs, call an identical one 'unchanged', and skip writing it to
    the origin it was actually importing into. Not reachable through today's
    callers; pinned so it cannot become reachable silently.
    """
    text = _doc("retail", "revenue")
    import_documents({"source": "manual", "source_ref": "somewhere-else"}, [text])

    report = import_documents({"source": "manual", "source_ref": None}, [text])

    assert report.models_written == 1, "identical content under another ref is not 'unchanged' here"
    assert report.models_unchanged == 0
