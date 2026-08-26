"""Tests for the ADF walk that finds an issue's attachments.

``JiraService._extract_media_from_adf`` recurses an Atlassian Document Format
body looking for the ``media`` / ``mediaInline`` / ``mediaSingle`` nodes that
reference an uploaded attachment by id. It had no tests at all. The CHANGELOG
bullet for the malformed-``attrs`` fix carries the full story; the short version
is that no live document in a 1,594-body sample reaches the path these tests
pin, so they guard the malformed-input side rather than anything in daily use.
"""

import pytest

from connectors.jira.service import JiraService


def _media(media_id: str, node_type: str = "media") -> dict:
    return {"type": node_type, "attrs": {"id": media_id}}


def _doc(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


@pytest.fixture
def scan():
    """The media walk, unbound from any Jira configuration."""
    return JiraService.__new__(JiraService)._extract_media_from_adf


def test_finds_media_nested_under_paragraphs_and_tables(scan):
    doc = _doc(
        {"type": "mediaSingle", "content": [_media("top")]},
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [{"type": "tableCell", "content": [{"type": "paragraph", "content": [_media("cell")]}]}],
                }
            ],
        },
    )
    assert scan(doc) == ["top", "cell"]


@pytest.mark.parametrize("node_type", ["media", "mediaInline", "mediaSingle"])
def test_every_media_node_type_is_collected(scan, node_type):
    assert scan(_doc({"type": "paragraph", "content": [_media("m1", node_type)]})) == ["m1"]


def test_non_media_nodes_carrying_an_id_are_ignored(scan):
    """``attrs.id`` is not exclusive to media — a mention node has one too."""
    doc = _doc({"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "accountid:1"}}]})
    assert scan(doc) == []


def test_duplicate_ids_are_kept(scan):
    """Order and multiplicity are the caller's to reconcile, not this walk's."""
    assert scan(_doc(_media("m1"), _media("m1"))) == ["m1", "m1"]


@pytest.mark.parametrize("attrs", ["broken", None, 7, []])
def test_a_non_object_attrs_is_skipped_and_the_walk_continues(scan, attrs):
    """The regression: this used to raise and lose ``survivor`` along with it."""
    doc = _doc({"type": "media", "attrs": attrs}, _media("survivor"))
    assert scan(doc) == ["survivor"]


@pytest.mark.parametrize("attrs", [{"id": None}, {"id": ""}, {}])
def test_a_media_node_with_no_usable_id_records_nothing(scan, attrs):
    """A well-formed ``attrs`` that simply has no id — a separate path from above."""
    doc = _doc({"type": "media", "attrs": attrs}, _media("survivor"))
    assert scan(doc) == ["survivor"]


@pytest.mark.parametrize("content", ["not a list", None, 7])
def test_a_malformed_content_ends_the_descent_quietly(scan, content):
    assert scan({"type": "doc", "content": content}) == []


@pytest.mark.parametrize("node", [None, "text", 7, {}, []])
def test_input_with_no_media_returns_empty(scan, node):
    assert scan(node) == []
