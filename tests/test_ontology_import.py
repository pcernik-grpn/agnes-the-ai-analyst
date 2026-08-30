"""Tests for scripts/ontology/import_ontology.py -- the ontology.yaml ->
Ossie semantic-model translation script (fact-graph build-order step 1,
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
Sec 11 / Sec 16).

Fixture: tests/fixtures/eval/ontology.yaml is the REAL producer ontology
(Northwind Star / TCRD-185, v0.2.0), committed verbatim under the scope-note
waiver at the top of that spec (dated 2026-08-27): "this spec deliberately
contains customer-specific material ... If this repository ever returns to
public distribution, the spec must be scrubbed" -- the same waiver covers
eval fixtures that carry the same vocabulary, per Sec 16 step 5
("fixtures ... under the header's scope-note waiver").

These are pure-function tests: translate_ontology() takes a parsed dict and
returns (document_text, report), no app/server/network involved.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scripts.ontology.import_ontology import translate_ontology
from src.semantic.document_validation import validate_document

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "ontology.yaml"


@pytest.fixture()
def ontology() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def test_fixture_exists_and_parses(ontology):
    assert str(ontology["version"]) == "0.2.0"
    assert "node_types" in ontology
    assert "edge_types" in ontology


def test_translation_produces_schema_valid_document(ontology):
    document_text, report = translate_ontology(ontology)
    result = validate_document(document_text)
    assert result.ok, result.errors


def test_dataset_and_relationship_counts_derived_from_fixture(ontology):
    """Never hardcode the counts -- derive them from the fixture itself so a
    future ontology.yaml revision (the file already carries a changelog
    comment recording one such addition) does not silently desync the test
    from the source of truth."""
    document_text, report = translate_ontology(ontology)
    parsed = yaml.safe_load(document_text)
    model = parsed["semantic_model"][0]

    assert len(model["datasets"]) == len(ontology["node_types"])
    assert report.node_type_count == len(ontology["node_types"])
    assert report.edge_type_count == len(ontology["edge_types"])

    expected_relationships = len(ontology["edge_types"]) + len(report.self_relationships)
    assert len(model["relationships"]) == expected_relationships

    dataset_names = {d["name"] for d in model["datasets"]}
    assert dataset_names == set(ontology["node_types"])

    relationship_names = {r["name"] for r in model["relationships"]}
    for edge_name in ontology["edge_types"]:
        assert edge_name in relationship_names


def test_evidence_rule_lands_verbatim_in_ai_context(ontology):
    document_text, report = translate_ontology(ontology)
    parsed = yaml.safe_load(document_text)
    model = parsed["semantic_model"][0]
    instructions = model["ai_context"]["instructions"]

    evidence_rule = ontology["conventions"]["evidence"]["rule"].strip()
    assert evidence_rule in instructions

    entity_resolution_rule = ontology["conventions"]["entity_resolution"].strip()
    assert entity_resolution_rule in instructions


def test_leftover_report_lists_known_leftovers(ontology):
    document_text, report = translate_ontology(ontology)
    rendered = report.render()

    assert "industry.parent" in rendered
    assert "confirm" in rendered.lower()
    assert any("industry" in name for name in report.self_relationships)

    assert "evidenced_by" in rendered
    assert "possible_duplicate_of" in rendered
    assert set(report.wildcard_relationships) == {"evidenced_by", "possible_duplicate_of"}

    assert "purpose" in rendered
    assert "conventions" in rendered


def test_translation_is_deterministic(ontology):
    text1, report1 = translate_ontology(copy.deepcopy(ontology))
    text2, report2 = translate_ontology(copy.deepcopy(ontology))
    assert text1 == text2
    assert report1.render() == report2.render()


def test_no_customer_vocabulary_in_translator_source():
    """The script is generic -- the ontology's own vocabulary (node/edge type
    names, attribute names) must never appear as a literal in the script's
    own source, only flow through as data."""
    script_path = Path(__file__).parent.parent / "scripts" / "ontology" / "import_ontology.py"
    source = script_path.read_text()
    for needle in ("engagement", "sponsor", "northwind", "litware", "fabrikam"):
        assert needle not in source.lower(), f"customer/ontology vocabulary {needle!r} leaked into the script"


# --- CLI-level tests: --out / --server / fail-loudly-on-invalid-document ---


def test_cli_writes_out_file(tmp_path):
    from scripts.ontology.import_ontology import main

    out_path = tmp_path / "model.yaml"
    rc = main([str(FIXTURE), "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()

    result = validate_document(out_path.read_text())
    assert result.ok, result.errors


def test_cli_fails_loudly_on_invalid_document(tmp_path, capsys):
    from scripts.ontology.import_ontology import main

    # A node_types-less ontology translates to zero datasets -- the vendored
    # Ossie schema requires at least one, so this must be refused loudly
    # (nonzero exit, message on stderr, nothing written) rather than
    # producing a document nobody can load.
    empty_ontology_path = tmp_path / "empty_ontology.yaml"
    empty_ontology_path.write_text("node_types: {}\nedge_types: {}\n")
    out_path = tmp_path / "model.yaml"

    rc = main([str(empty_ontology_path), "--out", str(out_path)])
    captured = capsys.readouterr()

    assert rc != 0
    assert "schema validation" in captured.err.lower()
    assert not out_path.exists()


def test_cli_server_requires_token(tmp_path):
    from scripts.ontology.import_ontology import main

    rc = main([str(FIXTURE), "--server", "https://agnes.example.com"])
    assert rc != 0


def test_cli_posts_to_server(monkeypatch, capsys):
    import httpx

    from scripts.ontology.import_ontology import main

    captured_calls = []

    class _FakeResponse:
        status_code = 201

        def json(self):
            body = dict()
            body["id"] = "manual/_/ontology"
            body["slug"] = "ontology"
            return body

    def fake_post(url, json=None, headers=None, timeout=None):
        captured_calls.append((url, json, headers))
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    rc = main([str(FIXTURE), "--server", "https://agnes.example.com/", "--token", "secret-token"])
    captured = capsys.readouterr()

    assert rc == 0
    assert len(captured_calls) == 1
    url, payload, headers = captured_calls[0]
    assert url == "https://agnes.example.com/api/admin/semantic-models"
    assert headers["Authorization"] == "Bearer secret-token"
    assert "document" in payload
    assert "Imported semantic model" in captured.out
