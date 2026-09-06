"""PG-only HTTP round-trip tests for split-merge
(``POST /api/admin/sharepoint/connections/{id}/splits/merge``) — folding
several sibling SharePoint connections (a manually split site) back into
one.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — the
DuckDB-side typed 501 lives in
``tests/test_admin_sharepoint.py::TestSplitMergeFailsCleanOnDuckDB``.
State-union algorithm correctness (collision tie-breaks) is covered
directly by ``tests/db_pg/test_sharepoint_connection_merge_pg.py``; this
file proves the ROUTE'S OWN orchestration — auth, validation, the
precondition refusals, scope/collection/run-history folding end to end, and
that a repeat call converges.
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


def _set_scopes(conn_id: str, scopes: list) -> None:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(conn_id)
    source_connections_repo().update(conn_id, config={**(row.get("config") or {}), "scopes": scopes})


def _scope(*, source_scope_id: str, collection_id: str, display_path: str, drive_id: str | None = None) -> dict:
    return {
        "source_scope_id": source_scope_id,
        "display_path": display_path,
        "collection_id": collection_id,
        "anonymize": False,
        "access_mode": "manual",
        "drive_id": drive_id,
    }


def _put_crawl_state(pg_engine, connection_id: str, payload: dict) -> None:
    from src.repositories import sharepoint_state_repo

    sharepoint_state_repo().put(connection_id, "crawl", payload)


class TestSplitMergeRoute:
    def test_404_for_unknown_target(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        r = client.post(
            f"{BASE}/does-not-exist/splits/merge",
            json={"sibling_ids": ["x"], "target": {"name": "Merged"}},
            headers=_auth(token),
        )
        assert r.status_code == 404

    def test_requires_admin(self, tmp_path, monkeypatch, pg_engine):
        client, _ = _pg_client(tmp_path, monkeypatch, pg_engine)
        r = client.post(f"{BASE}/nope/splits/merge", json={"sibling_ids": ["x"], "target": {"name": "y"}})
        assert r.status_code == 401

    def test_both_sibling_selectors_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-both-selectors")
        r = client.post(
            f"{BASE}/{conn_id}/splits/merge",
            json={"sibling_ids": ["x"], "all_split_siblings": True, "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "both_sibling_ids_and_all_split_siblings"

    def test_neither_sibling_selector_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-no-selector")
        r = client.post(f"{BASE}/{conn_id}/splits/merge", json={"target": {"name": "y"}}, headers=_auth(token))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "sibling_ids_or_all_split_siblings_required"

    def test_both_target_fields_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-both-target")
        r = client.post(
            f"{BASE}/{conn_id}/splits/merge",
            json={"sibling_ids": ["x"], "target": {"collection_id": "c1", "name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "target_field_required"

    def test_missing_target_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-missing-target")
        r = client.post(f"{BASE}/{conn_id}/splits/merge", json={"sibling_ids": ["x"]}, headers=_auth(token))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "target_field_required"

    def test_sibling_ids_including_target_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-self-sibling")
        r = client.post(
            f"{BASE}/{conn_id}/splits/merge",
            json={"sibling_ids": [conn_id], "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "sibling_ids_includes_target"

    def test_unknown_sibling_id_is_404(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-unknown-sibling")
        r = client.post(
            f"{BASE}/{conn_id}/splits/merge",
            json={"sibling_ids": ["nope"], "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 404

    def test_no_split_siblings_found_is_400(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        conn_id = _create_connection(client, token, name="merge-lonely — part 1/1")
        r = client.post(
            f"{BASE}/{conn_id}/splits/merge",
            json={"all_split_siblings": True, "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "no_siblings_found"

    def test_dry_run_reports_scopes_state_and_collections_without_writing(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-dry-target")
        sib1 = _create_connection(client, token, name="merge-dry-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _seed_file(pg_engine, file_id="ft", corpus_id="col_target", path="t.docx")
        _seed_file(pg_engine, file_id="f1", corpus_id="col_sib1", path="s1.docx")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])
        _put_crawl_state(pg_engine, sib1, {"delta_links": {"drive:a": "u1"}})

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert body["target"]["id"] == "col_target"
        assert len(body["siblings"]) == 1
        sib_out = body["siblings"][0]
        assert sib_out["connection_id"] == sib1
        assert sib_out["scopes_moved"] == 1
        assert sib_out["state"]["crawl"]["delta_links_carried"] == 1
        assert body["blocking"] == []

        # Nothing written: scopes/collections/state all unchanged.
        from src.repositories import sharepoint_state_repo, source_connections_repo

        assert source_connections_repo().get(target)["config"]["scopes"] == [
            _scope(source_scope_id="s-t", collection_id="col_target", display_path="T")
        ]
        assert sharepoint_state_repo().get(target, "crawl") is None
        with pg_engine.connect() as conn:
            corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
        assert corpus_ids == ["col_sib1", "col_target"]

    def test_real_merge_folds_scopes_state_collections_and_runs(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-real-target")
        sib1 = _create_connection(client, token, name="merge-real-sib1")
        sib2 = _create_connection(client, token, name="merge-real-sib2")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _seed_collection(pg_engine, "col_sib2", "Sib2")
        _seed_file(pg_engine, file_id="ft", corpus_id="col_target", path="t.docx")
        _seed_file(pg_engine, file_id="f1", corpus_id="col_sib1", path="s1.docx")
        _seed_file(pg_engine, file_id="f2", corpus_id="col_sib2", path="s2.docx")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])
        _set_scopes(sib2, [_scope(source_scope_id="s-2", collection_id="col_sib2", display_path="S2")])
        _put_crawl_state(pg_engine, sib1, {"delta_links": {"drive:a": "u1"}, "ctags": {"graph:1": "c1"}})
        _put_crawl_state(pg_engine, sib2, {"delta_links": {"drive:b": "u2"}})

        from src.repositories import extraction_runs_repo

        run_id = extraction_runs_repo().start(connection_id=sib1)
        extraction_runs_repo().finish(run_id, status="done")

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1, sib2], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is False
        assert body["target"]["id"] == "col_target"
        by_sibling = {s["connection_id"]: s for s in body["siblings"]}
        assert by_sibling[sib1]["runs_repointed"] == 1
        assert by_sibling[sib2]["runs_repointed"] == 0

        from src.repositories import extraction_runs_repo as er_repo
        from src.repositories import sharepoint_crawl_items_repo, sharepoint_state_repo, source_connections_repo

        # Run history re-pointed, with provenance kept.
        run = er_repo().get(run_id)
        assert run["connection_id"] == target
        assert run["progress"]["merged_from"] == sib1

        # Crawl state unioned onto the target. `ctags` (and its two
        # siblings) live in the per-file table after the split, not the
        # blob — see migration 0110_sharepoint_crawl_items.
        target_state = sharepoint_state_repo().get(target, "crawl")
        assert target_state["delta_links"] == {"drive:a": "u1", "drive:b": "u2"}
        assert sharepoint_crawl_items_repo().get_all(target, "crawl")["ctags"] == {"graph:1": "c1"}
        # Siblings' own state rows are left untouched.
        assert sharepoint_state_repo().get(sib1, "crawl") == {
            "delta_links": {"drive:a": "u1"},
            "ctags": {"graph:1": "c1"},
        }

        # Scopes moved onto the target, all repointed to the target collection.
        target_scopes = source_connections_repo().get(target)["config"]["scopes"]
        assert {s["source_scope_id"] for s in target_scopes} == {"s-t", "s-1", "s-2"}
        assert {s["collection_id"] for s in target_scopes} == {"col_target"}

        # Siblings marked merged-away, scopes cleared, never deleted.
        sib1_row = source_connections_repo().get(sib1)
        assert sib1_row["config"]["scopes"] == []
        assert sib1_row["config"]["merged_into"]["connection_id"] == target
        assert sib1_row["source_type"] == "sharepoint"  # still exists

        # Collections folded into the target.
        with pg_engine.connect() as conn:
            corpus_ids = sorted(conn.execute(sa.text("SELECT corpus_id FROM corpus_files")).scalars().all())
            deleted_at = dict(
                conn.execute(
                    sa.text("SELECT id, deleted_at FROM file_corpora WHERE id IN ('col_sib1', 'col_sib2')")
                ).all()
            )
        assert corpus_ids == ["col_target", "col_target", "col_target"]
        assert deleted_at["col_sib1"] is not None
        assert deleted_at["col_sib2"] is not None

    def test_repeat_call_after_success_is_refused_as_already_merged(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-repeat-target")
        sib1 = _create_connection(client, token, name="merge-repeat-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])

        r1 = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r1.status_code == 200, r1.text

        r2 = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["error"] == "sibling_already_merged"

    def test_refuses_when_a_sibling_has_a_running_crawl_job(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-running-target")
        sib1 = _create_connection(client, token, name="merge-running-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])

        from src.repositories import jobs_repo

        jobs_repo().enqueue("corpus-extraction", {"connection_id": sib1})

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "crawl_or_facts_running"
        assert sib1 in r.json()["detail"]["jobs"]

    def test_refuses_when_a_sibling_carries_acl_zones(self, tmp_path, monkeypatch, pg_engine):
        from src.repositories import source_connections_repo

        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-aclzones-target")
        sib1 = _create_connection(client, token, name="merge-aclzones-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])
        sib1_row = source_connections_repo().get(sib1)
        source_connections_repo().update(
            sib1, config={**sib1_row["config"], "acl_zones": [{"zone_item_id": "z1", "status": "active"}]}
        )

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "acl_zones_present"

    def test_refuses_when_a_sibling_mirrors_a_different_audience_than_the_target(
        self, tmp_path, monkeypatch, pg_engine
    ):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-audience-target")
        sib1 = _create_connection(client, token, name="merge-audience-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _set_scopes(
            target,
            [
                {
                    **_scope(source_scope_id="s-t", collection_id="col_target", display_path="T"),
                    "access_mode": "mirrored",
                    "audience_classes": [{"name": "internal", "group_ids": ["g1"]}],
                }
            ],
        )
        _set_scopes(
            sib1,
            [
                {
                    **_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1"),
                    "access_mode": "mirrored",
                    "audience_classes": [{"name": "internal", "group_ids": ["g2"]}],
                }
            ],
        )

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "audience_class_conflict"

    def test_all_split_siblings_resolves_by_naming_convention(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="Big Site — part 1/2")
        sib1 = _create_connection(client, token, name="Big Site — part 2/2")
        unrelated = _create_connection(client, token, name="Unrelated Site")
        _seed_collection(pg_engine, "col_target", "Target")
        _seed_collection(pg_engine, "col_sib1", "Sib1")
        _set_scopes(target, [_scope(source_scope_id="s-t", collection_id="col_target", display_path="T")])
        _set_scopes(sib1, [_scope(source_scope_id="s-1", collection_id="col_sib1", display_path="S1")])

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"all_split_siblings": True, "target": {"collection_id": "col_target"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert [s["connection_id"] for s in body["siblings"]] == [sib1]
        assert unrelated not in [s["connection_id"] for s in body["siblings"]]

    def test_duplicate_scope_across_siblings_is_deduped_not_doubled(self, tmp_path, monkeypatch, pg_engine):
        client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
        target = _create_connection(client, token, name="merge-dedupe-target")
        sib1 = _create_connection(client, token, name="merge-dedupe-sib1")
        _seed_collection(pg_engine, "col_target", "Target")
        _set_scopes(
            target,
            [_scope(source_scope_id="dup", collection_id="col_target", display_path="Dup", drive_id="d1")],
        )
        _set_scopes(
            sib1,
            [_scope(source_scope_id="dup", collection_id="col_target", display_path="Dup (sib1)", drive_id="d1")],
        )

        r = client.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib1], "target": {"collection_id": "col_target"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["siblings"][0]["scopes_moved"] == 0
        assert body["siblings"][0]["scopes_deduped"] == ["dup"]

        from src.repositories import source_connections_repo

        target_scopes = source_connections_repo().get(target)["config"]["scopes"]
        assert len(target_scopes) == 1
        assert target_scopes[0]["display_path"] == "Dup"
