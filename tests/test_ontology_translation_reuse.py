"""``app/api/ontology.py`` reuses ``scripts/ontology/import_ontology.py``'s
``translate_ontology`` server-side, unmodified -- no fork, no reimplementation.

Fact-graph-over-Collections §13.2 "Ontology builder" / §11 economics. These
are pure-function tests: no app, no server, no database.
"""

from __future__ import annotations

from app.api.ontology import _draft_to_ontology_dict
from scripts.ontology.import_ontology import translate_ontology
from src.semantic.document_validation import validate_document


def _draft(node_types=None, edge_types=None, name="test_ontology"):
    return {
        "name": name,
        "node_types": node_types or {},
        "edge_types": edge_types or {},
    }


def test_draft_to_ontology_dict_translates_to_a_valid_ossie_document():
    draft = _draft(
        node_types={
            "client": {"description": "A client company.", "attrs": {"name": {"type": "string", "required": True}}},
            "sponsor": {"description": "The PE firm.", "attrs": {"name": {"type": "string"}}},
        },
        edge_types={"owned_by": {"src": "client", "dst": "sponsor", "description": "Client is owned by sponsor."}},
    )
    ontology = _draft_to_ontology_dict(draft)
    document_text, report = translate_ontology(ontology)

    result = validate_document(document_text)
    assert result.ok, result.errors
    assert report.node_type_count == 2
    assert report.edge_type_count == 1


def test_evidence_required_false_folds_into_the_relationship_description():
    """`evidence_required` has no field in the ontology.yaml shape
    `translate_ontology` understands -- per the ontology-building skill's
    "rule with no field to live in" guidance, it must be folded somewhere
    visible rather than silently dropped."""
    draft = _draft(
        node_types={"a": {"attrs": {}}, "b": {"attrs": {}}},
        edge_types={"maybe_related": {"src": "a", "dst": "b", "evidence_required": False}},
    )
    ontology = _draft_to_ontology_dict(draft)
    assert "evidence_required" not in ontology["edge_types"]["maybe_related"]
    assert "NOT required" in ontology["edge_types"]["maybe_related"]["description"]

    document_text, _ = translate_ontology(ontology)
    result = validate_document(document_text)
    assert result.ok, result.errors
    assert "NOT required" in document_text


def test_evidence_required_true_is_the_default_and_adds_no_note():
    draft = _draft(
        node_types={"a": {"attrs": {}}, "b": {"attrs": {}}},
        edge_types={"related": {"src": "a", "dst": "b"}},
    )
    ontology = _draft_to_ontology_dict(draft)
    assert ontology["edge_types"]["related"].get("description") is None


def test_empty_draft_translates_but_fails_schema_validation():
    """A draft with no types is a real, distinct failure mode Save must
    catch before it ever reaches the schema validator (empty_draft check in
    save_draft) -- this test just pins that translate_ontology + the
    validator agree an empty document is invalid, so that earlier guard is
    not redundant."""
    ontology = _draft_to_ontology_dict(_draft())
    document_text, _ = translate_ontology(ontology)
    result = validate_document(document_text)
    assert not result.ok
