"""CLI surface for `agnes facts search|neighbors|claims` (build order step 6
of docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

Mocks `cli.commands.facts.api_get/api_post` (the mock-`api_*`-function
harness from `tests/test_cli_access_policy.py`) rather than a live
TestClient — these are CLI-shape tests (right payload to the right path,
right output/exit code for a given API response); server-side RBAC is
covered by `tests/db_pg/test_facts_read_pg.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    return r


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_happy_path_labels_server_origin_and_renders_table():
    subjects = [
        {
            "id": "f_1",
            "type": "person",
            "aliases": ["Alice"],
            "attrs": {"role": {"value": "engineer", "document_date": "2026-01-01"}},
            "claim_count": 2,
            "quote_count": 2,
            "revealed": False,
        }
    ]
    captured = {}

    def fake_post(path, **kwargs):
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return _resp(200, {"subjects": subjects, "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "search", "person"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/facts/search"
    assert captured["json"] == {"type": "person", "filters": {}, "limit": 20}
    assert "[server]" in result.output
    assert "facts have no local scope" in result.output
    assert "f_1" in result.output
    assert "person" in result.output
    assert "role=engineer" in result.output


def test_search_renders_conflicted_attribute_marker():
    subjects = [
        {
            "id": "f_2",
            "type": "person",
            "aliases": [],
            "attrs": {"title": {"conflicted": True, "values": ["VP", "SVP"]}},
            "claim_count": 2,
            "quote_count": 2,
            "revealed": False,
        }
    ]

    def fake_post(path, **kwargs):
        return _resp(200, {"subjects": subjects, "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "search", "person"])

    assert result.exit_code == 0, result.output
    assert "⚠ conflicted (2 values)" in result.output


def test_search_parses_filter_key_value_with_equals_in_value():
    """`--filter` splits on the FIRST `=` only, so a value containing `=`
    (a URL, a base64 blob) survives whole."""
    captured = {}

    def fake_post(path, **kwargs):
        captured["json"] = kwargs.get("json")
        return _resp(200, {"subjects": [], "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(
            app,
            ["facts", "search", "person", "--filter", "source_url=https://example.com?a=b"],
        )

    assert result.exit_code == 0, result.output
    assert captured["json"]["filters"] == {"source_url": "https://example.com?a=b"}


def test_search_filter_value_is_json_decoded_when_possible():
    captured = {}

    def fake_post(path, **kwargs):
        captured["json"] = kwargs.get("json")
        return _resp(200, {"subjects": [], "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "search", "person", "--filter", "age=30"])

    assert result.exit_code == 0, result.output
    assert captured["json"]["filters"] == {"age": 30}


def test_search_json_mode_emits_raw_response():
    def fake_post(path, **kwargs):
        return _resp(200, {"subjects": [], "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "search", "person", "--json"])

    assert result.exit_code == 0, result.output
    assert '"subjects"' in result.output
    # --json still gets the [server] label — it's an origin note, not part
    # of the machine-readable payload.
    assert "[server]" in result.output


def test_search_empty_result_hints_next_step():
    def fake_post(path, **kwargs):
        return _resp(200, {"subjects": [], "limit_applied": False})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "search", "widget"])

    assert result.exit_code == 0, result.output
    assert "No facts found for type 'widget'" in result.output


# ---------------------------------------------------------------------------
# neighbors
# ---------------------------------------------------------------------------


def test_neighbors_happy_path():
    data = {
        "nodes": [{"id": "f_1", "type": "person", "revealed": False}],
        "edges": [],
        "truncated": {"depth": False, "fanout": False, "result": False},
    }

    def fake_post(path, **kwargs):
        return _resp(200, data)

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "neighbors", "f_1"])

    assert result.exit_code == 0, result.output
    assert "[server]" in result.output
    assert "1 node(s), 0 edge(s)" in result.output
    assert "f_1" in result.output


def test_neighbors_edge_types_split_on_comma():
    captured = {}

    def fake_post(path, **kwargs):
        captured["json"] = kwargs.get("json")
        return _resp(
            200,
            {
                "nodes": [],
                "edges": [],
                "truncated": {"depth": False, "fanout": False, "result": False},
            },
        )

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "neighbors", "f_1", "--edge-types", "knows, reports_to"])

    assert result.exit_code == 0, result.output
    assert captured["json"]["edge_types"] == ["knows", "reports_to"]


def test_neighbors_404_emits_honest_hint():
    def fake_post(path, **kwargs):
        return _resp(404, {"detail": "fact_not_found"})

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "neighbors", "f_missing"])

    assert result.exit_code == 1
    assert "f_missing" in result.output
    # The hint must not claim a specific cause — it covers all three on
    # purpose (spec §5 rule 2).
    assert "the id is wrong" in result.output
    assert "you don't have access" in result.output
    assert "facts` feature isn't enabled" in result.output


def test_neighbors_truncated_notice():
    data = {
        "nodes": [{"id": "f_1", "type": "person", "revealed": False}],
        "edges": [],
        "truncated": {"depth": False, "fanout": True, "result": False},
    }

    def fake_post(path, **kwargs):
        return _resp(200, data)

    with patch("cli.commands.facts.api_post", side_effect=fake_post):
        result = runner.invoke(app, ["facts", "neighbors", "f_1"])

    assert result.exit_code == 0, result.output
    assert "truncated: fanout" in result.output


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------


def test_claims_happy_path():
    data = {
        "claims": [
            {
                "id": "c_1",
                "corpus_id": "col_a",
                "corpus_file_id": "cf_1",
                "document": {"name": "notes.md", "path": "notes.md"},
                "quote": "Alice is an engineer.",
                "attrs": {"role": "engineer"},
                "document_date": "2026-01-01",
            }
        ],
        "revealed": False,
    }

    def fake_get(path, **kwargs):
        assert path == "/api/facts/f_1/claims"
        return _resp(200, data)

    with patch("cli.commands.facts.api_get", side_effect=fake_get):
        result = runner.invoke(app, ["facts", "claims", "f_1"])

    assert result.exit_code == 0, result.output
    assert "[server]" in result.output
    assert "notes.md" in result.output
    assert "Alice is an engineer." in result.output
    assert "role=engineer" in result.output


def test_claims_404_emits_honest_hint():
    def fake_get(path, **kwargs):
        return _resp(404, {"detail": "fact_not_found"})

    with patch("cli.commands.facts.api_get", side_effect=fake_get):
        result = runner.invoke(app, ["facts", "claims", "f_missing"])

    assert result.exit_code == 1
    assert "f_missing" in result.output
    assert "facts` feature isn't enabled" in result.output


def test_claims_json_mode_emits_raw_response():
    data = {"claims": [], "revealed": False}

    def fake_get(path, **kwargs):
        return _resp(200, data)

    with patch("cli.commands.facts.api_get", side_effect=fake_get):
        result = runner.invoke(app, ["facts", "claims", "f_1", "--json"])

    assert result.exit_code == 0, result.output
    assert '"claims"' in result.output


def test_claims_revealed_suppresses_quote_and_notes_it():
    data = {
        "claims": [
            {
                "id": "c_1",
                "corpus_id": "col_a",
                "corpus_file_id": "cf_1",
                "document": {"name": "notes.md", "path": "notes.md"},
                "quote": "",
                "attrs": {},
                "document_date": None,
            }
        ],
        "revealed": True,
    }

    def fake_get(path, **kwargs):
        return _resp(200, data)

    with patch("cli.commands.facts.api_get", side_effect=fake_get):
        result = runner.invoke(app, ["facts", "claims", "f_1"])

    assert result.exit_code == 0, result.output
    assert "revealed" in result.output.lower()
    assert "quote withheld" in result.output
