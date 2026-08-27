"""``DOCUMENT_SCOPE`` — the grantable unit of a file source.

A file source (SharePoint, a Drive, an S3 prefix) is not granted as a whole:
an admin picks which sites and folders to crawl, and each of those becomes a
**scope** that a group can be given access to. Documents carry the scope they
came from, facts inherit from the documents evidencing them, so this one grant
decides what a caller eventually sees in an answer.

Deliberately backed by no new table. The selected scopes already live on the
connection row's ``config`` — the connector needs them to know what to crawl —
and the grants live in ``resource_grants`` like every other resource. Adding an
app-state table would mean a Postgres-only repository and an Alembic revision
(the A3 ratchet) to store a list the connector must hold anyway.
"""

from __future__ import annotations

import pytest

from app.resource_types import RESOURCE_TYPES, ResourceType


def _connection(name: str, scopes: list[dict], source_type: str = "sharepoint") -> dict:
    return {
        "id": f"conn-{name}",
        "name": name,
        "source_type": source_type,
        "config": {"tenant_id": "t", "client_id": "c", "scopes": scopes},
    }


@pytest.fixture
def fake_connections(monkeypatch):
    """Stand in for the connections repository the projection reads."""

    def _install(rows: list[dict]) -> None:
        monkeypatch.setattr("app.resource_types._file_source_connections", lambda: rows, raising=False)

    return _install


def test_document_scope_is_a_registered_resource_type():
    assert ResourceType.DOCUMENT_SCOPE in RESOURCE_TYPES

    spec = RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE]
    assert spec.key is ResourceType.DOCUMENT_SCOPE
    assert spec.display_name
    assert spec.description
    assert spec.id_format
    assert callable(spec.list_blocks)


def test_selected_scopes_are_offered_as_grantable_items(fake_connections):
    fake_connections(
        [
            _connection(
                "corp-files",
                [
                    {"id": "sharepoint:corp-files:site/projects", "label": "Projects", "documents": 812},
                    {"id": "sharepoint:corp-files:site/proposals", "label": "Proposals", "documents": 211},
                ],
            )
        ]
    )

    blocks = RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks()
    ids = [item["resource_id"] for block in blocks for item in block["items"]]

    assert ids == [
        "sharepoint:corp-files:site/projects",
        "sharepoint:corp-files:site/proposals",
    ]


def test_each_source_gets_its_own_block(fake_connections):
    """The page groups by connection, so an admin reads "which SharePoint"
    rather than a flat list of opaque scope ids."""
    fake_connections(
        [
            _connection("corp-files", [{"id": "sharepoint:corp-files:a", "label": "A"}]),
            _connection("archive", [{"id": "s3:archive:b", "label": "B"}], source_type="s3"),
        ]
    )

    blocks = RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks()

    assert len(blocks) == 2
    assert {block["name"] for block in blocks} == {"corp-files", "archive"}


def test_a_connection_with_nothing_selected_contributes_no_block(fake_connections):
    """A source connected but not yet scoped is not an empty heading on the
    access page — there is nothing to grant yet."""
    fake_connections([_connection("corp-files", [])])

    assert RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks() == []


def test_excluded_scopes_are_not_grantable(fake_connections):
    """A folder the admin excluded from crawling never becomes a document, so
    offering it as a grant would promise access to nothing."""
    fake_connections(
        [
            _connection(
                "corp-files",
                [
                    {"id": "sharepoint:corp-files:site/projects", "label": "Projects"},
                    {"id": "sharepoint:corp-files:site/personal", "label": "Personal", "excluded": True},
                ],
            )
        ]
    )

    ids = [
        item["resource_id"]
        for block in RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks()
        for item in block["items"]
    ]

    assert ids == ["sharepoint:corp-files:site/projects"]


def test_items_carry_a_document_count_so_the_admin_grants_informed(fake_connections):
    fake_connections(
        [_connection("corp-files", [{"id": "sharepoint:corp-files:a", "label": "Projects", "documents": 812}])]
    )

    item = RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks()[0]["items"][0]

    assert item["name"] == "Projects"
    assert "812" in item["description"]


def test_a_malformed_scope_entry_is_skipped_not_fatal(fake_connections):
    """Connection config is admin-writable and hand-editable; one bad entry
    must not take the whole access page down."""
    fake_connections(
        [
            _connection(
                "corp-files",
                [
                    {"label": "no id at all"},
                    "not even a mapping",
                    {"id": "sharepoint:corp-files:ok", "label": "Fine"},
                ],
            )
        ]
    )

    ids = [
        item["resource_id"]
        for block in RESOURCE_TYPES[ResourceType.DOCUMENT_SCOPE].list_blocks()
        for item in block["items"]
    ]

    assert ids == ["sharepoint:corp-files:ok"]
