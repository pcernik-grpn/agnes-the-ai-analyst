"""PG-only HTTP round-trip tests for SharePoint collection consolidation
(``POST /api/admin/sharepoint/connections/{id}/collections/consolidate``).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". The DuckDB-side typed
501 lives in
``tests/test_admin_sharepoint.py::TestConsolidateCollectionsFailsCleanOnDuckDB``.
Repository-level behavior (preview counts, the per-table move, grants
union, the path/stable-id conflict refusal) is covered directly by
``tests/db_pg/test_sharepoint_collection_consolidation_pg.py``; this file
proves the ROUTE wiring — auth, validation, dry-run-vs-real, the
cross-connection 409 — on a realistic two-scope fixture (files, chunks,
claims, grants).
"""

from __future__ import annotations


import sqlalchemy as sa

from tests.db_pg._parity_sweep_util import build_seeded_client

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    client, token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    return client, token


def _create_connection(client, token, *, name="corp-sharepoint") -> str:
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_collection(pg_engine, corpus_id: str, name: str) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": corpus_id, "slug": corpus_id, "name": name, "by": "admin1"},
        )


def _seed_file(pg_engine, *, file_id: str, corpus_id: str, path: str | None = None) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status, path) "
                "VALUES (:id, :corpus_id, :filename, 'sha1', 'indexed', :path)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "path": path},
        )


def _seed_chunk(pg_engine, *, corpus_id: str, file_id: str) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES (:id, :corpus_id, :file_id, 0, 'hello')"
            ),
            {"id": f"ck_{file_id}", "corpus_id": corpus_id, "file_id": file_id},
        )


def _seed_claim(pg_engine, *, claim_id: str, corpus_id: str, corpus_file_id: str) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO facts (id, type) VALUES (:id, 'company') ON CONFLICT DO NOTHING"),
            {"id": "fact_shared"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                "VALUES (:id, 'fact_shared', :file_id, :corpus_id, 'sha', 'quote', :qh)"
            ),
            {"id": claim_id, "file_id": corpus_file_id, "corpus_id": corpus_id, "qh": claim_id},
        )


def _set_scopes(conn_id: str, scopes: list) -> None:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(conn_id)
    source_connections_repo().update(conn_id, config={**(row.get("config") or {}), "scopes": scopes})


def _set_split(conn_id: str, split: dict) -> None:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(conn_id)
    source_connections_repo().update(conn_id, config={**(row.get("config") or {}), "split": split})


def _scope(*, source_scope_id: str, collection_id: str, display_path: str) -> dict:
    return {
        "source_scope_id": source_scope_id,
        "display_path": display_path,
        "collection_id": collection_id,
        "anonymize": False,
        "access_mode": "manual",
    }


class TestConsolidateCollectionsRoute:
    def test_404_for_unknown_connection(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        r = client.post(
            f"{BASE}/does-not-exist/collections/consolidate",
            json={"target": {"name": "x"}},
            headers=_auth(token),
        )
        assert r.status_code == 404

    def test_requires_admin(self, tmp_path, monkeypatch, pg_engine):
        client, _ = _pg_client(tmp_path, monkeypatch, pg_engine)
        r = client.post(
            f"{BASE}/nope/collections/consolidate",
            json={"target": {"name": "x"}},
        )
        assert r.status_code == 401

    def test_both_target_fields_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-both")
        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target_collection_id": "col_x", "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "both_target_collection_id_and_target"

    def test_neither_target_field_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-neither")
        r = client.post(f"{BASE}/{conn_id}/collections/consolidate", json={}, headers=_auth(token))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "target_required"

    def test_unknown_target_collection_id_is_404(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-404-target")
        _set_scopes(conn_id, [_scope(source_scope_id="s1", collection_id="col_a", display_path="A")])
        _seed_collection(pg_engine, "col_a", "A")
        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target_collection_id": "col_doesnotexist"},
            headers=_auth(token),
        )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "collection_not_found"

    def test_nothing_to_consolidate_is_400_and_mints_no_orphan(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-nothing")
        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "Merged"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "nothing_to_consolidate"

        from src.repositories import file_corpora_repo

        assert file_corpora_repo().list(search="Merged") == []

    def test_dry_run_lists_sources_and_file_counts_and_changes_nothing(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-dry-run")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_b", "Scope B")
        _seed_file(pg_engine, file_id="fa", corpus_id="col_a", path="a.docx")
        _seed_file(pg_engine, file_id="fb1", corpus_id="col_b", path="b1.docx")
        _seed_file(pg_engine, file_id="fb2", corpus_id="col_b", path="b2.docx")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _scope(source_scope_id="s-b", collection_id="col_b", display_path="B"),
            ],
        )

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "One Big Site"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert body["target"]["name"] == "One Big Site"
        # A NAMED target is not minted during a dry run — no real id yet,
        # never a partial write for what is supposed to be a pure read.
        assert body["target"]["id"] is None
        by_id = {s["id"]: s["file_count"] for s in body["sources"]}
        assert by_id == {"col_a": 1, "col_b": 2}
        assert body["blocking"] == []

        # Nothing moved: still 3 files split across the two ORIGINAL corpora.
        with pg_engine.connect() as conn:
            rows = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
        assert rows == ["col_a", "col_b", "col_b"]

        from src.repositories import source_connections_repo

        scopes = source_connections_repo().get(conn_id)["config"]["scopes"]
        assert {s["collection_id"] for s in scopes} == {"col_a", "col_b"}

        # No orphan collection minted by the preview itself.
        from src.repositories import file_corpora_repo

        assert file_corpora_repo().list(search="One Big Site") == []

    def test_real_merge_moves_everything_unions_grants_and_soft_deletes_sources(self, tmp_path, monkeypatch, pg_engine):
        from app.resource_types import ResourceType
        from src.repositories import resource_grants_repo, source_connections_repo, user_groups_repo

        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-real")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_b", "Scope B")
        _seed_file(pg_engine, file_id="fa", corpus_id="col_a", path="a.docx")
        _seed_file(pg_engine, file_id="fb", corpus_id="col_b", path="b.docx")
        _seed_chunk(pg_engine, corpus_id="col_a", file_id="fa")
        _seed_chunk(pg_engine, corpus_id="col_b", file_id="fb")
        _seed_claim(pg_engine, claim_id="cl_a", corpus_id="col_a", corpus_file_id="fa")
        _seed_claim(pg_engine, claim_id="cl_b", corpus_id="col_b", corpus_file_id="fb")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _scope(source_scope_id="s-b", collection_id="col_b", display_path="B"),
            ],
        )

        group_a = user_groups_repo().ensure(name="consolidate-real-group-a", created_by="test")["id"]
        group_b = user_groups_repo().ensure(name="consolidate-real-group-b", created_by="test")["id"]
        resource_grants_repo().ensure_grant(group_a, ResourceType.COLLECTION.value, "col_a", assigned_by="admin1")
        resource_grants_repo().ensure_grant(group_b, ResourceType.COLLECTION.value, "col_b", assigned_by="admin1")

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "One Big Site (real)"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is False
        target_id = body["target"]["id"]
        assert body["scopes_repointed"] == 2
        assert body["files_moved"] == 2
        assert body["chunks_moved"] == 2
        assert body["claims_moved"] == 2
        assert body["grants_merged"] == 2

        with pg_engine.connect() as conn:
            file_corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
            chunk_corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_chunks")).scalars().all())
            claim_corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM claims")).scalars().all())
        assert file_corpus_ids == [target_id, target_id]
        assert chunk_corpus_ids == [target_id, target_id]
        assert claim_corpus_ids == [target_id, target_id]

        grants = resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
        target_groups = {g["group_id"] for g in grants if g["resource_id"] == target_id}
        assert target_groups == {group_a, group_b}
        assert not any(g["resource_id"] in ("col_a", "col_b") for g in grants)

        scopes = source_connections_repo().get(conn_id)["config"]["scopes"]
        assert {s["collection_id"] for s in scopes} == {target_id}

        with pg_engine.connect() as conn:
            deleted_at = dict(
                conn.execute(sa.text("SELECT id, deleted_at FROM file_corpora WHERE id IN ('col_a', 'col_b')")).all()
            )
        assert deleted_at["col_a"] is not None
        assert deleted_at["col_b"] is not None

    def test_refuses_when_a_source_is_referenced_by_another_connection(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn1 = _create_connection(client, token, name="consolidate-cross-1")
        conn2 = _create_connection(client, token, name="consolidate-cross-2")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_shared", "Shared")
        _set_scopes(
            conn1,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _scope(source_scope_id="s-shared", collection_id="col_shared", display_path="Shared"),
            ],
        )
        _set_scopes(conn2, [_scope(source_scope_id="s-other", collection_id="col_a", display_path="A (conn2)")])

        r = client.post(
            f"{BASE}/{conn1}/collections/consolidate",
            json={"target_collection_id": "col_shared", "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "collection_referenced_by_other_connection"

        # Nothing moved.
        with pg_engine.connect() as conn:
            deleted_at = conn.execute(sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_a'")).scalar()
        assert deleted_at is None

    def test_conflict_from_the_repo_surfaces_as_409_and_applies_nothing(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-conflict")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_file(pg_engine, file_id="fa", corpus_id="col_a", path="same.docx")
        _seed_file(pg_engine, file_id="ft", corpus_id="col_target", path="same.docx")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _scope(source_scope_id="s-target", collection_id="col_target", display_path="Target"),
            ],
        )

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target_collection_id": "col_target", "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "consolidation_conflict"
        assert r.json()["detail"]["kind"] == "corpus_files.path"


class TestConsolidateCollectionsIncludeSplitSiblings:
    """``include_split_siblings: true`` — widens the fold from THIS
    connection alone to its whole site-split family (see
    ``app.api.admin_sharepoint._split_family_connection_ids``): one call
    folds the collections of a site split into N parts into a single
    target, instead of N repeats with the same target."""

    def _family(self, client, token, pg_engine) -> tuple[str, str]:
        """A two-part split: `part1` (the connection this call is made
        on) carries `config.split.parent_connection_id == "parent-x"`
        (a parent id that names no REAL connection — the common case once
        the original, un-split connection has been deleted); `part2` is
        the sibling, sharing that same parent id. Each has its own scope
        collection with one file."""
        part1 = _create_connection(client, token, name="split-sibling-part1")
        part2 = _create_connection(client, token, name="split-sibling-part2")
        _seed_collection(pg_engine, "col_1", "Part 1")
        _seed_collection(pg_engine, "col_2", "Part 2")
        _seed_file(pg_engine, file_id="f1", corpus_id="col_1", path="a.docx")
        _seed_file(pg_engine, file_id="f2", corpus_id="col_2", path="b.docx")
        _set_scopes(part1, [_scope(source_scope_id="s-1", collection_id="col_1", display_path="A")])
        _set_scopes(part2, [_scope(source_scope_id="s-2", collection_id="col_2", display_path="B")])
        _set_split(
            part1, {"parent_connection_id": "parent-x", "part": 1, "n": 2, "created_at": "2026-09-03T00:00:00+00:00"}
        )
        _set_split(
            part2, {"parent_connection_id": "parent-x", "part": 2, "n": 2, "created_at": "2026-09-03T00:00:00+00:00"}
        )
        return part1, part2

    def test_default_false_only_folds_this_connection_not_its_siblings(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        part1, part2 = self._family(client, token, pg_engine)
        # Give part1 a SECOND collection of its own — the target excludes
        # itself, so `col_1b` is the only candidate source WITHOUT
        # siblings; part2's `col_2` must never appear.
        _seed_collection(pg_engine, "col_1b", "Part 1b")
        _seed_file(pg_engine, file_id="f1b", corpus_id="col_1b", path="c.docx")
        _set_scopes(
            part1,
            [
                _scope(source_scope_id="s-1", collection_id="col_1", display_path="A"),
                _scope(source_scope_id="s-1b", collection_id="col_1b", display_path="Ab"),
            ],
        )

        r = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target_collection_id": "col_1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_ids"] == [part1]
        assert [s["id"] for s in body["sources"]] == ["col_1b"]

    def test_dry_run_reports_the_whole_family_and_changes_nothing(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        part1, part2 = self._family(client, token, pg_engine)

        r = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target": {"name": "Whole Site"}, "include_split_siblings": True},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert sorted(body["connection_ids"]) == sorted([part1, part2])
        by_id = {s["id"]: s["file_count"] for s in body["sources"]}
        assert by_id == {"col_1": 1, "col_2": 1}
        assert body["blocking"] == []
        assert body["running"] == []
        # A NAMED target is not minted during a dry run.
        assert body["target"]["id"] is None

        with pg_engine.connect() as conn:
            rows = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
        assert rows == ["col_1", "col_2"]

    def test_real_merge_folds_every_part_and_repoints_every_connection(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        part1, part2 = self._family(client, token, pg_engine)

        r = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target": {"name": "Whole Site"}, "include_split_siblings": True, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        target_id = body["target"]["id"]
        assert body["scopes_repointed"] == 2
        assert sorted(body["connection_ids"]) == sorted([part1, part2])

        with pg_engine.connect() as conn:
            file_corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
        assert file_corpus_ids == [target_id, target_id]

        from src.repositories import source_connections_repo

        for cid in (part1, part2):
            scopes = source_connections_repo().get(cid)["config"]["scopes"]
            assert {s["collection_id"] for s in scopes} == {target_id}

    def test_a_sibling_scope_is_never_treated_as_foreign(self, tmp_path, monkeypatch, pg_engine):
        """Without `include_split_siblings`, `part2`'s own scope routing to
        `col_2` would make `col_2` a "foreign" collection blocking the
        fold — that guard exists to protect a genuinely UNRELATED
        connection's crawl target, never a sibling of the SAME split being
        folded together on purpose."""
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        part1, part2 = self._family(client, token, pg_engine)
        # Give part1 a SECOND collection of its own so there is something
        # to consolidate even without pulling in part2's.
        _seed_collection(pg_engine, "col_1b", "Part 1b")
        _seed_file(pg_engine, file_id="f1b", corpus_id="col_1b", path="c.docx")
        _set_scopes(
            part1,
            [
                _scope(source_scope_id="s-1", collection_id="col_1", display_path="A"),
                _scope(source_scope_id="s-1b", collection_id="col_1b", display_path="Ab"),
            ],
        )

        r = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target_collection_id": "col_1", "include_split_siblings": True},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        # `col_2` (part2's own) never appears as blocking, because part2 is
        # part of the SAME family this call already covers.
        assert r.json()["blocking"] == []

    def test_refuses_when_a_sibling_has_a_running_crawl(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        part1, part2 = self._family(client, token, pg_engine)

        from src.repositories import jobs_repo

        jobs_repo().enqueue("corpus-extraction", {"connection_id": part2}, idempotency_key=f"corpus-extraction:{part2}")

        preview = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target": {"name": "Whole Site"}, "include_split_siblings": True},
            headers=_auth(token),
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["running"] == [part2]

        real = client.post(
            f"{BASE}/{part1}/collections/consolidate",
            json={"target": {"name": "Whole Site"}, "include_split_siblings": True, "dry_run": False},
            headers=_auth(token),
        )
        assert real.status_code == 409, real.text
        detail = real.json()["detail"]
        assert detail["error"] == "sibling_crawl_running"
        assert detail["connection_ids"] == [part2]

        # Nothing touched — the running-crawl refusal fires BEFORE the merge.
        with pg_engine.connect() as conn:
            rows = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
        assert rows == ["col_1", "col_2"]


def _mirrored_scope(*, source_scope_id: str, collection_id: str, display_path: str) -> dict:
    return {
        **_scope(source_scope_id=source_scope_id, collection_id=collection_id, display_path=display_path),
        "access_mode": "mirrored",
    }


class TestConsolidateRefusesMirroredSources:
    """2026-09 fix: consolidation unions every source collection's grants
    onto the target, which would silently widen a secure-folder's
    sentinel-owned grant into a whole-site grant — refused outright until
    an audience-zone design exists."""

    def test_real_merge_refuses_when_a_source_scope_is_mirrored(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-mirrored")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_secure", "Secure Folder")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _mirrored_scope(source_scope_id="s-secure", collection_id="col_secure", display_path="Secure"),
            ],
        )

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "One Big Site (mirrored)"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409, r.text
        body = r.json()["detail"]
        assert body["error"] == "mirrored_scope_in_sources"
        assert body["mirrored_sources"] == [{"collection_id": "col_secure", "source_scope_id": "s-secure"}]

        # Nothing moved, nothing minted.
        with pg_engine.connect() as conn:
            deleted_at = conn.execute(sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_secure'")).scalar()
        assert deleted_at is None

        from src.repositories import file_corpora_repo

        assert file_corpora_repo().list(search="One Big Site (mirrored)") == []

    def test_dry_run_surfaces_mirrored_sources_without_refusing(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-mirrored-dry")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_secure", "Secure Folder")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _mirrored_scope(source_scope_id="s-secure", collection_id="col_secure", display_path="Secure"),
            ],
        )

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "Dry Run Mirrored"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert body["mirrored_sources"] == [{"collection_id": "col_secure", "source_scope_id": "s-secure"}]

    def test_target_itself_being_mirrored_is_not_refused(self, tmp_path, monkeypatch, pg_engine):
        """Only SOURCE collections are checked — folding an ordinary manual
        scope's collection INTO a target that happens to be mirrored is a
        different (currently unguarded) shape this fix does not touch."""
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="consolidate-mirrored-target")
        _seed_collection(pg_engine, "col_a", "Scope A")
        _seed_collection(pg_engine, "col_mirrored_target", "Mirrored Target")
        _set_scopes(
            conn_id,
            [
                _scope(source_scope_id="s-a", collection_id="col_a", display_path="A"),
                _mirrored_scope(source_scope_id="s-target", collection_id="col_mirrored_target", display_path="Target"),
            ],
        )

        r = client.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target_collection_id": "col_mirrored_target", "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["scopes_repointed"] == 1

        with pg_engine.connect() as conn:
            deleted_at = conn.execute(sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_a'")).scalar()
        assert deleted_at is not None
