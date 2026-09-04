"""Web UI: /library/{slug} file cards must badge needs_review/rejected files
and surface the failure reason (not render bare, unstyled status text)."""

from __future__ import annotations

from pathlib import Path


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_corpus(slug: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="admin1")


def _add_file(corpus_id: str, filename: str, file_type: str, path: str) -> str:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256="sha_" + filename,
        file_type=file_type,
        size_bytes=Path(path).stat().st_size if Path(path).exists() else 0,
        storage_path=path,
    )


def test_library_detail_shows_needs_review_reason(seeded_app, tmp_path):
    """File card must badge needs_review and surface the reason text."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "empty.csv"
    doc.write_text("col_a,col_b\n")

    corpus_id = _new_corpus("needs-review-ui")
    file_id = _add_file(corpus_id, "empty.csv", "csv", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="needs_review",
        detail={"reason": "extraction produced empty table"},
    )

    r = seeded_app["client"].get("/library/needs-review-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "needs_review" in r.text
    assert "extraction produced empty table" in r.text


def test_library_detail_shows_rejected_reason(seeded_app, tmp_path):
    """Rejected files also badge + surface their reason."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "bad.docx"
    doc.write_text("not really a docx")

    corpus_id = _new_corpus("rejected-ui")
    file_id = _add_file(corpus_id, "bad.docx", "docx", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="rejected",
        detail={"reason": "unsupported or corrupt file"},
    )

    r = seeded_app["client"].get("/library/rejected-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "rejected" in r.text
    assert "unsupported or corrupt file" in r.text


def test_library_detail_admin_sees_reingest_button(seeded_app, tmp_path):
    """Admin viewing a needs_review file must see a re-ingest button."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "review.csv"
    doc.write_text("col_a,col_b\n")

    corpus_id = _new_corpus("reingest-ui")
    file_id = _add_file(corpus_id, "review.csv", "csv", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="needs_review",
        detail={"reason": "extraction produced empty table"},
    )

    r = seeded_app["client"].get("/library/reingest-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "data-reingest" in r.text


def test_library_detail_indexed_file_has_no_reason(seeded_app, tmp_path):
    """A clean, indexed file must not render a reason block."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "good.csv"
    doc.write_text("col_a,col_b\n1,2\n")

    corpus_id = _new_corpus("indexed-ui")
    file_id = _add_file(corpus_id, "good.csv", "csv", str(doc))
    corpus_files_repo().set_status(file_id, status="indexed", detail=None)

    r = seeded_app["client"].get("/library/indexed-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "indexed" in r.text
    assert '<div class="file__reason">' not in r.text


# ---------------------------------------------------------------------------
# Owner-facing management controls (edit / delete) — who is offered them
# ---------------------------------------------------------------------------


def _seed_grant(corpus_id: str, user_id: str) -> None:
    """Give ``user_id`` read access via Everyone, membership included."""
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    grp = user_groups_repo().get_by_name("Everyone")
    assert grp
    members = user_group_members_repo()
    if grp["id"] not in set(members.list_groups_for_user(user_id)):
        members.add_member(user_id, grp["id"], source="system_seed")
    grants = resource_grants_repo()
    if not grants.has_grant([grp["id"]], "collection", corpus_id):
        grants.create(group_id=grp["id"], resource_type="collection", resource_id=corpus_id, assigned_by="test")


def test_owner_is_offered_edit_and_delete(seeded_app, tmp_path):
    """Managing a collection was CLI-only: nothing on this page could rename
    it, and nothing anywhere in the UI could delete it."""
    doc = tmp_path / "a.csv"
    doc.write_text("a\n")
    corpus_id = _new_corpus("owner-manages")
    _add_file(corpus_id, "a.csv", "csv", str(doc))

    r = seeded_app["client"].get("/library/owner-manages", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    # `aria-controls="lib-edit"` / `data-…>` / `data-delete-file="` are markup
    # only a rendered control carries — the JS selectors are `[data-…]`.
    assert 'aria-controls="lib-edit"' in r.text
    assert "data-delete-collection>" in r.text
    assert 'data-delete-file="' in r.text
    # …and the form the edit action reveals is actually on the page.
    assert 'id="lib-edit-form"' in r.text


def test_the_legacy_theme_offers_the_same_actions_in_the_hero_menu(seeded_app, tmp_path, monkeypatch):
    """Two chromes, ONE action list: the redesign draws the Manage cluster in
    the rail, an explicitly-themed (pre-#896) instance draws the same actions
    in the hero's overflow menu. Neither may be the only one that works."""
    doc = tmp_path / "legacy.csv"
    doc.write_text("a\n")
    corpus_id = _new_corpus("legacy-manages")
    _add_file(corpus_id, "legacy.csv", "csv", str(doc))

    monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
    from app.instance_config import get_instance_theme

    assert get_instance_theme() == "blue", "precondition: the legacy chrome is what we are rendering"

    r = seeded_app["client"].get("/library/legacy-manages", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "data-delete-collection>" in r.text
    assert 'aria-controls="lib-edit"' in r.text
    assert 'id="lib-edit-form"' in r.text


def test_a_reader_with_only_a_grant_is_offered_neither(seeded_app, tmp_path):
    """The API answers 403 for a mere grant-holder, so the page must not show
    controls that could only fail."""
    doc = tmp_path / "b.csv"
    doc.write_text("b\n")
    corpus_id = _new_corpus("reader-cannot-manage")
    _add_file(corpus_id, "b.csv", "csv", str(doc))
    _seed_grant(corpus_id, "analyst1")

    r = seeded_app["client"].get("/library/reader-cannot-manage", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200, r.text
    assert 'aria-controls="lib-edit"' not in r.text
    assert "data-delete-collection>" not in r.text
    assert 'data-delete-file="' not in r.text
    assert 'id="lib-edit-form"' not in r.text


def test_a_source_managed_collection_offers_no_editor(seeded_app, tmp_path):
    """PATCH answers 409 there — its name comes from the source scope — so the
    page offers deletion but not editing, matching what the API accepts."""
    from src.repositories import source_connections_repo

    doc = tmp_path / "c.csv"
    doc.write_text("c\n")
    corpus_id = _new_corpus("source-managed-ui")
    _add_file(corpus_id, "c.csv", "csv", str(doc))
    source_connections_repo().create(
        id="conn-ui-sm",
        name="Corp SharePoint",
        source_type="sharepoint",
        config={"scopes": [{"source_scope_id": "site,a,b", "collection_id": corpus_id}]},
    )

    r = seeded_app["client"].get("/library/source-managed-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'aria-controls="lib-edit"' not in r.text
    assert 'id="lib-edit-form"' not in r.text
    assert 'data-delete-file="' not in r.text
    assert "data-delete-collection>" in r.text


def test_source_managed_collection_acl_snapshot_degrades_clean_on_duckdb(seeded_app, tmp_path):
    """TCRD-296 gap #79: the "In SharePoint, this folder is visible to"
    line reads a PG-only table (`sharepoint_connection_state`). On the
    DuckDB-backed default (`seeded_app`) it must render nothing rather than
    a 500 — the happy path (a real captured snapshot rendering names) lives
    in tests/db_pg/test_sharepoint_acl_snapshot_pg.py."""
    from src.repositories import source_connections_repo

    doc = tmp_path / "d.csv"
    doc.write_text("d\n")
    corpus_id = _new_corpus("source-managed-acl-ui")
    _add_file(corpus_id, "d.csv", "csv", str(doc))
    source_connections_repo().create(
        id="conn-ui-sm-acl",
        name="Corp SharePoint ACL",
        source_type="sharepoint",
        config={
            "scopes": [
                {
                    "source_scope_id": "root",
                    "display_path": "Site / Docs",
                    "drive_id": "drive-1",
                    "collection_id": corpus_id,
                }
            ]
        },
    )

    r = seeded_app["client"].get("/library/source-managed-acl-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "In SharePoint, this folder is visible to" not in r.text


def test_file_page_offers_delete_to_its_owner(seeded_app, tmp_path):
    doc = tmp_path / "d.csv"
    doc.write_text("d\n")
    corpus_id = _new_corpus("file-page-delete")
    file_id = _add_file(corpus_id, "d.csv", "csv", str(doc))
    _add_file(corpus_id, "e.csv", "csv", str(doc))  # keep it a multi-file collection

    r = seeded_app["client"].get(f"/library/file-page-delete/f/{file_id}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert "data-delete-file>" in r.text


def test_file_page_offers_delete_under_the_legacy_chrome_too(seeded_app, tmp_path, monkeypatch):
    """`detail.hero` drops its menu outside the redesign, so the overflow entry
    alone would leave a legacy-themed instance with no control."""
    doc = tmp_path / "f.csv"
    doc.write_text("f\n")
    corpus_id = _new_corpus("legacy-file-delete")
    file_id = _add_file(corpus_id, "f.csv", "csv", str(doc))
    _add_file(corpus_id, "g.csv", "csv", str(doc))

    monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
    r = seeded_app["client"].get(f"/library/legacy-file-delete/f/{file_id}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert "data-delete-file>" in r.text


def test_file_page_hides_delete_from_a_non_owner(seeded_app, tmp_path):
    doc = tmp_path / "h.csv"
    doc.write_text("h\n")
    corpus_id = _new_corpus("file-page-no-delete")
    file_id = _add_file(corpus_id, "h.csv", "csv", str(doc))
    _add_file(corpus_id, "i.csv", "csv", str(doc))
    _seed_grant(corpus_id, "analyst1")

    r = seeded_app["client"].get(
        f"/library/file-page-no-delete/f/{file_id}", headers=_auth(seeded_app["analyst_token"])
    )
    assert r.status_code == 200, r.text
    assert "data-delete-file>" not in r.text
