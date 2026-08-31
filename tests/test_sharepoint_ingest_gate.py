"""Unit tests for the SharePoint source-ACL ingest gate (2026-08-31 plan,
Task 5 — ``connectors/sharepoint/ingest_gate.py``).

Builds a connection/scope/zone config directly via ``source_connections_repo``
(the same direct-repo-write idiom ``tests/test_sharepoint_acl_sync.py`` uses),
never through the wizard's HTTP endpoint. No Graph calls happen here at all —
this module only reads already-persisted config.
"""

from __future__ import annotations

import pytest

from connectors.sharepoint.ingest_gate import source_acl_index_for_collection, source_acl_refusal

CONN_ID = "conn-gate-1"


def _make_connection(connection_id: str = CONN_ID) -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name="Test SharePoint",
        source_type="sharepoint",
        config={"tenant_id": "tenant-1", "client_id": "client-1", "scopes": []},
    )
    return connection_id


def _add_scope(
    connection_id: str,
    *,
    source_scope_id: str,
    collection_id: str,
    display_path: str = "Site/Documents",
    drive_id: str = "d1",
    excluded_subtrees: "list[dict] | None" = None,
) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    scopes = list(config.get("scopes") or [])
    scopes.append(
        {
            "source_scope_id": source_scope_id,
            "display_path": display_path,
            "anonymize": False,
            "collection_id": collection_id,
            "drive_id": drive_id,
            "access_mode": "mirrored",
            "excluded_subtrees": excluded_subtrees or [],
        }
    )
    config["scopes"] = scopes
    repo.update(connection_id, config=config)


def _add_zone(
    connection_id: str,
    *,
    zone_item_id: str,
    parent_scope_id: str,
    collection_id: str,
    rel_path: str,
    drive_id: str = "d1",
    status: str = "active",
) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    zones = list(config.get("acl_zones") or [])
    zones.append(
        {
            "zone_item_id": zone_item_id,
            "parent_scope_id": parent_scope_id,
            "drive_id": drive_id,
            "name": zone_item_id,
            "display_path": f"{parent_scope_id}/{rel_path}",
            "rel_path": rel_path,
            "collection_id": collection_id,
            "detected_at": "2026-08-31T00:00:00+00:00",
            "status": status,
        }
    )
    config["acl_zones"] = zones
    repo.update(connection_id, config=config)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, ``acl_mirroring`` on, one
    mirrored scope (``col_parent``) carrying a folder exclusion (``Secret``)
    and a file exclusion (``open/f.docx``), and one active permission zone
    (``col_zone``, rel_path ``Legal``) rooted under that same scope. Mirrors
    the plan's Task 5 Step 1 fixture exactly."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")

    from src.db import close_system_db, get_system_db

    get_system_db()

    conn_id = _make_connection()
    _add_scope(
        conn_id,
        source_scope_id="root-1",
        collection_id="col_parent",
        display_path="Site/Documents",
        excluded_subtrees=[
            {
                "item_id": "X",
                "path": "Secret",
                "rel_path": "Secret",
                "kind": "folder",
                "detected_at": "2026-08-31T00:00:00+00:00",
            },
            {
                "item_id": "F",
                "path": "open/f.docx",
                "rel_path": "open/f.docx",
                "kind": "file",
                "detected_at": "2026-08-31T00:00:00+00:00",
            },
        ],
    )
    _add_zone(
        conn_id,
        zone_item_id="Z",
        parent_scope_id="root-1",
        collection_id="col_zone",
        rel_path="Legal",
    )

    yield conn_id

    close_system_db()


def test_folder_exclusion_blocks_descendants(gate_env):
    idx = source_acl_index_for_collection("col_parent")
    assert idx is not None
    assert source_acl_refusal(idx, path="Secret/inner/doc.docx", stable_id="graph:other") == "source_acl_excluded"
    # component-safe: "SecretishName" is not "Secret" nor "Secret/..."
    assert source_acl_refusal(idx, path="SecretishName/doc.docx", stable_id=None) is None


def test_file_exclusion_blocks_by_stable_id(gate_env):
    idx = source_acl_index_for_collection("col_parent")
    assert idx is not None
    assert source_acl_refusal(idx, path="open/f.docx", stable_id="graph:F") == "source_acl_excluded"


def test_zone_content_refused_in_parent_but_allowed_in_zone(gate_env):
    parent = source_acl_index_for_collection("col_parent")
    zone = source_acl_index_for_collection("col_zone")
    assert parent is not None
    assert zone is not None
    assert source_acl_refusal(parent, path="Legal/contract.docx", stable_id=None) == "source_acl_zone_mismatch"
    assert source_acl_refusal(zone, path="Legal/contract.docx", stable_id=None) is None


def test_non_sharepoint_collection_is_untouched(gate_env):
    assert source_acl_index_for_collection("col_unrelated") is None


def test_index_is_none_when_acl_mirroring_disabled(gate_env, monkeypatch):
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "false")
    assert source_acl_index_for_collection("col_parent") is None


def test_dissolved_zone_collection_is_not_indexed(gate_env):
    """An active zone's collection is indexed; once dissolved (Task 3/6's
    ``status`` flip) it is no longer treated as a live permission zone —
    the ingest gate leaves it to the sweep's own retroactive cleanup."""
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(gate_env)
    config = dict(row.get("config") or {})
    zones = [dict(z) for z in config.get("acl_zones") or []]
    for z in zones:
        z["status"] = "dissolved"
    config["acl_zones"] = zones
    source_connections_repo().update(gate_env, config=config)

    assert source_acl_index_for_collection("col_zone") is None
    # the parent no longer treats the dissolved zone as foreign either.
    parent = source_acl_index_for_collection("col_parent")
    assert parent is not None
    assert source_acl_refusal(parent, path="Legal/contract.docx", stable_id=None) is None


def test_nested_zone_only_carries_its_own_exclusions(gate_env):
    """A file excluded elsewhere in the scope's tree (``open/f.docx``, NOT
    nested under the zone's own ``Legal`` prefix) must not leak into the
    zone's own index."""
    zone = source_acl_index_for_collection("col_zone")
    assert zone is not None
    assert source_acl_refusal(zone, path="open/f.docx", stable_id="graph:F") is None
