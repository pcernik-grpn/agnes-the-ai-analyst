"""Resolving a `document:` source citation to a real, RBAC-safe file link.

`app/chat/sources.py::_document_needles` already peels a citation ref down
to the spellings a filename might have been written as; these tests check
the OTHER half — matching those spellings against real ``corpus_files`` rows
without ever surfacing a file the caller cannot already reach, and refusing
to guess when more than one accessible file shares a name.
"""

from __future__ import annotations

from app.chat.document_links import attach_document_urls, resolve_document_url


def _make_collection(*, owner: str, slug: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(name=slug, slug=slug, description=None, created_by=owner)


def _add_file(corpus_id: str, filename: str) -> str:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256="s",
        file_type="pdf",
        size_bytes=10,
        storage_path=f"/blobs/{filename}",
    )


# ---------------------------------------------------------------------------
# resolve_document_url
# ---------------------------------------------------------------------------


def test_resolves_the_one_accessible_file_matching_the_ref(e2e_env):
    cid = _make_collection(owner="u1", slug="board-docs")
    fid = _add_file(cid, "Q3_Board_Review.pdf")

    url = resolve_document_url("Q3_Board_Review.pdf", {"id": "u1"})

    assert url == f"/library/board-docs/f/{fid}"


def test_a_models_description_tail_still_resolves(e2e_env):
    """Mirrors `_document_needles`'s own peeling: `document: name — gloss`
    carries the model's own description after an em-dash, which is not part
    of the filename."""
    cid = _make_collection(owner="u1", slug="board-docs")
    fid = _add_file(cid, "Q3_Board_Review.pdf")

    url = resolve_document_url("Q3_Board_Review.pdf — the quarterly deck", {"id": "u1"})

    assert url == f"/library/board-docs/f/{fid}"


def test_a_directory_prefixed_ref_still_resolves(e2e_env):
    cid = _make_collection(owner="u1", slug="board-docs")
    fid = _add_file(cid, "Q3_Board_Review.pdf")

    url = resolve_document_url("collections/board-docs/Q3_Board_Review.pdf", {"id": "u1"})

    assert url == f"/library/board-docs/f/{fid}"


def test_no_matching_file_resolves_to_none(e2e_env):
    _make_collection(owner="u1", slug="board-docs")
    _add_file(_make_collection(owner="u1", slug="board-docs-2"), "Unrelated.pdf")

    assert resolve_document_url("Nothing_Like_This.pdf", {"id": "u1"}) is None


def test_ambiguous_match_across_accessible_collections_resolves_to_none(e2e_env):
    """Two distinct files the SAME caller can reach both answer to the same
    citation — resolving to either would be a guess, so neither wins."""
    cid_a = _make_collection(owner="u1", slug="finance-a")
    cid_b = _make_collection(owner="u1", slug="finance-b")
    _add_file(cid_a, "Report.pdf")
    _add_file(cid_b, "Report.pdf")

    assert resolve_document_url("Report.pdf", {"id": "u1"}) is None


def test_an_inaccessible_file_never_resolves(e2e_env):
    """The RBAC case: a file the caller has no grant to, and did not create,
    must never surface — even though it is the only file named that."""
    cid = _make_collection(owner="someone_else", slug="private-docs")
    _add_file(cid, "Q3_Board_Review.pdf")

    assert resolve_document_url("Q3_Board_Review.pdf", {"id": "u1"}) is None


def test_a_per_file_grant_resolves_even_without_collection_access(e2e_env):
    """`library_file_detail` opens a file for `can_parent OR file_granted` —
    a per-file grant reaches the page even when the caller has no access to
    the parent collection at all. Resolution must use the same authorization
    model, or a citation naming that same file would silently stay unlinked
    even though the reader can already open it directly."""
    from app.resource_types import ResourceType
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    cid = _make_collection(owner="someone_else", slug="shared-out-docs")
    fid = _add_file(cid, "Onboarding.pdf")

    # Sanity: without the per-file grant, u2 cannot reach it — same as
    # test_an_inaccessible_file_never_resolves.
    assert resolve_document_url("Onboarding.pdf", {"id": "u2"}) is None

    group = user_groups_repo().create(name="onboarding-readers")
    user_group_members_repo().add_member("u2", group["id"], source="test")
    resource_grants_repo().create(group["id"], ResourceType.CORPUS_FILE.value, fid, assigned_by="test")

    assert resolve_document_url("Onboarding.pdf", {"id": "u2"}) == f"/library/shared-out-docs/f/{fid}"


def test_owning_the_collection_is_enough_without_a_group_grant(e2e_env):
    """`accessible_collection_ids` already treats ownership as access (an
    upload is private to its creator) — resolution rides the same rule."""
    cid = _make_collection(owner="u1", slug="my-upload")
    fid = _add_file(cid, "Notes.md")

    assert resolve_document_url("Notes.md", {"id": "u1"}) == f"/library/my-upload/f/{fid}"


def test_blank_ref_resolves_to_none(e2e_env):
    assert resolve_document_url("", {"id": "u1"}) is None


def test_a_collection_with_no_slug_never_resolves(e2e_env, monkeypatch):
    """Defensive: if a corpus row somehow carries no slug, a URL cannot be
    built from it — resolution must not raise, and must not link."""
    cid = _make_collection(owner="u1", slug="odd-one")
    fid = _add_file(cid, "Weird.pdf")

    from src.repositories.file_corpora import FileCorporaRepository

    real_get = FileCorporaRepository.get

    def _get_without_slug(self, corpus_id):
        row = real_get(self, corpus_id)
        if row and row["id"] == cid:
            row = dict(row)
            row["slug"] = None
        return row

    monkeypatch.setattr(FileCorporaRepository, "get", _get_without_slug)

    assert resolve_document_url("Weird.pdf", {"id": "u1"}) is None
    assert fid  # sanity: the file really was created


# ---------------------------------------------------------------------------
# attach_document_urls
# ---------------------------------------------------------------------------


def test_attach_document_urls_adds_url_only_to_resolved_document_claims(e2e_env):
    cid = _make_collection(owner="u1", slug="attach-test")
    fid = _add_file(cid, "Known.pdf")

    sources = {
        "declared": True,
        "claims": [
            {"kind": "document", "ref": "Known.pdf", "verified": True},
            {"kind": "document", "ref": "Unknown.pdf", "verified": False},
            {"kind": "table", "ref": "orders", "verified": True},
        ],
    }

    attach_document_urls(sources, {"id": "u1"})

    by_ref = {c["ref"]: c for c in sources["claims"]}
    assert by_ref["Known.pdf"]["url"] == f"/library/attach-test/f/{fid}"
    assert "url" not in by_ref["Unknown.pdf"]
    assert "url" not in by_ref["orders"], "resolution is document-only"


def test_attach_document_urls_is_a_noop_without_a_user(e2e_env):
    sources = {"declared": True, "claims": [{"kind": "document", "ref": "Whatever.pdf", "verified": True}]}
    attach_document_urls(sources, None)
    assert "url" not in sources["claims"][0]


def test_a_directory_only_ref_never_resolves(e2e_env):
    """`_document_needles`'s own floor (`_MIN_DERIVED_NEEDLE`) already
    refuses a derived basename this short (`document: docs/` peels to `""`);
    resolution must inherit that refusal rather than working around it by
    falling back to a LIKE scan with no real needle."""
    cid = _make_collection(owner="u1", slug="floor-test")
    _add_file(cid, "a.pdf")

    assert resolve_document_url("docs/", {"id": "u1"}) is None
