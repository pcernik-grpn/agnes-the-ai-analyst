"""S1-source, claim-visibility form (2026-08-30 plan, Task 12; parent spec
``docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md``
§15.1). Companion to ``tests/test_sharepoint_acl_phase.py``'s reachability-
level S1/S7/C9 suite: that file proves ``run_acl_sync`` mirrors a
SharePoint-only sharing set into a reachable ``resource_grants`` row (via
``app.auth.access.accessible_collection_ids``) using the DuckDB backend, so
its S7 (revocation) and C9 (permission-only change) tests need no Postgres
at all. THIS file proves the claim actually behind that grant is answerable
end to end — a real seeded claim, read back through a real
``facts_repo().search()``/``.claims()`` call — which needs the PG-only
``facts`` repo (A3 ratchet, no DuckDB sibling; see ``src/repositories/
__init__.py``'s ``_REGISTRY["facts"]``), hence the split (same pattern as
``tests/test_api_facts.py`` / ``tests/db_pg/test_facts_read_pg.py``).

``pg_env`` mirrors ``tests/db_pg/test_facts_audience.py``'s own fixture
(Alembic upgrade to head + seeded system groups) plus the ACL-sync env
knobs from ``tests/test_sharepoint_acl_sync.py``'s ``acl_env`` fixture —
this test needs both halves live in the same process: ``run_acl_sync``
writes through the PG-backed RBAC repos (``source_connections``,
``user_groups``, ``user_group_members``, ``resource_grants`` — all frozen
pre-A3 dual-backend pairs), and ``facts_repo()`` reads the resulting grant
back through a real claim.

Graph is mocked exactly as ``tests/test_sharepoint_acl_sync.py`` does:
``graph_client.get_app_token``, ``graph_client.list_item_permissions`` and
``graph_client.list_group_transitive_members`` monkeypatched on the
``graph_client`` module itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connectors.sharepoint import acl_sync, graph_client

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres wired as the active backend, ACL mirroring
    on (mirrors ``test_facts_audience.py``'s own ``pg_env`` + ``test_
    sharepoint_acl_sync.py``'s ``acl_env`` knobs)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")

    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


@pytest.fixture
def facts(pg_env):
    from src.repositories.facts_pg import FactsPgRepository

    import src.db_pg as db_pg

    return FactsPgRepository(db_pg.get_engine())


def _make_connection(connection_id: str = "conn-s1-pg") -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name="S1 PG SharePoint",
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


def _make_user(user_id: str, email: str) -> dict:
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name=user_id)
    return {"id": user_id, "email": email}


async def _fake_get_app_token(tenant_id, client_id, private_key, *, client_secret=""):
    return "fake-token"


def _perms_fake(mapping: dict):
    async def fake(token, drive_id, item_id):
        return mapping.get((drive_id, item_id), [])

    return fake


def _members_fake(mapping: dict):
    async def fake(token, group_id):
        return mapping.get(group_id, [])

    return fake


def _group_perm(oid: str) -> dict:
    return {"id": f"perm-{oid}", "roles": ["read"], "grantedToV2": {"group": {"id": oid}}}


def test_sharepoint_only_sharing_set_makes_a_seeded_claim_answerable(pg_env, facts, monkeypatch):
    """Alice starts with NO Agnes-side grant, group, or membership at all.
    A claim already sits in the mirrored scope's collection. Fake Graph
    reports her as a transitive member of the group shared on the scope
    root — a sharing set that exists in SharePoint alone. Fails if the
    claim is answerable before the sync (the fixture, not the sync, granted
    access) or still unanswerable after it (the sync failed to mirror the
    sharing set all the way to a real read)."""
    from src.repositories import corpus_files_repo, file_corpora_repo, users_repo

    users_repo().create(id="uploader1", email="uploader1@test.com", name="Uploader")
    col_id = file_corpora_repo().create(
        name="S1 Source Col", slug="s1-source-col", description=None, created_by="uploader1"
    )
    conn_id = _make_connection()
    _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id)
    alice = _make_user("u-alice", "alice@example.com")

    file_id = corpus_files_repo().add(
        corpus_id=col_id,
        filename="s1.md",
        sha256="sha1",
        file_type="markdown",
        size_bytes=10,
        storage_path=None,
    )

    fact_id = facts.create_fact(type="engagement")
    facts.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:s1-target")
    facts.add_claim(
        fact_id=fact_id,
        corpus_file_id=file_id,
        corpus_id=col_id,
        file_sha256="sha1",
        quote="Sharing set exists in SharePoint only.",
        attrs={"status": "active"},
    )

    result_before = facts.search(alice, type="engagement")
    assert fact_id not in {s["id"] for s in result_before["subjects"]}, "no Agnes-side grant exists yet"

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake({("drive-1", "scope-1"): [_group_perm("g-alice")]}),
    )
    monkeypatch.setattr(
        graph_client,
        "list_group_transitive_members",
        _members_fake({"g-alice": [{"mail": "alice@example.com", "userPrincipalName": "alice@example.com"}]}),
    )

    result = acl_sync.run_acl_sync({"connection_id": conn_id})
    assert result["matched"] == 1

    result_after = facts.search(alice, type="engagement")
    assert fact_id in {s["id"] for s in result_after["subjects"]}, "sync must mirror the sharing set into a real read"

    claims = facts.claims(alice, fact_id)
    assert claims["claims"][0]["quote"] == "Sharing set exists in SharePoint only."


# ---------------------------------------------------------------------------
# 2026-08-31 plan, Task 6 — retroactive cleanup's stable-id matching path.
# PG-only: ``corpus_file_sources`` has no DuckDB sibling (A3 ratchet, see
# ``src/repositories/corpus_file_sources_pg.py``'s own module docstring),
# so this half of ``_cleanup_connection_content``'s matcher (as opposed to
# the path-prefix half, covered on DuckDB by
# ``tests/test_sharepoint_subtree_sweep.py::TestRetroactiveCleanup``) can
# only be exercised here.
# ---------------------------------------------------------------------------


def test_stable_id_match_purges_file_outside_excluded_folder(pg_env, monkeypatch):
    """A file's PATH sits outside any excluded folder (so the cleanup's
    path-prefix matcher would NOT catch it), but its Graph item id is
    itself the excluded (unique-permission) file — the crawler could have
    placed it under a differently-named local path than the one Graph
    reports. Fails if the file survives cleanup (stable-id matching
    silently skipped, e.g. because ``corpus_file_sources_repo()`` raised
    and was not caught) or if the path-based match would have caught it
    anyway (which would make this assertion vacuous)."""
    from src.repositories import corpus_file_sources_repo, corpus_files_repo, file_corpora_repo, users_repo

    users_repo().create(id="uploader1", email="uploader1@test.com", name="Uploader")
    col_id = file_corpora_repo().create(
        name="Stable Id Col", slug="stable-id-col", description=None, created_by="uploader1"
    )
    conn_id = _make_connection("conn-s6-pg")
    _add_scope(conn_id, source_scope_id="root", collection_id=col_id)

    file_id = corpus_files_repo().add(
        corpus_id=col_id,
        filename="F.docx",
        sha256="sha-f",
        file_type="text/plain",
        size_bytes=1,
        storage_path=None,
        path="open/F.docx",  # NOT under the excluded item's own rel_path ("F.docx")
    )
    corpus_file_sources_repo().upsert(corpus_file_id=file_id, corpus_id=col_id, source_stable_id="graph:F")

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

    async def fake_children(token, drive_id, item_id):
        if item_id == "root":
            return [{"id": "F", "name": "F.docx", "is_folder": False, "child_count": 0}]
        raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

    async def fake_probe(token, drive_id, item_ids):
        return {i: True for i in item_ids}

    monkeypatch.setattr(graph_client, "list_item_children", fake_children)
    monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

    result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

    assert result["errors"] == []
    assert corpus_files_repo().list_for_corpus(col_id) == [], (
        "the file must be purged by its Graph stable id even though its local path "
        "falls outside the excluded item's own rel_path"
    )
