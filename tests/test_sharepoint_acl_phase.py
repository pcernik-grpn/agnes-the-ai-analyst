"""Phase-ACL acceptance suite (2026-08-30 plan, Task 12) — the parent
spec's pre-declared Phase ACL tests
(``docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md``
§15.1): **S1-source** (the sharing set exists in SharePoint only, never in
Agnes), **S7-source** (source-layer grant revocation), and **C9** (a
permission-only Graph change is caught by the next periodic ACL re-read —
no content/extraction machinery involved anywhere). This plan's own spec is
``docs/superpowers/specs/2026-08-28-sharepoint-acl-mirroring-design.md``.

Runs entirely against the DuckDB-backed system db, mirroring
``tests/test_sharepoint_acl_sync.py``'s own fixture idiom exactly (fresh
``system.duckdb`` under a tmp ``DATA_DIR``, ``acl_mirroring.enabled=true``, a
resolvable-but-never-parsed certificate env var). Reachability is asserted
at the ``app.auth.access.accessible_collection_ids`` level — the same
function every read surface (search, neighbors, claims, ``facts_pg.py``'s
own visibility helpers) calls to resolve a caller's readable collections —
rather than through a live ``facts_repo()`` search, since that repo is
PG-only (A3 ratchet) and this file's other two tests (S7, C9) need no PG at
all. The claim-visibility form of S1 (an actual seeded claim, read back
through a real ``facts_repo().search()``/``.claims()`` call) lives
separately in ``tests/db_pg/test_sharepoint_acl_phase_pg.py`` — this file's
S1 proves the mirrored grant exists and is reachable; that file's test
proves a claim behind it is actually answerable end to end.

Graph is mocked exactly as ``tests/test_sharepoint_acl_sync.py`` does:
``graph_client.get_app_token``, ``graph_client.list_item_permissions`` and
``graph_client.list_group_transitive_members`` monkeypatched on the
``graph_client`` module itself (the sync body calls them module-qualified —
``graph_client.xxx(...)`` — specifically so this monkeypatch idiom works).
"""

from __future__ import annotations

import pytest

from connectors.sharepoint import acl_sync, graph_client

CONN_ID = "conn-phase-acl"


@pytest.fixture
def acl_env(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, feature flag on, and a
    resolvable (but never actually parsed — get_app_token is faked)
    certificate env var so ``resolve_sharepoint_settings`` doesn't raise."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")

    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


# ---------------------------------------------------------------------------
# fixture builders — direct repo writes, no HTTP (mirrors
# tests/test_sharepoint_acl_sync.py's own helpers of the same name)
# ---------------------------------------------------------------------------


def _make_connection(connection_id: str = CONN_ID) -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name="Phase ACL SharePoint",
        source_type="sharepoint",
        config={"tenant_id": "tenant-1", "client_id": "client-1", "scopes": []},
    )
    return connection_id


def _add_scope(connection_id: str, *, source_scope_id: str, collection_id: str, drive_id: str = "drive-1") -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    scopes = list(config.get("scopes") or [])
    scopes.append(
        {
            "source_scope_id": source_scope_id,
            "display_path": source_scope_id,
            "anonymize": False,
            "collection_id": collection_id,
            "drive_id": drive_id,
            "access_mode": "mirrored",
        }
    )
    config["scopes"] = scopes
    repo.update(connection_id, config=config)


def _make_collection(name: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(
        name=name, slug=name.lower().replace(" ", "-"), description=None, created_by="test"
    )


def _make_user(user_id: str, email: str) -> dict:
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name=user_id)
    return {"id": user_id, "email": email}


async def _fake_get_app_token(tenant_id, client_id, private_key, *, client_secret=""):
    return "fake-token"


def _perms_fake(mapping: dict):
    """``mapping``: ``{(drive_id, item_id): [perm, ...]}``."""

    async def fake(token, drive_id, item_id):
        return mapping.get((drive_id, item_id), [])

    return fake


def _members_fake(mapping: dict):
    """``mapping``: ``{oid: [{"mail":..., "userPrincipalName":...}, ...]}``."""

    async def fake(token, group_id):
        return mapping.get(group_id, [])

    return fake


def _group_perm(oid: str) -> dict:
    return {"id": f"perm-{oid}", "roles": ["read"], "grantedToV2": {"group": {"id": oid}}}


def _member(email: str) -> dict:
    return {"mail": email, "userPrincipalName": email}


def _audit_rows(action: str) -> list:
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action=action, limit=1000)
    return rows


def _can_reach(user: dict, collection_id: str) -> bool:
    from app.auth.access import accessible_collection_ids

    ids = accessible_collection_ids(user)
    return ids is None or collection_id in ids


# ---------------------------------------------------------------------------
# S1-source — the sharing set exists in SharePoint only, no Agnes-side grant
# ---------------------------------------------------------------------------


class TestS1SourceSharingSetOnly:
    def test_sharepoint_only_sharing_set_becomes_reachable_after_sync(self, acl_env, monkeypatch):
        """Alice starts with NO Agnes-side grant, group, or membership
        whatsoever. Fake Graph reports her as a transitive member of the
        group shared on the scope root — a sharing set that exists in
        SharePoint alone. Fails if the collection is reachable before the
        sync runs (that would mean the test fixture itself, not the sync,
        granted access) or still unreachable after it (the sync failed to
        mirror the sharing set)."""
        conn_id = _make_connection()
        col_id = _make_collection("S1 Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id)
        alice = _make_user("u-alice", "alice@example.com")

        assert _can_reach(alice, col_id) is False, "no Agnes-side grant exists yet"

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-alice")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-alice": [_member("alice@example.com")]}),
        )

        result = acl_sync.run_acl_sync({"connection_id": conn_id})
        assert result["matched"] == 1
        assert result["unmatched"] == 0

        assert _can_reach(alice, col_id) is True, "sync must mirror the SharePoint-only sharing set"


# ---------------------------------------------------------------------------
# S7-source — revocation of a source-layer (SharePoint) grant
# ---------------------------------------------------------------------------


class TestS7SourceRevocation:
    def test_group_dropped_in_graph_revokes_access_and_audits(self, acl_env, monkeypatch):
        """After the S1 state (Alice reachable via a mirrored group), fake
        Graph drops the group from the scope root entirely (as if the
        SharePoint-side share had been removed). Re-running the sync must
        both revoke Alice's reachability AND leave an auditable,
        timestamped record of the removal — the "measured" latency record
        at unit scale is that revocation lands at the next completed sync,
        never later."""
        conn_id = _make_connection()
        col_id = _make_collection("S7 Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id)
        alice = _make_user("u-alice", "alice@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-alice")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-alice": [_member("alice@example.com")]}),
        )
        acl_sync.run_acl_sync({"connection_id": conn_id})
        assert _can_reach(alice, col_id) is True

        removed_before = len(_audit_rows("sharepoint_acl.grant_removed"))

        # The SharePoint-side share is gone: the scope root now reports no
        # permissions at all.
        monkeypatch.setattr(graph_client, "list_item_permissions", _perms_fake({}))

        result = acl_sync.run_acl_sync({"connection_id": conn_id})
        assert result["errors"] == []

        assert _can_reach(alice, col_id) is False, "revocation in SharePoint must propagate on the next sync"

        removed_rows = _audit_rows("sharepoint_acl.grant_removed")
        assert len(removed_rows) == removed_before + 1
        new_row = removed_rows[0]  # newest first (query() orders timestamp DESC)
        assert new_row["resource"] == f"file_corpus:{col_id}"
        assert new_row["timestamp"] is not None, "the run report's timestamp is the measured revocation record"


# ---------------------------------------------------------------------------
# C9 — permission-only change, no content/extraction machinery involved
# ---------------------------------------------------------------------------


class TestC9PermissionOnlyChange:
    def test_permission_only_graph_change_is_caught_by_next_sync(self, acl_env, monkeypatch):
        """Two sync runs against the SAME connection/scope/collection where
        ONLY the fake Graph permissions/membership payload changes between
        them — no corpus file, no crawl, no extraction is ever touched in
        this test. Pins "the periodic ACL re-read is the floor" for a
        permission-only change (parent spec C9, §15.2): the grant delta
        (Bob's group replaced by Carol's) must be fully applied by the
        second run."""
        conn_id = _make_connection()
        col_id = _make_collection("C9 Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id)
        bob = _make_user("u-bob", "bob@example.com")
        carol = _make_user("u-carol", "carol@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-old")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-old": [_member("bob@example.com")]}),
        )
        acl_sync.run_acl_sync({"connection_id": conn_id})
        assert _can_reach(bob, col_id) is True
        assert _can_reach(carol, col_id) is False

        # ONLY the fake Graph payload changes between runs.
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-new")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-new": [_member("carol@example.com")]}),
        )

        acl_sync.run_acl_sync({"connection_id": conn_id})

        assert _can_reach(bob, col_id) is False, "the old group's grant must be removed"
        assert _can_reach(carol, col_id) is True, "the new group's grant must be added"
